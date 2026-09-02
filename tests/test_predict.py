"""`bench predict`: the predictability HITL flow + the comparative MAPE (ticket Harness 09).

The 12-cell subgrid is estimated blind BEFORE each cell runs — the flow refuses an
estimate once the cell's real exists in the raw datasets, under any protocol vintage —
and re-estimated informed at the end. Every record is timestamped and hash-locked; a
registry edited after the fact refuses to grow and refuses to report. The report
re-derives the reals from the analyze derivatives in native units (weekly pp / dollars
of credits), with a percentile bootstrap CI per aggregate, the paired bootstrap of
MAPE_legacy - MAPE_new as the comparative verdict, and the sub-resolution cells (real
dp under a tick) excluded from the legacy side and flagged as an opacity finding.

Everything is asserted through produced artifacts; the fake observes ZERO requests
during any predict invocation. The manual-math dataset below is hand-crafted raw
JSONL so every MAPE is computed by hand in the comments (official rates per 1M:
glm-5.3-flash 0.15/0.03/0.50, kimi-k3 3.0/0.3/15.0, kimi-k2.7-code 0.95/0.19/4.0).
"""

from __future__ import annotations

import json
import pathlib

from conftest import standard_table, write_table
from test_dry_run import json_doc, run_cli

from ocharness import predict, workloads
from ocharness.client import PROTOCOL_VERSION
from ocharness.schema import validate_batch_line, validate_estimate_line, validate_request_line

PRICING = "pricing"  # relative to --base: the tests write the table there


def pricing_dir(tmp_path) -> str:
    write_table(tmp_path / PRICING, "2026-08-31", standard_table())
    return str(tmp_path / PRICING)


def estimate(
    tmp_path, phase: str, workload: str, model: str, pp, usd, *extra
) -> tuple[int, str, str]:
    return run_cli(
        tmp_path,
        "predict",
        "--phase",
        phase,
        "--workload",
        workload,
        "--model",
        model,
        "--pp",
        str(pp),
        "--usd",
        str(usd),
        "--pricing-dir",
        str(pathlib.Path(tmp_path) / PRICING),
        *extra,
    )


def estimates_path(tmp_path, phase: str) -> pathlib.Path:
    nombre = predict.PHASE_FILE[phase]
    return pathlib.Path(tmp_path, predict.PREDICT_DIR, nombre)


def write_raw(tmp_path, requests: list[dict], batches: list[dict]) -> None:
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "batches").mkdir(parents=True, exist_ok=True)
    with (tmp_path / "runs" / "requests-predict-test.jsonl").open("a", encoding="utf-8") as f:
        for r in requests:
            f.write(json.dumps(r) + "\n")
    with (tmp_path / "batches" / "batches-predict-test.jsonl").open("a", encoding="utf-8") as f:
        for b in batches:
            f.write(json.dumps(b) + "\n")


def craft_cell(tmp_path, workload: str, model: str, *, reps: dict[int, tuple[int, int, float]]):
    """One measured cell: per rep (tokens_in, tokens_out, dpp_weekly), one request
    per rep, schema-valid on write."""
    level = next(c.level for c in predict.grid() if c.workload == workload)
    requests: list[dict] = []
    batches: list[dict] = []
    for rep, (tin, tout, dpp) in sorted(reps.items()):
        bid = f"b-{workload}-{model}-{rep}".replace(".", "-")
        requests.append(
            {
                "req_id": f"{bid}-0000",
                "batch_id": bid,
                "run_id": "predict-test",
                "level": level,
                "workload": workload,
                "model": model,
                "seed": rep,
                "rep": rep,
                "k": 1,
                "t_start": 1.0 + rep,
                "t_first_chunk": 1.05 + rep,
                "t_total": 2.0 + rep,
                "chunks": 3,
                "tok_in": tin,
                "tok_out": tout,
                "tok_cached": None,
                "prompt_sha256": "p" * 64,
                "nonce_sha256": "n" * 64,
                "api": {"done": True},
                "http": 200,
                "err": None,
                "checker": "pass",
                "tool_calls": [],
                "steps": [],
                "sandbox": None,
                "out_text_hash": "h" * 64,
                "fixture_hash": "f" * 64,
                "table_version": "2026-08-31",
                "protocol_version": PROTOCOL_VERSION,
            }
        )
        batches.append(
            {
                "batch_id": bid,
                "run_id": "predict-test",
                "level": level,
                "workload": workload,
                "model": model,
                "fixture_hash": "f" * 64,
                "k": 1,
                "n": 1,
                "reps": 1,
                "pool": None,
                "settle_s": 60.0,
                "settle_mode": "registration",
                "settle_reads": 2,
                "registered_session_s": 0.0,
                "registered_weekly_s": 0.0,
                "settle_exit": "stable",
                "count_check_s": 0.5,
                "wall_clock_s": 10.0,
                "medidor_pre": {"limits": {"session": {"usage": 0.5}, "weekly": {"usage": 0.5}}},
                "medidor_post": {
                    "limits": {
                        "session": {"usage": 0.5 + dpp / 200},
                        "weekly": {"usage": 0.5 + dpp / 100},
                    }
                },
                "dpp_session": dpp,
                "dpp_weekly": dpp,
                "request_counts": {"pre": {}, "count_check": {}, "post": {}},
                "table_version": "2026-08-31",
                "protocol_version": PROTOCOL_VERSION,
                "notes": "",
            }
        )
    for r in requests:
        validate_request_line(r)  # the docstring's claim is enforced, not assumed
    for b in batches:
        validate_batch_line(b)
    write_raw(tmp_path, requests, batches)


