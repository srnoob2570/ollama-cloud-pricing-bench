"""The concurrency workstream (methodology v1 §6): cut-off probe + k∈{1,4,8} cells.

Two phases against the live endpoint, one `bench probe-concurrency` invocation:

1. **Probe** (discovery, not measurement): for k = 4..`k_max`, one volley of k
   simultaneous copies of the anchor's single short request — the calibration
   fixture — recording per-request acceptance / 429 / error. The first volley
   that is not fully accepted ends the sweep: the cut-off is the largest k whose
   volley was fully accepted (None = below the probe floor of 4). One line per
   volley lands in `runs/probe-<run_id>.jsonl` (its own schema; the per-request
   outcomes are the 429 evidence).
2. **Cells**: the k∈{1,4,8} cells as bracketed batches of `CELL_REQUESTS` copies
   of the same fixture — every cell carries the same total tokens, only k
   differs. A cell whose planned k exceeds the measured cut-off **re-anchors**
   to the cut-off (documented on the batch line and in the summary); the
   sub-floor case leaves only the k=1 cell. Before the first cell bracket, the
   registration loop flushes the probe's unbracketed spend so no cell's Δpp
   absorbs it (skipped when the probe was reused and some cell already closed:
   that close proves the flush already ran for this run). The cells are
   measured brackets — salted under the cache-free lane; the probe's volleys
   are exempt.

The verdict metric — **effective cost per task under k** — is computed from the
raw `batches/*.jsonl` + `runs/*.jsonl` lines with the anchor (`--ancla` USD/month
amortized weekly ÷ 100 pp): cost per task = Δpp(weekly) × USD/pp ÷ tasks, per
attempted and per completed (checker-passed) task. The probe and the cells share
one per-level manifest (`runs/manifest-<level>-concurrency.json`): the cut-off
is a per-key property, so it is probed once and reused (loudly) by later cells
for other models; the cells themselves resume like any bracketed batch.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time

from . import fixtures, schema
from .client import PROTOCOL_VERSION, OllamaCloud
from .meter import usd_per_pp  # the anchor bridge: a Δpp's paid dollars
from .runner import (
    BatchContext,
    BatchSpec,
    RunnerError,
    _append_jsonl,
    _burst,
    _ensure_canary,
    _execute_batch,
    _registration_settle,
    batch_id,
    open_workstream_manifest,
)
from .schema import read_jsonl

ANCHOR_LEVEL = "T1"  # the workstream runs on the T1 anchor: its cheapest fixture
ANCHOR_WORKLOAD = "concurrency"
PROBE_WORKLOAD = "probe"
CELL_REQUESTS = 8  # per cell, divisible by every planned k: the same total tokens
CELL_KS = (1, 4, 8)
PROBE_K_FROM = 4
# The probe's spend scales with the sweep's ceiling and is unbudgeted by the
# dry-run mark: a hard ceiling bounds the worst case (every volley accepted up
# to k_max) to ~2k one-word requests, far above the published per-plan limits.
PROBE_K_CEILING = 64


def re_anchor(cut_off: int | None) -> list[tuple[int, str]]:
    """The k∈{1,4,8} cells after re-anchoring to the measured cut-off.

    Returns (k, plan_note) sorted by k: a planned k above the cut-off clamps to
    it (the max concurrency the key actually sustains) and duplicates collapse —
    a non-empty note marks the cell as re-anchored. A sub-floor cut-off (None)
    leaves only the k=1 cell.
    """
    if cut_off is None:
        return [(1, "")]
    planeados: dict[int, list[int]] = {}
    for k in sorted(CELL_KS):
        planeados.setdefault(min(k, cut_off), []).append(k)
    celdas = []
    for efectivo, origenes in sorted(planeados.items()):
        re_anclados = [k for k in origenes if k > cut_off]
        if re_anclados:
            nota = (
                f"re-anchored: probe cut-off {cut_off} < planned k "
                f"{', '.join(str(k) for k in re_anclados)}; the cell runs at k={efectivo} "
                "(the max the key sustained)"
            )
        else:
            nota = ""
        celdas.append((efectivo, nota))
    return celdas


def _probe_attempt(ruta_probe: pathlib.Path, run_id: str) -> int:
    """The next probe attempt number for this run_id (a re-probed sweep is a new attempt)."""
    prefijo = f"{run_id}-a"
    mayor = 0
    for linea in read_jsonl(ruta_probe):
        pid = linea.get("probe_id")
        if isinstance(pid, str) and pid.startswith(prefijo):
            cola = pid[len(prefijo) :].split("-", 1)[0]
            try:
                mayor = max(mayor, int(cola))
            except ValueError:
                continue
    return mayor + 1


def _probe_line(
    *,
    probe_id: str,
    run_id: str,
    model: str,
    k: int,
    seeds: list[int],
    outcomes: list[dict],
    fixture_hash: str,
    t_start: float,
    t_total: float,
    table_version: str,
) -> dict:
    accepted = sum(1 for o in outcomes if o["http"] == 200)
    rejected = sum(1 for o in outcomes if o["http"] == 429)
    return {
        "probe_id": probe_id,
        "run_id": run_id,
        "level": ANCHOR_LEVEL,
        "model": model,
        "workload": PROBE_WORKLOAD,
        "k": k,
        "requested": k,
        "accepted": accepted,
        "rejected": rejected,
        "errored": k - accepted - rejected,
        "t_start": t_start,
        "t_total": t_total,
        "outcomes": outcomes,
        "seeds": seeds,
        "fixture_hash": fixture_hash,
        "table_version": table_version,
        "protocol_version": PROTOCOL_VERSION,
    }


async def _sweep(
    client: OllamaCloud,
    model: str,
    modelo_api: str,
    *,
    k_max: int,
    run_id: str,
    ruta_probe: pathlib.Path,
    table_version: str,
    emit,
) -> tuple[int | None, str, list[dict]]:
    """The limit probe: k=4..k_max volleys of the anchor's single short request.

    Each volley is the runner's own burst (`_burst`): n == k copies at
    concurrency k, so the probe and the cells cannot drift apart in how they
    fire simultaneous requests. The volley's seeds derive from the `probe`
    workload name (discovery evidence, distinguishable from the cells' seeds).

    Stops at the first volley that is not fully accepted. Returns
    (cut_off, cut_off_note, volley summaries); the raw per-request evidence
    lands in `ruta_probe` as schema-validated lines. Like every dataset line,
    the volley records the slate id (`model`) while the wire carries
    `modelo_api` (the preflight's catalog match).
    """
    attempt = _probe_attempt(ruta_probe, run_id)
    volleys: list[dict] = []
    fallo: dict | None = None
    for k in range(PROBE_K_FROM, k_max + 1):
        specs = fixtures.build(ANCHOR_LEVEL, ANCHOR_WORKLOAD, k)
        hash_fixture = fixtures.fixture_hash(specs)
        probe_id = f"{run_id}-a{attempt}-k{k:02d}"
        # A transient spec: the volley is not a cell — no manifest state, no
        # request lines; the burst itself (n == k, concurrency k) is reused.
        volley = BatchSpec(
            level=ANCHOR_LEVEL,
            batch_id=probe_id,
            workload=PROBE_WORKLOAD,
            model=model,
            rep=1,
            k=k,
            n=k,
            fixture_hash=hash_fixture,
        )
        t_start = time.time()
        registros = await _burst(client, volley, specs, modelo_api)
        t_total = time.time()
        semillas = [fixtures.seed(PROBE_WORKLOAD, model, volley.rep, i) for i in range(volley.n)]
        outcomes = [
            {"http": r["http"], "err": r["err"], "done": r["done"] is not None} for r in registros
        ]
        linea = _probe_line(
            probe_id=probe_id,
            run_id=run_id,
            model=model,
            k=k,
            seeds=semillas,
            outcomes=outcomes,
            fixture_hash=hash_fixture,
            t_start=t_start,
            t_total=t_total,
            table_version=table_version,
        )
        schema.validate_probe_line(linea)
        _append_jsonl(ruta_probe, linea)
        resumen = {
            "probe_id": probe_id,
            "k": k,
            "requested": linea["requested"],
            "accepted": linea["accepted"],
            "rejected": linea["rejected"],
            "errored": linea["errored"],
        }
        volleys.append(resumen)
        if emit:
            emit(
                f"probe: k={k}: {linea['accepted']}/{k} accepted, "
                f"{linea['rejected']} rejected, {linea['errored']} errored"
            )
        if linea["accepted"] < k:
            if linea["errored"]:
                # The cut-off is a per-key 429 phenomenon. A transport error is
                # not a measurement of it: persisting a sub-floor (or too-low)
                # conclusion from failed requests would pin every later run to
                # the k=1 cell. The volley line above keeps the raw evidence;
                # the sweep aborts and a healthy resume re-probes.
                raise RunnerError(
                    f"probe volley k={k}: {linea['errored']} of {k} requests errored "
                    "(transport failures, not 429 rejections) - the cut-off cannot be "
                    "measured from failed requests; re-run the probe"
                )
            fallo = resumen
            break
    if fallo is None:
        cut_off = k_max
        nota = f"no rejection up to the probe ceiling k={k_max}; the cut-off is >= {k_max}"
    elif fallo["k"] <= PROBE_K_FROM:
        cut_off = None
        nota = (
            f"the cut-off is below the probe floor ({PROBE_K_FROM}): even the k={fallo['k']} "
            f"volley was not fully accepted ({fallo['rejected']} rejected) - only the k=1 "
            "cell is viable"
        )
    else:
        cut_off = fallo["k"] - 1
        causa = f"{fallo['rejected']} of {fallo['requested']} rejected (HTTP 429)"
        nota = f"volley k={fallo['k']}: {causa}; the cut-off is {cut_off}"
    return cut_off, nota, volleys


def _cell_specs(*, run_id: str, model: str, cut_off: int | None) -> list[BatchSpec]:
    """The re-anchored cell specs: one bracketed batch per viable k."""
    specs_requeridos = fixtures.build(ANCHOR_LEVEL, ANCHOR_WORKLOAD, CELL_REQUESTS)
    hash_fixture = fixtures.fixture_hash(specs_requeridos)
    specs = []
    for k, nota in re_anchor(cut_off):
        specs.append(
            BatchSpec(
                level=ANCHOR_LEVEL,
                batch_id=batch_id(run_id, ANCHOR_LEVEL, ANCHOR_WORKLOAD, model, 1, k),
                workload=ANCHOR_WORKLOAD,
                model=model,
                rep=1,
                k=k,
                n=CELL_REQUESTS,
                fixture_hash=hash_fixture,
                plan_note=nota,
            )
        )
    return specs


def cell_plan(cut_off: int | None) -> dict:
    """The manifest-stamped cell plan (ks + n), for the resume drift guard."""
    return {"ks": [k for k, _nota in re_anchor(cut_off)], "n": CELL_REQUESTS}


def _build_summary(
    *,
    run_id: str,
    ancla: float,
    cut_off: int | None,
    cut_off_note: str,
    volleys: list[dict],
    k_max: int,
    table_version: str,
    runs_dir: pathlib.Path,
    batches_dir: pathlib.Path,
) -> dict:
    """The workstream's dataset doc, computed from the raw lines alone.

    Cells join the batch bracket with its request lines: dpp and wall-clock come
    from the batch line, completed tasks from the checker verdicts, and the
    effective cost per task from Δpp(weekly) × USD/pp ÷ tasks. The doc covers
    the whole run_id — every model's cells recorded so far (one manifest per
    level; the cut-off is a per-key property) — so `models` lists them and each
    cell carries its own model.
    """
    usd = usd_per_pp(ancla)
    batches = read_jsonl(batches_dir / f"batches-{run_id}.jsonl")
    requests = read_jsonl(runs_dir / f"requests-{run_id}.jsonl")
    celdas = []
    por_batch: dict[str | None, list[dict]] = {}
    for r in requests:
        por_batch.setdefault(r.get("batch_id"), []).append(r)
    # (model, k): the doc covers every model of the run, grouped and stable
    for b in sorted(batches, key=lambda x: (x["model"], x["k"])):
        lineas = por_batch.get(b.get("batch_id"), [])
        aceptadas = sum(1 for r in lineas if r.get("http") == 200)
        completadas = sum(1 for r in lineas if r.get("checker") == "pass")
        dpp_weekly = b.get("dpp_weekly")
        # The bracket's Δpp bills only the requests the endpoint ACCEPTED (a
        # 429 never lands on the meter), so the attempted-task cost divides by
        # the accepted count, not the planned n — the same denominator
        # analyze's k-axis sweep reads from the same raw lines.
        coste_intento = (
            dpp_weekly * usd / aceptadas
            if isinstance(dpp_weekly, (int, float)) and aceptadas
            else None
        )
        coste_completada = (
            dpp_weekly * usd / completadas
            if isinstance(dpp_weekly, (int, float)) and completadas
            else None
        )
        celdas.append(
            {
                "k": b["k"],
                "batch_id": b["batch_id"],
                "model": b["model"],
                "re_anchored": "re-anchored" in (b.get("notes") or ""),
                "n": b["n"],
                "accepted": aceptadas,
                "completed": completadas,
                "dpp_session": b.get("dpp_session"),
                "dpp_weekly": dpp_weekly,
                "wall_clock_s": b.get("wall_clock_s"),
                "cost_per_attempted_task_usd": coste_intento,
                "cost_per_completed_task_usd": coste_completada,
                "notes": b.get("notes") or "",
            }
        )
    return {
        "run_id": run_id,
        "level": ANCHOR_LEVEL,
        "kind": "concurrency",
        "models": sorted({c["model"] for c in celdas}),
        "table_version": table_version,
        "protocol_version": PROTOCOL_VERSION,
        "ancla": ancla,
        "usd_per_pp": usd,
        "probe": {
            "k_from": PROBE_K_FROM,
            "k_max": k_max,
            "cut_off": cut_off,
            "cut_off_note": cut_off_note,
            "volleys": volleys,
        },
        "cells": celdas,
        "notes": (
            "effective cost per task = dpp_weekly x usd_per_pp / tasks (the weekly window "
            "anchors the study), per attempted and per completed (checker-passed) task; "
            "computed from the raw batches/*.jsonl + runs/*.jsonl lines. The probe's volleys "
            "are not bracketed: their spend is flushed (settle + meter read) before the first "
            "cell bracket and is never attributed to a cell."
        ),
    }


async def _run_async(cfg: dict) -> dict:
    base: pathlib.Path = cfg["base"]
    model: str = cfg["model"]
    runs_dir = base / "runs"
    batches_dir = base / "batches"
    runs_dir.mkdir(parents=True, exist_ok=True)
    batches_dir.mkdir(parents=True, exist_ok=True)
    ruta_manifest = runs_dir / f"manifest-{ANCHOR_LEVEL}-concurrency.json"

    manifiesto = open_workstream_manifest(
        runs_dir,
        # the workstream's own identity: `status` renders it from the manifest doc,
        # while the batch/request lines carry the anchor's density class (T1)
        level=f"{ANCHOR_LEVEL}-concurrency",
        run_id_prefix=f"{ANCHOR_LEVEL}-cc",
        cfg=cfg,  # k=None: the cells carry their own k; the probe is a per-key property
    )
    run_id = manifiesto.run_id
    ruta_probe = runs_dir / f"probe-{run_id}.jsonl"
    modelo_api = cfg.get("model_map", {}).get(model, model)
    emit = cfg["emit"]

    client = OllamaCloud(transport=cfg["transport"])
    try:
        # ---- phase 0: the billing canary (once per run) ----
        # The k-cells are measured brackets under the cache-free lane: the lane
        # is proven before anything bills under it, exactly like `bench run`.
        # The canary's own spend is unbracketed; the flush below baselines it
        # together with the probe's before the first cell's pre-read.
        contexto_canary = BatchContext(
            base=base,
            manifiesto=manifiesto,
            cfg=cfg,
            rutas_requests=runs_dir / f"requests-{run_id}.jsonl",
            ruta_batches=batches_dir / f"batches-{run_id}.jsonl",
            ruta_canary=runs_dir / f"canary-{run_id}.jsonl",
        )
        await _ensure_canary(client, ctx=contexto_canary, cfg=cfg, level=ANCHOR_LEVEL)

        # ---- phase 1: the probe (once per run: the cut-off is a per-key property) ----
        probe_doc = manifiesto.doc.get("probe") or {}
        probe_ran_ahora = False
        if probe_doc.get("status") == "done":
            cut_off = probe_doc.get("cut_off")
            cut_off_note = probe_doc.get("cut_off_note", "")
            volleys = probe_doc.get("volleys", [])
            if emit:
                emit(
                    f"probe: already measured for this run - reusing cut-off {cut_off} "
                    f"({cut_off_note}); delete {ruta_manifest.name} to re-probe"
                )
        else:
            probe_ran_ahora = True
            probe_doc = {"status": "in_flight", "k_max": cfg["k_max"], "at": time.time()}
            manifiesto.doc["probe"] = probe_doc
            manifiesto.save()
            try:
                cut_off, cut_off_note, volleys = await _sweep(
                    client,
                    model,
                    modelo_api,
                    k_max=cfg["k_max"],
                    run_id=run_id,
                    ruta_probe=ruta_probe,
                    table_version=cfg["table_version"],
                    emit=emit,
                )
            except Exception as e:  # noqa: BLE001 - a probe failure aborts the invocation
                raise RunnerError(f"probe sweep failed: {type(e).__name__}: {e}") from None
            probe_doc.update(
                {
                    "status": "done",
                    "cut_off": cut_off,
                    "cut_off_note": cut_off_note,
                    "k_from": PROBE_K_FROM,
                    "volleys": volleys,
                    "at": time.time(),
                }
            )
            manifiesto.doc["probe"] = probe_doc
            manifiesto.save()

        # ---- phase 2: the k-cells as bracketed batches ----
        specs = _cell_specs(run_id=run_id, model=model, cut_off=cut_off)
        # The cell plan (ks + n) is pinned once per run_id: a harness change to
        # CELL_KS/CELL_REQUESTS between invocations must not append a mixed plan
        # under one run_id — the same drift the runner's k guard prevents for k.
        plan = cell_plan(cut_off)
        plan_previo = manifiesto.doc.get("cell_plan")
        if plan_previo is None:
            manifiesto.doc["cell_plan"] = plan
            manifiesto.save()
        elif plan_previo != plan:
            raise RunnerError(
                f"manifest {ruta_manifest.name} records cell plan {plan_previo!r} but this "
                f"invocation would run {plan!r} - the cell plan may not drift inside one "
                "run_id - keep the datasets apart"
            )
        pendientes = [s for s in specs if manifiesto.status(s.batch_id) is None]

        cerradas = [
            e
            for e in manifiesto.doc["batches"].values()
            if isinstance(e, dict) and e.get("status") in ("done", "aborted")
        ]
        if pendientes and (probe_ran_ahora or not cerradas):
            # The probe's own spend is unbracketed: the registration loop (poll
            # until the meter is stable) so its usage lands before the first
            # cell's pre-read and no cell's Δpp absorbs it. Skipped when the
            # probe was reused AND some cell already closed (that close proves
            # the flush already ran for this run). A flush that never read the
            # meter is no flush at all: the baselining failed. A capped one is
            # no better: the meter lag (60-90 s documented) can outlast the cap,
            # and a capped "post" predates the probe's registration — accepting
            # it would baseline the cells before the probe's spend lands and
            # silently absorb it into the first cell's Δpp. Only a stable flush
            # proves the spend predates the cells.
            flush = await _registration_settle(
                client, primera=None, cap_s=cfg["settle_s"], poll_s=cfg["settle_poll_s"]
            )
            if flush["exit"] != "stable" or flush["post"] is None:
                raise RunnerError(
                    f"probe flush did not stabilize ({flush['error'] or flush['exit'] or 'no meter read landed'}) - "
                    "the probe's spend cannot be proven to predate the cells' brackets; "
                    "raise --settle-s and re-run"
                )
            if emit:
                emit(
                    "probe: flush read ok - the probe's spend predates the cells' brackets "
                    f"({flush['reads']} reads, {flush['exit']})"
                )

        previa = manifiesto.doc.get("planned")
        manifiesto.doc["planned"] = max(
            previa if isinstance(previa, int) else 0,
            len(manifiesto.doc["batches"]) + len(pendientes),
            len(specs),
        )
        manifiesto.save()

        contexto = BatchContext(
            base=base,
            manifiesto=manifiesto,
            cfg=cfg,
            rutas_requests=runs_dir / f"requests-{run_id}.jsonl",
            ruta_batches=batches_dir / f"batches-{run_id}.jsonl",
        )
        hechos = omitidos = 0
        for spec in specs:
            estado_celda = manifiesto.status(spec.batch_id)
            if estado_celda in ("done", "in_flight", "aborted"):
                omitidos += 1
                if emit:
                    emit(
                        f"resume: cell k={spec.k} ({spec.batch_id}) {estado_celda} from an "
                        "earlier attempt - skipped"
                        + (
                            ", never silently retried"
                            if estado_celda == "in_flight"
                            else "; its spend is already in the dataset"
                        )
                    )
                continue
            if emit:
                emit(f"cells: k={spec.k}" + (f" ({spec.plan_note})" if spec.plan_note else ""))
            resultado = await _execute_batch(client, spec, ctx=contexto)
            hechos += 1
            if emit:
                emit(
                    f"[{hechos + omitidos}/{len(specs)}] {ANCHOR_WORKLOAD}/{model} "
                    f"k{spec.k}: {resultado.ok}/{resultado.intentados} ok, "
                    f"dpp_session={resultado.dpp_session}, "
                    f"wall_clock={resultado.wall_clock_s}s"
                )
    finally:
        await client.aclose()

    resumen = _build_summary(
        run_id=run_id,
        ancla=cfg["ancla"],
        cut_off=cut_off,
        cut_off_note=cut_off_note,
        volleys=volleys,
        k_max=probe_doc.get("k_max", cfg["k_max"]),
        table_version=cfg["table_version"],
        runs_dir=runs_dir,
        batches_dir=batches_dir,
    )
    ruta_resumen = runs_dir / f"concurrency-{run_id}.json"
    ruta_resumen.write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    return resumen


def run_probe(
    base,
    *,
    model: str,
    k_max: int,
    settle_s: float,
    settle_poll_s: float = 5.0,
    ancla: float,
    table_version: str,
    catalog: dict | None = None,
    model_map: dict[str, str] | None = None,
    transport=None,
    emit=print,
) -> dict:
    """Executes the probe + the re-anchored k-cells; raises RunnerError on abort.

    The k-cells are measured brackets: they run under the cache-free lane
    (`lane=True` records the nonce spec on the workstream's manifest). The probe
    itself is exempt — a locator, not a measured cell — and its volleys fire
    unsalted through `_burst`'s explicit no-salt path."""
    cfg = {
        "base": pathlib.Path(base),
        "model": model,
        "k_max": k_max,
        "settle_s": settle_s,
        "settle_poll_s": settle_poll_s,
        "ancla": ancla,
        "table_version": table_version,
        "catalog": catalog,
        "model_map": model_map or {},
        "transport": transport,
        "emit": emit,
        "k": None,  # _check_drift: the cells carry their own k
        "lane": True,  # the cells are measured; the probe's volleys stay exempt
    }
    return asyncio.run(_run_async(cfg))
