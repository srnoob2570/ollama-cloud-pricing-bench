# ollama-cloud-pricing-bench

Metodología de benchmarks para medir el **costo efectivo** de ejecutar cargas de trabajo LLM
en [Ollama Cloud](https://ollama.com) durante su transición del sistema de facturación legado por GPU-time al nuevo
por tokens (anunciado el 2026-08-31 en
[`ollama.com/blog/transparent-pricing`](https://ollama.com/blog/transparent-pricing)).

> 🇬🇧 **Este repositorio se escribe en inglés.** Esta página es un espejo en español;
> la fuente de verdad es [README.md](./README.md).

El objetivo es responder con datos, no comparar precios nominales: ¿el pricing por tokens
es más económico y para qué workloads? ¿se sostiene el argumento de que "GPU-time es difícil
de predecir"? ¿qué incentivos tiene Ollama y a quién beneficia el cambio?

> **Estado: metodología v1 consolidada (2026-09-01); harness completo (tickets #17-26).** El
> mapa wayfinder cerró con las 14 decisiones registradas, y el harness que la ejecuta está
> construido: la CLI `bench` con su compuerta de gasto (dry-run antes de toda ejecución
> real, una marca por corrida), los runners T1/T2/T3 con lotes bracketeados, preflight de
> catálogo y checkers reales, el workstream de concurrencia (`bench probe-concurrency` mide
> el corte real por key; las celdas k∈{1,4,8} se re-anclan a él), la calibración de caché
> (`bench calibrate-cache` reemplaza el supuesto S1=50% por el hit-rate medido cuando es
> concluyente), el flujo de predictibilidad (`bench predict`: las 14 celdas del conjunto
> medible estimadas a ciegas y
> bloqueadas con hash antes de correr, con el reporte MAPE comparativo), `bench analyze`
> (el paquete break-even completo recalculado solo desde el raw, así que un cambio de precios
> re-corre el veredicto con cuota cero) y la sincronización de datasets a releases de
> GitHub (`bench release --run <id>` empareja raw ↔ código ↔ tabla; `bench analyze --release
> <tag>` consume una). Los benchmarks en sí aún no se han ejecutado. Esa es la fase de
> ejecución, gobernada por la compuerta (metodología §11).

> **Transparencia:** proyecto generado y mantenido con asistencia de IA
> ([Claude Code](https://claude.com/claude-code)). No está afiliado ni avalado por Ollama.

## Decisiones de diseño ya fijadas

- **Doble rama de facturación**: medición viva bajo el plan legado GPU-time (la única
  cuenta disponible; congelada, sin migrar) + extrapolación al plan nuevo con la tabla
  oficial de tokens, hasta disponer de una key del plan nuevo.
- **Unidad de cuenta**: cuota % del plan legado + ancla a dólares (precio mensual ÷ cuota);
  puente a $ por token para la misma tarea, con y sin cache estándar.
- **Catálogo completo** (19 modelos) con densidad escalonada: T1 micro en todos, suites
  estructurales T2 en ~6, workloads agénticos T3 en 2–3.
- **Calidad**: éxito binario verificable (tests/compilación/checkers). Sin LLM-judge en v1.
- **Concurrencia** como workstream de primera clase; harness agéntico propio y determinista.

## Uso: la CLI `bench`

El harness es un único binario (`src/ocharness/`, Python >= 3.12, httpx;
dependencias mínimas, sin TUI ni base de datos):

```bash
uv sync          # o: pip install -e .
bench --help     # cada subcomando acepta --base DIR (por defecto .) y --json
```

Todo opera sobre un directorio de trabajo (`--base`, por defecto `.`) que contiene
`pricing/` (las tablas de precios versionadas), `runs/` + `batches/` (el dataset crudo
inmutable) y, conforme avanza el trabajo, manifiestos de corrida, `analysis/` (derivadas,
dashboard), `predictability/` (estimaciones bloqueadas) y `releases/`.

### La compuerta de gasto (la aplica la herramienta, no la memoria)

1. `bench dry-run --level T1` estima el coste del nivel (en `$` de tokens bajo S0/S1 y los
   pp esperados) sin tocar la API, y escribe una marca de compuerta ligada al
   `--table-version` y al `--reps` aprobados.
2. `bench run --level T1` se niega a arrancar si la marca no existe o no coincide con la
   tabla viva y con la densidad de esta corrida; luego verifica el catálogo vivo
   (`/v1/models`) contra el slate y consume la marca: un dry-run habilita exactamente
   una corrida.
3. Un fallo a mitad de corrida nunca corrompe ni duplica facturación: `batch_id` es
   determinista a partir de (run, level, workload, model, rep, k), y los lotes aborted /
   in-flight se saltan con aviso al reanudar, nunca se reintentan en silencio.

### Los subcomandos

| Subcomando | Qué hace |
|---|---|
| `dry-run --level T1\|T2\|T3 [--reps N] [--s S]` | La estimación gratuita que abre la compuerta (coste en tokens S0/S1 + pp esperado). Cero requests. |
| `run --level T1\|T2\|T3 [--model X] [--rep N] [--reps N] [--k K]` | Ejecuta el nivel con **lotes bracketeados** (protocolo v3): lectura del medidor → andanada (sin warmup, sin reintento; cada request medida lleva el nonce sembrado de la corrida, el carril sin cache) → verificación `request_count` por modelo ≤ 2 s → settle por registro (poll de /api/usage hasta dos lecturas consecutivas iguales en ambas ventanas; poll 5 s, tope 60 s) → lectura final; cada request queda como una línea JSONL inmutable. |
| `resume --level ...` | El gemelo reanudable de `run`: continúa una corrida interrumpida desde su manifiesto sin duplicar ningún lote. Pasa la compuerta igual que `run`, una dry-run fresca (gratis) que apruebe la misma densidad, y en un nivel sin manifiesto es simplemente una corrida nueva. |
| `probe-concurrency --model X [--k-max K]` | Lanza andanadas cortas con k creciente para medir el corte real por key (429/encolamiento) y luego ejecuta las celdas k∈{1,4,8} re-ancladas a él (un k planificado por encima del corte corre EN el corte, documentado en el dataset). |
| `calibrate-cache [--model X] [--spaced-gaps 5 30 90]` | Re-repite un prefijo fijo de ~20K por modelo del slate T2 (referencia fría, r=4 intra-lote, re-envíos espaciados); cuando es concluyente, el hit-rate medido reemplaza el supuesto S1 por modelo, con S0 como piso. |
| `predict [--phase blind\|informed ...] [--report]` | El flujo de predictibilidad (§8): las 14 celdas del conjunto medible (las cuatro fuertes de T2 + T3, re-alcance v1.1) estimadas a ciegas (solo la descripción pública del fixture + las tarifas), bloqueadas con timestamp y hash antes de que la celda corra; re-estimación informada después; `--report` emite el MAPE comparativo (pp legado vs $ nuevo, bootstrap CI), anclado al par S0/S1 persistido, excluyendo las celdas sub-resolución y marcándolas. Cuota cero. |
| `analyze [--ancla P] [--s S] [--table-version V] [--level L] [--model M]` | **El re-correr sin re-medir**: todas las derivadas (medianas con p25–p75/p95 por modelo×workload, costes por tarea S0/S1, umbral crítico pp/1M, quién-gana-por-perfil, la curva Δpp-tokens, los 4 barridos de sensibilidad) y el dashboard estático (gráficos SVG con tokens de tema, veredicto primero, deslizador de cache), regenerados solo desde el raw, sin red. |
| `analyze --release <tag> [--repo owner/name]` | El mismo análisis sobre una release de dataset descargada, verificada contra su mapa sha256 del metadata y valorada con la tabla de la propia release. |
| `status [--level L]` | Lotes pending/done/aborted/in-flight y la cuota consumida por nivel, leídos solo de los manifiestos. |
| `release --run <run_id> [--repo owner/name]` | Empaqueta el dataset de una corrida: requests + batches + el manifiesto que los liga + la instantánea de la tabla de precios + un `metadata.json` (commit del código, versión de tabla + sha256, versión de protocolo, un mapa sha256 de cada archivo), y lo publica como release de GitHub. **Una release por corrida, nunca reescrita**; la key viva de la API (y cualquier cadena con forma de bearer token) no puede aparecer en ningún byte empaquetado o la release se niega. |

### Re-correr cuando cambian los precios (cuota cero)

1. Guarda la tabla oficial nueva como `pricing/<nueva-version>.json` (input / cached input /
   output por 1M de tokens, por modelo).
2. `bench analyze --table-version <nueva-version> --ancla 100 --s 0.5` re-deriva el paquete
   completo desde el raw inmutable. Nada se re-mide, no se envía ningún request.
3. Solo `dry-run` → `run` gasta; el análisis nunca lo hace.

Los datasets se sincronizan a releases de GitHub, una por corrida: `bench release --run
<run_id>` empareja raw ↔ código (el commit que lo produjo) ↔ tabla (instantánea + sha256), y
`bench analyze --release <tag>` consume una release sin ninguna otra entrada.

### Guardarraíles

- La key de API se lee solo del entorno (`OLLAMA_API_KEY`) y nunca se escribe en ningún
  dataset ni release.
- Ninguna corrida real sin su marca de dry-run; el JSONL crudo es inmutable; las derivadas
  se regeneran.
- La cuenta legado está congelada: no migrarla jamás.

## Contenido

| Ruta | Contenido |
|---|---|
| [`docs/methodology-v1.md`](./docs/methodology-v1.md) | **La metodología v1 consolidada** (entregable del mapa; en inglés) |
| [`CONTEXT.md`](./CONTEXT.md) | Glosario del dominio (inglés, con términos en español como referencias) |
| `docs/research/` | Línea base de pricing, verificación del medidor, comparables de mercado |
| `pricing/2026-08-31.json` | Tabla oficial versionada (entrada del harness) |
| `src/ocharness/` + `tests/` | CLI `bench`: compuerta de gasto, dry-run, fake de ollama.com |
| `runs/`, `batches/`, `analysis/`, `releases/` | Datos de trabajo del harness: JSONL crudo, derivadas, releases de dataset descargadas |
