"""The cache-free lane (methodology v1.2): run-scoped seeded nonces force cache misses.

Ollama Cloud's prefix cache keys from token 0 and offers no toggle: every request
whose prefix repeats an earlier one bills at the cache discount, and the legacy
meter's pp then reads the discounted work, never the workload's raw cost. The
lane is the protocol v3 answer — the RUNNER salts every measured request with a
deterministic, run-scoped nonce as its very first tokens:

- ``nonce_seed = f(run_id)`` is recorded in the manifest (the run's reproducibility:
  fixture seed + nonce seed regenerate every sent prompt within the run);
- ``nonce_i = RNG(nonce_seed, i)`` derives each request's nonce from stable cell
  coordinates (level, workload, model, rep, k, request index — plus the step
  number for the T3 loop's turns), so a resume re-derives the same nonce per
  request and a new run_id never reuses one;
- the nonce's size is ~1.5 % of the workload's expected input tokens, clamped to
  [4, 400] words — long bodies stay inside the paired probe's validated zone
  (``live-probes/``, commit ``1f3c564``: ~400 words defeated the cache at ~40K
  tokens), short prompts carry the minimum, which is enough because the first
  differing token invalidates the whole prefix match.

Multi-turn salts every turn: the T3 loop's steps and multi_turn's requests each
carry their own nonce, so the measured pp is the raw cost of re-sending the
context. Exempt traffic (unsalted, its lines carry ``nonce_sha256 = null``):
``calibrate-cache``'s prefix replays, the billing canary's replay volley, and the
concurrency probe (a locator, not a measured cell).

The fixtures are never touched: salting happens at send time, and every request
line persists ``prompt_sha256`` (the exact prompt billed) + ``nonce_sha256``.
"""

from __future__ import annotations

import hashlib

# The nonce's size rule: ~1.5 % of the workload's expected input tokens, clamped
# to [4, 400] words (the paired probe's validated zone; the floor defeats the
# cache on short prompts by the prefix-match mechanism itself).
NONCE_SHARE = 0.015
NONCE_WORDS_MIN = 4
NONCE_WORDS_MAX = 400

# The dry-run/predict estimates' tokenization allowance per nonce word: the
# generator emits common English words, which tokenize a little above one token
# each. The runner's overhead is the word count itself; this factor only prices
# the estimate (the nonce's real token count lands in the raw tok_in).
TOKENS_PER_WORD = 1.3

# The nonce's wordlist: common English words, lowercase, no punctuation — noise
# to the model, unique prefixes to the cache. 96 words keep every nonce well
# inside the clamped size while the sha256 stream picks them without repetition
# guarantees (repeats are harmless: the prefix is what differs per request).
_WORDS: tuple[str, ...] = (
    "anchor",
    "basin",
    "candle",
    "delta",
    "ember",
    "fable",
    "garnet",
    "harbor",
    "ivory",
    "jasper",
    "kernel",
    "lagoon",
    "marble",
    "nectar",
    "orchid",
    "pixel",
    "quartz",
    "ribbon",
    "saddle",
    "timber",
    "umber",
    "velvet",
    "willow",
    "yonder",
    "zenith",
    "accordion",
    "brigade",
    "cinnamon",
    "domino",
    "eagle",
    "falcon",
    "granite",
    "hazel",
    "index",
    "juniper",
    "kelp",
    "lantern",
    "meadow",
    "nickel",
    "opal",
    "pepper",
    "quiet",
    "raven",
    "saffron",
    "tulip",
    "urchin",
    "vessel",
    "walnut",
    "xenon",
    "yarn",
    "zephyr",
    "amber",
    "beacon",
    "cactus",
    "dune",
    "echo",
    "forest",
    "glacier",
    "heron",
    "island",
    "jungle",
    "kayak",
    "lotus",
    "mosaic",
    "nimbus",
    "oasis",
    "prism",
    "quiver",
    "reef",
    "summit",
    "tundra",
    "umbra",
    "vertex",
    "whisk",
    "yeast",
    "zinc",
    "almond",
    "birch",
    "copper",
    "drift",
    "elm",
    "fjord",
    "grove",
    "haven",
    "ingot",
    "jade",
    "krill",
    "lichen",
    "moss",
    "noble",
    "onyx",
    "petal",
    "quill",
    "ridge",
    "spruce",
    "thistle",
)

