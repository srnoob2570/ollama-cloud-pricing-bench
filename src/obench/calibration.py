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
   cache persists. The offsets are cumulative from the prefix's last refresh
   (the ladder keeps its shape above the bracket's own registration time), and
   every replay's actual age is derived from the raw stamps, never assumed.

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
import pathlib
import statistics

from . import fixtures
from .client import PROTOCOL_VERSION
from .meter import TICK_BAND, TICK_PP  # the meter's resolution, in percentage points
from .pricing import TableError
from .runner import BatchSpec, RunnerError, Workstream, batch_id, open_workstream
from .schema import read_jsonl

CACHE_LEVEL = "T2-cache"  # the workstream's manifest identity (status renders it)
COLD_WORKLOAD = "cache_cold"
INTRA_WORKLOAD = "cache_intra"
SPACED_WORKLOAD = "cache_spaced"
CACHE_REPEATS = 4  # the intra-batch replay count (methodology v1 §7's r=4)
SPACED_TARGETS = (5.0, 30.0, 90.0)  # the spaced replays' cumulative offsets (s)
CONCLUSIVE_TICKS = 2.0  # the override rule's resolution floor (>2 ticks)
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


def _dp_window(batch: dict | None) -> float | None:
    """The bracket's Δpp on the study's window (weekly), session as the fallback."""
    if not batch:
        return None
    weekly = batch.get("dpp_weekly")
    if isinstance(weekly, (int, float)):
        return float(weekly)
    session = batch.get("dpp_session")
    return float(session) if isinstance(session, (int, float)) else None


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


def _iqr_de(estimates: list[float]) -> tuple[float, float] | None:
    """The estimates' IQR (q25, q75); None below the two points it needs."""
    if len(estimates) < 2:
        return None
    q = statistics.quantiles(estimates, n=4)
    return q[0], q[2]


def resolve_s(calibration, models, *, default_s: float = 0.5) -> dict[str, EffectiveS]:
    """The analyze seam: each model's effective hit rate under the agreed precedence.

    Measurement wins over the S1 assumption when the calibration was conclusive;
    a conclusive absence of caching puts the model at the S0 floor (0.0); an
    inconclusive measurement — or a model the calibration never measured — keeps
    the S1 assumption, marked as such so no number ever reads as measured when
    it is not. Malformed readings resolve as assumed, never as a traceback.
    """
    readings = calibration.get("readings") if isinstance(calibration, dict) else None
    readings = readings if isinstance(readings, dict) else {}
    resolved: dict[str, EffectiveS] = {}
    for model in models:
        input = readings.get(model)
        if not isinstance(input, dict):
            # No calibration data: the S1 assumption stands, nothing reported.
            s, font, reportada, declarado = default_s, "assumed", None, False
            note = "no calibration data - the S1 assumption is kept"
        else:
            descuento = input.get("paper_discount")
            declarado = bool(isinstance(descuento, dict) and descuento.get("declared"))
            tasa = input.get("hit_rate")
            if input.get("conclusive") and isinstance(tasa, (int, float)):
                medida = max(0.0, min(1.0, float(tasa)))
                s, font, reportada = medida, "measured", medida
                if medida == 0.0:
                    note = "measured no caching - the effective hit rate sits at the S0 floor"
                else:
                    note = f"the measured hit rate replaces the S1 assumption ({default_s:g})"
            else:
                # An inconclusive measurement keeps S1; the raw rate rides along
                # unclamped, never reading as measured.
                s, font = default_s, "assumed"
                reportada = tasa if isinstance(tasa, (int, float)) else None
                note = "inconclusive calibration - the S1 assumption is kept and marked"
        resolved[model] = EffectiveS(model, s, font, font == "measured", reportada, declarado, note)
    return resolved


def _hit_samples(
    calientes: list[dict], tin_frio: int | None
) -> tuple[list[float], list[float], bool, bool]:
    """The warm replays' hit-rate samples from the token evidence.

    A reported cache-hit count (its zeros too, a reported field vouches for
    them) when the API tracks one; else the prompt-eval drop against the cold
    send. Returns (samples, explicit samples, used-field, used-drop).
    """
    samples: list[float] = []
    explicitas: list[float] = []
    usage_field = uso_caida = False
    for rec in calientes:
        tin, tcache = rec.get("tok_in"), rec.get("tok_cached")
        if isinstance(tin, int) and isinstance(tcache, int) and tin + tcache > 0:
            samples.append(tcache / (tin + tcache))
            explicitas.append(samples[-1])
            usage_field = True
        elif (
            isinstance(tin, int)
            and tin > 0  # a zero warm report is broken telemetry, not a perfect hit
            and isinstance(tin_frio, int)
            and tin_frio > 0
        ):
            samples.append(max(0.0, 1.0 - tin / tin_frio))
            uso_caida = True
    return samples, explicitas, usage_field, uso_caida


def _conclusive(senal_ticks: float | None, above_zero: bool, explicit_zero: bool) -> str | None:
    """The conclusive rule over the unrounded tick signal: "yes", "no", or None.

    The tick comparisons read the unrounded signal through the residue band:
    a gap of exactly 2 ticks must resolve as "no" (the meter's quantum), not
    as "yes" on a 2.0000000000000084.
    """
    resolved = senal_ticks is not None and senal_ticks > CONCLUSIVE_TICKS * (1 + TICK_BAND)
    if resolved and above_zero:
        return "yes"
    if senal_ticks is not None and explicit_zero and not resolved:
        return "no"
    return None


def _persistence(
    spaced_lines: list[dict], intra_lines: list[dict], tin_frio: int | None
) -> str | None:
    """Which spaced replay offsets still hit, from the raw stamps: a horizon."""
    golpes = [_hit_de(r, tin_frio) for r in spaced_lines]
    reference = intra_lines[-1].get("t_total")
    edades = [
        r["t_start"] - reference
        if isinstance(reference, (int, float)) and isinstance(r.get("t_start"), (int, float))
        else None
        for r in spaced_lines
    ]
    if edades[-1] is not None and all(g is True for g in golpes):
        return f">= {edades[-1]:g} s"
    if golpes[0] is False:
        if edades[0] is not None:
            return f"below {edades[0]:g} s"
        return None
    if (
        any(g is True for g in golpes)
        and any(g is False for g in golpes)
        and all(e is not None for e in edades)
    ):
        ultimo_golpe = max(i for i, g in enumerate(golpes) if g is True)
        first_failure = min(i for i, g in enumerate(golpes) if g is False)
        return f"between {edades[ultimo_golpe]:g} and {edades[first_failure]:g} s"
    return None


def _analyze_model(
    model: str,
    brackets: dict[str, dict],
    requests_by_batch: dict[str, list[dict]],
    *,
    table,
) -> dict:
    """One model's calibration reading, derived from its raw lines alone.

    Every signal is null-safe: a bracket that never closed (or closed without a
    readable meter payload) leaves its evidence out and the model inconclusive
    with a note, never a traceback.
    """
    frio = brackets.get(COLD_WORKLOAD)
    intra = brackets.get(INTRA_WORKLOAD)
    espaciada = brackets.get(SPACED_WORKLOAD)
    cold_lines = requests_by_batch.get(COLD_WORKLOAD, [])
    intra_lines = requests_by_batch.get(INTRA_WORKLOAD, [])
    spaced_lines = requests_by_batch.get(SPACED_WORKLOAD, [])

    notes = [
        f"the {w} bracket never closed - no reading from it"
        for w, b in ((COLD_WORKLOAD, frio), (INTRA_WORKLOAD, intra), (SPACED_WORKLOAD, espaciada))
        if b is None
    ]

    dp_frio = _dp_window(frio)
    dp_intra = _dp_window(intra)
    dp_espaciada = _dp_window(espaciada)

    # The warm replays: the intra bracket's requests after its primer, plus every
    # spaced replay (each re-sends the prefix an earlier bracket refreshed).
    tin_frio = cold_lines[0].get("tok_in") if cold_lines else None
    calientes = intra_lines[1:] + spaced_lines
    samples, explicitas, usage_field, uso_caida = _hit_samples(calientes, tin_frio)

    # The Δpp signal: the cold per-request cost against the intra bracket's
    # average (its primer plus warm replays), in ticks of the meter's resolution.
    senal_pp = senal_ticks = None
    if dp_frio is not None and dp_frio > 0 and dp_intra is not None and intra_lines:
        senal_pp = dp_frio - dp_intra / len(intra_lines)
        senal_ticks = senal_pp / TICK_PP

    proxy: dict | None = None
    if not samples and dp_frio is not None and dp_frio > 0:
        estimadores = []
        for dp, n in ((dp_intra, len(intra_lines)), (dp_espaciada, len(spaced_lines))):
            if dp is not None and n:
                estimadores.append(max(0.0, 1.0 - (dp / n) / dp_frio))
        if len(estimadores) >= 2:
            q = statistics.quantiles(estimadores, n=4)
            proxy = {
                "estimates": list(estimadores),
                "iqr": [q[0], q[2]],
            }

    estimates = samples if samples else (proxy["estimates"] if proxy else [])
    iqr = _iqr_de(estimates)
    above_zero = iqr is not None and iqr[0] > 0
    # A conclusive "no" needs the field's own zeros: a reported hit count of 0
    # on every replay is evidence of absence, while an unchanged prompt-eval
    # count only says the API reveals nothing (caching could still exist,
    # invisible in both signals) — that stays unknown, and S1 stays marked.
    explicit_zero = len(explicitas) >= 2 and all(m == 0.0 for m in explicitas)

    conclusiva = _conclusive(senal_ticks, above_zero, explicit_zero)

    if conclusiva == "yes":
        tasa: float | None = statistics.median(estimates)
    elif conclusiva == "no":
        tasa = 0.0
    else:
        tasa = None

    if conclusiva == "yes" or any(m > 0 for m in samples):
        existe = "yes"
    elif conclusiva == "no":
        existe = "no"
    else:
        existe = "unknown"

    if usage_field:
        estimate_basis = "reported cache-hit tokens"
    elif uso_caida:
        estimate_basis = "prompt-eval drop"
    elif proxy:
        estimate_basis = "dpp proxy"
    else:
        estimate_basis = None

    # Persistence: which spaced replay offsets still hit, from the raw stamps.
    # A horizon needs a cache whose existence the evidence established first —
    # an unknown reading reports no horizon at all.
    persistencia: str | None = None
    if conclusiva == "no":
        persistencia = "none observed"
    elif existe == "yes" and spaced_lines and intra_lines:
        persistencia = _persistence(spaced_lines, intra_lines, tin_frio)

    try:
        declarado = table.rate(model).has_cache_discount
    except TableError:
        # A slate model the chosen table no longer prices: the reading still
        # ships (the brackets' spend is already in the raw dataset) with no
        # declared-discount signal and a note - never a traceback after the
        # quota is spent, and never a lost runs/calibration-<run_id>.json.
        declarado = None
        notes.append(f"the price table does not price {model!r} - no declared-discount signal")
    materialized = {"yes": True, "no": False}.get(existe)

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
            "requests": [_evidencia(r) for r in cold_lines],
        },
        INTRA_WORKLOAD: {
            "batch_id": intra.get("batch_id") if intra else None,
            "dpp": dp_intra,
            "replays": [_evidencia(r) for r in intra_lines[1:]],
        },
        SPACED_WORKLOAD: {
            "batch_id": espaciada.get("batch_id") if espaciada else None,
            "dpp": dp_espaciada,
            "replays": [{**_evidencia(r), "hit": _hit_de(r, tin_frio)} for r in spaced_lines],
        },
    }
    return {
        "cache_exists": existe,
        "persistence": persistencia,
        "hit_rate": tasa,
        "hit_rate_basis": estimate_basis,
        "conclusive": conclusiva is not None,
        "rule": {
            "dp_signal_pp": senal_pp,
            "dp_signal_ticks": senal_ticks,
            "conclusive_ticks_required": CONCLUSIVE_TICKS,
            "estimates": list(estimates) if estimates else None,
            "iqr": list(iqr) if iqr else None,
            "estimate_basis": estimate_basis,
        },
        "signals": senales,
        "paper_discount": {"declared": declarado, "materialized": materialized},
        "notes": "; ".join(notes),
    }


