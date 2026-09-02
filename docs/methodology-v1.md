# Methodology v1: Ollama Cloud effective cost, legacy GPU-time vs new token-based

**Version 1.3 · 2026-09-02 · status: specified, ready for execution** (a phase after this
wayfinder map). This document integrates the decisions of every closed ticket of the
[map](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/1), of the Harness
v1.1 map ([#27](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/27): medibilidad,
latencia, precisión y lectura) and of the Methodology v1.2 map
([#44](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/44): carriles sin cache
y S(x) custom). A future session must be able to run the benchmarks by reading
this without making any design decision. Glossary: [`CONTEXT.md`](../CONTEXT.md).

**v1.1 changelog** (map #27: [composición](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/30) ·
[settle](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/31) ·
[latencia](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/29) ·
[sesión-USD](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/36)):
composición híbrida de brackets (T1 calibrador, T2 híbrido real) · settle por registro
(el fijo de 90 s muere) · precisión exacta (cero redondeos en lo persistido) · margen del
veredicto {winner, margin_pct} · sesión como señal secundaria con $/pp derivado ·
predictibilidad re-alcanzada sobre el conjunto medible.

**v1.2 changelog** (map #44: [carril sin cache](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/45) ·
[S(x) custom](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/46) ·
[cierre](https://github.com/srnoob2570/ollama-cloud-pricing-bench/issues/47)):
**carril sin cache**: todo request medido lleva un nonce seeded por run (~1.5 % de los
tokens de entrada esperados, clamp [4, 400] palabras) que fuerza cache-miss: el pp medido es
trabajo raw (el subconteo de cache del dataset v2 es el defecto documentado detrás de esto) ·
**canario de facturación**: 5 salted + 5 replays por run; alarma ratio > 0.5 aborta en
compuerta; más el detector pasivo Δpp-vs-presupuesto · **S(x)**: S1 pasa a ser el default
versionado (50 %) declarado aquí; el par S0/S1 sigue siendo la referencia persistida (MAPE
anclado al par); S ≠ default se congela solo como re-runs estampados · el dashboard muestra
el S efectivo por modelo · el re-run T1/T2 bajo v3 con carriles sin cache es requisito
de v1.2 pendiente de la compuerta del owner (§11).

**v1.3 changelog** (credit ratio, post-hoc): veredicto, margen y umbral pp/1M comparan en
**dólares pagados**: el lado nuevo vende créditos con multiplicador por tier (Pro ×3,
Max ×3, Team ×2; el ancla es el tier Max ⇒ `--credit-ratio` default 3), su coste nominal
en créditos se divide por el ratio antes de comparar; las cifras de coste por tarea se
quedan a valor nominal; ratio 1 reproduce la comparación 1:1 de v1.2. Corrección puramente
post-hoc: no re-mide nada, pero mueve veredictos y márgenes (el re-run v3 pendiente de
compuerta la incorpora).

**Hard guardrails**: 🚫 do not migrate the legacy Max account (the only live GPU-time key;
migration is voluntary and irreversible) · 💰 pre-approved spend covers only the meter
verification (already done); every real run goes through the gate (§11).

---

## 1. Brief questions → where they are answered

| # | Original brief question | Status | Resolution |
|---|---|---|---|
| 1 | Baseline of both systems | ✅ closed | "Pricing baseline" + "Live meter verification" (docs/research/) |
| 2 | Representative benchmarks | ✅ closed | "Workloads and checkers (T1/T2/T3)": 13 workloads, 3 levels |
| 3 | Measured variables | ✅ closed | "Measurement protocol and dataset schema" (per-request/batch schema) |
| 4 | Normalization | ✅ closed | "Cost model and unit of account" (cost/task, $/1M, pp/1M, anchor) |
| 5 | Who wins/loses | ✅ designed | Break-even (user profiles), executed with data |
| 6 | Ollama's claim | ✅ designed | "Predictability experiment" (comparative MAPE) + literal quote in baseline |
| 7 | Economic incentives | ✅ matrix | "Incentives checklist" + "External price comparables" |
| 8 | Reproducible tests | ✅ closed | "Harness specification" + re-run without re-measuring |
| 9 | Break-even points | ✅ closed | Critical pp/1M threshold + 4 sensitivity sweeps |
| 10 | Data-backed conclusions | 🔜 later phase | final-report structure defined in §12 |

## 2. The two billing systems (baseline 2026-08-31)

- **Legacy (GPU-time)**: quota as a % per window: session 5 h and weekly 7 days
  (`limits.*.usage` from `GET /api/usage` with the Bearer API key), a fraction with a 0.001
  tick (0.1 pp) and an instant, exact per-model `request_count` breakdown. No public rate and
  no mapping to GPU-seconds.
- **New**: per-plan dollar credits (Free starter · Pro $20→$60/mo · Max $100→$300/mo ·
  Team $500→$1000/mo) consumed by tokens at the official input/cached/output table × 19
  models. Migration is voluntary and irreversible; new signups already enter the new system.
  The credits sell at a **per-tier multiplier over the money paid** (Pro ×3, Max ×3,
  Team ×2): one credit dollar is not one paid dollar, and every cross-system comparison
  re-denominates by it (see the verdict margin row in §3).
- Market comparables: full table in `docs/research/comparables-open-weights.md` (near-literal
  passthrough of upstream rates; the margin lives in the credit ratio; GPU-time no longer
  exists in any shared API in the market).

## 3. Units and cost model

| Concept | Definition |
|---|---|
| **Legacy unit** | pp (quota point) of the **weekly** window (7 days), `limits.weekly.usage`, 0.001 tick |
| **New unit** | $ per token (input/cached/output × versioned per-model table) |
| **Anchor** | `P_LEGADO=$100/mo` → **$0.2302 per weekly pp** (÷4.345 ÷100); tick ≈ $0.0230 |
| **Credit ratio** | the new plan's credits per paid dollar, per tier: **×3 at the study's anchor tier (Max: $100→$300 credits)**, Pro ×3, Team ×2. Every comparison point (verdict, margin, pp/1M threshold) is stated in **paid dollars**: the new side's nominal credit cost divides by the ratio (`--credit-ratio`, default 3; 1 reproduces the v1.2 1:1 credit comparison). The per-task cost figures stay at credit face value |
| **Session anchor (derived)** | session $/pp = weekly $/pp ÷ R, R = session:weekly ticks ≈ 5–7 (live verification: 6.22) → **≈ $0.037 per session pp** (session tick ≈ $0.0037). A **derived secondary value**, never an independent anchor |
| **Separate constraints** | 5 h session and the rolling 4-week `activity` window = saturation, not the anchor |
| **Extrapolation** | `t_in×r_in + t_cached×r_cached + t_out×r_out` under **S0 = 0 %** (floor) and **S1 = the versioned default (50 %)**; any other **S(x)** enters only as a stamped re-run; S(x)≡S0 for the 5 models with cached=input |
| **Useful unit** | primary = cost per **completed task** (checker passes); attempted and $/1M as secondary |
| **Verdict margin** | (loser − winner) ÷ winner, as a percentage of the winner's cost (can exceed 100 % when the loser costs more than twice the winner), **in paid dollars**: the new side's credits divide by the credit ratio before the comparison (v1.3), so the tie band applies to the re-denominated gap and the pp/1M threshold **falls** by the same factor (the new plan is 3× cheaper in paid dollars, so the legacy quota tolerates credit_ratio times fewer pp/1M before the new plan undercuts it); a **sub-tick winner** (weekly reads 0.0) prices with its session-derived weekly-equivalent — under the R mapping the session dollar figure IS that estimate — falling back to the loser's cost (exactly 100 %) with no session reading; the verdict is `{winner, margin_pct}`; a tie inside the tie band (>2 ticks or >5 % of the cheaper cost); an **allocated reading is never verdicted** |
| **Uncertainty** | new side: n=5, median, p25–p75+p95 over per-request tokens; legacy side: the meter's tick (±1 tick of phase error per reading) at the level the legacy was measured (direct bracket or pooled bracket); a real difference = clears the tie band at that level; brackets are planned with expected Δpp ≥ 3.5 ticks |

Cache: the legacy side measures **cache-free work** (the cache-free lane forces misses, so
the measured Δpp is raw work. The v2 dataset's baked-in caching was the documented defect);
the scenarios apply only to the new-plan side; calibration replaces S1 with the measured hit
rate when conclusive.

## 4. Measurement (confirmed protocol, v1.1)

- **Bracketed batch** (never per-request): `/api/usage` read (Bearer) → N requests →
  **registration settle**: per-model `request_count` check (~0.2 s) → poll every 5 s until two
  consecutive reads report **equal pp in both windows** → close; defensive cap 60 s
  (`settle_exit: "stable" | "capped"`). Registration in both windows' counts (~≤6 s measured)
  means both pp figures are already recalculated. A pp delta below the tick is legitimate
  (resolution, never lag). The fixed 90 s settle of v1.0 is dead.
- **Both windows read per bracket**: weekly = unit of account (the anchor); session = secondary
  resolution signal (5–7× more movement per token), unanchored. Its $/pp is the derived value
  of §3.
- **Precision policy (v1.1)**: zero rounding in anything persisted: raw, manifests,
  derivatives, analysis, predict, calibration. Meter deltas keep their exact float; tick
  semantics live only in comparison logic. Timestamps unrounded.
- **Cache-free lane (v1.2)**: every measured request carries a run-scoped, seeded **nonce as
  its very first tokens** (`nonce_seed = f(run_id)` in the manifest, `nonce_i = RNG(nonce_seed, i)`;
  size ≈ 1.5 % of the fixture's expected input tokens, clamped [4, 400] words), forcing a
  cache miss. The measured pp is the workload's raw work. Multi-turn salts every turn.
  Exempt: `calibrate-cache`/prefix replays, the billing canary, and the concurrency probe.
  Every request persists `prompt_sha256` + `nonce_sha256`; dry-run/predict include the nonce
  tokens.
- **Billing canary (v1.2)**: once per run, before the first bracket: 5 salted + 5
  identical-prefix replays (T2-size body), **billed on a fixed reference model (kimi-k3)** —
  never on the run's measured model, whose replay can fall below the meter's 0.001-tick
  resolution and read a false ratio of 0.0 (the 2026-09-02 T1 run on deepseek-v4-flash did
  exactly that); the replay must bill at ~11–14 % of the salted quota (measured 1/7 on
  kimi-k3, the only model the ratio reads as evidence); **alarm: ratio > 0.5 → abort at the
  gate**. Passive
  detector: every bracket's measured Δpp is cross-checked against the §5 token budget. A
  collapse below prediction is the signature of broken salting (threshold refined with v3
  data).
- **Immutable raw**: `runs/*.jsonl` per request (`k`, seed, tokens, streaming TTFT, verbatim
  done, checker) + `batches/*.jsonl` per batch (raw meter pre/post with every model's counts,
  Δpp per window, `pool` map for pooled brackets, settle fields).
- **Streaming-first** (the only mode with real cloud latency), round-robin across models, no
  warmup, no in-batch auto-retry, `table_version` on every line.
- **Dataset on GitHub releases**; derivatives and analysis regenerable from raw. The protocol
  v2 dataset (T1/T2 of 2026-09-01) is **frozen as the opacity case study** and never mixed with
  v3 data.

## 5. Workloads and bracket composition (v1.1)

**T1 × 19 models = calibrator, 1 rep** (~77.7K tokens): catalog-wide tokens↔pp curve, new-plan
calibration, TTFT/throughput/pass-rates. **No legacy claim**: T1's legacy side is declared an
**opacity finding at level level** (53/57 brackets read 0; clearing 3 weekly ticks needs ~15×
the quota, wall-bound by `nemotron-3-ultra`). The legacy-vs-new comparison lives on the
stratified slate.
**T2 × 6 = hybrid real** (~30 brackets, ~4.34M tokens at n=5): the **strong four** per-cell
(`long_context`, `long_generation`, `ratio_in`, `ratio_out`, expected 3.8–6.5 weekly ticks) with
legacy measured per (model, workload); the **weak trio** (`multi_turn`, `reasoning`,
`tool_calling`) pooled per model (one bracket ≈ 5 ticks) with legacy allocated by token
share. Marked allocated, never verdicted. Verdicts only where the legacy is measured.
**T3 × 3** (kimi-k2.7-code, glm-5.3-flash, deepseek-v4-pro): `multi_file`, `debugging`,
`refactoring`: own deterministic loop (read/write/patch/list/run_tests, max 12 steps),
subprocess+timeout sandbox without network, **pytest as checker**. Unchanged: a single T3
request is already worth ~1.7–9.7 weekly ticks.
Fixtures and checkers never change: composition changes the bracket, never the fixtures.
Gate input: T2 ≈ 11–24 % of a fresh weekly window + T1 ≈ 0.4 % + T3 on top.
Under the cache-free lane the composition's sizes stand: the nonce adds ~1.5 % of tokens (far
below the 3-tick margin) and salting protects the budget: pooled reps bill full price
instead of landing under it on the cache discount.

## 6. Concurrency (workstream)

Limit probe (k up to the real cut-off under legacy) + cells k∈{1,4,8} on the anchor with the
same total tokens per cell; verdict metric: **effective cost per task under k**. Coded
outcomes: invariant → *squeeze*; growing → overhead; serialized → k irrelevant. `k` field in
the schema; errors/429s recorded.

## 7. Cache calibration

Intra-batch replay (prefix ~20K × r=4) + between-batches (5/30/90 s), T2 slate, three signals
(tokens, Δpp, TTFT). **Measurement wins**: a conclusive hit rate replaces S1 per model
(versioned); S0 as the floor; paper discounts declared (the 5 cached=input models).

## 8. Predictability (Ollama's claim under test, re-scoped v1.1)

The blind cells redraw onto the **measurable-legacy set**: the strong four T2 workloads + T3
on the legacy side, the new-plan side on every cell (two phases, zero extra quota, native
units: weekly pp / $ credits). Comparative verdict (legacy vs new MAPE, bootstrap CI, no
absolute threshold); sub-tick cells stay excluded from the legacy side by the v1 rule and are
reported as opacity findings. Estimates and verdicts stay anchored to the **persisted S0/S1
pair**; custom S(x) enters only through stamped re-runs, never by re-anchoring the locked
estimates.

## 9. Incentives

Evidence matrix 9 hypotheses × 6 columns, pre-loaded with what is known; open-weights
comparables per family (dedicated doc); the owner's own data as consented evidence.
Data-vs-speculation threshold: only sources (URLs or study measurements) enter the matrix.

## 10. Break-even

`pp/1M* = (new-plan $/1M) ÷ anchor` per model/workload/scenario; automatic bundle (tables,
Δpp↔tokens curves, who-wins-by-profile) + **static HTML dashboard** (in-page theme-token
SVG charts, verdict-first with margins, presentation-layer cache slider); 4 sensitivity
sweeps (rates ±20 %, cache S∈{0,25,50,90} %, P_LEGADO ±30 %, k axis). Pure post-hoc analysis:
re-run without re-measuring on price changes.

## 11. Execution gate (when funds/quota exist)

1. `bench dry-run` before each level (estimated cost, no API).
2. Order: T1 (calibrates real pp/token) → cache calibration → concurrency → predictability
   (blind estimates BEFORE each cell) → T2 → T3.
3. Per-run caps agreed at the gate; if the weekly quota cannot cover n=5 in T3,
   fallback n→T3 first (documented); fixtures/checkers are never touched.
4. A new-plan key ($20 Pro) when funds exist → re-runs the measured branch under tokens.
5. **v1.2 requirement**: the study's conclusions need the T1/T2 re-run under protocol v3
   with cache-free lanes (the v2 dataset's legacy readings carry the cache discount). A
   gate decision: spend, never an automatic act.

## 12. Final report structure (later phase)

Answers with data: (1) is token-based more economical, and for which workloads? (2) who pays
more? (3) does the "GPU-time is hard to predict" claim hold (comparative MAPE)?
(4) what incentives does Ollama have according to the evidence matrix? (5) who benefits?
Each answer carries its uncertainty band and sensitivity to the 4 sweeps.

## Open questions declared (fog transferred to execution)

- What does the meter's `activity.cost` measure (per-request extra balance?) Only
  verifiable with funds.
- New-plan extra-balance policy (cap, auto-billing).
- Announced priority tiers / "fast mode": if they appear, re-run break-even.
- kimi-k2.7-code serverless prices at some providers: "n/p" in the comparables table.
