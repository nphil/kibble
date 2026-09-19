"""Cat-identification HA-side logic: the label-select's option-to-bucket mapping and its
guard against acting with nothing pending, the pending-face image's cache-busting URL, the
per-cat presence window, and the coordinator's cat/face-sample write methods (label, unlabel,
delete cat, upload sample, delete sample). Mirrors `test_coordinator_ble_wiring.py`'s style:
duck-typed `self` stand-ins for the one seam worth pinning, rather than constructing a real
`HomeAssistant` core instance this repo has no fixture for and these tests don't need.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from kibble.api import DetectionEvent, IdentifyResult, KibbleError, ReviewFace
from kibble.binary_sensor import PRESENCE_WINDOW, KibbleCatPresentBinarySensor, is_present, last_seen
from kibble.const import parse_vendor_pet_ids
from kibble.coordinator import KibbleCoordinator, Sighting, sightings
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


# --- coordinator.async_label_face / async_unlabel_face -----------------------------------------


def _fake_coordinator(client: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(client=client, async_refresh=AsyncMock())


async def test_async_label_face_labels_then_refreshes_immediately() -> None:
    client = AsyncMock()
    fake_self = _fake_coordinator(client)
    await KibbleCoordinator.async_label_face(fake_self, "1-5.jpg", "Kitty")
    client.label_face.assert_awaited_once_with("1-5.jpg", "Kitty")
    fake_self.async_refresh.assert_awaited_once()


async def test_async_unlabel_face_is_the_exact_inverse_call_shape() -> None:
    """`kibble.unlabel_face`'s own wire field is `name`, not `crop_id` -- confirming the
    coordinator still calls the client with (crop_id, cat) positionally, matching `label_face`,
    regardless of what the HA-facing service schema calls the first argument."""
    client = AsyncMock()
    fake_self = _fake_coordinator(client)
    await KibbleCoordinator.async_unlabel_face(fake_self, "1-5.jpg", "Kitty")
    client.unlabel_face.assert_awaited_once_with("1-5.jpg", "Kitty")
    fake_self.async_refresh.assert_awaited_once()


async def test_async_delete_cat_deletes_then_refreshes_immediately() -> None:
    client = AsyncMock(delete_cat=AsyncMock(return_value={}))
    fake_self = _fake_coordinator(client)
    result = await KibbleCoordinator.async_delete_cat(fake_self, "Ghost")
    client.delete_cat.assert_awaited_once_with("Ghost")
    fake_self.async_refresh.assert_awaited_once()
    assert result == {}


async def test_async_upload_face_sample_uploads_then_returns_the_agent_result() -> None:
    client = AsyncMock(
        upload_face_sample=AsyncMock(return_value={"name": "upload-1.jpg", "samples": 3})
    )
    fake_self = _fake_coordinator(client)
    result = await KibbleCoordinator.async_upload_face_sample(fake_self, "Kitty", b"jpeg-bytes")
    client.upload_face_sample.assert_awaited_once_with("Kitty", b"jpeg-bytes")
    fake_self.async_refresh.assert_awaited_once()
    assert result == {"name": "upload-1.jpg", "samples": 3}


async def test_async_delete_face_sample_deletes_then_refreshes() -> None:
    client = AsyncMock(delete_face_sample=AsyncMock(return_value={}))
    fake_self = _fake_coordinator(client)
    result = await KibbleCoordinator.async_delete_face_sample(fake_self, "Kitty", "upload-1.jpg")
    client.delete_face_sample.assert_awaited_once_with("Kitty", "upload-1.jpg")
    fake_self.async_refresh.assert_awaited_once()
    assert result == {}



# --- image._image_url -------------------------------------------------------------------------


def test_image_url_is_none_when_nothing_has_ever_been_captured() -> None:
    assert _image_url(_entry(), ReviewFace(status="none", name=None, cat=None), 0) is None


def test_image_url_includes_host_port_and_a_cache_busting_id() -> None:
    url = _image_url(_entry(), ReviewFace(status="pending", name="1-unknown.jpg", cat=None), 1)
    assert url == f"http://{HOST}:{PORT}/faces/current?id=pending-1-unknown.jpg-1"


def test_image_url_changes_when_the_same_named_crop_transitions_to_labelled() -> None:
    """Even though `GET /faces/current` would serve byte-identical content for the same
    -- the entity's *meaning* (awaiting review vs. already reviewed) genuinely changed."""
    pending_url = _image_url(
        _entry(), ReviewFace(status="pending", name="1-unknown.jpg", cat=None), 1
    )
    labelled_url = _image_url(
        _entry(), ReviewFace(status="labelled", name="1-unknown.jpg", cat="Rashy"), 0
    )
    assert pending_url != labelled_url


def test_image_url_changes_when_only_the_pending_count_changes() -> None:
    """A new crop arriving behind the current one (or an unlabel that isn't the current one)
    changes the pending count without changing which crop `review_face` names -- cards
    watching this entity's state to know when to refetch the pending list need that count
    folded in, or a same-review-face mutation would never bump the cache key."""
    review = ReviewFace(status="pending", name="1-unknown.jpg", cat=None)
    assert _image_url(_entry(), review, 1) != _image_url(_entry(), review, 2)


# --- binary_sensor.is_present -------------------------------------------------------------------

_NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def _ts_seconds_ago(seconds: int) -> int:
    return int((_NOW.timestamp())) - seconds


def _sighting(cat: str | None, ts: int, pet_id: str | None = "101320712") -> Sighting:
    return Sighting(ts=ts, pet_id=pet_id, cat=cat, total_score=None)


