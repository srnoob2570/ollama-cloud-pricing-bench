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

from . import checkers, fixtures, schema
from .client import PROTOCOL_VERSION, OllamaCloud


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
        for rep in [rep_filter] if rep_filter else range(1, reps + 1):
            for model in models:
                textos = fixtures.prompts(level, w.name, w.requests)
                specs.append(
                    BatchSpec(
                        level=level,
                        batch_id=batch_id(run_id, level, w.name, model, rep, k),
                        workload=w.name,
                        model=model,
                        rep=rep,
                        k=k,
                        n=w.requests,
                        fixture_hash=fixtures.fixture_hash(textos),
                    )
                )
    return specs


class Manifest:
    """Per-level run state (runs/manifest-<level>.json): resume without re-billing."""

    def __init__(self, ruta: pathlib.Path, doc: dict) -> None:
        self.ruta = ruta
        self.doc = doc

    @classmethod
    def load(cls, ruta: pathlib.Path) -> Manifest | None:
        if not ruta.exists():
            return None
        try:
            doc = json.loads(ruta.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("batches"), dict):
                raise TypeError("missing the batches map")
            if not isinstance(doc.get("run_id"), str):
                raise TypeError("missing run_id")
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
        k: int,
        planned: int,
        catalog: dict | None = None,
    ) -> Manifest:
        doc = {
            "run_id": run_id,
            "level": level,
            "table_version": table_version,
            "protocol_version": PROTOCOL_VERSION,
            "k": k,
            "started_at": round(time.time(), 3),
            "planned": planned,
            "batches": {},
        }
        if catalog is not None:  # the /v1/models snapshot this run was prefighted against
            doc["catalog"] = {"captured_at": round(time.time(), 3), **catalog}
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

    def save(self) -> None:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ruta.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.doc, indent=2), encoding="utf-8")
        tmp.replace(self.ruta)


def _counts(payload: dict | None, window: str = "session") -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    modelos = payload.get("limits", {}).get(window, {}).get("models", [])
    if not isinstance(modelos, list):
        return {}
    return {m["name"]: int(m["request_count"]) for m in modelos if isinstance(m, dict)}


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
        "tok_in": done.get("prompt_eval_count") if done else None,
        "tok_out": done.get("eval_count") if done else None,
        "tok_cached": done.get("prompt_eval_cache_hit_count") if done else None,
        "api": done,
        "http": rec["http"],
        "err": rec["err"],
        "checker": checker,  # real verdict for T1; T2/T3 arrive with later tickets
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


async def _burst(client: OllamaCloud, spec: BatchSpec, textos: list[str]) -> list[dict]:
    """The batch's N requests, k-concurrent; no warmup, no auto-retry."""
    semaforo = asyncio.Semaphore(spec.k)

    async def _one(i: int) -> dict:
        seed_value = fixtures.seed(spec.workload, spec.model, spec.rep, i)
        async with semaforo:
            return await client.chat(model=spec.model, prompt=textos[i], seed=seed_value)

    return list(await asyncio.gather(*(_one(i) for i in range(spec.n))))


