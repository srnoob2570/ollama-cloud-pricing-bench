# Verificación en vivo del medidor de uso (2026-08-31)

Resolución del ticket «Verificar en vivo el medidor de uso» — la única corrida con cuenta real
de este mapa (guardarraíl del mapa: ~10 requests triviales). Gasto real: 6 requests
`nemotron-3-nano:30b` (156 in + 72 out tokens, ~3.4 s de `total_duration` agregado).

Logs crudo: [`logs/medidor-vivo-2026-08-31/reads.jsonl`](./logs/medidor-vivo-2026-08-31/reads.jsonl)
(y `requests.jsonl` hermano) — sin credenciales. Complemento teórico:
[`medidor-uso-ollama.md`](./medidor-uso-ollama.md), que este doc corrige en vivo.

## 1. La API key (Bearer) SÍ autentica el medidor

`GET https://ollama.com/api/usage` con `Authorization: Bearer <API key>` → **200**.
Sin auth → 401 (`{"error":"invalid credentials"}`); `/api/usage/session` y `/api/usage/weekly` → 404.
El research documental decía que solo la cookie de sesión estaba probada: queda **corregido** —
la propuesta original del dueño (poll del endpoint entre peticiones) funciona directamente, sin navegador.

## 2. Qué expone exactamente (estructura observada)

```json
{"activity": {"cost": "0.00000", "period": {"type": "last_4_weeks", ...}, "models": []},
 "limits": {
   "session": {"usage": 0.234, "models": [{"name": "glm-5.3-flash", "request_count": 962}, ...]},
   "weekly":  {"usage": 0.146, "models": [{"name": "kimi-k3", "request_count": 209}, ...]}}}
```

- `limits.session.usage` y `limits.weekly.usage`: **fracción de cuota** (0.234 = 23.4 %), resolución
  observada **0.001 (0.1 %)** por lectura. Ni GPU-seg, ni tokens, ni dólares por request.
  ⚠️ **Convención de este doc y de las tablas**: los valores de `usage` se escriben siempre como
  fracción tal cual los da el API (`0.235` equivale a 23.5 %; un paso de `0.001` = 0.1 puntos
  porcentuales). No leerlos como "0.235 %".
- `limits.*.models[]`: `request_count` **entero por modelo** (nombre = id del catálogo, con tag:
  `nemotron-3-nano:30b`).
- `activity.cost`: saldo en $ a 5 decimales, **quedó invariante** ("0.00000") durante todo el
  experimento bajo cuota incluida. Hipótesis: solo acumula con *balance extra* pay-as-you-go —
  no verificable sin fondos (queda como niebla del mapa).

## 3. Lag y cuantización medidos

**Convención**: `sess`/`week` son fracciones tal como las devuelve el API — `0.235` = 23.5 % de la cuota.

| Lectura | t (s) | sess | week | nemotron reqs |
|---|---|---|---|---|
| baseline / pre_r1 | 0.4 / 2.7 | 0.234 | 0.146 | — |
| lag+0s (r1 completada 0.35 s antes) | 3.9 | 0.234 | 0.146 | **1** |
| lag+39s | 44.4 | 0.234 | 0.146 | 1 |
| lag+69s | 74.7 | **0.235** | 0.146 | 1 |
| pre_r3 | 83.2 | 0.235 | **0.147** | 2 |
| pre_r4..r6 | 92–108 | 0.235 | 0.147 | 3,4,5 |
| settle_final (+45 s) | 161.4 | **0.236** | 0.147 | **6** |

- **`request_count` se registra casi instantáneo**: r1 estaba contada ~1 s después de completarse,
  mucho antes de que el % se moviera. Es el atribuidor por-request fiable y el "acuse de recibo"
  de que una request ya entró a la contabilidad.
- **El % de cuota laguea ~60–90 s** (primer cambio de sesión ∈ (39 s, 69 s] tras r1; weekly ~76–83 s)
  y **cuantiza en pasos de 0.1 %**: las 6 requests movieron solo +0.002 (sesión) y +0.001 (weekly).
