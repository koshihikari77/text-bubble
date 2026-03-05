from __future__ import annotations

from bubble.models import BubblePlan, TEXT_COLUMN_GAP_RATIO


def build_text_metrics(font_size: int, columns: list[str]) -> dict[str, int]:
    em = max(font_size, 24)
    char_step = max(24, int(round(em * 1.08)))
    column_width = max(26, int(round(em * 1.0)))
    column_gap = max(4, int(round(em * TEXT_COLUMN_GAP_RATIO)))
    block_width = column_width * len(columns) + column_gap * max(0, len(columns) - 1)
    block_height = char_step * max(len(column) for column in columns)
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
) -> dict[str, int]:
    metrics = build_text_metrics(font_size, plan.columns)
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
