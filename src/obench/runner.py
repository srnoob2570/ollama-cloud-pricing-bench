"""The bracketed-batch runner (methodology v1 §4, protocol v3).

Per batch = one bracketed batch: a (workload, model, rep, k) cell measured per
rep on T1/T3, or a bracket POOL on T2 — the strong four per-cell (every rep of
the cell in one bracket) and the weak trio pooled per model (`workload` null,
`pool` naming the workloads; methodology v1.1 §5):

1. raw meter read (full payload kept);
2. the batch's N requests — streaming, k-concurrent via semaphore, **no warmup,
   no in-batch auto-retry** (every request the meter sees is an intended one).
   Under the cache-free lane every request is salted with its run-scoped seeded
   nonce (`lane.py`), so the measured pp is the workload's raw work;
3. per-model `request_count` check issued immediately after the burst (the
   counter is instant and exact, ~0.2 s — a dropped request aborts the run; the
   bracket is still closed so the aborted batch's real spend is attributed to it).
   This read is the registration loop's first sample;
4. the **registration settle** (protocol v3; the fixed >= 90 s wait is dead):
   poll `/api/usage` every `poll_s` (5 s) until two consecutive reads report
   equal pp in BOTH windows — the batch's usage has registered in the meter —
   closing at `settle_s`'s cap (60 s) with ``settle_exit: "capped"`` when the
   meter never stabilizes;
5. the last read is the bracket's post payload -> Δpp per window, attributed to
   this batch alone.

Before the first bracket, once per run, the **billing canary** checks the lane
holds: 5 salted requests + 5 identical-prefix replays (T2-size body); the replay
must bill at the cache discount (~11–14 % of the salted quota, measured 1/7 on
kimi-k3) — a ratio above 0.5 aborts the run at the gate. A passive detector
cross-checks every closed bracket's Δpp against the #28 token budget (its
threshold is deferred until v3 data exists).

`batch_id` is deterministic from (run, level, workload, model, rep, k); the
per-level manifest records in_flight/done/aborted, the lane spec and the canary
result. A re-run resumes done batches and **skips** aborted/in_flight ones with
a loud report — an aborted batch is never silently retried (its requests are
already billed; `bench status` shows the state and recovery stays an explicit
operator decision).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import pathlib
import time
import uuid

from . import agent, checkers, fixtures, lane, schema, workloads as workloads_mod
from .client import PROTOCOL_VERSION, OllamaCloud
from .fixtures import FIXTURE_VERSION
from .meter import TICK_BAND, TICK_PP  # the meter's resolution, in percentage points

# The passive detector's expected Δpp rates (weekly pp per 1M tokens), from the
# measurability-budget derivation (issue #28, docs/research/presupuesto-
# medibilidad-2026-09-01.md §3): prefill-dominated brackets (in-share >= 0.9)
# move the weekly window at ~2.6 pp/1M, generation-carrying ones at ~5.4. The
# across-model spread is >=12x, so the detector only flags a COLLAPSE — a
# bracket whose token budget predicts a readable Δpp (>= 3.5 ticks, the note's
# planning floor) but measures none; its threshold is refined once v3 data
# exists (the v2 dataset inherits the cache discount and cannot re-derive it).
DETECTOR_RATES = {"prefill": 2.6, "generation": 5.4}
DETECTOR_PREFILL_SHARE = 0.9
DETECTOR_TICKS_FLOOR = 3.5

# The bracket composition the plan derives (methodology v1.1 §5). A manifest
# binds its composition: a pre-hybrid manifest's batch ids cover per-rep
# brackets, and resuming it under the hybrid plan would read a 5-rep pooled
# bracket as "done" on a rep-1 id collision — re-billing nothing and measuring
# less than the plan says. Composition drift refuses like any other drift.
COMPOSITION_VERSION = "hybrid-v1.1"


class RunnerError(Exception):
    """The protocol aborted (meter failure, dropped request, or run-state drift)."""


@dataclasses.dataclass(frozen=True)
class BatchSpec:
    level: str
    batch_id: str
    workload: str | None  # the per-cell workload; None on a pooled bracket
    model: str
    rep: int  # the bracket's first repetition (its batch_id anchor)
    k: int
    n: int  # requests in the batch (every unit's requests added up)
    fixture_hash: str
    # The bracket's units — (workload, rep, requests) triples in send order.
    # A per-cell bracket carries one workload across its repetitions; a pooled
    # bracket walks the pool workload by workload. Empty on the workstreams
    # that build their own single-unit spec (calibration, concurrency probe).
    units: tuple[tuple[str, int, int], ...] = ()
    # Repetitions the bracket pools (the cell's n on a per-cell bracket; the
    # pool's per-workload count on a pooled one). 1 on a single-rep bracket.
    reps: int = 1
    # The workloads a pooled bracket covers (empty on a per-cell bracket).
    pool: tuple[str, ...] = ()
    plan_note: str = ""  # informational provenance carried on the batch line (no abort)
    # Sleep before each request, index-aligned (empty = no spacing). The cache
    # calibration's spaced replays time their sends from the prefix's last
    # refresh; the caller computes the tuple, the burst only honors it (the
    # calibration always fires k=1, so the sleeps serialize exactly).
    gap_s: tuple[float, ...] = ()


@dataclasses.dataclass(frozen=True)
class BatchOutcome:
    """What one bracketed batch yielded, for the caller's counters and progress."""

    ok: int
    intentados: int
    dpp_session: float | None
    wall_clock_s: float | None


def batch_id(run_id: str, level: str, workload: str, model: str, rep: int, k: int) -> str:
    """Deterministic from (run, level, workload, model, rep, k). A pooled
    bracket's workload slot carries its pool joined with '+' — the pool is the
    bracket's identity, and no workload name contains a '+'."""
    material = f"{run_id}|{level}|{workload}|{model}|{rep}|{k}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


def plan(
    *,
    run_id: str,
    level: str,
    workloads,
    models: list[str],
    reps: int,
    rep_filter: int | None,
    k: int,
) -> list[BatchSpec]:
    """All batches of the run — the hybrid bracket composition (methodology
    v1.1 §5). T1 and T3 compose one single-rep bracket per (workload, model,
    rep), round-robin across the slate: workload -> rep -> model. T2 composes
    the hybrid: the strong four per-cell — one bracket per (workload, model)
    pooling ALL the cell's repetitions, the legacy measured; the weak trio
    pooled per model — one bracket covering the trio's workloads and reps
    (`workload` null, `pool` naming them), its legacy attributed per workload
    post-hoc by token share, never a stored weight. `--rep` narrows every
    bracket's repetition range to that one rep; `--reps` is the cell's n."""
    specs: list[BatchSpec] = []
    if level == "T2":
        rango = [rep_filter] if rep_filter else range(1, reps + 1)
        fuertes = [w for w in workloads if w.name in workloads_mod.STRONG_T2]
        debiles = [w for w in workloads if w.name in workloads_mod.WEAK_T2]
        # The strong four: per-cell brackets, every rep of the cell inside one.
        for w in fuertes:
            hash_fixture = fixtures.fixture_hash(fixtures.build(level, w.name, w.requests))
            for model in models:
                specs.append(
                    BatchSpec(
                        level=level,
                        batch_id=batch_id(run_id, level, w.name, model, rango[0], k),
                        workload=w.name,
                        model=model,
                        rep=rango[0],
                        k=k,
                        n=w.requests * len(rango),
                        fixture_hash=hash_fixture,
                        units=tuple((w.name, rep, w.requests) for rep in rango),
                        reps=len(rango),
                        plan_note=(
                            f"per-cell bracket: {len(rango)} rep(s) of the cell in one "
                            "bracket, legacy measured"
                        ),
                    )
                )
        # The weak trio: pooled per model, the legacy allocated post-hoc.
        pool_specs = tuple(
            spec for w in debiles for spec in fixtures.build(level, w.name, w.requests)
        )
        hash_pool = fixtures.fixture_hash(pool_specs)
        for model in models:
            specs.append(
                BatchSpec(
                    level=level,
                    batch_id=batch_id(
                        run_id, level, "+".join(w.name for w in debiles), model, rango[0], k
                    ),
                    workload=None,
                    model=model,
                    rep=rango[0],
                    k=k,
                    n=sum(w.requests for w in debiles) * len(rango),
                    fixture_hash=hash_pool,
                    units=tuple((w.name, rep, w.requests) for w in debiles for rep in rango),
                    reps=len(rango),
                    pool=tuple(w.name for w in debiles),
                    plan_note=(
                        "pooled bracket: per-workload legacy derives post-hoc by token "
                        "share (allocated, never verdicted)"
                    ),
                )
            )
        return specs
    for w in workloads:
        specs_requeridos = fixtures.build(level, w.name, w.requests)
        hash_fixture = fixtures.fixture_hash(specs_requeridos)
        for rep in [rep_filter] if rep_filter else range(1, reps + 1):
            for model in models:
                specs.append(
                    BatchSpec(
                        level=level,
                        batch_id=batch_id(run_id, level, w.name, model, rep, k),
                        workload=w.name,
                        model=model,
                        rep=rep,
                        k=k,
                        n=w.requests,
                        fixture_hash=hash_fixture,
                    )
                )
    return specs


