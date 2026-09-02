# Measurability budget: bracket size per workload (2026-09-01)

Resolves the research ticket "Measurability budget: bracket size per workload" of the
[Harness v1.1 map](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/27). It asks how
large a bracketed batch must be for its Δpp to clear the meter's tick, and what each grouping
option costs in quota and wall time. Purely local derivation: **no API calls, no spend**; every
number comes from the raw dataset already on disk (T1 + T2, protocol v2, table 2026-08-31).
Glossary: [`CONTEXT.md`](../../CONTEXT.md) (pp, tick, bracketed batch, anchor). Sizing input
for the composition grilling ticket; to be re-derived after the latency re-measurement ticket
lands (§7).

**Conventions** (same as [`medidor-vivo-2026-08-31.md`](./medidor-vivo-2026-08-31.md)):
`usage` values are the fraction exactly as the API returns it (`0.382` = 38.2 %); one step of
`0.001` = **0.1 pp = 1 tick**. Δpp below is quoted in pp (`dpp_weekly = 0.1` means 0.1 pp =
1 tick). The **weekly** window is the unit of account (the anchor), so the sizing targets are
weekly ticks; session ticks are given where the evidence only exists there.

**Data basis**: `batches/batches-T1-20260901T173354Z-a2b9e7c3.jsonl` (57 brackets, 1 rep per
cell) + `batches/batches-T2-20260901T195143Z-65da4e26.jsonl` (9 brackets) + the sibling
`runs/requests-*.jsonl` (465 request lines, token totals per `batch_id`). 66 brackets, 465
requests, **377,396 tokens** in total. Uncommitted, per the standing rule.

## 1. What the existing data can (and cannot) say

- **Only 9 of 66 brackets (13.6 %) moved any window**: 4 of 57 in T1 (53/57 measured
  `dpp_session = 0.0`, which is the opacity this budget quantifies) and 5 of 9 in T2.
- **Only 2 of 66 moved the weekly window**, the unit of account. Both are T2 `long_context`
  (~38.5–38.8K tokens each, both exactly +0.1 pp). Every other non-zero reading is
  session-only.
- **Cross-check (extraction validated)**: the bracket deltas sum exactly to the run's
  endpoint-to-endpoint movement: session 0.052 → 0.066 = +1.4 pp = 14 ticks = Σ of the 8
  non-zero session brackets; weekly 0.382 → 0.384 = +0.2 pp = 2 ticks = Σ of the 2 non-zero
  weekly brackets. Nothing double-counted, nothing dropped.
- **Read another way**: the entire T1 level (57 brackets, 77,735 tokens, 456 requests)
  produced **4 session ticks and 0 weekly ticks**. At today's scale, T1 is invisible in the
  unit of account.
- **Coverage is thin and skewed**: 15 of 19 T1 models have no non-zero bracket in either
  window; in T2, `minimax-m3` and `deepseek-v4-flash` have none. Where a bracket did move, the
  reading carries ±1 tick of endpoint-rounding error, so the 1-tick observations have huge
  relative uncertainty. **Every table below is an estimate with a stated spread, not a
  measurement.**

## 2. The measured pp/token curve (dp-vs-tokens) and its spread across models

Every bracket that moved a window (tokens from the request lines, Δpp as persisted):

| Model | Workload | tok in / out | in-share | tokens | Δpp sess | Δpp week | ticks s/w | session pp/1M | weekly pp/1M |
|---|---|---|---|---|---|---|---|---|---|
| `glm-5.1` | qa_short | 386 / 3,337 | 0.10 | 3,723 | 0.1 | 0.0 | 1 / – | 26.9 | – |
| `kimi-k3` | qa_short | 3,126 / 1,691 | 0.65 | 4,817 | 0.2 | 0.0 | 2 / – | 41.5 | – |
| `qwen3.5:397b` | qa_short | 488 / 7,977 | 0.06 | 8,465 | 0.1 | 0.0 | 1 / – | 11.8 | – |
| `glm-5.3` | throughput | 42 / 1,200 | 0.03 | 1,242 | 0.1 | 0.0 | 1 / – | 80.5 | – |
| `glm-5.3-flash` | long_context | 40,031 / 293 | 0.99 | 40,324 | 0.1 | 0.0 | 1 / – | 2.5 | – |
| `gpt-oss:20b` | long_context | 38,087 / 663 | 0.98 | 38,750 | 0.0 | 0.1 | – / 1 | – | 2.6 |
| `glm-5.3` | long_context | 40,031 / 155 | 1.00 | 40,186 | 0.2 | 0.0 | 2 / – | 5.0 | – |
| `kimi-k3` | long_context | 38,293 / 224 | 0.99 | 38,517 | 0.5 | 0.1 | 5 / 1 | 13.0 | 2.6 |
| `gpt-oss:20b` | long_generation | 1,418 / 13,776 | 0.09 | 15,194 | 0.1 | 0.0 | 1 / – | 6.6 | – |

