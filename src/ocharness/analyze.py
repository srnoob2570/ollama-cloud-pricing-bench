"""`bench analyze` — the re-run without re-measuring (methodology v1 §10).

Pure post-hoc analysis over the immutable raw datasets (runs/*.jsonl +
batches/*.jsonl), the versioned price table and the analysis parameters
(--table-version, --ancla, --s). It never touches the API: a price change
re-derives the whole bundle with zero quota spent.

The bundle (written to `analysis/`):

- `analysis.json` — the full doc: per (model, workload) derivatives (median,
  p25-p75, p95 of the measured pp/1M and of the per-task costs, pass rate),
  the new-plan extrapolation under S0/S1 with the anchor, the critical
  threshold pp/1M, the who-wins-by-user-profile table, the dp-vs-tokens
  curve data and the 4 fixed sensitivity sweeps;
- `dashboard.html` — the static self-contained dashboard (no CDNs, no fetches:
  the data rides inside the file): the recommendation band leads, every
  measured verdict carries its margin, and the charts are theme-aware SVG
  (system/light/dark tokens, the validated legacy-blue / new-orange palette).
  The v1.2 cache slider is presentation-layer only: from the embedded
  per-cell tokens + rates + anchor it recomputes new-plan costs, the
  critical threshold and the verdict margins in live JS — nothing persisted
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
  unmeasurable; margin_pct = (loser − winner) / loser. Allocated readings
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
from .calibration import TICK_BAND, TICK_PP  # the meter's resolution, in percentage points
from .client import PROTOCOL_VERSION
from .concurrency import ANCHOR_WORKLOAD as K_WORKLOAD  # the k-cells' workload
from .concurrency import _read_jsonl, usd_per_pp
from .cost import new_task_cost  # the cost model's single pricing formula
from .pricing import TableError

CACHE_SWEEP_S = (0.0, 0.25, 0.5, 0.9)  # the fixed cache sweep (S0 included)
RATE_FACTORS = (0.8, 1.2)  # the fixed rates sweep (+/-20 %)
ANCLA_FACTORS = (0.7, 1.0, 1.3)  # the fixed P_LEGADO sweep (+/-30 %)
# The sensitivity sweeps move ONE cost-model axis at a time over the S0 floor
# scenario; the cache sweep covers the S axis itself.
SWEEP_SCENARIO = "s0"
# The session window's secondary $/pp is DERIVED, never an independent anchor
# (methodology v1 §3): session $/pp = weekly $/pp / R, R the session:weekly
# tick ratio, live-verified at 6.22 (expected range 5-7) -> ~$0.037/session pp.
SESSION_R = 6.22
SESSION_CAVEAT = (
    "session figures are the derived secondary signal, unanchored — never an "
    "independent anchor: session $/pp = weekly $/pp / R (R = session:weekly "
    f"ticks, live verification {SESSION_R:g}, expected range 5-7); the weekly "
    "window remains the study's unit of account and its only anchor"
)


def session_usd_per_pp(usd_weekly: float) -> float:
    """The session window's derived $/pp: the weekly bridge divided by R."""
    return usd_weekly / SESSION_R


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


def verdict_of(legacy: float | None, nuevo: float | None, tick_usd: float) -> dict:
    """Who wins this cell, and by how much: `{winner, margin_pct}`, where
    margin_pct = (loser − winner) / loser — the saving from picking the winner,
    as a percentage of the loser's cost (the methodology's verdict margin).

    A real margin is the methodology's uncertainty rule in dollars — the gap
    must exceed 2 ticks of the meter OR 5 % of the cheaper cost — so the tie
    band is the MINIMUM of the two: the binding (smaller) threshold decides.
    Inside it the verdict is a tie with no margin (margin_pct null); "no data"
    when either side is unmeasurable. An allocated reading never reaches this
    function: verdicts require a directly measured legacy reading.
    """
    if legacy is None or nuevo is None:
        return {"winner": "no data", "margin_pct": None}
    margen = min(2 * tick_usd, 0.05 * min(legacy, nuevo))
    if abs(legacy - nuevo) <= margen:
        return {"winner": "tie", "margin_pct": None}
    if legacy < nuevo:
        return {"winner": "legacy", "margin_pct": (nuevo - legacy) / nuevo * 100}
    return {"winner": "new", "margin_pct": (legacy - nuevo) / legacy * 100}


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
        "attempted": intentadas,
        "completed": completadas,
        "tokens_in": tin,
        "tokens_out": tout,
        "tokens_total": tokens,
        "dpp_weekly": dpp if _es_numero(dpp) else None,
        "dpp_session": dpp_s if _es_numero(dpp_s) else None,
        "pp_per_1m": pp if pp is not None else None,
        "cost_task_attempted_usd": dpp * usd / intentadas if medible else None,
        "cost_task_completed_usd": (dpp * usd / completadas if medible and completadas else None),
        "cost_task_attempted_usd_session": (
            dpp_s * session_usd / intentadas if medible_s else None
        ),
        "cost_task_completed_usd_session": (
            dpp_s * session_usd / completadas if medible_s and completadas else None
        ),
    }


