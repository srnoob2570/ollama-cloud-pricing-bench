# ollama-cloud-pricing-bench

Metodología de benchmarks para medir el **costo efectivo** de ejecutar cargas de trabajo LLM
en [Ollama Cloud](https://ollama.com) durante su transición del sistema de facturación
**legado por GPU-time** al nuevo **por tokens** (anunciado el 2026-08-31 en
[`ollama.com/blog/transparent-pricing`](https://ollama.com/blog/transparent-pricing)).

> 🇬🇧 **Este repositorio se escribe en inglés.** Esta página es un espejo en español;
> la fuente de verdad es [README.md](./README.md).

El objetivo no es comparar precios nominales, sino responder con datos: ¿el pricing por tokens
es más económico y para qué workloads? ¿se sostiene el argumento de que "GPU-time es difícil
de predecir"? ¿qué incentivos tiene Ollama y a quién beneficia el cambio?

> **Estado: metodología v1 consolidada (2026-09-01)** — el mapa wayfinder cerró con las 14
> decisiones registradas; los benchmarks aún no se han ejecutado (fase posterior, compuerta de
> gasto en [`docs/methodology-v1.md`](./docs/methodology-v1.md) §11). El harness se está
> implementando por tickets (Harness 01-08 cerrados: andamio, runner T1 con lotes
> bracketeados, preflight de catálogo + checkers T1 reales + `status`, fixtures T2 + las
> 7 suites estructurales con sus checkers reales, los mini-repos sintéticos T3 con el
> bucle de agente determinista y los checkers pytest en sandbox, el workstream de
> concurrencia: `bench probe-concurrency` barre k con andanadas cortas para medir el
> corte real por key y luego ejecuta las celdas k∈{1,4,8} sobre el ancla T1 con el mismo
> total de tokens por celda — una celda cuyo k planificado supera el corte medido se
> re-ancla a él, documentado en el dataset — la calibración de caché:
> `bench calibrate-cache` re-repite un prefijo fijo de ~20K por modelo del slate T2
> (referencia fría, r=4 intra-lote, re-envíos espaciados) y su hit-rate medido
> reemplaza el supuesto S1 cuando es concluyente, con S0 como piso — y `bench analyze`:
> el paquete break-even completo (derivadas con mediana/IQR/p95 por modelo×workload,
> costes por tarea S0/S1 con el ancla, barras de umbral crítico pp/1M vs medido,
> curvas Δpp-tokens, quién-gana-por-perfil, PNGs y un dashboard HTML estático y
> autocontenido) recalculado solo desde el raw — un cambio de precios re-correre el
> veredicto con cuota cero, más los 4 barridos de sensibilidad fijos (tarifas ±20 %,
> caché {0,25,50,90} %, P_LEGADO ±30 %, el eje k)).

> **Transparencia:** proyecto generado y mantenido con asistencia de IA
> ([Claude Code](https://claude.com/claude-code)). No está afiliado ni avalado por Ollama.

## Decisiones de diseño ya fijadas

- **Doble rama de facturación**: medición viva bajo el **plan legado** GPU-time (la única
  cuenta disponible; congelada, sin migrar) + **extrapolación** al plan nuevo con la tabla
  oficial de tokens, hasta disponer de una key del plan nuevo.
- **Unidad de cuenta**: cuota % del plan legado + ancla a dólares (precio mensual ÷ cuota);
  puente a $ por token para la misma tarea, con y sin cache estándar.
- **Catálogo completo** (19 modelos) con densidad escalonada: T1 micro en todos, suites
  estructurales T2 en ~6, workloads agénticos T3 en 2–3.
- **Calidad**: éxito binario verificable (tests/compilación/checkers). Sin LLM-judge en v1.
- **Concurrencia** como workstream de primera clase; harness agéntico propio y determinista.

## Contenido

| Ruta | Contenido |
|---|---|
| [`docs/methodology-v1.md`](./docs/methodology-v1.md) | **La metodología v1 consolidada** (entregable del mapa; en inglés) |
| [`CONTEXT.md`](./CONTEXT.md) | Glosario del dominio (inglés, con términos en español como referencias) |
| `docs/research/` | Línea base de pricing, verificación del medidor, comparables de mercado |
| `pricing/2026-08-31.json` | Tabla oficial versionada (entrada del harness) |
| `src/ocharness/` + `tests/` | CLI `bench`: compuerta de gasto, dry-run, fake de ollama.com |
