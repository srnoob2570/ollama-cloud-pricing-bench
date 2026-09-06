"""The predictability experiment (methodology v1 §8): locked estimates + the comparative MAPE.

`bench predict` is the HITL flow around Ollama's claim — "GPU-time based billing was
difficult to predict". The owner is subject and judge: before a cell runs, they estimate
its cost blind; when the execution ends, they re-estimate it informed. The real of a cell
is the median of its n=5 runs; the error is |estimate − real| / real, and the verdict is
COMPARATIVE (MAPE legacy vs MAPE new, bootstrap CI) — never an absolute threshold. The
experiment reuses the workloads' runs: zero extra quota.

The cell list (redrawn onto the measurable set — methodology v1.1 §8: the strong
four T2 workloads + T3 on the legacy side, where the meter resolves the real;
the sub-tick cells stay out — the weak trio pools per model, marked allocated
and never verdicted, which is a treatment, not an opacity finding of this report):

    {long_context, long_generation, ratio_in, ratio_out}
        x {glm-5.3-flash, kimi-k3}                                = 8 cells
    + {multi_file, debugging, refactoring}
        x {glm-5.3-flash, kimi-k2.7-code}                         = 6

14 cells. (kimi-k3 does not belong to the T3 slate, so the agentic cells carry
kimi-k2.7-code instead — the v1 design's slate-coherence rule, kept.) Every
estimate is recorded in the system's NATIVE units — weekly-window pp (legacy)
and dollars of credits (new): bridging tokens to GPU-time is part of the
difficulty under test, so the anchor never enters the MAPE.

Two phases, one locked estimate per cell per phase, under
`predictability/estimates-phase{1,2}.jsonl`:

- **blind** (phase 1): recorded only while the cell's real does not exist. The flow
  enforces the ordering against the raw datasets themselves — any request or batch line
  for the cell, under any protocol vintage, refuses the estimate: an estimate made after
  seeing the real would be dishonest by construction, not merely unluckily biased.
- **informed** (phase 2): the re-estimation, recorded only for a cell whose blind
  estimate exists (it is a RE-estimation of that number) and whose real measurement is
  already in the dataset.

Every record locks on write: the line carries its timestamp and a sha256 over its own
content, the file is rewritten atomically (tmp + rename), and every later read
re-verifies every prior line's hash — a registry edited after the fact refuses to grow
and refuses to report.

The report (`bench predict --report`) is offline like analyze: the real of each cell
comes from the analyze derivatives — legacy = the median Δpp(weekly) of the cell's reps;
new = the S0 extrapolation of each rep's measured tokens x the versioned table (the
model's effective S1 — measured where the calibration was conclusive, else the
versioned default — as the sensitivity). Anchoring (methodology v1.2): estimates and
verdicts read the persisted S0/S1 pair — S0 floor + S1 (default 50 %) with the
measured hit-rate winning where conclusive; a custom S(x) never re-anchors them
(custom S enters only through analyze's stamped re-runs). MAPE per system, carried
per cell (APE), per workload and aggregate, each
aggregate with a percentile bootstrap CI under a fixed seed — the study never gambles —
plus the paired bootstrap of MAPE_legacy − MAPE_new as the comparative verdict.

Sub-resolution rule: a cell whose real Δpp sits under a tick (0.1 pp) is EXCLUDED from
the legacy-side MAPE and reported as an opacity finding in itself — the meter cannot
resolve the cost of a small workload, so legacy predictability for it is structurally
unmeasurable rather than good. The new side has no such floor: its resolution is cents.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import pathlib
import random
import statistics
import time

from . import analyze as analyze_mod
from . import lane as lane_mod
from . import workloads as workloads_mod
from .analyze import S1_DEFAULT
from .analyze import _es_numero  # the cost model's number test, shared with analyze
from .client import PROTOCOL_VERSION
from .cost import new_task_cost
from .meter import TICK_BAND, TICK_PP  # the meter's resolution, in percentage points
from .pricing import TableError
from .schema import read_dataset, validate_estimate_line

BLIND = "blind"
INFORMED = "informed"
PHASES = (BLIND, INFORMED)
PREDICT_DIR = "predictability"
PHASE_FILE = {BLIND: "estimates-phase1.jsonl", INFORMED: "estimates-phase2.jsonl"}
BOOTSTRAP_B = 2000  # resamples per percentile CI
BOOTSTRAP_SEED = 20260901  # fixed: the same estimates always yield the same CI
# Float-noise floor for the verdict's sign test (comparison only, never applied
# to the persisted CI bounds): a delta CI whose bounds sit within it of zero is
# the meter's residue, not a resolvable difference.
SIGN_BAND = 1e-12
MEASURED = "measured"
SUB_RESOLUTION = "sub_resolution"
UNMEASURED = "unmeasured"


class PredictError(Exception):
    """The flow refused (ordering, locking, or a cell outside the grid)."""


# The cell list: (workload, model) pairs — the measurable set (methodology v1.1
# §8): the strong four T2 workloads + T3, the pairs the meter resolves per cell.
# The workload half is DERIVED from the workload table (the same STRONG_T2 set
# runner.py anchors the hybrid brackets to) so a re-scope cannot leave a stale
# copy here; the model half is the study's own decision (kimi-k3 does not belong
# to the T3 slate — the v1 design's slate-coherence rule, kept). The level
# follows the workload's own.
_MEDIBLES = tuple(w.name for w in workloads_mod.T2 if w.name in workloads_mod.STRONG_T2)
_AGENTICAS = tuple(w.name for w in workloads_mod.T3)
_GRID: tuple[tuple[str, str], ...] = (
    *((w, m) for w in _MEDIBLES for m in ("glm-5.3-flash", "kimi-k3")),
    *((w, m) for w in _AGENTICAS for m in ("glm-5.3-flash", "kimi-k2.7-code")),
)

# The fixture's public description — everything the blind estimator receives beyond
# the rate table. Prose carries the SHAPE only: the numbers live in the brief's
# structured fields (requests_per_run, tokens_in/out_per_request, straight from the
# workload table), so the two can never drift apart. The three T3 briefs share
# their boilerplate (the billed agent loop); only the task and the repo differ —
# one template, two parameters, no drift.
_T3_BRIEF = (
    "one agentic{task} task over a synthetic{repo}: a deterministic agent loop "
    "where every step is a billed chat request (up to the loop's step cap), "
    "pytest as the checker."
)
WORKLOAD_BRIEFS: dict[str, str] = {
    "long_context": "one document-comprehension request over a long register.",
    "long_generation": "one long-generation request: a short prompt, a very long output.",
    "ratio_in": "one extreme-input request: a huge document in, a tiny answer out.",
    "ratio_out": "extreme-output: many short requests, each a small prompt with a long answer.",
    "multi_file": _T3_BRIEF.format(task="", repo=" multi-file repo"),
    "debugging": _T3_BRIEF.format(task=" debugging", repo=" repo with a known bug"),
    "refactoring": _T3_BRIEF.format(task=" refactoring", repo=" multi-file repo"),
}


@dataclasses.dataclass(frozen=True)
class PredictCell:
    workload: str
    model: str
    level: str

    @property
    def key(self) -> str:
        return f"{self.workload}/{self.model}"


def _level_of(workload: str) -> str:
    for level, workloads in workloads_mod.WORKLOADS_BY_LEVEL.items():
        if any(w.name == workload for w in workloads):
            return level
    raise PredictError(f"unknown workload: {workload!r}")


def grid() -> tuple[PredictCell, ...]:
    """The measurable-set cells, validated against the workload table on every call."""
    cells = tuple(PredictCell(w, m, _level_of(w)) for w, m in _GRID)
    if len({c.key for c in cells}) != len(_GRID):
        raise PredictError(f"the predictability grid does not hold {len(_GRID)} distinct cells")
    return cells


def find_cell(workload: str | None, model: str | None) -> PredictCell:
    """The grid cell for a (workload, model) pair; PredictError outside the grid."""
    cells = grid()
    for c in cells:
        if c.workload == workload and c.model == model:
            return c
    raise PredictError(
        f"{workload!r}/{model!r} is not one of the predictability cells "
        f"({len(_GRID)} cells: {', '.join(c.key for c in cells)})"
    )


def fixture_brief(cell: PredictCell, table) -> dict:
    """What the estimator receives: the fixture's public description and the rate
    table — and nothing measured, ever. The cache-free lane's per-request salt
    (protocol v3) is public protocol, not measurement: its overhead is part of
    the brief, so the estimate can account for what will actually be sent."""
    workload = next(
        w for w in workloads_mod.WORKLOADS_BY_LEVEL[cell.level] if w.name == cell.workload
    )
    rate = table.rate(cell.model)
    nonce_words = lane_mod.nonce_words(workload.t_in)
    return {
        "cell": {"workload": cell.workload, "model": cell.model},
        "level": cell.level,
        "description": WORKLOAD_BRIEFS[cell.workload],
        "requests_per_run": workload.requests,
        "tokens_in_per_request": workload.t_in,
        "tokens_out_per_request": workload.t_out,
        # The cache-free lane's overhead (protocol v3): a run-scoped seeded nonce
        # rides every measured request as its first tokens.
        "nonce_words_per_request": nonce_words,
        "nonce_tokens_per_request": lane_mod.nonce_tokens_estimate(workload.t_in),
        "lane": (
            "cache-free: every measured request carries a seeded nonce "
            f"(~{nonce_words} words here) as its first tokens, forcing a cache "
            "miss - the measured cost is the workload's raw work"
        ),
        "rates": {
            "input": rate.input,
            "cached_input": rate.cached_input,
            "output": rate.output,
            "per": table.per,
        },
        "cache_discount": rate.has_cache_discount,
        "table_version": table.table_version,
    }


# ---------------------------------------------------------------------------
# the locked registry: estimates-phase{1,2}.jsonl
# ---------------------------------------------------------------------------


def _estimates_path(base, phase: str) -> pathlib.Path:
    return pathlib.Path(base) / PREDICT_DIR / PHASE_FILE[phase]


def line_hash(line: dict) -> str:
    """sha256 over the record's own content (every field but the hash itself)."""
    material = {k: v for k, v in line.items() if k != "hash"}
    canonico = json.dumps(
        material, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonico).hexdigest()