def _cells(batches: list[dict], requests: list[dict], usd: float, session_usd: float) -> dict:
    """The k=1 cells grouped by (model, workload): per-rep rows plus the raw
    lines that back them (tokens, checker verdicts). Pooled brackets carry no
    single workload, so they never enter a cell — their legacy attribution is
    the allocation section's, never a measured cell's."""
    por_batch = _por_batch(requests)
    celdas: dict = {}
    for batch in batches:
        if batch.get("k") != 1 or batch.get("workload") not in TASK_WORKLOADS:
            continue
        if not isinstance(batch.get("model"), str) or not isinstance(batch.get("batch_id"), str):
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
) -> dict:
    """One (model, workload) derivative: distributions, extrapolation, verdict."""
    reps = sorted(celda["reps"], key=lambda r: r["rep"] if isinstance(r["rep"], int) else 0)
    lineas = celda["lineas"]
    intentadas = sum(r["attempted"] for r in reps)
    completadas = sum(r["completed"] for r in reps)

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
    # is ever borrowed from another model's measurement.
    tokens_medios = tin_med + tout_med if tin_med is not None and tout_med is not None else None

    legacy_cuantiles = _cuantiles(
        [r["cost_task_attempted_usd"] for r in reps if r["cost_task_attempted_usd"] is not None]
    )
    legacy_med = legacy_cuantiles["median"] if legacy_cuantiles else None
    # The threshold exists only for a MEASURED cell: without a readable bracket
    # there is no comparison to draw, and no line is invented for the bars.
    umbral = None
    if s0 is not None and tokens_medios and legacy_med is not None:
        umbral = {
            "s0": s0 / (tokens_medios / 1e6) / usd,
            "s1": s1 / (tokens_medios / 1e6) / usd if s1 is not None else None,
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
        "pp_per_1m": _cuantiles([r["pp_per_1m"] for r in reps if r["pp_per_1m"] is not None]),
        "legacy_cost_task_usd": legacy_cuantiles,
        "legacy_cost_completed_usd": _cuantiles(
            [r["cost_task_completed_usd"] for r in reps if r["cost_task_completed_usd"] is not None]
        ),
        # the session window rides as the secondary signal: the same per-task
        # math on the bracket's dpp_session, priced at the DERIVED session
        # $/pp — the doc-level unanchored caveat applies to every figure here
        "legacy_cost_task_usd_session": _cuantiles(
            [
                r["cost_task_attempted_usd_session"]
                for r in reps
                if r["cost_task_attempted_usd_session"] is not None
            ]
        ),
        "legacy_cost_completed_usd_session": _cuantiles(
            [
                r["cost_task_completed_usd_session"]
                for r in reps
                if r["cost_task_completed_usd_session"] is not None
            ]
        ),
        "new_cost_task_s0_usd": s0,
        "new_cost_task_s1_usd": s1,
        "s_effective": {"s": s_efectivo.s, "source": s_efectivo.source},
        "threshold_pp_per_1m": umbral,
        "verdict": {
            "s0": verdict_of(legacy_med, s0, tick_usd),
            "s1": verdict_of(legacy_med, s1, tick_usd),
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
    and any unknown workload are workstream evidence, never curve points)."""
    por_batch = _por_batch(requests)
    puntos = []
    for batch in batches:
        workload = batch.get("workload")
        dpp = batch.get("dpp_weekly")
        if workload not in CURVE_WORKLOADS or not _es_numero(dpp):
            continue
        if not isinstance(batch.get("model"), str):
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


def _sweep_rates(celdas: list[dict], tick_usd: float) -> dict:
    """Rates +/-20 %: the new-plan side scales with the table, the legacy side
    is meter-native and cannot move. Flips read against the S0 verdict."""
    barrido = {"factors": list(RATE_FACTORS), "cells": {}, "flips": {}}
    for factor in RATE_FACTORS:
        clave = f"{factor:g}"
        celdas_f, vueltas = [], []
        for c in celdas:
            if c["legacy_cost_task_usd"] is None or c["new_cost_task_s0_usd"] is None:
                continue
            nuevo = c["new_cost_task_s0_usd"] * factor
            veredicto = verdict_of(c["legacy_cost_task_usd"]["median"], nuevo, tick_usd)
            celdas_f.append(
                {
                    "model": c["model"],
                    "workload": c["workload"],
                    "new_cost_task_usd": nuevo,
                    "threshold_pp_per_1m": (
                        c["threshold_pp_per_1m"]["s0"] * factor
                        if c["threshold_pp_per_1m"]
                        else None
                    ),
                    "verdict": veredicto,
                }
            )
            if veredicto["winner"] != c["verdict"][SWEEP_SCENARIO]["winner"]:
                vueltas.append(
                    {"model": c["model"], "workload": c["workload"], "verdict": veredicto}
                )
        barrido["cells"][clave] = celdas_f
        barrido["flips"][clave] = vueltas
    barrido["note"] = (
        "every table rate scaled by the factor; the legacy side is meter-native "
        "and cannot move. Flips are read against the S0 baseline verdict."
    )
    return barrido


def _sweep_cache(celdas: list[dict], tabla, tick_usd: float) -> dict:
    """Cache hit-rate in {0, 25, 50, 90} %: only models the table discounts move."""
    barrido = {"s_values": list(CACHE_SWEEP_S), "cells": {}, "flips": {}}
    for s in CACHE_SWEEP_S:
        clave = f"{s:g}"
        celdas_s, vueltas = [], []
        for c in celdas:
            if (
                c["legacy_cost_task_usd"] is None
                or c["tok_in_median"] is None
                or c["tok_out_median"] is None
            ):
                continue
            try:
                tarifa = tabla.rate(c["model"])
            except TableError:
                continue
            nuevo = new_task_cost(
                c["tok_in_median"], c["tok_out_median"], tarifa, s=s, per=tabla.per
            )
            veredicto = verdict_of(c["legacy_cost_task_usd"]["median"], nuevo, tick_usd)
            celdas_s.append(
                {
                    "model": c["model"],
                    "workload": c["workload"],
                    "new_cost_task_usd": nuevo,
                    "verdict": veredicto,
                }
            )
            if veredicto["winner"] != c["verdict"][SWEEP_SCENARIO]["winner"]:
                vueltas.append(
                    {"model": c["model"], "workload": c["workload"], "verdict": veredicto}
                )
        barrido["cells"][clave] = celdas_s
        barrido["flips"][clave] = vueltas
    barrido["note"] = (
        "the hit rate applied uniformly to every model the table discounts "
        "(the baseline cells use each model's effective S: measured where the "
        "calibration was conclusive, assumed otherwise). Flips read against S0."
    )
    return barrido


def _sweep_ancla(celdas: list[dict], tick_usd: float) -> dict:
    """P_LEGADO +/-30 %: every legacy dollar moves with the anchor, measured
    pp/1M cannot. Flips read against the baseline verdict."""
    barrido = {"factors": list(ANCLA_FACTORS), "cells": {}, "flips": {}}
    for factor in ANCLA_FACTORS:
        clave = f"{factor:g}"
        celdas_f, vueltas = [], []
        for c in celdas:
            if c["legacy_cost_task_usd"] is None or c["new_cost_task_s0_usd"] is None:
                continue
            legacy = c["legacy_cost_task_usd"]["median"] * factor
            veredicto = verdict_of(legacy, c["new_cost_task_s0_usd"], tick_usd * factor)
            celdas_f.append(
                {
                    "model": c["model"],
                    "workload": c["workload"],
                    "legacy_cost_task_usd": legacy,
                    "pp_per_1m": c["pp_per_1m"]["median"] if c["pp_per_1m"] else None,
                    "verdict": veredicto,
                }
            )
            if veredicto["winner"] != c["verdict"][SWEEP_SCENARIO]["winner"]:
                vueltas.append(
                    {"model": c["model"], "workload": c["workload"], "verdict": veredicto}
                )
        barrido["cells"][clave] = celdas_f
        barrido["flips"][clave] = vueltas
    barrido["note"] = (
        "the anchor (P_LEGADO, USD/month) scaled by the factor: every legacy "
        "dollar and the pp/1M threshold move with it; the measured pp/1M is "
        "meter-native and cannot move. Flips read against the baseline verdict."
    )
    return barrido


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


def _read_dataset(directorio: pathlib.Path, patron: str) -> list[dict]:
    """Every raw line of the directory's dataset files, torn tails skipped."""
    lineas: list[dict] = []
    directorio = pathlib.Path(directorio)
    if not directorio.exists():
        return lineas
    for ruta in sorted(directorio.glob(patron)):
        lineas.extend(_read_jsonl(ruta))
    return lineas


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
) -> dict:
    """The analysis doc, computed from raw alone. Raises AnalyzeError when the
    base holds no raw dataset at all.

    `protocol_version` pins the vintage the filter keeps (default: this
    harness's own). A fetched dataset release is analyzed with ITS OWN protocol
    — the raw<->code<->table pairing, consumed: a frozen v2 release stays
    analyzable as the opacity case study it is kept as, while the local path
    never mixes vintages."""
    base = pathlib.Path(base)
    vintage = protocol_version or PROTOCOL_VERSION
    runs_dir = base / "runs"
    requests = _read_dataset(runs_dir, "requests-*.jsonl")
    batches = _read_dataset(base / "batches", "batches-*.jsonl")
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
    return {
        "kind": "analysis",
        "generated_at": time.time(),
        "protocol_version": vintage,  # the vintage the filter kept (a release's own)
        "base_params": {
            "table_version": tabla.table_version,
            "ancla": ancla,
            "usd_per_pp": usd,
            "s": s,
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
        "pooled": _pooled_section(batches, requests, usd=usd, session_usd=session_usd),
        "who_wins": _who_wins(celdas),
        "dp_tokens_curve": _curva_dp_tokens(batches, requests),
        "sensitivity": {
            "rates": _sweep_rates(celdas, tick_usd),
            "cache": _sweep_cache(celdas, tabla, tick_usd),
            "ancla": _sweep_ancla(celdas, tick_usd),
            "k_axis": _sweep_k(batches, requests, usd, session_usd),
        },
        "paper_discounts": sin_materializar,
        "notes": (
            "computed from the raw runs/*.jsonl + batches/*.jsonl lines alone "
            "(k=1 cells are the derivatives' baseline; k>1 cells and the "
            "calibration/probe workstreams stay out of the cells). The threshold "
            "pp/1M prices each cell's OWN measured token mix on the table: an "
            "unmeasured cell reports no data and never borrows a threshold. A "
            "verdict is {winner, margin_pct}, the margin (loser - winner)/loser: "
            "a real margin is >2 meter ticks or >5 % of the cheaper cost; the "
            "per-rep quantiles carry the full uncertainty. Both windows ship per "
            f"bracket: weekly is the primary (the anchor); the session is the "
            f"secondary signal, {SESSION_CAVEAT[0].lower() + SESSION_CAVEAT[1:]}. "
            "Pooled "
            "brackets (workload null) are never cells: their per-workload legacy "
            "sits in 'pooled' as token-share allocations, marked allocated and "
            "never verdicted, and who-wins counts only measured verdicts."
        ),
    }


# ---------------------------------------------------------------------------
# the bundle: analysis.json and the dashboard (the charts are in-page SVG —
# the matplotlib PNGs left the bundle with dashboard v2, #41)
# ---------------------------------------------------------------------------


def rates_map(tabla, doc: dict) -> dict:
    """The per-model rates the dashboard's slider recomputes from, embedded in
    the DASHBOARD ONLY (presentation layer, #41's amendment v1.2): nothing
    persisted changes — analysis.json carries no rates, the raw is immutable,
    and derivatives regenerate only with versioned parameters. A cell's model
    the chosen table no longer prices takes no rate: the dashboard already
    renders it as no data and the slider cannot recompute it either."""
    tarifas = {}
    for modelo in sorted({c["model"] for c in doc["cells"]}):
        try:
            r = tabla.rate(modelo)
        except TableError:
            continue
        tarifas[modelo] = {
            "input": r.input,
            "cached_input": r.cached_input,
            "output": r.output,
            "has_cache_discount": r.has_cache_discount,
        }
    return {"per": tabla.per, "rates": tarifas}


def write_bundle(
    base: pathlib.Path, doc: dict, emit=print, *, rates: dict | None = None
) -> pathlib.Path:
    """Writes the analysis bundle under `base/analysis/`; returns the folder.
    `rates` (from `rates_map`) rides only inside the dashboard: the slider's
    live recomputation needs them, analysis.json does not."""
    carpeta = pathlib.Path(base) / "analysis"
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "analysis.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (carpeta / "dashboard.html").write_text(render_dashboard(doc, rates), encoding="utf-8")
    return carpeta


# ---------------------------------------------------------------------------
# the static self-contained dashboard: one HTML file, zero external resources
# ---------------------------------------------------------------------------

_DASHBOARD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ollama Cloud cost analysis &mdash; legacy or new?</title>
<style>
/* three-state theme tokens: system is the default (no attribute), light and
   dark are selectable. Chart colors NEVER appear here outside these tokens. */
:root {
  color-scheme: light;
  --plane: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,.10);
  --legacy: #2a78d6; --new: #eb6834;            /* series: legacy / new (validated) */
  --legacy-soft: rgba(42,120,214,.14); --new-soft: rgba(235,104,52,.14);
  --tie: #898781;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --plane: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,.10);
    --legacy: #3987e5; --new: #d95926;
    --legacy-soft: rgba(57,135,229,.18); --new-soft: rgba(217,89,38,.18);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --plane: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,.10);
  --legacy: #3987e5; --new: #d95926;
  --legacy-soft: rgba(57,135,229,.18); --new-soft: rgba(217,89,38,.18);
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 20px; background: var(--plane); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 19px; margin: 0 0 2px; letter-spacing: -.01em; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-2);
  margin: 26px 0 10px; font-weight: 600; }
