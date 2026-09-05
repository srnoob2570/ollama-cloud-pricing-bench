"""The shared-formulas block: the pages' JS math equals the persisted rule.

Python and the browser speak different languages, so a literal single source is
unreachable without a JS engine in the runtime; the honest alternative is one
hand copy per template (the byte-equal `<script id="shared-formulas">` block)
plus a guard that never sleeps. Three assertions: the block is the same bytes
on both pages (node-free), the shipped JS matches verdict_of / new_task_cost /
statistics.median on the same hand-math goldens the CLI-seam tests use, and the
rendered dashboard's own block, run over the data the dashboard embeds,
re-verdicts every persisted s1 cell. The node-based assertions skip when node
is absent; the block-equality one always runs.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import statistics
import subprocess

import pytest

from test_analyze import RUN, U, analyze_doc, craft_dataset, with_tables

from obench import analyze as analyze_module
from obench.analyze import verdict_of
from obench.cost import new_task_cost
from obench.meter import SESSION_R
from obench.pricing import Rate

WEB = pathlib.Path(analyze_module.__file__).parent / "web"
DRIVER = pathlib.Path(__file__).parent / "js_driver.js"
MARCADOR = '<script id="shared-formulas">'
NOMBRES = ("dashboard_template.html", "calculator_template.html")

TICK = 0.1 * U  # one meter tick in dollars: the analyze build's TICKUSD


def _bloque(nombre: str) -> str:
    cuerpo = (WEB / nombre).read_text(encoding="utf-8")
    assert MARCADOR in cuerpo, nombre
    return cuerpo.split(MARCADOR, 1)[1].split("</script>", 1)[0]


def run_js(bloque: str, prelude: str, exprs: list[str], tmp_path: pathlib.Path) -> list:
    ruta = tmp_path / "shared-formulas.js"
    ruta.write_text(bloque, encoding="utf-8")
    trabajo = {"file": str(ruta), "prelude": prelude, "exprs": exprs}
    proc = subprocess.run(
        ["node", str(DRIVER)],
        input=json.dumps(trabajo),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_shared_formulas_block_is_byte_equal_across_pages():
    """One hand copy per template, and they must not drift apart: the block is
    the same bytes on both pages, and it is self-contained (pure functions, no
    DOM or page globals), so the node-free equality is the only intra-template
    guard needed."""
    tablero, calculadora = (_bloque(n) for n in NOMBRES)
    assert tablero == calculadora
    for prohibido in (
        "document",
        "window",
        "DATA",
        "RATES",
        "estado",
        "PER",
        "TICKUSD",
        "CREDITRATIO",
    ):
        assert re.search(rf"\b{prohibido}\b", tablero) is None, prohibido
    for funcion in ("isNum", "isNull", "newCostCore", "verdictOf", "medianOf"):
        assert f"function {funcion}(" in tablero, funcion


GOLDEN_VERDICTS = [
    # (legacy, nuevo, tick_usd, legacy_session, credit_ratio) — the manual-math
    # cells of test_analyze: 9.6U vs 2/3 paid (new), 0.2U vs 2/3 (legacy), no
    # data, the two-tick-band tie at ratio 1, the sub-tick winner priced
    # with its session equivalent, and the ratio-1 1:1 comparison
    (9.6 * U, 2.0, TICK, None, 3.0),
    (0.2 * U, 2.0, TICK, None, 3.0),
    (None, 2.0, TICK, None, 3.0),
    (2.04, 2.0, TICK, None, 1.0),
    (0.0, 2.0, TICK, 0.2 * U / SESSION_R, 3.0),
    (0.2 * U, 2.0, TICK, None, 1.0),
]

GOLDEN_MARGINS = [
    # the hand-math literals, one per fixture above, anchored here so neither
    # side can drift into agreement by copying the other's mistake
    ((9.6 * U - 2.0 / 3) / (2.0 / 3) * 100, "new"),
    ((2.0 / 3 - 0.2 * U) / (0.2 * U) * 100, "legacy"),
    (None, "no data"),
    (None, "tie"),
    ((2.0 / 3) / (0.2 * U / SESSION_R) * 100, "legacy"),
    ((2.0 - 0.2 * U) / (0.2 * U) * 100, "legacy"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_verdict_and_cost_match_the_persisted_rule(tmp_path):
    """The shipped verdictOf/newCostCore/medianOf return what Python's
    verdict_of/new_task_cost/statistics.median return on the same inputs, and
    both agree with the hand-written margins above."""
    bloque = _bloque(NOMBRES[0])
    exprs = [
        f"verdictOf({json.dumps(v[0])}, {json.dumps(v[1])}, {json.dumps(v[2])}, "
        f"{json.dumps(v[3])}, {json.dumps(v[4])})"
        for v in GOLDEN_VERDICTS
    ]
    exprs += [
        "newCostCore(1000, 500, ALPHA, 0, 1e6)",  # cell A S0: (1000*.6+500*1.2)/1e6
        "newCostCore(1000, 500, ALPHA, 0.5, 1e6)",  # cell A S1: (500*.6+500*.3+500*1.2)/1e6
        "newCostCore(2000000, 0, BETA, 0.9, 1e6)",  # the pin: cached=input, S(x) is S0
        "newCostCore(10/11, 1/11, BETA, 0.5, 1e6)",  # the calculator's weighted split
        "medianOf([3, 1, 2])",  # odd: the middle value
        "medianOf([4, 1, 3, 2])",  # even: the mean of the two middles
        "medianOf([5])",
        "medianOf([])",  # no readings: no median
    ]
    prelude = (
        "var ALPHA = {input: 0.6, cached_input: 0.3, output: 1.2, has_cache_discount: true};"
        "var BETA = {input: 1.0, cached_input: 1.0, output: 2.0, has_cache_discount: false};"
    )
    js = run_js(bloque, prelude, exprs, tmp_path)

    # verdicts: Python and JS, then the hand literals
    for fixture, (margen, ganador), salida in zip(GOLDEN_VERDICTS, GOLDEN_MARGINS, js):
        py = verdict_of(*fixture)
        assert py == {"winner": ganador, "margin_pct": margen}, fixture
        assert salida == {"winner": ganador, "margin_pct": margen}, fixture
        if margen is not None:
            assert round(salida["margin_pct"], 9) == round(margen, 9)
    # costs: Python and JS, then the hand literals
    alfa = Rate("alpha", 0.60, 0.30, 1.20)
    beta = Rate("beta", 1.00, 1.00, 2.00)
    pares = [
        (new_task_cost(1000, 500, alfa, s=0, per=1_000_000), js[6], 0.0012),
        (new_task_cost(1000, 500, alfa, s=0.5, per=1_000_000), js[7], 0.00105),
        (new_task_cost(2_000_000, 0, beta, s=0.9, per=1_000_000), js[8], 2.0),
        (
            new_task_cost(10 / 11, 1 / 11, beta, s=0.5, per=1_000_000),
            js[9],
            (10 / 11 + 1 / 11 * 2.0) / 1e6,
        ),
    ]
    for py, js_cost, mano in pares:
        assert round(py, 12) == round(js_cost, 12) == round(mano, 12), py
    # medians: the convention the persisted doc uses
    assert js[10] == statistics.median([3, 1, 2]) == 2
    assert js[11] == statistics.median([4, 1, 3, 2]) == 2.5
    assert js[12] == 5
    assert js[13] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_shipped_block_re_verdicts_the_rendered_dashboard(tmp_path):
    """The artifact-level pin: the block the dashboard ships, run over the data
    the dashboard embeds, reproduces every persisted s1 verdict. The prelude is
    only the page's data plumbing (which tokens, which S, which tick) — the
    math rides from the shipped block itself."""
    pricing = with_tables(tmp_path)
    craft_dataset(tmp_path)
    # alpha gets a conclusive measured hit rate: its S1 must win the parity
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
    analyze_doc(tmp_path, "--pricing-dir", pricing, "--table-version", "2026-08-31")
    html = (tmp_path / "analysis" / "dashboard.html").read_text(encoding="utf-8")
    datos = json.loads(
        html.split('<script id="analysis-data" type="application/json">', 1)[1].split(
            "</script>", 1
        )[0]
    )
    tarifas = json.loads(
        html.split('<script id="rates-data" type="application/json">', 1)[1].split("</script>", 1)[
            0
        ]
    )
    # the slider sits at the assumed S1 default, so every cell's JS verdict
    # must match the persisted s1 verdict (measured models keep their measured s)
    prelude = (
        f"var DATA = {json.dumps(datos)};"
        f"var RATES = {json.dumps(tarifas)};"
        "var PER = RATES.per;"
        "var estado = {slider: 50};"
        "function sOf(model) {"
        "  var e = DATA.s_per_model && DATA.s_per_model[model];"
        '  return e && e.source === "measured" && isNum(e.s) ? e.s : estado.slider / 100;'
        "}"
        "function cellVerdict(c) {"
        "  var legacy = c.legacy_cost_task_usd ? c.legacy_cost_task_usd.median : null;"
        "  var session = c.legacy_cost_task_usd_session ? c.legacy_cost_task_usd_session.median : null;"
        "  return verdictOf(legacy,"
        "    newCostCore(c.tok_in_median, c.tok_out_median, RATES.rates[c.model], sOf(c.model), PER),"
        "    DATA.base_params.tick_usd, session, DATA.base_params.credit_ratio);"
        "}"
    )
    celdas = [c for c in datos["cells"] if c["workload"] != "concurrency"]
    exprs = [f"cellVerdict(DATA.cells[{i}])" for i in range(len(datos["cells"]))]
    js = run_js(_bloque(NOMBRES[0]), prelude, exprs, tmp_path)
    assert len(js) == len(celdas) and celdas
    for celda, js_verdict in zip(celdas, js):
        persistido = celda["verdict"]["s1"]
        assert js_verdict == persistido, celda["model"] + "/" + celda["workload"]