def craft_study_dataset(tmp_path) -> None:
    """The four measured cells of the manual-math dataset:

    - qa_short/glm-5.3-flash: 2 reps of 80K in / 80K out, dpp 2.0
        -> real 2.0 pp; new S0 = (80000*0.15 + 80000*0.50)/1e6 = $0.052
    - multi_turn/kimi-k3: 2 reps of 5.6K in / 1.6K out, dpp 0.5 and 1.5
        -> real median 1.0 pp; new S0 = (5600*3 + 1600*15)/1e6 = $0.0408
    - reasoning/glm-5.3-flash: 2 reps of 100K in / 300K out, dpp 0.05
        -> real 0.05 pp, UNDER the 0.1 pp tick; new S0 = $0.165
    - multi_file/kimi-k2.7-code: 1 rep of 150K in / 30K out, dpp 4.0
        -> real 4.0 pp; new S0 = (150000*0.95 + 30000*4)/1e6 = $0.2625
    """
    craft_cell(
        tmp_path,
        "qa_short",
        "glm-5.3-flash",
        reps={1: (80_000, 80_000, 2.0), 2: (80_000, 80_000, 2.0)},
    )
    craft_cell(
        tmp_path, "multi_turn", "kimi-k3", reps={1: (5_600, 1_600, 0.5), 2: (5_600, 1_600, 1.5)}
    )
    craft_cell(
        tmp_path,
        "reasoning",
        "glm-5.3-flash",
        reps={1: (100_000, 300_000, 0.05), 2: (100_000, 300_000, 0.05)},
    )
    craft_cell(tmp_path, "multi_file", "kimi-k2.7-code", reps={1: (150_000, 30_000, 4.0)})


def estimate_all_blind(tmp_path) -> None:
    """The 12 blind estimates, locked while the dataset holds nothing.

    The four measured cells' estimates are chosen so every hand-derived MAPE below
    is simple; the eight still-unmeasured cells get placeholder numbers.
    """
    valores = {
        ("qa_short", "glm-5.3-flash"): (
            4.0,
            0.0624,
        ),  # APE: legacy |4-2|/2 = 1.0, new (0.0624-0.052)/0.052 = 0.2
        ("multi_turn", "kimi-k3"): (4.0, 0.0408),  # APE: legacy |4-1|/1 = 3.0, new 0.0
        ("reasoning", "glm-5.3-flash"): (
            0.2,
            0.33,
        ),  # new (0.33-0.165)/0.165 = 1.0; legacy excluded
        ("multi_file", "kimi-k2.7-code"): (
            8.0,
            0.28875,
        ),  # legacy |8-4|/4 = 1.0, new (0.28875-0.2625)/0.2625 = 0.1
    }
    for workload, model in predict._GRID:
        pp, usd = valores.get((workload, model), (1.0, 0.01))
        code, out, err = estimate(tmp_path, "blind", workload, model, pp, usd)
        assert code == 0, out or err


