# External price comparables — open-weights providers (2026-09-01)

Answers issue [#15](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/15)
("External price comparables (open-weights)"; the issue calls it
`comparables-pricing.md`, this file is its implementation). It feeds the
`H_comparables` row of the incentives matrix and the study's break-even.

Comparison base: the new plan's official table of 2026-08-31
([`base-pricing-2026-08-31.md`](./base-pricing-2026-08-31.md)). Capture date of the
comparables: **2026-08-31 / 2026-09-01**. One single price per cell, always with its
source; whatever is unverifiable is explicitly flagged in its section.

## Method

- **Primary source first**: each provider's pricing pages and docs, read
  directly. The aggregators (DeepInfra, AI//COST, tokencost, benchlm, layer3labs,
  computeprices) were used only as backup and **are marked "secondary"**.
- **Pages that required JS / were unreadable** and how they were resolved:
  `openrouter.ai/<model>`
  (JS-heavy web → its public JSON endpoint `GET /api/v1/models` was used, which is a
  first-party source); `groq.com/pricing` and `console.groq.com/docs/price` (landing
  page without a table / 404 → aggregators); `build.nvidia.com` (timeout → aggregators);
  z.ai's rate-limit console (requires login → "not public"); Alibaba's `model-pricing`
  price page readable (the JS-heavy `#/doc/...` model pages were avoided);
  `platform.moonshot.ai` → 301 to `platform.kimi.ai`.
- Everything in **USD per 1M tokens** unless stated (CNY prices are reported as-is).
- Convention of the "price" column: **input / output** $/1M. The "cache" column is the
  cached-input rate (or the published discount). "n/p" = no public/verified price.
- The eight reference providers (the issue asks for 4–6): **Moonshot direct, z.ai direct,
  MiniMax direct, DeepSeek direct, Alibaba Model Studio, Fireworks, Together,
  OpenRouter**, plus notable hosts for orphan families (Groq, Cerebras, Mistral,
  DeepInfra, Gemini API, Bitdeer/NIM).

---

## 1. Kimi — Ollama: `kimi-k3` · `kimi-k2.7-code` · `kimi-k2.6`

| Ollama model | Ollama rate (in/cached/out) | Comparable provider | Price (in/out $/1M) | Cache | GPU-time? | Source |
|---|---|---|---|---|---|---|
| kimi-k3 | $3.00 / $0.30 / $15.00 | **Moonshot direct** (`kimi-k3`, ctx 1M) | $3.00 / $15.00 | $0.30 (−90%) | No — per token | [platform.kimi.ai/docs/pricing/chat-k3](https://platform.kimi.ai/docs/pricing/chat-k3) |
| kimi-k3 | ditto | Fireworks — `kimi-k3` | $3.00 / $15.00 | $0.30 (Priority ×1.25: $3.75/$18.75; Fast $4.50/$22.50) | No (dedicated: GPU-hour $8–20) | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| kimi-k3 | ditto | OpenRouter — `moonshotai/kimi-k3` | $3.00 / $15.00 | $0.30 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) (official JSON) |
| kimi-k3 | ditto | Together — `moonshotai/Kimi-K3` (serverless) | $3.00 / $15.00 | $0.30 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| kimi-k3 | ditto | DeepInfra — `kimi-k3` | $2.85 / $14.25 | $0.285 | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secondary) |
| kimi-k2.7-code | $0.95 / $0.19 / $4.00 | Moonshot direct — `kimi-k2.7-code` | $0.95 / $4.00 | $0.19 (−80%); highspeed: $1.90/$8.00 | No | [platform.kimi.ai/docs/pricing/chat-k27-code](https://platform.kimi.ai/docs/pricing/chat-k27-code) |
| kimi-k2.7-code | ditto | Fireworks — `kimi-k2.7-code` | $0.95 / $4.00 | $0.19 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| kimi-k2.7-code | ditto | Together — `moonshotai/Kimi-K2.7-Code` | $0.95 / $4.00 | $0.19 | No (price published but **not on serverless**: "launching soon"; Dedicated per GPU-hour) | [together.ai/models/kimi-k27-code](https://www.together.ai/models/kimi-k27-code) |
| kimi-k2.6 | $0.95 / $0.16 / $4.00 | Moonshot direct — `kimi-k2.6` | $0.95 / $4.00 | $0.16 (−83%) | No | [platform.kimi.ai/docs/pricing/chat-k26](https://platform.kimi.ai/docs/pricing/chat-k26) |
| kimi-k2.6 | ditto | Fireworks — `kimi-k2p6` | $0.95 / $4.00 | $0.16 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| kimi-k2.6 | ditto | OpenRouter — `moonshotai/kimi-k2.6` | $0.95 / $4.00 | $0.16 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| kimi-k2.6 | ditto | Together — `moonshotai/Kimi-K2.6` | $1.20 / $4.50 | $0.20 | No (price published but **not on serverless**: "launching soon"; Dedicated per GPU-hour) | [together.ai/models/kimi-k26](https://www.together.ai/models/kimi-k26) |
| kimi-k2.6 | ditto | DeepInfra — `kimi-k2.6` | $0.75 / $3.50 | $0.15 | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secondary) |
| (kimi-k2.6 as historical analog) | ditto | OpenRouter — `moonshotai/kimi-k2-thinking` | $0.60 / $2.50 | $0.15 | No | ditto |

Moonshot's published limits (per cumulative account top-up, all models):
Tier0 ($1): 1 concurrent / 3 RPM / 0.5M TPM; Tier5 ($3,000): 1,000 concurrent / 10,000 RPM
/ 5M TPM ([platform.kimi.ai/docs/pricing/limits](https://platform.kimi.ai/docs/pricing/limits.md)).
K2 and K2-thinking were **delisted** from Moonshot's catalog (2026-05-25) and K2.5 on
2026-08-31; the K2-thinking price in circulation ($0.60/$2.50) survives only on third
parties (OpenRouter).

## 2. DeepSeek — Ollama: `deepseek-v4-flash` · `deepseek-v4-pro`

| Ollama model | Ollama rate (in/cached/out) | Comparable provider | Price (in/out $/1M) | Cache | GPU-time? | Source |
|---|---|---|---|---|---|---|
| deepseek-v4-flash | $0.44 / $0.014 / $1.32 | **DeepSeek direct** — `deepseek-v4-flash` (V4-Flash-0731) | $0.44 / $1.32 peak; **off-peak −50%** ($0.22/$0.66) | $0.014 peak ($0.007) | No — per token | [api-docs.deepseek.com/quick_start/pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| deepseek-v4-flash | ditto | Fireworks — `deepseek-v4-flash-0731` | $0.22 / $0.66 | $0.007 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| deepseek-v4-flash | ditto | OpenRouter — `deepseek/deepseek-v4-flash-0731` | $0.065 / $0.18 | $0.016 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| deepseek-v4-flash | ditto | Together — `deepseek-ai/DeepSeek-V4-Flash-0731` (serverless) | $0.14 / $0.28 | $0.03 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| deepseek-v4-flash | ditto | DeepInfra — `deepseek-v4-flash-0731` | $0.08 / $0.18 | $0.016 | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secondary) |
| deepseek-v4-pro | $1.32 / $0.044 / $3.96 | **DeepSeek direct** — `deepseek-v4-pro` (V4-Pro-0813) | $1.32 / $3.96 peak (off-peak −50%) | $0.044 peak ($0.022) | No | [api-docs.deepseek.com/quick_start/pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| deepseek-v4-pro | ditto | Fireworks — `deepseek-v4-pro-0813` | $1.32 / $3.96 | $0.044 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| deepseek-v4-pro | ditto | OpenRouter — `deepseek/deepseek-v4-pro-0813` | $0.66 / $1.98 | $0.022 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| deepseek-v4-pro | ditto | Together — `deepseek-ai/DeepSeek-V4-Pro-0813` (serverless) | $1.32 / $3.96 | $0.13 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| deepseek-v4-pro | ditto | DeepInfra — `deepseek-v4-pro` | $1.30 / $2.60 | $0.10 | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secondary) |

DeepSeek concurrency (published): concurrent connections per account only —
**2,500** (flash) / **500** (pro) —; it does not publish RPM/TPM
([api-docs.deepseek.com/quick_start/rate_limit](https://api-docs.deepseek.com/quick_start/rate_limit)).
Off-peak (−50% on all three columns): 01:00–04:00 and 06:00–10:00 UTC, Mon–Fri.

## 3. GLM — Ollama: `glm-5.3` · `glm-5.3-flash` · `glm-5.2` · `glm-5.1`

| Ollama model | Ollama rate (in/cached/out) | Comparable provider | Price (in/out $/1M) | Cache | GPU-time? | Source |
|---|---|---|---|---|---|---|
| glm-5.3 | $1.40 / $0.26 / $4.40 | **z.ai direct** — GLM-5.3 | $1.40 / $4.40 | $0.26 | No — per token | [docs.z.ai/guides/overview/pricing](https://docs.z.ai/guides/overview/pricing) |
| glm-5.3 | ditto | Fireworks — `glm-5.3` | $1.40 / $4.40 | $0.26 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| glm-5.3 | ditto | Together — `zai-org/GLM-5.3` (serverless) | $1.40 / $4.40 | $0.26 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| glm-5.3 | ditto | OpenRouter — `z-ai/glm-5.3` | $1.40 / $4.40 | $0.26 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| glm-5.3-flash | $0.15 / $0.03 / $0.50 | z.ai direct — GLM-5.3-Flash | $0.15 / $0.50 **list**; −50% promo ($0.075/$0.25) until 2026-09-09 | $0.03 (promo $0.015) | No | [docs.z.ai/guides/overview/pricing](https://docs.z.ai/guides/overview/pricing) |
| glm-5.3-flash | ditto | Fireworks — `glm-5.3-flash` | $0.15 / $0.50 | $0.03 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| glm-5.3-flash | ditto | Together — `zai-org/GLM-5.3-Flash` (serverless) | $0.15 / $0.50 | $0.03 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| glm-5.3-flash | ditto | OpenRouter — `z-ai/glm-5.3-flash` | $0.075 / $0.25 (promo) | $0.015 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| glm-5.2 | $1.40 / $0.26 / $4.40 | z.ai direct — GLM-5.2 | $1.40 / $4.40 | $0.26 | No | [docs.z.ai/guides/overview/pricing](https://docs.z.ai/guides/overview/pricing) |
| glm-5.2 | ditto | Fireworks — `glm-5.2` | $1.40 / $4.40 | $0.14 (Fast: $2.10/$6.60) | No | ditto |
| glm-5.2 | ditto | Together — `zai-org/GLM-5.2` (serverless) | $1.40 / $4.40 | $0.26 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| glm-5.2 | ditto | OpenRouter — `z-ai/glm-5.2` | $1.19 / $3.74 | $0.22 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| glm-5.2 | ditto | Mistral La Plateforme (resells GLM 5.2) | $1.40 / $4.40 | $0.14 | No | [mistral.ai/pricing/api](https://mistral.ai/pricing/api) (primary) |
| glm-5.1 | $1.00 / $0.20 / $3.20 | z.ai direct — GLM-5 | $1.00 / $3.20 | $0.20 | No | [docs.z.ai/guides/overview/pricing](https://docs.z.ai/guides/overview/pricing) |
| glm-5.1 | ditto | Together — GLM 5.1 | n/p (Dedicated Endpoints only, no per-token price published) | — | No (Dedicated: per GPU-hour) | [docs.together.ai/docs/dedicated-endpoints/models](https://docs.together.ai/docs/dedicated-endpoints/models) |
| glm-5.1 | ditto | OpenRouter — `z-ai/glm-5.1` | $0.97 / $3.04 | $0.18 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |

Mapping note: z.ai's table groups "GLM-5.2/5.1" at $1.40/$4.40 and lists "GLM-5" at
$1.00/$3.20; the exact analog of Ollama's `glm-5.1` is ambiguous between those two rows.
z.ai limits: the official rate-limit page requires login → **not public**; the docs say
only "dynamic limits per user/plan" (the only number in circulation, secondary: "GLM-5.2
concurrency 10", [github.com/zai-org/GLM-5/issues/83](https://github.com/zai-org/GLM-5/issues/83)).
z.ai's Coding Plan (DevPack, from $18/month) consumes **credits** with rolling 5h
+ weekly windows — the closest structural relative of Ollama's legacy scheme, but measured in
credits per tokens, not in GPU-time ([docs.z.ai/devpack/overview.md](https://docs.z.ai/devpack/overview.md)).

## 4. Qwen — Ollama: `qwen3.5:397b`

| Ollama model | Ollama rate (in/cached/out) | Comparable provider | Price (in/out $/1M) | Cache | GPU-time? | Source |
|---|---|---|---|---|---|---|
| qwen3.5:397b | $0.60 / $0.60 / $3.60 | **Alibaba Model Studio (Intl.)** — `qwen3.5-397b-a17b` | $0.60 / $3.60 (Singapore; Beijing/US/Germany: $0.172 ≤128K / $0.43 128–256K) | **not public/verified** (house rule: cache hit = 10% of input; no cache for this model) | No — per token | [model-pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing) · [qwen3.5-397b-a17b](https://www.alibabacloud.com/help/en/model-studio/qwen3-5-397b-a17b) |
| qwen3.5:397b | ditto | Together — `Qwen/Qwen3.5-397B-A17B` (serverless) | $0.60 / $3.60 | $0.35 (−42%) | No | [together.ai/models/qwen3-5-397b-a17b](https://www.together.ai/models/qwen3-5-397b-a17b) |
| qwen3.5:397b | ditto | OpenRouter — `qwen/qwen3.5-397b-a17b` | $0.39 / $2.34 | not public | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| qwen3.5:397b | ditto | Fireworks | n/p (not offered on serverless; `qwen3.8-max` $2.00/$6.00 and `qwen-3.7-plus` $0.40/$1.60 are) | — | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| (flagship reference) | — | Alibaba — `qwen3.8-max` | $2.00 / $6.00 (Singapore) | hit = 10% of input | No | [model-pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing) |

Alibaba's published limits (regional): `qwen3.5-397b-a17b` **600 RPM / 1M TPM** in all
regions; the closed tiers (3.7-plus, 3.8-max/flash) 15,000–30,000 RPM / 2–5M TPM
([rate-limit](https://www.alibabacloud.com/help/en/model-studio/rate-limit)).

## 5. MiniMax — Ollama: `minimax-m3` · `minimax-m2.7`

| Ollama model | Ollama rate (in/cached/out) | Comparable provider | Price (in/out $/1M) | Cache | GPU-time? | Source |
|---|---|---|---|---|---|---|
| minimax-m3 | $0.60 / $0.12 / $2.40 | **MiniMax direct** — M3, input ≤512k | $0.30 / $1.20 (permanent −50%; list $0.60/$2.40) | cache read $0.06; 200 RPM / 10M TPM | No — per token (priority tier ×1.5) | [platform.minimax.io/docs/guides/pricing-paygo.md](https://platform.minimax.io/docs/guides/pricing-paygo.md) |
| minimax-m3 | ditto | MiniMax direct — M3, input >512k | $0.60 / $2.40 | $0.12 | No | ditto |
| minimax-m3 | ditto | Fireworks — `minimax-m3` | $0.30 / $1.20 (ctx 512K) | $0.06 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| minimax-m3 | ditto | Together — `MiniMaxAI/MiniMax-M3` (serverless) | $0.30 / $1.20 | $0.06 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| minimax-m3 | ditto | OpenRouter — `minimax/minimax-m3` | $0.30 / $1.20 | $0.06 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| minimax-m2.7 | $0.30 / $0.06 / $1.20 | MiniMax direct — M2.7 | $0.30 / $1.20 (highspeed $0.60/$2.40) | read $0.06; write $0.375; 500 RPM / 20M TPM | No | ditto + [rate-limits](https://platform.minimax.io/docs/guides/rate-limits.md) |
| minimax-m2.7 | ditto | Fireworks — `minimax-m2p7` | $0.30 / $1.20 | $0.059 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| minimax-m2.7 | ditto | Together — `MiniMaxAI/MiniMax-M2.7` (serverless) | $0.30 / $1.20 | $0.06 | No | [together.ai/pricing](https://www.together.ai/pricing) |
| minimax-m2.7 | ditto | OpenRouter — `minimax/minimax-m2.7` | $0.30 / $1.20 | $0.06 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |

Note: Ollama's `minimax-m3` rate matches MiniMax direct's **>512k tier / list price**,
double the effective price charged by MiniMax and its resellers in the
standard tier. Besides PAYGO, MiniMax sells a subscription **Token Plan** (Plus $22 /
Max $55 / Ultra $132) with 5h + weekly windows ([pricing-token-plan](https://platform.minimax.io/docs/guides/pricing-token-plan.md)).

## 6. gpt-oss (OpenAI open-weights) — Ollama: `gpt-oss:120b` · `gpt-oss:20b`

| Ollama model | Ollama rate (in/cached/out) | Comparable provider | Price (in/out $/1M) | Cache | GPU-time? | Source |
|---|---|---|---|---|---|---|
| gpt-oss:120b | $0.15 / $0.014 / $0.60 | Fireworks — `gpt-oss-120b` | $0.15 / $0.60 | $0.015 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| gpt-oss:120b | ditto | Groq — `gpt-oss-120b` | $0.15 / $0.60 | $0.075 | No | secondary: [layer3labs](https://www.layer3labs.io/guides/groq-pricing) · [computeprices](https://computeprices.com/providers/groq/models/gpt-oss-120b) (primary page unreadable) |
| gpt-oss:120b | ditto | Cerebras — `gpt-oss-120b` | $0.35 / $0.75 | not published | No | [inference-docs.cerebras.ai/models/openai-oss](https://inference-docs.cerebras.ai/models/openai-oss) (primary; 1,000 RPM / 1M input tok/min) |
| gpt-oss:120b | ditto | Together — `openai/gpt-oss-120b` (serverless) | $0.15 / $0.60 | not published | No | [together.ai/models/gpt-oss-120b](https://www.together.ai/models/gpt-oss-120b) |
| gpt-oss:120b | ditto | OpenRouter — `openai/gpt-oss-120b` | $0.037 / $0.17 | field absent | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| gpt-oss:20b | $0.07 / $0.035 / $0.30 | Together — `openai/gpt-oss-20b` (serverless) | $0.05 / $0.20 | not published | No | [together.ai/pricing](https://www.together.ai/pricing) |
| gpt-oss:20b | ditto | Groq — `gpt-oss-20b` | $0.075 / $0.30 | $0.0375 | No | secondary (ditto) |
| gpt-oss:20b | ditto | OpenRouter — `openai/gpt-oss-20b` | $0.03 / $0.13 | $0.03 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| gpt-oss:20b | ditto | Fireworks | n/p (serverless "Not supported"; on-demand/dedicated only) | — | on-demand: GPU-hour $8–20 | [fireworks.ai/models/fireworks/gpt-oss-20b](https://fireworks.ai/models/fireworks/gpt-oss-20b) |

## 7. Mistral Large — Ollama: `mistral-large-3`

| Ollama model | Ollama rate (in/cached/out) | Comparable provider | Price (in/out $/1M) | Cache | GPU-time? | Source |
|---|---|---|---|---|---|---|
| mistral-large-3 | $0.50 / $0.50 / $1.50 | **Mistral La Plateforme** — `mistral-large-latest` ("Large 3") | $0.50 / $1.50 (batch −50%) | published only as "reduce input cost up to 90%", with no absolute figure | No — per token | [mistral.ai/pricing](https://mistral.ai/pricing) · [/pricing/api](https://mistral.ai/pricing/api) |
| mistral-large-3 | ditto | OpenRouter — `mistralai/mistral-large-2512` | $0.50 / $1.50 | $0.05 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| mistral-large-3 | ditto | Fireworks — `mistral-large-3-fp8` | n/p (serverless "Not supported"; aggregators cite $1.20/$1.20 unverified) | — | on-demand: per GPU-hour | [fireworks.ai/models/fireworks/mistral-large-3-fp8](https://fireworks.ai/models/fireworks/mistral-large-3-fp8) |
| mistral-large-3 | ditto | Together | **not offered** (neither serverless nor dedicated; only Mistral 7B v0.3 and legacy Mixtral 8x7B on Dedicated) | — | — | [docs.together.ai/docs/dedicated-endpoints/models](https://docs.together.ai/docs/dedicated-endpoints/models) |
| (catalog neighbor) | — | Mistral — `mistral-medium-3.5` / `mistral-small-4` | $1.50/$7.50 · $0.15/$0.60 | not published | No | [mistral.ai/pricing/api](https://mistral.ai/pricing/api) |

## 8. Nemotron — Ollama: `nemotron-3-nano` · `nemotron-3-super` · `nemotron-3-ultra`

| Ollama model | Ollama rate (in/cached/out) | Comparable provider | Price (in/out $/1M) | Cache | GPU-time? | Source |
|---|---|---|---|---|---|---|
| nemotron-3-ultra | $0.10 / $0.10 / $3.00 | Fireworks — `nemotron-3-ultra` (NVFP4, preview) | $0.60 / $2.40 | $0.12 | No | [docs.fireworks.ai/serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| nemotron-3-ultra | ditto | OpenRouter — `nvidia/nemotron-3-ultra-550b-a55b` | $0.50 / $2.20 | $0.10 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| nemotron-3-ultra | ditto | Together — Nemotron 3 Ultra 550B | n/p (Dedicated only, no per-token price published) | — | No (Dedicated: per GPU-hour) | [docs.together.ai/docs/dedicated-endpoints/models](https://docs.together.ai/docs/dedicated-endpoints/models) |
| nemotron-3-ultra | ditto | NVIDIA NIM (first-party) | **not public/verified** — no per-token rate of its own; free evaluation; self-host via an NVIDIA AI Enterprise license (quote-based) | — | GPU-time only via the NVE license (price not public) | [build.nvidia.com](https://build.nvidia.com) (unreadable) + [aicost.tools](https://aicost.tools/llm-cost/nvidia/) (secondary) |
| nemotron-3-super | $0.015 / $0.015 / $0.60 | OpenRouter — `nvidia/nemotron-3-super-120b-a12b` | $0.085 / $0.40 | not public | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| nemotron-3-super | ditto | Fireworks | n/p (in the catalog with no visible price; offered analog: `nemotron-3.5-lightning-30b` $0.05/$0.20) | — | No | [fireworks.ai/models](https://fireworks.ai/models) · [serverless/pricing](https://docs.fireworks.ai/serverless/pricing) |
| nemotron-3-nano | $0.06 / $0.06 / $0.24 | OpenRouter — `nvidia/nemotron-3-nano-30b-a3b` | $0.05 / $0.20 | $0.025 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |

## 9. Gemma — Ollama: `gemma4`

| Ollama model | Ollama rate (in/cached/out) | Comparable provider | Price (in/out $/1M) | Cache | GPU-time? | Source |
|---|---|---|---|---|---|---|
| gemma4 | $0.14 / $0.05 / $0.40 | **Gemini API (Google)** — Gemma 4 | **free** (paid tier: "Not available") | free of charge | No | [ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| gemma4 | ditto | Together — `google/gemma-4-31B-it` (serverless) | $0.39 / $0.97 | not published | No | [together.ai/pricing](https://www.together.ai/pricing) |
| gemma4 | ditto | OpenRouter — `google/gemma-4-31b-it` | $0.09 / $0.34 | $0.05 | No | [openrouter.ai/api/v1/models](https://openrouter.ai/api/v1/models) |
| gemma4 | ditto | DeepInfra — `gemma-4-31b-it` | $0.13 / $0.38 | not public | No | [deepinfra.com/pricing](https://deepinfra.com/pricing) (secondary) |
| gemma4 | ditto | Fireworks — `gemma-4-31b-it` | n/p (serverless "Not supported"; on-demand only) | — | on-demand: GPU-hour $8–20 | [fireworks.ai/models/fireworks/gemma-4-31b-it](https://fireworks.ai/models/fireworks/gemma-4-31b-it) |

---

## Ollama's position in the market range (by family)

| Family (analog) | Ollama rate (in/out) | Market range (in/out, verified) | Ollama's position |
|---|---|---|---|
| kimi-k3 | $3.00 / $15.00 | $2.85–$3.00 / $14.25–$15.00 | **Top of range** — equal to Moonshot, Fireworks and Together's direct price |
| kimi-k2.6 / k2.7-code | $0.95 / $4.00 | $0.75–$1.20 / $3.50–$4.50 | Top of range — equal to Moonshot direct; DeepInfra 20% cheaper; Together lists K2.6 at $1.20/$4.50 but outside serverless |
| glm-5.3 | $1.40 / $4.40 | $1.19–$1.40 / $3.74–$4.40 | Top of range — equal to z.ai direct, Fireworks and Together; OpenRouter −15% |
| glm-5.3-flash | $0.15 / $0.50 | $0.075–$0.15 / $0.25–$0.50 | List; z.ai sells at **half** under the current promo |
| deepseek-v4-pro | $1.32 / $3.96 | $0.66–$1.32 / $1.98–$3.96 | Top of range — equal to direct peak, Fireworks and Together; 2× OpenRouter/DeepInfra |
| deepseek-v4-flash | $0.44 / $1.32 | $0.065–$0.44 / $0.16–$1.32 | Top of range — equal to direct peak; 2× Fireworks, 2.9× Together, 5–6× OpenRouter |
| glm-5.2 / glm-5.1 | $1.40 / $4.40 · $1.00 / $3.20 | $1.19–$1.40 / $3.74–$4.40 | Top of range — equal to z.ai direct |
| minimax-m3 | $0.60 / $2.40 | $0.30–$0.60 / $1.20–$2.40 | **2× the effective price** (MiniMax −50% permanent; Fireworks, Together and OpenRouter at the discounted price) |
| minimax-m2.7 | $0.30 / $1.20 | $0.30 / $1.20 | At market (= direct) |
| qwen3.5:397b | $0.60 / $3.60 | $0.39–$0.60 / $2.34–$3.60 | Top of range — equal to Alibaba Singapore and Together; OpenRouter −35% |
| gpt-oss:120b | $0.15 / $0.60 | $0.037–$0.35 / $0.17–$0.75 | In the mid-upper range ( = Fireworks, Together and Groq; Cerebras +133%; OpenRouter −75%) |
| gpt-oss:20b | $0.07 / $0.30 | $0.03–$0.075 / $0.13–$0.30 | In the mid-upper range (Together $0.05/$0.20; Groq secondary, equal) |
| mistral-large-3 | $0.50 / $1.50 | $0.50 / $1.50 | At market (= Mistral direct) |
| nemotron-3-ultra | $0.10 / $3.00 | $0.50–$0.80 / $2.20–$2.60 | **Cheap outlier on input (5–8×)**, somewhat expensive on output (+15–36%) |
| nemotron-3-super | $0.015 / $0.60 | $0.085 / $0.40 | Input 5.7× cheaper; output 1.5× more expensive |
| nemotron-3-nano | $0.06 / $0.24 | $0.05 / $0.20 | Close to market (+20%) |
| gemma4 | $0.14 / $0.40 | free (Gemini API) – $0.39/$0.97 (Together) | In the lower-middle of the resellers' range; Google gives it away free |

## Non-per-token schemes that are still alive (GPU-time and kin)

- **No comparable bills its shared per-token API by GPU-time.** The five
  direct providers (Moonshot, DeepSeek, z.ai, Alibaba, MiniMax) and all the aggregators
  in this sweep bill per token; Fireworks declares per token in serverless with the three
  in/cached/out columns — the same structure Ollama's new plan adopted.
- **Per-GPU-hour survives only in dedicated/reserved capacity**: Fireworks Dedicated
  Deployments bills per GPU-second at **$8.00 H100/H200, $13.00 B200, $15.00 B300,
  $20.00 GB300 per GPU-hour** (rates as of 2026-09-01; 1.5× regional surcharge)
  ([fireworks.ai/pricing](https://fireworks.ai/pricing)). Together Dedicated Inference
  bills **per GPU-hour per active replica** (H100 $5.49 list / **$3.99 promo until
  2026-09-30**, B200 $8.99; free scale-to-zero; on-demand clusters $3.99–$8.19)
  ([docs.together.ai/docs/dedicated-endpoints/pricing](https://docs.together.ai/docs/dedicated-endpoints/pricing)).
  It is a different product — reserved capacity —, not a quota scheme.
- **The structural relative of Ollama's legacy system is not any API**: it is the
  **credit-window subscriptions** — z.ai Coding Plan (rolling 5h + weekly,
  2,000–140,000 credits/month) and MiniMax Token Plan (5h + weekly, $22–$132/month). Ollama's
  legacy system (% quota with a 5h + weekly window on the Max plan) resembles those
  subscription products, not the per-GPU-hour billing of a dedicated deployment.
- **NVIDIA publishes no first-party rate** per token for Nemotron via NIM (free
  evaluation; self-host via an NVIDIA AI Enterprise license by quote): the prices in
  circulation are from partners (Bitdeer $0.80/$2.60, DeepInfra $0.50/$2.20, Fireworks $0.60/$2.40).

## Concurrency / published limits (summary)

| Provider | Published metric | Published cap |
|---|---|---|
| Moonshot (Kimi) | concurrent / RPM / TPM per top-up tier | 1 → 1,000 concurrent; 3 → 10,000 RPM; 0.5M → 5M TPM |
| DeepSeek | concurrent connections per account | 2,500 (flash) · 500 (pro); no RPM/TPM |
| MiniMax | RPM / TPM per model | M3: 200 / 10M · M2.7: 500 / 20M |
| Fireworks | adaptive per-account+model TPM; account RPM | 216k Generated TPM (~3.6k tok/s); max. 6,000 RPM; ramps → 429 |
| Alibaba Model Studio | RPM / TPM per model and region | 600 RPM / 1M TPM (qwen3.5-397b) · 15k–30k RPM / 2–5M TPM (closed tiers) |
| Together | **dynamic, no published RPM/TPM** (per organization and model) | 429 `dynamic_request_limited` with a reset header; fixed limits only on Dedicated/PTU/Batch |
| OpenRouter | only `:free` variants (20 req/min); paid without a platform cap | 50–1,000 req/day without/with credits on free |
| z.ai | login-gated console | **not public/verified** (docs: "dynamic") |
| Ollama (ref.) | concurrent per plan | 1 (Free) · 3 (Pro) · 10 (Max/Team) — no published TPM/RPM |

## Not public / verified

- **Together**: does not publish RPM/TPM/concurrency (dynamic limits, only a 429 with a
  header); Kimi K2/K2-thinking/K2.5 out of its catalog (the `kimi-k2-thinking` price cited in the
  baseline was for a model delisted by Moonshot on 2026-05-25); K2.6 ($1.20/$4.50) and
  K2.7-Code publish a price but are **not on serverless** ("launching soon"); Mistral
  Large out of catalog; GLM-5.1, DeepSeek V3.1/V4-Pro base, Nemotron 3.5 Lightning/Ultra
  and Gemma 4 26B Dedicated-only with no per-token price; cached input not published for
  gpt-oss (both), gemma-4-31B and several Qwen; internal discrepancy between its own
  pages (docs/serverless-models shows Qwen3.8-Max $2.50/$6.25 vs $2.00/$6.00 on
  together.ai/pricing — the latter was taken, being the pricing page).
- **DeepSeek**: does not publish RPM/TPM (concurrency only); the prices of `deepseek-chat` /
  `deepseek-reasoner` / v3.1 / v3.2 are no longer on its pricing page (V4 only).
- **z.ai**: concurrency limits not public (login-gated console); implicit-cache values
  per model not published (only the ≈1/5-of-input rule).
- **Alibaba**: exact implicit/explicit cache values per region (only the rule "hit =
  10% of input, creation 125%"); CNY prices on the domestic site; limits of the
  original `qwen3-235b-a22b`.
- **Fireworks**: no serverless per-token price for kimi-k2-thinking/k2.5, gpt-oss-20b,
  mistral-large-3, gemma, nemotron-3-super, qwen3-235b, GLM-4.6 — on-demand/dedicated only,
  where the public price is the per-GPU-hour one.
- **Groq**: primary pricing page unreadable (landing page without a table; docs 404) —
  gpt-oss prices secondary only.
- **Cerebras**: no public price for `gpt-oss-20b` nor for 120b cached input.
- **NVIDIA NIM**: no first-party per-token rate (self-host by quote).
- **Gemma**: on Gemini API only Gemma 4 exists, and free (no paid tier); the Gemma 3/4
  prices in circulation are from resellers.
- **MiniMax M2.5/M2**: current price published (legacy), but its actual retirement is
  third-party data, not from the primary page.

## Sources

- **Moonshot**: [pricing](https://platform.kimi.ai/docs/pricing) · [k3](https://platform.kimi.ai/docs/pricing/chat-k3) · [k2.7-code](https://platform.kimi.ai/docs/pricing/chat-k27-code) · [k2.6](https://platform.kimi.ai/docs/pricing/chat-k26) · [limits](https://platform.kimi.ai/docs/pricing/limits.md) · [models](https://platform.kimi.ai/docs/models.md) · CNY: [platform.kimi.com K3](https://platform.kimi.com/docs/pricing/chat-k3)
- **DeepSeek**: [pricing](https://api-docs.deepseek.com/quick_start/pricing) · [rate limit](https://api-docs.deepseek.com/quick_start/rate_limit)
- **z.ai**: [pricing](https://docs.z.ai/guides/overview/pricing) · [FAQ](https://docs.z.ai/help/faq.md) · [DevPack](https://docs.z.ai/devpack/overview.md)
- **MiniMax**: [pricing-paygo](https://platform.minimax.io/docs/guides/pricing-paygo.md) · [rate-limits](https://platform.minimax.io/docs/guides/rate-limits.md) · [token plan](https://platform.minimax.io/docs/guides/pricing-token-plan.md)
- **Alibaba Model Studio**: [model-pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing) · [rate-limit](https://www.alibabacloud.com/help/en/model-studio/rate-limit) · [qwen3.5-397b-a17b](https://www.alibabacloud.com/help/en/model-studio/qwen3-5-397b-a17b)
- **Fireworks**: [serverless pricing](https://docs.fireworks.ai/serverless/pricing) · [rate limits](https://docs.fireworks.ai/serverless/rate-limits) · [pricing](https://fireworks.ai/pricing) · [kimi-k3](https://fireworks.ai/models/fireworks/kimi-k3) · [gpt-oss-20b](https://fireworks.ai/models/fireworks/gpt-oss-20b) · [gemma-4-31b-it](https://fireworks.ai/models/fireworks/gemma-4-31b-it) · [mistral-large-3-fp8](https://fireworks.ai/models/fireworks/mistral-large-3-fp8)
- **Together**: [pricing](https://www.together.ai/pricing) · [serverless models](https://docs.together.ai/docs/serverless-models) · [dedicated pricing](https://docs.together.ai/docs/dedicated-endpoints/pricing) · [dedicated models](https://docs.together.ai/docs/dedicated-endpoints/models) · [rate limits](https://docs.together.ai/docs/rate-limits) · [kimi-k2.6](https://www.together.ai/models/kimi-k26) · [kimi-k2.7-code](https://www.together.ai/models/kimi-k27-code) · [qwen3.5-397b](https://www.together.ai/models/qwen3-5-397b-a17b) · [gpt-oss-120b](https://www.together.ai/models/gpt-oss-120b)
- **OpenRouter**: [GET /api/v1/models](https://openrouter.ai/api/v1/models) (official JSON, captured 2026-08-31/09-01) · [limits](https://openrouter.ai/docs/api-reference/limits)
- **Mistral**: [pricing](https://mistral.ai/pricing) · [pricing/api](https://mistral.ai/pricing/api)
- **Other hosts**: [Cerebras gpt-oss](https://inference-docs.cerebras.ai/models/openai-oss) · [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) · [DeepInfra pricing](https://deepinfra.com/pricing) (secondary) · Groq via [layer3labs](https://www.layer3labs.io/guides/groq-pricing) / [computeprices](https://computeprices.com/providers/groq/models/gpt-oss-120b) (secondary) · Nemotron partners via [AI//COST](https://aicost.tools/llm-cost/nvidia/) (secondary)
- Internal: [`base-pricing-2026-08-31.md`](./base-pricing-2026-08-31.md) · snapshot [`pricing-snapshot/`](./pricing-snapshot/)
