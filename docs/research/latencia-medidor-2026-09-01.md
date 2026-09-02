# Latencia del medidor de uso, 2026-09-01

Medición en vivo para el ticket «Remedir la latencia del medidor: conteos y pp en ambas
ventanas» (mapa «Harness v1.1: medibilidad, latencia, precisión y lectura»). Complementa y
corrige el protocolo del ticket «Verificar en vivo el medidor de uso» (2026-08-31), que fijó
"conteo instantáneo, pp con lag ~60–90 s" a partir de una sola verificación.

## Protocolo

Cada ensayo: lectura cruda de `/api/usage` → **1 request trivial streaming** (`kimi-k3`,
147 tokens in + 8 out, `num_predict=8`, seed fija) → poll de `/api/usage` cada 5 s durante
120 s, registrando por sondeo: `request_count` del modelo en la ventana de sesión y en
la semanal, el `usage` (pp) de cada ventana, y la lista completa de modelos de ambas
ventanas. 3 ensayos + 1 descartado (gemma4, bug de tags, ver Lección). Gasto total: 4
requests triviales (≤155 tokens c/u), ≪1 tick, bajo el tope aprobado de ≤10.

**Experimento natural gratis**: durante los ensayos la cuenta tuvo tráfico concurrente en
`glm-5.3-flash` (+21 requests en ~8 min, +5 ticks de pp de sesión). Cada salto de ese
conteo correlacionado con el pp mide la latencia del medidor sin gastar requests propios.

## Resultados

| Señal | Latencia observada | Evidencia |
|---|---|---|
| `request_count`, ventana sesión | **≤6.2 s** (primer sondeo, 3/3 ensayos) | 25→26, 26→27, 27→28 en t=6.1–6.2 s |
| `request_count`, ventana semanal | **≤6.2 s** (primer sondeo, 3/3 ensayos) | 234→235→236→237 en t=6.1–6.2 s |
| pp de **sesión** | **≤5 s** (co-movimiento con conteos, misma resolución de sondeo) | 4 pasos (0.079→0.083), cada uno en el mismo sondeo que saltos de conteo concurrentes |
| pp de **semanal** | **>8 min sin moverse** pese a +21 requests y +5 ticks de sesión | 0.386 → 0.386 durante los 3 ensayos completos |

La latencia real del conteo está acotada a (0, 6.2] s; el count-check del runner lo lee a
~0.17 s del burst, así que el verdadero valor probablemente es ~1 s o menos. El pp semanal
debería haber absorbido ≈1–2.6 ticks con el tráfico observado (≈38.5K tokens de prefill por
tick semanal) y no se movió ni una décima de tick.

## Hallazgos

1. **La condición de parada propuesta (petición registrada en el conteo de sesión Y en el
   semanal) es instantánea**: se cumple en el primer sondeo (~≤6 s). Ambos conteos son el
   señal rápido y exacto. Confirma el diseño del protocolo v1/v2.
2. **Las dos ventanas no son iguales**: el pp de sesión aterriza en segundos (≤5 s de los
   conteos); el pp semanal no aterrizó en 8 minutos. La única medición previa del lag
   semanal (76–83 s, 2026-08-31) y los brackets T2 `long_context` (Δpp semanal 0.1–0.5
   dentro de settles de 90 s, ~19:51Z del mismo día) muestran que eventualmente aterriza.
   Pero hoy, con ~1–2.6 ticks semanales acumulándose en vivo, no lo hizo en 8 min.
   Hipótesis: el pp semanal cuantiza/agrega en quanta más grandes o en un ciclo más largo.
3. **El settle de 90 s fijo probablemente recorta la ventana semanal** (sospecha ya anotada
   por el «Presupuesto de medibilidad»): si el aterrizaje semanal supera los 90 s, el post-read
   del bracket cierra antes de que la unidad de cuenta de la estudio refleje el gasto.