def estimate_all_informed(tmp_path) -> None:
    """The informed re-estimation of the four measured cells: exact (the learning
    curve's floor - MAPE 0 against the same reals)."""
    valores = {
        ("qa_short", "glm-5.3-flash"): (2.0, 0.052),
        ("multi_turn", "kimi-k3"): (1.0, 0.0408),
        ("reasoning", "glm-5.3-flash"): (0.05, 0.165),
        ("multi_file", "kimi-k2.7-code"): (4.0, 0.2625),
    }
    for (workload, model), (pp, usd) in valores.items():
        code, out, err = estimate(tmp_path, "informed", workload, model, pp, usd)
        assert code == 0, out or err


def report(tmp_path, *extra) -> dict:
    doc = json_doc(
        tmp_path, "predict", "--report", "--pricing-dir", str(tmp_path / PRICING), *extra
    )
    assert doc["kind"] == "predictability-report"
    return doc


def fila(doc: dict, workload: str, model: str) -> dict:
    return next(c for c in doc["cells"] if c["workload"] == workload and c["model"] == model)


# ---------------------------------------------------------------------------
# the grid
# ---------------------------------------------------------------------------


def test_grid_holds_twelve_cells_on_the_slates():
    celdas = predict.grid()
    assert len(celdas) == 12
    slates = {
        "T1": set(standard_table()),
        "T2": set(workloads.SLATE_T2),
        "T3": set(workloads.SLATE_T3),
    }
    for c in celdas:
        assert c.model in slates[c.level], c.key
        assert c.workload in {w.name for w in workloads.WORKLOADS_BY_LEVEL[c.level]}, c.key
    assert {(c.workload, c.model) for c in celdas} == (
        {
            (w, m)
            for w in ("qa_short", "long_context", "multi_turn", "reasoning", "ratio_in")
            for m in ("glm-5.3-flash", "kimi-k3")
        }
        | {("multi_file", "glm-5.3-flash"), ("multi_file", "kimi-k2.7-code")}
    )
    # the agentic cell carries the T3 slate's code model, never kimi-k3
    assert {c.model for c in celdas if c.workload == "multi_file"} == {
        "glm-5.3-flash",
        "kimi-k2.7-code",
    }


# ---------------------------------------------------------------------------
# the walk-through brief: only public information, zero API
# ---------------------------------------------------------------------------


def test_brief_walkthrough_shows_only_public_information(tmp_path, fake_cli):
    pricing = pricing_dir(tmp_path)
    doc = json_doc(tmp_path, "predict", "--pricing-dir", pricing)
    assert doc["counts"] == {"blind": 0, "informed": 0, "cells": 12}
    assert len(doc["cells"]) == 12
    for f in doc["cells"]:
        assert f["blind"] is None and f["informed"] is None
        b = f["brief"]
        assert b["requests_per_run"] > 0 and b["tokens_in_per_request"] > 0
        # The cache-free lane's overhead is public protocol, part of the brief:
        # the estimate must be able to account for what will actually be sent.
        assert b["nonce_words_per_request"] >= 4 and b["nonce_tokens_per_request"] > 0
        assert "cache-free" in b["lane"]
        assert set(b["rates"]) == {"input", "cached_input", "output", "per"}
        assert b["table_version"] == "2026-08-31"
    # the T3 cell's brief describes the billed agent loop, not a single request
    agenticas = [f for f in doc["cells"] if f["workload"] == "multi_file"]
    assert all("agent" in f["brief"]["description"] for f in agenticas)
    assert fake_cli.calls == []  # the brief never touches the API
    _code, out, _err = run_cli(tmp_path, "predict", "--pricing-dir", pricing)
    assert "PENDING blind" in out and "12 cells, 0 blind" in out


def test_brief_marks_the_locked_cells_and_their_next_phase(tmp_path):
    pricing = pricing_dir(tmp_path)
    code, out, err = estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.05)
    assert code == 0, out or err
    doc = json_doc(tmp_path, "predict", "--pricing-dir", pricing)
    assert doc["counts"]["blind"] == 1
    qa = next(
        f for f in doc["cells"] if f["workload"] == "qa_short" and f["model"] == "glm-5.3-flash"
    )
    assert qa["brief"] is None  # estimated cells show no brief: nothing left to brief
    assert qa["blind"]["estimated_pp"] == 2.0 and qa["blind"]["estimated_usd"] == 0.05
    otras = [f for f in doc["cells"] if f["blind"] is None]
    assert len(otras) == 11  # the pending cells keep their public brief


# ---------------------------------------------------------------------------
# ordering: the flow refuses an estimate after the cell's real exists
# ---------------------------------------------------------------------------


