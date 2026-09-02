"""The deterministic T3 agent loop (methodology v1 §5): the harness drives, the
model acts.

Each step is one chat request — the task prompt (goal + action contract) plus the
full transcript so far — whose reply must carry exactly one JSON action. The
harness parses it, executes it against the task's working copy, and appends the
outcome; the loop ends when the model plays `finish`, when MAX_STEPS actions are
spent, when a step's request fails at the transport level (a dead endpoint is
never consulted again, and nothing inside a batch is retried), or when the
harness itself fails to execute an action (a broken working copy is the same
class of stop). The model executes nothing itself: `run_tests` runs the
sandbox's pytest, and the checker re-runs it independently after the loop, so a
model that claims "the tests pass" without passing them lands as a failed
checker. A task that crashes (disk full, a hostile write) still lands in the
dataset as its own record — its siblings' evidence and the batch's count check
survive it.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time

from . import fixtures, lane, sandbox
from .client import OllamaCloud
from .fixtures_t3 import MAX_STEPS

REPLY_LIMIT = 4000  # a longer reply is truncated here and on the step record
RESULT_LIMIT = 1500  # same bound for the harness's execution results (test output)


def parse_action(reply: str) -> dict | None:
    """The single JSON action object the reply carries (first `{` to last `}`), or None."""
    texto = reply.strip()
    if not texto:
        return None
    inicio, fin = texto.find("{"), texto.rfind("}")
    candidato = texto[inicio : fin + 1] if 0 <= inicio < fin else texto
    try:
        accion = json.loads(candidato)
    except ValueError:
        return None
    return accion if isinstance(accion, dict) else None


def _cap(texto: str, limite: int) -> str:
    return texto if len(texto) <= limite else texto[:limite] + f" ...[truncated at {limite} chars]"


def _registro_crash(task_dir: pathlib.Path, causa: str) -> dict:
    """The degenerate record of a task that produced no step at all (its repo
    could not even be seeded): present in the dataset with the crash as its
    err, so the batch's other tasks keep their records and the count check
    still sees the task's real (zero) spend."""
    ahora = time.time()
    return {
        "t_start": ahora,
        "t_first_chunk": None,
        "t_total": ahora,
        "chunks": 0,
        "http": None,
        "err": f"task crashed before its first step: {causa}",
        "done": None,
        "content": "",
        "steps": [],
        "tool_calls": [],
        "repo_dir": str(task_dir),
    }


def _inside(task_dir: pathlib.Path, rel) -> pathlib.Path | None:
    """The task-relative path resolved inside the working copy, or None when it
    escapes (absolute paths, `..`, or a symlink pointing out of the copy)."""
    if not isinstance(rel, str) or not rel:
        return None
    ruta = pathlib.PurePosixPath(rel)
    if ruta.is_absolute() or ".." in ruta.parts:
        return None
    destino = (task_dir / ruta).resolve()
    try:
        destino.relative_to(task_dir.resolve())
    except ValueError:
        return None
    return destino