class Manifest:
    """Per-level run state (runs/manifest-<level>.json): resume without re-billing."""

    def __init__(self, ruta: pathlib.Path, doc: dict) -> None:
        self.ruta = ruta
        self.doc = doc

    @classmethod
    def load(cls, ruta: pathlib.Path, *, strict: bool = False) -> Manifest | None:
        if not ruta.exists():
            return None
        try:
            doc = json.loads(ruta.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("batches"), dict):
                raise TypeError("missing the batches map")
            if not isinstance(doc.get("run_id"), str):
                raise TypeError("missing run_id")
            if strict:
                # A run must never dereference shapes it did not write: a null
                # catalog or a malformed batch entry would either crash the
                # resume (traceback) or look "unseen" and silently re-bill a
                # cell whose state is unknown. `status` stays tolerant and
                # renders broken entries as corrupt instead.
                if not isinstance(doc.get("catalog", []), list):
                    raise TypeError("'catalog' must be a list of snapshots")
                for entrada in doc["batches"].values():
                    if not isinstance(entrada, dict) or not isinstance(entrada.get("status"), str):
                        raise TypeError("a batch entry is not a status map")
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            raise RunnerError(
                f"manifest {ruta.name} is corrupt ({e}); the run state is unreadable - "
                "delete it (and its runs/*-<run_id>.jsonl) only as an explicit operator decision"
            ) from None
        return cls(ruta, doc)

    @classmethod
    def create(
        cls,
        ruta: pathlib.Path,
        *,
        run_id: str,
        level: str,
        table_version: str,
        k: int | None,  # None: the workstream's cells carry their own k per batch
        planned: int,
        reps: int | None = None,  # the composition's density (None: the workstreams')
        catalog: dict | None = None,
    ) -> Manifest:
        doc = {
            "run_id": run_id,
            "level": level,
            "table_version": table_version,
            "protocol_version": PROTOCOL_VERSION,
            "fixture_version": FIXTURE_VERSION,
            "composition": COMPOSITION_VERSION,
            "k": k,
            "started_at": time.time(),
            "planned": planned,
            "batches": {},
        }
        if reps is not None:
            # The density the composition was planned at: the pooled brackets'
            # batch ids anchor on the first rep only, so a resume at another
            # --reps would collide with them (a wide resume would read every
            # narrow bracket done). Drift refuses like any other.
            doc["reps"] = reps
        if catalog is not None:  # /v1/models snapshots, one per attempt (provenance)
            doc["catalog"] = [{"captured_at": time.time(), **catalog}]
        m = cls(ruta, doc)
        m.save()
        return m

    @property
    def run_id(self) -> str:
        return self.doc["run_id"]

    def status(self, bid: str) -> str | None:
        entrada = self.doc["batches"].get(bid)
        return entrada["status"] if entrada else None

    def set(self, bid: str, status: str, **extra) -> None:
        entrada = {"status": status, "at": time.time(), **extra}
        self.doc["batches"][bid] = entrada
        self.save()

    def append_catalog(self, catalogo: dict) -> None:
        """Adds one /v1/models snapshot to the catalog history (provenance)."""
        self.doc.setdefault("catalog", []).append({"captured_at": time.time(), **catalogo})

    def save(self) -> None:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ruta.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.doc, indent=2), encoding="utf-8")
        tmp.replace(self.ruta)


def _numero(valor) -> float | None:
    """A real number from a manifest (bools are not numbers here), or None."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    return valor


def status_doc(nivel: str, manifiesto: Manifest) -> dict:
    """The status of one level's run, computed from its manifest (no API).

    The single owner of the manifest-shape-to-report contract (the writer is
    `Manifest.set` and the canary's `_ensure_canary`, so the reader lives
    next to them): a key rename cannot silently break `bench status`. The
    manifest is run state that a recovering operator may have hand-edited:
    malformed entries render as unknown/corrupt instead of crashing the report.
    """
    doc = manifiesto.doc
    counts: dict[str, int] = {"done": 0, "aborted": 0, "in_flight": 0}
    dpp_session = dpp_weekly = 0.0
    con_bracket = cerrados = 0
    requests_ok = 0
    batches = []
    # The billing canary's volleys are bracketed spend no batch line carries:
    # kept separate so the quota totals stay equal to the per-batch rows, and
    # the report can state the canary's own consumption explicitly.
    canario = doc.get("canary")
    canario_dpp = canario.get("dpp") if isinstance(canario, dict) else None

    def _pareada(salted, replay):
        """A canary window's paired spend: salted + replay, or None when either
        reading is unreadable (a half-pair would understate the quota)."""
        salada, repeticion = _numero(salted), _numero(replay)
        return salada + repeticion if salada is not None and repeticion is not None else None

    canario_sesion = canario_semanal = None
    if isinstance(canario_dpp, dict):
        canario_sesion = _pareada(
            canario_dpp.get("salted_session"), canario_dpp.get("replay_session")
        )
        canario_semanal = _pareada(
            canario_dpp.get("salted_weekly"), canario_dpp.get("replay_weekly")
        )
    for batch_id, entrada in doc.get("batches", {}).items():
        if not isinstance(entrada, dict):
            entrada = {"status": "corrupt"}
        estado = str(entrada.get("status", "?"))
        counts[estado] = counts.get(estado, 0) + 1
        dpp_s = _numero(entrada.get("dpp_session"))
        dpp_w = _numero(entrada.get("dpp_weekly"))
        if estado in ("done", "aborted"):
            cerrados += 1
        # Each window accumulates on its own readable delta, so the quota totals
        # always agree with the report's own per-batch rows; `batches_with_bracket`
        # keeps counting only the brackets both windows resolved.
        if dpp_s is not None:
            dpp_session += dpp_s
        if dpp_w is not None:
            dpp_weekly += dpp_w
        if dpp_s is not None and dpp_w is not None:
            con_bracket += 1
        ok = _numero(entrada.get("requests_ok"))
        if ok is not None:
            requests_ok += int(ok)
        batches.append(
            {
                "batch_id": batch_id,
                "status": estado,
                "workload": entrada.get("workload"),
                "pool": entrada.get("pool"),
                "model": entrada.get("model"),
                "rep": entrada.get("rep"),
                "dpp_session": dpp_s,
                "dpp_weekly": dpp_w,
                "requests_ok": None if ok is None else int(ok),
                "note": entrada.get("note"),
            }
        )
    try:
        planned = int(doc.get("planned", len(batches)))
    except (TypeError, ValueError):
        planned = len(batches)
    counts["pending"] = max(0, planned - len(batches))
    return {
        "level": doc.get("level", nivel),
        "run_id": doc.get("run_id"),
        "table_version": doc.get("table_version"),
        "protocol_version": doc.get("protocol_version"),
        "k": doc.get("k"),
        "started_at": doc.get("started_at"),
        "planned": planned,
        "counts": counts,
        "requests_ok": requests_ok,
        "quota": {
            # the deltas keep their exact float (methodology v1.1 §4); the
            # human table formats them, the JSON never re-rounds them
            "dpp_session": dpp_session,
            "dpp_weekly": dpp_weekly,
            "batches_with_bracket": con_bracket,
            "closed_batches": cerrados,
            "canary_dpp_session": canario_sesion,
            "canary_dpp_weekly": canario_semanal,
        },
        "batches": batches,
    }


def open_workstream_manifest(
    runs_dir: pathlib.Path,
    *,
    level: str,
    run_id_prefix: str,
    cfg: dict,
    planned: int = 0,
) -> Manifest:
    """The spending workstreams' shared bootstrap: load the level's manifest
    strictly, mint its run_id and create it when absent, refuse drift, and join
    this attempt's catalog snapshot to the history on reuse. One resume state,
    one drift guard, one creation path — every workstream resumes alike."""
    ruta = runs_dir / f"manifest-{level}.json"
    existente = Manifest.load(ruta, strict=True)
    run_id = (
        existente.run_id
        if existente
        else f"{run_id_prefix}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        f"-{uuid.uuid4().hex[:8]}"
    )
    if existente:
        _check_drift(existente, cfg)
    manifiesto = existente or Manifest.create(
        ruta,
        run_id=run_id,
        level=level,
        table_version=cfg["table_version"],
        k=cfg.get("k"),
        planned=planned,
        reps=cfg.get("reps"),
        catalog=cfg.get("catalog"),
    )
    if existente:
        if cfg.get("catalog"):
            manifiesto.append_catalog(cfg["catalog"])
    # The cache-free lane binds its run: the spec is derived from the run_id, so
    # a resume always re-derives the same nonce stream; a recorded spec that
    # disagrees is a hand-edited manifest, refused like any other drift.
    if cfg.get("lane"):
        previa = manifiesto.doc.get("lane")
        esperado = lane.lane_spec(run_id)
        if previa is None:
            manifiesto.doc["lane"] = esperado
            manifiesto.save()
        elif previa != esperado:
            raise RunnerError(
                f"manifest {ruta.name} records lane spec {previa!r} but this run would "
                f"use {esperado!r} - the lane spec may not drift inside one run_id "
                "- keep the datasets apart"
            )
        cfg["lane"] = manifiesto.doc["lane"]
    if existente:
        manifiesto.save()
    return manifiesto


def _usage_ventana(payload: dict | None, window: str) -> float | None:
    """The window's raw usage fraction (0.382 = 38.2 %), or None when unreadable."""
    try:
        valor = payload["limits"][window]["usage"]
    except (KeyError, TypeError):
        return None
    return valor if isinstance(valor, (int, float)) else None