def test_blind_estimate_refused_after_the_real_exists(tmp_path, fake_cli):
    pricing_dir(tmp_path)
    craft_cell(tmp_path, "qa_short", "glm-5.3-flash", reps={1: (80_000, 80_000, 2.0)})
    code, _out, err = estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 1.0, 0.01)
    assert code == 2 and "real already exists" in err
    assert "1 request lines, 1 batch lines" in err
    assert not (tmp_path / predict.PREDICT_DIR).exists()  # nothing was written
    # a batch line alone is evidence too (an aborted bracket still billed requests)
    craft_cell(tmp_path, "multi_turn", "kimi-k3", reps={1: (5_600, 1_600, 0.5)})
    code, _out, err = estimate(tmp_path, "blind", "multi_turn", "kimi-k3", 1.0, 0.01)
    assert code == 2 and "real already exists" in err
    # a cell without any raw evidence still takes its blind estimate
    code, out, err = estimate(tmp_path, "blind", "reasoning", "glm-5.3-flash", 1.0, 0.01)
    assert code == 0, out or err
    assert fake_cli.calls == []  # the flow never spends anything


def test_blind_estimate_is_one_per_cell_and_locked(tmp_path):
    pricing_dir(tmp_path)
    code, out, err = estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.05)
    assert code == 0, out or err
    code, _out, err = estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 3.0, 0.06)
    assert code == 2 and "locked" in err and "not revisable" in err
    lineas = [
        json.loads(cruda)
        for cruda in (tmp_path / predict.PREDICT_DIR / "estimates-phase1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if cruda.strip()
    ]
    assert len(lineas) == 1  # the second estimate never landed
    linea = lineas[0]
    validate_estimate_line(linea)
    assert predict.line_hash({k: v for k, v in linea.items() if k != "hash"}) == linea["hash"]
    assert linea["evidence"] == {"request_lines": 0, "batch_lines": 0}
    assert linea["phase"] == "blind" and linea["table_version"] == "2026-08-31"


def test_registry_tamper_refuses_everything_after_it(tmp_path):
    pricing_dir(tmp_path)
    assert estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.05)[0] == 0
    ruta = pathlib.Path(tmp_path, predict.PREDICT_DIR, "estimates-phase1.jsonl")
    linea = json.loads(ruta.read_text(encoding="utf-8").splitlines()[0])
    linea["estimated_pp"] = 1.0  # an estimate edited after its lock
    ruta.write_text(json.dumps(linea) + "\n", encoding="utf-8")
    code, _out, err = estimate(tmp_path, "blind", "multi_turn", "kimi-k3", 1.0, 0.01)
    assert code == 2 and "lock" in err and "edited" in err
    code, _out, err = run_cli(
        tmp_path, "predict", "--report", "--pricing-dir", str(pathlib.Path(tmp_path) / PRICING)
    )
    assert code == 2 and "lock" in err  # the report refuses a tampered registry too


def test_torn_and_foreign_registry_lines_are_refused(tmp_path):
    pricing_dir(tmp_path)
    assert estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.05)[0] == 0
    ruta = pathlib.Path(tmp_path, predict.PREDICT_DIR, "estimates-phase1.jsonl")
    ruta.write_text(ruta.read_text(encoding="utf-8") + "{torn\n", encoding="utf-8")
    code, _out, err = estimate(tmp_path, "blind", "multi_turn", "kimi-k3", 1.0, 0.01)
    assert code == 2 and "not JSON" in err
    ruta.write_text('{"junk": true}\n', encoding="utf-8")  # a foreign object, valid JSON
    code, _out, err = estimate(tmp_path, "blind", "multi_turn", "kimi-k3", 1.0, 0.01)
    assert code == 2 and "schema" in err


# ---------------------------------------------------------------------------
# the informed phase: re-estimation, gated on blind + measured evidence
# ---------------------------------------------------------------------------


def test_informed_needs_the_blind_estimate_first(tmp_path, fake_cli):
    pricing_dir(tmp_path)
    craft_cell(tmp_path, "qa_short", "glm-5.3-flash", reps={1: (80_000, 80_000, 2.0)})
    code, _out, err = estimate(tmp_path, "informed", "qa_short", "glm-5.3-flash", 2.0, 0.05)
    assert code == 2 and "blind estimate" in err
    assert not (tmp_path / predict.PREDICT_DIR / "estimates-phase2.jsonl").exists()
    assert fake_cli.calls == []


