"""The predictability experiment (methodology v1 §8): locked estimates + the comparative MAPE.

`bench predict` is the HITL flow around Ollama's claim — "GPU-time based billing was
difficult to predict". The owner is subject and judge: before a cell runs, they estimate
its cost blind; when the execution ends, they re-estimate it informed. The real of a cell
is the median of its n=5 runs; the error is |estimate − real| / real, and the verdict is
COMPARATIVE (MAPE legacy vs MAPE new, bootstrap CI) — never an absolute threshold. The
experiment reuses the workloads' runs: zero extra quota.

The 12-cell subgrid (the design's decision, adjusted to the slates):

    {qa_short, long_context, multi_turn, reasoning, ratio_in}
        x {glm-5.3-flash, kimi-k3}                     = 10 cells
    + {multi_file} x {glm-5.3-flash, kimi-k2.7-code}   = 12

(kimi-k3 does not belong to the T3 slate, so the agentic cell carries kimi-k2.7-code
instead.) Every estimate is recorded in the system's NATIVE units — weekly-window pp
(legacy) and dollars of credits (new): bridging tokens to GPU-time is part of the
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
model's effective S1 — measured where the calibration was conclusive — as the
sensitivity). MAPE per system, carried per cell (APE), per workload and aggregate, each
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
from .analyze import _es_numero  # the cost model's number test, shared with analyze
from .calibration import TICK_BAND, TICK_PP
from .client import PROTOCOL_VERSION
from .cost import new_task_cost
from .pricing import TableError
from .schema import validate_estimate_line

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


# The subgrid: (workload, model) pairs — the design's 12 cells. The level follows
# the workload's own (qa_short is the T1 anchor; the rest are T2/T3 workloads).
_GRID: tuple[tuple[str, str], ...] = (
    *(
        (w, m)
        for w in ("qa_short", "long_context", "multi_turn", "reasoning", "ratio_in")
        for m in ("glm-5.3-flash", "kimi-k3")
    ),
    ("multi_file", "glm-5.3-flash"),
    ("multi_file", "kimi-k2.7-code"),
)

# The fixture's public description — everything the blind estimator receives beyond
# the rate table. Prose carries the SHAPE only: the numbers live in the brief's
# structured fields (requests_per_run, tokens_in/out_per_request, straight from the
# workload table), so the two can never drift apart.
WORKLOAD_BRIEFS: dict[str, str] = {
    "qa_short": "short Q&A prompts, one per request, a one-sentence answer each.",
    "long_context": "one document-comprehension request over a long register.",
    "multi_turn": "one multi-turn conversation, billed per turn.",
    "reasoning": "one reasoning task: a small prompt, a long thinking output.",
    "ratio_in": "one extreme-input request: a huge document in, a tiny answer out.",
    "multi_file": "one agentic task over a synthetic multi-file repo: a deterministic agent "
    "loop where every step is a billed chat request (up to the loop's step cap), pytest "
    "as the checker.",
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
    for nivel, cargas in workloads_mod.WORKLOADS_BY_LEVEL.items():
        if any(w.name == workload for w in cargas):
            return nivel
    raise PredictError(f"unknown workload: {workload!r}")


def grid() -> tuple[PredictCell, ...]:
    """The 12 cells, validated against the workload table on every call."""
    celdas = tuple(PredictCell(w, m, _level_of(w)) for w, m in _GRID)
    if len({c.key for c in celdas}) != 12:
        raise PredictError("the predictability grid does not hold 12 distinct cells")
    return celdas


def find_cell(workload: str | None, model: str | None) -> PredictCell:
    """The grid cell for a (workload, model) pair; PredictError outside the grid."""
    for c in grid():
        if c.workload == workload and c.model == model:
            return c
    raise PredictError(
        f"{workload!r}/{model!r} is not one of the predictability cells "
        f"({len(_GRID)} cells: {', '.join(c.key for c in grid())})"
    )


def fixture_brief(celda: PredictCell, tabla) -> dict:
    """What the estimator receives: the fixture's public description and the rate
    table — and nothing measured, ever. The cache-free lane's per-request salt
    (protocol v3) is public protocol, not measurement: its overhead is part of
    the brief, so the estimate can account for what will actually be sent."""
    carga = next(
        w for w in workloads_mod.WORKLOADS_BY_LEVEL[celda.level] if w.name == celda.workload
    )
    tarifa = tabla.rate(celda.model)
    nonce_palabras = lane_mod.nonce_words(carga.t_in)
    return {
        "cell": {"workload": celda.workload, "model": celda.model},
        "level": celda.level,
        "description": WORKLOAD_BRIEFS[celda.workload],
        "requests_per_run": carga.requests,
        "tokens_in_per_request": carga.t_in,
        "tokens_out_per_request": carga.t_out,
        # The cache-free lane's overhead (protocol v3): a run-scoped seeded nonce
        # rides every measured request as its first tokens.
        "nonce_words_per_request": nonce_palabras,
        "nonce_tokens_per_request": lane_mod.nonce_tokens_estimate(carga.t_in),
        "lane": (
            "cache-free: every measured request carries a seeded nonce "
            f"(~{nonce_palabras} words here) as its first tokens, forcing a cache "
            "miss - the measured cost is the workload's raw work"
        ),
        "rates": {
            "input": tarifa.input,
            "cached_input": tarifa.cached_input,
            "output": tarifa.output,
            "per": tabla.per,
        },
        "cache_discount": tarifa.has_cache_discount,
        "table_version": tabla.table_version,
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
    any protocol vintage: the real exists or it does not, whatever wrote it."""
    base = pathlib.Path(base)
    peticiones = sum(
        1
        for linea in analyze_mod._read_dataset(base / "runs", "requests-*.jsonl")
        if linea.get("workload") == workload and linea.get("model") == model
    )
    lotes = sum(
        1
        for linea in analyze_mod._read_dataset(base / "batches", "batches-*.jsonl")
        if linea.get("workload") == workload and linea.get("model") == model
    )
    return peticiones, lotes


