"""Deterministic fixtures: seeded prompts (+ tool schemas), hashed (methodology v1 §5).

The fixture is the request sequence itself — identical across models and
repetitions so every cell serves the same load, and hash-stamped on every raw
line. T1 generators live here; the 7 structural T2 suites live in `fixtures_t2`
and the 3 T3 mini-repos in `fixtures_t3`, all behind the same `build()` seam.
qa_short carries the answer key its checker grades against; the other contracts
live in the prompts themselves. English, synthetic, version-in-repo.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json

# Version of the fixture_hash derivation (sha256 over the JSON of prompts +
# tool schemas — plus, once T3 exists, the mini-repos' file bytes). Any change
# to the algorithm re-rolls every hash: resumed runs refuse to mix schemes
# under one run_id (the runner checks the manifest).
FIXTURE_VERSION = "2"

CALIBRATION_PROMPT = (
    "This is a calibration request for a cost benchmark. Reply with exactly the single word: OK"
)

THROUGHPUT_PROMPT = (
    "List every number from 1 to 150 in order, separated by commas only. "
    "Then, on a new line, write the single word: DONE"
)

# The exact suffix every qa_short prompt carries (kept here so prompt and checker
# can never disagree about where the question ends).
QA_SHORT_SUFFIX = " Answer in one short sentence."

# The answer key the qa_short checker grades against — the single source for
# both the questions (QA_SHORT_QUESTIONS derives from it) and the accepted
# spellings a correct short answer may take (digits or words, common aliases,
# the natural-language equivalents of the study's own context). Any one of them
# suffices; where the world has two defensible answers, both are accepted.
QA_SHORT_ANSWERS: dict[str, tuple[str, ...]] = {
    "What is the capital of France?": ("Paris",),
    "How many days are there in a leap year?": ("366", "three hundred sixty six"),
    "What is 7 times 8?": ("56", "fifty six"),
    "Name the largest ocean on Earth.": ("Pacific",),
    "What is the official language of Brazil?": ("Portuguese", "português"),
    "Who wrote Romeo and Juliet?": ("Shakespeare", "William Shakespeare"),
    "What is the boiling point of water in Celsius?": ("100", "one hundred", "a hundred"),
    "How many continents are there?": ("7", "seven"),
    "What is the chemical symbol for gold?": ("Au",),
    "Which planet is closest to the Sun?": ("Mercury",),
    "What is the square root of 144?": ("12", "twelve"),
    "Name the longest river in the world.": ("Nile",),
    "What color results from mixing blue and yellow paint?": ("Green",),
    "How many minutes are in an hour?": ("60", "sixty"),
    "What is the tallest mountain above sea level?": ("Everest", "Mount Everest"),
    "How many sides does a hexagon have?": ("6", "six"),
    "What is the freezing point of water in Fahrenheit?": ("32", "thirty two"),
    "Which currency is used in Japan?": ("Yen",),
    "What is the largest desert in the world?": ("Sahara", "Antarctica"),
    "How many strings does a standard guitar have?": ("6", "six"),
}

# The 20 short Q&A prompts, in the answer key's order (qa_short fires exactly
# one per request, in order): a single derivation from the key, never a copy.
QA_SHORT_QUESTIONS: tuple[str, ...] = tuple(QA_SHORT_ANSWERS)


def question_of(prompt: str) -> str:
    """The QA question a qa_short prompt carries (the fixture suffix stripped).

    Raises on a prompt that is not a known qa_short fixture: a prompt/checker
    mismatch is a fixture drift, never a model verdict.
    """
    if not prompt.endswith(QA_SHORT_SUFFIX):
        raise ValueError(f"qa_short prompt does not carry the fixture suffix: {prompt!r}")
    pregunta = prompt[: -len(QA_SHORT_SUFFIX)]
    if pregunta not in QA_SHORT_ANSWERS:
        raise ValueError(f"qa_short prompt is not in the answer key: {pregunta!r}")
    return pregunta


def _t1_prompts(workload: str, n: int) -> list[str]:
    if workload == "qa_short":
        if n > len(QA_SHORT_QUESTIONS):
            # Silent truncation would send fewer requests than the batch's n and
            # crash the burst mid-gather AFTER the requests were billed.
            raise ValueError(
                f"qa_short carries {len(QA_SHORT_QUESTIONS)} prompts, not {n} - the "
                "workload table disagrees with the fixture"
            )
        return [f"{q}{QA_SHORT_SUFFIX}" for q in QA_SHORT_QUESTIONS[:n]]
    if workload in ("calibration", "concurrency"):
        # The study's cheapest bracketable request. `concurrency` is the
        # concurrency workstream's anchor fixture (methodology v1 §6): n copies
        # per cell so every k∈{1,4,8} cell carries the same total tokens, and
        # the probe's volleys fire the same single short request at increasing
        # k. Both grade the same contract (the aliased checker).
        return [CALIBRATION_PROMPT] * n
    if workload == "throughput":
        return [THROUGHPUT_PROMPT] * n
    raise ValueError(f"unknown T1 workload: {workload!r}")


@dataclasses.dataclass(frozen=True)
class RequestSpec:
    """One planned request: its prompt plus the tool schemas sent alongside.

    T3 tasks also carry `repo`: the mini-repo's (relative path, content) pairs,
    seeded into the task's working copy by the agent loop.
    """

    prompt: str
    tools: tuple[dict, ...] = ()
    repo: tuple[tuple[str, str], ...] = ()


@functools.cache
def _build_cached(level: str, workload: str, n: int) -> tuple[RequestSpec, ...]:
    """The workload's request specs, generated once per (level, workload, n)."""
    if level == "T1":
        return tuple(RequestSpec(texto) for texto in _t1_prompts(workload, n))
    if level == "T2":
        from . import fixtures_t2  # lazy: T1 runs never load the T2 generators

        return tuple(
            RequestSpec(prompt=texto, tools=tuple(herramientas))
            for texto, herramientas in fixtures_t2.specs(workload, n)
        )
    if level == "T3":
        from . import fixtures_t3  # lazy: T1/T2 runs never load the T3 generators

        return tuple(
            RequestSpec(prompt=texto, repo=tuple(archivos))
            for texto, archivos in fixtures_t3.specs(workload, n)
        )
    raise ValueError(f"fixture generators for {level!r} arrive with a later Harness ticket")


def build(level: str, workload: str, n: int) -> tuple[RequestSpec, ...]:
    """The workload's `n` deterministic request specs (prompts + tool schemas).

    Cached: the tuple is shared and must be treated as immutable — every cell
    of a workload serves the exact same requests.
    """
    return _build_cached(level, workload, n)


def fixture_hash(specs) -> str:
    """sha256 over the batch's exact request specs (prompts + tool schemas +
    repo files when the workload carries a mini-repo).

    The `repo` key joins the material only when some spec carries one: the
    T1/T2 hashes stay byte-identical to the pre-T3 dataset.
    """
    con_repo = any(s.repo for s in specs)
    material = json.dumps(
        [
            {
                "prompt": s.prompt,
                "tools": list(s.tools),
                **({"repo": [list(par) for par in s.repo]} if con_repo else {}),
            }
            for s in specs
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def seed(workload: str, model: str, rep: int, index: int) -> int:
    """Stable per-request seed derived from the cell coordinates (never random).

    The value stays inside the signed 63-bit range an API's seed field can
    decode: a 64-bit unsigned derivation would hand every other request a
    number the endpoint may reject outright.
    """
    material = f"{workload}|{model}|{rep}|{index}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % (2**63)
