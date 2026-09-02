"""`bench run --level T1` end-to-end against the fake: ticket Harness 02.

The bracketed-batch protocol (meter read -> burst -> count check -> registration
settle -> post) is asserted only through produced artifacts and the requests the
fake observed. Protocol v3 adds the cache-free lane (every measured request
carries its run-scoped nonce), the billing canary (once per run, before the
first bracket: 5 salted + 5 identical-prefix replays), and the registration
settle (poll until two consecutive reads agree in both windows, capped).
"""

from __future__ import annotations

import hashlib
import json

import httpx
from test_dry_run import run_cli, with_pricing

from ocharness.schema import validate_batch_line, validate_request_line

# The fake's lag_reads = 2: a bracket's registration converges in 2 polls (the
# count-check read, then a moving read, then a confirming one) -> 4 meter reads
# per bracket (pre + count check + 2 polls). The canary spends 10 chats and 7
# reads (4 salted-bracket + 3 replay-bracket: the replays bill nothing, so its
# replay settle confirms at the first poll).
CANARY_CHATS = 10
CANARY_READS = 7
READS_PER_BATCH = 4  # pre + count check + 2 registration polls (lag_reads = 2)
SETTLE = ("--settle-s", "2", "--settle-poll-s", "0.01")  # fast, converging registration


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
    return run_cli(tmp_path, "run", "--level", "T1", *SETTLE, *extra)


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
    # No warmup, no retry: the fake saw EXACTLY the planned chats (plus the
    # canary's 10, which open the run) plus meter reads.
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    reads = [c for c in fake_cli.calls if c["path"] == "/api/usage"]
    assert len(chats) == 19 * 24 + CANARY_CHATS
    assert len(reads) == 57 * READS_PER_BATCH + CANARY_READS
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
    # A dropped bill inside the batch window: the canary's 10 chats open the
    # run, so the batch's 5th request is chat #15 — accepted, billed, uncounted.
    fake_cli.undercount_at = CANARY_CHATS + 5
    pricing = prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 1
    assert "request_count" in err
    # The aborted batch is the first one (the canary's 10 chats opened the run);
    # nothing after it ran.
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    assert len(chats) == 20 + CANARY_CHATS
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 1
    assert "request_count" in batches[0]["notes"]
    # The bracket was still closed: the aborted batch's real spend is attributed to it.
    # 20 accepted requests x 1 tick x 0.1 pp: the raw payloads' exact float,
    # with the tick count still pinned as an independent magnitude anchor
    assert (
        batches[0]["dpp_session"]
        == (
            batches[0]["medidor_post"]["limits"]["session"]["usage"]
            - batches[0]["medidor_pre"]["limits"]["session"]["usage"]
        )
        * 100
    )
    assert 1.9 <= batches[0]["dpp_session"] <= 2.1
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
    # The canary's 10 chats open the run: the first batch's request 5 is chat #15.
    fake_cli.fails_on = CANARY_CHATS + 5  # request 5 of the first batch gets a scripted 500
    prepare(tmp_path)
    code, out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 0, out or err  # a failed request is recorded, not retried
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    assert len(chats) == 24 + CANARY_CHATS  # exactly the planned requests + the canary
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
    assert batch["settle_s"] == 2.0 and batch["n"] == 20
    # Protocol v3's registration settle, stamped on the line: the mode, the poll
    # count, the exit reason and both windows' registration times.
    assert batch["settle_mode"] == "registration"
    assert batch["settle_reads"] == 2 and batch["settle_exit"] == "stable"
    assert batch["registered_session_s"] is not None
    assert batch["registered_weekly_s"] is not None
    # The post payload is the meter's CUMULATIVE counter: this batch's delta is
    # 20 (the canary's 10 same-model chats ride in the baseline).
    assert (
        batch["request_counts"]["post"]["glm-5.3-flash"]
        - batch["request_counts"]["pre"].get("glm-5.3-flash", 0)
        == 20
    )


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
        tmp_path,
        "run",
        "--level",
        "T1",
        "--reps",
        "1",
        "--settle-s",
        "2",
        "--settle-poll-s",
        "0.01",
        "--json",
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


