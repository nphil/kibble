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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from kibble.api import DetectionEvent, IdentifyResult, KibbleError, ReviewFace
from kibble.binary_sensor import PRESENCE_WINDOW, is_present
from kibble.const import parse_vendor_pet_ids
from kibble.coordinator import VendorSighting, vendor_sightings
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

    with pytest.raises(ServiceValidationError) as excinfo:
        await KibbleLabelFaceSelect.async_select_option(fake_self, "Rashy")
    assert excinfo.value.translation_key == "no_pending_face"


async def test_select_option_raises_when_there_is_no_crop_at_all() -> None:
    fake_self = _fake_select(ReviewFace(status="none", name=None, cat=None))

    with pytest.raises(ServiceValidationError) as excinfo:
        await KibbleLabelFaceSelect.async_select_option(fake_self, "Rashy")
    assert excinfo.value.translation_key == "no_pending_face"


async def test_select_option_wraps_a_kibble_error_as_a_home_assistant_error() -> None:
    label_face = AsyncMock(side_effect=KibbleError("agent unreachable"))
    fake_self = _fake_select(ReviewFace(status="pending", name="4-unknown.jpg", cat=None), label_face)

    with pytest.raises(HomeAssistantError) as excinfo:
        await KibbleLabelFaceSelect.async_select_option(fake_self, "Rashy")
    assert excinfo.value.translation_key == "agent_action_failed"
    assert excinfo.value.translation_placeholders == {"action": "Label", "error": "agent unreachable"}



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
    assert is_present("Rashy", identify, (), _NOW) is True


def test_is_present_false_for_a_different_cat() -> None:
    identify = _identify("Ghost", _ts_seconds_ago(60))
    assert is_present("Rashy", identify, (), _NOW) is False


def test_is_present_false_with_no_timestamp() -> None:
    identify = _identify("Rashy", None)
    assert is_present("Rashy", identify, (), _NOW) is False


def test_is_present_false_once_outside_the_presence_window() -> None:
    identify = _identify("Rashy", _ts_seconds_ago(int(PRESENCE_WINDOW.total_seconds()) + 60))
    assert is_present("Rashy", identify, (), _NOW) is False


def test_is_present_true_just_inside_the_presence_window_boundary() -> None:
    identify = _identify("Rashy", _ts_seconds_ago(int(PRESENCE_WINDOW.total_seconds()) - 1))
    assert is_present("Rashy", identify, (), _NOW) is True


def _sighting(cat: str | None, ts: int, pet_id: str = "101320712") -> VendorSighting:
    return VendorSighting(ts=ts, pet_id=pet_id, cat=cat, total_score=None)


def test_is_present_true_from_a_recent_vendor_sighting_alone() -> None:
    """The classifier has never seen this cat, but the vendor's own identifier has."""
    identify = _identify("Ghost", _ts_seconds_ago(60))
    sightings = (_sighting("Kitty", _ts_seconds_ago(90)),)
    assert is_present("Kitty", identify, sightings, _NOW) is True


def test_is_present_ignores_an_unmapped_vendor_sighting() -> None:
    """An id with no `vendor_pet_ids` entry names nobody, so it makes nobody present."""
    identify = _identify(None, None)
    sightings = (_sighting(None, _ts_seconds_ago(60)),)
    assert is_present("Kitty", identify, sightings, _NOW) is False


def test_is_present_false_once_a_vendor_sighting_ages_out() -> None:
    identify = _identify(None, None)
    old = _sighting("Kitty", _ts_seconds_ago(int(PRESENCE_WINDOW.total_seconds()) + 1))
    assert is_present("Kitty", identify, (old,), _NOW) is False


# --- coordinator.vendor_sightings / const.parse_vendor_pet_ids --------------------------------


def _event(seq: int, ts: int, cls: str, pet_id: str | None, total_score: float | None = None) -> DetectionEvent:
    return DetectionEvent(
        seq=seq, ts=ts, cls=cls, image=None, cat=None, score=None, pet_id=pet_id, total_score=total_score
    )


def test_vendor_sightings_keeps_only_track_events_in_time_order_and_maps_names() -> None:
    events = (
        _event(3, 300, "track", "101320712", 1531.2),
        _event(1, 100, "visit", None),
        _event(2, 200, "track", "5"),
    )
    got = vendor_sightings(events, {"101320712": "Kitty"})
    assert [s.ts for s in got] == [200, 300]
    assert got[0].cat is None and got[0].pet_id == "5"
    assert got[1].cat == "Kitty" and got[1].total_score == 1531.2


def test_parse_vendor_pet_ids_accepts_spaces_and_a_trailing_comma() -> None:
    assert parse_vendor_pet_ids(" 101320712 = Kitty ,5=Pancake, ") == {"101320712": "Kitty", "5": "Pancake"}
    assert parse_vendor_pet_ids("") == {}


@pytest.mark.parametrize("raw", ["Kitty", "abc=Kitty", "101320712=", "=Kitty"])
def test_parse_vendor_pet_ids_rejects_malformed_entries(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_vendor_pet_ids(raw)
