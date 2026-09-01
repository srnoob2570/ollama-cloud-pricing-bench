"""The real T1 checkers (methodology v1 §5): binary pass/fail, no LLM-judge.

Every request line carries a `checker` verdict instead of a placeholder:

- a request that never produced a response (HTTP error, transport exception)
  has no outcome to grade -> `null` (`err`/`http` already record why);
- a billed-but-truncated stream (200 that ends without a done frame) is never
  verifiable -> `fail`;
- otherwise the workload's contract decides:

  * qa_short: the answer key — the raw response equals an accepted answer, or
    the answer's normalized tokens appear in the response;
  * calibration: the contracted single word, and the reported tokens within 2 %
    of the cell's median — three identical requests whose token reports
    disagree beyond the band are a polluted calibration point, not a
    measurement;
  * throughput: the complete, untruncated structure — 1..150 in order and the
    final DONE word.

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


class CheckersError(Exception):
    """A checker could not be applied (fixture drift or an unimplemented workload)."""


def _tokens(texto: str) -> list[str]:
    """Normalized word tokens: casefolded, punctuation stripped."""
    return [t for t in re.split(r"[^0-9a-z]+", texto.casefold()) if t]


def _contains(secuencia: list[str], patron: list[str]) -> bool:
    """Whether `patron` appears in `secuencia` as a contiguous token run."""
    n = len(patron)
    return any(secuencia[i : i + n] == patron for i in range(len(secuencia) - n + 1))


def _judge_qa_short(prompt: str, rec: dict, _registros: list[dict]) -> bool:
    contenido = rec["content"]
    try:
        aceptadas = fixtures.QA_SHORT_ANSWERS[fixtures.question_of(prompt)]
    except ValueError as e:
        raise CheckersError(str(e)) from None
    if contenido.strip() in aceptadas:  # the exact answer, verbatim
        return True
    tokens = _tokens(contenido)
    return any(_contains(tokens, _tokens(a)) for a in aceptadas)


def _judge_calibration(_prompt: str, rec: dict, registros: list[dict]) -> bool:
    if _tokens(rec["content"]) != ["ok"]:  # the fixture's exact-word contract
        return False
    medianas = {}
    for campo, done_campo in (("tok_in", "prompt_eval_count"), ("tok_out", "eval_count")):
        valores = [
            r["done"][done_campo]
            for r in registros
            if r["done"] and isinstance(r["done"].get(done_campo), int)
        ]
        medianas[done_campo] = statistics.median(valores) if valores else None
    done = rec["done"]
    for campo in ("prompt_eval_count", "eval_count"):
        valor = done.get(campo)
        mediana = medianas[campo]
        if not isinstance(valor, int) or mediana is None:
            return False
        if abs(valor - mediana) > CALIBRATION_BAND * mediana:
            return False
    return True


def _judge_throughput(_prompt: str, rec: dict, _registros: list[dict]) -> bool:
    contenido = rec["content"]
    enteros = [int(x) for x in re.findall(r"\d+", contenido)]
    if enteros != _THROUGHPUT_SEQUENCE:
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
