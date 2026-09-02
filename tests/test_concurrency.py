"""`bench probe-concurrency` end-to-end against the fake: ticket Harness 06.

The concurrency workstream (methodology v1 §6): a limit probe sweeps k in short
volleys to locate the real per-key cut-off, then the k∈{1,4,8} cells run as
bracketed batches with the same total tokens per cell, re-anchored to the
measured cut-off. Everything is asserted through produced artifacts and the
requests the fake observed.
"""

from __future__ import annotations

import json
import pathlib

from test_run import read_jsonl  # the one-JSONL-file assertion helper

from ocharness.concurrency import PROBE_K_FROM, WEEKS_PER_MONTH

MODEL = "glm-5.3-flash"


def prepare(tmp_path, *dry_extra) -> str:
    from test_dry_run import run_cli, with_pricing

    pricing = with_pricing(tmp_path)
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T1",
            "--reps",
            "1",
            "--pricing-dir",
            pricing,
            *dry_extra,
        )[0]
        == 0
    )
    return pricing


def probe_cli(tmp_path, *extra) -> tuple[int, str, str]:
    from test_dry_run import run_cli

    return run_cli(
        tmp_path, "probe-concurrency", "--settle-s", "2", "--settle-poll-s", "0.01", *extra
    )


def with_cutoff(fake, limit: int | None) -> None:
    """Scripts the fake's per-key limit; overlap needs held slots (latency > 0)."""
    fake.concurrency_limit = limit
    fake.chat_latency = 0.05


def chats(fake) -> list[dict]:
    return [c for c in fake.calls if c["path"] == "/api/chat"]


def reads(fake) -> list[dict]:
    return [c for c in fake.calls if c["path"] == "/api/usage"]


def probe_lines(tmp_path) -> list[dict]:
    files = sorted((pathlib.Path(tmp_path) / "runs").glob("probe-*.jsonl"))
    assert len(files) == 1, f"expected exactly one probe dataset, got {files}"
    return [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]


def summary(tmp_path) -> dict:
    files = sorted((pathlib.Path(tmp_path) / "runs").glob("concurrency-*.json"))
    assert len(files) == 1, f"expected exactly one concurrency summary, got {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))


def usd_per_pp(ancla: float) -> float:
    return (ancla / WEEKS_PER_MONTH) / 100.0


def test_probe_refuses_without_a_dry_run_mark(tmp_path, fake_cli):
    prepare(tmp_path)
    pathlib.Path(tmp_path, "runs", "gate-T1.json").unlink()  # the mark is gone
    code, _out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 2
    assert "dry-run" in err
    assert chats(fake_cli) == []  # nothing was billed


def test_probe_runs_on_the_t1_anchor_only(tmp_path, fake_cli):
    prepare(tmp_path)
    code, _out, err = probe_cli(tmp_path, "--model", MODEL, "--level", "T2")
    assert code == 2
    assert "T1 anchor" in err
    assert fake_cli.calls == []


def test_probe_requires_a_table_model(tmp_path, fake_cli):
    prepare(tmp_path)
    code, _out, err = probe_cli(tmp_path)
    assert code == 2 and "--model" in err
    code, _out, err = probe_cli(tmp_path, "--model", "no-existe")
    assert code == 2 and "no-existe" in err
    assert fake_cli.calls == []  # refused before any request


def test_k_max_bounds_are_refused(tmp_path, fake_cli):
    prepare(tmp_path)
    code, _out, err = probe_cli(tmp_path, "--model", MODEL, "--k-max", "3")
    assert code == 2 and "--k-max" in err
    assert fake_cli.calls == []
    # the hard ceiling bounds the probe's unbudgeted spend (the gate hole)
    code, _out, err = probe_cli(tmp_path, "--model", MODEL, "--k-max", "65")
    assert code == 2 and "--k-max" in err
    assert fake_cli.calls == []


