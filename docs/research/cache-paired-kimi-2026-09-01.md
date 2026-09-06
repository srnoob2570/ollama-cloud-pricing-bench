# Paired cache on kimi-k3: the prefix discount, measured with a clean design

**Date**: 2026-09-01 · **Type**: live research (owner-run) · **Input to**: the "cache-free
lanes" requirement, the correction of the `pp-session-usd-2026-09-01.md` §8 finding, and the
v3 composition (measurability). **Instruments and raw logs**:
[`live-probes/`](../../live-probes/README.md).

## 1. The question

The §8 finding ("an exact-prefix replay bills at ~11 % of the price, ~9× cheaper") had two
defects the owner pointed out:

1. **The pairing was never proven on the meter**: the cached arm (5 identical requests) and
   the salted arm (10 with nonce) came from different script variants; nothing proves they
   shared the same prompt body.
2. **The "11 % of the price" framing mixes denominators**: the legacy meter does not bill by
   tokens. It measures usage (declared GPU-time, opaque). The correct ratio is over consumed
   quota, not price per token; §8's "full price" was meter ticks, not dollars.

Also, external research (docs.ollama.com, ollama/ollama #16714/#15758) confirmed that
**there is no cache toggle**. The cache is implicit, prefix-indexed, and invisible
(`cached_tokens` is never reported). "Turning the cache off" operationally = forcing a
cache miss with a random nonce at the start of every prompt (the cache matches the prefix
left→right from token 0).

## 2. Paired design (`live-probes/kimi_paired_cache_probe.py`)

Same body (T2 `long_context`, sha `ca123ce574e4febc`, 153,071 chars), same nonce budget
(~400 words) in all three arms; the only variable is whether the nonce repeats. Each phase
is its own bracket (quiet 5 s → pre → serial burst → settle 15 s → post → confirm 30 s),
with a contamination guard (`glm-5.3-flash` counts pre/post, flat in both windows for the
whole run: 391/10,558). 30 real requests, all 200, `tok_in` 39,892–39,893/request.

| Arm | Construction | Expected |
|---|---|---|
| **A** cache-free | fresh nonce per request (10 distinct prefixes) | 10 forced cache misses, full price |
| **B1** cold replay | one fixed nonce, first pass | req 1 miss, reqs 2–10 hits |
| **B2** warm replay | same fixed nonce, second pass (~35 s later) | 10 hits |

## 3. Results

| Arm | Δpp session | Δpp weekly | session ticks/req | tokens | R (s:w) | latency |
|---|---|---|---|---|---|---|
| A | +0.056 (56 ticks) | +0.008 (8) | **5.6** | 402,062 | 7.0 | 5.0–6.8 s |
| B1 | +0.011 (11) | +0.002 (2) | 1.1 | 401,531 | 5.5 | req 1: 5.26 s (cold); rest 2.4–7.4 s |
| B2 | +0.008 (8) | +0.001 (1) | **0.8** | 402,066 | 8.0 | 2.3–6.0 s (mostly warm) |

**Ratios within the legacy meter** (session pp/1M): B2/A = **0.143** (exactly 1/7),
B1/A = 0.197, weekly B2/A = 0.125.

## 4. Reading

1. **Arm A replicates the §8 verified bracket**: 402,062 vs 402,150 tokens;
   +0.056/+0.008 vs +0.056/+0.009. The salted design is re-validated: `tok_in` uniformity
   (39,892–39,893) and uniform full price per request.
2. **The cached-work discount is r ≈ 0.11–0.15** (central read ~1/7 ≈ 0.14): a request
   served from cache consumes ~7× less quota than the same request cache-free (band 7–9×).
   B1 implies r ≈ 0.108 by tick arithmetic ((1+9r)/10 = 0.197, req 1 cold); B2 gives exactly
   1/7. §8's ~11 % falls inside the band. Its magnitude survives, now with proven-identical
   prefixes and the correct framing.
3. **The corrected framing (owner)**: Ollama Cloud's cache reduces the work the legacy
   meter reflects; it is not a per-token bill discount (the legacy plan has no per-token
   price). Consequence: each pp buys ~7× more cached work. That is more effective plan
   capacity, not a "discounted price". kimi-k3's published 10 % ($3.00 → $0.30 cached) is a
   different denominator (a billing discount on the new side); its closeness to the measured
   ratio is suggestive, not established, and per model.
4. **Persistence**: B2 started warm immediately (~35 s after B1) and in the §8 test the
   replay was warm *before* the cached arm's first request (it had persisted since the T2
   bracket hours earlier). The cache's horizon exceeds minutes; the fine-grained
   measurement (5/30/90 s) is `calibrate-cache` work.
5. **Latency as TTFT corroboration**: warm requests visibly run faster (B2 ~2.3–6.0 s vs
   A ~5.0–6.8 s), the second signal, next to Δpp, that prefill is being skipped.
6. **R (session:weekly) is noisy on cached arms** (5.5/8.0, weekly Δpp sub-tick): the
   R ≈ 5–7 band stays anchored to full-price requests; cache ratios should ride on session
   (the practically finer readout), with weekly as corroboration.

## 5. Consequences for decisions in flight

- **The v2 dataset's documented defect**: every request whose queue repeats prefixes
  (bracket reps, identical k>1 cells, multi-turn turns, warm re-runs) underestimates the raw
  cost on the legacy meter. The cache-free lanes (per-request nonce, recorded in the
  manifest) are a protocol v3 requirement, not a refinement.
- **Correction to the #36 record**: the finding is re-stated as "an exact-prefix replay
  consumes ~11–14 % of the quota the same cache-free request consumes (kimi-k3, ~40K-token
  prefix)", never as "11 % of the price". The *Cache scenario* glossary entry ("the legacy
  side measures the caching Ollama actually does") dies under cache-free lanes: the legacy
  side will measure cache-free work; the cache is only observed in calibration.
- **The billing canary** (a prefix replay must bill ~1/7–1/10; if it bills ~100 % with
  salted requests in flight, the salting broke) stands as the operational safeguard of v3.

## Files

- [`live-probes/kimi_paired_cache_probe.py`](../../live-probes/kimi_paired_cache_probe.py) ·
  [`kimi_session_weekly_test.py`](../../live-probes/kimi_session_weekly_test.py) (instruments)
- [`live-probes/kimi-paired-cache-probe.jsonl`](../../live-probes/kimi-paired-cache-probe.jsonl) ·
  [`kimi-bracket-series.jsonl`](../../live-probes/kimi-bracket-series.jsonl) (raw logs)
- [`live-probes/kimi-paired-cache-probe-20260901-console.txt`](../../live-probes/kimi-paired-cache-probe-20260901-console.txt) (transcript, with SUMMARY)
- Spend: 30 requests ≈ 1.21 M tokens ≈ +7.5 session pp, +1.1 weekly pp (0.426 → 0.437).
