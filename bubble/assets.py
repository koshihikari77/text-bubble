from __future__ import annotations

import base64
import copy
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pathops
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.svgLib.path import parse_path
from PIL import Image

from bubble.models import (
    BUBBLE_FILL_ALPHA_PNG,
    BUBBLE_FILL_OPACITY,
    BUBBLE_STROKE_COLOR,
    DEFAULT_BUBBLE_TYPE,
    FONT_CANDIDATES,
    PROJECT_ROOT,
    SVG_NS,
)
from bubble.procedural_bubbles import generate_procedural_bubble_svg, procedural_asset_key


ET.register_namespace("", SVG_NS)

TRANSFORM_PATTERN = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")
MERGED_BUBBLE_STROKE_SCALE = 0.84
BUBBLE_ASSET_MANIFEST = PROJECT_ROOT / "assets" / "bubble_assets.json"


@dataclass(frozen=True)
class BubbleAssetCatalog:
    default_type: str
    assets: dict[str, "BubbleAssetEntry"]


@dataclass(frozen=True)
class BubbleAssetEntry:
    kind: str
    path: Path | None = None
    generator: str | None = None
    params: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedBubbleAsset:
    bubble_type: str
    source_kind: str
    source_key: str
    asset_path: Path | None = None
    svg_source: str | None = None


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


def _legacy_bubble_asset_candidates() -> list[Path]:
    return [
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


def load_bubble_asset_catalog(manifest_path: Path | None = None) -> BubbleAssetCatalog:
    path = manifest_path or BUBBLE_ASSET_MANIFEST
    if not path.exists():
        legacy_asset = next((candidate for candidate in _legacy_bubble_asset_candidates() if candidate.exists()), None)
        assets = (
            {DEFAULT_BUBBLE_TYPE: BubbleAssetEntry(kind="static", path=legacy_asset)}
            if legacy_asset is not None
            else {}
        )
        return BubbleAssetCatalog(default_type=DEFAULT_BUBBLE_TYPE, assets=assets)

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"bubble asset manifest must be an object: {path}")
    default_type = data.get("default_type")
    if not isinstance(default_type, str) or not default_type.strip():
        raise RuntimeError(f"bubble asset manifest must include non-empty default_type: {path}")
    raw_assets = data.get("types")
    if not isinstance(raw_assets, dict) or not raw_assets:
        raise RuntimeError(f"bubble asset manifest must include a non-empty types object: {path}")

    resolved_assets: dict[str, BubbleAssetEntry] = {}
    for bubble_type, raw_entry in raw_assets.items():
        if not isinstance(bubble_type, str) or not bubble_type.strip():
            raise RuntimeError(f"bubble asset manifest type keys must be non-empty strings: {path}")
        normalized_type = bubble_type.strip()
        if isinstance(raw_entry, str):
            asset_path = Path(raw_entry)
            if not asset_path.is_absolute():
                asset_path = (path.parent / asset_path).resolve()
            resolved_assets[normalized_type] = BubbleAssetEntry(kind="static", path=asset_path)
            continue
        if not isinstance(raw_entry, dict):
            raise RuntimeError(f"bubble asset manifest entry must be a string or object: {path}")
        kind = raw_entry.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise RuntimeError(f"bubble asset manifest object entry must include non-empty kind: {path}")
        normalized_kind = kind.strip()
        if normalized_kind in {"static", "static_svg", "static_png"}:
            raw_path = raw_entry.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise RuntimeError(f"static bubble asset entry must include non-empty path: {path}")
            asset_path = Path(raw_path)
            if not asset_path.is_absolute():
                asset_path = (path.parent / asset_path).resolve()
            resolved_assets[normalized_type] = BubbleAssetEntry(kind="static", path=asset_path)
            continue
        if normalized_kind == "procedural":
            generator = raw_entry.get("generator")
            if not isinstance(generator, str) or not generator.strip():
                raise RuntimeError(f"procedural bubble asset entry must include non-empty generator: {path}")
            params = raw_entry.get("params", {})
            if not isinstance(params, dict):
                raise RuntimeError(f"procedural bubble asset params must be an object: {path}")
            resolved_assets[normalized_type] = BubbleAssetEntry(
                kind="procedural",
                generator=generator.strip(),
                params=params,
            )
            continue
        raise RuntimeError(f"unsupported bubble asset kind '{normalized_kind}': {path}")

    default_type = default_type.strip()
    if default_type not in resolved_assets:
        raise RuntimeError(f"default bubble type is missing from manifest: {default_type}")
    return BubbleAssetCatalog(default_type=default_type, assets=resolved_assets)


