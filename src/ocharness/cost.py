"""Presupuesto estimado (dry-run): tokens × tarifas en los escenarios S0 y S1.

Sin tocar la API: es la entrada de la compuerta — el número que el operador aprueba
antes de gastar cuota.
"""

from __future__ import annotations

import dataclasses

from . import workloads
from .pricing import TablaPrecios


@dataclasses.dataclass
class LineaPresupuesto:
    workload: str
    nivel: str
    modelos: int
    reps: int                  # repeticiones por celda (n del modelo de costo)
    requests: int              # requests totales del nivel (modelos × reps × requests/workload)
    tokens_in: int
    tokens_out: int
    costo_s0: float            # USD, hit-rate 0 %
    costo_s1: float            # USD con hit-rate `s`; idéntico a S0 en modelos sin descuento
    pp_esperado: float | None  # requiere calibración pp/1M; None = s/calibración


def presupuesto(nivel: str, tabla: TablaPrecios, *, reps: int = 5, s: float = 0.5) -> list[LineaPresupuesto]:
    """Estima el costo de un nivel completo, por workload, en los escenarios S0 y S1.

    `s` es el hit-rate de cache asumido del escenario S1. Los modelos sin descuento de
    cache en la tabla (cached_input == input) quedan idénticos en ambos escenarios.
    La unidad `per` declarada por la tabla se respeta (no se asume 1M fijo).
    """
    if nivel not in workloads.WORKLOADS_POR_NIVEL:
        raise ValueError(f"nivel desconocido: {nivel!r}")
    modelos = workloads.slate(nivel, tabla)
    filas: list[LineaPresupuesto] = []
    for w in workloads.WORKLOADS_POR_NIVEL[nivel]:
        t_in_total = 0
        t_out_total = 0
        s0_total = 0.0
        s1_total = 0.0
        for modelo in modelos:
            tarifa = tabla.tarifa(modelo)
            t_in = w.t_in * w.requests * reps
            t_out = w.t_out * w.requests * reps
            t_in_total += t_in
            t_out_total += t_out
            s0 = (t_in * tarifa.input + t_out * tarifa.output) / tabla.per
            s0_total += s0
            if tarifa.tiene_descuento_cache:
                s1_total += (t_in * (1 - s) * tarifa.input + t_in * s * tarifa.cached_input
                             + t_out * tarifa.output) / tabla.per
            else:
                s1_total += s0  # sin descuento de cache: S1 ≡ S0 para este modelo
        filas.append(LineaPresupuesto(
            workload=w.nombre, nivel=nivel, modelos=len(modelos), reps=reps,
            requests=len(modelos) * reps * w.requests,
            tokens_in=t_in_total, tokens_out=t_out_total,
            costo_s0=round(s0_total, 4), costo_s1=round(s1_total, 4), pp_esperado=None))
    return filas