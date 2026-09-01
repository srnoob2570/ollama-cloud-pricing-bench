# ollama-cloud-pricing-bench

Benchmark methodology to measure the **effective cost** of running LLM workloads on
[Ollama Cloud](https://ollama.com) during its transition from **legacy GPU-time** billing to
**token-based** billing (announced 2026-08-31 in
[`ollama.com/blog/transparent-pricing`](https://ollama.com/blog/transparent-pricing)).

The goal is not comparing nominal prices, but answering with data:

- Is token-based pricing more economical for the user? For which workloads, and for which is it more expensive?
- Does the "GPU-time is hard to predict" argument hold?
- What economic incentives does Ollama have for the change, and who does the change benefit?

> **Status: methodology v1 consolidated (2026-09-01)** - the wayfinder map closed with all 14
> decisions recorded; the benchmarks have not run yet (a later phase, spending gate in
> [`docs/methodology-v1.md`](./docs/methodology-v1.md) §11). The harness (tickets #17-26) is
> being implemented; Harness 01-08 are done (scaffold, T1 runner with bracketed
> batches, catalog preflight + real T1 checkers + `status`, the T2 structural suites
> with their real checkers, the T3 synthetic mini-repos with the deterministic
> agent loop and the sandboxed pytest checkers, the concurrency workstream:
> `bench probe-concurrency` sweeps k in short volleys to measure the real per-key
> cut-off, then runs the k∈{1,4,8} cells on the T1 anchor with the same total
> tokens per cell — a cell whose planned k exceeds the measured cut-off is
> re-anchored to it, documented in the dataset — the cache calibration:
> `bench calibrate-cache` replays one fixed ~20K prefix per T2-slate model
> (cold reference, r=4 intra-batch, spaced replays) and its measured hit rate
> replaces the S1 assumption when conclusive, with S0 as the floor — and
> `bench analyze`: the whole break-even bundle (derivatives with median/IQR/p95
> per model×workload, per-task S0/S1 costs with the anchor, critical-threshold
> bars pp/1M vs measured, dp-tokens curves, who-wins-by-profile, PNGs and a
> static self-contained HTML dashboard) recomputed from raw alone — a price
> change re-runs the verdict with zero quota spent, plus the 4 fixed sensitivity
> sweeps (rates ±20 %, cache {0,25,50,90} %, P_LEGADO ±30 %, the k axis)).
> [Leer en español →](./README.es.md)

> **Transparency:** this project is generated and maintained with AI assistance
> ([Claude Code](https://claude.com/claude-code)). Not affiliated with or endorsed by Ollama.
> Review the code and data before trusting it.

## Design decisions already fixed

- **Dual billing branch**: live measurement under the **legacy plan** (the only available
  account; frozen, not migrated) + **extrapolation** to the new plan with the official token
  table, until a new-plan key exists.
- **Unit of account**: legacy quota % + the dollar **anchor** (monthly price ÷ quota); token-$
  bridge for the same task, with and without standard cache.
- **Full catalog** (19 models in the official table) with staged density: T1 micro on all,
  T2 structural suites on ~6, T3 code agents on 2-3.
- **Quality**: verifiable binary success (tests/compilation/checkers). No LLM-judge in v1.
- **Concurrency** as a first-class workstream; own deterministic agentic harness.

## Repository

| Path | Content |
|---|---|
| [`docs/methodology-v1.md`](./docs/methodology-v1.md) | **The consolidated methodology v1** (the map's deliverable) |
| [`CONTEXT.md`](./CONTEXT.md) | Domain glossary (GPU-time, quota, anchor, cache scenario, critical threshold...) |
| `docs/research/base-pricing-2026-08-31.md` | Verifiable baseline of both billing systems |
| `docs/research/medidor-vivo-2026-08-31.md` | Live meter verification (API key, lag, quantization) |
| `docs/research/medidor-uso-ollama.md` | Documentary research on the usage meter |
| `docs/research/comparables-open-weights.md` | Per-family open-weights price comparables |
| `docs/research/logs/` | Raw logs of the meter verification |
| `pricing/2026-08-31.json` | Versioned official price table (the harness's input) |
| `src/ocharness/` + `tests/` | The `bench` CLI: spending gate, dry-run, fake ollama.com seam |

## Sibling repos in this workspace

- [`ollama-usage-breakdown`](https://github.com/srnoob2570/ollama-usage-breakdown) -
  userscript that reads the `ollama.com/settings` meters (candidate source of the quota delta).
- [`OMeter`](https://github.com/srnoob2570/OMeter) - TTFT/TPS benchmarks for Ollama endpoints
  (reused where applicable).
- [`opencode-ollama-cloud`](https://github.com/srnoob2570/opencode-ollama-cloud) - live catalog
  from `ollama.com/v1/models` (source of the model list).
