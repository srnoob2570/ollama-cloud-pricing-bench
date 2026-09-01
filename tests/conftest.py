"""Shared fixtures: the fake ollama.com as the test seam, plus CLI helpers."""

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


@pytest.fixture()
def fake_cli(fake, monkeypatch):
    """The fake wired as the CLI's transport seam (client hits it, never the network)."""
    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    from ocharness import client

    monkeypatch.setattr(client, "default_transport", lambda: fake.transport())
    return fake


def write_table(pricing_dir: pathlib.Path, version: str, models: dict) -> pathlib.Path:
    """Writes a test versioned price table; returns the DIRECTORY."""
    pricing_dir.mkdir(parents=True, exist_ok=True)
    ruta = pricing_dir / f"{version}.json"
    ruta.write_text(
        json.dumps(
            {
                "table_version": version,
                "captured": version,
                "source": "test fake",
                "per": 1_000_000,
                "currency": "USD",
                "models": models,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return pricing_dir


def standard_table() -> dict:
    """The 19 official rates (2026-08-31) - T2/T3 slates need them in dry-runs.

    Single source of truth: read from the versioned table in pricing/ when it
    exists (kept in sync by definition, per the Harness-01 review note).
    """
    real = pathlib.Path(__file__).resolve().parents[1] / "pricing" / "2026-08-31.json"
    if real.exists():
        return json.loads(real.read_text(encoding="utf-8"))["models"]
    raise FileNotFoundError(
        "pricing/2026-08-31.json is missing: tests validate against the official table"
    )


def no_discount_table() -> dict:
    """A table where every model prices cached_input = input (S1 == S0)."""
    return {m: {"input": 0.60, "cached_input": 0.60, "output": 3.60} for m in standard_table()}
