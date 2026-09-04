"""Paired cache probe on kimi-k3 — same body, same nonce budget, one variable.

Redo of the live prefix-cache test (the first non-cached bracket lives in
/tmp/kimi_session_weekly_test.py and its log in /tmp/kimi-bracket-series.jsonl).
The original ~11% "cached discount" compared two arms whose prompt bodies are
not proven equal in the record — this script pairs the arms exactly: the SAME
T2 long_context body and the SAME ~400-word nonce budget in every arm, so the
only variable is whether the leading nonce repeats.

Phases (each its own bracket: quiet -> pre -> burst -> settle -> post ->
confirm, glm-5.3-flash counts quantifying contamination):

  A  cache-free : fresh random nonce per request  -> 10 forced cache MISSES
                  (all requests bill full price; this is the "no cache" arm)
  B1 replay cold: one fixed nonce, 10 firings     -> hits after the first
  B2 replay warm: same fixed nonce again after the settle -> all hits

The verdict number is computed INSIDE the legacy meter (session pp per 1M
billed tokens of B2 vs A) — a work/usage ratio, never a token-price claim.
Nonces are random per run, so a rerun never inherits a warm cache from this
script's own phases; a B1/B2 warm start from earlier runs of the same T2 prompt
is a persistence finding, not an error — the PRE counts and glmflash counts
expose it. The owner runs this personally; the agent never bills a request.

Usage (from the repo root):
  python3 /tmp/kimi_paired_cache_probe.py --plan            # no network, no key
  python3 /tmp/kimi_paired_cache_probe.py                   # A + B1 + B2
  python3 /tmp/kimi_paired_cache_probe.py --phases A        # only the cache-free arm
  python3 /tmp/kimi_paired_cache_probe.py --phases B1,B2    # only the replay arms
"""

import argparse
import asyncio
import hashlib
import json
import random
import sys
import time

sys.path.insert(0, "src")
from obench import fixtures  # noqa: E402
from obench.client import OllamaCloud  # noqa: E402

MODEL = "kimi-k3"
SEED = 2488655082790996814  # the T2 rep-1 seed for (long_context, kimi-k3)
N_REQUESTS = 10
NONCE_WORDS = 400  # ~400-600 tokens of noise ahead of the shared body
QUIET_S = 5.0
SETTLE_S = 15.0
CONFIRM_S = 30.0
LOG = "/tmp/kimi-paired-cache-probe.jsonl"


def emit(line):
    print(line, flush=True)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def log(row):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def fields(payload):
    usage, kimi, glmflash = {}, {}, {}
    if not isinstance(payload, dict):
        return usage, kimi, glmflash
    for w in ("session", "weekly"):
        try:
            usage[w] = payload["limits"][w]["usage"]
        except (KeyError, TypeError):
            usage[w] = None
        try:
            for m in payload["limits"][w]["models"]:
                name = m.get("name")
                if name == "kimi-k3":
                    kimi[w] = m.get("request_count")
                elif name == "glm-5.3-flash":
                    glmflash[w] = m.get("request_count")
        except (KeyError, TypeError):
            pass
    return usage, kimi, glmflash


def fresh_nonce(rng):
    return " ".join(f"qz{rng.randrange(10**6)}" for _ in range(NONCE_WORDS))


def build_phases(base_prompt, n):
    """A gets n distinct prompts (forced misses); B1/B2 share one fixed prompt
    (prefix hits after the first). Same body, same nonce word budget everywhere."""
    rng = random.Random()  # system entropy: fresh per run, never a warm start
    fresh = [f"{fresh_nonce(rng)}\n\n{base_prompt}" for _ in range(n)]
    fixed = f"{fresh_nonce(rng)}\n\n{base_prompt}"
    return {"A": fresh, "B1": [fixed] * n, "B2": [fixed] * n}