4. **Bajo tráfico concurrente, "pp estable" nunca ocurre**: el pp de sesión subió 5 ticks en
   8 min por tráfico ajeno al batch. La parada del loop no puede ser solo "dos lecturas
   iguales". Debe anclarse en el conteo (exacto e instantáneo) + pasos de pp por ventana +
   tope por ventana.
5. **Lección de tags**: los nombres de la lista de modelos son los ids con tag del
   catálogo** (`gemma4:31b`, `deepseek-v4-flash:0731`...). Buscar el id de slate sin tag
   devuelve "modelo ausente" aunque el medidor lo cuente. El runner ya envía `modelo_api`
   con tag; el bug fue exclusivo del script de este ensayo.

## Implicación para el diseño del settle (input al ticket «Settle adaptativo»)

- Parada primaria: conteo del modelo verificado en ambas ventanas (≤2 sondeos, ~10 s).
- Parada por ventana: cerrar el bracket de sesión con 2 lecturas estables (~10–15 s
  totales); la ventana semanal necesita su propia espera larga con tope. El valor lo fija
  el probe de la pregunta abierta siguiente.
- El 90 s pasa a tope de sesión, no estándar; el tope semanal es otro número (≥2–3 min
  provisional hasta medir).

## Pregunta abierta (gradúa a ticket)

**¿Cuándo aterriza el pp semanal?** Requiere un probe super-tick: 1 request del tamaño de
los fixtures `long_context` (~30–50K tokens ≈ 0.1–0.5 pp ≈ $0.023–0.115 del ancla), sondeando
hasta ver el paso semanal. Supera el tope "≪1 tick" aprobado para este ticket → necesita
aprobación de gasto explícita antes de correrse.

## Corrección del dueño (2026-09-01, post-probe), la semántica correcta

La lectura de «la ventana semanal es la lenta» era un error de categoría: se confundió la
**latencia** del pp semanal con la **ausencia de aumento** del pp semanal. La semántica
correcta, fijada por el dueño:

- **La señal de asentamiento es el REGISTRO de las peticiones**, no el movimiento del pp.
- Cuando las requests se registran en el `request_count` de ambas ventanas, el uso de la
  sesión y el semanal ya se recalcularon. No hay una ventana «lenta» que esperar.
- Que el pp (de sesión o semanal) no varíe **está bien**: el plan legado de $100 absorbe
  uso pequeño sin mover el porcentaje visible. Un Δpp bajo el tick es resolución, no lag.

## Probe super-tick (kimi-k3, 38,293 tokens in, la celda T2 que midió Δpp 0.5/0.1)

| Evento | t tras la request | Observación |
|---|---|---|
| CHAT completada | 6.48 s | http 200, tok_in exacto del fixture |
| COUNT sesión + COUNT semanal | 11.9 s (~5.4 s tras la request) | 28→29 y 237→238 en el mismo sondeo |
| **PP sesión** | 11.9 s | 0.087→0.093 (+6 ticks ≈ el 0.5 pp de T2) |
| **PP semanal** | 11.9 s | 0.387→0.388 (+1 tick = el 0.1 pp de T2) |
| PP sesión adicional | 28.1 s | +1 tick, tráfico concurrente del dueño, no del probe |

**Conclusión corregida**: cuando el Δpp es observable, aterriza junto con el registro
(~5 s), en ambas ventanas. Los «8 min sin movimiento semanal» de los ensayos de 155 tokens
no eran lag: eran sub-resolución. Las requests se registraron y el pp recalculado legítimamente
no varió. No existe un tope semanal distinto que fijar: el settle se ancla en el registro.
(La evidencia de 76–83 s del 2026-08-31 queda como observación histórica sin reproducir;
el ticket de settle decide si se conserva alguna espera defensiva.)

## Registro crudo

`/tmp/latency-results.jsonl` (3 líneas, una por ensayo: eventos + sondeos completos con las
listas de modelos por ventana). Script del ensayo: `/tmp/latency_trial.py` (fuera del repo:
no es código del harness). Ensayo 1 descartado (gemma4): resultados no atribuibles por el
bug de tags; sus pp son confundidos con el tráfico concurrente.
