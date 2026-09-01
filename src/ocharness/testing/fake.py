"""Fake ollama.com — the harness's test seam.

Reproduces the behavior MEASURED during the live meter verification
(docs/research/medidor-vivo-2026-08-31.md): usage in 0.001 ticks with read lag,
instant and exact per-model request_count, and scriptable errors. Injected as an
httpx transport; never touches the network.
"""

from __future__ import annotations

import json

import httpx


class FakeOllama:
    def __init__(
        self, *, session_usage: float = 0.234, weekly_usage: float = 0.146, lag_reads: int = 2
    ) -> None:
        self.calls: list[dict] = []
        self.lag_reads = lag_reads
        self.concurrency_limit: int | None = None
        self.fails_on: int | None = None  # 1-based: ONLY that request fails
        self.ticks_per_request = 1  # quota ticks billed per accepted chat request
        self.undercount_by = 0  # requests dropped from the LAST-BILLED model's reported count
        self.truncate_stream = False  # 200 streams that end without a done frame
        self._session_usage = round(session_usage, 3)
        self._weekly_usage = round(weekly_usage, 3)
        self._n_chat = 0
        self._reads = 0
        self._reads_at_program = 0
        self._pending_ticks = 0
        self._in_flight = 0
        self.chat_latency = 0.0  # seconds the chat handler holds (for overlap tests)
        self._counts: dict[str, int] = {}
        self._last_billed: str | None = None

    # ---- scripting ----
    def program_consumption(self, ticks: int) -> None:
        """Quota to bill (ACCUMULATES with pending); the meter reflects it only after
        `lag_reads` meter reads since this call."""
        self._pending_ticks += ticks
        self._reads_at_program = self._reads

    def probe_concurrency(self, k: int) -> int:
        """Direct probe (real 429s arrive through the transport with in-flight requests)."""
        if self.concurrency_limit is not None and k > self.concurrency_limit:
            return 429
        return 200

    # ---- transport ----
    # NOTE: the handler is synchronous, so through the async runner k>1 bursts never
    # overlap (each chat blocks the event loop) and the 429 branch is unreachable via
    # run_level. Scripting 429s against async bursts arrives with the concurrency
    # ticket (Harness 06) — probe_concurrency and sync clients already cover the seam.
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body: dict = {}
        if request.content:
            body = json.loads(request.content)
        self.calls.append(
            {"path": request.url.path, "auth": "authorization" in request.headers, "body": body}
        )
        if request.url.path == "/api/usage":
            if "authorization" not in request.headers:
                return httpx.Response(401, json={"error": "invalid credentials"})
            return httpx.Response(200, json=self._read_meter())
        if request.url.path == "/api/chat":
            if "authorization" not in request.headers:
                return httpx.Response(401, json={"error": "invalid credentials"})
            self._n_chat += 1
            if self.fails_on is not None and self._n_chat == self.fails_on:
                return httpx.Response(500, json={"error": "scripted one-shot failure"})
            if self.concurrency_limit is not None and self._in_flight >= self.concurrency_limit:
                return httpx.Response(429, json={"error": "concurrency limit exceeded"})
            self._in_flight += 1
            try:
                if self.chat_latency:
                    import time

                    time.sleep(self.chat_latency)
                self._bill(body)
                if body.get("stream"):
                    return httpx.Response(200, content=self._chat_chunks(body))
                return httpx.Response(200, json=self._done(body))
            finally:
                self._in_flight -= 1
        return httpx.Response(404, json={"error": "not found"})

    def _bill(self, body: dict) -> None:
        modelo = body.get("model", "?")
        self._counts[modelo] = self._counts.get(modelo, 0) + 1
        self._last_billed = modelo
        if self.ticks_per_request:
            self.program_consumption(ticks=self.ticks_per_request)

    def _chat_chunks(self, body: dict) -> bytes:
        modelo = body.get("model", "glm-5.3-flash")
        parciales = [
            {"model": modelo, "message": {"role": "assistant", "content": "wor"}, "done": False},
            {"model": modelo, "message": {"role": "assistant", "content": "ld"}, "done": False},
            self._done(body),
        ]
        if self.truncate_stream:
            parciales = parciales[:-1]  # billed, but the stream ends without a done frame
        return b"".join((json.dumps(c) + "\n").encode() for c in parciales)

    def _done(self, body: dict) -> dict:
        return {
            "model": body.get("model", "glm-5.3-flash"),
            "done": True,
            "done_reason": "stop",
            "total_duration": 500_000_000,
            "load_duration": None,
            "prompt_eval_count": 26,
            "prompt_eval_duration": None,
            "eval_count": 12,
            "eval_duration": None,
        }

    def _read_meter(self) -> dict:
        self._reads += 1
        if self._pending_ticks and (self._reads - self._reads_at_program >= self.lag_reads):
            delta = round(self._pending_ticks * 0.001, 3)
            self._session_usage = round(self._session_usage + delta, 3)
            self._weekly_usage = round(self._weekly_usage + delta, 3)
            self._pending_ticks = 0
        modelos = [{"name": m, "request_count": c} for m, c in sorted(self._counts.items())]
        if self.undercount_by:
            # A dropped request stays dropped: the counter of the LAST-BILLED model
            # (the one under test) is reported short by `undercount_by` on every read,
            # like the real counters which never decay.
            for entrada in modelos:
                if entrada["name"] == self._last_billed:
                    entrada["request_count"] = max(0, entrada["request_count"] - 1)
        return {
            "activity": {"cost": "0.00000", "period": {"type": "last_4_weeks"}, "models": []},
            "limits": {
                "session": {"usage": self._session_usage, "models": modelos},
                "weekly": {"usage": self._weekly_usage, "models": [dict(m) for m in modelos]},
            },
        }