async def _run_async(cfg: dict) -> dict:
    base: pathlib.Path = cfg["base"]
    level: str = cfg["level"]
    runs_dir = base / "runs"
    batches_dir = base / "batches"
    runs_dir.mkdir(parents=True, exist_ok=True)
    batches_dir.mkdir(parents=True, exist_ok=True)

    existente = Manifest.load(runs_dir / f"manifest-{level}.json")
    run_id = (
        existente.run_id
        if existente
        else f"{level}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    )
    if existente:
        _check_drift(existente, cfg)
    specs = plan(
        run_id=run_id,
        level=level,
        workloads=cfg["workloads"],
        models=cfg["models"],
        reps=cfg["reps"],
        rep_filter=cfg["rep_filter"],
        k=cfg["k"],
    )
    manifiesto = existente or Manifest.create(
        runs_dir / f"manifest-{level}.json",
        run_id=run_id,
        level=level,
        table_version=cfg["table_version"],
        k=cfg["k"],
        planned=len(specs),
        catalog=cfg.get("catalog"),
    )
    rutas_requests = runs_dir / f"requests-{manifiesto.run_id}.jsonl"
    ruta_batches = batches_dir / f"batches-{manifiesto.run_id}.jsonl"

    client = OllamaCloud(transport=cfg["transport"])
    hechos = omitidos = en_vuelo = abortados_previos = escritas = 0
    try:
        for spec in specs:
            estado = manifiesto.status(spec.batch_id)
            if estado in ("done", "in_flight", "aborted"):
                omitidos += 1
                if estado == "in_flight":
                    en_vuelo += 1
                    cfg["emit"](
                        f"resume: batch {spec.batch_id} ({spec.workload}/{spec.model}) is "
                        "in_flight from an interrupted run - skipped, never silently retried"
                    )
                elif estado == "aborted":
                    abortados_previos += 1
                    cfg["emit"](
                        f"resume: batch {spec.batch_id} ({spec.workload}/{spec.model}) aborted "
                        "in an earlier attempt - skipped; its spend is already in the dataset"
                    )
                continue
            manifiesto.set(spec.batch_id, "in_flight", workload=spec.workload, model=spec.model)

            status, pre = await client.usage()
            if status != 200 or pre is None:
                manifiesto.set(spec.batch_id, "aborted")
                raise RunnerError(f"meter read failed (HTTP {status}) before batch {spec.batch_id}")

            try:
                textos = fixtures.prompts(level, spec.workload, spec.n)
                registros = await _burst(client, spec, textos)
                veredictos = checkers.judge(spec.workload, textos, registros)
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
                    _append_jsonl(rutas_requests, linea)
            except Exception as e:  # noqa: BLE001 - any failure aborts the batch, loudly
                nota = f"aborted: {type(e).__name__}: {e}"
                manifiesto.set(
                    spec.batch_id, "aborted", workload=spec.workload, model=spec.model, note=nota
                )
                raise RunnerError(
                    f"batch {spec.batch_id} ({spec.workload}/{spec.model}): {nota}"
                ) from None
            ok = sum(1 for r in registros if r["http"] == 200)
            t_burst_end = time.time()

            # Per-model count check, issued immediately after the burst (<= ~2 s):
            # the counter is instant and exact, so a dropped request aborts here.
            status_c, leido = await client.usage()
            count_check_s = round(time.time() - t_burst_end, 3)
            counts_pre = _counts(pre)
            counts_check = _counts(leido)
            contados = counts_check.get(spec.model, 0) - counts_pre.get(spec.model, 0)

            post: dict | None = None
            if status_c == 200 and contados == ok:
                await asyncio.sleep(cfg["settle_s"])  # >= 90 s: the % lags ~60-90 s
                status_p, post = await client.usage()
                if status_p != 200 or post is None:
                    nota = f"aborted: meter read failed (HTTP {status_p}) after the settle"
                    _close_batch(
                        ruta_batches,
                        spec,
                        manifiesto,
                        cfg,
                        pre,
                        None,
                        counts_pre,
                        counts_check,
                        ok,
                        nota,
                    )
                    manifiesto.set(
                        spec.batch_id,
                        "aborted",
                        workload=spec.workload,
                        model=spec.model,
                        note=nota,
                    )
                    raise RunnerError(
                        f"batch {spec.batch_id} ({spec.workload}/{spec.model}): {nota}"
                    )
                nota = ""
            else:
                # Close the bracket even on abort: the burst's real consumption belongs
                # to THIS batch, not to the next run's pre-read.
                nota = (
                    f"aborted: request_count check failed - expected {ok} accepted "
                    f"requests, meter counted {contados} (delta {contados - ok})"
                )
                await asyncio.sleep(cfg["settle_s"])
                status_p, post = await client.usage()
                if status_p != 200:
                    post = None  # an aborted batch may carry a null post payload

            linea = _batch_line(
                spec,
                manifiesto=manifiesto,
                pre=pre,
                post=post,
                counts_pre=counts_pre,
                counts_check=counts_check,
                counts_post=_counts(post) if post else None,
                count_check_s=count_check_s,
                ok=ok,
                settle_s=cfg["settle_s"],
                table_version=cfg["table_version"],
                notes=nota,
            )
            schema.validate_batch_line(linea)
            _append_jsonl(ruta_batches, linea)
            estado_final = "done" if not nota else "aborted"
            manifiesto.set(
                spec.batch_id,
                estado_final,
                workload=spec.workload,
                model=spec.model,
                rep=spec.rep,
                dpp_session=_dpp(pre, post, "session"),
                dpp_weekly=_dpp(pre, post, "weekly"),
                requests_ok=ok,
            )
            if nota:
                raise RunnerError(f"batch {spec.batch_id} ({spec.workload}/{spec.model}): {nota}")
            hechos += 1
            escritas += spec.n
            if cfg["emit"]:
                cfg["emit"](
                    f"[{hechos + omitidos}/{len(specs)}] {spec.workload}/{spec.model} "
                    f"rep{spec.rep} k{spec.k}: {ok}/{spec.n} ok, "
                    f"dpp_session={_dpp(pre, post, 'session')}"
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
    """A manifest binds its run: table, protocol, and k may not drift mid-run."""
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
    if existente.doc.get("k") != cfg["k"]:
        raise RunnerError(
            f"manifest {existente.ruta.name} records k={existente.doc.get('k')!r} but this "
            f"run would use k={cfg['k']!r} - mixing k inside one run_id would duplicate cells"
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
    ok: int,
    notes: str,
) -> None:
    _append_jsonl(
        ruta_batches,
        _batch_line(
            spec,
            manifiesto=manifiesto,
            pre=pre,
            post=post,
            counts_pre=counts_pre,
            counts_check=counts_check,
            counts_post=_counts(post) if post else None,
            count_check_s=None,
            ok=ok,
            settle_s=cfg["settle_s"],
            table_version=cfg["table_version"],
            notes=notes,
        ),
    )


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
        "transport": transport,
        "emit": emit,
    }
    return asyncio.run(_run_async(cfg))
