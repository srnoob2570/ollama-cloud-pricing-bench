# Pricing baseline, the two systems (2026-08-31)

Sources from transition day: [ollama.com/pricing](https://ollama.com/pricing) (snapshot in
[`pricing-snapshot/`](./pricing-snapshot/)) and the official post
[ollama.com/blog/transparent-pricing](https://ollama.com/blog/transparent-pricing) dated
2026-08-31. Anything unverifiable is explicitly flagged in the final section.

## 1. New plan (token-based), in effect for new signups

| Plan | Price | Included credits/month | Concurrency |
|---|---|---|---|
| Free | $0 | "starter usage credits" (amount not published) | 1 |
| Pro | $20/month (or $200/year = $16.67/month) | $60 | 3 |
| Max | $100/month | $300 | 10 |
| Team | $500/month | $1,000 (shared, unlimited users) | 10 |
| Enterprise | custom | volume pricing | custom |

- Billing is per token at each model's rates ("Usage is measured in tokens
  at each model's rates", official FAQ).
- Credits do not accumulate ("unused credits do not roll over"); they refresh at the
  monthly reset. Once exhausted, usage draws from the extra pay-as-you-go balance
  (available even on Free).
- Pro advertises **"Fast mode (coming soon)"**: per-plan speed announced, not implemented;
  today "Speed depends on model size, architecture, and hardware optimization. […] Priority
  tiers with faster performance may be available in the future."
- The FAQ states that the old limits of 5 h sessions and 7-day weekly windows no longer
  apply to the new plans.

### Official per-1M-token table (input / cached input / output)

| Model | Input | Cached input | Output |
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

## 2. Legacy system, GPU-time (status of the existing subscriber as of 2026-08-31)

- Existing Pro/Max/Team subscribers "**remain on your current plan**" and may migrate
  voluntarily in billing settings; new signups already enter the new system.
- The study owner's legacy plan: **Max on GPU-time** (the only account available, see
  guardrail: do not migrate during collection).
- **Unverifiable**: how much GPU-time the quota represents (% of the 5 h session / % of the
  7-day weekly window / per-model levels); neither the rate in $ nor the level → GPU-seconds
  mapping was ever published. The official phrase removed from the dashboard said: *"Usage
  reflects actual utilization of Ollama's cloud infrastructure – primarily GPU time, which
  depends on model size and request duration"* (cited by
  [ollamatps.com/limits](https://ollamatps.com/limits/) and
  [BSWEN](https://docs.bswen.com/blog/2026-04-20-what-is-ollama-cloud/)).
- The per-request API reports `prompt_eval_count`, `eval_count` and durations in ns
  (`total_duration`, `load_duration`, `prompt_eval_duration`, `eval_duration`)
  ([docs.ollama.com/api/usage.md](https://docs.ollama.com/api/usage.md)); in streaming the
  usage fields arrive in the last chunk (`done: true`). **Not documented**: GPU-time per
  request, billed cost per request, cached tokens, quota headers, webhooks
  (issue [15663](https://github.com/ollama/ollama/issues/15663) still open; billing
  inconsistency `402 "extra usage only"` in
  [17639](https://github.com/ollama/ollama/issues/17639)).

## 3. Migration terms

- Migration is **voluntary** for existing subscribers (billing settings); irreversible.
- On plan change: "Your usage is reset: the new plan's full monthly amount is available
  right away."
- **Not stated** anywhere: any double-billing or proration period; the fate of extra
  credits already purchased under the legacy system; refund policy.

## 4. Ollama's public justification

Official post of 2026-08-31 (`/blog/transparent-pricing`):

> "GPU-time based billing was difficult to predict, especially as open models have grown
> much larger (Kimi K3 has 2.8 trillion parameters)."

Also: "no service fees and no 5-hour or weekly limits"; per-request cost visible in
account settings. Prior pressure context (not an Ollama quote):

- [Issue 17435](https://github.com/ollama/ollama/issues/17435): "Usage quota silently
  slashed ~70% with ZERO notification" (GPU-time "completely opaque" for an annual Pro user).
- [Issue 15663](https://github.com/ollama/ollama/issues/15663): request to expose
  quota/usage via the API; "the outlier" compared to OpenAI/Anthropic.
- [Issue 15741](https://github.com/ollama/ollama/issues/15741): large models behind a
  403 paywall without prior notice.

## 5. Open-weights comparables served in the cloud

Verified for **Kimi K2 Thinking** (1T params, open-weights, INT4). Secondary sources
(aggregators), not live pricing pages:

| Provider | Input | Output |
|---|---|---|
| Moonshot AI (direct) | $0.60 | $2.50 |
| Fireworks AI | $0.60 | $2.50 |
| Together AI | $1.20 | $4.00 |
| OpenRouter | $0.60 (+$0.15 cache) | $2.50 |

The direct comparable model on Ollama today would be kimi-k2.6 ($0.95/$4.00); no
comparable public data exists for kimi-k3. Not verified in this pass: Gemini API and
DeepSeek direct.

## 6. Unverifiable (explicit)

- The $ rate of the legacy system's GPU-time and the quota % → GPU-seconds mapping (the $
  value of "100 %" of the legacy plan): never published.
- The exact amount of the Free plan's "starter credits".
- Policy on extra credits already purchased when migrating; proration/double billing.
- Published per-plan TPM/RPM/tok/s: they do not exist; the speed reports (8–22 tok/s on
  heavy models) are third-party anecdotes.

## Sources

- [ollama.com/pricing](https://ollama.com/pricing) · local snapshot: [`pricing-snapshot/`](./pricing-snapshot/)
- [ollama.com/blog/transparent-pricing](https://ollama.com/blog/transparent-pricing)
- [docs.ollama.com/api/usage.md](https://docs.ollama.com/api/usage.md) · [docs.ollama.com/cloud](https://docs.ollama.com/cloud) · [docs.ollama.com/api/openai-compatibility.md](https://docs.ollama.com/api/openai-compatibility.md)
- [ollamatps.com/limits](https://ollamatps.com/limits/) · [docs.bswen.com](https://docs.bswen.com/blog/2026-04-20-what-is-ollama-cloud/)
- Issues [17435](https://github.com/ollama/ollama/issues/17435) · [15663](https://github.com/ollama/ollama/issues/15663) · [15741](https://github.com/ollama/ollama/issues/15741) · [17223](https://github.com/ollama/ollama/issues/17223) · [17639](https://github.com/ollama/ollama/issues/17639)
- [fireworks.ai kimi-k2-thinking](https://fireworks.ai/models/fireworks/kimi-k2-thinking) · [docs.together.ai](https://docs.together.ai/docs/kimi-k2-thinking-quickstart) · [cloudprice.net](https://cloudprice.net/models/moonshot-kimi-k2-thinking) · [llmreference.com](https://www.llmreference.com/model/kimi-k2-thinking/providers) · [threatfrontier.com](https://threatfrontier.com/articles/ollama-cloud-slowdown-hosted-open-model-performance)
