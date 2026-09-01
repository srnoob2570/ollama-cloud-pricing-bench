"""Fake de ollama.com — el seam de tests del harness.

Reproduce el comportamiento MEDIDO en la verificación en vivo del medidor
(docs/research/medidor-vivo-2026-08-31.md): usage en ticks de 0.001 con lag de
lecturas, request_count instantáneo y exacto por modelo, 401 sin auth, y
429s/fallos puntuales scriptables. Se inyecta como transport de httpx; nunca
toca la red.
"""

from __future__ import annotations

import json

import httpx


class FakeOllama:
    def __init__(self, *, uso_sesion: float = 0.234, uso_semanal: float = 0.146,
                 lag_lecturas: int = 2) -> None:
        self.llamadas: list[dict] = []
        self.lag_lecturas = lag_lecturas
        self.limite_concurrencia: int | None = None
        self.falla_en: int | None = None  # 1-based: SOLO esa request falla
        self._uso_sesion = round(uso_sesion, 3)
        self._uso_semanal = round(uso_semanal, 3)
        self._n_chat = 0
        self._lecturas = 0
        self._lecturas_al_programar = 0
        self._ticks_pendientes = 0
        self._en_vuelo = 0
        self._counts: dict[str, int] = {}

    # ---- scripting ----
    def programar_consumo(self, ticks: int) -> None:
        """Cuota a facturar (se ACUMULA con lo pendiente); el medidor no la refleja
        hasta que pasen `lag_lecturas` lecturas desde este punto."""
        self._ticks_pendientes += ticks
        self._lecturas_al_programar = self._lecturas

    def sondear_concurrencia(self, k: int) -> int:
        """Sonda directa (los 429 reales llegan por el transport con requests en vuelo)."""
        if self.limite_concurrencia is not None and k > self.limite_concurrencia:
            return 429
        return 200

    # ---- transport ----
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._manejar)

    def _manejar(self, request: httpx.Request) -> httpx.Response:
        cuerpo: dict = {}
        if request.content:
            cuerpo = json.loads(request.content)
        self.llamadas.append({"path": request.url.path, "auth": "authorization" in request.headers,
                              "body": cuerpo})
        if request.url.path == "/api/usage":
            if "authorization" not in request.headers:
                return httpx.Response(401, json={"error": "invalid credentials"})
            return httpx.Response(200, json=self._leer_medidor())
        if request.url.path == "/api/chat":
            if "authorization" not in request.headers:
                return httpx.Response(401, json={"error": "invalid credentials"})
            self._n_chat += 1
            if self.falla_en is not None and self._n_chat == self.falla_en:
                return httpx.Response(500, json={"error": "fallo puntual scriptado"})
            if (self.limite_concurrencia is not None
                    and self._en_vuelo >= self.limite_concurrencia):
                return httpx.Response(429, json={"error": "concurrencia excedida"})
            self._en_vuelo += 1
            try:
                self._facturar(cuerpo)
                if cuerpo.get("stream"):
                    return httpx.Response(200, content=self._chunks_chat(cuerpo))
                return httpx.Response(200, json=self._done(cuerpo))
            finally:
                self._en_vuelo -= 1
        return httpx.Response(404, json={"error": "not found"})

    def _facturar(self, cuerpo: dict) -> None:
        modelo = cuerpo.get("model", "?")
        self._counts[modelo] = self._counts.get(modelo, 0) + 1

    def _chunks_chat(self, cuerpo: dict) -> bytes:
        modelo = cuerpo.get("model", "glm-5.3-flash")
        parciales = [
            {"model": modelo, "message": {"role": "assistant", "content": "mun"}, "done": False},
            {"model": modelo, "message": {"role": "assistant", "content": "do"}, "done": False},
            self._done(cuerpo),
        ]
        return b"".join((json.dumps(c) + "\n").encode() for c in parciales)

    def _done(self, cuerpo: dict) -> dict:
        return {
            "model": cuerpo.get("model", "glm-5.3-flash"),
            "done": True,
            "done_reason": "stop",
            "total_duration": 500_000_000,
            "load_duration": None,
            "prompt_eval_count": 26,
            "prompt_eval_duration": None,
            "eval_count": 12,
            "eval_duration": None,
        }

    def _leer_medidor(self) -> dict:
        self._lecturas += 1
        if self._ticks_pendientes and (self._lecturas - self._lecturas_al_programar
                                       >= self.lag_lecturas):
            delta = round(self._ticks_pendientes * 0.001, 3)
            self._uso_sesion = round(self._uso_sesion + delta, 3)
            self._uso_semanal = round(self._uso_semanal + delta, 3)
            self._ticks_pendientes = 0
        modelos = [{"name": m, "request_count": c} for m, c in sorted(self._counts.items())]
        return {
            "activity": {"cost": "0.00000", "period": {"type": "last_4_weeks"}, "models": []},
            "limits": {
                "session": {"usage": self._uso_sesion, "models": modelos},
                "weekly": {"usage": self._uso_semanal, "models": [dict(m) for m in modelos]},
            },
        }