async def _registration_settle(
    client: OllamaCloud, *, primera: dict | None, cap_s: float, poll_s: float
) -> dict:
    """The protocol v3 settle: the registration loop.

    Polls `/api/usage` every `poll_s` until two consecutive reads report equal
    pp in BOTH windows — the bracket's usage has registered, so its Δpp belongs
    to this batch alone — burning `cap_s` with ``exit: "capped"`` when the meter
    never stabilizes. `primera` (the count-check read, taken ~0.2 s after the
    burst) is the loop's first sample: a meter that already registered the batch
    closes after a single confirming poll.

    Returns the loop's record: reads (polls issued), exit
    ("stable" | "capped" | "error"), the last good payload as `post`, each
    window's registration time in seconds after the loop's first sample
    (0.0 = already at its final value), and `error` on a failed poll (the caller
    decides what a failed registration read costs the batch).
    """
    t0 = time.monotonic()
    lecturas: list[tuple[float, float, float]] = []  # (session, weekly, monotonic)
    if primera is not None:
        s, w = _usage_ventana(primera, "session"), _usage_ventana(primera, "weekly")
        if s is not None and w is not None:
            lecturas.append((s, w, t0))
    lecturas_payload: list[dict | None] = [primera] if primera is not None else []
    leidas = 0
    exito, error = "", ""
    while True:
        if len(lecturas) >= 2 and lecturas[-2][:2] == lecturas[-1][:2]:
            exito = "stable"
            break
        if time.monotonic() - t0 >= cap_s:
            exito = "capped"
            break
        await asyncio.sleep(poll_s)
        try:
            status, payload = await client.usage()
        except Exception as e:  # noqa: BLE001 - a failed read is the caller's decision
            error = f"{type(e).__name__}: {e}"
            break
        leidas += 1
        if status != 200 or payload is None:
            error = f"HTTP {status}"
            break
        s, w = _usage_ventana(payload, "session"), _usage_ventana(payload, "weekly")
        if s is None or w is None:
            error = "unreadable meter payload"
            break
        lecturas.append((s, w, time.monotonic()))
        lecturas_payload.append(payload)

    def _registrada(window: int) -> float | None:
        """Seconds after the loop's first sample when the window last took a new
        value; 0.0 when it was already at its final value there."""
        if not lecturas:
            return None
        ultima = lecturas[0][window]
        momento = lecturas[0][2]
        for lectura in lecturas[1:]:
            if lectura[window] != ultima:
                ultima, momento = lectura[window], lectura[2]
        return momento - t0

    post = lecturas_payload[-1] if lecturas_payload else None
    return {
        "reads": leidas,
        "exit": exito or None,
        "error": error,
        "post": post,
        "registered_session_s": _registrada(0),
        "registered_weekly_s": _registrada(1),
    }


def _salter(cfg: dict, spec: BatchSpec):
    """The bracket's per-request nonce callable ((workload, rep, index) -> nonce
    text), or None when the workstream is exempt from the cache-free lane. The
    coordinates key per unit — a pooled bracket's requests span workloads and
    reps, and every one must salt from its own cell coordinates."""
    lane_cfg = cfg.get("lane")
    if not lane_cfg:
        return None
    seed_ = lane_cfg["nonce_seed"]
    palabras_de: dict[str, int] = {}

    def _nonce(workload: str, rep: int, indice, turno=None):
        # The nonce's coordinates include k: two cells of the same (workload,
        # model, rep) at different k must never share a prefix — the glossary's
        # comparability clause reads on fixture tokens, and a shared salt would
        # let one cell's burst warm the next cell's cache.
        coords = [spec.level, workload, spec.model, rep, spec.k, indice]
        if turno is not None:
            coords.append(turno)
        if workload not in palabras_de:
            palabras_de[workload] = lane.nonce_words(lane.expected_tin(spec.level, workload))
        return lane.nonce_text(seed_, lane.nonce_index(*coords), palabras_de[workload])

    return _nonce


def _counts(payload: dict | None, window: str = "session") -> dict[str, int]:
    """The meter's per-model request counts; {} when the payload is unreadable.

    Guarded like _dpp: a meter payload whose model entries lack (or mis-type)
    `request_count` must surface as a count-check failure — an aborted batch —
    never as a KeyError traceback out of the runner (that would strand the
    cell in_flight with its spend attributed to nothing).
    """
    if not isinstance(payload, dict):
        return {}
    limits = payload.get("limits")
    ventana = limits.get(window) if isinstance(limits, dict) else None
    modelos = ventana.get("models") if isinstance(ventana, dict) else None
    if not isinstance(modelos, list):
        return {}
    counts: dict[str, int] = {}
    for m in modelos:
        if (
            isinstance(m, dict)
            and isinstance(m.get("name"), str)
            and isinstance(m.get("request_count"), int)
            and not isinstance(m["request_count"], bool)
        ):
            counts[m["name"]] = m["request_count"]
    return counts


def _dpp(pre: dict | None, post: dict | None, window: str) -> float | None:
    """Quota delta in percentage points (a 0.001 usage step = 0.1 pp); None if unreadable.

    The delta keeps its exact float (precision policy, methodology v1.1 §4): the
    tick belongs to comparison logic only — the verdicts' tie band, the
    conclusive-override rule, the sub-resolution exclusion and the k-sweep all
    read it through `TICK_BAND`'s residue band — never a rounding applied to
    the persisted measurement.
    """
    try:
        antes = pre["limits"][window]["usage"]
        despues = post["limits"][window]["usage"]
    except (KeyError, TypeError):
        return None
    if not isinstance(antes, (int, float)) or not isinstance(despues, (int, float)):
        return None
    return (despues - antes) * 100


def _wall_clock_s(registros: list[dict]) -> float | None:
    """The batch's makespan: last completion minus first launch across its requests.

    For a k=1 cell this is the serialized total; for k>1 it is what parallelism
    actually bought — the dp-vs-k and wall-clock-vs-k comparison reads this with
    `k`. Null when the batch carried no request that ever reported both stamps.
    """
    tiempos = [
        (r["t_start"], r["t_total"])
        for r in registros
        if isinstance(r.get("t_start"), (int, float)) and isinstance(r.get("t_total"), (int, float))
    ]
    if not tiempos:
        return None
    return max(fin for _ini, fin in tiempos) - min(ini for ini, _fin in tiempos)


def _sum_steps(pasos: list[dict], campo: str) -> int | None:
    """A T3 task's token total across its loop steps: None unless EVERY step
    reports the count (a partial sum would silently understate the billing)."""
    valores = [p.get(campo) for p in pasos]
    if not valores or any(not isinstance(v, int) or isinstance(v, bool) for v in valores):
        return None
    return sum(valores)


