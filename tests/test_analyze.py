"""`bench analyze`: the re-run without re-measuring (ticket Harness 08).

The analysis reads only the raw datasets (runs/*.jsonl + batches/*.jsonl), the
versioned price table and the analysis parameters (--table-version/--anchor/--s):
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
import re

from conftest import write_table
from test_dry_run import run_cli  # noqa: F401  (the shared CLI-seam runner, kept for parity)

from obench.client import PROTOCOL_VERSION
from obench.meter import SESSION_R, usd_per_pp
from obench import analyze as analyze_module
from obench.schema import validate_batch_line, validate_request_line

# The default anchor ($100/mo) bridged to USD per weekly pp (tested primitive).
U = usd_per_pp(100.0)


def analyze_cli(tmp_path, *args) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            from obench.cli import main

            code = main(["--base", str(tmp_path), "analyze", *args])
    except SystemExit as e:  # argparse exits 2 on usage errors
        code = int(e.code or 0)
    return code, output.getvalue(), errors.getvalue()


def analyze_doc(tmp_path, *args) -> dict:
    code, output, errors = analyze_cli(tmp_path, *args, "--json")
    assert code == 0, output or errors
    return json.loads(output)


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
    meter_post = (
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
        "meter_pre": {"limits": {"session": {"usage": 0.5}, "weekly": {"usage": 0.5}}},
        "meter_post": meter_post,
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

    Verdicts compare PAID dollars (methodology v1.3): the new side's credits
    divide by credit_ratio = 3 (the CLI's default).

    - alpha/qa_short: 2 reps x 2 requests (1000 in / 500 out), dpp 0.2 each
      -> pp/1M = 0.2 / 0.003 = 66.6667; legacy $/task = 0.2*U/2 = 0.023015;
      new S0 = 0.0012 credits ($0.0004 paid), S1(s=.5) = 0.00105;
      threshold S0 = 0.8/(3U).
    - beta/throughput: 1 request of 2M in / 0 out, dpp 0.2
      -> pp/1M = 0.1; legacy $/task = 0.2*U; new S0 = $2.0 credits
      ($0.6667 paid) -> LEGACY wins.
    - beta/qa_short: 1 request of 500k in / 750k out, dpp 9.6
      -> pp/1M = 9.6 / 1.25 = 7.68; legacy $/task = 9.6*U = 2.20944 vs new S0
      = $0.6667 paid -> NEW wins by a real margin; the paid gap is far too
      wide for the sweeps to flip it (they would need x<0.3 rates or anchor).
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


def sweep_cell(sweep: dict, key: str, model: str, workload: str) -> dict:
    return next(c for c in sweep["cells"][key] if c["model"] == model and c["workload"] == workload)


def near(a, b, places=6):
    return round(a, places) == round(b, places)


def snapshot_raw(tmp_path: pathlib.Path) -> dict[str, bytes]:
    return {
        f"{folder}/{p.name}": p.read_bytes()
        for folder in ("runs", "batches")
        for p in sorted((tmp_path / folder).glob("*.jsonl"))
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
    # threshold pp/1M from the cell's own measured mix (0.8 and 0.7 $/1M paid
    # — credits divide by the CLI's default credit_ratio = 3)
    assert near(a["threshold_pp_per_1m"]["s0"], 0.80 / (3 * U), 4)
    assert near(a["threshold_pp_per_1m"]["s1"], 0.70 / (3 * U), 4)
    assert a["verdict"]["s0"]["winner"] == "new" and a["verdict"]["s1"]["winner"] == "new"

    b = cell(doc, "beta", "throughput")
    assert near(b["legacy_cost_task_usd"]["median"], 0.2 * U, 9)
    assert b["pp_per_1m"]["median"] == 0.2 * 1e6 / 2_000_000
    assert near(b["new_cost_task_s0_usd"], 2.0, 9)
    assert near(b["threshold_pp_per_1m"]["s0"], 2.0 / 2.0 / (3 * U), 4)
    assert b["verdict"]["s0"]["winner"] == "legacy" and b["verdict"]["s1"]["winner"] == "legacy"

    c = cell(doc, "beta", "qa_short")
    assert c["pp_per_1m"]["median"] == 9.6 * 1e6 / 1_250_000
    assert near(c["legacy_cost_task_usd"]["median"], 9.6 * U, 9)
    assert near(c["new_cost_task_s0_usd"], 2.0, 9)
    # threshold from the measured mix: $2.0 credits per 1.25M tokens = 1.6 $/1M
    assert near(c["threshold_pp_per_1m"]["s0"], 2.0 / 1.25 / (3 * U), 4)
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
# the dashboard page: the Pages-first contract (data inline, CDN styling)
# ---------------------------------------------------------------------------


def test_dashboard_loads_the_pages_contract_from_the_cdn(tmp_path):
    """Pages-first: the dashboard's data still rides inside the file as JSON
    (a single file, no sibling fetches), but styling loads from the CDN because
    GitHub Pages serves online — the Tailwind browser build, Google Fonts and
    the Lucide icons with a guarded createIcons refresh helper, plus the inline
    SVG favicon. No unresolved __TOKEN__ placeholder survives."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    html = (tmp_path / "analysis" / "dashboard.html").read_text(encoding="utf-8")
    # the CDN head block: Tailwind browser build, Google Fonts, Lucide icons
    assert 'src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"' in html
    assert "fonts.googleapis.com/css2?family=Inter" in html
    assert "display=swap" in html
    assert "unpkg.com/lucide@1.40.0/dist/umd/lucide.min.js" in html
    assert "createIcons" in html  # the guarded refresh helper the page ships
    assert 'rel="icon"' in html  # the inline SVG favicon (a data URI, no fetch)
    # the favicon's xmlns is the SVG XML namespace: an identifier, not a fetch
    assert "http://www.w3.org/2000/svg" in html
    # every http(s):// literal belongs to the known Pages-first origin set: the
    # pinned CDNs, the font hosts and the SVG namespace identifier — the
    # successor of the old offline no-http-literals evasion guards
    for url in re.findall(r'https?://[^"\'\s<>)]+', html):
        assert url.startswith(
            (
                "https://cdn.jsdelivr.net",
                "https://fonts.googleapis.com",
                "https://fonts.gstatic.com",
                "https://unpkg.com",
                "https://srnoob2570.github.io",
                "https://github.com/srnoob2570",
                "http://www.w3.org/2000/svg",
            )
        ), url
    assert 'id="filter-model"' in html  # the model filter
    assert 'id="cache-s"' in html  # the free S(x) input (the slider's successor)
    assert "button.cache-preset" in html  # and the quick presets that drive it
    assert 'id="input-tokens-in"' in html  # the token scenario: input...
    assert 'id="input-tokens-out"' in html  # ...and output, priced at their own rates
    assert "data-in" in html  # the ratio presets that set both fields
    assert 'name="theme"' in html  # the theme radios survive inside the segmented control
    # the cells table prices nominally at N tokens: the meter-native columns
    # left the thead (they survive in tooltips)
    assert "pp/1M</th>" not in html and "/task</th>" not in html
    assert html.count('class="hdr-tokens') == 2  # legacy + new price headers
    # the markup/CSS/JS live in the template file, not a Python string
    template = pathlib.Path(analyze_module.__file__).parent / "web" / "dashboard_template.html"
    assert template.exists()
    template_text = template.read_text(encoding="utf-8")
    # the readout strips left the pages in the chrome slim-down: the template
    # still carries exactly the three fills render_dashboard makes
    for name in ("OPTIONS", "DATA", "RATES"):
        assert f"__{name}__" in template_text
    # the data rides inside the file (no sibling fetch): it parses back
    marker = '<script id="analysis-data" type="application/json">'
    assert marker in html
    incrustado = html.split(marker, 1)[1].split("</script>", 1)[0]
    data = json.loads(incrustado)
    assert {c["model"] for c in data["cells"]} == {"alpha", "beta", "gamma"}
    # filter options cover every model present in the dataset
    for model in ("alpha", "beta", "gamma"):
        assert f'value="{model}"' in html
    # every __TOKEN__ placeholder resolved: none survives in the rendered page
    # (ESTILO_BASE included: the vendored fill no longer exists anywhere)
    for name in ("RESUMEN", "OPCIONES", "NOTAS", "DATOS", "RATES", "ESTILO_BASE"):
        assert f"__{name}__" not in html


