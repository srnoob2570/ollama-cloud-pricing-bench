"""Workloads and slates per level (decisions from the design tickets)."""

from __future__ import annotations

from dataclasses import dataclass

from .pricing import PriceTable


@dataclass(frozen=True)
class Workload:
    name: str
    requests: int  # requests per repetition
    t_in: int  # input tokens per request
    t_out: int  # output tokens per request


T1: tuple[Workload, ...] = (
    Workload("qa_short", 20, 80, 80),
    Workload("calibration", 3, 200, 150),
    Workload("throughput", 1, 150, 400),
)
T2: tuple[Workload, ...] = (
    Workload("long_context", 1, 30_000, 300),
    Workload("long_generation", 1, 1_200, 5_000),
    Workload("multi_turn", 8, 700, 200),
    Workload("tool_calling", 6, 670, 200),
    Workload("reasoning", 1, 1_500, 4_500),
    Workload("ratio_in", 1, 50_000, 120),
    Workload("ratio_out", 20, 200, 500),
)
T3: tuple[Workload, ...] = (
    Workload("multi_file", 1, 150_000, 30_000),
    Workload("debugging", 1, 50_000, 15_000),
    Workload("refactoring", 1, 60_000, 12_500),
)

WORKLOADS_BY_LEVEL: dict[str, tuple[Workload, ...]] = {"T1": T1, "T2": T2, "T3": T3}

# The hybrid split (methodology v1.1 §5): the strong four move the meter enough
# to be measured per cell (3.8–6.5 weekly ticks each), so they run per-cell with
# the legacy measured; the weak trio reads at or under the tick per cell and
# pools per model instead — one bracket per model covering the trio's workloads
# and repetitions, its legacy attributed per workload post-hoc by token share.
STRONG_T2 = frozenset({"long_context", "long_generation", "ratio_in", "ratio_out"})
WEAK_T2 = frozenset({"multi_turn", "tool_calling", "reasoning"})

SLATE_T2 = ("glm-5.3-flash", "gpt-oss:20b", "deepseek-v4-flash", "minimax-m3", "glm-5.3", "kimi-k3")
SLATE_T3 = ("kimi-k2.7-code", "glm-5.3-flash", "deepseek-v4-pro")


def slate(level: str, tabla: PriceTable) -> list[str]:
    """Models carrying the level: T1 = every model in the table; T2/T3 = fixed slates."""
    if level == "T1":
        return sorted(tabla.models)
    if level == "T2":
        return list(SLATE_T2)
    if level == "T3":
        return list(SLATE_T3)
    raise ValueError(f"unknown level: {level!r}")