def _build_summary(
    *,
    run_id: str,
    runs_dir: pathlib.Path,
    batches_dir: pathlib.Path,
    table_version: str,
    table,
) -> dict:
    """The calibration doc, computed from the raw batches + requests lines alone.

    It covers every model the run_id ever calibrated — later invocations extend
    it, so `analyze` reads one doc per run and finds each model's reading there.
    """
    batches = read_jsonl(batches_dir / f"batches-{run_id}.jsonl")
    requests = read_jsonl(runs_dir / f"requests-{run_id}.jsonl")
    per_model: dict[str, dict[str, dict]] = {}
    for b in batches:
        if b.get("workload") in _WORKLOADS:
            per_model.setdefault(b.get("model"), {})[b.get("workload")] = b

    by_batch: dict[str | None, list[dict]] = {}
    for r in requests:
        by_batch.setdefault(r.get("batch_id"), []).append(r)

    readings: dict[str, dict] = {}
    for model, brackets in sorted(per_model.items()):
        requests_by_batch: dict[str, list[dict]] = {}
        for w in _WORKLOADS:
            batch = brackets.get(w)
            lines = by_batch.get(batch.get("batch_id"), []) if batch else []
            requests_by_batch[w] = sorted(lines, key=lambda r: r.get("req_id") or "")
        analisis = _analyze_model(model, brackets, requests_by_batch, table=table)
        stamps = [
            r.get("t_total")
            for w in _WORKLOADS
            for r in requests_by_batch[w]
            if isinstance(r.get("t_total"), (int, float))
        ]
        analisis["calibrated_at"] = max(stamps) if stamps else None
        readings[model] = analisis
    not_materialized = sorted(
        m
        for m, a in readings.items()
        if a["paper_discount"]["declared"] and a["paper_discount"]["materialized"] is False
    )
    return {
        "run_id": run_id,
        "kind": "cache-calibration",
        "level": CACHE_LEVEL,
        "models": sorted(readings),
        "readings": readings,
        "table_version": table_version,
        "protocol_version": PROTOCOL_VERSION,
        "unmaterialized_paper_discounts": not_materialized,
        "notes": (
            "derived from the raw batches/*.jsonl + runs/*.jsonl lines alone. The "
            "override rule: a measured hit rate replaces S1 when conclusive (>2 ticks "
            "of cold-vs-warm dp and a non-overlapping IQR); a conclusive absence puts "
            "the model at the S0 floor; an inconclusive measurement keeps the S1 "
            "assumption, marked. Unmaterialized paper discounts are models whose "
            "table declares a cached-input discount while the measurement found none."
        ),
    }


