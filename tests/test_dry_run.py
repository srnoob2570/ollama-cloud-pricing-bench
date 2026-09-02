"""`bench dry-run` and the spending gate: external behavior of ticket Harness 01."""

from __future__ import annotations

import contextlib
import io
import json

from conftest import standard_table, write_table

from ocharness.cli import main


def run_cli(tmp_path, *args) -> tuple[int, str, str]:
    """Runs the CLI at the seam, capturing stdout/stderr (argparse included)."""
    salida, errores = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
            codigo = main(["--base", str(tmp_path), *args])
    except SystemExit as e:  # argparse exits 2 on usage errors
        codigo = int(e.code or 0)
    return codigo, salida.getvalue(), errores.getvalue()


def json_doc(tmp_path, *args) -> dict:
    codigo, salida, errores = run_cli(tmp_path, *args, "--json")
    assert codigo == 0, salida or errores
    return json.loads(salida)


def with_pricing(tmp_path, version: str = "2026-08-31") -> str:
    write_table(tmp_path / "pricing", version, standard_table())
    return str(tmp_path / "pricing")


def test_dry_run_estimates_s0_s1_without_api_calls(tmp_path, fake, capsys):
    pricing = with_pricing(tmp_path)
    doc = json_doc(tmp_path, "dry-run", "--level", "T1", "--pricing-dir", pricing)
    assert fake.calls == []  # zero requests: the dry-run is free
    assert doc["table_version"] == "2026-08-31"
    filas = doc["rows"]
    assert [f["workload"] for f in filas] == ["qa_short", "calibration", "throughput"]
    qa = filas[0]
    assert qa["models"] == 19 and qa["requests"] == 1900  # 20 req × 19 models × n=5
    assert qa["cost_s0"] > 0
    assert qa["cost_s1"] < qa["cost_s0"]  # glm/kimi discount cache; the total drops
    assert qa["pp_expected"] is None  # no pp/1M calibration yet


def test_dry_run_cache_discount_only_when_table_has_one(tmp_path, fake, capsys):
    """A table where every model prices cached=input means S1 equals S0 in every row."""
    from conftest import no_discount_table

    pricing = write_table(tmp_path / "pricing", "2026-08-31", no_discount_table())
    doc = json_doc(tmp_path, "dry-run", "--level", "T1", "--pricing-dir", str(pricing))
    assert all(f["cost_s1"] == f["cost_s0"] for f in doc["rows"])


def test_run_refuses_without_dry_run(tmp_path, fake, capsys):
    pricing = with_pricing(tmp_path)
    codigo, _, errores = run_cli(tmp_path, "run", "--level", "T1", "--pricing-dir", pricing)
    assert codigo == 2
    assert "dry-run" in errores
    assert fake.calls == []  # refusing also spends nothing


def test_gate_passes_then_run_demands_the_api_key(tmp_path, fake, capsys, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    pricing = with_pricing(tmp_path)
    assert run_cli(tmp_path, "dry-run", "--level", "T1", "--pricing-dir", pricing)[0] == 0
    codigo, _, errores = run_cli(tmp_path, "run", "--level", "T1")
    assert codigo == 2  # the gate opened; the run stops at the missing key, mark intact
    assert "OLLAMA_API_KEY" in errores


def test_missing_table_gives_clean_error(tmp_path, fake, capsys):
    codigo, _, errores = run_cli(
        tmp_path, "dry-run", "--level", "T1", "--pricing-dir", str(tmp_path / "empty")
    )
    assert codigo == 2
    assert "price" in errores.lower()
    assert "traceback" not in errores.lower()


def test_table_version_selects_snapshot(tmp_path, fake, capsys):
    pricing = str(tmp_path / "pricing")
    write_table(tmp_path / "pricing", "2026-09-01", standard_table())
    write_table(tmp_path / "pricing", "2026-08-31", standard_table())
    doc = json_doc(
        tmp_path,
        "dry-run",
        "--level",
        "T1",
        "--pricing-dir",
        pricing,
        "--table-version",
        "2026-09-01",
    )
    assert doc["table_version"] == "2026-09-01"
    doc2 = json_doc(
        tmp_path,
        "dry-run",
        "--level",
        "T1",
        "--pricing-dir",
        pricing,
        "--table-version",
        "2026-08-31",
    )
    assert doc2["table_version"] == "2026-08-31"


def test_s_and_reps_are_validated_parameters(tmp_path, fake, capsys):
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing, "--s", "1.5")[0]
        == 2
    )
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing, "--reps", "0")[0]
        == 2
    )
    doc_s0 = json_doc(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing, "--s", "0.0")
    doc_s1 = json_doc(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing, "--s", "1.0")
    assert any(
        a["cost_s1"] != b["cost_s1"] for a, b in zip(doc_s0["rows"], doc_s1["rows"], strict=True)
    )


