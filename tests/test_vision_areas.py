"""Typed client coverage for the daemon detection-area and bowl-zone contract."""

from __future__ import annotations

from typing import Any, Self

from kibble.api import KibbleClient, VisionAreas

HOST = "192.168.4.85"
PORT = 8765


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: str | None = None) -> Any:
        return self._body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingSession:
    def __init__(self, body: Any) -> None:
        self.body = body
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, url, kwargs.get("json")))
        return _FakeResponse(200, self.body)


async def test_vision_areas_get_is_typed_and_uses_daemon_route() -> None:
    body = {"exclude": [[0.3, 0.4, 0.5, 0.6]]}
    session = _RecordingSession(body)

    areas = await KibbleClient(session, HOST, PORT).vision_areas()

    assert areas == VisionAreas(exclude=body["exclude"])
    assert session.calls == [("GET", f"http://{HOST}:{PORT}/vision/areas", None)]


async def test_vision_areas_get_ignores_a_stale_include_key_from_an_older_agent() -> None:
    """An agent that hasn't picked up the daemon's own include-removal yet may still echo an
    `"include"` key on `/vision/areas` -- confirms that key is simply ignored, not a parse
    failure, so a mixed-version rollout (new card, old daemon) never breaks."""
    body = {"include": [[0.1, 0.2, 0.8, 0.9]], "exclude": [[0.3, 0.4, 0.5, 0.6]]}
    session = _RecordingSession(body)

    areas = await KibbleClient(session, HOST, PORT).vision_areas()

    assert areas == VisionAreas(exclude=[[0.3, 0.4, 0.5, 0.6]])
    assert not hasattr(areas, "include")


async def test_vision_areas_set_sends_only_exclude() -> None:
    exclude = [[0.3, 0.4, 0.5, 0.6]]
    session = _RecordingSession({"exclude": exclude})

    areas = await KibbleClient(session, HOST, PORT).set_vision_areas(exclude)

    assert areas == VisionAreas(exclude=exclude)
    assert session.calls == [("POST", f"http://{HOST}:{PORT}/vision/areas", {"exclude": exclude})]


async def test_vision_bowl_roi_reads_the_field_off_the_full_vision_config() -> None:
    session = _RecordingSession(
        {"enabled": True, "bowl_roi": [0.25, 0.6, 0.55, 1.0], "body_exclusion_rois": []}
    )

    bowl_roi = await KibbleClient(session, HOST, PORT).vision_bowl_roi()

    assert bowl_roi == [0.25, 0.6, 0.55, 1.0]
    assert session.calls == [("GET", f"http://{HOST}:{PORT}/vision", None)]


async def test_vision_bowl_roi_falls_back_to_empty_for_an_agent_predating_the_field() -> None:
    session = _RecordingSession({"enabled": True})

    bowl_roi = await KibbleClient(session, HOST, PORT).vision_bowl_roi()

    assert bowl_roi == []


async def test_set_vision_bowl_roi_merges_through_post_vision() -> None:
    roi = [0.25, 0.6, 0.55, 1.0]
    session = _RecordingSession({"enabled": True, "bowl_roi": roi})

    result = await KibbleClient(session, HOST, PORT).set_vision_bowl_roi(roi)

    assert result == roi
    assert session.calls == [("POST", f"http://{HOST}:{PORT}/vision", {"bowl_roi": roi})]
