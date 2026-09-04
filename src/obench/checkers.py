"""The real T1/T2 checkers (methodology v1 §5): binary pass/fail, no LLM-judge.

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
  * concurrency (the calibration fixture fired k-wide): the contracted single
    word and the token band over the siblings that DID report — a request the
    endpoint rejected mid-cell is the phenomenon under test and never voids
    the verdicts of the responses that landed;
  * throughput: the complete, untruncated structure — the ordered 1..150 list
    (digits in surrounding prose don't break it) and the final DONE word.

T2 (structural suites, same rules):

  * long_context / ratio_in: every datum the register's task block asks for
    appears in a sentence that also names its unit — a right value attached to
    the wrong unit's label fails (the binding lives in the reply's own text);
  * multi_turn: the access code the FINAL turn's question asks about appears
    in the reply (the transcript accumulated it turns earlier);
  * tool_calling: the emitted tool-call sequence matches the scenario's
    declared order exactly, and every call's arguments validate against that
    tool's JSON schema (required fields, types, enums, numeric bounds);
  * long_generation: the complete untruncated structure — sections 1..25 in
    order, exactly 20 items each, finished by the contracted tail line;
  * reasoning: the derived bay access number (fixture-parseable) appears in
    the reply, and the prescribed ANSWER marker is present;
  * ratio_out: the complete 10-note structure plus the contracted final word.

T3 (agent loop over the synthetic mini-repos, same rules):

  * multi_file / debugging / refactoring: the working copy's REAL pytest suite,
    run in the sandbox after the loop — hard timeout, no network, isolated cwd,
    protected files (the suite and its conftest) restored from the fixture
    first. The verdict is the exit code; whatever the model claimed on its
    `finish` action never reaches it. A task whose loop died mid-way is still
    graded on the repo state it left behind (a fix that already landed passes);
    a task with no accepted exchange at all has no outcome to grade -> `null`.

An unknown workload raises CheckersError: a level must never run with a silent
placeholder checker (that placeholder is exactly what this replaces).
"""

from __future__ import annotations

import pathlib
import re
import statistics

from . import fixtures, fixtures_t2, fixtures_t3, sandbox

# The calibration reproducibility band: |reported - median| <= 2 % of the median.
CALIBRATION_BAND = 0.02

_THROUGHPUT_SEQUENCE = list(range(1, 151))

# A match is invalid when a negation hugs it — the few tokens right before it
# ("is NOT Paris") or right after its last token ("Paris is NOT the capital").
# An enumeration of wrong candidates ending in the right number is a
# documented residual ambiguity, not worth NLU.
_NEGATIONS = {"not", "no", "never"}
_NEGATION_WINDOW = 3


class CheckersError(Exception):
    """A checker could not be applied (fixture drift or an unimplemented workload)."""


def _tokens(texto: str) -> list[str]:
    """Normalized word tokens: casefolded, punctuation stripped, unicode kept
    (Português stays one token instead of mangling into `portugu` + `s`)."""
    return [t for t in re.split(r"[^\w]+", texto.casefold()) if t]


def _match_end(secuencia: list[str], patron: list[str], inicio: int) -> int | None:
    """Index of the LAST token of the first in-order match starting at `inicio`."""
    fin = inicio
    for esperado in patron[1:]:
        try:
            fin += 1 + secuencia[fin + 1 :].index(esperado)
        except ValueError:
            return None
    return fin


def _negated_around(secuencia: list[str], inicio: int, fin: int) -> bool:
    """A negation hugging the match — just before it or right after it — flips it.

    Both sides: "is NOT Paris" fails, and so does "Paris is not the capital".
    A negation further out ("Paris, not London, is the capital") stays a
    documented residual ambiguity, like the listed-wrong-answers enumeration.
    """
    antes = secuencia[max(0, inicio - _NEGATION_WINDOW) : inicio]
    despues = secuencia[fin + 1 : fin + 1 + _NEGATION_WINDOW]
    return any(t in _NEGATIONS for t in antes) or any(t in _NEGATIONS for t in despues)


def _match_clean(secuencia: list[str], patron: list[str]) -> bool:
    """Whether `patron` appears in order somewhere, un-negated at some match."""
    for i, token in enumerate(secuencia):
        if token != patron[0]:
            continue
        fin = _match_end(secuencia, patron, i)
        if fin is not None and not _negated_around(secuencia, i, fin):
            return True
    return False


