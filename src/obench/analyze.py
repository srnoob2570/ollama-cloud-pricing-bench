"""`bench analyze` — the re-run without re-measuring (methodology v1 §10).

Pure post-hoc analysis over the immutable raw datasets (runs/*.jsonl +
batches/*.jsonl), the versioned price table and the analysis parameters
(--table-version, --ancla, --s). It never touches the API: a price change
re-derives the whole bundle with zero quota spent.

The bundle (written to `analysis/`; a custom S(x) — a stamped re-run — writes
its own `analysis-s<x>/` set and never edits the persisted s0/s1 reference):

- `analysis.json` — the full doc: per (model, workload) derivatives (median,
  p25-p75, p95 of the measured pp/1M and of the per-task costs, pass rate),
  the new-plan extrapolation under S0/S1 with the anchor, the critical
  threshold pp/1M, the who-wins-by-user-profile table, the dp-vs-tokens
  curve data and the 4 fixed sensitivity sweeps;
- `dashboard.html` — the dashboard page of the Pages bundle (the data rides
  inside the file as JSON; the styling loads from the CDN because GitHub Pages
  serves online): the recommendation band leads, every
  measured verdict carries its margin, and the charts are theme-aware SVG
  (system/light/dark tokens, the validated legacy-blue / new-orange palette).
  The cells table prices each cell nominally for a user-chosen token scenario
  (default 1M in / 250k out): the new column prices the scenario exactly on the
  official table (input / cached input / output at their own rates, following
  the S(x) slider), the legacy column applies the cell's measured pp/1M to the
  scenario's in+out total at the weekly anchor (a sub-tick weekly read falls
  back to the session-derived equivalent, marked) — the legacy meter emits one
  pp read per bracket with no in/out decomposition, so no billing model is
  invented; the meter-native pp/1M and the per-task prices ride in the
  tooltips, and the verdict chips stay measured.
  The v1.2 cache slider is presentation-layer only: from the embedded
  per-cell tokens + rates + anchor it recomputes new-plan costs and the
  verdict margins in live JS — nothing persisted
  changes, measured hit rates keep precedence (visibly marked), and models
  without a published discount are noted as unmoved (S(x) ≡ S0). No
  matplotlib PNGs: the charts are SVG and leave the bundle with them.

Cost model (methodology v1 §3, CONTEXT.md "critical threshold"):

- legacy side: Δpp(weekly) of the bracketed batch x (P_LEGADO / 4.345 / 100)
  USD per pp, per attempted (accepted) and per completed (checker-passed) task;
  the session window rides per bracket as the secondary signal at the derived
  session $/pp (weekly / R, R ≈ 6.22) — unanchored, never a second anchor;
- new side: measured median tokens x the versioned table rates, under S0=0 %
  and S1=`--s` (or the measured hit rate where the calibration was conclusive);
- threshold pp/1M = the cell's own measured token mix priced on the table
  ($/1M) / USD-per-pp — the pp/1M above which legacy becomes more expensive
  for that cell. It is computed ONLY from the cell's own measurement: an
  unmeasured cell gets no threshold and never borrows one from another model;
- verdict: `{winner, margin_pct}` — legacy/new when one system is cheaper by a
  real margin (>2 meter ticks OR >5 % of the cheaper cost, the methodology's
  uncertainty rule), tie inside it, "no data" whenever either side is
  unmeasurable; margin_pct = (loser − winner) / winner, unbounded (it exceeds
  100 % when the loser costs more than twice the winner). Allocated readings
  (pooled brackets) carry the allocated marker and are never verdicted.

The derivatives' baseline is the k=1 (serial) cell, as methodology v1 §6
fixes; k>1 cells feed the k-axis sweep (the concurrency workstream's cells)
and the dp-tokens curve. The calibration replays and the probe volleys are
workstream evidence, never per-workload cells.
"""

from __future__ import annotations

import dataclasses
import html
import json
import pathlib
import statistics
import time

from . import calibration as calibration_mod
from . import workloads as workloads_mod
from .client import PROTOCOL_VERSION
from .concurrency import ANCHOR_WORKLOAD as K_WORKLOAD  # the k-cells' workload
from .cost import new_task_cost  # the cost model's single pricing formula
from .meter import (  # the meter's resolution and the anchor bridge
    DEFAULT_CREDIT_RATIO,
    SESSION_R,
    TICK_BAND,
    TICK_PP,
    session_usd_per_pp,
    usd_per_pp,
)
from .pricing import TableError
from .schema import read_dataset

CACHE_SWEEP_S = (0.0, 0.25, 0.5, 0.9)  # the fixed cache sweep (S0 included)
# The methodology version this harness implements: the versioned S1 default is
# declared here (v1.2, unchanged in v1.3), and every analysis set's header
# carries the version so a default change never ambiguates old artifacts (#46).
METHODOLOGY_VERSION = "v1.3"
# The persisted S0/S1 pair's S1: the versioned default hit rate (declared since
# methodology v1.2), carried by the methodology and mirrored by analyze's --s
# default.
# A custom S(x) never re-anchors the locked estimates or the comparative MAPE
# (predict reads this constant, not a flag); it enters only through analyze's
# stamped re-runs.
S1_DEFAULT = 0.5
RATE_FACTORS = (0.8, 1.2)  # the fixed rates sweep (+/-20 %)
ANCLA_FACTORS = (0.7, 1.0, 1.3)  # the fixed P_LEGADO sweep (+/-30 %)
# The sensitivity sweeps move ONE cost-model axis at a time over the S0 floor
# scenario; the cache sweep covers the S axis itself.
SWEEP_SCENARIO = "s0"
# The session window's secondary $/pp is DERIVED, never an independent anchor
# (methodology v1 §3): session $/pp = weekly $/pp / R, R the session:weekly tick
# ratio in meter.SESSION_R (live-verified 6.22, expected range 5-7) — the weekly
# window remains the study's unit of account and its only anchor.
SESSION_CAVEAT = (
    "session figures are the derived secondary signal, unanchored and never an "
    "independent anchor: session $/pp = weekly $/pp / R (R = session:weekly "
    f"ticks, live verification {SESSION_R:g}, expected range 5-7); the weekly "
    "window remains the study's unit of account and its only anchor"
)


TASK_WORKLOADS = frozenset(w.name for ws in workloads_mod.WORKLOADS_BY_LEVEL.values() for w in ws)
# The curve's allow-list: the real workloads' cells plus the concurrency
# workstream's k-cells. Anything else (probe volleys, calibration replays,
# a future pseudo-workload) is workstream evidence, never a measured cell.
CURVE_WORKLOADS = TASK_WORKLOADS | {K_WORKLOAD}