The curve is **not one curve**: it splits by in/out mix, and the per-model spread inside each
family is ≥12×.

| Family | n | session pp/1M tokens | tokens per session tick |
|---|---|---|---|
| Prefill-dominated (in-share ≥ 0.9) | 3 non-zero | 2.5 – 13.0 (median 5.0) | 7.7K – 40K (median 20K) |
| Generation-carrying | 5 non-zero | 6.6 – 80.5 (median 26.9) | 1.2K – 15.2K (median 3.7K) |

- Output tokens are worth far more GPU-time than input tokens (same model, same meter:
  `glm-5.3` moved 2 session ticks for 40K input tokens on `long_context` and 1 tick for 1,200
  output tokens on `throughput`, a ≥30× per-token gap). A bracket's expected Δpp therefore
  depends on its in/out mix as well as its token count.
- **The cross-model spread is ≥12× inside a family** (2.5 → 80.5 session pp/1M overall), so a
  rate measured on one model does not transfer to another. This is the meter's opacity made
  concrete: pp is GPU-time, and GPU-time per token is a per-model, per-workload property that
  no other model's bracket can reveal.
- **Sub-tick upper bounds** (brackets that moved 0 ticks; rate < 0.1 pp / tokens). The only
  bounds tight enough to constrain sizing: `deepseek-v4-flash` @ long_context < 2.6 and
  `minimax-m3` @ long_context < 2.6 session pp/1M (both ~38.5K tokens, no move at all),
  `deepseek-v4-flash` @ long_generation < 3.8 and `glm-5.3-flash` @ long_generation < 4.3.
  The other 53 sub-tick brackets are too small to bound anything (bounds of 20–1,250 pp/1M).
- **Two windows disagree at ~40K tokens**: `glm-5.3-flash` @ long_context read +0.1 session /
  0.0 weekly; `gpt-oss:20b` read 0.0 session / +0.1 weekly on nearly the same token count. At
  this scale a single bracket cannot reliably distinguish 0 from 1 tick. The readings sit on
  the tick boundary. Where both windows moved for the same bracket (`kimi-k3` @ long_context)
  the ratio is 5.0 session : 1.0 weekly; over the whole run's endpoints it is 7.0 (14 : 2).
  The structural 5 h : 7 d window ratio would predict 33.6×. It does not hold, and why is
  unmapped (fog).
- **Too thin to say per model**: no model has more than one non-zero bracket, so there is no
  within-cell spread at all, only the across-model spread above. 15 T1 models and 2 T2 models
  have no rate estimate of their own; for them the tables use the family median and say so.

## 3. Tokens per weekly tick (the sizing conversion)

| Family | Rate (weekly pp per 1M tokens) | Basis | Tokens per weekly tick |
|---|---|---|---|
| Prefill-dominated | **2.6** | directly observed, n = 2 brackets (both exactly 1 tick / ~38.6K tok) | **38.5K** |
| Generation-carrying (central) | **5.4** | session median 26.9 ÷ measured session:weekly ratio 5 | **18.5K** |
| Generation-carrying (spread) | 0.9 – 16.1 | session 6.6 – 80.5 ÷ ratio 5 | **6.2K – 76K** |

Margin rule for the composition ticket: a bracket whose **expected** Δpp is exactly 3 ticks
reads 3; below 3.0 it reads 3 only part of the time (each endpoint rounds to the 0.1 pp grid,
so a reading carries ±1 tick of phase error). **Plan the expected Δpp at ≥3.5 ticks** where a
3-tick reading must be reliable, and never interpret a single 1-tick reading as more than
"0–2 ticks of true movement".

## 4. Sizing table per workload at ≥1, ≥3 and ≥10 ticks

