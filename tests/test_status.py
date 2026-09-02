"""`bench status` (ticket Harness 03): pending/done batches and the quota they
consumed, summarized from artifacts only — the manifest, never the API."""

from __future__ import annotations

import json

from test_dry_run import json_doc, run_cli, with_pricing


def prepare(tmp_path) -> str:
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    return pricing


def run_t1(tmp_path, *extra) -> tuple[int, str, str]:
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


def test_status_without_any_run_is_quiet_and_free(tmp_path, fake):
    code, out, err = run_cli(tmp_path, "status")
    assert code == 0
    assert out == "" and "no run manifest" in err  # notices are log noise, not report
    assert fake.calls == []  # status never talks to the API


def test_status_json_stdout_stays_parseable_without_any_run(tmp_path, fake):
    code, out, err = run_cli(tmp_path, "status", "--json")
    assert code == 0
    assert json.loads(out) == {"levels": []}  # pure JSON stdout, always
    assert "no run manifest" in err


def test_status_summarizes_done_batches_and_consumed_quota(tmp_path, fake_cli):
    prepare(tmp_path)
    assert run_t1(tmp_path)[0] == 0
    antes = len(fake_cli.calls)
    code, out, _err = run_cli(tmp_path, "status", "--level", "T1")
    assert code == 0
    assert len(fake_cli.calls) == antes  # artifacts only: zero requests
    assert "3 planned | 3 done" in out and "0 pending" in out
    assert "requests ok: 24" in out  # 20 qa + 3 calibration + 1 throughput
    assert "session 2.4 pp" in out and "weekly 2.4 pp" in out  # 24 ticks x 0.1 pp
    assert "3/3 closed batches with a readable bracket" in out


def test_status_flags_aborted_and_in_flight_batches(tmp_path, fake_cli):
    # A dropped bill inside the first bracket (the canary's 10 chats open the
    # run): the qa_short batch aborts; the run stops there.
    fake_cli.undercount_at = 10 + 5
    prepare(tmp_path)
    assert run_t1(tmp_path)[0] == 1
    ruta = tmp_path / "runs" / "manifest-T1.json"
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    # ...and a crash mid-batch leaves a second one in_flight (operator simulation)
    manifiesto["batches"]["crashed00000000"] = {
        "status": "in_flight",
        "at": 0.0,
        "workload": "calibration",
        "model": "glm-5.3-flash",
    }
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")

    code, out, _err = run_cli(tmp_path, "status", "--level", "T1")
    assert code == 0
    assert "0 done, 1 aborted, 1 in_flight, 1 pending" in out
    assert "session 2.0 pp" in out  # only the aborted batch's bracket is readable
    assert "1/1 closed batches with a readable bracket" in out  # in_flight is not closed
    assert "attention: 1 aborted, 1 in_flight" in out
    assert "aborted: qa_short/glm-5.3-flash" in out
    assert "in_flight: calibration/glm-5.3-flash" in out


def test_status_json_carries_the_full_batch_map(tmp_path, fake_cli):
    prepare(tmp_path)
    assert run_t1(tmp_path)[0] == 0
    doc = json_doc(tmp_path, "status", "--level", "T1")
    nivel = doc["levels"][0]
    assert nivel["level"] == "T1" and nivel["run_id"].startswith("T1-")
    assert nivel["planned"] == 3
    assert nivel["counts"] == {"done": 3, "aborted": 0, "in_flight": 0, "pending": 0}
    assert nivel["requests_ok"] == 24
    # 24 ticks x 0.1 pp: the exact float of the brackets' delta chain, unrounded
    assert nivel["quota"]["dpp_session"] == sum(
        b["dpp_session"] for b in nivel["batches"] if b["dpp_session"] is not None
    )
    assert nivel["quota"]["dpp_weekly"] == sum(
        b["dpp_weekly"] for b in nivel["batches"] if b["dpp_weekly"] is not None
    )
    assert [b["status"] for b in nivel["batches"]] == ["done", "done", "done"]
    assert {b["workload"] for b in nivel["batches"]} == {"qa_short", "calibration", "throughput"}
    assert all(b["batch_id"] and b["model"] == "glm-5.3-flash" for b in nivel["batches"])