def test_informed_needs_measured_evidence(tmp_path):
    pricing_dir(tmp_path)
    assert estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.05)[0] == 0
    code, _out, err = estimate(tmp_path, "informed", "qa_short", "glm-5.3-flash", 2.0, 0.05)
    assert code == 2 and "no measured evidence" in err


def test_informed_records_after_blind_and_evidence(tmp_path, fake_cli):
    pricing_dir(tmp_path)
    assert estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.05)[0] == 0
    craft_cell(
        tmp_path,
        "qa_short",
        "glm-5.3-flash",
        reps={1: (80_000, 80_000, 2.0), 2: (80_000, 80_000, 2.0)},
    )
    code, out, err = estimate(tmp_path, "informed", "qa_short", "glm-5.3-flash", 2.0, 0.05)
    assert code == 0, out or err
    linea = json.loads(
        (tmp_path / predict.PREDICT_DIR / "estimates-phase2.jsonl").read_text(encoding="utf-8")
    )
    assert linea["evidence"] == {"request_lines": 2, "batch_lines": 2}
    assert linea["phase"] == "informed"
    code, _out, err = estimate(tmp_path, "informed", "qa_short", "glm-5.3-flash", 2.0, 0.05)
    assert code == 2 and "not revisable" in err


# ---------------------------------------------------------------------------
# the comparative MAPE report: manual math, bootstrap CI, verdict
# ---------------------------------------------------------------------------


def full_study(tmp_path) -> None:
    pricing_dir(tmp_path)
    estimate_all_blind(tmp_path)
    craft_study_dataset(tmp_path)
    estimate_all_informed(tmp_path)


def test_report_mape_matches_the_manual_math(tmp_path, fake_cli):
    full_study(tmp_path)
    antes = {
        str(p): p.read_bytes()
        for carpeta in ("runs", "batches")
        for p in sorted(pathlib.Path(tmp_path, carpeta).glob("*.jsonl"))
    }
    doc = report(tmp_path)

    assert fake_cli.calls == []  # the report never touches the API
    despues = {
        str(p): p.read_bytes()
        for carpeta in ("runs", "batches")
        for p in sorted(pathlib.Path(tmp_path, carpeta).glob("*.jsonl"))
    }
    assert despues == antes  # raw untouched
    assert doc["estimates"] == {"blind": 12, "informed": 4}

    # the blind phase: legacy over cells 1/2/4 (reasoning is sub-resolution),
    # new over the four cells whose extrapolation exists. The MAPEs are the
    # unrounded means of the per-cell APEs (full float precision).
    ciego = doc["aggregate"]["blind"]
    assert ciego["mape_legacy"] == {"mape": 1.6666666666666667, "cells": 3, "ci": [1.0, 3.0]}
    assert ciego["mape_new"]["cells"] == 4 and ciego["mape_new"]["mape"] == 0.325
    assert (
        0.0
        <= ciego["mape_new"]["ci"][0]
        <= ciego["mape_new"]["mape"]
        <= ciego["mape_new"]["ci"][1]
        <= 1.0
    )
    assert ciego["paired_cells"] == 3
    assert ciego["delta_mape"] == 1.5666666666666667  # mean(1,3,1) - mean(0.2,0,0.1)
    assert ciego["ci_delta"][0] > 0  # every paired resample keeps legacy worse
    assert ciego["verdict"] == "legacy less predictable"
    assert ciego["ollama_claim"] == "supported"
    # the S1 sensitivity prices the same estimates against the cached-input mix
    assert (
        ciego["mape_new_s1"]["cells"] == 4
        and ciego["mape_new_s1"]["mape"] != ciego["mape_new"]["mape"]
    )

    # the informed phase: the re-estimation is exact, the comparison unresolved
    informada = doc["aggregate"]["informed"]
    assert informada["mape_legacy"] == {"mape": 0.0, "cells": 3, "ci": [0.0, 0.0]}
    assert informada["mape_new"]["mape"] == 0.0
    assert informada["delta_mape"] == 0.0 and informada["ci_delta"] == [0.0, 0.0]
    assert informada["verdict"] == "unresolved at this sample size"
    assert informada["ollama_claim"] == "not resolved"

    # per-cell APEs match the hand math
    qa = next(
        c for c in doc["cells"] if c["workload"] == "qa_short" and c["model"] == "glm-5.3-flash"
    )
    assert qa["real_pp"] == 2.0 and qa["real_new_s0_usd_per_run"] == 0.052
    assert qa["blind"]["ape_legacy"] == 1.0 and qa["blind"]["ape_new"] == 0.2
    assert qa["informed"]["ape_legacy"] == 0.0 and qa["informed"]["ape_new"] == 0.0
    multi = next(
        c for c in doc["cells"] if c["workload"] == "multi_turn" and c["model"] == "kimi-k3"
    )
    assert multi["real_pp"] == 1.0 and multi["real_new_s0_usd_per_run"] == 0.0408
    assert multi["blind"]["ape_legacy"] == 3.0 and multi["blind"]["ape_new"] == 0.0
    archivo = next(
        c for c in doc["cells"] if c["workload"] == "multi_file" and c["model"] == "kimi-k2.7-code"
    )
    assert archivo["real_pp"] == 4.0 and archivo["real_new_s0_usd_per_run"] == 0.2625
    assert archivo["blind"]["ape_new"] == 0.09999999999999998  # (0.28875-0.2625)/0.2625, exact

    # the per-workload breakdown carries the same numbers, grouped
    desglose = {w["workload"]: w for w in doc["workloads"]}
    assert desglose["qa_short"]["blind"] == {"mape_legacy": 1.0, "mape_new": 0.2}
    assert desglose["multi_turn"]["blind"] == {"mape_legacy": 3.0, "mape_new": 0.0}
    assert desglose["multi_file"]["blind"] == {
        "mape_legacy": 1.0,
        "mape_new": 0.09999999999999998,
    }

    ruta = pathlib.Path(tmp_path, predict.PREDICT_DIR, "report.json")
    assert (
        ruta.exists()
        and json.loads(ruta.read_text(encoding="utf-8"))["kind"] == "predictability-report"
    )