def test_manifest_refuses_composition_drift_mid_run(tmp_path, fake_cli):
    """A pre-hybrid manifest (per-rep brackets) never resumes under the hybrid
    plan: the batch ids would collide — a 5-rep pooled bracket read as done on
    a rep-1 id — billing nothing and measuring less than the plan says."""
    pricing = prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    del manifiesto["composition"]  # a pre-hybrid manifest carries none
    (tmp_path / "runs" / "manifest-T1.json").write_text(json.dumps(manifiesto), encoding="utf-8")
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    assert "composition" in err


def test_truncated_stream_is_flagged_not_counted_as_clean(tmp_path, fake_cli):
    """Billed-but-token-less: a 200 without a done frame carries err, never silence."""
    fake_cli.truncate_stream = True
    fake_cli.truncate_from = CANARY_CHATS + 1  # the canary's chats stay healthy
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 0, err  # the request was billed: recorded, not retried
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert all("truncated" in r["err"] for r in requests)
    assert all(r["tok_in"] is None and r["api"] is None for r in requests)
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    assert len(chats) == 24 + CANARY_CHATS  # the meter counted them; the count check passed


def test_recorded_seed_is_transmitted_to_the_api(tmp_path, fake_cli):
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 0, err
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    # The canary's T2-size chats are excluded by size: this pins the cell's seeds.
    celdas = [c for c in chats if len(c["body"]["messages"][0]["content"]) < 10_000]
    semillas = {c["body"]["options"]["seed"] for c in celdas}
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    assert {r["seed"] for r in requests} == semillas  # what was recorded is what was sent


def test_fully_rejected_burst_aborts_instead_of_completing(tmp_path, fake_cli):
    """ok == 0 measures nothing: aborted loudly (nothing billed), never a silent done."""
    fake_cli.reject_all = True
    fake_cli.reject_from = CANARY_CHATS + 1  # the canary runs clean; the brackets reject
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
    # The canary spends 7 reads; this run's batch pre-read is #8, count check #9.
    fake_cli.usage_raise_from = 9  # the pre-read ok; the count-check read dies
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
    fake_cli.usage_raise_from = 1  # the canary's very first meter read fails
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    # The canary opens the run: its pre-read dies before any chat is billed.
    assert "before the salted volley" in err and "Traceback" not in err
    assert not [c for c in fake_cli.calls if c["path"] == "/api/chat"]  # nothing was spent
    assert not list((tmp_path / "batches").glob("batches-*.jsonl"))


def test_settled_read_failure_still_records_the_batchs_spend(tmp_path, fake_cli):
    """The registration read failing keeps the bracket: requests_ok, no dpp."""
    fake_cli.usage_raise = httpx.ConnectError("meter dropped")
    # The canary spends 7 reads; this run's batch: pre #8, count check #9, first
    # registration poll #10 — that is the read that dies.
    fake_cli.usage_raise_from = 10
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1 and "during registration" in err and "Traceback" not in err
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 1 and batches[0]["medidor_post"] is None
    assert batches[0]["settle_exit"] is None and batches[0]["settle_reads"] == 0
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


def test_meter_payload_without_request_counts_aborts_cleanly(tmp_path, fake_cli):
    """A meter payload lacking request_count aborts the batch - never a traceback."""
    prepare(tmp_path)
    original = fake_cli._read_meter

    def payload_sin_counts():
        payload = original()
        for ventana in payload["limits"].values():
            for entrada in ventana["models"]:
                entrada.pop("request_count", None)
        return payload

    fake_cli._read_meter = payload_sin_counts
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 1 and "Traceback" not in err
    assert "request_count" in err  # a clean count-check failure, loudly
    # The cell is recoverable run state (aborted), not a stranded in_flight:
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    estados = [e["status"] for e in manifiesto["batches"].values()]
    assert "in_flight" not in estados and estados.count("aborted") == 1
    # And the bracket closed: the batch's real spend is attributed to it.
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 1 and "request_count" in batches[0]["notes"]


