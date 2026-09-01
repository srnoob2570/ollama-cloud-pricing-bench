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

from ocharness import workloads
from ocharness.cost import new_task_cost
from ocharness.pricing import PriceTable

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
        for ventana in ("session", "weekly"):
            pre = b["medidor_pre"]["limits"][ventana]["usage"]
            post = b["medidor_post"]["limits"][ventana]["usage"]
            assert b[f"dpp_{ventana}"] == (post - pre) * 100, b["workload"]


def test_timestamps_persist_unrounded(tmp_path, fake_cli, monkeypatch):
    """t_start / t_first_chunk / t_total, and the manifests' started_at / at /
    captured_at / dry_run_at, keep their exact stamps (ticket acceptance)."""
    monkeypatch.setattr(time, "time", lambda: TICK)
    prepare_t1(tmp_path)
    marca = json.loads((tmp_path / "runs" / "gate-T1.json").read_text(encoding="utf-8"))
    assert marca["dry_run_at"] == TICK  # the run consumes the mark: assert it first
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash", "--rep", "1", "--reps", "1")
    assert code == 0, err

    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    assert manifiesto["started_at"] == TICK
    assert manifiesto["catalog"][-1]["captured_at"] == TICK
    for entrada in manifiesto["batches"].values():
        assert entrada["at"] == TICK
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
    marca = json.loads((tmp_path / "runs" / "gate-T1.json").read_text(encoding="utf-8"))
    tabla = PriceTable.load(tmp_path / "pricing")
    for fila in marca["estimado"]["rows"]:
        carga = next(w for w in workloads.WORKLOADS_BY_LEVEL["T1"] if w.name == fila["workload"])
        t_in = carga.t_in * carga.requests
        t_out = carga.t_out * carga.requests
        esperado_s0 = esperado_s1 = 0.0
        for modelo in workloads.slate("T1", tabla):
            tarifa = tabla.rate(modelo)
            esperado_s0 += new_task_cost(t_in, t_out, tarifa, s=0.0, per=tabla.per)
            esperado_s1 += new_task_cost(t_in, t_out, tarifa, s=0.5, per=tabla.per)
        assert fila["cost_s0"] == esperado_s0, fila["workload"]
        assert fila["cost_s1"] == esperado_s1, fila["workload"]


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
    tarifa = PriceTable.load(tmp_path / "pricing").rate("alpha")
    s0 = new_task_cost(1000, 500, tarifa, s=0.0, per=1_000_000)
    assert a["new_cost_task_s0_usd"] == s0
    assert a["threshold_pp_per_1m"]["s0"] == s0 / (1500 / 1e6) / U


def test_status_quota_sum_equals_the_raw_payloads_chain(tmp_path, fake_cli):
    """`status --json` sums the brackets' exact deltas: the report's figure is
    the meter payloads' chain, not a re-rounded display value."""
    prepare_t1(tmp_path)
    assert run_t1(tmp_path, "--settle-s", "0", "--reps", "1")[0] == 0
    batches = read_jsonl(tmp_path, "batches", "batches-*.jsonl")
    doc = json.loads(run_cli(tmp_path, "status", "--level", "T1", "--json")[1])
    nivel = doc["levels"][0]
    esperado_s = esperado_w = 0.0
    for b in batches:
        esperado_s += (
            b["medidor_post"]["limits"]["session"]["usage"]
            - b["medidor_pre"]["limits"]["session"]["usage"]
        ) * 100
        esperado_w += (
            b["medidor_post"]["limits"]["weekly"]["usage"]
            - b["medidor_pre"]["limits"]["weekly"]["usage"]
        ) * 100
    assert nivel["quota"]["dpp_session"] == esperado_s
    assert nivel["quota"]["dpp_weekly"] == esperado_w


def test_predict_report_persists_full_precision(tmp_path):
    """The APEs and the aggregates they feed are the exact |estimate - real| /
    real floats - the bootstrap reads unrounded errors."""
    full_study(tmp_path)
    doc = report(tmp_path)
    # the expected side prices on the SAME table the study ran against (the
    # tmp pricing dir), never on whichever snapshot the repo holds today
    tabla = PriceTable.load(tmp_path / "pricing")

    qa = next(
        c for c in doc["cells"] if c["workload"] == "qa_short" and c["model"] == "glm-5.3-flash"
    )
    real = new_task_cost(80_000, 80_000, tabla.rate("glm-5.3-flash"), s=0.0, per=tabla.per)
    assert qa["real_new_s0_usd_per_run"] == real
    assert qa["blind"]["ape_new"] == abs(0.0624 - real) / real

    archivo = next(
        c for c in doc["cells"] if c["workload"] == "multi_file" and c["model"] == "kimi-k2.7-code"
    )
    real_file = new_task_cost(150_000, 30_000, tabla.rate("kimi-k2.7-code"), s=0.0, per=tabla.per)
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
        "0",
        "--spaced-gaps",
        "0.02",
        "0.04",
        "0.12",
    )
    assert code == 0, err
    requests = read_jsonl(tmp_path, "runs", "requests-*.jsonl")
    lectura = summary(tmp_path)["readings"][MODEL]
    assert lectura["calibrated_at"] == max(r["t_total"] for r in requests)
    frio = requests[0]  # the cold bracket's single request, first in the file
    ev = lectura["signals"]["cache_cold"]["requests"][0]
    assert ev["ttft_s"] == frio["t_first_chunk"] - frio["t_start"]


def test_no_rounding_on_any_persisted_path():
    """The policy's structural guard (the ticket's grep criterion, executable):
    no `round(` call survives anywhere in the package except the fake meter's
    own 0.001 quantization (the real meter's resolution, mirrored) and the T3
    fixture bytes (seeded synthetic-repo data, hash-pinned). The dashboard's
    Math.round presentation never matches the word-boundary pattern."""
    patron = re.compile(r"(?<![\w.])round\(")
    exentos = {"testing/fake.py", "fixtures_t3.py"}
    raiz = pathlib.Path(__file__).resolve().parents[1] / "src" / "ocharness"
    for ruta in sorted(raiz.rglob("*.py")):
        relativo = ruta.relative_to(raiz).as_posix()
        if relativo in exentos:
            continue
        for numero, linea in enumerate(ruta.read_text(encoding="utf-8").splitlines(), 1):
            assert not patron.search(linea), f"{relativo}:{numero}: {linea.strip()}"


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
        "0",
    )
    assert code == 0, err
    for linea in read_jsonl(tmp_path, "runs", "probe-*.jsonl"):
        assert linea["t_start"] == TICK
        assert linea["t_total"] == TICK
    manifiesto = json.loads(
        (tmp_path / "runs" / "manifest-T1-concurrency.json").read_text(encoding="utf-8")
    )
    assert manifiesto["probe"]["at"] == TICK
