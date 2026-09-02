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

import pytest

from test_dry_run import run_cli, with_pricing

from ocharness import fixtures, fixtures_t2
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
    # The hybrid composition (methodology v1.1 §5): 24 per-cell brackets (the
    # strong four x 6 models) + 6 pooled (the weak trio, one per model).
    per_celda = [b for b in batches if b["workload"] is not None]
    agrupados = [b for b in batches if b["workload"] is None]
    assert len(per_celda) == 24 and len(agrupados) == 6 and len(requests) == 6 * 38
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
    # Seed-regenerable fixtures, unchanged by the composition: the per-cell
    # brackets hash their workload's one-run fixture; the pooled brackets hash
    # the pool's one-rep sequence (the trio's fixtures, in pool order).
    peticiones_de = {w.name: w.requests for w in T2_WORKLOADS}
    for b in batches:
        validate_batch_line(b)
        if b["workload"] is not None:
            esperado = fixture_hash(build("T2", b["workload"], peticiones_de[b["workload"]]))
        else:
            esperado = fixture_hash(
                tuple(s for w in b["pool"]["workloads"] for s in build("T2", w, peticiones_de[w]))
            )
        assert b["fixture_hash"] == esperado, b["batch_id"]
    for w in T2_NAMES:
        hashes = {r["fixture_hash"] for r in requests if r["workload"] == w}
        assert hashes == {fixture_hash(build("T2", w, peticiones_de[w]))}
    # No warmup, no retry: the fake saw exactly the planned chats and meter reads.
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    reads = [c for c in fake_cli.calls if c["path"] == "/api/usage"]
    # No warmup, no retry: exactly the planned chats (plus the canary's 10 that
    # open the run) and meter reads (per bracket: pre + count check + 2 polls).
    assert len(chats) == 6 * 38 + 10 and len(reads) == 30 * 4 + 7
    assert all(c["auth"] for c in chats + reads)


def test_t2_default_reps_build_the_full_grid(tmp_path, fake_cli):
    """The default n=5 run: 5 brackets for one model — 4 per-cell (each pooling
    the cell's 5 reps) + 1 pooled — with the reps living in the request rows."""
    pricing = with_pricing(tmp_path)
    assert run_cli(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing)[0] == 0
    assert run_t2(tmp_path, "--model", "glm-5.3-flash")[0] == 0
    batches = read_batches(tmp_path)
    per_celda = [b for b in batches if b["workload"] is not None]
    agrupados = [b for b in batches if b["workload"] is None]
    assert len(per_celda) == 4 and len(agrupados) == 1
    assert len({b["batch_id"] for b in batches}) == 5  # deterministic and unique
    for b in per_celda:
        assert b["reps"] == 5 and b["pool"] is None
    assert agrupados[0]["reps"] == 5
    assert agrupados[0]["pool"] == {
        "workloads": ["multi_turn", "tool_calling", "reasoning"],
        "reps": 5,
    }
    requests = read_requests(tmp_path)
    assert len(requests) == 38 * 5  # 23 per-cell + 15 pooled, x 5 reps
    for w in T2_NAMES:
        reps = {r["rep"] for r in requests if r["workload"] == w}
        assert reps == {1, 2, 3, 4, 5}


def test_t2_full_slate_plans_24_per_cell_plus_6_pooled_at_n5(tmp_path, fake_cli):
    """Ticket #39's shape: the hybrid composition plans 30 brackets at n=5 —
    the strong four per-cell (each bracket pooling its cell's 5 reps) and the
    weak trio pooled per model — with the request count growing, never the
    bracket count."""
    pricing = with_pricing(tmp_path)
    assert run_cli(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing)[0] == 0
    assert run_t2(tmp_path)[0] == 0
    batches = read_batches(tmp_path)
    per_celda = [b for b in batches if b["workload"] is not None]
    agrupados = [b for b in batches if b["workload"] is None]
    assert len(per_celda) == 24 and len(agrupados) == 6
    peticiones_de = {w.name: w.requests for w in T2_WORKLOADS}
    for b in per_celda:
        assert b["reps"] == 5
        assert b["n"] == peticiones_de[b["workload"]] * 5
    for b in agrupados:
        assert b["workload"] is None
        assert b["pool"]["reps"] == 5
        assert b["n"] == sum(peticiones_de[w] for w in b["pool"]["workloads"]) * 5
    requests = read_requests(tmp_path)
    assert len(requests) == 6 * 38 * 5  # the reps live in the request rows
    assert len({b["batch_id"] for b in batches}) == 30