# The nonce word count for a workload the level table does not carry (the
# concurrency workstream's anchor fixture reuses the calibration body): a small
# expected input lands on the clamp's minimum, which is all a short prompt needs.
EXPECTED_TIN_FALLBACK = 100


def nonce_seed(run_id: str) -> str:
    """The run's nonce seed, recorded in the manifest: f(run_id), never repeated
    across runs (a new run_id salts freshly — no warm starts from an old run)."""
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]


def nonce_index(*coords) -> int:
    """The request's stable coordinate hash — the ``i`` of nonce_i = RNG(seed, i).

    Cell coordinates (level, workload, model, rep, request index) plus the turn
    number where the workload is multi-turn, so the derivation is stable across
    resumes and unique per billed request inside the run."""
    material = "|".join(str(c) for c in coords).encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def nonce_words(expected_in: int) -> int:
    """~1.5 % of the expected input tokens, clamped to [4, 400] words (floored:
    the rule prices an estimate, it never rounds a persisted measurement)."""
    words = int(expected_in * NONCE_SHARE)
    return max(NONCE_WORDS_MIN, min(NONCE_WORDS_MAX, words))


def nonce_text(seed: str, index: int, words: int) -> str:
    """RNG(nonce_seed, i): a deterministic word stream from the seed and the
    request's coordinate hash — sha256 in counter mode, so no Python RNG
    internals sit between the manifest and the sent prompt."""
    salida: list[str] = []
    for j in range(words):
        digest = hashlib.sha256(f"{seed}|{index}|{j}".encode("utf-8")).digest()
        salida.append(_WORDS[int.from_bytes(digest[:4], "big") % len(_WORDS)])
    return " ".join(salida)


def salted_prompt(prompt: str, nonce: str) -> str:
    """The wire prompt: the nonce as its very first tokens, then the fixture."""
    return f"{nonce}\n\n{prompt}"


def prompt_sha256(prompt: str) -> str:
    """sha256 over the exact prompt bytes sent (the line's billing evidence)."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def nonce_sha256(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def expected_tin(level: str, workload: str) -> int:
    """The workload's expected input tokens (the nonce size's basis), from the
    level's workload table; the fallback covers workstreams outside it."""
    from . import workloads  # deferred: the lane must import cheaply everywhere

    for carga in workloads.WORKLOADS_BY_LEVEL.get(level, ()):
        if carga.name == workload:
            return carga.t_in
    return EXPECTED_TIN_FALLBACK


def lane_spec(run_id: str) -> dict:
    """The manifest's lane record: the flag, the nonce spec, the exemptions."""
    return {
        "mode": "cache-free",
        "nonce_seed": nonce_seed(run_id),
        "nonce_rule": (
            "nonce_i = RNG(nonce_seed, i), i = the request's stable cell coordinates; "
            f"size = clamp(floor({NONCE_SHARE} * expected_input), {NONCE_WORDS_MIN}, "
            f"{NONCE_WORDS_MAX}) words; prepended as the request's first tokens; "
            "multi-turn salted per turn"
        ),
        "exempt": ["calibrate-cache", "billing-canary", "concurrency-probe"],
    }


def nonce_tokens_estimate(expected_in: int) -> int:
    """The estimate's nonce overhead in tokens (dry-run/predict): the clamped
    word count priced at TOKENS_PER_WORD, floored — the runner bills the words,
    this only prices the estimate's tokenization."""
    return int(nonce_words(expected_in) * TOKENS_PER_WORD)