def cell_evidence(base, workload: str, model: str) -> tuple[int, int]:
    """(request lines, batch lines) the raw dataset already holds for the cell —
    any protocol vintage: the real exists or it does not, whatever wrote it.

    Pooled evidence counts (methodology v1.1 §5): the weak-trio cells' requests
    live inside pooled brackets whose batch lines carry workload null, but their
    REQUEST lines name the cell — so once a T2 hybrid run has measured the pool,
    the cell's real exists and a blind estimate refuses (an estimate made after
    the pool's data existed would be dishonest); the informed phase opens on it.
    The pooled real is an allocated reading, never a verdict — the report scores
    those cells as unmeasured until the verdict-level work consumes the
    allocation, and the v1.1 re-scoping redraws the grid onto the measurable
    set, where every cell is measured per-cell."""
    base = pathlib.Path(base)
    conteos = [
        sum(
            1
            for linea in read_dataset(base / folder, pattern)
            if linea.get("workload") == workload and linea.get("model") == model
        )
        for folder, pattern in (("runs", "requests-*.jsonl"), ("batches", "batches-*.jsonl"))
    ]
    return conteos[0], conteos[1]


def load_estimates(base, phase: str) -> list[dict]:
    """The phase's locked records, every hash re-verified.

    The registry's integrity IS the lock: a torn, foreign or edited line refuses the
    load — an estimate registry is never hand-repaired, it is restored or retired —
    and a duplicated cell is a corrupt registry, never a second chance to estimate.
    """
    if phase not in PHASES:
        raise PredictError(f"unknown phase: {phase!r}")
    path = _estimates_path(base, phase)
    if not path.exists():
        return []
    records: list[dict] = []
    for numero, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            linea = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PredictError(
                f"{path.name} line {numero} is not JSON ({e}): the registry is unreadable - "
                "an estimate registry is never hand-repaired; restore it or retire the phase"
            ) from None
        try:
            validate_estimate_line(linea)
        except Exception as e:  # noqa: BLE001 - a foreign line names itself in the message
            raise PredictError(
                f"{path.name} line {numero} does not honor the estimate schema: {e}"
            ) from None
        actual = line_hash(linea)
        if actual != linea["hash"]:
            raise PredictError(
                f"{path.name} line {numero} does not match its lock (stored "
                f"{str(linea['hash'])[:12]}, computed {actual[:12]}): the registry was edited "
                "after the estimate - its timestamp no longer proves anything"
            )
        records.append(linea)
    vistas: set[tuple[str, str]] = set()
    for linea in records:
        key = (linea["cell"]["workload"], linea["cell"]["model"])
        if key in vistas:
            raise PredictError(
                f"{path.name} holds two estimates for {key[0]}/{key[1]}: a cell carries "
                "exactly one estimate per phase - the registry is corrupt"
            )
        vistas.add(key)
    return records


