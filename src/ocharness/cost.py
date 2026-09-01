"""Budget estimate (dry-run): tokens × rates under the S0 and S1 scenarios.

Without touching the API: this is the gate's input - the number the operator approves
before spending quota.
"""

from __future__ import annotations

import dataclasses

from . import workloads


@dataclasses.dataclass
class BudgetLine:
    workload: str
    level: str
    models: int
    reps: int  # repetitions per cell (the cost model's n)
    requests: int  # total requests of the level (models × reps × requests/workload)
    tokens_in: int
    tokens_out: int
    cost_s0: float  # USD at a 0 % hit-rate
    cost_s1: float  # USD at hit-rate `s`; identical to S0 for undiscounted models
    pp_expected: float | None  # requires pp/1M calibration; None = unmeasured


def budget(level: str, tabla, *, reps: int = 5, s: float = 0.5) -> list[BudgetLine]:
    """Estimates the cost of a full level, per workload, under the S0 and S1 scenarios.

    `s` is the S1 cache hit-rate assumption. Models without a cache discount in the
    table (cached_input == input) are identical under both scenarios. The table's
    declared `per` unit is honored (1M is not assumed).
    """
    if level not in workloads.WORKLOADS_BY_LEVEL:
        raise ValueError(f"unknown level: {level!r}")
    modelos = workloads.slate(level, tabla)
    filas: list[BudgetLine] = []
    for w in workloads.WORKLOADS_BY_LEVEL[level]:
        t_in_total = 0
        t_out_total = 0
        s0_total = 0.0
        s1_total = 0.0
        for modelo in modelos:
            tarifa = tabla.rate(modelo)
            t_in = w.t_in * w.requests * reps
            t_out = w.t_out * w.requests * reps
            t_in_total += t_in
            t_out_total += t_out
            s0 = (t_in * tarifa.input + t_out * tarifa.output) / tabla.per
            s0_total += s0
            if tarifa.has_cache_discount:
                s1_total += (
                    t_in * (1 - s) * tarifa.input
                    + t_in * s * tarifa.cached_input
                    + t_out * tarifa.output
                ) / tabla.per
            else:
                s1_total += s0  # no cache discount: S1 equals S0 for this model
        filas.append(
            BudgetLine(
                workload=w.name,
                level=level,
                models=len(modelos),
                reps=reps,
                requests=len(modelos) * reps * w.requests,
                tokens_in=t_in_total,
                tokens_out=t_out_total,
                cost_s0=round(s0_total, 4),
                cost_s1=round(s1_total, 4),
                pp_expected=None,
            )
        )
    return filas