def test_probe_rejects_flags_it_does_not_read(tmp_path, fake_cli):
    """--k/--reps/--rep/--s are not the probe's knobs: argparse refuses, no silent no-op."""
    prepare(tmp_path)
    for bandera in ("--k", "--reps", "--rep", "--s"):
        code, _out, err = probe_cli(tmp_path, "--model", MODEL, bandera, "8")
        assert code == 2 and "unrecognized" in err, bandera
    assert fake_cli.calls == []  # nothing ran for any refused invocation


def test_errored_volley_is_never_a_cut_off_conclusion(tmp_path, fake_cli):
    """A transient blip that errors the first volley is not a measured sub-floor
    cut-off: the sweep aborts loudly, nothing is pinned, and a healthy resume
    re-probes from the floor."""
    import httpx

    prepare(tmp_path)
    fake_cli.chat_raise = httpx.ConnectError("network blip")
    fake_cli.chat_raise_from = 10 + 1  # the canary's 10 chats open the run
    code, _out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 1
    assert "errored" in err and "Traceback" not in err
    volleys = probe_lines(tmp_path)
    assert len(volleys) == 1 and volleys[0]["errored"] == 4  # the raw evidence stays
    manifiesto = json.loads(
        (pathlib.Path(tmp_path) / "runs" / "manifest-T1-concurrency.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifiesto["probe"]["status"] == "in_flight"  # no cut_off was persisted
    assert "cell_plan" not in manifiesto and manifiesto["batches"] == {}
    # the endpoint recovers: the resume re-probes (a new attempt) and runs the cells
    fake_cli.chat_raise = None
    from test_dry_run import run_cli

    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T1",
            "--reps",
            "1",
            "--pricing-dir",
            str(pathlib.Path(tmp_path) / "pricing"),
        )[0]
        == 0
    )
    code, out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    volleys = probe_lines(tmp_path)
    assert len(volleys) == 18  # attempt 1's errored volley + the full 4..20 re-probe
    doc = summary(tmp_path)
    assert doc["probe"]["cut_off"] == 20
    assert [c["k"] for c in doc["cells"]] == [1, 4, 8]


def test_probe_discovers_the_configured_cut_off_and_cells_re_anchor(tmp_path, fake_cli):
    """limit=6: volleys 4..6 fully accepted, volley 7 rejected once, sweep stops there."""
    with_cutoff(fake_cli, 6)
    prepare(tmp_path)
    code, out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    volleys = probe_lines(tmp_path)
    assert [v["k"] for v in volleys] == [4, 5, 6, 7]  # the sweep stopped at the rejection
    assert [(v["accepted"], v["rejected"], v["errored"]) for v in volleys] == [
        (4, 0, 0),
        (5, 0, 0),
        (6, 0, 0),
        (6, 1, 0),
    ]
    assert all(v["model"] == MODEL and v["workload"] == "probe" for v in volleys)
    doc = summary(tmp_path)
    assert doc["probe"]["cut_off"] == 6
    assert "k=7" in doc["probe"]["cut_off_note"] and "429" in doc["probe"]["cut_off_note"]
    # The probe's rejections are the ticket's 429 evidence, verbatim per request:
    rechazadas = [o for v in volleys for o in v["outcomes"] if o["http"] == 429]
    assert len(rechazadas) == 1 and "429" in rechazadas[0]["err"]
    # probe volleys + three cells + the canary's 10 (which open the run)
    assert len(chats(fake_cli)) == 4 + 5 + 6 + 7 + 3 * 8 + 10


def test_unlimited_key_reaches_the_probe_ceiling_with_cells_1_4_8(tmp_path, fake_cli):
    prepare(tmp_path)
    code, out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    volleys = probe_lines(tmp_path)
    assert [v["k"] for v in volleys] == list(range(PROBE_K_FROM, 20 + 1))  # 4..20, all accepted
    assert all(v["rejected"] == 0 and v["accepted"] == v["requested"] for v in volleys)
    doc = summary(tmp_path)
    assert doc["probe"]["cut_off"] == 20
    assert [c["k"] for c in doc["cells"]] == [1, 4, 8]
    assert all(not c["re_anchored"] for c in doc["cells"])