def load_estimates(base, phase: str) -> list[dict]:
    """The phase's locked records, every hash re-verified.

    The registry's integrity IS the lock: a torn, foreign or edited line refuses the
    load — an estimate registry is never hand-repaired, it is restored or retired —
    and a duplicated cell is a corrupt registry, never a second chance to estimate.
    """
    if phase not in PHASES:
        raise PredictError(f"unknown phase: {phase!r}")
    ruta = _estimates_path(base, phase)
    if not ruta.exists():
        return []
    registros: list[dict] = []
    for numero, cruda in enumerate(ruta.read_text(encoding="utf-8").splitlines(), start=1):
        if not cruda.strip():
            continue
        try:
            linea = json.loads(cruda)
        except json.JSONDecodeError as e:
            raise PredictError(
                f"{ruta.name} line {numero} is not JSON ({e}): the registry is unreadable - "
                "an estimate registry is never hand-repaired; restore it or retire the phase"
            ) from None
        try:
            validate_estimate_line(linea)
        except Exception as e:  # noqa: BLE001 - a foreign line names itself in the message
            raise PredictError(
                f"{ruta.name} line {numero} does not honor the estimate schema: {e}"
            ) from None
        actual = line_hash(linea)
        if actual != linea["hash"]:
            raise PredictError(
                f"{ruta.name} line {numero} does not match its lock (stored "
                f"{str(linea['hash'])[:12]}, computed {actual[:12]}): the registry was edited "
                "after the estimate - its timestamp no longer proves anything"
            )
        registros.append(linea)
    vistas: set[tuple[str, str]] = set()
    for linea in registros:
        clave = (linea["cell"]["workload"], linea["cell"]["model"])
        if clave in vistas:
            raise PredictError(
                f"{ruta.name} holds two estimates for {clave[0]}/{clave[1]}: a cell carries "
                "exactly one estimate per phase - the registry is corrupt"
            )
        vistas.add(clave)
    return registros


def _find(registros: list[dict], workload: str, model: str) -> dict | None:
    for r in registros:
        if r["cell"]["workload"] == workload and r["cell"]["model"] == model:
            return r
    return None