def test_run_refuses_a_manifest_with_a_corrupt_batch_entry(tmp_path, fake_cli):
    """A hand-edited entry must look corrupt to `run`, never 'unseen' (re-billing)."""
    prepare(tmp_path)
    assert (
        run_t1(
            tmp_path,
            "--model",
            "glm-5.3-flash",
            "--reps",
            "1",
            "--settle-s",
            "2",
            "--settle-poll-s",
            "0.01",
        )[0]
        == 0
    )
    ruta = tmp_path / "runs" / "manifest-T1.json"
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    manifiesto["batches"]["broken00000000"] = "not a dict"
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")
    antes = len(fake_cli.calls)
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
        )[0]
        == 0
    )
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1 and "Traceback" not in err
    assert "corrupt" in err  # the promised corrupt-manifest RunnerError, not a crash
    assert [c for c in fake_cli.calls[antes:] if c["path"] == "/api/chat"] == []
    # `status` stays tolerant: it renders the entry as corrupt without crashing.
    code, out, _err = run_cli(tmp_path, "status", "--level", "T1")
    assert code == 0 and "attention: 1 corrupt" in out


# ---- protocol v3: the cache-free lane, the canary, the registration settle ----


def read_canary(tmp_path) -> list[dict]:
    files = sorted((tmp_path / "runs").glob("canary-*.jsonl"))
    assert len(files) == 1, f"expected exactly one canary line, got {files}"
    return [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]


def test_every_measured_request_is_salted_and_the_evidence_persists(tmp_path, fake_cli):
    """The lane: every request carries its run-scoped nonce as the first tokens;
    the line pins both hashes; the fixtures stay untouched; the nonce derives
    from the manifest's spec (regenerable within the run)."""
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 0, err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    # qa_short fires first: request index 2 carries "What is 7 times 8?".
    ok = next(r for r in requests if r["req_id"].endswith("-0002"))
    assert ok["nonce_sha256"] and ok["prompt_sha256"]
    # The sent prompt was the nonce + blank line + the fixture body: the wire
    # shows it, and the recorded hash matches what the fake received.
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    chat = next(
        c
        for c in chats
        if c["body"]["messages"][0]["content"].endswith(
            "What is 7 times 8? Answer in one short sentence."
        )
    )
    enviado = chat["body"]["messages"][0]["content"]
    assert hashlib.sha256(enviado.encode()).hexdigest() == ok["prompt_sha256"]
    nonce, sep, cuerpo = enviado.partition("\n\n")
    assert (
        sep and "\n" not in nonce and cuerpo == "What is 7 times 8? Answer in one short sentence."
    )
    assert hashlib.sha256(nonce.encode()).hexdigest() == ok["nonce_sha256"]
    # The nonce is regenerable from the manifest's lane spec + cell coordinates.
    from ocharness import lane

    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    lane_cfg = manifiesto["lane"]
    assert lane_cfg["mode"] == "cache-free"
    assert lane_cfg["nonce_seed"] == lane.nonce_seed(manifiesto["run_id"])
    words = lane.nonce_words(lane.expected_tin("T1", "qa_short"))
    idx = lane.nonce_index("T1", "qa_short", "glm-5.3-flash", 1, ok["k"], 2)
    assert (
        lane.nonce_sha256(lane.nonce_text(lane_cfg["nonce_seed"], idx, words))
        == (ok["nonce_sha256"])
    )
    # Every measured request's nonce differs (no warm starts, ever): the 24
    # cell chats all carry distinct prefixes. The canary's replays are the one
    # deliberate repeat (salted[0]'s nonce, the identical prefix its ratio reads).
    celdas = [c for c in chats if len(c["body"]["messages"][0]["content"]) < 10_000]
    assert len(celdas) == 24
    assert len({c["body"]["messages"][0]["content"].split("\n\n", 1)[0] for c in celdas}) == 24
    # The fixtures are untouched: the batch's fixture_hash matches the bare specs.
    from ocharness.fixtures import build, fixture_hash

    assert {r["fixture_hash"] for r in requests} == {
        fixture_hash(build("T1", w, n))
        for w, n in (("qa_short", 20), ("calibration", 3), ("throughput", 1))
    }