async def bracket(client, phase, prompts, t0):
    """One bracketed phase; returns its measured block (exact floats kept)."""
    emit(f"[{phase}] quiet sleep {QUIET_S:.0f}s begins")
    await asyncio.sleep(QUIET_S)
    _st, pre = await client.usage()
    u_pre, kimi_pre, glm_pre = fields(pre)
    emit(f"[{phase}] PRE t={time.time() - t0:.1f} usage={u_pre} kimi={kimi_pre} glmflash={glm_pre}")
    log(
        {
            "t_rel": round(time.time() - t0, 1),
            "phase": f"{phase}:pre",
            "usage": u_pre,
            "kimi": kimi_pre,
            "glmflash": glm_pre,
        }
    )

    tokens_total = 0
    errs = 0
    for i, prompt in enumerate(prompts, 1):
        t_fire = time.time()
        rec = await client.chat(model=MODEL, prompt=prompt, seed=SEED)
        done = rec["done"] or {}
        tok_in = done.get("prompt_eval_count")
        tok_out = done.get("eval_count")
        tokens_total += (tok_in or 0) + (tok_out or 0)
        errs += 1 if rec["err"] else 0
        emit(
            f"[{phase}] CHAT req={i}/{len(prompts)} http={rec['http']} "
            f"t={time.time() - t_fire:.2f}s tok_in={tok_in} tok_out={tok_out} err={rec['err']}"
        )
        log(
            {
                "t_rel": round(time.time() - t0, 1),
                "phase": f"{phase}:chat",
                "req": i,
                "tok_in": tok_in,
                "tok_out": tok_out,
                "err": rec["err"],
            }
        )

    emit(f"[{phase}] settle sleep {SETTLE_S:.0f}s (registration <=6 s; margin)")
    await asyncio.sleep(SETTLE_S)
    _st, post = await client.usage()
    u_post, kimi_post, glm_post = fields(post)
    emit(
        f"[{phase}] POST t={time.time() - t0:.1f} usage={u_post} kimi={kimi_post} glmflash={glm_post}"
    )
    log(
        {
            "t_rel": round(time.time() - t0, 1),
            "phase": f"{phase}:post",
            "usage": u_post,
            "kimi": kimi_post,
            "glmflash": glm_post,
        }
    )

    await asyncio.sleep(CONFIRM_S)
    _st, confirm = await client.usage()
    u_conf, kimi_conf, glm_conf = fields(confirm)
    emit(
        f"[{phase}] CONFIRM t={time.time() - t0:.1f} usage={u_conf} kimi={kimi_conf} glmflash={glm_conf}"
    )
    log(
        {
            "t_rel": round(time.time() - t0, 1),
            "phase": f"{phase}:confirm",
            "usage": u_conf,
            "kimi": kimi_conf,
            "glmflash": glm_conf,
        }
    )

    ds = (u_post.get("session") or 0) - (u_pre.get("session") or 0)
    dw = (u_post.get("weekly") or 0) - (u_pre.get("weekly") or 0)
    d_kimi_s = (kimi_post.get("session") or 0) - (kimi_pre.get("session") or 0)
    d_kimi_w = (kimi_post.get("weekly") or 0) - (kimi_pre.get("weekly") or 0)
    d_glm_s = (glm_post.get("session") or 0) - (glm_pre.get("session") or 0)
    emit(
        f"[{phase}] RESULT dpp_session={ds:+.3f} dpp_weekly={dw:+.3f} "
        f"kimi_requests(s/w)={d_kimi_s}/{d_kimi_w} glmflash_requests={d_glm_s} "
        f"tokens_billed={tokens_total} errs={errs}"
    )
    return {
        "pre": pre,
        "post": post,
        "confirm": confirm,
        "tokens_total": tokens_total,
        "dpp_session": ds,
        "dpp_weekly": dw,
        "kimi_requests": d_kimi_s,
        "glmflash_requests": d_glm_s,
        "errs": errs,
    }


