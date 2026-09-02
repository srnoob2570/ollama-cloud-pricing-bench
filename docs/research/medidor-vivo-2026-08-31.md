# Live verification of the usage meter (2026-08-31)

Resolves the ticket "Verify the usage meter live", the only run with a real account of
this map (map guardrail: ~10 trivial requests). Actual spend: 6 requests to
`nemotron-3-nano:30b` (156 in + 72 out tokens, ~3.4 s of aggregate `total_duration`).

Raw logs: [`logs/medidor-vivo-2026-08-31/reads.jsonl`](./logs/medidor-vivo-2026-08-31/reads.jsonl)
(and sibling `requests.jsonl`). No credentials. Theoretical complement:
[`medidor-uso-ollama.md`](./medidor-uso-ollama.md), which this doc corrects live.

## 1. The API key (Bearer) DOES authenticate the meter

`GET https://ollama.com/api/usage` with `Authorization: Bearer <API key>` → **200**.
Without auth → 401 (`{"error":"invalid credentials"}`); `/api/usage/session` and
`/api/usage/weekly` → 404. The documentary research said only the session cookie was
proven. Now **corrected**: the owner's original proposal (polling the endpoint between
requests) works directly, without a browser.

## 2. What exactly it exposes (observed structure)

```json
{"activity": {"cost": "0.00000", "period": {"type": "last_4_weeks", ...}, "models": []},
 "limits": {
   "session": {"usage": 0.234, "models": [{"name": "glm-5.3-flash", "request_count": 962}, ...]},
   "weekly":  {"usage": 0.146, "models": [{"name": "kimi-k3", "request_count": 209}, ...]}}}
```

- `limits.session.usage` and `limits.weekly.usage`: **quota fraction** (0.234 = 23.4 %),
  observed resolution **0.001 (0.1 %)** per read. No GPU-sec, no tokens, no dollars per
  request.
  ⚠️ **Convention of this doc and of the tables**: `usage` values are always written as
  the fraction exactly as the API returns it (`0.235` means 23.5 %; one step of `0.001`
  = 0.1 percentage points). Do not read them as "0.235 %".
- `limits.*.models[]`: `request_count` **integer per model** (name = catalog id, with
  tag: `nemotron-3-nano:30b`).
- `activity.cost`: balance in $ at 5 decimals, stayed invariant ("0.00000")
  throughout the experiment, quota-based usage included. Hypothesis: it only accumulates
  with the *extra* pay-as-you-go balance, not verifiable without funds (remains in the
  map's fog).

## 3. Measured lag and quantization

**Convention**: `sess`/`week` are fractions exactly as the API returns them:
`0.235` = 23.5 % of the quota.

| Read | t (s) | sess | week | nemotron reqs |
|---|---|---|---|---|
| baseline / pre_r1 | 0.4 / 2.7 | 0.234 | 0.146 | — |
| lag+0s (r1 completed 0.35 s earlier) | 3.9 | 0.234 | 0.146 | **1** |
| lag+39s | 44.4 | 0.234 | 0.146 | 1 |
| lag+69s | 74.7 | **0.235** | 0.146 | 1 |
| pre_r3 | 83.2 | 0.235 | **0.147** | 2 |
| pre_r4..r6 | 92–108 | 0.235 | 0.147 | 3,4,5 |
| settle_final (+45 s) | 161.4 | **0.236** | 0.147 | **6** |

- **`request_count` registers almost instantly**: r1 was counted ~1 s after completing,
  long before the % moved. It is the reliable per-request attributor and the
  acknowledgment that a request has already entered the accounting.
- **The quota % lags ~60–90 s** (first session change ∈ (39 s, 69 s] after r1; weekly
  ~76–83 s) and **quantizes in 0.1 % steps**: the 6 requests moved only +0.002 (session)
  and +0.001 (weekly).
- With these data, attributing Δ% to an individual request is ruled out (as the
  research anticipated); what this ticket adds is that the per-model counter does not
  lag and is exact: `nemotron-3-nano:30b` → 1,1,2,3,4,5,6 with 6 requests.

## 4. Derived measurement protocol (input for "Cost model" and "Measurement protocol")

**Primitive = bracketed batch**, not an individual request:

1. Meter read (full raw JSON saved).
2. Batch of N requests, one model or few, all `request_count` values present in the
   pre read.
3. Immediate registration confirmation via Δ`request_count` (≈1 s), also useful as a
   sanity check that the whole batch was billed.
4. Wait **≥ 90 s** and second read: quota Δ% per batch (not per request).
5. The tokens per request (`prompt_eval_count`/`eval_count` from the API) are
   cross-checked against the batch's Δ% to build the tokens↔quota mapping; the resolution
   of that mapping is **0.001 of quota**, so batches must be large relative to the
   quantum (e.g. ≥ 30× the content of a trivial batch like this one, or the Δ% is
   indistinguishable from rounding).

Corollary for the formulas: the error of any per-batch Δ% is ±0.001 (resolution) and the
billing clock stabilizes at ~90 s. Benchmark runs must sleep ~90 s between batches or
accept carryover from one batch to the next.

## 5. Side discoveries

- **Catalog ids with tag** (`nemotron-3-nano:30b`, `gemma4:31b`, `deepseek-v4-pro:0813`,
  `mistral-large-3:675b`…) versus the price table without tags (`nemotron-3-nano`):
  the harness needs a prefix mapping rule.
- **Owner's real usage** (baseline context): glm-5.3-flash dominates with 2391 weekly
  requests and 962 session ones, perfect as anchor model and as a proxy for "a user
  with many small requests".
- `activity.cost` (a $ field with 5 decimals) is the natural candidate for a direct cost
  read if it ever accumulates; today, at 0.00000, nothing can be confirmed.

## 6. Post-closing addition: the owner's second read (~23:04, real usage in between)

Owner's read ~25 min after the experiment (42 real glm-5.3-flash requests in between,
1004/2433 in their session/week versus 962/2391 when this ticket started):

- **Natural calibration experiment**: 42 real glm-5.3-flash requests moved the session
  quota +0.005 (0.236 → 0.241, i.e. 23.6 % → 24.1 %), ~0.00012 %/request on a
  mid-size "flash" model, consistent
  with the 0.001 quantum per batch. Second tokens↔quota calibration point (with no
  known tokens per request for those 42, it is an order-of-magnitude figure,
  not an exact ratio).
- **`activity.period` is a rolling 4-week window** (`type: "last_4_weeks"`,
  `starting_at: 2026-08-10T00:00:00Z`, `ending_at` advances with each call). Data for
  the **anchor** to dollars: the limits' "monthly quota" is not a calendar month; the
  activity period rolls.
- **"web search" counts as a pseudo-model** in `request_count` (9 session / 51 weekly
  for the owner). The harness must decide whether to count it or filter it out.
- `nemotron-3-nano:30b` stayed stable at 6 in both tracks exactly as the experiment
  left it: the counters do not decay or revert.

## 7. Questions that remain open (fog of the map)

- Does `activity.cost` increase per request when an extra balance is active? (requires
  funds; if yes, it would be the missing direct cost read).
- Is the ~60–90 s lag backend batching or propagation? (irrelevant to the protocol as
  long as the settle is awaited, which is why it does not become a ticket).