def test_is_present_true_for_a_recent_sighting() -> None:
    assert is_present("Rashy", (_sighting("Rashy", _ts_seconds_ago(60)),), _NOW) is True


def test_is_present_false_for_a_different_cat() -> None:
    assert is_present("Rashy", (_sighting("Ghost", _ts_seconds_ago(60)),), _NOW) is False


def test_is_present_false_once_outside_the_presence_window() -> None:
    old = _sighting("Rashy", _ts_seconds_ago(int(PRESENCE_WINDOW.total_seconds()) + 60))
    assert is_present("Rashy", (old,), _NOW) is False


def test_is_present_true_just_inside_the_presence_window_boundary() -> None:
    edge = _sighting("Rashy", _ts_seconds_ago(int(PRESENCE_WINDOW.total_seconds()) - 1))
    assert is_present("Rashy", (edge,), _NOW) is True


def test_is_present_ignores_an_unmapped_vendor_sighting() -> None:
    """An id with no `vendor_pet_ids` entry names nobody, so it makes nobody present."""
    assert is_present("Kitty", (_sighting(None, _ts_seconds_ago(60)),), _NOW) is False


def test_presence_ignores_the_pending_crop_classifier_entirely() -> None:
    """`GET /identify` describes the review queue, not a visit: a cat whose crop was just
    labelled is not thereby "here", and that conflation is what put "Last here 17 hours ago"
    on a cat nobody had seen (and "Not seen yet" on one that had just eaten)."""
    assert is_present("Kitty", (), _NOW) is False
    assert last_seen("Kitty", ()) is None


def test_last_seen_is_the_newest_sighting_of_that_cat() -> None:
    seen = last_seen("Kitty", (_sighting("Kitty", _ts_seconds_ago(600)), _sighting("Kitty", _ts_seconds_ago(90)),
                               _sighting("Pancake", _ts_seconds_ago(10))))
    assert seen is not None and (_NOW - seen).total_seconds() == pytest.approx(90, abs=1)


def test_last_seen_none_when_never_identified() -> None:
    assert last_seen("Kitty", (_sighting("Pancake", _ts_seconds_ago(5)),)) is None


def test_cat_present_last_seen_attribute_prefers_a_newer_live_sighting_over_restored() -> None:
    restored = _NOW.replace(hour=0, minute=0, second=0)  # earlier the same day
    live = _sighting("Kitty", _ts_seconds_ago(30))
    sensor = SimpleNamespace(
        _cat_name="Kitty",
        _restored_last_seen=restored,
        coordinator=SimpleNamespace(data=SimpleNamespace(sightings=(live,))),
    )
    sensor._live_last_seen = lambda: KibbleCatPresentBinarySensor._live_last_seen(sensor)
    attrs = KibbleCatPresentBinarySensor.extra_state_attributes.fget(sensor)
    assert attrs["last_seen"] == datetime.fromtimestamp(live.ts, tz=timezone.utc).isoformat()


def test_cat_present_last_seen_attribute_falls_back_to_restored_with_no_live_sighting() -> None:
    restored = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
    sensor = SimpleNamespace(
        _cat_name="Kitty",
        _restored_last_seen=restored,
        coordinator=SimpleNamespace(data=SimpleNamespace(identify=_identify(None, None), sightings=())),
    )
    sensor._live_last_seen = lambda: KibbleCatPresentBinarySensor._live_last_seen(sensor)
    attrs = KibbleCatPresentBinarySensor.extra_state_attributes.fget(sensor)
    assert attrs["last_seen"] == restored.isoformat()

# --- coordinator.sightings / const.parse_vendor_pet_ids --------------------------------


def _event(seq: int, ts: int, cls: str, pet_id: str | None, total_score: float | None = None) -> DetectionEvent:
    return DetectionEvent(
        seq=seq, ts=ts, cls=cls, image=None, cat=None, score=None, pet_id=pet_id, total_score=total_score
    )


def test_sightings_keeps_only_track_events_in_time_order_and_maps_names() -> None:
    events = (
        _event(3, 300, "track", "101320712", 1531.2),
        _event(1, 100, "visit", None),
        _event(2, 200, "track", "5"),
    )
    got = sightings(events, {"101320712": "Kitty"})
    assert [s.ts for s in got] == [200, 300]
    assert got[0].cat is None and got[0].pet_id == "5"
    assert got[1].cat == "Kitty" and got[1].total_score == 1531.2


def test_sightings_include_rows_the_librefeed_classifier_named() -> None:
    """LibreFeed has no vendor `track` events at all -- it names the visit/eat row itself.
    Those rows were dropped here until 2026-09-19, leaving the cat tiles with no sighting to
    show however many cats the feeder identified."""
    named = DetectionEvent(
        seq=7, ts=700, cls="visit", image="x.jpg", cat="Pancake", score=0.71, pet_id=None, total_score=None
    )
    unnamed = _event(6, 600, "visit", None)
    got = sightings((unnamed, named), {})
    assert [(s.ts, s.cat, s.pet_id) for s in got] == [(700, "Pancake", None)]


def test_parse_vendor_pet_ids_accepts_spaces_and_a_trailing_comma() -> None:
    assert parse_vendor_pet_ids(" 101320712 = Kitty ,5=Pancake, ") == {"101320712": "Kitty", "5": "Pancake"}
    assert parse_vendor_pet_ids("") == {}


@pytest.mark.parametrize("raw", ["Kitty", "abc=Kitty", "101320712=", "=Kitty"])
def test_parse_vendor_pet_ids_rejects_malformed_entries(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_vendor_pet_ids(raw)
