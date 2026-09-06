"""The spending gate: a level only runs if a live dry-run authorizes it.

The mark records the approved budget, the `table_version` it was estimated with
and the protocol it was estimated UNDER (the billing canary and the lane's
nonce overhead are protocol v3 spend the mark must have priced):
`run` validates it (level, integrity, protocol, and the live table) and
consumes it at startup — one dry-run enables exactly one run.
"""

from __future__ import annotations

import json
import pathlib
import time

from .client import PROTOCOL_VERSION


class GateClosed(Exception):
    """`run` was requested for a level with no live dry-run or an invalidated budget."""


def _mark_path(base, level: str) -> pathlib.Path:
    return pathlib.Path(base) / "runs" / f"gate-{level}.json"


def mark_dry_run(base, level: str, estimado: dict) -> pathlib.Path:
    """Registers the level's dry-run atomically (crash-safe: tmp + rename)."""
    path = _mark_path(base, level)
    path.parent.mkdir(parents=True, exist_ok=True)
    mark = {
        "dry_run_at": time.time(),
        "level": level,
        "table_version": str(estimado.get("table_version")),
        "protocol_version": PROTOCOL_VERSION,
        "estimado": estimado,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(mark), encoding="utf-8")
    tmp.replace(path)
    return path


def require_dry_run(
    base,
    level: str,
    *,
    table_version: str | None = None,
    reps: int | None = None,
    models: list[str] | None = None,
) -> None:
    """Raises GateClosed if the mark is missing, corrupt, from another table, does not
    cover this run's density (--reps) or its models: the gate binds what was approved
    to what will bill. The models check (a run may never bill what the dry-run did
    not approve) skips marks written before it existed."""
    path = _mark_path(base, level)
    if not path.exists():
        raise GateClosed(f"gate: run `bench dry-run --level {level}` before running this level")
    try:
        mark = json.loads(path.read_text(encoding="utf-8"))
        assert mark["level"] == level
        assert isinstance(mark["dry_run_at"], (int, float))
        assert mark["estimado"]["rows"]
    except (json.JSONDecodeError, KeyError, AssertionError, TypeError):
        raise GateClosed(
            f"gate: the mark for {level} is corrupt - re-run `bench dry-run --level {level}`"
        ) from None
    if table_version is not None and mark.get("table_version") != str(table_version):
        raise GateClosed(
            f"gate: the dry-run approved table {mark.get('table_version')!r} but the run "
            f"would use {table_version!r}; re-run `bench dry-run --level {level}`"
        )
    if mark.get("protocol_version") != PROTOCOL_VERSION:
        # A mark from another protocol vintage priced a different spend: the
        # billing canary and the lane's nonce overhead are protocol v3 costs a
        # pre-v3 mark never approved (and never knew existed).
        raise GateClosed(
            f"gate: the dry-run mark was written under protocol "
            f"{mark.get('protocol_version')!r} but this harness bills under "
            f"{PROTOCOL_VERSION!r} (the billing canary + the lane's nonce overhead are "
            f"v3 spend the mark never priced); re-run `bench dry-run --level {level}`"
        )
    if reps is not None:
        aprobadas = mark.get("estimado", {}).get("reps")
        if aprobadas != reps:
            raise GateClosed(
                f"gate: the dry-run approved {aprobadas!r} repetitions but this run would "
                f"execute {reps!r}; the run may never bill more than the dry-run approved - "
                f"re-run `bench dry-run --level {level}`"
            )
    if models is not None:
        aprobados = mark.get("estimado", {}).get("models")
        if aprobados is not None:
            fuera = [m for m in models if m not in aprobados]
            if fuera:
                raise GateClosed(
                    f"gate: the dry-run approved {aprobados!r} models but this run would "
                    f"bill {fuera!r}; the run may never bill what the dry-run did not "
                    f"approve - re-run `bench dry-run --level {level}`"
                )


def consume(base, level: str) -> None:
    """Consumes the level's mark (its run has started): one dry-run, one run."""
    path = _mark_path(base, level)
    if path.exists():
        path.unlink()
