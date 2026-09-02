"""The 7 structural T2 suites end-to-end against the fake: ticket Harness 04.

Seeded fixture generators (30K register, accumulated turns, tool scenarios with
declared schemas, reasoning, ~50K ratio_in, 20 generations of ~500 out) over the
6-model slate, graded by the real checkers. Asserted at the only seam — the CLI
vs the fake — on the produced JSONL and the requests the fake observed.
"""

from __future__ import annotations

import itertools
import json
import re

from test_dry_run import run_cli, with_pricing

from ocharness import fixtures_t2
from ocharness.fixtures import build, fixture_hash
from ocharness.schema import validate_batch_line, validate_request_line
from ocharness.workloads import T2 as T2_WORKLOADS

T2_NAMES = tuple(w.name for w in T2_WORKLOADS)


def prepare(tmp_path) -> str:
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T2", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    return pricing


def run_t2(tmp_path, *extra) -> tuple[int, str, str]:
    return run_cli(
        tmp_path, "run", "--level", "T2", "--settle-s", "2", "--settle-poll-s", "0.01", *extra
    )


def read_jsonl(tmp_path, dirname, pattern) -> list[dict]:
    files = sorted((tmp_path / dirname).glob(pattern))
    assert len(files) == 1, f"expected exactly one {pattern} in {dirname}/"
    return [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]


def read_requests(tmp_path) -> list[dict]:
    return read_jsonl(tmp_path, "runs", "requests-*.jsonl")


def read_batches(tmp_path) -> list[dict]:
    return read_jsonl(tmp_path, "batches", "batches-*.jsonl")


# ---- scripted model behavior ------------------------------------------------


def schema_args(parameters: dict) -> dict:
    """Arguments a tool schema accepts: first enum value, minimal numbers, any string."""
    args: dict = {}
    for name in parameters.get("required", ()):
        sub = parameters["properties"][name]
        if "enum" in sub:
            args[name] = sub["enum"][0]
        elif sub.get("type") == "string":
            args[name] = f"ok-{name}"
        elif sub.get("type") == "integer":
            args[name] = sub.get("minimum", 1)
        elif sub.get("type") == "number":
            args[name] = float(sub.get("minimum", 1))
    return args


def cuerpo(prompt: str) -> str:
    """The fixture body the test-side scripting parses: the wire prompt carries
    the lane's nonce as one line above the fixture."""
    return prompt.split("\n\n", 1)[1]


def declared_calls(prompt: str) -> list[dict] | None:
    """The scenario's expected calls, in order, with schema-valid arguments."""
    prompt = cuerpo(prompt)
    if fixtures_t2.workload_of(prompt) != "tool_calling":
        return None  # fall through to the text reply
    escenario = fixtures_t2.tool_scenario(prompt)
    llamadas = []
    for nombre in escenario["sequence"]:
        parameters = next(
            t["function"]["parameters"]
            for t in escenario["tools"]
            if t["function"]["name"] == nombre
        )
        llamadas.append({"function": {"name": nombre, "arguments": schema_args(parameters)}})
    return llamadas


def correct_transcript(prompt: str) -> str:
    """Every workload answers exactly as its fixture's contract prescribes."""
    prompt = cuerpo(prompt)
    workload = fixtures_t2.workload_of(prompt)
    if workload in ("long_context", "ratio_in"):
        datums = fixtures_t2.register_datums(prompt)
        frases = []
        for label, campo in fixtures_t2.register_asks(prompt):
            if campo == "code":
                frases.append(
                    f"The access code of the unit tagged [R-{label}] is {datums[label]['code']}."
                )
            else:
                frases.append(
                    f"The unit tagged [R-{label}] is inspected every {datums[label]['days']} days."
                )
        return " ".join(frases)
    if workload == "multi_turn":
        preguntada = re.findall(r"access code of the (\w+) line\?", prompt)[-1]
        codigo = fixtures_t2.multi_turn_expected(prompt)
        return f"The access code of the {preguntada} line is {codigo}."
    if workload == "long_generation":
        lineas = []
        for s in range(1, fixtures_t2.LONG_GENERATION_SECTIONS + 1):
            lineas.append(f"Section {s}: subsystem {s} overview")
            lineas += [
                f"item {k}: part {k} of subsystem {s}, torqued and logged" for k in range(1, 21)
            ]
        return "\n".join(lineas) + "\nEND OF GENERATION"
    if workload == "reasoning":
        esperado = fixtures_t2.reasoning_expected(prompt)
        return f"The bay access number is {esperado}.\nANSWER: {esperado}"
    if workload == "ratio_out":
        return (
            "\n".join(
                f"Note {n}: the unit stayed inside its recorded band for the whole shift."
                for n in range(1, fixtures_t2.RATIO_OUT_NOTES + 1)
            )
            + "\nENDCAP"
        )
    if workload == "tool_calling":
        return ""  # the outcome is the tool calls; scripted via tool_calls_for
    raise AssertionError(f"no scripted transcript for {workload}")


