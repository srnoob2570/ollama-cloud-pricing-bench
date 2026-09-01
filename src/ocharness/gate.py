"""La compuerta de gasto: un nivel solo corre si su dry-run vigente lo autoriza.

La marca registra el presupuesto aprobado y la `table_version` con la que se estimó:
`run` la valida (nivel, integridad y tabla vigente) y la consume al arrancar —
un dry-run habilita exactamente una corrida.
"""

from __future__ import annotations

import json
import pathlib
import time


class CompuertaCerrada(Exception):
    """Se pidió `run` de un nivel sin dry-run vigente o con presupuesto invalidado."""


def _ruta_marca(base: pathlib.Path | str, nivel: str) -> pathlib.Path:
    return pathlib.Path(base) / "runs" / f"gate-{nivel}.json"


def marcar_dry_run(base: pathlib.Path | str, nivel: str, estimado: dict) -> pathlib.Path:
    """Registra el dry-run del nivel de forma atómica (crash-safe: tmp + rename)."""
    ruta = _ruta_marca(base, nivel)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    marca = {"dry_run_at": round(time.time(), 3), "level": nivel,
             "table_version": str(estimado.get("table_version")), "estimado": estimado}
    tmp = ruta.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(marca), encoding="utf-8")
    tmp.replace(ruta)
    return ruta


def exigir_dry_run(base: pathlib.Path | str, nivel: str, *,
                   table_version: str | None = None) -> None:
    """Lanza CompuertaCerrada si la marca falta, está corrupta o cambió de tabla."""
    ruta = _ruta_marca(base, nivel)
    if not ruta.exists():
        raise CompuertaCerrada(
            f"compuerta: ejecuta primero `bench dry-run --level {nivel}` antes de correr")
    try:
        marca = json.loads(ruta.read_text(encoding="utf-8"))
        assert marca["level"] == nivel
        assert isinstance(marca["dry_run_at"], (int, float))
        assert marca["estimado"]["filas"]
    except (json.JSONDecodeError, KeyError, AssertionError, TypeError):
        raise CompuertaCerrada(
            f"compuerta: la marca de {nivel} está corrupta — re-ejecuta "
            f"`bench dry-run --level {nivel}`") from None
    if table_version is not None and marca.get("table_version") != str(table_version):
        raise CompuertaCerrada(
            f"compuerta: el dry-run aprobó la tabla {marca.get('table_version')!r} pero se "
            f"usaría {table_version!r}; re-ejecuta `bench dry-run --level {nivel}`")


def consumir(base: pathlib.Path | str, nivel: str) -> None:
    """Consume la marca del nivel (su corrida ya arrancó)."""
    ruta = _ruta_marca(base, nivel)
    if ruta.exists():
        ruta.unlink()