"""Versioned price table (input / cached input / output per `per` tokens).

A malformed table is a data error, not a crash: every invalid table surfaces as a
TableError with a clear message (exit 2), never a traceback.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib


class TableError(Exception):
    """The price table could not be loaded or is invalid."""


@dataclasses.dataclass
class Rate:
    model: str
    input: float
    cached_input: float
    output: float

    @property
    def has_cache_discount(self) -> bool:
        return self.cached_input < self.input


class PriceTable:
    def __init__(self, ruta):
        self._ruta = pathlib.Path(ruta)
        try:
            doc = json.loads(self._ruta.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise TableError(f"table {self._ruta.name} is not valid JSON: {e}") from None
        for key in ("table_version", "models"):
            if key not in doc:
                raise TableError(f"table {self._ruta.name} is missing {key!r}")
        self.table_version = str(doc["table_version"])
        self.per = int(doc.get("per", 1_000_000))
        if self.per <= 0:
            raise TableError(f"table {self._ruta.name} has an invalid `per`: {self.per}")
        self.currency = doc.get("currency", "USD")
        self.models: dict[str, dict[str, float]] = doc["models"]
        self._validate()

    def _validate(self) -> None:
        for nombre, t in self.models.items():
            try:
                entrada = float(t["input"])
                cacheada = float(t["cached_input"])
                salida = float(t["output"])
            except (KeyError, TypeError, ValueError) as e:
                raise TableError(
                    f"table {self._ruta.name}: invalid rates for {nombre!r} ({e})"
                ) from None
            if min(entrada, cacheada, salida) < 0:
                raise TableError(f"table {self._ruta.name}: negative rates for {nombre!r}")
            if cacheada > entrada:
                raise TableError(
                    f"table {self._ruta.name}: {nombre!r} prices cached_input ({cacheada}) "
                    f"ABOVE input ({entrada}) - a data error, not a discount"
                )

    @classmethod
    def load(cls, pricing_dir, version: str | None = None) -> PriceTable:
        """Loads the `version` table, or the most recent one in the directory."""
        directorio = pathlib.Path(pricing_dir)
        if not directorio.exists():
            raise TableError(f"price-table directory does not exist: {directorio}")
        ruta = directorio / f"{version}.json" if version else None
        if ruta is None:
            candidatas = sorted(directorio.glob("*.json"))
            if not candidatas:
                raise TableError(f"no price table in {directorio}")
            ruta = candidatas[-1]
        if not ruta.exists():
            raise TableError(f"price table does not exist: {ruta}")
        return cls(ruta)

    def rate(self, modelo: str) -> Rate:
        try:
            t = self.models[modelo]
        except KeyError:
            raise TableError(f"table {self._ruta.name} has no model {modelo!r}") from None
        return Rate(modelo, float(t["input"]), float(t["cached_input"]), float(t["output"]))