# ---------------------------------------------------------------------------
# dashboard v2 (#41): verdict first, theme-token charts, cache slider
# ---------------------------------------------------------------------------


def render_dashboard_v2(tmp_path: pathlib.Path, *args) -> str:
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31", *args)
    return (tmp_path / "analysis" / "dashboard.html").read_text(encoding="utf-8")


def render_bundle_v2(tmp_path: pathlib.Path, *args) -> tuple[str, str]:
    """Both shipped pages through the CLI seam: (dashboard.html, calculator.html)."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31", *args)
    folder = tmp_path / "analysis"
    return (
        (folder / "dashboard.html").read_text(encoding="utf-8"),
        (folder / "calculator.html").read_text(encoding="utf-8"),
    )


def test_dashboard_v2_charts_are_theme_token_svgs_and_pngs_leave_the_bundle(tmp_path):
    """The matplotlib PNGs leave the dashboard: the charts are inline SVG drawn
    from theme tokens, and the bundle ships no pngs/ folder at all."""
    html = render_dashboard_v2(tmp_path)
    assert not (tmp_path / "analysis" / "pngs").exists()
    assert html.count("<svg") >= 2  # diverging margins (dp curve left with the robustness section)
    # zero hardcoded chart colors: every SVG fill/stroke is a CSS variable, and
    # the validated palette lives ONLY in the theme token definitions
    assert 'fill="#' not in html and "stroke='#" not in html
    assert "background:#" not in html and "background: #" not in html
    assert "var(--legacy)" in html and "var(--new)" in html
    # the validated two-mode palette (legacy blue / new orange, light + dark)
    for hexcolor in ("#2a78d6", "#3987e5", "#eb6834", "#d95926"):
        assert hexcolor in html
    # the old matplotlib chart colors are gone
    for hexcolor in ("#4878a8", "#c44e52", "#dd8452"):
        assert hexcolor not in html


def test_dashboard_v2_theme_has_three_states(tmp_path):
    """system/light/dark: the toggle stamps data-theme (system = none), and the
    tokens carry both palettes under their own selectors."""
    html = render_dashboard_v2(tmp_path)
    for state in ("system", "light", "dark"):
        assert f'value="{state}"' in html
    assert "data-theme" in html
    # dark tokens are selected, not flipped: their own block, distinct values
    assert '[data-theme="dark"]' in html
    assert "prefers-color-scheme: dark" in html
    assert '[data-theme="light"]' in html


def test_dashboard_v2_verdict_band_leads_and_margins_ride_everywhere(tmp_path):
    """The recommendation band leads; every measured verdict shows its margin
    in the cells column AND in the diverging chart (legacy left / new right /
    tie dot)."""
    html = render_dashboard_v2(tmp_path)
    # the recommendation band leads: it precedes the cells table in the file
    assert html.index('id="reco"') < html.index('id="table-body"')
    # the cells table carries a margin column; the diverging chart exists
    assert ">margin (paid $)</th>" in html
    assert 'id="chart-margins"' in html
    # the verdict chips and the diverging bars both draw from margin_pct
    assert "margin_pct" in html


def test_dashboard_v2_cache_recomputes_from_embedded_rates(tmp_path):
    """The amendment v1.2: a free cache-percentage input (0-100, default 85)
    plus quick presets recompute new-plan costs and the verdict margins
    in live JS from the embedded per-cell tokens + rates + anchor. Nothing
    persisted changes: the rates ride only in the dashboard."""
    html = render_dashboard_v2(tmp_path)
    # the free S(x) input spans 0-100 and defaults to 85 % (measured agent
    # traffic runs ~90 % of input as cache reads; 85 is the conservative pick)
    marker = '<script id="rates-data" type="application/json">'
    assert marker in html
    rates = json.loads(html.split(marker, 1)[1].split("</script>", 1)[0])
    assert set(rates["rates"]) == {"alpha", "beta", "gamma"}
    assert rates["per"] == 1_000_000
    for model, t in rates["rates"].items():
        assert set(t) == {"input", "cached_input", "output", "has_cache_discount"}
    assert rates["rates"]["beta"]["has_cache_discount"] is False  # cached=input
    input = html[html.index('id="cache-s"') : html.index('id="cache-s"') + 400]
    assert 'min="0"' in input and 'max="100"' in input and 'value="85"' in input
    # quick cache % presets ride the cache control: floor, versioned S1, default, real high
    assert "cache-preset" in html and 'data-s="85"' in html
    # they carry their own binding (which sets the input) and are excluded
    # from the token presets' data-in/data-out binding, which would zero the scenario
    assert "button.cache-preset" in html
    assert "button.preset:not(.cache-preset)" in html


def test_dashboard_v2_usage_block_compares_both_plans_at_full_meter(tmp_path):
    """The hero's right column is one plain sentence: for the same monthly
    price, the winner delivers ×N more usage (legacy plan vs the $300 in
    credits), with a muted note that the figure varies by workload. Live JS
    from embedded data; nothing persisted."""
    html = render_dashboard_v2(tmp_path)
    assert 'id="reco-usage"' in html
    # the multiplier and its derivation wiring ride the template's JS
    assert "usageBlock" in html
    assert "more usage" in html
    assert "for the same $" in html
    assert "in credits" in html
    assert "legacy plan" in html
    # the margin of error, said in words: the figure varies by workload
    assert "varies by workload" in html


def test_dashboard_v2_marks_measured_s_and_notes_the_s0_models(tmp_path):
    """Models with a conclusive measured hit-rate keep it, visibly marked, and
    the slider cannot move them; models without a published discount
    (cached=input) are noted as unmoved: S(x) ≡ S0 for them."""
    html = render_dashboard_v2(tmp_path)
    assert "S(x) &equiv; S0" in html or "S(x) ≡ S0" in html  # the in-place note
    assert "measured" in html  # the measured marker's label
    # the per-model effective S rides in the embedded doc so the JS can pin it
    marker = '<script id="analysis-data" type="application/json">'
    data = json.loads(html.split(marker, 1)[1].split("</script>", 1)[0])
    assert set(data["s_per_model"]) == {"alpha", "beta", "gamma"}


def test_dashboard_v2_copy_is_english(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    for name in ("dashboard.html", "calculator.html"):
        bajo = (tmp_path / "analysis" / name).read_text(encoding="utf-8").lower()
        for palabra in ("margen", "empate", "asignado", "escenario", "ganador"):
            assert palabra not in bajo


def test_dashboard_v2_plan_token_budget_section(tmp_path):
    """The plan token-budget matrix — split onto the standalone calculator page:
    one table per plan, the preset in : out splits as bare columns plus a
    custom split from the section's own in/out fields, ALL models the pricing
    table lists as rows — how many input/output tokens the $20 and $100 plans'
    credits buy at that split, priced live from the embedded rates (credits at
    face value, nothing persisted). The dashboard carries none of it anymore."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    html = (tmp_path / "analysis" / "calculator.html").read_text(encoding="utf-8")
    page = (tmp_path / "analysis" / "dashboard.html").read_text(encoding="utf-8")
    # the section's home is the calculator page now; the dashboard keeps no
    # trace of the matrix, its controls or its export code
    assert 'id="plan-tokens"' not in page
    assert 'id="plan-custom-in"' not in page
    assert "exportPlanImage" not in page
    assert 'id="plan-tokens"' in html
    # the in : out selector stays gone: presets ride as matrix columns
    assert 'id="plan-ratio"' not in html
    # the credit figures at face value, in the per-plan sub-headings:
    # Pro $20 paid -> $60 credits, Max $100 paid -> $300 credits
    assert "$20 paid" in html and "$60 credits" in html
    # the credits are a monthly grant: every figure is a monthly budget
    assert "monthly" in html
    assert "$100 paid" in html and "$300 credits" in html
    # the matrix columns are the bare splits (no category labels — they did not
    # help), each header composed at runtime from RATIOS
    for text in ("60 : 1", "10 : 1", "4 : 1"):
        assert text in html
    # the category labels left the headers per decision
    for removed in ("Agent session", "Coding (no reasoning)", "Chat / RAG"):
        assert removed not in html
    # the reasoning/generation columns left the matrix per decision
    for removed in ("Reasoning medium", "Reasoning high", "Reasoning max", "Generation"):
        assert removed not in html
    # the custom split rides in its own in/out fields, priced live
    assert 'id="plan-custom-in"' in html and 'id="plan-custom-out"' in html
    assert "Custom" in html
    # each plan table footnotes the live cache % (a JS-source literal in the static file)
    assert "<tfoot>" in html
    assert "a measured S keeps precedence per model" in html
    # the headers say what the figures are: a two-row thead — the split name
    # over a colspan=3 group, then the ↑ in / ↓ out / total sub-headers —
    # both in the DOM table and the canvas export; the corner header is two
    # stacked headers (in : out ratio over model), one per axis
    assert "<th>model</th>" in html
    assert '<th class="num">in : out ratio</th>' in html
    assert "each cell: ↓ input, ↑ output, then the total" in html
    assert 'colspan="3"' in html
    assert "↓ in" in html and "↑ out" in html
    assert "columns.length * 3" in html
    assert "sep-l" in html  # vertical hairlines between the ratio groups
    # arrow cells: ↑ input | ↓ output | total as three sub-cells per split
    assert "↑" in html and "↓" in html
    # the per-plan PNG/clipboard export: canvas-drawn, offline, one pair per plan
    assert "exportPlanImage" in html and "toBlob" in html
    assert "plan-export" in html
    assert 'data-mode="download"' in html and 'data-mode="copy"' in html
    # light sanity only: the copy guard is fully covered by the English test above
    bajo = html.lower()
    for palabra in ("margen", "empate", "asignado", "escenario", "ganador"):
        assert palabra not in bajo