class AnalyzeError(Exception):
    """The analysis could not run (no raw dataset to derive from)."""


def _es_numero(valor) -> bool:
    """A real number (bools are never numbers in the cost model)."""
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def _cuantiles(values: list) -> dict | None:
    """median / p25 / p75 / p95, at full float precision (methodology v1.1 §4:
    zero rounding in anything persisted); None with nothing to measure. A
    single observation collapses to the value itself (quantiles need 2+ points)."""
    limpios = [float(v) for v in values if _es_numero(v)]
    if not limpios:
        return None
    if len(limpios) == 1:
        v = limpios[0]
        return {"median": v, "p25": v, "p75": v, "p95": v}
    cuartiles = statistics.quantiles(limpios, n=4)
    return {
        "median": statistics.median(limpios),
        "p25": cuartiles[0],
        "p75": cuartiles[2],
        "p95": statistics.quantiles(limpios, n=20)[18],
    }


def verdict_of(
    legacy: float | None,
    nuevo: float | None,
    tick_usd: float,
    legacy_session: float | None = None,
    credit_ratio: float = DEFAULT_CREDIT_RATIO,
) -> dict:
    """Who wins this cell, and by how much: `{winner, margin_pct}`, where
    margin_pct = (loser − winner) / winner — how much more expensive the loser
    is, as a percentage of the winner's cost (the methodology's verdict
    margin). Unbounded above: it exceeds 100 % whenever the loser costs more
    than twice the winner.

    Both sides are compared in PAID dollars (methodology v1.3): the new plan
    sells credits at a per-tier multiplier (Max $100 → $300 credits), so the
    new side's nominal credit cost is re-denominated by `credit_ratio`
    (credits of face value per paid dollar; 3.0 for the study's Max-tier
    anchor, 1.0 keeps the legacy 1:1 comparison). The legacy side is already
    paid dollars — the anchor amortizes the plan's monthly price — and the
    session fallback prices the LEGACY winner, so neither moves.

    A sub-tick winner is not a free winner: when the winner's weekly reading
    is 0.0 the weekly meter cannot resolve it (the cost tends to zero per task
    but accumulates), so the margin prices the winner with its session-derived
    weekly-equivalent — under the R mapping (session $/pp = weekly $/pp / R)
    the session dollar figure IS that estimate — and inherits the mapping's
    uncertainty (R verified 6.22, expected 5-7). With no session reading
    either, the margin falls to the loser's own cost: a free winner saves the
    whole bill, exactly 100 %.

    A real margin is the methodology's uncertainty rule in dollars — the gap
    must exceed 2 ticks of the meter OR 5 % of the cheaper cost — so the tie
    band is the MINIMUM of the two: the binding (smaller) threshold decides.
    Inside it the verdict is a tie with no margin (margin_pct null); "no data"
    when either side is unmeasurable. An allocated reading never reaches this
    function: verdicts require a directly measured legacy reading.
    """
    if legacy is None or nuevo is None:
        return {"winner": "no data", "margin_pct": None}
    nuevo = nuevo / credit_ratio
    margen = min(2 * tick_usd, 0.05 * min(legacy, nuevo))
    if abs(legacy - nuevo) <= margen:
        return {"winner": "tie", "margin_pct": None}
    if legacy < nuevo:
        ganador = legacy or legacy_session or nuevo
        return {"winner": "legacy", "margin_pct": (nuevo - legacy) / ganador * 100}
    ganador = nuevo or legacy
    return {"winner": "new", "margin_pct": (legacy - nuevo) / ganador * 100}


def load_calibrations(runs_dir: pathlib.Path) -> dict:
    """Merged cache-calibration readings (runs/calibration-*.json, later files
    winning per model). Malformed docs are skipped, never fatal: analyze must
    be able to run offline on whatever evidence exists."""
    lecturas: dict = {}
    for ruta in sorted(pathlib.Path(runs_dir).glob("calibration-*.json")):
        try:
            doc = json.loads(ruta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("readings"), dict):
            continue
        for modelo, lectura in doc["readings"].items():
            if isinstance(lectura, dict):
                lecturas[modelo] = lectura
    return {"readings": lecturas}


def _tokens_de(lineas: list[dict]) -> tuple[int | None, int | None]:
    """(tokens_in, tokens_out) summed over the lines that report both counts."""
    t_in = t_out = 0
    visto = False
    for r in lineas:
        tin, tout = r.get("tok_in"), r.get("tok_out")
        if _es_numero(tin) and _es_numero(tout):
            t_in += int(tin)
            t_out += int(tout)
            visto = True
    return (t_in, t_out) if visto else (None, None)


def _por_batch(requests: list[dict]) -> dict:
    """The request lines grouped by the batch that billed them."""
    por_batch: dict = {}
    for r in requests:
        if isinstance(r.get("batch_id"), str):
            por_batch.setdefault(r["batch_id"], []).append(r)
    return por_batch


def _rep_row(batch: dict, lineas: list[dict], usd: float, session_usd: float) -> dict:
    """One bracketed batch's contribution: its measured rep of the cell.

    Both windows ship per bracket (methodology v1 §4): dpp_weekly is the
    primary (the anchor); dpp_session rides as the secondary signal, priced
    with the derived session $/pp and carrying the doc-level unanchored caveat.

    Batch lines carry no `rep` field (the schema never declared one); the
    repetition comes from the batch's own request lines, where it is required.
    """
    rep = lineas[0].get("rep") if lineas else None
    tin, tout = _tokens_de(lineas)
    tokens = tin + tout if tin is not None else None
    intentadas = sum(1 for r in lineas if r.get("http") == 200)
    completadas = sum(1 for r in lineas if r.get("checker") == "pass")
    dpp = batch.get("dpp_weekly")
    dpp_s = batch.get("dpp_session")
    medible = _es_numero(dpp) and intentadas > 0
    medible_s = medible and _es_numero(dpp_s)
    pp = dpp * 1e6 / tokens if medible and tokens else None
    return {
        "rep": rep if isinstance(rep, int) else None,
        "batch_id": batch.get("batch_id"),
        # The settle's exit rides with the row: a "capped" bracket's read
        # predates part of its own spend, so the derivative carries the marker
        # that lets a consumer exclude it (the raw line keeps it too).
        "settle_exit": batch.get("settle_exit"),
        "attempted": intentadas,
        "completed": completadas,
        "tokens_in": tin,
        "tokens_out": tout,
        "tokens_total": tokens,
        "dpp_weekly": dpp if _es_numero(dpp) else None,
        "dpp_session": dpp_s if _es_numero(dpp_s) else None,
        "pp_per_1m": pp,
        "cost_task_attempted_usd": dpp * usd / intentadas if medible else None,
        "cost_task_completed_usd": (dpp * usd / completadas if medible and completadas else None),
        "cost_task_attempted_usd_session": (
            dpp_s * session_usd / intentadas if medible_s else None
        ),
        "cost_task_completed_usd_session": (
            dpp_s * session_usd / completadas if medible_s and completadas else None
        ),
    }


