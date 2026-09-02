"""The spending gate: validates the mark, the level, the live table; one run per mark."""

from __future__ import annotations

import json

import pytest
from conftest import standard_table, write_table

from ocharness.cli import main
from ocharness.gate import GateClosed, require_dry_run


def _pricing(tmp_path, version: str = "2026-08-31") -> str:
    write_table(tmp_path / "pricing", version, standard_table())
    return str(tmp_path / "pricing")


def test_corrupt_mark_does_not_open(tmp_path, capsys):
    _pricing(tmp_path)
    assert (
        main(
            [
                "--base",
                str(tmp_path),
                "dry-run",
                "--level",
                "T1",
                "--pricing-dir",
                _pricing(tmp_path),
            ]
        )
        == 0
    )
    (tmp_path / "runs" / "gate-T1.json").write_text("", encoding="utf-8")  # empty mark
    assert main(["--base", str(tmp_path), "run", "--level", "T1"]) == 2
    assert "corrupt" in capsys.readouterr().err


def test_mark_of_another_level_does_not_open_another_level(tmp_path):
    _pricing(tmp_path)
    assert (
        main(
            [
                "--base",
                str(tmp_path),
                "dry-run",
                "--level",
                "T1",
                "--pricing-dir",
                _pricing(tmp_path),
            ]
        )
        == 0
    )
    assert main(["--base", str(tmp_path), "run", "--level", "T3"]) == 2  # only T1 open


def test_table_change_invalidates_the_gate(tmp_path):
    """The dry-run approved table A; if the live table is another one, run refuses."""
    pricing = _pricing(tmp_path)
    assert (
        main(["--base", str(tmp_path), "dry-run", "--level", "T1", "--pricing-dir", pricing]) == 0
    )
    write_table(tmp_path / "pricing", "2026-09-01", standard_table())  # newer table
    with pytest.raises(GateClosed, match="table"):
        require_dry_run(tmp_path, "T1", table_version="2026-09-01")


def test_one_dry_run_enables_one_run(tmp_path, fake_cli):
    pricing = _pricing(tmp_path)
    assert (
        main(
            [
                "--base",
                str(tmp_path),
                "dry-run",
                "--level",
                "T1",
                "--reps",
                "1",
                "--pricing-dir",
                pricing,
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--base",
                str(tmp_path),
                "run",
                "--level",
                "T1",
                "--reps",
                "1",
                "--settle-s",
                "2",
                "--settle-poll-s",
                "0.01",
            ]
        )
        == 0
    )  # passes and consumes
    assert main(["--base", str(tmp_path), "run", "--level", "T1"]) == 2  # mark consumed


def test_mark_is_atomic_and_versioned(tmp_path):
    from ocharness.gate import mark_dry_run

    mark_dry_run(tmp_path, "T1", {"table_version": "2026-08-31", "rows": [{"a": 1}]})
    marca = json.loads((tmp_path / "runs" / "gate-T1.json").read_text(encoding="utf-8"))
    assert marca["level"] == "T1" and marca["table_version"] == "2026-08-31"
    require_dry_run(tmp_path, "T1", table_version="2026-08-31")  # does not raise
    with pytest.raises(GateClosed, match="table"):
        require_dry_run(tmp_path, "T1", table_version="other")
