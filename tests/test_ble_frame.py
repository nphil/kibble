"""`kibble/frame.py`: the Kibble BLE feed-frame codec.

No Home Assistant or bleak dependency -- see `tests/conftest.py` for how `kibble` resolves
without running the package's real `__init__.py`.
"""

from __future__ import annotations

import base64
import json

import pytest
from kibble import frame


def test_round_trip_preserves_every_field() -> None:
    encoded = frame.encode_feed_frame(
        cancel=False, feed_id="kibbletest1", amount1=5, amount2=3
    )
    decoded = frame.decode_feed_frame(encoded)

    assert decoded == frame.FeedFrame(
        cancel=False, feed_id="kibbletest1", amount1=5, amount2=3
    )


def test_round_trip_carries_cancel_flag() -> None:
    encoded = frame.encode_feed_frame(cancel=True, feed_id="x", amount1=0, amount2=0)
    assert frame.decode_feed_frame(encoded).cancel is True


def test_wire_bytes_match_the_proven_struct_layout() -> None:
    """docs/14-feed-test.md's live-dispensing test sent this exact 67-byte struct for
    `feed:1:0:kibbletest1`: cancel=0, id="kibbletest1" then zero-padded to 64, amount1=1,
    amount2=0. Kibble's frame must carry the identical struct bytes as its base64 payload --
    this is the whole point of reusing the proven struct instead of inventing a new one."""
    encoded = frame.encode_feed_frame(cancel=False, feed_id="kibbletest1", amount1=1, amount2=0)
    obj = json.loads(encoded)
    payload = base64.b64decode(obj["payload"])

    expected = bytes([0]) + b"kibbletest1" + bytes(64 - len("kibbletest1")) + bytes([1, 0])
    assert payload == expected
    assert len(payload) == 67


def test_type_is_outside_the_five_vendor_values() -> None:
    obj = json.loads(frame.encode_feed_frame(amount1=1, amount2=0))
    assert obj["type"] == frame.FEED_TYPE
    assert obj["type"] not in frame.VENDOR_TYPES
    # Every known vendor type fits in one byte; Kibble's does not, so it can never collide
    # even if the vendor allocates more single-byte types later.
    assert obj["type"] > 0xFF


@pytest.mark.parametrize("vendor_type", sorted(frame.VENDOR_TYPES))
def test_decoder_rejects_vendor_frames(vendor_type: int) -> None:
    """A frame using one of the five *stock* type values must not be mistaken for one of
    Kibble's own, in either direction -- proves the discriminator is actually load-bearing."""
    payload = base64.b64encode(bytes(67)).decode("ascii")
    forged = json.dumps({"type": vendor_type, "len": 67, "crc16": 0, "payload": payload}).encode()

    with pytest.raises(frame.FrameError, match="not a Kibble feed frame"):
        frame.decode_feed_frame(forged)


def test_decoder_rejects_corrupted_payload() -> None:
    encoded = bytearray(frame.encode_feed_frame(feed_id="abc", amount1=9, amount2=1))
    # Flip a byte inside the base64 payload text without touching type/len/crc16, simulating
    # a bit-flip in transit.
    obj = json.loads(bytes(encoded))
    raw = bytearray(base64.b64decode(obj["payload"]))
    raw[10] ^= 0xFF
    obj["payload"] = base64.b64encode(bytes(raw)).decode("ascii")
    corrupted = json.dumps(obj).encode()

    with pytest.raises(frame.FrameError, match="checksum mismatch"):
        frame.decode_feed_frame(corrupted)


def test_decoder_rejects_truncated_payload() -> None:
    obj = json.loads(frame.encode_feed_frame(amount1=1, amount2=0))
    payload = base64.b64decode(obj["payload"])[:-1]  # drop the last byte
    obj["payload"] = base64.b64encode(payload).decode("ascii")
    obj["len"] = len(payload)
    truncated = json.dumps(obj).encode()

    with pytest.raises(frame.FrameError, match="feed_ctrl is 67"):
        frame.decode_feed_frame(truncated)


def test_decoder_rejects_len_field_mismatch() -> None:
    obj = json.loads(frame.encode_feed_frame(amount1=1, amount2=0))
    obj["len"] = obj["len"] + 1
    tampered = json.dumps(obj).encode()

    with pytest.raises(frame.FrameError, match="'len' says"):
        frame.decode_feed_frame(tampered)


def test_decoder_rejects_non_json() -> None:
    with pytest.raises(frame.FrameError, match="not valid UTF-8 JSON"):
        frame.decode_feed_frame(b"not json at all")


def test_decoder_rejects_json_that_is_not_an_object() -> None:
    with pytest.raises(frame.FrameError, match="not an object"):
        frame.decode_feed_frame(b"[1, 2, 3]")


def test_decoder_rejects_missing_payload_field() -> None:
    forged = json.dumps({"type": frame.FEED_TYPE, "len": 67, "crc16": 0}).encode()
    with pytest.raises(frame.FrameError, match="no 'payload' field"):
        frame.decode_feed_frame(forged)


@pytest.mark.parametrize(
    ("hopper", "expected"),
    [("1", (7, 0)), ("2", (0, 7)), ("both", (7, 7))],
)
def test_hopper_amounts_matches_agent_translation(
    hopper: str, expected: tuple[int, int]
) -> None:
    """Must match `agent/src/main.rs::feed()`'s match arms exactly: the BLE path bypasses the
    agent, so this is the only place that translation happens for a BLE-delivered command."""
    assert frame.hopper_amounts(hopper, 7) == expected


def test_hopper_amounts_rejects_unknown_hopper() -> None:
    with pytest.raises(frame.FrameError, match="hopper must be"):
        frame.hopper_amounts("3", 5)


def test_feed_id_over_63_bytes_is_rejected() -> None:
    with pytest.raises(frame.FrameError, match="max 63"):
        frame.encode_feed_frame(feed_id="x" * 64, amount1=1, amount2=0)


def test_feed_id_at_63_bytes_is_accepted() -> None:
    feed_id = "x" * 63
    decoded = frame.decode_feed_frame(
        frame.encode_feed_frame(feed_id=feed_id, amount1=1, amount2=0)
    )
    assert decoded.feed_id == feed_id


@pytest.mark.parametrize("amount", [-1, 256, 1000])
def test_out_of_range_amount_is_rejected(amount: int) -> None:
    with pytest.raises(frame.FrameError, match="does not fit in one byte"):
        frame.encode_feed_frame(feed_id="x", amount1=amount, amount2=0)


def test_default_feed_id_has_the_agents_shape() -> None:
    """Same `kibble-<unix ts>` shape as `agent/src/main.rs`'s fallback id, so a feed logged
    without an explicit id looks the same regardless of which transport carried it."""
    feed_id = frame.default_feed_id()
    assert feed_id.startswith("kibble-")
    assert feed_id.removeprefix("kibble-").isdigit()