.sub { color: var(--muted); font-size: 12.5px; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
  border: 1px solid var(--ring); border-radius: 4px; padding: 1px 6px; color: var(--ink-2);
  background: var(--surface); margin-left: 6px; vertical-align: 1px; }
.panel { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px;
  padding: 16px 18px; margin: 0 0 14px; }
.controls { display: flex; gap: 22px; flex-wrap: wrap; align-items: center; margin: 14px 0 4px; }
select, fieldset { font: inherit; }
fieldset { border: 1px solid var(--ring); border-radius: 6px; padding: 4px 10px; margin: 0; }
legend { font-size: 11px; color: var(--muted); }
input[type="range"] { accent-color: var(--new); vertical-align: middle; width: 260px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
  text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--axis); font-weight: 600; }
td { padding: 7px 8px; border-bottom: 1px solid var(--grid); vertical-align: middle; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.chip { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; padding: 2px 9px;
  border-radius: 999px; font-weight: 600; white-space: nowrap; }
.chip .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.chip.legacy { color: var(--legacy); background: var(--legacy-soft); }
.chip.legacy .dot { background: var(--legacy); }
.chip.new { color: var(--new); background: var(--new-soft); }
.chip.new .dot { background: var(--new); }
.chip.tie { color: var(--ink-2); border: 1px dashed var(--axis); }
.chip.nodata { color: var(--muted); font-weight: 500; }
.mbar { display: inline-flex; align-items: center; gap: 7px; }
.mbar .track { position: relative; width: 84px; height: 10px; background: var(--grid);
  border-radius: 3px; overflow: hidden; }
.mbar .fill { position: absolute; top: 0; bottom: 0; border-radius: 3px; }
.mbar .fill.legacy { left: 0; background: var(--legacy); }
.mbar .fill.new { right: 0; background: var(--new); }
.mbar b { font-weight: 600; min-width: 48px; text-align: right; }
.big { font-size: 26px; font-weight: 700; letter-spacing: -.02em; margin: 4px 0 2px; }
.h-legacy { color: var(--legacy); } .h-new { color: var(--new); }
.pills { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.pill { background: var(--surface); border: 1px solid var(--ring); border-radius: 8px;
  padding: 8px 12px; font-size: 12.5px; min-width: 150px; }
.pill b { display: block; font-size: 11px; color: var(--muted); font-weight: 600;
  text-transform: uppercase; letter-spacing: .05em; margin-bottom: 3px; }
.legend { display: flex; gap: 14px; align-items: center; font-size: 12px; color: var(--ink-2);
  margin: 8px 0 0; flex-wrap: wrap; }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  margin-right: 5px; vertical-align: -1px; }
.note { font-size: 12px; color: var(--muted); }
details { border-top: 1px solid var(--grid); padding-top: 10px; margin-top: 10px; }
summary { cursor: pointer; color: var(--ink-2); font-weight: 600; font-size: 13px; }
svg { display: block; max-width: 100%; }
</style>
</head>
<body>
<h1>Ollama Cloud cost analysis &mdash; legacy or new?</h1>
<div class="sub">__RESUMEN__</div>

<section id="reco" class="panel">
  <div class="big" id="reco-headline"></div>
  <div class="sub" id="reco-sub"></div>
  <div class="pills" id="reco-pills"></div>
</section>

<div class="panel">
  <div class="controls">
    <label>model
      <select id="filter-model"><option value="">all models</option>__OPCIONES__</select>
    </label>
    <fieldset>
      <legend>theme</legend>
      <label><input type="radio" name="theme" value="system" checked> system</label>
      <label><input type="radio" name="theme" value="light"> light</label>
      <label><input type="radio" name="theme" value="dark"> dark</label>
    </fieldset>
    <span id="estado-filtro" class="note"></span>
  </div>
</div>

<div class="panel">
  <label for="slider-s"><b>Cache hit-rate S(x)</b> &mdash; presentation layer only:
    <input type="range" id="slider-s" min="0" max="100" value="50" step="1">
    <b id="slider-val" class="mono">50%</b></label>
  <p class="note" id="slider-note">The slider recomputes in live JS the new-plan costs, the critical
    threshold and the verdict margins from the embedded per-cell tokens, rates and anchor &mdash;
    nothing persisted changes (raw immutable; derivatives regenerate only with versioned parameters).
    Models with a conclusive measured hit-rate keep it (marked &laquo;measured S&raquo;) and the slider
    cannot move them; models without a published discount are unmoved: S(x) &equiv; S0 for them. A
    custom value that overrides measurements belongs to a stamped re-run (--s), never to this
    dashboard.</p>
</div>

<h2>Cells (model &times; workload)</h2>
<div class="panel" style="overflow-x: auto;">
  <table>
    <thead><tr>
      <th>model</th><th>workload</th><th>level</th>
      <th class="num">legacy $/task</th><th class="num">new $/task</th>
      <th class="num">measured pp/1M</th><th class="num">threshold pp/1M</th>
      <th class="num">pass rate</th><th>verdict</th><th>margin</th>
    </tr></thead>
    <tbody id="tabla-cuerpo"></tbody>
  </table>
  <p class="note" style="margin-top:8px">Every verdict shows its margin: the saving from picking the
    winner, (loser &minus; winner) &divide; loser. A verdict exists only when the margin clears the
    tie band (&gt;2 meter ticks or &gt;5 % of the cheaper cost); inside it the cell is a tie. The new
    $/task, the threshold and the verdicts follow the slider.</p>
</div>

<h2>Critical threshold (pp/1M): measured vs new-plan price</h2>
<div class="panel">
  <div id="chart-threshold"></div>
  <div class="legend"><span><span class="sw" style="background:var(--legacy)"></span>measured pp/1M
    (median, k=1 cells)</span>
    <span><span class="sw" style="background:var(--new);width:3px"></span>threshold at the slider's
    S(x)</span>
    <span>measured left of the threshold &rArr; legacy is cheaper</span></div>
</div>

<h2>How much do you save by picking the winner?</h2>
<div class="panel">
  <div id="chart-margins"></div>
  <div class="legend"><span><span class="sw" style="background:var(--legacy)"></span>legacy wins
    (left)</span>
    <span><span class="sw" style="background:var(--new)"></span>new wins (right)</span>
    <span><span class="sw" style="background:var(--tie);border-radius:50%"></span>tie</span>
    <span>the center axis is the tie band (&gt;2 ticks or &gt;5 %); margins follow the slider</span></div>
</div>

<details>
  <summary>Robustness: who wins by profile, sensitivity sweeps, cache calibration, legacy quota
    vs tokens</summary>
  <h2>Who wins, by user profile (persisted S0/S1 reference)</h2>
  <div class="panel">__WHO_WINS__</div>
  <h2>Sensitivity sweeps</h2>
  <div class="panel">__SENSIBILIDAD__</div>
  <h2>Cache calibration (effective S per model)</h2>
  <div class="panel">__CALIBRACION__</div>
  <h2>Legacy quota vs billed tokens</h2>
  <div class="panel"><div id="chart-dp"></div></div>
</details>

<p class="note" style="margin-top:16px">__NOTAS__</p>

<script id="analysis-data" type="application/json">__DATOS__</script>
<script id="rates-data" type="application/json">__RATES__</script>
<script>
"use strict";
var DATA = JSON.parse(document.getElementById("analysis-data").textContent);
var RATES = JSON.parse(document.getElementById("rates-data").textContent);
var BP = DATA.base_params;
var USDPP = BP.usd_per_pp;   // the anchor: USD per weekly pp
var TICKUSD = BP.tick_usd;   // one meter tick, in USD
var PER = RATES.per || 1000000;
var SLIDER_DEFAULT = Math.round((typeof BP.s === "number" ? BP.s : 0.5) * 100);
var estado = { model: "", slider: SLIDER_DEFAULT };

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function isNum(x) { return typeof x === "number" && isFinite(x); }
function isNull(x) { return x === null || x === undefined; }
function money(x) { return isNull(x) ? "no data" : "$" + Number(x).toPrecision(6); }
function pps(x) { return isNull(x) ? "no data" : Number(x).toPrecision(6) + " pp/1M"; }

function cellsFiltered() {
  return DATA.cells.filter(function (c) { return !estado.model || c.model === estado.model; });
}
function rateOf(model) { return (RATES && RATES.rates) ? RATES.rates[model] || null : null; }
function sPerModel(model) { return (DATA.s_per_model || {})[model] || null; }
function measuredS(model) {
  var spm = sPerModel(model);
  return spm && spm.source === "measured" && isNum(spm.s) ? spm.s : null;
}
function effectiveS(model) {
  // measured keeps precedence: the slider governs only the assumed models
  var m = measuredS(model);
  return m === null ? estado.slider / 100 : m;
}
function newCostAt(cell) {
  var r = rateOf(cell.model);
  if (!r || isNull(cell.tok_in_median) || isNull(cell.tok_out_median)) return null;
  var s = effectiveS(cell.model);
  if (!r.has_cache_discount) s = 0;  // no published discount: S(x) is S0, the slider cannot move it
  return (cell.tok_in_median * (1 - s) * r.input +
          cell.tok_in_median * s * r.cached_input +
          cell.tok_out_median * r.output) / PER;
}
function thresholdAt(cell) {
  // the threshold exists only for a MEASURED cell (the same rule as the
  // persisted `_cell_doc`): without a readable legacy bracket there is no
  // comparison to draw, and no line is invented for the bars
  if (!cell.legacy_cost_task_usd) return null;
  var cost = newCostAt(cell);
  var tokens = isNull(cell.tok_in_median) || isNull(cell.tok_out_median)
    ? null : cell.tok_in_median + cell.tok_out_median;
  if (!isNum(cost) || !tokens) return null;
  return cost / (tokens / 1e6) / USDPP;
}
function verdictOf(legacy, nuevo, tickUsd) {
  // the same rule as the persisted verdicts: the tie band is the MINIMUM of
  // 2 ticks and 5 % of the cheaper cost; margin_pct = (loser - winner) / loser
  if (isNull(legacy) || isNull(nuevo)) return { winner: "no data", margin_pct: null };
  var band = Math.min(2 * tickUsd, 0.05 * Math.min(legacy, nuevo));
  if (Math.abs(legacy - nuevo) <= band) return { winner: "tie", margin_pct: null };
  if (legacy < nuevo) return { winner: "legacy", margin_pct: (nuevo - legacy) / nuevo * 100 };
  return { winner: "new", margin_pct: (legacy - nuevo) / legacy * 100 };
}
function verdictAt(cell) {
  var legacy = cell.legacy_cost_task_usd ? cell.legacy_cost_task_usd.median : null;
  return verdictOf(legacy, newCostAt(cell), TICKUSD);
}

function chip(v) {
  if (!v || v.winner === "no data") return '<span class="chip nodata">no data</span>';
  if (v.winner === "tie") return '<span class="chip tie">tie</span>';
  return '<span class="chip ' + v.winner + '"><span class="dot"></span>' + v.winner +
    " &minus;" + Number(v.margin_pct).toFixed(1) + "%</span>";
}
function marginBar(v) {
  if (!v || v.winner === "no data" || v.winner === "tie") return '<span class="note">&mdash;</span>';
  return '<span class="mbar"><span class="track"><span class="fill ' + v.winner +
    '" style="width:' + Math.min(100, v.margin_pct).toFixed(2) + '%"></span></span><b class="mono">' +
    Number(v.margin_pct).toFixed(1) + "%</b></span>";
}

function medianOf(vals) {
  // statistics.median's convention: the middle value, or the mean of the two
  // middles for an even count — the same median the persisted doc uses
  if (!vals.length) return null;
  var a = vals.slice().sort(function (x, y) { return x - y; });
  var m = Math.floor(a.length / 2);
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
}
function renderReco() {
  var grupos = {};
  cellsFiltered().forEach(function (c) {
    var v = verdictAt(c);
    if (v.winner === "no data") return;
    (grupos[c.workload] = grupos[c.workload] || []).push(v);
  });
  var workloads = Object.keys(grupos).sort();
  var newWl = 0, legWl = 0, decided = 0;
  var pills = workloads.map(function (wl) {
    var xs = grupos[wl];
    var nw = 0, lw = 0;
    xs.forEach(function (v) {
      if (v.winner === "new") nw++;
      else if (v.winner === "legacy") lw++;
    });
    // the pill's side is the cells' majority; a workload whose cells all tie
    // renders the tie chip, never a fabricated winner with a fake margin
    var win = nw > lw ? "new" : lw > nw ? "legacy" : "tie";
    // the margin is the median over the WINNING side's cells only
    var side = win === "tie" ? [] : xs.filter(function (v) { return v.winner === win; })
      .map(function (v) { return v.margin_pct; }).filter(function (x) { return x !== null; });
    if (win === "new") { newWl++; decided++; }
    else if (win === "legacy") { legWl++; decided++; }
    return '<div class="pill"><b>' + esc(wl) + "</b>" +
      chip({ winner: win, margin_pct: medianOf(side) }) + "</div>";
  }).join("");
  var headline;
  if (!workloads.length) headline = "No measured verdict under this filter";
  else if (!decided) headline = "Every measured workload ties inside the band";
  else if (newWl && !legWl) headline = 'The <span class="h-new">new plan</span> wins every decided workload';
  else if (legWl && !newWl) headline = 'The <span class="h-legacy">legacy plan</span> wins every decided workload';
  else headline = 'Split: the <span class="h-legacy">legacy</span> plan wins ' + legWl + " of " +
    decided + " decided workloads";
  $("reco-headline").innerHTML = headline;
  $("reco-pills").innerHTML = pills || '<p class="note">no measured cell under this filter</p>';
  $("reco-sub").textContent =
    "The margin is the saving from picking the winner. Anchor: $" + BP.ancla + "/mo = " +
    Number(USDPP).toFixed(6) + " USD per weekly pp (tick $" + Number(TICKUSD).toFixed(6) + ").";
}

function renderTable() {
  var filas = cellsFiltered().map(function (c) {
    var v = verdictAt(c);
    var r = rateOf(c.model);
    var measuredBadge = measuredS(c.model) !== null ? ' <span class="badge">measured S</span>' : "";
    var s0Badge = r && !r.has_cache_discount ? ' <span class="badge">S(x) &equiv; S0</span>' : "";
    return '<tr><td class="mono">' + esc(c.model) + measuredBadge + "</td>" +
      "<td>" + esc(c.workload) + "</td>" +
      "<td>" + esc(isNull(c.level) ? "—" : c.level) + "</td>" +
      '<td class="num mono">' + money(c.legacy_cost_task_usd ? c.legacy_cost_task_usd.median : null) + "</td>" +
      '<td class="num mono">' + money(newCostAt(c)) + s0Badge + "</td>" +
      '<td class="num mono">' + pps(c.pp_per_1m ? c.pp_per_1m.median : null) + "</td>" +
      '<td class="num mono">' + pps(thresholdAt(c)) + "</td>" +
      '<td class="num mono">' + (isNull(c.pass_rate) ? "no data" : Math.round(c.pass_rate * 100) + "%") + "</td>" +
      "<td>" + chip(v) + "</td>" +
      "<td>" + marginBar(v) + "</td></tr>";
  }).join("");
  $("tabla-cuerpo").innerHTML = filas;
}

function renderThreshold() {
  var cont = $("chart-threshold");
  var medidas = cellsFiltered().filter(function (c) {
    return c.pp_per_1m && thresholdAt(c) !== null;
  }).sort(function (a, b) { return b.pp_per_1m.median - a.pp_per_1m.median; });
  if (!medidas.length) {
    cont.innerHTML = '<p class="note">no measured cell under this filter</p>';
    return;
  }
  var top = 0;
  medidas.forEach(function (c) {
    top = Math.max(top, c.pp_per_1m.median, thresholdAt(c));
  });
  top *= 1.05;
  if (!(top > 0)) top = 1;  // an all-zero scale must not divide into NaN widths
  var W = 660, labelW = 200, right = 90, rowH = 24, H = medidas.length * rowH + 12;
  var plotW = W - labelW - right;
  var rows = "";
  medidas.forEach(function (c, i) {
    var y = i * rowH + 10;
    // the meter can recalculate a bracket downward (negative median): clamp,
    // the row keeps its label and threshold, the bar just never goes negative
    var bw = Math.max(0, c.pp_per_1m.median / top * plotW);
    var tx = thresholdAt(c) / top * plotW;
    rows += '<text x="' + (labelW - 8) + '" y="' + (y + 10) + '" text-anchor="end" font-size="11" ' +
      'fill="var(--ink-2)" font-family="ui-monospace,Menlo,monospace">' +
      esc(c.model) + " &middot; " + esc(c.workload) + "</text>" +
      '<rect x="' + labelW + '" y="' + y + '" width="' + bw.toFixed(2) + '" height="14" rx="3" ' +
      'fill="var(--legacy)"><title>' + esc(c.model) + " (" + esc(c.workload) + "): measured " +
      c.pp_per_1m.median + " pp/1M</title></rect>" +
      '<rect x="' + (labelW + tx - 1.5).toFixed(2) + '" y="' + (y - 3) + '" width="3" height="20" ' +
      'rx="1.5" fill="var(--new)"><title>' + esc(c.model) + " (" + esc(c.workload) +
      "): threshold " + Number(thresholdAt(c)).toPrecision(6) + " pp/1M at S(x)=" +
      Math.round(effectiveS(c.model) * 100) + "%</title></rect>" +
      '<text x="' + (labelW + bw + 6).toFixed(2) + '" y="' + (y + 10) + '" font-size="10" ' +
      'fill="var(--muted)" font-family="ui-monospace,Menlo,monospace">' +
      Number(c.pp_per_1m.median).toPrecision(4) + "</text>";
  });
  cont.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" width="' + W + '" height="' + H +
    '" role="img" aria-label="measured pp per 1M against the new-plan threshold">' + rows + "</svg>";
}

function renderMargins() {
  var cont = $("chart-margins");
  var filas = cellsFiltered().filter(function (c) { return c.legacy_cost_task_usd; });
  if (!filas.length) {
    cont.innerHTML = '<p class="note">no measured cell under this filter</p>';
    return;
  }
  var verdicts = filas.map(function (c) { return { c: c, v: verdictAt(c) }; });
  var max = 12;
  verdicts.forEach(function (x) {
    if (x.v.margin_pct !== null && x.v.margin_pct > max) max = x.v.margin_pct;
  });
  max *= 1.1;
  var W = 660, labelW = 220, rowH = 26, H = verdicts.length * rowH + 20;
  var half = (W - labelW - 16) / 2, cx = labelW + half;
  var svg = '<line x1="' + cx + '" y1="8" x2="' + cx + '" y2="' + (H - 10) +
    '" stroke="var(--axis)" stroke-width="1"/>';
  verdicts.forEach(function (x, i) {
    var y = i * rowH + 14;
    svg += '<text x="' + (labelW - 8) + '" y="' + (y + 4) + '" text-anchor="end" font-size="11" ' +
      'fill="var(--ink-2)" font-family="ui-monospace,Menlo,monospace">' +
      esc(x.c.model) + " &middot; " + esc(x.c.workload) + "</text>";
    if (x.v.winner === "no data") {
      svg += '<text x="' + cx + '" y="' + (y + 4) + '" text-anchor="middle" font-size="10" ' +
        'fill="var(--muted)">no data</text>';
      return;
    }
    if (x.v.winner === "tie") {
      svg += '<circle cx="' + cx + '" cy="' + y + '" r="4" fill="var(--tie)"><title>tie: inside ' +
        'the band (2 meter ticks or 5 % of the cheaper cost)</title></circle>';
      return;
    }
    var len = x.v.margin_pct / max * half;
    var isLegacy = x.v.winner === "legacy";
    svg += '<rect x="' + (isLegacy ? cx - len : cx).toFixed(2) + '" y="' + (y - 7) +
      '" width="' + len.toFixed(2) + '" height="14" rx="3" fill="var(--' + x.v.winner +
      ')"><title>' + esc(x.c.model) + " (" + esc(x.c.workload) + "): " + x.v.winner +
      " by " + Number(x.v.margin_pct).toFixed(2) + "%</title></rect>" +
      '<text x="' + (isLegacy ? cx - len - 6 : cx + len + 6).toFixed(2) + '" y="' + (y + 4) +
      '" text-anchor="' + (isLegacy ? "end" : "start") + '" font-size="10" fill="var(--ink-2)" ' +
      'font-family="ui-monospace,Menlo,monospace">' + Number(x.v.margin_pct).toFixed(1) + "%</text>";
  });
  cont.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" width="' + W + '" height="' + H +
    '" role="img" aria-label="verdict margin by cell: legacy left, new right, tie dot">' +
    svg + "</svg>";
}

function renderDp() {
  var cont = $("chart-dp");
  var puntos = DATA.dp_tokens_curve || [];
  if (!puntos.length) {
    cont.innerHTML = '<p class="note">no measured bracket</p>';
    return;
  }
  var maxX = Math.max.apply(null, puntos.map(function (p) { return p.tokens_total; })) * 1.05;
  var maxY = Math.max.apply(null, puntos.map(function (p) { return p.dpp_weekly; })) * 1.05;
  // an all-zero axis (every readable bracket below the tick) must not divide
  // by zero into NaN coordinates: the dots sit on the baseline instead
  if (!(maxX > 0)) maxX = 1;
  if (!(maxY > 0)) maxY = 1;
  var W = 660, H = 280, padL = 52, padB = 34, padT = 12, padR = 12;
  var ejes = '<line x1="' + padL + '" y1="' + (H - padB) + '" x2="' + (W - padR) + '" y2="' +
    (H - padB) + '" stroke="var(--axis)" stroke-width="1"/>' +
    '<line x1="' + padL + '" y1="' + padT + '" x2="' + padL + '" y2="' + (H - padB) +
    '" stroke="var(--axis)" stroke-width="1"/>';
  var dots = puntos.map(function (p) {
    var x = padL + p.tokens_total / maxX * (W - padL - padR);
    var y = H - padB - p.dpp_weekly / maxY * (H - padB - padT);
    return '<circle cx="' + x.toFixed(2) + '" cy="' + y.toFixed(2) + '" r="4" fill="var(--legacy)" ' +
      'fill-opacity="0.85"><title>' + esc(p.model) + " &middot; " +
      esc(isNull(p.workload) ? "pooled" : p.workload) + ": " + p.dpp_weekly + " weekly pp / " +
      p.tokens_total + " tokens</title></circle>";
  }).join("");
  cont.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" width="' + W + '" height="' + H +
    '" role="img" aria-label="legacy weekly quota delta versus billed tokens">' + ejes + dots +
    "</svg>" +
    '<p class="note">one dot per measured bracket: legacy weekly &Delta;pp vs billed tokens ' +
    "(the legacy side never moves with the slider)</p>";
}

function renderStatus() {
  $("estado-filtro").textContent = (estado.model || "all models") +
    " - cache slider at " + estado.slider + "%";
}
function renderAll() {
  renderReco();
  renderTable();
  renderThreshold();
  renderMargins();
  renderStatus();
}

$("filter-model").addEventListener("change", function (e) {
  estado.model = e.target.value;
  renderAll();
});
var slider = $("slider-s");
slider.value = String(SLIDER_DEFAULT);
slider.addEventListener("input", function () {
  estado.slider = Number(slider.value);
  $("slider-val").textContent = estado.slider + "%";
  renderAll();
});
$("slider-val").textContent = SLIDER_DEFAULT + "%";

function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
}
Array.prototype.forEach.call(document.querySelectorAll('input[name="theme"]'), function (radio) {
  radio.addEventListener("change", function (e) {
    applyTheme(e.target.value);
    try { localStorage.setItem("dashboard-theme", e.target.value); } catch (err) { /* unavailable */ }
  });
});
(function initTheme() {
  var guardado = null;
  try { guardado = localStorage.getItem("dashboard-theme"); } catch (err) { /* unavailable */ }
  if (guardado !== "light" && guardado !== "dark") guardado = "system";
  var radio = document.querySelector('input[name="theme"][value="' + guardado + '"]');
  if (radio) radio.checked = true;
  applyTheme(guardado);
})();