def _bracket_medible(batch: dict) -> bool:
    """The shared bracket-eligibility guards: the model must be a plain string,
    the bracket must not be aborted (its burst broke its own contract — a
    dropped request inflates pp_per_1m, a dead meter read prices nothing), and
    it must not be a zero-movement bracket. A settle that reports "stable" (or
    burns to "capped") while BOTH windows read exactly the pre-burst value is
    the meter's registration lag masquerading as stability — two stale reads
    agree at the pre-burst plateau (the documented 60–90 s lag) and the bracket
    closes with dpp 0 as if measured. Every planned bracket's token budget
    predicts readable Δpp (>= 3.5 ticks, the #28 planning floor), so zero
    movement in both windows is a failed registration, never a measurement."""
    if not isinstance(batch.get("model"), str):
        return False
    if isinstance(batch.get("notes"), str) and batch["notes"].startswith("aborted"):
        return False
    if _es_numero(batch.get("dpp_session")) and _es_numero(batch.get("dpp_weekly")):
        if batch["dpp_session"] == 0.0 and batch["dpp_weekly"] == 0.0:
            return False
    return True


def _cells(batches: list[dict], requests: list[dict], usd: float, session_usd: float) -> dict:
    """The k=1 cells grouped by (model, workload): per-rep rows plus the raw
    lines that back them (tokens, checker verdicts). Pooled brackets carry no
    single workload, so they never enter a cell — their legacy attribution is
    the allocation section's, never a measured cell's. An aborted bracket
    neither: a zero-movement one does not either — see _bracket_medible."""
    por_batch = _por_batch(requests)
    celdas: dict = {}
    for batch in batches:
        if batch.get("k") != 1 or batch.get("workload") not in TASK_WORKLOADS:
            continue
        if not isinstance(batch.get("batch_id"), str) or not _bracket_medible(batch):
            continue
        clave = (batch["model"], batch["workload"])
        celda = celdas.setdefault(clave, {"level": batch.get("level"), "reps": [], "lineas": []})
        celda["reps"].append(
            _rep_row(batch, por_batch.get(batch["batch_id"], []), usd, session_usd)
        )
        celda["lineas"].extend(por_batch.get(batch["batch_id"], []))
    return celdas


def _cell_doc(
    model: str,
    workload: str,
    celda: dict,
    *,
    tabla,
    usd: float,
    tick_usd: float,
    s_efectivo,
    credit_ratio: float = DEFAULT_CREDIT_RATIO,
) -> dict:
    """One (model, workload) derivative: distributions, extrapolation, verdict."""
    reps = sorted(celda["reps"], key=lambda r: r["rep"] if isinstance(r["rep"], int) else 0)
    lineas = celda["lineas"]
    intentadas = sum(r["attempted"] for r in reps)
    completadas = sum(r["completed"] for r in reps)

    def cuant(campo: str) -> dict | None:
        return _cuantiles([r[campo] for r in reps if r[campo] is not None])

    tins = [r["tok_in"] for r in lineas if _es_numero(r.get("tok_in"))]
    touts = [r["tok_out"] for r in lineas if _es_numero(r.get("tok_out"))]
    tin_med = statistics.median(tins) if tins else None
    tout_med = statistics.median(touts) if touts else None

    s0 = s1 = None
    try:
        tarifa = tabla.rate(model)
    except TableError:
        tarifa = None  # raw from a model the chosen table no longer prices
    if (
        tarifa is not None
        and tin_med is not None
        and tout_med is not None
        and tin_med + tout_med > 0
    ):
        s0 = new_task_cost(tin_med, tout_med, tarifa, s=0.0, per=tabla.per)
        s1 = new_task_cost(tin_med, tout_med, tarifa, s=s_efectivo.s, per=tabla.per)

    # The threshold prices the cell's OWN measured mix on the new table and
    # bridges it to the meter's unit: an unmeasured cell gets none, and none
    # is ever borrowed from another model's measurement. The comparison is in
    # paid dollars (methodology v1.3): the new side's credits divide by
    # credit_ratio, so the threshold falls by the ratio — the legacy quota
    # tolerates credit_ratio times FEWER pp/1M before the new plan (3x cheaper
    # in paid dollars at the Max tier) undercuts it.
    tokens_medios = tin_med + tout_med if tin_med is not None and tout_med is not None else None

    legacy_cuantiles = cuant("cost_task_attempted_usd")
    legacy_med = legacy_cuantiles["median"] if legacy_cuantiles else None
    legacy_s_cuantiles = cuant("cost_task_attempted_usd_session")
    legacy_s_med = legacy_s_cuantiles["median"] if legacy_s_cuantiles else None
    # The threshold exists only for a MEASURED cell: without a readable bracket
    # there is no comparison to draw, and no line is invented for the bars.
    umbral = None
    if s0 is not None and tokens_medios and legacy_med is not None:
        umbral = {
            "s0": s0 / (tokens_medios / 1e6) / (usd * credit_ratio),
            "s1": (s1 / (tokens_medios / 1e6) / (usd * credit_ratio) if s1 is not None else None),
        }
    return {
        "model": model,
        "workload": workload,
        "level": celda["level"],
        "reps": reps,
        "attempted": intentadas,
        "completed": completadas,
        "pass_rate": completadas / intentadas if intentadas else None,
        "tok_in_median": tin_med,
        "tok_out_median": tout_med,
        "pp_per_1m": cuant("pp_per_1m"),
        "legacy_cost_task_usd": legacy_cuantiles,
        "legacy_cost_completed_usd": cuant("cost_task_completed_usd"),
        # the session window rides as the secondary signal: the same per-task
        # math on the bracket's dpp_session, priced at the DERIVED session
        # $/pp — the doc-level unanchored caveat applies to every figure here.
        # Its median also backs the verdict margin when the weekly winner
        # reads 0.0 (sub-tick): see verdict_of.
        "legacy_cost_task_usd_session": legacy_s_cuantiles,
        "legacy_cost_completed_usd_session": cuant("cost_task_completed_usd_session"),
        "new_cost_task_s0_usd": s0,
        "new_cost_task_s1_usd": s1,
        "s_effective": {"s": s_efectivo.s, "source": s_efectivo.source},
        "threshold_pp_per_1m": umbral,
        "verdict": {
            "s0": verdict_of(
                legacy_med, s0, tick_usd, legacy_session=legacy_s_med, credit_ratio=credit_ratio
            ),
            "s1": verdict_of(
                legacy_med, s1, tick_usd, legacy_session=legacy_s_med, credit_ratio=credit_ratio
            ),
        },
    }


