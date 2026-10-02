"""`media_processing.process_upload_image`: independent re-validation of every uploaded photo
regardless of what the browser's own `lib/image-prep.ts` pipeline already attempted -- the
acceptance list this whole robustness pass was specified against: an oversize file is rejected,
a non-image file is rejected, a pixel bomb is rejected, and an EXIF-rotated photo is stored
upright. `test_upload_views.py` covers the same guards again at the real HTTP layer; this file
pins the underlying Pillow logic directly and faster.
"""

from __future__ import annotations

import io
import struct
import zlib

import numpy as np
import pytest
from PIL import Image

from kibble import media_processing


def _real_jpeg(size: tuple[int, int] = (300, 200), color: tuple[int, int, int] = (180, 90, 40)) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _crafted_png(w: int, h: int) -> bytes:
    """A PNG whose header alone declares `w`x`h` pixels -- enough for `Image.open` to read the
    dimensions without ever decoding real pixel data, which is exactly the attack shape the
    megapixel guard has to catch before any expensive/dangerous decode happens."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 3)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# --- rejections -----------------------------------------------------------------------------


def test_empty_bytes_are_rejected() -> None:
    with pytest.raises(media_processing.UploadRejected) as exc:
        media_processing.process_upload_image(b"")
    assert exc.value.reason == "bad_image"


def test_a_request_over_the_byte_cap_is_rejected() -> None:
    oversize = b"\xff\xd8" + b"0" * (media_processing.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(media_processing.UploadRejected) as exc:
        media_processing.process_upload_image(oversize)
    assert exc.value.reason == "too_large"


def test_a_non_image_file_is_rejected() -> None:
    with pytest.raises(media_processing.UploadRejected) as exc:
        media_processing.process_upload_image(b"this is a plain text file, not a photo at all")
    assert exc.value.reason == "bad_image"


def test_a_pixel_bomb_over_the_megapixel_cap_is_rejected() -> None:
    """80 MP: over `MAX_MEGAPIXELS` (60 MP) but under Pillow's own default decompression-bomb
    threshold (~89.5 MP) -- proves Kibble's own explicit guard fires, not just Pillow's."""
    assert 60_000_000 < 10_000 * 8_000 < 89_478_485
    bomb = _crafted_png(10_000, 8_000)
    with pytest.raises(media_processing.UploadRejected) as exc:
        media_processing.process_upload_image(bomb)
    assert exc.value.reason == "too_large"


def test_a_far_larger_pixel_bomb_is_still_rejected_even_via_pillows_own_guard() -> None:
    """Belt-and-suspenders: a declared size so large Pillow's own built-in protection trips
    first (inside `Image.open`, before Kibble's explicit check ever runs) -- still rejected,
    never an unhandled exception escaping `process_upload_image`."""
    bomb = _crafted_png(20_000, 20_000)
    with pytest.raises(media_processing.UploadRejected):
        media_processing.process_upload_image(bomb)


def test_heic_shaped_bytes_get_a_specific_message_when_the_optional_codec_is_absent() -> None:
    """`pillow_heif` is not installed in this environment (confirmed: `media_processing.
    pillow_heif is None`), so a real HEIC/HEIF ftyp box must fail with a message naming HEIC
    specifically, not a generic "unsupported format" -- the clear per-file message the upload
    flow's design requires."""
    assert media_processing.pillow_heif is None
    heic_shaped = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100
    with pytest.raises(media_processing.UploadRejected) as exc:
        media_processing.process_upload_image(heic_shaped)
    assert exc.value.reason == "bad_image"
    assert "heic" in str(exc.value).lower()


# --- successful processing: downscale, never upscale, strip metadata, orient ------------------


def test_an_image_larger_than_the_store_edge_is_downscaled() -> None:
    raw = _real_jpeg(size=(3000, 2000))
    out = media_processing.process_upload_image(raw)
    result = Image.open(io.BytesIO(out))
    assert max(result.size) == media_processing.STORE_MAX_EDGE
    assert result.size == (media_processing.STORE_MAX_EDGE, round(2000 * media_processing.STORE_MAX_EDGE / 3000))


def test_an_image_smaller_than_the_store_edge_is_never_upscaled() -> None:
    raw = _real_jpeg(size=(100, 80))
    out = media_processing.process_upload_image(raw)
    result = Image.open(io.BytesIO(out))
    assert result.size == (100, 80)


def test_output_is_always_a_fresh_jpeg_with_no_exif_payload() -> None:
    im = Image.new("RGB", (50, 50), (10, 20, 30))
    buf = io.BytesIO()
    exif = im.getexif()
    exif[0x010F] = "TestCam"  # Make
    im.save(buf, format="PNG")  # a non-JPEG input, so "always JPEG out" is a real conversion
    out = media_processing.process_upload_image(buf.getvalue())
    result = Image.open(io.BytesIO(out))
    assert result.format == "JPEG"
    assert dict(result.getexif()) == {}


def test_an_exif_rotated_image_is_stored_upright() -> None:
    """A 40x20 image with a green stripe on its physical LEFT edge, tagged EXIF orientation 6
    (needs a 90 deg rotation to display upright) -- Pillow's `exif_transpose` must physically
    rotate the pixels so the stored JPEG carries no orientation tag and already reads correctly
    without one: the green stripe should now be at the TOP of a tall 20x40 result."""
    w, h = 40, 20
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = 255  # red background
    arr[:, :10, :] = (0, 255, 0)  # green stripe on the left third
    im = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    exif = im.getexif()
    exif[0x0112] = 6  # Orientation tag: rotate 90 CW to correct
    im.save(buf, format="JPEG", exif=exif, quality=95)

    out = media_processing.process_upload_image(buf.getvalue())
    result = Image.open(io.BytesIO(out))
    assert dict(result.getexif()) == {}
    assert result.size == (h, w)  # dimensions swapped: now tall, not wide
    top_strip = np.asarray(result.convert("RGB"))[:5, :, :]
    bottom_strip = np.asarray(result.convert("RGB"))[-5:, :, :]
    # The green stripe physically moved to one end of the now-vertical image; it must not still
    # read as a vertical strip down one side (which is what "orientation tag silently dropped,
    # pixels never rotated" would look like).
    assert top_strip[..., 1].mean() > 150 or bottom_strip[..., 1].mean() > 150
    assert not (top_strip[..., 1].mean() > 150 and bottom_strip[..., 1].mean() > 150)
