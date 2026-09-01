# Línea base de pricing — los dos sistemas (2026-08-31)

Fuente del día de la transición: [ollama.com/pricing](https://ollama.com/pricing) (snapshot en
[`pricing-snapshot/`](./pricing-snapshot/)) y el post oficial
[ollama.com/blog/transparent-pricing](https://ollama.com/blog/transparent-pricing) del 2026-08-31.
Marcado explícito de lo **no verificable** en la sección final.

## 1. Plan nuevo (token-based) — vigente para nuevos signups

| Plan | Precio | Créditos incluidos/mes | Concurrencia |
|---|---|---|---|
| Free | $0 | "starter usage credits" (monto no publicado) | 1 |
| Pro | $20/mes (o $200/año = $16.67/mes) | $60 | 3 |
| Max | $100/mes | $300 | 10 |
| Team | $500/mes | $1,000 (compartido, usuarios ilimitados) | 10 |
| Enterprise | custom | volume pricing | custom |

- La facturación es **por tokens a la tarifa de cada modelo** ("Usage is measured in tokens
  at each model's rates" — FAQ oficial).
- Los créditos **no se acumulan** ("unused credits do not roll over"); se refrescan en el
  reset mensual. Agotados, se dibuja del balance extra pay-as-you-go (disponible incluso en Free).
- Pro anuncia **"Fast mode (coming soon)"**: velocidad por plan anunciada, no implementada;
  hoy "Speed depends on model size, architecture, and hardware optimization. […] Priority
  tiers with faster performance may be available in the future."
- La FAQ dice que los limites viejos de 5 h de sesión y 7 días semanales **ya no aplican**
  a los planes nuevos.

### Tabla oficial por 1M tokens (input / cached input / output)

| Modelo | Input | Cached input | Output |
|---|---|---|---|
| deepseek-v4-flash | $0.44 | $0.014 | $1.32 |
| deepseek-v4-pro | $1.32 | $0.044 | $3.96 |
| gemma4 | $0.14 | $0.05 | $0.40 |
| glm-5.3 | $1.40 | $0.26 | $4.40 |
| glm-5.3-flash | $0.15 | $0.03 | $0.50 |
| glm-5.2 | $1.40 | $0.26 | $4.40 |
| glm-5.1 | $1.00 | $0.20 | $3.20 |
| gpt-oss:120b | $0.15 | $0.014 | $0.60 |
| gpt-oss:20b | $0.07 | $0.035 | $0.30 |
| kimi-k3 | $3.00 | $0.30 | $15.00 |
| kimi-k2.7-code | $0.95 | $0.19 | $4.00 |
| kimi-k2.6 | $0.95 | $0.16 | $4.00 |
| minimax-m3 | $0.60 | $0.12 | $2.40 |
| minimax-m2.7 | $0.30 | $0.06 | $1.20 |
| mistral-large-3 | $0.50 | $0.50 | $1.50 |
| nemotron-3-nano | $0.06 | $0.06 | $0.24 |
| nemotron-3-super | $0.015 | $0.015 | $0.60 |
| nemotron-3-ultra | $0.10 | $0.10 | $3.00 |
| qwen3.5:397b | $0.60 | $0.60 | $3.60 |

## 2. Sistema legado — GPU-time (estado del suscriptor existente a 2026-08-31)

- Los suscriptores existentes de Pro/Max/Team "**remain on your current plan**" y pueden
  migrar voluntariamente en billing settings; los nuevos signups ya entran al sistema nuevo.
- El plan legado del dueño del estudio: **Max bajo GPU-time** (única cuenta disponible — ver
  guardarraíl: no migrar durante la recolección).
- **No verificable**: cuánto GPU-time representa la cuota (% sesión 5 h / % semanal 7 días/
  niveles por modelo); nunca se publicó la tarifa en $ ni el mapeo nivel → GPU-segundos.
  La frase oficial eliminada del dashboard decía: *"Usage reflects actual utilization of
  Ollama's cloud infrastructure – primarily GPU time, which depends on model size and
  request duration"* (citada por [ollamatps.com/limits](https://ollamatps.com/limits/) y
  [BSWEN](https://docs.bswen.com/blog/2026-04-20-what-is-ollama-cloud/)).
- La API por request reporta `prompt_eval_count`, `eval_count` y duraciones en ns
  (`total_duration`, `load_duration`, `prompt_eval_duration`, `eval_duration`)
  ([docs.ollama.com/api/usage.md](https://docs.ollama.com/api/usage.md)); en streaming los
  usage fields llegan en el último chunk (`done: true`). **No documentado**: GPU-time por
  request, costo facturado por request, tokens cacheados, cabeceras de cuota, webhooks
  (issue [15663](https://github.com/ollama/ollama/issues/15663) sigue abierto; inconsistencia
  de billing `402 "extra usage only"` en
  [17639](https://github.com/ollama/ollama/issues/17639)).

## 3. Términos de migración

- Migración **voluntaria** para suscriptores existentes (billing settings); irreversible.
- Al cambiar de plan: "Your usage is reset: the new plan's full monthly amount is available
  right away."
- **No declarado** en ningún lado: período de doble facturación o prorrateo; destino de los
  créditos extra ya comprados bajo el sistema legado; política de reembolsos.

## 4. Justificación pública de Ollama

Post oficial del 2026-08-31 (`/blog/transparent-pricing`):

> "GPU-time based billing was difficult to predict, especially as open models have grown
> much larger (Kimi K3 has 2.8 trillion parameters)."

Además: "no service fees and no 5-hour or weekly limits"; costo por request visible en
account settings. Contexto previo de presión (no es quote de Ollama):

- [Issue 17435](https://github.com/ollama/ollama/issues/17435) — "Usage quota silently
  slashed ~70% with ZERO notification" (GPU-time "completely opaque" para un usuario Pro anual).
- [Issue 15663](https://github.com/ollama/ollama/issues/15663) — pedido de exponer cuota/uso
  por API; "the outlier" frente a OpenAI/Anthropic.
- [Issue 15741](https://github.com/ollama/ollama/issues/15741) — modelos grandes detrás de un
  paywall 403 sin anuncio previo.

## 5. Comparables open-weights servidos en la nube

Verificado para **Kimi K2 Thinking** (1T params, open-weights, INT4) — fuentes secundarias
(agregadores), no páginas de precio en vivo:

| Proveedor | Input | Output |
|---|---|---|
| Moonshot AI (directo) | $0.60 | $2.50 |
| Fireworks AI | $0.60 | $2.50 |
| Together AI | $1.20 | $4.00 |
| OpenRouter | $0.60 (+$0.15 cache) | $2.50 |

El modelo comparable directo hoy en Ollama sería kimi-k2.6 ($0.95/$4.00); **no hay datos
públicos comparables para kimi-k3**. Sin verificar en esta pasada: Gemini API y DeepSeek directos.

## 6. No verificable (explícito)

- Tarifa en $ del GPU-time del sistema legado y el mapeo cuota % → GPU-segundos ($ de un
  "100 %" del plan legado): nunca publicado.
- Monto exacto de los "starter credits" del plan Free.
- Política sobre créditos extra ya comprados al migrar; prorrateo/doble facturación.
- TPM/RPM/tok/s publicados por plan: no existen; los reportes de velocidad (8–22 tok/s en
  modelos pesados) son anecdóticos de terceros.

## Sources

- [ollama.com/pricing](https://ollama.com/pricing) · snapshot local: [`pricing-snapshot/`](./pricing-snapshot/)
- [ollama.com/blog/transparent-pricing](https://ollama.com/blog/transparent-pricing)
- [docs.ollama.com/api/usage.md](https://docs.ollama.com/api/usage.md) · [docs.ollama.com/cloud](https://docs.ollama.com/cloud) · [docs.ollama.com/api/openai-compatibility.md](https://docs.ollama.com/api/openai-compatibility.md)
- [ollamatps.com/limits](https://ollamatps.com/limits/) · [docs.bswen.com](https://docs.bswen.com/blog/2026-04-20-what-is-ollama-cloud/)
- Issues [17435](https://github.com/ollama/ollama/issues/17435) · [15663](https://github.com/ollama/ollama/issues/15663) · [15741](https://github.com/ollama/ollama/issues/15741) · [17223](https://github.com/ollama/ollama/issues/17223) · [17639](https://github.com/ollama/ollama/issues/17639)
- [fireworks.ai kimi-k2-thinking](https://fireworks.ai/models/fireworks/kimi-k2-thinking) · [docs.together.ai](https://docs.together.ai/docs/kimi-k2-thinking-quickstart) · [cloudprice.net](https://cloudprice.net/models/moonshot-kimi-k2-thinking) · [llmreference.com](https://www.llmreference.com/model/kimi-k2-thinking/providers) · [threatfrontier.com](https://threatfrontier.com/articles/ollama-cloud-slowdown-hosted-open-model-performance)