def test_status_without_level_lists_every_manifest(tmp_path, fake_cli):
    prepare(tmp_path)
    assert run_t1(tmp_path)[0] == 0
    (tmp_path / "runs" / "manifest-T2.json").write_text(
        json.dumps(
            {
                "run_id": "T2-fake",
                "level": "T2",
                "table_version": "2026-08-31",
                "protocol_version": "1",
                "k": 1,
                "started_at": 0.0,
                "planned": 210,
                "batches": {},
            }
        ),
        encoding="utf-8",
    )
    code, out, _err = run_cli(tmp_path, "status")
    assert code == 0
    assert "T2 run T2-fake" in out
    assert "210 planned" in out and "210 pending" in out  # nothing touched yet
    assert "T1 run T1-" in out  # ...next to the real T1 block


def test_status_reports_a_corrupt_manifest_cleanly(tmp_path, fake_cli):
    prepare(tmp_path)
    (tmp_path / "runs" / "manifest-T1.json").write_text("{corrupt", encoding="utf-8")
    code, _, err = run_cli(tmp_path, "status", "--level", "T1")
    assert code == 1
    assert "corrupt" in err and "Traceback" not in err


def test_status_tolerates_structurally_corrupt_entries(tmp_path, fake_cli):
    """Hand-edited run state renders as corrupt/unknown instead of crashing."""
    prepare(tmp_path)
    assert run_t1(tmp_path)[0] == 0
    ruta = tmp_path / "runs" / "manifest-T1.json"
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    manifiesto["batches"]["broken00000000"] = "not a dict"  # a structurally broken entry
    manifiesto["planned"] = "many"  # ...and a corrupt planned
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")
    code, out, err = run_cli(tmp_path, "status", "--level", "T1")
    assert code == 0 and "Traceback" not in err
    assert "4 planned | 3 done" in out  # planned falls back to the touched count
    assert "attention: 1 corrupt" in out  # the broken entry is visible, not hidden
    assert "corrupt: ?/? [broken000000" in out
    assert "requests ok: 24" in out  # the real entries still sum correctly


def test_status_planned_grows_with_a_wider_resume(tmp_path, fake_cli):
    """A scope-widening resume updates the run's plan: no self-contradictory report."""
    pricing = prepare(tmp_path)
    assert run_t1(tmp_path)[0] == 0  # planned 3 (one model)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    assert (
        run_cli(
            tmp_path,
            "run",
            "--level",
            "T1",
            "--reps",
            "1",
            "--settle-s",
            "2",
            "--settle-poll-s",
            "0.01",
        )[0]
        == 0
    )
    code, out, _err = run_cli(tmp_path, "status", "--level", "T1")
    assert code == 0
    assert "57 planned | 57 done" in out and "0 pending" in out


def test_status_quota_accumulates_each_window_independently(tmp_path, fake_cli):
    """A batch with only one readable window contributes that window's delta:
    the quota totals always agree with the report's own per-batch rows."""
    prepare(tmp_path)
    assert run_t1(tmp_path)[0] == 0
    ruta = tmp_path / "runs" / "manifest-T1.json"
    manifiesto = json.loads(ruta.read_text(encoding="utf-8"))
    victima = next(iter(manifiesto["batches"]))
    manifiesto["batches"][victima]["dpp_weekly"] = None  # a session-only bracket
    ruta.write_text(json.dumps(manifiesto), encoding="utf-8")

    doc = json_doc(tmp_path, "status", "--level", "T1")
    nivel = doc["levels"][0]
    sesiones = [b["dpp_session"] for b in nivel["batches"] if b["dpp_session"] is not None]
    assert nivel["quota"]["dpp_session"] == sum(sesiones)
    assert nivel["quota"]["dpp_weekly"] == sum(
        b["dpp_weekly"] for b in nivel["batches"] if b["dpp_weekly"] is not None
    )
    # the bracket census still counts only the brackets both windows resolved
    assert nivel["quota"]["batches_with_bracket"] == len(sesiones) - 1