def broken_reply(prompt: str) -> str:
    """Each workload breaks in its own characteristic way (every checker fails)."""
    prompt = cuerpo(prompt)
    workload = fixtures_t2.workload_of(prompt)
    if workload == "long_context":
        datums = fixtures_t2.register_datums(prompt)
        preguntado = fixtures_t2.register_asks(prompt)[0][0]
        otro = next(c["code"] for l, c in datums.items() if l != preguntado)
        return f"The access code of the unit tagged [R-{preguntado}] is {otro}."
    if workload == "ratio_in":
        return "The access code of the unit tagged [R-1500] is QQ-0000-ZZ."
    if workload == "multi_turn":
        # A real-but-different code when the transcript offers one, else a wrong one.
        esperado = fixtures_t2.multi_turn_expected(prompt)
        otros = sorted(set(re.findall(r"[A-Z]{2}-\d{4}-[A-Z]{2}", prompt)) - {esperado})
        return f"The access code of that line is {otros[0] if otros else 'QQ-0000-ZZ'}."
    if workload == "reasoning":
        mal = fixtures_t2.reasoning_expected(prompt) + 1
        return f"The bay access number is {mal}.\nANSWER: {mal}"
    if workload == "long_generation":
        lineas = []
        for s in range(1, fixtures_t2.LONG_GENERATION_SECTIONS):  # 24 of 25 sections
            lineas.append(f"Section {s}: subsystem {s} overview")
            lineas += [f"item {k}: part {k} of subsystem {s}" for k in range(1, 21)]
        return "\n".join(lineas) + "\nEND OF GENERATION"
    if workload == "ratio_out":
        return (
            "\n".join(
                f"Note {n}: the unit stayed inside its band for the whole shift."
                for n in range(1, fixtures_t2.RATIO_OUT_NOTES)  # only 9 of 10 notes
            )
            + "\nENDCAP"
        )
    if workload == "tool_calling":
        return ""  # prose instead of calls: nothing valid to grade
    raise AssertionError(f"no broken transcript scripted for {workload}")


def mutated_calls(prompt: str) -> list[dict] | None:
    """One tool_calling violation per scenario; TR-1 stays valid as the control."""
    llamadas = declared_calls(prompt)  # declared_calls strips the lane's nonce
    if not llamadas:
        return None
    tid = fixtures_t2.tool_scenario(cuerpo(prompt))["id"]
    if tid == "TR-2":
        llamadas.reverse()  # wrong call order
    elif tid == "TR-3":
        llamadas[0]["function"]["arguments"].pop("date")  # missing required argument
    elif tid == "TR-4":
        for llamada in llamadas:
            if llamada["function"]["name"] == "weather_lookup":
                llamada["function"]["arguments"]["unit"] = "kelvin"  # undeclared enum value
    elif tid == "TR-5":
        llamadas[1]["function"]["arguments"]["affected"] = "12"  # string where a number is due
    elif tid == "TR-6":
        llamadas.append({"function": {"name": "geocode", "arguments": {"city": "x"}}})  # extra call
    return llamadas


# ---- the tests --------------------------------------------------------------