Per workload, **median model** of the level (tokens/rep measured from the raw data; the
level's slowest and fastest models span the columns "expected Δpp" by roughly the family
spread). Δpp in weekly ticks; option (c) = raise the workload's own request count from one rep.

| Level | Workload | Reqs/rep | tok/rep | Δpp @ 1 rep | Δpp @ 5-rep pool | n-multiplier for 1 / 3 / 10 ticks | requests per bracket at 1 / 3 / 10 ticks |
|---|---|---|---|---|---|---|---|
| T1 | calibration | 3 | 315 | 0.02 | 0.09 | ×59 / ×177 / ×588 | 177 / 531 / 1,764 |
| T1 | qa_short | 20 | 2,771 | 0.15 | 0.75 | ×7 / ×21 / ×67 | 140 / 420 / 1,340 |
| T1 | throughput | 1 | 952 | 0.05 | 0.26 | ×20 / ×59 / ×195 | 20 / 59 / 195 |
| T2 | long_context | 1 | 38,687 | 1.01 | 5.03 | ×1 / ×3 / ×10 | 1 / 3 / 10 |
| T2 | long_generation | 1 | 23,400 | 1.26 | 6.32 | ×1 / ×3 / ×8 | 1 / 3 / 8 |
| T2 | multi_turn *(design)* | 8 | 7,200 | 0.39 | 1.94 | ×3 / ×8 / ×26 | 24 / 64 / 208 |
| T2 | tool_calling *(design)* | 6 | 5,220 | 0.28 | 1.41 | ×4 / ×11 / ×36 | 24 / 66 / 216 |
| T2 | reasoning *(design)* | 1 | 6,000 | 0.32 | 1.62 | ×4 / ×10 / ×31 | 4 / 10 / 31 |
| T2 | ratio_in *(design)* | 1 | 50,120 | 1.30 | 6.52 | ×1 / ×3 / ×8 | 1 / 3 / 8 |
| T2 | ratio_out *(design)* | 20 | 14,000 | 0.76 | 3.78 | ×2 / ×4 / ×14 | 40 / 80 / 280 |

*(design) = never ran; tokens/rep are the nominal fixture values from `workloads.py` inflated
by the measured design→actual ratios of the two T2 workloads that did run (prefill ×1.28,
generation ×2.5–3.8), so those rows carry the widest spread of the table. T3 is out of scope
here; for scale only, its fixtures are so large that a single request is already worth ~1.7–9.7
weekly ticks at these rates (design tokens, no measurement).

## 5. Grouping options: what each one costs

The n=5 repetitions are a statistical requirement (median, IQR, p95), and they are computed
from **per-request rows**: pooling the bracket only pools the *meter reading*. Pooling
therefore costs no tokens beyond the n=5 plan and loses only the per-rep Δpp, which is
unobservable below the tick anyway (53/57 T1 brackets prove it). Settles serialize: no other
model's requests may run inside another bracket's 90 s settle window, so the bracket count,
not the token count, is what wall time is made of.

### T1 (19 models × 3 workloads = 57 cells)

| Option | Brackets (settles) | Settle time | Tokens (quota) | Request wall | Total wall | Expected Δpp per bracket (weekly ticks) |
|---|---|---|---|---|---|---|
| Today as run (1 rep, 1 bracket/cell) | 57 | 85.5 min | 77,735 | 44.6 min | **130 min** | 53/57 brackets = 0; level total 4 session / 0 weekly |
| (a) 5 reps pooled per (workload, model) | 57 | 85.5 min | 388,675 | 223 min | **308 min** | qa_short 0.16–2.29 (median 0.75) · calibration 0.02–0.29 · throughput 0.11–0.43 |
| (b) 5 reps, level pooled per model | 19 | 28.5 min | 388,675 | 223 min | **252 min** | 0.32–3.00 across models (median 1.05) |
| (b) at n = 15 (3× the plan) | 19 | 28.5 min | 1,166,025 | 669 min | **~11.6 h** | 0.97–9.0 (median 3.2) |

Per-model detail for option (b) at n = 5 (expected weekly ticks at the generation-family
central rate; the per-model rate is the dominant unknown: the four models with their own
non-zero bracket are marked):

