# Cost benchmark — Ollama Cloud

Study of the effective cost of running LLM workloads on Ollama Cloud during its transition
from the legacy (GPU-time) billing system to the new (token-based) one. This glossary is the
lingua franca of the methodology and all of its tickets. The study was drafted in Spanish;
the Spanish terms of record appear under each entry as cross-references.

## Language

### Billing

**GPU-time**:
GPU infrastructure time consumed by requests; the declared unit of the **legacy** billing
system. Its rate was never published and never mapped to observable units (that opacity is
what this study evaluates). _Avoid_: GPU minutes, usage points · _es_: GPU-time

**Legacy plan**:
An account subscribed under GPU-time billing. The only account available to this study
(legacy Max plan) and therefore **frozen**: it must not be migrated during data collection.
_Avoid_: old plan, cuenta vieja · _es_: plan legado

**New plan**:
Token-based billing against dollar credits refreshed monthly (Pro $20 → $60/mo · Max
$100 → $300/mo · Team $500 → $1000/mo). The official price table is input / cached input /
output per 1M tokens per model. _Avoid_: token-based plan, plan por créditos · _es_: plan nuevo

**Credits**:
The dollar balance included in new plans, refreshed monthly and consumed by tokens; it does
not roll over. Once exhausted, usage draws from the extra balance, billed by token.
_Avoid_: cupo, balance · _es_: créditos

**Quota window**:
Each of the two windows with a usage percentage in the legacy plan: **session** (5 h) and
**weekly** (7 days). The **weekly window anchors** the study and usually binds first; the
session window is a saturation constraint. Monetary `activity` rolls on 4-week windows — a
calendar month is never assumed. _Avoid_: límite mensual · _es_: ventana de cuota

**Quota point (pp)**:
One hundredth of 1 % of a quota window. The meter is only observable in multiples of a
**tick** of 0.1 pp; any difference below the tick is resolution noise.
_Avoid_: punto de uso · _es_: punto de cuota (pp)

**Registration**:
The batch's requests becoming visible in the per-model `request_count` of **both** quota
windows; the settlement signal of the bracketed batch. When the requests register, both
windows' usage has already been recalculated — a pp delta below the tick is expected (small
usage need not move the displayed pp) and is resolution, never latency.
_Avoid_: usage update (confuses registration with pp movement) · _es_: registro

**Quota**:
The legacy plan's usage budget, measured as a fraction per window (`0.235` equals 23.5 %),
with a per-model breakdown. The primary unit of account of the legacy system.
_Avoid_: límite, credit usage · _es_: cuota

**Anchor**:
The dollar bridge for the quota: `P_LEGADO` (the monthly price paid for the legacy plan —
$100/mo in this study's account, no annual variant) amortized per week (÷ 4.345) and divided
by 100 pp, so every Δ% of quota is expressed in plan dollars. It makes the two billing
systems comparable for the same task. _Avoid_: conversión, tipo de cambio · _es_: ancla

**Measured usage**:
Delta of the ollama.com usage meter (API-key endpoint) between two moments; the primary
observation of legacy consumption. Measured with **bracketed batches** (never per request).
_Avoid_: consumo reportado (vague) · _es_: uso medido

**Bracket pool**:
One bracketed batch covering several workloads and/or all repetitions of a cell: the meter
reading is the pool's Δpp; the n=5 statistics live in the per-request rows, so pooling costs
no extra tokens. Settles serialize — the bracket count, not the token count, is what wall
time is made of. _Avoid_: batch (without k), lote agrupado (vague) · _es_: bracket agrupado

**Allocated reading**:
A legacy cost attributed to a cell by token-share allocation from a pooled bracket's
measured Δpp. Marked as allocated and never verdicted: verdicts require a directly
measured legacy reading (margin rule: >2 ticks or >5 % of the cheaper cost).
_Avoid_: estimado (unmarked), medido (it is not) · _es_: lectura asignada

**Verdict margin**:
The saving from picking the winner: (loser − winner) ÷ loser, as a percentage of the
loser's cost. It rides the verdict object alongside the winner; a verdict exists only
when the margin clears the tie band (>2 ticks or >5 % of the cheaper cost).
_Avoid_: beneficio (unquantified), ventaja · _es_: margen del veredicto

**Extrapolation**:
Estimating the new-plan cost from measured tokens × official model rates, without a new-plan
key; computed under **S0 and the versioned S1 default**, with any other **S(x)** entering
only as a stamped re-run's separate analysis set. _Avoid_: simulación · _es_: extrapolación

**Cache scenario (S0/S1)**:
The persisted reference pair for every new-plan prediction: **S0 = 0 %** (the floor) and
**S1 = the versioned default hit rate (50 %)**, declared by the methodology — a versioned
parameter, never a fixed constant. **S(x)** is the parameterized hit rate a stamped re-run or
the dashboard's slider may use; S(x) ≡ S0 for models without a published discount (cached
input = input). The legacy side keeps no scenarios: under the **cache-free lane** it measures
cache-free work, and real caching is measured only by calibration and the billing canary.
_Avoid_: valor fijo, porcentaje estándar · _es_: escenario de cache

**Cached input**:
Input tokens served from cache (its own rate, distinct from input, in the new-plan table).
_Avoid_: prompt cache hit (unspecified as input) · _es_: cached input

**Cache-free lane**:
The run mode where every measured request carries a run-scoped, seeded **nonce as its very
first tokens** — Ollama Cloud's prefix cache keys from token 0 and offers no toggle — forcing
a cache miss, so the measured pp is the workload's **raw work**. Multi-turn salts every turn.
Exempt: prefix replay (calibration), the billing canary, and the concurrency probe (a
locator, not a measured cell). _Avoid_: desactivar el cache (there is no switch), warm run ·
_es_: carril sin cache

