"""Ticket Harness v2 01 (spec #16): zero rounding in everything persisted.

The meter deltas keep their exact float, timestamps persist unrounded, and the
derivatives / extrapolations / MAPEs regenerate at full precision from the raw
data. Every assertion reads produced artifacts (the testing contract's seam);
the clock patch makes "unrounded" decidable: a stamp that survived a round()
would have lost its trailing digits.
"""

from __future__ import annotations

import json
import pathlib
import re
import time

from conftest import standard_table, write_table
from test_analyze import TABLE_V1, U, analyze_doc, craft_dataset
from test_calibrate_cache import MODEL, always_hits, prepare as prepare_t2, summary
from test_dry_run import run_cli, with_pricing
from test_predict import full_study, report
from test_run import read_jsonl, run_t1

from obench import workloads
from obench.cost import new_task_cost
from obench.pricing import PriceTable

# A clock no rounding survives: ...1234567 loses digits at 6 places (...123457)
# and at 3 places (...123), so any persisted rounding changes the value.
TICK = 1725200000.1234567


def prepare_t1(tmp_path) -> str:
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    return pricing


def test_batch_dpp_persists_the_exact_meter_delta(tmp_path, fake_cli):
    """The bracket's dpp is the raw payloads' exact float difference - the tick
    is comparison logic (the verdicts' tie band), never persisted rounding."""
    prepare_t1(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 0, err
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    assert batches
    for b in batches:
        for window in ("session", "weekly"):
            pre = b["meter_pre"]["limits"][window]["usage"]
            post = b["meter_post"]["limits"][window]["usage"]
            assert b[f"dpp_{window}"] == (post - pre) * 100, b["workload"]


def test_timestamps_persist_unrounded(tmp_path, fake_cli, monkeypatch):
    """t_start / t_first_chunk / t_total, and the manifests' started_at / at /
    captured_at / dry_run_at, keep their exact stamps (ticket acceptance)."""
    monkeypatch.setattr(time, "time", lambda: TICK)
    prepare_t1(tmp_path)
    mark = json.loads((tmp_path / "runs" / "gate-T1.json").read_text(encoding="utf-8"))
    assert mark["dry_run_at"] == TICK  # the run consumes the mark: assert it first
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 0, err

    manifest = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    assert manifest["started_at"] == TICK
    assert manifest["catalog"][-1]["captured_at"] == TICK
    for input in manifest["batches"].values():
        assert input["at"] == TICK
    for r in read_jsonl(tmp_path, "runs", "requests-*.jsonl"):
        assert r["t_start"] == TICK
        assert r["t_first_chunk"] == TICK
        assert r["t_total"] == TICK


def test_dry_run_budget_persists_the_exact_cost(tmp_path):
    """The gate mark's approved budget is the pricing formula's exact float - a
    rounding there would authorize a different spend than the one approved."""
    rates = {m: {"input": 0.07, "cached_input": 0.03, "output": 0.13} for m in standard_table()}
    pricing = str(write_table(tmp_path / "pricing", "2026-08-31", rates))
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    mark = json.loads((tmp_path / "runs" / "gate-T1.json").read_text(encoding="utf-8"))
    table = PriceTable.load(tmp_path / "pricing")
    from obench.lane import nonce_tokens_estimate

    for row in mark["estimado"]["rows"]:
        workload = next(w for w in workloads.WORKLOADS_BY_LEVEL["T1"] if w.name == row["workload"])
        # The estimate prices what will actually be sent: the workload's tokens
        # plus the cache-free lane's per-request nonce overhead (protocol v3).
        nonce = nonce_tokens_estimate(workload.t_in)
        t_in = (workload.t_in + nonce) * workload.requests
        t_out = workload.t_out * workload.requests
        expected_s0 = expected_s1 = 0.0
        for model in workloads.slate("T1", table):
            rate = table.rate(model)
            expected_s0 += new_task_cost(t_in, t_out, rate, s=0.0, per=table.per)
            expected_s1 += new_task_cost(t_in, t_out, rate, s=0.5, per=table.per)
        assert row["cost_s0"] == expected_s0, row["workload"]
        assert row["cost_s1"] == expected_s1, row["workload"]
        # The lane's overhead rides the row transparently: what was added to
        # tokens_in is exactly the row's nonce_tokens (models × reps × requests).
        assert row["nonce_tokens"] == nonce * workload.requests * 19
        assert row["tokens_in"] == (workload.t_in + nonce) * workload.requests * 19


def test_analyze_persists_full_precision_derivatives(tmp_path, monkeypatch):
    """The regenerated derivatives carry more digits than any rounding allowed:
    quantiles, per-rep rows, medians, extrapolations and the threshold are the
    exact floats of the raw evidence."""
    monkeypatch.setattr(time, "time", lambda: TICK)
    pricing = str(tmp_path / "pricing")
    write_table(tmp_path / "pricing", "2026-08-31", TABLE_V1)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")

    assert doc["generated_at"] == TICK
    assert doc["base_params"]["tick_usd"] == U * 0.1
    a = next(c for c in doc["cells"] if c["model"] == "alpha" and c["workload"] == "qa_short")
    # legacy: dpp 0.2 pp x U / 2 tasks, per rep; the median over two equal reps
    assert a["legacy_cost_task_usd"]["median"] == 0.2 * U / 2
    # measured pp/1M: 0.2 pp per 3000 tokens = 66.66666666666667, not 66.6667
    assert a["pp_per_1m"]["median"] == 0.2 * 1e6 / 3000
    assert a["pp_per_1m"]["p25"] == 0.2 * 1e6 / 3000
    assert a["pp_per_1m"]["p95"] == 0.2 * 1e6 / 3000
    # new-plan extrapolation from the measured median tokens (1000 in / 500 out)
    rate = PriceTable.load(tmp_path / "pricing").rate("alpha")
    s0 = new_task_cost(1000, 500, rate, s=0.0, per=1_000_000)
    assert a["new_cost_task_s0_usd"] == s0
    # the threshold compares paid dollars: the new side's credits divide by
    # the default credit_ratio (3)
    assert a["threshold_pp_per_1m"]["s0"] == s0 / (1500 / 1e6) / (U * 3)


def test_status_quota_sum_equals_the_raw_payloads_chain(tmp_path, fake_cli):
    """`status --json` sums the brackets' exact deltas: the report's figure is
    the meter payloads' chain, not a re-rounded display value."""
    prepare_t1(tmp_path)
    assert run_t1(tmp_path, "--settle-s", "2", "--settle-poll-s", "0.01", "--reps", "1")[0] == 0
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    doc = json.loads(run_cli(tmp_path, "status", "--level", "T1", "--json")[1])
    level = doc["levels"][0]
    expected_s = expected_w = 0.0
    for b in batches:
        expected_s += (
            b["meter_post"]["limits"]["session"]["usage"]
            - b["meter_pre"]["limits"]["session"]["usage"]
        ) * 100
        expected_w += (
            b["meter_post"]["limits"]["weekly"]["usage"]
            - b["meter_pre"]["limits"]["weekly"]["usage"]
        ) * 100
    assert level["quota"]["dpp_session"] == expected_s
    assert level["quota"]["dpp_weekly"] == expected_w


def test_predict_report_persists_full_precision(tmp_path):
    """The APEs and the aggregates they feed are the exact |estimate - real| /
    real floats - the bootstrap reads unrounded errors."""
    full_study(tmp_path)
    doc = report(tmp_path)
    # the expected side prices on the SAME table the study ran against (the
    # tmp pricing dir), never on whichever snapshot the repo holds today
    table = PriceTable.load(tmp_path / "pricing")

    larga = next(
        c for c in doc["cells"] if c["workload"] == "long_context" and c["model"] == "glm-5.3-flash"
    )
    real = new_task_cost(30_000, 300, table.rate("glm-5.3-flash"), s=0.0, per=table.per)
    assert larga["real_new_s0_usd_per_run"] == real
    assert larga["blind"]["ape_new"] == abs(0.00558 - real) / real

    archivo = next(
        c for c in doc["cells"] if c["workload"] == "multi_file" and c["model"] == "kimi-k2.7-code"
    )
    real_file = new_task_cost(150_000, 30_000, table.rate("kimi-k2.7-code"), s=0.0, per=table.per)
    assert archivo["real_new_s0_usd_per_run"] == real_file
    assert archivo["blind"]["ape_new"] == abs(0.28875 - real_file) / real_file

    # the aggregate re-derives from the per-cell APEs with no re-rounding
    ciego = doc["aggregate"]["blind"]
    apes_new = [
        c["blind"]["ape_new"]
        for c in doc["cells"]
        if c["blind"] and c["blind"]["ape_new"] is not None
    ]
    assert ciego["mape_new"]["mape"] == sum(apes_new) / len(apes_new)


def test_calibration_persists_unrounded_evidence(tmp_path, fake_cli):
    """The calibration doc's per-replay ttft and its calibrated_at stamp are the
    request lines' exact spans - no rounding between raw and reading."""
    always_hits(fake_cli)
    prepare_t2(tmp_path)
    code, _out, err = run_cli(
        tmp_path,
        "calibrate-cache",
        "--model",
        MODEL,
        "--settle-s",
        "2",
        "--settle-poll-s",
        "0.01",
        "--spaced-gaps",
        "0.02",
        "0.04",
        "0.12",
    )
    assert code == 0, err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    reading = summary(tmp_path)["readings"][MODEL]
    assert reading["calibrated_at"] == max(r["t_total"] for r in requests)
    frio = requests[0]  # the cold bracket's single request, first in the file
    ev = reading["signals"]["cache_cold"]["requests"][0]
    assert ev["ttft_s"] == frio["t_first_chunk"] - frio["t_start"]


def test_no_rounding_on_any_persisted_path():
    """The policy's structural guard (the ticket's grep criterion, executable):
    no `round(` call survives anywhere in the package except the fake meter's
    own 0.001 quantization (the real meter's resolution, mirrored) and the T3
    fixture bytes (seeded synthetic-repo data, hash-pinned). The dashboard's
    Math.round presentation never matches the word-boundary pattern."""
    pattern = re.compile(r"(?<![\w.])round\(")
    exentos = {"testing/fake.py", "fixtures_t3.py"}
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "obench"
    for path in sorted(root.rglob("*.py")):
        relativo = path.relative_to(root).as_posix()
        if relativo in exentos:
            continue
        for numero, linea in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not pattern.search(linea), f"{relativo}:{numero}: {linea.strip()}"


def test_probe_timestamps_persist_unrounded(tmp_path, fake_cli, monkeypatch):
    """The probe's volley lines stamp t_start/t_total like every raw line."""
    monkeypatch.setattr(time, "time", lambda: TICK)
    prepare_t1(tmp_path)
    code, _out, err = run_cli(
        tmp_path,
        "probe-concurrency",
        "--model",
        "glm-5.3-flash",
        "--k-max",
        "4",
        "--settle-s",
        "2",
        "--settle-poll-s",
        "0.01",
    )
    assert code == 0, err
    for linea in read_jsonl(tmp_path, "runs", "probe-*.jsonl"):
        assert linea["t_start"] == TICK
        assert linea["t_total"] == TICK
    manifest = json.loads(
        (tmp_path / "runs" / "manifest-T1-concurrency.json").read_text(encoding="utf-8")
    )
    assert manifest["probe"]["at"] == TICK


def test_the_passive_detector_flags_only_a_collapse():
    """The detector's predicate: below one tick (the tick read through the
    comparison band) against a >= 3.5-tick budget collapses; anything readable
    is recorded without a flag, and a sub-floor budget never flags."""
    from obench import runner

    record = [{"done": {"prompt_eval_count": 30_000, "eval_count": 0}}]
    # Prefill body (in-share 1.0): 30K tokens x 2.6 pp/1M = 0.078 pp expected.
    colapsado = runner._passive_detector(record, 0.0)
    assert colapsado["collapsed"] is False  # 0.078 pp < the 3.5-tick (0.35 pp) floor
    # A 5-rep pool (150K tokens): 0.39 pp expected >= 3.5 ticks.
    grande = [{"done": {"prompt_eval_count": 30_000, "eval_count": 0}} for _ in range(5)]
    assert runner._passive_detector(grande, 0.0)["collapsed"] is True
    # Exactly one tick (0.1) is readable: not a collapse — and the tick read
    # through the comparison band keeps a boundary-residue float (the exact
    # 0.1 landing at 0.0999999999999999) on the "one tick" side too.
    assert runner._passive_detector(grande, 0.1)["collapsed"] is False
    assert runner._passive_detector(grande, 0.1 - 1e-13)["collapsed"] is False
    # Below the band (a sub-tick residue), it collapses.
    assert runner._passive_detector(grande, 0.0999)["collapsed"] is True
    # A readable delta never flags, and an unreadable budget never flags.
    assert runner._passive_detector(grande, 0.5)["collapsed"] is False
    assert (
        runner._passive_detector([{"done": {"prompt_eval_count": 26, "eval_count": 12}}], 0.0)[
            "collapsed"
        ]
        is False
    )
