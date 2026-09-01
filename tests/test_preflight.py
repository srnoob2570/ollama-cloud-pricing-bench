"""Catalog preflight against the fake (ticket Harness 03): the run aborts with a
diff before spending when /v1/models no longer carries every slate id, passes on
tag-mapped ids, and pins the snapshot it saw into the run manifest.
"""

from __future__ import annotations

import json

import httpx
from conftest import standard_table
from test_dry_run import run_cli, with_pricing
from test_run import consumer_calls


def prepare(tmp_path) -> str:
    pricing = with_pricing(tmp_path)
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    return pricing


def full_catalog() -> list[str]:
    return sorted(standard_table())


def run_t1(tmp_path, *extra) -> tuple[int, str, str]:
    return run_cli(tmp_path, "run", "--level", "T1", "--reps", "1", "--settle-s", "0", *extra)


def test_missing_slate_model_aborts_with_a_diff_before_spending(tmp_path, fake_cli):
    fake_cli.catalog = [m for m in full_catalog() if m != "glm-5.3"]
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path)
    assert code == 1
    assert "preflight" in err and "glm-5.3" in err and "19" in err  # the diff, with counts
    assert consumer_calls(fake_cli) == []  # zero billed requests, zero meter reads
    # The mark is consumed before preflight (the gate's race window must stay a
    # pair of adjacent fs ops): the abort costs a fresh dry-run, never a double run.
    assert not (tmp_path / "runs" / "gate-T1.json").exists()
    assert not (tmp_path / "runs" / "manifest-T1.json").exists()  # no run state was created


def test_renamed_id_shows_as_missing_plus_new_in_the_diff(tmp_path, fake_cli):
    catalogo = full_catalog()
    catalogo.remove("kimi-k3")
    fake_cli.catalog = sorted(catalogo + ["kimi-k3.1"])
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path)
    assert code == 1
    assert "slate ids: kimi-k3\n" in err  # the missing slate id
    assert "kimi-k3.1" in err  # ...with its likely rename visible in the same diff


def test_tagged_catalog_ids_satisfy_the_slate(tmp_path, fake_cli):
    """Catalog ids carry tags the table lacks (medidor-vivo §5): base id matches."""
    catalogo = full_catalog()
    catalogo.remove("nemotron-3-nano")
    catalogo.remove("gemma4")
    fake_cli.catalog = sorted(catalogo + ["nemotron-3-nano:30b", "gemma4:31b"])
    prepare(tmp_path)
    code, out, err = run_t1(tmp_path, "--model", "gemma4")
    assert code == 0, out or err
    assert "gemma4 -> gemma4:31b" in err  # the mapping is surfaced, not hidden
    assert "nemotron-3-nano -> nemotron-3-nano:30b" in err


def test_tag_drift_on_a_tagged_slate_id_is_not_a_match(tmp_path, fake_cli):
    """gpt-oss:20b gone while gpt-oss:120b remains is drift: the table prices them apart."""
    fake_cli.catalog = [m for m in full_catalog() if m != "gpt-oss:20b"] + ["gpt-oss:200b"]
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path, "--model", "gpt-oss:20b")
    assert code == 1
    assert "gpt-oss:20b" in err


def test_new_catalog_models_are_surfaced_but_do_not_abort(tmp_path, fake_cli):
    fake_cli.catalog = sorted(full_catalog() + ["glm-6"])
    prepare(tmp_path)
    code, out, err = run_t1(tmp_path, "--model", "glm-5.3-flash")
    assert code == 0, out or err
    assert "glm-6" in err  # visible in the preflight line: rename candidates are data
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    snapshot = manifiesto["catalog"][-1]  # one snapshot per attempt, latest last
    assert set(snapshot["ids"]) == set(full_catalog() + ["glm-6"])
    assert snapshot["http"] == 200
    assert snapshot["matched"]["glm-5.3-flash"] == "glm-5.3-flash"


def test_catalog_read_failure_aborts_before_spending(tmp_path, fake_cli):
    fake_cli.catalog_http = 503
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path)
    assert code == 1
    assert "preflight" in err and "503" in err
    assert consumer_calls(fake_cli) == []
    assert not (tmp_path / "runs" / "gate-T1.json").exists()  # consumed by this attempt


def test_transport_failure_aborts_cleanly_without_a_traceback(tmp_path, fake_cli):
    """An unreachable catalog host is a clean abort, never a raw crash."""
    fake_cli.catalog_raise = httpx.ConnectError("name resolution failed")
    prepare(tmp_path)
    code, _out, err = run_t1(tmp_path)
    assert code == 1
    assert err.startswith("error: preflight: catalog read failed (ConnectError:")
    assert "Traceback" not in err
    assert consumer_calls(fake_cli) == []
    assert not (tmp_path / "runs" / "manifest-T1.json").exists()


def test_preflight_reruns_on_every_invocation(tmp_path, fake_cli):
    """Resume is not exempt: drift between attempts aborts the continuation too."""
    pricing = prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash")[0] == 0
    fake_cli.catalog = []  # the catalog goes empty between attempts
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, _out, err = run_t1(tmp_path, "--model", "glm-5.3-flash")
    assert code == 1
    assert "preflight" in err and "glm-5.3-flash" in err


def test_resume_appends_its_own_catalog_snapshot(tmp_path, fake_cli):
    """Each attempt's preflight joins the snapshot history: provenance stays true."""
    pricing = prepare(tmp_path)
    assert run_t1(tmp_path, "--model", "glm-5.3-flash")[0] == 0
    fake_cli.catalog = sorted(full_catalog() + ["glm-6"])  # benign drift between attempts
    assert (
        run_cli(tmp_path, "dry-run", "--level", "T1", "--reps", "1", "--pricing-dir", pricing)[0]
        == 0
    )
    code, out, err = run_t1(tmp_path, "--model", "glm-5.3-flash")
    assert code == 0, out or err
    manifiesto = json.loads((tmp_path / "runs" / "manifest-T1.json").read_text(encoding="utf-8"))
    assert len(manifiesto["catalog"]) == 2
    assert set(manifiesto["catalog"][0]["ids"]) == set(full_catalog())
    assert set(manifiesto["catalog"][-1]["ids"]) == set(full_catalog() + ["glm-6"])
