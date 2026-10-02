"""Robust image intake for every upload path (training photos and cat avatars): decode
independently of whatever the browser claims the file is, guard against decompression bombs,
apply real EXIF orientation, strip every other byte of metadata, and downscale to what the
training set actually stores. Pillow only -- no new required dependency -- with an optional
HEIC decoder that degrades to a clear per-file rejection when it is not installed.

The card's own upload pipeline (`kibble-card/src/lib/image-prep.ts`) already downscales to
~1600px/q0.85 and applies EXIF orientation client-side before the request is even sent; nothing
here trusts that happened. Every guard below is independently enforced against whatever bytes
actually arrive, exactly per docs/36-ai-pipeline.md's ownership rule that HA, not the browser,
is the system of record.

Every function here is synchronous, CPU-bound Pillow work: callers on the event loop MUST run
`process_upload_image` through `hass.async_add_executor_job`, the same rule `identity.py`'s
module docstring states for appearance descriptors.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

try:
    import pillow_heif
except ImportError:
    pillow_heif = None
else:
    pillow_heif.register_heif_opener()

# The client already downscales before sending, so this is a backstop against a client that
# skipped or failed that step -- a raw phone photo runs 15-50 MB; a request past this is never
# a legitimate single photo. Enforced twice: `views.py` rejects an over-size request outright
# (413, before reading the whole body), and again here against whatever bytes actually made it
# through, so this module is a safe boundary on its own regardless of caller.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Decompression-bomb guard, checked against the header's own declared dimensions *before*
# decoding any pixel data. Pillow's own `Image.MAX_IMAGE_PIXELS` only warns by default and is a
# global mutable another part of the HA process could change, so this is an explicit, local,
# always-enforced bound instead. 60 MP comfortably covers a 61 MP (9504x6336) high-end phone/
# camera JPEG while still rejecting a crafted multi-hundred-megapixel file designed to exhaust
# memory on decode.
MAX_MEGAPIXELS = 60_000_000

# What actually gets written to `training/`/`avatars/` -- docs/36-ai-pipeline.md's storage
# math: ~640px max edge, JPEG q82. Downscale only, never upscale a crop already smaller than
# this (true of every device-captured body/face crop, which arrives at 224px).
STORE_MAX_EDGE = 640
STORE_JPEG_QUALITY = 82

# Pillow's own format names for whatever `Image.open` actually decoded, never the upload's file
# extension or declared content-type -- "verify the real image type with PIL" from the design
# note this module implements. "HEIF" only actually resolves when `pillow_heif` registered its
# opener above; otherwise `Image.open` never gets far enough to report it.
_DECODABLE_FORMATS = {"JPEG", "PNG", "WEBP", "BMP", "GIF", "TIFF", "HEIF"}

# ISOBMFF `ftyp` box + HEIC/HEIF brand codes, sniffed independently of whether Pillow can
# actually decode the file -- lets a rejection say "this is HEIC" specifically (install
# pillow-heif, or export as JPEG) rather than a generic "unsupported format" when the codec
# simply is not installed.
_HEIC_BRANDS = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1")


def _looks_like_heic(raw: bytes) -> bool:
    return len(raw) >= 12 and raw[4:8] == b"ftyp" and raw[8:12] in _HEIC_BRANDS


class UploadRejected(Exception):
    """A per-file reason a card can show directly -- never a stack trace, never a generic
    "upload failed". `reason` is a short machine code (`"too_large"`, `"bad_image"`,
    `"no_cat"`, `"duplicate"`); `str(exc)` is already a human sentence."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _open_checked(raw: bytes) -> Image.Image:
    """Opens `raw`, confirms Pillow itself recognises it as one of `_DECODABLE_FORMATS` (never
    trusting a filename or content-type), and rejects on the header's own declared pixel count
    *before* decoding any pixel data -- the decompression-bomb guard has to run before the one
    call (`.load()`) that would actually be expensive/dangerous on a crafted file."""
    try:
        im = Image.open(io.BytesIO(raw))
        im_format = im.format
    except Exception as exc:  # noqa: BLE001 - any structural problem is the same rejection
        if _looks_like_heic(raw):
            raise UploadRejected(
                "bad_image",
                "That's an HEIC photo and this server can't convert it. Export it as JPEG first.",
            ) from exc
        raise UploadRejected("bad_image", f"That file isn't a photo Kibble can read: {exc}") from exc
    if im_format not in _DECODABLE_FORMATS:
        im.close()
        raise UploadRejected("bad_image", f"Unsupported image format: {im_format or 'unknown'}")
    width, height = im.size
    if width <= 0 or height <= 0 or width * height > MAX_MEGAPIXELS:
        im.close()
        raise UploadRejected("too_large", f"That photo is too large ({width}x{height} pixels).")
    try:
        im.load()
    except Exception as exc:  # noqa: BLE001 - a header that parses but a body that fails to decode
        im.close()
        raise UploadRejected("bad_image", f"That file isn't a photo Kibble can read: {exc}") from exc
    return im


def process_upload_image(raw: bytes) -> bytes:
    """Validates, orients, strips and downscales one uploaded photo. Always returns a fresh
    JPEG at or under `STORE_MAX_EDGE` on its long edge, `STORE_JPEG_QUALITY` quality, carrying no
    EXIF/ICC/XMP payload -- Pillow only carries metadata forward when explicitly told to, so a
    plain re-encode from decoded pixels already drops it, no separate stripping step needed.
    Raises `UploadRejected` (short machine `reason` + human `message`) for anything that is not
    a decodable, reasonably-sized photo -- never lets a bare Pillow exception escape."""
    if not raw:
        raise UploadRejected("bad_image", "That file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise UploadRejected("too_large", "That file is too large.")
    im = _open_checked(raw)
    try:
        im = ImageOps.exif_transpose(im) or im
        im = im.convert("RGB")
        edge = max(im.size)
        if edge > STORE_MAX_EDGE:
            scale = STORE_MAX_EDGE / edge
            new_size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
            im = im.resize(new_size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=STORE_JPEG_QUALITY)
        return buf.getvalue()
    finally:
        im.close()
