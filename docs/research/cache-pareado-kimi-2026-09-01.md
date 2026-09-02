# Cache pareado en kimi-k3: el descuento de prefijo, medido con diseño limpio

**Fecha**: 2026-09-01 · **Tipo**: investigación viva (owner-run) · **Insumo de**: requerimiento
"carriles sin cache", corrección del hallazgo de `pp-sesion-usd-2026-09-01.md` §8, y de la
composición v3 (medibilidad). **Instrumentos y logs crudos**: [`live-probes/`](../../live-probes/README.md).

## 1. La pregunta

El hallazgo del §8 ("replay de prefijo exacto se factura a ~11 % del precio, ~9× más barato")
tenía dos defectos que el owner señaló:

1. **El emparejamiento no está probado en el registro**: el brazo cacheado (5 requests
   idénticos) y el brazo salteado (10 con nonce) vinieron de variantes distintas del script;
   nada prueba que compartieran el mismo cuerpo de prompt.
2. **El encuadre "11 % del precio" mezcla denominadores**: el medidor legado no cobra por
   tokens. Mide uso (GPU-time declarado, opaco). El ratio correcto es de cuota consumida,
   no de precio por token; el "precio completo" del §8 eran ticks del medidor, no dólares.

Además, la investigación externa (docs.ollama.com, ollama/ollama #16714/#15758) confirmó que
**no existe toggle de cache**. El cache es implícito, prefijo-indexado e invisible
(`cached_tokens` nunca se reporta). "Desactivar el cache" operacionalmente = forzar cache-miss
con un nonce aleatorio al inicio de cada prompt (el cache casa el prefijo izquierda→derecha
desde el token 0).

## 2. Diseño pareado (`live-probes/kimi_paired_cache_probe.py`)

Mismo cuerpo (T2 `long_context`, sha `ca123ce574e4febc`, 153,071 chars), mismo presupuesto de
nonce (~400 palabras) en los tres brazos; la única variable es si el nonce se repite. Cada fase
es su propio bracket (quiet 5 s → pre → burst serial → settle 15 s → post → confirm 30 s), con
guardia de contaminación (`glm-5.3-flash` counts pre/post, planos en ambas ventanas durante
toda la corrida: 391/10,558). 30 requests reales, todos 200, `tok_in` 39,892–39,893/request.

| Brazo | Construcción | Esperado |
|---|---|---|
| **A** sin cache | nonce fresco por request (10 prefijos distintos) | 10 cache-miss forzados, precio completo |
| **B1** replay frío | un nonce fijo, primera pasada | req 1 miss, reqs 2–10 hits |
| **B2** replay caliente | mismo nonce fijo, segunda pasada (~35 s después) | 10 hits |

## 3. Resultados

| Brazo | Δpp sesión | Δpp semanal | ticks sesión/req | tokens | R (s:w) | latencia |
|---|---|---|---|---|---|---|
| A | +0.056 (56 ticks) | +0.008 (8) | **5.6** | 402,062 | 7.0 | 5.0–6.8 s |
| B1 | +0.011 (11) | +0.002 (2) | 1.1 | 401,531 | 5.5 | req 1: 5.26 s (frío); resto 2.4–7.4 s |
| B2 | +0.008 (8) | +0.001 (1) | **0.8** | 402,066 | 8.0 | 2.3–6.0 s (mayormente caliente) |

**Ratios dentro del medidor legado** (session pp/1M): B2/A = **0.143** (exactamente 1/7),
B1/A = 0.197, semanal B2/A = 0.125.

## 4. Lectura

1. **El brazo A replica el bracket verificado del §8**: 402,062 vs 402,150 tokens;
   +0.056/+0.008 vs +0.056/+0.009. El diseño salteado queda re-validado: uniformidad de
   `tok_in` (39,892–39,893) y precio completo uniforme por request.
2. **El descuento de trabajo cacheado es r ≈ 0.11–0.15** (lectura central ~1/7 ≈ 0.14):
   un request servido desde cache consume ~7× menos cuota que el mismo request sin cache
   (banda 7–9×). El B1 implica r ≈ 0.108 por aritmética de ticks ((1+9r)/10 = 0.197, req 1
   frío); el B2 da 1/7 exacto. El ~11 % del §8 queda dentro de la banda. Su magnitud
   sobrevive, pero ahora con prefijos probadamente iguales y con el encuadre correcto.
3. **El encuadre corregido (owner)**: el cache de Ollama Cloud reduce el trabajo que el
   medidor legado refleja, no es un descuento de factura por token (el plan legado no tiene
   precio por token). Consecuencia: cada pp compra ~7× más trabajo cacheado. Es mayor capacidad
   efectiva del plan, no "precio con descuento". El 10 % publicado de kimi-k3 ($3.00 →
   $0.30 cached) es un denominador distinto (descuento de facturación del lado nuevo);
   su cercanía al ratio medido es sugestiva, no establecida, y por modelo.
4. **Persistencia**: B2 arrancó caliente inmediatamente (~35 s tras B1) y en el test del §8
   el replay calentó *antes* del primer request del brazo cacheado (persistió desde el
   bracket T2 de horas antes). El horizonte del cache excede los minutos; la medición fina
   (5/30/90 s) es trabajo de `calibrate-cache`.
5. **Latencia como corroboración TTFT**: los requests calientes corren visiblemente más
   rápido (B2 ~2.3–6.0 s vs A ~5.0–6.8 s), la segunda señal, junto al Δpp, de que el
   prefill se está saltando.
6. **R (sesión:semanal) en brazos cacheados es ruidoso** (5.5/8.0, Δpp semanal sub-tick):
   la banda R ≈ 5–7 sigue anclada a requests a precio completo; los ratios de cache deben
   montarse en sesión (el readout prácticamente más fino), con la semanal como
   corroboración.

## 5. Consecuencias para las decisiones en vuelo

- **Defecto documentado del dataset v2**: todo request cuya cola repite prefijos (reps de
  bracket, celdas k>1 idénticas, turnos de multi-turn, re-runs calientes) subestima el costo
  raw en el medidor legado. Los carriles sin cache (nonce por request, registrado en el
  manifiesto) son requisito de protocolo v3, no una refinación.
- **Corrección al registro de #36**: el hallazgo se re-redacta como "un replay de prefijo
  exacto consume ~11–14 % de la cuota que consume el mismo request sin cache (kimi-k3,
  prefijo ~40K tokens)", nunca como "11 % del precio". La entrada de glosario *Cache scenario*
  ("the legacy side measures the caching Ollama actually does") muere bajo carriles sin
  cache: el lado legado medirá trabajo sin cache; el cache solo se observa en calibración.
- **El canario de facturación** (un replay de prefijo debe facturar ~1/7–1/10; si factura
  ~100 % con requests salteados en vuelo, el salteado se rompió) queda como salvaguarda
  operativa de v3.

## Archivos

- [`live-probes/kimi_paired_cache_probe.py`](../../live-probes/kimi_paired_cache_probe.py) ·
  [`kimi_session_weekly_test.py`](../../live-probes/kimi_session_weekly_test.py) (instrumentos)
- [`live-probes/kimi-paired-cache-probe.jsonl`](../../live-probes/kimi-paired-cache-probe.jsonl) ·
  [`kimi-bracket-series.jsonl`](../../live-probes/kimi-bracket-series.jsonl) (logs crudos)
- [`live-probes/kimi-paired-cache-probe-20260901-console.txt`](../../live-probes/kimi-paired-cache-probe-20260901-console.txt) (transcripción, con SUMMARY)
- Gasto: 30 requests ≈ 1.21 M tokens ≈ +7.5 pp de sesión, +1.1 pp semanal (0.426 → 0.437).
