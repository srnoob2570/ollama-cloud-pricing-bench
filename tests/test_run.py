"""`bench run --level T1` end-to-end against the fake: ticket Harness 02.

The bracketed-batch protocol (meter read -> burst -> count check -> settle -> read)
is asserted only through produced artifacts and the requests the fake observed.
"""

from __future__ import annotations

import hashlib
import json

import httpx
from test_dry_run import run_cli, with_pricing

from ocharness.schema import validate_batch_line, validate_request_line


def prepare(tmp_path, *dry_extra) -> str:
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


def run_t1(tmp_path, *extra) -> tuple[int, str, str]:
    return run_cli(tmp_path, "run", "--level", "T1", "--settle-s", "0", *extra)


def consumer_calls(fake) -> list[dict]:
    """Calls that consume or meter quota (preflight's /v1/models read excluded)."""
    return [c for c in fake.calls if c["path"] != "/v1/models"]


def read_jsonl(tmp_path, dirname, pattern) -> list[dict]:
    files = sorted((tmp_path / dirname).glob(pattern))
    assert len(files) == 1, f"expected exactly one {pattern} in {dirname}/"
    return [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]


def test_full_t1_run_produces_the_raw_dataset(tmp_path, fake_cli):
    prepare(tmp_path)
    code, out, err = run_t1(tmp_path, "--reps", "1")
    assert code == 0, out or err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    # 19 models x (20 qa_short + 3 calibration + 1 throughput) requests, one rep
    assert len(requests) == 19 * 24
    assert len(batches) == 19 * 3
    # No warmup, no retry: the fake saw EXACTLY the planned chats plus meter reads.
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    reads = [c for c in fake_cli.calls if c["path"] == "/api/usage"]
    assert len(chats) == 19 * 24
    assert len(reads) == 57 * 3  # pre + count check + post per batch
    assert all(c["auth"] for c in chats + reads)


def test_batch_dpp_matches_scripted_consumption_within_a_tick(tmp_path, fake_cli):
    """Each accepted request bills `ticks_per_request` ticks; dpp must agree to 0.1 pp."""
    fake_cli.ticks_per_request = 2
    prepare(tmp_path)
    code, out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 0, out or err
    batches = {b["workload"]: b for b in read_jsonl(tmp_path, "batches", "batches-*.jsonl")}
    esperado = {"qa_short": 20 * 0.2, "calibration": 3 * 0.2, "throughput": 1 * 0.2}
    for w, esperado_pp in esperado.items():
        assert abs(batches[w]["dpp_session"] - esperado_pp) <= 0.1, w
        assert abs(batches[w]["dpp_weekly"] - esperado_pp) <= 0.1, w


def test_request_count_check_aborts_when_the_fake_drops_a_request(tmp_path, fake_cli):
    fake_cli.undercount_by = 1
    pricing = prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 1
    assert "request_count" in err
    # The aborted batch is the first one; nothing after it ran.
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    assert len(chats) == 20
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 1
    assert "request_count" in batches[0]["notes"]
    # The bracket was still closed: the aborted batch's real spend is attributed to it.
    assert batches[0]["dpp_session"] == 2.0  # 20 accepted requests x 1 tick x 0.1 pp
    validate_batch_line(batches[0])
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    estados = [b["status"] for b in manifiesto["batches"].values()]
    assert estados.count("aborted") == 1 and "done" not in estados

    # Resume: the aborted batch is skipped (its spend is already in the dataset), and
    # the still-pending batches run under the fresh dry-run.
    antes = len(fake_cli.calls)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 0, err
    chats_nuevos = [c for c in fake_cli.calls[antes:] if c["path"] == "/api/chat"]
    assert len(chats_nuevos) == 4  # calibration (3) + throughput (1); qa_short NOT retried
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    estados = [b["status"] for b in manifiesto["batches"].values()]
    assert estados == ["aborted", "done", "done"]


def test_failed_request_stays_failed_without_retry(tmp_path, fake_cli):
    fake_cli.fails_on = 5  # request 5 of the first batch gets a scripted 500
    prepare(tmp_path)
    code, out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 0, out or err  # a failed request is recorded, not retried
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    assert len(chats) == 24  # exactly the planned requests, no resend
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    fallidas = [r for r in requests if r["http"] != 200]
    assert len(fallidas) == 1
    assert fallidas[0]["http"] == 500 and fallidas[0]["err"]
    assert fallidas[0]["tok_in"] is None and fallidas[0]["api"] is None