renderDp();
renderAll();
</script>
</body>
</html>
"""


def _tabla_html(encabezados: list[str], filas: list[list[str]]) -> str:
    """A small static table as HTML (values escaped, right-aligned numerics)."""
    ths = "".join(
        f'<th class="{"v" if i else ""}">{html.escape(e)}</th>' for i, e in enumerate(encabezados)
    )
    cuerpo = ""
    for fila in filas:
        celdas = "".join(
            f'<td class="{"v" if i else ""}">{html.escape(str(e))}</td>' for i, e in enumerate(fila)
        )
        cuerpo += f"<tr>{celdas}</tr>"
    return f"<table><thead><tr>{ths}</tr></thead><tbody>{cuerpo}</tbody></table>"


def _who_wins_html(doc: dict) -> str:
    """The static who-wins table: one row per workload profile, one count set
    per scenario (the model/scenario filters above act on the cells instead)."""
    filas = []
    for w in doc["who_wins"]:
        s0, s1 = w["s0"], w["s1"]
        filas.append(
            [
                w["workload"],
                w["level"] or "",
                f"{s0['legacy']} / {s0['new']} / {s0['tie']} / {s0['unmeasured']}",
                f"{s1['legacy']} / {s1['new']} / {s1['tie']} / {s1['unmeasured']}",
            ]
        )
    if not filas:
        return '<p class="nota">no measured cell under this filter</p>'
    tabla = _tabla_html(
        [
            "workload (user profile)",
            "level",
            "S0 legacy / new / tie / unmeasured",
            "S1 legacy / new / tie / unmeasured",
        ],
        filas,
    )
    return tabla + (
        '<p class="nota">counts of measured models whose winner is each system '
        "(a margin beyond 2 meter ticks or 5 %); unmeasured models count as "
        "unmeasured, never as a win.</p>"
    )


def _sensibilidad_html(doc: dict) -> str:
    """The 4 sweeps as a static summary: the axis moved and where it flipped."""
    sens = doc["sensitivity"]
    filas = []
    for nombre, barrido, valores in (
        ("table rates", sens["rates"], sens["rates"]["factors"]),
        ("cache hit rate S", sens["cache"], sens["cache"]["s_values"]),
        ("P_LEGADO (anchor)", sens["ancla"], sens["ancla"]["factors"]),
    ):
        filas.append(
            [
                nombre,
                ", ".join(f"{v:g}" for v in valores),
                ", ".join(
                    f"{clave}: {len(barrido['flips'][clave])} flips" for clave in barrido["flips"]
                )
                or "no flips",
            ]
        )
    kfila = [f"{f['model']}: {f['verdict'] or 'no comparison'}" for f in sens["k_axis"]["models"]]
    filas.append(["k axis (cells under k)", "1 / 4 / 8", "; ".join(kfila) or "no cells"])
    return _tabla_html(["sweep", "axis values", "verdict changes"], filas)


def _calibracion_html(doc: dict) -> str:
    """The hit-rate provenance per model + the unmaterialized paper discounts."""
    lineas = []
    for modelo, resuelto in sorted(doc["s_per_model"].items()):
        fuente = "measured" if resuelto["source"] == "measured" else "assumed"
        detalle = (
            f", measured {resuelto['measured_hit_rate'] * 100:.0f}%"
            if fuente == "measured" and resuelto["measured_hit_rate"] is not None
            else ""
        )
        lineas.append(f"{modelo}: S={resuelto['s'] * 100:.0f}% ({fuente}{detalle})")
    cuerpo = "".join(f"<p class='nota'>{html.escape(t)}</p>" for t in lineas)
    descuentos = doc.get("paper_discounts") or []
    aviso = (
        "<p class='nota'>unmaterialized paper discounts: "
        + ", ".join(html.escape(d) for d in descuentos)
        + "</p>"
        if descuentos
        else ""
    )
    return (
        cuerpo or "<p class='nota'>no calibration data: every S1 is the assumed hit rate</p>"
    ) + aviso


def render_dashboard(doc: dict, rates: dict | None = None) -> str:
    """The dashboard: one self-contained HTML file. The analysis doc rides
    inside it as JSON (no fetches, no CDN, no sibling files), the model filter,
    the three-state theme and the cache slider are plain DOM, and every value
    escapes through textContent, html.escape or the JS esc() helper. `rates`
    (from `rates_map`) rides in its own JSON block: the slider's live
    recomputation needs them; analysis.json never does."""
    bp = doc["base_params"]
    bruto = doc["raw"]
    opciones = "".join(
        f'<option value="{html.escape(m)}">{html.escape(m)}</option>'
        for m in sorted({c["model"] for c in doc["cells"]})
    )
    resumen = (
        f"table {html.escape(bp['table_version'])} | anchor ${bp['ancla']:g}/mo = "
        f"{bp['usd_per_pp']:.6f} USD/pp (tick {bp['tick_usd']:.6f} USD) | "
        f"S1 assumed {bp['s'] * 100:g}% | generated "
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(doc['generated_at']))} UTC | "
        f"raw: {bruto['request_lines']} request lines, {bruto['batch_lines']} batch lines "
        f"({', '.join(bruto['run_ids']) or 'no runs'}) | protocol {doc['protocol_version']}"
    )
    tarifas = rates if rates is not None else {"per": 1_000_000, "rates": {}}
    return (
        _DASHBOARD.replace("__RESUMEN__", html.escape(resumen))
        .replace("__OPCIONES__", opciones)
        .replace("__WHO_WINS__", _who_wins_html(doc))
        .replace("__SENSIBILIDAD__", _sensibilidad_html(doc))
        .replace("__CALIBRACION__", _calibracion_html(doc))
        .replace("__NOTAS__", html.escape(doc["notes"]))
        .replace(
            "__RATES__",
            json.dumps(tarifas, ensure_ascii=False).replace("</", "<\\/"),
        )
        .replace(
            "__DATOS__",
            json.dumps(doc, ensure_ascii=False).replace("</", "<\\/"),
        )
    )