def _request_line(
    rec: dict,
    spec: BatchSpec,
    *,
    run_id: str,
    level: str,
    index: int,
    workload: str,
    rep: int,
    fixture_hash: str,
    seed_value: int,
    table_version: str,
    checker: str | None,
) -> dict:
    done = rec["done"]  # the verbatim done-object; None when the request never completed
    pasos = rec.get("steps") or []  # a T3 task's loop steps (its raw per-step evidence)
    if pasos:
        tok_in = _sum_steps(pasos, "tok_in")
        tok_out = _sum_steps(pasos, "tok_out")
        tok_cached = _sum_steps(pasos, "tok_cached")
    else:
        tok_in = done.get("prompt_eval_count") if done else None
        tok_out = done.get("eval_count") if done else None
        tok_cached = done.get("prompt_eval_cache_hit_count") if done else None
    return {
        "req_id": f"{spec.batch_id}-{index:04d}",
        "batch_id": spec.batch_id,
        "run_id": run_id,
        "level": level,
        "workload": workload,
        "model": spec.model,
        "seed": seed_value,
        "rep": rep,
        "k": spec.k,
        "t_start": rec["t_start"],
        "t_first_chunk": rec["t_first_chunk"],
        "t_total": rec["t_total"],
        "chunks": rec["chunks"],
        "tok_in": tok_in,
        "tok_out": tok_out,
        "tok_cached": tok_cached,
        # The cache-free lane's evidence: what was actually billed (nonce +
        # fixture), and the nonce itself. Null on exempt traffic.
        "prompt_sha256": rec.get("prompt_sha256"),
        "nonce_sha256": rec.get("nonce_sha256"),
        "api": done,
        "http": rec["http"],
        "err": rec["err"],
        "checker": checker,
        "tool_calls": rec.get("tool_calls"),
        "steps": pasos,
        "sandbox": rec.get("sandbox"),  # the T3 checker's sandbox run; null for T1/T2
        "out_text_hash": (
            hashlib.sha256(rec["content"].encode("utf-8")).hexdigest() if rec["content"] else None
        ),
        "fixture_hash": fixture_hash,
        "table_version": table_version,
        "protocol_version": PROTOCOL_VERSION,
    }


def _append_jsonl(ruta: pathlib.Path, line: dict) -> None:
    with ruta.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _judge_units(
    specs_requeridos: tuple,
    registros: list[dict],
    unidades: tuple[tuple[str, int, int], ...],
) -> list[str | None]:
    """One verdict per request of the batch, each workload judged with its own
    checker: a pooled bracket grades its workloads separately (every verdict
    lands back on its own request line), a per-cell bracket judges its single
    workload whole — the whole batch is that one workload's slice, byte-identical
    to the pre-pool path. `unidades` carries the units' (workload, rep, request
    count) in send order, exactly as the burst laid them out."""
    indices_de: dict[str, list[int]] = {}
    pos = 0
    for workload, _rep, peticiones in unidades:
        indices_de.setdefault(workload, []).extend(range(pos, pos + peticiones))
        pos += peticiones
    juzgados: list[tuple[int, str | None]] = []
    for workload, indices in indices_de.items():
        veredictos = checkers.judge(
            workload,
            [specs_requeridos[i].prompt for i in indices],
            [registros[i] for i in indices],
        )
        juzgados.extend(zip(indices, veredictos, strict=True))
    return [v for _i, v in sorted(juzgados, key=lambda par: par[0])]


# ---------------------------------------------------------------------------
# the billing canary (protocol v3): the once-per-run lane check
# ---------------------------------------------------------------------------

CANARY_WORKLOAD = "billing-canary"
# The canary's reference model lives in lane (the lane module owns the
# cache-free rules; the comment there carries the why).
CANARY_MODEL = lane.CANARY_MODEL
CANARY_SALTED = 5
CANARY_REPLAYS = 5
CANARY_ALARM_RATIO = 0.5  # the replay billing near 1 means the salting broke


def _canary_nonces(run_id: str, palabras: int) -> tuple[list[str], str]:
    """The canary volley's nonces: CANARY_SALTED fresh ones + the replay nonce,
    which re-uses the FIRST salted nonce verbatim — the identical prefix the
    replays must share (per-request re-salting the replays would defeat them,
    and the ratio would read ~1: the alarm)."""
    seed_ = lane.nonce_seed(run_id)
    salados = [
        lane.nonce_text(seed_, lane.nonce_index("canary", "salted", k), palabras)
        for k in range(CANARY_SALTED)
    ]
    return salados, salados[0]


async def _read_meter(client: OllamaCloud, cuando: str) -> tuple[int, dict | None]:
    """One meter read bracketing a canary volley: a failed read aborts cleanly,
    loudly, naming the phase (`cuando`) so the operator knows where it died."""
    try:
        estado, payload = await client.usage()
    except Exception as e:  # noqa: BLE001 - a meter failure aborts cleanly, loudly
        raise RunnerError(
            f"canary: meter read failed ({type(e).__name__}: {e}) {cuando} volley"
        ) from None
    if estado != 200 or payload is None:
        raise RunnerError(f"canary: meter read failed (HTTP {estado}) {cuando} volley")
    return estado, payload


async def _canary_volley(
    client: OllamaCloud,
    *,
    run_id: str,
    modelo_api: str,
    model: str,
    prompts: list[str],
    cfg: dict,
    fase: str,
) -> dict:
    """One canary volley, bracketed by the registration loop: pre read -> the
    volley's requests (serial, k=1, no warmup, no retry) -> a fresh read as the
    registration loop's first sample. Returns the volley's raw evidence."""
    semillas = [fixtures.seed(CANARY_WORKLOAD, model, 1, k) for k in range(len(prompts))]
    _, pre = await _read_meter(client, f"before the {fase}")
    outcomes = []
    for prompt, semilla in zip(prompts, semillas, strict=True):
        rec = await client.chat(model=modelo_api, prompt=prompt, seed=semilla)
        outcomes.append({"http": rec["http"], "err": rec["err"], "done": rec["done"] is not None})
    _, primera = await _read_meter(client, f"after the {fase}")
    registro = await _registration_settle(
        client, primera=primera, cap_s=cfg["settle_s"], poll_s=cfg["settle_poll_s"]
    )
    if registro["exit"] is None:
        raise RunnerError(
            f"canary: meter read failed ({registro['error']}) registering the {fase} volley"
        )
    return {
        "seeds": semillas,
        "outcomes": outcomes,
        "meter": {"pre": pre, "post": registro["post"]},
        "reads": registro["reads"],
        "settle_exit": registro["exit"],
    }


def _exigir_volley_aceptado(volley: dict, fase: str) -> None:
    """The lane can only be proven from fully accepted volleys: a rejected or
    errored chat deflates its volley's dpp (a false alarm) or empties the
    numerator (a false all-clear) — the ratio reads nothing from failed chats."""
    outcomes = volley["outcomes"]
    aceptados = sum(1 for o in outcomes if o["http"] == 200 and not o["err"])
    if aceptados != len(outcomes):
        raise RunnerError(
            f"canary: the {fase} volley was not fully accepted ({aceptados} of "
            f"{len(outcomes)} requests) - the ratio cannot be measured from failed "
            "requests; the run aborts at the gate"
        )