# ---------------------------------------------------------------------------
# the calculator page (the plan token-budget section's standalone home)
# ---------------------------------------------------------------------------


def _block_tokens(path: pathlib.Path) -> str:
    """A template's theme-token block: from the tokens' comment header to the
    first base rule — the block every page of the bundle must share verbatim."""
    body = path.read_text(encoding="utf-8")
    inicio = body.index("/* three-state theme tokens")
    caja = body.index("box-sizing", inicio)
    return body[inicio : body.index("}", caja) + 1]


def test_calculator_loads_the_pages_contract_from_the_cdn(tmp_path):
    """The calculator meets the same Pages-first contract as the dashboard: the
    data blobs ride embedded and parsable, styling loads from the CDN (the
    Tailwind browser build, Google Fonts and Lucide with a guarded createIcons
    helper), and its model options cover every model the embedded rates price.
    The same analysis blob rides byte-equal in both pages, the theme-token
    block is shared verbatim, and no unresolved __TOKEN__ placeholder survives."""
    page, html = render_bundle_v2(tmp_path)
    # the CDN head block: Tailwind browser build, Google Fonts, Lucide icons
    assert 'src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"' in html
    assert "fonts.googleapis.com/css2?family=Inter" in html and "display=swap" in html
    assert "unpkg.com/lucide@1.40.0/dist/umd/lucide.min.js" in html and "createIcons" in html
    assert 'rel="icon"' in html
    # every http(s):// literal belongs to the known Pages-first origin set
    for url in re.findall(r'https?://[^"\'\s<>)]+', html):
        assert url.startswith(
            (
                "https://cdn.jsdelivr.net",
                "https://fonts.googleapis.com",
                "https://fonts.gstatic.com",
                "https://unpkg.com",
                "https://srnoob2570.github.io",
                "https://github.com/srnoob2570",
                "http://www.w3.org/2000/svg",
            )
        ), url
    assert 'id="filter-model"' in html  # the (hidden) multi-select the picker drives
    assert 'id="slider-s"' not in html  # the range slider is gone by design (Phase 2)
    # the S(x) control: cache preset buttons plus a custom 0-100 input — the
    # same selectors the page's JS binds
    assert "button.cache-preset" in html and 'data-s="85"' in html
    assert 'id="cache-custom"' in html
    assert 'id="plan-custom-in"' in html and 'id="plan-custom-out"' in html
    assert 'name="theme"' in html  # the theme radios survive on this page too
    # the markup/CSS/JS live in the template file, not a Python string
    templates = pathlib.Path(analyze_module.__file__).parent / "web"
    template = templates / "calculator_template.html"
    assert template.exists()
    body = template.read_text(encoding="utf-8")
    assert "__DATA__" in body and "__RATES__" in body and "__OPTIONS__" in body
    # the analysis doc rides inside the file (no sibling fetch): it parses back
    marker = '<script id="analysis-data" type="application/json">'
    assert marker in html
    data = json.loads(html.split(marker, 1)[1].split("</script>", 1)[0])
    assert {c["model"] for c in data["cells"]} == {"alpha", "beta", "gamma"}
    # one source of truth: the same doc, byte-equal, rides in both pages
    assert (
        html.split(marker, 1)[1].split("</script>", 1)[0]
        == (page.split(marker, 1)[1].split("</script>", 1)[0])
    )
    # the rates blob parses back, and every priced model has its <option>
    # (the all-models spirit of the dashboard's filter-options check)
    marker_rates = '<script id="rates-data" type="application/json">'
    assert marker_rates in html
    rates = json.loads(html.split(marker_rates, 1)[1].split("</script>", 1)[0])
    assert rates["per"] == 1_000_000
    for model in rates["rates"]:
        assert f'value="{model}"' in html
    # every __TOKEN__ placeholder resolved: none survives in either page
    for pagina in (page, html):
        assert re.search(r"__[A-Z_]+__", pagina) is None

    # the theme tokens are copied, not drifted: both templates carry the
    # identical :root / dark-mode block
    assert _block_tokens(templates / "dashboard_template.html") == _block_tokens(template)


