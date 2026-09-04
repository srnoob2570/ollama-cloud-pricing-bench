"""The three real T1 checkers against scripted fake transcripts: ticket Harness 03.

`checker` stops being a placeholder: every request line carries pass/fail (or null
when the request never produced a response to judge), computed from the response
against the fixture's contract. Asserted at the only seam: the CLI vs the fake,
on the produced JSONL.
"""

from __future__ import annotations

import json

from test_dry_run import run_cli, with_pricing

from obench.fixtures import CALIBRATION_PROMPT, QA_SHORT_ANSWERS, THROUGHPUT_PROMPT, seed

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
        tmp_path,
        "run",
        "--level",
        "T1",
        "--model",
        "glm-5.3-flash",
        "--reps",
        "1",
        "--settle-s",
        "2",
        "--settle-poll-s",
        "0.01",
        *extra,
    )


def read_requests(tmp_path) -> list[dict]:
    files = sorted((tmp_path / "runs").glob("requests-*.jsonl"))
    assert len(files) == 1
    return [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines() if l.strip()]


def cuerpo(prompt: str) -> str:
    """The fixture body the reply scripts match: under the cache-free lane the
    sent prompt carries the run's nonce as one line above the fixture."""
    if prompt.startswith((PRIMERA_PREGUNTA, CALIBRATION_PROMPT, THROUGHPUT_PROMPT)):
        return prompt
    return prompt.split("\n\n", 1)[1]  # the nonce's single line + the blank separator


def qa_reply(prompt: str) -> str:
    """A one-sentence reply that contains the question's expected answer.
    Receives the fixture BODY (callers strip the lane's nonce)."""
    respuesta = next(ans[0] for q, ans in QA_SHORT_ANSWERS.items() if prompt.startswith(q))
    return f"The answer is {respuesta}."


def correct_transcript(prompt: str) -> str:
    """Every workload answers exactly as its fixture asks."""
    prompt = cuerpo(prompt)
    if prompt == CALIBRATION_PROMPT:
        return "OK"
    if prompt == THROUGHPUT_PROMPT:
        return ", ".join(str(i) for i in range(1, 151)) + "\nDONE"
    try:
        return qa_reply(prompt)
    except StopIteration:
        return "world"  # the canary's T2-size body: never graded, only accepted


def test_scripted_correct_transcripts_pass_all_three_checkers(tmp_path, fake_cli):
    fake_cli.reply_for = correct_transcript
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
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
        if cuerpo(prompt).startswith(PRIMERA_PREGUNTA)
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    fallidos = [r for r in requests if r["checker"] == "fail"]
    assert len(fallidos) == 1
    assert fallidos[0]["workload"] == "qa_short" and fallidos[0]["req_id"].endswith("-0000")
    assert all(r["checker"] == "pass" for r in requests if r is not fallidos[0])


def test_calibration_fails_when_reported_tokens_drift_beyond_2pct(tmp_path, fake_cli):
    """The 3 identical calibration requests must report reproducible tokens (2 % band)."""
    fake_cli.reply_for = lambda prompt: "OK" if cuerpo(prompt) == CALIBRATION_PROMPT else "world"
    drift = seed("calibration", "glm-5.3-flash", 1, 1)
    fake_cli.counts_for = lambda _prompt, s: (26, 13) if s == drift else (26, 12)
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    calibraciones = [r for r in read_requests(tmp_path) if r["workload"] == "calibration"]
    assert len(calibraciones) == 3
    veredictos = {r["tok_out"]: r["checker"] for r in calibraciones}
    assert veredictos == {12: "pass", 13: "fail"}  # 13 vs the 12 median = 8.3 % off


def test_truncated_stream_fails_the_checker_even_with_looking_correct_content(tmp_path, fake_cli):
    """Billed-but-truncated: no done frame means the outcome is never verifiable."""
    fake_cli.reply_for = correct_transcript
    fake_cli.truncate_stream = True
    fake_cli.truncate_from = 10 + 1  # the canary's chats stay healthy
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err  # billed: recorded, not retried
    requests = read_requests(tmp_path)
    assert requests and all(r["checker"] == "fail" for r in requests)
    assert all(r["tok_in"] is None and r["api"] is None for r in requests)


