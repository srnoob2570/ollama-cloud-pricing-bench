"""The bracketed-batch runner (methodology v1 §4).

Per batch = one (workload, model, rep, k) cell:

1. raw meter read (full payload kept);
2. the batch's N requests — streaming, k-concurrent via semaphore, **no warmup,
   no in-batch auto-retry** (every request the meter sees is an intended one);
3. per-model `request_count` check issued immediately after the burst (the
   counter is instant and exact — a dropped request aborts the run; the bracket
   is still closed so the aborted batch's real spend is attributed to it);
4. settle >= `settle_s` (90 s by default: the quota % lags ~60–90 s and
   quantizes at 0.001 = 0.1 pp);
5. second raw read -> Δpp per window, attributed to this batch alone.

`batch_id` is deterministic from (run, level, workload, model, rep, k); the
per-level manifest records in_flight/done/aborted. A re-run resumes done batches
and **skips** aborted/in_flight ones with a loud report — an aborted batch is
never silently retried (its requests are already billed; `bench status` shows
the state and recovery stays an explicit operator decision).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import pathlib
import time
import uuid

from . import agent, checkers, fixtures, schema
from .client import PROTOCOL_VERSION, OllamaCloud
from .fixtures import FIXTURE_VERSION


class RunnerError(Exception):
    """The protocol aborted (meter failure, dropped request, or run-state drift)."""


@dataclasses.dataclass(frozen=True)
class BatchSpec:
    level: str
    batch_id: str
    workload: str
    model: str
    rep: int
    k: int
    n: int  # requests in the batch (the workload's requests per repetition)
    fixture_hash: str
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
    """All batches of the run, round-robin across the slate: workload -> rep -> model."""
    specs: list[BatchSpec] = []
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
        catalog: dict | None = None,
    ) -> Manifest:
        doc = {
            "run_id": run_id,
            "level": level,
            "table_version": table_version,
            "protocol_version": PROTOCOL_VERSION,
            "fixture_version": FIXTURE_VERSION,
            "k": k,
            "started_at": round(time.time(), 3),
            "planned": planned,
            "batches": {},
        }
        if catalog is not None:  # /v1/models snapshots, one per attempt (provenance)
            doc["catalog"] = [{"captured_at": round(time.time(), 3), **catalog}]
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
        entrada = {"status": status, "at": round(time.time(), 3), **extra}
        self.doc["batches"][bid] = entrada
        self.save()

    def append_catalog(self, catalogo: dict) -> None:
        """Adds one /v1/models snapshot to the catalog history (provenance)."""
        self.doc.setdefault("catalog", []).append(
            {"captured_at": round(time.time(), 3), **catalogo}
        )

    def save(self) -> None:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ruta.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.doc, indent=2), encoding="utf-8")
        tmp.replace(self.ruta)


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
        catalog=cfg.get("catalog"),
    )
    if existente:
        if cfg.get("catalog"):
            manifiesto.append_catalog(cfg["catalog"])
        manifiesto.save()
    return manifiesto


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
    """Quota delta in percentage points (a 0.001 usage step = 0.1 pp); None if unreadable."""
    try:
        antes = pre["limits"][window]["usage"]
        despues = post["limits"][window]["usage"]
    except (KeyError, TypeError):
        return None
    if not isinstance(antes, (int, float)) or not isinstance(despues, (int, float)):
        return None
    return round((despues - antes) * 100, 1)


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
    return round(max(fin for _ini, fin in tiempos) - min(ini for ini, _fin in tiempos), 6)


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
        "workload": spec.workload,
        "model": spec.model,
        "seed": seed_value,
        "rep": spec.rep,
        "k": spec.k,
        "t_start": round(rec["t_start"], 6),
        "t_first_chunk": None if rec["t_first_chunk"] is None else round(rec["t_first_chunk"], 6),
        "t_total": round(rec["t_total"], 6),
        "chunks": rec["chunks"],
        "tok_in": tok_in,
        "tok_out": tok_out,
        "tok_cached": tok_cached,
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
        "fixture_hash": spec.fixture_hash,
        "table_version": table_version,
        "protocol_version": PROTOCOL_VERSION,
    }


def _append_jsonl(ruta: pathlib.Path, line: dict) -> None:
    with ruta.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


async def _burst(client: OllamaCloud, spec: BatchSpec, specs: tuple, modelo_api: str) -> list[dict]:
    """The batch's N requests, k-concurrent; no warmup, no auto-retry.

    `specs` are the workload's request specs (prompt + tool schemas); each
    request re-derives its seed from the cell coordinates. `modelo_api` is the
    id actually sent (the preflight's catalog match — the live catalog tags ids
    the price table lists untagged); the dataset records the slate id, the
    manifest's catalog history carries the mapping.
    """
    semaforo = asyncio.Semaphore(spec.k)

    async def _one(i: int) -> dict:
        seed_value = fixtures.seed(spec.workload, spec.model, spec.rep, i)
        async with semaforo:
            if i < len(spec.gap_s) and spec.gap_s[i]:
                await asyncio.sleep(spec.gap_s[i])
            return await client.chat(
                model=modelo_api,
                prompt=specs[i].prompt,
                seed=seed_value,
                tools=list(specs[i].tools) or None,
            )

    return list(await asyncio.gather(*(_one(i) for i in range(spec.n))))


@dataclasses.dataclass(frozen=True)
class BatchContext:
    """Everything one bracketed batch needs beyond its spec and the client."""

    base: pathlib.Path
    manifiesto: Manifest
    cfg: dict
    rutas_requests: pathlib.Path
    ruta_batches: pathlib.Path


def _notes(*partes: str) -> str:
    """The batch line's notes: the non-empty parts, abort causes first."""
    return "; ".join(p for p in partes if p)