def resolve_bubble_asset(explicit: str | None, bubble_type: str | None = None) -> Path | None:
    asset = resolve_bubble_renderable_asset(explicit, bubble_type)
    return asset.asset_path if asset is not None else None


def _resolved_path_source_kind(asset_path: Path) -> str:
    suffix = asset_path.suffix.lower()
    if suffix in {".svg", ".txt"}:
        return "svg"
    if suffix == ".png":
        return "png"
    raise RuntimeError(f"unsupported bubble asset type: {asset_path}")


def resolve_bubble_renderable_asset(explicit: str | None, bubble_type: str | None = None) -> ResolvedBubbleAsset | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    else:
        catalog = load_bubble_asset_catalog()
        normalized_type = bubble_type.strip() if isinstance(bubble_type, str) and bubble_type.strip() else catalog.default_type
        entry = catalog.assets.get(normalized_type)
        if entry is not None:
            if entry.kind == "static":
                if entry.path is not None:
                    candidates.append(entry.path)
            elif entry.kind == "procedural":
                assert entry.generator is not None
                params = entry.params or {}
                return ResolvedBubbleAsset(
                    bubble_type=normalized_type,
                    source_kind="svg",
                    source_key=procedural_asset_key(entry.generator, params),
                    svg_source=generate_procedural_bubble_svg(entry.generator, params),
                )
            else:
                raise RuntimeError(f"unsupported bubble asset entry kind: {entry.kind}")
        if normalized_type == catalog.default_type:
            candidates.extend(_legacy_bubble_asset_candidates())
    for candidate in candidates:
        if candidate.exists():
            resolved = candidate.resolve()
            return ResolvedBubbleAsset(
                bubble_type=bubble_type.strip() if isinstance(bubble_type, str) and bubble_type.strip() else DEFAULT_BUBBLE_TYPE,
                source_kind=_resolved_path_source_kind(resolved),
                source_key=f"file:{resolved}",
                asset_path=resolved,
            )
    return None


def load_bubble_svg_source_from_asset(asset: ResolvedBubbleAsset) -> str:
    if asset.svg_source is not None:
        return asset.svg_source
    if asset.asset_path is None:
        raise RuntimeError("bubble asset does not contain an SVG source")
    return load_bubble_svg_source(asset.asset_path)


def resolve_chromium_executable() -> str | None:
    browser_root = PROJECT_ROOT / ".playwright-browsers"
    candidates = sorted(browser_root.glob("chromium-*/chrome-linux64/chrome"))
    if candidates:
        return str(candidates[-1])
    return None


def resolve_resvg_executable() -> str | None:
    explicit = os.environ.get("TEXT_BUBBLE_RESVG", "").strip()
    if explicit:
        path = Path(explicit)
        if path.exists():
            return str(path)
    resolved = shutil.which("resvg")
    return resolved if resolved else None


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
  fill: #ffffff !important;
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


def build_bubble_svg_source(svg_source: str, width: int, height: int) -> str:
    root = ET.fromstring(svg_source)
    root.set("width", str(width))
    root.set("height", str(height))
    root.set("preserveAspectRatio", "xMidYMid meet")

    defs: ET.Element | None = None
    for child in list(root):
        if child.tag == svg_qname("defs"):
            defs = child
            break
    if defs is None:
        defs = ET.Element(svg_qname("defs"))
        root.insert(0, defs)

    style = ET.SubElement(defs, svg_qname("style"))
    style.text = (
        ":root {"
        f"--stroke: {BUBBLE_STROKE_COLOR};"
        "--fill: #ffffff;"
        "}"
        ".bubble {"
        "fill: #ffffff !important;"
        f"fill-opacity: {BUBBLE_FILL_OPACITY} !important;"
        f"stroke: {BUBBLE_STROKE_COLOR} !important;"
        "}"
    )
    return ET.tostring(root, encoding="unicode")


def _identity_matrix() -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    return (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )


