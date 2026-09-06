"""`--model` as a 1..N list (issue #49): dry-run/run replace the slate with it.

The list never adds outside the level's slate (outsiders are refused), dedupes,
and the gate binds what will bill to what the dry-run approved (run models must
be a subset of the mark's; old marks without the key skip the check). Single-
model consumers keep their exactly-one contract.
"""

from __future__ import annotations

import json

import pytest
from test_dry_run import run_cli, with_pricing
from test_run import run_t1

DOS = ("--model", "glm-5.3-flash", "kimi-k3")


def read_lines(tmp_path, dirname, pattern) -> list[dict]:
    return [
        json.loads(l)
        for archivo in sorted((tmp_path / dirname).glob(pattern))
        for l in archivo.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


def mark_models(tmp_path, level="T1") -> list:
    marca = json.loads((tmp_path / "runs" / f"gate-{level}.json").read_text(encoding="utf-8"))
    return marca["estimado"]["models"]


def narrow_dry_run(tmp_path, pricing, level, *model) -> None:
    """Runs a free dry-run at reps=1 with an optional model list (mark recorded)."""
    codigo, _, errores = run_cli(
        tmp_path, "dry-run", "--level", level, "--reps", "1", "--pricing-dir", pricing, *model
    )
    assert codigo == 0, errores


def test_dry_run_prices_and_records_the_requested_models(tmp_path, fake):
    pricing = with_pricing(tmp_path)
    narrow_dry_run(tmp_path, pricing, "T1", *DOS)
    _, salida, _ = run_cli(
        tmp_path,
        "dry-run",
        "--level",
        "T1",
        "--reps",
        "1",
        "--pricing-dir",
        pricing,
        *DOS,
        "--json",
    )
    doc = json.loads(salida)
    assert all(fila["models"] == 2 for fila in doc["rows"])
    assert all(fila["requests"] == 20 * 2 for fila in doc["rows"] if fila["workload"] == "qa_short")
    assert doc["models"] == ["glm-5.3-flash", "kimi-k3"]  # the mark's approved set
    assert fake.calls == []


def test_dry_run_without_model_records_the_full_slate(tmp_path, fake):
    pricing = with_pricing(tmp_path)
    narrow_dry_run(tmp_path, pricing, "T2")
    doc = mark_models(tmp_path, "T2")
    assert len(doc) == 6 and "glm-5.3-flash" in doc and "kimi-k3" in doc
    codigo, salida, _ = run_cli(
        tmp_path, "dry-run", "--level", "T2", "--reps", "1", "--pricing-dir", pricing, "--json"
    )
    assert codigo == 0
    filas = json.loads(salida)["rows"]
    assert all(fila["models"] == 6 for fila in filas)


def test_dry_run_dedupes_preserving_order(tmp_path, fake):
    pricing = with_pricing(tmp_path)
    narrow_dry_run(tmp_path, pricing, "T1", "--model", "kimi-k3", "kimi-k3", "glm-5.2")
    assert mark_models(tmp_path) == ["kimi-k3", "glm-5.2"]
    _, salida, _ = run_cli(
        tmp_path,
        "dry-run",
        "--level",
        "T1",
        "--reps",
        "1",
        "--pricing-dir",
        pricing,
        "--model",
        "kimi-k3",
        "kimi-k3",
        "glm-5.2",
        "--json",
    )
    assert all(fila["models"] == 2 for fila in json.loads(salida)["rows"])


def test_dry_run_refuses_models_outside_the_slate(tmp_path, fake):
    pricing = with_pricing(tmp_path)
    codigo, _, errores = run_cli(
        tmp_path,
        "dry-run",
        "--level",
        "T2",
        "--reps",
        "1",
        "--pricing-dir",
        pricing,
        "--model",
        "glm-5.3-flash",
        "no-existe",
    )
    assert codigo == 2
    assert "no-existe" in errores and "T2 slate" in errores
    assert fake.calls == []


def test_run_bills_exactly_the_requested_models(tmp_path, fake_cli):
    pricing = with_pricing(tmp_path)
    narrow_dry_run(tmp_path, pricing, "T1", *DOS)
    code, out, err = run_t1(tmp_path, "--reps", "1", *DOS)
    assert code == 0, out or err
    batches = read_lines(tmp_path, "batches", "batches-*.jsonl")
    requests = read_lines(tmp_path, "runs", "requests-*.jsonl")
    assert sorted({b["model"] for b in batches}) == ["glm-5.3-flash", "kimi-k3"]
    assert len(batches) == 2 * 3 and len(requests) == 2 * 24


def test_run_refuses_models_outside_the_slate(tmp_path, fake_cli):
    pricing = with_pricing(tmp_path)
    narrow_dry_run(tmp_path, pricing, "T1")
    codigo, _, errores = run_cli(
        tmp_path, "run", "--level", "T1", "--reps", "1", "--model", "no-existe"
    )
    assert codigo == 2
    assert "no-existe" in errores
    assert fake_cli.calls == []


def test_gate_refuses_models_the_dry_run_did_not_approve(tmp_path, fake_cli):
    pricing = with_pricing(tmp_path)
    narrow_dry_run(tmp_path, pricing, "T1", *DOS)  # narrowed mark
    codigo, _, errores = run_t1(tmp_path, "--reps", "1")  # full slate would bill more
    assert codigo == 2
    assert fake_cli.calls == []  # refused before any spend
    assert "never bill what the dry-run did not approve" in errores


def test_gate_accepts_a_narrower_run_than_approved(tmp_path, fake_cli):
    pricing = with_pricing(tmp_path)
    narrow_dry_run(tmp_path, pricing, "T1")  # full-slate mark
    code, out, err = run_t1(tmp_path, "--reps", "1", *DOS)  # subset of it
    assert code == 0, out or err
    batches = read_lines(tmp_path, "batches", "batches-*.jsonl")
    assert sorted({b["model"] for b in batches}) == ["glm-5.3-flash", "kimi-k3"]


def test_old_marks_without_models_skip_the_subset_check(tmp_path, fake_cli):
    """A pre-#49 mark never recorded models: the run's narrowing cannot have
    exceeded it, so the gate opens (compat with the frozen harness's marks)."""
    from obench.gate import mark_dry_run

    pricing = with_pricing(tmp_path)
    mark_dry_run(tmp_path, "T1", {"table_version": "2026-08-31", "reps": 1, "rows": [{"a": 1}]})
    code, out, err = run_t1(tmp_path, "--reps", "1", *DOS)
    assert code == 0, out or err


def test_t2_subset_run_bills_only_the_list(tmp_path, fake_cli):
    pricing = with_pricing(tmp_path)
    narrow_dry_run(tmp_path, pricing, "T2", *DOS)
    codigo, salida, errores = run_cli(
        tmp_path,
        "run",
        "--level",
        "T2",
        "--reps",
        "1",
        "--settle-s",
        "2",
        "--settle-poll-s",
        "0.01",
        *DOS,
    )
    assert codigo == 0, salida or errores
    batches = read_lines(tmp_path, "batches", "batches-*.jsonl")
    assert sorted({b["model"] for b in batches}) == ["glm-5.3-flash", "kimi-k3"]


def test_probe_concurrency_keeps_the_single_model_contract(tmp_path, fake_cli):
    pricing = with_pricing(tmp_path)
    narrow_dry_run(tmp_path, pricing, "T1")
    codigo, _, errores = run_cli(tmp_path, "probe-concurrency", "--pricing-dir", pricing, *DOS)
    assert codigo == 2
    assert "exactly one" in errores


def test_predict_and_analyze_keep_the_single_model_contract(tmp_path, fake_cli):
    pricing = with_pricing(tmp_path)
    predict = run_cli(tmp_path, "predict", "--pricing-dir", pricing, *DOS)
    analyze = run_cli(tmp_path, "analyze", "--pricing-dir", pricing, *DOS)
    assert predict[0] == 2 and "exactly one" in predict[2]
    assert analyze[0] == 2 and "exactly one" in analyze[2]
    assert fake_cli.calls == []


def test_gate_subset_unit(tmp_path):
    from obench.gate import GateClosed, mark_dry_run, require_dry_run

    mark_dry_run(
        tmp_path,
        "T1",
        {"table_version": "2026-08-31", "reps": 1, "models": ["a", "b"], "rows": [{"x": 1}]},
    )
    require_dry_run(tmp_path, "T1", models=["b"])  # subset: opens
    require_dry_run(tmp_path, "T1", models=["a", "b"])  # equal: opens
    with pytest.raises(GateClosed, match="bill"):
        require_dry_run(tmp_path, "T1", models=["b", "c"])
