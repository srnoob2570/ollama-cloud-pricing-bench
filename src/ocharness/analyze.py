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
  the data rides inside the file) with model/scenario filters;
- `pngs/*.png` — the threshold bars and the dp-tokens curve (matplotlib).

Cost model (methodology v1 §3, CONTEXT.md "critical threshold"):

- legacy side: Δpp(weekly) of the bracketed batch x (P_LEGADO / 4.345 / 100)
  USD per pp, per attempted (accepted) and per completed (checker-passed) task;
- new side: measured median tokens x the versioned table rates, under S0=0 %
  and S1=`--s` (or the measured hit rate where the calibration was conclusive);
- threshold pp/1M = the cell's own measured token mix priced on the table
  ($/1M) / USD-per-pp — the pp/1M above which legacy becomes more expensive
  for that cell. It is computed ONLY from the cell's own measurement: an
  unmeasured cell gets no threshold and never borrows one from another model;
- verdict: legacy/new when one system is cheaper by a real margin (>2 meter
  ticks OR >5 % of the cheaper cost, the methodology's uncertainty rule), tie
  inside it, "no data" whenever either side is unmeasurable.

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
from .calibration import TICK_PP  # the meter's resolution, in percentage points
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


def _cuantiles(values: list, places: int) -> dict | None:
    """median / p25 / p75 / p95; None with nothing to measure. A single
    observation collapses to the value itself (quantiles need 2+ points)."""
    limpios = [float(v) for v in values if _es_numero(v)]
    if not limpios:
        return None
    if len(limpios) == 1:
        v = round(limpios[0], places)
        return {"median": v, "p25": v, "p75": v, "p95": v}
    cuartiles = statistics.quantiles(limpios, n=4)
    return {
        "median": round(statistics.median(limpios), places),
        "p25": round(cuartiles[0], places),
        "p75": round(cuartiles[2], places),
        "p95": round(statistics.quantiles(limpios, n=20)[18], places),
    }


def verdict_of(legacy: float | None, nuevo: float | None, tick_usd: float) -> str:
    """Who wins this cell: legacy/new when cheaper by a real margin, tie
    inside the meter's resolution, "no data" when either side is unmeasurable.

    A real margin is the methodology's uncertainty rule in dollars — the gap
    must exceed 2 ticks of the meter OR 5 % of the cheaper cost — so the tie
    band is the MINIMUM of the two: the binding (smaller) threshold decides.
    """
    if legacy is None or nuevo is None:
        return "no data"
    margen = min(2 * tick_usd, 0.05 * min(legacy, nuevo))
    if abs(legacy - nuevo) <= margen:
        return "tie"
    return "legacy" if legacy < nuevo else "new"


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


def _rep_row(batch: dict, lineas: list[dict], usd: float) -> dict:
    """One bracketed batch's contribution: its measured rep of the cell.

    Batch lines carry no `rep` field (the schema never declared one); the
    repetition comes from the batch's own request lines, where it is required.
    """
    rep = lineas[0].get("rep") if lineas else None
    tin, tout = _tokens_de(lineas)
    tokens = tin + tout if tin is not None else None
    intentadas = sum(1 for r in lineas if r.get("http") == 200)
    completadas = sum(1 for r in lineas if r.get("checker") == "pass")
    dpp = batch.get("dpp_weekly")
    medible = _es_numero(dpp) and intentadas > 0
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
        "pp_per_1m": round(pp, 4) if pp is not None else None,
        "cost_task_attempted_usd": round(dpp * usd / intentadas, 9) if medible else None,
        "cost_task_completed_usd": (
            round(dpp * usd / completadas, 9) if medible and completadas else None
        ),
    }


