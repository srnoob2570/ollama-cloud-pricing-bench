# Metodología v1 — Costo efectivo de Ollama Cloud: legado GPU-time vs nuevo token-based

**Versión 1.0 · 2026-09-01 · estado: especificada, lista para ejecución** (fase posterior a este
mapa wayfinder). Este documento integra las decisiones de todos los tickets cerrados del
[mapa](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/1). Una sesión futura debe
poder ejecutar los benchmarks leyendo esto sin decidir nada de diseño. Glosario: [`CONTEXT.md`](../../CONTEXT.md).

**Guardarraíles (rígidos)**: 🚫 no migrar la cuenta Max legada (única key GPU-time viva; migración
voluntaria e irreversible) · 💰 gasto pre-aprobado solo para la verificación del medidor (ya
ejecutada); toda corrida real pasa por la compuerta (§11).

---

## 1. Preguntas del brief → dónde se responden

| # | Pregunta del brief original | Estado | Resolución |
|---|---|---|---|
| 1 | Línea base de ambos sistemas | ✅ cerrada | «Línea base de pricing» + «Verificación en vivo» (docs/research/) |
| 2 | Benchmarks representativos | ✅ cerrada | «Workloads y checkers (T1/T2/T3)» — 13 workloads, 3 niveles |
| 3 | Variables a medir | ✅ cerrada | «Protocolo de medición y schema del dataset» (schema por request/lote) |
| 4 | Normalización | ✅ cerrada | «Modelo de costo» (costo/tarea, $/1M, pp/1M, ancla) |
| 5 | Quién gana/pierde | ✅ diseñada | Break-even (perfiles de usuario) — se ejecuta con datos |
| 6 | La afirmación de Ollama | ✅ diseñado | «Experimento de predictibilidad» (MAPE comparativo) + cita literal en línea base |
| 7 | Incentivos económicos | ✅ matriz | «Checklist de incentivos» + «Comparables de precios externos» |
| 8 | Pruebas reproducibles | ✅ cerrada | «Especificación del harness» + re-correr sin re-medir |
| 9 | Puntos de equilibrio | ✅ cerrada | Umbral crítico pp/1M + 4 sensibilidades |
| 10 | Conclusiones con datos | 🔜 fase posterior | estructura del informe definida en §12 |

## 2. Los dos sistemas de facturación (línea base 2026-08-31)

- **Legado (GPU-time)**: cuota en % por ventana — sesión 5 h y semanal 7 días (`limits.*.usage`
  de `GET /api/usage` con API key Bearer), fracción con tick 0.001 (0.1 pp), desglose
  `request_count` por modelo instantáneo y exacto. Sin tarifa pública ni mapeo a GPU-segundos.
- **Nuevo**: créditos en $ por plan (Free starter · Pro $20→$60/mes · Max $100→$300/mes ·
  Team $500→$1000) consumidos **por tokens** a tabla oficial input/cached/output × 19 modelos.
  Migración voluntaria e irreversible; nuevos signups ya entran al nuevo.
- Comparables de mercado: tabla completa en `docs/research/comparables-open-weights.md`
  (passthrough casi literal de tarifas upstream; margen en el ratio de créditos; GPU-time ya no
  existe en ninguna API compartida del mercado).

## 3. Unidades y modelo de costo

| Concepto | Definición |
|---|---|
| **Unidad legado** | pp (punto de cuota) de la ventana **semanal** (7 días), tick 0.001 |
| **Unidad nuevo** | $ por tokens (input/cached/output × tabla versionada del modelo) |
| **Ancla** | `P_LEGADO=$100/mes` → **$0.2302 por pp semanal** (÷4.345 ÷100); tick ≈ $0.0230 |
| **Restricciones aparte** | sesión 5 h y ventana rodante de 4 semanas (`activity`) = saturación, no ancla |
| **Extrapolación** | `t_in×r_in + t_cached×r_cached + t_out×r_out` en **S0=0 % y S1=50 %** (versionado); S1≡S0 en los 5 modelos con cached=input |
| **Unidad útil** | primaria = costo por **tarea completada** (checker pasa); intentada y $/1M como secundarias |
| **Incertidumbre** | n=5, mediana, p25–p75+p95; diferencia real = no-solape IQR **y** >2 ticks (≈$0.046) o >5 % en $; lotes ≥30× quantum |

Cache: el lado legado **mide** el cache real (horneado en Δpp); los escenarios S0/S1 aplican solo
al lado nuevo; la calibración reemplaza S1 por el hit-rate medido cuando es concluyente.

## 4. Medición (protocolo confirmado)

- **Lote bracketeado** (nunca por-request): lectura de `/api/usage` (Bearer) → N requests → settle
  ≥90 s → lectura; Δpp con error ±0.001; `request_count` por modelo verificado ≤2 s (contador
  instantáneo y exacto; % de cuota lagea ~60–90 s).
- **Crudo inmutable**: `runs/*.jsonl` por request (`k`, seed, tokens, TTFT del streaming, done
  verbatim, checker) + `batches/*.jsonl` por lote (medidor raw pre/post, Δpp por ventana).
