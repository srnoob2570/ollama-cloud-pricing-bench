"""Ollama Cloud client: streaming chat + usage meter, both Bearer-authenticated.

The base URL and transport are injectable — the harness's single test seam — and
the API key comes only from the environment; it is never written to any dataset.
`chat()` timestamps the first chunk (TTFT) and returns the verbatim done-object.
"""

from __future__ import annotations

import json
import os
import time

import httpx

PROTOCOL_VERSION = "1"


def default_transport() -> httpx.BaseTransport | None:
    """None = the real network. Tests patch this to inject the fake's transport."""
    return None


class OllamaCloud:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OLLAMA_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "OLLAMA_API_KEY is not set - the harness reads the key only "
                "from the environment and never writes it to any dataset"
            )
        self.base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
        self._transport = transport if transport is not None else default_transport()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(10.0, read=600.0),
        )

    async def usage(self) -> tuple[int, dict | None]:
        """GET /api/usage -> (http status, full raw payload or None)."""
        r = await self._client.get("/api/usage")
        try:
            payload = r.json()
        except ValueError:
            payload = None
        return r.status_code, payload

    async def chat(self, *, model: str, prompt: str, seed: int | None = None) -> dict:
        """One streaming chat request with chunk timestamps; errors are data, not raises."""
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,  # streaming-first: the only mode with real cloud latency
            "options": {"seed": seed},  # the recorded seed is transmitted, not just provenance
        }
        rec: dict = {
            "t_start": time.time(),
            "t_first_chunk": None,
            "t_total": None,
            "chunks": 0,
            "http": None,
            "err": None,
            "done": None,
            "content": "",
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as r:
                rec["http"] = r.status_code
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", errors="replace")
                    rec["err"] = f"HTTP {r.status_code}: {body[:500]}"
                else:
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        if rec["t_first_chunk"] is None:
                            rec["t_first_chunk"] = time.time()
                        rec["chunks"] += 1
                        obj = json.loads(line)
                        if obj.get("done"):
                            rec["done"] = obj
                        else:
                            rec["content"] += (obj.get("message") or {}).get("content") or ""
                    if rec["done"] is None:
                        # Billed but token-less: a 200 that ends without a done frame is
                        # not a clean success — flagged so analysis can exclude it.
                        rec["err"] = "truncated: stream ended without a done frame"
        except Exception as e:  # noqa: BLE001 - a failed request is data: recorded, not raised
            # (an abort mid-batch would leave billed-but-unlogged requests; the count
            # check in the runner is what catches the drop)
            rec["err"] = f"{type(e).__name__}: {e}"
        rec["t_total"] = time.time()
        return rec

    async def aclose(self) -> None:
        await self._client.aclose()
