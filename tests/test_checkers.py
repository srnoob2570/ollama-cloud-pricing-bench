"""The three real T1 checkers against scripted fake transcripts: ticket Harness 03.

`checker` stops being a placeholder: every request line carries pass/fail (or null
when the request never produced a response to judge), computed from the response
against the fixture's contract. Asserted at the only seam: the CLI vs the fake,
on the produced JSONL.
"""

from __future__ import annotations

import json

from test_dry_run import run_cli, with_pricing

from ocharness.fixtures import CALIBRATION_PROMPT, QA_SHORT_ANSWERS, THROUGHPUT_PROMPT, seed

PRIMERA_PREGUNTA = "What is the capital of France?"


def prepare(tmp_path) -> str:
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    return pricing


def run_one_model(tmp_path, *extra) -> tuple[int, str, str]:
    return run_cli(
        tmp_path, "run", "--level", "T1", "--model", "glm-5.3-flash", "--reps", "1", *extra
    )


def read_requests(tmp_path) -> list[dict]:
    files = sorted((tmp_path / "runs").glob("requests-*.jsonl"))
    assert len(files) == 1
    return [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]


def qa_reply(prompt: str) -> str:
    """A one-sentence reply that contains the question's expected answer."""
    respuesta = next(ans[0] for q, ans in QA_SHORT_ANSWERS.items() if prompt.startswith(q))
    return f"The answer is {respuesta}."


def correct_transcript(prompt: str) -> str:
    """Every workload answers exactly as its fixture asks."""
    if prompt == CALIBRATION_PROMPT:
        return "OK"
    if prompt == THROUGHPUT_PROMPT:
        return ", ".join(str(i) for i in range(1, 151)) + "\nDONE"
    return qa_reply(prompt)


def test_scripted_correct_transcripts_pass_all_three_checkers(tmp_path, fake_cli):
    fake_cli.reply_for = correct_transcript
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path, "--settle-s", "0")
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    assert len(requests) == 24  # 20 qa_short + 3 calibration + 1 throughput
    por_workload: dict[str, set] = {}
    for r in requests:
        por_workload.setdefault(r["workload"], set()).add(r["checker"])
    assert por_workload == {"qa_short": {"pass"}, "calibration": {"pass"}, "throughput": {"pass"}}


def test_qa_short_fails_on_a_wrong_answer_and_passes_the_rest(tmp_path, fake_cli):
    fake_cli.reply_for = lambda prompt: (
        "The capital of France is Berlin."
        if prompt.startswith(PRIMERA_PREGUNTA)
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path, "--settle-s", "0")
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    fallidos = [r for r in requests if r["checker"] == "fail"]
    assert len(fallidos) == 1
    assert fallidos[0]["workload"] == "qa_short" and fallidos[0]["req_id"].endswith("-0000")
    assert all(r["checker"] == "pass" for r in requests if r is not fallidos[0])


def test_calibration_fails_when_reported_tokens_drift_beyond_2pct(tmp_path, fake_cli):
    """The 3 identical calibration requests must report reproducible tokens (2 % band)."""
    fake_cli.reply_for = lambda prompt: "OK" if prompt == CALIBRATION_PROMPT else "world"
    drift = seed("calibration", "glm-5.3-flash", 1, 1)
    fake_cli.counts_for = lambda _prompt, s: (26, 13) if s == drift else (26, 12)
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path, "--settle-s", "0")
    assert code == 0, out or err
    calibraciones = [r for r in read_requests(tmp_path) if r["workload"] == "calibration"]
    assert len(calibraciones) == 3
    veredictos = {r["tok_out"]: r["checker"] for r in calibraciones}
    assert veredictos == {12: "pass", 13: "fail"}  # 13 vs the 12 median = 8.3 % off


def test_truncated_stream_fails_the_checker_even_with_looking_correct_content(tmp_path, fake_cli):
    """Billed-but-truncated: no done frame means the outcome is never verifiable."""
    fake_cli.reply_for = correct_transcript
    fake_cli.truncate_stream = True
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path, "--settle-s", "0")
    assert code == 0, out or err  # billed: recorded, not retried
    requests = read_requests(tmp_path)
    assert requests and all(r["checker"] == "fail" for r in requests)
    assert all(r["tok_in"] is None and r["api"] is None for r in requests)


def test_throughput_fails_when_the_number_sequence_is_incomplete(tmp_path, fake_cli):
    fake_cli.reply_for = lambda prompt: (
        "1, 2, 3 and so on up to 150, DONE"
        if prompt == THROUGHPUT_PROMPT
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path, "--settle-s", "0")
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    throughput = [r for r in requests if r["workload"] == "throughput"]
    assert throughput[0]["checker"] == "fail"
    assert all(r["checker"] == "pass" for r in requests if r["workload"] != "throughput")


def test_errored_request_carries_no_verdict(tmp_path, fake_cli):
    """A request that never produced a response has no outcome to check: null, not fail."""
    fake_cli.fails_on = 3  # request 3 of the first batch gets a scripted 500
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path, "--settle-s", "0")
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    fallidas = [r for r in requests if r["http"] != 200]
    assert len(fallidas) == 1
    assert fallidas[0]["checker"] is None and fallidas[0]["err"]
    assert sum(1 for r in requests if r["checker"] is None) == 1
