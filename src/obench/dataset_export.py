"""Readable dataset export: a release's raw evidence flattened to JSON, CSV
and Excel, so a dataset can be read and audited without touching JSONL.

The flattening is lossless and schema-tolerant: every raw JSONL line becomes
one row — scalar fields as columns, nested objects/lists (api, medidor_pre/
post, tool_calls, salted...) serialized as compact JSON inside their cell —
and the column set is the first-appearance union across rows, so a protocol
version that adds or drops a field needs no exporter change. The same
flattening renders all three formats:

- `dataset/dataset.json`: one self-describing document (header + the four
  tables as parsed JSON — no cell encoding needed).
- `dataset/{requests,batches,canary,pricing}.csv`: one file per table,
  UTF-8, for any spreadsheet or `pandas.read_csv`.
- `dataset/dataset.xlsx`: one workbook, a sheet per table plus a `README`
  sheet carrying the header (run, table vintage, source hashes, how to
  regenerate).

Four fixed tables come from the packaged files: `requests` and `batches`
(the raw evidence), `canary` (when the run shipped one) and `pricing` (the
table snapshot, one row per model). The manifest, probe and calibration
sidecars stay JSONL/JSON-only: they are harness bookkeeping, not workload
evidence.

Everything here is derived work (methodology: derivatives regenerate from
raw and never edit it): the caller stamps the written files into the
release's sha256 map, so they ship sealed like any other packaged byte.
"""

from __future__ import annotations

import csv
import hashlib
import json
import pathlib

from .pricing import PriceTable

DATASET_KIND = "obench-dataset-readable"
# The tarball's arcname prefix for the derived files (their release-relative
# names are `dataset/<file>`); the local staging directory uses the same
# basename layout so package() can stamp and add them in one pass.
DATASET_ARCPREFIX = "dataset"
# Table name -> the packaged rel-path each source must match.
_SOURCES = (
    ("requests", "runs/requests-"),
    ("batches", "batches/batches-"),
    ("canary", "runs/canary-"),
)


class ExportError(Exception):
    """The readable dataset could not be produced (unreadable input or I/O)."""