def _judge_qa_short(prompt: str, rec: dict, _registros: list[dict]) -> bool:
    aceptadas = fixtures.QA_SHORT_ANSWERS[fixtures.question_of(prompt)]
    tokens = _tokens(rec["content"])
    return any(_match_clean(tokens, _tokens(aceptada)) for aceptada in aceptadas)


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
        # The 2 % band is reproducibility EVIDENCE: with any sibling missing its
        # token report (truncated, failed), the survivor's median is itself and
        # the band is a tautology — the cell has no reproducibility to show.
        medianas[done_campo] = (
            statistics.median(valores) if len(valores) == len(registros) else None
        )
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


def _judge_concurrency(_prompt: str, rec: dict, registros: list[dict]) -> bool:
    """The concurrency cell's contract: the exact word, plus the token band over
    the siblings that DID report.

    Unlike `calibration` (whose n=3 exists to prove reproducibility, so a
    missing sibling voids the cell), a concurrency cell exists to measure k —
    a request the endpoint rejected mid-cell is the phenomenon under test, and
    it must not void the verdicts of the responses that did land. The band is
    computed over the reporting siblings; a lone survivor has no band evidence
    and grades on the word alone.
    """
    if _tokens(rec["content"]) != ["ok"]:
        return False
    for done_campo in ("prompt_eval_count", "eval_count"):
        valor = rec["done"].get(done_campo) if rec["done"] else None
        if not isinstance(valor, int) or valor <= 0:
            return False
        hermanos = [
            r["done"][done_campo]
            for r in registros
            if r is not rec
            and r["done"]
            and isinstance(r["done"].get(done_campo), int)
            and r["done"][done_campo] > 0
        ]
        if len(hermanos) >= 2:
            mediana = statistics.median(hermanos)
            if abs(valor - mediana) > CALIBRATION_BAND * mediana:
                return False
    return True


def _integers_bounded(contenido: str, digits: int) -> list[int]:
    """The reply's digit runs as ints; runs longer than `digits` are structure
    violations (a degenerate blob cannot carry the contracted list) — never a
    ValueError from Python's int-parsing cap."""
    return [int(x) for x in re.findall(rf"\d{{1,{digits}}}", contenido)]


def _judge_throughput(_prompt: str, rec: dict, _registros: list[dict]) -> bool:
    contenido = rec["content"]
    enteros = _integers_bounded(contenido, digits=4)
    # The ordered list must appear complete and contiguous; digits in surrounding
    # prose ("Here are the numbers from 1 to 150:") do not break the structure.
    n = len(_THROUGHPUT_SEQUENCE)
    if not any(enteros[i : i + n] == _THROUGHPUT_SEQUENCE for i in range(len(enteros) - n + 1)):
        return False
    tokens = _tokens(contenido)
    return bool(tokens) and tokens[-1] == "done"


def _match_anywhere(tokens: list[str], patron: list[str]) -> bool:
    """Whether `patron` appears in order anywhere, un-negated at some match."""
    return _match_clean(tokens, patron)


def _datum_presente(prompt: str, label: str, campo: str, rec: dict) -> bool:
    """Whether the reply attaches the labeled datum to ITS unit.

    The value must appear in a sentence that also names the unit: a reply that
    carries every right value but attached to the wrong units fails.
    """
    datums = fixtures_t2.register_datums(prompt)  # ValueError -> CheckersError (judge wraps)
    if label not in datums:
        raise CheckersError(f"register task asks about unknown unit [R-{label}]")
    esperado = datums[label][campo]
    contenido = rec["content"].casefold()
    etiqueta = f"r-{label}"
    for oracion in re.split(r"[.\n]", contenido):
        if etiqueta in oracion and _match_clean(_tokens(oracion), _tokens(esperado)):
            return True
    return False


def _judge_register(prompt: str, rec: dict, _registros: list[dict]) -> bool:
    """long_context / ratio_in: every asked datum present, anchored to the prompt."""
    for label, campo in fixtures_t2.register_asks(prompt):
        if not _datum_presente(prompt, label, campo, rec):
            return False
    return True


def _judge_multi_turn(prompt: str, rec: dict, _registros: list[dict]) -> bool:
    esperado = fixtures_t2.multi_turn_expected(prompt)  # the code the FINAL turn asks about
    return _match_anywhere(_tokens(rec["content"]), _tokens(esperado))


