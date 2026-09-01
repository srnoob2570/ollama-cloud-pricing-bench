"""`bench dry-run` y la compuerta: comportamiento externo del ticket Harness 01."""

from __future__ import annotations

import contextlib
import io
import json

from conftest import escribir_tabla, tabla_estandar

from ocharness.cli import main


def correr(tmp_path, *args) -> tuple[int, str, str]:
    """Ejecuta la CLI en el seam, capturando stdout/stderr (argparse incluido)."""
    salida, errores = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
            codigo = main(["--base", str(tmp_path), *args])
    except SystemExit as e:  # argparse sale con 2 en errores de uso
        codigo = int(e.code or 0)
    return codigo, salida.getvalue(), errores.getvalue()


def json_doc(tmp_path, *args) -> dict:
    codigo, salida, errores = correr(tmp_path, *args, "--json")
    assert codigo == 0, salida or errores
    return json.loads(salida)


def con_pricing(tmp_path, version: str = "2026-08-31") -> str:
    escribir_tabla(tmp_path / "pricing", version, tabla_estandar())
    return str(tmp_path / "pricing")


def test_dry_run_estima_s0_y_s1_sin_llamar_api(tmp_path, fake, capsys):
    pricing = con_pricing(tmp_path)
    doc = json_doc(tmp_path, "dry-run", "--level", "T1", "--pricing-dir", pricing)
    assert fake.llamadas == []  # cero requests: el dry-run es gratis
    assert doc["table_version"] == "2026-08-31"
    filas = doc["filas"]
    assert [f["workload"] for f in filas] == ["qa_corto", "calibracion", "throughput"]
    qa = filas[0]
    assert qa["modelos"] == 19 and qa["requests"] == 1900  # 20 req × 19 modelos × n=5
    assert qa["costo_s0"] > 0
    assert qa["costo_s1"] < qa["costo_s0"]  # glm/kimi descuentan cache; el total baja
    assert qa["pp_esperado"] is None  # sin calibración pp/1M todavía


def test_dry_run_cache_solo_con_descuento(tmp_path, fake, capsys):
    """Tabla donde todos los modelos tienen cached=input ⇒ S1 ≡ S0 en cada fila."""
    tabla = {m: {"input": 0.60, "cached_input": 0.60, "output": 3.60}
             for m in tabla_estandar()}
    pricing = escribir_tabla(tmp_path / "pricing", "2026-08-31", tabla)
    doc = json_doc(tmp_path, "dry-run", "--level", "T1", "--pricing-dir", str(pricing))
    assert all(f["costo_s1"] == f["costo_s0"] for f in doc["filas"])


def test_run_sin_dry_run_se_niega(tmp_path, fake, capsys):
    pricing = con_pricing(tmp_path)
    codigo, _, errores = correr(tmp_path, "run", "--level", "T1", "--pricing-dir", pricing)
    assert codigo == 2
    assert "dry-run" in errores
    assert fake.llamadas == []  # negarse tampoco gasta


def test_compuerta_pasa_con_dry_run_previo(tmp_path, fake, capsys):
    pricing = con_pricing(tmp_path)
    assert correr(tmp_path, "dry-run", "--level", "T1", "--pricing-dir", pricing)[0] == 0
    codigo, _, errores = correr(tmp_path, "run", "--level", "T1")
    assert codigo != 0  # pasa la compuerta; run es stub hasta Harness 02
    assert "no implementado" in errores


def test_tabla_ausente_da_error_claro(tmp_path, fake, capsys):
    codigo, _, errores = correr(tmp_path, "dry-run", "--level", "T1",
                                "--pricing-dir", str(tmp_path / "vacio"))
    assert codigo == 2
    assert "tabla" in errores.lower() and "precio" in errores.lower()


def test_table_version_versionada(tmp_path, fake, capsys):
    pricing = str(tmp_path / "pricing")
    escribir_tabla(tmp_path / "pricing", "2026-09-01", tabla_estandar())
    escribir_tabla(tmp_path / "pricing", "2026-08-31", tabla_estandar())
    doc = json_doc(tmp_path, "dry-run", "--level", "T1", "--pricing-dir", pricing,
                   "--table-version", "2026-09-01")
    assert doc["table_version"] == "2026-09-01"
    doc2 = json_doc(tmp_path, "dry-run", "--level", "T1", "--pricing-dir", pricing,
                    "--table-version", "2026-08-31")
    assert doc2["table_version"] == "2026-08-31"


def test_s_es_parametro_del_escenario(tmp_path, fake, capsys):
    pricing = con_pricing(tmp_path)
    filas_0 = json_doc(tmp_path, "dry-run", "--level", "T2", "--pricing-dir", pricing,
                       "--s", "0.0")["filas"]
    filas_100 = json_doc(tmp_path, "dry-run", "--level", "T2", "--s", "1.0",
                         "--pricing-dir", pricing)["filas"]
    assert any(a["costo_s1"] != b["costo_s1"]
               for a, b in zip(filas_0, filas_100, strict=True))


def test_dry_run_estima_los_tres_niveles(tmp_path, fake, capsys):
    pricing = con_pricing(tmp_path)
    for nivel, n_filas in (("T1", 3), ("T2", 7), ("T3", 3)):
        doc = json_doc(tmp_path, "dry-run", "--level", nivel, "--pricing-dir", pricing)
        assert len(doc["filas"]) == n_filas
        assert all(f["modelos"] >= 3 for f in doc["filas"])
        assert all(f["pp_esperado"] is None for f in doc["filas"])  # s/calib hasta la calibración
    assert correr(tmp_path, "dry-run", "--level", "T9", "--pricing-dir", pricing)[0] == 2