"""`bench analyze`: the re-run without re-measuring (ticket Harness 08).

The analysis reads only the raw datasets (runs/*.jsonl + batches/*.jsonl), the
versioned price table and the analysis parameters (--table-version/--ancla/--s):
every assertion here is on produced artifacts, and the fake observes ZERO new
requests during any analyze invocation.

The manual-math dataset (three measured cells + one unmeasured) is hand-crafted
raw JSONL so every expected number below is computed by hand in the comments.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib

from conftest import write_table
from test_dry_run import run_cli  # noqa: F401  (the shared CLI-seam runner, kept for parity)

from ocharness.client import PROTOCOL_VERSION
from ocharness.concurrency import usd_per_pp
from ocharness.schema import validate_batch_line, validate_request_line

# The default anchor ($100/mo) bridged to USD per weekly pp (tested primitive).
U = usd_per_pp(100.0)


def analyze_cli(tmp_path, *args) -> tuple[int, str, str]:
    salida, errores = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
            from ocharness.cli import main

            codigo = main(["--base", str(tmp_path), "analyze", *args])
    except SystemExit as e:  # argparse exits 2 on usage errors
        codigo = int(e.code or 0)
    return codigo, salida.getvalue(), errores.getvalue()


def analyze_doc(tmp_path, *args) -> dict:
    codigo, salida, errores = analyze_cli(tmp_path, *args, "--json")
    assert codigo == 0, salida or errores
    return json.loads(salida)


# ---------------------------------------------------------------------------
# the known fake dataset: hand-crafted raw lines, schema-valid on write
# ---------------------------------------------------------------------------

TABLE_V1 = {
    "alpha": {"input": 0.60, "cached_input": 0.30, "output": 1.20},  # cache discount
    "beta": {"input": 1.00, "cached_input": 1.00, "output": 2.00},  # cached=input
    "gamma": {"input": 0.50, "cached_input": 0.25, "output": 1.50},
}

RUN = "run-test"
FIX = "fix" * 16  # fixture_hash (any str for the schema)


def req(
    workload: str,
    model: str,
    batch_id: str,
    *,
    rep: int = 1,
    k: int = 1,
    tok_in: int = 1000,
    tok_out: int = 500,
    checker: str | None = "pass",
    index: int = 0,
) -> dict:
    return {
        "req_id": f"{batch_id}-{index:04d}",
        "batch_id": batch_id,
        "run_id": RUN,
        "level": "T1",
        "workload": workload,
        "model": model,
        "seed": index + 1,
        "rep": rep,
        "k": k,
        "t_start": 1.0 + index,
        "t_first_chunk": 1.05 + index,
        "t_total": 2.0 + index,
        "chunks": 3,
        "tok_in": tok_in,
        "tok_out": tok_out,
        "tok_cached": None,
        "prompt_sha256": "p" * 64,
        "nonce_sha256": "n" * 64,
        "api": {"done": True},
        "http": 200,
        "err": None,
        "checker": checker,
        "tool_calls": [],
        "steps": [],
        "sandbox": None,
        "out_text_hash": "h" * 64,
        "fixture_hash": FIX,
        "table_version": "2026-08-31",
        "protocol_version": PROTOCOL_VERSION,
    }


def batch(
    workload: str | None,
    model: str,
    batch_id: str,
    *,
    rep: int = 1,
    k: int = 1,
    n: int = 1,
    reps: int = 1,
    pool: dict | None = None,
    dpp_weekly: float | None = 0.2,
) -> dict:
    medidor_post = (
        None
        if dpp_weekly is None
        else {"limits": {"session": {"usage": 0.6}, "weekly": {"usage": 0.7}}}
    )
    return {
        "batch_id": batch_id,
        "run_id": RUN,
        "level": "T1",
        "workload": workload,
        "model": model,
        "fixture_hash": FIX,
        "k": k,
        "n": n,
        "reps": reps,
        "pool": pool,
        "settle_s": 60.0,
        "settle_mode": "registration",
        "settle_reads": 2,
        "registered_session_s": 0.0,
        "registered_weekly_s": 0.0,
        "settle_exit": "stable",
        "count_check_s": 0.5,
        "wall_clock_s": 10.0,
        "medidor_pre": {"limits": {"session": {"usage": 0.5}, "weekly": {"usage": 0.5}}},
        "medidor_post": medidor_post,
        "dpp_session": dpp_weekly,
        "dpp_weekly": dpp_weekly,
        "request_counts": {"pre": {}, "count_check": {}, "post": {}},
        "table_version": "2026-08-31",
        "protocol_version": PROTOCOL_VERSION,
        "notes": "",
    }


def write_raw(base: pathlib.Path, requests: list[dict], batches: list[dict]) -> None:
    for r in requests:
        validate_request_line(r)
    for b in batches:
        validate_batch_line(b)
    (base / "runs").mkdir(parents=True, exist_ok=True)
    (base / "batches").mkdir(parents=True, exist_ok=True)
    with (base / "runs" / f"requests-{RUN}.jsonl").open("a", encoding="utf-8") as f:
        for r in requests:
            f.write(json.dumps(r) + "\n")
    with (base / "batches" / f"batches-{RUN}.jsonl").open("a", encoding="utf-8") as f:
        for b in batches:
            f.write(json.dumps(b) + "\n")


def craft_dataset(base: pathlib.Path) -> None:
    """Three measured cells + one unmeasured, with hand-computable numbers.

    - alpha/qa_short: 2 reps x 2 requests (1000 in / 500 out), dpp 0.2 each
      -> pp/1M = 0.2 / 0.003 = 66.6667; legacy $/task = 0.2*U/2 = 0.023015;
      new S0 = 0.0012, S1(s=.5) = 0.00105; threshold S0 = 0.9/U = 3.9103.
    - beta/throughput: 1 request of 2M in / 0 out, dpp 0.2
      -> pp/1M = 0.1; legacy $/task = 0.2*U; new S0 = $2.0 -> LEGACY wins.
    - beta/qa_short: 1 request of 500k in / 750k out, dpp 9.6
      -> pp/1M = 9.6 / 1.25 = 7.68; legacy $/task = 9.6*U = 2.20944 vs new S0
      = $2.0 -> NEW wins by a real margin, and the winner FLIPS to legacy at
      rates x1.2 (new $2.4) and at ancla x0.7 (legacy $1.5466).
    - gamma/qa_short: requests but an unreadable bracket (dpp null) -> no data.
    """
    requests: list[dict] = []
    batches: list[dict] = []
    # cell A: alpha / qa_short, reps 1-2, 2 tasks each; rep 2 grades one fail
    for rep in (1, 2):
        bid = f"bA{rep}"
        for i in range(2):
            pasa = rep == 1 or i == 0  # rep 1 passes both, rep 2 only the first
            requests.append(
                req("qa_short", "alpha", bid, rep=rep, index=i, checker="pass" if pasa else "fail")
            )
        batches.append(batch("qa_short", "alpha", bid, rep=rep, n=2, dpp_weekly=0.2))
    # cell B: beta / throughput, 1 rep, 1 task of 2M in
    requests.append(req("throughput", "beta", "bB", tok_in=2_000_000, tok_out=0))
    batches.append(batch("throughput", "beta", "bB", dpp_weekly=0.2))
    # cell C: beta / qa_short, 1 rep, 1 task of 1.25M tokens, dpp 9.6 pp
    requests.append(req("qa_short", "beta", "bC", tok_in=500_000, tok_out=750_000))
    batches.append(batch("qa_short", "beta", "bC", dpp_weekly=9.6))
    # cell D: gamma / qa_short, billed requests but a failed bracket (no dpp)
    requests.append(req("qa_short", "gamma", "bD"))
    batches.append(batch("qa_short", "gamma", "bD", dpp_weekly=None))
    # the concurrency workstream's k-cells: same tokens, k varies. Alpha's
    # growth (2 then 4 ticks of per-task cost) sits clearly beyond the one-tick
    # tie band -> overhead; beta's pair grows by EXACTLY one tick — the squeeze
    # rule's boundary call, pinned deterministically through the residue band.
    for k, dpp in ((1, 1.6), (4, 3.2), (8, 4.8)):
        bid = f"bk{k}"
        for i in range(8):
            requests.append(req("concurrency", "alpha", bid, k=k, index=i))
        batches.append(batch("concurrency", "alpha", bid, k=k, n=8, dpp_weekly=dpp))
    for k, dpp in ((1, 1.6), (4, 2.4)):
        bid = f"bb{k}"
        for i in range(8):
            requests.append(req("concurrency", "beta", bid, k=k, index=i))
        batches.append(batch("concurrency", "beta", bid, k=k, n=8, dpp_weekly=dpp))
    write_raw(base, requests, batches)


def with_tables(tmp_path: pathlib.Path) -> str:
    """v1 (baseline) and v2 (+20% rates): same models, different dollars."""
    write_table(tmp_path / "pricing", "2026-08-31", TABLE_V1)
    encarecidas = {
        m: {
            "input": r["input"] * 1.2,
            "cached_input": r["cached_input"] * 1.2,
            "output": r["output"] * 1.2,
        }
        for m, r in TABLE_V1.items()
    }
    write_table(tmp_path / "pricing", "2026-09-01", encarecidas)
    return str(tmp_path / "pricing")


def cell(doc: dict, model: str, workload: str) -> dict:
    return next(c for c in doc["cells"] if c["model"] == model and c["workload"] == workload)


def sweep_cell(barrido: dict, clave: str, model: str, workload: str) -> dict:
    return next(
        c for c in barrido["cells"][clave] if c["model"] == model and c["workload"] == workload
    )


def near(a, b, places=6):
    return round(a, places) == round(b, places)


def snapshot_raw(tmp_path: pathlib.Path) -> dict[str, bytes]:
    return {
        f"{carpeta}/{p.name}": p.read_bytes()
        for carpeta in ("runs", "batches")
        for p in sorted((tmp_path / carpeta).glob("*.jsonl"))
    }


# ---------------------------------------------------------------------------
# golden: the same raw with a changed table recomputes with ZERO new requests
# ---------------------------------------------------------------------------


def test_golden_recompute_from_changed_table_spends_zero_requests(tmp_path, fake_cli):
    """The acceptance gate: analyze never touches the API, and a new table
    version re-derives a different bundle from byte-identical raw files."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    antes = snapshot_raw(tmp_path)

    doc1 = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    doc2 = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-09-01")

    assert fake_cli.calls == []  # zero requests across BOTH analyses
    assert snapshot_raw(tmp_path) == antes  # raw untouched
    assert doc1["base_params"]["table_version"] == "2026-08-31"
    assert doc2["base_params"]["table_version"] == "2026-09-01"
    # the same cell re-priced: identical legacy side, different new-plan side
    a, b = cell(doc1, "alpha", "qa_short"), cell(doc2, "alpha", "qa_short")
    assert near(a["legacy_cost_task_usd"]["median"], b["legacy_cost_task_usd"]["median"])
    assert not near(a["new_cost_task_s0_usd"], b["new_cost_task_s0_usd"])
    assert near(b["new_cost_task_s0_usd"], a["new_cost_task_s0_usd"] * 1.2)
    # ...and the threshold moved with it (new $/1M x1.2 -> pp/1M threshold x1.2)
    assert near(b["threshold_pp_per_1m"]["s0"], a["threshold_pp_per_1m"]["s0"] * 1.2, 4)
    # the dashboard was regenerated for the new table
    html = (tmp_path / "analysis" / "dashboard.html").read_text(encoding="utf-8")
    assert "2026-09-01" in html


