"""Wrap the original flowers and an edge-matched stem in one SVG coordinate space.

Run from any directory with Python and Pillow after replacing either source PNG.
The original image bytes are embedded unchanged. A clipped view of its first
column extends the stem, including alpha, so the junction scales as one image.
"""

import base64
from pathlib import Path

from PIL import Image


def build(name: str) -> None:
    directory = Path(__file__).resolve().parent
    source = directory / f"{name}-divider-right.png"
    with Image.open(source) as image:
        width, height = image.size
        extension = 16384
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{extension + width}" height="{height}" '
        f'viewBox="0 0 {extension + width} {height}">\n'
        '<!-- One original PNG; extend its first column without changing its pixels. -->\n'
        f'<defs><image id="flower" width="{width}" height="{height}" '
        f'href="data:image/png;base64,{encoded}"/></defs>\n'
        f'<svg width="{extension}" height="{height}" viewBox="0 0 1 {height}" '
        'preserveAspectRatio="none" overflow="hidden"><use href="#flower"/></svg>\n'
        f'<use href="#flower" x="{extension}"/>\n</svg>\n'
    )
    (directory / f"{name}-divider.svg").write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    for variant in ("wisteria", "lily3"):
        build(variant)
