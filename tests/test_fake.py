"""The fake ollama.com as seam infrastructure.

Must reproduce the behavior MEASURED during the live verification: usage in 0.001
ticks with lag, instant exact per-model request_count, 401 without auth, and
scriptable 429s/one-shot failures.
"""

from __future__ import annotations

import json

import httpx
from conftest import standard_table


def test_chat_streaming_delivers_chunks_and_done_with_usage(fake):
    body = {
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    auth = {"Authorization": "Bearer test-key"}
    with (
        httpx.Client(transport=fake.transport()) as client,
        client.stream("POST", "https://fake.ollama/api/chat", json=body, headers=auth) as r,
    ):
        assert r.status_code == 200
        lineas = [l for l in r.iter_lines() if l.strip()]
    chunks = [json.loads(l) for l in lineas]
    assert chunks[-1]["done"] is True
    assert isinstance(chunks[-1]["prompt_eval_count"], int)
    assert isinstance(chunks[-1]["eval_count"], int)
    assert len(fake.calls) == 1


def test_usage_without_auth_returns_401(fake):
    with httpx.Client(transport=fake.transport()) as client:
        assert client.get("https://fake.ollama/api/usage").status_code == 401


def test_chat_without_auth_returns_401(fake):
    with httpx.Client(transport=fake.transport()) as client:
        r = client.post(
            "https://fake.ollama/api/chat", json={"model": "glm-5.3-flash", "stream": False}
        )
    assert r.status_code == 401


def test_usage_moves_in_0_001_ticks_with_lag(fake):
    fake.program_consumption(ticks=5)
    auth = {"Authorization": "Bearer test-key"}
    with httpx.Client(transport=fake.transport()) as client:
        r0 = client.get("https://fake.ollama/api/usage", headers=auth).json()
        for _ in range(fake.lag_reads):
            client.get("https://fake.ollama/api/usage", headers=auth)
        r1 = client.get("https://fake.ollama/api/usage", headers=auth).json()
    assert r0["limits"]["session"]["usage"] == 0.234  # the lag has not applied it yet
    assert round(r1["limits"]["session"]["usage"] - 0.234, 3) == 0.005
    assert round(r1["limits"]["weekly"]["usage"] - 0.146, 3) == 0.005


def test_consumption_accumulates_and_lag_counts_since_programming(fake):
    auth = {"Authorization": "Bearer test-key"}
    with httpx.Client(transport=fake.transport()) as client:
        client.get("https://fake.ollama/api/usage", headers=auth)  # two prior reads
        client.get("https://fake.ollama/api/usage", headers=auth)
        fake.program_consumption(ticks=3)  # lag must restart here
        r_first = client.get("https://fake.ollama/api/usage", headers=auth).json()
    assert r_first["limits"]["session"]["usage"] == 0.234  # lag reset by programming


def test_request_count_is_instant_and_exact_per_model(fake):
    auth = {"Authorization": "Bearer test-key"}
    with httpx.Client(transport=fake.transport()) as client:
        client.post(
            "https://fake.ollama/api/chat",
            json={"model": "glm-5.3-flash", "stream": False},
            headers=auth,
        )
        despues = client.get("https://fake.ollama/api/usage", headers=auth).json()
    counts = {m["name"]: m["request_count"] for m in despues["limits"]["session"]["models"]}
    assert counts.get("glm-5.3-flash", 0) == 1


def test_429_by_concurrency_scriptable(fake):
    fake.concurrency_limit = 3
    assert fake.probe_concurrency(k=3) == 200
    assert fake.probe_concurrency(k=4) == 429


def test_429_reachable_through_transport(fake):
    """The seam's 429 must be reachable over HTTP, not only via the direct probe."""
    import concurrent.futures

    fake.concurrency_limit = 1
    fake.chat_latency = 0.05  # keep requests in flight so they overlap
    auth = {"Authorization": "Bearer test-key"}
    with (
        httpx.Client(transport=fake.transport()) as client,
        concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
    ):
        resultados = list(
            pool.map(
                lambda _: (
                    client.post(
                        "https://fake.ollama/api/chat",
                        json={"model": "glm-5.3-flash", "stream": False},
                        headers=auth,
                    ).status_code
                ),
                range(4),
            )
        )
    assert sorted(resultados) == [200, 429, 429, 429]


def test_one_shot_failure_scriptable(fake):
    fake.fails_on = 2  # the second request fails; the third recovers
    auth = {"Authorization": "Bearer test-key"}
    with httpx.Client(transport=fake.transport()) as client:
        r1 = client.post(
            "https://fake.ollama/api/chat",
            json={"model": "glm-5.3-flash", "stream": False},
            headers=auth,
        )
        r2 = client.post(
            "https://fake.ollama/api/chat",
            json={"model": "glm-5.3-flash", "stream": False},
            headers=auth,
        )
        r3 = client.post(
            "https://fake.ollama/api/chat",
            json={"model": "glm-5.3-flash", "stream": False},
            headers=auth,
        )
    assert (r1.status_code, r2.status_code, r3.status_code) == (200, 500, 200)


def test_chat_bills_ticks_per_accepted_request(fake):
    """Every ACCEPTED request bills; 500/429 do not (the harness's no-retry contract)."""
    fake.ticks_per_request = 2
    auth = {"Authorization": "Bearer test-key"}
    with httpx.Client(transport=fake.transport()) as client:
        client.post("https://fake.ollama/api/chat", json={"model": "glm-5.3-flash"}, headers=auth)
        fake.fails_on = 2
        client.post("https://fake.ollama/api/chat", json={"model": "glm-5.3-flash"}, headers=auth)
        for _ in range(fake.lag_reads):
            client.get("https://fake.ollama/api/usage", headers=auth)
        r = client.get("https://fake.ollama/api/usage", headers=auth).json()
    assert round(r["limits"]["session"]["usage"] - 0.234, 3) == 0.002  # only the accepted one


def test_undercount_drops_requests_from_the_reported_counts(fake):
    """Scriptable meter undercount: the runner's count check must abort on it."""
    auth = {"Authorization": "Bearer test-key"}
    with httpx.Client(transport=fake.transport()) as client:
        client.post("https://fake.ollama/api/chat", json={"model": "glm-5.3-flash"}, headers=auth)
        client.post("https://fake.ollama/api/chat", json={"model": "glm-5.3-flash"}, headers=auth)
        fake.undercount_by = 1
        r = client.get("https://fake.ollama/api/usage", headers=auth).json()
    counts = {m["name"]: m["request_count"] for m in r["limits"]["session"]["models"]}
    assert counts["glm-5.3-flash"] == 1  # two billed, one dropped from the report


def test_standard_table_covers_the_full_catalog():
    assert len(standard_table()) == 19
    assert standard_table()["kimi-k3"]["output"] == 15.0
