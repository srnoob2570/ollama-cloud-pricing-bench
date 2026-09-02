# ¿Cuánto vale un pp de sesión en dólares? Conversión sesión↔semanal (2026-09-01)

Resolves the research ticket "¿Cuánto vale un pp de sesión en dólares? (conversión
sesión↔semanal)" of the [Harness v1.1 map](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/27).
The $/pp anchor is built on the **weekly** window ($0.2302/pp, `methodology-v1.md` §3); the
session window is a saturation constraint with no independent anchor, and the composition
ticket fixed it as a secondary resolution signal. This ticket asks whether a session pp can be
priced anyway, via the measured session:weekly conversion. Purely local derivation: **no API
calls, no spend**; every number is recomputed from the raw lines already on disk. Extends §2
and §7 of [`presupuesto-medibilidad-2026-09-01.md`](./presupuesto-medibilidad-2026-09-01.md)
(the session:weekly evidence and caveats live there) and the co-landing point of
[`latencia-medidor-2026-09-01.md`](./latencia-medidor-2026-09-01.md) (the super-tick probe).

**Conventions** (same as the budget doc): `usage` values are the fraction exactly as the API
returns it; one step of `0.001` = 0.1 pp = **1 tick**. The conversion **R** below is
*session ticks : weekly ticks* observed for the same spend. Its reciprocal prices a session
pp in weekly-pp terms. Δpp is quoted in ticks unless pp is stated.

**Data basis**: `batches/batches-T1-20260901T173354Z-a2b9e7c3.jsonl` (57 brackets) +
`batches/batches-T2-20260901T195143Z-65da4e26.jsonl` (9 brackets) + the sibling
`runs/requests-*.jsonl` (465 requests, protocol v2, table 2026-08-31): 66 brackets, 465
requests. The super-tick probe's post-landing reads are in `/tmp/weekly-probe.jsonl` (outside
the repo; its pre-read lives only in the live transcript recorded in the latency doc).
Uncommitted, per the standing rule.

## 1. What the raw lines confirm before any conversion

- **9 of 66 brackets moved any window** (4 of 57 in T1, 5 of 9 in T2), re-summed from the
  raw `medidor_pre`/`medidor_post` readings, matching the budget doc's §1 census.
- **Exactly one bracket-level co-landing point**: `kimi-k3` @ `long_context` read session
  0.060 → 0.065 (+0.5 pp = 5 ticks) and weekly 0.383 → 0.384 (+0.1 pp = 1 tick). Every other
  mover is single-window: 7 session-only (weekly sub-tick) and 1 inverted,
  `gpt-oss:20b` @ `long_context`, session 0.058 → 0.058 (0 ticks) with weekly 0.382 → 0.383
  (+1 tick). See §5.
- **Registration is clean in both windows** (the conversion's premise): per bracket, the
  model's `request_count` delta is identical in the session and weekly windows: 50 of 66
  brackets have same-name pre/post reads in both windows, **0 mismatches**; the other 16 are
  the model's first requests in a window this session (the list grows at the post read; the
  one apparent mismatch was `gpt-oss:20b` @ `qa_short` first appearing in the session list).
  Same requests, both windows, so the two usage figures are two denominators over the same
  GPU-time, which is what makes a conversion meaningful at all.