def execute_action(accion: dict, task_dir: pathlib.Path) -> tuple[bool, str]:
    """One action against the working copy; returns (executed, transcript result)."""
    nombre = accion.get("action")
    if nombre == "list_dir":
        destino = _inside(task_dir, accion.get("path", "."))
        if destino is None:
            return False, "rejected: the path escapes the working copy"
        if not destino.is_dir():
            return False, f"rejected: no such directory: {accion.get('path')!r}"
        entradas = sorted(e.name + ("/" if e.is_dir() else "") for e in destino.iterdir())
        return True, "\n".join(entradas) if entradas else "(empty directory)"
    if nombre == "read_file":
        destino = _inside(task_dir, accion.get("path"))
        if destino is None:
            return False, "rejected: the path escapes the working copy"
        if not destino.is_file():
            return False, f"rejected: no such file: {accion.get('path')!r}"
        try:
            contenido = destino.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False, "rejected: unreadable file"
        return True, _cap(contenido, REPLY_LIMIT)
    if nombre == "write_file":
        contenido = accion.get("content")
        if not isinstance(contenido, str):
            return False, "rejected: 'content' must be a string"
        destino = _inside(task_dir, accion.get("path"))
        if destino is None:
            return False, "rejected: the path escapes the working copy"
        if destino.exists() and not destino.is_file():
            return False, f"rejected: {accion.get('path')!r} is a directory"
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(contenido, encoding="utf-8")
        return True, f"wrote {len(contenido)} chars to {accion.get('path')!r}"
    if nombre == "apply_patch":
        busca, reemplazo = accion.get("search"), accion.get("replace")
        if not isinstance(busca, str) or not isinstance(reemplazo, str):
            return False, "rejected: 'search' and 'replace' must be strings"
        destino = _inside(task_dir, accion.get("path"))
        if destino is None:
            return False, "rejected: the path escapes the working copy"
        if not destino.is_file():
            return False, f"rejected: no such file: {accion.get('path')!r}"
        contenido = destino.read_text(encoding="utf-8")
        if busca not in contenido:
            return False, "rejected: the search text does not appear in the file"
        destino.write_text(contenido.replace(busca, reemplazo, 1), encoding="utf-8")
        return True, f"patched {accion.get('path')!r}"
    if nombre == "run_tests":
        resultado = sandbox.run_pytest(task_dir)
        estado = "timed out" if resultado["timed_out"] else f"exit {resultado['returncode']}"
        return True, f"pytest {estado}\n{_cap(resultado['tail'], RESULT_LIMIT)}"
    if nombre == "finish":
        resumen = accion.get("summary")
        return True, "the model finished" + (f": {resumen}" if isinstance(resumen, str) else "")
    return False, f"rejected: unknown action {nombre!r}"


def _step_prompt(task_prompt: str, transcripcion: list[str], numero: int) -> str:
    """One consultation's prompt: the task, plus the transcript once there is one."""
    if not transcripcion:
        return (
            f"{task_prompt}\n\nThis is action {numero} of {MAX_STEPS}. "
            "Reply with your first JSON action."
        )
    return (
        f"{task_prompt}\n\nTranscript of your session so far:\n\n"
        + "\n\n".join(transcripcion)
        + f"\n\nThis is action {numero} of {MAX_STEPS}. "
        "Reply with the next single JSON action."
    )