def test_report_excludes_sub_resolution_cells_from_the_legacy_side(tmp_path):
    full_study(tmp_path)
    doc = report(tmp_path)
    reasoning = next(
        c for c in doc["cells"] if c["workload"] == "reasoning" and c["model"] == "glm-5.3-flash"
    )
    # the real exists but sits under the tick: no legacy APE anywhere
    assert reasoning["real_pp"] == 0.05 and reasoning["legacy_status"] == "sub_resolution"
    assert reasoning["blind"]["ape_legacy"] is None
    assert reasoning["informed"]["ape_legacy"] is None
    assert reasoning["blind"]["ape_new"] == 1.0  # the new side has no such floor
    hallazgos = doc["findings"]
    assert hallazgos["sub_resolution_legacy"] == [
        "reasoning/glm-5.3-flash (real 0.05 pp, under the 0.1 pp tick)"
    ]
    # the blind legacy MAPE counted 3 cells, not 4
    assert doc["aggregate"]["blind"]["mape_legacy"]["cells"] == 3
    _code, out, _err = run_cli(
        tmp_path, "predict", "--report", "--pricing-dir", str(pathlib.Path(tmp_path) / PRICING)
    )
    assert "sub-resolution (excluded from the legacy side)" in out
    assert "reasoning/glm-5.3-flash (real 0.05 pp, under the 0.1 pp tick)" in out


def test_report_lists_unmeasured_and_pending_cells(tmp_path):
    full_study(tmp_path)
    doc = report(tmp_path)
    hallazgos = doc["findings"]
    assert len(hallazgos["unmeasured"]) == 8
    assert hallazgos["pending_blind"] == []
    assert len(hallazgos["pending_informed"]) == 8  # blind but never re-estimated
    # an unmeasured cell carries no real and no APE, even with an estimate in hand
    larga = next(
        c for c in doc["cells"] if c["workload"] == "long_context" and c["model"] == "glm-5.3-flash"
    )
    assert larga["real_pp"] is None and larga["legacy_status"] == "unmeasured"
    assert larga["blind"]["ape_legacy"] is None and larga["blind"]["ape_new"] is None


def test_report_without_raw_data_is_a_clean_error(tmp_path):
    pricing_dir(tmp_path)
    assert estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.05)[0] == 0
    code, _out, err = run_cli(
        tmp_path, "predict", "--report", "--pricing-dir", str(pathlib.Path(tmp_path) / PRICING)
    )
    assert code == 2 and "raw" in err.lower() and "Traceback" not in err
    assert not (tmp_path / predict.PREDICT_DIR / "report.json").exists()


