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
    def __init__(self, path):
        self._path = pathlib.Path(path)
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise TableError(f"table {self._path.name} is not valid JSON: {e}") from None
        for key in ("table_version", "models"):
            if key not in doc:
                raise TableError(f"table {self._path.name} is missing {key!r}")
        self.table_version = str(doc["table_version"])
        self.per = int(doc.get("per", 1_000_000))
        if self.per <= 0:
            raise TableError(f"table {self._path.name} has an invalid `per`: {self.per}")
        self.currency = doc.get("currency", "USD")
        self.models: dict[str, dict[str, float]] = doc["models"]
        self._validate()

    def _validate(self) -> None:
        for name, t in self.models.items():
            try:
                input = float(t["input"])
                cacheada = float(t["cached_input"])
                output = float(t["output"])
            except (KeyError, TypeError, ValueError) as e:
                raise TableError(
                    f"table {self._path.name}: invalid rates for {name!r} ({e})"
                ) from None
            if min(input, cacheada, output) < 0:
                raise TableError(f"table {self._path.name}: negative rates for {name!r}")
            if cacheada > input:
                raise TableError(
                    f"table {self._path.name}: {name!r} prices cached_input ({cacheada}) "
                    f"ABOVE input ({input}) - a data error, not a discount"
                )

    @classmethod
    def load(cls, pricing_dir, version: str | None = None) -> PriceTable:
        """Loads the `version` table, or the most recent one in the directory."""
        directory = pathlib.Path(pricing_dir)
        if not directory.exists():
            raise TableError(f"price-table directory does not exist: {directory}")
        path = directory / f"{version}.json" if version else None
        if path is None:
            candidatas = sorted(directory.glob("*.json"))
            if not candidatas:
                raise TableError(f"no price table in {directory}")
            path = candidatas[-1]
        if not path.exists():
            raise TableError(f"price table does not exist: {path}")
        return cls(path)

    def rate(self, model: str) -> Rate:
        try:
            t = self.models[model]
        except KeyError:
            raise TableError(f"table {self._path.name} has no model {model!r}") from None
        return Rate(model, float(t["input"]), float(t["cached_input"]), float(t["output"]))
