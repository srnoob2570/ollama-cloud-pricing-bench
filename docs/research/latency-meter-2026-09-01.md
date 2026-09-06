# Usage meter latency, 2026-09-01

Live measurement for the ticket "Re-measure meter latency: counts and pp in both windows"
(map "Harness v1.1: measurability, latency, precision, and reading"). Complements and corrects
the protocol of the ticket "Verify the usage meter live" (2026-08-31), which set "instant
count, pp with ~60–90 s lag" from a single verification.

## Protocol

Each trial: raw read of `/api/usage` → **1 trivial streaming request** (`kimi-k3`,
147 tokens in + 8 out, `num_predict=8`, fixed seed) → poll `/api/usage` every 5 s for
120 s, recording per poll: the model's `request_count` in the session window and in the
weekly one, each window's `usage` (pp), and the full model lists of both windows. 3 trials +
1 discarded (gemma4, a tag bug, see Lesson). Total spend: 4 trivial requests (≤155 tokens
each), ≪1 tick, under the approved cap of ≤10.

**Free natural experiment**: during the trials the account had concurrent traffic on
`glm-5.3-flash` (+21 requests in ~8 min, +5 ticks of session pp). Every jump in that count
correlated with the pp measures meter latency without spending our own requests.

## Results

| Signal | Observed latency | Evidence |
|---|---|---|
| `request_count`, session window | **≤6.2 s** (first poll, 3/3 trials) | 25→26, 26→27, 27→28 at t=6.1–6.2 s |
| `request_count`, weekly window | **≤6.2 s** (first poll, 3/3 trials) | 234→235→236→237 at t=6.1–6.2 s |
| **session** pp | **≤5 s** (co-movement with counts, same poll resolution) | 4 steps (0.079→0.083), each in the same poll as concurrent count jumps |
| **weekly** pp | **>8 min without moving** despite +21 requests and +5 session ticks | 0.386 → 0.386 for all 3 full trials |

The real count latency is bounded at (0, 6.2] s; the runner's count-check reads it ~0.17 s
after the burst, so the true value is probably ~1 s or less. The weekly pp should have
absorbed ≈1–2.6 ticks with the observed traffic (≈38.5K tokens of prefill per weekly tick)
and it did not move a tenth of a tick.

## Findings

1. **The proposed stop condition (request registered in the session count AND in the weekly
   one) is instant**: it holds at the first poll (~≤6 s). Both counts are the fast, exact
   signal. Confirms the protocol v1/v2 design.
2. **The two windows are not the same**: session pp lands in seconds (≤5 s of the counts);
   weekly pp did not land in 8 minutes. The only prior weekly-lag measurement (76–83 s,
   2026-08-31) and the T2 `long_context` brackets (weekly Δpp 0.1–0.5 inside 90 s settles,
   ~19:51Z the same day) show it eventually lands. But today, with ~1–2.6 weekly ticks
   accumulating live, it did not land in 8 min. Hypothesis: weekly pp quantizes/aggregates in
   larger quanta or on a longer cycle.
3. **The fixed 90 s settle probably clips the weekly window** (suspicion already noted by the
   "Measurability budget" doc): if weekly landing exceeds 90 s, the bracket's post-read closes
   before the study's unit of account reflects the spend.
4. **Under concurrent traffic, "stable pp" never happens**: session pp rose 5 ticks in
   8 min from traffic outside the batch. Loop termination cannot be just "two equal reads".
   It must anchor on the count (exact, instant) + pp steps per window + a cap per window.
5. **Tag lesson**: the names in the model list are the catalog's tagged ids
   (`gemma4:31b`, `deepseek-v4-flash:0731`...). Looking up an untagged slate id returns
   "model absent" even when the meter counts it. The runner already sends `modelo_api` with
   the tag; the bug was exclusive to this trial's script.

## Implication for the settle design (input to the "Adaptive settle" ticket)

- Primary stop: the model's count verified in both windows (≤2 polls, ~10 s).
- Per-window stop: close the session bracket with 2 stable reads (~10–15 s
  total); the weekly window needs its own long wait with a cap. The value is set by the
  probe of the open question below.
- The 90 s becomes a session cap, not a standard; the weekly cap is another number (≥2–3 min
  provisional until measured).

## Open question (graduates to a ticket)

**When does the weekly pp land?** Needs a super-tick probe: 1 request the size of the
`long_context` fixtures (~30–50K tokens ≈ 0.1–0.5 pp ≈ $0.023–0.115 of the anchor), polling
until the weekly step is seen. It exceeds the "≪1 tick" cap approved for this ticket → needs
explicit spend approval before running.

## Owner correction (2026-09-01, post-probe), the correct semantics

The reading "the weekly window is the slow one" was a category error: it confused the
**latency** of the weekly pp with the **absence of movement** of the weekly pp. The correct
semantics, fixed by the owner:

- **The settling signal is the REGISTRATION of the requests**, not pp movement.
- When requests register in both windows' `request_count`, the session and weekly usage
  have already been recalculated. There is no "slow" window to wait for.
- The pp (session or weekly) not moving **is fine**: the $100 legacy plan absorbs small
  usage without moving the visible percentage. A Δpp below the tick is resolution, not lag.

## Super-tick probe (kimi-k3, 38,293 tokens in, the T2 cell that measured Δpp 0.5/0.1)

| Event | t after the request | Observation |
|---|---|---|
| CHAT completed | 6.48 s | http 200, fixture-exact tok_in |
| COUNT session + COUNT weekly | 11.9 s (~5.4 s after the request) | 28→29 and 237→238 in the same poll |
| **PP session** | 11.9 s | 0.087→0.093 (+6 ticks ≈ T2's 0.5 pp) |
| **PP weekly** | 11.9 s | 0.387→0.388 (+1 tick = T2's 0.1 pp) |
| Extra session pp | 28.1 s | +1 tick, the owner's concurrent traffic, not the probe's |

**Corrected conclusion**: when Δpp is observable, it lands together with registration
(~5 s), in both windows. The "8 min without weekly movement" of the 155-token trials was not
lag: it was sub-resolution. Requests registered and the legitimately recalculated pp did not
move. There is no separate weekly cap to fix: the settle anchors on registration. (The
76–83 s evidence of 2026-08-31 stands as an unreproduced historical observation; the settle
ticket decides whether any defensive wait is kept.)

## Raw record

`/tmp/latency-results.jsonl` (3 lines, one per trial: events + full polls with the model
lists per window). Trial script: `/tmp/latency_trial.py` (outside the repo: not harness
code). Trial 1 discarded (gemma4): results not attributable due to the tag bug; its pp values
are confounded with concurrent traffic.