| Model | tok/rep (level) | 5-rep pooled | Expected Δpp | n-multiplier for 3 ticks (tokens) |
|---|---|---|---|---|
| `qwen3.5:397b` | 11,114 | 55,570 | 3.00 | ×1 (55.6K) |
| `minimax-m3` | 6,867 | 34,335 | 1.85 | ×2 (68.7K) |
| `kimi-k3` * | 6,568 | 32,840 | 1.77 | ×2 (65.7K) |
| `minimax-m2.7` | 5,526 | 27,630 | 1.49 | ×3 (82.9K) |
| `glm-5.1` * | 5,324 | 26,620 | 1.44 | ×3 (79.9K) |
| `nemotron-3-nano` | 4,864 | 24,320 | 1.31 | ×3 (73.0K) |
| `glm-5.2` | 4,462 | 22,310 | 1.20 | ×3 (66.9K) |
| `kimi-k2.6` | 4,348 | 21,740 | 1.17 | ×3 (65.2K) |
| `glm-5.3` * | 4,056 | 20,280 | 1.10 | ×3 (60.8K) |
| `gpt-oss:20b` | 3,888 | 19,440 | 1.05 | ×3 (58.3K) |
| `gpt-oss:120b` | 3,662 | 18,310 | 0.99 | ×4 (73.2K) |
| `glm-5.3-flash` | 3,042 | 15,210 | 0.82 | ×4 (60.8K) |
| `nemotron-3-super` | 2,656 | 13,280 | 0.72 | ×5 (66.4K) |
| `nemotron-3-ultra` | 2,480 | 12,400 | 0.67 | ×5 (62.0K) |
| `kimi-k2.7-code` | 2,442 | 12,210 | 0.66 | ×5 (61.1K) |
| `deepseek-v4-flash` | 2,171 | 10,855 | 0.59 | ×6 (65.1K) |
| `deepseek-v4-pro` | 1,667 | 8,335 | 0.45 | ×7 (58.3K) |
| `gemma4` | 1,397 | 6,985 | 0.38 | ×8 (55.9K) |
| `mistral-large-3` | 1,201 | 6,005 | 0.32 | ×10 (60.1K) |

\* has a non-zero bracket of its own (§2); its expected Δpp is the observed one scaled, not a
borrowed rate.

Wall-time warning for option (c) and for raising n anywhere: T1's request wall is dominated by
the slow models: `nemotron-3-ultra` runs 24.3 min of request wall per rep, so its
level-pooled bracket is 121 min at n = 5 and 607 min at n = 25. A per-model n (rather than one
n for the level) is the only way raising n stays affordable; the methodology's "reduce T3's n
first" fallback logic applies here too.

### T2 (6-model slate × 7 workloads = 42 cells; 5 workloads never ran, design tokens)

| Option | Brackets (settles) | Settle time | Tokens (quota) | Expected Δpp per bracket (weekly ticks) |
|---|---|---|---|---|
| 1 rep, 1 bracket per cell (the T1-style plan) | 42 | 63 min | 867,762 | long_context 1.01 · long_generation 1.26 · ratio_in 1.30 · ratio_out 0.76 · multi_turn 0.39 · reasoning 0.32 · tool_calling 0.28 |
| (a) 5 reps pooled per (workload, model) | 42 | 63 min | 4,338,810 | long_context 5.03 · long_generation 6.32 · ratio_in 6.52 · ratio_out 3.78 · multi_turn 1.94 · reasoning 1.62 · tool_calling 1.41 |
| (b) 5 reps, level pooled per model | 6 | 9 min | 4,338,810 | **18.8 – 39.0** (723K tokens per model) |
| (b) at 1 rep only | 6 | 9 min | 867,762 | **3.8 – 7.8** (144.6K tokens per model) |

T2's measurability lives entirely in the pooling: at one rep per cell only `long_context`,
`long_generation` and `ratio_in` clear even 1 tick alone, while the level pooled per model
clears ≥3 weekly ticks **at one rep** (9 minutes of settle for the whole level). The five-rep
version clears 10 ticks and adds the statistical n. The tokens are the same either way.

## 6. Workloads where no reasonable bracket clears 3 ticks (opacity findings)

- **T1 `calibration`**: sub-tick at every reasonable scale. Its 5-rep pooled bracket expects
  0.09 weekly ticks; reaching 3 needs 531 requests (177 fixture cycles) in one bracket, and 10
  ticks needs 1,764. The workload whose name promises a pp/token calibration is the one the
  meter cannot see. **No reasonable bracket clears 3 weekly ticks.**
