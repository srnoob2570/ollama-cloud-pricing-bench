# live-probes: one-off, owner-run live instruments

Manual, single-purpose test scripts and their raw logs. These are NOT harness
protocol data (`runs/`/`batches/` carry that); nothing here feeds the releases.
Every script here bills real quota, reads the key only from the environment,
and is run by the owner personally. The agent never fires a request.

| File | What it is |
|---|---|
| `kimi_session_weekly_test.py` | The original non-cached bracket (unique ~400-word nonce per request) that produced the verified R = 6.22 bracket in `docs/research/pp-sesion-usd-2026-09-01.md` §8. |
| `kimi-bracket-series.jsonl` | That bracket's raw meter/chat log. |
| `kimi_paired_cache_probe.py` | The paired redo: arms A (fresh nonce per request, forced cache misses), B1 (fixed nonce, first firing), B2 (fixed nonce, warm refire). Same T2 `long_context` body and same nonce budget in every arm. |
| `kimi-paired-cache-probe.jsonl` | The paired run's raw meter/chat log (2026-09-01). |
| `kimi-paired-cache-probe-20260901-console.txt` | The captured console transcript of that run (includes the SUMMARY ratios). |

Findings and their reading live in `docs/research/cache-pareado-kimi-2026-09-01.md`.

Erratum for reuse: the paired script's `*_pp/1M` summary label is off by ×100.
It prints the usage-fraction delta per 1M tokens (e.g. `0.139`), so the
conventional pp/1M value is ×100 (13.93). Ratios are unaffected. The tracked
copies are what ran, after this repo's pre-commit formatting (end-of-file
fixer, ruff format, style only, no behavioral change); the raw JSONL keeps
the exact floats.
