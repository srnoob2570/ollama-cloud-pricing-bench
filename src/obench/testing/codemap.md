# src/obench/testing/

## Responsibility

In-repo test double for ollama.com — the harness's test seam. `FakeOllama` (`fake.py`) is a fake model API transport for the benchmark CLI: it reproduces the behavior measured during the live meter verification (usage in 0.001 ticks with read lag, instant and exact per-model `request_count`, scriptable errors) so tests exercise the CLI's real code paths without touching the network, the API key, or quota. `__init__.py` is an empty package marker.

## Design

Test-double-at-the-seam: the fake implements ollama.com's three endpoints (`/api/chat`, `/api/usage`, `/v1/models`) behind `httpx.MockTransport` and is injected where the real transport would go; it never opens a socket. All endpoints demand the `authorization` header (401 otherwise) and every request is appended to `self.calls` (`path`, auth-header presence, parsed JSON body) — the observable the test suite asserts on.

- **Transports.** `transport()` is the sync seam only (drives the handler on a throwaway loop per request; its requests never overlap). The harness's `AsyncClient` must take `async_transport()`: the handler is a coroutine, so k-bursts genuinely overlap at `chat_latency` and the per-key 429 branch fires exactly like the real endpoint.
- **Deterministic world.** The default reply is the canned "world" stream — two content chunks plus a done frame with (26, 12) prompt/eval token counts, exactly as the first tests pinned. `reply_for`, `counts_for`, and `tool_calls_for` remap the reply text, the reported token counts, and the tool-call frames per prompt; streaming always emits the scripted chunks verbatim in NDJSON lines.
- **Meter simulation.** `program_consumption(ticks)` accumulates pending quota; `_read_meter()` moves session and weekly usage by 0.001-tick deltas only after `lag_reads` reads since programming, while per-model `request_count` registers instantly and exactly — the settlement behavior a bracketed batch relies on. Scriptable pathologies: `undercount_at` (request accepted and billed but the counter never sees it — the signature the runner's post-burst count check catches), `drift_ticks_per_read` (no two consecutive reads agree, so registration burns its cap), `reject_from`/`reject_all` (429s), `fails_on` (one-shot 500), `truncate_stream` (a 200 stream billed but ending without a done frame), `concurrency_limit` (in-flight cap), and transport raises (`chat_raise`, `usage_raise`, `catalog_raise` with ordinal gating).
- **Cache scripting (the calibrate-cache seam).** With `cache_horizon_s` set, the fake caches per (model, prompt): a repeat within the horizon is a hit — the done-object reports `prompt_eval_cache_hit_count` and a reduced `prompt_eval_count` (`cached_eval_count`, the prompt's tail), the meter bills `cached_ticks` instead of `ticks_per_request`, and the hit answers `cache_ttft_benefit_s` sooner (a skipped prefill). Every serve — hit or miss — refreshes the cache: the real behavior prefix replay's between-batches spacing probes. `cache_report_hits = False` models a deployment that caches but never reports the field; with the horizon unset the done-object carries no cache-hit field at all. These are the three evidence shapes the calibration's measured hit-rate must distinguish — explicit zero (evidence of no hits), absent field (no token evidence), nonzero (replaces the S1 assumption per model) — under the persisted S0/S1 reference pair.
- **Recording.** The fake records what it observed, never what the CLI decided internally: assertions come from `fake.calls` plus the artifacts the run produced.

## Flow

1. A test requests the `fake_cli` fixture; it sets `OLLAMA_API_KEY` from the environment (never written into any dataset) and monkeypatches `client.default_transport` to return the fake's async transport.
2. The test invokes the CLI as-is. The CLI's client hits the fake — it cannot tell the difference from the real API.
3. The fake observes each request, simulates the meter (with lag), and returns real response shapes (NDJSON streams, done frames, usage objects, model catalogs), so the run produces the same raw JSONL artifacts a real run would.
4. Assertions come from those artifacts and the requests the fake observed. The fake is workload-agnostic: it serves whatever prompt the CLI sends, from any fixture — including code fixtures whose tests act as the checker — and never inspects or validates outcomes itself.

## Integration

- `tests/conftest.py` owns the wiring: the `fake` fixture builds a plain `FakeOllama`; `fake_cli` installs it as the CLI's transport seam and stamps the default world — `catalog` = the official 19-rate table (`standard_table()`, read from `pricing/2026-08-31.json` so T2/T3 slates resolve in dry-runs), `cache_horizon_s = 3600`, `cache_report_hits = False`: the paired probe's measured world, where the endpoint caches, the billing canary's replay volley bills the discount, and hits stay invisible in reported tokens.
- `write_table()` writes versioned test price tables; `no_discount_table()` builds one where cached_input = input (S1 ≡ S0) for verdict/preflight tests.
- The CLI remains the only seam: tests drive it end-to-end against the fake and never mock harness internals; targeted unit tests are accepted only for pure analysis functions (checkers, fixtures, the calibration analyzer), per the harness spec. The same guardrails hold as for real runs — the key lives only in the environment, and raw JSONL produced under the fake is treated as immutable raw data.
