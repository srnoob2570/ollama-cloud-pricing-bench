"""La compuerta de gasto: valida marca, nivela, tabla vigente y se consume por corrida."""

from __future__ import annotations

import json

import pytest
from conftest import escribir_tabla, tabla_estandar

from ocharness.cli import main
from ocharness.gate import CompuertaCerrada, exigir_dry_run, marcar_dry_run


def _pricing(tmp_path, version: str = "2026-08-31") -> str:
    escribir_tabla(tmp_path / "pricing", version, tabla_estandar())
    return str(tmp_path / "pricing")


def test_marca_corrupta_no_abre(tmp_path, capsys):
    _pricing(tmp_path)
    assert main(["--base", str(tmp_path), "dry-run", "--level", "T1",
                 "--pricing-dir", _pricing(tmp_path)]) == 0
    (tmp_path / "runs" / "gate-T1.json").write_text("", encoding="utf-8")  # marker vacío
    assert main(["--base", str(tmp_path), "run", "--level", "T1"]) == 2
    assert "corrupta" in capsys.readouterr().err


def test_marca_de_otro_nivel_no_abre_otro_nivel(tmp_path):
    _pricing(tmp_path)
    assert main(["--base", str(tmp_path), "dry-run", "--level", "T1",
                 "--pricing-dir", _pricing(tmp_path)]) == 0
    assert main(["--base", str(tmp_path), "run", "--level", "T3"]) == 2  # solo T1 abierta


def test_cambio_de_tabla_invalida_la_compuerta(tmp_path):
    """El dry-run aprobó con la tabla A; si la vigente es otra, `run` se niega."""
    pricing = _pricing(tmp_path)
    assert main(["--base", str(tmp_path), "dry-run", "--level", "T1",
                 "--pricing-dir", pricing]) == 0
    escribir_tabla(tmp_path / "pricing", "2026-09-01", tabla_estandar())  # más nueva
    with pytest.raises(CompuertaCerrada, match="tabla"):
        exigir_dry_run(tmp_path, "T1", table_version="2026-09-01")


def test_un_dry_run_habilita_una_corrida(tmp_path):
    pricing = _pricing(tmp_path)
    assert main(["--base", str(tmp_path), "dry-run", "--level", "T1",
                 "--pricing-dir", pricing]) == 0
    assert main(["--base", str(tmp_path), "run", "--level", "T1"]) == 3  # pasa y consume
    assert main(["--base", str(tmp_path), "run", "--level", "T1"]) == 2  # marca consumida


def test_marca_atomica_y_versionada(tmp_path):
    marcar_dry_run(tmp_path, "T1", {"table_version": "2026-08-31", "filas": [{"a": 1}]})
    marca = json.loads((tmp_path / "runs" / "gate-T1.json").read_text(encoding="utf-8"))
    assert marca["level"] == "T1" and marca["table_version"] == "2026-08-31"
    exigir_dry_run(tmp_path, "T1", table_version="2026-08-31")  # no lanza
    with pytest.raises(CompuertaCerrada, match="tabla"):
        exigir_dry_run(tmp_path, "T1", table_version="otra")