def _who_wins(celdas: list[dict]) -> list[dict]:
    """The who-wins-by-user-profile table: one row per workload (the user's
    profile is the workload they run), one column set per cache scenario."""
    por_workload: dict = {}
    for c in celdas:
        por_workload.setdefault(c["workload"], []).append(c)
    filas = []
    for workload in sorted(por_workload):
        grupo = por_workload[workload]
        fila = {"workload": workload, "level": grupo[0]["level"]}
        for escenario in ("s0", "s1"):
            conteo = {"legacy": 0, "new": 0, "tie": 0, "unmeasured": 0}
            for c in grupo:
                v = c["verdict"][escenario]["winner"]
                # only measured verdicts count as wins: ties count as ties and
                # an allocated reading never reaches a verdict at all
                conteo["unmeasured" if v == "no data" else v] += 1
            fila[escenario] = conteo
        filas.append(fila)
    return filas


def _curva_dp_tokens(batches: list[dict], requests: list[dict]) -> list[dict]:
    """The dp-vs-tokens points: one per bracketed batch of a KNOWN workload
    with a readable bracket and a readable token total (any k — the k cells
    are part of the story the curve tells; probe volleys, calibration replays
    and any unknown workload are workstream evidence, never curve points).
    The same rules as the cells apply to a bracket that cannot measure: an
    aborted bracket (its burst broke its own contract) and a zero-movement
    one (stale reads at the pre-burst plateau) never enter the curve — see
    _bracket_medible."""
    por_batch = _por_batch(requests)
    puntos = []
    for batch in batches:
        workload = batch.get("workload")
        dpp = batch.get("dpp_weekly")
        if workload not in CURVE_WORKLOADS or not _es_numero(dpp):
            continue
        if not _bracket_medible(batch):
            continue
        tin, tout = _tokens_de(por_batch.get(batch.get("batch_id"), []))
        if tin is None:
            continue
        dpp_s = batch.get("dpp_session")
        puntos.append(
            {
                "model": batch["model"],
                "workload": workload,
                "level": batch.get("level"),
                "k": batch.get("k"),
                "rep": batch.get("rep"),
                "batch_id": batch.get("batch_id"),
                "tokens_total": tin + tout,
                "dpp_weekly": dpp,
                "dpp_session": dpp_s if _es_numero(dpp_s) else None,
            }
        )
    return sorted(puntos, key=lambda p: (p["workload"] or "", p["model"] or "", p["tokens_total"]))


def _veredicto_comparado(
    c: dict, legado: float, nuevo: float, tick_usd: float, credit_ratio: float
) -> dict:
    """The sweep's verdict: the same verdict_of call every sweep makes, with
    the legacy session median backing the margin when the weekly winner reads
    0.0 (sub-tick). The caller decides which side moves (legado/nuevo) and
    where the tick lands."""
    return verdict_of(
        legado,
        nuevo,
        tick_usd,
        legacy_session=(
            c["legacy_cost_task_usd_session"]["median"]
            if c.get("legacy_cost_task_usd_session")
            else None
        ),
        credit_ratio=credit_ratio,
    )


def _sweep(
    celdas: list[dict],
    valores: tuple,
    eje: str,
    por_celda,
    note: str,
) -> dict:
    """The shared sweep driver: one row (and verdict) per cell per value,
    flips read against the baseline verdict, cells/flips keyed by the value's
    `:g` string. `por_celda(c, valor)` returns `(fila, veredicto)` or None to
    skip the cell at this value (unmeasured cell, unpriced model)."""
    barrido = {eje: list(valores), "cells": {}, "flips": {}}
    for valor in valores:
        clave = f"{valor:g}"
        filas, vueltas = [], []
        for c in celdas:
            resultado = por_celda(c, valor)
            if resultado is None:
                continue
            fila, veredicto = resultado
            filas.append(fila)
            if veredicto["winner"] != c["verdict"][SWEEP_SCENARIO]["winner"]:
                vueltas.append(
                    {"model": c["model"], "workload": c["workload"], "verdict": veredicto}
                )
        barrido["cells"][clave] = filas
        barrido["flips"][clave] = vueltas
    barrido["note"] = note
    return barrido


def _sweep_rates(
    celdas: list[dict], tick_usd: float, credit_ratio: float = DEFAULT_CREDIT_RATIO
) -> dict:
    """Rates +/-20 %: the new-plan side scales with the table, the legacy side
    is meter-native and cannot move. Flips read against the S0 verdict."""

    def por_celda(c: dict, factor: float):
        if c["legacy_cost_task_usd"] is None or c["new_cost_task_s0_usd"] is None:
            return None
        nuevo = c["new_cost_task_s0_usd"] * factor
        veredicto = _veredicto_comparado(
            c, c["legacy_cost_task_usd"]["median"], nuevo, tick_usd, credit_ratio
        )
        return (
            {
                "model": c["model"],
                "workload": c["workload"],
                "new_cost_task_usd": nuevo,
                "threshold_pp_per_1m": (
                    c["threshold_pp_per_1m"]["s0"] * factor if c["threshold_pp_per_1m"] else None
                ),
                "verdict": veredicto,
            },
            veredicto,
        )

    return _sweep(
        celdas,
        RATE_FACTORS,
        "factors",
        por_celda,
        "every table rate scaled by the factor; the legacy side is meter-native "
        "and cannot move. Flips are read against the S0 baseline verdict.",
    )


