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

import asyncio
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
        self.undercount_at: int | None = None  # 1-based chat ordinal the meter never counts
        self.drift_ticks_per_read = 0  # a meter that never stabilizes (the settle's capped exit)
        self.truncate_stream = False  # 200 streams that end without a done frame
        self.truncate_from = 1  # 1-based chat ordinal from which streams truncate
        self.reject_from = 1  # 1-based chat ordinal from which everything 429s
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
        self.chat_raise: Exception | None = None  # transport failure on /api/chat
        self.chat_raise_from = 1  # 1-based chat ordinal from which it starts failing
        self.usage_raise: Exception | None = None  # transport failure on /api/usage
        self.usage_raise_from = 10**9  # meter read ordinal from which it starts failing
        # Cache scripting (the calibrate-cache seam): with `cache_horizon_s` set, the
        # fake caches per (model, prompt) — a repeat within the horizon is a hit, the
        # done-object reports `prompt_eval_cache_hit_count` (0 on a cold send) and only
        # `cached_eval_count` freshly evaluated tokens, the meter bills `cached_ticks`
        # instead of `ticks_per_request`, and a hit answers `cache_ttft_benefit_s`
        # sooner (a skipped prefill). Unset (default): no cache, and the done-object
        # carries no cache-hit field at all — the world where the API does not report
        # hits, which the calibration must read as "no token evidence".
        self.cache_horizon_s: float | None = None
        self.cached_eval_count = 6  # tokens re-evaluated on a hit (the prompt's tail)
        self.cached_ticks = 0  # ticks billed for a cache-hit request
        self.cache_ttft_benefit_s = 0.0
        # False: a deployment that caches but never reports the hit field — the
        # done-object keeps the reduced prompt_eval_count, the field stays absent.
        self.cache_report_hits = True
        self._cache_last: dict[tuple[str, str], float] = {}

    # ---- scripting ----
    def program_consumption(self, ticks: int) -> None:
        """Quota to bill (ACCUMULATES with pending); the meter reflects it only after
        `lag_reads` meter reads since this call."""
        self._pending_ticks += ticks
        self._reads_at_program = self._reads

    # ---- transport ----
    # The handler is a coroutine: the async transport (`async_transport`) awaits it,
    # so a k-burst genuinely overlaps at `chat_latency` and the 429 branch fires
    # exactly like the real endpoint's per-key limit. `transport()` is the SYNC
    # seam only (it drives the same handler on a throwaway loop per request; its
    # requests never overlap): the harness's AsyncClient must take
    # `async_transport()` — this wrapper would call asyncio.run inside the live
    # loop and raise per request.
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(lambda request: asyncio.run(self._handle(request)))

    def async_transport(self) -> httpx.MockTransport:
        """Transport for AsyncClient seams: bursts overlap at `chat_latency`."""
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
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
            if self.chat_raise is not None and self._n_chat + 1 >= self.chat_raise_from:
                raise self.chat_raise
            if self.reject_all and self._n_chat + 1 >= self.reject_from:
                return httpx.Response(429, json={"error": "scripted: everything rejected"})
            self._n_chat += 1
            if self.fails_on is not None and self._n_chat == self.fails_on:
                return httpx.Response(500, json={"error": "scripted one-shot failure"})
            if self.concurrency_limit is not None and self._in_flight >= self.concurrency_limit:
                return httpx.Response(429, json={"error": "concurrency limit exceeded"})
            self._in_flight += 1
            try:
                cacheada = self._cache_lookup(body)
                if self.chat_latency:
                    latencia = self.chat_latency
                    if cacheada:
                        latencia -= self.cache_ttft_benefit_s
                    await asyncio.sleep(max(0.0, latencia))
                self._bill(body, cacheada)
                if body.get("stream"):
                    return httpx.Response(200, content=self._chat_chunks(body, cacheada))
                return httpx.Response(200, json=self._done(body, cacheada))
            finally:
                self._in_flight -= 1
        return httpx.Response(404, json={"error": "not found"})

    def _bill(self, body: dict, cacheada: int = 0) -> None:
        modelo = body.get("model", "?")
        self._last_billed = modelo
        # A dropped bill (undercount_at): the request was accepted and billed
        # (its ticks land) but the meter's cumulative counter never sees it —
        # the signature the runner's post-burst count check exists to catch.
        if self.undercount_at is None or self._n_chat != self.undercount_at:
            self._counts[modelo] = self._counts.get(modelo, 0) + 1
        ticks = self.cached_ticks if cacheada else self.ticks_per_request
        if ticks:
            self.program_consumption(ticks=ticks)

    def _cache_lookup(self, body: dict) -> int:
        """Tokens served from cache for this request (0 = cold send or expired miss).

        A served prompt refreshes the cache whether it hit or not — the real
        behavior the replay's between-batches spacing probes.
        """
        if self.cache_horizon_s is None:
            return 0
        ahora = asyncio.get_running_loop().time()
        clave = (body.get("model", "?"), self._prompt_of(body))
        servido = self._cache_last.get(clave)
        self._cache_last[clave] = ahora
        if servido is None or (ahora - servido) > self.cache_horizon_s:
            return 0
        completa, _ = self._token_counts(body)
        return max(0, completa - self.cached_eval_count)

    def _reply_text(self, body: dict) -> str:
        if self.reply_for is not None:
            return self.reply_for(self._prompt_of(body))
        return "world"

    def _token_counts(self, body: dict) -> tuple[int, int]:
        if self.counts_for is not None:
            semilla = (body.get("options") or {}).get("seed")
            return self.counts_for(self._prompt_of(body), semilla)
        return (26, 12)

    def _prompt_of(self, body: dict) -> str:
        return (body.get("messages") or [{}])[0].get("content") or ""

    def _chat_chunks(self, body: dict, cacheada: int = 0) -> bytes:
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
                self._done(body, cacheada),
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
            self._done(body, cacheada),
        ]
        if self.truncate_stream and self._n_chat >= self.truncate_from:
            parciales = parciales[:-1]  # billed, but the stream ends without a done frame
        return b"".join((json.dumps(c) + "\n").encode() for c in parciales)

    def _done(self, body: dict, cacheada: int = 0) -> dict:
        prompt_eval, eval_ = self._token_counts(body)
        done = {
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
        if self.cache_horizon_s is not None:
            if cacheada:
                done["prompt_eval_count"] = self.cached_eval_count
            if self.cache_report_hits:
                # A deployment that tracks cache hits reports them even when
                # zero — the calibration's explicit-zero evidence for "no".
                done["prompt_eval_cache_hit_count"] = cacheada
        return done

    def _read_meter(self) -> dict:
        self._reads += 1
        if self._pending_ticks and (self._reads - self._reads_at_program >= self.lag_reads):
            delta = round(self._pending_ticks * 0.001, 3)
            self._session_usage = round(self._session_usage + delta, 3)
            self._weekly_usage = round(self._weekly_usage + delta, 3)
            self._pending_ticks = 0
        if self.drift_ticks_per_read:
            # Scripted drift: every read moves both windows, so no two consecutive
            # reads ever agree — the registration loop can only burn its cap.
            delta = round(self.drift_ticks_per_read * 0.001, 3)
            self._session_usage = round(self._session_usage + delta, 3)
            self._weekly_usage = round(self._weekly_usage + delta, 3)
        modelos = [{"name": m, "request_count": c} for m, c in sorted(self._counts.items())]
        return {
            "activity": {"cost": "0.00000", "period": {"type": "last_4_weeks"}, "models": []},
            "limits": {
                "session": {"usage": self._session_usage, "models": modelos},
                "weekly": {"usage": self._weekly_usage, "models": [dict(m) for m in modelos]},
            },
        }
