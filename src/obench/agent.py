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
    text = reply.strip()
    if not text:
        return None
    inicio, fin = text.find("{"), text.rfind("}")
    candidato = text[inicio : fin + 1] if 0 <= inicio < fin else text
    try:
        action = json.loads(candidato)
    except ValueError:
        return None
    return action if isinstance(action, dict) else None


def _cap(text: str, limite: int) -> str:
    return text if len(text) <= limite else text[:limite] + f" ...[truncated at {limite} chars]"


def _record_crash(task_dir: pathlib.Path, cause: str) -> dict:
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
        "err": f"task crashed before its first step: {cause}",
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
    path = pathlib.PurePosixPath(rel)
    if path.is_absolute() or ".." in path.parts:
        return None
    dest = (task_dir / path).resolve()
    try:
        dest.relative_to(task_dir.resolve())
    except ValueError:
        return None
    return dest


def execute_action(action: dict, task_dir: pathlib.Path) -> tuple[bool, str]:
    """One action against the working copy; returns (executed, transcript result)."""
    name = action.get("action")
    if name == "list_dir":
        dest = _inside(task_dir, action.get("path", "."))
        if dest is None:
            return False, "rejected: the path escapes the working copy"
        if not dest.is_dir():
            return False, f"rejected: no such directory: {action.get('path')!r}"
        inputs = sorted(e.name + ("/" if e.is_dir() else "") for e in dest.iterdir())
        return True, "\n".join(inputs) if inputs else "(empty directory)"
    if name == "read_file":
        dest = _inside(task_dir, action.get("path"))
        if dest is None:
            return False, "rejected: the path escapes the working copy"
        if not dest.is_file():
            return False, f"rejected: no such file: {action.get('path')!r}"
        try:
            content = dest.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False, "rejected: unreadable file"
        return True, _cap(content, REPLY_LIMIT)
    if name == "write_file":
        content = action.get("content")
        if not isinstance(content, str):
            return False, "rejected: 'content' must be a string"
        dest = _inside(task_dir, action.get("path"))
        if dest is None:
            return False, "rejected: the path escapes the working copy"
        if dest.exists() and not dest.is_file():
            return False, f"rejected: {action.get('path')!r} is a directory"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return True, f"wrote {len(content)} chars to {action.get('path')!r}"
    if name == "apply_patch":
        busca, reemplazo = action.get("search"), action.get("replace")
        if not isinstance(busca, str) or not isinstance(reemplazo, str):
            return False, "rejected: 'search' and 'replace' must be strings"
        dest = _inside(task_dir, action.get("path"))
        if dest is None:
            return False, "rejected: the path escapes the working copy"
        if not dest.is_file():
            return False, f"rejected: no such file: {action.get('path')!r}"
        content = dest.read_text(encoding="utf-8")
        if busca not in content:
            return False, "rejected: the search text does not appear in the file"
        dest.write_text(content.replace(busca, reemplazo, 1), encoding="utf-8")
        return True, f"patched {action.get('path')!r}"
    if name == "run_tests":
        resultado = sandbox.run_pytest(task_dir)
        state = "timed out" if resultado["timed_out"] else f"exit {resultado['returncode']}"
        return True, f"pytest {state}\n{_cap(resultado['tail'], RESULT_LIMIT)}"
    if name == "finish":
        summary = action.get("summary")
        return True, "the model finished" + (f": {summary}" if isinstance(summary, str) else "")
    return False, f"rejected: unknown action {name!r}"


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
        for path, content in repo:
            dest = task_dir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
    except OSError as e:
        return _record_crash(task_dir, f"{type(e).__name__}: {e}")
    steps: list[dict] = []
    transcripcion: list[str] = []
    for numero in range(1, MAX_STEPS + 1):
        nonce = salt(task_index, numero) if salt else None
        prompt_step = _step_prompt(task_prompt, transcripcion, numero)
        prompt = lane.salted_prompt(prompt_step, nonce) if nonce else prompt_step
        rec = await client.chat(
            model=model,
            prompt=prompt,
            seed=seed_value,
        )
        try:
            action = parse_action(rec["content"]) if rec["http"] == 200 and rec["done"] else None
            if action is None:
                name = "invalid"
                ok, resultado = False, "rejected: the reply carries no parsable JSON action"
            else:
                name = str(action.get("action"))
                # run_tests runs a subprocess (up to the sandbox's timeout plus
                # its post-kill drain): off the event loop's thread, or one
                # task's pytest would freeze the shared loop and stall every
                # sibling task's awaited chat/meter work in a k>1 cell.
                ok, resultado = await asyncio.to_thread(execute_action, action, task_dir)
        except Exception as e:  # noqa: BLE001 - a harness-side crash is data, not a lost batch
            # a write that cannot land (disk full, a lone surrogate) ends the
            # task: the step is still recorded (it was billed), and a broken
            # working copy is never consulted again.
            name, ok = "error", False
            resultado = f"the harness failed to execute the action: {type(e).__name__}: {e}"
        done = rec["done"]
        step = {
            "step": numero,
            "action": name,
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
        steps.append(step)
        transcripcion.append(
            f"[action {numero}] {name} -> {'ok' if ok else 'rejected'}\n{step['result']}"
        )
        if name == "finish" and ok:
            break  # the model ended its session
        if rec["err"] is not None or name == "error":
            break  # a dead endpoint or a broken working copy is never consulted again
    return {
        "t_start": steps[0]["t_start"],
        "t_first_chunk": steps[0]["t_first_chunk"],
        "t_total": steps[-1]["t_total"],
        "chunks": sum(p["chunks"] for p in steps),
        "http": steps[-1]["http"],
        "err": next((p["err"] for p in steps if p["err"]), None),
        "done": steps[-1]["api"],
        "content": "\n".join(p["reply"] for p in steps),
        "steps": steps,
        "tool_calls": [],  # the loop declares no API tools: actions travel in text
        "repo_dir": str(task_dir),
        # The task-level lane evidence: its first turn's nonce + prompt.
        "prompt_sha256": steps[0]["prompt_sha256"],
        "nonce_sha256": steps[0]["nonce_sha256"],
    }


async def run_tasks(
    client: OllamaCloud,
    spec,
    specs_requeridos,
    api_model: str,
    *,
    sandbox_root: pathlib.Path,
    salt=None,
) -> list[dict]:
    """The batch's tasks (one working copy each), k-concurrent; records in order.

    One task crashing never discards its siblings: every task yields a record
    (a degenerate one when it produced no step), so the batch's billed evidence
    stays attributable and the runner's count check stays truthful. `salt`
    (task index, turn -> nonce text) is the cache-free lane's per-turn salter."""
    root = pathlib.Path(sandbox_root) / spec.batch_id
    semaforo = asyncio.Semaphore(spec.k)

    async def _una(i: int) -> dict:
        seed_value = fixtures.seed(spec.workload, spec.model, spec.rep, i)
        task_dir = root / f"task-{i:04d}"
        async with semaforo:  # the cell's k bounds the tasks in flight
            try:
                return await run_task(
                    client,
                    model=api_model,
                    task_prompt=specs_requeridos[i].prompt,
                    task_dir=task_dir,
                    seed_value=seed_value,
                    repo=specs_requeridos[i].repo,
                    salt=salt,
                    task_index=i,
                )
            except Exception as e:  # noqa: BLE001 - the record is the evidence
                return _record_crash(task_dir, f"{type(e).__name__}: {e}")

    return list(await asyncio.gather(*(_una(i) for i in range(spec.n))))