async def _execute_batch(
    client: OllamaCloud, spec: BatchSpec, *, ctx: BatchContext
) -> BatchOutcome:
    """One bracketed batch, from in_flight to its closed bracket; raises RunnerError on abort.

    The single protocol both spending paths share (`run`'s cells and the
    concurrency workstream's k-cells alike): meter pre-read -> burst -> per-model
    count check -> settle >= settle_s -> post read -> schema-validated raw lines.
    `spec.plan_note` is informational provenance carried on the batch line; only
    a checker failure or a bracket failure aborts the batch.
    """
    cfg = ctx.cfg
    manifiesto = ctx.manifiesto
    level = spec.level
    manifiesto.set(spec.batch_id, "in_flight", workload=spec.workload, model=spec.model)

    try:
        status, pre = await client.usage()
    except Exception as e:  # noqa: BLE001 - a meter failure aborts cleanly, loudly
        manifiesto.set(
            spec.batch_id,
            "aborted",
            workload=spec.workload,
            model=spec.model,
            note=f"aborted: meter read failed ({type(e).__name__}: {e}) before the batch",
        )
        raise RunnerError(
            f"batch {spec.batch_id}: meter read failed ({type(e).__name__}: {e}) before the batch"
        ) from None
    if status != 200 or pre is None:
        manifiesto.set(spec.batch_id, "aborted")
        raise RunnerError(f"meter read failed (HTTP {status}) before batch {spec.batch_id}")

    modelo_api = cfg.get("model_map", {}).get(spec.model, spec.model)
    nota_checker = ""
    try:
        specs_requeridos = fixtures.build(level, spec.workload, spec.n)
        if level == "T3":
            # T3's burst IS the agent loop: each task consults the model
            # step by step over its own working copy, and every step is
            # one billed chat request.
            registros = await agent.run_tasks(
                client,
                spec,
                specs_requeridos,
                modelo_api,
                sandbox_root=ctx.base / "sandbox" / manifiesto.run_id,
            )
            ok = sum(1 for r in registros for p in r["steps"] if p["http"] == 200)
            intentados = sum(len(r["steps"]) for r in registros)
        else:
            registros = await _burst(client, spec, specs_requeridos, modelo_api)
            ok = sum(1 for r in registros if r["http"] == 200)
            intentados = spec.n
        try:
            veredictos = checkers.judge(
                spec.workload, [s.prompt for s in specs_requeridos], registros
            )
        except checkers.CheckersError as e:
            # Checker drift is a harness bug, not a model outcome: the billed
            # requests are still logged (null verdicts) and the batch aborts.
            nota_checker = f"aborted: checker failure - {type(e).__name__}: {e}"
            if cfg["emit"]:
                cfg["emit"](f"batch {spec.batch_id} ({spec.workload}/{spec.model}): {nota_checker}")
            veredictos = [None] * len(registros)
        for idx, rec in enumerate(registros):
            linea = _request_line(
                rec,
                spec,
                run_id=manifiesto.run_id,
                level=level,
                index=idx,
                seed_value=fixtures.seed(spec.workload, spec.model, spec.rep, idx),
                table_version=cfg["table_version"],
                checker=veredictos[idx],
            )
            schema.validate_request_line(linea)
            _append_jsonl(ctx.rutas_requests, linea)
    except Exception as e:  # noqa: BLE001 - any failure aborts the batch, loudly
        nota = f"aborted: {type(e).__name__}: {e}"
        manifiesto.set(
            spec.batch_id, "aborted", workload=spec.workload, model=spec.model, note=nota
        )
        raise RunnerError(f"batch {spec.batch_id} ({spec.workload}/{spec.model}): {nota}") from None
    if ok == 0:
        # A fully rejected burst bills nothing but measures nothing either:
        # recorded as aborted (the request lines above carry the evidence),
        # never as a silent done cell.
        nota = (
            f"aborted: 0 of {intentados} requests accepted - the endpoint rejected "
            "every request (model id or catalog drift?); nothing was billed"
        )
        manifiesto.set(
            spec.batch_id,
            "aborted",
            workload=spec.workload,
            model=spec.model,
            rep=spec.rep,
            note=nota,
        )
        raise RunnerError(f"batch {spec.batch_id} ({spec.workload}/{spec.model}): {nota}")
    t_burst_end = time.time()

    # Per-model count check, issued immediately after the burst (<= ~2 s):
    # the counter is instant and exact, so a dropped request aborts here.
    error_lectura = ""
    try:
        status_c, leido = await client.usage()
    except Exception as e:  # noqa: BLE001 - the bracket still closes below
        status_c, leido, error_lectura = 0, None, f"{type(e).__name__}: {e}"
    count_check_s = round(time.time() - t_burst_end, 3)
    counts_pre = _counts(pre)
    counts_check = _counts(leido)
    contados = counts_check.get(modelo_api, 0) - counts_pre.get(modelo_api, 0)

    post: dict | None = None
    abort_headline = ""
    if status_c == 200 and contados == ok:
        await asyncio.sleep(cfg["settle_s"])  # >= 90 s: the % lags ~60-90 s
        try:
            status_p, post = await client.usage()
        except Exception as e:  # noqa: BLE001 - the bracket still closes below
            status_p, post, error_lectura = 0, None, f"{type(e).__name__}: {e}"
        if status_p != 200 or post is None:
            causa = error_lectura or f"HTTP {status_p}"
            # A checker-invalidated batch stays invalidated: the note must
            # say BOTH causes, or the operator re-runs a suite whose
            # verdicts were never valid.
            nota = _notes(f"aborted: meter read failed ({causa}) after the settle", nota_checker)
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
            )
            manifiesto.set(
                spec.batch_id,
                "aborted",
                workload=spec.workload,
                model=spec.model,
                rep=spec.rep,
                dpp_session=None,
                dpp_weekly=None,
                requests_ok=ok,
                note=nota,
            )
            raise RunnerError(f"batch {spec.batch_id} ({spec.workload}/{spec.model}): {nota}")
    else:
        # Close the bracket even on abort: the burst's real consumption belongs
        # to THIS batch, not to the next run's pre-read.
        abort_headline = (
            f"aborted: meter read failed ({error_lectura}) at the count check"
            if error_lectura
            else (
                f"aborted: request_count check failed - expected {ok} accepted "
                f"requests, meter counted {contados} (delta {contados - ok})"
            )
        )
        await asyncio.sleep(cfg["settle_s"])
        try:
            status_p, post = await client.usage()
        except Exception:  # noqa: BLE001 - an aborted batch may lack a post
            status_p, post = 0, None
        if status_p != 200:
            post = None  # an aborted batch may carry a null post payload

    notas = _notes(abort_headline, nota_checker, spec.plan_note)
    dpp_sesion = _dpp(pre, post, "session")
    wall_clock = _wall_clock_s(registros)
    linea = _batch_line(
        spec,
        manifiesto=manifiesto,
        pre=pre,
        post=post,
        counts_pre=counts_pre,
        counts_check=counts_check,
        counts_post=_counts(post) if post else None,
        count_check_s=count_check_s,
        wall_clock_s=wall_clock,
        ok=ok,
        settle_s=cfg["settle_s"],
        table_version=cfg["table_version"],
        notes=notas,
    )
    schema.validate_batch_line(linea)
    _append_jsonl(ctx.ruta_batches, linea)
    # Only a real failure aborts; spec.plan_note is provenance, never a verdict.
    estado_final = "aborted" if (abort_headline or nota_checker) else "done"
    manifiesto.set(
        spec.batch_id,
        estado_final,
        workload=spec.workload,
        model=spec.model,
        rep=spec.rep,
        dpp_session=dpp_sesion,
        dpp_weekly=_dpp(pre, post, "weekly"),
        requests_ok=ok,
    )
    if estado_final == "aborted":
        raise RunnerError(f"batch {spec.batch_id} ({spec.workload}/{spec.model}): {notas}")
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

    client = OllamaCloud(transport=cfg["transport"])
    contexto = BatchContext(
        base=base,
        manifiesto=manifiesto,
        cfg=cfg,
        rutas_requests=rutas_requests,
        ruta_batches=ruta_batches,
    )
    hechos = omitidos = en_vuelo = abortados_previos = escritas = 0
    try:
        for spec in specs:
            estado = manifiesto.status(spec.batch_id)
            if estado in ("done", "in_flight", "aborted"):
                omitidos += 1
                if estado == "in_flight":
                    en_vuelo += 1
                    if cfg["emit"]:
                        cfg["emit"](
                            f"resume: batch {spec.batch_id} ({spec.workload}/{spec.model}) is "
                            "in_flight from an interrupted run - skipped, never silently retried"
                        )
                elif estado == "aborted":
                    abortados_previos += 1
                    if cfg["emit"]:
                        cfg["emit"](
                            f"resume: batch {spec.batch_id} ({spec.workload}/{spec.model}) aborted "
                            "in an earlier attempt - skipped; its spend is already in the dataset"
                        )
                continue
            resultado = await _execute_batch(client, spec, ctx=contexto)
            hechos += 1
            escritas += spec.n
            if cfg["emit"]:
                cfg["emit"](
                    f"[{hechos + omitidos}/{len(specs)}] {spec.workload}/{spec.model} "
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
    """A manifest binds its run: table, protocol, fixture scheme, and k may not drift."""
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
    k_cfg = cfg.get("k")
    if k_cfg is not None and existente.doc.get("k") != k_cfg:
        raise RunnerError(
            f"manifest {existente.ruta.name} records k={existente.doc.get('k')!r} but this "
            f"run would use k={k_cfg!r} - mixing k inside one run_id would duplicate cells"
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
) -> None:
    linea = _batch_line(
        spec,
        manifiesto=manifiesto,
        pre=pre,
        post=post,
        counts_pre=counts_pre,
        counts_check=counts_check,
        counts_post=_counts(post) if post else None,
        count_check_s=None,
        wall_clock_s=wall_clock_s,
        ok=ok,
        settle_s=cfg["settle_s"],
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
    table_version: str,
    notes: str = "",
) -> dict:
    return {
        "batch_id": spec.batch_id,
        "run_id": manifiesto.run_id,
        "level": spec.level,
        "workload": spec.workload,
        "model": spec.model,
        "fixture_hash": spec.fixture_hash,
        "k": spec.k,
        "n": spec.n,
        "settle_s": settle_s,
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
    table_version: str,
    catalog: dict | None = None,
    model_map: dict[str, str] | None = None,
    transport=None,
    emit=print,
) -> dict:
    """Executes the level's batches under the bracketed protocol; raises RunnerError."""
    cfg = {
        "base": pathlib.Path(base),
        "level": level,
        "workloads": workloads,
        "models": models,
        "reps": reps,
        "rep_filter": rep_filter,
        "k": k,
        "settle_s": settle_s,
        "table_version": table_version,
        "catalog": catalog,
        "model_map": model_map or {},
        "transport": transport,
        "emit": emit,
    }
    return asyncio.run(_run_async(cfg))