def _sweep_cache(
    celdas: list[dict], tabla, tick_usd: float, credit_ratio: float = DEFAULT_CREDIT_RATIO
) -> dict:
    """Cache hit-rate in {0, 25, 50, 90} %: only models the table discounts move."""

    def por_celda(c: dict, s: float):
        if (
            c["legacy_cost_task_usd"] is None
            or c["tok_in_median"] is None
            or c["tok_out_median"] is None
        ):
            return None
        try:
            tarifa = tabla.rate(c["model"])
        except TableError:
            return None
        nuevo = new_task_cost(c["tok_in_median"], c["tok_out_median"], tarifa, s=s, per=tabla.per)
        veredicto = _veredicto_comparado(
            c, c["legacy_cost_task_usd"]["median"], nuevo, tick_usd, credit_ratio
        )
        return (
            {
                "model": c["model"],
                "workload": c["workload"],
                "new_cost_task_usd": nuevo,
                "verdict": veredicto,
            },
            veredicto,
        )

    return _sweep(
        celdas,
        CACHE_SWEEP_S,
        "s_values",
        por_celda,
        "the hit rate applied uniformly to every model the table discounts "
        "(the baseline cells use each model's effective S: measured where the "
        "calibration was conclusive, assumed otherwise). Flips read against S0.",
    )


def _sweep_ancla(
    celdas: list[dict], tick_usd: float, credit_ratio: float = DEFAULT_CREDIT_RATIO
) -> dict:
    """P_LEGADO +/-30 %: every legacy dollar moves with the anchor, measured
    pp/1M cannot. Flips read against the baseline verdict."""

    def por_celda(c: dict, factor: float):
        if c["legacy_cost_task_usd"] is None or c["new_cost_task_s0_usd"] is None:
            return None
        legado = c["legacy_cost_task_usd"]["median"] * factor
        veredicto = _veredicto_comparado(
            c, legado, c["new_cost_task_s0_usd"], tick_usd * factor, credit_ratio
        )
        return (
            {
                "model": c["model"],
                "workload": c["workload"],
                "legacy_cost_task_usd": legado,
                "pp_per_1m": c["pp_per_1m"]["median"] if c["pp_per_1m"] else None,
                # the threshold rides with the anchor: it divides by
                # USD/pp (× credit_ratio), so a stronger anchor lowers it
                "threshold_pp_per_1m": (
                    c["threshold_pp_per_1m"]["s0"] / factor if c["threshold_pp_per_1m"] else None
                ),
                "verdict": veredicto,
            },
            veredicto,
        )

    return _sweep(
        celdas,
        ANCLA_FACTORS,
        "factors",
        por_celda,
        "the anchor (P_LEGADO, USD/month) scaled by the factor: every legacy "
        "dollar and the pp/1M threshold move with it; the measured pp/1M is "
        "meter-native and cannot move. Flips read against the baseline verdict.",
    )


def _sweep_k(batches: list[dict], requests: list[dict], usd: float, session_usd: float) -> dict:
    """The k axis: the concurrency workstream's cells — effective cost per task
    under k, with the methodology's coded outcome (invariant -> squeeze,
    growing -> overhead; wall_clock_s carries the serialized reading). The
    per-bracket math is `_rep_row`'s — the same metric the cells and the
    workstream summary carry."""
    por_batch = _por_batch(requests)
    por_modelo: dict = {}
    for batch in batches:
        if batch.get("workload") != K_WORKLOAD or not isinstance(batch.get("model"), str):
            continue
        if not isinstance(batch.get("k"), int):
            continue
        fila = _rep_row(batch, por_batch.get(batch.get("batch_id"), []), usd, session_usd)
        if fila["cost_task_attempted_usd"] is None:
            continue
        entrada = {
            "k": batch["k"],
            "batch_id": fila["batch_id"],
            "attempted": fila["attempted"],
            "completed": fila["completed"],
            "dpp_weekly": fila["dpp_weekly"],
            "cost_task_attempted_usd": fila["cost_task_attempted_usd"],
            "cost_task_completed_usd": fila["cost_task_completed_usd"],
            "wall_clock_s": batch.get("wall_clock_s"),
        }
        por_modelo.setdefault(batch["model"], {}).setdefault(batch["k"], []).append(entrada)

    filas = []
    for modelo, por_k in sorted(por_modelo.items()):
        celdas = []
        for _k, entradas in sorted(por_k.items()):
            if len(entradas) == 1:
                celdas.append(entradas[0])
            else:  # several batches of the same (model, k): the median one speaks
                mediana = statistics.median(e["cost_task_attempted_usd"] for e in entradas)
                celdas.append(
                    min(entradas, key=lambda e: abs(e["cost_task_attempted_usd"] - mediana))
                )
        celdas.sort(key=lambda c: c["k"])
        veredicto = None
        if len(celdas) >= 2:
            costes = [c["cost_task_attempted_usd"] for c in celdas]
            # One tick of resolution, read through the residue band: a spread of
            # exactly one tick (which unrounded arithmetic lands a few 1e-18
            # either side of the band) is the meter's quantum, deterministically
            # squeeze — never a coin flip on the payloads' last bits.
            if max(costes) - min(costes) <= usd * TICK_PP * (1 + TICK_BAND):
                veredicto = "squeeze"
            elif costes == sorted(costes) and costes[0] < costes[-1]:
                veredicto = "overhead"
            else:
                veredicto = "mixed"
        filas.append({"model": modelo, "cells": celdas, "verdict": veredicto})
    return {
        "models": filas,
        "note": (
            "effective cost per task = dpp_weekly x USD/pp / tasks, per attempted "
            "and per completed task (the concurrency workstream's metric, "
            "recomputed here from raw with the current anchor). Outcome codes: "
            "invariant cost -> squeeze, growing -> overhead, anything else -> mixed."
        ),
    }


# ---------------------------------------------------------------------------
# the pooled brackets' post-hoc allocation (methodology v1.1 §5)
# ---------------------------------------------------------------------------


def allocate_pooled(batch: dict, lineas: list[dict]) -> dict[str, dict] | None:
    """One pooled bracket's per-workload legacy attribution, post-hoc by token
    share: the pool's measured Δpp × the workload's share of the pool's request
    tokens. No weight is stored anywhere — the shares derive here, at analysis
    time, from the request lines' own token counts, so any re-run re-derives
    them and a meter or fixture change can never desync a stored weight from
    the raw evidence (there is none). An allocated reading is marked allocated
    and never verdicted (the glossary's rule); the verdict-level consumption is
    the verdict-margin work's.

    Null-safe: None when the line is not a pooled bracket, when no request
    reports both token counts, or when the pool total is zero. A workload with
    no readable token reports takes no share (the denominator is the reported
    tokens only) and its Δpp allocation is None, never a measured-looking 0.0:
    an unattributable reading is no data, not a free one. The allocation keeps
    the exact floats — the precision policy rounds nothing that is persisted.
    """
    pool = batch.get("pool")
    if not isinstance(pool, dict) or not isinstance(pool.get("workloads"), list):
        return None
    tokens_de: dict[str, int] = {}
    for r in lineas:
        workload = r.get("workload")
        tin, tout = r.get("tok_in"), r.get("tok_out")
        if workload in pool["workloads"] and _es_numero(tin) and _es_numero(tout):
            tokens_de[workload] = tokens_de.get(workload, 0) + int(tin) + int(tout)
    total = sum(tokens_de.values())
    if total <= 0:
        return None
    dpp_s, dpp_w = batch.get("dpp_session"), batch.get("dpp_weekly")
    asignacion: dict[str, dict] = {}
    for workload in pool["workloads"]:
        tokens = tokens_de.get(workload, 0)
        # no readable token report -> the workload's slice is unattributable:
        # its Δpp stays None (no data), never dpp x 0.0 in a $0.00 disguise
        atribuible = tokens > 0
        asignacion[workload] = {
            "tokens_total": tokens,
            "share": tokens / total,
            "dpp_session": dpp_s * tokens / total if _es_numero(dpp_s) and atribuible else None,
            "dpp_weekly": dpp_w * tokens / total if _es_numero(dpp_w) and atribuible else None,
        }
    return asignacion


