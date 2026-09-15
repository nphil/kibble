"""Cat-identification HA-side logic: the label-select's option-to-bucket mapping and its
guard against acting with nothing pending, the pending-face image's cache-busting URL, and the
per-cat presence window. Mirrors `test_coordinator_ble_wiring.py`'s style: duck-typed `self`
stand-ins for the one seam worth pinning, rather than constructing a real `HomeAssistant` core
instance this repo has no fixture for and these tests don't need.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from kibble.api import IdentifyResult, KibbleError, ReviewFace
from kibble.binary_sensor import PRESENCE_WINDOW, is_present
from kibble.image import _image_url
from kibble.select import KibbleLabelFaceSelect, cat_for_option

HOST = "192.168.4.85"
PORT = 8765


def _identify(cat: str | None, ts: int | None) -> IdentifyResult:
    return IdentifyResult(cat=cat, score=None, second_best=None, crop=None, source=None, ts=ts)


def _entry(host: str = HOST, port: int = PORT) -> SimpleNamespace:
    return SimpleNamespace(data={"host": host, "port": port})


# --- select.cat_for_option -------------------------------------------------------------------


def test_cat_for_option_maps_skip_to_the_other_bucket() -> None:
    assert cat_for_option("Skip") == "other"


def test_cat_for_option_maps_not_a_cat_to_its_bucket() -> None:
    assert cat_for_option("Not a cat") == "not_a_cat"


def test_cat_for_option_passes_through_a_real_cat_name_unchanged() -> None:
    assert cat_for_option("Rashy") == "Rashy"


# --- select.KibbleLabelFaceSelect.async_select_option -----------------------------------------


def _fake_select(review: ReviewFace, label_face: AsyncMock | None = None) -> SimpleNamespace:
    """A stand-in for `self` with just what `async_select_option` reads: `.coordinator.data`
    and `.coordinator.async_label_face`."""
    return SimpleNamespace(
        coordinator=SimpleNamespace(
            data=SimpleNamespace(review_face=review),
            async_label_face=label_face or AsyncMock(),
        )
    )


async def test_select_option_labels_the_pending_crop_with_the_mapped_cat() -> None:
    label_face = AsyncMock()
    fake_self = _fake_select(ReviewFace(status="pending", name="1-unknown.jpg", cat=None), label_face)

    await KibbleLabelFaceSelect.async_select_option(fake_self, "Skip")

    label_face.assert_awaited_once_with("1-unknown.jpg", "other")


async def test_select_option_passes_a_real_cat_name_through_unchanged() -> None:
    label_face = AsyncMock()
    fake_self = _fake_select(ReviewFace(status="pending", name="2-unknown.jpg", cat=None), label_face)

    await KibbleLabelFaceSelect.async_select_option(fake_self, "Rashy")

    label_face.assert_awaited_once_with("2-unknown.jpg", "Rashy")


async def test_select_option_raises_when_the_review_queue_is_not_pending() -> None:
    fake_self = _fake_select(ReviewFace(status="labelled", name="3-unknown.jpg", cat="Rashy"))

    with pytest.raises(HomeAssistantError, match="No pending face"):
        await KibbleLabelFaceSelect.async_select_option(fake_self, "Rashy")


async def test_select_option_raises_when_there_is_no_crop_at_all() -> None:
    fake_self = _fake_select(ReviewFace(status="none", name=None, cat=None))

    with pytest.raises(HomeAssistantError, match="No pending face"):
        await KibbleLabelFaceSelect.async_select_option(fake_self, "Rashy")


async def test_select_option_wraps_a_kibble_error_as_a_home_assistant_error() -> None:
    label_face = AsyncMock(side_effect=KibbleError("agent unreachable"))
    fake_self = _fake_select(ReviewFace(status="pending", name="4-unknown.jpg", cat=None), label_face)

    with pytest.raises(HomeAssistantError, match="agent unreachable"):
        await KibbleLabelFaceSelect.async_select_option(fake_self, "Rashy")


# --- image._image_url -------------------------------------------------------------------------


def test_image_url_is_none_when_nothing_has_ever_been_captured() -> None:
    assert _image_url(_entry(), ReviewFace(status="none", name=None, cat=None)) is None


def test_image_url_includes_host_port_and_a_cache_busting_id() -> None:
    url = _image_url(_entry(), ReviewFace(status="pending", name="1-unknown.jpg", cat=None))
    assert url == f"http://{HOST}:{PORT}/faces/current?id=pending-1-unknown.jpg"


def test_image_url_changes_when_the_same_named_crop_transitions_to_labelled() -> None:
    """Even though `GET /faces/current` would serve byte-identical content for the same
    filename either way, the URL must still change so `ImageEntity` bumps `image_last_updated`
    -- the entity's *meaning* (awaiting review vs. already reviewed) genuinely changed."""
    pending_url = _image_url(_entry(), ReviewFace(status="pending", name="1-unknown.jpg", cat=None))
    labelled_url = _image_url(
        _entry(), ReviewFace(status="labelled", name="1-unknown.jpg", cat="Rashy")
    )
    assert pending_url != labelled_url


# --- binary_sensor.is_present -------------------------------------------------------------------

_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def _ts_seconds_ago(seconds: int) -> int:
    return int((_NOW.timestamp())) - seconds


def test_is_present_true_for_a_recent_matching_identification() -> None:
    identify = _identify("Rashy", _ts_seconds_ago(60))
    assert is_present("Rashy", identify, _NOW) is True


def test_is_present_false_for_a_different_cat() -> None:
    identify = _identify("Ghost", _ts_seconds_ago(60))
    assert is_present("Rashy", identify, _NOW) is False


def test_is_present_false_with_no_timestamp() -> None:
    identify = _identify("Rashy", None)
    assert is_present("Rashy", identify, _NOW) is False


def test_is_present_false_once_outside_the_presence_window() -> None:
    identify = _identify("Rashy", _ts_seconds_ago(int(PRESENCE_WINDOW.total_seconds()) + 60))
    assert is_present("Rashy", identify, _NOW) is False


def test_is_present_true_just_inside_the_presence_window_boundary() -> None:
    identify = _identify("Rashy", _ts_seconds_ago(int(PRESENCE_WINDOW.total_seconds()) - 1))
    assert is_present("Rashy", identify, _NOW) is True