async def _ensure_canary(client: OllamaCloud, *, ctx, cfg: dict, level: str) -> dict:
    """The billing canary, once per run, before the first bracket — always on
    CANARY_MODEL, never on the run's measured model.

    5 salted requests (fresh nonces — full price, cache misses by construction)
    + 5 identical-prefix replays (salted[0]'s nonce verbatim, the prefix a
    salted request just established). A caching endpoint bills the replay
    volley at the cache discount (~11–14 % of the salted quota on kimi-k3, the
    paired probe's measured band — the only model the ratio reads as evidence,
    which is why the canary is pinned to it); a ratio above
    CANARY_ALARM_RATIO means the replays billed ~full price — per-request
    salting leaked into them, or the endpoint stopped caching — and the run
    aborts at the gate: no bracket runs under an unproven lane. The volleys
    must be fully accepted (a 429 or an errored chat would reshape the ratio
    in either direction) and both settles must close stable: capped evidence
    is recorded as inconclusive, never as a verdict. The result lives in the
    manifest (a resume reuses it — an alarmed or failed canary keeps refusing
    until the operator deletes the manifest — an explicit decision, never a
    silent retry). A canary recorded on another model (a pre-pinning manifest)
    is stale evidence: it re-runs."""
    manifiesto = ctx.manifiesto
    emit = cfg["emit"]
    previo = manifiesto.doc.get("canary")
    if (
        isinstance(previo, dict)
        and previo.get("status") in ("ok", "inconclusive")
        and previo.get("model") == CANARY_MODEL
    ):
        if emit:
            emit(
                f"canary: already ran for this run on {previo.get('model')!r} (ratio "
                f"{_fmt_ratio(previo.get('ratio'))}, status {previo['status']}) - reused"
            )
        return previo
    if isinstance(previo, dict) and previo.get("status") in ("alarm", "failed"):
        causa = (
            "already alarmed"
            if previo.get("status") == "alarm"
            else "never completed (a mid-canary failure left it unfinished)"
        )
        raise RunnerError(
            f"canary: this run's billing canary {causa} (ratio "
            f"{_fmt_ratio(previo.get('ratio'))}) - the lane was never proven for this "
            f"run_id; delete {manifiesto.ruta.name} to start a clean run"
        )

    modelo_api = cfg.get("model_map", {}).get(CANARY_MODEL, CANARY_MODEL)
    specs_cuerpo = fixtures.build("T2", "long_context", 1)
    cuerpo = specs_cuerpo[0].prompt
    palabras = lane.nonce_words(lane.expected_tin("T2", "long_context"))
    salados, nonce_replay = _canary_nonces(manifiesto.run_id, palabras)
    prompts_salados = [lane.salted_prompt(cuerpo, n) for n in salados]
    prompt_replay = lane.salted_prompt(cuerpo, nonce_replay)

    if emit:
        emit(
            f"canary: {CANARY_SALTED} salted + {CANARY_REPLAYS} identical-prefix replays "
            f"({CANARY_MODEL}, T2-size body) - proving the cache-free lane holds"
        )
    # Crash attribution: the volleys bill real quota. A failure between or
    # inside them persists the partial evidence (the billed chats land in the
    # canary line, the manifest marks the canary failed) before re-raising —
    # a resume refuses, it never re-bills the canary from scratch.
    fallo_canary = ""
    salado = None
    repeticion = None
    try:
        salado = await _canary_volley(
            client,
            run_id=manifiesto.run_id,
            modelo_api=modelo_api,
            model=CANARY_MODEL,
            prompts=prompts_salados,
            cfg=cfg,
            fase="salted",
        )
        _exigir_volley_aceptado(salado, "salted")
        if emit:
            emit(
                f"canary: salted volley registered ({salado['reads']} reads, {salado['settle_exit']})"
            )
        repeticion = await _canary_volley(
            client,
            run_id=manifiesto.run_id,
            modelo_api=modelo_api,
            model=CANARY_MODEL,
            prompts=[prompt_replay] * CANARY_REPLAYS,
            cfg=cfg,
            fase="replay",
        )
        _exigir_volley_aceptado(repeticion, "replay")
        if emit:
            emit(
                f"canary: replay volley registered ({repeticion['reads']} reads, "
                f"{repeticion['settle_exit']})"
            )
    except RunnerError as e:
        fallo_canary = str(e)

    vacio = {
        "seeds": [],
        "outcomes": [],
        "meter": {"pre": None, "post": None},
        "reads": 0,
        "settle_exit": None,
    }
    salado = salado or vacio
    repeticion = repeticion or vacio
    dpp = {
        "salted_session": _dpp(salado["meter"]["pre"], salado["meter"]["post"], "session"),
        "salted_weekly": _dpp(salado["meter"]["pre"], salado["meter"]["post"], "weekly"),
        "replay_session": _dpp(repeticion["meter"]["pre"], repeticion["meter"]["post"], "session"),
        "replay_weekly": _dpp(repeticion["meter"]["pre"], repeticion["meter"]["post"], "weekly"),
    }
    # The ratio mounts on the session window (the probe's practically finer
    # readout), the weekly as the fallback; both sub-resolution -> inconclusive.
    ratio = None
    base = None
    for ventana in ("session", "weekly"):
        salado_pp, replay_pp = dpp[f"salted_{ventana}"], dpp[f"replay_{ventana}"]
        if (
            isinstance(salado_pp, (int, float))
            and salado_pp > 0
            and isinstance(replay_pp, (int, float))
        ):
            ratio = replay_pp / salado_pp
            base = ventana
            break
    estable = salado["settle_exit"] == "stable" and repeticion["settle_exit"] == "stable"
    alarma = ratio is not None and estable and ratio > CANARY_ALARM_RATIO
    if fallo_canary:
        estado_canary = "failed"
    elif alarma:
        estado_canary = "alarm"
    elif ratio is not None and estable:
        estado_canary = "ok"
    else:
        estado_canary = "inconclusive"
    causa_inconclusa = ""
    if estado_canary == "inconclusive":
        causa_inconclusa = (
            " - the ratio was unmeasurable (sub-tick volleys)"
            if ratio is None
            else " - a volley's registration settle never stabilized (capped); the "
            "ratio is not trustworthy evidence"
        )
    linea = {
        "canary_id": f"{manifiesto.run_id}-canary",
        "run_id": manifiesto.run_id,
        "level": level,
        "model": CANARY_MODEL,
        "workload": CANARY_WORKLOAD,
        "body_fixture_hash": fixtures.fixture_hash(specs_cuerpo),
        "body_sha256": lane.prompt_sha256(cuerpo),
        "salted": {
            "nonce_sha256": [lane.nonce_sha256(n) for n in salados],
            "seeds": salado["seeds"],
            "outcomes": salado["outcomes"],
        },
        "replay": {
            "nonce_sha256": lane.nonce_sha256(nonce_replay),
            "seeds": repeticion["seeds"],
            "outcomes": repeticion["outcomes"],
        },
        "meter": {
            "salted_pre": salado["meter"]["pre"],
            "salted_post": salado["meter"]["post"],
            "replay_pre": repeticion["meter"]["pre"],
            "replay_post": repeticion["meter"]["post"],
        },
        "dpp": dpp,
        "ratio": ratio,
        "ratio_basis": base,
        "alarm": alarma,
        "reads": {"salted": salado["reads"], "replay": repeticion["reads"]},
        "settle_exits": {"salted": salado["settle_exit"], "replay": repeticion["settle_exit"]},
        "table_version": cfg["table_version"],
        "protocol_version": PROTOCOL_VERSION,
        "notes": (
            "5 salted requests (fresh nonces, full price) + 5 identical-prefix replays "
            "(salted[0]'s nonce, the cache discount); the ratio mounts on the session "
            "window with the weekly as corroboration; alarm above "
            f"{CANARY_ALARM_RATIO} aborts the run at the gate"
            + causa_inconclusa
            + (f" - incomplete: {fallo_canary}" if fallo_canary else "")
        ),
        "at": time.time(),
    }
    schema.validate_canary_line(linea)
    _append_jsonl(ctx.ruta_canary, linea)
    manifiesto.doc["canary"] = {
        "status": estado_canary,
        "ratio": ratio,
        "ratio_basis": base,
        "alarm": alarma,
        "model": CANARY_MODEL,
        "at": linea["at"],
        "dpp": dpp,  # the canary's own quota spend (its volleys are bracketed too)
        "settle_exits": {"salted": salado["settle_exit"], "replay": repeticion["settle_exit"]},
    }
    manifiesto.save()
    if fallo_canary:
        if emit:
            emit(f"canary: FAILED - {fallo_canary}")
        raise RunnerError(fallo_canary)
    if emit:
        if alarma:
            veredicto = "ALARM: the replay billed near full price - the run aborts at the gate"
        elif estado_canary == "ok":
            veredicto = "the lane holds"
        else:
            veredicto = (
                f"inconclusive{causa_inconclusa} - proceeding, the passive detector "
                "watches the brackets"
            )
        emit(f"canary: ratio {_fmt_ratio(ratio)} ({base or 'unmeasurable'}) - " + veredicto)
    if alarma:
        raise RunnerError(
            f"billing canary: replay ratio {_fmt_ratio(ratio)} > {CANARY_ALARM_RATIO} - "
            "the cache-free lane's salting is broken (the replays billed near full "
            "price); the run aborts at the gate with no bracket measured. The canary "
            "runs on kimi-k3, the model whose discount the paired probe measured - a "
            "full-price replay there is an alarm, not honest pricing - verify before "
            "deleting the manifest"
        )
    return manifiesto.doc["canary"]


def _fmt_ratio(ratio) -> str:
    return "n/a" if ratio is None else f"{ratio:.3f}"


async def _burst(
    client: OllamaCloud,
    spec: BatchSpec,
    specs: tuple,
    modelo_api: str,
    *,
    salt=None,
    coords: list[tuple[str, int, int]] | None = None,
) -> list[dict]:
    """The batch's N requests, k-concurrent; no warmup, no auto-retry.

    `specs` are the batch's request specs in send order (prompt + tool
    schemas); each request re-derives its seed from the cell coordinates.
    `coords` — one (workload, rep, index-within-unit) triple per request,
    aligned with `specs` — parameterizes seeds and nonces across the bracket's
    units (a per-cell bracket's reps, a pooled one's workloads); the default
    treats the batch as one single (workload, rep) unit. `modelo_api` is the
    id actually sent (the preflight's catalog match — the live catalog tags ids
    the price table lists untagged); the dataset records the slate id, the
    manifest's catalog history carries the mapping. `salt` ((workload, rep,
    index) -> nonce text) is the cache-free lane's salter: when given, every
    request's prompt carries its nonce as the first tokens and the record
    persists both hashes.
    """
    if coords is None:
        coords = [(spec.workload, spec.rep, i) for i in range(spec.n)]
    semaforo = asyncio.Semaphore(spec.k)

    async def _one(pos: int) -> dict:
        workload, rep, indice = coords[pos]
        seed_value = fixtures.seed(workload, spec.model, rep, indice)
        async with semaforo:
            if pos < len(spec.gap_s) and spec.gap_s[pos]:
                await asyncio.sleep(spec.gap_s[pos])
            nonce = salt(workload, rep, indice) if salt else None
            prompt = lane.salted_prompt(specs[pos].prompt, nonce) if nonce else specs[pos].prompt
            rec = await client.chat(
                model=modelo_api,
                prompt=prompt,
                seed=seed_value,
                tools=list(specs[pos].tools) or None,
            )
            # The line always pins the exact prompt billed; the nonce hash is
            # null only on exempt (unsalted) traffic.
            rec["prompt_sha256"] = lane.prompt_sha256(prompt)
            if nonce:
                rec["nonce_sha256"] = lane.nonce_sha256(nonce)
            return rec

    return list(await asyncio.gather(*(_one(pos) for pos in range(spec.n))))