def record_estimate(
    base,
    *,
    phase: str,
    workload: str,
    model: str,
    estimated_pp: float,
    estimated_usd: float,
    notes: str = "",
    tabla,
    now: float | None = None,
) -> dict:
    """Records one locked estimate; raises PredictError on any ordering violation.

    The write is atomic (tmp + rename) over the whole file: the records already there
    were re-verified by the load, the new line joins them in one rename, and a crash
    mid-write can never tear a line.
    """
    if phase not in PHASES:
        raise PredictError(f"unknown phase: {phase!r}")
    for nombre, valor in (("estimated_pp", estimated_pp), ("estimated_usd", estimated_usd)):
        # finite is the load-bearing half of the guard: +inf would pass a bare
        # "> 0" check and lock an infinite estimate into a hash-chained,
        # never-revisable registry whose report.json could not even parse
        if (
            isinstance(valor, bool)
            or not isinstance(valor, (int, float))
            or not math.isfinite(valor)
            or not float(valor) > 0
        ):
            raise PredictError(
                f"the estimate must be a finite number > 0 in native units "
                f"({nombre}); got {valor!r}"
            )
    celda = find_cell(workload, model)
    previos = load_estimates(base, phase)
    previo = _find(previos, workload, model)
    if previo is not None:
        raise PredictError(
            f"{celda.key} already has a locked {phase} estimate (recorded at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(previo['timestamp']))} UTC, "
            f"hash {str(previo['hash'])[:12]}): an estimate is locked, not revisable"
        )
    peticiones, lotes = cell_evidence(base, workload, model)
    if phase == BLIND:
        if peticiones or lotes:
            raise PredictError(
                f"refusing a blind estimate for {celda.key}: its real already exists in the "
                f"dataset ({peticiones} request lines, {lotes} batch lines) - an estimate "
                "made after seeing the real would be dishonest; record it as informed"
            )
    else:
        if _find(load_estimates(base, BLIND), workload, model) is None:
            raise PredictError(
                f"the informed re-estimation re-estimates the blind estimate: record the "
                f"blind estimate for {celda.key} first"
            )
        if not (peticiones or lotes):
            raise PredictError(
                f"no measured evidence for {celda.key} yet: the informed phase re-estimates "
                "with the data already taken - run the cell first"
            )
    linea = {
        "cell": {"workload": workload, "model": model},
        "phase": phase,
        "estimated_pp": float(estimated_pp),
        "estimated_usd": float(estimated_usd),
        "notes": notes,
        "timestamp": time.time() if now is None else now,
        "table_version": tabla.table_version,
        "evidence": {"request_lines": peticiones, "batch_lines": lotes},
    }
    linea["hash"] = line_hash(linea)
    validate_estimate_line(linea)
    ruta = _estimates_path(base, phase)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for registro in previos:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
        f.write(json.dumps(linea, ensure_ascii=False) + "\n")
    tmp.replace(ruta)
    return linea


def plan_doc(base, tabla) -> dict:
    """The walk-through artifact: every cell's phase state plus the pending cells'
    public brief. Raises TableError when the table does not price a grid model —
    the brief would be lying about the rates the estimator will see."""
    base = pathlib.Path(base)
    ciegos = load_estimates(base, BLIND)
    informadas = load_estimates(base, INFORMED)
    filas = []
    for celda in grid():
        ciego = _find(ciegos, celda.workload, celda.model)
        informada = _find(informadas, celda.workload, celda.model)
        filas.append(
            {
                "workload": celda.workload,
                "model": celda.model,
                "level": celda.level,
                "blind": None
                if ciego is None
                else {
                    "estimated_pp": ciego["estimated_pp"],
                    "estimated_usd": ciego["estimated_usd"],
                    "timestamp": ciego["timestamp"],
                    "hash": ciego["hash"],
                },
                "informed": None
                if informada is None
                else {
                    "estimated_pp": informada["estimated_pp"],
                    "estimated_usd": informada["estimated_usd"],
                    "timestamp": informada["timestamp"],
                    "hash": informada["hash"],
                },
                # a brief only for what is still pending: an estimated cell has
                # already been walked through
                "brief": fixture_brief(celda, tabla) if ciego is None else None,
            }
        )
    return {
        "kind": "predictability-plan",
        "table_version": tabla.table_version,
        "cells": filas,
        "counts": {
            "blind": len(ciegos),
            "informed": len(informadas),
            "cells": len(grid()),
        },
    }


# ---------------------------------------------------------------------------
# the comparative MAPE report
# ---------------------------------------------------------------------------