def test_canary_alarms_and_aborts_the_run_at_the_gate(tmp_path, fake_cli):
    """A replay volley billing ~full price means the lane's plumbing broke: the
    canary aborts before the first bracket, loudly, and the run state refuses."""
    fake_cli.cache_horizon_s = None  # the no-cache world: replays bill full price
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    assert "canary" in err and "aborts at the gate" in err and "Traceback" not in err
    assert not list((tmp_path / "batches").glob("batches-*.jsonl"))  # no bracket ran
    lineas = read_canary(tmp_path)
    assert len(lineas) == 1
    assert lineas[0]["alarm"] is True and lineas[0]["ratio"] > 0.5
    assert lineas[0]["ratio_basis"] == "session"
    assert lineas[0]["workload"] == "billing-canary"
    assert len(lineas[0]["salted"]["nonce_sha256"]) == 5
    assert len({n for n in lineas[0]["salted"]["nonce_sha256"]}) == 5  # fresh nonces each
    # The replay re-sends salted[0]'s prefix verbatim.
    assert lineas[0]["replay"]["nonce_sha256"] == lineas[0]["salted"]["nonce_sha256"][0]
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    assert manifiesto["canary"]["status"] == "alarm"
    assert not list((tmp_path / "runs").glob("requests-*.jsonl"))

    # A resume refuses: the lane was never proven for this run_id (an explicit
    # operator decision — delete the manifest — reopens it, never a retry).
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1 and "already alarmed" in err


def test_canary_runs_once_per_run_and_is_reused_on_resume(tmp_path, fake_cli):
    """The canary is per run_id: a resume reuses the recorded ratio, billing
    nothing new for it."""
    pricing = prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    lineas = read_canary(tmp_path)
    assert len(lineas) == 1 and lineas[0]["alarm"] is False
    assert len(lineas[0]["salted"]["outcomes"]) == 5
    assert len(lineas[0]["replay"]["outcomes"]) == 5
    # The replay volley's outcomes show the cache discount: the done-objects
    # report the reduced prompt (the fake's hit behavior), the replays bill 0.
    assert lineas[0]["ratio"] < 0.5 and lineas[0]["dpp"]["salted_session"] > 0
    antes = len(fake_cli.calls)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 0, err
    assert "already ran for this run" in err  # reused, not repeated
    nuevos = [c for c in fake_cli.calls[antes:] if c["path"] == "/api/chat"]
    assert nuevos == []  # zero new chats: everything was done, the canary included


def test_registration_settle_closes_capped_when_the_meter_never_stabilizes(tmp_path, fake_cli):
    """A meter that keeps drifting burns the cap: the bracket closes with
    settle_exit 'capped' and whatever the last read saw — recorded, never
    silently mistaken for a registered bracket."""
    fake_cli.drift_ticks_per_read = 1
    fake_cli.ticks_per_request = 50  # real billing dominates the drift reads:
    # the canary's ratio stays under the alarm (salted ~250 ticks vs replay ~15)
    pricing = prepare(tmp_path)
    code, _out, err = run_t1(
        tmp_path,
        "--model",
        "glm-5.3-flash",
        "--reps",
        "1",
        "--settle-s",
        "0.3",
        "--settle-poll-s",
        "0.02",
    )
    assert code == 0, err
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 3
    for b in batches:
        assert b["settle_exit"] == "capped"
        assert b["settle_mode"] == "registration"
        assert b["settle_reads"] > 0
        # The post read still carries the spend the loop could see.
        assert b["medidor_post"] is not None and b["dpp_session"] is not None


def test_registration_settle_converges_within_three_polls_under_the_fake(tmp_path, fake_cli):
    """The AC: a bracket under the fake closes after <= 3 polls (lag_reads = 2:
    the count-check read, a moving read, a confirming one) with exit 'stable',
    and the per-window registration times are stamped."""
    prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")[0] == 0
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert len(batches) == 3
    for b in batches:
        assert b["settle_exit"] == "stable"
        assert 1 <= b["settle_reads"] <= 3
        assert 0 <= b["registered_session_s"] <= b["settle_reads"] * 0.01 + 0.05
        assert 0 <= b["registered_weekly_s"] <= b["settle_reads"] * 0.01 + 0.05