# ---------------------------------------------------------------------------
# threshold bars and who-wins match the manual calculation
# ---------------------------------------------------------------------------


def test_threshold_and_costs_match_manual_math(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")

    a = cell(doc, "alpha", "qa_short")
    # legacy: dpp 0.2 pp x U / 2 tasks
    assert near(a["legacy_cost_task_usd"]["median"], 0.2 * U / 2, 9)
    # measured pp/1M: 0.2 pp per 3000 tokens = 66.66666666666667, full precision
    assert a["pp_per_1m"]["median"] == 0.2 * 1e6 / 3000
    assert a["pp_per_1m"]["p25"] == 0.2 * 1e6 / 3000 and a["pp_per_1m"]["p95"] == 0.2 * 1e6 / 3000
    # new-plan per task from the MEASURED median tokens (1000 in / 500 out)
    assert near(a["new_cost_task_s0_usd"], (1000 * 0.6 + 500 * 1.2) / 1e6, 9)
    assert near(a["new_cost_task_s1_usd"], (500 * 0.6 + 500 * 0.3 + 500 * 1.2) / 1e6, 9)
    # threshold pp/1M from the cell's own measured mix (0.8 and 0.7 $/1M)
    assert near(a["threshold_pp_per_1m"]["s0"], 0.80 / U, 4)
    assert near(a["threshold_pp_per_1m"]["s1"], 0.70 / U, 4)
    assert a["verdict"]["s0"]["winner"] == "new" and a["verdict"]["s1"]["winner"] == "new"

    b = cell(doc, "beta", "throughput")
    assert near(b["legacy_cost_task_usd"]["median"], 0.2 * U, 9)
    assert b["pp_per_1m"]["median"] == 0.2 * 1e6 / 2_000_000
    assert near(b["new_cost_task_s0_usd"], 2.0, 9)
    assert near(b["threshold_pp_per_1m"]["s0"], 2.0 / 2.0 / U, 4)
    assert b["verdict"]["s0"]["winner"] == "legacy" and b["verdict"]["s1"]["winner"] == "legacy"

    c = cell(doc, "beta", "qa_short")
    assert c["pp_per_1m"]["median"] == 9.6 * 1e6 / 1_250_000
    assert near(c["legacy_cost_task_usd"]["median"], 9.6 * U, 9)
    assert near(c["new_cost_task_s0_usd"], 2.0, 9)
    # threshold from the measured mix: $2.0 per 1.25M tokens = 1.6 $/1M
    assert near(c["threshold_pp_per_1m"]["s0"], 2.0 / 1.25 / U, 4)
    assert c["verdict"]["s0"]["winner"] == "new"


def test_pass_rate_and_completed_task_costs_match_manual_math(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    a = cell(doc, "alpha", "qa_short")
    assert a["attempted"] == 4 and a["completed"] == 3
    assert near(a["pass_rate"], 0.75)
    # rep 1 completed 2 tasks, rep 2 only 1: median of (0.2U/2, 0.2U/1)
    assert near(a["legacy_cost_completed_usd"]["median"], (0.2 * U / 2 + 0.2 * U) / 2, 9)


def test_who_wins_by_profile_table(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    per_workload = {w["workload"]: w for w in doc["who_wins"]}
    # qa_short: alpha -> new, beta -> new, gamma -> unmeasured
    assert per_workload["qa_short"]["s0"] == {
        "legacy": 0,
        "new": 2,
        "tie": 0,
        "unmeasured": 1,
    }
    # throughput: beta -> legacy
    assert per_workload["throughput"]["s0"] == {
        "legacy": 1,
        "new": 0,
        "tie": 0,
        "unmeasured": 0,
    }


def test_unmeasured_cell_is_no_data_and_never_extrapolated(tmp_path):
    """gamma has billed requests but no readable bracket: no legacy measurement,
    no threshold bar - and nothing borrowed from the measured models. Its own
    token report still prices the new-plan extrapolation (that is its evidence)."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    g = cell(doc, "gamma", "qa_short")
    assert g["pp_per_1m"] is None
    assert g["legacy_cost_task_usd"] is None
    assert g["threshold_pp_per_1m"] is None  # no measurement -> no comparison line
    assert near(g["new_cost_task_s0_usd"], (1000 * 0.5 + 500 * 1.5) / 1e6, 9)  # its OWN rates
    assert g["verdict"]["s0"]["winner"] == "no data"
    assert g["verdict"]["s1"]["winner"] == "no data"
    html = (tmp_path / "analysis" / "dashboard.html").read_text(encoding="utf-8")
    assert "no data" in html


# ---------------------------------------------------------------------------
# the self-contained offline dashboard
# ---------------------------------------------------------------------------


def test_dashboard_is_selfcontained_and_offline(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    html = (tmp_path / "analysis" / "dashboard.html").read_text(encoding="utf-8")
    bajo = html.lower()
    assert "http://" not in bajo and "https://" not in bajo  # no CDN, no fetches
    assert "<script src" not in bajo and "<link " not in bajo and "url(" not in bajo
    assert 'id="filter-model"' in html  # the model filter
    assert 'name="filter-scenario"' in html  # the scenario toggle
    # the data rides inside the file (no sibling fetch): it parses back
    marcador = '<script id="analysis-data" type="application/json">'
    assert marcador in html
    incrustado = html.split(marcador, 1)[1].split("</script>", 1)[0]
    datos = json.loads(incrustado)
    assert {c["model"] for c in datos["cells"]} == {"alpha", "beta", "gamma"}
    # filter options cover every model present in the dataset
    for modelo in ("alpha", "beta", "gamma"):
        assert f'value="{modelo}"' in html


def test_analyze_writes_threshold_bars_and_curve_pngs(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    pngs = sorted(p.name for p in (tmp_path / "analysis" / "pngs").glob("*.png"))
    assert "dp-tokens.png" in pngs
    assert any(n.startswith("threshold-") for n in pngs)
    for p in (tmp_path / "analysis" / "pngs").glob("*.png"):
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # a real PNG, not a stub


# ---------------------------------------------------------------------------
# the 4 fixed sensitivity sweeps
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# verdict margins {winner, margin_pct} and the allocated/session rules (#40)
# ---------------------------------------------------------------------------


def test_every_measured_verdict_carries_its_margin_pct(tmp_path):
    """margin_pct = (loser - winner) / loser, as a percentage of the loser's
    cost — the saving from picking the winner, riding the verdict object.

    Hand math (cell C): legacy $9.6*U vs new S0 $2.0 -> new wins by
    (9.6U - 2.0) / (9.6U). Cell B: legacy $0.2U vs new $2.0 -> legacy wins by
    (2.0 - 0.2U) / 2.0. gamma has no legacy reading: no data, no margin."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")

    c = cell(doc, "beta", "qa_short")
    assert c["verdict"]["s0"] == {"winner": "new", "margin_pct": (9.6 * U - 2.0) / (9.6 * U) * 100}
    b = cell(doc, "beta", "throughput")
    assert near(b["verdict"]["s0"]["margin_pct"], (2.0 - 0.2 * U) / 2.0 * 100, 9)
    assert b["verdict"]["s0"]["winner"] == "legacy"
    g = cell(doc, "gamma", "qa_short")
    assert g["verdict"]["s0"] == {"winner": "no data", "margin_pct": None}


def test_a_tie_verdict_carries_no_margin(tmp_path):
    """Inside the tie band (min of 2 ticks and 5 % of the cheaper cost) the
    verdict is a tie with no margin to report. Hand math: legacy dpp 2.04/U
    sits $0.04 above the new S0 $2.0 — under the $0.046 two-tick band."""
    pricing = with_tables(tmp_path)
    requests = [req("throughput", "beta", "bTie", tok_in=2_000_000, tok_out=0)]
    write_raw(
        tmp_path,
        requests,
        [batch("throughput", "beta", "bTie", dpp_weekly=2.04 / U)],
    )
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    t = cell(doc, "beta", "throughput")
    assert t["verdict"]["s0"] == {"winner": "tie", "margin_pct": None}


def test_allocated_readings_carry_costs_marked_and_never_verdicted(tmp_path):
    """An allocated reading shows its allocated costs but never a verdict: the
    rows are marked `reading: allocated` with `verdict: null`, and the
    who-wins table counts only measured verdicts (a pooled workload never
    appears in it).

    Hand math: the pool's dpp_weekly 0.5 / dpp_session 0.4, shares 0.75/0.25
    -> allocated 0.375/0.125 weekly, 0.3/0.1 session; multi_turn ran 2
    attempted tasks, reasoning 1; session $/pp = U / 6.22."""
    pricing = with_tables(tmp_path)
    requests = [
        req("multi_turn", "alpha", "bP", index=0, tok_in=1000, tok_out=500),
        req("multi_turn", "alpha", "bP", index=1, tok_in=1000, tok_out=500, checker="fail"),
        req("reasoning", "alpha", "bP", index=2, tok_in=800, tok_out=200),
    ]
    lote = batch(
        None,
        "alpha",
        "bP",
        n=3,
        reps=2,
        pool={"workloads": ["multi_turn", "reasoning"], "reps": 2},
        dpp_weekly=0.5,
    )
    lote["dpp_session"] = 0.4
    write_raw(tmp_path, requests, [lote])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    sesion = U / 6.22
    a = doc["pooled"][0]["allocations"]
    for lectura in a.values():
        assert lectura["reading"] == "allocated"
        assert lectura["verdict"] is None
    mt, rea = a["multi_turn"], a["reasoning"]
    assert mt["attempted"] == 2 and mt["completed"] == 1
    assert rea["attempted"] == 1 and rea["completed"] == 1
    assert near(mt["cost_task_attempted_usd"], 0.375 * U / 2, 12)
    assert near(mt["cost_task_completed_usd"], 0.375 * U / 1, 12)
    assert near(rea["cost_task_attempted_usd"], 0.125 * U, 12)
    assert near(mt["cost_task_attempted_usd_session"], 0.3 * sesion / 2, 12)
    assert near(rea["cost_task_attempted_usd_session"], 0.1 * sesion, 12)
    # the allocated workloads are never verdicted anywhere: not cells, not
    # who-wins rows
    assert all(c["model"] != "alpha" for c in doc["cells"])
    assert all(w["workload"] not in ("multi_turn", "reasoning") for w in doc["who_wins"])


def test_a_pooled_workload_without_token_reports_is_unattributable_not_free(tmp_path):
    """A mixed pool where one workload's lines all omit their token counts: its
    share is 0.0 but its allocated Δpp is None (no data) — never dpp x 0.0
    shipping a measured-looking $0.00 legacy cost."""
    pricing = with_tables(tmp_path)
    requests = [
        req("multi_turn", "alpha", "bMix", index=0, tok_in=1000, tok_out=500),
        req("reasoning", "alpha", "bMix", index=1, tok_in=1000, tok_out=500),
    ]
    requests[1].update({"tok_in": None, "tok_out": None})
    lote = batch(
        None,
        "alpha",
        "bMix",
        n=2,
        reps=1,
        pool={"workloads": ["multi_turn", "reasoning"], "reps": 1},
        dpp_weekly=0.4,
    )
    write_raw(tmp_path, requests, [lote])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    a = doc["pooled"][0]["allocations"]
    assert a["reasoning"]["share"] == 0.0
    assert a["reasoning"]["dpp_weekly"] is None and a["reasoning"]["dpp_session"] is None
    assert a["reasoning"]["cost_task_attempted_usd"] is None
    assert near(a["multi_turn"]["dpp_weekly"], 0.4, 12)
    assert near(a["multi_turn"]["cost_task_attempted_usd"], 0.4 * U, 12)


def test_both_windows_ship_per_bracket_and_the_session_ships_unanchored(tmp_path):
    """Weekly is the primary (the anchor); the session window rides as the
    secondary signal, priced with the derived $/pp (weekly / R, R = 6.22) and
    carrying its unanchored caveat in the doc, the notes and the dashboard.

    Hand math (alpha/qa_short, dpp 0.2 both windows, 2 tasks): session
    $/task = 0.2 * (U/6.22) / 2."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    sesion = U / 6.22

    # the derived session $/pp is stamped with its caveat, never as an anchor
    s = doc["base_params"]["session"]
    assert s["ratio_r"] == 6.22
    assert s["usd_per_pp"] == sesion
    assert "unanchored" in s["caveat"] and "secondary" in s["caveat"]

    a = cell(doc, "alpha", "qa_short")
    # both windows per bracket: the per-rep rows carry both dpp...
    assert all(r["dpp_weekly"] == 0.2 and r["dpp_session"] == 0.2 for r in a["reps"])
    # ...and the cell's legacy distributions exist for both windows
    assert near(a["legacy_cost_task_usd_session"]["median"], 0.2 * sesion / 2, 12)
    assert near(a["legacy_cost_task_usd"]["median"], 0.2 * U / 2, 12)
    assert near(
        a["legacy_cost_completed_usd_session"]["median"],
        (0.2 * sesion / 2 + 0.2 * sesion) / 2,  # median of rep1 (2 tasks) and rep2 (1)
        9,
    )
    # every curve point (one per bracket) reports both windows
    puntos = [p for p in doc["dp_tokens_curve"] if p["batch_id"] == "bA1"]
    assert puntos and puntos[0]["dpp_weekly"] == 0.2 and puntos[0]["dpp_session"] == 0.2
    # the caveat ships in the doc's notes and the rendered dashboard
    assert "unanchored" in doc["notes"]
    html = (tmp_path / "analysis" / "dashboard.html").read_text(encoding="utf-8")
    assert "unanchored" in html


def test_sweep_rates_plus20_flips_the_borderline_cell(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    barrido = doc["sensitivity"]["rates"]
    assert barrido["factors"] == [0.8, 1.2]
    vueltas = {(f["model"], f["workload"]): f["verdict"] for f in barrido["flips"]["1.2"]}
    # cell C: legacy $2.2094 sits between new $2.0 and new x1.2 = $2.4, both by
    # a real margin -> +20% rates push the new side past the legacy side
    assert vueltas[("beta", "qa_short")]["winner"] == "legacy"
    assert len(barrido["flips"]["1.2"]) == 1
    assert barrido["flips"]["0.8"] == []  # cheaper rates flip nothing here


def test_sweep_cache_scenarios_recompute_only_discounted_models(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    barrido = doc["sensitivity"]["cache"]
    assert barrido["s_values"] == [0.0, 0.25, 0.5, 0.9]
    # S1(s=0.9): 10% input + 90% cached + output
    esperado = (1000 * 0.1 * 0.6 + 1000 * 0.9 * 0.3 + 500 * 1.2) / 1e6
    assert near(sweep_cell(barrido, "0.9", "alpha", "qa_short")["new_cost_task_usd"], esperado, 9)
    # beta has no discount: its cost is identical under every hit rate
    for s in ("0", "0.25", "0.5", "0.9"):
        celda = sweep_cell(barrido, s, "beta", "throughput")
        assert near(celda["new_cost_task_usd"], 2.0, 9)
    assert barrido["flips"]["0.9"] == []  # alpha's winner is stable across S here


def test_sweep_anchor_scales_legacy_side_and_flips(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    barrido = doc["sensitivity"]["ancla"]
    assert barrido["factors"] == [0.7, 1.0, 1.3]
    # cell C: legacy $9.6*U vs new $2.0 -> new wins at ancla=100; at -30% the
    # legacy side drops to $1.5466 and the winner flips to legacy
    c07 = sweep_cell(barrido, "0.7", "beta", "qa_short")
    assert near(c07["legacy_cost_task_usd"], 9.6 * U * 0.7, 9)
    assert c07["verdict"]["winner"] == "legacy"
    vueltas = {(f["model"], f["workload"]) for f in barrido["flips"]["0.7"]}
    assert vueltas == {("beta", "qa_short")}
    # the measured pp/1M never moves with the anchor (it is meter-native)
    assert sweep_cell(barrido, "1.3", "beta", "qa_short")["pp_per_1m"] == 9.6 * 1e6 / 1_250_000


def test_sweep_k_axis_reads_the_concurrency_cells(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    barrido = doc["sensitivity"]["k_axis"]
    filas = {f["model"]: f for f in barrido["models"]}
    por_k = {c["k"]: c for c in filas["alpha"]["cells"]}
    # cost per attempted task = dpp x U / 8 tasks
    assert near(por_k[1]["cost_task_attempted_usd"], 1.6 * U / 8, 9)
    assert near(por_k[4]["cost_task_attempted_usd"], 3.2 * U / 8, 9)
    # alpha's per-task cost grows 2 then 4 ticks with the same tokens, a real
    # margin beyond the one-tick tie band -> overhead
    assert filas["alpha"]["verdict"] == "overhead"
    # beta grows by EXACTLY one tick: within the meter's resolution — read
    # through the residue band, so the boundary is squeeze deterministically,
    # never a coin flip on the payloads' last bits
    assert filas["beta"]["verdict"] == "squeeze"
    # the k cells never leak into the per-workload derivatives
    assert not any(c["workload"] == "concurrency" for c in doc["cells"])


# ---------------------------------------------------------------------------
# measured hit rates replace --s when conclusive
# ---------------------------------------------------------------------------


def test_measured_hit_rate_replaces_the_s1_assumption(tmp_path, fake_cli):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    (tmp_path / "runs" / f"calibration-{RUN}.json").write_text(
        json.dumps(
            {
                "run_id": RUN,
                "kind": "cache-calibration",
                "readings": {
                    "alpha": {
                        "conclusive": True,
                        "hit_rate": 0.8,
                        "paper_discount": {"declared": True, "materialized": True},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    resuelto = doc["s_per_model"]["alpha"]
    assert resuelto["s"] == 0.8 and resuelto["source"] == "measured"
    # S1 now extrapolates with the MEASURED hit rate, not the 50% assumption
    a = cell(doc, "alpha", "qa_short")
    assert near(a["new_cost_task_s1_usd"], (200 * 0.6 + 800 * 0.3 + 500 * 1.2) / 1e6, 9)
    # beta was never calibrated: it keeps the assumed S1, marked as such
    assert doc["s_per_model"]["beta"]["source"] == "assumed"
    assert doc["s_per_model"]["beta"]["s"] == 0.5


# ---------------------------------------------------------------------------
# CLI behavior: offline by construction, clean errors, filters
# ---------------------------------------------------------------------------


def test_analyze_needs_no_api_key_and_reports_its_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    codigo, salida, errores = analyze_cli(
        tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31"
    )
    assert codigo == 0, salida or errores
    assert (tmp_path / "analysis" / "analysis.json").exists()
    assert "analysis" in (salida + errores).lower()


def test_analyze_without_raw_data_is_a_clean_error(tmp_path):
    pricing = with_tables(tmp_path)
    codigo, _salida, errores = analyze_cli(
        tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31"
    )
    assert codigo == 2
    assert "raw" in errores.lower()
    assert "Traceback" not in errores
    assert not (tmp_path / "analysis").exists()


def test_level_and_model_filters_narrow_the_cells(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--model", "alpha")
    assert {c["model"] for c in doc["cells"]} == {"alpha"}
    doc2 = analyze_doc(tmp_path, "--pricing-dir", pricing, "--level", "T2")
    assert doc2["cells"] == []
    assert doc2["who_wins"] == []


def test_analyze_ignores_torn_and_foreign_lines(tmp_path):
    """A torn tail line (crash mid-write) and non-JSON junk are skipped."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    ruta = tmp_path / "runs" / f"requests-{RUN}.jsonl"
    ruta.write_text(ruta.read_text(encoding="utf-8") + "{torn\n\n[]\n", encoding="utf-8")
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    assert len([c for c in doc["cells"] if c["model"] == "alpha"]) >= 1


# ---------------------------------------------------------------------------
# hardening from the Harness 08 review
# ---------------------------------------------------------------------------


def test_rep_provenance_comes_from_the_request_lines(tmp_path):
    """Batch lines never carry a rep (the schema forbids one); the per-rep
    rows take it from the batch's own request lines, where it is required."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    a = cell(doc, "alpha", "qa_short")
    assert [r["rep"] for r in a["reps"]] == [1, 2]


def test_lines_of_other_protocol_vintages_are_set_aside(tmp_path):
    """Raw lines stamped with another protocol are counted, never blended
    into the medians (the schema is versioned on every line)."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    vieja = batch("qa_short", "beta", "bViejo", dpp_weekly=0.3)
    vieja["protocol_version"] = "v0"
    with (tmp_path / "batches" / f"batches-{RUN}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(vieja) + "\n")
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    assert doc["raw"]["lines_other_protocol"] == 1
    assert all(r["batch_id"] != "bViejo" for c in doc["cells"] for r in c["reps"])


def test_k_axis_cost_divides_by_accepted_not_planned(tmp_path):
    """A 429 never lands on the meter: the cell's Δpp bills the accepted
    requests, so the per-task cost divides by the ACCEPTED count."""
    pricing = with_tables(tmp_path)
    requests = [req("concurrency", "alpha", "bkr", k=8, index=i) for i in range(8)]
    requests[7].update(
        {
            "http": 429,
            "err": "rate limited",
            "tok_in": None,
            "tok_out": None,
            "api": None,
            "checker": None,
            "t_first_chunk": None,
        }
    )
    write_raw(tmp_path, requests, [batch("concurrency", "alpha", "bkr", k=8, n=8, dpp_weekly=1.6)])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    fila = doc["sensitivity"]["k_axis"]["models"][0]
    assert fila["cells"][0]["attempted"] == 7
    assert near(fila["cells"][0]["cost_task_attempted_usd"], 1.6 * U / 7, 9)


def test_cache_sweep_survives_a_token_report_missing_the_output_count(tmp_path):
    """A done object can carry prompt_eval_count but omit eval_count: the
    cell extrapolates nothing, and the sweep skips it instead of crashing."""
    pricing = with_tables(tmp_path)
    requests = [req("qa_short", "alpha", "bSinOut", tok_out=0)]
    requests[0]["tok_out"] = None  # the API never reported the output count
    write_raw(tmp_path, requests, [batch("qa_short", "alpha", "bSinOut", dpp_weekly=0.2)])
    codigo, _salida, errores = analyze_cli(
        tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31"
    )
    assert codigo == 0, errores


# ---------------------------------------------------------------------------
# the pooled brackets' post-hoc allocation (methodology v1.1 §5, ticket #39)
# ---------------------------------------------------------------------------


def test_pooled_brackets_allocate_legacy_by_token_share_post_hoc(tmp_path):
    """A pooled bracket (workload null, pool naming the trio) is never a cell:
    its per-workload legacy sits in the analysis' 'pooled' section, allocated
    by each workload's share of the pool's request tokens — derived here, at
    analysis time, from the raw lines (no stored weight anywhere).

    Hand math: the pool measures dpp_weekly 0.5 / dpp_session 0.4; multi_turn
    reports 2 x 1500 = 3000 tokens, reasoning 1000 -> shares 0.75 / 0.25, so
    the allocations are 0.375 / 0.125 weekly and 0.3 / 0.1 session."""
    pricing = with_tables(tmp_path)
    requests = [
        req("multi_turn", "alpha", "bP", index=0, tok_in=1000, tok_out=500),
        req("multi_turn", "alpha", "bP", index=1, tok_in=1000, tok_out=500),
        req("reasoning", "alpha", "bP", index=2, tok_in=800, tok_out=200),
    ]
    lote = batch(
        None,
        "alpha",
        "bP",
        n=3,
        reps=2,
        pool={"workloads": ["multi_turn", "reasoning"], "reps": 2},
        dpp_weekly=0.5,
    )
    lote["dpp_session"] = 0.4
    write_raw(tmp_path, requests, [lote])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    # A pooled bracket never becomes a measured cell, even though its request
    # lines name task workloads: the legacy is allocated, never verdicted.
    assert all(c["model"] != "alpha" for c in doc["cells"])
    filas = doc["pooled"]
    assert len(filas) == 1
    fila = filas[0]
    assert fila["batch_id"] == "bP" and fila["model"] == "alpha"
    assert fila["workloads"] == ["multi_turn", "reasoning"] and fila["reps"] == 2
    assert fila["dpp_weekly"] == 0.5 and fila["dpp_session"] == 0.4
    a = fila["allocations"]
    assert a["multi_turn"]["tokens_total"] == 3000
    assert a["reasoning"]["tokens_total"] == 1000
    assert a["multi_turn"]["share"] == 0.75 and a["reasoning"]["share"] == 0.25
    assert a["multi_turn"]["dpp_weekly"] == 0.375 and a["reasoning"]["dpp_weekly"] == 0.125
    # 0.4 * 0.75 carries float residue; the exact-float policy persists it as-is.
    assert near(a["multi_turn"]["dpp_session"], 0.3, 12)
    assert near(a["reasoning"]["dpp_session"], 0.1, 12)


def test_allocation_needs_the_requests_tokens_and_reports_without_them(tmp_path):
    """The shares derive from the request lines' tokens: a pooled bracket whose
    requests report nothing ships with null allocations (the reading is simply
    not attributable), and a per-cell line never enters the section."""
    pricing = with_tables(tmp_path)
    sin_tokens = req("multi_turn", "alpha", "bSinPool", tok_in=0, tok_out=0)
    sin_tokens.update({"tok_in": None, "tok_out": None})
    pooled = batch(
        None,
        "alpha",
        "bSinPool",
        n=1,
        reps=2,
        pool={"workloads": ["multi_turn", "reasoning"], "reps": 2},
        dpp_weekly=0.5,
    )
    write_raw(tmp_path, [sin_tokens], [pooled, batch("qa_short", "beta", "bCelda")])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    assert len(doc["pooled"]) == 1
    assert doc["pooled"][0]["allocations"] is None
    assert any(c["workload"] == "qa_short" and c["model"] == "beta" for c in doc["cells"])
