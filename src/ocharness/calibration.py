"""The cache-calibration workstream (methodology v1 §7): prefix replays + the S1 override.

`bench calibrate-cache` replays one fixed ~20K prefix (the long_context register's
opening span, shared provenance) per T2-slate model in three bracketed batches:

1. **Cold reference** (`cache_cold`, 1 request): the prefix's first send — the
   baseline for the reported tokens, the bracketed Δpp and TTFT.
2. **Intra-batch replay** (`cache_intra`, r=4): the same prefix fired back to
   back at k=1. The first request (re)primes; the three after it carry the
   intra-batch hit evidence.
3. **Spaced replays** (`cache_spaced`, 3): the same prefix again at increasing
   offsets (targets 5/30/90 s, `--spaced-gaps`) inside one bracket — how far the
   cache persists. The bracket's settle already puts its first replay ~settle
   seconds after the last refresh; the ladder keeps its shape above that floor,
   and every replay's actual age is derived from the raw stamps, never assumed.

The three signals (reported tokens, Δpp, TTFT) land in the standard raw
datasets — nothing new to trust. The calibration doc
(`runs/calibration-<run_id>.json`) is derived from those lines alone and
identifies per model whether a **cache exists** (yes / no / unknown), its
**persistence horizon**, the **effective hit rate**, and the **unmaterialized
paper discounts** (a table that prices cached_input below input while the
measurement finds no caching).

`resolve_s()` is analyze's seam: the per-model hit rate under the agreed
precedence — **measurement wins** over S1 when conclusive, **S0 is the floor**
when the measurement conclusively finds no cache, and an inconclusive
measurement keeps the S1 assumption, marked as such.

Conclusive rule (>2 ticks + non-overlapping IQR): a measured hit rate replaces
S1 only when the meter's own evidence resolves beyond its resolution — the
cold-vs-warm Δpp gap exceeds 2 ticks (0.2 pp) — and the per-replay hit-rate
estimates agree (their IQR excludes zero). The estimates read the token
evidence: a reported cache-hit count when the API tracks one, else the
prompt-eval drop against the cold send; without either, the Δpp proxy (1 −
warm-per-request / cold, one estimate per warm bracket) stands in, same rule.
A conclusive "no" is the mirror image: a deployment that tracks cache hits
reporting zeros on every replay, with no Δpp discount. Anything else is
inconclusive.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import statistics

from . import fixtures
from .client import PROTOCOL_VERSION, OllamaCloud
from .concurrency import _read_jsonl
from .runner import (
    BatchContext,
    BatchSpec,
    RunnerError,
    _execute_batch,
    batch_id,
    open_workstream_manifest,
)

CACHE_LEVEL = "T2-cache"  # the workstream's manifest identity (status renders it)
COLD_WORKLOAD = "cache_cold"
INTRA_WORKLOAD = "cache_intra"
SPACED_WORKLOAD = "cache_spaced"
CACHE_REPEATS = 4  # the intra-batch replay count (methodology v1 §7's r=4)
SPACED_TARGETS = (5.0, 30.0, 90.0)  # the spaced replays' cumulative offsets (s)
TICK_PP = 0.1  # one 0.001 meter tick, in percentage points
CONCLUSIVE_TICKS = 2.0  # the override rule's resolution floor (>2 ticks)
# Relative float-residue band around a tick boundary, for COMPARISON logic
# only (the deltas themselves stay exact — methodology v1.1 §4): a meter delta
# is a difference of tick-quantized readings, so a true exact-boundary value
# lands within ~1e-13 relative of the threshold; anything genuinely past it is
# orders of magnitude beyond this band.
TICK_BAND = 1e-9
_WORKLOADS = (COLD_WORKLOAD, INTRA_WORKLOAD, SPACED_WORKLOAD)


@dataclasses.dataclass(frozen=True)
class EffectiveS:
    """The effective hit rate for one model — what `analyze` extrapolates with."""

    model: str
    s: float
    source: str  # "measured" | "assumed"
    conclusive: bool
    measured_hit_rate: float | None
    paper_discount_declared: bool
    note: str


def _bracket_specs(*, run_id: str, model: str) -> dict[str, BatchSpec]:
    """The model's three bracket specs (the spaced one carries no gaps yet: the
    sleeps are fixed once the intra bracket's raw stamps exist)."""
    specs: dict[str, BatchSpec] = {}
    for workload, n in (
        (COLD_WORKLOAD, 1),
        (INTRA_WORKLOAD, CACHE_REPEATS),
        (SPACED_WORKLOAD, len(SPACED_TARGETS)),
    ):
        specs_requeridos = fixtures.build("T2", workload, n)
        specs[workload] = BatchSpec(
            level="T2",
            batch_id=batch_id(run_id, "T2", workload, model, 1, 1),
            workload=workload,
            model=model,
            rep=1,
            k=1,
            n=n,
            fixture_hash=fixtures.fixture_hash(specs_requeridos),
        )
    return specs


