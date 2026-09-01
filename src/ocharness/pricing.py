"""Tabla de precios versionada (input / cached input / output por `per` tokens).

Una tabla malformada es un error de datos, no un crash: toda invalidez sale como
ErrorTabla con mensaje claro (exit 2), nunca con traceback.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib


class ErrorTabla(Exception):
    """La tabla de precios no se pudo cargar o es inválida."""


@dataclasses.dataclass
class Tarifa:
    modelo: str
    input: float
    cached_input: float
    output: float

    @property
    def tiene_descuento_cache(self) -> bool:
        return self.cached_input < self.input


class TablaPrecios:
    def __init__(self, ruta: pathlib.Path | str):
        self._ruta = pathlib.Path(ruta)
        try:
            doc = json.loads(self._ruta.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ErrorTabla(f"la tabla {self._ruta.name} no es JSON válido: {e}") from None
        for clave in ("table_version", "models"):
            if clave not in doc:
                raise ErrorTabla(f"la tabla {self._ruta.name} no tiene {clave!r}")
        self.table_version = str(doc["table_version"])
        self.per = int(doc.get("per", 1_000_000))
        if self.per <= 0:
            raise ErrorTabla(f"la tabla {self._ruta.name} tiene `per` inválido: {self.per}")
        self.currency = doc.get("currency", "USD")
        self.models: dict[str, dict[str, float]] = doc["models"]
        self._validar()

    def _validar(self) -> None:
        for nombre, t in self.models.items():
            try:
                entrada = float(t["input"])
                cacheada = float(t["cached_input"])
                salida = float(t["output"])
            except (KeyError, TypeError, ValueError) as e:
                raise ErrorTabla(
                    f"tabla {self._ruta.name}: tarifas inválidas en {nombre!r} ({e})") from None
            if min(entrada, cacheada, salida) < 0:
                raise ErrorTabla(f"tabla {self._ruta.name}: tarifas negativas en {nombre!r}")
            if cacheada > entrada:
                raise ErrorTabla(
                    f"tabla {self._ruta.name}: {nombre!r} cobra cached_input ({cacheada}) "
                    f"por ENCIMA de input ({entrada}) — invalidez de datos, no un descuento")

    @classmethod
    def carga(cls, pricing_dir: pathlib.Path | str, version: str | None = None) -> TablaPrecios:
        """Carga la tabla `version`, o la más reciente del directorio si no se pide una."""
        directorio = pathlib.Path(pricing_dir)
        if not directorio.exists():
            raise ErrorTabla(f"no existe el directorio de tablas de precios: {directorio}")
        ruta = directorio / f"{version}.json" if version else None
        if ruta is None:
            candidatas = sorted(directorio.glob("*.json"))
            if not candidatas:
                raise ErrorTabla(f"no hay tabla de precios en {directorio}")
            ruta = candidatas[-1]
        if not ruta.exists():
            raise ErrorTabla(f"no existe la tabla de precios: {ruta}")
        return cls(ruta)

    def tarifa(self, modelo: str) -> Tarifa:
        try:
            t = self.models[modelo]
        except KeyError:
            raise ErrorTabla(f"la tabla {self._ruta.name} no tiene el modelo {modelo!r}") from None
        return Tarifa(modelo, float(t["input"]), float(t["cached_input"]), float(t["output"]))