def _find(records: list[dict], workload: str, model: str) -> dict | None:
    for r in records:
        if r["cell"]["workload"] == workload and r["cell"]["model"] == model:
            return r
    return None


def _keys_grid() -> set[tuple[str, str]]:
    """Every cell's (workload, model) key — the grid-membership test."""
    return {(c.workload, c.model) for c in grid()}


def _en_grid(records: list[dict], keys: set[tuple[str, str]]) -> int:
    """How many of the phase's locked records still belong to the grid: estimates
    locked under a retired scope (a v1-era registry, say) count nowhere — they
    are neither a cell's estimate nor evidence, and the report flags them."""
    return sum(1 for r in records if (r["cell"]["workload"], r["cell"]["model"]) in keys)


def record_estimate(
    base,
    *,
    phase: str,
    workload: str,
    model: str,
    estimated_pp: float,
    estimated_usd: float,
    notes: str = "",
    table,
    now: float | None = None,
) -> dict:
    """Records one locked estimate; raises PredictError on any ordering violation.

    The write is atomic (tmp + rename) over the whole file: the records already there
    were re-verified by the load, the new line joins them in one rename, and a crash
    mid-write can never tear a line.
    """
    if phase not in PHASES:
        raise PredictError(f"unknown phase: {phase!r}")
    for name, value in (("estimated_pp", estimated_pp), ("estimated_usd", estimated_usd)):
        # finite is the load-bearing half of the guard: +inf would pass a bare
        # "> 0" check and lock an infinite estimate into a hash-chained,
        # never-revisable registry whose report.json could not even parse
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not float(value) > 0
        ):
            raise PredictError(
                f"the estimate must be a finite number > 0 in native units ({name}); got {value!r}"
            )
    cell = find_cell(workload, model)
    previos = load_estimates(base, phase)
    prior = _find(previos, workload, model)
    if prior is not None:
        raise PredictError(
            f"{cell.key} already has a locked {phase} estimate (recorded at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(prior['timestamp']))} UTC, "
            f"hash {str(prior['hash'])[:12]}): an estimate is locked, not revisable"
        )
    requests, batches = cell_evidence(base, workload, model)
    if phase == BLIND:
        if requests or batches:
            raise PredictError(
                f"refusing a blind estimate for {cell.key}: its real already exists in the "
                f"dataset ({requests} request lines, {batches} batch lines) - an estimate "
                "made after seeing the real would be dishonest; record it as informed"
            )
    else:
        if _find(load_estimates(base, BLIND), workload, model) is None:
            raise PredictError(
                f"the informed re-estimation re-estimates the blind estimate: record the "
                f"blind estimate for {cell.key} first"
            )
        if not (requests or batches):
            raise PredictError(
                f"no measured evidence for {cell.key} yet: the informed phase re-estimates "
                "with the data already taken - run the cell first"
            )
    linea = {
        "cell": {"workload": workload, "model": model},
        "phase": phase,
        "estimated_pp": float(estimated_pp),
        "estimated_usd": float(estimated_usd),
        "notes": notes,
        "timestamp": time.time() if now is None else now,
        "table_version": table.table_version,
        "evidence": {"request_lines": requests, "batch_lines": batches},
    }
    linea["hash"] = line_hash(linea)
    validate_estimate_line(linea)
    path = _estimates_path(base, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for record in previos:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.write(json.dumps(linea, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return linea


def _state(record: dict | None) -> dict | None:
    """A phase row's projection of the locked estimate (key order preserved)."""
    if record is None:
        return None
    return {
        "estimated_pp": record["estimated_pp"],
        "estimated_usd": record["estimated_usd"],
        "timestamp": record["timestamp"],
        "hash": record["hash"],
    }


def plan_doc(base, table) -> dict:
    """The walk-through artifact: every cell's phase state plus the pending cells'
    public brief. Raises TableError when the table does not price a grid model —
    the brief would be lying about the rates the estimator will see. The counts
    cover grid cells only; estimates locked under a retired scope ride in
    counts['off_grid'] (the report flags them in findings.off_grid_estimates)."""
    base = pathlib.Path(base)
    keys = _keys_grid()
    ciegos = load_estimates(base, BLIND)
    informadas = load_estimates(base, INFORMED)
    rows = []
    for cell in grid():
        ciego = _find(ciegos, cell.workload, cell.model)
        informada = _find(informadas, cell.workload, cell.model)
        rows.append(
            {
                "workload": cell.workload,
                "model": cell.model,
                "level": cell.level,
                "blind": _state(ciego),
                "informed": _state(informada),
                # a brief only for what is still pending: an estimated cell has
                # already been walked through
                "brief": fixture_brief(cell, table) if ciego is None else None,
            }
        )
    ciegos_en, informadas_en = _en_grid(ciegos, keys), _en_grid(informadas, keys)
    return {
        "kind": "predictability-plan",
        "table_version": table.table_version,
        "cells": rows,
        "counts": {
            "blind": ciegos_en,
            "informed": informadas_en,
            "cells": len(grid()),
            "off_grid": (len(ciegos) - ciegos_en) + (len(informadas) - informadas_en),
        },
    }


# ---------------------------------------------------------------------------
# the comparative MAPE report
# ---------------------------------------------------------------------------


def _percentiles(samples: list[float]) -> tuple[float, float]:
    """The 2.5 / 97.5 percentile bounds of the resample means (sorted in place)."""
    samples.sort()
    return samples[int(0.025 * BOOTSTRAP_B)], samples[int(0.975 * BOOTSTRAP_B)]


def _bootstrap_ci(values: list[float]) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean, under the study's fixed seed.

    A single observation collapses to itself (there is nothing to resample);
    None with nothing to aggregate.
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(values)
    medias = []
    for _ in range(BOOTSTRAP_B):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        medias.append(sum(sample) / n)
    return _percentiles(medias)


def _bootstrap_delta_ci(legado: list[float], newCost: list[float]) -> tuple[float, float] | None:
    """Percentile bootstrap CI of mean(legacy APE) − mean(new APE), PAIRED: the same
    resample of cells feeds both systems, so their correlation survives resampling."""
    if not legado or len(legado) != len(newCost):
        return None
    if len(legado) == 1:
        d = legado[0] - newCost[0]
        return d, d
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(legado)
    deltas = []
    for _ in range(BOOTSTRAP_B):
        indices = [rng.randrange(n) for _ in range(n)]
        ml = sum(legado[i] for i in indices) / n
        mn = sum(newCost[i] for i in indices) / n
        deltas.append(ml - mn)
    return _percentiles(deltas)


def _verdict(ci_delta: tuple[float, float] | None) -> tuple[str, str]:
    """The comparative verdict (no absolute threshold): which system's cost the owner
    predicts better, and what that does to Ollama's claim. The claim — "GPU-time based
    billing was difficult to predict" — is supported when the legacy MAPE is the
    significantly larger one."""
    if ci_delta is None:
        return "no comparison", "not resolvable without paired measurable cells"
    lo, hi = ci_delta
    # The sign test reads the unrounded CI through the float-noise band: a CI
    # of ±1e-16 around zero is residue, never a verdict either way.
    if lo > SIGN_BAND:
        return "legacy less predictable", "supported"
    if hi < -SIGN_BAND:
        return "new less predictable", "contradicted"
    return "unresolved at this sample size", "not resolved"


def _cell_real(cell_doc: dict, table) -> dict:
    """The cell's real, from the analyze derivatives alone (native units).

    legacy: the median Δpp(weekly) of the cell's reps — the meter's own unit.
    new: PER RUN (one rep — the whole bracketed batch, all its requests' tokens
    priced together), the S0 extrapolation x the table via the single pricing
    formula, then the median over the reps — with the model's effective S1 as the
    sensitivity. That per-run basis is what the estimate describes (one run of the
    workload) and what the legacy Δpp measures, so the pairing is honest; it is a
    DIFFERENT aggregation from analyze's `new_cost_task_*_usd`, which prices the
    median per-request tokens — the two agree only for one-request workloads, and
    the field names never pretend otherwise. Null-safe: a rep without a readable
    bracket or without token evidence contributes nothing; a model the chosen
    table no longer prices extrapolates nothing.
    """
    reps = cell_doc["reps"]
    dpps = [r["dpp_weekly"] for r in reps if _es_numero(r.get("dpp_weekly"))]
    real_pp = statistics.median(dpps) if dpps else None

    try:
        rate = table.rate(cell_doc["model"])
    except TableError:
        rate = None
    s0s: list[float] = []
    s1s: list[float] = []
    if rate is not None:
        s_effective = (cell_doc.get("s_effective") or {}).get("s")
        s_value = s_effective if _es_numero(s_effective) else 0.0
        for r in reps:
            tin, tout = r.get("tokens_in"), r.get("tokens_out")
            if tin is None or tout is None:
                continue
            s0s.append(new_task_cost(tin, tout, rate, s=0.0, per=table.per))
            s1s.append(new_task_cost(tin, tout, rate, s=s_value, per=table.per))
    real_s0 = statistics.median(s0s) if s0s else None
    real_s1 = statistics.median(s1s) if s1s else None

    if real_pp is None:
        state = UNMEASURED
    elif real_pp < TICK_PP * (1 - TICK_BAND):
        # "Under a tick" through the residue band: the meter's deltas are
        # tick-quantized, so a real of exactly one tick (which unrounded
        # arithmetic lands a few 1e-14 below or above 0.1) is measured,
        # while anything genuinely under the tick stays excluded.
        state = SUB_RESOLUTION
    else:
        state = MEASURED
    return {
        "real_pp": real_pp,
        "real_new_s0_usd_per_run": real_s0,
        "real_new_s1_usd_per_run": real_s1,
        "legacy_status": state,
    }


def _ape(estimado: float | None, real: float | None) -> float | None:
    """|estimate − real| / real; None whenever either side is missing (never a 0/0)."""
    if real is None or not _es_numero(real) or real <= 0 or not _es_numero(estimado):
        return None
    return abs(estimado - real) / real


def _apes(rows: list[dict], fase: str, field: str, *, solo_medidos: bool = False) -> list[float]:
    """The phase's non-None APEs of one field across the rows; `solo_medidos` keeps
    the legacy-side exclusion (a real under a tick carries no legacy APE anywhere)."""
    return [
        f[fase][field]
        for f in rows
        if f[fase] is not None
        and f[fase][field] is not None
        and (not solo_medidos or f["legacy_status"] == MEASURED)
    ]


def _cell_rows(
    celdas_analyze: dict[tuple[str, str], dict],
    ciegos: list[dict],
    informadas: list[dict],
    table,
) -> tuple[list[dict], list[str]]:
    """One report row per grid cell, with each phase's APEs where an estimate exists."""
    rows = []
    obsoletas: list[str] = []
    for cell in grid():
        cell_doc = celdas_analyze.get((cell.model, cell.workload))
        real = (
            _cell_real(cell_doc, table)
            if cell_doc
            else {
                "real_pp": None,
                "real_new_s0_usd_per_run": None,
                "real_new_s1_usd_per_run": None,
                "legacy_status": UNMEASURED,
            }
        )
        row = {
            "workload": cell.workload,
            "model": cell.model,
            "level": cell.level,
            **real,
            "blind": None,
            "informed": None,
        }
        for fase, records in ((BLIND, ciegos), (INFORMED, informadas)):
            estimate = _find(records, cell.workload, cell.model)
            if estimate is None:
                continue
            # Vintage guard: an estimate locked against one table and a real priced
            # on another do not divide — the repricing itself would become the
            # error. The new-side APEs are set aside (analyze's set-aside
            # precedent), the legacy APE stands (pp is meter-native, the table
            # never touches it), and the mismatch is flagged, never blended.
            coinciden = estimate["table_version"] == table.table_version
            if not coinciden:
                obsoletas.append(
                    f"{cell.key} (estimate locked on {estimate['table_version']}, "
                    f"report priced on {table.table_version})"
                )
            row[fase] = {
                "estimated_pp": estimate["estimated_pp"],
                "estimated_usd": estimate["estimated_usd"],
                "timestamp": estimate["timestamp"],
                "table_version": estimate["table_version"],
                "table_vintage_mismatch": not coinciden,
                # the sub-resolution exclusion holds per cell too: a real under
                # a tick carries no legacy APE anywhere in the report
                "ape_legacy": _ape(estimate["estimated_pp"], real["real_pp"])
                if real["legacy_status"] == MEASURED
                else None,
                "ape_new": _ape(estimate["estimated_usd"], real["real_new_s0_usd_per_run"])
                if coinciden
                else None,
                "ape_new_s1": _ape(estimate["estimated_usd"], real["real_new_s1_usd_per_run"])
                if coinciden
                else None,
            }
        rows.append(row)
    return rows, obsoletas


def _fase_aggregates(rows: list[dict]) -> dict:
    """The per-system aggregates: legacy over the cells whose real resolves above a
    tick; new over the cells whose extrapolation exists (no resolution floor)."""

    def _mape(fase: str, field: str) -> dict | None:
        apes = _apes(rows, fase, field, solo_medidos=field == "ape_legacy")
        if not apes:
            return None
        ci = _bootstrap_ci(apes)
        return {
            "mape": sum(apes) / len(apes),
            "cells": len(apes),
            "ci": list(ci) if ci else None,
        }

    fases = {}
    for fase in PHASES:
        if not any(f[fase] is not None for f in rows):
            continue
        legacy = _mape(fase, "ape_legacy")
        newCost = _mape(fase, "ape_new")
        s1 = _mape(fase, "ape_new_s1")
        # the paired comparison only where BOTH systems are measurable
        parejask, parejasn = [], []
        for f in rows:
            if (
                f[fase] is not None
                and f["legacy_status"] == MEASURED
                and f[fase]["ape_legacy"] is not None
                and f[fase]["ape_new"] is not None
            ):
                parejask.append(f[fase]["ape_legacy"])
                parejasn.append(f[fase]["ape_new"])
        ci_delta = _bootstrap_delta_ci(parejask, parejasn)
        verdict, claim = _verdict(ci_delta)
        fases[fase] = {
            "mape_legacy": legacy,
            "mape_new": newCost,
            "mape_new_s1": s1,
            "paired_cells": len(parejask),
            "delta_mape": statistics.mean(parejask) - statistics.mean(parejasn)
            if parejask
            else None,
            "ci_delta": list(ci_delta) if ci_delta else None,
            "verdict": verdict,
            "ollama_claim": claim,
        }
    return fases


def _workload_breakdown(rows: list[dict]) -> list[dict]:
    """The per-workload MAPE means."""
    por_workload: dict[str, list[dict]] = {}
    for f in rows:
        por_workload.setdefault(f["workload"], []).append(f)
    desglose = []
    for workload, grupo in sorted(por_workload.items()):
        input = {"workload": workload, "level": grupo[0]["level"], "cells": []}
        for fase in PHASES:
            legado = _apes(grupo, fase, "ape_legacy", solo_medidos=True)
            newCost = _apes(grupo, fase, "ape_new")
            input[fase] = {
                "mape_legacy": statistics.mean(legado) if legado else None,
                "mape_new": statistics.mean(newCost) if newCost else None,
            }
        desglose.append(input)
    return desglose


def _findings(
    rows: list[dict],
    obsoletas: list[str],
    ciegos: list[dict],
    informadas: list[dict],
    keys: set[tuple[str, str]],
) -> dict:
    """The opacity findings: what the report cannot score, named never anonymized."""
    return {
        "sub_resolution_legacy": [
            f"{f['workload']}/{f['model']} (real {f['real_pp']:g} pp, under the {TICK_PP:g} pp tick)"
            for f in rows
            if f["legacy_status"] == SUB_RESOLUTION
        ],
        "unmeasured": [
            f"{f['workload']}/{f['model']}" for f in rows if f["legacy_status"] == UNMEASURED
        ],
        "stale_table_estimates": obsoletas,
        "pending_blind": [f"{f['workload']}/{f['model']}" for f in rows if f["blind"] is None],
        "pending_informed": [
            f"{f['workload']}/{f['model']}"
            for f in rows
            if f["blind"] is not None and f["informed"] is None
        ],
        # a cell measured before any blind estimate exists is a permanent dead
        # end for the study (the flow refuses a blind after the run): name it,
        # never leave it as an anonymous line in pending_blind
        "measured_without_blind": [
            f"{f['workload']}/{f['model']}"
            for f in rows
            if f["real_pp"] is not None and f["blind"] is None
        ],
        # estimates locked under a retired scope: valid hashes, real money
        # spent on the estimate, but no grid cell to attach to anymore
        "off_grid_estimates": sorted(
            f"{r['cell']['workload']}/{r['cell']['model']} ({r['phase']})"
            for fase, records in ((BLIND, ciegos), (INFORMED, informadas))
            for r in records
            if (r["cell"]["workload"], r["cell"]["model"]) not in keys
        ),
    }


def build_report(base, *, table) -> dict:
    """The MAPE report, offline from the raw datasets + the locked estimates.

    Anchored to the persisted S0/S1 pair (methodology v1.2): the new side's S1
    sensitivity resolves per model from the calibration (measured wins where
    conclusive) against the versioned default S1_DEFAULT — never against a
    custom S(x), which enters only through analyze's stamped re-runs. The
    verdict's MAPEs are native-unit, so the anchor never enters (analyze still
    receives the inert default anchor: its dollar derivatives are not this
    report's input — so the build is cells-only, the pooled/who-wins/curve/
    sensitivity derivatives are not paid).

    Estimates locked under a retired scope (cells outside the current grid, say
    a v1-era registry) count nowhere — neither in the headline counts nor in any
    APE — and are flagged in findings.off_grid_estimates; a cell whose real
    exists but whose blind estimate is missing can never join the study (the
    flow refuses a blind after the run) and is flagged in
    findings.measured_without_blind.
    """
    base = pathlib.Path(base)
    ciegos = load_estimates(base, BLIND)
    informadas = load_estimates(base, INFORMED)
    keys = _keys_grid()
    doc = analyze_mod.build(base, table=table, anchor=100.0, s=S1_DEFAULT, cells_only=True)
    celdas_analyze = {(c["model"], c["workload"]): c for c in doc["cells"]}
    rows, obsoletas = _cell_rows(celdas_analyze, ciegos, informadas, table)
    return {
        "kind": "predictability-report",
        "generated_at": time.time(),
        "protocol_version": PROTOCOL_VERSION,
        "table_version": table.table_version,
        "params": {
            "s1_default": S1_DEFAULT,
            "tick_pp": TICK_PP,
            "bootstrap_samples": BOOTSTRAP_B,
            "bootstrap_seed": BOOTSTRAP_SEED,
        },
        "estimates": {"blind": _en_grid(ciegos, keys), "informed": _en_grid(informadas, keys)},
        "cells": rows,
        "workloads": _workload_breakdown(rows),
        "aggregate": _fase_aggregates(rows),
        "findings": _findings(rows, obsoletas, ciegos, informadas, keys),
        "notes": (
            "computed offline from the locked estimate registries and the raw datasets "
            "(the reals come from the analyze derivatives: legacy = median dpp_weekly of "
            "the cell's reps; new = the S0 extrapolation of each rep's measured tokens x "
            "the versioned table, with the model's effective S1 as sensitivity - per RUN, "
            "one rep's whole batch, a different aggregation from analyze's per-task "
            "new_cost_task_*_usd). Every MAPE aggregate carries a percentile bootstrap CI "
            "under the fixed seed; the verdict is the paired bootstrap of MAPE_legacy - "
            "MAPE_new, comparative only. Cells whose real dp sits under a tick are "
            "excluded from the legacy side and reported as an opacity finding: the meter "
            "cannot resolve them, which makes legacy predictability structurally "
            "unmeasurable for those workloads. Estimates locked against another table "
            "vintage are set aside from the new-side APEs (the repricing itself would "
            "become the error) and flagged in findings.stale_table_estimates; the legacy "
            "APE stands, pp being meter-native. Estimates locked under a retired scope "
            "(cells outside the current grid) count nowhere and are flagged in "
            "findings.off_grid_estimates; a measured cell with no blind estimate is "
            "flagged in findings.measured_without_blind (the flow refuses a blind after "
            "the run, so that cell can never join the study)."
        ),
    }