async def _run_async(
    base,
    *,
    models: list[str],
    spaced_ages: tuple[float, ...],
    settle_s: float,
    settle_poll_s: float,
    table_version: str,
    table,
    catalog: dict | None,
    model_map: dict[str, str],
    transport,
    emit,
) -> dict:
    session = open_workstream(
        base,
        # k=1: every calibration bracket fires at k=1, the replays' design
        Workstream(level=CACHE_LEVEL, run_id_prefix="T2-cache", k=1, lane=False),
        table_version=table_version,
        settle_s=settle_s,
        settle_poll_s=settle_poll_s,
        catalog=catalog,
        model_map=model_map,
        transport=transport,
        emit=emit,
    )
    async with session:
        run_id = session.run_id
        # The gap plan (spaced targets + repeats) is pinned once per run_id: mixing
        # ladders under one dataset would make the persistence readings incomparable.
        session.pin("gap_plan", {"targets": list(spaced_ages), "repeats": CACHE_REPEATS})
        for model in models:
            specs = _bracket_specs(run_id=run_id, model=model)
            estados = {w: session.manifest.status(s.batch_id) for w, s in specs.items()}
            if all(e in ("done", "aborted") for e in estados.values()):
                cerrados = sum(1 for e in estados.values() if e == "done")
                emit(
                    f"calibrate: {model} already calibrated for this run "
                    f"({cerrados} closed, {len(estados) - cerrados} aborted) - skipped"
                )
                continue
            if any(e == "in_flight" for e in estados.values()):
                emit(
                    f"calibrate: {model} has an in_flight bracket from an interrupted "
                    "run - model skipped, never silently retried"
                )
                continue

            # ---- the cold reference, then the intra-batch replay ----
            for w in (COLD_WORKLOAD, INTRA_WORKLOAD):
                if estados[w] is not None:  # closed earlier: its spend is in the dataset
                    emit(f"calibrate: {model}/{w} closed in an earlier attempt - skipped")
                    continue
                resultado = await session.execute_one(specs[w])
                emit(
                    f"calibrate: {model}/{w}: {resultado.ok}/{resultado.intentados} ok, "
                    f"dpp={resultado.dpp_session}"
                )
            estados = {w: session.manifest.status(s.batch_id) for w, s in specs.items()}
            if estados[INTRA_WORKLOAD] != "done":
                raise RunnerError(
                    f"calibrate: {model}: the intra bracket never closed (an aborted "
                    "bracket is never retried), so the spaced ladder has no refresh to "
                    "sit on - delete manifest-T2-cache.json to re-calibrate this run "
                    "cleanly, or keep the incomplete evidence"
                )

            # ---- the spaced replays, at their cumulative offsets ----
            if estados[SPACED_WORKLOAD] is not None:
                emit(f"calibrate: {model}/{SPACED_WORKLOAD} closed in an earlier attempt - skipped")
                continue
            objetivo = spaced_ages
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
            resultado = await session.execute_one(espec_spaced)
            emit(
                f"calibrate: {model}/{SPACED_WORKLOAD}: {resultado.ok}/{resultado.intentados} "
                f"ok, dpp={resultado.dpp_session}"
            )
        session.grow_planned(
            max(len(session.manifest.doc["batches"]), len(_WORKLOADS) * len(models))
        )

    summary = _build_summary(
        run_id=run_id,
        runs_dir=session.runs_dir,
        batches_dir=session.batches_dir,
        table_version=table_version,
        table=table,
    )
    session.write_summary("calibration", summary)
    return summary


def run_calibration(
    base,
    *,
    models: list[str],
    spaced_ages: tuple[float, ...] = SPACED_TARGETS,
    settle_s: float,
    settle_poll_s: float = 5.0,
    table_version: str,
    table,
    catalog: dict | None = None,
    model_map: dict[str, str] | None = None,
    transport=None,
    emit=print,
) -> dict:
    """Executes the calibration replays per model; raises RunnerError on abort.

    The calibration is EXEMPT from the cache-free lane (no `lane` in its cfg):
    its replays re-send one identical prefix on purpose — prefix replay is the
    only legitimate cached traffic a measured run never sees."""
    return asyncio.run(
        _run_async(
            base,
            models=models,
            spaced_ages=tuple(float(a) for a in spaced_ages),
            settle_s=settle_s,
            settle_poll_s=settle_poll_s,
            table_version=table_version,
            table=table,
            catalog=catalog,
            model_map=model_map or {},
            transport=transport,
            emit=emit,
        )
    )
