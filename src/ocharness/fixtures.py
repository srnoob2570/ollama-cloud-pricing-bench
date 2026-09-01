"""Deterministic T1 fixtures: seeded prompts, hashed (methodology v1 §5).

T1 carries micro-benchmarks without checkers; the fixture is the prompt sequence
itself — identical across models and repetitions so every cell serves the same
load, and hash-stamped on every raw line. English, synthetic, version-in-repo.
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


def _t1_prompts(workload: str, n: int) -> list[str]:
    if workload == "qa_short":
        return [f"{q} Answer in one short sentence." for q in QA_SHORT_QUESTIONS[:n]]
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