def test_pricing_dir_resolves_against_base(tmp_path, fake, capsys):
    """--pricing-dir is relative to --base, not to the caller's cwd."""
    with_pricing(tmp_path)
    codigo, salida, errores = run_cli(tmp_path, "dry-run", "--level", "T1")
    assert codigo == 0, salida or errores  # default "pricing" resolves under --base


def test_dry_run_estimates_all_three_levels(tmp_path, fake, capsys):
    pricing = with_pricing(tmp_path)
    for nivel, n_filas in (("T1", 3), ("T2", 7), ("T3", 3)):
        doc = json_doc(tmp_path, "dry-run", "--level", nivel, "--pricing-dir", pricing)
        assert len(doc["rows"]) == n_filas
        assert all(f["models"] >= 3 for f in doc["rows"])
        assert all(f["pp_expected"] is None for f in doc["rows"])  # unmeasured until calibration
    assert run_cli(tmp_path, "dry-run", "--level", "T9", "--pricing-dir", pricing)[0] == 2


def test_malformed_table_gives_clean_error(tmp_path, fake, capsys):
    """A broken table is a clean data error, never a traceback (Harness-01 review)."""
    pricing = tmp_path / "pricing"
    pricing.mkdir()
    (pricing / "mala.json").write_text(
        '{"table_version": "x", "models": {"m": {"input": "a"}}}', encoding="utf-8"
    )
    codigo, _, errores = run_cli(
        tmp_path, "dry-run", "--level", "T1", "--pricing-dir", str(pricing)
    )
    assert codigo == 2
    assert "invalid rates" in errores
    assert "Traceback" not in errores


def test_dry_run_estimates_carry_the_lane_nonce_and_the_canary(tmp_path, fake):
    """Protocol v3: the estimate prices what will actually be sent — the
    cache-free lane's per-request nonce rides tokens_in, broken out as its own
    field — and the billing canary's once-per-run spend is stated, not hidden."""
    pricing = with_pricing(tmp_path)
    doc = json_doc(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing)
    assert fake.calls == []
    for fila in doc["rows"]:
        assert fila["nonce_tokens"] > 0
        # The nonce is bounded: [4, 400] words x the tokenization allowance.
        assert fila["nonce_tokens"] <= 400 * 1.3 * fila["requests"] + 1
    # The long_context row's overhead is ~1.5 % of its body (clamped to 400 words).
    larga = next(f for f in doc["rows"] if f["workload"] == "long_context")
    por_request = larga["nonce_tokens"] // larga["requests"]
    assert por_request == int(400 * 1.3)  # the clamp's ceiling, priced
    canario = doc["canary"]
    assert canario["requests"] == 10 and canario["tokens_estimate"] > 0
    assert "ratio above 0.5" in canario["note"]
    _code, out, _err = run_cli(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing)
    assert "billing canary (once per run)" in out and "nonce" in out


def test_t3_budget_prices_the_agent_loops_worst_case(tmp_path, fake, capsys):
    """A T3 task is an agent loop, not one request: up to MAX_STEPS billed
    consultations, each re-sending the task plus the transcript grown so far,
    each with its nonce — the gate approves the worst case, never a fraction
    of it ('a run may never bill more than the dry-run approved')."""
    from ocharness import lane
    from ocharness.fixtures_t3 import MAX_STEPS
    from ocharness.workloads import SLATE_T3

    pricing = with_pricing(tmp_path)
    doc = json_doc(tmp_path, "dry-run", "--level", "T3", "--reps", "1", "--pricing-dir", pricing)
    fila = next(f for f in doc["rows"] if f["workload"] == "multi_file")
    nonce = lane.nonce_tokens_estimate(150_000)  # the workload's own nonce size
    # per model: MAX_STEPS consultations, step p carries the task + nonce + the
    # p prior outputs; the output side bills MAX_STEPS answers
    esperado_in = sum(150_000 + nonce + paso * 30_000 for paso in range(MAX_STEPS))
    esperado_out = 30_000 * MAX_STEPS
    assert fila["requests"] == len(SLATE_T3) * 1 * 1 * MAX_STEPS
    assert fila["tokens_in"] == len(SLATE_T3) * esperado_in
    assert fila["tokens_out"] == len(SLATE_T3) * esperado_out
    assert fila["cost_s0"] > 0  # and it prices strictly above the one-request story
    una = 150_000 + nonce  # what a single-request budget would send in, per model
    assert fila["tokens_in"] > len(SLATE_T3) * una