def _matmul(
    left: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    right: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    result: list[list[float]] = [[0.0, 0.0, 0.0] for _ in range(3)]
    for row in range(3):
        for col in range(3):
            result[row][col] = sum(left[row][index] * right[index][col] for index in range(3))
    return (
        (result[0][0], result[0][1], result[0][2]),
        (result[1][0], result[1][1], result[1][2]),
        (result[2][0], result[2][1], result[2][2]),
    )


def _translate_matrix(tx: float, ty: float) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    return (
        (1.0, 0.0, tx),
        (0.0, 1.0, ty),
        (0.0, 0.0, 1.0),
    )


def _scale_matrix(sx: float, sy: float) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    return (
        (sx, 0.0, 0.0),
        (0.0, sy, 0.0),
        (0.0, 0.0, 1.0),
    )


def _parse_transform_matrix(value: str | None) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    matrix = _identity_matrix()
    if not value:
        return matrix
    for name, args_text in TRANSFORM_PATTERN.findall(value):
        raw_args = [part for part in re.split(r"[\s,]+", args_text.strip()) if part]
        args = [float(part) for part in raw_args]
        lowered = name.lower()
        if lowered == "translate":
            tx = args[0] if args else 0.0
            ty = args[1] if len(args) > 1 else 0.0
            current = _translate_matrix(tx, ty)
        elif lowered == "scale":
            sx = args[0] if args else 1.0
            sy = args[1] if len(args) > 1 else sx
            current = _scale_matrix(sx, sy)
        elif lowered == "matrix" and len(args) == 6:
            current = (
                (args[0], args[2], args[4]),
                (args[1], args[3], args[5]),
                (0.0, 0.0, 1.0),
            )
        else:
            continue
        matrix = _matmul(matrix, current)
    return matrix


def _extract_bubble_path_specs(
    svg_source: str,
) -> tuple[tuple[float, float, float, float], list[tuple[str, tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]], float]:
    root = ET.fromstring(svg_source)
    viewbox = parse_svg_viewbox(root)
    path_specs: list[
        tuple[str, tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]]
    ] = []
    stroke_width = 6.0

    for style_node in root.findall(f".//{svg_qname('style')}"):
        text = style_node.text or ""
        match = re.search(r"--strokeW\s*:\s*([0-9.]+)", text)
        if match:
            stroke_width = float(match.group(1))
            break
        match = re.search(r"stroke-width\s*:\s*([0-9.]+)", text)
        if match:
            stroke_width = float(match.group(1))
            break

    def walk(
        node: ET.Element,
        inherited_matrix: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
        *,
        in_defs: bool,
    ) -> None:
        current_matrix = _matmul(inherited_matrix, _parse_transform_matrix(node.attrib.get("transform")))
        is_defs = in_defs or node.tag == svg_qname("defs")
        if node.tag == svg_qname("path") and not is_defs:
            d_value = node.attrib.get("d", "").strip()
            if d_value:
                path_specs.append((d_value, current_matrix))
        for child in list(node):
            walk(child, current_matrix, in_defs=is_defs)

    walk(root, _identity_matrix(), in_defs=False)
    if not path_specs:
        raise RuntimeError("bubble SVG must contain at least one <path>")
    return viewbox, path_specs, stroke_width


def _pathops_matrix(
    *,
    source_matrix: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    vb_x: float,
    vb_y: float,
    scale_x: float,
    scale_y: float,
    bubble_left: int,
    bubble_top: int,
) -> list[float]:
    return [
        source_matrix[0][0] * scale_x,
        source_matrix[0][1] * scale_x,
        bubble_left + (source_matrix[0][2] - vb_x) * scale_x,
        source_matrix[1][0] * scale_y,
        source_matrix[1][1] * scale_y,
        bubble_top + (source_matrix[1][2] - vb_y) * scale_y,
        0.0,
        0.0,
        1.0,
    ]


