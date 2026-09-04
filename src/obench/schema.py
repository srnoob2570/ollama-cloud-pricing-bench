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
    # The cache-free lane's billing evidence (protocol v3): the exact prompt sent
    # (nonce + fixture) and the nonce itself; both null on exempt traffic.
    "prompt_sha256": (str, None),
    "nonce_sha256": (str, None),
    "api": (dict, None),
    "http": (int, None),
    "err": (str, None),
    "checker": (str, None),
    "tool_calls": (list, None),  # accumulated tool-call frames (T2 tool_calling; [] otherwise)
    "steps": (list, None),  # per-step records of the T3 agent loop ([] otherwise):
    # action, outcome, reply, and the tokens each loop step billed.
    "sandbox": (dict, None),  # the T3 checker's sandbox run (null for T1/T2)
    "out_text_hash": (str, None),
    "fixture_hash": (str,),
    "table_version": (str,),
    "protocol_version": (str,),
}

_BATCH_SCHEMA: dict[str, tuple] = {
    "batch_id": (str,),
    "run_id": (str,),
    "level": (str,),
    # null on a pooled bracket: the pool below names the workloads it covers
    "workload": (str, None),
    "model": (str,),
    "fixture_hash": (str,),
    "k": (int,),
    "n": (int,),
    # The repetitions the bracket pools (methodology v1.1 §5's hybrid
    # composition): the cell's n on a per-cell bracket, the pool's
    # per-workload count on a pooled one; 1 on a single-rep bracket.
    "reps": (int,),
    # {"workloads": [..], "reps": N} on a pooled bracket — its legacy
    # attribution per workload derives post-hoc from the request lines' tokens
    # (never a stored weight); null on a per-cell bracket.
    "pool": (dict, None),
    "settle_s": (float, int),  # the registration cap the bracket was granted (v3)
    "settle_mode": (str,),  # v3: the settle is the registration loop
    "settle_reads": (int,),  # meter polls issued by the registration loop
    # Seconds after the count-check read when each window's pp took its final
    # value (0.0 = already at it; null when no registration sample exists).
    "registered_session_s": (float, int, None),
    "registered_weekly_s": (float, int, None),
    "settle_exit": (str, None),  # "stable" | "capped"; null when the loop never ran
    "count_check_s": (float, int, None),  # null on an aborted batch closed without a check
    "wall_clock_s": (float, int, None),  # the cell's makespan (null when nothing completed)
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

# The concurrency probe's volley line (runs/probe-*.jsonl): one line per k level.
# The probe is discovery, not a bracketed measurement: no meter payload, no
# checker — the per-request outcomes (accepted / 429 / error) are its evidence.
_PROBE_SCHEMA: dict[str, tuple] = {
    "probe_id": (str,),
    "run_id": (str,),
    "level": (str,),
    "model": (str,),
    "workload": (str,),
    "k": (int,),
    "requested": (int,),
    "accepted": (int,),
    "rejected": (int,),
    "errored": (int,),
    "t_start": (float, int),
    "t_total": (float, int),
    "outcomes": (list,),  # per request: {http, err, done}, in launch order
    "seeds": (list,),  # the volley's transmitted seeds, aligned with outcomes
    "fixture_hash": (str,),
    "table_version": (str,),
    "protocol_version": (str,),
}


# The billing canary's line (runs/canary-<run_id>.jsonl, protocol v3): the
# once-per-run paired check that the cache-free lane holds — 5 salted requests +
# 5 identical-prefix replays of one T2-size body. The replay must bill at the
# cache discount (~11–14 % of the salted quota, measured 1/7 on kimi-k3); the
# line carries both volleys' raw meter brackets so the ratio is re-derivable.
_CANARY_SCHEMA: dict[str, tuple] = {
    "canary_id": (str,),
    "run_id": (str,),
    "level": (str,),
    "model": (str,),
    "workload": (str,),  # "billing-canary": the exempt traffic's own identity
    "body_fixture_hash": (str,),
    "body_sha256": (str,),  # the unsalted body's hash (the shared provenance)
    "salted": (dict,),  # {nonce_sha256: [5], seeds: [5], outcomes: [{http, err, done}]}
    "replay": (dict,),  # {nonce_sha256, seeds: [5], outcomes: [...]} — salted[0]'s prefix
    "meter": (dict,),  # the four raw payloads: salted/replay x pre/post
    "dpp": (dict,),  # {salted_session, salted_weekly, replay_session, replay_weekly}
    "ratio": (float, int, None),  # replay / salted, on the basis window; null = unmeasurable
    "ratio_basis": (str, None),  # "session" | "weekly" (the probe's finer window first)
    "alarm": (bool,),
    "reads": (dict,),  # {salted: n, replay: n} — the registration loops' poll counts
    "settle_exits": (dict,),  # {salted: "stable"|"capped"|None, replay: ...}
    "table_version": (str,),
    "protocol_version": (str,),
    "notes": (str,),
    "at": (float, int),
}


# The predictability flow's estimate line (predictability/estimates-phase*.jsonl):
# the owner's locked estimate for one cell — timestamped and hash-stamped before
# the run that validates it (methodology v1 §8; the hash covers the whole line
# minus itself, so an edited registry never verifies again).
_ESTIMATE_SCHEMA: dict[str, tuple] = {
    "cell": (dict,),  # {"workload": str, "model": str}
    "phase": (str,),  # "blind" | "informed"
    "estimated_pp": (float, int),  # the legacy side's native unit (weekly pp)
    "estimated_usd": (float, int),  # the new side's native unit (dollars of credits)
    "notes": (str,),
    "timestamp": (float, int),
    "table_version": (str,),  # the table the estimator was shown
    "evidence": (dict,),  # raw-line counts at record time (blindness provenance)
    "hash": (str,),
}


class SchemaError(Exception):
    """A raw line does not honor the agreed dataset schema."""


def _validate(line: dict, esquema: dict, tipo: str) -> None:
    if not isinstance(line, dict):
        raise SchemaError(f"{tipo} line is not an object: {type(line).__name__}")
    faltantes = [k for k in esquema if k not in line]
    if faltantes:
        raise SchemaError(f"{tipo} line is missing fields: {', '.join(sorted(faltantes))}")
    sobrantes = [k for k in line if k not in esquema]
    if sobrantes:
        # A field the schema does not declare is a producer bug (a typo writes
        # the wrong evidence under the wrong name): it fails loudly on write.
        raise SchemaError(f"{tipo} line carries undeclared fields: {', '.join(sorted(sobrantes))}")
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
    if not isinstance(line["reps"], bool) and line["reps"] < 1:
        raise SchemaError(f"batch line: field 'reps' must be >= 1 (got {line['reps']!r})")
    pool = line["pool"]
    if pool is None:
        if line["workload"] is None:
            raise SchemaError("batch line: a bracket without 'workload' must carry its 'pool'")
        return
    cargas = pool.get("workloads")
    if (
        not isinstance(cargas, list)
        or not cargas
        or not all(isinstance(w, str) and w for w in cargas)
    ):
        raise SchemaError("batch line: 'pool.workloads' must be a non-empty list of workload names")
    if len(set(cargas)) != len(cargas):
        raise SchemaError("batch line: 'pool.workloads' repeats a workload name")
    reps = pool.get("reps")
    if not isinstance(reps, int) or isinstance(reps, bool) or reps < 1:
        raise SchemaError("batch line: 'pool.reps' must be an int >= 1")
    if reps != line["reps"]:
        raise SchemaError("batch line: 'pool.reps' and 'reps' disagree")
    if line["workload"] is not None:
        raise SchemaError("batch line: a pooled bracket carries 'workload': null")


def validate_probe_line(line: dict) -> None:
    _validate(line, _PROBE_SCHEMA, "probe")


def validate_canary_line(line: dict) -> None:
    _validate(line, _CANARY_SCHEMA, "canary")


def validate_estimate_line(line: dict) -> None:
    _validate(line, _ESTIMATE_SCHEMA, "estimate")
    celda = line["cell"]
    if not isinstance(celda.get("workload"), str) or not isinstance(celda.get("model"), str):
        raise SchemaError("estimate line: 'cell' must carry 'workload' and 'model' strings")