def test_cut_off_below_the_floor_leaves_only_the_serial_cell(tmp_path, fake_cli):
    with_cutoff(fake_cli, 2)
    prepare(tmp_path)
    code, out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err  # a measured sub-floor cut-off is a finding, not an abort
    volleys = probe_lines(tmp_path)
    assert len(volleys) == 1 and volleys[0]["k"] == 4
    assert (volleys[0]["accepted"], volleys[0]["rejected"]) == (2, 2)
    doc = summary(tmp_path)
    assert doc["probe"]["cut_off"] is None
    assert [c["k"] for c in doc["cells"]] == [1]  # nothing above k=1 is viable
    assert "floor" in doc["probe"]["cut_off_note"]


def test_cells_re_anchor_to_the_measured_cut_off(tmp_path, fake_cli):
    with_cutoff(fake_cli, 6)
    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    doc = summary(tmp_path)
    assert [c["k"] for c in doc["cells"]] == [1, 4, 6]  # k=8 re-anchored to the cut-off
    reanclada = doc["cells"][2]
    assert reanclada["re_anchored"] is True and "re-anchored" in reanclada["notes"]
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    por_k = {b["k"]: b for b in batches}
    assert set(por_k) == {1, 4, 6}
    assert "re-anchored" in por_k[6]["notes"] and por_k[6]["n"] == 8


def test_cells_carry_the_same_total_tokens_and_their_own_k(tmp_path, fake_cli):
    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(requests) == 24 and len(batches) == 3
    por_celda: dict[int, list[dict]] = {}
    for r in requests:
        assert r["workload"] == "concurrency" and r["level"] == "T1"
        por_celda.setdefault(r["k"], []).append(r)
    assert sorted(por_celda) == [1, 4, 8]
    hashes = set()
    for k, lineas in sorted(por_celda.items()):
        assert len(lineas) == 8
        assert all(r["k"] == k for r in lineas)  # the dataset carries the k field
        assert {r["seed"] for r in lineas} == {r["seed"] for r in por_celda[1]}
        assert {r["fixture_hash"] for r in lineas} == {
            r["fixture_hash"] for r in por_celda[1]
        }  # the same fixture: the same total tokens per cell
        assert all(r["tok_in"] == 26 and r["tok_out"] == 12 for r in lineas)
        hashes |= {r["fixture_hash"]}
    assert len(hashes) == 1
    # dp vs k: identical billed load, so the cell deltas agree within a tick
    dpps = {b["k"]: b["dpp_session"] for b in batches}
    assert all(abs(dpps[k] - 0.8) <= 0.1 for k in dpps), dpps


def test_wall_clock_records_serialization_vs_parallelism(tmp_path, fake_cli):
    with_cutoff(fake_cli, 8)
    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    batches = {b["k"]: b for b in read_jsonl(tmp_path, "batches", "batches-*.jsonl")}
    assert all(b["wall_clock_s"] is not None for b in batches.values())
    # 8 serial one-word requests take several times the single parallel wave
    assert batches[1]["wall_clock_s"] > 2 * batches[8]["wall_clock_s"], batches
    assert all(b["count_check_s"] is not None for b in batches.values())