def build_merged_bubble_svg_source(
    *,
    bubble_asset: ResolvedBubbleAsset,
    placements: list[dict[str, int]],
) -> tuple[str, int, int, int, int]:
    if not placements:
        raise RuntimeError("placements must be non-empty")
    if bubble_asset.source_kind != "svg":
        raise RuntimeError("merged bubble SVG requires an SVG bubble asset")

    asset_svg = load_bubble_svg_source_from_asset(bubble_asset)
    request_paths: list[dict[str, object]] = []
    stroke_widths: list[float] = []
    for placement in placements:
        bubble_width = int(placement["width"])
        bubble_height = int(placement["height"])
        bubble_left = int(placement["left"])
        bubble_top = int(placement["top"])
        warped_svg = warp_svg_source_to_aspect(asset_svg, bubble_width / max(1, bubble_height))
        viewbox, path_specs, stroke_width = _extract_bubble_path_specs(warped_svg)
        vb_x, vb_y, vb_w, vb_h = viewbox
        scale_x = bubble_width / max(vb_w, 1e-6)
        scale_y = bubble_height / max(vb_h, 1e-6)
        stroke_widths.append(stroke_width * ((abs(scale_x) + abs(scale_y)) / 2.0))

        for d_value, source_matrix in path_specs:
            request_paths.append({
                "d": d_value,
                "matrix": _pathops_matrix(
                    source_matrix=source_matrix,
                    vb_x=vb_x,
                    vb_y=vb_y,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    bubble_left=bubble_left,
                    bubble_top=bubble_top,
                ),
            })

    if not request_paths:
        raise RuntimeError("failed to build merged bubble path request")

    merged_path: pathops.Path | None = None
    for spec in request_paths:
        current = pathops.Path()
        parse_path(spec["d"], current.getPen())
        matrix = spec["matrix"]
        transformed = current.transform(matrix[0], matrix[3], matrix[1], matrix[4], matrix[2], matrix[5])
        if merged_path is None:
            merged_path = transformed
        else:
            merged_path = pathops.op(merged_path, transformed, pathops.PathOp.UNION)
    if merged_path is None:
        raise RuntimeError("failed to build merged bubble path")

    path_pen = SVGPathPen(None)
    merged_path.draw(path_pen)
    left_bound, top_bound, right_bound, bottom_bound = merged_path.bounds
    stroke_width_px = max(0.75, (sum(stroke_widths) / max(1, len(stroke_widths))) * MERGED_BUBBLE_STROKE_SCALE)
    min_x = float(left_bound)
    min_y = float(top_bound)
    max_x = float(right_bound)
    max_y = float(bottom_bound)
    padding = max(2.0, stroke_width_px * 1.5)
    left = int(math.floor(min_x - padding))
    top = int(math.floor(min_y - padding))
    right = int(math.ceil(max_x + padding))
    bottom = int(math.ceil(max_y + padding))
    width = max(1, right - left)
    height = max(1, bottom - top)
    svg_source = f"""<svg xmlns="{SVG_NS}" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <path d="{path_pen.getCommands()}"
        transform="translate({-left} {-top})"
        fill="#ffffff"
        fill-opacity="{BUBBLE_FILL_OPACITY}"
        stroke="{BUBBLE_STROKE_COLOR}"
        stroke-width="{stroke_width_px:.3f}"
        stroke-linecap="round"
        stroke-linejoin="round"
        fill-rule="nonzero" />
</svg>"""
    return svg_source, left, top, width, height


def render_svg_with_resvg(
    svg_source: str,
    width: int,
    height: int,
    executable: str,
) -> Image.Image:
    return render_raw_svg_with_resvg(
        svg_source=build_bubble_svg_source(svg_source, width, height),
        width=width,
        height=height,
        executable=executable,
    )


def render_raw_svg_with_resvg(
    svg_source: str,
    width: int,
    height: int,
    executable: str,
    *,
    font_path: str | None = None,
    font_family: str | None = None,
) -> Image.Image:
    with tempfile.TemporaryDirectory(prefix="text-bubble-resvg-") as temp_dir:
        source_path = Path(temp_dir) / "input.svg"
        output_path = Path(temp_dir) / "output.png"
        source_path.write_text(svg_source, encoding="utf-8")
        command = [executable, "--width", str(width), "--height", str(height)]
        if font_family:
            command.extend(["--font-family", font_family])
        if font_path:
            command.extend(["--use-font-file", font_path])
        command.extend([str(source_path), str(output_path)])
        process = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            stderr = process.stderr.strip()
            stdout = process.stdout.strip()
            details = stderr or stdout or f"exit code {process.returncode}"
            raise RuntimeError(f"resvg failed: {details}")
        if not output_path.exists():
            raise RuntimeError("resvg did not produce output image")
        return Image.open(output_path).convert("RGBA")


def white_to_transparent(image: Image.Image, cutoff: int = 248) -> Image.Image:
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
    white_mask = np.all(rgba[:, :, :3] >= cutoff, axis=2)
    rgba[white_mask, 3] = 0
    rgba[~white_mask, 3] = 255
    return Image.fromarray(rgba, mode="RGBA")