- **T1 `throughput`**: 5-rep pooled expects 0.26 ticks; 3 ticks needs 59 cycles of a
  single-request fixture (59 requests, 56K tokens) in one bracket, which stops being a
  throughput sample. **No reasonable bracket clears 3 weekly ticks without re-purposing the
  fixture.**
- **T1 `qa_short`**: 5-rep pooled expects 0.16–2.29 weekly ticks (median 0.75): clears 1 tick
  for most models, 3 for none of them except at n-multiplier ×21 (420 requests, ~58K tokens
  for the median model). **Does not clear 3 weekly ticks at n = 5.**
- **T1 as a level**: even fully pooled per model (option b), n = 5 gives 0.32–3.00 expected
  ticks (median 1.05): only the token-heaviest model reaches 3, and without margin. Clearing 3
  for every model needs n ≈ 5–50 pooled per model (55–83K tokens each, ~1.17M tokens for the
  level), a 15× quota increase over today's T1 for a resolution the anchor then reads as
  ~$0.07 of the $100/mo plan.
- **T2 unrun workloads at n = 5, per-cell brackets**: `multi_turn` (1.94), `tool_calling`
  (1.41) and `reasoning` (1.62) are sub-3-tick on design tokens; only pooling (option b) or
  extra reps rescue them, and their rows rest on nominal fixture values, not measurement.
- **15 of 19 T1 models, 2 of 6 T2 models**: no non-zero bracket in any window, so their
  per-model expected Δpp in every table above is a family-median estimate, not a measurement.
  This is the deepest opacity finding: the dataset cannot say how expensive these models are
  per token, only that they are not free.

## 7. Caveats, and what re-derives after the latency re-measurement ticket

1. **The sample behind every rate is 9 brackets**, 2 of them weekly. The 2.6 weekly pp/1M
   prefill rate rests on exactly two brackets that both landed on the same tick; the
   generation-family weekly rate is a session reading divided by a 5:1 conversion observed
   once. Treat the tables as order-of-magnitude budgets with a ±2× band per family, and the
   per-model rows as family medians unless marked.
2. **The 90 s settle may clip the weekly window.** The live meter test measured the weekly %
   lagging ~76–83 s, inside the settle but barely, and this dataset shows weekly readings
   systematically quieter than session ones (2 of 9 non-zero brackets). If part of the weekly
   silence is lag rather than accounting, the weekly rates above are underestimates and the
   required brackets shrink. The latency re-measurement ticket (capped live re-measurement of
   the meter's latency, replacing the fixed 90 s with a poll loop) owns that question; this
   budget must be **re-derived from the next run's brackets after it lands**.
3. **The session:weekly conversion (5:1 bracket-level, 7:1 endpoint-level) is one observation
   and contradicts the structural 33.6× ratio** of the two windows' lengths. If the composition
   ticket weighs reading the session window instead (same 0.1 pp tick, same settle, 5–7× more
   movement per token: T1 pooled per model at n = 5 becomes ~5.2 session ticks, median), it
   must first accept that a session pp has no anchor. The $/pp bridge is built on the weekly
   window, and the session window's GPU-time value is unmapped fog.
4. **Quantization, not noise, dominates at these scales.** Every Δpp reading is a whole number
   of ticks with ±1 tick of phase error, so "expected Δpp" in the tables is a central
   estimate, not a bound; the margin rule in §3 is the composition ticket's protection.
5. **What feeds the composition grilling ticket**: the hybrid decision (per-rep brackets only
   where one rep clears ≥3 ticks; pool where needed) resolves, from this data, as follows: T2
   `long_context` / `long_generation` / `ratio_in` can stand alone at 1 rep (1.0–1.3 ticks) or
   pooled per level at 3.8–7.8 ticks with 6 settles; T1 cannot clear the tick per workload at
   n = 5 under any grouping and must either pool per model (19 settles, median 1.05 expected
   ticks, accept 1-tick resolution), raise n per model (n ≈ 5–50 for 3 ticks, ≈15× the quota,
   wall-time-bound by `nemotron-3-ultra`), or give up weekly-tick resolution for T1 and say so
   as an opacity finding. Attribution granularity and
   resolution trade against each other exactly where the tokens run out.
