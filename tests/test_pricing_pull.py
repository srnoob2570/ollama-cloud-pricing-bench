"""Snapshotting the upstream rate card: `bench pricing-pull`.

The seam stays the CLI: `fetch_document` is monkeypatched to serve the
upstream document (no network), and the assertions land on the produced
artifacts — the new `pricing/<version>.json` — plus the report the command
printed. The pure functions (validate, map, diff) get targeted unit tests
alongside, per the testing contract.
"""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import write_table
from test_dry_run import json_doc, run_cli

from obench import pricing_pull
from obench.pricing import PriceTable, TableError

URL = pricing_pull.DEFAULT_PRICING_URL


def upstream_doc(**overrides) -> dict:
    """A minimal but shape-faithful upstream document: one identity id, one
    aliased id, one id with no cache_read, and the peak block."""
    doc = {
        "generated_at": "2026-09-05T11:52:44.615Z",
        "provider": "ollama-cloud",
        "source": "https://ollama.com/pricing",
        "models": {
            "glm-5.3-flash": {"input": 0.15, "cache_read": 0.03, "output": 0.5},
            "deepseek-v4-flash:0731": {"input": 0.22, "cache_read": 0.007, "output": 0.66},
            "qwen3.5:397b": {"input": 0.6, "output": 3.6},
        },
        "x_ollama": {
            "peak_window": "Peak pricing applies between 12:00 and 18:00 UTC, Monday to Friday.",
            "models": {
                "deepseek-v4-flash:0731": {"input": 0.44, "cache_read": 0.014, "output": 1.32}
            },
        },
    }
    doc.update(overrides)
    return doc


def serve(doc) -> httpx.BaseTransport:
    """A MockTransport serving `doc` (or a status code / raw body)."""
    if isinstance(doc, tuple):
        status, body = doc
        payload = body if isinstance(body, bytes) else body.encode()
    else:
        status, payload = 200, json.dumps(doc).encode()
    return httpx.MockTransport(lambda request: httpx.Response(status, content=payload))


def wire_upstream(monkeypatch, doc) -> None:
    monkeypatch.setattr(pricing_pull, "fetch_document", lambda url, **kw: doc)


# --- fetch ---------------------------------------------------------------


def test_fetch_document_parses_the_upstream():
    doc = pricing_pull.fetch_document(URL, transport=serve(upstream_doc()))
    assert doc["generated_at"] == "2026-09-05T11:52:44.615Z"


