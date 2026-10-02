# Kibble brand images

Home Assistant core serves `custom_components/<domain>/brand/<image>.png` directly, so these files show up in the UI and in HACS with no submission to the `home-assistant/brands` repository.

| File | Size |
| --- | --- |
| `icon.png` / `icon@2x.png` | 256x256 / 512x512 |
| `logo.png` / `logo@2x.png` | 864x256 / 1728x512 |
| `dark_icon.png` / `dark_icon@2x.png` | 256x256 / 512x512 |
| `dark_logo.png` / `dark_logo@2x.png` | 864x256 / 1728x512 |

The mark is a black cat peeking over a food bowl. Flat colour, no text in the icon; the logo adds the "Kibble" wordmark.

All eight PNGs are rendered by `tools/render_brand.py` (Pillow only, deterministic; the vector twin is `icon.svg` at the repo root). Do not edit the PNGs by hand: change the script and re-run it.

`dark_*` variants use a cream plate with an amber bowl so they keep their contrast on the dark Home Assistant header; the light variants use an amber plate with a cream bowl.
