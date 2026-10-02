"""`coral_client.py`'s `CoralHubClient`: the `X-Client`/bearer-token headers actually sent,
transparent batching at `EMBED_BATCH_MAX` with order preserved across concurrently-dispatched
batches, retry-then-give-up on a transient failure, and the failure-streak logging shape mirrored
from `judge.py::VisionJudge._call_model` (see that module's own test file for the identical
pattern this one repeats against a different endpoint).
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from kibble.coral_client import CoralHubClient

# --- fakes ---------------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, json_data: Any = None, raise_exc: Exception | None = None) -> None:
        self._json_data = json_data
        self._raise_exc = raise_exc

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc

    async def json(self, content_type: Any = None) -> Any:
        return self._json_data

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeSession:
    """Queue-based fake mirroring `test_vision_judge.py`'s own `_FakeSession`, extended to
    record headers (the thing this client adds that `judge.py`'s never needed) and to keep a
    separate queue for GET (`/health`) vs POST (`/embed`)."""

    def __init__(self, responses: list[Any] | None = None, get_responses: list[Any] | None = None) -> None:
        self._responses = list(responses or [])
        self._get_responses = list(get_responses or [])
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def post(self, url: str, json: Any = None, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url: str, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        self.get_calls.append({"url": url, "headers": headers})
        item = self._get_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _EchoSession:
    """Every POST /embed answers with one 1-D vector per image, its value the DECODED byte
    length of that image -- computed from the request itself rather than a canned queue, so
    the batching test can verify per-image order survives concurrent batch dispatch without
    depending on which chunk's HTTP call happens to land first."""

    def __init__(self) -> None:
        self.post_calls: list[list[str]] = []

    def post(self, url: str, json: Any = None, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        images = json["images"]
        self.post_calls.append(images)
        embeddings = [[float(len(base64.b64decode(s)))] for s in images]
        return _FakeResponse(json_data={"model": json["model"], "dim": 1, "embeddings": embeddings})


def _client(monkeypatch: pytest.MonkeyPatch, session: Any, *, token: str = "tok") -> CoralHubClient:
    monkeypatch.setattr("kibble.coral_client.async_get_clientsession", lambda hass: session)
    return CoralHubClient(None, "http://coralhub.local:8720", token)


def _embed_ok(vec: list[float] = [0.5]) -> _FakeResponse:
    return _FakeResponse(json_data={"model": "m", "dim": len(vec), "embeddings": [vec]})


# --- headers / token -------------------------------------------------------------------------


async def test_embed_sends_client_header_and_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(responses=[_embed_ok()])
    client = _client(monkeypatch, session, token="secret-token")

    result = await client.embed("m", [b"jpeg"])

    assert result == [[0.5]]
    call = session.post_calls[0]
    assert call["url"] == "http://coralhub.local:8720/api/v1/embed"
    assert call["headers"]["X-Client"] == "kibble"
    assert call["headers"]["Authorization"] == "Bearer secret-token"


async def test_embed_omits_authorization_header_when_no_token_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(responses=[_embed_ok()])
    client = _client(monkeypatch, session, token="")

    await client.embed("m", [b"jpeg"])

    headers = session.post_calls[0]["headers"]
    assert headers["X-Client"] == "kibble"
    assert "Authorization" not in headers


async def test_health_sends_the_same_client_header_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(get_responses=[_FakeResponse(json_data={
        "ok": True, "device": {"status": "ALIVE", "temperature_c": 46.5}, "runtime": {},
    })])
    client = _client(monkeypatch, session, token="secret-token")

    await client.health()

    call = session.get_calls[0]
    assert call["url"] == "http://coralhub.local:8720/api/v1/health"
    assert call["headers"]["X-Client"] == "kibble"
    assert call["headers"]["Authorization"] == "Bearer secret-token"


# --- batching --------------------------------------------------------------------------------


async def test_embed_returns_empty_list_for_no_images_without_making_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(responses=[])
    client = _client(monkeypatch, session)

    assert await client.embed("m", []) == []
    assert session.post_calls == []


async def test_embed_batches_at_32_images_and_preserves_order_across_concurrent_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _EchoSession()
    client = _client(monkeypatch, session)
    # 40 images of distinct lengths (1..40 bytes) so each one's echoed-back length uniquely
    # identifies it -- proves the final list lines up with the input regardless of which of
    # the two concurrently-dispatched batches (32 + 8) answers first.
    images = [bytes([i % 256]) * (i + 1) for i in range(40)]

    result = await client.embed("m", images)

    assert result is not None
    assert [vec[0] for vec in result] == [float(len(img)) for img in images]
    assert sorted(len(call) for call in session.post_calls) == [8, 32]


async def test_embed_returns_none_for_the_whole_call_when_any_batch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp import ClientError

    class _MixedSession:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, url: str, json: Any = None, headers: Any = None, timeout: Any = None) -> _FakeResponse:
            self.calls += 1
            if len(json["images"]) == 32:
                return _FakeResponse(json_data={
                    "model": "m", "dim": 1, "embeddings": [[0.0]] * 32,
                })
            raise ClientError("second batch down")

    session = _MixedSession()
    monkeypatch.setattr("kibble.coral_client.async_get_clientsession", lambda hass: session)
    client = CoralHubClient(None, "http://coralhub.local:8720", "tok")

    result = await client.embed("m", [b"x"] * 40)

    assert result is None
    assert session.calls == 2 + 1  # the 32-batch (one call) plus the failing 8-batch's 2 attempts


async def test_embed_mismatched_embedding_count_is_a_malformed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(responses=[_FakeResponse(json_data={"model": "m", "dim": 1, "embeddings": [[0.1]]})])
    client = _client(monkeypatch, session)

    result = await client.embed("m", [b"a", b"b"])  # 2 images sent, only 1 embedding answered

    assert result is None


# --- retry / failure streak (mirrors judge.py's own _call_model tests) ------------------------


async def test_embed_retries_once_after_a_transient_failure_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    session = _FakeSession(responses=[ClientError("connection reset"), _embed_ok([0.7])])
    client = _client(monkeypatch, session)

    result = await client.embed("m", [b"jpeg"])

    assert result == [[0.7]]
    assert len(session.post_calls) == 2
    assert client._consecutive_failures == 0 and client.last_error is None


async def test_embed_gives_up_quietly_after_exhausting_its_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    session = _FakeSession(responses=[ClientError("down"), ClientError("still down")])
    client = _client(monkeypatch, session)

    result = await client.embed("m", [b"jpeg"])

    assert result is None
    assert len(session.post_calls) == 2
    assert client._consecutive_failures == 1
    assert client.last_error == "still down"


async def test_a_non_2xx_status_counts_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    session = _FakeSession(responses=[
        _FakeResponse(raise_exc=ClientError("500")), _FakeResponse(raise_exc=ClientError("500")),
    ])
    client = _client(monkeypatch, session)

    assert await client.embed("m", [b"jpeg"]) is None


async def test_failure_streak_increments_across_calls_and_resets_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    client = _client(monkeypatch, _FakeSession(responses=[ClientError("a"), ClientError("b")]))
    await client.embed("m", [b"x"])
    assert client._consecutive_failures == 1

    monkeypatch.setattr(
        "kibble.coral_client.async_get_clientsession",
        lambda hass: _FakeSession(responses=[ClientError("c"), ClientError("d")]),
    )
    await client.embed("m", [b"x"])
    assert client._consecutive_failures == 2

    monkeypatch.setattr(
        "kibble.coral_client.async_get_clientsession",
        lambda hass: _FakeSession(responses=[_embed_ok([0.0])]),
    )
    result = await client.embed("m", [b"x"])
    assert result == [[0.0]]
    assert client._consecutive_failures == 0 and client.last_error is None


# --- health --------------------------------------------------------------------------------


async def test_health_ok_reports_device_status_and_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(get_responses=[_FakeResponse(json_data={
        "ok": True,
        "device": {"path": "/dev/apex_0", "status": "ALIVE", "temperature_c": 46.5},
        "runtime": {"libedgetpu": "16.0", "pycoral": "2.0.0", "tflite_runtime": "2.5.0.post1"},
    })])
    client = _client(monkeypatch, session)

    status = await client.health()

    assert status.ok is True
    assert status.device_status == "ALIVE"
    assert status.temperature_c == 46.5
    assert status.error is None


async def test_health_reports_not_ok_when_coralhub_itself_says_the_device_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(get_responses=[_FakeResponse(json_data={
        "ok": False, "device": {"status": "DOWN"}, "runtime": {},
    })])
    client = _client(monkeypatch, session)

    status = await client.health()

    assert status.ok is False
    assert status.device_status == "DOWN"
    assert status.error is not None


async def test_health_is_not_ok_when_coralhub_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import ClientError

    session = _FakeSession(get_responses=[ClientError("refused"), ClientError("refused again")])
    client = _client(monkeypatch, session)

    status = await client.health()

    assert status.ok is False
    assert status.error is not None