def _valor_del_tipo(valor, tipo: str) -> bool:
    """Type conformance for the scalar JSON types (bool is never a number)."""
    if tipo == "string":
        return isinstance(valor, str)
    if tipo == "boolean":
        return isinstance(valor, bool)
    if tipo == "integer":
        return isinstance(valor, int) and not isinstance(valor, bool)
    if tipo == "number":
        return isinstance(valor, (int, float)) and not isinstance(valor, bool)
    return False


def _args_validos(valor, esquema) -> bool:
    """JSON-Schema subset: type, enum, required, properties, items, numeric bounds.

    Extra (undeclared) keys are tolerated; a required key that is missing or
    null is a violation; a declared-but-unknown type is treated as drift.
    """
    if not isinstance(esquema, dict) or not esquema:
        return True  # unconstrained
    tipo = esquema.get("type")
    if "enum" in esquema:
        if tipo is not None and not _valor_del_tipo(valor, tipo):
            return False  # an enum member of the wrong Python type is a violation too
        return valor in esquema["enum"]
    if tipo == "object":
        if not isinstance(valor, dict):
            return False
        for requerido in esquema.get("required", ()):
            if requerido not in valor or valor[requerido] is None:
                return False
        for clave, sub in (esquema.get("properties") or {}).items():
            if clave in valor and not _args_validos(valor[clave], sub):
                return False
        return True
    if tipo == "array":
        if not isinstance(valor, list):
            return False
        return all(_args_validos(item, esquema.get("items") or {}) for item in valor)
    if tipo in ("string", "boolean", "integer", "number"):
        if not _valor_del_tipo(valor, tipo):
            return False
        if "minimum" in esquema and valor < esquema["minimum"]:
            return False
        return not ("maximum" in esquema and valor > esquema["maximum"])
    return tipo is None  # no declared type: the value is unconstrained


def _judge_tool_calling(prompt: str, rec: dict, _registros: list[dict]) -> bool:
    escenario = fixtures_t2.tool_scenario(prompt)  # ValueError -> CheckersError (judge)
    esperados = list(escenario["sequence"])
    llamadas = rec.get("tool_calls") or []
    nombres = [str(((tc or {}).get("function") or {}).get("name")) for tc in llamadas]
    if nombres != esperados:
        return False  # wrong order, a missing call, an extra call, or prose instead
    esquemas = {t["function"]["name"]: t["function"]["parameters"] for t in escenario["tools"]}
    for tc in llamadas:
        funcion = (tc or {}).get("function") or {}
        if not isinstance(funcion.get("arguments"), dict):
            return False  # Ollama parses arguments into an object; a string is malformed
        if not _args_validos(funcion["arguments"], esquemas[funcion["name"]]):
            return False
    return True


def _judge_long_generation(prompt: str, rec: dict, _registros: list[dict]) -> bool:
    """The complete structure: sections 1..N in order, K items each, contracted tail."""
    contenido = rec["content"].casefold()
    seccion = 0
    items = 0
    # Section/item numbers never exceed two digits: a longer digit run is prose
    # noise (and would overflow Python's int parsing at ~4300 digits).
    for m in re.finditer(r"section (\d{1,3}):|item (\d{1,3}):", contenido):
        if m.group(1) is not None:
            if seccion and items != fixtures_t2.LONG_GENERATION_ITEMS:
                return False  # the previous section was incomplete
            if int(m.group(1)) != seccion + 1:
                return False
            seccion, items = int(m.group(1)), 0
        else:
            if seccion == 0:
                return False  # items before the first section header
            items += 1
            if int(m.group(2)) != items or items > fixtures_t2.LONG_GENERATION_ITEMS:
                return False
    if (
        seccion != fixtures_t2.LONG_GENERATION_SECTIONS
        or items != fixtures_t2.LONG_GENERATION_ITEMS
    ):
        return False
    tokens = _tokens(contenido)
    return bool(tokens) and list(tokens[-3:]) == list(fixtures_t2.LONG_GENERATION_TAIL)


def _judge_reasoning(prompt: str, rec: dict, _registros: list[dict]) -> bool:
    esperado = fixtures_t2.reasoning_expected(prompt)  # derived from the prompt's own rules
    tokens = _tokens(rec["content"])
    return "answer" in tokens and _match_anywhere(tokens, [str(esperado)])