def _dp_ventana(batch: dict | None) -> float | None:
    """The bracket's Δpp on the study's window (weekly), session as the fallback."""
    if not batch:
        return None
    semanal = batch.get("dpp_weekly")
    if isinstance(semanal, (int, float)):
        return float(semanal)
    sesion = batch.get("dpp_session")
    return float(sesion) if isinstance(sesion, (int, float)) else None


def _ttft_s(rec: dict) -> float | None:
    """The replay's TTFT in seconds: the request lines' exact span (the
    precision policy keeps every persisted float unrounded)."""
    inicio, primero = rec.get("t_start"), rec.get("t_first_chunk")
    if isinstance(inicio, (int, float)) and isinstance(primero, (int, float)):
        return primero - inicio
    return None


def _hit_de(rec: dict, tin_frio: int | None) -> bool | None:
    """Whether one replay was served from cache, from the token evidence alone.

    A reported cache-hit count decides outright; without the field, a warm
    `prompt_eval_count` below the cold send's is the caching Ollama's own
    client shows (only the prefix tail re-evaluated). A zero count on either
    side is broken telemetry, not evidence; neither signal present: None.
    """
    tin, tcache = rec.get("tok_in"), rec.get("tok_cached")
    if isinstance(tin, int) and isinstance(tcache, int):
        return tcache > 0
    if isinstance(tin, int) and tin > 0 and isinstance(tin_frio, int) and tin_frio > 0:
        return tin < tin_frio
    return None


def _iqr_de(estimaciones: list[float]) -> tuple[float, float] | None:
    """The estimates' IQR (q25, q75); None below the two points it needs."""
    if len(estimaciones) < 2:
        return None
    q = statistics.quantiles(estimaciones, n=4)
    return q[0], q[2]


def resolve_s(calibracion, modelos, *, default_s: float = 0.5) -> dict[str, EffectiveS]:
    """The analyze seam: each model's effective hit rate under the agreed precedence.

    Measurement wins over the S1 assumption when the calibration was conclusive;
    a conclusive absence of caching puts the model at the S0 floor (0.0); an
    inconclusive measurement — or a model the calibration never measured — keeps
    the S1 assumption, marked as such so no number ever reads as measured when
    it is not. Malformed readings resolve as assumed, never as a traceback.
    """
    lecturas = calibracion.get("readings") if isinstance(calibracion, dict) else None
    lecturas = lecturas if isinstance(lecturas, dict) else {}
    resueltos: dict[str, EffectiveS] = {}
    for modelo in modelos:
        entrada = lecturas.get(modelo)
        if not isinstance(entrada, dict):
            resueltos[modelo] = EffectiveS(
                modelo,
                default_s,
                "assumed",
                False,
                None,
                False,
                "no calibration data - the S1 assumption is kept",
            )
            continue
        descuento = entrada.get("paper_discount")
        declarado = bool(isinstance(descuento, dict) and descuento.get("declared"))
        tasa = entrada.get("hit_rate")
        if entrada.get("conclusive") and isinstance(tasa, (int, float)):
            medida = max(0.0, min(1.0, float(tasa)))
            if medida == 0.0:
                resueltos[modelo] = EffectiveS(
                    modelo,
                    0.0,
                    "measured",
                    True,
                    medida,
                    declarado,
                    "measured no caching - the effective hit rate sits at the S0 floor",
                )
            else:
                resueltos[modelo] = EffectiveS(
                    modelo,
                    medida,
                    "measured",
                    True,
                    medida,
                    declarado,
                    f"the measured hit rate replaces the S1 assumption ({default_s:g})",
                )
        else:
            resueltos[modelo] = EffectiveS(
                modelo,
                default_s,
                "assumed",
                False,
                tasa if isinstance(tasa, (int, float)) else None,
                declarado,
                "inconclusive calibration - the S1 assumption is kept and marked",
            )
    return resueltos


