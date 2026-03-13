from __future__ import annotations

import math
import os
from functools import lru_cache

from bubble.glyph_paths import HarfBuzzGlyphPathRenderer
from bubble.models import BubblePlan, FONT_CANDIDATES, TEXT_COLUMN_GAP_RATIO
from bubble.vertical_uax import classify_text_clusters


DEFAULT_TEXT_LETTER_SPACING_PX = -1.0


def _resolve_metric_font_path(font_path: str | None) -> str | None:
    if font_path and os.path.exists(font_path):
        return font_path
    for candidate in FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


@lru_cache(maxsize=8)
def _get_path_renderer(font_path: str | None) -> HarfBuzzGlyphPathRenderer | None:
    if not font_path:
        return None
    try:
        return HarfBuzzGlyphPathRenderer(font_path)
    except Exception:  # noqa: BLE001
        return None


def _shape_bounds_px(
    *,
    renderer: HarfBuzzGlyphPathRenderer,
    text: str,
    font_size: int,
    direction: str,
    features: dict[str, int] | None,
    rotate_90: bool,
) -> tuple[float, float] | None:
    shaped = renderer.shape_path(
        text,
        direction=direction,
        script="Jpan",
        language="ja",
        features=features,
    )
    if shaped.bounds is None:
        return None
    x_min, y_min, x_max, y_max = shaped.bounds
    scale = float(font_size) / float(max(1, int(shaped.upem)))
    width = max(1.0, float(x_max - x_min) * scale)
    height = max(1.0, float(y_max - y_min) * scale)
    if rotate_90:
        return height, width
    return width, height


def _measure_vertical_column(
    *,
    column: str,
    font_size: int,
    font_path: str | None,
    letter_spacing_px: float,
    resvg_tu_override: bool,
) -> tuple[int, int]:
    if not column:
        return 0, 0
    em = max(font_size, 24)
    nominal_column_width = max(26.0, float(int(round(em * 1.0))))
    grid_step = float(max(16, int(round(float(font_size) + letter_spacing_px))))
    decisions = classify_text_clusters(
        column,
        font_path=font_path,
        resvg_tu_override=resvg_tu_override,
    )
    if not decisions:
        return int(round(nominal_column_width)), 0

    renderer = _get_path_renderer(font_path)
    width_pad = max(2.0, em * 0.08)
    height_pad = max(2.0, em * 0.05)
    min_x = math.inf
    max_x = -math.inf
    min_y = math.inf
    max_y = -math.inf
    pen_y = 0.0

    for decision in decisions:
        bounds: tuple[float, float] | None = None
        if renderer is not None:
            if decision.action == "safe":
                bounds = _shape_bounds_px(
                    renderer=renderer,
                    text=decision.cluster,
                    font_size=font_size,
                    direction="ttb",
                    features={"vert": 1, "vrt2": 1},
                    rotate_90=False,
                )
            elif decision.action == "manual_sideways":
                bounds = _shape_bounds_px(
                    renderer=renderer,
                    text=decision.cluster,
                    font_size=font_size,
                    direction="ltr",
                    features=None,
                    rotate_90=True,
                )
            else:
                bounds = _shape_bounds_px(
                    renderer=renderer,
                    text=decision.cluster,
                    font_size=font_size,
                    direction="ltr",
                    features=None,
                    rotate_90=False,
                )

        if bounds is None:
            bounds = (nominal_column_width * 0.82, grid_step * 0.82)

        glyph_width = max(1.0, bounds[0] + width_pad)
        glyph_height = max(1.0, bounds[1] + height_pad)
        center_y = pen_y + grid_step / 2.0
        min_x = min(min_x, -glyph_width / 2.0)
        max_x = max(max_x, glyph_width / 2.0)
        min_y = min(min_y, center_y - glyph_height / 2.0)
        max_y = max(max_y, center_y + glyph_height / 2.0)
        pen_y += grid_step

    measured_width = max(1.0, max_x - min_x)
    measured_height = max(1.0, max_y - min_y)
    return int(math.ceil(measured_width)), int(math.ceil(measured_height))