def _bootstrap_ci(valores: list[float]) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean, under the study's fixed seed.

    A single observation collapses to itself (there is nothing to resample);
    None with nothing to aggregate.
    """
    if not valores:
        return None
    if len(valores) == 1:
        return valores[0], valores[0]
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(valores)
    medias = []
    for _ in range(BOOTSTRAP_B):
        muestra = [valores[rng.randrange(n)] for _ in range(n)]
        medias.append(sum(muestra) / n)
    medias.sort()
    lo = medias[int(0.025 * BOOTSTRAP_B)]
    hi = medias[int(0.975 * BOOTSTRAP_B)]
    return lo, hi


def _bootstrap_delta_ci(legado: list[float], nuevo: list[float]) -> tuple[float, float] | None:
    """Percentile bootstrap CI of mean(legacy APE) − mean(new APE), PAIRED: the same
    resample of cells feeds both systems, so their correlation survives resampling."""
    if not legado or len(legado) != len(nuevo):
        return None
    if len(legado) == 1:
        d = legado[0] - nuevo[0]
        return d, d
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(legado)
    deltas = []
    for _ in range(BOOTSTRAP_B):
        indices = [rng.randrange(n) for _ in range(n)]
        ml = sum(legado[i] for i in indices) / n
        mn = sum(nuevo[i] for i in indices) / n
        deltas.append(ml - mn)
    deltas.sort()
    lo = deltas[int(0.025 * BOOTSTRAP_B)]
    hi = deltas[int(0.975 * BOOTSTRAP_B)]
    return lo, hi


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


def _cell_real(celda_doc: dict, tabla) -> dict:
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
    reps = celda_doc["reps"]
    dpps = [r["dpp_weekly"] for r in reps if _es_numero(r.get("dpp_weekly"))]
    real_pp = statistics.median(dpps) if dpps else None

    try:
        tarifa = tabla.rate(celda_doc["model"])
    except TableError:
        tarifa = None
    s0s: list[float] = []
    s1s: list[float] = []
    if tarifa is not None:
        s_efectivo = (celda_doc.get("s_effective") or {}).get("s")
        s_valor = s_efectivo if _es_numero(s_efectivo) else 0.0
        for r in reps:
            tin, tout = r.get("tokens_in"), r.get("tokens_out")
            if tin is None or tout is None:
                continue
            s0s.append(new_task_cost(tin, tout, tarifa, s=0.0, per=tabla.per))
            s1s.append(new_task_cost(tin, tout, tarifa, s=s_valor, per=tabla.per))
    real_s0 = statistics.median(s0s) if s0s else None
    real_s1 = statistics.median(s1s) if s1s else None

    if real_pp is None:
        estado = UNMEASURED
    elif real_pp < TICK_PP * (1 - TICK_BAND):
        # "Under a tick" through the residue band: the meter's deltas are
        # tick-quantized, so a real of exactly one tick (which unrounded
        # arithmetic lands a few 1e-14 below or above 0.1) is measured,
        # while anything genuinely under the tick stays excluded.
        estado = SUB_RESOLUTION
    else:
        estado = MEASURED
    return {
        "real_pp": real_pp,
        "real_new_s0_usd_per_run": real_s0,
        "real_new_s1_usd_per_run": real_s1,
        "legacy_status": estado,
    }


def _ape(estimado: float | None, real: float | None) -> float | None:
    """|estimate − real| / real; None whenever either side is missing (never a 0/0)."""
    if real is None or not _es_numero(real) or real <= 0 or not _es_numero(estimado):
        return None
    return abs(estimado - real) / real


def build_report(base, *, tabla, s: float) -> dict:
    """The MAPE report, offline from the raw datasets + the locked estimates.

    `s` prices only the S1 sensitivity of the new side; the verdict's MAPEs are
    native-unit, so the anchor never enters (analyze still receives the inert
    default anchor: its dollar derivatives are not this report's input).
    """
    base = pathlib.Path(base)
    ciegos = load_estimates(base, BLIND)
    informadas = load_estimates(base, INFORMED)
    doc = analyze_mod.build(base, tabla=tabla, ancla=100.0, s=s)
    celdas_analyze = {(c["model"], c["workload"]): c for c in doc["cells"]}

    filas = []
    obsoletas: list[str] = []
    for celda in grid():
        celda_doc = celdas_analyze.get((celda.model, celda.workload))
        real = (
            _cell_real(celda_doc, tabla)
            if celda_doc
            else {
                "real_pp": None,
                "real_new_s0_usd_per_run": None,
                "real_new_s1_usd_per_run": None,
                "legacy_status": UNMEASURED,
            }
        )
        fila = {
            "workload": celda.workload,
            "model": celda.model,
            "level": celda.level,
            "real_pp": real["real_pp"],
            "real_new_s0_usd_per_run": real["real_new_s0_usd_per_run"],
            "real_new_s1_usd_per_run": real["real_new_s1_usd_per_run"],
            "legacy_status": real["legacy_status"],
            "blind": None,
            "informed": None,
        }
        for fase, registros in ((BLIND, ciegos), (INFORMED, informadas)):
            estimacion = _find(registros, celda.workload, celda.model)
            if estimacion is None:
                continue
            # Vintage guard: an estimate locked against one table and a real priced
            # on another do not divide — the repricing itself would become the
            # error. The new-side APEs are set aside (analyze's set-aside
            # precedent), the legacy APE stands (pp is meter-native, the table
            # never touches it), and the mismatch is flagged, never blended.
            coinciden = estimacion["table_version"] == tabla.table_version
            if not coinciden:
                obsoletas.append(
                    f"{celda.key} (estimate locked on {estimacion['table_version']}, "
                    f"report priced on {tabla.table_version})"
                )
            fila[fase] = {
                "estimated_pp": estimacion["estimated_pp"],
                "estimated_usd": estimacion["estimated_usd"],
                "timestamp": estimacion["timestamp"],
                "table_version": estimacion["table_version"],
                "table_vintage_mismatch": not coinciden,
                # the sub-resolution exclusion holds per cell too: a real under
                # a tick carries no legacy APE anywhere in the report
                "ape_legacy": _ape(estimacion["estimated_pp"], real["real_pp"])
                if real["legacy_status"] == MEASURED
                else None,
                "ape_new": _ape(estimacion["estimated_usd"], real["real_new_s0_usd_per_run"])
                if coinciden
                else None,
                "ape_new_s1": _ape(estimacion["estimated_usd"], real["real_new_s1_usd_per_run"])
                if coinciden
                else None,
            }
        filas.append(fila)

    # The per-system aggregates: legacy over the cells whose real resolves above a
    # tick; new over the cells whose extrapolation exists (no resolution floor).
    def _mape(fase: str, campo: str) -> dict | None:
        apes = [
            f[fase][campo]
            for f in filas
            if f[fase] is not None
            and f[fase][campo] is not None
            and (campo != "ape_legacy" or f["legacy_status"] == MEASURED)
        ]
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
        if not any(f[fase] is not None for f in filas):
            continue
        legacy = _mape(fase, "ape_legacy")
        nuevo = _mape(fase, "ape_new")
        s1 = _mape(fase, "ape_new_s1")
        # the paired comparison only where BOTH systems are measurable
        parejask, parejasn = [], []
        for f in filas:
            if (
                f[fase] is not None
                and f["legacy_status"] == MEASURED
                and f[fase]["ape_legacy"] is not None
                and f[fase]["ape_new"] is not None
            ):
                parejask.append(f[fase]["ape_legacy"])
                parejasn.append(f[fase]["ape_new"])
        ci_delta = _bootstrap_delta_ci(parejask, parejasn)
        veredicto, claim = _verdict(ci_delta)
        fases[fase] = {
            "mape_legacy": legacy,
            "mape_new": nuevo,
            "mape_new_s1": s1,
            "paired_cells": len(parejask),
            "delta_mape": statistics.mean(parejask) - statistics.mean(parejasn)
            if parejask
            else None,
            "ci_delta": list(ci_delta) if ci_delta else None,
            "verdict": veredicto,
            "ollama_claim": claim,
        }

    por_workload: dict[str, list[dict]] = {}
    for f in filas:
        por_workload.setdefault(f["workload"], []).append(f)
    desglose = []
    for workload, grupo in sorted(por_workload.items()):
        entrada = {"workload": workload, "level": grupo[0]["level"], "cells": []}
        for fase in PHASES:
            legado = [
                c[fase]["ape_legacy"]
                for c in grupo
                if c[fase] is not None
                and c[fase]["ape_legacy"] is not None
                and c["legacy_status"] == MEASURED
            ]
            nuevo = [
                c[fase]["ape_new"]
                for c in grupo
                if c[fase] is not None and c[fase]["ape_new"] is not None
            ]
            entrada[fase] = {
                "mape_legacy": statistics.mean(legado) if legado else None,
                "mape_new": statistics.mean(nuevo) if nuevo else None,
            }
        desglose.append(entrada)

    hallazgos = {
        "sub_resolution_legacy": [
            f"{f['workload']}/{f['model']} (real {f['real_pp']:g} pp, under the {TICK_PP:g} pp tick)"
            for f in filas
            if f["legacy_status"] == SUB_RESOLUTION
        ],
        "unmeasured": [
            f"{f['workload']}/{f['model']}" for f in filas if f["legacy_status"] == UNMEASURED
        ],
        "stale_table_estimates": obsoletas,
        "pending_blind": [f"{f['workload']}/{f['model']}" for f in filas if f["blind"] is None],
        "pending_informed": [
            f"{f['workload']}/{f['model']}"
            for f in filas
            if f["blind"] is not None and f["informed"] is None
        ],
    }
    return {
        "kind": "predictability-report",
        "generated_at": time.time(),
        "protocol_version": PROTOCOL_VERSION,
        "table_version": tabla.table_version,
        "params": {
            "s": s,
            "tick_pp": TICK_PP,
            "bootstrap_samples": BOOTSTRAP_B,
            "bootstrap_seed": BOOTSTRAP_SEED,
        },
        "estimates": {"blind": len(ciegos), "informed": len(informadas)},
        "cells": filas,
        "workloads": desglose,
        "aggregate": fases,
        "findings": hallazgos,
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
            "APE stands, pp being meter-native."
        ),
    }