def _analyze_model(
    modelo: str,
    brackets: dict[str, dict],
    peticiones: dict[str, list[dict]],
    *,
    tabla,
) -> dict:
    """One model's calibration reading, derived from its raw lines alone.

    Every signal is null-safe: a bracket that never closed (or closed without a
    readable meter payload) leaves its evidence out and the model inconclusive
    with a note, never a traceback.
    """
    frio = brackets.get(COLD_WORKLOAD)
    intra = brackets.get(INTRA_WORKLOAD)
    espaciada = brackets.get(SPACED_WORKLOAD)
    lineas_frio = peticiones.get(COLD_WORKLOAD, [])
    lineas_intra = peticiones.get(INTRA_WORKLOAD, [])
    lineas_espaciada = peticiones.get(SPACED_WORKLOAD, [])

    notas = [
        f"the {w} bracket never closed - no reading from it"
        for w, b in ((COLD_WORKLOAD, frio), (INTRA_WORKLOAD, intra), (SPACED_WORKLOAD, espaciada))
        if b is None
    ]

    dp_frio = _dp_ventana(frio)
    dp_intra = _dp_ventana(intra)
    dp_espaciada = _dp_ventana(espaciada)

    # The warm replays: the intra bracket's requests after its primer, plus every
    # spaced replay (each re-sends the prefix an earlier bracket refreshed). The
    # hit rate samples come from the token evidence: a reported cache-hit count
    # when the API tracks one, else the prompt-eval drop against the cold send.
    tin_frio = lineas_frio[0].get("tok_in") if lineas_frio else None
    calientes = lineas_intra[1:] + lineas_espaciada
    muestras: list[float] = []
    explicitas: list[float] = []  # samples a reported field vouched for (its zeros too)
    uso_campo = uso_caida = False
    for rec in calientes:
        tin, tcache = rec.get("tok_in"), rec.get("tok_cached")
        if isinstance(tin, int) and isinstance(tcache, int) and tin + tcache > 0:
            muestras.append(tcache / (tin + tcache))
            explicitas.append(muestras[-1])
            uso_campo = True
        elif (
            isinstance(tin, int)
            and tin > 0  # a zero warm report is broken telemetry, not a perfect hit
            and isinstance(tin_frio, int)
            and tin_frio > 0
        ):
            muestras.append(max(0.0, 1.0 - tin / tin_frio))
            uso_caida = True

    # The Δpp signal: the cold per-request cost against the intra bracket's
    # average (its primer plus warm replays), in ticks of the meter's resolution.
    senal_pp = senal_ticks = None
    if dp_frio is not None and dp_frio > 0 and dp_intra is not None and lineas_intra:
        senal_pp = dp_frio - dp_intra / len(lineas_intra)
        senal_ticks = senal_pp / TICK_PP

    proxy: dict | None = None
    if not muestras and dp_frio is not None and dp_frio > 0:
        estimadores = []
        for dp, n in ((dp_intra, len(lineas_intra)), (dp_espaciada, len(lineas_espaciada))):
            if dp is not None and n:
                estimadores.append(max(0.0, 1.0 - (dp / n) / dp_frio))
        if len(estimadores) >= 2:
            q = statistics.quantiles(estimadores, n=4)
            proxy = {
                "estimates": list(estimadores),
                "iqr": [q[0], q[2]],
            }

    estimaciones = muestras if muestras else (proxy["estimates"] if proxy else [])
    iqr = _iqr_de(estimaciones)
    sobre_cero = iqr is not None and iqr[0] > 0
    # A conclusive "no" needs the field's own zeros: a reported hit count of 0
    # on every replay is evidence of absence, while an unchanged prompt-eval
    # count only says the API reveals nothing (caching could still exist,
    # invisible in both signals) — that stays unknown, and S1 stays marked.
    explicitos_cero = len(explicitas) >= 2 and all(m == 0.0 for m in explicitas)

    conclusiva: str | None = None
    # The tick comparisons read the unrounded signal through the residue band:
    # a gap of exactly 2 ticks must resolve as "no" (the meter's quantum), not
    # as "yes" on a 2.0000000000000084.
    if senal_ticks is not None and senal_ticks > CONCLUSIVE_TICKS * (1 + TICK_BAND) and sobre_cero:
        conclusiva = "yes"
    elif (
        senal_ticks is not None
        and explicitos_cero
        and senal_ticks <= CONCLUSIVE_TICKS * (1 + TICK_BAND)
    ):
        conclusiva = "no"

    if conclusiva == "yes":
        tasa: float | None = statistics.median(estimaciones)
    elif conclusiva == "no":
        tasa = 0.0
    else:
        tasa = None

    existe = (
        "yes"
        if conclusiva == "yes" or any(m > 0 for m in muestras)
        else "no"
        if conclusiva == "no"
        else "unknown"
    )
    base_estimacion = (
        "reported cache-hit tokens"
        if uso_campo
        else "prompt-eval drop"
        if uso_caida
        else "dpp proxy"
        if proxy
        else None
    )

    # Persistence: which spaced replay offsets still hit, from the raw stamps.
    # A horizon needs a cache whose existence the evidence established first —
    # an unknown reading reports no horizon at all.
    persistencia: str | None = None
    if conclusiva == "no":
        persistencia = "none observed"
    elif existe == "yes" and lineas_espaciada and lineas_intra:
        golpes = [_hit_de(r, tin_frio) for r in lineas_espaciada]
        referencia = lineas_intra[-1].get("t_total")
        edades = [
            r["t_start"] - referencia
            if isinstance(referencia, (int, float)) and isinstance(r.get("t_start"), (int, float))
            else None
            for r in lineas_espaciada
        ]
        if edades[-1] is not None and all(g is True for g in golpes):
            persistencia = f">= {edades[-1]:g} s"
        elif golpes[0] is False:
            if edades[0] is not None:
                persistencia = f"below {edades[0]:g} s"
        elif (
            any(g is True for g in golpes)
            and any(g is False for g in golpes)
            and all(e is not None for e in edades)
        ):
            ultimo_golpe = max(i for i, g in enumerate(golpes) if g is True)
            primer_fallo = min(i for i, g in enumerate(golpes) if g is False)
            persistencia = f"between {edades[ultimo_golpe]:g} and {edades[primer_fallo]:g} s"

    declarado = tabla.rate(modelo).has_cache_discount
    materializado = {"yes": True, "no": False}.get(existe)

    def _evidencia(rec: dict) -> dict:
        return {
            "tok_in": rec.get("tok_in"),
            "tok_cached": rec.get("tok_cached"),
            "ttft_s": _ttft_s(rec),
        }

    senales = {
        COLD_WORKLOAD: {
            "batch_id": frio.get("batch_id") if frio else None,
            "dpp": dp_frio,
            "requests": [_evidencia(r) for r in lineas_frio],
        },
        INTRA_WORKLOAD: {
            "batch_id": intra.get("batch_id") if intra else None,
            "dpp": dp_intra,
            "replays": [_evidencia(r) for r in lineas_intra[1:]],
        },
        SPACED_WORKLOAD: {
            "batch_id": espaciada.get("batch_id") if espaciada else None,
            "dpp": dp_espaciada,
            "replays": [{**_evidencia(r), "hit": _hit_de(r, tin_frio)} for r in lineas_espaciada],
        },
    }
    return {
        "cache_exists": existe,
        "persistence": persistencia,
        "hit_rate": tasa,
        "hit_rate_basis": base_estimacion,
        "conclusive": conclusiva is not None,
        "rule": {
            "dp_signal_pp": senal_pp,
            "dp_signal_ticks": senal_ticks,
            "conclusive_ticks_required": CONCLUSIVE_TICKS,
            "estimates": list(muestras) if muestras else (proxy or {}).get("estimates"),
            "iqr": list(iqr) if iqr else (proxy or {}).get("iqr"),
            "estimate_basis": base_estimacion,
        },
        "signals": senales,
        "paper_discount": {"declared": declarado, "materialized": materializado},
        "notes": "; ".join(notas),
    }


