"""The sandbox: the mini-repos' real pytest, in a subprocess the model's code
cannot hang or game (methodology v1 §5).

Two layers keep the graded verdict honest:

- **What is graded**: `run_checker` builds the graded copy from scratch — a fresh
  seed of the fixture repo plus ONLY the model's changes that are not pytest's
  config surface (the suite, its conftest, and every inifile name stay the
  fixture's own) — so a model cannot pass by writing `pytest.ini`, a skipping
  `tests/conftest.py`, or a planted passing test instead of doing the work. The
  working copy itself is never mutated: it stays on disk as the run's evidence.
- **How it runs**: a fresh subprocess rooted at the graded copy, a hard timeout
  enforced on the whole process group (a hanging suite is killed, never waited
  on — and the post-kill read is bounded too, so a detached descendant holding
  the pipe cannot hang the harness), a minimal allowlist environment — built
  from scratch, never a copy of the parent's, so no API key ever reaches
  model-written code — and a network guard installed inside the subprocess
  before pytest starts (`sandbox_runner`), with a pytest config pinned to the
  task's own parent so no config or conftest above `--base` leaks in.

The result dict is the checker's raw evidence, kept verbatim on the request
line: exit code, timing, whether the timeout fired, the environment's key
names, and the output tail. `run_checker` also reports whether the sandbox
ever reached pytest (`sandbox_ok`): a harness misconfiguration there aborts
the batch loudly instead of publishing every cell as a model failure.

Requires pytest importable by `sys.executable` (a dev dependency of this project).
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

import ocharness
from .sandbox_runner import SANDBOX_READY, PYTEST_UNAVAILABLE_EXIT

SANDBOX_TIMEOUT_S = 60.0  # hard per-run ceiling, in-loop run_tests included
OUTPUT_TAIL = 4000  # combined pytest output kept on the sandbox record
POST_KILL_READ_S = 10.0  # bound on draining the pipe after the kill

# pytest's config surface: never carried from the model's working copy. The
# suite and every config file the graded copy sees come from the fixture only,
# so the model's edits cannot decide the verdict.
_PYTEST_CONFIG_NAMES = frozenset(
    {"conftest.py", "pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"}
)


def run_checker(task_dir: pathlib.Path, fixture_files) -> dict:
    """The authoritative grade of one T3 task: the fixture's own tests decide.

    Builds the graded copy (fresh fixture seed + the model's carried changes),
    runs the sandbox pytest there, and records the run as raw evidence. The
    task's working copy is left untouched on disk.
    """
    task_dir = pathlib.Path(task_dir)
    graded = task_dir.parent / f"graded-{task_dir.name}"
    if graded.exists():
        shutil.rmtree(graded)  # a re-grade of the same task starts from the seed
    graded.mkdir(parents=True)
    for rel, contenido in fixture_files:
        _write(graded, rel, contenido)
    for ruta in sorted(task_dir.rglob("*")):
        if not ruta.is_file():
            continue
        rel = ruta.relative_to(task_dir).as_posix()
        if _is_pytest_config(rel):
            continue  # the suite and pytest's config are the fixture's, never the model's
        _copy(graded, rel, ruta)
    return run_pytest(graded)


def run_pytest(task_dir: pathlib.Path) -> dict:
    """Runs pytest in `task_dir` inside the sandbox; returns the result record.

    pytest's config is pinned to a file the sandbox writes one level above the
    graded directory — a path model-written code cannot reach — so the run is
    hermetic to inifiles and conftests above it. The hard ceiling is
    SANDBOX_TIMEOUT_S.
    """
    destino = pathlib.Path(task_dir)
    config = destino.parent / ".ocharness-pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    env = _subprocess_env(destino)
    argv = [sys.executable, "-m", "ocharness.sandbox_runner", "-c", str(config)]
    limite = SANDBOX_TIMEOUT_S
    t0 = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=destino,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # the kill hits the whole group, not just the child
    )
    timed_out = False
    try:
        salida, _ = proc.communicate(timeout=limite)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # nothing survives the hard timeout
        except ProcessLookupError:  # the subprocess exited inside the race window
            pass
        try:
            salida, _ = proc.communicate(timeout=POST_KILL_READ_S)
        except subprocess.TimeoutExpired:
            # A descendant outside the process group still holds the pipe: the
            # hard timeout must not turn into a hang. Drop the pipe; whatever
            # was already captured is the record.
            salida = b""
            if proc.stdout is not None:
                proc.stdout.close()
    texto = salida.decode("utf-8", errors="replace")
    return {
        "argv": argv,
        "cwd": str(destino),
        "env_keys": sorted(env),
        "returncode": proc.returncode,
        "timed_out": timed_out,
        # The handshake prints only after pytest imported: its absence means the
        # sandbox never graded anything (a harness misconfiguration, exit 90).
        "sandbox_ok": SANDBOX_READY in texto,
        "duration_s": round(time.monotonic() - t0, 3),
        "output_sha256": hashlib.sha256(texto.encode("utf-8")).hexdigest(),
        "tail": texto[-OUTPUT_TAIL:],
    }


def _is_pytest_config(rel: str) -> bool:
    """Whether a working-copy path is pytest's config surface (never carried)."""
    nombre = pathlib.PurePosixPath(rel).name
    return nombre in _PYTEST_CONFIG_NAMES or rel.startswith("tests/")


def _write(destino: pathlib.Path, rel: str, contenido: str) -> None:
    ruta = destino / rel
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")


def _copy(destino: pathlib.Path, rel: str, fuente: pathlib.Path) -> None:
    ruta = destino / rel
    ruta.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fuente, ruta)


def _subprocess_env(task_dir: pathlib.Path) -> dict:
    """An allowlist environment: nothing from the parent's environment leaks in."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(task_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        # `-m ocharness.sandbox_runner` needs the package importable (src layout
        # or site-packages: the directory that CONTAINS `ocharness`).
        "PYTHONPATH": str(pathlib.Path(ocharness.__file__).resolve().parents[1]),
    }
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env
