"""Async client for CoralHub (docs/41-coral-recognition.md), Nitin's Google Coral Edge TPU
inference server on Unraid. Every call identifies itself with `X-Client: kibble` plus a bearer
token (CoralHub's own named-token feature, `app.client_identity`/`app.security`), so Kibble
shows up as its own named caller on CoralHub's Live activity dashboard rather than an anonymous
container IP. The whole Coral-backed recognizer is off whenever `coralhub_url` is unset
(`const.CONF_CORALHUB_URL`) -- this client is only ever constructed once a URL is configured
(`__init__.py`).

Failure handling mirrors `judge.py`'s `VisionJudge._call_model` exactly: a request failure is
logged once at warning level for a NEW consecutive-failure streak, silently (debug) for every
repeat while the streak continues, and the streak resets the moment a request next succeeds --
so a CoralHub outage (or a not-yet-configured one) costs one log line, not one per embed call.
`coral_identity.CoralRecognizer` is what actually decides to fall back to the histogram
recognizer when this client keeps failing; this module only ever returns `None` on failure, it
never raises past its own boundary.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Identifies every request on CoralHub's own Live activity dashboard (`app.client_identity`'s
# own `X-Client` priority) as "Kibble" regardless of the casing CoralHub's registered token name
# uses -- `client_identity.resolve_client` folds a matching header onto the token's own name.
CLIENT_HEADER = "kibble"

# CoralHub's own `EmbedRequest.images` cap (`app/schemas.py`) -- batching above this would just
# get the whole request rejected with a 422.
EMBED_BATCH_MAX = 32
# CoralHub itself funnels every TPU-touching call through one single-worker executor -- there is
# exactly one physical device (`app/README.md`) -- so concurrent batches only ever queue there
# regardless; a small pool here still lets one slow backfill batch not head-of-line-block an
# unrelated health check or a fresh event's own classify-time embed.
EMBED_CONCURRENCY = 2
REQUEST_TIMEOUT_S = 10.0
REQUEST_TIMEOUT = ClientTimeout(total=REQUEST_TIMEOUT_S)
MAX_RETRIES = 1  # one retry -- two attempts total per request, same as judge.py's _call_model


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """`GET /api/v1/health`'s own shape, trimmed to what `diagnostics.py` needs. `ok=False`
    with `error` set covers both a real device fault CoralHub itself reported and this client's
    own request failure (unreachable, timeout, malformed body) -- a diagnostics reader doesn't
    need those told apart, only whether Coral is currently usable."""

    ok: bool
    device_status: str | None = None
    temperature_c: float | None = None
    error: str | None = None


class CoralHubClient:
    """One instance per config entry, constructed only when `coralhub_url` is set
    (`__init__.py`). Every image is sent as base64 JPEG; every embedding comes back
    L2-normalised float32 (CoralHub's own contract, its README.md), handed back here as plain
    `list[float]` -- a caller that wants a `numpy` array or an `identity.pack`-style BLOB
    converts it itself, the same "wire format" vs. "what the model math wants" split
    `identity.py` already draws for its own face embeddings."""

    def __init__(self, hass: HomeAssistant, base_url: str, token: str) -> None:
        self._hass = hass
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._semaphore = asyncio.Semaphore(EMBED_CONCURRENCY)
        self._consecutive_failures = 0
        self.last_error: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"X-Client": CLIENT_HEADER}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def embed(self, model: str, images: list[bytes]) -> list[list[float]] | None:
        """Embeds `images` (raw JPEG bytes, any length) with `model`, transparently batching at
        `EMBED_BATCH_MAX` and running the resulting batches concurrently (bounded by
        `EMBED_CONCURRENCY`). One L2-normalised vector per image, in input order -- or `None`
        for the WHOLE call the moment any one batch fails, never a partial list a caller could
        silently misalign against its own `images`."""
        if not images:
            return []
        chunks = [images[i : i + EMBED_BATCH_MAX] for i in range(0, len(images), EMBED_BATCH_MAX)]
        results: list[list[list[float]] | None] = [None] * len(chunks)

        async def _run(i: int, chunk: list[bytes]) -> None:
            async with self._semaphore:
                results[i] = await self._embed_batch(model, chunk)

        await asyncio.gather(*(_run(i, chunk) for i, chunk in enumerate(chunks)))
        if any(r is None for r in results):
            return None
        out: list[list[float]] = []
        for r in results:
            assert r is not None  # narrowed by the `any(...)` check above
            out.extend(r)
        return out

    async def _embed_batch(self, model: str, images: list[bytes]) -> list[list[float]] | None:
        payload = {"model": model, "images": [base64.b64encode(jpeg).decode() for jpeg in images]}
        data = await self._request("POST", "/api/v1/embed", payload)
        if data is None:
            return None
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(images):
            return None
        return embeddings

    async def health(self) -> HealthStatus:
        """`GET /api/v1/health`, contributing to the SAME failure streak `embed` does -- both
        are "can I reach CoralHub right now" signals, and `CoralRecognizer` reads this one
        specifically to decide whether to even attempt a Coral-backed rebuild at all."""
        data = await self._request("GET", "/api/v1/health", None)
        if data is None:
            return HealthStatus(ok=False, error=self.last_error)
        device = data.get("device") or {}
        return HealthStatus(
            ok=bool(data.get("ok")),
            device_status=device.get("status"),
            temperature_c=device.get("temperature_c"),
            error=None if data.get("ok") else "CoralHub reports its device is not ready",
        )

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Mirrors `judge.py::VisionJudge._call_model`'s own retry/failure-streak shape exactly
        (see that method's docstring): `MAX_RETRIES` retries on top of the first attempt, each
        bounded by `REQUEST_TIMEOUT`, `None` on total failure -- logged once per NEW failure
        streak and silent for every repeat while it continues."""
        url = f"{self._base_url}{path}"
        session = async_get_clientsession(self._hass)
        headers = self._headers()
        last_err: Exception | None = None
        for _attempt in range(MAX_RETRIES + 1):
            try:
                if method == "POST":
                    resp_cm = session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
                else:
                    resp_cm = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                async with resp_cm as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            except (ClientError, TimeoutError, ValueError) as err:
                last_err = err
                continue
            self._consecutive_failures = 0
            self.last_error = None
            return data
        self._consecutive_failures += 1
        self.last_error = str(last_err)
        if self._consecutive_failures == 1:
            _LOGGER.warning(
                "CoralHub request failed, falling back to the histogram recognizer: %s", last_err
            )
        else:
            _LOGGER.debug(
                "CoralHub request failed (streak %d): %s", self._consecutive_failures, last_err
            )
        return None