def _allocated_costs(
    asignacion: dict[str, dict],
    lineas: list[dict],
    *,
    usd: float,
    session_usd: float,
) -> None:
    """The allocated readings' costs, in place: the workload's slice of the
    pool's measured Δpp priced at the anchor (weekly, primary) and at the
    derived session $/pp (secondary), per attempted and per completed task of
    the workload's own request lines. Every reading is marked allocated and
    carries NO verdict: verdicts require a directly measured legacy reading
    (the glossary's rule), so who-wins never sees an allocation."""
    for workload, lectura in asignacion.items():
        propias = [r for r in lineas if r.get("workload") == workload]
        intentadas = sum(1 for r in propias if r.get("http") == 200)
        completadas = sum(1 for r in propias if r.get("checker") == "pass")
        dpp_w, dpp_s = lectura["dpp_weekly"], lectura["dpp_session"]
        lectura.update(
            {
                "attempted": intentadas,
                "completed": completadas,
                "reading": "allocated",
                "verdict": None,
                "cost_task_attempted_usd": (
                    dpp_w * usd / intentadas if intentadas and dpp_w is not None else None
                ),
                "cost_task_completed_usd": (
                    dpp_w * usd / completadas if completadas and dpp_w is not None else None
                ),
                "cost_task_attempted_usd_session": (
                    dpp_s * session_usd / intentadas if intentadas and dpp_s is not None else None
                ),
                "cost_task_completed_usd_session": (
                    dpp_s * session_usd / completadas if completadas and dpp_s is not None else None
                ),
            }
        )


def _pooled_section(
    batches: list[dict], requests: list[dict], *, usd: float, session_usd: float
) -> list[dict]:
    """The pooled brackets' allocation rows: the raw bracket, its pool, and the
    per-workload allocation each of its workloads derives post-hoc — costs
    marked allocated, never verdicted."""
    por_batch = _por_batch(requests)
    filas = []
    for batch in batches:
        pool = batch.get("pool")
        if not isinstance(pool, dict):
            continue
        lineas = por_batch.get(batch.get("batch_id"), [])
        asignacion = allocate_pooled(batch, lineas)
        if asignacion is not None:
            _allocated_costs(asignacion, lineas, usd=usd, session_usd=session_usd)
        filas.append(
            {
                "batch_id": batch.get("batch_id"),
                "run_id": batch.get("run_id"),
                "level": batch.get("level"),
                "model": batch.get("model"),
                "workloads": pool.get("workloads"),
                "reps": pool.get("reps"),
                "dpp_session": batch.get("dpp_session"),
                "dpp_weekly": batch.get("dpp_weekly"),
                "allocations": asignacion,
            }
        )
    return filas