def test_raw_lines_honor_the_agreed_schema(tmp_path, fake_cli):
    prepare(tmp_path)
    code, out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 0, out or err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    for r in requests:
        validate_request_line(r)
    for b in batches:
        validate_batch_line(b)
    ok = next(r for r in requests if r["http"] == 200)
    assert ok["tok_in"] == 26 and ok["tok_out"] == 12 and ok["tok_cached"] is None
    assert ok["api"]["done"] is True and ok["api"]["done_reason"] == "stop"  # verbatim
    assert ok["t_first_chunk"] is not None and ok["chunks"] == 3
    assert ok["k"] == 1 and ok["rep"] == 1 and isinstance(ok["seed"], int)
    # Harness 03: the checker is real. The default fake replies "world", which
    # matches no answer-key entry -> the graded verdict is fail, not a placeholder.
    assert ok["checker"] == "fail"
    assert ok["out_text_hash"] == hashlib.sha256(b"world").hexdigest()
    assert ok["table_version"] == "2026-08-31" and ok["protocol_version"]
    batch = batches[0]
    for campo in ("medidor_pre", "medidor_post"):
        assert isinstance(batch[campo]["limits"]["session"]["usage"], float)  # full raw payload
    assert batch["settle_s"] == 0.0 and batch["n"] == 20
    assert batch["request_counts"]["post"]["glm-5.3-flash"] == 20


def test_resume_skips_completed_batches_without_new_requests(tmp_path, fake_cli):
    pricing = prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    antes = len(consumer_calls(fake_cli))
    # A new invocation needs a fresh dry-run (the gate); the manifest carries the run.
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 0, out or err
    assert len(consumer_calls(fake_cli)) == antes  # zero new requests: every batch was done


def test_in_flight_batch_is_never_silently_retried(tmp_path, fake_cli):
    pricing = prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")[0] == 0
    antes = len(consumer_calls(fake_cli))
    ruta = tmp_path / "runs" / "manifest-T1.json"
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    victima = next(iter(manifiesto["batches"]))
    manifiesto["batches"][victima]["status"] = "in_flight"  # simulate a crash mid-batch
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 0, out or err
    assert len(consumer_calls(fake_cli)) == antes  # skipped, never re-requested
    assert "in_flight" in (out + err)  # ...and reported, not silent


def test_run_validates_its_parameters(tmp_path, fake_cli):
    pricing = prepare(tmp_path)
    assert run_t1(tmp_path, "--k", "0")[0] == 2
    assert run_t1(tmp_path, "--settle-s", "-1")[0] == 2
    assert run_t1(tmp_path, "--settle-s", "nan")[0] == 2
    assert run_t1(tmp_path, "--settle-s", "inf")[0] == 2
    assert run_t1(tmp_path, "--rep", "9", "--reps", "1")[0] == 2
    assert run_t1(tmp_path, "--model", "no-existe")[0] == 2
    assert fake_cli.calls == []  # nothing ran for any invalid invocation
    assert pricing  # pricing dir used above


def test_gate_binds_the_run_density_to_the_approved_estimate(tmp_path, fake_cli):
    """The run may never bill more than the dry-run approved: --reps is bound by the gate."""
    prepare(tmp_path)  # dry-run approved --reps 1
    code, _out, err = run_t1(tmp_path, "--reps", "5")
    assert code == 2
    assert "repetitions" in err
    assert fake_cli.calls == []  # refused before any request


def test_json_run_prints_pure_json_on_stdout(tmp_path, fake_cli):
    prepare(tmp_path)
    codigo, salida, errores = run_cli(
        tmp_path, "run", "--level", "T1", "--reps", "1", "--settle-s", "0", "--json"
    )
    assert codigo == 0, salida or errores
    doc = json.loads(salida)  # progress lines live on stderr; stdout parses
    assert doc["batches_done"] == 57 and doc["requests_written"] == 19 * 24
    assert errores  # the progress went to stderr


def test_corrupt_manifest_is_a_clean_error(tmp_path, fake_cli):
    prepare(tmp_path)
    (tmp_path / "runs" / "manifest-T1.json").write_text("{corrupt", encoding="utf-8")
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    assert "manifest" in err and "corrupt" in err
    assert "Traceback" not in err


def test_manifest_refuses_table_drift_mid_run(tmp_path, fake_cli):
    from conftest import standard_table, write_table

    prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    write_table(tmp_path / "pricing", "2026-09-01", standard_table())
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T1",
            "--reps",
            "1",
            "--pricing-dir",
            str(tmp_path / "pricing"),
            "--table-version",
            "2026-09-01",
        )[0]
        == 0
    )
    code, _out, err = run_t1(
        tmp_path, "--model", "glm-5.3-flash", "--reps", "1", "--table-version", "2026-09-01"
    )
    assert code == 1
    assert "table" in err


def test_manifest_refuses_k_drift_mid_run(tmp_path, fake_cli):
    pricing = prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1", "--k", "4")
    assert code == 1
    assert "k=" in err


def test_truncated_stream_is_flagged_not_counted_as_clean(tmp_path, fake_cli):
    """Billed-but-token-less: a 200 without a done frame carries err, never silence."""
    fake_cli.truncate_stream = True
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 0, err  # the request was billed: recorded, not retried
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert all("truncated" in r["err"] for r in requests)
    assert all(r["tok_in"] is None and r["api"] is None for r in requests)
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    assert len(chats) == 24  # the meter counted them; the count check passed


