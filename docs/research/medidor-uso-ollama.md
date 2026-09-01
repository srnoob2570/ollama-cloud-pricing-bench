# El medidor de uso de ollama.com — qué expone y con qué granularidad (2026-09-01)

Resolución del issue
[srnoob2570/ollama-cloud-pricing-bench#2](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/2).
Pregunta: ¿puede un cliente externo medir el consumo del plan legado **GPU-time** por request,
y con qué granularidad? Todo lo marcado como sondeo se ejecutó sin credenciales el
2026-09-01 (UTC±, `x-build-commit: fcafe397` del servidor); cero peticiones de inferencia,
cero cuota gastada, ninguna key inventada.

## 1. ¿Existe un endpoint del medidor y qué auth pide?

- **`https://ollama.com/api/usage` existe.** Sondeo propio: `GET /api/usage` sin auth →
  **HTTP 401**, `content-type: application/json`, cuerpo `{"error":"invalid credentials"}`.
  Los endpoints vecinos no existen: `/api/usage/session`, `/api/usage/weekly`,
  `/api/user` y `/api/rates` responden **404** JSON. Es decir, hay un único endpoint
  agregado, no uno por ventana.
- **Preflight CORS bloqueado**: `OPTIONS /api/usage` → **405 Method Not Allowed** en JSON,
  sin headers `Access-Control-*`. El endpoint no está diseñado para clientes web de
  terceros; consumirlo externamente exige un script propio (curl/Python) con credenciales.
- **`GET https://ollama.com/settings` sin sesión → HTTP 303** a `/signin`: la página del
  medidor requiere sesión web.
- **Qué credencial acepta no es verificable sin cuenta.** El 401 es idéntico con o sin
  `Authorization: Bearer` (probado con valor dummy `oml-invalid-key-probe`: mismo cuerpo
  `{"error":"invalid credentials"}`), así que no se puede distinguir por respuesta si
  admite API key, cookie de sesión, o ambas. Lo relevante: la API de `ollama.com` se
  documenta con `Authorization: Bearer $OLLAMA_API_KEY`
  ([docs.ollama.com/api/authentication](https://docs.ollama.com/api/authentication.md),
  keys en `ollama.com/settings/keys`), y `{"error":"invalid credentials"}` es el formato
  de error de esa API. Dato (inferencia) vs. hecho: que el `401` no cambie con un Bearer
  inválido es compatible con "acepta Bearer pero esta key es inválida" — no lo confirma.
- **Evidencia de terceros**: el único cliente externo conocido que lee el medidor,
  [dzackgarza/usage-limits](https://github.com/dzackgarza/usage-limits/blob/main/src/usage_limits/providers/ollama.py),
  **no usa API key**: hace *scrape* del HTML de `https://ollama.com/settings` con las
  cookies de Chromium del usuario (`browser_cookie3`) y `allow_redirects=False`.
  Confirmación de que la vía probada externamente es la **cookie de sesión web**, no una key.
- Contexto de demanda insatisfecha: [ollama/ollama#15663](https://github.com/ollama/ollama/issues/15663)
  pedía exactamente esto (quota vía headers o body de la API) y fue **cerrado como
  duplicado el 2026-05-23 sin implementación ni respuesta de maintainer**;
  [#17223](https://github.com/ollama/ollama/issues/17223) (dashboard, abierto) y
  [#17639](https://github.com/ollama/ollama/issues/17639) (abierto) siguen pidiendo
  lectura programática. No existe endpoint de cuota documentado.

## 2. ¿Qué campos y unidades expone?

Fuente primaria: el HTML de `ollama.com/settings`, decodificado por el userscript local
`~/Documentos/srnoob2570/ollama-usage-breakdown/` ([ollama-usage-breakdown.user.js](https://github.com/srnoob2570/ollama-usage-breakdown/blob/main/ollama-usage-breakdown.user.js),
v1.3.7) e independiente por el scraper de dzackgarza. Coinciden selector por selector.

| Dato | Dónde vive en el HTML | Unidad real |
|---|---|---|
| Total usado del período | `div[data-usage-track]` → `aria-label` `"Session usage N.N%"` / `"Weekly usage N.N%"` (dzackgarza) | **% de la cuota** de la ventana (1 decimal) |
| Desglose por modelo | `div[data-usage-segment]` por modelo: `data-model` (nombre), `data-requests` (entero), `aria-label` `"modelo: N requests"`, `style.width` (ancho % del total usado) | share ∝ ancho; **nº de requests entero por modelo** |
| Ventanas | dos tracks: **sesión 5 h** y **semanal 7 días** | períodos fijos |
| Reset | `.local-time[data-time]` junto a cada meter (ISO 8601 absoluto) | timestamp |
| Lo que NO hay | — | **ni GPU-segundos, ni tokens, ni dólares** en el medidor |

- El README del userscript lo resume: *"Ollama only reports the overall 'X% used' and the
  request counts. Each model's share exists only in the page HTML, encoded as bar segment
  widths."* El porcentaje por modelo se recalcula como `ancho_del_segmento × %_total`
  (ejemplo citado: 84.2 % de una sesión al 10.7 % → 9.01 % de la cuota).
- Crucial según el README: *"Percentages are read from Ollama's page (bar segment widths),
  **not from a private API**."* El userscript no hace ninguna llamada (`@grant none`,
  cero red). El nombre `data-usage-*` es del markup de Ollama, del que extrae con
  `MutationObserver` porque la página "se re-renderiza in place" (htmx).
- Testimonio de usuario sobre el medidor en vivo
  ([ollama/ollama#17639](https://github.com/ollama/ollama/issues/17639), 2026-08-09):
  "Session usage: 0%", "Weekly usage: ~68.5% remaining", "Models used this week includes
  `glm-5.2` with **thousands of requests**", más "Extra usage balance: $0". Coherente con
  la tabla de arriba (la ventana semanal también lista request counts por modelo).
- La API de inferencia no aporta cuota: `docs.ollama.com/api/usage.md` documenta la
  "usage" como métricas **por-request** (`prompt_eval_count`, `eval_count`, duraciones en
  ns) — nada de cuenta. Y en las respuestas cloud no hay headers de cuota (hecho central
  de [#15663](https://github.com/ollama/ollama/issues/15663): "Response headers also
  contain no quota metadata").
- El medidor session/weekly es exclusivo del **sistema legado**: la página de pricing dice
  de los planes migrados que *"the session and weekly limits of the old plans no longer
  apply"* ([ollama.com/pricing](https://ollama.com/pricing)); los planes nuevos se miden en
  créditos $ y tokens. El plan Max legado congelado del estudio sigue mostrándolos.

## 3. ¿Los deltas son atribuibles a requests individuales o agregados?

**Agregados con retardo, con dos ayudas parciales para la atribución** (no un contador
por-request):

1. **Resolución del total: 1 decimal de % por track.** El `aria-label` da el total con
   una cifra decimal (10.7 % en el ejemplo del README). El incremento mínimo observable
   entre dos lecturas es ~0.1 % de la cuota del período. Sobre la ventana de 7 días esa
   resolución es gruesa: puede corresponder a muchas requests cortas o ninguna (si bien
   el medidor puede refrescar con más precisión interna, lo publicado queda redondeado).
2. **Anchos de segmento: más resolución interna por modelo.** La anchura
   viene de `style.width` con decimales y el userscript la reescala a `% de cuota` con 2
   decimales; ese es el mejor proxy de magnitud por modelo. Pero es una fracción del
   total redondeado, no un contador absoluto independiente.
3. **Request counts por modelo: cardinalidad exacta.** `data-requests` / `aria-label`
   dan el **número entero de requests por modelo** en la sesión (y la lista nativa
   semanal también las muestra, #17639). Esto permite saber *cuántas* requests hizo cada
   modelo entre N-1 y N mediciones — la magnitud se divide entre ellas a posteriori.
4. **Lag de backend: no verificable por fuentes públicas.** Que la página se actualiza
   via htmx sin recargar es un hecho (README: *"Survives htmx updates"*); cuánto tarda el
   backend en reflejar una request acabada en el `%` servido no lo documenta nadie. Es
   exactamente lo que debe medir el ticket hijo de verificación en vivo.

Conclusión: el delta **por-request estricto** (lectura → 1 request → lectura =
% exacto de esa request) no está garantizado en ningún dato público; lo observable es un
**delta agregado por modelo** (Δ% total + Δrequests por modelo), donde la imputación a
cada request individual es un redondeo de resolución 0.1 % (mejor caso) y está sujeto a
un lag de actualización no cuantificado.

## 4. ¿Polling frecuente tiene límites?

- **No documentado.** Ni docs.ollama.com ni la FAQ de `ollama.com/pricing` publican rate
  limits de `/api/usage` ni de `/settings` (la palabra "usage" en docs.ollama.com solo
  existe como métricas por-request; en pricing, como créditos/tokens). Distinguir del
  **429 de inferencia** por uso excesivo, que sí existe y es lo único documentado por la
  comunidad (p. ej. el PR comunitario "wait out Ollama 429 rate/usage limits").
- El sondeo no autenticado no puede medir el rate limit real (probarlo exigiría sesionar
  y golpear el endpoint). Señales indirectas: la infra es Google Frontend (headers
  `x-cloud-trace-context`), y `OPTIONS` está prohibido explícitamente (405), lo que
  sugiere superficie mínima. **No verificable**: umbral de 429/403 del endpoint.
- Indirecta de prudencia de terceros: ollamatps.com muestrea la inferencia "about hourly
  to avoid burning through the weekly free-tier balance" — nadie publica mediciones de
  polling del medidor; el consumo de cuota viene de las requests de inferencia, no
  (que sepamos) de leer el medidor.

## Implicaciones para la metodología

**Qué es medible**: la unidad de cuenta observable del plan legado es el **% de cuota**
por ventana (sesión 5 h y semanal 7 días), con **desglose por modelo** (share + nº de
requests) y **timestamp de reset** absoluto (`data-time`). GPU-segundos y tokens **no
son observables** en ninguna fuente pública (confirma el glosario: la tarifa de GPU-time
nunca se publicó).

**Qué esquema de delta es viable**:

1. **Por-request estricto: NO viable de forma fiable.** El medidor publica % con 1
   decimal y backend con lag desconocido; no hay garantía de que dos lecturas alrededor
   de una única request la aíslen. Cualquier estimación por-request dependería de
   espaciar las requests y reconciliar ventanas, y seguiría siendo una estimación.
2. **Delta agregado con lag: viable y recomendado.** Polleando las dos ventanas se
   registran `(timestamp, %_total, share_ por modelo, n_requests por modelo, reset_at)`
   por track; el delta entre polls atribuye el consumo a nivel de **modelo + tramo de
   tiempo**, con cardinalidad exacta de requests (Δrequests) y magnitud agregada (Δ%).
   Para el bench: ejecutar un workload, esperar el refresh, tomar snapshot — los Δ%
   con Δrequests son la observación primaria ("uso medido" del glosario).
3. **El nº entero de requests por modelo (`data-requests`) es el conector clave** para
   la atribución fina: dado un tramo con 1 modelo y Δrequests = 1, el Δ% de ese tramo
   es el coste de esa request (con resolución 0.1 % del total — suficiente para
   workloads T1 micro? se decide con datos del ticket de verificación en vivo).
4. **Vía técnica del cliente externo**: cookie de sesión web (la que usa la comunidad);
   las API keys autentican la inferencia y no hay evidencia de que `/api/usage` las
   acepte. Sin CORS: polling desde script propio (curl/Python con cookies), no desde
   una página. Mejor aún, dentro del propio navegador: el userscript ya decodifica todo
   el DOM y puede exportar snapshots sin tocar la red.
5. **Pendiente del ticket de verificación en vivo** (hijo de este issue): (a) lag real
   del medidor tras una request; (b) rate limit efectivo del polling; (c) confirmación
   de si `Authorization: Bearer <key>` (o solo cookie) abre `/api/usage`, y de paso su
   JSON — nunca observado por nosotros, solo inferido del 401 y del flujo htmx.

## Fuentes

- Sondeo propio sin credenciales (2026-09-01): `401 {"error":"invalid credentials"}` en
  `/api/usage`; 404 en variantes; 405 OPTIONS; 303 `/settings` → `/signin`.
- Userscript local `~/Documentos/srnoob2570/ollama-usage-breakdown/ollama-usage-breakdown.user.js`
  (v1.3.7, repo [srnoob2570/ollama-usage-breakdown](https://github.com/srnoob2570/ollama-usage-breakdown))
  + su [README](https://github.com/srnoob2570/ollama-usage-breakdown#what-it-does).
- [dzackgarza/usage-limits — providers/ollama.py](https://github.com/dzackgarza/usage-limits/blob/main/src/usage_limits/providers/ollama.py)
  (scrape con cookies de Chromium; `aria-label` "Session/Weekly usage N%"; `div[data-time]`).
- [docs.ollama.com/api/usage.md](https://docs.ollama.com/api/usage.md) (usage = métricas
  por-request, ns/counts) · [docs.ollama.com/api/authentication.md](https://docs.ollama.com/api/authentication.md)
  (Bearer key para ollama.com) · [docs.ollama.com/llms.txt](https://docs.ollama.com/llms.txt)
  (índice: no hay página de "usage meter" ni de rate limits de cloud).
- [ollama/ollama#15663](https://github.com/ollama/ollama/issues/15663) (petición de cuota
  via API; cerrado como duplicado) · [#17223](https://github.com/ollama/ollama/issues/17223)
  (dashboard; abierto) · [#17639](https://github.com/ollama/ollama/issues/17639)
  (medidor leído a mano; abierto).
- [ollama.com/pricing](https://ollama.com/pricing) ("session and weekly limits of the old
  plans no longer apply") · [ollamatps.com](https://ollamatps.com) (solo referencias
  indirectas a su propio muestreo horario).