def build(
    base: pathlib.Path,
    *,
    tabla,
    ancla: float,
    s: float,
    level: str | None = None,
    model: str | None = None,
    protocol_version: str | None = None,
    cells_only: bool = False,
    credit_ratio: float = DEFAULT_CREDIT_RATIO,
) -> dict:
    """The analysis doc, computed from raw alone. Raises AnalyzeError when the
    base holds no raw dataset at all.

    `credit_ratio` re-denominates the new-plan side at every comparison point
    (verdicts, margins, pp/1M threshold) into paid dollars: the new plan sells
    credits at a per-tier multiplier (Max $100 → $300, the study's anchor tier)
    so the ratio is 3.0 by default; 1.0 keeps the legacy 1:1 credit comparison.
    The persisted per-task cost figures stay at credit face value either way.

    `protocol_version` pins the vintage the filter keeps (default: this
    harness's own). A fetched dataset release is analyzed with ITS OWN protocol
    — the raw<->code<->table pairing, consumed: a frozen v2 release stays
    analyzable as the opacity case study it is kept as, while the local path
    never mixes vintages.

    `cells_only` skips the pooled/who-wins/dp-tokens/sensitivity derivatives
    (the doc then carries the cells and nothing around them): predict's
    comparative report reads the cells alone, so it never pays for the sweeps
    it does not consume. The full doc stays the default."""
    base = pathlib.Path(base)
    vintage = protocol_version or PROTOCOL_VERSION
    runs_dir = base / "runs"
    requests = read_dataset(runs_dir, "requests-*.jsonl")
    batches = read_dataset(base / "batches", "batches-*.jsonl")
    if not requests and not batches:
        raise AnalyzeError(
            f"no raw dataset under {base} (runs/requests-*.jsonl and batches/batches-*.jsonl): "
            "nothing to analyze - run a level first"
        )
    # Every line is stamped with the protocol that wrote it (methodology v1
    # story 17): mixing vintages under one median would silently blend
    # incomparable measurements, so only the pinned vintage is analyzed and
    # whatever else the directory holds is counted, never averaged in.
    protocolo_requests = [r for r in requests if r.get("protocol_version") == vintage]
    protocolo_batches = [b for b in batches if b.get("protocol_version") == vintage]
    descartadas = (len(requests) - len(protocolo_requests)) + (
        len(batches) - len(protocolo_batches)
    )
    requests, batches = protocolo_requests, protocolo_batches
    if not requests and not batches:
        raise AnalyzeError(
            f"no raw dataset under {base} speaks protocol {vintage!r} "
            f"({descartadas} lines of other vintages were set aside): nothing to analyze"
        )
    if level is not None:
        batches = [b for b in batches if b.get("level") == level]
        ids_validos = {b.get("batch_id") for b in batches}
        requests = [r for r in requests if r.get("batch_id") in ids_validos]

    usd = usd_per_pp(ancla)
    session_usd = session_usd_per_pp(usd)
    tick_usd = usd * TICK_PP

    # The effective hit rate per model: the calibration's measured rate wins
    # over the --s assumption when it was conclusive (methodology v1 §7).
    calibracion = load_calibrations(runs_dir)
    agrupadas = _cells(batches, requests, usd, session_usd)
    modelos = sorted({modelo for modelo, _w in agrupadas if isinstance(modelo, str)})
    resueltos = calibration_mod.resolve_s(calibracion, modelos, default_s=s)

    celdas = [
        _cell_doc(
            modelo,
            workload,
            agrupadas[(modelo, workload)],
            tabla=tabla,
            usd=usd,
            tick_usd=tick_usd,
            s_efectivo=resueltos[modelo],
            credit_ratio=credit_ratio,
        )
        for (modelo, workload) in sorted(agrupadas)
        if model is None or modelo == model
    ]

    sin_materializar = sorted(
        m
        for m, a in calibracion.get("readings", {}).items()
        if isinstance(a, dict)
        and isinstance(a.get("paper_discount"), dict)
        and a["paper_discount"].get("declared")
        and a["paper_discount"].get("materialized") is False
    )
    run_ids = sorted(
        {r.get("run_id") for r in requests if isinstance(r.get("run_id"), str)}
        | {b.get("run_id") for b in batches if isinstance(b.get("run_id"), str)}
    )
    doc = {
        "kind": "analysis",
        "generated_at": time.time(),
        "protocol_version": vintage,  # the vintage the filter kept (a release's own)
        "base_params": {
            "methodology_version": METHODOLOGY_VERSION,
            "table_version": tabla.table_version,
            "ancla": ancla,
            "usd_per_pp": usd,
            "s": s,
            "credit_ratio": credit_ratio,
            "tick_pp": TICK_PP,
            "tick_usd": tick_usd,
            # the session window's derived $/pp (secondary, unanchored — the
            # caveat travels with the stamp, never alone)
            "session": {
                "ratio_r": SESSION_R,
                "usd_per_pp": session_usd,
                "caveat": SESSION_CAVEAT,
            },
        },
        "raw": {
            "run_ids": run_ids,
            "request_lines": len(requests),
            "batch_lines": len(batches),
            "lines_other_protocol": descartadas,
            "levels": sorted({b.get("level") for b in batches if isinstance(b.get("level"), str)}),
        },
        "s_per_model": {m: dataclasses.asdict(resueltos[m]) for m in modelos},
        "cells": celdas,
        "paper_discounts": sin_materializar,
        "notes": (
            "computed from the raw runs/*.jsonl + batches/*.jsonl lines alone "
            "(k=1 cells are the derivatives' baseline; k>1 cells and the "
            "calibration/probe workstreams stay out of the cells). The threshold "
            "pp/1M prices each cell's OWN measured token mix on the table: an "
            "unmeasured cell reports no data and never borrows a threshold. "
            "Verdicts, margins and the threshold compare PAID dollars: the new "
            f"plan sells credits at a per-tier multiplier, so its nominal credit "
            f"cost divides by credit_ratio={credit_ratio:g} (the anchor's Max tier: "
            "$100 -> $300 credits); the per-task cost figures stay at face value. A "
            "verdict is {winner, margin_pct}, the margin (loser - winner)/winner: "
            "a real margin is >2 meter ticks or >5 % of the cheaper cost; a "
            "sub-tick winner (weekly reads 0.0) prices with its session-derived "
            "weekly-equivalent, falling back to the loser's own cost (exactly "
            "100 %) with no session reading either; the "
            "per-rep quantiles carry the full uncertainty. Both windows ship per "
            f"bracket: weekly is the primary (the anchor); "
            f"{SESSION_CAVEAT[0].lower() + SESSION_CAVEAT[1:]}. "
            "Pooled "
            "brackets (workload null) are never cells: their per-workload legacy "
            "is allocated in 'pooled' as token shares, marked allocated and never "
            "verdicted; who-wins counts only measured verdicts."
        ),
    }
    if not cells_only:
        doc["pooled"] = _pooled_section(batches, requests, usd=usd, session_usd=session_usd)
        doc["who_wins"] = _who_wins(celdas)
        doc["dp_tokens_curve"] = _curva_dp_tokens(batches, requests)
        doc["sensitivity"] = {
            "rates": _sweep_rates(celdas, tick_usd, credit_ratio=credit_ratio),
            "cache": _sweep_cache(celdas, tabla, tick_usd, credit_ratio=credit_ratio),
            "ancla": _sweep_ancla(celdas, tick_usd, credit_ratio=credit_ratio),
            "k_axis": _sweep_k(batches, requests, usd, session_usd),
        }
    return doc


# ---------------------------------------------------------------------------
# the bundle: analysis.json and the dashboard (the charts are in-page SVG —
# the matplotlib PNGs left the bundle with dashboard v2, #41)
# ---------------------------------------------------------------------------


def _rates_payload(modelos, tabla, *, skip_unpriced: bool) -> dict:
    """The {per, rates} payload both Pages pages embed: one entry per model
    with the four rate fields the JS recomputes from. With skip_unpriced, a
    model the chosen table no longer prices takes no rate (the dashboard
    already renders it as no data and the slider cannot recompute it either);
    without it, every model passed must be priced (the calculator's matrix
    prices any model the table carries)."""
    tarifas = {}
    for modelo in sorted(modelos):
        try:
            r = tabla.rate(modelo)
        except TableError:
            if skip_unpriced:
                continue
            raise
        tarifas[modelo] = {
            "input": r.input,
            "cached_input": r.cached_input,
            "output": r.output,
            "has_cache_discount": r.has_cache_discount,
        }
    return {"per": tabla.per, "rates": tarifas}


def _rates_map(tabla, doc: dict) -> dict:
    """The per-model rates the dashboard's slider recomputes from, embedded in
    the DASHBOARD ONLY (presentation layer, #41's amendment v1.2): nothing
    persisted changes — analysis.json carries no rates, the raw is immutable,
    and derivatives regenerate only with versioned parameters. A cell's model
    the chosen table no longer prices takes no rate. The calculator page
    embeds the FULL table's rates instead (_rates_map_full)."""
    return _rates_payload({c["model"] for c in doc["cells"]}, tabla, skip_unpriced=True)