async def main():
    ap = argparse.ArgumentParser(description="paired cache probe on kimi-k3")
    ap.add_argument(
        "--phases",
        default="A,B1,B2",
        help="comma list among A (cache-free), B1 (replay cold), B2 (replay warm)",
    )
    ap.add_argument("--requests", type=int, default=N_REQUESTS)
    ap.add_argument(
        "--plan",
        action="store_true",
        help="build the prompts, print the pairing hashes, exit (no network)",
    )
    ap.add_argument("--yes", action="store_true", help="skip the pre-fire confirmation")
    args = ap.parse_args()
    phases = [p.strip().upper() for p in args.phases.split(",") if p.strip()]
    unknown = [p for p in phases if p not in ("A", "B1", "B2")]
    if unknown:
        emit(f"unknown phases: {unknown} (use A, B1, B2)")
        return 2

    base_prompt = fixtures.build("T2", "long_context", 1)[0].prompt
    prompts = build_phases(base_prompt, args.requests)

    if args.plan:
        emit(
            f"PLAN body={len(base_prompt)} chars, sha={sha(base_prompt)}, "
            f"nonce={NONCE_WORDS} words per request, {args.requests} req/phase"
        )
        for p in phases:
            hashes = [sha(x) for x in prompts[p]]
            emit(
                f"[{p}] {len(hashes)} prompts, distinct={len(set(hashes))}, "
                f"first_sha={hashes[0]}, body_in_every_prompt="
                f"{all(x.endswith(base_prompt) for x in prompts[p])}"
            )
        emit("PLAN DONE (no request sent)")
        return 0

    total = len(phases) * args.requests
    if not args.yes:
        emit(
            f"About to fire {total} REAL {MODEL} requests "
            f"(phases {','.join(phases)} x {args.requests}); "
            f"arm A alone ~= the earlier verified bracket (~5.6 session pp, ~0.9 weekly pp)."
        )
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            emit("aborted by owner before any request")
            return 1

    client = OllamaCloud()
    t0 = time.time()
    res = {}
    for phase in phases:
        res[phase] = await bracket(client, phase, prompts[phase], t0)
    await client.aclose()

    emit("=== SUMMARY ===")
    for phase, r in res.items():
        per1m_s = r["dpp_session"] / (r["tokens_total"] / 1e6) if r["tokens_total"] else None
        per1m_w = r["dpp_weekly"] / (r["tokens_total"] / 1e6) if r["tokens_total"] else None
        r["session_pp_per_1m"] = per1m_s
        r["weekly_pp_per_1m"] = per1m_w
        emit(
            f"{phase}: tokens={r['tokens_total']} dpp_s={r['dpp_session']:+.3f} "
            f"dpp_w={r['dpp_weekly']:+.3f} session_pp/1M={per1m_s} weekly_pp/1M={per1m_w} "
            f"errs={r['errs']}"
        )
    if "A" in res and "B2" in res:
        a, b2 = res["A"], res["B2"]
        if a["session_pp_per_1m"] and b2["session_pp_per_1m"]:
            emit(
                f"CACHED-WORK RATIO B2/A session = {b2['session_pp_per_1m'] / a['session_pp_per_1m']:.3f} "
                f"(weekly = {b2['weekly_pp_per_1m'] / a['weekly_pp_per_1m']:.3f})"
                if a["weekly_pp_per_1m"] and b2["weekly_pp_per_1m"]
                else f"CACHED-WORK RATIO B2/A (session) = {b2['session_pp_per_1m'] / a['session_pp_per_1m']:.3f}"
            )
    if "A" in res and "B1" in res:
        a, b1 = res["A"], res["B1"]
        if a["session_pp_per_1m"] and b1["session_pp_per_1m"]:
            emit(
                f"COLD-REPLAY RATIO B1/A (session) = {b1['session_pp_per_1m'] / a['session_pp_per_1m']:.3f} "
                f"(expected ~ (1+9r)/10 if only req 1 misses)"
            )
    log(
        {
            "t_rel": round(time.time() - t0, 1),
            "phase": "summary",
            "result": {
                p: {k: v for k, v in r.items() if k not in ("pre", "post", "confirm")}
                for p, r in res.items()
            },
        }
    )
    emit("PAIRED PROBE DONE")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
