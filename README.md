# ollama-cloud-pricing-bench

Benchmark methodology to measure the **effective cost** of running LLM workloads on
[Ollama Cloud](https://ollama.com) during its transition from **legacy GPU-time** billing to
**token-based** billing (announced 2026-08-31 in
[`ollama.com/blog/transparent-pricing`](https://ollama.com/blog/transparent-pricing)).

The goal is not comparing nominal prices, but answering with data:

- Is token-based pricing more economical for the user? For which workloads, and for which is it more expensive?
- Does the "GPU-time is hard to predict" argument hold?
- What economic incentives does Ollama have for the change, and who does the change benefit?

> **Status: methodology v1 consolidated (2026-09-01); harness complete (tickets #17-26)** - the
> wayfinder map closed with all 14 decisions recorded, and the harness that executes it is
> built: the `bench` CLI with its spending gate (dry-run before any real run, one mark per
> run), the T1/T2/T3 runners with bracketed batches, catalog preflight and real checkers, the
> concurrency workstream (`bench probe-concurrency` measures the real per-key cut-off, the
> k∈{1,4,8} cells re-anchor to it), the cache calibration (`bench calibrate-cache` replaces
> the S1=50 % assumption with the measured hit rate when conclusive), the predictability
> flow (`bench predict`: 12 cells estimated blind and hash-locked before they run, with the
> comparative MAPE report), `bench analyze` (the whole break-even bundle recomputed from raw
> alone - a price change re-runs the verdict with zero quota spent), and dataset sync to
> GitHub releases (`bench release --run <id>` pairs raw ↔ code ↔ table; `bench analyze
> --release <tag>` consumes one). The benchmarks themselves have not run yet: that is the
> execution phase, governed by the gate (methodology §11).
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

## Usage: the `bench` CLI

The harness is one binary (`src/ocharness/`, Python >= 3.12, httpx + matplotlib; minimal
deps, no TUI, no database):

```bash
uv sync          # or: pip install -e .
bench --help     # every subcommand takes --base DIR (default .) and --json
```

Everything operates on a working directory (`--base`, default `.`) that holds `pricing/`
(the versioned price tables), `runs/` + `batches/` (the immutable raw dataset), and — as
work happens — run manifests, `analysis/` (derivatives, dashboard, PNGs), `predictability/`
(locked estimates) and `releases/`.

### The spending gate (enforced by the tool, not by memory)

1. `bench dry-run --level T1` estimates the level's cost (tokens `$` under S0/S1 and the
   expected pp) **without touching the API**, and writes a gate mark bound to the
   `--table-version` and `--reps` it approved.
2. `bench run --level T1` refuses to start unless the mark exists and matches the live
   table and this run's density; it then verifies the live catalog (`/v1/models`) against
   the slate and **consumes the mark**: one dry-run enables exactly one run.
3. A crash mid-run never corrupts or duplicates billing: `batch_id` is deterministic from
   (run, level, workload, model, rep, k), and aborted / in-flight batches are skipped
   loudly on resume, never silently retried.

### The subcommands

| Subcommand | What it does |
|---|---|
| `dry-run --level T1\|T2\|T3 [--reps N] [--s S]` | The free estimate that opens the gate (S0/S1 token cost + expected pp). Zero requests. |
| `run --level T1\|T2\|T3 [--model X] [--rep N] [--reps N] [--k K]` | Executes the level with **bracketed batches**: meter read → burst (no warmup, no auto-retry) → per-model `request_count` check ≤ 2 s → settle ≥ 90 s → post read; every request lands as one immutable JSONL line. |
| `resume --level ...` | `run`'s resumable twin: continues an interrupted run from its manifest without duplicating any batch. It passes the gate like `run` — a fresh (free) dry-run approving the same density — and on a level with no manifest it is simply a fresh run. |
| `probe-concurrency --model X [--k-max K]` | Fires short volleys at increasing k to measure the real per-key cut-off (429/queueing), then runs the k∈{1,4,8} cells **re-anchored to it** (a planned k above the cut-off runs at the cut-off, documented in the dataset). |
| `calibrate-cache [--model X] [--spaced-gaps 5 30 90]` | Replays one fixed ~20K prefix per T2-slate model (cold reference, r=4 intra-batch, spaced replays); when conclusive, the **measured hit-rate replaces the S1 assumption** per model, with S0 as the floor. |
| `predict [--phase blind\|informed ...] [--report]` | The predictability flow (§8): 12 cells estimated **blind** (only the fixture's public description + rates), locked with timestamp and hash before the cell runs; informed re-estimation after; `--report` emits the comparative MAPE (legacy pp vs new $, bootstrap CI), excluding sub-resolution cells and flagging them. Zero quota. |
| `analyze [--ancla P] [--s S] [--table-version V] [--level L] [--model M]` | **The re-run without re-measuring**: every derivative (medians with p25–p75/p95 per model×workload, per-task S0/S1 costs, critical-threshold pp/1M, who-wins-by-profile, the dp-tokens curve, the 4 sensitivity sweeps), the PNGs and the static dashboard — regenerated from the raw data alone, offline. |
| `analyze --release <tag> [--repo owner/name]` | The same analysis over a fetched dataset release, verified against its metadata sha256 map and priced by **the release's own table**. |
| `status [--level L]` | Pending/done/aborted/in-flight batches and the quota consumed per level, read from the manifests alone. |
| `release --run <run_id> [--repo owner/name]` | Packages one run's dataset — requests + batches + the binding manifest + the price-table snapshot + a `metadata.json` (code commit, table version + sha256, protocol version, a sha256 map over every file) — and publishes it as a GitHub release. **One release per run, never rewritten**; the live API key (and any bearer-token-shaped string) must not appear in any packaged byte or the release refuses. |

### Re-running when prices change (zero quota)

1. Save the new official table as `pricing/<new-version>.json` (input / cached input /
   output per 1M tokens, per model).
2. `bench analyze --table-version <new-version> --ancla 100 --s 0.5` re-derives the whole
   bundle from the immutable raw data — nothing is re-measured, no request is sent.
3. Only `dry-run` → `run` spends; analysis never does.

Datasets are synced to GitHub releases, one per run: `bench release --run <run_id>` pairs
raw ↔ code (the producing git commit) ↔ table (snapshot + sha256), and
`bench analyze --release <tag>` consumes a release with no other input.

### Guardrails

- The API key is read only from the environment (`OLLAMA_API_KEY`) and is never written to
  any dataset or release.
- No real run without its dry-run mark; raw JSONL is immutable; derivatives regenerate.
- The legacy account is frozen: never migrate it.

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
| `runs/`, `batches/`, `analysis/`, `releases/` | The harness's working data: raw JSONL, derivatives, fetched dataset releases |

## Sibling repos in this workspace

- [`ollama-usage-breakdown`](https://github.com/srnoob2570/ollama-usage-breakdown) -
  userscript that reads the `ollama.com/settings` meters (candidate source of the quota delta).
- [`OMeter`](https://github.com/srnoob2570/OMeter) - TTFT/TPS benchmarks for Ollama endpoints
  (reused where applicable).
- [`opencode-ollama-cloud`](https://github.com/srnoob2570/opencode-ollama-cloud) - live catalog
  from `ollama.com/v1/models` (source of the model list).
