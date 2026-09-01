"""Workloads y slates por nivel (decisiones de los tickets de diseño)."""

from __future__ import annotations

from dataclasses import dataclass

from .pricing import TablaPrecios


@dataclass(frozen=True)
class Workload:
    nombre: str
    requests: int  # requests por repetición
    t_in: int      # tokens de entrada por request
    t_out: int     # tokens de salida por request


T1: tuple[Workload, ...] = (
    Workload("qa_corto", 20, 80, 80),
    Workload("calibracion", 3, 200, 150),
    Workload("throughput", 1, 150, 400),
)
T2: tuple[Workload, ...] = (
    Workload("contexto_largo", 1, 30_000, 300),
    Workload("generaciones_largas", 1, 1_200, 5_000),
    Workload("multi_turno", 8, 700, 200),
    Workload("tool_calling", 6, 670, 200),
    Workload("reasoning", 1, 1_500, 4_500),
    Workload("ratio_in", 1, 50_000, 120),
    Workload("ratio_out", 20, 200, 500),
)
T3: tuple[Workload, ...] = (
    Workload("multiarchivo", 1, 150_000, 30_000),
    Workload("debugging", 1, 50_000, 15_000),
    Workload("refactor", 1, 60_000, 12_500),
)

WORKLOADS_POR_NIVEL: dict[str, tuple[Workload, ...]] = {"T1": T1, "T2": T2, "T3": T3}

SLATE_T2 = ("glm-5.3-flash", "gpt-oss:20b", "deepseek-v4-flash", "minimax-m3", "glm-5.3",
            "kimi-k3")
SLATE_T3 = ("kimi-k2.7-code", "glm-5.3-flash", "deepseek-v4-pro")


def slate(nivel: str, tabla: TablaPrecios) -> list[str]:
    """Modelos que llevan el nivel: T1 = todos los de la tabla; T2/T3 = slates fijos."""
    if nivel == "T1":
        return sorted(tabla.models)
    if nivel == "T2":
        return list(SLATE_T2)
    if nivel == "T3":
        return list(SLATE_T3)
    raise ValueError(f"nivel desconocido: {nivel!r}")