def test_recorded_seed_is_transmitted_to_the_api(tmp_path, fake_cli):
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 0, err
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    semillas = {c["body"]["options"]["seed"] for c in chats}
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert {r["seed"] for r in requests} == semillas  # what was recorded is what was sent


def test_fully_rejected_burst_aborts_instead_of_completing(tmp_path, fake_cli):
    """ok == 0 measures nothing: aborted loudly (nothing billed), never a silent done."""
    fake_cli.reject_all = True
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    assert "0 of 20 requests accepted" in err and "nothing was billed" in err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert len(requests) == 20 and all(r["http"] == 429 for r in requests)
    assert not list((tmp_path / "batches").glob("batches-*.jsonl"))  # no bracket: zero spend
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    assert all(e["status"] == "aborted" for e in manifiesto["batches"].values())


def test_requests_use_the_matched_catalog_id(tmp_path, fake_cli):
    """The wire carries the catalog's tagged id; the dataset records the slate id."""
    catalogo = full_catalog_less("nemotron-3-nano") + ["nemotron-3-nano:30b"]
    fake_cli.catalog = sorted(catalogo)
    prepare(tmp_path)
    code, out, err = run_t1(tmp_path, "--model", "nemotron-3-nano", "--reps", "1")
    assert code == 0, out or err
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    assert {c["body"]["model"] for c in chats} == {"nemotron-3-nano:30b"}
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert {r["model"] for r in requests} == {"nemotron-3-nano"}  # the study's unit
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 3 and all(b["notes"] == "" for b in batches)  # clean cells
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    assert manifiesto["catalog"][-1]["matched"]["nemotron-3-nano"] == "nemotron-3-nano:30b"


def full_catalog_less(modelo: str) -> list[str]:
    from conftest import standard_table

    return [m for m in sorted(standard_table()) if m != modelo]


def test_meter_read_failure_mid_batch_closes_the_bracket_cleanly(tmp_path, fake_cli):
    """A meter blip after billing is a clean abort with the spend attributed, not a crash."""
    fake_cli.usage_raise = httpx.ConnectError("meter dropped")
    fake_cli.usage_raise_from = 2  # the pre-read ok; the count-check read dies
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    assert "meter read failed (ConnectError" in err and "Traceback" not in err
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")  # the bracket closed
    assert len(batches) == 1 and "meter read failed" in batches[0]["notes"]
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    entrada = next(iter(manifiesto["batches"].values()))
    assert entrada["status"] == "aborted" and entrada["requests_ok"] == 20
    # the 20 billed requests are in the dataset
    assert len(read_jsonl(tmp_path, "runs", "requests-*.jsonl")) == 20


def test_pre_batch_meter_failure_aborts_before_any_request(tmp_path, fake_cli):
    fake_cli.usage_raise = httpx.ConnectError("meter dropped")
    fake_cli.usage_raise_from = 1  # the very first read fails
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    assert "before the batch" in err and "Traceback" not in err
    assert not [c for c in fake_cli.calls if c["path"] == "/api/chat"]  # nothing was spent
    assert not list((tmp_path / "batches").glob("batches-*.jsonl"))


def test_settled_read_failure_still_records_the_batchs_spend(tmp_path, fake_cli):
    """The post-settle read failing keeps the bracket: requests_ok, no dpp."""
    fake_cli.usage_raise = httpx.ConnectError("meter dropped")
    fake_cli.usage_raise_from = 3  # pre-read and count check ok; the post read dies
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1 and "after the settle" in err and "Traceback" not in err
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 1 and batches[0]["medidor_post"] is None
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    entrada = next(iter(manifiesto["batches"].values()))
    assert entrada["status"] == "aborted" and entrada["requests_ok"] == 20
    assert entrada["rep"] == 1 and entrada["dpp_session"] is None


def test_checker_failure_keeps_the_billed_evidence(tmp_path, fake_cli, monkeypatch):
    """Checker drift stops the run loudly but never discards billed requests."""
    import ocharness.checkers as checkers_mod
    from ocharness.checkers import CheckersError

    prepare(tmp_path)

    def _boom(*_a, **_k):
        raise CheckersError("scripted checker drift")

    monkeypatch.setattr(checkers_mod, "judge", _boom)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    assert "checker failure" in err and "Traceback" not in err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert len(requests) == 20 and all(r["checker"] is None for r in requests)  # billed, kept
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 1 and "checker failure" in batches[0]["notes"]
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    assert all(e["status"] == "aborted" for e in manifiesto["batches"].values())


def test_run_t2_t3_are_not_implemented_yet(tmp_path, fake_cli):
    prepare(tmp_path)
    assert (
        run_cli(
            tmp_path,
            "dry-run",
            "--level",
            "T2",
            "--reps",
            "1",
            "--pricing-dir",
            str(tmp_path / "pricing"),
        )[0]
        == 0
    )
    code, _, err = run_cli(tmp_path, "run", "--level", "T2", "--reps", "1", "--settle-s", "0")
    assert code == 3 and "only implemented for T1" in err
