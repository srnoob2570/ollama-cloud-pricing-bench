"""Readable dataset export: a release's raw evidence flattened to JSON, CSV
and Excel, so a dataset can be read and audited without touching JSONL.

The flattening is lossless and schema-tolerant: every raw JSONL line becomes
one row — scalar fields as columns, nested objects/lists (api, meter_pre/
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


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl_rows(path: pathlib.Path, table: str) -> list[dict]:
    """The file's parseable lines; a torn line is skipped — the sha256 map
    pins the bytes and analyze tolerates the same tails."""
    rows: list[dict] = []
    text = path.read_bytes().decode("utf-8", errors="replace")
    for linea in text.splitlines():
        if not linea.strip():
            continue
        try:
            doc = json.loads(linea)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            doc["table"] = table
            rows.append(doc)
    return rows


def _pricing_rows(path: pathlib.Path) -> list[dict]:
    """The table snapshot, one row per model: the auditable shape of the
    pairing's `table` half."""
    table = PriceTable(path)  # parses + validates the rates
    doc = json.loads(path.read_text(encoding="utf-8"))
    capturado = doc.get("captured")
    return [
        {
            "table_version": table.table_version,
            "captured": capturado,
            "model": model,
            "input": rates.get("input"),
            "cached_input": rates.get("cached_input"),
            "output": rates.get("output"),
            "per": table.per,
            "currency": table.currency,
            "table": "pricing",
        }
        for model, rates in sorted(table.models.items())
    ]


def _tables(archivos: list[tuple[str, pathlib.Path]]) -> dict[str, list[dict]]:
    """{table: rows} over the packaged files that carry one of the four
    tables; everything else (manifest, probe, calibrations) is bookkeeping
    and stays out."""
    tables: dict[str, list[dict]] = {}
    sources: dict[str, pathlib.Path] = {}
    for rel, path in archivos:
        for table, prefijo in _SOURCES:
            if rel.startswith(prefijo):
                sources[table] = path
    if "requests" not in sources or "batches" not in sources:
        raise ExportError("the dataset carries no requests/batches raw lines - nothing to flatten")
    for table, prefijo in _SOURCES:
        path = sources.get(table)
        if path is not None:
            tables[table] = _jsonl_rows(path, table)
    price_path = next((path for rel, path in archivos if rel.startswith("pricing/")), None)
    if price_path is not None:
        tables["pricing"] = _pricing_rows(price_path)
    return tables


def _columns(rows: list[dict]) -> list[str]:
    """First-appearance union across rows: stable against added fields and
    never reordered by a late column."""
    columns: list[str] = []
    vistas: set[str] = set()
    for row in rows:
        for key in row:
            if key not in vistas:
                vistas.add(key)
                columns.append(key)
    return columns


def _cell(value):
    """One row value for the flat formats: nested shapes serialize as compact
    JSON; None reads as an empty cell and booleans as lowercase literals
    (JSON's, not Python's)."""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (str, int, float)):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _write_json(
    path: pathlib.Path, tables: dict[str, list[dict]], header: dict, sources: dict
) -> None:
    doc = {
        "kind": DATASET_KIND,
        **header,
        "generated_from": {rel: _sha256(path) for rel, path in sources.items()},
        "tables": tables,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_csvs(output_dir: pathlib.Path, tables: dict[str, list[dict]]) -> None:
    for table, rows in tables.items():
        path = output_dir / f"{table}.csv"
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            escritor = csv.writer(f)
            columns = _columns(rows)
            escritor.writerow(columns)
            for row in rows:
                escritor.writerow([_cell(row.get(c)) for c in columns])
        tmp.replace(path)


def _write_xlsx(
    path: pathlib.Path, tables: dict[str, list[dict]], header: dict, sources: dict
) -> None:
    try:
        import xlsxwriter
    except ImportError as e:  # pragma: no cover - a hard dependency, but say so
        raise ExportError(f"the Excel export needs xlsxwriter installed ({e})") from None
    tmp = path.with_name(path.name + ".tmp")
    workbook = xlsxwriter.Workbook(str(tmp))
    # The README sheet first: anyone opening the workbook lands on the
    # provenance — what this is, what priced it, and how to regenerate it.
    readme = workbook.add_worksheet("README")
    readme.write(0, 0, "bench readable dataset")
    readme.write(1, 0, f"kind: {DATASET_KIND}")
    readme.write(2, 0, f"run_id: {header.get('run_id')}")
    readme.write(3, 0, f"level: {header.get('level')}")
    readme.write(4, 0, f"table_version: {header.get('table_version')}")
    readme.write(5, 0, f"protocol_version: {header.get('protocol_version')}")
    readme.write(6, 0, "sources (sha256):")
    row = 7
    for rel, sha in sources.items():
        readme.write(row, 0, f"{rel}  sha256={sha}")
        row += 1
    readme.write(
        row, 0, "regenerate: bench analyze --release <tag> / bench dataset --release <tag>"
    )
    for name, rows in tables.items():
        worksheet = workbook.add_worksheet(name[:31])
        columns = _columns(rows)
        for c, key in enumerate(columns):
            worksheet.write(0, c, str(key))
        for f, row_doc in enumerate(rows, start=1):
            for c, key in enumerate(columns):
                worksheet.write(f, c, _cell(row_doc.get(key)))
    workbook.close()
    tmp.replace(path)


def export_dataset(
    dest: pathlib.Path, archivos: list[tuple[str, pathlib.Path]], *, header: dict
) -> list[tuple[str, pathlib.Path]]:
    """Writes the readable dataset under `dest` and returns
    (release-relative name, local path) per written file, ready for the
    caller to stamp and package. Never edits the sources: everything here is
    derived from the bytes the release already pairs."""
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    tables = _tables(archivos)
    sources = {
        rel: path
        for rel, path in archivos
        if rel.startswith(tuple(prefijo for _, prefijo in _SOURCES)) or rel.startswith("pricing/")
    }
    json_path = dest / "dataset.json"
    _write_json(json_path, tables, header, sources)
    _write_csvs(dest, tables)
    xlsx_path = dest / "dataset.xlsx"
    _write_xlsx(xlsx_path, tables, header, sources)
    return [(f"{DATASET_ARCPREFIX}/{p.name}", p) for p in sorted(dest.iterdir()) if p.is_file()]