def test_report_without_estimates_reports_only_findings(tmp_path, fake_cli):
    pricing_dir(tmp_path)
    craft_study_dataset(tmp_path)
    doc = report(tmp_path)
    assert doc["aggregate"] == {}  # nothing estimated: nothing to compare
    assert doc["estimates"] == {"blind": 0, "informed": 0}
    assert len(doc["findings"]["pending_blind"]) == 12
    assert fake_cli.calls == []
    _code, out, _err = run_cli(
        tmp_path, "predict", "--report", "--pricing-dir", str(pathlib.Path(tmp_path) / PRICING)
    )
    assert "blind: no estimates recorded" in out


def test_report_bootstrap_seed_is_fixed(tmp_path):
    full_study(tmp_path)
    uno = report(tmp_path)
    dos = report(tmp_path)
    assert (
        uno["aggregate"]["blind"]["mape_legacy"]["ci"]
        == dos["aggregate"]["blind"]["mape_legacy"]["ci"]
    )
    assert uno["aggregate"]["blind"]["ci_delta"] == dos["aggregate"]["blind"]["ci_delta"]
    assert uno["params"]["bootstrap_seed"] == dos["params"]["bootstrap_seed"]


# ---------------------------------------------------------------------------
# CLI discipline: the flags predict does not read are refused, cleanly
# ---------------------------------------------------------------------------


def test_refuses_flags_it_does_not_read(tmp_path):
    pricing = pricing_dir(tmp_path)
    code, _out, err = run_cli(tmp_path, "predict", "--reps", "5", "--pricing-dir", pricing)
    assert code == 2 and "unrecognized" in err
    code, _out, err = run_cli(tmp_path, "predict", "--k", "4", "--pricing-dir", pricing)
    assert code == 2 and "unrecognized" in err
    code, _out, err = run_cli(tmp_path, "predict", "--ancla", "100", "--pricing-dir", pricing)
    assert code == 2 and "unrecognized" in err  # the MAPEs are native-unit: no anchor
    code, _out, err = run_cli(tmp_path, "predict", "--level", "T1", "--pricing-dir", pricing)
    assert code == 2 and "no --level" in err


def test_recording_flags_need_a_phase_and_a_full_cell(tmp_path):
    pricing = pricing_dir(tmp_path)
    code, _out, err = run_cli(tmp_path, "predict", "--pp", "2.0", "--pricing-dir", pricing)
    assert code == 2 and "--phase" in err
    code, _out, err = run_cli(
        tmp_path, "predict", "--workload", "qa_short", "--pricing-dir", pricing
    )
    assert code == 2 and "--phase" in err
    code, _out, err = run_cli(
        tmp_path,
        "predict",
        "--phase",
        "blind",
        "--workload",
        "qa_short",
        "--pp",
        "2.0",
        "--pricing-dir",
        pricing,
    )
    assert code == 2 and "--model" in err
    code, _out, err = run_cli(
        tmp_path,
        "predict",
        "--phase",
        "blind",
        "--workload",
        "qa_short",
        "--model",
        "glm-5.3-flash",
        "--usd",
        "0.05",
        "--pricing-dir",
        pricing,
    )
    assert code == 2 and "--pp" in err
    code, _out, err = run_cli(
        tmp_path, "predict", "--report", "--workload", "qa_short", "--pricing-dir", pricing
    )
    assert code == 2 and "whole grid" in err


def test_estimate_values_are_validated(tmp_path):
    pricing_dir(tmp_path)
    for pp, usd in ((0, 0.01), (-1, 0.01), (2.0, 0), (2.0, -0.5)):
        code, _out, err = estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", pp, usd)
        assert code == 2 and "> 0" in err, (pp, usd)
    code, _out, err = estimate(tmp_path, "blind", "qa_short", "no-existe", 2.0, 0.01)
    assert code == 2 and "not one of the predictability cells" in err
    code, _out, err = run_cli(
        tmp_path,
        "predict",
        "--phase",
        "blind",
        "--workload",
        "tool_calling",
        "--model",
        "glm-5.3-flash",
        "--pp",
        "1",
        "--usd",
        "0.01",
        "--pricing-dir",
        str(pathlib.Path(tmp_path) / PRICING),
    )
    assert code == 2 and "not one of the predictability cells" in err  # outside the grid
    code, _out, err = estimate(
        tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.01, "--s", "1.5"
    )
    assert code == 2 and "--s must be in [0, 1]" in err
    assert not (tmp_path / predict.PREDICT_DIR).exists()