- **`activity.cost` carries no signal here**: all 132 meter reads (66 pre + 66 post) show
  `0.00000`. The only direct-dollar field the meter exposes is invariant under included
  quota; verifying it needs extra-balance funds (the fog of the cost-effectiveness map, out
  of this map's scope).
- **Correction to the budget doc**: its §1 sentence says T1 produced "4 session ticks"; the
  raw lines give **5** (0.052 → 0.057 across 4 non-zero brackets summing 1+2+1+1 = 5 ticks;
  the bracket table in its §2 is right and its own sum confirms it). Nothing downstream
  changes. The combined endpoint ratio below uses endpoint readings, not that sentence.

## 2. The conversion's evidence, recomputed from the raw lines

| # | Point | session read | weekly read | ticks s / w | R | Kind |
|---|---|---|---|---|---|---|
| 1 | `kimi-k3` @ `long_context` (T2 bracket) | 0.060 → 0.065 | 0.383 → 0.384 | 5 / 1 | **5.0** | bracket, both windows, in-share 0.99 (38,293 in / 224 out) |
| 2 | Super-tick probe, same cell | (0.087 →) 0.093 | (0.387 →) 0.388 | 6 / 1 | 6.0 | probe, same cell, same fixture: corroboration, not an independent point; carries ±1 session tick of concurrent traffic (the +1 tick at t = 28.1 s) |
| 3 | T2 run endpoints | 0.057 → 0.066 | 0.382 → 0.384 | 9 / 2 | **4.5** | run aggregate (mixture of prefill and generation-carrying brackets) |
| 4 | T1+T2 combined endpoints | 0.052 → 0.066 | 0.382 → 0.384 | 14 / 2 | **7.0** | both runs share one session window (17:33–20:14 Z, continuous non-decreasing usage), aggregate including T1's generation-family session-only ticks |
| 5 | T1 run endpoints | 0.052 → 0.057 | 0.382 → 0.382 | 5 / 0 | > 4 | **bound only**: no weekly movement at the endpoints; 5 session ticks with < 1 weekly tick of true movement |
| 6 | `gpt-oss:20b` @ `long_context` | 0.058 → 0.058 | 0.382 → 0.383 | 0 / 1 | inverted | anomaly, see §5 |

Bracket deltas re-sum exactly to the endpoint movement in both windows for both runs (T1
+5/+0, T2 +9/+2). Nothing double-counted.

**The best-supported conversion is R ≈ 5–7 : 1**. The same GPU-time that moves the weekly
window 1 tick moves the session window 5–7 ticks. The three readable ratio points span
4.5–7.0 (central ≈ 5.5); the one single-cell point is 5.0 and its same-cell probe reads 6.0.
The 7 session-only movers are *consistent* with the band (each reads 1–2 session ticks, whose
R = 5–7 weekly equivalent of 0.14–0.4 ticks is legitimately sub-tick) but cannot confirm it.
They would equally tolerate R ≈ 3 or R ≈ 20.

**What the endpoint ratios can and cannot carry**: they are the only whole-dataset readings.
Each is one aggregate over one session window and one weekly window, so they weight every
moved tick in the dataset but as a mixture of cells and in/out mixes, not a replication of
any single cell. The T2 mixture (4.5) and the combined (7.0) bracket the single-cell 5.0 from
both sides, which is weak evidence that R does not swing wildly across mixes. But it is
n = 1 run pair, and the combined ratio's higher value is driven by T1's session-only ticks,
whose weekly side is unmeasurable (bound only). **n = 2–3 points is thin: every number in this
doc is an order-of-magnitude estimate with a stated spread, not a measurement.**

## 3. The derived session $/pp, provisionally stable, unanchored, secondary

`session $/pp = weekly $/pp ÷ R = $0.2302 ÷ R`:

| R | session $/pp | session tick (0.1 pp) | Read |
|---|---|---|---|
| 4.5 | **$0.0512** | $0.0051 | T2 run endpoints (mixture) |
| 5.0 | **$0.0460** | $0.0046 | `kimi-k3` @ `long_context` bracket (cleanest single cell) |
| 5.5 | **$0.0419** | $0.0042 | central of the three read points |
| 6.0 | **$0.0384** | $0.0038 | super-tick probe (same cell, corroboration) |
| 7.0 | **$0.0329** | $0.0033 | T1+T2 combined endpoints (mixture) |
| 33.6 | $0.0069 | $0.0007 | *what the structural ratio would have implied, rejected in §4* |

**Headline: a session pp is worth ≈ $0.033–$0.051 (central ≈ $0.04); a session tick
≈ $0.0033–$0.0051, roughly 1/5 to 1/7 of the weekly tick's $0.0230.** Reporting as a
**derived, unanchored secondary value** is defensible; the reporting decision itself rides the
synthesis ticket with these numbers in hand. Two conditions on that value:

- It prices a session pp by the weekly-window dollars of the same GPU-time, nothing more.
  It is *not* evidence about the session window's own allowance or saturation economics; the
  5 h limit remains a hard constraint, not a price.
- It inherits the anchor's basis (`$100/mo ÷ 4.345 weeks ÷ 100 pp`), and the conversion's
  spread on top: the honest figure is the band, never the central point alone.

## 4. Against the structural 5 h : 7 d ratio

The windows' lengths predict R = 7 d / 5 h = **33.6×**. The data says it does not hold, and by
how much:

- The measured band is **4.5–7.0×**. The structural ratio over-predicts by **4.8×–7.5×**
  (33.6 ÷ 7.0 to 33.6 ÷ 4.5).
- The exclusion survives the readings' ±1-tick phase error at the endpoint level: the combined
  read (14 : 2) has true movement in (13–15) : (1–3) ticks → R ∈ (4.3, 15); the T2 mixture →
  R ∈ (2.7, 10). The structural 33.6 lies outside both envelopes. The single 5:1 bracket alone
  cannot exclude it (its weekly read is 1 tick, true (0, 2)), but two independent endpoint
  aggregates can.
- Equivalently: the session pp behaves as a share of a **~24–37 h** capacity (7 d ÷ R), not a
  5 h one. **Why is unmapped (fog)**: window capacity is not observed, only priced backwards
  from these ratios; no mechanism is claimed.

## 5. The anomaly the conversion does not explain

`gpt-oss:20b` @ `long_context` read **0 session ticks with +1 weekly tick** on 38,087 in /
663 out. Under a constant R ≈ 5–7, that request should have read ~5–7 session ticks, and it
read none (0.058 → 0.058 exactly). Under the registration semantics (both pp figures already
recalculated when the counts register), this is not lag. With every reading sitting on the
tick boundary (±1 tick of phase error per endpoint), the honest readings are:

1. **Phase noise at 1 tick**: the true movement is somewhere in (0–1) : (0–2) ticks, and this
   cell simply sits across the boundary (the budget doc already flags that `glm-5.3-flash` and
   `gpt-oss:20b` disagree in both directions at ~40K tokens).
2. **R is not constant across cells**: this cell's session-vs-weekly attribution genuinely
   differs, which would cap the conversion's confidence at order-of-magnitude.

n = 1 inverted point cannot distinguish these. It does not overturn the band (three other
points agree inside it, and its own phase box reaches R ≈ 2 at the edge), but it is the
concrete reason the session $/pp must ship as a band, not a point.

## 6. Does the conversion split by model or in/out mix? Unanswerable

The pp/token curve splits ≥12× per model inside a family (budget doc §2); whether R splits the
same way cannot be resolved from this dataset. The one clean co-landing point is
prefill-dominated (in-share 0.99); the probe reads the same cell; the only other R values are
mixtures that blend families; and every generation-carrying mover has no weekly read at all.
There is no within-cell spread and no second single-cell point in a different family. Any
split claim from 1 cell + 2 mixtures would be pure over-reading. The hybrid scheme's per-cell
brackets are what make this question answerable (§7).

## 7. Caveats, and what the hybrid scheme's brackets re-derive

1. **The sample is 1 bracket + 1 run pair + 1 corroborating probe.** Three readable R values
   (4.5, 5.0, 7.0) with ±1 tick of phase error each, one inverted point, and 7 movers too
   coarse to confirm anything. Treat R as **5–7 with a ±2× band**; the session $/pp as
   $0.033–$0.051 (order of magnitude, $0.04 central); never as a calibrated rate.
2. **Both windows' usage figures re-derive every bracket under the registration settle**:
   the latency re-measurement's owner-corrected semantics: registration, not pp movement,
   settles; sub-tick pp deltas are legitimate. The hybrid composition therefore turns every
   future bracket into a candidate co-landing point: per-cell brackets read both windows after
   the same ≤6 s registration, and pooled brackets clear 3.8–7.8 weekly ticks per T2 model at
   one rep, with session movement ~5–7× that on the same ticks. **Co-landing points arrive in
   bulk**: n goes from 1 bracket to ~30 per run, the spread gets measured instead of assumed,
   and the mix-split question (§6) opens up.
3. **Re-derivation note for methodology v1.1**: re-derive R from the first v3 run's brackets
   (endpoint-to-endpoint per run *and* per bracket), keep the weekly window as the anchor and
   the unit of account, and carry the session $/pp only as a **derived secondary value** with
   its measured spread, never as an independent anchor, and never as the price of the
   session's saturation constraint. If the hybrid brackets reproduce R ≈ 5–7, the band
   tightens; if they spread, the session window returns to resolution-only (the composition
   ticket's standing decision) and the question closes as an opacity finding.
4. **`activity.cost` stays fog** (§1): the only direct-dollar field is invariant `0.00000`
   under included quota; a direct check of any $/pp figure in this doc needs extra-balance
   funds, which the cost-effectiveness map keeps out of scope and no map in flight proposes
   to spend.
## 8. Live verification (owner-run, same day): R = 6.22 at full price, and the band holds

The owner upgraded the ticket mid-flight with a **live, owner-run test on kimi-k3**
(10 requests authorized; the owner ran the script personally so the agent never billed a
request). Two protocol corrections came out of it, both now part of the record:

1. **Identical prompts cache** (owner-caught): a first variant fired 5 identical
   `long_context` requests. They cost **+3 session ticks total** (~0.6/request) against the
   full price of ~5-6/request, and a warm-cache rerun cost +3 more with zero weekly
   movement. The prefix cache on kimi-k3 discounts an exact-prefix replay to ~11 % of full
   price (~9× cheaper), a live prefix-replay measurement in the cache-calibration
   workstream's domain, worth its own follow-up.
2. **The fix is unique prefixes**: every request prepends a fresh ~400-word random nonce, so
   the cache (prefix-keyed) never reuses anything across requests or runs. Every request
   bills at full price.

**The verified bracket** (owner-run, 10 unique-prefix requests on kimi-k3, the exact T2
cell + nonce, `glm-5.3-flash` count flat pre→post = zero contamination):

| Signal | Value |
|---|---|
| Tokens billed (sum of the done frames) | **402,150** (39,891-39,893 in + 197-564 out per request) |
| dpp_session | **+0.056 usage = 5.6 pp = 56 ticks** |
| dpp_weekly | **+0.009 usage = 0.9 pp = 9 ticks** |
| **R (session : weekly)** | **6.22** (56/9; the 10th weekly tick lost to sub-tick phase) |
| Registration | kimi-k3 `request_count` **+10 in BOTH windows** (pre = post-confirm) |
| Confirm read | identical to POST in both windows, the bracket closed settled |
| Per-request cost | 5.6 session ticks / 0.9 weekly ticks per request, uniform within tick resolution |

**Recalculated rates for kimi-k3 at full price** (vs the T2 bracket's single point):

| Rate | This run | T2 bracket (single request) |
|---|---|---|
| session pp/1M | **13.93** (5.6 pp / 0.402 M) | 12.98 |
| weekly pp/1M | **2.24** (0.9 pp / 0.402 M) | 2.60 |

**The session's dollar value, tightened**: session $/pp = $0.2302 ÷ R = **$0.037 central**
(band $0.033-$0.046 over R ∈ [5, 7]); a session tick ≈ **$0.0037** vs the weekly tick's
$0.0230. The clean 6.22 lands mid-band and matches the T2 bracket (5.0), the probe (6.0) and
the combined endpoints (7.0). Four independent points now, all inside 5-7. The structural
33.6× stays rejected; the inverted bracket (§5) remains the lone outlier.

Also settled here: the owner shortened the bracket's waits mid-run (quiet 90→5 s, settle
45→15 s) and the confirm read still found both windows settled, direct live support for the
registration-anchored settle design (15 s + one confirm read suffices; the settle-design
ticket holds the decision).
