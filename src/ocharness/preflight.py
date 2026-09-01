"""Catalog preflight (ticket Harness 03): /v1/models must still carry the slate.

The live catalog is read BEFORE a single request is billed: a slate id gone from
it (renamed, removed) would produce silently-missing suites. Drift aborts the
run with a diff. The caller has already consumed the dry-run mark by then (the
require->consume window stays two adjacent filesystem ops, so a concurrent run
can never double the approved spend): an aborted preflight only costs a fresh
(free) dry-run, which every abort message says. Catalog ids arrive tagged where
the price table lists the base id (medidor-vivo-2026-08-31 §5:
`nemotron-3-nano:30b` vs `nemotron-3-nano`), so a slate id that already carries
a tag must match exactly, and a base id matches its tagged variants. The
snapshot seen at run start is pinned in the manifest.
"""

from __future__ import annotations

import asyncio
import dataclasses

from .client import OllamaCloud


class PreflightError(Exception):
    """The live catalog drifted from the slate: the run must not spend."""


@dataclasses.dataclass(frozen=True)
class CatalogReport:
    http: int
    ids: list[str]  # the catalog ids exactly as /v1/models served them
    matched: dict[str, str]  # slate id -> catalog id that satisfied it
    missing: list[str]  # slate ids with no catalog match (the aborting drift)
    unseen: list[str]  # catalog ids outside the price table (rename candidates)
    ambiguous: dict[str, list[str]]  # slate id -> the several variants it matched between


def _base(model_id: str) -> str:
    return model_id.split(":", 1)[0]


def _match(slate_id: str, catalog_ids: list[str]) -> list[str]:
    """Catalog ids referring to the same model as the slate id.

    A slate id with a tag (`gpt-oss:20b`) is exact-only: the table distinguishes
    tagged variants, so another tag is drift, not the same model. A base id
    (`nemotron-3-nano`) matches its tagged catalog variants.
    """
    exactos = [c for c in catalog_ids if c == slate_id]
    if exactos or ":" in slate_id:
        return exactos
    return sorted(c for c in catalog_ids if _base(c) == _base(slate_id))


def _drift_message(reporte: CatalogReport, slate_count: int) -> str:
    lineas = [
        (
            f"preflight: catalog drift - /v1/models is missing {len(reporte.missing)} of "
            f"{slate_count} slate ids: {', '.join(reporte.missing)}"
        )
    ]
    if reporte.unseen:
        lineas.append(
            "catalog ids not in the price table (rename candidates?): " + ", ".join(reporte.unseen)
        )
    lineas.append(
        "aborted before any request; the dry-run mark was consumed - re-run "
        "`bench dry-run --level <L>` before the next attempt"
    )
    return "\n".join(lineas)


async def _verify_async(
    cliente: OllamaCloud, slate_ids: list[str], table_models: set
) -> CatalogReport:
    status, payload = await cliente.models()
    if status != 200 or not isinstance(payload, dict):
        raise PreflightError(
            f"preflight: catalog read failed (HTTP {status}) - aborting before any request; "
            "the dry-run mark was consumed - re-run `bench dry-run --level <L>`"
        )
    data = payload.get("data")
    if not isinstance(data, list):
        raise PreflightError(
            "preflight: /v1/models returned an unrecognized payload - aborting before "
            "any request; the dry-run mark was consumed - re-run `bench dry-run --level <L>`"
        )
    catalog_ids = sorted(
        entrada["id"]
        for entrada in data
        if isinstance(entrada, dict) and isinstance(entrada.get("id"), str)
    )
    matched: dict[str, str] = {}
    missing: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    for slate_id in slate_ids:
        coincidencias = _match(slate_id, catalog_ids)
        if not coincidencias:
            missing.append(slate_id)
        else:
            matched[slate_id] = coincidencias[0]
            if len(coincidencias) > 1:
                # Several tagged variants refer to the same slate id: requests
                # will bill whichever sorted first, while the price table prices
                # the untagged row. Surfaced loudly, never silently swallowed.
                ambiguous[slate_id] = coincidencias
    bases_tabla = {_base(m) for m in table_models}
    unseen = sorted(c for c in catalog_ids if _base(c) not in bases_tabla)
    reporte = CatalogReport(
        http=status,
        ids=catalog_ids,
        matched=matched,
        missing=missing,
        unseen=unseen,
        ambiguous=ambiguous,
    )
    if missing:
        raise PreflightError(_drift_message(reporte, len(slate_ids)))
    return reporte


def verify(*, slate_ids: list[str], table_models) -> CatalogReport:
    """Reads the live catalog and validates the slate against it.

    Owns its client (and its one event loop, closed on the way out); the API key
    comes from the environment exactly as everywhere else in the harness. Any
    transport failure (unreachable host, timeout, a misconfigured base URL)
    surfaces as a clean PreflightError, never a traceback.
    """

    async def _corrida() -> CatalogReport:
        cliente = OllamaCloud()
        try:
            return await _verify_async(cliente, list(slate_ids), set(table_models))
        finally:
            await cliente.aclose()

    try:
        return asyncio.run(_corrida())
    except PreflightError:
        raise
    except Exception as e:  # noqa: BLE001 - a transport failure is a clean abort, not a crash
        raise PreflightError(
            f"preflight: catalog read failed ({type(e).__name__}: {e}) - aborting before "
            "any request; the dry-run mark was consumed - re-run `bench dry-run --level <L>`"
        ) from None
