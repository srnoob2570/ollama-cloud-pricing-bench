"""El fake de ollama.com como infraestructura del seam.

Debe reproducir el comportamiento MEDIDO en la verificación en vivo:
usage en ticks de 0.001 con lag, request_count instantáneo y exacto por modelo,
401 sin auth, y 429s/fallos scriptables.
"""

from __future__ import annotations

import json

import httpx


def test_chat_streaming_entrega_chunks_y_done_con_usage(fake):
    body = {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "hola"}], "stream": True}
    auth = {"Authorization": "Bearer test-key"}
    with httpx.Client(transport=fake.transport()) as client, client.stream(
        "POST", "https://fake.ollama/api/chat", json=body, headers=auth
    ) as r:
        assert r.status_code == 200
        lineas = [l for l in r.iter_lines() if l.strip()]
    chunks = [json.loads(l) for l in lineas]
    assert chunks[-1]["done"] is True
    assert isinstance(chunks[-1]["prompt_eval_count"], int)
    assert isinstance(chunks[-1]["eval_count"], int)
    assert len(fake.llamadas) == 1


def test_usage_sin_auth_da_401(fake):
    with httpx.Client(transport=fake.transport()) as client:
        assert client.get("https://fake.ollama/api/usage").status_code == 401


def test_usage_avanza_en_ticks_de_0_001_y_laguea(fake):
    fake.programar_consumo(ticks=5)
    auth = {"Authorization": "Bearer test-key"}
    with httpx.Client(transport=fake.transport()) as client:
        r0 = client.get("https://fake.ollama/api/usage", headers=auth).json()
        for _ in range(fake.lag_lecturas):
            client.get("https://fake.ollama/api/usage", headers=auth)
        r1 = client.get("https://fake.ollama/api/usage", headers=auth).json()
    assert r0["limits"]["session"]["usage"] == 0.234  # el lag aún no aplicó el consumo
    assert round(r1["limits"]["session"]["usage"] - 0.234, 3) == 0.005


def test_request_count_es_instantaneo_y_exacto_por_modelo(fake):
    with httpx.Client(transport=fake.transport()) as client:
        client.post("https://fake.ollama/api/chat", json={"model": "glm-5.3-flash", "stream": False},
                    headers={"Authorization": "Bearer test-key"})
        despues = client.get("https://fake.ollama/api/usage",
                             headers={"Authorization": "Bearer test-key"}).json()
    counts = {m["name"]: m["request_count"] for m in despues["limits"]["session"]["models"]}
    assert counts.get("glm-5.3-flash", 0) == 1


def test_429_por_concurrencia_scriptable(fake):
    fake.limite_concurrencia = 3
    assert fake.sondear_concurrencia(k=3) == 200
    assert fake.sondear_concurrencia(k=4) == 429


def test_fallo_puntual_scriptable(fake):
    fake.falla_en = 2  # la segunda request falla (índice 1-based)
    with httpx.Client(transport=fake.transport()) as client:
        auth = {"Authorization": "Bearer test-key"}
        r1 = client.post("https://fake.ollama/api/chat", json={"model": "glm-5.3-flash", "stream": False}, headers=auth)
        r2 = client.post("https://fake.ollama/api/chat", json={"model": "glm-5.3-flash", "stream": False}, headers=auth)
    assert r1.status_code == 200
    assert r2.status_code == 500