# Methodology v1 — Ollama Cloud effective cost: legacy GPU-time vs new token-based

**Version 1.0 · 2026-09-01 · status: specified, ready for execution** (a phase after this
wayfinder map). This document integrates the decisions of every closed ticket of the
[map](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/1). A future session must
be able to run the benchmarks by reading this without making any design decision. Glossary:
[`CONTEXT.md`](../CONTEXT.md). (Versión original en español: se tradujo íntegra; el mapa cierra
con esta metodología.)

**Hard guardrails**: 🚫 do not migrate the legacy Max account (the only live GPU-time key;
migration is voluntary and irreversible) · 💰 pre-approved spend covers only the meter
verification (already done); every real run goes through the gate (§11).

---

## 1. Brief questions → where they are answered

| # | Original brief question | Status | Resolution |
|---|---|---|---|
| 1 | Baseline of both systems | ✅ closed | "Pricing baseline" + "Live meter verification" (docs/research/) |
| 2 | Representative benchmarks | ✅ closed | "Workloads and checkers (T1/T2/T3)" — 13 workloads, 3 levels |
| 3 | Measured variables | ✅ closed | "Measurement protocol and dataset schema" (per-request/batch schema) |
| 4 | Normalization | ✅ closed | "Cost model and unit of account" (cost/task, $/1M, pp/1M, anchor) |
| 5 | Who wins/loses | ✅ designed | Break-even (user profiles) — executed with data |
| 6 | Ollama's claim | ✅ designed | "Predictability experiment" (comparative MAPE) + literal quote in baseline |
| 7 | Economic incentives | ✅ matrix | "Incentives checklist" + "External price comparables" |
| 8 | Reproducible tests | ✅ closed | "Harness specification" + re-run without re-measuring |
| 9 | Break-even points | ✅ closed | Critical pp/1M threshold + 4 sensitivity sweeps |
| 10 | Data-backed conclusions | 🔜 later phase | final-report structure defined in §12 |

## 2. The two billing systems (baseline 2026-08-31)

- **Legacy (GPU-time)**: quota as a % per window — session 5 h and weekly 7 days
  (`limits.*.usage` from `GET /api/usage` with the Bearer API key), a fraction with a 0.001
  tick (0.1 pp) and an instant, exact per-model `request_count` breakdown. No public rate and
  no mapping to GPU-seconds.
- **New**: per-plan dollar credits (Free starter · Pro $20→$60/mo · Max $100→$300/mo ·
  Team $500→$1000/mo) consumed **by tokens** at the official input/cached/output table × 19
  models. Migration is voluntary and irreversible; new signups already enter the new system.
- Market comparables: full table in `docs/research/comparables-open-weights.md` (near-literal
  passthrough of upstream rates; the margin lives in the credit ratio; GPU-time no longer
  exists in any shared API in the market).

## 3. Units and cost model

| Concept | Definition |
|---|---|
| **Legacy unit** | pp (quota point) of the **weekly** window (7 days), `limits.weekly.usage`, 0.001 tick |
| **New unit** | $ per token (input/cached/output × versioned per-model table) |
| **Anchor** | `P_LEGADO=$100/mo` → **$0.2302 per weekly pp** (÷4.345 ÷100); tick ≈ $0.0230 |
| **Separate constraints** | 5 h session and the rolling 4-week `activity` window = saturation, not the anchor |
| **Extrapolation** | `t_in×r_in + t_cached×r_cached + t_out×r_out` under **S0=0 % and S1=50 %** (versioned); S1≡S0 for the 5 models with cached=input |
| **Useful unit** | primary = cost per **completed task** (checker passes); attempted and $/1M as secondary |
| **Uncertainty** | n=5, median, p25–p75+p95; a "real" difference = non-overlapping IQR **and** >2 ticks (≈$0.046) or >5 % in $; batches ≥30× the quantum |

Cache: the legacy side **measures** real caching (baked into the measured Δpp); the S0/S1
scenarios apply only to the new-plan side; calibration replaces S1 with the measured hit rate
when conclusive.

## 4. Measurement (confirmed protocol)

- **Bracketed batch** (never per-request): `/api/usage` read (Bearer) → N requests → settle
  ≥90 s → read; Δpp with ±0.001 error; per-model `request_count` verified ≤2 s (the counter is
  instant and exact; the usage % lags ~60–90 s).
