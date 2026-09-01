"""Bracketed session<->weekly test on kimi-k3 — NON-CACHED variant.

Fix for the cache flaw the owner caught: identical prompts let the prefix
cache discount requests 2..N (measured live: 5 identical long_context
requests cost +3 session ticks instead of the full-price ~25; a rerun on the
warm cache cost +3 more with ZERO weekly movement). This variant prepends a
UNIQUE random nonce block (~400 words) to every request, so each request's
prefix is distinct and nothing is cached — every request bills at full price.
The nonces are random per run, so re-running never inherits a warm cache.

Phase design (owner-specified): quiet sleep -> pre read -> burst -> settle
sleep -> post read -> confirm. The only requests sent are the granted
kimi-k3 ones + free meter reads; glm-5.3-flash counts pre/post quantify any
contamination.
"""

import asyncio
import json
import random
import sys
import time

sys.path.insert(0, "src")
from ocharness import fixtures  # noqa: E402
from ocharness.client import OllamaCloud  # noqa: E402

MODEL = "kimi-k3"
N_REQUESTS = 10
SEED = 2488655082790996814  # the T2 rep-1 seed for (long_context, kimi-k3)
NONCE_WORDS = 400  # ~400-600 tokens of unique noise ahead of the shared body
QUIET_S = 5.0
SETTLE_S = 5.0
CONFIRM_S = 5.0
LOG = "/tmp/kimi-bracket-series.jsonl"


def emit(line):
    print(line, flush=True)


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


def log(row):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def unique_prompt(base_prompt: str, i: int) -> str:
    """A fresh prefix per request: the cache keys on the prefix, so a unique
    leading nonce defeats cross-request reuse entirely."""
    rng = random.Random(f"{time.time_ns()}-{i}")
    nonce = " ".join(f"qz{rng.randrange(10**6)}" for _ in range(NONCE_WORDS))
    return f"{nonce}\n\n{base_prompt}"


async def main():
    base_prompt = fixtures.build("T2", "long_context", 1)[0].prompt
    client = OllamaCloud()
    t0 = time.time()

    emit(f"PHASE0 launch t=0.0 - quiet sleep {QUIET_S:.0f}s begins (owner pauses glm-5.3-flash)")
    _st, pre_early = await client.usage()
    u, kimi, glmflash = fields(pre_early)
    emit(f"PRE_EARLY usage={u} kimi={kimi} glmflash={glmflash}")
    log({"t_rel": 0.0, "phase": "pre_early", "usage": u, "kimi": kimi, "glmflash": glmflash})

    await asyncio.sleep(QUIET_S)
    _st, pre = await client.usage()
    u_pre, kimi_pre, glm_pre = fields(pre)
    emit(
        f"PHASE2 PRE (bracket opens) t={time.time() - t0:.1f} usage={u_pre} kimi={kimi_pre} glmflash={glm_pre}"
    )
    log(
        {
            "t_rel": round(time.time() - t0, 1),
            "phase": "pre",
            "usage": u_pre,
            "kimi": kimi_pre,
            "glmflash": glm_pre,
        }
    )

    emit(f"PHASE3 burst: {N_REQUESTS} x long_context (unique prefix per request - no cache)")
    tokens_total = 0
    for i in range(N_REQUESTS):
        prompt = unique_prompt(base_prompt, i)
        t_fire = time.time()
        rec = await client.chat(model=MODEL, prompt=prompt, seed=SEED)
        done = rec["done"] or {}
        tok_in = done.get("prompt_eval_count")
        tokens_total += (tok_in or 0) + (done.get("eval_count") or 0)
        emit(
            f"CHAT req={i + 1}/{N_REQUESTS} http={rec['http']} t={time.time() - t_fire:.2f}s "
            f"tok_in={tok_in} tok_out={done.get('eval_count')} err={rec['err']}"
        )
        log(
            {
                "t_rel": round(time.time() - t0, 1),
                "phase": "chat",
                "req": i + 1,
                "tok_in": tok_in,
                "tok_out": done.get("eval_count"),
                "err": rec["err"],
            }
        )

    emit(f"PHASE4 settle sleep {SETTLE_S:.0f}s (registration <=6 s; margin)")
    await asyncio.sleep(SETTLE_S)
    _st, post = await client.usage()
    u_post, kimi_post, glm_post = fields(post)
    emit(
        f"PHASE5 POST (bracket closes) t={time.time() - t0:.1f} usage={u_post} kimi={kimi_post} glmflash={glm_post}"
    )
    log(
        {
            "t_rel": round(time.time() - t0, 1),
            "phase": "post",
            "usage": u_post,
            "kimi": kimi_post,
            "glmflash": glm_post,
        }
    )

    await asyncio.sleep(CONFIRM_S)
    _st, confirm = await client.usage()
    u_conf, kimi_conf, glm_conf = fields(confirm)
    emit(
        f"PHASE6 CONFIRM t={time.time() - t0:.1f} usage={u_conf} kimi={kimi_conf} glmflash={glm_conf}"
    )
    log(
        {
            "t_rel": round(time.time() - t0, 1),
            "phase": "confirm",
            "usage": u_conf,
            "kimi": kimi_conf,
            "glmflash": glm_conf,
        }
    )

    ds = (u_post.get("session") or 0) - (u_pre.get("session") or 0)
    dw = (u_post.get("weekly") or 0) - (u_pre.get("weekly") or 0)
    ratio = round(ds / dw, 2) if dw else None
    d_glm_s = (glm_post.get("session") or 0) - (glm_pre.get("session") or 0)
    d_kimi = (kimi_post.get("session") or 0) - (kimi_pre.get("session") or 0)
    emit(
        f"RESULT dpp_session={ds:+.3f} dpp_weekly={dw:+.3f} R(session:weekly)={ratio} "
        f"kimi_requests={d_kimi} glmflash_requests={d_glm_s} tokens_billed={tokens_total}"
    )
    emit("BRACKET DONE")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
