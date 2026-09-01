# ollama-cloud-pricing-bench

Metodología de benchmarks para medir el **costo efectivo** de ejecutar cargas de trabajo LLM
en [Ollama Cloud](https://ollama.com) durante su transición del sistema de facturación
**legado por GPU-time** al nuevo **por tokens** (anunciado el 2026-08-31 en
[`ollama.com/blog/transparent-pricing`](https://ollama.com/blog/transparent-pricing)).

El objetivo no es comparar precios nominales, sino responder con datos:

- ¿El pricing por tokens es más económico para el usuario? ¿Para qué workloads y para cuáles es más caro?
- ¿Se sostiene el argumento de que "GPU-time es difícil de predecir"?
- ¿Qué incentivos económicos tiene Ollama para el cambio, y el cambio beneficia a quién?

> **Estado: metodología v1 consolidada (2026-09-01)** — el mapa wayfinder cerró con las 14
> decisiones registradas; los benchmarks aún no se han ejecutado (fase posterior, compuerta de
> gasto en [`docs/metodologia-v1.md`](./docs/metodologia-v1.md) §11).

> **Transparencia:** este proyecto se genera y mantiene con asistencia de IA
> ([Claude Code](https://claude.com/claude-code)). No está afiliado ni avalado por Ollama.
> Revisa el código y los datos antes de confiar en ellos.

## Decisiones de diseño ya fijadas

- **Doble rama de facturación**: medición viva bajo el **plan legado** GPU-time (la única
  cuenta disponible; congelada, sin migrar) + **extrapolación** al plan nuevo con la tabla
  oficial de tokens, hasta disponer de una key del plan nuevo.
- **Unidad de cuenta**: cuota % del plan legado + ancla a dólares (precio mensual ÷ cuota);
  puente a $ por token para la misma tarea, con y sin cache estándar.
- **Catálogo completo** (19 modelos de la tabla oficial) con densidad escalonada: micro T1
  en todos, suites estructurales T2 en ~6, workloads agénticos T3 en 2–3.
- **Calidad**: éxito binario verificable (tests/compilación/checkers). Sin LLM-judge en v1.
- **Concurrencia** como workstream de primera clase; harness agéntico propio y determinista.

## Archivos

| Ruta | Contenido |
|---|---|
| [`docs/metodologia-v1.md`](./docs/metodologia-v1.md) | **La metodología v1 consolidada** (el entregable del mapa) |
| [`CONTEXT.md`](./CONTEXT.md) | Glosario del dominio (GPU-time, cuota, ancla, escenario de cache, umbral crítico…) |
| `docs/research/base-pricing-2026-08-31.md` | Línea base verificable de ambos sistemas de facturación |
| `docs/research/medidor-vivo-2026-08-31.md` | Verificación en vivo del medidor (API key, lag, cuantización) |
| `docs/research/medidor-uso-ollama.md` | Research documental del medidor |
| `docs/research/comparables-open-weights.md` | Comparables de precios por familia open-weights |
| `docs/research/logs/` | Logs crudos de la verificación del medidor |

## Repos hermanos de este workspace

- [`ollama-usage-breakdown`](https://github.com/srnoob2570/ollama-usage-breakdown) —
  userscript que lee los medidores de `ollama.com/settings` (fuente candidata del delta de cuota).
- [`OMeter`](https://github.com/srnoob2570/OMeter) — benchmarks TTFT/TPS para endpoints de Ollama
  (a reutilizar donde aplique).
- [`opencode-ollama-cloud`](https://github.com/srnoob2570/opencode-ollama-cloud) — catálogo vivo
  de `ollama.com/v1/models` (fuente de la lista de modelos).