def _cells(batches: list[dict], requests: list[dict], usd: float) -> dict:
    """The k=1 cells grouped by (model, workload): per-rep rows plus the raw
    lines that back them (tokens, checker verdicts)."""
    por_batch = _por_batch(requests)
    celdas: dict = {}
    for batch in batches:
        if batch.get("k") != 1 or batch.get("workload") not in TASK_WORKLOADS:
            continue
        if not isinstance(batch.get("model"), str) or not isinstance(batch.get("batch_id"), str):
            continue
        clave = (batch["model"], batch["workload"])
        celda = celdas.setdefault(clave, {"level": batch.get("level"), "reps": [], "lineas": []})
        celda["reps"].append(_rep_row(batch, por_batch.get(batch["batch_id"], []), usd))
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
    tin_med = round(statistics.median(tins), 3) if tins else None
    tout_med = round(statistics.median(touts), 3) if touts else None

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
        s0 = round(new_task_cost(tin_med, tout_med, tarifa, s=0.0, per=tabla.per), 9)
        s1 = round(new_task_cost(tin_med, tout_med, tarifa, s=s_efectivo.s, per=tabla.per), 9)

    # The threshold prices the cell's OWN measured mix on the new table and
    # bridges it to the meter's unit: an unmeasured cell gets none, and none
    # is ever borrowed from another model's measurement.
    tokens_medios = tin_med + tout_med if tin_med is not None and tout_med is not None else None

    legacy_cuantiles = _cuantiles(
        [r["cost_task_attempted_usd"] for r in reps if r["cost_task_attempted_usd"] is not None], 9
    )
    legacy_med = legacy_cuantiles["median"] if legacy_cuantiles else None
    # The threshold exists only for a MEASURED cell: without a readable bracket
    # there is no comparison to draw, and no line is invented for the bars.
    umbral = None
    if s0 is not None and tokens_medios and legacy_med is not None:
        umbral = {
            "s0": round(s0 / (tokens_medios / 1e6) / usd, 4),
            "s1": round(s1 / (tokens_medios / 1e6) / usd, 4) if s1 is not None else None,
        }
    return {
        "model": model,
        "workload": workload,
        "level": celda["level"],
        "reps": reps,
        "attempted": intentadas,
        "completed": completadas,
        "pass_rate": round(completadas / intentadas, 4) if intentadas else None,
        "tok_in_median": tin_med,
        "tok_out_median": tout_med,
        "pp_per_1m": _cuantiles([r["pp_per_1m"] for r in reps if r["pp_per_1m"] is not None], 4),
        "legacy_cost_task_usd": legacy_cuantiles,
        "legacy_cost_completed_usd": _cuantiles(
            [
                r["cost_task_completed_usd"]
                for r in reps
                if r["cost_task_completed_usd"] is not None
            ],
            9,
        ),
        "new_cost_task_s0_usd": s0,
        "new_cost_task_s1_usd": s1,
        "s_effective": {"s": round(s_efectivo.s, 4), "source": s_efectivo.source},
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
                v = c["verdict"][escenario]
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
                    "new_cost_task_usd": round(nuevo, 9),
                    "threshold_pp_per_1m": (
                        round(c["threshold_pp_per_1m"]["s0"] * factor, 4)
                        if c["threshold_pp_per_1m"]
                        else None
                    ),
                    "verdict": veredicto,
                }
            )
            if veredicto != c["verdict"][SWEEP_SCENARIO]:
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
                    "new_cost_task_usd": round(nuevo, 9),
                    "verdict": veredicto,
                }
            )
            if veredicto != c["verdict"][SWEEP_SCENARIO]:
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
                    "legacy_cost_task_usd": round(legacy, 9),
                    "pp_per_1m": c["pp_per_1m"]["median"] if c["pp_per_1m"] else None,
                    "verdict": veredicto,
                }
            )
            if veredicto != c["verdict"][SWEEP_SCENARIO]:
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


def _sweep_k(batches: list[dict], requests: list[dict], usd: float) -> dict:
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
        fila = _rep_row(batch, por_batch.get(batch.get("batch_id"), []), usd)
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
            if max(costes) - min(costes) <= usd * TICK_PP:  # one tick of resolution
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