def _build_summary(
    *,
    run_id: str,
    runs_dir: pathlib.Path,
    batches_dir: pathlib.Path,
    table_version: str,
    tabla,
) -> dict:
    """The calibration doc, computed from the raw batches + requests lines alone.

    It covers every model the run_id ever calibrated — later invocations extend
    it, so `analyze` reads one doc per run and finds each model's reading there.
    """
    batches = _read_jsonl(batches_dir / f"batches-{run_id}.jsonl")
    requests = _read_jsonl(runs_dir / f"requests-{run_id}.jsonl")
    por_modelo: dict[str, dict[str, dict]] = {}
    for b in batches:
        if b.get("workload") in _WORKLOADS:
            por_modelo.setdefault(b.get("model"), {})[b.get("workload")] = b

    lecturas: dict[str, dict] = {}
    for modelo, brackets in sorted(por_modelo.items()):
        peticiones: dict[str, list[dict]] = {}
        for w in _WORKLOADS:
            batch = brackets.get(w)
            lineas = [r for r in requests if batch and r.get("batch_id") == batch.get("batch_id")]
            peticiones[w] = sorted(lineas, key=lambda r: r.get("req_id") or "")
        analisis = _analyze_model(modelo, brackets, peticiones, tabla=tabla)
        sellos = [
            r.get("t_total")
            for w in _WORKLOADS
            for r in peticiones[w]
            if isinstance(r.get("t_total"), (int, float))
        ]
        analisis["calibrated_at"] = max(sellos) if sellos else None
        lecturas[modelo] = analisis
    sin_materializar = sorted(
        m
        for m, a in lecturas.items()
        if a["paper_discount"]["declared"] and a["paper_discount"]["materialized"] is False
    )
    return {
        "run_id": run_id,
        "kind": "cache-calibration",
        "level": CACHE_LEVEL,
        "models": sorted(lecturas),
        "readings": lecturas,
        "table_version": table_version,
        "protocol_version": PROTOCOL_VERSION,
        "unmaterialized_paper_discounts": sin_materializar,
        "notes": (
            "derived from the raw batches/*.jsonl + runs/*.jsonl lines alone. The "
            "override rule: a measured hit rate replaces S1 when conclusive (>2 ticks "
            "of cold-vs-warm dp and a non-overlapping IQR); a conclusive absence puts "
            "the model at the S0 floor; an inconclusive measurement keeps the S1 "
            "assumption, marked. Unmaterialized paper discounts are models whose "
            "table declares a cached-input discount while the measurement found none."
        ),
    }


