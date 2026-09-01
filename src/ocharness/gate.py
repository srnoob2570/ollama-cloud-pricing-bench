"""The spending gate: a level only runs if a live dry-run authorizes it.

The mark records the approved budget and the `table_version` it was estimated with:
`run` validates it (level, integrity, and the live table) and consumes it at startup —
one dry-run enables exactly one run.
"""

from __future__ import annotations

import json
import pathlib
import time


class GateClosed(Exception):
    """`run` was requested for a level with no live dry-run or an invalidated budget."""


def _mark_path(base, level: str) -> pathlib.Path:
    return pathlib.Path(base) / "runs" / f"gate-{level}.json"


def mark_dry_run(base, level: str, estimado: dict) -> pathlib.Path:
    """Registers the level's dry-run atomically (crash-safe: tmp + rename)."""
    ruta = _mark_path(base, level)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    marca = {
        "dry_run_at": round(time.time(), 3),
        "level": level,
        "table_version": str(estimado.get("table_version")),
        "estimado": estimado,
    }
    tmp = ruta.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(marca), encoding="utf-8")
    tmp.replace(ruta)
    return ruta


def require_dry_run(
    base, level: str, *, table_version: str | None = None, reps: int | None = None
) -> None:
    """Raises GateClosed if the mark is missing, corrupt, from another table, or does not
    cover this run's density (--reps): the gate binds what was approved to what will bill."""
    ruta = _mark_path(base, level)
    if not ruta.exists():
        raise GateClosed(f"gate: run `bench dry-run --level {level}` before running this level")
    try:
        marca = json.loads(ruta.read_text(encoding="utf-8"))
        assert marca["level"] == level
        assert isinstance(marca["dry_run_at"], (int, float))
        assert marca["estimado"]["rows"]
    except (json.JSONDecodeError, KeyError, AssertionError, TypeError):
        raise GateClosed(
            f"gate: the mark for {level} is corrupt - re-run `bench dry-run --level {level}`"
        ) from None
    if table_version is not None and marca.get("table_version") != str(table_version):
        raise GateClosed(
            f"gate: the dry-run approved table {marca.get('table_version')!r} but the run "
            f"would use {table_version!r}; re-run `bench dry-run --level {level}`"
        )
    if reps is not None:
        aprobadas = marca.get("estimado", {}).get("reps")
        if aprobadas != reps:
            raise GateClosed(
                f"gate: the dry-run approved {aprobadas!r} repetitions but this run would "
                f"execute {reps!r}; the run may never bill more than the dry-run approved - "
                f"re-run `bench dry-run --level {level}`"
            )


def consume(base, level: str) -> None:
    """Consumes the level's mark (its run has started): one dry-run, one run."""
    ruta = _mark_path(base, level)
    if ruta.exists():
        ruta.unlink()