def _judge_ratio_out(_prompt: str, rec: dict, _registros: list[dict]) -> bool:
    contenido = rec["content"].casefold()
    notas = [int(x) for x in re.findall(r"note (\d{1,3})", contenido)]  # 1..10 only
    n = fixtures_t2.RATIO_OUT_NOTES
    if not any(notas[i : i + n] == list(range(1, n + 1)) for i in range(len(notas) - n + 1)):
        return False
    tokens = _tokens(contenido)
    return bool(tokens) and tokens[-1] == fixtures_t2.RATIO_OUT_TAIL


def _make_sandbox_judge(workload: str):
    """The T3 judge for one workload: the sandbox's pytest run is the verdict.

    The graded copy is rebuilt from the fixture (the suite and pytest's config
    are never the model's), so a task only passes when the fixture's own tests
    pass on the delivered source. A sandbox that never reached pytest is a
    harness misconfiguration, not a model outcome: it aborts the batch loudly
    with the billed evidence kept as null-verdict lines.
    """

    def _juez(_prompt: str, rec: dict, _registros: list[dict]) -> bool:
        resultado = sandbox.run_checker(
            pathlib.Path(rec["repo_dir"]), fixtures_t3.repo_files(workload)
        )
        if not resultado["sandbox_ok"]:
            raise CheckersError(
                f"the sandbox never ran pytest (exit {resultado['returncode']}): "
                f"{resultado['tail'][-200:]}"
            )
        rec["sandbox"] = resultado  # the checker's raw evidence, kept on the line
        return resultado["returncode"] == 0 and not resultado["timed_out"]

    return _juez


def _judge_cache_prefix(_prompt: str, rec: dict, _registros: list[dict]) -> bool:
    """The cache calibration replay's contract: the exact contracted word.

    The token report is the calibration's SIGNAL here, never a reproducibility
    band: a warm replay legitimately reports fewer freshly-evaluated tokens
    than a cold one, so the calibration band would grade the very phenomenon
    under measurement as drift.
    """
    return _tokens(rec["content"]) == ["ok"]


_JUDGES = {
    "qa_short": _judge_qa_short,
    "calibration": _judge_calibration,
    "concurrency": _judge_concurrency,  # the calibration fixture, the cell's own band rule
    "throughput": _judge_throughput,
    "long_context": _judge_register,
    "long_generation": _judge_long_generation,
    "multi_turn": _judge_multi_turn,
    "tool_calling": _judge_tool_calling,
    "reasoning": _judge_reasoning,
    "ratio_in": _judge_register,
    "ratio_out": _judge_ratio_out,
    "cache_cold": _judge_cache_prefix,  # the calibration replay's three phases:
    "cache_intra": _judge_cache_prefix,  # the same prefix and the same word-only
    "cache_spaced": _judge_cache_prefix,  # contract (the token report is the signal)
    "multi_file": _make_sandbox_judge("multi_file"),
    "debugging": _make_sandbox_judge("debugging"),
    "refactoring": _make_sandbox_judge("refactoring"),
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
        if workload in fixtures_t3.WORKLOADS:
            # A T3 task is graded on the working copy the loop leaves behind:
            # any accepted exchange (a step with HTTP 200) means there is a repo
            # state to grade, even when the loop died mid-way. No accepted
            # exchange at all means the model never engaged -> null, like T1/T2.
            if not any(p.get("http") == 200 for p in rec.get("steps") or ()):
                veredictos.append(None)
                continue
        elif rec["done"] is None:
            # No completed response: a transport/HTTP failure stays null (the request
            # is a failed attempt, not a graded outcome); a truncated 200 fails.
            veredictos.append(None if not rec["content"] and rec["http"] != 200 else "fail")
            continue
        try:
            veredicto = juez(texto, rec, registros)
        except CheckersError:
            raise
        except Exception as e:  # noqa: BLE001 - fixture drift keeps the billed evidence
            # A judge that cannot grade (unknown prompt shape, drift between the
            # fixture table and the data) is a harness bug, never a model verdict:
            # the runner turns any CheckersError into null verdicts + an aborted
            # batch, so the billed requests stay in the dataset.
            raise CheckersError(f"checker for {workload!r}: {type(e).__name__}: {e}") from None
        veredictos.append("pass" if veredicto else "fail")
    return veredictos