- Con estos datos, **la atribución de Δ% a una request individual queda descartada** (tal como
  anticipaba el research); lo que este ticket añade es que **el contador por modelo no solo no
  laguea sino que es exacto**: `nemotron-3-nano:30b` → 1,1,2,3,4,5,6 con 6 requests.

## 4. Protocolo de medición derivado (insumo para «Modelo de costo» y «Protocolo de medición»)

**Primitiva = lote bracketeado**, no request individual:

1. Lectura del medidor (crudo JSON completo guardado).
2. Lote de N requests, **un modelo o pocos**, todos los `request_count` presentes en la lectura pre.
3. Confirmación de registro inmediata vía Δ`request_count` (≈1 s), útil también como sanity-check
   de que todo el lote se facturó.
4. Espera **≥ 90 s** y segunda lectura: Δ% de cuota **por lote** (no por request).
5. Los tokens por request (`prompt_eval_count`/`eval_count` de la API) se cruzan con el Δ% del
   lote para construir el mapeo tokens↔cuota; la resolución de ese mapeo es **0.001 de cuota**,
   así que los lotes deben ser grandes frente al quantum (p. ej. ≥ 30× el contenido de un lote
   trivial como este, o el Δ% es indistinguible del redondeo).

Corolario para las fórmulas: el error de cualquier Δ% por lote es ±0.001 (resolución) y el
reloj de facturación se estabiliza a ~90 s — las corridas del benchmark deben dormir ~90 s
entre lotes o aceptar arrastre de un lote a otro.

## 5. Descubrimientos colaterales

- **Ids de catálogo con tag** (`nemotron-3-nano:30b`, `gemma4:31b`, `deepseek-v4-pro:0813`,
  `mistral-large-3:675b`…) frente a la tabla de precios sin tags (`nemotron-3-nano`):
  el harness necesita regla de mapeo por prefijo.
- **Uso real del dueño** (contexto de la línea base): glm-5.3-flash domina con 2391 requests
  semanales y 962 de sesión — perfecto como modelo-ancla y como proxy de "usuario con muchas
  solicitudes pequeñas".
- `activity.cost` (campo $ con 5 decimales) es el candidato natural a lectura directa de costo
  si alguna vez acumula; hoy, con 0.00000, no se puede confirmar nada.

## 6. Añadido post-cierre: segunda lectura del dueño (~23:04, uso real entre medias)

Lectura del dueño ~25 min después del experimento (42 requests reales de glm-5.3-flash entre
medias, 1004/2433 en su sesión/semana frente a 962/2391 al arrancar este ticket):

- **Experimento natural de calibración**: 42 requests reales de glm-5.3-flash movieron la cuota
  de sesión +0.005 (0.236 → 0.241, es decir **23.6 % → 24.1 %**) — ~0.00012 %/request en un
  modelo "flash" mediano, coherente
  con el quantum de 0.001 por lote. Segundo punto de calibración tokens↔cuota (sin tokens por
  request conocidos de esas 42, queda como cotación de orden de magnitud, no como ratio exacto).
- **`activity.period` es ventana rodante de 4 semanas** (`type: "last_4_weeks"`,
  `starting_at: 2026-08-10T00:00:00Z`, `ending_at` avanza con cada llamada) — dato para el
  **ancla** a dólares: la "cuota mensual" de los límites no es un mes calendario; el period
  de activity rueda.
- **"web search" cuenta como pseudo-modelo** en `request_count` (9 sesiones / 51 semana del
  dueño) — el harness debe decidir si contarlo o filtrarlo.
- `nemotron-3-nano:30b` quedó estable en **6** en ambas tracks exactamente como dejó el
  experimento: los contadores no decaen ni se revierten.

## 7. Preguntas que quedan abiertas (niebla del mapa)

- ¿`activity.cost` se incrementa por request cuando hay balance extra activo? (requiere fondos;
  si sí, sería la lectura directa de costo faltante).
- ¿El lag de ~60–90 s es batching del backend o propagación? (irrelevante para el protocolo
  mientras se espere el settle, y por eso no sube a ticket).