from __future__ import annotations

import base64
import copy
import mimetypes
import os
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

from bubble.models import (
    BUBBLE_FILL_ALPHA_PNG,
    BUBBLE_FILL_OPACITY,
    BUBBLE_STROKE_COLOR,
    FONT_CANDIDATES,
    PROJECT_ROOT,
    SVG_NS,
)


ET.register_namespace("", SVG_NS)


def pick_font_path(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for candidate in FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def encode_file_as_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def browser_font_stack(font_path: str | None) -> str:
    families = []
    if font_path:
        name = Path(font_path).stem
        if "NotoSansCJK" in name:
            families.append('"Noto Sans CJK JP"')
        elif "NotoSerifCJK" in name:
            families.append('"Noto Serif CJK JP"')
        elif "IPA" in name or "ipag" in name.lower():
            families.append('"IPAGothic"')
        elif "DejaVuSans" in name:
            families.append('"DejaVu Sans"')
    families.extend(
        [
            '"BIZ UDPGothic"',
            '"BIZ UDMincho"',
            '"Hiragino Sans"',
            '"Hiragino Mincho ProN"',
            '"Yu Gothic"',
            '"Yu Gothic UI"',
            '"Yu Mincho"',
            '"IPAexGothic"',
            '"IPAGothic"',
            '"Noto Sans CJK JP"',
            '"Noto Serif CJK JP"',
            '"MS PGothic"',
            '"MS Mincho"',
            "sans-serif",
        ]
    )
    return ", ".join(dict.fromkeys(families))


def css_font_literal(value: str) -> str:
    value = value.strip()
    if "," in value or value.startswith(("'", '"')):
        return value
    return f'"{value}"'


def build_font_css(font_path: str | None, font_family: str | None) -> tuple[str, str]:
    fallback = browser_font_stack(font_path)
    if font_path:
        embedded_family = "__BubbleFont__"
        font_css = (
            "@font-face {"
            f"font-family: '{embedded_family}';"
            f"src: url('{encode_file_as_data_url(Path(font_path))}');"
            "font-display: swap;"
            "}"
        )
        return font_css, f'"{embedded_family}", {fallback}'
    if font_family:
        return "", f"{css_font_literal(font_family)}, {fallback}"
    return "", fallback


def resolve_bubble_asset(explicit: str | None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            PROJECT_ROOT / "assets" / "bubble_ellipse.svg",
            PROJECT_ROOT / "resources" / "bubble.svg",
            PROJECT_ROOT / "resources" / "bubble.png",
            PROJECT_ROOT / "resources" / "bubble_svg.txt",
            PROJECT_ROOT / "imgs" / "bubble.svg",
            PROJECT_ROOT / "imgs" / "bubble.png",
            Path("/notebooks/imgs/bubble.svg"),
            Path("/notebooks/imgs/bubble.png"),
            Path("/notebooks/resources/bubble.svg"),
            Path("/notebooks/resources/bubble.png"),
            Path("/notebooks/resources/bubble_svg.txt"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def resolve_chromium_executable() -> str | None:
    browser_root = PROJECT_ROOT / ".playwright-browsers"
    candidates = sorted(browser_root.glob("chromium-*/chrome-linux64/chrome"))
    if candidates:
        return str(candidates[-1])
    return None


def flood_fill_outside_open_regions(grayscale: np.ndarray, outline_cutoff: int) -> np.ndarray:
    height, width = grayscale.shape
    outside = np.zeros((height, width), dtype=bool)
    queue: deque[tuple[int, int]] = deque()

    def enqueue_if_open(x: int, y: int) -> None:
        if 0 <= x < width and 0 <= y < height and not outside[y, x] and grayscale[y, x] >= outline_cutoff:
            outside[y, x] = True
            queue.append((x, y))

    for x in range(width):
        enqueue_if_open(x, 0)
        enqueue_if_open(x, height - 1)
    for y in range(height):
        enqueue_if_open(0, y)
        enqueue_if_open(width - 1, y)

    while queue:
        x, y = queue.popleft()
        enqueue_if_open(x + 1, y)
        enqueue_if_open(x - 1, y)
        enqueue_if_open(x, y + 1)
        enqueue_if_open(x, y - 1)

    return outside


def bubble_png_to_rgba(asset_path: Path) -> Image.Image:
    grayscale = np.asarray(Image.open(asset_path).convert("L"), dtype=np.uint8)
    outline_cutoff = 240
    outside = flood_fill_outside_open_regions(grayscale, outline_cutoff)

    rgba = np.zeros((grayscale.shape[0], grayscale.shape[1], 4), dtype=np.uint8)
    outline_mask = grayscale < outline_cutoff
    fill_mask = ~outline_mask & ~outside

    rgba[outline_mask] = np.array([0, 0, 0, 255], dtype=np.uint8)
    rgba[fill_mask] = np.array([255, 255, 255, BUBBLE_FILL_ALPHA_PNG], dtype=np.uint8)
    return Image.fromarray(rgba, mode="RGBA")


def load_bubble_svg_source(asset_path: Path) -> str:
    if asset_path.suffix.lower() == ".svg":
        return asset_path.read_text(encoding="utf-8")
    if asset_path.suffix.lower() == ".txt":
        return asset_path.read_text(encoding="utf-8")
    raise RuntimeError(f"unsupported SVG asset type: {asset_path}")


def svg_qname(local: str) -> str:
    return f"{{{SVG_NS}}}{local}"


def parse_svg_viewbox(root: ET.Element) -> tuple[float, float, float, float]:
    raw = root.attrib.get("viewBox")
    if not raw:
        raise RuntimeError("bubble SVG is missing a viewBox")
    parts = [float(part) for part in raw.replace(",", " ").split()]
    if len(parts) != 4:
        raise RuntimeError(f"invalid bubble SVG viewBox: {raw}")
    return parts[0], parts[1], parts[2], parts[3]


def warp_svg_source_to_aspect(svg_source: str, target_aspect: float) -> str:
    if target_aspect <= 0:
        raise RuntimeError("target aspect must be positive")
    root = ET.fromstring(svg_source)
    vb_x, vb_y, vb_w, vb_h = parse_svg_viewbox(root)
    source_aspect = vb_w / vb_h
    if abs(source_aspect - target_aspect) < 1e-6:
        return svg_source

    center_x = vb_x + vb_w / 2.0
    center_y = vb_y + vb_h / 2.0

    defs_nodes: list[ET.Element] = []
    drawable_nodes: list[ET.Element] = []
    for child in list(root):
        if child.tag == svg_qname("defs"):
            defs_nodes.append(copy.deepcopy(child))
        else:
            drawable_nodes.append(copy.deepcopy(child))

    if target_aspect >= source_aspect:
        target_w = vb_h * target_aspect
        target_h = vb_h
        scale_x = target_w / vb_w
        scale_y = 1.0
    else:
        target_w = vb_w
        target_h = vb_w / target_aspect
        scale_x = 1.0
        scale_y = target_h / vb_h

    target_x = center_x - target_w / 2.0
    target_y = center_y - target_h / 2.0

    new_root = ET.Element(
        svg_qname("svg"),
        {
            "width": f"{target_w:.4f}",
            "height": f"{target_h:.4f}",
            "viewBox": f"{target_x:.4f} {target_y:.4f} {target_w:.4f} {target_h:.4f}",
        },
    )
    for defs in defs_nodes:
        new_root.append(defs)

    warp_group = ET.SubElement(
        new_root,
        svg_qname("g"),
        {
            "transform": (
                f"translate({center_x:.4f} {center_y:.4f}) "
                f"scale({scale_x:.6f} {scale_y:.6f}) "
                f"translate({-center_x:.4f} {-center_y:.4f})"
            )
        },
    )
    for node in drawable_nodes:
        warp_group.append(node)

    return ET.tostring(new_root, encoding="unicode")


def build_bubble_svg_html(svg_source: str, width: int, height: int) -> str:
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html, body {{
  margin: 0;
  width: {width}px;
  height: {height}px;
  overflow: hidden;
  background: transparent !important;
}}
#asset {{
  width: {width}px;
  height: {height}px;
}}
#asset > svg {{
  display: block;
  width: 100%;
  height: 100%;
}}
#asset :root {{
  --stroke: {BUBBLE_STROKE_COLOR};
}}
#asset .bubble {{
  fill-opacity: {BUBBLE_FILL_OPACITY} !important;
  stroke: {BUBBLE_STROKE_COLOR} !important;
}}
</style>
</head>
<body>
  <div id="asset">{svg_source}</div>
</body>
</html>
"""


def white_to_transparent(image: Image.Image, cutoff: int = 248) -> Image.Image:
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
    white_mask = np.all(rgba[:, :, :3] >= cutoff, axis=2)
    rgba[white_mask, 3] = 0
    rgba[~white_mask, 3] = 255
    return Image.fromarray(rgba, mode="RGBA")