async def run_task(
    client: OllamaCloud,
    *,
    model: str,
    task_prompt: str,
    task_dir: pathlib.Path,
    seed_value: int,
    repo: tuple[tuple[str, str], ...],
    salt=None,
    task_index: int = 0,
) -> dict:
    """One agent task over its own working copy; returns the task record.

    The record mirrors a plain request's fields (last step's timing and done
    object) plus `steps`: the loop's raw evidence — action, outcome, reply, and
    the tokens each step billed. `salt` (task index, turn -> nonce text) is the
    cache-free lane's per-turn salter: every step's prompt carries its own nonce
    as the first tokens (the raw cost of re-sending the context), and each step
    persists both hashes.
    """
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        for ruta, contenido in repo:
            destino = task_dir / ruta
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(contenido, encoding="utf-8")
    except OSError as e:
        return _registro_crash(task_dir, f"{type(e).__name__}: {e}")
    pasos: list[dict] = []
    transcripcion: list[str] = []
    for numero in range(1, MAX_STEPS + 1):
        nonce = salt(task_index, numero) if salt else None
        prompt_paso = _step_prompt(task_prompt, transcripcion, numero)
        prompt = lane.salted_prompt(prompt_paso, nonce) if nonce else prompt_paso
        rec = await client.chat(
            model=model,
            prompt=prompt,
            seed=seed_value,
        )
        try:
            accion = parse_action(rec["content"]) if rec["http"] == 200 and rec["done"] else None
            if accion is None:
                nombre = "invalid"
                ok, resultado = False, "rejected: the reply carries no parsable JSON action"
            else:
                nombre = str(accion.get("action"))
                # run_tests runs a subprocess (up to the sandbox's timeout plus
                # its post-kill drain): off the event loop's thread, or one
                # task's pytest would freeze the shared loop and stall every
                # sibling task's awaited chat/meter work in a k>1 cell.
                ok, resultado = await asyncio.to_thread(execute_action, accion, task_dir)
        except Exception as e:  # noqa: BLE001 - a harness-side crash is data, not a lost batch
            # a write that cannot land (disk full, a lone surrogate) ends the
            # task: the step is still recorded (it was billed), and a broken
            # working copy is never consulted again.
            nombre, ok = "error", False
            resultado = f"the harness failed to execute the action: {type(e).__name__}: {e}"
        done = rec["done"]
        paso = {
            "step": numero,
            "action": nombre,
            "action_ok": ok,
            "reply": _cap(rec["content"], REPLY_LIMIT),
            "result": _cap(resultado, RESULT_LIMIT),
            "t_start": rec["t_start"],
            "t_first_chunk": rec["t_first_chunk"],
            "t_total": rec["t_total"],
            "chunks": rec["chunks"],
            "http": rec["http"],
            "err": rec["err"],
            "tok_in": done.get("prompt_eval_count") if done else None,
            "tok_out": done.get("eval_count") if done else None,
            "tok_cached": done.get("prompt_eval_cache_hit_count") if done else None,
            "api": done,
            # The lane's per-turn evidence: what this step billed (nonce + prompt).
            "prompt_sha256": lane.prompt_sha256(prompt) if nonce else None,
            "nonce_sha256": lane.nonce_sha256(nonce) if nonce else None,
        }
        pasos.append(paso)
        transcripcion.append(
            f"[action {numero}] {nombre} -> {'ok' if ok else 'rejected'}\n{paso['result']}"
        )
        if nombre == "finish" and ok:
            break  # the model ended its session
        if rec["err"] is not None or nombre == "error":
            break  # a dead endpoint or a broken working copy is never consulted again
    return {
        "t_start": pasos[0]["t_start"],
        "t_first_chunk": pasos[0]["t_first_chunk"],
        "t_total": pasos[-1]["t_total"],
        "chunks": sum(p["chunks"] for p in pasos),
        "http": pasos[-1]["http"],
        "err": next((p["err"] for p in pasos if p["err"]), None),
        "done": pasos[-1]["api"],
        "content": "\n".join(p["reply"] for p in pasos),
        "steps": pasos,
        "tool_calls": [],  # the loop declares no API tools: actions travel in text
        "repo_dir": str(task_dir),
        # The task-level lane evidence: its first turn's nonce + prompt.
        "prompt_sha256": pasos[0]["prompt_sha256"],
        "nonce_sha256": pasos[0]["nonce_sha256"],
    }


async def run_tasks(
    client: OllamaCloud,
    spec,
    specs_requeridos,
    modelo_api: str,
    *,
    sandbox_root: pathlib.Path,
    salt=None,
) -> list[dict]:
    """The batch's tasks (one working copy each), k-concurrent; records in order.

    One task crashing never discards its siblings: every task yields a record
    (a degenerate one when it produced no step), so the batch's billed evidence
    stays attributable and the runner's count check stays truthful. `salt`
    (task index, turn -> nonce text) is the cache-free lane's per-turn salter."""
    raiz = pathlib.Path(sandbox_root) / spec.batch_id
    semaforo = asyncio.Semaphore(spec.k)

    async def _una(i: int) -> dict:
        seed_value = fixtures.seed(spec.workload, spec.model, spec.rep, i)
        task_dir = raiz / f"task-{i:04d}"
        async with semaforo:  # the cell's k bounds the tasks in flight
            try:
                return await run_task(
                    client,
                    model=modelo_api,
                    task_prompt=specs_requeridos[i].prompt,
                    task_dir=task_dir,
                    seed_value=seed_value,
                    repo=specs_requeridos[i].repo,
                    salt=salt,
                    task_index=i,
                )
            except Exception as e:  # noqa: BLE001 - the record is the evidence
                return _registro_crash(task_dir, f"{type(e).__name__}: {e}")

    return list(await asyncio.gather(*(_una(i) for i in range(spec.n))))