def test_full_t2_slate_produces_the_structural_dataset(tmp_path, fake_cli):
    fake_cli.reply_for = correct_transcript
    fake_cli.tool_calls_for = declared_calls
    prepare(tmp_path)
    code, out, err = run_t2(tmp_path, "--reps", "1")
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    batches = read_batches(tmp_path)
    assert len(batches) == 7 * 6 and len(requests) == 6 * 38
    por_workload: dict[str, set] = {}
    for r in requests:
        validate_request_line(r)
        por_workload.setdefault(r["workload"], set()).add(r["checker"])
    assert (
        set(por_workload)
        == set(T2_NAMES)
        == {
            "long_context",
            "long_generation",
            "multi_turn",
            "tool_calling",
            "reasoning",
            "ratio_in",
            "ratio_out",
        }
    )
    assert all(veredictos == {"pass"} for veredictos in por_workload.values())
    # Seed-regenerable fixtures: same hash across the slate, one per workload.
    for b in batches:
        validate_batch_line(b)
        assert b["fixture_hash"] == fixture_hash(build("T2", b["workload"], b["n"]))
    for w in T2_NAMES:
        hashes = {b["fixture_hash"] for b in batches if b["workload"] == w}
        assert len(hashes) == 1
    assert len({b["fixture_hash"] for b in batches}) == 7
    # No warmup, no retry: the fake saw exactly the planned chats and meter reads.
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    reads = [c for c in fake_cli.calls if c["path"] == "/api/usage"]
    # No warmup, no retry: exactly the planned chats (plus the canary's 10 that
    # open the run) and meter reads (per bracket: pre + count check + 2 polls).
    assert len(chats) == 6 * 38 + 10 and len(reads) == 42 * 4 + 7
    assert all(c["auth"] for c in chats + reads)


def test_t2_default_reps_build_the_full_grid(tmp_path, fake_cli):
    """The default n=5 run shapes the dataset 7 workloads x 6 models x 5 reps."""
    pricing = with_pricing(tmp_path)
    assert run_cli(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing)[0] == 0
    assert run_t2(tmp_path, "--model", "glm-5.3-flash")[0] == 0
    batches = read_batches(tmp_path)
    assert len(batches) == 7 * 5
    assert len({b["batch_id"] for b in batches}) == 35  # deterministic and unique per rep
    requests = read_requests(tmp_path)
    assert len(requests) == 38 * 5
    for w in T2_NAMES:
        reps = {r["rep"] for r in requests if r["workload"] == w}
        assert reps == {1, 2, 3, 4, 5}


def test_tool_calling_grades_the_call_sequence_and_the_arguments(tmp_path, fake_cli):
    """TR-1 stays valid (control); the other scenarios each violate one rule."""
    fake_cli.reply_for = correct_transcript
    fake_cli.tool_calls_for = mutated_calls
    prepare(tmp_path)
    assert run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    lineas = [r for r in read_requests(tmp_path) if r["workload"] == "tool_calling"]
    assert len(lineas) == 6
    por_indice = {r["req_id"].rsplit("-", 1)[1]: r for r in lineas}
    scenarios = fixtures_t2.specs("tool_calling", 6)  # prompt i == request index i
    cuerpo = lambda p: p.split("\n\n", 1)[1]  # noqa: E731 - the lane's nonce stripped
    for i, (prompt, _tools) in enumerate(scenarios):
        linea = por_indice[f"{i:04d}"]
        nombres = [tc["function"]["name"] for tc in linea["tool_calls"]]
        esperados = list(fixtures_t2.tool_scenario(prompt)["sequence"])
        if fixtures_t2.tool_scenario(prompt)["id"] == "TR-1":
            assert linea["checker"] == "pass" and nombres == esperados  # the control
        elif fixtures_t2.tool_scenario(prompt)["id"] == "TR-2":
            assert nombres == list(reversed(esperados)) and linea["checker"] == "fail"
        else:
            assert linea["checker"] == "fail"  # missing arg / bad enum / bad type / extra call