# ---------------------------------------------------------------------------
# stale-artifact trap: every template class token must resolve (Gate 2 F2)
# ---------------------------------------------------------------------------

# the tiny JS-state allowlist: state classes the JS builds from values
# (v.winner -> "legacy"/"new", "tie"; the nodata chip; classList "active")
_STATE_CLASSES = {"active", "nodata", "tie", "legacy", "new"}


def _tokens_clase(body: str) -> set[str]:
    """Every static class token a template can put on an element: the HTML
    class attributes plus the fragments built in JS (innerHTML strings like
    '<span class=\"chip ' + v.winner', classList.add("active")). A value cut
    by a quote/plus (JS concatenation) contributes only its real tokens."""
    tokens: set[str] = set()
    for m in re.finditer(r'class="([^"+]*)"', body):
        tokens.update(t for t in m.group(1).split() if re.fullmatch(r"[A-Za-z][\w-]*", t))
    for m in re.finditer(r"class='([^\"+]*)'", body):
        tokens.update(t for t in m.group(1).split() if re.fullmatch(r"[A-Za-z][\w-]*", t))
    for m in re.finditer(r'classList\.(?:add|remove|toggle)\("([^"]+)"\)', body):
        tokens.update(m.group(1).split())
    return tokens


def _selectores_pagina(estilos: list[str]) -> set[str]:
    """Class selectors of a template's page CSS (its custom <style> blocks —
    the only styling the template ships besides the CDN head block)."""
    css = re.sub(r"/\*.*?\*/", "", "\n".join(estilos), flags=re.S)
    sels: set[str] = set()
    for m in re.finditer(r"([^{}]+)\{", css):
        sels.update(re.findall(r"\.([A-Za-z][\w-]*)", m.group(1)))
    return sels


def _selectores_js(body: str) -> set[str]:
    """Class tokens the template's own JS binds: querySelector(All) and
    closest() selector strings, classList arguments, and the ids reached
    through the $() helper (a class token sharing that exact string marks the
    same element)."""
    ligaduras: set[str] = set()
    for m in re.finditer(r'(?:querySelector(?:All)?|closest)\("([^"]+)"\)', body):
        ligaduras.update(re.findall(r"\.([A-Za-z][\w-]*)", m.group(1)))
    for m in re.finditer(r"\$\(\"([^\"]+)\"\)", body):
        ligaduras.add(m.group(1))
    return ligaduras


def test_template_class_tokens_resolve_to_a_consumer():
    """The stale-artifact trap: every static class token in both templates must
    resolve to a real consumer — a page-CSS selector in that template's own
    <style>, a selector the template's JS binds, or the tiny JS-state
    allowlist. There is no vendored sheet to freeride on any more (Pages-first:
    styling loads from the CDN, and the Lucide icons ride data-lucide
    attributes, which carry no classes), so a stray utility class in the
    markup fails red."""
    web = pathlib.Path(analyze_module.__file__).parent / "web"
    rojos: list[str] = []
    for name in ("dashboard_template.html", "calculator_template.html"):
        body = (web / name).read_text(encoding="utf-8")
        tokens = _tokens_clase(body)
        pagina = _selectores_pagina(re.findall(r"<style>(.*?)</style>", body, re.S))
        js = _selectores_js(body)
        for t in sorted(tokens):
            if t in pagina or t in js or t in _STATE_CLASSES:
                continue
            rojos.append(f"{name}: .{t}")
    assert not rojos, (
        "class tokens with no consumer (dead markup, or a stray Tailwind "
        "utility typed into the markup — the Pages-first pages carry no "
        "compiled sheet to resolve it): " + str(rojos)
    )


