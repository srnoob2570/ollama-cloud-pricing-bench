# src/obench/

## Responsibility

`obench` is the benchmark harness of the Ollama Cloud cost study (methodology v1, `METHODOLOGY_VERSION = "v1.3"` in `analyze.py`): it measures the effective cost — and the latency signals (TTFT, total time, chunk counts, wall-clock) — of LLM workloads under the legacy GPU-time quota billing and the new token-based billing, then derives the verdicts, margins, critical thresholds (pp/1M) and datasets that compare the two systems. The package has no library API: a single console command, `bench`, is the system's external seam, dispatching ten subcommands (`dry-run`, `run`, `probe-concurrency`, `calibrate-cache`, `predict`, `analyze`, `status`, `resume`, `release`, `dataset`). Every write is confined to the `--base` working directory (`runs/`, `batches/`, `releases/`, `analysis/`, `predictability/`, `sandbox/`); nothing is written outside it.

## Design

Key patterns:

- **CLI-as-seam** (`cli.py`): all behavior is reachable through `bench` subcommands over a dispatch table (`DESPACHO`); assertions come from produced artifacts and the requests the fake transport observed. Targeted unit tests are accepted only for pure analysis functions (checkers, fixtures, the calibration analyzer).
- **Fake transport for testing** (`client.py`): `OllamaCloud` takes an injectable `httpx.BaseTransport`; `default_transport()` returns `None` (real network) and tests inject the fake from `src/obench/testing/fake.py`. `releases.py` follows the same one-seam philosophy: `gh` is called as a subprocess, so tests put a fake `gh` executable on PATH.
- **Immutable raw JSONL, regenerable derivatives**: `runs/requests-*.jsonl` and `batches/batches-*.jsonl` (plus `probe-*`, `canary-*`) are append-only raw evidence, schema-validated on write and never edited. `analysis.json`, `dashboard.html`, `calculator.html`, the MAPE report and the readable dataset are derivatives recomputed from raw with versioned parameters (table version, anchor, S(x), credit ratio); any price change is a re-run, never an edit.
- **Dry-run mark as spending gate** (`gate.py`): a level (or workstream consuming T1/T2 marks) runs only after a live dry-run mark — which prices the canary and lane overhead under protocol v3 — is validated (level, integrity, `table_version`, protocol, `--reps` density) and then consumed: one mark enables exactly one run. The require→consume pair is two adjacent filesystem ops, so concurrent runs can never double approved spend.
- **Manifest run state** (`runner.Manifest`): `batch_id` is deterministic from `(run, level, workload, model, rep, k)`; resume reuses done batches and loudly skips `aborted`/`in_flight` (never silently retried). Drift guards refuse table, protocol, fixture-version, composition, k and T2 `--reps` drift inside one `run_id`; the lane spec, gap plan and cell plan are pinned per manifest and refused on change.
- **Pure analysis functions**: `analyze.build`, `verdict_of`, `calibration.resolve_s`, `predict.build_report`, `checkers.judge` and the pricing formulas never touch the API; every refusal mode has a named exception (`RunnerError`, `GateClosed`, `TableError`, `SchemaError`, `CheckersError`, `AnalyzeError`, `PredictError`, `ReleaseError`, `PreflightError`, `ExportError`) surfaced as clean exit codes — no tracebacks, no silent placeholder verdicts (an unknown workload raises `CheckersError`).
- **Precision policy**: persisted floats are never rounded; the meter's tick (0.1 pp) enters only comparison logic through the `TICK_BAND` residue band (tie band, conclusive rule, sub-resolution exclusion, k-sweep).

Architectural layers (dependency direction roughly top-down):