def test_tool_schemas_reach_the_api_verbatim(tmp_path, fake_cli):
    """The wire payload carries the scenario's tool declarations byte-for-byte."""
    fake_cli.reply_for = correct_transcript
    fake_cli.tool_calls_for = declared_calls
    prepare(tmp_path)
    assert run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    tool_chats = [c for c in fake_cli.calls if c["path"] == "/api/chat" and c["body"].get("tools")]
    assert len(tool_chats) == 6
    for c in tool_chats:
        prompt = c["body"]["messages"][0]["content"]
        escenario = fixtures_t2.tool_scenario(prompt)
        enviadas = c["body"]["tools"]
        # The FULL schemas travel on the wire — names, parameters, required —
        # not just the names (a payload that dropped the schemas must fail here).
        enviadas_json = {json.dumps(t, sort_keys=True) for t in enviadas}
        esperadas_json = {json.dumps(t, sort_keys=True) for t in escenario["tools"]}
        assert enviadas_json == esperadas_json  # verbatim, order-independent
        assert [t["function"]["name"] for t in enviadas] == list(escenario["sequence"])


def test_prose_instead_of_tool_calls_fails(tmp_path, fake_cli):
    fake_cli.reply_for = correct_transcript  # the fake answers in prose, no tool frames
    prepare(tmp_path)
    assert run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    lineas = [r for r in read_requests(tmp_path) if r["workload"] == "tool_calling"]
    assert len(lineas) == 6
    assert all(r["tool_calls"] == [] and r["checker"] == "fail" for r in lineas)


def test_multi_turn_accumulates_context_turn_by_turn(tmp_path, fake_cli):
    fake_cli.reply_for = correct_transcript
    fake_cli.counts_for = lambda prompt, seed: (max(1, len(prompt) // 4), 12)
    prepare(tmp_path)
    assert run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    turnos = [r for r in read_requests(tmp_path) if r["workload"] == "multi_turn"]
    assert len(turnos) == 8
    tok_ins = [r["tok_in"] for r in turnos]
    assert all(a < b for a, b in itertools.pairwise(tok_ins))  # each turn re-sends the log
    assert all(r["checker"] == "pass" for r in turnos)


def test_structural_checkers_fail_on_broken_output(tmp_path, fake_cli):
    """Each suite's checker rejects its characteristic broken reply."""
    fake_cli.reply_for = broken_reply  # tool prompts get prose: no tool frames to grade
    prepare(tmp_path)
    assert run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    por_workload: dict[str, set] = {}
    requests = read_requests(tmp_path)
    for r in requests:
        por_workload.setdefault(r["workload"], set()).add(r["checker"])
    assert set(por_workload) == set(T2_NAMES)
    assert all(veredictos == {"fail"} for veredictos in por_workload.values())
    assert all(r["http"] == 200 for r in requests)  # billed attempts, graded as failures


def test_register_grading_binds_values_to_their_units(tmp_path, fake_cli):
    """Right values attached to the wrong units fail: the binding is per-sentence."""
    fake_cli.reply_for = lambda prompt: (
        _wrong_unit_transcript(prompt)
        if fixtures_t2.workload_of(cuerpo(prompt)) == "long_context"
        else correct_transcript(prompt)
    )
    fake_cli.tool_calls_for = declared_calls
    prepare(tmp_path)
    assert run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    requests = read_requests(tmp_path)
    long_context = [r for r in requests if r["workload"] == "long_context"]
    assert long_context[0]["checker"] == "fail"
    # The other suites keep their correct verdicts: only the wrong binding fails.
    others = [r for r in requests if r["workload"] not in ("long_context", "tool_calling")]
    assert all(r["checker"] == "pass" for r in others)


def _wrong_unit_transcript(prompt: str) -> str:
    """Every ask answered with a right-looking value attached to the WRONG unit."""
    prompt = cuerpo(prompt)
    datums = fixtures_t2.register_datums(prompt)
    asks = fixtures_t2.register_asks(prompt)
    labels = [l for l, _ in asks]
    frases = []
    for i, (label, campo) in enumerate(asks):
        otro = labels[(i + 1) % len(labels)]  # rotate: right shape, wrong unit
        frases.append(
            f"The access code of the unit tagged [R-{label}] is {datums[otro]['code']}."
            if campo == "code"
            else f"The unit tagged [R-{label}] is inspected every {datums[otro]['days']} days."
        )
    return " ".join(frases)