def test_throughput_fails_when_the_number_sequence_is_incomplete(tmp_path, fake_cli):
    fake_cli.reply_for = lambda prompt: (
        "1, 2, 3 and so on up to 150, DONE"
        if cuerpo(prompt) == THROUGHPUT_PROMPT
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    throughput = [r for r in requests if r["workload"] == "throughput"]
    assert throughput[0]["checker"] == "fail"
    assert all(r["checker"] == "pass" for r in requests if r["workload"] != "throughput")


def test_errored_request_carries_no_verdict(tmp_path, fake_cli):
    """A request that never produced a response has no outcome to check: null, not fail."""
    # The canary's 10 chats open the run: the first batch's request 3 is chat #13.
    fake_cli.fails_on = 13  # request 3 of the first batch gets a scripted 500
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    fallidas = [r for r in requests if r["http"] != 200]
    assert len(fallidas) == 1
    assert fallidas[0]["checker"] is None and fallidas[0]["err"]
    assert sum(1 for r in requests if r["checker"] is None) == 1


def test_throughput_passes_with_prose_around_the_complete_list(tmp_path, fake_cli):
    """Digits in the preamble ("from 1 to 150") do not break a complete structure."""
    fake_cli.reply_for = lambda prompt: (
        "Sure! Here are the numbers from 1 to 150:\n"
        + ", ".join(str(i) for i in range(1, 151))
        + "\nDONE"
        if cuerpo(prompt) == THROUGHPUT_PROMPT
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    throughput = next(r for r in read_requests(tmp_path) if r["workload"] == "throughput")
    assert throughput["checker"] == "pass"


def test_qa_short_accepts_natural_phrasing_between_answer_tokens(tmp_path, fake_cli):
    """'three hundred AND sixty-six' is the accepted spelling, with gaps allowed."""
    pregunta = "How many days are there in a leap year?"
    fake_cli.reply_for = lambda prompt: (
        "There are three hundred and sixty-six days in a leap year."
        if cuerpo(prompt).startswith(pregunta)
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    assert all(r["checker"] == "pass" for r in requests), [
        (r["workload"], r["checker"]) for r in requests if r["checker"] != "pass"
    ]


def test_qa_short_accepts_a_unicode_reply(tmp_path, fake_cli):
    pregunta = "What is the official language of Brazil?"
    fake_cli.reply_for = lambda prompt: (
        "A língua oficial do Brasil é o Português."
        if cuerpo(prompt).startswith(pregunta)
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    assert all(r["checker"] == "pass" for r in requests)


def test_qa_short_rejects_negated_answers(tmp_path, fake_cli):
    """'is not Paris' is a wrong answer, however much it contains the right token."""
    fake_cli.reply_for = lambda prompt: (
        "The capital of France is not Paris, it is a myth."
        if cuerpo(prompt).startswith("What is the capital of France?")
        else "It is definitely not 56."
        if cuerpo(prompt).startswith("What is 7 times 8?")
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    fallidos = {r["req_id"][-4:]: r["checker"] for r in requests if r["checker"] == "fail"}
    assert set(fallidos) == {"0000", "0002"}  # the negated France and 7x8 answers only


def test_calibration_rejects_zero_token_reports(tmp_path, fake_cli):
    """A zero-token report is not a measurement: it can never be the median reference."""
    fake_cli.reply_for = lambda prompt: "OK" if cuerpo(prompt) == CALIBRATION_PROMPT else "world"
    # Zero-token reports for the calibration cells only: the canary's T2-size
    # chats keep real counts, or its replays could never register a hit.
    fake_cli.counts_for = lambda prompt, _seed: (0, 0) if len(prompt) < 10_000 else (26, 12)
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    calibraciones = [r for r in read_requests(tmp_path) if r["workload"] == "calibration"]
    assert len(calibraciones) == 3
    assert all(r["checker"] == "fail" for r in calibraciones)


def test_qa_short_fails_when_a_negation_follows_the_answer(tmp_path, fake_cli):
    """A negation AFTER the answer flips it too ("Paris is not the capital")."""
    fake_cli.reply_for = lambda prompt: (
        "Paris is not the capital of France."
        if cuerpo(prompt).startswith(PRIMERA_PREGUNTA)
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    requests = read_requests(tmp_path)
    fallidos = [r for r in requests if r["checker"] == "fail"]
    assert len(fallidos) == 1
    assert fallidos[0]["workload"] == "qa_short" and fallidos[0]["req_id"].endswith("-0000")


def test_calibration_fails_without_full_sibling_evidence(tmp_path, fake_cli):
    """2 of 3 identical requests truncated: the survivor has no reproducibility
    evidence (its median would be itself) and must not grade pass."""
    fake_cli.reply_for = lambda prompt: "OK" if cuerpo(prompt) == CALIBRATION_PROMPT else "world"
    # requests 21-23 are the calibration burst; #22 (its second request) gets a 500
    fake_cli.fails_on = 10 + 22  # the canary's 10 chats shift the ordinal
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err
    calibraciones = [r for r in read_requests(tmp_path) if r["workload"] == "calibration"]
    assert len(calibraciones) == 3
    veredictos = [r["checker"] for r in calibraciones]
    assert None in veredictos  # the failed request is a null attempt
    assert "pass" not in veredictos  # the lone survivor cannot vouch for itself


def test_throughput_blob_of_digits_is_graded_never_a_harness_error(tmp_path, fake_cli):
    """A degenerate digit run must not crash int-parsing: it grades, never aborts."""
    fake_cli.reply_for = lambda prompt: (
        ", ".join(str(i) for i in range(1, 151)) + "\n" + "9" * 5000 + "\nDONE"
        if cuerpo(prompt) == THROUGHPUT_PROMPT
        else correct_transcript(prompt)
    )
    prepare(tmp_path)
    code, out, err = run_one_model(tmp_path)
    assert code == 0, out or err  # a model outcome, graded - not a CheckersError abort
    throughput = [r for r in read_requests(tmp_path) if r["workload"] == "throughput"]
    # The complete list + final DONE is the contract; the degenerate blob is
    # bounded digit noise that neither crashes int() nor breaks the structure.
    assert throughput[0]["checker"] == "pass"
    assert "Traceback" not in err
