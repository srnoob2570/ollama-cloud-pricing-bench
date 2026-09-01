"""The real T1 checkers (methodology v1 §5): binary pass/fail, no LLM-judge.

Every request line carries a `checker` verdict instead of a placeholder:

- a request that never produced a response (HTTP error, transport exception)
  has no outcome to grade -> `null` (`err`/`http` already record why);
- a billed-but-truncated stream (200 that ends without a done frame) is never
  verifiable -> `fail`;
- otherwise the workload's contract decides:

  * qa_short: the answer key — an accepted answer's normalized tokens appear
    in the response in order (gaps allowed: "three hundred AND sixty-six"),
    and the match is not preceded by a negation ("is NOT Paris" grades fail;
    a listed-wrong-answers enumeration is a known, documented limit);
  * calibration: the contracted single word, and the reported tokens within 2 %
    of the cell's median — three identical requests whose token reports
    disagree beyond the band are a polluted calibration point, not a
    measurement; a zero-token report is no measurement at all;
  * throughput: the complete, untruncated structure — the ordered 1..150 list
    (digits in surrounding prose don't break it) and the final DONE word.

An unknown workload raises CheckersError: a level must never run with a silent
placeholder checker (that placeholder is exactly what this replaces).
"""

from __future__ import annotations

import re
import statistics

from . import fixtures

# The calibration reproducibility band: |reported - median| <= 2 % of the median.
CALIBRATION_BAND = 0.02

_THROUGHPUT_SEQUENCE = list(range(1, 151))

# A qa_short match is invalid when a negation sits right before it ("is not
# Paris"); an enumeration of wrong candidates ending in the right number is a
# documented residual ambiguity, not worth NLU.
_NEGATIONS = {"not", "no", "never"}
_NEGATION_WINDOW = 3


class CheckersError(Exception):
    """A checker could not be applied (fixture drift or an unimplemented workload)."""


def _tokens(texto: str) -> list[str]:
    """Normalized word tokens: casefolded, punctuation stripped, unicode kept
    (Português stays one token instead of mangling into `portugu` + `s`)."""
    return [t for t in re.split(r"[^\w]+", texto.casefold()) if t]


def _subsequence(secuencia: list[str], patron: list[str]) -> bool:
    """Whether `patron` appears in `secuencia` in order, gaps allowed."""
    resto = iter(secuencia)
    return all(token in resto for token in patron)


def _match_starts(secuencia: list[str], patron: list[str]) -> list[int]:
    """Indexes where the pattern's FIRST token begins a full in-order match."""
    return [
        i
        for i, token in enumerate(secuencia)
        if token == patron[0] and _subsequence(secuencia[i + 1 :], patron[1:])
    ]


def _negated_before(secuencia: list[str], inicio: int) -> bool:
    """A negation among the few tokens right before a match flips its meaning."""
    antes = secuencia[max(0, inicio - _NEGATION_WINDOW) : inicio]
    return any(t in _NEGATIONS for t in antes)


def _judge_qa_short(prompt: str, rec: dict, _registros: list[dict]) -> bool:
    contenido = rec["content"]
    try:
        aceptadas = fixtures.QA_SHORT_ANSWERS[fixtures.question_of(prompt)]
    except ValueError as e:
        raise CheckersError(str(e)) from None
    tokens = _tokens(contenido)
    for aceptada in aceptadas:
        patron = _tokens(aceptada)
        for inicio in _match_starts(tokens, patron):
            if not _negated_before(tokens, inicio):
                return True
    return False


def _judge_calibration(_prompt: str, rec: dict, registros: list[dict]) -> bool:
    if _tokens(rec["content"]) != ["ok"]:  # the fixture's exact-word contract
        return False
    medianas = {}
    for done_campo in ("prompt_eval_count", "eval_count"):
        valores = [
            r["done"][done_campo]
            for r in registros
            if r["done"] and isinstance(r["done"].get(done_campo), int)
        ]
        medianas[done_campo] = statistics.median(valores) if valores else None
    done = rec["done"]
    for done_campo in ("prompt_eval_count", "eval_count"):
        valor = done.get(done_campo)
        mediana = medianas[done_campo]
        # Zero (or missing) token reports grade as broken, never as the reference.
        if not isinstance(valor, int) or not mediana:
            return False
        if abs(valor - mediana) > CALIBRATION_BAND * mediana:
            return False
    return True


def _judge_throughput(_prompt: str, rec: dict, _registros: list[dict]) -> bool:
    contenido = rec["content"]
    enteros = [int(x) for x in re.findall(r"\d+", contenido)]
    # The ordered list must appear complete and contiguous; digits in surrounding
    # prose ("Here are the numbers from 1 to 150:") do not break the structure.
    n = len(_THROUGHPUT_SEQUENCE)
    if not any(enteros[i : i + n] == _THROUGHPUT_SEQUENCE for i in range(len(enteros) - n + 1)):
        return False
    tokens = _tokens(contenido)
    return bool(tokens) and tokens[-1] == "done"


_JUDGES = {
    "qa_short": _judge_qa_short,
    "calibration": _judge_calibration,
    "throughput": _judge_throughput,
}


def judge(workload: str, textos: list[str], registros: list[dict]) -> list[str | None]:
    """One verdict per request of the batch, aligned with `textos`/`registros`."""
    juez = _JUDGES.get(workload)
    if juez is None:
        raise CheckersError(f"no checker implemented for workload {workload!r}")
    if len(textos) != len(registros):
        raise CheckersError(
            f"checker for {workload!r}: {len(textos)} prompts vs {len(registros)} responses"
        )
    veredictos: list[str | None] = []
    for texto, rec in zip(textos, registros):
        if rec["done"] is None:
            # No completed response: a transport/HTTP failure stays null (the request
            # is a failed attempt, not a graded outcome); a truncated 200 fails.
            veredictos.append(None if not rec["content"] and rec["http"] != 200 else "fail")
            continue
        veredictos.append("pass" if juez(texto, rec, registros) else "fail")
    return veredictos