def test_calculator_prices_every_model_the_table_lists(tmp_path):
    """The calculator's model set is the FULL Ollama-reported pricing table —
    every model the chosen table prices, measured or not, and the multi-select
    starts with all of them selected — while the dashboard keeps its
    cells-derived rates payload exactly as it always was."""
    pricing = tmp_path / "pricing-extra"
    write_table(
        pricing,
        "2026-08-31",
        {
            **TABLE_V1,
            "delta": {"input": 0.90, "cached_input": 0.45, "output": 1.80},  # never measured
        },
    )
    craft_dataset(tmp_path)
    analyze_doc(tmp_path, "--pricing-dir", str(pricing), "--table-version", "2026-08-31")
    folder = tmp_path / "analysis"
    calc = (folder / "calculator.html").read_text(encoding="utf-8")
    page = (folder / "dashboard.html").read_text(encoding="utf-8")
    marker = '<script id="rates-data" type="application/json">'
    rates = json.loads(calc.split(marker, 1)[1].split("</script>", 1)[0])
    # the calculator's rows and options cover the whole table, delta included
    assert set(rates["rates"]) == {"alpha", "beta", "gamma", "delta"}
    assert 'value="delta"' in calc
    # the picker's default state: the hidden multi-select carries every
    # embedded-rate model as an option, and the JS state starts with ALL of
    # them selected
    assert re.search(r'<select(?=[^>]*\bid="filter-model")(?=[^>]*\bmultiple\b)[^>]*>', calc)
    assert "var ALL_MODELS = Object.keys(RATES.rates" in calc
    assert "models: ALL_MODELS.slice()" in calc
    # the dashboard's payload stays cells-derived: delta is nowhere on it
    dash_rates = json.loads(page.split(marker, 1)[1].split("</script>", 1)[0])
    assert set(dash_rates["rates"]) == {"alpha", "beta", "gamma"}
    assert 'value="delta"' not in page


def test_navbar_links_the_two_pages_and_marks_the_current_one(tmp_path):
    """The split ships both pages in one bundle: a top navbar with relative
    links (dashboard.html / calculator.html — the Pages publication copies
    dashboard.html to BOTH index.html and dashboard.html, so both bundle names
    and both nav links resolve everywhere) on each page, the current page
    marked with aria-current and the theme-token active state."""
    page, calc = render_bundle_v2(tmp_path)
    # dashboard: Dashboard is the current page, Calculator the link out
    assert '<a href="dashboard.html" aria-current="page">Dashboard</a>' in page
    assert '<a href="calculator.html">Calculator</a>' in page
    # calculator: the roles swap
    assert '<a href="dashboard.html">Dashboard</a>' in calc
    assert '<a href="calculator.html" aria-current="page">Calculator</a>' in calc
    # every navbar href resolves to a file the bundle actually ships:
    # index.html exists only after the gh-pages rename, never inside the bundle
    templates = pathlib.Path(analyze_module.__file__).parent / "web"
    for name in ("dashboard_template.html", "calculator_template.html"):
        body = (templates / name).read_text(encoding="utf-8")
        nav = body[body.index('<nav class="topnav"') : body.index("</nav>")]
        for href in re.findall(r'href="([^"]+)"', nav):
            assert href in {"dashboard.html", "calculator.html"}, href
    # the navbar sits above the page's main content on both pages
    assert page.index('class="topnav"') < page.index("<main")
    assert calc.index('class="topnav"') < calc.index("<main")
    # the active state paints through the theme tokens only
    assert 'a[aria-current="page"]' in page and 'a[aria-current="page"]' in calc


# ---------------------------------------------------------------------------
# the 4 fixed sensitivity sweeps
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# verdict margins {winner, margin_pct} and the allocated/session rules (#40)
# ---------------------------------------------------------------------------


def test_every_measured_verdict_carries_its_margin_pct(tmp_path):
    """margin_pct = (loser - winner) / winner, as a percentage of the winner's
    PAID cost — how much more expensive the loser is, riding the verdict
    object. The new side's credits divide by credit_ratio = 3 first.

    Hand math (cell C): legacy $9.6*U vs new S0 $2/3 paid -> new wins by
    (9.6U - 2/3) / (2/3). Cell B: legacy $0.2U vs new $2/3 paid -> legacy wins
    by (2/3 - 0.2U) / 0.2U. gamma has no legacy reading: no data, no margin."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")

    c = cell(doc, "beta", "qa_short")
    expected_c = (9.6 * U - 2.0 / 3) / (2.0 / 3) * 100
    assert c["verdict"]["s0"] == {"winner": "new", "margin_pct": expected_c}
    b = cell(doc, "beta", "throughput")
    assert near(b["verdict"]["s0"]["margin_pct"], (2.0 / 3 - 0.2 * U) / (0.2 * U) * 100, 9)
    assert b["verdict"]["s0"]["winner"] == "legacy"
    g = cell(doc, "gamma", "qa_short")
    assert g["verdict"]["s0"] == {"winner": "no data", "margin_pct": None}


def test_a_tie_verdict_carries_no_margin(tmp_path):
    """Inside the tie band (min of 2 ticks and 5 % of the cheaper cost) the
    verdict is a tie with no margin to report. Hand math (credit_ratio 1, the
    1:1 credit comparison): legacy dpp 2.04/U sits $0.04 above the new S0
    $2.0 — under the $0.046 two-tick band."""
    pricing = with_tables(tmp_path)
    requests = [req("throughput", "beta", "bTie", tok_in=2_000_000, tok_out=0)]
    write_raw(
        tmp_path,
        requests,
        [batch("throughput", "beta", "bTie", dpp_weekly=2.04 / U)],
    )
    doc = analyze_doc(
        tmp_path,
        "--pricing-dir",
        pricing,
        "--table-version",
        "2026-08-31",
        "--credit-ratio",
        "1",
    )
    t = cell(doc, "beta", "throughput")
    assert t["verdict"]["s0"] == {"winner": "tie", "margin_pct": None}


def test_credit_ratio_re_denominates_the_comparison(tmp_path):
    """Methodology v1.3: verdicts, margins and the pp/1M threshold compare
    PAID dollars — the new side's credits divide by --credit-ratio (default 3,
    the anchor's Max tier $100 -> $300 credits) — while the persisted per-task
    cost figures stay at face value and the legacy session fallback never
    re-denominates. --credit-ratio 1 reproduces the legacy 1:1 comparison."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc3 = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    doc1 = analyze_doc(
        tmp_path,
        "--pricing-dir",
        pricing,
        "--table-version",
        "2026-08-31",
        "--credit-ratio",
        "1",
    )
    assert doc3["base_params"]["credit_ratio"] == 3.0
    assert doc1["base_params"]["credit_ratio"] == 1.0

    a3, a1 = cell(doc3, "alpha", "qa_short"), cell(doc1, "alpha", "qa_short")
    # the threshold scales EXACTLY by the ratio: the credits make the new side
    # 3x cheaper in paid dollars, so the legacy quota tolerates 3x FEWER
    # pp/1M before the discounted credits undercut it
    assert near(a3["threshold_pp_per_1m"]["s0"], a1["threshold_pp_per_1m"]["s0"] / 3, 9)
    # ...and ratio 1 reproduces the 1:1 credit comparison of methodology v1.2
    assert near(a1["threshold_pp_per_1m"]["s0"], 0.80 / U, 4)  # the v1.2 value

    b3, b1 = cell(doc3, "beta", "throughput"), cell(doc1, "beta", "throughput")
    # the per-task cost figures stay at face value under either ratio
    assert near(b3["new_cost_task_s0_usd"], 2.0, 9)
    assert near(b1["new_cost_task_s0_usd"], 2.0, 9)
    # the margin prices the winner in paid dollars: (2/3 - 0.2U) / 0.2U under
    # ratio 3, exactly one third of the v1.2 1:1 margin
    assert near(b3["verdict"]["s0"]["margin_pct"], (2.0 / 3 - 0.2 * U) / (0.2 * U) * 100, 9)
    assert near(b1["verdict"]["s0"]["margin_pct"], (2.0 - 0.2 * U) / (0.2 * U) * 100, 9)
    # the two margins are genuinely different numbers
    assert not near(b3["verdict"]["s0"]["margin_pct"], b1["verdict"]["s0"]["margin_pct"], 2)


