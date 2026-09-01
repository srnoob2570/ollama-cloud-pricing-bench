# Benchmark de costos — Ollama Cloud

Estudio del costo efectivo de ejecutar cargas de trabajo LLM en Ollama Cloud durante su
transición del sistema de facturación legado (GPU-time) al nuevo (tokens). Este glosario es
la lingua franca de la metodología y de todos sus tickets.

## Language

### Facturación

**GPU-time**:
Tiempo de infraestructura GPU consumido por las requests; unidad declarada del sistema de
facturación **legado**. Su tarifa nunca se publicó ni se mapeó a unidades observables (es
la afirmación de opacidad que este estudio evalúa). _Avoid_: GPU minutes, usage points

**Plan legado**:
Cuenta suscrita bajo facturación por GPU-time. La única cuenta disponible para este estudio
(plan Max legado) y, por eso, **congelada**: no migrarla durante la recolección.
_Avoid_: plan viejo, cuenta vieja, old plan

**Plan nuevo**:
Facturación por tokens contra créditos en dólares que se refrescan cada mes
(Pro $20 → $60/mes · Max $100 → $300/mes · Team $500 → $1000/mes). La tabla oficial de
precios por 1M tokens es input / cached input / output por modelo.
_Avoid_: token-based plan, plan por créditos

**Créditos**:
Saldo mensual incluido en los planes nuevos, en dólares, que se consume por tokens; no se
acumula. Al agotarse, el consumo dibuja del balance extra, facturado por token.
_Avoid_: cupo, balance

**Ventana de cuota**:
Cada una de las dos ventanas con % de uso del plan legado: **sesión** (5 h) y **semanal**
(7 días). La **semanal es la que ancla** y la que habitualmente liga antes; la sesión es
restricción de saturación. La actividad monetaria (`activity`) rueda sobre ventanas de
4 semanas — nunca se presume mes calendario. _Avoid_: límite mensual, mes de facturación

**Punto de cuota (pp)**:
Una centésima de 1 % de la ventana de cuota. El medidor solo puede observarse en múltiplos
de un **tick** de 0.1 pp; cualquier diferencia por debajo del tick es ruido de resolución.
_Avoid_: punto de uso, tick de cuota (sin contexto)

**Cuota**:
Presupuesto de uso del plan legado, medido como fracción (`0.235` equivale a 23.5 %) por
ventana, con desglose por modelo. Es la unidad de cuenta primaria del sistema legado.
_Avoid_: límite, credit usage

**Ancla**:
Puente de dólares para la cuota: `P_LEGADO` (precio mensual pagado del plan legado —
$100/mes en la cuenta de este estudio, sin variante anual) amortizado por semana (÷ 4.345)
y dividido entre 100 pp, de modo que todo Δ% de cuota se expresa en dólares del plan.
Convierte las dos unidades de cuenta en comparables por la misma tarea.
_Avoid_: conversión, tipo de cambio

**Uso medido**:
Delta del medidor de uso de ollama.com (endpoint con API key) entre dos momentos; la
observación primaria del consumo legado. Se mide por **lote bracketeado** (nunca por
request). _Avoid_: consumo reportado (vago)

**Extrapolación**:
Estimar el costo bajo el plan nuevo usando tokens medidos × tarifas oficiales del modelo,
sin key del plan nuevo; se calcula siempre en los dos **escenarios de cache**.
_Avoid_: simulación, proxy de costo

**Escenario de cache (S0/S1)**:
Par de hit-rates asumidos sobre el que toda predicción del plan nuevo se reporta:
**S0 = 0 %** y **S1 = 50 %** (parámetro versionado del modelo de costo). La tabla oficial ya
fija el *descuento* por modelo; para los modelos sin descuento (cached input = input) S1 es
idéntico a S0. El lado legado no usa escenarios: mide el cache que Ollama realmente hace,
horneado en el Δ% observado. _Avoid_: porcentaje estándar (sin par), hit-rate solo

**Cached input**:
Tokens de entrada servidos desde cache (tarifa propia, distinta a la de input, en la tabla
del plan nuevo). _Avoid_: prompt cache hit (sin especificar que es input)

### Benchmarks

**Workload**:
Tipo de carga representativa, determinista y con seed (Q&A corto, contexto largo,
multi-turno, tool-calling, agéntico de código, debugging, refactoring, reasoning,
ratios extremos in/out). _Avoid_: bench, caso de prueba

**Fixture**:
Datos y prompts deterministas, generados con seed y versionados en el repo, que definen
una corrida de un workload (en inglés). Los de código son mini-repos sintéticos con bug o
objetivo conocido y tests como checker. _Avoid_: dataset (sin seed), caso de prueba

