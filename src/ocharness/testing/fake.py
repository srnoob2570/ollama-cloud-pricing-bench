"""Fake ollama.com — the harness's test seam.

Reproduces the behavior MEASURED during the live meter verification
(docs/research/medidor-vivo-2026-08-31.md): usage in 0.001 ticks with read lag,
instant and exact per-model request_count, and scriptable errors. Injected as an
httpx transport; never touches the network.

Scripting hooks (default behavior unless overridden): `reply_for` maps a chat
prompt to its reply text, `counts_for` maps (prompt, seed) to the done-object's
reported token counts, `catalog` is what /v1/models serves.
"""

from __future__ import annotations

import json
from collections.abc import Callable

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
        # Scriptable transcripts: the default reply is the "world" canned stream
        # with the (26, 12) token counts, exactly as the first tests pinned them.
        self.reply_for: Callable[[str], str] | None = None
        self.counts_for: Callable[[str, int | None], tuple[int, int]] | None = None
        # Tool-call scripting: return the call frames for a prompt, or None to
        # fall through to the text reply. Each frame: {"function": {"name", "arguments"}}.
        self.tool_calls_for: Callable[[str], list[dict] | None] | None = None
        # What /v1/models serves (list of ids); empty = an unscripted catalog.
        self.catalog: list[str] = []
        self.catalog_http = 200  # status the models endpoint answers with
        self.catalog_raise: Exception | None = None  # transport failure on /v1/models
        self.reject_all = False  # every chat request rejected (429), nothing billed
        self.usage_raise: Exception | None = None  # transport failure on /api/usage
        self.usage_raise_from = 10**9  # meter read ordinal from which it starts failing

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
            # _reads is 0-based before the increment: read ordinal = _reads + 1
            if self.usage_raise is not None and self._reads + 1 >= self.usage_raise_from:
                raise self.usage_raise
            return httpx.Response(200, json=self._read_meter())
        if request.url.path == "/v1/models":
            if "authorization" not in request.headers:
                return httpx.Response(401, json={"error": "invalid credentials"})
            if self.catalog_raise is not None:
                raise self.catalog_raise
            return httpx.Response(
                self.catalog_http,
                json={
                    "object": "list",
                    "data": [{"id": m, "object": "model"} for m in self.catalog],
                },
            )
        if request.url.path == "/api/chat":
            if "authorization" not in request.headers:
                return httpx.Response(401, json={"error": "invalid credentials"})
            if self.reject_all:
                return httpx.Response(429, json={"error": "scripted: everything rejected"})
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

    def _reply_text(self, body: dict) -> str:
        if self.reply_for is not None:
            mensajes = body.get("messages") or [{}]
            return self.reply_for(mensajes[0].get("content") or "")
        return "world"

    def _token_counts(self, body: dict) -> tuple[int, int]:
        if self.counts_for is not None:
            mensajes = body.get("messages") or [{}]
            prompt = mensajes[0].get("content") or ""
            semilla = (body.get("options") or {}).get("seed")
            return self.counts_for(prompt, semilla)
        return (26, 12)

    def _prompt_of(self, body: dict) -> str:
        return (body.get("messages") or [{}])[0].get("content") or ""

    def _chat_chunks(self, body: dict) -> bytes:
        modelo = body.get("model", "glm-5.3-flash")
        llamadas = self.tool_calls_for(self._prompt_of(body)) if self.tool_calls_for else None
        if llamadas:
            # The scripted calls stream as one message frame, verbatim, before the done.
            parciales = [
                {
                    "model": modelo,
                    "message": {"role": "assistant", "content": "", "tool_calls": llamadas},
                    "done": False,
                },
                self._done(body),
            ]
            if self.truncate_stream:
                parciales = parciales[:-1]
            return b"".join((json.dumps(c) + "\n").encode() for c in parciales)
        texto = self._reply_text(body)
        mitad = -(-len(texto) // 2)  # ceil: "world" -> "wor" + "ld", as always scripted
        parciales = [
            {
                "model": modelo,
                "message": {"role": "assistant", "content": texto[:mitad]},
                "done": False,
            },
            {
                "model": modelo,
                "message": {"role": "assistant", "content": texto[mitad:]},
                "done": False,
            },
            self._done(body),
        ]
        if self.truncate_stream:
            parciales = parciales[:-1]  # billed, but the stream ends without a done frame
        return b"".join((json.dumps(c) + "\n").encode() for c in parciales)

    def _done(self, body: dict) -> dict:
        prompt_eval, eval_ = self._token_counts(body)
        return {
            "model": body.get("model", "glm-5.3-flash"),
            "done": True,
            "done_reason": "stop",
            "total_duration": 500_000_000,
            "load_duration": None,
            "prompt_eval_count": prompt_eval,
            "prompt_eval_duration": None,
            "eval_count": eval_,
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
