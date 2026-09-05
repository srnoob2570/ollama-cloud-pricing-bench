# Repository Atlas: ollama-cloud-pricing-bench

## Project Responsibility

`obench` — an effective-cost benchmark harness for Ollama Cloud (methodology v1, `METHODOLOGY_VERSION = "v1.3"`). It measures the effective cost and latency signals (TTFT, total time, token counts, wall-clock) of LLM workloads under Ollama Cloud's GPU-time quota billing vs the token-based billing, and derives verdicts, pp/1M thresholds, sensitivity sweeps, human-readable dashboards and published datasets that compare both systems.

## System Entry Points

- `pyproject.toml` — package manifest (`obench` 0.1.0, hatchling, deps `httpx` + `xlsxwriter`); defines the sole console script: `bench = "obench.cli:main"`.
- `src/obench/cli.py` — CLI dispatch of the ten `bench` subcommands (`dry-run`, `run`, `probe-concurrency`, `calibrate-cache`, `predict`, `analyze`, `status`, `resume`, `release`, `dataset`). The CLI is the system's only external seam.
- `.github/workflows/pages.yml` — CI: on release publish, re-derives the dashboard/calculator from the release itself (`bench analyze --release <tag>`, zero quota) and pushes them to the `gh-pages` branch.
- `CONTEXT.md` — glossary of record (pp, anchor, bracketed batch, slate, checker, S0/S1, prefix replay, measured hit-rate); `docs/methodology-v1.md` is the behavioral source of truth.

## Directory Map (Aggregated)

| Directory | Responsibility Summary | Detailed Map |
|-----------|------------------------|--------------|
| `src/` | src-layout root; packages the `obench` wheel via hatchling. | [View Map](src/codemap.md) |
| `src/obench/` | The harness: CLI-as-seam dispatch, schema-validated immutable raw JSONL, bracketed-batch runner, fake-injectable client, deterministic checkers + spending gate, offline analysis/calibration, versioned pricing, releases. | [View Map](src/obench/codemap.md) |
| `src/obench/web/` | Static HTML report templates (`dashboard.html`, `calculator.html`) filled by `str.replace()` placeholder substitution; deployed to GitHub Pages. | [View Map](src/obench/web/codemap.md) |
| `src/obench/testing/` | `FakeOllama`: fake ollama.com transport (`/api/chat`, `/api/usage`, `/v1/models`) with scriptable meter/cache behavior; the test seam behind `tests/conftest.py`. | [View Map](src/obench/testing/codemap.md) |

## Root Assets & Data Locations (unmapped by design)

| Path | Role |
|------|------|
| `pricing/2026-08-31.json` | The versioned price-table snapshot; tables resolve by `--table-version` (latest by default). |
| `tests/` | CLI-seam end-to-end tests against the fake transport (+ fake `gh` for releases); targeted unit tests only for pure analysis functions. |
| `runs/`, `batches/`, `releases/`, `analysis/` | Run artifacts: immutable raw JSONL evidence (requests/batches/probes/canary), release packages, and derived analysis bundles. |
| `live-probes/` | One-off manual probe scripts and console logs (paired cache probe, weekly session test). |
| `docs/` | Methodology (Spanish, source of truth), runbook, research notes. |
| `README.md` / `README.es.md` | Bilingual README pair; `LOCAL.md` — local dev notes. |

## Data & Control Flow (top level)

`bench run` → gate (dry-run mark require/consume) → preflight (catalog coverage) → runner (bracketed batches over `client.OllamaCloud`) → immutable raw JSONL in `runs/`/`batches/` → `bench calibrate-cache` → `bench analyze` (pure offline derivation) → bundle in `analysis/` → `bench release` (sha256-mapped package via `gh`) → Pages workflow re-derives the public dashboard from the release.

## Integration & Invariants

- External services: only `ollama.com` (chat/usage/models) and the `gh` CLI; everything else is offline.
- API key (`OLLAMA_API_KEY`) lives only in the environment; releases scrub credentials.
- Raw JSONL is immutable; derivatives regenerate; every line carries `table_version` + `protocol_version`.
- No real run without its dry-run mark; one mark enables exactly one run.