def test_verdict_computes_effective_cost_per_task_from_raw_with_the_anchor(tmp_path, fake_cli):
    fake_cli.reply_for = lambda prompt: "OK"  # the calibration contract: every task completes
    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL, "--ancla", "100")[0] == 0
    doc = summary(tmp_path)
    assert doc["ancla"] == 100.0
    assert abs(doc["usd_per_pp"] - usd_per_pp(100)) < 1e-12
    batches = {b["k"]: b for b in read_jsonl(tmp_path, "batches", "batches-*.jsonl")}
    for celda in doc["cells"]:
        b = batches[celda["k"]]
        esperado = b["dpp_weekly"] * usd_per_pp(100) / celda["n"]
        assert abs(celda["cost_per_attempted_task_usd"] - esperado) < 1e-9
        # the fake replies OK, so every task completes: the primary unit matches
        assert celda["completed"] == 8
        assert abs(celda["cost_per_completed_task_usd"] - esperado) < 1e-9
    assert doc["cells"][0]["cost_per_attempted_task_usd"] > 0


def test_verdict_flags_uncompleted_tasks_instead_of_dividing_by_zero(tmp_path, fake_cli):
    """The fake's default reply grades fail: completed = 0 must not crash the verdict."""
    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    doc = summary(tmp_path)
    assert all(c["completed"] == 0 for c in doc["cells"])
    assert all(c["cost_per_completed_task_usd"] is None for c in doc["cells"])
    assert all(c["cost_per_attempted_task_usd"] > 0 for c in doc["cells"])


def test_probe_spend_is_flushed_before_the_first_cell_bracket(tmp_path, fake_cli):
    """The probe's unbracketed spend must land before cell k=1's pre-read."""
    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    batches = sorted(read_jsonl(tmp_path, "batches", "batches-*.jsonl"), key=lambda b: b["k"])
    # 3 cells x 8 requests x 1 tick = 0.8 pp each; a contaminated bracket would
    # carry the probe's 17 volleys (204 ticks = 20.4 pp) inside the first one
    assert all(abs(b["dpp_session"] - 0.8) <= 0.1 for b in batches)
    assert all(b["notes"] == "" for b in batches)


def test_raw_lines_honor_the_schemas_and_leak_no_key(tmp_path, fake_cli):
    from ocharness.schema import (
        validate_batch_line,
        validate_probe_line,
        validate_request_line,
    )

    with_cutoff(fake_cli, 6)
    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    for v in probe_lines(tmp_path):
        validate_probe_line(v)
    for r in read_jsonl(tmp_path, "runs", "requests-*.jsonl"):
        validate_request_line(r)
    for b in read_jsonl(tmp_path, "batches", "batches-*.jsonl"):
        validate_batch_line(b)
    doc = summary(tmp_path)
    assert doc["protocol_version"] and doc["table_version"] == "2026-08-31"
    for carpeta in ("runs", "batches"):
        for ruta in (pathlib.Path(tmp_path) / carpeta).iterdir():
            assert "test-key" not in ruta.read_text(encoding="utf-8"), ruta.name


def test_abort_inside_a_cell_keeps_the_billed_evidence(tmp_path, fake_cli):
    """A failure mid-cells aborts loudly; earlier brackets and requests stay raw."""
    import httpx

    with_cutoff(fake_cli, 4)  # cut-off 4 -> cells {1, 4}
    prepare(tmp_path)
    # reads: flush, k=1 pre/count/post, k=4 pre, then the k=4 count check dies
    fake_cli.usage_raise = httpx.ConnectError("meter dropped")
    # canary (7 reads) + flush (3) + the k=1 cell's 4 + the k=4 pre-read:
    # the k=4 cell's COUNT check is the read that dies
    fake_cli.usage_raise_from = 16
    code, _out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 1
    assert "meter read failed" in err and "Traceback" not in err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert len(requests) == 16 and all(r["k"] in (1, 4) for r in requests)  # billed, kept
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 2  # the k=1 bracket closed done; the k=4 one closed aborted
    estados = sorted((b["k"], "done" if not b["notes"] else "aborted") for b in batches)
    assert estados == [(1, "done"), (4, "aborted")]
    manifiesto = json.loads(
        (pathlib.Path(tmp_path) / "runs" / "manifest-T1-concurrency.json").read_text(
            encoding="utf-8"
        )
    )
    entry_status = [e["status"] for e in manifiesto["batches"].values()]
    assert entry_status.count("aborted") == 1 and entry_status.count("done") == 1