1. **Schema/validation** (`schema.py`): on-write validation of request/batch/probe/canary/estimate lines — exact field sets (undeclared fields fail loudly), typed with explicit null-tolerance.
2. **Fixtures/datasets** (`fixtures.py`, `fixtures_t2.py`, `fixtures_t3.py`, `workloads.py`): deterministic, seeded, hash-stamped request specs behind one `build()` seam; the workload table (requests/tokens per level) and the model slates (T1 = all table models; 6 stratified T2; 3 T3); the `STRONG_T2`/`WEAK_T2` hybrid split.
3. **Runner** (`runner.py`, `lane.py`, `agent.py`): the bracketed-batch protocol (meter pre-read → k-concurrent burst → per-model count check → registration settle → Δpp per window), the cache-free lane's nonce salting, the billing canary, the T3 agent loop, and the workstream manifests (concurrency, calibration).
4. **Client** (`client.py`): streaming chat, `/api/usage` meter, `/v1/models` catalog; Bearer auth from `OLLAMA_API_KEY` (environment only, never written to any dataset); failed requests are recorded as data, never raised mid-batch; `PROTOCOL_VERSION = "3"`.
5. **Checkers/gate** (`checkers.py`, `sandbox.py`, `sandbox_runner.py`, `gate.py`): deterministic binary pass/fail per workload (no LLM-judge); T3 verdicts come from a sandboxed pytest subprocess (network guard, allowlist environment, hard timeout, fixture-owned test files); the spending gate.
6. **Analysis/calibration** (`analyze.py`, `calibration.py`, `predict.py`): offline derivatives — cells, verdicts `{winner, margin_pct}`, thresholds, pooled token-share allocation (marked allocated, never verdicted), sensitivity sweeps, the S0/S1 pair with the measured hit-rate winning where conclusive, and the comparative MAPE with paired bootstrap CI.
7. **Pricing/cost** (`pricing.py`, `cost.py`): the versioned price table (input / cached input / output per `per` tokens, `TableError` on any malformation) and the single pricing formula `new_task_cost` shared by the gate's budget and analyze's extrapolation.
8. **Releases/export** (`releases.py`, `dataset_export.py`): one release per run pairing raw↔code↔table with a sha256 map and credential scrub; fetch verifies integrity in both directions; the readable JSON/CSV/XLSX dataset is derived work stamped into the map.
9. **CLI dispatch** (`cli.py`): argparse subcommands with per-command knob discipline (spend-tuning flags absent where they would be silent no-ops), human and `--json` output; progress goes to stderr so stdout stays parseable.

## Flow

A real run (`bench run --level L`, `resume`, `probe-concurrency`, `calibrate-cache`):

1. **CLI entry** (`cli.py`): load `PriceTable` from `--pricing-dir`/`--table-version`; validate arguments; `gate.require_dry_run` checks the level's dry-run mark, then `gate.consume` deletes it — no real run without its dry-run mark, and one mark enables exactly one invocation. `OLLAMA_API_KEY` must be in the environment.
2. **Preflight** (`preflight.verify`): `/v1/models` must still carry every slate id (tag-aware matching); drift aborts before any billed request, loudly; the slate→catalog mapping and the catalog snapshot ride the manifest.
3. **Runner/client**: `runner.run_level` opens or reuses the per-level manifest, records the cache-free lane spec (run-scoped nonce seed), runs the **billing canary** once (5 salted + 5 identical-prefix replays on the pinned `kimi-k3`; ratio > 0.5 aborts at the gate), then executes each bracketed batch: meter pre-read → burst (seeded, k-concurrent, salted so measured pp is raw work) → per-model count check (a dropped request aborts) → registration settle (poll `/api/usage` until two consecutive equal reads in both windows, capped) → bracket post read. T3 cells dispatch to `agent.run_tasks`; the concurrency workstream probes the k cut-off then re-anchors k∈{1,4,8} cells; the calibration workstream fires cold / intra (r=4) / spaced prefix replays (exempt from salting — the only legitimate cached traffic).
4. **Raw JSONL artifacts**: one request line per billed request (timings, tokens, checker verdict, `prompt_sha256`/`nonce_sha256`, verbatim done-object, per-step records for T3) and one batch line per bracket (raw meter payloads, settle record, Δpp per window, canary line separately) — schema-validated on write, append-only, **immutable**. Checkers run at batch time and their pass/fail/null verdicts land on the lines.
5. **Calibration → analysis**: `bench calibrate-cache` writes `runs/calibration-<run_id>.json`; `analyze.load_calibrations` merges it and `resolve_s` gives each model its effective hit rate — the measured hit-rate replaces the S1 assumption when conclusive (>2 ticks and non-overlapping IQR), a conclusive absence sits at the S0 floor, inconclusive keeps S1 marked as assumed.
6. **Analyze** (`analyze.build` → `write_bundle`): pure offline derivation from raw + versioned table + parameters (`--ancla` anchor, `--s`, `--credit-ratio`); protocol-vintage filtering keeps one vintage per bundle; the bundle lands in `analysis/` (the persisted S0/S1 reference, never shrunk in place) or `analysis-s<x>/` for a stamped re-run at a custom S(x).
7. **Releases/export**: `bench release --run` packages the run's raw evidence + its own table snapshot + the readable dataset into a GitHub release (credential scrub, sha256 map, refuses over an existing tag); `bench analyze --release` / `bench dataset --release` fetch, verify both directions, and analyze or flatten offline — zero quota, the published release never rewritten.

Invariants threaded through the flow: the API key exists only in the environment (client raises without it; releases scrub packaged bytes); every dataset line carries `table_version` and `protocol_version` so vintages never mix; raw JSONL is immutable while derivatives regenerate; aborted brackets close and attribute their real spend, and are never silently retried.

## Integration