async def _run_async(cfg: dict) -> dict:
    base: pathlib.Path = cfg["base"]
    runs_dir = base / "runs"
    batches_dir = base / "batches"
    runs_dir.mkdir(parents=True, exist_ok=True)
    batches_dir.mkdir(parents=True, exist_ok=True)

    manifiesto = open_workstream_manifest(
        runs_dir,
        level=CACHE_LEVEL,
        run_id_prefix="T2-cache",
        cfg=cfg,  # k=1: every calibration bracket fires at k=1, the replays' design
    )
    run_id = manifiesto.run_id
    ruta_manifest = manifiesto.ruta

    # The gap plan (spaced targets + repeats) is pinned once per run_id: mixing
    # ladders under one dataset would make the persistence readings incomparable.
    plan_gaps = {"targets": list(cfg["spaced_ages"]), "repeats": CACHE_REPEATS}
    plan_previo = manifiesto.doc.get("gap_plan")
    if plan_previo is None:
        manifiesto.doc["gap_plan"] = plan_gaps
        manifiesto.save()
    elif plan_previo != plan_gaps:
        raise RunnerError(
            f"manifest {ruta_manifest.name} records gap plan {plan_previo!r} but this "
            f"invocation would run {plan_gaps!r} - the spacing may not drift inside one "
            "run_id - keep the datasets apart"
        )

    emit = cfg["emit"]
    rutas_requests = runs_dir / f"requests-{run_id}.jsonl"
    contexto = BatchContext(
        base=base,
        manifiesto=manifiesto,
        cfg=cfg,
        rutas_requests=rutas_requests,
        ruta_batches=cfg["base"] / "batches" / f"batches-{run_id}.jsonl",
    )
    client = OllamaCloud(transport=cfg["transport"])
    try:
        for modelo in cfg["models"]:
            specs = _bracket_specs(run_id=run_id, model=modelo)
            estados = {w: manifiesto.status(s.batch_id) for w, s in specs.items()}
            if all(e in ("done", "aborted") for e in estados.values()):
                cerrados = sum(1 for e in estados.values() if e == "done")
                emit(
                    f"calibrate: {modelo} already calibrated for this run "
                    f"({cerrados} closed, {len(estados) - cerrados} aborted) - skipped"
                )
                continue
            if any(e == "in_flight" for e in estados.values()):
                emit(
                    f"calibrate: {modelo} has an in_flight bracket from an interrupted "
                    "run - model skipped, never silently retried"
                )
                continue

            # ---- the cold reference, then the intra-batch replay ----
            for w in (COLD_WORKLOAD, INTRA_WORKLOAD):
                if estados[w] is not None:  # closed earlier: its spend is in the dataset
                    emit(f"calibrate: {modelo}/{w} closed in an earlier attempt - skipped")
                    continue
                resultado = await _execute_batch(client, specs[w], ctx=contexto)
                emit(
                    f"calibrate: {modelo}/{w}: {resultado.ok}/{resultado.intentados} ok, "
                    f"dpp={resultado.dpp_session}"
                )
            estados = {w: manifiesto.status(s.batch_id) for w, s in specs.items()}
            if estados[INTRA_WORKLOAD] != "done":
                raise RunnerError(
                    f"calibrate: {modelo}: the intra bracket never closed (an aborted "
                    "bracket is never retried), so the spaced ladder has no refresh to "
                    "sit on - delete manifest-T2-cache.json to re-calibrate this run "
                    "cleanly, or keep the incomplete evidence"
                )

            # ---- the spaced replays, at their cumulative offsets ----
            if estados[SPACED_WORKLOAD] is not None:
                emit(
                    f"calibrate: {modelo}/{SPACED_WORKLOAD} closed in an earlier attempt - skipped"
                )
                continue
            objetivo = cfg["spaced_ages"]
            gaps = (objetivo[0],) + tuple(
                objetivo[i] - objetivo[i - 1] for i in range(1, len(objetivo))
            )
            espec_spaced = dataclasses.replace(
                specs[SPACED_WORKLOAD],
                gap_s=gaps,
                plan_note=(
                    "spaced replays, cumulative offsets "
                    + ", ".join(f"{o:g}" for o in objetivo)
                    + " s"
                ),
            )
            resultado = await _execute_batch(client, espec_spaced, ctx=contexto)
            emit(
                f"calibrate: {modelo}/{SPACED_WORKLOAD}: {resultado.ok}/{resultado.intentados} "
                f"ok, dpp={resultado.dpp_session}"
            )
    finally:
        await client.aclose()

    manifiesto.doc["planned"] = max(
        manifiesto.doc.get("planned") or 0,
        len(manifiesto.doc["batches"]),
        len(_WORKLOADS) * len(cfg["models"]),
    )
    manifiesto.save()

    resumen = _build_summary(
        run_id=run_id,
        runs_dir=runs_dir,
        batches_dir=base / "batches",
        table_version=cfg["table_version"],
        tabla=cfg["tabla"],
    )
    ruta_resumen = runs_dir / f"calibration-{run_id}.json"
    ruta_resumen.write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    return resumen


def run_calibration(
    base,
    *,
    models: list[str],
    spaced_ages: tuple[float, ...] = SPACED_TARGETS,
    settle_s: float,
    table_version: str,
    tabla,
    catalog: dict | None = None,
    model_map: dict[str, str] | None = None,
    transport=None,
    emit=print,
) -> dict:
    """Executes the calibration replays per model; raises RunnerError on abort."""
    cfg = {
        "base": pathlib.Path(base),
        "models": models,
        "spaced_ages": tuple(float(a) for a in spaced_ages),
        "settle_s": settle_s,
        "table_version": table_version,
        "tabla": tabla,
        "catalog": catalog,
        "model_map": model_map or {},
        "transport": transport,
        "emit": emit,
        "k": 1,  # _check_drift pins the brackets' k
    }
    return asyncio.run(_run_async(cfg))