- **Streaming-first** (único modo con latencia real en cloud), round-robin entre modelos, sin
  warmup, sin auto-retry en lote, `table_version` en cada línea.
- **Dataset en GitHub releases**; derivadas y análisis regenerables desde el crudo.

## 5. Workloads (fixtures sintéticos, inglés, checkers binarios)

**T1 × 19 modelos**: `qa_corto` · `calibracion` · `throughput`
**T2 × 6** (glm-5.3-flash, gpt-oss:20b, deepseek-v4-flash, minimax-m3, glm-5.3, kimi-k3):
`contexto_largo` (~30K), `generaciones_largas` (4–6K out), `multi_turno` (8 turnos),
`tool_calling` (3 herramientas), `reasoning`, `ratio_in` (~50K in/≤120 out), `ratio_out`
(20 gens × ~500 out).
**T3 × 3** (kimi-k2.7-code, glm-5.3-flash, deepseek-v4-pro): `multiarchivo`, `debugging`,
`refactor` — loop propio determinista (read/write/patch/list/run_tests, máx 12 pasos),
sandbox subprocess+timeout sin red, **pytest como checker**.
Repeticiones: **n=5** (mediana, IQR+p95; diferencia real = no-solape + >2 ticks o >5 %).
Fallback si la cuota no alcanza: reducir n de T3 primero, documentado; fixtures/checkers jamás.

## 6. Concurrencia (workstream)

Sonda de límite (k hasta el corte real bajo legado) + celdas k∈{1,4,8} sobre el ancla con
mismo total de tokens por celda; métrica de veredicto: **costo efectivo por tarea bajo k**.
Desenlaces codificados: invariante → *exprimir*; creciente → overhead; serializado → k irrelevante.
Campo `k` en el schema; errores/429 registrados.

## 7. Calibración de cache

Replay intra-lote (prefijo ~20K × r=4) + entre-lotes (5/30/90 s), slate T2, tres señales
(tokens, Δpp, TTFT). **Medición manda**: hit-rate concluyente reemplaza S1 por modelo
(versionado); S0 cota mínima; descuentos de papel declarados (los 5 modelos cached=input).

## 8. Predictibilidad (el claim de Ollama a prueba)

12 celdas estimadas a ciegas + re-estimación informada (doble fase, 0 cuota extra), en
**unidades nativas** (pp semanal / $ créditos); veredicto **comparativo** (MAPE legado vs nuevo,
bootstrap CI, sin umbral absoluto); celdas sub-resolución (Δpp < tick) excluidas del lado legado
y reportadas como hallazgo de opacidad.

## 9. Incentivos

Matriz de evidencia 9 hipótesis × 6 columnas, precargada con lo conocido; comparables
open-weights por familia (doc dedicado); datos propios del dueño como evidencia consentida.
Umbral dato-vs-especulación: solo entra en la matriz lo verificable con fuente o medición.

## 10. Break-even

`pp/1M* = ($/1M nuevo) ÷ ancla` por modelo/workload/escenario; bundle automático (tablas,
curvas Δpp↔tokens, quién-gana por perfil, PNGs) + **dashboard HTML estático**; 4 sensibilidades
(tarifas ±20 %, cache S∈{0,25,50,90} %, P_LEGADO ±30 %, eje k). Análisis post-hoc puro:
**re-correr sin re-medir** ante cambios de precios.

## 11. Compuerta de ejecución (cuando haya fondos/cuota)

1. `bench dry-run` antes de cada nivel (costo estimado, sin API).
2. Orden: T1 (calibra pp/token real) → calibración de cache → concurrencia → predictibilidad
   (estimaciones a ciegas ANTES de cada celda) → T2 → T3.
3. Techos por corrida acordados en la compuerta; si la cuota semanal no alcanza para n=5 en T3,
   **fallback n→T3 primero** (documentado); nunca se tocan fixtures/checkers.
4. Key nueva del plan nuevo ($20 Pro) cuando haya fondos → re-ejecuta la rama medida bajo tokens.

## 12. Estructura del informe final (fase posterior)

Responde con datos: (1) ¿el token-based es más económico y para qué workloads? (2) ¿a quién
encarece? (3) ¿el claim "GPU-time es difícil de predecir" se sostiene (MAPE comparativo)?
(4) ¿qué incentivos tiene Ollama según la matriz de evidencia? (5) ¿a quién beneficia el cambio?
— cada respuesta con su banda de incertidumbre y su sensibilidad a los 4 barridos.

## Preguntas abiertas declaradas (niebla transferida a la ejecución)

- ¿Qué mide `activity.cost` (¿saldo extra por request?) — solo verificable con fondos.
- Política de balance extra del plan nuevo (techo, autofacturación).
- Priority tiers / "fast mode" anunciados: si aparecen, re-correr break-even.
- Precios de kimi-k2.7-code serverless en algunos proveedores: "s/p" en la tabla de comparables.