@dataclasses.dataclass(frozen=True)
class BatchContext:
    """Everything one bracketed batch needs beyond its spec and the client."""

    base: pathlib.Path
    manifiesto: Manifest
    cfg: dict
    rutas_requests: pathlib.Path
    ruta_batches: pathlib.Path
    ruta_canary: pathlib.Path | None = None  # the billing canary's line (measured runs)


def _notes(*partes: str) -> str:
    """The batch line's notes: the non-empty parts, abort causes first."""
    return "; ".join(p for p in partes if p)


def _label(spec: BatchSpec) -> str:
    """The bracket's progress label: workload/model, or pool[...]/model."""
    return f"{spec.workload or 'pool[' + '+'.join(spec.pool) + ']'}/{spec.model}"


def _passive_detector(registros: list[dict], dpp_weekly: float | None) -> dict:
    """The canary's passive companion: the closed bracket's Δpp against the #28
    token budget.

    The budget prices the bracket's REPORTED tokens at the #28 family rates
    (weekly pp/1M, split by in/out share); a bracket whose budget predicts a
    readable Δpp (>= 3.5 ticks, the note's planning floor) but measures none is
    a collapse — the signature of broken salting under the lane. The across-model
    spread is wide, so anything above zero is recorded without a flag; the
    collapse flag rides the manifest entry and, when it fires, the batch line's
    notes. The threshold is deferred until v3 data exists.
    """
    tokens_in = tokens_out = 0
    for rec in registros:
        pasos = rec.get("steps") or []
        if pasos:
            tin, tout = _sum_steps(pasos, "tok_in"), _sum_steps(pasos, "tok_out")
        else:
            done = rec.get("done")
            tin = done.get("prompt_eval_count") if done else None
            tout = done.get("eval_count") if done else None
        if isinstance(tin, int):
            tokens_in += tin
        if isinstance(tout, int):
            tokens_out += tout
    tokens = tokens_in + tokens_out
    if tokens <= 0:
        return {"expected_pp": None, "measured_pp": dpp_weekly, "collapsed": False}
    familia = "prefill" if tokens_in / tokens >= DETECTOR_PREFILL_SHARE else "generation"
    esperado = tokens / 1_000_000 * DETECTOR_RATES[familia]
    # A collapse is a bracket whose budget predicts a readable Δpp but measures
    # LESS THAN ONE TICK (below the meter's quantum, the tick read through the
    # comparison band — an exact-zero test would miss a 1-tick residue left by
    # a partial collapse or an unrelated consumer of the key).
    colapsado = bool(
        dpp_weekly is not None
        and dpp_weekly < TICK_PP * (1 - TICK_BAND)
        and esperado >= DETECTOR_TICKS_FLOOR * TICK_PP
    )
    return {
        "expected_pp": esperado,
        "measured_pp": dpp_weekly,
        "collapsed": colapsado,
    }


def _mark(
    manifiesto: Manifest, spec: BatchSpec, status: str, *, include_rep: bool = False, **extra
) -> None:
    """One manifest state write: the bracket's identity kwargs in one place.
    `rep` is included only when the caller asks — some abort paths predate it,
    and bench status renders entrada.get("rep"), so normalizing would change
    the persisted manifest."""
    kw: dict = {"workload": spec.workload, "model": spec.model}
    if include_rep:
        kw["rep"] = spec.rep
    kw["pool"] = list(spec.pool) or None
    kw.update(extra)
    manifiesto.set(spec.batch_id, status, **kw)


def _unit_plan(spec: BatchSpec, level: str, unidades, sal) -> tuple:
    """The bracket's static plan, built once before any request: the units'
    fixture specs, per-request coordinates, per-workload fixture hashes and
    T3's per-turn salter (the agent loop salts every step)."""
    specs_unidades = [
        (workload, rep, fixtures.build(level, workload, peticiones))
        for workload, rep, peticiones in unidades
    ]
    specs_requeridos = tuple(spec for _w, _r, u in specs_unidades for spec in u)
    # Per-request coordinates for seeds and nonces: (workload, rep, index
    # within the unit) in send order — the same derivation a per-rep
    # bracket would use, so pooling never re-rolls a prompt.
    coords = [(workload, rep, i) for workload, rep, u in specs_unidades for i in range(len(u))]
    # Each request line pins its own workload's fixture hash (the
    # workload's one-run fixture, composition-independent); the batch line
    # pins spec.fixture_hash (the workload's, or the pool's one-rep
    # sequence for a pooled bracket).
    hashes = {workload: fixtures.fixture_hash(u) for workload, _rep, u in specs_unidades}
    sal_t3 = None
    if level == "T3":
        sal_t3 = (
            (lambda indice, turno=None, _u=(spec.workload, spec.rep): sal(*_u, indice, turno))
            if sal
            else None
        )
    return specs_requeridos, coords, hashes, sal_t3


async def _pre_read_or_abort(client: OllamaCloud, spec: BatchSpec, manifiesto: Manifest) -> dict:
    """The bracket's meter pre-read: a failed read aborts the batch before any
    request — the manifest says so, and the error names the bracket."""
    try:
        status, pre = await client.usage()
    except Exception as e:  # noqa: BLE001 - a meter failure aborts cleanly, loudly
        _mark(
            manifiesto,
            spec,
            "aborted",
            note=f"aborted: meter read failed ({type(e).__name__}: {e}) before the batch",
        )
        raise RunnerError(
            f"batch {spec.batch_id}: meter read failed ({type(e).__name__}: {e}) before the batch"
        ) from None
    if status != 200 or pre is None:
        manifiesto.set(spec.batch_id, "aborted")
        raise RunnerError(f"meter read failed (HTTP {status}) before batch {spec.batch_id}")
    return pre


