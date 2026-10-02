"""Deterministic Pillow renderer for the Kibble brand kit.

Writes the eight images Home Assistant serves straight from
``custom_components/kibble/brand/`` (no brands-repo submission needed):

    icon.png        256x256      dark_icon.png        256x256
    icon@2x.png     512x512      dark_icon@2x.png     512x512
    logo.png        864x256      dark_logo.png        864x256
    logo@2x.png     1728x512     dark_logo@2x.png     1728x512

The mark: a black cat peeking over a food bowl. Flat colour only, ten
shapes, no gradients. ``../icon.svg`` is the same mark as hand-authored
vector art; this script does not read it, so keep the numbers below in sync
with it.

Light variant ("icon", "logo"): amber plate, cream bowl.
Dark variant ("dark_icon", "dark_logo"): cream plate, amber bowl, so it keeps
its contrast on the dark Home Assistant header.

Pillow only, 4x supersampling, no randomness: re-running produces
byte-identical PNGs (given the same Pillow and font files).

Usage: python3 tools/render_brand.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / "custom_components" / "kibble" / "brand"

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Liberation Sans Bold, then DejaVu Bold, then Pillow's default font."""
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


# ---------------------------------------------------------------------------
# Geometry, in a 256x256 reference space (identical numbers to ../icon.svg).
# ---------------------------------------------------------------------------

PLATE_RX = 56.0

HEAD = (128.0, 116.0, 64.0)  # cx, cy, r
# Ears are triangles with rounded tips: the polygon below is the triangle
# pulled in by EAR_R, drawn with a round-capped stroke 2 * EAR_R wide on every
# side, which restores the full size and rounds the three corners (in the SVG:
# stroke-width 10, stroke-linejoin round). The right ear mirrors the left one
# about x = 128.
EAR_R = 5.0
EAR_LEFT = ((68.0, 50.5), (78.7, 83.1), (101.0, 63.5))
EAR_RIGHT = tuple((256.0 - x, y) for x, y in EAR_LEFT)

EYES = ((106.0, 124.0), (150.0, 124.0))  # centres
EYE_RX, EYE_RY = 11.0, 14.0
PUPIL_DY = -3.0  # pupils look up a little
PUPIL_R = 6.0

BOWL = (54.0, 164.0, 202.0, 46.0)  # x0, rim y, x1, depth: a half ellipse
RIM = (53.0, 152.0, 203.0, 176.0)  # the bowl's opening (the kibble), an ellipse bbox

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

INK = (0x3A, 0x2C, 0x28)
CREAM = (0xFF, 0xF6, 0xE8)
AMBER = (0xF4, 0xA4, 0x52)
AMBER_DEEP = (0xDE, 0x8A, 0x3A)

LIGHT_VARIANT = {  # amber plate ("icon", "logo")
    "plate": AMBER,
    "cat": INK,
    "eye": CREAM,
    "bowl": CREAM,
    "kibble": AMBER_DEEP,
}
DARK_VARIANT = {  # cream plate ("dark_icon", "dark_logo")
    "plate": CREAM,
    "cat": INK,
    "eye": CREAM,
    "bowl": AMBER,
    "kibble": AMBER_DEEP,
}

WORDMARK_ON_LIGHT_BG = INK  # logo.png
WORDMARK_ON_DARK_BG = CREAM  # dark_logo.png

SS = 4  # supersampling factor


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _capsule(draw, p0, p1, r, k, fill):
    """A round-capped stroke from p0 to p1, radius r (reference units)."""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    nx, ny = -dy / length * r, dx / length * r
    draw.polygon(
        [((x0 + nx) * k, (y0 + ny) * k), ((x1 + nx) * k, (y1 + ny) * k),
         ((x1 - nx) * k, (y1 - ny) * k), ((x0 - nx) * k, (y0 - ny) * k)],
        fill=fill,
    )
    for cx, cy in ((x0, y0), (x1, y1)):
        draw.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)


def _ear(draw, tri, k, fill):
    draw.polygon([(x * k, y * k) for x, y in tri], fill=fill)
    for i in range(3):
        _capsule(draw, tri[i], tri[(i + 1) % 3], EAR_R, k, fill)


