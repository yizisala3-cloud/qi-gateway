"""Wrap the original flowers and an edge-matched stem in one SVG coordinate space.

Run from any directory with Python and Pillow after replacing either source PNG.
The original image bytes are embedded unchanged. A clipped view of its first
column extends the stem, including alpha, so the junction scales as one image.
"""

import base64
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image


def build(name: str) -> None:
    directory = Path(__file__).resolve().parent
    source = directory / f"{name}-divider-right.png"
    with Image.open(source) as image:
        width, height = image.size
        extension = 16384
        column_buffer = BytesIO()
        image.crop((0, 0, 1, height)).save(column_buffer, format="PNG")
        column_encoded = base64.b64encode(column_buffer.getvalue()).decode("ascii")
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    column_definition = (
        f'<image id="stem-pixels" width="1" height="{height}" '
        f'href="data:image/png;base64,{column_encoded}"/>'
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{extension + width}" height="{height}" '
        f'viewBox="0 0 {extension + width} {height}">\n'
        '<!-- One original PNG; extend its first column without changing its pixels. -->\n'
        f'<defs><image id="flower" width="{width}" height="{height}" '
        f'href="data:image/png;base64,{encoded}"/>{column_definition}</defs>\n'
        f'<svg width="{extension}" height="{height}" viewBox="0 0 1 {height}" '
        f'preserveAspectRatio="none" overflow="hidden"><use href="#stem-pixels"/></svg>\n'
        f'<use href="#flower" x="{extension}"/>\n</svg>\n'
    )
    (directory / f"{name}-divider.svg").write_text(svg, encoding="utf-8")
    finial_path = directory / f"{name}-finial.svg"
    root = ET.parse(finial_path).getroot()
    namespace = "http://www.w3.org/2000/svg"
    column = root.find(f'.//{{{namespace}}}image[@id="stem-pixels"]')
    if column is None:
        raise ValueError(f"Missing stem-pixels image in {finial_path}")
    column.set("href", f"data:image/png;base64,{column_encoded}")
    ET.register_namespace("", namespace)
    ET.indent(root, space="  ")
    finial_path.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")


if __name__ == "__main__":
    for variant in ("wisteria", "lily3"):
        build(variant)