async def _execute_batch(
    client: OllamaCloud, spec: BatchSpec, *, ctx: BatchContext
) -> BatchOutcome:
    """One bracketed batch, from in_flight to its closed bracket; raises RunnerError on abort.

    The single protocol both spending paths share (`run`'s cells and the
    concurrency workstream's k-cells alike): meter pre-read -> burst (salted
    under the cache-free lane) -> per-model count check (~0.2 s, the
    registration loop's first sample) -> the registration settle -> the last
    read as the bracket's post -> schema-validated raw lines.
    `spec.plan_note` is informational provenance carried on the batch line; only
    a checker failure or a bracket failure aborts the batch.
    """
    cfg = ctx.cfg
    manifiesto = ctx.manifiesto
    level = spec.level
    _mark(manifiesto, spec, "in_flight")
    pre = await _pre_read_or_abort(client, spec, manifiesto)

    modelo_api = cfg.get("model_map", {}).get(spec.model, spec.model)
    nota_checker = ""
    sal = _salter(cfg, spec)
    # The bracket's units: (workload, rep, requests) in send order. Specs the
    # workstreams build themselves (calibration, probe) carry no units — their
    # batch is one single (workload, rep) unit of n requests.
    unidades = spec.units or ((spec.workload, spec.rep, spec.n),)
    try:
        specs_requeridos, coords, hashes, sal_t3 = _unit_plan(spec, level, unidades, sal)
        if level == "T3":
            # T3's burst IS the agent loop: each task consults the model
            # step by step over its own working copy, and every step is
            # one billed chat request — salted per turn under the lane.
            registros = await agent.run_tasks(
                client,
                spec,
                specs_requeridos,
                modelo_api,
                sandbox_root=ctx.base / "sandbox" / manifiesto.run_id,
                salt=sal_t3,
            )
            ok = sum(1 for r in registros for p in r["steps"] if p["http"] == 200)
            intentados = sum(len(r["steps"]) for r in registros)
        else:
            registros = await _burst(
                client, spec, specs_requeridos, modelo_api, salt=sal, coords=coords
            )
            ok = sum(1 for r in registros if r["http"] == 200)
            intentados = spec.n
        try:
            veredictos = _judge_units(specs_requeridos, registros, unidades)
        except checkers.CheckersError as e:
            # Checker drift is a harness bug, not a model outcome: the billed
            # requests are still logged (null verdicts) and the batch aborts.
            nota_checker = f"aborted: checker failure - {type(e).__name__}: {e}"
            if cfg["emit"]:
                cfg["emit"](f"batch {spec.batch_id} ({_label(spec)}): {nota_checker}")
            veredictos = [None] * len(registros)
        for idx, rec in enumerate(registros):
            workload, rep, indice = coords[idx]
            linea = _request_line(
                rec,
                spec,
                run_id=manifiesto.run_id,
                level=level,
                index=idx,
                workload=workload,
                rep=rep,
                fixture_hash=hashes[workload],
                seed_value=fixtures.seed(workload, spec.model, rep, indice),
                table_version=cfg["table_version"],
                checker=veredictos[idx],
            )
            schema.validate_request_line(linea)
            _append_jsonl(ctx.rutas_requests, linea)
    except Exception as e:  # noqa: BLE001 - any failure aborts the batch, loudly
        nota = f"aborted: {type(e).__name__}: {e}"
        _mark(manifiesto, spec, "aborted", note=nota)
        raise RunnerError(f"batch {spec.batch_id} ({_label(spec)}): {nota}") from None
    if ok == 0:
        # A fully rejected burst bills nothing but measures nothing either:
        # recorded as aborted (the request lines above carry the evidence),
        # never as a silent done cell.
        nota = (
            f"aborted: 0 of {intentados} requests accepted - the endpoint rejected "
            "every request (model id or catalog drift?); nothing was billed"
        )
        _mark(manifiesto, spec, "aborted", include_rep=True, note=nota)
        raise RunnerError(f"batch {spec.batch_id} ({_label(spec)}): {nota}")
    t_burst_end = time.time()

    # Per-model count check, issued immediately after the burst (<= ~2 s):
    # the counter is instant and exact, so a dropped request aborts here.
    error_lectura = ""
    try:
        status_c, leido = await client.usage()
    except Exception as e:  # noqa: BLE001 - the bracket still closes below
        status_c, leido, error_lectura = 0, None, f"{type(e).__name__}: {e}"
    count_check_s = time.time() - t_burst_end
    counts_pre = _counts(pre)
    counts_check = _counts(leido)
    contados = counts_check.get(modelo_api, 0) - counts_pre.get(modelo_api, 0)

    post: dict | None = None
    abort_headline = ""
    if not (status_c == 200 and contados == ok):
        # Close the bracket even on abort: the burst's real consumption belongs
        # to THIS batch, not to the next run's pre-read. The registration loop
        # still runs (first sample null when the count-check read itself died),
        # so the aborted bracket's post payload carries the spend it can see.
        abort_headline = (
            f"aborted: meter read failed ({error_lectura}) at the count check"
            if error_lectura
            else (
                f"aborted: request_count check failed - expected {ok} accepted "
                f"requests, meter counted {contados} (delta {contados - ok})"
            )
        )
    # The registration settle: the count-check read is the loop's first
    # sample; the loop polls until two consecutive reads agree in both
    # windows, or the cap burns (the bracket still closes, marked capped).
    registro = await _registration_settle(
        client,
        primera=leido if status_c == 200 else None,
        cap_s=cfg["settle_s"],
        poll_s=cfg["settle_poll_s"],
    )
    post = registro["post"]
    if registro["exit"] is None:
        if not abort_headline:
            causa = registro["error"] or "unknown meter failure"
            # A checker-invalidated batch stays invalidated: the note must
            # say BOTH causes, or the operator re-runs a suite whose
            # verdicts were never valid.
            nota = _notes(f"aborted: meter read failed ({causa}) during registration", nota_checker)
            _close_batch(
                ctx.ruta_batches,
                spec,
                manifiesto,
                cfg,
                pre,
                None,
                counts_pre,
                counts_check,
                _wall_clock_s(registros),
                ok,
                _notes(nota, spec.plan_note),
                settle=registro,
                count_check_s=None,
            )
            _mark(
                manifiesto,
                spec,
                "aborted",
                include_rep=True,
                dpp_session=None,
                dpp_weekly=None,
                requests_ok=ok,
                note=nota,
            )
            raise RunnerError(f"batch {spec.batch_id} ({_label(spec)}): {nota}")
        post = None  # an aborted batch may carry a null post payload

    notas = _notes(abort_headline, nota_checker, spec.plan_note)
    dpp_sesion = _dpp(pre, post, "session")
    wall_clock = _wall_clock_s(registros)
    # The passive detector: the closed bracket's Δpp against the #28 token
    # budget (a collapse below a readable prediction is broken salting's
    # signature; the threshold is refined once v3 data exists).
    detectoro = _passive_detector(registros, _dpp(pre, post, "weekly"))
    if detectoro.get("collapsed"):
        notas = _notes(
            notas,
            "passive detector: the bracket's weekly dpp measures "
            f"{detectoro['measured_pp']:g} pp against a token budget of "
            f"{detectoro['expected_pp']:.2f} pp (broken salting signature? threshold "
            "deferred, #28)",
        )
    _close_batch(
        ctx.ruta_batches,
        spec,
        manifiesto,
        cfg,
        pre,
        post,
        counts_pre,
        counts_check,
        wall_clock,
        ok,
        notas,
        settle=registro,
        count_check_s=count_check_s,
    )
    # Only a real failure aborts; spec.plan_note is provenance, never a verdict.
    estado_final = "aborted" if (abort_headline or nota_checker) else "done"
    _mark(
        manifiesto,
        spec,
        estado_final,
        include_rep=True,
        dpp_session=dpp_sesion,
        dpp_weekly=_dpp(pre, post, "weekly"),
        requests_ok=ok,
        settle_exit=registro["exit"],  # a capped bracket's read is analysis-visible
        detector=detectoro,
    )
    if estado_final == "aborted":
        raise RunnerError(f"batch {spec.batch_id} ({_label(spec)}): {notas}")
    return BatchOutcome(
        ok=ok,
        intentados=intentados,
        dpp_session=dpp_sesion,
        wall_clock_s=wall_clock,
    )


async def _run_async(cfg: dict) -> dict:
    base: pathlib.Path = cfg["base"]
    level: str = cfg["level"]
    runs_dir = base / "runs"
    batches_dir = base / "batches"
    runs_dir.mkdir(parents=True, exist_ok=True)
    batches_dir.mkdir(parents=True, exist_ok=True)

    manifiesto = open_workstream_manifest(runs_dir, level=level, run_id_prefix=level, cfg=cfg)
    run_id = manifiesto.run_id
    try:
        specs = plan(
            run_id=run_id,
            level=level,
            workloads=cfg["workloads"],
            models=cfg["models"],
            reps=cfg["reps"],
            rep_filter=cfg["rep_filter"],
            k=cfg["k"],
        )
    except ValueError as e:  # fixture drift is a clean abort, not a traceback
        raise RunnerError(f"run aborted before any request: {e}") from None
    # The plan is the max union of everything this run_id has ever covered:
    # a wider resume grows it, and status's pending count stays truthful.
    planned_previo = manifiesto.doc.get("planned")
    union = max(
        planned_previo if isinstance(planned_previo, int) else 0,
        len(specs),
        len(manifiesto.doc["batches"]),
    )
    if union != manifiesto.doc.get("planned"):
        manifiesto.doc["planned"] = union
        manifiesto.save()
    rutas_requests = runs_dir / f"requests-{run_id}.jsonl"
    ruta_batches = batches_dir / f"batches-{manifiesto.run_id}.jsonl"
    ruta_canary = runs_dir / f"canary-{run_id}.jsonl"

    client = OllamaCloud(transport=cfg["transport"])
    contexto = BatchContext(
        base=base,
        manifiesto=manifiesto,
        cfg=cfg,
        rutas_requests=rutas_requests,
        ruta_batches=ruta_batches,
        ruta_canary=ruta_canary,
    )
    hechos = omitidos = en_vuelo = abortados_previos = escritas = 0
    try:
        if cfg.get("lane") and specs:
            # The billing canary opens the run: the lane must be proven before
            # the first bracket bills anything under it.
            await _ensure_canary(client, ctx=contexto, cfg=cfg, level=level)
        for spec in specs:
            estado = manifiesto.status(spec.batch_id)
            if estado in ("done", "in_flight", "aborted"):
                omitidos += 1
                nota_resume = ""
                if estado == "in_flight":
                    en_vuelo += 1
                    nota_resume = (
                        f"resume: batch {spec.batch_id} ({_label(spec)}) is in_flight from "
                        "an interrupted run - skipped, never silently retried"
                    )
                elif estado == "aborted":
                    abortados_previos += 1
                    nota_resume = (
                        f"resume: batch {spec.batch_id} ({_label(spec)}) aborted in an earlier "
                        "attempt - skipped; its spend is already in the dataset"
                    )
                if nota_resume and cfg["emit"]:
                    cfg["emit"](nota_resume)
                continue
            resultado = await _execute_batch(client, spec, ctx=contexto)
            hechos += 1
            escritas += spec.n
            if cfg["emit"]:
                cfg["emit"](
                    f"[{hechos + omitidos}/{len(specs)}] {_label(spec)} "
                    f"rep{spec.rep} k{spec.k}: {resultado.ok}/{resultado.intentados} ok, "
                    f"dpp_session={resultado.dpp_session}"
                )
    finally:
        await client.aclose()

    return {
        "run_id": manifiesto.run_id,
        "level": level,
        "table_version": cfg["table_version"],
        "batches_planned": len(specs),
        "batches_done": hechos,
        "batches_skipped_done": omitidos - en_vuelo - abortados_previos,
        "batches_in_flight_skipped": en_vuelo,
        "batches_aborted_skipped": abortados_previos,
        "requests_written": escritas,
    }