def render_mark(size, palette):
    """(size, size) RGBA icon: flat rounded plate with the cat-and-bowl mark."""
    big = size * SS
    k = big / 256.0
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=PLATE_RX * k, fill=palette["plate"] + (255,))

    cat = palette["cat"] + (255,)
    cx, cy, r = HEAD
    d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=cat)
    _ear(d, EAR_LEFT, k, cat)
    _ear(d, EAR_RIGHT, k, cat)

    eye = palette["eye"] + (255,)
    for ex, ey in EYES:
        d.ellipse([(ex - EYE_RX) * k, (ey - EYE_RY) * k, (ex + EYE_RX) * k, (ey + EYE_RY) * k], fill=eye)
        py = ey + PUPIL_DY
        d.ellipse([(ex - PUPIL_R) * k, (py - PUPIL_R) * k, (ex + PUPIL_R) * k, (py + PUPIL_R) * k], fill=cat)

    bx0, by, bx1, depth = BOWL
    d.pieslice([bx0 * k, (by - depth) * k, bx1 * k, (by + depth) * k], 0, 180, fill=palette["bowl"] + (255,))
    rx0, ry0, rx1, ry1 = RIM
    d.ellipse([rx0 * k, ry0 * k, rx1 * k, ry1 * k], fill=palette["kibble"] + (255,))

    return canvas.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Wordmark lockup
# ---------------------------------------------------------------------------

LOGO_ASPECT = 864 / 256  # width / height (about 3.4x)
LOGO_MARK_FRAC = 0.86  # mark size as a fraction of canvas height
LOGO_LEFT_FRAC = 0.05
LOGO_GAP_FRAC = 0.10
LOGO_NAME_FRAC = 0.62  # "Kibble" font size / canvas height
LOGO_TRACK_FRAC = 0.004


def _tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def _draw_tracked(draw, x, y, text, font, fill, tracking):
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def render_logo(height, palette, name_rgb):
    """RGBA lockup: mark on the left, "Kibble" on the right, transparent
    background, exactly `height` tall."""
    width = round(height * LOGO_ASPECT)
    mark_size = round(height * LOGO_MARK_FRAC)
    mark = render_mark(mark_size, palette)

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    left = round(height * LOGO_LEFT_FRAC)
    canvas.alpha_composite(mark, (left, (height - mark_size) // 2))

    name_font = load_font(round(height * LOGO_NAME_FRAC))
    tracking = height * LOGO_TRACK_FRAC
    draw = ImageDraw.Draw(canvas)

    # Vertically centre the word on ink bounds, not font ascent.
    n_l, n_t, n_r, n_b = draw.textbbox((0, 0), "Kibble", font=name_font)
    top = (height - (n_b - n_t)) / 2
    text_x = left + mark_size + height * LOGO_GAP_FRAC

    _draw_tracked(draw, text_x - n_l, top - n_t, "Kibble", name_font, name_rgb + (255,), tracking)

    right_edge = text_x + _tracked_width(draw, "Kibble", name_font, tracking)
    assert right_edge <= width - height * 0.03, f"wordmark clipped at height {height}: {right_edge} > {width}"
    return canvas


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _save_pair(master, stem):
    """<stem>@2x.png from the 512-based master, <stem>.png as an exact half."""
    master.save(BRAND_DIR / f"{stem}@2x.png")
    half = (master.width // 2, master.height // 2)
    master.resize(half, Image.LANCZOS).save(BRAND_DIR / f"{stem}.png")


def main():
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    _save_pair(render_mark(512, LIGHT_VARIANT), "icon")
    _save_pair(render_mark(512, DARK_VARIANT), "dark_icon")
    _save_pair(render_logo(512, LIGHT_VARIANT, WORDMARK_ON_LIGHT_BG), "logo")
    _save_pair(render_logo(512, DARK_VARIANT, WORDMARK_ON_DARK_BG), "dark_logo")


if __name__ == "__main__":
    main()
