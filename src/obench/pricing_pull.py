"""Snapshotting the upstream rate card into a versioned local price table.

`bench pricing-pull` fetches the published rate card (the ollama-cloud-catalog
artifact, itself scraped from ollama.com/pricing), validates it fail-loud,
maps catalog ids onto the harness's model ids, and lands a NEW
`pricing/<version>.json`. It never overwrites a shipped table, and it never
enters runtime analysis: the harness stays offline and every verdict is priced
by its own table vintage, so the pull is a maintenance step the owner reviews
(the rate-by-rate diff) before committing.

A malformed upstream document is a data error, not a crash: every surprise
surfaces as a PullError with a clear message (exit 2), never a traceback —
and never a silently misread rate published as fresh.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import httpx

DEFAULT_PRICING_URL = (
    "https://raw.githubusercontent.com/srnoob2570/ollama-cloud-catalog/main/pricing.json"
)

# Catalog ids that differ from the harness's model ids (the id ollama.com
# serves for runs). Identity is the fallback: the map carries renames only,
# so a new upstream qualifier that should be renamed shows up in the diff
# (an added model) instead of being silently folded into the wrong rates.
ALIASES = {
    "deepseek-v4-flash:0731": "deepseek-v4-flash",
    "deepseek-v4-pro:0813": "deepseek-v4-pro",
    "gemma4:31b": "gemma4",
    "mistral-large-3:675b": "mistral-large-3",
    "nemotron-3-nano:30b": "nemotron-3-nano",
}


class PullError(Exception):
    """The upstream rate card could not be fetched, is invalid, or the
    snapshot would overwrite a shipped table."""


def fetch_document(
    url: str, *, timeout: float = 20.0, transport: httpx.BaseTransport | None = None
) -> dict:
    """Fetches the upstream rate card; a broken fetch aborts, never degrades."""
    try:
        with httpx.Client(timeout=timeout, transport=transport) as cliente:
            respuesta = cliente.get(url)
    except httpx.HTTPError as e:
        raise PullError(f"pricing fetch failed for {url}: {e}") from None
    if respuesta.status_code != 200:
        raise PullError(f"pricing fetch failed for {url}: HTTP {respuesta.status_code}")
    try:
        return respuesta.json()
    except (ValueError, json.JSONDecodeError) as e:
        raise PullError(f"pricing fetch for {url} returned invalid JSON: {e}") from None


def _rate(name: str, t, contexto: str) -> None:
    """Validates one rate entry: numbers only, non-negative, no inverted cache."""
    if not isinstance(t, dict):
        raise PullError(f"{contexto}: rate for {name!r} is not an object")
    for field in ("input", "output"):
        if not isinstance(t.get(field), (int, float)) or isinstance(t.get(field), bool):
            raise PullError(f"{contexto}: rate for {name!r} is missing numeric {field!r}")
        if t[field] < 0:
            raise PullError(f"{contexto}: negative {field!r} for {name!r}")
    cacheada = t.get("cache_read")
    if cacheada is None:
        return  # no published cache discount; the snapshot prices cached_input = input
    if not isinstance(cacheada, (int, float)) or isinstance(cacheada, bool):
        raise PullError(f"{contexto}: non-numeric cache_read for {name!r}")
    if cacheada < 0:
        raise PullError(f"{contexto}: negative cache_read for {name!r}")
    if cacheada > t["input"]:
        raise PullError(
            f"{contexto}: {name!r} prices cache_read ({cacheada}) "
            f"ABOVE input ({t['input']}) - a data error, not a discount"
        )


def validate_document(doc) -> None:
    """Fails loudly on every structure surprise; no silent partial updates."""
    if not isinstance(doc, dict):
        raise PullError("upstream pricing is not an object")
    for key in ("generated_at", "models", "source"):
        if key not in doc:
            raise PullError(f"upstream pricing is missing {key!r}")
    models = doc["models"]
    if not isinstance(models, dict) or not models:
        raise PullError("upstream pricing has an empty or invalid `models`")
    for name, t in models.items():
        _rate(name, t, contexto="models")
    pico = doc.get("x_ollama")
    if isinstance(pico, dict) and "models" in pico:
        if not isinstance(pico.get("peak_window"), str):
            raise PullError("x_ollama.peak_window is missing or not a string")
        for name, t in pico["models"].items():
            _rate(name, t, contexto="x_ollama.models")


def table_version(doc: dict) -> str:
    """The snapshot version: the upstream scrape's date (generated_at)."""
    generado = str(doc.get("generated_at", ""))
    try:
        return datetime.datetime.fromisoformat(generado.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        raise PullError(f"upstream generated_at is not a timestamp: {generado!r}") from None


def map_models(doc: dict) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Catalog rates -> harness rates, with the alias map applied.

    Absent `cache_read` means no published cache discount: cached_input prices
    at input (the same shape the 2026-08-31 snapshot carries for those
    models), surfaced as a note so the diff shows it, never hides it.
    """
    models: dict[str, dict[str, float]] = {}
    notes: list[str] = []
    for catalogo_id, t in doc["models"].items():
        model = ALIASES[catalogo_id] if catalogo_id in ALIASES else catalogo_id
        if model in models:
            raise PullError(
                f"alias collapse: {catalogo_id!r} renames onto {model!r}, already present"
            )
        input = float(t["input"])
        cacheada = t.get("cache_read")
        if cacheada is None:
            cacheada = input
            notes.append(f"{model}: no cache_read upstream; cached_input = input (no discount)")
        models[model] = {
            "input": input,
            "cached_input": float(cacheada),
            "output": float(t["output"]),
        }
    return models, notes


def peak_block(doc: dict) -> dict | None:
    """The peak rates + window (x_ollama), preserved as snapshot metadata.

    The harness prices its runs off-peak today; the block rides along so the
    snapshot records the vintage fully without the table format growing a
    peak/off-peak concept it does not use yet.
    """
    pico = doc.get("x_ollama")
    if not isinstance(pico, dict) or not isinstance(pico.get("models"), dict):
        return None
    tasas = {}
    for catalogo_id, t in pico["models"].items():
        model = ALIASES[catalogo_id] if catalogo_id in ALIASES else catalogo_id
        tasas[model] = {
            "input": float(t["input"]),
            "cached_input": float(t.get("cache_read", t["input"])),
            "output": float(t["output"]),
        }
    return {"window": str(pico.get("peak_window", "")), "rates": tasas}


def build_snapshot(doc: dict, url: str) -> tuple[dict, list[str]]:
    """The local table document: the upstream rates in the harness's shape."""
    version = table_version(doc)
    models, notes = map_models(doc)
    snapshot = {
        "table_version": version,
        "captured": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
        "source": f"{url} (upstream {doc['source']})",
        "per": 1_000_000,
        "currency": "USD",
        "models": models,
    }
    pico = peak_block(doc)
    if pico:
        snapshot["peak"] = pico
    return snapshot, notes


def diff(viejos: dict | None, nuevos: dict) -> dict:
    """The rate-by-rate change the owner reviews: added / removed / updated."""
    viejos = viejos or {}
    cambios: dict = {"added": [], "removed": [], "updated": []}
    for model in sorted(nuevos):
        if model not in viejos:
            cambios["added"].append(model)
        elif viejos[model] != nuevos[model]:
            cambios["updated"].append({"model": model, "old": viejos[model], "new": nuevos[model]})
    for model in sorted(viejos):
        if model not in nuevos:
            cambios["removed"].append(model)
    return cambios


def pull(
    url: str,
    pricing_dir,
    *,
    check: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Fetches, validates and lands the snapshot (or just diffs, with `check`).

    No-change short circuit: if the latest local table already carries the
    upstream rates, nothing is written — the snapshot churns only when prices
    actually move. A target file that exists with different content is
    refused: tables are immutable once landed.
    """
    directory = pathlib.Path(pricing_dir)
    doc = fetch_document(url, transport=transport)
    validate_document(doc)
    snapshot, notes = build_snapshot(doc, url)
    version = snapshot["table_version"]
    objetivo = directory / f"{version}.json"

    existentes = sorted(directory.glob("*.json")) if directory.exists() else []
    previos: dict | None = None
    if existentes:
        previos = json.loads(existentes[-1].read_text(encoding="utf-8")).get("models") or {}
    cambios = diff(previos, snapshot["models"])

    informe = {
        "table_version": version,
        "generated_at": doc["generated_at"],
        "source_url": url,
        "models": len(snapshot["models"]),
        "notes": notes,
        "latest": existentes[-1].name if existentes else None,
        "changes": cambios,
    }
    if previos == snapshot["models"]:
        informe.update(up_to_date=True, wrote=False, path=None)
        return informe
    if objetivo.exists():
        raise PullError(
            f"refusing to overwrite {objetivo.name}: it exists with different rates - "
            "tables are immutable once landed; check the upstream generated_at"
        )
    if check:
        informe.update(up_to_date=False, wrote=False, path=None)
        return informe
    directory.mkdir(parents=True, exist_ok=True)
    objetivo.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    informe.update(up_to_date=False, wrote=True, path=str(objetivo))
    return informe
