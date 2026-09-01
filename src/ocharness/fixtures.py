"""Deterministic T1 fixtures: seeded prompts, hashed (methodology v1 §5).

The fixture is the prompt sequence itself — identical across models and
repetitions so every cell serves the same load, and hash-stamped on every raw
line. qa_short carries the answer key its checker grades against; the other two
contracts (the single calibration word, the throughput list) live in the prompts
themselves. English, synthetic, version-in-repo.
"""

from __future__ import annotations

import hashlib

# 20 short Q&A prompts (qa_short fires exactly one per request, in order).
QA_SHORT_QUESTIONS: tuple[str, ...] = (
    "What is the capital of France?",
    "How many days are there in a leap year?",
    "What is 7 times 8?",
    "Name the largest ocean on Earth.",
    "What is the official language of Brazil?",
    "Who wrote Romeo and Juliet?",
    "What is the boiling point of water in Celsius?",
    "How many continents are there?",
    "What is the chemical symbol for gold?",
    "Which planet is closest to the Sun?",
    "What is the square root of 144?",
    "Name the longest river in the world.",
    "What color results from mixing blue and yellow paint?",
    "How many minutes are in an hour?",
    "What is the tallest mountain above sea level?",
    "How many sides does a hexagon have?",
    "What is the freezing point of water in Fahrenheit?",
    "Which currency is used in Japan?",
    "What is the largest desert in the world?",
    "How many strings does a standard guitar have?",
)

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

# 20 short Q&A prompts (qa_short fires exactly one per request, in order).
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
        return [f"{q}{QA_SHORT_SUFFIX}" for q in QA_SHORT_QUESTIONS[:n]]
    if workload == "calibration":
        return [CALIBRATION_PROMPT] * n
    if workload == "throughput":
        return [THROUGHPUT_PROMPT] * n
    raise ValueError(f"unknown T1 workload: {workload!r}")


def prompts(level: str, workload: str, n: int) -> list[str]:
    """The `n` deterministic prompts of one batch (workload, single model)."""
    if level != "T1":
        raise ValueError(f"fixture generators for {level!r} arrive with a later Harness ticket")
    return _t1_prompts(workload, n)


def fixture_hash(textos: list[str]) -> str:
    """sha256 over the batch's exact prompt sequence (the fixture is the bytes)."""
    return hashlib.sha256("\n".join(textos).encode("utf-8")).hexdigest()


def seed(workload: str, model: str, rep: int, index: int) -> int:
    """Stable per-request seed derived from the cell coordinates (never random)."""
    material = f"{workload}|{model}|{rep}|{index}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