def test_credit_ratio_below_one_is_rejected(tmp_path):
    """No tier sells credits below face value (Team x2, Pro/Max x3): a ratio
    < 1 would silently price the new side MORE EXPENSIVE than its dollars —
    the validator refuses it instead of pricing an unsupported hypothesis."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    for ratio in ("0.5", "0", "-3"):
        code, _output, errors = analyze_cli(
            tmp_path,
            "--pricing-dir",
            pricing,
            "--table-version",
            "2026-08-31",
            "--credit-ratio",
            ratio,
        )
        assert code == 2, ratio
        assert "must be a finite number >= 1" in errors, ratio


def test_a_sub_tick_winner_prices_with_its_session_equivalent(tmp_path):
    """A weekly reading of 0.0 is sub-tick, not free: the cost tends to zero
    per task but accumulates, so the margin prices the winner with its
    session-derived weekly-equivalent (under the R mapping — session $/pp =
    weekly $/pp / R — the session dollar figure IS that estimate). The
    fallback prices the LEGACY winner, so it never re-denominates. Hand math:
    legacy weekly $0.0 vs new S0 $2/3 paid -> legacy wins; margin =
    (2/3) / (0.2 * U / SESSION_R) * 100 — unbounded, far above 100 %."""
    pricing = with_tables(tmp_path)
    b = batch("throughput", "beta", "bSub")
    b["dpp_weekly"] = 0.0
    b["dpp_session"] = 0.2
    requests = [req("throughput", "beta", "bSub", tok_in=2_000_000, tok_out=0)]
    write_raw(tmp_path, requests, [b])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    t = cell(doc, "beta", "throughput")
    assert t["verdict"]["s0"]["winner"] == "legacy"
    expected = (2.0 / 3) / (0.2 * U / SESSION_R) * 100
    assert near(t["verdict"]["s0"]["margin_pct"], expected, 9)


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
    pooled = batch(
        None,
        "alpha",
        "bP",
        n=3,
        reps=2,
        pool={"workloads": ["multi_turn", "reasoning"], "reps": 2},
        dpp_weekly=0.5,
    )
    pooled["dpp_session"] = 0.4
    write_raw(tmp_path, requests, [pooled])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    session = U / 6.22
    a = doc["pooled"][0]["allocations"]
    for reading in a.values():
        assert reading["reading"] == "allocated"
        assert reading["verdict"] is None
    mt, rea = a["multi_turn"], a["reasoning"]
    assert mt["attempted"] == 2 and mt["completed"] == 1
    assert rea["attempted"] == 1 and rea["completed"] == 1
    assert near(mt["cost_task_attempted_usd"], 0.375 * U / 2, 12)
    assert near(mt["cost_task_completed_usd"], 0.375 * U / 1, 12)
    assert near(rea["cost_task_attempted_usd"], 0.125 * U, 12)
    assert near(mt["cost_task_attempted_usd_session"], 0.3 * session / 2, 12)
    assert near(rea["cost_task_attempted_usd_session"], 0.1 * session, 12)
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
    pooled = batch(
        None,
        "alpha",
        "bMix",
        n=2,
        reps=1,
        pool={"workloads": ["multi_turn", "reasoning"], "reps": 1},
        dpp_weekly=0.4,
    )
    write_raw(tmp_path, requests, [pooled])
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
    carrying its unanchored caveat in the doc and the notes.

    Hand math (alpha/qa_short, dpp 0.2 both windows, 2 tasks): session
    $/task = 0.2 * (U/6.22) / 2."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    session = U / 6.22

    # the derived session $/pp is stamped with its caveat, never as an anchor
    s = doc["base_params"]["session"]
    assert s["ratio_r"] == 6.22
    assert s["usd_per_pp"] == session
    assert "unanchored" in s["caveat"] and "secondary" in s["caveat"]

    a = cell(doc, "alpha", "qa_short")
    # both windows per bracket: the per-rep rows carry both dpp...
    assert all(r["dpp_weekly"] == 0.2 and r["dpp_session"] == 0.2 for r in a["reps"])
    # ...and the cell's legacy distributions exist for both windows
    assert near(a["legacy_cost_task_usd_session"]["median"], 0.2 * session / 2, 12)
    assert near(a["legacy_cost_task_usd"]["median"], 0.2 * U / 2, 12)
    assert near(
        a["legacy_cost_completed_usd_session"]["median"],
        (0.2 * session / 2 + 0.2 * session) / 2,  # median of rep1 (2 tasks) and rep2 (1)
        9,
    )
    # every curve point (one per bracket) reports both windows
    puntos = [p for p in doc["dp_tokens_curve"] if p["batch_id"] == "bA1"]
    assert puntos and puntos[0]["dpp_weekly"] == 0.2 and puntos[0]["dpp_session"] == 0.2
    # the caveat ships in the doc's notes (the dashboard no longer renders
    # the notes footer, so the doc is the caveat's carrier)
    assert "unanchored" in doc["notes"]


def test_sweep_rates_plus20_flips_the_borderline_cell(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    # a borderline cell for the PAID comparison: legacy $0.75 sits between the
    # new side's paid $0.6667 (x1.0) and $0.8 (x1.2) — real margins both ways,
    # so +20 % rates push the new side past the legacy side
    requests = [req("tool_calling", "beta", "bFlip", tok_in=2_000_000, tok_out=0)]
    write_raw(tmp_path, requests, [batch("tool_calling", "beta", "bFlip", dpp_weekly=0.75 / U)])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    sweep = doc["sensitivity"]["rates"]
    assert sweep["factors"] == [0.8, 1.2]
    turns = {(f["model"], f["workload"]): f["verdict"] for f in sweep["flips"]["1.2"]}
    assert turns[("beta", "tool_calling")]["winner"] == "legacy"
    assert len(sweep["flips"]["1.2"]) == 1
    assert sweep["flips"]["0.8"] == []  # cheaper rates flip nothing here


def test_sweep_cache_scenarios_recompute_only_discounted_models(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    sweep = doc["sensitivity"]["cache"]
    assert sweep["s_values"] == [0.0, 0.25, 0.5, 0.9]
    # S1(s=0.9): 10% input + 90% cached + output
    expected = (1000 * 0.1 * 0.6 + 1000 * 0.9 * 0.3 + 500 * 1.2) / 1e6
    assert near(sweep_cell(sweep, "0.9", "alpha", "qa_short")["new_cost_task_usd"], expected, 9)
    # beta has no discount: its cost is identical under every hit rate
    for s in ("0", "0.25", "0.5", "0.9"):
        cell = sweep_cell(sweep, s, "beta", "throughput")
        assert near(cell["new_cost_task_usd"], 2.0, 9)
    assert sweep["flips"]["0.9"] == []  # alpha's winner is stable across S here


def test_sweep_anchor_scales_legacy_side_and_flips(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    # a borderline cell for the PAID comparison: legacy $0.8 beats the new
    # side's paid $0.6667 at the anchor, but -30% drops it to $0.56 and the
    # winner flips to legacy (real margins both ways, past the two-tick band)
    requests = [req("tool_calling", "beta", "bAncla", tok_in=2_000_000, tok_out=0)]
    write_raw(tmp_path, requests, [batch("tool_calling", "beta", "bAncla", dpp_weekly=0.8 / U)])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    sweep = doc["sensitivity"]["anchor"]
    assert sweep["factors"] == [0.7, 1.0, 1.3]
    c07 = sweep_cell(sweep, "0.7", "beta", "tool_calling")
    assert near(c07["legacy_cost_task_usd"], 0.8 * 0.7, 9)
    assert c07["verdict"]["winner"] == "legacy"
    turns = {(f["model"], f["workload"]) for f in sweep["flips"]["0.7"]}
    assert turns == {("beta", "tool_calling")}
    # the measured pp/1M never moves with the anchor (it is meter-native)
    assert (
        sweep_cell(sweep, "1.3", "beta", "tool_calling")["pp_per_1m"] == (0.8 / U) * 1e6 / 2_000_000
    )
    # the threshold DOES ride with the anchor (it divides by USD/pp): x0.7
    # raises it by 1/0.7, exactly what the sweep's note promises
    base_thr = cell(doc, "beta", "tool_calling")["threshold_pp_per_1m"]["s0"]
    assert near(c07["threshold_pp_per_1m"], base_thr / 0.7, 9)
    # cell C's paid gap is far too wide for a ±30 % anchor to close: it stays new
    assert sweep_cell(sweep, "0.7", "beta", "qa_short")["verdict"]["winner"] == "new"


def test_sweep_k_axis_reads_the_concurrency_cells(tmp_path):
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    sweep = doc["sensitivity"]["k_axis"]
    rows = {f["model"]: f for f in sweep["models"]}
    por_k = {c["k"]: c for c in rows["alpha"]["cells"]}
    # cost per attempted task = dpp x U / 8 tasks
    assert near(por_k[1]["cost_task_attempted_usd"], 1.6 * U / 8, 9)
    assert near(por_k[4]["cost_task_attempted_usd"], 3.2 * U / 8, 9)
    # alpha's per-task cost grows 2 then 4 ticks with the same tokens, a real
    # margin beyond the one-tick tie band -> overhead
    assert rows["alpha"]["verdict"] == "overhead"
    # beta grows by EXACTLY one tick: within the meter's resolution — read
    # through the residue band, so the boundary is squeeze deterministically,
    # never a coin flip on the payloads' last bits
    assert rows["beta"]["verdict"] == "squeeze"
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
    resolved = doc["s_per_model"]["alpha"]
    assert resolved["s"] == 0.8 and resolved["source"] == "measured"
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
    code, output, errors = analyze_cli(
        tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31"
    )
    assert code == 0, output or errors
    assert (tmp_path / "analysis" / "analysis.json").exists()
    assert "analysis" in (output + errors).lower()


def test_analyze_without_raw_data_is_a_clean_error(tmp_path):
    pricing = with_tables(tmp_path)
    code, _output, errors = analyze_cli(
        tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31"
    )
    assert code == 2
    assert "raw" in errors.lower()
    assert "Traceback" not in errors
    assert not (tmp_path / "analysis").exists()


def test_level_and_model_filters_narrow_the_cells(tmp_path):
    """--model/--level narrow the doc's cells; the narrowed doc is a stamped
    re-run (a filtered default-S run would shrink the persisted reference and
    is refused - see test_reference_bundle_is_never_shrunk)."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing, "--s", "0.35", "--model", "alpha")
    assert {c["model"] for c in doc["cells"]} == {"alpha"}
    doc2 = analyze_doc(tmp_path, "--pricing-dir", pricing, "--s", "0.35", "--level", "T2")
    assert doc2["cells"] == []
    assert doc2["who_wins"] == []