def test_fetch_document_fails_loud_on_http_and_json_errors():
    for doc in ((404, "not found"), (200, "<html>not json</html>")):
        with pytest.raises(pricing_pull.PullError):
            pricing_pull.fetch_document(URL, transport=serve(doc))

    def roto(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(pricing_pull.PullError, match="pricing fetch failed"):
        pricing_pull.fetch_document(URL, transport=httpx.MockTransport(roto))


# --- validation ----------------------------------------------------------


def test_validate_document_fails_loud_on_every_surprise():
    sin_generated_at = upstream_doc()
    del sin_generated_at["generated_at"]
    with pytest.raises(pricing_pull.PullError, match="missing 'generated_at'"):
        pricing_pull.validate_document(sin_generated_at)

    sin_modelos = upstream_doc(models={})
    with pytest.raises(pricing_pull.PullError, match="empty or invalid"):
        pricing_pull.validate_document(sin_modelos)

    con_negativo = upstream_doc()
    con_negativo["models"]["glm-5.3-flash"]["input"] = -0.15
    with pytest.raises(pricing_pull.PullError, match="negative 'input'"):
        pricing_pull.validate_document(con_negativo)

    con_invertida = upstream_doc()
    con_invertida["models"]["glm-5.3-flash"]["cache_read"] = 0.16
    with pytest.raises(pricing_pull.PullError, match="ABOVE input"):
        pricing_pull.validate_document(con_invertida)

    con_pico_roto = upstream_doc(x_ollama={"models": {}})
    with pytest.raises(pricing_pull.PullError, match="peak_window"):
        pricing_pull.validate_document(con_pico_roto)


def test_table_version_is_the_upstream_scrape_date():
    assert pricing_pull.table_version(upstream_doc()) == "2026-09-05"
    with pytest.raises(pricing_pull.PullError, match="not a timestamp"):
        pricing_pull.table_version(upstream_doc(generated_at="yesterday"))


# --- mapping -------------------------------------------------------------


def test_map_models_applies_aliases_and_notes_the_missing_cache():
    modelos, notas = pricing_pull.map_models(upstream_doc())
    assert modelos["deepseek-v4-flash"] == {"input": 0.22, "cached_input": 0.007, "output": 0.66}
    assert modelos["qwen3.5:397b"] == {"input": 0.6, "cached_input": 0.6, "output": 3.6}
    assert any(n.startswith("qwen3.5:397b: no cache_read") for n in notas)


def test_map_models_fails_on_an_alias_collapse(monkeypatch):
    roto = upstream_doc()
    roto["models"]["deepseek-v4-flash:alt"] = roto["models"]["deepseek-v4-flash:0731"]
    monkeypatch.setitem(pricing_pull.ALIASES, "deepseek-v4-flash:alt", "deepseek-v4-flash")
    with pytest.raises(pricing_pull.PullError, match="alias collapse"):
        pricing_pull.map_models(roto)


def test_build_snapshot_carries_the_peak_block_and_provenance():
    snapshot, _ = pricing_pull.build_snapshot(upstream_doc(), URL)
    assert snapshot["table_version"] == "2026-09-05"
    assert snapshot["per"] == 1_000_000
    assert snapshot["source"] == f"{URL} (upstream https://ollama.com/pricing)"
    assert snapshot["peak"]["rates"]["deepseek-v4-flash"] == {
        "input": 0.44,
        "cached_input": 0.014,
        "output": 1.32,
    }


# --- the seam ------------------------------------------------------------


def OLD_TABLE() -> dict:
    """The local 2026-08-31 rates the diff is taken against."""
    return {
        "glm-5.3-flash": {"input": 0.15, "cached_input": 0.03, "output": 0.5},
        "deepseek-v4-flash": {"input": 0.44, "cached_input": 0.014, "output": 1.32},
        "legacy-model": {"input": 1.0, "cached_input": 0.2, "output": 3.2},
    }


def test_pull_lands_a_new_table_and_the_diff_shows_every_change(tmp_path):
    pricing = write_table(tmp_path / "pricing", "2026-08-31", OLD_TABLE())
    informe = pricing_pull.pull(URL, pricing, transport=serve(upstream_doc()))
    assert informe["wrote"] is True
    assert informe["table_version"] == "2026-09-05"
    cambios = informe["changes"]
    assert cambios["added"] == ["qwen3.5:397b"]
    assert cambios["removed"] == ["legacy-model"]
    actualizados = {c["model"]: c for c in cambios["updated"]}
    assert actualizados["deepseek-v4-flash"]["new"] == {
        "input": 0.22,
        "cached_input": 0.007,
        "output": 0.66,
    }
    # The landed table is a real PriceTable: the harness consumes it like any other.
    tabla = PriceTable.load(pricing, "2026-09-05")
    assert tabla.rate("deepseek-v4-flash").input == 0.22
    # kimi-k3 only rides along when the upstream doc carries it; identity ids stay.
    assert "glm-5.3-flash" in tabla.models


def test_pull_upstream_rates_reach_the_table(tmp_path):
    pricing = write_table(tmp_path / "pricing", "2026-08-31", OLD_TABLE())
    doc = upstream_doc()
    doc["models"]["kimi-k3"] = {"input": 3.0, "cache_read": 0.3, "output": 15.0}
    pricing_pull.pull(URL, pricing, transport=serve(doc))
    tabla = PriceTable.load(pricing, "2026-09-05")
    assert tabla.rate("kimi-k3").output == 15.0


def test_pull_is_a_no_op_when_rates_unchanged(tmp_path):
    pricing = write_table(tmp_path / "pricing", "2026-08-31", OLD_TABLE())
    doc = upstream_doc()
    del doc["models"]["qwen3.5:397b"]  # the old table's ids, unchanged rates only
    doc["models"]["legacy-model"] = {"input": 1.0, "cache_read": 0.2, "output": 3.2}
    doc["models"]["deepseek-v4-flash:0731"] = {"input": 0.44, "cache_read": 0.014, "output": 1.32}
    informe = pricing_pull.pull(URL, pricing, transport=serve(doc))
    assert informe["up_to_date"] is True
    assert informe["wrote"] is False
    assert [p.name for p in pricing.glob("*.json")] == ["2026-08-31.json"]


def test_pull_refuses_to_overwrite_a_landed_table(tmp_path):
    pricing = write_table(tmp_path / "pricing", "2026-08-31", OLD_TABLE())
    write_table(
        pricing, "2026-09-05", {"glm-5.3-flash": {"input": 9.0, "cached_input": 9.0, "output": 9.0}}
    )
    with pytest.raises(pricing_pull.PullError, match="refusing to overwrite"):
        pricing_pull.pull(URL, pricing, transport=serve(upstream_doc()))
    # The landed table is untouched: the rates on disk are still its own.
    assert PriceTable.load(pricing, "2026-09-05").rate("glm-5.3-flash").input == 9.0


def test_check_diffs_without_writing(tmp_path):
    pricing = write_table(tmp_path / "pricing", "2026-08-31", OLD_TABLE())
    informe = pricing_pull.pull(URL, pricing, check=True, transport=serve(upstream_doc()))
    assert informe["wrote"] is False
    assert informe["changes"]["updated"]
    assert [p.name for p in pricing.glob("*.json")] == ["2026-08-31.json"]


# --- the CLI seam --------------------------------------------------------


def test_cli_writes_the_snapshot_and_reports_the_diff(tmp_path, monkeypatch):
    pricing = write_table(tmp_path / "pricing", "2026-08-31", OLD_TABLE())
    doc = upstream_doc()
    doc["models"]["kimi-k3"] = {"input": 3.0, "cache_read": 0.3, "output": 15.0}
    wire_upstream(monkeypatch, doc)
    informe = json_doc(tmp_path, "pricing-pull", "--pricing-dir", str(pricing))
    assert informe["wrote"] is True
    assert "kimi-k3" in informe["changes"]["added"]
    landed = json.loads((tmp_path / "pricing" / "2026-09-05.json").read_text())
    assert landed["table_version"] == "2026-09-05"
    assert landed["peak"]["rates"]["deepseek-v4-flash"]["input"] == 0.44


def test_cli_error_is_a_clean_exit_2(tmp_path, monkeypatch):
    def roto(url, **kw):
        raise pricing_pull.PullError("pricing fetch failed for x: HTTP 404")

    monkeypatch.setattr(pricing_pull, "fetch_document", roto)
    codigo, salida, errores = run_cli(tmp_path, "pricing-pull")
    assert codigo == 2
    assert "HTTP 404" in errores
    assert not (tmp_path / "pricing" / "2026-09-05.json").exists()


def test_cli_check_leaves_the_tables_alone(tmp_path, monkeypatch):
    pricing = write_table(tmp_path / "pricing", "2026-08-31", OLD_TABLE())
    wire_upstream(monkeypatch, upstream_doc())
    informe = json_doc(tmp_path, "pricing-pull", "--pricing-dir", str(pricing), "--check")
    assert informe["wrote"] is False
    assert [p.name for p in pricing.glob("*.json")] == ["2026-08-31.json"]


def test_off_peak_snapshot_is_a_valid_local_table_end_to_end(tmp_path, monkeypatch):
    """The landed snapshot loads through the harness's own loader, peak block
    riding along as ignored metadata — the table format does not grow a
    peak/off-peak concept it does not use yet."""
    pricing = write_table(tmp_path / "pricing", "2026-08-31", OLD_TABLE())
    wire_upstream(monkeypatch, upstream_doc())
    run_cli(tmp_path, "pricing-pull", "--pricing-dir", str(pricing))
    tabla = PriceTable.load(pricing)
    assert tabla.table_version == "2026-09-05"
    with pytest.raises(TableError):
        tabla.rate("deepseek-v4-flash:0731")  # only the aliased id exists