def _sha256(ruta: pathlib.Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def _jsonl_rows(ruta: pathlib.Path, tabla: str) -> list[dict]:
    """The file's parseable lines; a torn line is skipped — the sha256 map
    pins the bytes and analyze tolerates the same tails."""
    filas: list[dict] = []
    texto = ruta.read_bytes().decode("utf-8", errors="replace")
    for linea in texto.splitlines():
        if not linea.strip():
            continue
        try:
            doc = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            doc["table"] = tabla
            filas.append(doc)
    return filas


def _pricing_rows(ruta: pathlib.Path) -> list[dict]:
    """The table snapshot, one row per model: the auditable shape of the
    pairing's `table` half."""
    tabla = PriceTable(ruta)  # parses + validates the rates
    doc = json.loads(ruta.read_text(encoding="utf-8"))
    capturado = doc.get("captured")
    return [
        {
            "table_version": tabla.table_version,
            "captured": capturado,
            "model": modelo,
            "input": tarifas.get("input"),
            "cached_input": tarifas.get("cached_input"),
            "output": tarifas.get("output"),
            "per": tabla.per,
            "currency": tabla.currency,
            "table": "pricing",
        }
        for modelo, tarifas in sorted(tabla.models.items())
    ]


def _tables(archivos: list[tuple[str, pathlib.Path]]) -> dict[str, list[dict]]:
    """{table: rows} over the packaged files that carry one of the four
    tables; everything else (manifest, probe, calibrations) is bookkeeping
    and stays out."""
    tablas: dict[str, list[dict]] = {}
    fuentes: dict[str, pathlib.Path] = {}
    for rel, ruta in archivos:
        for tabla, prefijo in _SOURCES:
            if rel.startswith(prefijo):
                fuentes[tabla] = ruta
    if "requests" not in fuentes or "batches" not in fuentes:
        raise ExportError("the dataset carries no requests/batches raw lines - nothing to flatten")
    for tabla, prefijo in _SOURCES:
        ruta = fuentes.get(tabla)
        if ruta is not None:
            tablas[tabla] = _jsonl_rows(ruta, tabla)
    ruta_precio = next((ruta for rel, ruta in archivos if rel.startswith("pricing/")), None)
    if ruta_precio is not None:
        tablas["pricing"] = _pricing_rows(ruta_precio)
    return tablas


def _columns(filas: list[dict]) -> list[str]:
    """First-appearance union across rows: stable against added fields and
    never reordered by a late column."""
    columnas: list[str] = []
    vistas: set[str] = set()
    for fila in filas:
        for clave in fila:
            if clave not in vistas:
                vistas.add(clave)
                columnas.append(clave)
    return columnas


def _celda(valor, *, plano: bool):
    """One row value for the flat formats. `plano` (CSV/XLSX) serializes the
    nested shapes as compact JSON; None reads as an empty cell and booleans
    as lowercase literals (JSON's, not Python's)."""
    if valor is None:
        return None if not plano else ""
    if valor is True:
        return "true"
    if valor is False:
        return "false"
    if isinstance(valor, (str, int, float)):
        return valor
    return valor if not plano else json.dumps(valor, ensure_ascii=False, separators=(",", ":"))


def _write_json(
    ruta: pathlib.Path, tablas: dict[str, list[dict]], header: dict, fuentes: dict
) -> None:
    doc = {
        "kind": DATASET_KIND,
        **header,
        "generated_from": {rel: _sha256(ruta) for rel, ruta in fuentes.items()},
        "tables": tablas,
    }
    tmp = ruta.with_name(ruta.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ruta)


def _write_csvs(dir_salida: pathlib.Path, tablas: dict[str, list[dict]]) -> None:
    for tabla, filas in tablas.items():
        ruta = dir_salida / f"{tabla}.csv"
        tmp = ruta.with_name(ruta.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            escritor = csv.writer(f)
            columnas = _columns(filas)
            escritor.writerow(columnas)
            for fila in filas:
                escritor.writerow([_celda(fila.get(c), plano=True) for c in columnas])
        tmp.replace(ruta)


def _write_xlsx(
    ruta: pathlib.Path, tablas: dict[str, list[dict]], header: dict, fuentes: dict
) -> None:
    try:
        import xlsxwriter
    except ImportError as e:  # pragma: no cover - a hard dependency, but say so
        raise ExportError(f"the Excel export needs xlsxwriter installed ({e})") from None
    tmp = ruta.with_name(ruta.name + ".tmp")
    libro = xlsxwriter.Workbook(str(tmp))
    # The README sheet first: anyone opening the workbook lands on the
    # provenance — what this is, what priced it, and how to regenerate it.
    readme = libro.add_worksheet("README")
    readme.write(0, 0, "bench readable dataset")
    readme.write(1, 0, f"kind: {DATASET_KIND}")
    readme.write(2, 0, f"run_id: {header.get('run_id')}")
    readme.write(3, 0, f"level: {header.get('level')}")
    readme.write(4, 0, f"table_version: {header.get('table_version')}")
    readme.write(5, 0, f"protocol_version: {header.get('protocol_version')}")
    readme.write(6, 0, "sources (sha256):")
    fila = 7
    for rel, sha in fuentes.items():
        readme.write(fila, 0, f"{rel}  sha256={sha}")
        fila += 1
    readme.write(
        fila, 0, "regenerate: bench analyze --release <tag> / bench dataset --release <tag>"
    )
    for nombre, filas in tablas.items():
        hoja = libro.add_worksheet(nombre[:31])
        columnas = _columns(filas)
        for c, clave in enumerate(columnas):
            hoja.write(0, c, str(clave))
        for f, fila_doc in enumerate(filas, start=1):
            for c, clave in enumerate(columnas):
                hoja.write(f, c, _celda(fila_doc.get(clave), plano=True))
    libro.close()
    tmp.replace(ruta)


def export_dataset(
    destino: pathlib.Path, archivos: list[tuple[str, pathlib.Path]], *, header: dict
) -> list[tuple[str, pathlib.Path]]:
    """Writes the readable dataset under `destino` and returns
    (release-relative name, local path) per written file, ready for the
    caller to stamp and package. Never edits the sources: everything here is
    derived from the bytes the release already pairs."""
    destino = pathlib.Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    tablas = _tables(archivos)
    fuentes = {
        rel: ruta
        for rel, ruta in archivos
        if rel.startswith(tuple(prefijo for _, prefijo in _SOURCES)) or rel.startswith("pricing/")
    }
    ruta_json = destino / "dataset.json"
    _write_json(ruta_json, tablas, header, fuentes)
    _write_csvs(destino, tablas)
    ruta_xlsx = destino / "dataset.xlsx"
    _write_xlsx(ruta_xlsx, tablas, header, fuentes)
    return [(f"{DATASET_ARCPREFIX}/{p.name}", p) for p in sorted(destino.iterdir()) if p.is_file()]