def _check_drift(existente: Manifest, cfg: dict) -> None:
    """A manifest binds its run: table, protocol, fixture scheme, composition
    and k may not drift."""
    if existente.doc.get("table_version") != cfg["table_version"]:
        raise RunnerError(
            f"manifest {existente.ruta.name} belongs to table "
            f"{existente.doc.get('table_version')!r} but this run would use "
            f"{cfg['table_version']!r} - finish or archive that run (delete its manifest) "
            "before running another table"
        )
    if existente.doc.get("protocol_version") != PROTOCOL_VERSION:
        raise RunnerError(
            f"manifest {existente.ruta.name} was written under protocol "
            f"{existente.doc.get('protocol_version')!r}; this harness speaks "
            f"{PROTOCOL_VERSION!r} - keep the datasets apart"
        )
    if existente.doc.get("fixture_version") != FIXTURE_VERSION:
        # The fixture_hash algorithm (or the fixture bytes it pins) changed: a
        # resumed run_id would mix incomparable hashes under one dataset.
        raise RunnerError(
            f"manifest {existente.ruta.name} was written with fixture scheme "
            f"{existente.doc.get('fixture_version')!r}; this harness produces "
            f"{FIXTURE_VERSION!r} - the batch hashes are not comparable - keep the "
            "datasets apart"
        )
    if existente.doc.get("composition", "per-rep") != COMPOSITION_VERSION:
        # The bracket composition changed: the manifest's batch ids name
        # per-rep brackets, and the hybrid plan would read its 5-rep pooled
        # brackets as done on rep-1 id collisions (a cell billed once, measured
        # under a different bracket shape) - a resume never mixes compositions.
        raise RunnerError(
            f"manifest {existente.ruta.name} was written with composition "
            f"{existente.doc.get('composition', 'per-rep (pre-hybrid)')!r}; this harness "
            f"plans {COMPOSITION_VERSION!r} (methodology v1.1 §5's hybrid: the strong "
            "four per-cell, the weak trio pooled per model) - resuming would mix "
            "incomparable batch ids - keep the datasets apart"
        )
    k_cfg = cfg.get("k")
    if k_cfg is not None and existente.doc.get("k") != k_cfg:
        raise RunnerError(
            f"manifest {existente.ruta.name} records k={existente.doc.get('k')!r} but this "
            f"run would use k={k_cfg!r} - mixing k inside one run_id would duplicate cells"
        )
    reps_cfg = cfg.get("reps")
    if (
        reps_cfg is not None
        and existente.doc.get("level") == "T2"
        and existente.doc.get("reps") != reps_cfg
    ):
        # The hybrid composition (T2 only) anchors its brackets' batch ids on
        # the first rep alone — the strong four pool all the cell's reps into
        # one bracket, the weak trio pools the trio's reps per model — so a
        # resume at another density would read the earlier brackets done (a
        # wide resume would measure nothing new) or collide with them. The
        # per-rep compositions (T1/T3) never collide — their batch ids embed
        # the rep, so their wider resume still grows the plan the union allows.
        raise RunnerError(
            f"manifest {existente.ruta.name} was planned at --reps "
            f"{existente.doc.get('reps')!r} but this run would use --reps {reps_cfg!r} - "
            "the pooled brackets' batch ids do not separate densities, and a resume at "
            "another one would misread the run state - finish or archive that run "
            "(delete its manifest) before running another density"
        )


def _close_batch(
    ruta_batches: pathlib.Path,
    spec: BatchSpec,
    manifiesto: Manifest,
    cfg: dict,
    pre: dict | None,
    post: dict | None,
    counts_pre: dict[str, int],
    counts_check: dict[str, int],
    wall_clock_s: float | None,
    ok: int,
    notes: str,
    settle: dict,
    count_check_s: float | None,
) -> None:
    """The bracket's one schema-validated raw line: built, validated, appended —
    the abort path and the done path both close through here."""
    linea = _batch_line(
        spec,
        manifiesto=manifiesto,
        pre=pre,
        post=post,
        counts_pre=counts_pre,
        counts_check=counts_check,
        counts_post=_counts(post) if post else None,
        count_check_s=count_check_s,
        wall_clock_s=wall_clock_s,
        ok=ok,
        settle_s=cfg["settle_s"],
        settle=settle,
        table_version=cfg["table_version"],
        notes=notes,
    )
    schema.validate_batch_line(linea)
    _append_jsonl(ruta_batches, linea)


def _batch_line(
    spec: BatchSpec,
    *,
    manifiesto: Manifest,
    pre: dict | None,
    post: dict | None,
    counts_pre: dict[str, int],
    counts_check: dict[str, int],
    counts_post: dict[str, int] | None,
    count_check_s: float | None,
    wall_clock_s: float | None,
    ok: int,
    settle_s: float,
    settle: dict,
    table_version: str,
    notes: str = "",
) -> dict:
    return {
        "batch_id": spec.batch_id,
        "run_id": manifiesto.run_id,
        "level": spec.level,
        "workload": spec.workload,  # null on a pooled bracket (the pool names them)
        "model": spec.model,
        "fixture_hash": spec.fixture_hash,
        "k": spec.k,
        "n": spec.n,
        "reps": spec.reps,  # the repetitions the bracket pools (1 on a single-rep one)
        # A pooled bracket names its workloads and repetition count: the legacy
        # attribution per workload derives post-hoc from the request lines'
        # tokens — never a stored weight. Null on a per-cell bracket.
        "pool": {"workloads": list(spec.pool), "reps": spec.reps} if spec.pool else None,
        "settle_s": settle_s,
        # Protocol v3's registration settle: the loop's mode, poll count, exit
        # reason and per-window registration times (seconds after the loop's
        # first sample; 0.0 = already at its final value there).
        "settle_mode": "registration",
        "settle_reads": settle["reads"],
        "registered_session_s": settle["registered_session_s"],
        "registered_weekly_s": settle["registered_weekly_s"],
        "settle_exit": settle["exit"],
        "count_check_s": count_check_s,
        "wall_clock_s": wall_clock_s,
        "medidor_pre": pre,
        "medidor_post": post,
        "dpp_session": _dpp(pre, post, "session"),
        "dpp_weekly": _dpp(pre, post, "weekly"),
        "request_counts": {"pre": counts_pre, "count_check": counts_check, "post": counts_post},
        "table_version": table_version,
        "protocol_version": PROTOCOL_VERSION,
        "notes": notes,
    }


def run_level(
    base,
    *,
    level: str,
    workloads,
    models: list[str],
    reps: int,
    rep_filter: int | None,
    k: int,
    settle_s: float,
    settle_poll_s: float = 5.0,
    table_version: str,
    catalog: dict | None = None,
    model_map: dict[str, str] | None = None,
    transport=None,
    emit=print,
) -> dict:
    """Executes the level's batches under the bracketed protocol; raises RunnerError.

    The measured workstream always runs under the cache-free lane (protocol v3):
    `lane=True` makes the manifest record the nonce spec and the billing canary
    open the run."""
    cfg = {
        "base": pathlib.Path(base),
        "level": level,
        "workloads": workloads,
        "models": models,
        "reps": reps,
        "rep_filter": rep_filter,
        "k": k,
        "settle_s": settle_s,
        "settle_poll_s": settle_poll_s,
        "lane": True,
        "table_version": table_version,
        "catalog": catalog,
        "model_map": model_map or {},
        "transport": transport,
        "emit": emit,
    }
    return asyncio.run(_run_async(cfg))