def build(
    base: pathlib.Path,
    *,
    tabla,
    ancla: float,
    s: float,
    level: str | None = None,
    model: str | None = None,
) -> dict:
    """The analysis doc, computed from raw alone. Raises AnalyzeError when the
    base holds no raw dataset at all."""
    base = pathlib.Path(base)
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
    # incomparable measurements, so only this harness's protocol is analyzed
    # and whatever else the directory holds is counted, never averaged in.
    protocolo_requests = [r for r in requests if r.get("protocol_version") == PROTOCOL_VERSION]
    protocolo_batches = [b for b in batches if b.get("protocol_version") == PROTOCOL_VERSION]
    descartadas = (len(requests) - len(protocolo_requests)) + (
        len(batches) - len(protocolo_batches)
    )
    requests, batches = protocolo_requests, protocolo_batches
    if not requests and not batches:
        raise AnalyzeError(
            f"no raw dataset under {base} speaks protocol {PROTOCOL_VERSION!r} "
            f"({descartadas} lines of other vintages were set aside): nothing to analyze"
        )
    if level is not None:
        batches = [b for b in batches if b.get("level") == level]
        ids_validos = {b.get("batch_id") for b in batches}
        requests = [r for r in requests if r.get("batch_id") in ids_validos]

    usd = usd_per_pp(ancla)
    tick_usd = usd * TICK_PP

    # The effective hit rate per model: the calibration's measured rate wins
    # over the --s assumption when it was conclusive (methodology v1 §7).
    calibracion = load_calibrations(runs_dir)
    agrupadas = _cells(batches, requests, usd)
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
        "generated_at": round(time.time(), 3),
        "protocol_version": PROTOCOL_VERSION,
        "base_params": {
            "table_version": tabla.table_version,
            "ancla": ancla,
            "usd_per_pp": usd,
            "s": s,
            "tick_pp": TICK_PP,
            "tick_usd": round(tick_usd, 9),
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
        "who_wins": _who_wins(celdas),
        "dp_tokens_curve": _curva_dp_tokens(batches, requests),
        "sensitivity": {
            "rates": _sweep_rates(celdas, tick_usd),
            "cache": _sweep_cache(celdas, tabla, tick_usd),
            "ancla": _sweep_ancla(celdas, tick_usd),
            "k_axis": _sweep_k(batches, requests, usd),
        },
        "paper_discounts": sin_materializar,
        "notes": (
            "computed from the raw runs/*.jsonl + batches/*.jsonl lines alone "
            "(k=1 cells are the derivatives' baseline; k>1 cells and the "
            "calibration/probe workstreams stay out of the cells). The threshold "
            "pp/1M prices each cell's OWN measured token mix on the table: an "
            "unmeasured cell reports no data and never borrows a threshold. A "
            "verdict needs a real margin: >2 meter ticks or >5 % of the cheaper "
            "cost; the per-rep quantiles carry the full uncertainty."
        ),
    }


# ---------------------------------------------------------------------------
# the bundle: analysis.json, the dashboard, the PNGs
# ---------------------------------------------------------------------------


def write_bundle(base: pathlib.Path, doc: dict, emit=print) -> pathlib.Path:
    """Writes the analysis bundle under `base/analysis/`; returns the folder."""
    carpeta = pathlib.Path(base) / "analysis"
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "analysis.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (carpeta / "dashboard.html").write_text(render_dashboard(doc), encoding="utf-8")
    try:
        write_pngs(doc, carpeta / "pngs")
    except Exception as e:  # noqa: BLE001 - a PNG failure must not lose the analysis
        if emit:
            emit(
                f"analyze: PNG generation failed ({type(e).__name__}: {e}); the JSON and "
                "dashboard bundle is complete without them"
            )
    return carpeta


