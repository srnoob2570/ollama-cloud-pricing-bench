# Comparables de precios externos — proveedores open-weights (2026-09-01)

Responde al issue [#15](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/15)
(“Comparables de precios externos (open-weights)”; el issue lo llama
`comparables-pricing.md`, este archivo es su implementación). Alimenta la fila
`H_comparables` de la matriz de incentivos y el break-even del estudio.

Base de comparación: la tabla oficial del plan nuevo del 2026-08-31
([`base-pricing-2026-08-31.md`](./base-pricing-2026-08-31.md)). Fecha de captura de los
comparables: **2026-08-31 / 2026-09-01**. Un solo precio por celda, siempre con su fuente;
lo no verificable está marcado explícitamente en su sección.

## Método

- **Fuente primaria primero**: páginas de pricing y docs de cada proveedor, leídas
  directamente. Los agregadores (DeepInfra, AI//COST, tokencost, benchlm, layer3labs,
  computeprices) se usaron solo como respaldo y **van marcados “secundaria”**.
- **Páginas que exigían JS / no legibles** y cómo se resolvieron: `openrouter.ai/<modelo>`
  (web JS-heavy → se usó su endpoint JSON público `GET /api/v1/models`, que es fuente de
  primera parte); `groq.com/pricing` y `console.groq.com/docs/price` (landing sin tabla /
  404 → agregadores); `build.nvidia.com` (timeout → agregadores); consola de rate limits de
  z.ai (requiere login → “no público”); página de precios de Alibaba `model-pricing` legible
  (las fichas `#/doc/...` JS-heavy se evitaron); `platform.moonshot.ai` → 301 a
  `platform.kimi.ai`.
- Todo en **USD por 1M tokens** salvo indicación (los precios CNY se reportan tal cual).
- Convención de la columna “precio”: **input / output** $/1M. La columna “cache” es la
  tarifa de cached input (o el descuento publicado). “s/p” = sin precio público/verificado.
- Los ocho proveedores de referencia (el issue pide 4–6): **Moonshot directo, z.ai directo,
  MiniMax directo, DeepSeek directo, Alibaba Model Studio, Fireworks, Together,
  OpenRouter**, más hosts notables para familias huérfanas (Groq, Cerebras, Mistral,
  DeepInfra, Gemini API, Bitdeer/NIM).

---

## 1. Kimi — Ollama: `kimi-k3` · `kimi-k2.7-code` · `kimi-k2.6`

| Modelo Ollama | Tarifa Ollama (in/cached/out) | Proveedor comparable | Precio (in/out $/1M) | Cache | ¿GPU-time? | Fuente |
|---|---|---|---|---|---|---|
| kimi-k3 | $3.00 / $0.30 / $15.00 | **Moonshot directo** (`kimi-k3`, ctx 1M) | $3.00 / $15.00 | $0.30 (−90%) | No — por token | [platform.kimi.ai/docs/pricing/chat-k3](https://platform.kimi.ai/docs/pricing/chat-k3) |
| kimi-k3 | ídem | Fireworks — `kimi-k3` | $3.00 / $15.00 | $0.30 (Priority ×1.25: $3.75/$18.75; Fast $4.50/$22.50) | No (dedicated: GPU-hora $8–20) | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| kimi-k3 | ídem | OpenRouter — `moonshotai/kimi-k3` | $3.00 / $15.00 | $0.30 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) (JSON oficial) |
| kimi-k3 | ídem | Together — `moonshotai/Kimi-K3` (serverless) | $3.00 / $15.00 | $0.30 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| kimi-k3 | ídem | DeepInfra — `kimi-k3` | $2.85 / $14.25 | $0.285 | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secundaria) |
| kimi-k2.7-code | $0.95 / $0.19 / $4.00 | Moonshot directo — `kimi-k2.7-code` | $0.95 / $4.00 | $0.19 (−80%); highspeed: $1.90/$8.00 | No | [platform.kimi.ai/docs/pricing/chat-k27-code](https://platform.kimi.ai/docs/pricing/chat-k27-code) |
| kimi-k2.7-code | ídem | Fireworks — `kimi-k2.7-code` | $0.95 / $4.00 | $0.19 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| kimi-k2.7-code | ídem | Together — `moonshotai/Kimi-K2.7-Code` | $0.95 / $4.00 | $0.19 | No (precio publicado pero **no en serverless**: “launching soon”; Dedicated por GPU-hora) | [together.ai/models/kimi-k27-code](https://www.together.ai/models/kimi-k27-code) |
| kimi-k2.6 | $0.95 / $0.16 / $4.00 | Moonshot directo — `kimi-k2.6` | $0.95 / $4.00 | $0.16 (−83%) | No | [platform.kimi.ai/docs/pricing/chat-k26](https://platform.kimi.ai/docs/pricing/chat-k26) |
| kimi-k2.6 | ídem | Fireworks — `kimi-k2p6` | $0.95 / $4.00 | $0.16 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| kimi-k2.6 | ídem | OpenRouter — `moonshotai/kimi-k2.6` | $0.95 / $4.00 | $0.16 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| kimi-k2.6 | ídem | Together — `moonshotai/Kimi-K2.6` | $1.20 / $4.50 | $0.20 | No (precio publicado pero **no en serverless**: “launching soon”; Dedicated por GPU-hora) | [together.ai/models/kimi-k26](https://www.together.ai/models/kimi-k26) |
| kimi-k2.6 | ídem | DeepInfra — `kimi-k2.6` | $0.75 / $3.50 | $0.15 | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secundaria) |
| (kimi-k2.6 como análogo histórico) | ídem | OpenRouter — `moonshotai/kimi-k2-thinking` | $0.60 / $2.50 | $0.15 | No | ídem |

Límites de Moonshot publicados (por recarga acumulada de la cuenta, todos los modelos):
Tier0 ($1): 1 concurrente / 3 RPM / 0.5M TPM; Tier5 ($3,000): 1,000 concurrentes / 10,000 RPM
/ 5M TPM ([platform.kimi.ai/docs/pricing/limits](https://platform.kimi.ai/docs/pricing/limits.md)).
K2 y K2-thinking están **retirados** del catálogo de Moonshot (2026-05-25) y K2.5 el 2026-08-31;
el precio K2-thinking que circula ($0.60/$2.50) sobrevive solo en terceros (OpenRouter).

## 2. DeepSeek — Ollama: `deepseek-v4-flash` · `deepseek-v4-pro`

| Modelo Ollama | Tarifa Ollama (in/cached/out) | Proveedor comparable | Precio (in/out $/1M) | Cache | ¿GPU-time? | Fuente |
|---|---|---|---|---|---|---|
| deepseek-v4-flash | $0.44 / $0.014 / $1.32 | **DeepSeek directo** — `deepseek-v4-flash` (V4-Flash-0731) | $0.44 / $1.32 pico; **off-peak −50%** ($0.22/$0.66) | $0.014 pico ($0.007) | No — por token | [api-docs.deepseek.com/quick_start/pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| deepseek-v4-flash | ídem | Fireworks — `deepseek-v4-flash-0731` | $0.22 / $0.66 | $0.007 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| deepseek-v4-flash | ídem | OpenRouter — `deepseek/deepseek-v4-flash-0731` | $0.065 / $0.18 | $0.016 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| deepseek-v4-flash | ídem | Together — `deepseek-ai/DeepSeek-V4-Flash-0731` (serverless) | $0.14 / $0.28 | $0.03 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| deepseek-v4-flash | ídem | DeepInfra — `deepseek-v4-flash-0731` | $0.08 / $0.18 | $0.016 | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secundaria) |
| deepseek-v4-pro | $1.32 / $0.044 / $3.96 | **DeepSeek directo** — `deepseek-v4-pro` (V4-Pro-0813) | $1.32 / $3.96 pico (off-peak −50%) | $0.044 pico ($0.022) | No | [api-docs.deepseek.com/quick_start/pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| deepseek-v4-pro | ídem | Fireworks — `deepseek-v4-pro-0813` | $1.32 / $3.96 | $0.044 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| deepseek-v4-pro | ídem | OpenRouter — `deepseek/deepseek-v4-pro-0813` | $0.66 / $1.98 | $0.022 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| deepseek-v4-pro | ídem | Together — `deepseek-ai/DeepSeek-V4-Pro-0813` (serverless) | $1.32 / $3.96 | $0.13 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| deepseek-v4-pro | ídem | DeepInfra — `deepseek-v4-pro` | $1.30 / $2.60 | $0.10 | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secundaria) |

Concurrencia DeepSeek (publicada): solo por conexiones concurrentes por cuenta —
**2,500** (flash) / **500** (pro) —; no publica RPM/TPM
([api-docs.deepseek.com/quick_start/rate_limit](https://api-docs.deepseek.com/quick_start/rate_limit)).
Off-peak (−50% en las tres columnas): 01:00–04:00 y 06:00–10:00 UTC, L–V.

## 3. GLM — Ollama: `glm-5.3` · `glm-5.3-flash` · `glm-5.2` · `glm-5.1`

| Modelo Ollama | Tarifa Ollama (in/cached/out) | Proveedor comparable | Precio (in/out $/1M) | Cache | ¿GPU-time? | Fuente |
|---|---|---|---|---|---|---|
| glm-5.3 | $1.40 / $0.26 / $4.40 | **z.ai directo** — GLM-5.3 | $1.40 / $4.40 | $0.26 | No — por token | [docs.z.ai/guides/overview/pricing](https://docs.z.ai/guides/overview/pricing) |
| glm-5.3 | ídem | Fireworks — `glm-5.3` | $1.40 / $4.40 | $0.26 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| glm-5.3 | ídem | Together — `zai-org/GLM-5.3` (serverless) | $1.40 / $4.40 | $0.26 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| glm-5.3 | ídem | OpenRouter — `z-ai/glm-5.3` | $1.40 / $4.40 | $0.26 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| glm-5.3-flash | $0.15 / $0.03 / $0.50 | z.ai directo — GLM-5.3-Flash | $0.15 / $0.50 **lista**; promo −50% ($0.075/$0.25) hasta 2026-09-09 | $0.03 (promo $0.015) | No | [docs.z.ai/guides/overview/pricing](https://docs.z.ai/guides/overview/pricing) |
| glm-5.3-flash | ídem | Fireworks — `glm-5.3-flash` | $0.15 / $0.50 | $0.03 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| glm-5.3-flash | ídem | Together — `zai-org/GLM-5.3-Flash` (serverless) | $0.15 / $0.50 | $0.03 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| glm-5.3-flash | ídem | OpenRouter — `z-ai/glm-5.3-flash` | $0.075 / $0.25 (promo) | $0.015 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| glm-5.2 | $1.40 / $0.26 / $4.40 | z.ai directo — GLM-5.2 | $1.40 / $4.40 | $0.26 | No | [docs.z.ai/guides/overview/pricing](https://docs.z.ai/guides/overview/pricing) |
| glm-5.2 | ídem | Fireworks — `glm-5.2` | $1.40 / $4.40 | $0.14 (Fast: $2.10/$6.60) | No | ídem |
| glm-5.2 | ídem | Together — `zai-org/GLM-5.2` (serverless) | $1.40 / $4.40 | $0.26 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| glm-5.2 | ídem | OpenRouter — `z-ai/glm-5.2` | $1.19 / $3.74 | $0.22 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| glm-5.2 | ídem | Mistral La Plateforme (revende GLM 5.2) | $1.40 / $4.40 | $0.14 | No | [mistral.ai/pricing/api](https://mistral.ai/pricing/api) (primaria) |
| glm-5.1 | $1.00 / $0.20 / $3.20 | z.ai directo — GLM-5 | $1.00 / $3.20 | $0.20 | No | [docs.z.ai/guides/overview/pricing](https://docs.z.ai/guides/overview/pricing) |
| glm-5.1 | ídem | Together — GLM 5.1 | s/p (solo Dedicated Endpoints, sin precio por token publicado) | — | No (Dedicated: GPU-hora) | [docs.together.ai/docs/dedicated-endpoints/models](https://docs.together.ai/docs/dedicated-endpoints/models) |
| glm-5.1 | ídem | OpenRouter — `z-ai/glm-5.1` | $0.97 / $3.04 | $0.18 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |

Nota de mapeo: la tabla de z.ai agrupa “GLM-5.2/5.1” a $1.40/$4.40 y lista “GLM-5” a
$1.00/$3.20; el análogo exacto de `glm-5.1` de Ollama es ambiguo entre esas dos filas.
Límites z.ai: la página oficial de rate limits exige login → **no público**; la doc dice solo
“límites dinámicos por usuario/plan” (único número circulante, secundario: “GLM-5.2
concurrency 10”, [github.com/zai-org/GLM-5/issues/83](https://github.com/zai-org/GLM-5/issues/83)).
El Coding Plan de z.ai (DevPack, desde $18/mes) consume **créditos** con ventanas rolling 5h
+ semanal — el pariente estructural más cercano del esquema legado de Ollama, pero medido en
créditos por tokens, no en GPU-time ([docs.z.ai/devpack/overview.md](https://docs.z.ai/devpack/overview.md)).

## 4. Qwen — Ollama: `qwen3.5:397b`

| Modelo Ollama | Tarifa Ollama (in/cached/out) | Proveedor comparable | Precio (in/out $/1M) | Cache | ¿GPU-time? | Fuente |
|---|---|---|---|---|---|---|
| qwen3.5:397b | $0.60 / $0.60 / $3.60 | **Alibaba Model Studio (Intl.)** — `qwen3.5-397b-a17b` | $0.60 / $3.60 (Singapur; Beijing/US/Alemania: $0.172 ≤128K / $0.43 128–256K) | **no público/verificado** (regla general de la casa: cache hit = 10% del input; sin cache para este modelo) | No — por token | [model-pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing) · [qwen3.5-397b-a17b](https://www.alibabacloud.com/help/en/model-studio/qwen3-5-397b-a17b) |
| qwen3.5:397b | ídem | Together — `Qwen/Qwen3.5-397B-A17B` (serverless) | $0.60 / $3.60 | $0.35 (−42%) | No | [together.ai/models/qwen3-5-397b-a17b](https://www.together.ai/models/qwen3-5-397b-a17b) |
| qwen3.5:397b | ídem | OpenRouter — `qwen/qwen3.5-397b-a17b` | $0.39 / $2.34 | no público | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| qwen3.5:397b | ídem | Fireworks | s/p (no ofertado en serverless; sí `qwen3.8-max` $2.00/$6.00 y `qwen-3.7-plus` $0.40/$1.60) | — | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| (referencia flagship) | — | Alibaba — `qwen3.8-max` | $2.00 / $6.00 (Singapur) | hit = 10% del input | No | [model-pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing) |

Límites Alibaba publicados (regionales): `qwen3.5-397b-a17b` **600 RPM / 1M TPM** en todas
las regiones; los tier cerrados (3.7-plus, 3.8-max/flash) 15,000–30,000 RPM / 2–5M TPM
([rate-limit](https://www.alibabacloud.com/help/en/model-studio/rate-limit)).

## 5. MiniMax — Ollama: `minimax-m3` · `minimax-m2.7`

| Modelo Ollama | Tarifa Ollama (in/cached/out) | Proveedor comparable | Precio (in/out $/1M) | Cache | ¿GPU-time? | Fuente |
|---|---|---|---|---|---|---|
| minimax-m3 | $0.60 / $0.12 / $2.40 | **MiniMax directo** — M3, input ≤512k | $0.30 / $1.20 (−50% permanente; lista $0.60/$2.40) | cache read $0.06; 200 RPM / 10M TPM | No — por token (tier priority ×1.5) | [platform.minimax.io/docs/guides/pricing-paygo.md](https://platform.minimax.io/docs/guides/pricing-paygo.md) |
| minimax-m3 | ídem | MiniMax directo — M3, input >512k | $0.60 / $2.40 | $0.12 | No | ídem |
| minimax-m3 | ídem | Fireworks — `minimax-m3` | $0.30 / $1.20 (ctx 512K) | $0.06 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| minimax-m3 | ídem | Together — `MiniMaxAI/MiniMax-M3` (serverless) | $0.30 / $1.20 | $0.06 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| minimax-m3 | ídem | OpenRouter — `minimax/minimax-m3` | $0.30 / $1.20 | $0.06 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| minimax-m2.7 | $0.30 / $0.06 / $1.20 | MiniMax directo — M2.7 | $0.30 / $1.20 (highspeed $0.60/$2.40) | read $0.06; write $0.375; 500 RPM / 20M TPM | No | ídem + [rate-limits](https://platform.minimax.io/docs/guides/rate-limits.md) |
| minimax-m2.7 | ídem | Fireworks — `minimax-m2p7` | $0.30 / $1.20 | $0.059 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| minimax-m2.7 | ídem | Together — `MiniMaxAI/MiniMax-M2.7` (serverless) | $0.30 / $1.20 | $0.06 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| minimax-m2.7 | ídem | OpenRouter — `minimax/minimax-m2.7` | $0.30 / $1.20 | $0.06 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |

Nota: la tarifa Ollama de `minimax-m3` coincide con el **tramo >512k / precio de lista** de
MiniMax directo, el doble del precio efectivo que cobra MiniMax y sus revendedores en el
tramo estándar. Además del PAYGO, MiniMax vende un **Token Plan** de suscripción (Plus $22 /
Max $55 / Ultra $132) con ventanas 5h + semanales ([pricing-token-plan](https://platform.minimax.io/docs/guides/pricing-token-plan.md)).

## 6. gpt-oss (OpenAI open-weights) — Ollama: `gpt-oss:120b` · `gpt-oss:20b`

| Modelo Ollama | Tarifa Ollama (in/cached/out) | Proveedor comparable | Precio (in/out $/1M) | Cache | ¿GPU-time? | Fuente |
|---|---|---|---|---|---|---|
| gpt-oss:120b | $0.15 / $0.014 / $0.60 | Fireworks — `gpt-oss-120b` | $0.15 / $0.60 | $0.015 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| gpt-oss:120b | ídem | Groq — `gpt-oss-120b` | $0.15 / $0.60 | $0.075 | No | secundaria: [layer3labs](https://www.layer3labs.io/guides/groq-pricing) · [computeprices](https://computeprices.com/providers/groq/models/gpt-oss-120b) (página primaria no legible) |
| gpt-oss:120b | ídem | Cerebras — `gpt-oss-120b` | $0.35 / $0.75 | no publicado | No | [inference-docs.cerebras.ai/models/openai-oss](https://inference-docs.cerebras.ai/models/openai-oss) (primaria; 1,000 RPM / 1M input tok/min) |
| gpt-oss:120b | ídem | Together — `openai/gpt-oss-120b` (serverless) | $0.15 / $0.60 | no publicado | No | [together.ai/models/gpt-oss-120b](https://www.together.ai/models/gpt-oss-120b) |
| gpt-oss:120b | ídem | OpenRouter — `openai/gpt-oss-120b` | $0.037 / $0.17 | campo ausente | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| gpt-oss:20b | $0.07 / $0.035 / $0.30 | Together — `openai/gpt-oss-20b` (serverless) | $0.05 / $0.20 | no publicado | No | [together.ai/pricing](https://www.together.ai/pricing) |
| gpt-oss:20b | ídem | Groq — `gpt-oss-20b` | $0.075 / $0.30 | $0.0375 | No | secundaria (ídem) |
| gpt-oss:20b | ídem | OpenRouter — `openai/gpt-oss-20b` | $0.03 / $0.13 | $0.03 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| gpt-oss:20b | ídem | Fireworks | s/p (serverless “Not supported”; on-demand/dedicated solo) | — | on-demand: GPU-hora $8–20 | [fireworks.ai/models/fireworks/gpt-oss-20b](https://fireworks.ai/models/fireworks/gpt-oss-20b) |

## 7. Mistral Large — Ollama: `mistral-large-3`

| Modelo Ollama | Tarifa Ollama (in/cached/out) | Proveedor comparable | Precio (in/out $/1M) | Cache | ¿GPU-time? | Fuente |
|---|---|---|---|---|---|---|
| mistral-large-3 | $0.50 / $0.50 / $1.50 | **Mistral La Plateforme** — `mistral-large-latest` (“Large 3”) | $0.50 / $1.50 (batch −50%) | publicado solo como “reduce input cost up to 90%”, sin cifra absoluta | No — por token | [mistral.ai/pricing](https://mistral.ai/pricing) · [/pricing/api](https://mistral.ai/pricing/api) |
| mistral-large-3 | ídem | OpenRouter — `mistralai/mistral-large-2512` | $0.50 / $1.50 | $0.05 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| mistral-large-3 | ídem | Fireworks — `mistral-large-3-fp8` | s/p (serverless “Not supported”; agregadores citan $1.20/$1.20 sin verificación) | — | on-demand: GPU-hora | [fireworks.ai/models/fireworks/mistral-large-3-fp8](https://fireworks.ai/models/fireworks/mistral-large-3-fp8) |
| mistral-large-3 | ídem | Together | **no ofertado** (ni serverless ni dedicado; solo Mistral 7B v0.3 y Mixtral 8x7B legacy en Dedicated) | — | — | [docs.together.ai/docs/dedicated-endpoints/models](https://docs.together.ai/docs/dedicated-endpoints/models) |
| (vecino del catálogo) | — | Mistral — `mistral-medium-3.5` / `mistral-small-4` | $1.50/$7.50 · $0.15/$0.60 | no publicado | No | [mistral.ai/pricing/api](https://mistral.ai/pricing/api) |

## 8. Nemotron — Ollama: `nemotron-3-nano` · `nemotron-3-super` · `nemotron-3-ultra`

| Modelo Ollama | Tarifa Ollama (in/cached/out) | Proveedor comparable | Precio (in/out $/1M) | Cache | ¿GPU-time? | Fuente |
|---|---|---|---|---|---|---|
| nemotron-3-ultra | $0.10 / $0.10 / $3.00 | Fireworks — `nemotron-3-ultra` (NVFP4, preview) | $0.60 / $2.40 | $0.12 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| nemotron-3-ultra | ídem | OpenRouter — `nvidia/nemotron-3-ultra-550b-a55b` | $0.50 / $2.20 | $0.10 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| nemotron-3-ultra | ídem | Together — Nemotron 3 Ultra 550B | s/p (solo Dedicated, sin precio por token publicado) | — | No (Dedicated: GPU-hora) | [docs.together.ai/docs/dedicated-endpoints/models](https://docs.together.ai/docs/dedicated-endpoints/models) |
| nemotron-3-ultra | ídem | NVIDIA NIM (first-party) | **no público/verificado** — sin tarifa por token propia; evaluación gratuita; self-host por licencia NVIDIA AI Enterprise (cotización) | — | GPU-time solo vía licencia NVE (precio no público) | [build.nvidia.com](https://build.nvidia.com) (no legible) + [aicost.tools](https://aicost.tools/llm-cost/nvidia/) (secundaria) |
| nemotron-3-super | $0.015 / $0.015 / $0.60 | OpenRouter — `nvidia/nemotron-3-super-120b-a12b` | $0.085 / $0.40 | no público | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| nemotron-3-super | ídem | Fireworks | s/p (en catálogo sin precio visible; análogo ofertado: `nemotron-3.5-lightning-30b` $0.05/$0.20) | — | No | [fireworks.ai/models](https://fireworks.ai/models) · [serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| nemotron-3-nano | $0.06 / $0.06 / $0.24 | OpenRouter — `nvidia/nemotron-3-nano-30b-a3b` | $0.05 / $0.20 | $0.025 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |

## 9. Gemma — Ollama: `gemma4`

| Modelo Ollama | Tarifa Ollama (in/cached/out) | Proveedor comparable | Precio (in/out $/1M) | Cache | ¿GPU-time? | Fuente |
|---|---|---|---|---|---|---|
| gemma4 | $0.14 / $0.05 / $0.40 | **Gemini API (Google)** — Gemma 4 | **gratis** (paid tier: “Not available”) | free of charge | No | [ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| gemma4 | ídem | Together — `google/gemma-4-31B-it` (serverless) | $0.39 / $0.97 | no publicado | No | [together.ai/pricing](https://www.together.ai/pricing) |
| gemma4 | ídem | OpenRouter — `google/gemma-4-31b-it` | $0.09 / $0.34 | $0.05 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| gemma4 | ídem | DeepInfra — `gemma-4-31b-it` | $0.13 / $0.38 | no público | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secundaria) |
| gemma4 | ídem | Fireworks — `gemma-4-31b-it` | s/p (serverless “Not supported”; on-demand solo) | — | on-demand: GPU-hora $8–20 | [fireworks.ai/models/fireworks/gemma-4-31b-it](https://fireworks.ai/models/fireworks/gemma-4-31b-it) |

---

## Posición de Ollama en el rango de mercado (por familia)

| Familia (análogo) | Tarifa Ollama (in/out) | Rango de mercado (in/out, verificado) | Posición de Ollama |
|---|---|---|---|
| kimi-k3 | $3.00 / $15.00 | $2.85–$3.00 / $14.25–$15.00 | **Tope** — igual al precio directo de Moonshot, Fireworks y Together |
| kimi-k2.6 / k2.7-code | $0.95 / $4.00 | $0.75–$1.20 / $3.50–$4.50 | Tope — igual a Moonshot directo; DeepInfra 20% más barato; Together lista K2.6 a $1.20/$4.50 pero fuera de serverless |
| glm-5.3 | $1.40 / $4.40 | $1.19–$1.40 / $3.74–$4.40 | Tope — igual a z.ai directo, Fireworks y Together; OpenRouter −15% |
| glm-5.3-flash | $0.15 / $0.50 | $0.075–$0.15 / $0.25–$0.50 | Lista; z.ai vende a **mitad** por promo vigente |
| deepseek-v4-pro | $1.32 / $3.96 | $0.66–$1.32 / $1.98–$3.96 | Tope — igual al directo pico, Fireworks y Together; 2× OpenRouter/DeepInfra |
| deepseek-v4-flash | $0.44 / $1.32 | $0.065–$0.44 / $0.16–$1.32 | Tope — igual al directo pico; 2× Fireworks, 2.9× Together, 5–6× OpenRouter |
| glm-5.2 / glm-5.1 | $1.40 / $4.40 · $1.00 / $3.20 | $1.19–$1.40 / $3.74–$4.40 | Tope — igual a z.ai directo |
| minimax-m3 | $0.60 / $2.40 | $0.30–$0.60 / $1.20–$2.40 | **2× el precio efectivo** (MiniMax −50% permanente; Fireworks, Together y OpenRouter al precio descontado) |
| minimax-m2.7 | $0.30 / $1.20 | $0.30 / $1.20 | En el mercado (= directo) |
| qwen3.5:397b | $0.60 / $3.60 | $0.39–$0.60 / $2.34–$3.60 | Tope — igual a Alibaba Singapur y Together; OpenRouter −35% |
| gpt-oss:120b | $0.15 / $0.60 | $0.037–$0.35 / $0.17–$0.75 | En el medio-alto ( = Fireworks, Together y Groq; Cerebras +133%; OpenRouter −75%) |
| gpt-oss:20b | $0.07 / $0.30 | $0.03–$0.075 / $0.13–$0.30 | En el medio-alto (Together $0.05/$0.20; Groq secundario igual) |
| mistral-large-3 | $0.50 / $1.50 | $0.50 / $1.50 | En el mercado (= Mistral directo) |
| nemotron-3-ultra | $0.10 / $3.00 | $0.50–$0.80 / $2.20–$2.60 | **Outlier barato en input (5–8×)**, algo caro en output (+15–36%) |
| nemotron-3-super | $0.015 / $0.60 | $0.085 / $0.40 | Input 5.7× más barato; output 1.5× más caro |
| nemotron-3-nano | $0.06 / $0.24 | $0.05 / $0.20 | Cercano al mercado (+20%) |
| gemma4 | $0.14 / $0.40 | gratis (Gemini API) – $0.39/$0.97 (Together) | En el centro-bajo del rango de revendedores; Google lo da gratis |

## Esquemas no-por-token que siguen vivos (GPU-time y afines)

- **Ningún comparable factura por GPU-time su API compartida por token.** Los cinco
  proveedores directos (Moonshot, DeepSeek, z.ai, Alibaba, MiniMax) y todos los agregadores
  del rastreo facturan por token; Fireworks lo declara por token en serverless con las tres
  columnas in/cached/out — la misma estructura que adoptó el plan nuevo de Ollama.
- **GPU-hora sobrevive solo en capacidad dedicada/reservada**: Fireworks Dedicated
  Deployments factura por GPU-segundo a **$8.00 H100/H200, $13.00 B200, $15.00 B300,
  $20.00 GB300 por GPU-hora** (tarifas del 1-sep-2026; recargo 1.5× regional)
  ([fireworks.ai/pricing](https://fireworks.ai/pricing)). Together Dedicated Inference
  factura **por GPU-hora por réplica activa** (H100 $5.49 lista / **$3.99 promo hasta
  30-sep-2026**, B200 $8.99; scale-to-zero gratis; clusters on-demand $3.99–$8.19)
  ([docs.together.ai/docs/dedicated-endpoints/pricing](https://docs.together.ai/docs/dedicated-endpoints/pricing)).
  Es un producto distinto — capacidad reservada —, no un esquema de cuota.
- **El pariente estructural del sistema legado de Ollama no es ninguna API**: son las
  **suscripciones con ventanas de créditos** — z.ai Coding Plan (5h rolling + semanal,
  2,000–140,000 créditos/mes) y MiniMax Token Plan (5h + semanal, $22–$132/mes). El sistema
  legado de Ollama (cuota % con ventana 5h + semanal sobre plan Max) se parece a esos
  productos de suscripción, no a la facturación por GPU-hora de un deployment dedicado.
- **NVIDIA no publica tarifa first-party** por token para Nemotron vía NIM (evaluación
  gratuita; self-host vía licencia NVIDIA AI Enterprise por cotización): los precios que
  circulan son de partners (Bitdeer $0.80/$2.60, DeepInfra $0.50/$2.20, Fireworks $0.60/$2.40).

## Concurrencia / límites publicados (resumen)

| Proveedor | Métrica publicada | Tope publicado |
|---|---|---|
| Moonshot (Kimi) | concurrentes / RPM / TPM por tier de recarga | 1 → 1,000 concurrentes; 3 → 10,000 RPM; 0.5M → 5M TPM |
| DeepSeek | conexiones concurrentes por cuenta | 2,500 (flash) · 500 (pro); sin RPM/TPM |
| MiniMax | RPM / TPM por modelo | M3: 200 / 10M · M2.7: 500 / 20M |
| Fireworks | TPM adaptativo por cuenta+modelo; RPM de cuenta | 216k Generated TPM (~3.6k tok/s); máx. 6,000 RPM; ramps → 429 |
| Alibaba Model Studio | RPM / TPM por modelo y región | 600 RPM / 1M TPM (qwen3.5-397b) · 15k–30k RPM / 2–5M TPM (tiers cerrados) |
| Together | **dinámicos, sin RPM/TPM publicados** (por organización y modelo) | 429 `dynamic_request_limited` con header de reset; límites fijos solo en Dedicated/PTU/Batch |
| OpenRouter | solo variantes `:free` (20 req/min); pago sin cap de plataforma | 50–1,000 req/día sin/ con créditos en free |
| z.ai | consola con login | **no público/verificado** (doc: “dinámicos”) |
| Ollama (ref.) | concurrentes por plan | 1 (Free) · 3 (Pro) · 10 (Max/Team) — sin TPM/RPM publicados |

## No público / verificado

- **Together**: no publica RPM/TPM/concurrencia (límites dinámicos, solo 429 con header);
  Kimi K2/K2-thinking/K2.5 fuera de su catálogo (el precio de `kimi-k2-thinking` citado en la
  base era de un modelo retirado por Moonshot el 2026-05-25); K2.6 ($1.20/$4.50) y
  K2.7-Code publican precio pero **no están en serverless** (“launching soon”); Mistral
  Large fuera de catálogo; GLM-5.1, DeepSeek V3.1/V4-Pro base, Nemotron 3.5 Lightning/Ultra
  y Gemma 4 26B solo Dedicated sin precio por token; cached input no publicado para gpt-oss
  (ambos), gemma-4-31B y varios Qwen; discrepancia interna entre sus propias páginas
  (docs/serverless-models muestra Qwen3.8-Max $2.50/$6.25 vs $2.00/$6.00 en
  together.ai/pricing — se tomó esta última por ser la página de precios).
- **DeepSeek**: no publica RPM/TPM (solo concurrencia); precios de `deepseek-chat` /
  `deepseek-reasoner` / v3.1 / v3.2 ya no están en su página de pricing (solo V4).
- **z.ai**: límites de concurrencia no públicos (consola con login); valores de cache
  implícito por modelo no publicados (solo la regla ≈1/5 del input).
- **Alibaba**: valores exactos de cache implícito/explícito por región (solo regla “hit =
  10% del input, creación 125%”); precios en CNY del sitio doméstico; límites de
  `qwen3-235b-a22b` original.
- **Fireworks**: sin precio serverless por token para kimi-k2-thinking/k2.5, gpt-oss-20b,
  mistral-large-3, gemma, nemotron-3-super, qwen3-235b, GLM-4.6 — on-demand/dedicated solo,
  donde el precio público es el de GPU-hora.
- **Groq**: página de precios primaria no legible (landing sin tabla; docs 404) — precios de
  gpt-oss solo secundarios.
- **Cerebras**: sin precio público de `gpt-oss-20b` ni de cached input de 120b.
- **NVIDIA NIM**: sin tarifa first-party por token (self-host por cotización).
- **Gemma**: en Gemini API solo está Gemma 4, y gratis (sin tier de pago); los precios de
  Gemma 3/4 que circulan son de revendedores.
- **MiniMax M2.5/M2**: precio vigente publicado (legacy), pero su retiro real es dato de
  terceros, no de la página primaria.

## Fuentes

- **Moonshot**: [pricing](https://platform.kimi.ai/docs/pricing) · [k3](https://platform.kimi.ai/docs/pricing/chat-k3) · [k2.7-code](https://platform.kimi.ai/docs/pricing/chat-k27-code) · [k2.6](https://platform.kimi.ai/docs/pricing/chat-k26) · [limits](https://platform.kimi.ai/docs/pricing/limits.md) · [models](https://platform.kimi.ai/docs/models.md) · CNY: [platform.kimi.com K3](https://platform.kimi.com/docs/pricing/chat-k3)
- **DeepSeek**: [pricing](https://api-docs.deepseek.com/quick_start/pricing) · [rate limit](https://api-docs.deepseek.com/quick_start/rate_limit)
- **z.ai**: [pricing](https://docs.z.ai/guides/overview/pricing) · [FAQ](https://docs.z.ai/help/faq.md) · [DevPack](https://docs.z.ai/devpack/overview.md)
- **MiniMax**: [pricing-paygo](https://platform.minimax.io/docs/guides/pricing-paygo.md) · [rate-limits](https://platform.minimax.io/docs/guides/rate-limits.md) · [token plan](https://platform.minimax.io/docs/guides/pricing-token-plan.md)
- **Alibaba Model Studio**: [model-pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing) · [rate-limit](https://www.alibabacloud.com/help/en/model-studio/rate-limit) · [qwen3.5-397b-a17b](https://www.alibabacloud.com/help/en/model-studio/qwen3-5-397b-a17b)
- **Fireworks**: [serverless pricing](https://docs.fireworks.ai/serverless/pricing) · [rate limits](https://docs.fireworks.ai/serverless/rate-limits) · [pricing](https://fireworks.ai/pricing) · [kimi-k3](https://fireworks.ai/models/fireworks/kimi-k3) · [gpt-oss-20b](https://fireworks.ai/models/fireworks/gpt-oss-20b) · [gemma-4-31b-it](https://fireworks.ai/models/fireworks/gemma-4-31b-it) · [mistral-large-3-fp8](https://fireworks.ai/models/fireworks/mistral-large-3-fp8)
- **Together**: [pricing](https://www.together.ai/pricing) · [serverless models](https://docs.together.ai/docs/serverless-models) · [dedicated pricing](https://docs.together.ai/docs/dedicated-endpoints/pricing) · [dedicated models](https://docs.together.ai/docs/dedicated-endpoints/models) · [rate limits](https://docs.together.ai/docs/rate-limits) · [kimi-k2.6](https://www.together.ai/models/kimi-k26) · [kimi-k2.7-code](https://www.together.ai/models/kimi-k27-code) · [qwen3.5-397b](https://www.together.ai/models/qwen3-5-397b-a17b) · [gpt-oss-120b](https://www.together.ai/models/gpt-oss-120b)
- **OpenRouter**: [GET /api/v1/models](https://openrouter.ai/api/v1/models) (JSON oficial, captura 2026-08-31/09-01) · [limits](https://openrouter.ai/docs/api-reference/limits)
- **Mistral**: [pricing](https://mistral.ai/pricing) · [pricing/api](https://mistral.ai/pricing/api)
- **Otros hosts**: [Cerebras gpt-oss](https://inference-docs.cerebras.ai/models/openai-oss) · [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) · [DeepInfra pricing](https://deepinfra.com/pricing) (secundaria) · Groq vía [layer3labs](https://www.layer3labs.io/guides/groq-pricing) / [computeprices](https://computeprices.com/providers/groq/models/gpt-oss-120b) (secundarias) · Nemotron partners vía [AI//COST](https://aicost.tools/llm-cost/nvidia/) (secundaria)
- Interno: [`base-pricing-2026-08-31.md`](./base-pricing-2026-08-31.md) · snapshot [`pricing-snapshot/`](./pricing-snapshot/)