- **Console script**: `bench = "obench.cli:main"` ([project.scripts] in `pyproject.toml`); hatchling builds from `src/obench`; runtime deps `httpx`, `xlsxwriter`; dev dep `pytest` (required inside the sandbox).
- **Tests**: `tests/` drive the CLI seam against `src/obench/testing/fake.py` — a fake ollama.com serving chat/usage/models over the injected transport — plus a fake `gh` for release flows; assertions come from produced artifacts and observed requests.
- **Inputs it depends on**: `pricing/2026-08-31.json` (the versioned price-table snapshot; tables are resolved by `--table-version`, latest by default) and `docs/methodology-v1.md` conventions (bracketed batches, pp semantics, anchor amortization ÷ 4.345 weeks, S0/S1, verdict margin, hybrid composition; `METHODOLOGY_VERSION = "v1.3"`).
- **External services**: only `ollama.com` endpoints (`/api/chat`, `/api/usage`, `/v1/models`) through `client.OllamaCloud`, and the `gh` CLI for releases; analysis, predict reports, dataset export and release verification are fully offline.

## Module map

| Module | Responsibility |
| --- | --- |
| `__init__.py` | Package marker (src-layout target); the sandbox resolves `obench`'s parent for `PYTHONPATH`. |
| `agent.py` | Deterministic T3 agent loop: one billed chat request per step, JSON-action execution over an isolated working copy, per-turn salting, per-step evidence. |
| `analyze.py` | Offline analysis and bundle: cells, verdicts/margins, pp/1M thresholds, pooled allocation, sweeps, dashboard/calculator; the re-run without re-measuring. |
| `calibration.py` | Cache-calibration workstream: cold/intra/spaced prefix replays per T2-slate model; `resolve_s` measured-hit-rate precedence over S1. |
| `checkers.py` | Deterministic binary checkers per workload (no LLM-judge); T3 graded by the sandbox's pytest; unknown workloads abort. |
| `cli.py` | `bench` entry point: argparse dispatch, per-command validation, gate ordering, human/JSON reports. |
| `client.py` | Ollama Cloud HTTP client: streaming chat with TTFT stamps, usage meter, models catalog; injectable transport; env-only key. |
| `concurrency.py` | Concurrency workstream: k cut-off probe, re-anchored k∈{1,4,8} bracketed cells, cost-per-task summary. |
| `cost.py` | Dry-run budget (tokens × rates under S0/S1 with nonce overhead) and the shared `new_task_cost` pricing formula. |
| `dataset_export.py` | Lossless flattening of a release's raw evidence into JSON/CSV/XLSX tables (derived, sha256-stamped). |
| `fixtures.py` | T1 fixtures (qa_short answer key, calibration, throughput), the `build()` seam, `fixture_hash`, coordinate-derived seeds. |
| `fixtures_t2.py` | The 7 structural T2 suites plus the fixed ~20K cache-prefix prompt; parse helpers shared with checkers. |
| `fixtures_t3.py` | The 3 T3 mini-repos (bug/features/refactor), the single-JSON-action contract, `MAX_STEPS = 12`, canonical fixes. |
| `gate.py` | Spending gate: mark/require/consume of the per-level dry-run mark. |
| `lane.py` | Cache-free lane: run-scoped seeded nonces as first tokens, size rule and exemptions, canary model pinning, hash helpers. |
| `meter.py` | The meter's units and the anchor bridge (leaf, zero internal imports): TICK_PP/TICK_BAND, WEEKS_PER_MONTH/SESSION_R/DEFAULT_CREDIT_RATIO, `usd_per_pp`/`session_usd_per_pp`. |
| `preflight.py` | Catalog preflight: slate coverage against `/v1/models`, tag matching, drift abort before billing. |
| `predict.py` | Predictability HITL flow: hash-locked blind/informed estimate registries + comparative MAPE report (paired bootstrap CI). |
| `pricing.py` | Versioned price table loading/validation (input / cached input / output rates, `per` unit). |
| `pricing_pull.py` | `bench pricing-pull`: snapshots the upstream rate card (ollama-cloud-catalog artifact) into a new versioned table — fail-loud validation, alias mapping, rate-by-rate diff, immutable-landing refusal, peak block as metadata. |
| `releases.py` | Dataset releases via `gh`: package/publish/fetch/verify (sha256 both directions, credential scrub, frozen v2 handling). |
| `runner.py` | Bracketed-batch protocol: plan, manifest/resume/drift guards, burst, registration settle, Δpp, billing canary, raw line writing; `status_doc` (the single manifest-shape→report reader) next to `Manifest`. |
| `sandbox.py` | T3 checker sandbox: rebuild the graded copy (fixture-owned tests/config), run pytest in a bounded subprocess. |
| `sandbox_runner.py` | Sandbox subprocess entry: network guard install, readiness handshake, exit-code grading, exit 90 on misconfiguration. |
| `schema.py` | On-write schema validation of every raw dataset line (request/batch/probe/canary/estimate); the tolerant raw readers `read_jsonl`/`read_dataset`. |
| `workloads.py` | Workload table (requests, token shapes) and model slates per level; the STRONG/WEAK T2 hybrid split. |