def test_resume_never_reprobes_and_skips_done_cells(tmp_path, fake_cli):
    from test_dry_run import run_cli

    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    antes_chats, antes_reads = len(chats(fake_cli)), len(reads(fake_cli))
    ruta = pathlib.Path(tmp_path) / "runs" / "manifest-T1-concurrency.json"
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    victima = next(iter(manifiesto["batches"]))
    manifiesto["batches"][victima]["status"] = "in_flight"  # a crash mid-cell
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T1",
            "--reps",
            "1",
            "--pricing-dir",
            str(pathlib.Path(tmp_path) / "pricing"),
        )[0]
        == 0
    )
    code, out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 0, out or err
    assert len(chats(fake_cli)) == antes_chats  # no new volleys, no re-run cell
    assert len(reads(fake_cli)) == antes_reads
    assert "in_flight" in (out + err)  # skipped loudly, never silently retried
    doc = summary(tmp_path)
    assert [c["k"] for c in doc["cells"]] == [1, 4, 8]  # the raw evidence is unchanged


def test_json_stdout_is_the_summary_doc_alone(tmp_path, fake_cli):
    prepare(tmp_path)
    code, out, err = probe_cli(tmp_path, "--model", MODEL, "--json")
    assert code == 0, out or err
    doc = json.loads(out)  # progress went to stderr; stdout parses alone
    assert doc["kind"] == "concurrency" and doc["models"] == [MODEL]
    assert doc["probe"]["cut_off"] == 20 and len(doc["cells"]) == 3
    assert err  # the progress lines


def test_status_shows_the_concurrency_run(tmp_path, fake_cli):
    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    from test_dry_run import run_cli

    code, out, _err = run_cli(tmp_path, "status", "--level", "T1-concurrency")
    assert code == 0
    assert "3 done" in out and "concurrency" in out


def test_failed_sibling_does_not_void_the_cell_verdicts(tmp_path, fake_cli):
    """A request the endpoint rejected mid-cell is the measured phenomenon:
    the responses that landed still complete (the concurrency judge's own rule)."""
    fake_cli.reply_for = lambda prompt: "OK"
    prepare(tmp_path)
    # the k=1 cell's first request: the canary's 10 chats + 17 volleys = 214
    fake_cli.fails_on = 204 + 1 + 10
    code, _out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 0, err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    fallidas = [r for r in requests if r["http"] != 200]
    assert len(fallidas) == 1 and fallidas[0]["k"] == 1
    veredictos = [r["checker"] for r in requests]
    assert veredictos.count("pass") == 23 and veredictos.count(None) == 1  # the good ones pass
    doc = summary(tmp_path)
    por_k = {c["k"]: c for c in doc["cells"]}
    assert por_k[1]["completed"] == 7  # not voided to 0 by the rejected sibling
    batches = {b["k"]: b for b in read_jsonl(tmp_path, "batches", "batches-*.jsonl")}
    esperado = batches[1]["dpp_weekly"] * usd_per_pp(100) / 7
    assert abs(por_k[1]["cost_per_completed_task_usd"] - esperado) < 1e-9
    assert por_k[4]["completed"] == 8 and por_k[8]["completed"] == 8


