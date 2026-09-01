"""Fixtures compartidos: el fake de ollama.com como seam de tests y helper de CLI."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ocharness.testing.fake import FakeOllama


@pytest.fixture()
def fake() -> FakeOllama:
    return FakeOllama()


def escribir_tabla(pricing_dir: pathlib.Path, version: str, modelos: dict) -> pathlib.Path:
    """Escribe una tabla de precios versionada de prueba; devuelve el DIRECTORIO."""
    pricing_dir.mkdir(parents=True, exist_ok=True)
    ruta = pricing_dir / f"{version}.json"
    ruta.write_text(
        json.dumps(
            {
                "table_version": version,
                "captured": version,
                "source": "fake de test",
                "per": 1_000_000,
                "currency": "USD",
                "models": modelos,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return pricing_dir


def tabla_estandar() -> dict:
    """Las 19 tarifas oficiales (2026-08-31) — el slate T2/T3 del dry-run las necesita."""
    return {
        "deepseek-v4-flash": {"input": 0.44, "cached_input": 0.014, "output": 1.32},
        "deepseek-v4-pro": {"input": 1.32, "cached_input": 0.044, "output": 3.96},
        "gemma4": {"input": 0.14, "cached_input": 0.05, "output": 0.40},
        "glm-5.3": {"input": 1.40, "cached_input": 0.26, "output": 4.40},
        "glm-5.3-flash": {"input": 0.15, "cached_input": 0.03, "output": 0.50},
        "glm-5.2": {"input": 1.40, "cached_input": 0.26, "output": 4.40},
        "glm-5.1": {"input": 1.00, "cached_input": 0.20, "output": 3.20},
        "gpt-oss:120b": {"input": 0.15, "cached_input": 0.014, "output": 0.60},
        "gpt-oss:20b": {"input": 0.07, "cached_input": 0.035, "output": 0.30},
        "kimi-k3": {"input": 3.00, "cached_input": 0.30, "output": 15.00},
        "kimi-k2.7-code": {"input": 0.95, "cached_input": 0.19, "output": 4.00},
        "kimi-k2.6": {"input": 0.95, "cached_input": 0.16, "output": 4.00},
        "minimax-m3": {"input": 0.60, "cached_input": 0.12, "output": 2.40},
        "minimax-m2.7": {"input": 0.30, "cached_input": 0.06, "output": 1.20},
        "mistral-large-3": {"input": 0.50, "cached_input": 0.50, "output": 1.50},
        "nemotron-3-nano": {"input": 0.06, "cached_input": 0.06, "output": 0.24},
        "nemotron-3-super": {"input": 0.015, "cached_input": 0.015, "output": 0.60},
        "nemotron-3-ultra": {"input": 0.10, "cached_input": 0.10, "output": 3.00},
        "qwen3.5:397b": {"input": 0.60, "cached_input": 0.60, "output": 3.60},
    }