def test_reference_bundle_is_never_shrunk(tmp_path):
    """A filtered default-S run refuses instead of overwriting the persisted
    analysis/ reference with fewer cells (methodology v1.2, #46); the full
    re-derivation (a new table version) still rewrites the reference in place."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    analyze_doc(tmp_path, "--pricing-dir", pricing)  # the full reference, written to analysis/
    path = tmp_path / "analysis" / "analysis.json"
    reference = path.read_text(encoding="utf-8")
    code, _sal, err = analyze_cli(tmp_path, "--pricing-dir", pricing, "--model", "alpha")
    assert code == 2, err
    assert "never shrunk" in err
    assert path.read_text(encoding="utf-8") == reference  # untouched
    # the documented re-derivation still rewrites the reference in place
    doc2 = analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-09-01")
    assert doc2["base_params"]["table_version"] == "2026-09-01"
    assert json.loads(path.read_text(encoding="utf-8"))["base_params"]["table_version"] == (
        "2026-09-01"
    )


def test_analyze_ignores_torn_and_foreign_lines(tmp_path):
    """A torn tail line (crash mid-write) and non-JSON junk are skipped."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    path = tmp_path / "runs" / f"requests-{RUN}.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{torn\n\n[]\n", encoding="utf-8")
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
    row = doc["sensitivity"]["k_axis"]["models"][0]
    assert row["cells"][0]["attempted"] == 7
    assert near(row["cells"][0]["cost_task_attempted_usd"], 1.6 * U / 7, 9)


def test_cache_sweep_survives_a_token_report_missing_the_output_count(tmp_path):
    """A done object can carry prompt_eval_count but omit eval_count: the
    cell extrapolates nothing, and the sweep skips it instead of crashing."""
    pricing = with_tables(tmp_path)
    requests = [req("qa_short", "alpha", "bSinOut", tok_out=0)]
    requests[0]["tok_out"] = None  # the API never reported the output count
    write_raw(tmp_path, requests, [batch("qa_short", "alpha", "bSinOut", dpp_weekly=0.2)])
    code, _output, errors = analyze_cli(
        tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31"
    )
    assert code == 0, errors


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
    pooled = batch(
        None,
        "alpha",
        "bP",
        n=3,
        reps=2,
        pool={"workloads": ["multi_turn", "reasoning"], "reps": 2},
        dpp_weekly=0.5,
    )
    pooled["dpp_session"] = 0.4
    write_raw(tmp_path, requests, [pooled])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    # A pooled bracket never becomes a measured cell, even though its request
    # lines name task workloads: the legacy is allocated, never verdicted.
    assert all(c["model"] != "alpha" for c in doc["cells"])
    rows = doc["pooled"]
    assert len(rows) == 1
    row = rows[0]
    assert row["batch_id"] == "bP" and row["model"] == "alpha"
    assert row["workloads"] == ["multi_turn", "reasoning"] and row["reps"] == 2
    assert row["dpp_weekly"] == 0.5 and row["dpp_session"] == 0.4
    a = row["allocations"]
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


# ---------------------------------------------------------------------------
# stamped re-runs: S != the versioned default never edits the persisted pair
# ---------------------------------------------------------------------------


def test_custom_s_births_a_stamped_set_and_never_touches_the_reference(tmp_path, fake_cli):
    """`analyze --s 0.35` is a stamped re-run (methodology v1.2, #46): a NEW
    derivatives set under its parameter-stamped folder, its parameters in the
    doc's header (methodology version included), the persisted s0/s1 set and
    the raw untouched."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)

    # the reference set first: the persisted S0/S1 pair under analysis/
    reference = analyze_doc(tmp_path, "--pricing-dir", pricing)
    assert reference["base_params"]["methodology_version"] == analyze_module.METHODOLOGY_VERSION
    assert reference["base_params"]["s"] == 0.5
    assert (tmp_path / "analysis" / "analysis.json").exists()
    antes = (tmp_path / "analysis" / "analysis.json").read_bytes()

    stamp = analyze_doc(tmp_path, "--pricing-dir", pricing, "--s", "0.35")
    assert stamp["base_params"]["s"] == 0.35
    assert stamp["base_params"]["methodology_version"] == analyze_module.METHODOLOGY_VERSION
    folder = tmp_path / "analysis-s0.35"
    assert (folder / "analysis.json").exists()
    assert (folder / "dashboard.html").exists()
    # the stamped set recomputes the same cells: S0 columns identical, the
    # S1 sensitivity moved to the custom hit rate
    assert [c["new_cost_task_s0_usd"] for c in stamp["cells"]] == [
        c["new_cost_task_s0_usd"] for c in reference["cells"]
    ]
    assert [c["new_cost_task_s1_usd"] for c in stamp["cells"]] != [
        c["new_cost_task_s1_usd"] for c in reference["cells"]
    ]
    # the persisted s0/s1 set is never edited by a stamped re-run
    assert (tmp_path / "analysis" / "analysis.json").read_bytes() == antes
    assert not (tmp_path / "analysis-s0.35" / "analysis.json").samefile(
        tmp_path / "analysis" / "analysis.json"
    )
    assert fake_cli.calls == []  # a re-run never touches the API


def test_stamp_folder_names_follow_the_s_value(tmp_path, fake_cli):
    """The stamp is the parameter itself: 0 -> analysis-s0, 0.9 -> analysis-s0.9;
    an explicitly-passed default keeps the persisted analysis/ folder."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    for s, folder in (("0", "analysis-s0"), ("0.9", "analysis-s0.9"), ("0.5", "analysis")):
        code, output, errors = analyze_cli(tmp_path, "--pricing-dir", pricing, "--s", s)
        assert code == 0, output or errors
        assert (tmp_path / folder / "analysis.json").exists(), folder
    assert not (tmp_path / "analysis-s0.5").exists()  # the default is not a stamp


def test_zero_movement_and_aborted_brackets_never_enter_a_cell(tmp_path):
    """A bracket whose BOTH windows read exactly the pre-burst value closed over
    stale reads (the documented 60-90 s lag outlasting the settle): the
    'stable' exit is the lag masquerading as stability, so its dpp 0 is a
    failed registration, never a measurement — and an aborted bracket's burst
    broke its own contract (a dropped request inflates pp/1M). Neither enters
    a cell's median; the raw keeps both for the operator, and the rep rows
    carry settle_exit so a consumer can audit the exclusion."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    zero = batch("qa_short", "alpha", "bCero", rep=3, dpp_weekly=0.0)
    zero["dpp_session"] = 0.0  # BOTH windows at the pre-burst plateau
    aborted = batch("qa_short", "alpha", "bAbortada", rep=4, dpp_weekly=0.5)
    aborted["notes"] = "aborted: probe 429 mid-burst"  # non-zero dpp: only the abort excludes it
    write_raw(tmp_path, [], [zero, aborted])
    doc = analyze_doc(tmp_path, "--pricing-dir", pricing)
    got = cell(doc, "alpha", "qa_short")
    assert [r["rep"] for r in got["reps"]] == [1, 2]  # the two extra brackets stay out
    assert all(r["settle_exit"] == "stable" for r in got["reps"])  # the marker rides the row
    # the dp-tokens curve applies the same rule: neither bracket is a
    # measurement, so neither becomes a curve point
    ids = {p["batch_id"] for p in doc["dp_tokens_curve"]}
    assert "bCero" not in ids and "bAbortada" not in ids