def _rates_map_full(tabla) -> dict:
    """The FULL Ollama-reported pricing table as per-model rates: every model
    the chosen table prices — measured cells or not — because the calculator's
    matrix prices any model the table carries, never only the analysis set's.
    Rides in the CALCULATOR page's JSON only (presentation layer): the
    dashboard keeps its cells-derived payload, nothing persisted changes."""
    return _rates_payload(tabla.models, tabla, skip_unpriced=False)


def bundle_dirname(doc: dict) -> str:
    """The analysis set's folder: `analysis` holds the persisted S0/S1 reference
    (S at the versioned default); any other S(x) is a stamped re-run — its own
    parameter-stamped folder (`analysis-s0.35`), its parameters in the doc's
    header (methodology version included), the reference set never touched
    (methodology v1.2, #46). The doc is the parameter record: the stamp reads
    the same `s` the analysis actually ran with, at 15 significant digits so
    two distinct S values never round into one folder and silently overwrite
    each other (the default 6-digit 'g' collided, e.g. 0.1234561 vs 0.12345649)."""
    s = doc["base_params"]["s"]
    if s == S1_DEFAULT:
        return "analysis"
    return f"analysis-s{format(float(s), '.15g')}"


def _refutar_referencia(base: pathlib.Path, doc: dict) -> None:
    """Refuses a write that would SHRINK the persisted reference bundle
    (methodology v1.2, #46).

    `analysis/` holds the full-slate reference at the versioned default S. A
    re-parameterized default-S run (a new table version, a new anchor) is the
    documented re-derivation and rewrites it in place; a FILTERED one
    (--model/--level) is a narrower doc and never the reference — writing it
    would shrink the persisted set. The one allowed overwrite is a doc whose
    cells are the reference's own or a superset (new brackets since the last
    run grow the plan)."""
    previa = base / "analysis" / "analysis.json"
    if not previa.exists():
        return
    try:
        previo = json.loads(previa.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return  # an unreadable prior bundle is the operator's to clean up, not ours to guard
    claves_previas = {
        (c.get("model"), c.get("workload")) for c in previo.get("cells", []) if isinstance(c, dict)
    }
    claves = {
        (c.get("model"), c.get("workload")) for c in doc.get("cells", []) if isinstance(c, dict)
    }
    if claves < claves_previas:
        raise AnalyzeError(
            f"analysis/ holds the persisted reference with {len(claves_previas)} cells; this "
            f"default-S run would write {len(claves)} over it (a filtered --model/--level "
            "re-run) - the reference set is never shrunk in place; pass --s <x> for a "
            "stamped re-run"
        )


def write_bundle(base: pathlib.Path, doc: dict, *, tabla) -> pathlib.Path:
    """Writes the analysis bundle under `base/analysis/` — or, when the doc's S
    differs from the versioned default, under the stamped re-run's own folder
    (`analysis-s0.35`): the persisted s0/s1 set is never edited by a custom S
    (methodology v1.2, #46), nor shrunk by a default-S re-run that filters the
    slate (--model/--level). Returns the folder. Both embedded rates payloads
    derive from `tabla` here, so the caller cannot mis-assemble the bundle:
    the dashboard gets the cells-derived rates (_rates_map) its slider's live
    recomputation needs, the calculator gets the FULL table's rates
    (_rates_map_full) its matrix prices every listed model with. Analysis.json
    carries no rates — presentation layer only (#41's amendment v1.2), nothing
    persisted changes."""
    destino = bundle_dirname(doc)
    if destino == "analysis":
        _refutar_referencia(base, doc)
    carpeta = pathlib.Path(base) / destino
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "analysis.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (carpeta / "dashboard.html").write_text(
        render_dashboard(doc, _rates_map(tabla, doc)), encoding="utf-8"
    )
    (carpeta / "calculator.html").write_text(
        render_calculator(doc, _rates_map_full(tabla)), encoding="utf-8"
    )
    return carpeta


# ---------------------------------------------------------------------------
# the two Pages pages: .replace()-filled templates, data inline as JSON,
# styling from the CDN (GitHub Pages serves online)
# ---------------------------------------------------------------------------


def _plantilla(nombre: str) -> str:
    """The named Pages template (web/<nombre>_template.html, same package): the
    editable surface for the markup, CSS and JS. The renderers only fill the
    __TOKEN__ placeholders — the HTML never lives in a Python string."""
    return (
        pathlib.Path(__file__)
        .parent.joinpath("web", f"{nombre}_template.html")
        .read_text(encoding="utf-8")
    )


def _render_page(plantilla: str, opciones: str, tarifas: dict, doc: dict) -> str:
    """The shared .replace() fill of a page's three __TOKEN__ placeholders:
    the model filter options, the rates JSON and the doc JSON — both data
    blocks escape `</` so an inline value cannot close its own script tag."""
    return (
        plantilla.replace("__OPCIONES__", opciones)
        .replace(
            "__RATES__",
            json.dumps(tarifas, ensure_ascii=False).replace("</", "<\\/"),
        )
        .replace(
            "__DATOS__",
            json.dumps(doc, ensure_ascii=False).replace("</", "<\\/"),
        )
    )


def render_dashboard(doc: dict, rates: dict) -> str:
    """The dashboard page of the Pages bundle: the analysis doc rides inside
    it as JSON (a single file, no sibling fetches), the model filter, the
    three-state theme and the cache control are plain DOM, and every value
    escapes through textContent, html.escape or the JS esc() helper. Styling
    loads from the CDN — GitHub Pages serves online, so the Pages-first
    contract ships no vendored sheet. `rates` (from `_rates_map`) rides in its
    own JSON block: the cache control's live recomputation needs them;
    analysis.json never does."""
    opciones = "".join(
        f'<option value="{html.escape(m)}">{html.escape(m)}</option>'
        for m in sorted({c["model"] for c in doc["cells"]})
    )
    return _render_page(_plantilla("dashboard"), opciones, rates, doc)


def render_calculator(doc: dict, rates: dict) -> str:
    """The calculator page of the Pages bundle, carrying the plan token-budget
    matrices that left the dashboard in the split. `rates` is the FULL
    per-model map (from `_rates_map_full`): every model the chosen table prices
    gets a row and a filter option, measured cell or not — the dashboard keeps
    its own cells-derived rates payload unchanged. The doc rides inside it as
    JSON (its base_params and per-model S drive the pricing); same rules as
    render_dashboard: a single file with no sibling fetches, CDN styling,
    every value escaped through html.escape or the JS esc() helper."""
    opciones = "".join(
        f'<option value="{html.escape(m)}">{html.escape(m)}</option>'
        for m in sorted(rates["rates"])
    )
    return _render_page(_plantilla("calculator"), opciones, rates, doc)