def test_manifest_refuses_a_v2_manifest_under_protocol_v3(tmp_path, fake_cli):
    """The drift guard: a dataset written under protocol 2 never mixes with v3."""
    prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    antes = len(fake_cli.calls)
    ruta = tmp_path / "runs" / "manifest-T1.json"
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    manifiesto["protocol_version"] = "2"
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
            str(tmp_path / "pricing"),
        )[0]
        == 0
    )
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    assert "protocol" in err and "keep the datasets apart" in err
    assert [c for c in fake_cli.calls[antes:] if c["path"] == "/api/chat"] == []


def test_passive_detector_flags_a_collapsed_bracket(tmp_path, fake_cli):
    """A bracket whose #28 token budget predicts a readable weekly dpp but
    measures none carries the collapse flag: on the manifest entry and in the
    batch line's notes (the threshold itself is deferred until v3 data)."""
    from ocharness import runner as runner_mod

    def presupuesto_grande(registros, dpp_weekly):
        # A budget whose prediction collapses: predicted >= 3.5 ticks, none seen.
        return {"expected_pp": 5.0, "measured_pp": dpp_weekly, "collapsed": True}

    import ocharness.runner as runner_mod2

    prepare(tmp_path)
    original = runner_mod2._passive_detector
    runner_mod2._passive_detector = lambda registros, dpp: presupuesto_grande(registros, dpp)
    try:
        code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    finally:
        runner_mod2._passive_detector = original
    assert code == 0, _out
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert all("passive detector" in b["notes"] for b in batches)
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    for entrada in manifiesto["batches"].values():
        assert entrada["detector"]["collapsed"] is True
        assert entrada["detector"]["expected_pp"] == 5.0


def test_the_settle_parameters_are_validated_as_a_bounded_pair(tmp_path, fake_cli):
    """A zero cap closes every bracket before the meter can register it (all
    dpp would read 0.0); a zero poll hammers the meter; a poll at or above the
    cap could never land the two consecutive reads stability needs."""
    prepare(tmp_path)
    for malo in ("0", "-1", "nan", "inf"):
        assert run_t1(tmp_path, "--settle-s", malo)[0] == 2, malo
    for malo in ("0", "-1", "nan"):
        assert run_t1(tmp_path, "--settle-poll-s", malo)[0] == 2, malo
    assert run_t1(tmp_path, "--settle-poll-s", "3")[0] == 2  # >= the 2 s cap
    assert run_t1(tmp_path, "--settle-s", "2", "--settle-poll-s", "2")[0] == 2
    assert fake_cli.calls == []  # refused before any request
    # The probe and the calibration enforce the same pair.
    assert (
        run_cli(tmp_path, "probe-concurrency", "--model", "glm-5.3-flash", "--settle-s", "0")[0]
        == 2
    )
    assert run_cli(tmp_path, "calibrate-cache", "--settle-poll-s", "5", "--settle-s", "2")[0] == 2


def test_canary_refuses_to_measure_from_failed_requests(tmp_path, fake_cli):
    """A 429 or an errored chat reshapes the ratio in either direction: a
    rejected salted volley inflates it (false alarm), errored replays empty it
    (a false all-clear). The canary aborts instead, and the partial evidence
    stays attributed."""
    # Rejected salted chats: a deflated denominator must never alarm.
    fake_cli.reject_all = True
    fake_cli.reject_from = 1  # everything rejected, the canary included
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1 and "not fully accepted" in err and "Traceback" not in err
    lineas = read_canary(tmp_path)
    assert lineas[0]["ratio"] is None  # no verdict from failed chats
    assert len(lineas[0]["salted"]["outcomes"]) == 5  # the billed evidence is pinned
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    assert manifiesto["canary"]["status"] == "failed"
    assert not list((tmp_path / "runs").glob("requests-*.jsonl"))  # no bracket ran

    # A resume refuses: the canary never completed, it is never re-billed.
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1 and "never completed" in err