def test_pooled_requests_carry_their_own_workload_evidence(tmp_path, fake_cli):
    """Inside a pooled bracket every request line names its own workload and
    rep, and its nonce derives from the documented cell coordinates — the same
    derivation a per-rep bracket of that workload would produce (pooling never
    re-rolls a prompt)."""
    fake_cli.reply_for = correct_transcript
    fake_cli.tool_calls_for = declared_calls
    prepare(tmp_path)
    assert run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    from ocharness import lane

    batches = read_batches(tmp_path)
    agrupados = [b for b in batches if b["workload"] is None]
    assert len(agrupados) == 1
    pool_id = agrupados[0]["batch_id"]
    lineas = [r for r in read_requests(tmp_path) if r["batch_id"] == pool_id]
    assert {r["workload"] for r in lineas} == {"multi_turn", "tool_calling", "reasoning"}
    vistos: dict[tuple[str, int], int] = {}
    por_workload: dict[str, set] = {}
    for r in lineas:
        por_workload.setdefault(r["workload"], set()).add(r["rep"])
    assert por_workload == {"multi_turn": {1}, "tool_calling": {1}, "reasoning": {1}}
    for r in lineas:
        # The lines are written in send order, so each request's position within
        # its (workload, rep) unit is the index the coordinates key on.
        clave = (r["workload"], r["rep"])
        indice = vistos.get(clave, 0)
        vistos[clave] = indice + 1
        palabras = lane.nonce_words(lane.expected_tin("T2", r["workload"]))
        nonce = lane.nonce_text(
            lane.nonce_seed(agrupados[0]["run_id"]),
            lane.nonce_index("T2", r["workload"], "glm-5.3-flash", r["rep"], 1, indice),
            palabras,
        )
        assert r["nonce_sha256"] == lane.nonce_sha256(nonce), r["req_id"]
        # The seed is the same derivation the per-rep brackets use.
        assert r["seed"] == fixtures.seed(r["workload"], "glm-5.3-flash", r["rep"], indice)


def test_fixture_hashes_are_composition_independent(tmp_path, fake_cli):
    """Pool the reps into one bracket and the fixture hash does not move: the
    hash pins the workload's one-run fixture, never the bracket's composition."""
    fake_cli.reply_for = correct_transcript
    fake_cli.tool_calls_for = declared_calls
    pricing = with_pricing(tmp_path)
    hashes: dict[str, set] = {}
    for reps in ("1", "3"):
        base = tmp_path / f"reps{reps}"
        assert (
            run_cli(
                base,
                "dry-run",
                "--level",
                "T2",
                "--reps",
                reps,
                "--pricing-dir",
                pricing,
                "--json",
            )[0]
            == 0
        )
        assert (
            run_cli(
                base,
                "run",
                "--level",
                "T2",
                "--reps",
                reps,
                "--model",
                "glm-5.3-flash",
                "--pricing-dir",
                pricing,
                "--settle-s",
                "2",
                "--settle-poll-s",
                "0.01",
            )[0]
            == 0
        )
        files = list((base / "batches").glob("batches-*.jsonl"))
        for l in files[0].read_text(encoding="utf-8").splitlines():
            b = json.loads(l)
            if b["workload"] is not None:
                hashes.setdefault(b["workload"], set()).add(b["fixture_hash"])
    peticiones_de = {w.name: w.requests for w in T2_WORKLOADS}
    for w, valores in hashes.items():
        assert valores == {fixture_hash(build("T2", w, peticiones_de[w]))}


def test_tool_calling_grades_the_call_sequence_and_the_arguments(tmp_path, fake_cli):
    """TR-1 stays valid (control); the other scenarios each violate one rule."""
    fake_cli.reply_for = correct_transcript
    fake_cli.tool_calls_for = mutated_calls
    prepare(tmp_path)
    assert run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    lineas = [r for r in read_requests(tmp_path) if r["workload"] == "tool_calling"]
    assert len(lineas) == 6
    # tool_calling now lives in the pooled bracket: its requests appear in send
    # order, which within the rep's unit IS the fixture order.
    scenarios = fixtures_t2.specs("tool_calling", 6)  # prompt i == request index i
    for i, (prompt, _tools) in enumerate(scenarios):
        linea = lineas[i]
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