def test_second_model_reuses_the_probe_without_a_flush(tmp_path, fake_cli):
    """One manifest per level: the second model's cells skip the 90 s settle and
    the flush read (some cell already closed, so the flush already ran), and the
    summary covers the run's models without claiming to be one model's."""
    from test_dry_run import run_cli

    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    antes = len(reads(fake_cli))
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T1",
            "--reps",
            "1",
            "--pricing-dir",
            str(pathlib.Path(tmp_path) / "pricing"),
        )[0]
        == 0
    )
    code, out, err = probe_cli(tmp_path, "--model", "kimi-k3")
    assert code == 0, out or err
    # 3 cells x 4 bracket reads (pre + count + 2 polls), NO flush read; the
    # second model's billing canary re-runs (7 reads, 10 chats): the lane is
    # never inherited across models - MODEL's proof says nothing about kimi-k3's.
    assert len(reads(fake_cli)) == antes + 12 + 7
    assert "reusing cut-off" in (out + err)  # the probe was reused, loudly
    doc = summary(tmp_path)
    assert doc["models"] == sorted([MODEL, "kimi-k3"])  # the run's doc, not one model's
    assert [(c["model"], c["k"]) for c in doc["cells"]] == [
        (MODEL, 1),
        (MODEL, 4),
        (MODEL, 8),
        ("kimi-k3", 1),
        ("kimi-k3", 4),
        ("kimi-k3", 8),
    ]


def test_cell_plan_drift_is_refused(tmp_path, fake_cli):
    """A harness change to CELL_KS/CELL_REQUESTS between invocations must not
    append a mixed plan under one run_id (the k-drift guard's own hole)."""
    from test_dry_run import run_cli

    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    ruta = pathlib.Path(tmp_path) / "runs" / "manifest-T1-concurrency.json"
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    manifiesto["cell_plan"] = {"ks": [1, 4, 8], "n": 12}  # as if the harness changed
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T1",
            "--reps",
            "1",
            "--pricing-dir",
            str(pathlib.Path(tmp_path) / "pricing"),
        )[0]
        == 0
    )
    antes = len(chats(fake_cli))
    code, _out, err = probe_cli(tmp_path, "--model", MODEL)
    assert code == 1 and "cell plan" in err and "Traceback" not in err
    assert len(chats(fake_cli)) == antes  # refused before any request


def test_human_report_survives_an_aborted_cell_in_the_summary(tmp_path, fake_cli):
    """A resumed run whose summary carries an aborted cell (null dpp/costs) must
    not crash the non-json report with a None format."""
    import httpx
    from test_dry_run import run_cli

    prepare(tmp_path)
    fake_cli.usage_raise = httpx.ConnectError("meter dropped")
    fake_cli.usage_raise_from = 6 + 10  # the canary's chats shift the ordinal
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 1
    fake_cli.usage_raise = None  # the meter recovered for the resume
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T1",
            "--reps",
            "1",
            "--pricing-dir",
            str(pathlib.Path(tmp_path) / "pricing"),
        )[0]
        == 0
    )
    code, out, err = probe_cli(tmp_path, "--model", MODEL)  # resume: k=8 runs, k=4 skipped
    assert code == 0, err
    assert "n/a" in out  # the aborted cell's unmeasurable metrics render, no TypeError


def test_the_k_cells_run_under_a_proven_lane(tmp_path, fake_cli):
    """The k-cells are measured brackets: the workstream runs the billing canary
    (once per run, before the probe) and every cell request carries its nonce —
    the same gate `bench run` enforces."""
    prepare(tmp_path)
    assert probe_cli(tmp_path, "--model", MODEL)[0] == 0
    canarios = [
        json.loads(l)
        for l in (pathlib.Path(tmp_path, "runs").glob("canary-*.jsonl"))
        .__iter__()
        .__next__()
        .read_text()
        .splitlines()
        if l.strip()
    ]
    assert len(canarios) == 1 and canarios[0]["alarm"] is False
    assert canarios[0]["model"] == MODEL
    # The cells' requests are salted; the probe's volleys are exempt.
    lineas = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert all(r["nonce_sha256"] for r in lineas)  # measured cells: salted
    assert all(r["prompt_sha256"] for r in lineas)
    manifiesto = json.loads(
        pathlib.Path(tmp_path, "runs", "manifest-T1-concurrency.json").read_text(encoding="utf-8")
    )
    assert manifiesto["lane"]["mode"] == "cache-free"
    assert manifiesto["canary"]["status"] == "ok"
