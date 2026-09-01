"""Schema validation of the raw dataset lines, on write (methodology v1 §4).

`runs/*.jsonl` (per request) and `batches/*.jsonl` (per bracketed batch) are the
immutable raw evidence: every line is validated before it touches disk, so a
malformed producer fails loudly instead of poisoning the dataset.
"""

from __future__ import annotations

# field -> allowed types (None means the key must still be present, with value null)
_REQUEST_SCHEMA: dict[str, tuple] = {
    "req_id": (str,),
    "batch_id": (str,),
    "run_id": (str,),
    "level": (str,),
    "workload": (str,),
    "model": (str,),
    "seed": (int,),
    "rep": (int,),
    "k": (int,),
    "t_start": (float, int),
    "t_first_chunk": (float, int, None),
    "t_total": (float, int),
    "chunks": (int,),
    "tok_in": (int, None),
    "tok_out": (int, None),
    "tok_cached": (int, None),
    "api": (dict, None),
    "http": (int, None),
    "err": (str, None),
    "checker": (str, None),
    "tool_calls": (list, None),  # accumulated tool-call frames (T2 tool_calling; [] otherwise)
    "out_text_hash": (str, None),
    "fixture_hash": (str,),
    "table_version": (str,),
    "protocol_version": (str,),
}

_BATCH_SCHEMA: dict[str, tuple] = {
    "batch_id": (str,),
    "run_id": (str,),
    "level": (str,),
    "workload": (str,),
    "model": (str,),
    "fixture_hash": (str,),
    "k": (int,),
    "n": (int,),
    "settle_s": (float, int),
    "medidor_pre": (dict,),
    # medidor_post is null on an aborted batch whose post read itself failed
    "medidor_post": (dict, None),
    "dpp_session": (float, int, None),
    "dpp_weekly": (float, int, None),
    "request_counts": (dict,),
    "table_version": (str,),
    "protocol_version": (str,),
    "notes": (str,),
}


class SchemaError(Exception):
    """A raw line does not honor the agreed dataset schema."""


def _validate(line: dict, esquema: dict, tipo: str) -> None:
    if not isinstance(line, dict):
        raise SchemaError(f"{tipo} line is not an object: {type(line).__name__}")
    faltantes = [k for k in esquema if k not in line]
    if faltantes:
        raise SchemaError(f"{tipo} line is missing fields: {', '.join(sorted(faltantes))}")
    for campo, permitidos in esquema.items():
        valor = line[campo]
        admite_nulo = None in permitidos
        concretos = tuple(t for t in permitidos if t is not None)
        if isinstance(valor, bool) and int in concretos:
            raise SchemaError(f"{tipo} line: field {campo!r} must be an int, not a bool")
        if valor is None and not admite_nulo:
            raise SchemaError(f"{tipo} line: field {campo!r} must not be null (got null)")
        if valor is not None and not isinstance(valor, concretos):
            raise SchemaError(
                f"{tipo} line: field {campo!r} has type {type(valor).__name__}, "
                f"expected {' or '.join(t.__name__ for t in concretos)}"
            )


def validate_request_line(line: dict) -> None:
    _validate(line, _REQUEST_SCHEMA, "request")


def validate_batch_line(line: dict) -> None:
    _validate(line, _BATCH_SCHEMA, "batch")
