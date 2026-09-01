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
        tmp_path, "run", "--level", "T1", "--model", "glm-5.3-flash", "--reps", "1", *extra
    )


def test_status_without_any_run_is_quiet_and_free(tmp_path, fake):
    code, out, _err = run_cli(tmp_path, "status")
    assert code == 0
    assert "no run manifest" in out
    assert fake.calls == []  # status never talks to the API


def test_status_summarizes_done_batches_and_consumed_quota(tmp_path, fake_cli):
    prepare(tmp_path)
    assert run_t1(tmp_path, "--settle-s", "0")[0] == 0
    antes = len(fake_cli.calls)
    code, out, _err = run_cli(tmp_path, "status", "--level", "T1")
    assert code == 0
    assert len(fake_cli.calls) == antes  # artifacts only: zero requests
    assert "3 planned | 3 done" in out and "0 pending" in out
    assert "requests ok: 24" in out  # 20 qa + 3 calibration + 1 throughput
    assert "session 2.4 pp" in out and "weekly 2.4 pp" in out  # 24 ticks x 0.1 pp
    assert "3/3 closed batches with a readable bracket" in out


def test_status_flags_aborted_and_in_flight_batches(tmp_path, fake_cli):
    fake_cli.undercount_by = 1  # the qa_short batch aborts; the run stops there
    prepare(tmp_path)
    assert run_t1(tmp_path, "--settle-s", "0")[0] == 1
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
    assert run_t1(tmp_path, "--settle-s", "0")[0] == 0
    doc = json_doc(tmp_path, "status", "--level", "T1")
    nivel = doc["levels"][0]
    assert nivel["level"] == "T1" and nivel["run_id"].startswith("T1-")
    assert nivel["planned"] == 3
    assert nivel["counts"] == {"done": 3, "aborted": 0, "in_flight": 0, "pending": 0}
    assert nivel["requests_ok"] == 24
    assert nivel["quota"]["dpp_session"] == 2.4 and nivel["quota"]["dpp_weekly"] == 2.4
    assert [b["status"] for b in nivel["batches"]] == ["done", "done", "done"]
    assert {b["workload"] for b in nivel["batches"]} == {"qa_short", "calibration", "throughput"}
    assert all(b["batch_id"] and b["model"] == "glm-5.3-flash" for b in nivel["batches"])


def test_status_without_level_lists_every_manifest(tmp_path, fake_cli):
    prepare(tmp_path)
    assert run_t1(tmp_path, "--settle-s", "0")[0] == 0
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