def test_pooled_batch_lines_validate_their_pool_shape():
    """The dataset contract (ticket #39): pooled lines carry workload null +
    pool {workloads, reps}; a bracket without a workload must name its pool;
    the counts are positive ints."""
    from ocharness.schema import SchemaError, validate_batch_line

    def lote(**cambios):
        base = {
            "batch_id": "b" * 16,
            "run_id": "run",
            "level": "T2",
            "workload": None,
            "model": "glm-5.3-flash",
            "fixture_hash": "f" * 64,
            "k": 1,
            "n": 15,
            "reps": 2,
            "pool": {"workloads": ["multi_turn", "tool_calling", "reasoning"], "reps": 2},
            "settle_s": 60.0,
            "settle_mode": "registration",
            "settle_reads": 2,
            "registered_session_s": 0.0,
            "registered_weekly_s": 0.0,
            "settle_exit": "stable",
            "count_check_s": 0.5,
            "wall_clock_s": 10.0,
            "medidor_pre": {"limits": {"session": {"usage": 0.5}, "weekly": {"usage": 0.5}}},
            "medidor_post": {"limits": {"session": {"usage": 0.5}, "weekly": {"usage": 0.6}}},
            "dpp_session": 0.1,
            "dpp_weekly": 0.1,
            "request_counts": {"pre": {}, "count_check": {}, "post": {}},
            "table_version": "2026-08-31",
            "protocol_version": "3",
            "notes": "",
        }
        base.update(cambios)
        return base

    validate_batch_line(lote())  # the pooled shape is valid
    validate_batch_line(lote(workload="qa_short", pool=None, reps=1))  # per-cell is too

    mala = lote(workload="multi_turn")  # a pooled bracket names no workload
    with pytest.raises(SchemaError, match="workload"):
        validate_batch_line(mala)
    with pytest.raises(SchemaError):  # a bracket without a workload must name its pool
        validate_batch_line(lote(pool=None))
    with pytest.raises(SchemaError):  # an empty pool names nothing
        validate_batch_line(lote(pool={"workloads": [], "reps": 2}))
    with pytest.raises(SchemaError):  # the pool's reps are positive
        validate_batch_line(lote(pool={"workloads": ["multi_turn"], "reps": 0}))
    with pytest.raises(SchemaError):  # so are the bracket's
        validate_batch_line(lote(reps=0))


def test_t2_refuses_rep_narrowing_and_reps_drift(tmp_path, fake_cli):
    """The hybrid composition pools every rep of a cell into one bracket, so
    per-rep narrowing has no bracket to run, and a resume at another --reps
    would collide with the pooled brackets' batch ids (they anchor on the first
    rep alone). Both refuse loudly, before any request."""
    pricing = prepare(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T2", "--reps", "5", "--pricing-dir", pricing)[0]
        == 0
    )
    code, _out, err = run_t2(tmp_path, "--model", "glm-5.3-flash", "--rep", "2", "--reps", "5")
    assert code == 2
    assert "--rep" in err  # refused before any request: no per-rep bracket exists
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T2", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    assert run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T2", "--reps", "3", "--pricing-dir", pricing)[0]
        == 0
    )
    code, _out, err = run_t2(tmp_path, "--model", "glm-5.3-flash", "--reps", "3")
    assert code == 1
    assert "--reps" in err  # the manifest binds the density the pool was planned at


def test_t2_resume_at_another_reps_is_refused(tmp_path, fake_cli):
    """The drift guard lives where the collision lives: the hybrid composition's
    pooled brackets anchor their batch ids on the first rep alone, so a resume
    at another density would read the earlier brackets done or collide — the
    guard refuses it. The per-rep levels (T1/T3) take the wide resume."""
    pricing = prepare(tmp_path)
    assert run_t2(tmp_path, "--reps", "1")[0] == 0
    # the drift guard runs on any existing manifest, completed or not; the gate
    # still wants its fresh dry-run approving the wider density first
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T2", "--reps", "2", "--pricing-dir", pricing)[0]
        == 0
    )
    code, out, err = run_t2(tmp_path, "--reps", "2")
    # the drift guard fires after the gate (the dry-run mark is consumed): a
    # RunnerError refusal exits 1 with the error line on stderr
    assert code == 1, f"expected the drift refusal, got {code}: {out or err}"
    assert "planned at --reps 1" in err and "--reps 2" in err