**Slate de modelos**:
Subconjunto fijo y versionado que lleva cada nivel: los 19 en T1; **6 estratificados en
T2** (glm-5.3-flash, gpt-oss:20b, deepseek-v4-flash, minimax-m3, glm-5.3, kimi-k3); **3 en
T3** (kimi-k2.7-code, glm-5.3-flash, deepseek-v4-pro). _Avoid_: muestra de modelos, selección

**Nivel (T1/T2/T3)**:
Densidad escalonada de ejecución: **T1** micro-benchmarks en los 19 modelos, **T2** suites
estructurales en ~6 modelos estratificados, **T3** workloads agénticos de código en 2–3
modelos. _Avoid_: tier 1/2/3 sin contexto, categoría

**Celda (de concurrencia)**:
Estallido de k requests simultáneas del mismo fixture, medido con lote bracketeado; la celda
k=1 (serial) es el baseline. Las celdas comparables llevan el mismo total de tokens.
_Avoid_: batch (sin k), oleada

**Sonda (de límite)**:
Request corta y barata lanzada a k crecientes para localizar el corte real de concurrencia
por key (429 / encolamiento / aceptación) antes de medir celdas.
_Avoid_: ping, healthcheck

**Checker**:
Validador determinista y binario (pasa / no pasa) del resultado de una tarea: tests o
compilación para código, checks sintéticos para lo demás. No hay LLM-judge en v1.
_Avoid_: judge, evaluador con rúbrica

**Tarea completada / intentada**:
**Completada** = la que pasa su checker; **intentada** = corrida completa sin importar
veredicto. La primaria del costo efectivo es la completada; la intentada siempre visible
como columna secundaria (hace visible el costo de los fallidos).
_Avoid_: hit (vago), tarea sin más

**Estimación a ciegas / informada**:
Predicción de costo hecha antes de ejecutarse una celda: **a ciegas** solo con la descripción
pública del fixture y las tarifas (sin mediciones previas), **informada** con las mediciones
ya obtenidas. La comparación de sus errores mide la opacidad y la curva de aprendizaje.
_Avoid_: forecast, apuesta

**MAPE del estudio**:
Error porcentual absoluto medio (|estimado−real|/real) de las estimaciones, por sistema y
por celda; el veredicto de predictibilidad es **comparativo** (legado vs nuevo con bootstrap
CI), nunca un umbral absoluto. _Avoid_: precisión (sin referencia), error %

**Derivadas**:
Métricas regeneradas desde el crudo (costos, TTFT, throughput, pass-rates) — nunca editan ni
reemplazan el crudo; si el algoritmo cambia, se recalculan todas.
_Avoid_: agregados (vago), post-proceso

**Dry-run**:
Corrida simulada del harness que calcula el costo estimado de una suite sin tocar la API:
sirve para decidir el gasto antes de gastarlo (la compuerta lo exige antes de cada nivel).
_Avoid_: preview, simulación de corrida

**Sandbox (de tests)**:
Ejecución aislada de los checkers de código: subprocess con timeout, sin red y con directorio
propio por tarea. _Avoid_: contenedor (implícito), ambiente compartido

**Replay de prefijo**:
Secuencia determinista que re-envía un mismo prefijo grande (r veces dentro del lote y en
lotes espaciados) para revelar si la infraestructura cachea, a qué horizonte persiste y con
qué descuento real. _Avoid_: repetición (vaga), warm-cache

**Hit-rate medido**:
Fracción del input servida desde cache según la medición (tokens reportados o proxy Δpp);
cuando es concluyente, **reemplaza** al supuesto S1 por modelo. Si queda bajo la resolución
del medidor, el supuesto se conserva marcado como tal.
_Avoid_: tasa de acierto (sin medir), S1

**Break-even**:
Combinación de (tokens in, tokens out, throughput, % cache, concurrencia k) a partir de la
cual un sistema de facturación pasa a ser más barato que el otro para un modelo/plan dado.
_Avoid_: punto de equilibrio (usar el inglés entre paréntesis cuando haga falta)

**Umbral crítico**:
Valor de pp/1M tokens a partir del cual el legado pasa a ser más caro que el nuevo para un
modelo, workload y escenario dados: ($/1M del plan nuevo) ÷ ancla. Se compara contra el
pp/1M medido; nunca se extrapola desde modelos sin medición.
_Avoid_: break-even point (en tablas, usar ambos), crossover

**Re-correr (sin re-medir)**:
Recalcular todo el análisis desde el crudo + la tabla versionada + los parámetros (ancla, S,
k), sin tocar cuota: la respuesta del estudio a cualquier cambio de precios.
_Avoid_: re-benchmark (eso re-gasta cuota)

**Costo efectivo**:
Costo por unidad de trabajo útil completada (por tarea que pasa su checker), no por token
nominal: la métrica de veredicto del estudio. Secundarias visibles: por intentada y por
millón de tokens. _Avoid_: costo real, $/1M tokens (es solo una derivada)