def build_text_metrics(
    font_size: int,
    columns: list[str],
    *,
    font_path: str | None = None,
    letter_spacing_px: float = DEFAULT_TEXT_LETTER_SPACING_PX,
    resvg_tu_override: bool = True,
) -> dict[str, int]:
    em = max(font_size, 24)
    char_step = max(16, int(round(float(font_size) + letter_spacing_px)))
    column_gap = max(4, int(round(em * TEXT_COLUMN_GAP_RATIO)))
    resolved_font_path = _resolve_metric_font_path(font_path)

    measured_column_width = 0
    measured_block_height = 0
    for column in columns:
        column_width_px, column_height_px = _measure_vertical_column(
            column=column,
            font_size=font_size,
            font_path=resolved_font_path,
            letter_spacing_px=letter_spacing_px,
            resvg_tu_override=resvg_tu_override,
        )
        measured_column_width = max(measured_column_width, column_width_px)
        measured_block_height = max(measured_block_height, column_height_px)

    fallback_column_width = max(26, int(round(em * 1.0)))
    fallback_block_height = char_step * max(len(column) for column in columns)
    column_width = max(20, measured_column_width or fallback_column_width)
    block_width = column_width * len(columns) + column_gap * max(0, len(columns) - 1)
    block_height = max(1, measured_block_height or fallback_block_height)
    return {
        "em": em,
        "char_step": char_step,
        "column_width": column_width,
        "column_gap": column_gap,
        "block_width": block_width,
        "block_height": block_height,
    }


def compute_text_layout(
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    font_size: int,
    *,
    font_path: str | None = None,
    letter_spacing_px: float = DEFAULT_TEXT_LETTER_SPACING_PX,
    resvg_tu_override: bool = True,
) -> dict[str, int]:
    metrics = build_text_metrics(
        font_size,
        plan.columns,
        font_path=font_path,
        letter_spacing_px=letter_spacing_px,
        resvg_tu_override=resvg_tu_override,
    )
    char_step = metrics["char_step"]
    column_width = metrics["column_width"]
    column_gap = metrics["column_gap"]
    block_width = metrics["block_width"]
    block_height = metrics["block_height"]

    if block_width > canvas_width or block_height > canvas_height:
        raise RuntimeError("text block is larger than the image bounds")

    raw_anchor_x = int(canvas_width * plan.anchor_x)
    raw_anchor_y = int(canvas_height * plan.anchor_y)
    anchor_x = min(canvas_width, max(block_width, raw_anchor_x))
    anchor_y = min(max(0, raw_anchor_y), canvas_height - block_height)

    if anchor_x <= 0 or anchor_y < 0:
        raise RuntimeError("anchor point is outside the image")

    em = max(font_size, 24)
    text_left = anchor_x - block_width
    text_top = anchor_y
    text_right = anchor_x
    text_bottom = anchor_y + block_height
    if text_left < 0 or text_top < 0 or text_right > canvas_width or text_bottom > canvas_height:
        raise RuntimeError("text block layout exceeds image bounds")
    return {
        "font_size": font_size,
        "em": em,
        "char_step": char_step,
        "column_width": column_width,
        "column_gap": column_gap,
        "block_width": block_width,
        "block_height": block_height,
        "anchor_x": anchor_x,
        "anchor_y": anchor_y,
        "text_left": text_left,
        "text_top": text_top,
        "text_right": text_right,
        "text_bottom": text_bottom,
        "outline_width": max(3, canvas_width // 320),
    }


def compute_bubble_layout(
    canvas_width: int,
    canvas_height: int,
    text_bbox: tuple[int, int, int, int],
    text_layout: dict[str, int],
    font_size: int,
    outline_width: int,
) -> dict[str, int]:
    text_left, text_top, text_right, text_bottom = text_bbox
    text_width = text_right - text_left
    text_height = text_bottom - text_top
    em = max(font_size, 24)
    horizontal_padding = max(outline_width * 6, int(round(em * 1.35)))
    vertical_padding = max(outline_width * 4, int(round(em * 1.0)))
    bubble_width = text_width + horizontal_padding * 2
    bubble_height = text_height + vertical_padding * 2

    horizontal_slack = max(0, bubble_width - text_width)
    vertical_slack = max(0, bubble_height - text_height)
    bubble_left = text_left - horizontal_slack // 2
    bubble_top = text_top - vertical_slack // 2
    bubble_right = bubble_left + bubble_width
    bubble_bottom = bubble_top + bubble_height

    return {
        "bubble_left": bubble_left,
        "bubble_top": bubble_top,
        "bubble_right": bubble_right,
        "bubble_bottom": bubble_bottom,
        "bubble_width": bubble_width,
        "bubble_height": bubble_height,
        "outline_width": outline_width,
    }