**Billing canary**:
The per-run paired check that the cache-free lane holds: 5 salted requests + 5
identical-prefix replays; the replay must bill at ~11–14 % of the salted quota (measured
1/7 on kimi-k3). An alarm above 0.5 aborts the run at the gate — a ratio near 1 means the
salting broke. The passive detector cross-checks every bracket's Δpp against the token
budget. _Avoid_: healthcheck (it bills quota) · _es_: canario de facturación

### Benchmarks

**Workload**:
A representative, deterministic, seeded load type (short Q&A, long context, multi-turn,
tool-calling, code agent, debugging, refactoring, reasoning, extreme in/out ratios).
_Avoid_: bench, caso de prueba · _es_: workload

**Fixture**:
Deterministic, seeded, version-in-repo data and prompts defining one run of a workload (in
English). Code fixtures are synthetic mini-repos with a known bug or goal and tests as
checker. _Avoid_: dataset (unseeded) · _es_: fixture

**Model slate**:
The fixed, versioned subset carrying each level: all 19 in T1; **6 stratified in T2**
(glm-5.3-flash, gpt-oss:20b, deepseek-v4-flash, minimax-m3, glm-5.3, kimi-k3); **3 in T3**
(kimi-k2.7-code, glm-5.3-flash, deepseek-v4-pro). _Avoid_: muestra de modelos · _es_: slate

**Level (T1/T2/T3)**:
Staged execution density: **T1** micro-benchmarks on all 19 models, **T2** structural suites
on ~6 stratified models, **T3** agentic code workloads on 2–3 models.
_Avoid_: tier 1/2/3 without context · _es_: nivel

**Concurrency cell**:
A burst of k simultaneous requests of the same fixture, measured with a bracketed batch; the
k=1 (serial) cell is the baseline. Comparable cells carry the same total tokens.
_Avoid_: batch (without k), oleada · _es_: celda (de concurrencia)

**Probe**:
A short, cheap request fired at increasing k to locate the real per-key concurrency cut-off
(429 / queueing / acceptance) before measuring cells; the cells then **re-anchor** to the
measured cut-off (a planned k above it runs at the cut-off, documented in the dataset —
never at a published guess). _Avoid_: ping, healthcheck · _es_: sonda

**Checker**:
A deterministic binary (pass / fail) validator of a task's outcome: tests or compilation for
code, synthetic checks for the rest. No LLM-judge in v1. _Avoid_: judge, rúbrica · _es_: checker

**Completed / attempted task**:
**Completed** = one that passes its checker; **attempted** = full run regardless of verdict.
Completed is the primary effective-cost unit; attempted always visible as a secondary column
(it makes the cost of failures visible). _Avoid_: hit (vague) · _es_: tarea completada/intentada

**Blind / informed estimate**:
A cost prediction made before a cell runs: **blind** with only the fixture's public
description and the rates (no prior measurements), **informed** with the measurements already
taken. Comparing their errors measures opacity and the learning curve.
_Avoid_: forecast, apuesta · _es_: estimación a ciegas/informada

**Study MAPE**:
Mean absolute percentage error (|estimate−real|/real) of the estimates, per system and per
cell; the predictability verdict is **comparative** (legacy vs new with bootstrap CI), never
an absolute threshold. _Avoid_: precisión, error % · _es_: MAPE del estudio

**Derivatives**:
Metrics regenerated from the raw data (costs, TTFT, throughput, pass-rates) — they never edit
or replace the raw data; if the algorithm changes, everything is recomputed.
_Avoid_: aggregates (vague) · _es_: derivadas

**Dry-run**:
A simulated harness run that computes a suite's estimated cost without touching the API: it
is how spending is decided before spending (the spending gate requires it before each level).
_Avoid_: preview, simulación · _es_: dry-run

**Sandbox (tests)**:
Isolated execution of code checkers: subprocess with timeout, no network, per-task working
directory. _Avoid_: contenedor (implícito) · _es_: sandbox

**Prefix replay**:
A deterministic sequence re-sending one large fixed prefix (r times within a batch and in
spaced batches) to reveal whether the infrastructure caches, over what horizon it persists,
and at what real discount. _Avoid_: repetición (vague), warm-cache · _es_: replay de prefijo

**Measured hit-rate**:
The fraction of input served from cache according to measurement (reported tokens or Δpp
proxy); when conclusive, it **replaces** the S1 assumption per model. Below the meter's
resolution, the assumption is kept and marked as such.
_Avoid_: tasa de acierto (unmeasured), S1 · _es_: hit-rate medido

**Break-even**:
The combination of (tokens in, tokens out, throughput, % cache, concurrency k) at which one
billing system becomes cheaper than the other for a given model/plan.
_Avoid_: punto de equilibrio · _es_: break-even

**Critical threshold**:
The pp/1M value above which legacy becomes more expensive than the new system for a given
model, workload, and scenario: (new-plan $/1M) ÷ anchor. Compared against the measured pp/1M;
never extrapolated from unmeasured models. _Avoid_: crossover · _es_: umbral crítico

**Re-run (without re-measuring)**:
Recomputing the entire analysis from raw data + versioned table + parameters (anchor, S, k)
without touching quota: the study's answer to any price change — or any **S(x)** other than
the default, which produces a **new, parameter-stamped analysis set** (methodology version
included) and never edits the persisted S0/S1 reference.
_Avoid_: re-benchmark (that re-spends quota) · _es_: re-correr