def write_pngs(doc: dict, carpeta: pathlib.Path) -> list[pathlib.Path]:
    """The threshold bars (one chart per workload: the measured pp/1M median
    of each model against its own S0/S1 threshold marks) and the dp-tokens
    curve (one series per workload)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    carpeta = pathlib.Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    escritos: list[pathlib.Path] = []
    tabla_version = doc["base_params"]["table_version"]

    por_workload: dict = {}
    for c in doc["cells"]:
        if c["pp_per_1m"] and c["threshold_pp_per_1m"]:
            por_workload.setdefault(c["workload"], []).append(c)
    for workload, grupo in sorted(por_workload.items()):
        figura, ax = plt.subplots(figsize=(9, 4.8))
        modelos = [c["model"] for c in grupo]
        ax.bar(
            range(len(modelos)),
            [c["pp_per_1m"]["median"] for c in grupo],
            color="#4878a8",
            label="measured pp/1M (median)",
        )
        for i, c in enumerate(grupo):
            for escenario, color, estilo in (("s0", "#c44e52", "-"), ("s1", "#dd8452", "--")):
                etiqueta = f"threshold {escenario.upper()}" if i == 0 else None
                ax.hlines(
                    c["threshold_pp_per_1m"][escenario],
                    i - 0.4,
                    i + 0.4,
                    colors=color,
                    linestyles=estilo,
                    linewidth=2,
                    label=etiqueta,
                )
        ax.set_xticks(range(len(modelos)), modelos, rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("pp per 1M tokens")
        ax.set_title(f"Critical threshold - {workload} (table {tabla_version})")
        ax.legend(fontsize=8)
        ax.grid(True, axis="y", alpha=0.3)
        figura.tight_layout()
        destino = carpeta / f"threshold-{workload}.png"
        figura.savefig(destino, dpi=110)
        plt.close(figura)
        escritos.append(destino)

    puntos = doc["dp_tokens_curve"]
    if puntos:
        figura, ax = plt.subplots(figsize=(9, 5))
        series: dict = {}
        for p in puntos:
            series.setdefault(p["workload"], []).append(p)
        for workload, ps in sorted(series.items()):
            ax.scatter(
                [p["tokens_total"] / 1e6 for p in ps],
                [p["dpp_weekly"] for p in ps],
                s=28,
                alpha=0.85,
                label=workload,
            )
        ax.set_xlabel("tokens billed (millions)")
        ax.set_ylabel("weekly quota delta (pp)")
        ax.set_title(f"Legacy quota vs tokens (table {tabla_version})")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        figura.tight_layout()
        destino = carpeta / "dp-tokens.png"
        figura.savefig(destino, dpi=110)
        plt.close(figura)
        escritos.append(destino)
    return escritos


# ---------------------------------------------------------------------------
# the static self-contained dashboard: one HTML file, zero external resources
# ---------------------------------------------------------------------------

_DASHBOARD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ollama Cloud cost analysis</title>
<style>
:root { color-scheme: light dark; --tinta: #1c1c1c; --fondo: #f7f7f5;
        --panel: #ffffff; --borde: #d9d9d4; --tenue: #6b6b66; }
@media (prefers-color-scheme: dark) {
  :root { --tinta: #e4e4e0; --fondo: #17181a; --panel: #202226; --borde: #3a3d42;
          --tenue: #9a9a94; }
}
* { box-sizing: border-box; }
body { font: 14px/1.45 system-ui, sans-serif; color: var(--tinta);
       background: var(--fondo); margin: 0; padding: 24px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 28px 0 8px; border-bottom: 1px solid var(--borde);
     padding-bottom: 4px; }
h3 { font-size: 13px; margin: 12px 0 6px; }
.sub { color: var(--tenue); font-size: 12px; margin-bottom: 16px; }
.panel { background: var(--panel); border: 1px solid var(--borde); border-radius: 8px;
         padding: 14px 16px; margin-bottom: 12px; }
.filtros { display: flex; gap: 24px; flex-wrap: wrap; align-items: center;
           margin: 14px 0 4px; }
select, fieldset { font: inherit; }
fieldset { border: 1px solid var(--borde); border-radius: 6px; padding: 4px 10px;
           margin: 0; }
legend { font-size: 12px; color: var(--tenue); }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--borde); }
th { font-size: 12px; color: var(--tenue); font-weight: 600; }
td.v, th.v { text-align: right; font-variant-numeric: tabular-nums; }
tr.gana-legacy td:last-child { color: #b3543f; font-weight: 600; }
tr.gana-new td:last-child { color: #2e7d4f; font-weight: 600; }
.sin-datos { color: var(--tenue); font-style: italic; }
.fila-barra { display: flex; align-items: center; margin: 5px 0; }
.nombre { width: 170px; flex: none; font-size: 12px; overflow: hidden;
          text-overflow: ellipsis; white-space: nowrap; padding-right: 8px; }
.pista { position: relative; flex: 1; height: 14px;
         background: rgba(127, 127, 127, 0.15); border-radius: 3px; }
.barra { display: block; height: 100%; background: #4878a8; border-radius: 3px; }
.umbral { position: absolute; top: -3px; bottom: -3px; width: 2px; }
.umbral-s0 { background: #c44e52; }
.umbral-s1 { background: #dd8452; }
.umbral:not(.activo) { opacity: 0.3; }
.leyenda { font-size: 12px; color: var(--tenue); margin: 8px 0 0; }
.leyenda .muestra { display: inline-block; width: 10px; height: 10px;
                    border-radius: 2px; margin: 0 4px 0 12px; }
.nota { font-size: 12px; color: var(--tenue); }
</style>
</head>
<body>
<h1>Ollama Cloud cost analysis &mdash; legacy vs new</h1>
<div class="sub">__RESUMEN__</div>

<div class="panel">
  <div class="filtros">
    <label>model
      <select id="filter-model"><option value="">all models</option>__OPCIONES__</select>
    </label>
    <fieldset>
      <legend>cache scenario</legend>
      <label><input type="radio" name="filter-scenario" value="s0" checked> S0 (0% cache)</label>
      <label><input type="radio" name="filter-scenario" value="s1"> S1 (assumed/measured)</label>
    </fieldset>
    <span id="estado-filtro" class="nota"></span>
  </div>
</div>

<h2>Cells (per model &times; workload)</h2>
<div class="panel" style="overflow-x: auto;">
  <table>
    <thead><tr>
      <th>model</th><th>workload</th><th>level</th>
      <th class="v">legacy $/task</th><th class="v">new $/task</th>
      <th class="v">measured pp/1M</th><th class="v">threshold pp/1M</th>
      <th class="v">pass rate</th><th>verdict</th>
    </tr></thead>
    <tbody id="tabla-cuerpo"></tbody>
  </table>
</div>

<h2>Critical threshold (pp/1M): measured vs threshold</h2>
<div class="panel">
  <div id="barras"></div>
  <div class="leyenda">bar = measured pp/1M (median, k=1 cells)
    <span class="muestra" style="background:#c44e52"></span>threshold S0
    <span class="muestra" style="background:#dd8452"></span>threshold S1
    (the highlighted tick follows the scenario filter; unmeasured cells appear
    as no data and never take a bar or a threshold)</div>
</div>

<h2>Who wins, by user profile</h2>
<div class="panel">__WHO_WINS__</div>

<h2>Sensitivity sweeps</h2>
<div class="panel">__SENSIBILIDAD__</div>

<h2>Cache calibration</h2>
<div class="panel">__CALIBRACION__</div>

<p class="nota">__NOTAS__</p>

<script id="analysis-data" type="application/json">__DATOS__</script>
<script>
"use strict";
var DATA = JSON.parse(document.getElementById("analysis-data").textContent);
var estado = { modelo: "", escenario: "s0" };
function $(id) { return document.getElementById(id); }
function sin(x) { return x === null || x === undefined; }
function dinero(x) { return sin(x) ? "no data" : "$" + Number(x).toPrecision(6); }
function pps(x) { return sin(x) ? "no data" : Number(x).toFixed(4) + " pp/1M"; }

function celdasFiltradas() {
  return DATA.cells.filter(function (c) {
    return !estado.modelo || c.model === estado.modelo;
  });
}

function renderTabla() {
  var cuerpo = $("tabla-cuerpo");
  cuerpo.textContent = "";
  var esc = estado.escenario;
  celdasFiltradas().forEach(function (c) {
    var tr = document.createElement("tr");
    [
      c.model, c.workload, c.level,
      dinero(c.legacy_cost_task_usd ? c.legacy_cost_task_usd.median : null),
      dinero(esc === "s0" ? c.new_cost_task_s0_usd : c.new_cost_task_s1_usd),
      pps(c.pp_per_1m ? c.pp_per_1m.median : null),
      c.threshold_pp_per_1m ? pps(c.threshold_pp_per_1m[esc]) : "no data",
      sin(c.pass_rate) ? "no data" : Math.round(c.pass_rate * 100) + "%",
      c.verdict[esc]
    ].forEach(function (valor) {
      var td = document.createElement("td");
      td.textContent = valor;
      tr.appendChild(td);
    });
    if (c.verdict[esc] === "legacy") tr.className = "gana-legacy";
    if (c.verdict[esc] === "new") tr.className = "gana-new";
    cuerpo.appendChild(tr);
  });
}

function renderBarras() {
  var cont = $("barras");
  cont.textContent = "";
  var medidas = celdasFiltradas().filter(function (c) {
    return c.pp_per_1m && c.threshold_pp_per_1m;
  });
  if (!medidas.length) {
    var vacio = document.createElement("p");
    vacio.className = "nota";
    vacio.textContent = "no measured cell under this filter";
    cont.appendChild(vacio);
    return;
  }
  var maximo = 0;
  medidas.forEach(function (c) {
    maximo = Math.max(maximo, c.pp_per_1m.median, c.threshold_pp_per_1m.s0,
                      c.threshold_pp_per_1m.s1 || 0);
  });
  var porWorkload = {};
  medidas.forEach(function (c) {
    (porWorkload[c.workload] = porWorkload[c.workload] || []).push(c);
  });
  Object.keys(porWorkload).sort().forEach(function (workload) {
    var seccion = document.createElement("div");
    var titulo = document.createElement("h3");
    titulo.textContent = workload;
    seccion.appendChild(titulo);
    porWorkload[workload].forEach(function (c) {
      var fila = document.createElement("div");
      fila.className = "fila-barra";
      var nombre = document.createElement("span");
      nombre.className = "nombre";
      nombre.textContent = c.model;
      var pista = document.createElement("span");
      pista.className = "pista";
      var barra = document.createElement("span");
      barra.className = "barra";
      barra.style.width = (c.pp_per_1m.median / maximo * 100).toFixed(2) + "%";
      barra.title = "measured: " + c.pp_per_1m.median + " pp/1M";
      pista.appendChild(barra);
      ["s0", "s1"].forEach(function (esc) {
        if (sin(c.threshold_pp_per_1m[esc])) return;
        var tick = document.createElement("span");
        tick.className = "umbral umbral-" + esc +
          (esc === estado.escenario ? " activo" : "");
        tick.style.left = (c.threshold_pp_per_1m[esc] / maximo * 100).toFixed(2) + "%";
        tick.title = "threshold " + esc + ": " + c.threshold_pp_per_1m[esc] + " pp/1M";
        pista.appendChild(tick);
      });
      fila.appendChild(nombre);
      fila.appendChild(pista);
      seccion.appendChild(fila);
    });
    cont.appendChild(seccion);
  });
}

function render() {
  renderTabla();
  renderBarras();
  $("estado-filtro").textContent =
    (estado.modelo || "all models") + " - scenario " + estado.escenario.toUpperCase();
}
$("filter-model").addEventListener("change", function (e) {
  estado.modelo = e.target.value;
  render();
});
Array.prototype.forEach.call(
  document.querySelectorAll('input[name="filter-scenario"]'),
  function (radio) {
    radio.addEventListener("change", function (e) {
      estado.escenario = e.target.value;
      render();
    });
  }
);
render();
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
        ("P_LEGADO (ancla)", sens["ancla"], sens["ancla"]["factors"]),
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


def render_dashboard(doc: dict) -> str:
    """The dashboard: one self-contained HTML file. The analysis doc rides
    inside it as JSON (no fetches, no CDN, no sibling files), the model and
    scenario filters are plain DOM, and every value escapes through
    textContent or html.escape."""
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
    return (
        _DASHBOARD.replace("__RESUMEN__", html.escape(resumen))
        .replace("__OPCIONES__", opciones)
        .replace("__WHO_WINS__", _who_wins_html(doc))
        .replace("__SENSIBILIDAD__", _sensibilidad_html(doc))
        .replace("__CALIBRACION__", _calibracion_html(doc))
        .replace("__NOTAS__", html.escape(doc["notes"]))
        .replace(
            "__DATOS__",
            json.dumps(doc, ensure_ascii=False).replace("</", "<\\/"),
        )
    )