def test_infinite_estimates_are_refused_never_locked(tmp_path):
    """--pp 1e999 parses to +inf: a bare '> 0' guard would lock an infinite
    estimate into a hash-chained, never-revisable registry and write bare
    Infinity tokens the report could never parse back."""
    pricing_dir(tmp_path)
    for pp, usd in (("1e999", 0.01), (2.0, "1e999")):
        code, _out, err = estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", pp, usd)
        assert code == 2 and "finite" in err, (pp, usd)
    assert not (tmp_path / predict.PREDICT_DIR).exists()
    # the cell is not bricked: a real estimate still locks
    code, out, err = estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.05)
    assert code == 0, out or err
    reporte = estimate(tmp_path, "informed", "qa_short", "glm-5.3-flash", 2.0, 0.05)
    assert reporte[0] == 2 and "no measured evidence" in reporte[2]  # informed still gated


def test_brief_with_a_table_missing_a_grid_model_is_a_clean_error(tmp_path):
    """A table that predates kimi-k3 must refuse the walk-through with the
    harness's 'error: ...' exit 2, never a TableError traceback."""
    write_table(
        tmp_path / PRICING,
        "2026-08-31",
        {m: r for m, r in standard_table().items() if m != "kimi-k3"},
    )
    code, _out, err = run_cli(
        tmp_path, "predict", "--pricing-dir", str(pathlib.Path(tmp_path) / PRICING)
    )
    assert code == 2 and "kimi-k3" in err and "Traceback" not in err


def test_report_sets_aside_estimates_from_another_table_vintage(tmp_path):
    """Estimates locked against one table and a real priced on another do not
    divide - the repricing itself would become the error. The new-side APEs are
    set aside and flagged; the legacy APE stands (pp is meter-native)."""
    pricing = pricing_dir(tmp_path)
    full_study(tmp_path)  # the estimates lock while only the 08-31 table exists
    encarecida = {m: {k: v * 1.2 for k, v in r.items()} for m, r in standard_table().items()}
    write_table(tmp_path / PRICING, "2026-09-01", encarecida)
    code, salida, err = run_cli(
        tmp_path,
        "predict",
        "--report",
        "--table-version",
        "2026-09-01",
        "--pricing-dir",
        pricing,
        "--json",
    )
    assert code == 0, salida or err
    doc = json.loads(salida)
    assert doc["table_version"] == "2026-09-01"
    qa = next(
        c for c in doc["cells"] if c["workload"] == "qa_short" and c["model"] == "glm-5.3-flash"
    )
    assert qa["blind"]["table_vintage_mismatch"] is True
    assert qa["blind"]["ape_legacy"] == 1.0  # the legacy side never moved
    assert qa["blind"]["ape_new"] is None and qa["blind"]["ape_new_s1"] is None
    ciego = doc["aggregate"]["blind"]
    assert ciego["mape_legacy"] is not None  # pp comparisons are vintage-proof
    assert ciego["mape_new"] is None and ciego["paired_cells"] == 0
    assert ciego["verdict"] == "no comparison"
    obsoletas = doc["findings"]["stale_table_estimates"]
    assert len(obsoletas) == 16  # 12 blind + 4 informed records, each flagged once
    assert {e.split(" (")[0] for e in obsoletas} == {f"{w}/{m}" for w, m in predict._GRID}
    # and on its own vintage the same estimates price normally
    doc2 = report(tmp_path, "--table-version", "2026-08-31")
    qa2 = next(
        c for c in doc2["cells"] if c["workload"] == "qa_short" and c["model"] == "glm-5.3-flash"
    )
    assert qa2["blind"]["table_vintage_mismatch"] is False
    assert qa2["blind"]["ape_new"] == 0.2
    assert doc2["findings"]["stale_table_estimates"] == []


def test_the_flow_needs_no_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    pricing = pricing_dir(tmp_path)
    code, out, err = estimate(tmp_path, "blind", "qa_short", "glm-5.3-flash", 2.0, 0.05)
    assert code == 0, out or err
    code, _out, err = run_cli(tmp_path, "predict", "--pricing-dir", pricing)
    assert code == 0, err
    craft_cell(tmp_path, "qa_short", "glm-5.3-flash", reps={1: (80_000, 80_000, 2.0)})
    code, _out, err = run_cli(
        tmp_path, "predict", "--report", "--pricing-dir", str(pathlib.Path(tmp_path) / PRICING)
    )
    assert code == 0, err  # the report is offline, like analyze
