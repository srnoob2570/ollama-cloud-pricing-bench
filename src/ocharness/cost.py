"""Budget estimate (dry-run): tokens × rates under the S0 and S1 scenarios.

Without touching the API: this is the gate's input - the number the operator approves
before spending quota. The estimate includes the cache-free lane's nonce overhead
(protocol v3): every measured request carries its run-scoped salt, priced here at
the lane's clamped word count x its tokenization allowance.
"""

from __future__ import annotations

import dataclasses

from . import lane, workloads
from .pricing import Rate


def new_task_cost(t_in: float, t_out: float, tarifa: Rate, *, s: float, per: int) -> float:
    """The new-plan cost of one task under hit-rate `s` (S0 == s=0).

    The cost model's single pricing formula, shared by the gate's budget and
    by analyze's per-cell extrapolation: the table's declared `per` unit is
    honored (1M is not assumed), and a model without a cache discount
    (cached_input == input) is identical under every hit rate.
    """
    if tarifa.has_cache_discount:
        return (
            t_in * (1 - s) * tarifa.input + t_in * s * tarifa.cached_input + t_out * tarifa.output
        ) / per
    return (t_in * tarifa.input + t_out * tarifa.output) / per


@dataclasses.dataclass
class BudgetLine:
    workload: str
    level: str
    models: int
    reps: int  # repetitions per cell (the cost model's n)
    requests: int  # total requests of the level (models × reps × requests/workload)
    tokens_in: int  # includes the lane's nonce overhead (what will actually be sent)
    tokens_out: int
    nonce_tokens: int  # the lane's overhead inside tokens_in (transparent, not extra work)
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
        nonce_total = 0
        s0_total = 0.0
        s1_total = 0.0
        # The cache-free lane's per-request overhead (protocol v3): the same
        # nonce size for every model of the workload (it keys on the workload's
        # expected input, not on the model), so it adds once per request.
        nonce_por_request = lane.nonce_tokens_estimate(w.t_in)
        for modelo in modelos:
            tarifa = tabla.rate(modelo)
            t_in = (w.t_in + nonce_por_request) * w.requests * reps
            t_out = w.t_out * w.requests * reps
            t_in_total += t_in
            t_out_total += t_out
            nonce_total += nonce_por_request * w.requests * reps
            s0 = new_task_cost(t_in, t_out, tarifa, s=0.0, per=tabla.per)
            s0_total += s0
            # no cache discount: new_task_cost already makes S1 equal S0
            s1_total += new_task_cost(t_in, t_out, tarifa, s=s, per=tabla.per)
        filas.append(
            BudgetLine(
                workload=w.name,
                level=level,
                models=len(modelos),
                reps=reps,
                requests=len(modelos) * reps * w.requests,
                tokens_in=t_in_total,
                tokens_out=t_out_total,
                nonce_tokens=nonce_total,
                cost_s0=s0_total,
                cost_s1=s1_total,
                pp_expected=None,
            )
        )
    return filas


def canary_estimate() -> dict:
    """The billing canary's once-per-run spend estimate (protocol v3): 5 salted +
    5 identical-prefix replays of one T2-size body. The run always bills it
    before the first bracket, so the gate's estimate carries it — un-budgeted
    spend is how guardrails die quietly."""
    carga = next(w for w in workloads.T2 if w.name == "long_context")
    nonce_por_request = lane.nonce_tokens_estimate(carga.t_in)
    requests = 5 + 5
    return {
        "requests": requests,
        "tokens_estimate": requests * (carga.t_in + nonce_por_request + carga.t_out),
        "note": (
            "once per run, before the first bracket: 5 salted + 5 identical-prefix "
            "replays of the T2 long_context body; the replay must bill at the cache "
            "discount, a ratio above 0.5 aborts the run"
        ),
    }