- **Immutable raw**: `runs/*.jsonl` per request (`k`, seed, tokens, streaming TTFT, verbatim
  done, checker) + `batches/*.jsonl` per batch (raw meter pre/post, Δpp per window).
- **Streaming-first** (the only mode with real cloud latency), round-robin across models, no
  warmup, no in-batch auto-retry, `table_version` on every line.
- **Dataset on GitHub releases**; derivatives and analysis regenerable from raw.

## 5. Workloads (synthetic fixtures, English, binary checkers)

**T1 × 19 models**: `qa_short` · `calibration` · `throughput`
**T2 × 6** (glm-5.3-flash, gpt-oss:20b, deepseek-v4-flash, minimax-m3, glm-5.3, kimi-k3):
`long_context` (~30K), `long_generation` (4–6K out), `multi_turn` (8 turns), `tool_calling`
(3 tools), `reasoning`, `ratio_in` (~50K in/≤120 out), `ratio_out` (20 gens × ~500 out).
**T3 × 3** (kimi-k2.7-code, glm-5.3-flash, deepseek-v4-pro): `multi_file`, `debugging`,
`refactoring` — own deterministic loop (read/write/patch/list/run_tests, max 12 steps),
subprocess+timeout sandbox without network, **pytest as checker**.
Repetitions: **n=5** (median, IQR+p95; a "real" difference = non-overlap + >2 ticks or >5 %).
Fallback if quota falls short: reduce T3's n first, documented; fixtures/checkers never change.

## 6. Concurrency (workstream)

Limit probe (k up to the real cut-off under legacy) + cells k∈{1,4,8} on the anchor with the
same total tokens per cell; verdict metric: **effective cost per task under k**. Coded
outcomes: invariant → *squeeze*; growing → overhead; serialized → k irrelevant. `k` field in
the schema; errors/429s recorded.

## 7. Cache calibration

Intra-batch replay (prefix ~20K × r=4) + between-batches (5/30/90 s), T2 slate, three signals
(tokens, Δpp, TTFT). **Measurement wins**: a conclusive hit rate replaces S1 per model
(versioned); S0 as the floor; paper discounts declared (the 5 cached=input models).

## 8. Predictability (Ollama's claim under test)

12 cells estimated blind + informed re-estimation (two phases, zero extra quota), in **native
units** (weekly pp / $ credits); **comparative** verdict (legacy vs new MAPE, bootstrap CI,
no absolute threshold); sub-resolution cells (Δpp < tick) excluded from the legacy side and
reported as an opacity finding.

## 9. Incentives

Evidence matrix 9 hypotheses × 6 columns, pre-loaded with what is known; open-weights
comparables per family (dedicated doc); the owner's own data as consented evidence.
Data-vs-speculation threshold: only sources (URLs or study measurements) enter the matrix.

## 10. Break-even

`pp/1M* = (new-plan $/1M) ÷ anchor` per model/workload/scenario; automatic bundle (tables,
Δpp↔tokens curves, who-wins-by-profile, PNGs) + **static HTML dashboard**; 4 sensitivity
sweeps (rates ±20 %, cache S∈{0,25,50,90} %, P_LEGADO ±30 %, k axis). Pure post-hoc analysis:
**re-run without re-measuring** on price changes.

## 11. Execution gate (when funds/quota exist)

1. `bench dry-run` before each level (estimated cost, no API).
2. Order: T1 (calibrates real pp/token) → cache calibration → concurrency → predictability
   (blind estimates BEFORE each cell) → T2 → T3.
3. Per-run caps agreed at the gate; if the weekly quota cannot cover n=5 in T3,
   **fallback n→T3 first** (documented); fixtures/checkers are never touched.
4. A new-plan key ($20 Pro) when funds exist → re-runs the measured branch under tokens.

## 12. Final report structure (later phase)

Answers with data: (1) is token-based more economical, and for which workloads? (2) who pays
more? (3) does the "GPU-time is hard to predict" claim hold (comparative MAPE)?
(4) what incentives does Ollama have according to the evidence matrix? (5) who benefits?
— each answer with its uncertainty band and sensitivity to the 4 sweeps.

## Open questions declared (fog transferred to execution)

- What does the meter's `activity.cost` measure (per-request extra balance?) — only
  verifiable with funds.
- New-plan extra-balance policy (cap, auto-billing).
- Announced priority tiers / "fast mode": if they appear, re-run break-even.
- kimi-k2.7-code serverless prices at some providers: "n/p" in the comparables table.
