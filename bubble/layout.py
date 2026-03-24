from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Any

from bubble.glyph_paths import HarfBuzzGlyphPathRenderer
from bubble.models import BubblePlan, FONT_CANDIDATES, TEXT_COLUMN_GAP_RATIO
from bubble.vertical_uax import classify_text_clusters


DEFAULT_TEXT_LETTER_SPACING_PX = -1.0


def _shout_rect_rng(seed: int | None) -> Any | None:
    if seed is None:
        return None
    import random

    return random.Random(int(seed))


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _jitter_corner(
    point: tuple[float, float],
    *,
    orientation: str,
    rng: Any | None,
    tangent_jitter: float,
    inward_jitter: float,
) -> tuple[float, float]:
    if rng is None:
        return point
    x, y = point
    if orientation == "top-left":
        return (x + rng.uniform(-tangent_jitter, tangent_jitter), y + rng.uniform(-inward_jitter, inward_jitter))
    if orientation == "top-right":
        return (x + rng.uniform(-tangent_jitter, tangent_jitter), y + rng.uniform(-inward_jitter, inward_jitter))
    if orientation == "bottom-right":
        return (x + rng.uniform(-tangent_jitter, tangent_jitter), y + rng.uniform(-inward_jitter, inward_jitter))
    return (x + rng.uniform(-tangent_jitter, tangent_jitter), y + rng.uniform(-inward_jitter, inward_jitter))


def _clamp_direct_corner(
    point: tuple[float, float],
    *,
    orientation: str,
    text_left: float,
    text_top: float,
    text_right: float,
    text_bottom: float,
    clear_x: float,
    clear_y: float,
    bubble_width: float,
    bubble_height: float,
) -> tuple[float, float]:
    x, y = point
    if "left" in orientation:
        x = min(x, text_left)
    else:
        x = max(x, text_right)
    if "top" in orientation:
        y = min(y, text_top)
    else:
        y = max(y, text_bottom)
    return _clamp(x, 2.0, bubble_width - 2.0), _clamp(y, 2.0, bubble_height - 2.0)


def _edge_midpoints_direct(
    *,
    edge: str,
    start: tuple[float, float],
    end: tuple[float, float],
    inset_value: float,
    rng: Any | None,
    min_count: int,
    max_count: int,
    tangent_jitter: float,
    depth_jitter: float,
    bottom_vertex_bias: float,
    text_left: float,
    text_top: float,
    text_right: float,
    text_bottom: float,
    clear_x: float,
    clear_y: float,
) -> list[tuple[float, float]]:
    count = min_count
    if rng is not None and max_count > min_count:
        count = rng.randint(min_count, max_count)
    count = max(1, count)
    if count == 1:
        fractions = [0.5 if rng is None else rng.uniform(0.22, 0.78)]
    else:
        if rng is None:
            fractions = [1.0 / 3.0, 2.0 / 3.0]
        else:
            fractions = sorted([rng.uniform(0.18, 0.42), rng.uniform(0.58, 0.82)])
    points: list[tuple[float, float]] = []
    for fraction in fractions:
        if edge in {"top", "bottom"}:
            x = start[0] + (end[0] - start[0]) * fraction
            y = inset_value
            if edge == "bottom":
                y = y + (start[1] - y) * bottom_vertex_bias
            if rng is not None:
                x += rng.uniform(-tangent_jitter, tangent_jitter)
                y += rng.uniform(-depth_jitter, depth_jitter)
            x = _clamp(x, min(start[0], end[0]) + clear_x, max(start[0], end[0]) - clear_x)
            if edge == "top":
                y = min(y, text_top)
                y = max(y, min(start[1], end[1]))
            else:
                y = max(y, text_bottom)
                y = min(y, max(start[1], end[1]))
            points.append((x, y))
        else:
            x = inset_value
            y = start[1] + (end[1] - start[1]) * fraction
            if rng is not None:
                x += rng.uniform(-depth_jitter, depth_jitter)
                y += rng.uniform(-tangent_jitter, tangent_jitter)
            y = _clamp(y, min(start[1], end[1]) + clear_y, max(start[1], end[1]) - clear_y)
            if edge == "left":
                x = min(x, text_left)
                x = max(x, min(start[0], end[0]))
            else:
                x = max(x, text_right)
                x = min(x, max(start[0], end[0]))
            points.append((x, y))
    return points


def _edge_midpoints_anchored(
    *,
    edge: str,
    start: tuple[float, float],
    end: tuple[float, float],
    inset_anchor: tuple[float, float],
    rng: Any | None,
    min_count: int,
    max_count: int,
    tangent_jitter: float,
    depth_jitter: float,
    bottom_vertex_bias: float,
    text_left: float,
    text_top: float,
    text_right: float,
    text_bottom: float,
    clear_x: float,
    clear_y: float,
    single_fraction_min: float = 0.32,
    single_fraction_max: float = 0.68,
) -> list[tuple[float, float]]:
    count = min_count
    if rng is not None and max_count > min_count:
        count = rng.randint(min_count, max_count)
    count = max(1, count)
    if count == 1:
        fractions = [0.5 if rng is None else rng.uniform(single_fraction_min, single_fraction_max)]
    else:
        if rng is None:
            fractions = [1.0 / 3.0, 2.0 / 3.0]
        else:
            fractions = sorted([rng.uniform(0.28, 0.40), rng.uniform(0.60, 0.72)])
    points: list[tuple[float, float]] = []
    for fraction in fractions:
        if edge in {"top", "bottom"}:
            x = start[0] + (end[0] - start[0]) * fraction
            y = inset_anchor[1]
            if edge == "bottom":
                y = y + (start[1] - y) * bottom_vertex_bias
            if rng is not None:
                x += rng.uniform(-tangent_jitter, tangent_jitter)
                y += rng.uniform(-depth_jitter, depth_jitter)
            x = _clamp(x, min(start[0], end[0]) + clear_x, max(start[0], end[0]) - clear_x)
            if edge == "top":
                outer_y = min(start[1], end[1])
                midpoint_limit = outer_y + (text_top - outer_y) * 0.5
                y = min(y, midpoint_limit)
                y = max(y, outer_y)
            else:
                outer_y = max(start[1], end[1])
                midpoint_limit = text_bottom + (outer_y - text_bottom) * 0.5
                y = max(y, midpoint_limit)
                y = min(y, outer_y)
            points.append((x, y))
        else:
            x = inset_anchor[0]
            y = start[1] + (end[1] - start[1]) * fraction
            if rng is not None:
                x += rng.uniform(-depth_jitter, depth_jitter)
                y += rng.uniform(-tangent_jitter, tangent_jitter)
            y = _clamp(y, min(start[1], end[1]) + clear_y, max(start[1], end[1]) - clear_y)
            if edge == "left":
                outer_x = min(start[0], end[0])
                midpoint_limit = outer_x + (text_left - outer_x) * 0.5
                x = min(x, midpoint_limit)
                x = max(x, outer_x)
            else:
                outer_x = max(start[0], end[0])
                midpoint_limit = text_right + (outer_x - text_right) * 0.5
                x = max(x, midpoint_limit)
                x = min(x, outer_x)
            points.append((x, y))
    return points


def _segment_control_point(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    pull: float,
    bow: float,
) -> tuple[float, float]:
    seg_x = end[0] - start[0]
    seg_y = end[1] - start[1]
    seg_len = max(1e-6, (seg_x * seg_x + seg_y * seg_y) ** 0.5)
    normal_x = -seg_y / seg_len
    normal_y = seg_x / seg_len
    control_x = start[0] * (1.0 - pull) + end[0] * pull + normal_x * bow
    control_y = start[1] * (1.0 - pull) + end[1] * pull + normal_y * bow
    return control_x, control_y


def _bias_kink_control_toward_midpoint(
    *,
    edge: str,
    start: tuple[float, float],
    end: tuple[float, float],
    control: tuple[float, float],
    start_is_midpoint: bool,
    end_is_midpoint: bool,
) -> tuple[float, float]:
    if start_is_midpoint == end_is_midpoint:
        return control
    x, y = control
    midpoint = start if start_is_midpoint else end
    if edge in {"top", "bottom"}:
        bias = 0.90 if end_is_midpoint else 0.10
        x = start[0] * (1.0 - bias) + end[0] * bias
        return x, y
    bias = 0.90 if end_is_midpoint else 0.10
    y = start[1] * (1.0 - bias) + end[1] * bias
    return x, y


def _overshoot_kink_control_past_midpoint(
    *,
    edge: str,
    start: tuple[float, float],
    end: tuple[float, float],
    control: tuple[float, float],
    start_is_midpoint: bool,
    end_is_midpoint: bool,
    entering_overshoot_ratio: float = 0.18,
    leaving_overshoot_ratio: float = 0.34,
) -> tuple[float, float]:
    if start_is_midpoint == end_is_midpoint:
        return control
    x, y = control
    if edge in {"top", "bottom"}:
        midpoint = end if end_is_midpoint else start
        span = abs(end[0] - start[0])
        overshoot = span * (entering_overshoot_ratio if end_is_midpoint else leaving_overshoot_ratio)
        direction = 1.0 if edge == "top" else -1.0
        x = midpoint[0] + direction * overshoot
        return x, y
    midpoint = end if end_is_midpoint else start
    span = abs(end[1] - start[1])
    overshoot = span * (entering_overshoot_ratio if end_is_midpoint else leaving_overshoot_ratio)
    direction = 1.0 if edge == "right" else -1.0
    y = midpoint[1] + direction * overshoot
    return x, y


def _clamp_direct_control_point(
    control: tuple[float, float],
    *,
    edge: str,
    start: tuple[float, float],
    end: tuple[float, float],
    start_is_midpoint: bool = False,
    end_is_midpoint: bool = False,
    text_left: float,
    text_top: float,
    text_right: float,
    text_bottom: float,
    clear_x: float,
    clear_y: float,
    bubble_width: float,
    bubble_height: float,
) -> tuple[float, float]:
    x, y = control
    if edge == "top":
        lower = min(start[0], end[0]) + clear_x * 0.25
        upper = max(start[0], end[0]) - clear_x * 0.25
        span = abs(end[0] - start[0])
        extra = span * 0.35
        if start_is_midpoint or end_is_midpoint:
            upper += extra
        x = _clamp(x, lower, upper)
        y = min(y, text_top)
        y = max(y, min(start[1], end[1]))
    elif edge == "bottom":
        lower = min(start[0], end[0]) + clear_x * 0.25
        upper = max(start[0], end[0]) - clear_x * 0.25
        span = abs(end[0] - start[0])
        extra = span * 0.35
        if start_is_midpoint or end_is_midpoint:
            lower -= extra
        x = _clamp(x, lower, upper)
        y = max(y, text_bottom)
        y = min(y, max(start[1], end[1]))
    elif edge == "left":
        lower = min(start[1], end[1]) + clear_y * 0.25
        upper = max(start[1], end[1]) - clear_y * 0.25
        span = abs(end[1] - start[1])
        extra = span * 0.35
        if start_is_midpoint or end_is_midpoint:
            lower -= extra
        y = _clamp(y, lower, upper)
        x = min(x, text_left)
        x = max(x, min(start[0], end[0]))
    else:
        lower = min(start[1], end[1]) + clear_y * 0.25
        upper = max(start[1], end[1]) - clear_y * 0.25
        span = abs(end[1] - start[1])
        extra = span * 0.35
        if start_is_midpoint or end_is_midpoint:
            upper += extra
        y = _clamp(y, lower, upper)
        x = max(x, text_right)
        x = min(x, max(start[0], end[0]))
    return _clamp(x, 2.0, bubble_width - 2.0), _clamp(y, 2.0, bubble_height - 2.0)


def _shout_rect_variant_spec(bubble_type: str) -> dict[str, float | str]:
    if bubble_type == "shout_rect_pointed":
        return {
            "curve_style": "smooth",
            "frame_pad_x_ratio": 0.76,
            "frame_pad_top_ratio": 1.36,
            "frame_pad_bottom_ratio": 1.10,
            "vertical_inward_ratio": 0.30,
            "side_inward_ratio": 0.46,
        }
    if bubble_type == "shout_rect_pointed_drop":
        return {
            "curve_style": "kinked",
            "frame_pad_x_ratio": 0.70,
            "frame_pad_top_ratio": 1.62,
            "frame_pad_bottom_ratio": 1.24,
            "vertical_inward_ratio": 0.30,
            "side_inward_ratio": 0.52,
        }
    return {
        "curve_style": "kinked",
        "frame_pad_x_ratio": 0.72,
        "frame_pad_top_ratio": 1.68,
        "frame_pad_bottom_ratio": 1.28,
        "vertical_inward_ratio": 0.30,
        "side_inward_ratio": 0.54,
    }


def _frame_edge_from_font(
    *,
    text_value: float,
    outer_gap: float,
    font_pad: float,
    axis_min: float,
    axis_max: float,
    from_start: bool,
) -> float:
    usable_gap = max(0.0, outer_gap - 2.0)
    pad = min(font_pad, usable_gap)
    if from_start:
        return _clamp(text_value - pad, axis_min, axis_max)
    return _clamp(text_value + pad, axis_min, axis_max)


def _frame_inset_anchor(
    *,
    edge: str,
    frame_left: float,
    frame_top: float,
    frame_right: float,
    frame_bottom: float,
    keepout_left: float,
    keepout_top: float,
    keepout_right: float,
    keepout_bottom: float,
    clear_x: float,
    clear_y: float,
    inward_ratio: float,
) -> tuple[float, float]:
    if edge == "top":
        limit = keepout_top
        return ((frame_left + frame_right) * 0.5, frame_top + (limit - frame_top) * inward_ratio)
    if edge == "bottom":
        limit = keepout_bottom
        return ((frame_left + frame_right) * 0.5, frame_bottom - (frame_bottom - limit) * inward_ratio)
    if edge == "left":
        limit = keepout_left
        return (frame_left + (limit - frame_left) * inward_ratio, (frame_top + frame_bottom) * 0.5)
    limit = keepout_right
    return (frame_right - (frame_right - limit) * inward_ratio, (frame_top + frame_bottom) * 0.5)


def _kink_bow_profile(edge: str, midpoint_count: int) -> tuple[float, float]:
    if midpoint_count <= 1:
        if edge == "top":
            return 3.80, 4.15
        if edge == "bottom":
            return 3.55, 3.90
        return 3.20, 3.55
    if edge == "top":
        return 2.95, 3.25
    if edge == "bottom":
        return 2.70, 3.00
    return 2.45, 2.75


def compute_shout_rect_layout(
    *,
    bubble_type: str,
    bubble_width: int,
    bubble_height: int,
    text_bbox_local: tuple[int, int, int, int],
    bubble_box_local: tuple[int, int, int, int],
    font_size: int,
    variant_seed: int | None,
    bubble_params: dict[str, Any] | None,
) -> dict[str, Any]:
    params = dict(bubble_params or {})
    bubble_width_f = float(bubble_width)
    bubble_height_f = float(bubble_height)
    text_left = float(text_bbox_local[0])
    text_top = float(text_bbox_local[1])
    text_right = float(text_bbox_local[2])
    text_bottom = float(text_bbox_local[3])
    bubble_box_left = float(bubble_box_local[0])
    bubble_box_top = float(bubble_box_local[1])
    bubble_box_right = float(bubble_box_local[2])
    bubble_box_bottom = float(bubble_box_local[3])
    params.update(
        {
            "bubble_width": bubble_width_f,
            "bubble_height": bubble_height_f,
            "text_left": text_left,
            "text_top": text_top,
            "text_right": text_right,
            "text_bottom": text_bottom,
        }
    )
    pull = float(params.get("pull", 0.6))
    bow = float(params.get("bow", 16.0))
    side_bow = float(params.get("side_bow", bow))
    midpoint_count_min = int(params.get("midpoint_count_min", 1))
    midpoint_count_max = int(params.get("midpoint_count_max", 2))
    midpoint_tangent_jitter = float(params.get("midpoint_tangent_jitter", 0.0))
    midpoint_depth_jitter = float(params.get("midpoint_depth_jitter", 0.0))
    corner_tangent_jitter = float(params.get("corner_tangent_jitter", 0.0))
    corner_inward_jitter = float(params.get("corner_inward_jitter", 0.0))
    bottom_midpoint_vertex_bias = float(params.get("bottom_midpoint_vertex_bias", 0.0))
    rng = _shout_rect_rng(variant_seed)
    variant = _shout_rect_variant_spec(bubble_type)
    curve_style = str(variant["curve_style"])

    keepout_left = bubble_box_left
    keepout_right = bubble_box_right
    keepout_top = bubble_box_top
    keepout_bottom = bubble_box_bottom
    outer_left_gap = keepout_left
    outer_right_gap = bubble_width_f - keepout_right
    outer_top_gap = keepout_top
    outer_bottom_gap = bubble_height_f - keepout_bottom
    clear_x = 0.0
    clear_y = 0.0
    frame_pad_x = float(font_size) * float(variant["frame_pad_x_ratio"])
    frame_pad_top = float(font_size) * float(variant["frame_pad_top_ratio"])
    frame_pad_bottom = float(font_size) * float(variant["frame_pad_bottom_ratio"])

    left_x = _frame_edge_from_font(
        text_value=keepout_left,
        outer_gap=keepout_left,
        font_pad=frame_pad_x,
        axis_min=2.0,
        axis_max=keepout_left,
        from_start=True,
    )
    right_x = _frame_edge_from_font(
        text_value=keepout_right,
        outer_gap=bubble_width_f - keepout_right,
        font_pad=frame_pad_x,
        axis_min=keepout_right,
        axis_max=bubble_width_f - 2.0,
        from_start=False,
    )
    top_y = _frame_edge_from_font(
        text_value=keepout_top,
        outer_gap=keepout_top,
        font_pad=frame_pad_top,
        axis_min=2.0,
        axis_max=keepout_top,
        from_start=True,
    )
    bottom_y = _frame_edge_from_font(
        text_value=keepout_bottom,
        outer_gap=bubble_height_f - keepout_bottom,
        font_pad=frame_pad_bottom,
        axis_min=keepout_bottom,
        axis_max=bubble_height_f - 2.0,
        from_start=False,
    )

    corners = []
    for orientation, point in [
        ("top-left", (left_x, top_y)),
        ("top-right", (right_x, top_y)),
        ("bottom-right", (right_x, bottom_y)),
        ("bottom-left", (left_x, bottom_y)),
    ]:
        corner = _jitter_corner(
            point,
            orientation=orientation,
            rng=rng,
            tangent_jitter=corner_tangent_jitter,
            inward_jitter=corner_inward_jitter,
        )
        clamped_corner = _clamp_direct_corner(
            corner,
            orientation=orientation,
            text_left=keepout_left,
            text_top=keepout_top,
            text_right=keepout_right,
            text_bottom=keepout_bottom,
            clear_x=clear_x,
            clear_y=clear_y,
            bubble_width=bubble_width_f,
            bubble_height=bubble_height_f,
        )
        corners.append(clamped_corner)
    inset_anchors = {
        "top": _frame_inset_anchor(
            edge="top",
            frame_left=left_x,
            frame_top=top_y,
            frame_right=right_x,
            frame_bottom=bottom_y,
            keepout_left=keepout_left,
            keepout_top=keepout_top,
            keepout_right=keepout_right,
            keepout_bottom=keepout_bottom,
            clear_x=clear_x,
            clear_y=clear_y,
            inward_ratio=float(variant["vertical_inward_ratio"]),
        ),
        "right": _frame_inset_anchor(
            edge="right",
            frame_left=left_x,
            frame_top=top_y,
            frame_right=right_x,
            frame_bottom=bottom_y,
            keepout_left=keepout_left,
            keepout_top=keepout_top,
            keepout_right=keepout_right,
            keepout_bottom=keepout_bottom,
            clear_x=clear_x,
            clear_y=clear_y,
            inward_ratio=float(variant["side_inward_ratio"]),
        ),
        "bottom": _frame_inset_anchor(
            edge="bottom",
            frame_left=left_x,
            frame_top=top_y,
            frame_right=right_x,
            frame_bottom=bottom_y,
            keepout_left=keepout_left,
            keepout_top=keepout_top,
            keepout_right=keepout_right,
            keepout_bottom=keepout_bottom,
            clear_x=clear_x,
            clear_y=clear_y,
            inward_ratio=float(variant["vertical_inward_ratio"]),
        ),
        "left": _frame_inset_anchor(
            edge="left",
            frame_left=left_x,
            frame_top=top_y,
            frame_right=right_x,
            frame_bottom=bottom_y,
            keepout_left=keepout_left,
            keepout_top=keepout_top,
            keepout_right=keepout_right,
            keepout_bottom=keepout_bottom,
            clear_x=clear_x,
            clear_y=clear_y,
            inward_ratio=float(variant["side_inward_ratio"]),
        ),
    }

    edges: list[dict[str, Any]] = []
    side_pull_bias = 0.12
    side_bow_scale = 0.58
    for edge_index, (edge, corner_a, corner_b) in enumerate(
        [
            ("top", corners[0], corners[1]),
            ("right", corners[1], corners[2]),
            ("bottom", corners[2], corners[3]),
            ("left", corners[3], corners[0]),
        ]
    ):
        midpoints = _edge_midpoints_anchored(
            edge=edge,
            start=corner_a,
            end=corner_b,
            inset_anchor=inset_anchors[edge],
            rng=rng,
            min_count=midpoint_count_min,
            max_count=midpoint_count_max,
            tangent_jitter=midpoint_tangent_jitter,
            depth_jitter=midpoint_depth_jitter,
            bottom_vertex_bias=bottom_midpoint_vertex_bias,
            text_left=keepout_left,
            text_top=keepout_top,
            text_right=keepout_right,
            text_bottom=keepout_bottom,
            clear_x=clear_x,
            clear_y=clear_y,
            single_fraction_min=0.22,
            single_fraction_max=0.78,
        )
        if edge == "top":
            midpoint_limit = top_y + (keepout_top - top_y) * float(variant["vertical_inward_ratio"])
            midpoints = [(point[0], min(point[1], midpoint_limit)) for point in midpoints]
        elif edge == "bottom":
            midpoint_limit = bottom_y - (bottom_y - keepout_bottom) * float(variant["vertical_inward_ratio"])
            midpoints = [(point[0], max(point[1], midpoint_limit)) for point in midpoints]
        points = [corner_a, *midpoints, corner_b]
        controls: list[tuple[float, float]] = []
        is_side_edge = edge in {"left", "right"}
        local_bow = side_bow if is_side_edge else bow
        last_leaving_pull = pull
        last_leaving_bow = local_bow * 0.76
        for segment_index, (start, end) in enumerate(zip(points, points[1:])):
            kinked_here = curve_style == "kinked"
            if curve_style == "mixed-kink":
                kinked_here = rng.random() < 0.55 if rng is not None else (edge_index + segment_index) % 2 == 0
            if kinked_here:
                entering_mult, leaving_mult = _kink_bow_profile(edge, len(midpoints))
                if len(midpoints) == 1:
                    local_pull = min(0.84, pull + 0.02)
                    local_bow_value = local_bow * entering_mult
                    last_leaving_pull = max(0.18, pull - 0.20)
                    last_leaving_bow = local_bow * leaving_mult
                else:
                    local_pull = min(0.86, pull + 0.05)
                    local_bow_value = local_bow * entering_mult
                    last_leaving_pull = max(0.16, pull - 0.14)
                    last_leaving_bow = local_bow * leaving_mult
            else:
                local_pull = pull
                local_bow_value = local_bow * (0.92 if edge == "top" else 0.76)
                last_leaving_pull = pull
                last_leaving_bow = local_bow * (0.92 if edge == "top" else 0.76)
            if is_side_edge:
                local_pull = min(0.92, local_pull + side_pull_bias)
                if segment_index == len(midpoints):
                    last_leaving_pull = min(0.92, last_leaving_pull + side_pull_bias)
                if kinked_here:
                    local_bow_value *= 1.18 if len(midpoints) == 1 else 1.04
                else:
                    local_bow_value *= side_bow_scale
                if segment_index == len(midpoints):
                    if kinked_here:
                        last_leaving_bow *= 1.28 if len(midpoints) == 1 else 1.12
                    else:
                        last_leaving_bow *= side_bow_scale
            is_last_segment = segment_index == len(points) - 2
            pull_value = last_leaving_pull if is_last_segment else local_pull
            bow_value = last_leaving_bow if is_last_segment else local_bow_value
            start_is_midpoint = segment_index > 0
            end_is_midpoint = segment_index < len(midpoints)
            if kinked_here:
                if end_is_midpoint and not start_is_midpoint:
                    pull_value = 0.995
                    bow_value *= 3.10
                elif start_is_midpoint and not end_is_midpoint:
                    pull_value = 0.005
                    bow_value *= 3.10
                elif start_is_midpoint and end_is_midpoint:
                    pull_value = 0.50
            raw_control = _segment_control_point(start, end, pull=pull_value, bow=bow_value)
            if kinked_here:
                raw_control = _bias_kink_control_toward_midpoint(
                    edge=edge,
                    start=start,
                    end=end,
                    control=raw_control,
                    start_is_midpoint=start_is_midpoint,
                    end_is_midpoint=end_is_midpoint,
                )
                raw_control = _overshoot_kink_control_past_midpoint(
                    edge=edge,
                    start=start,
                    end=end,
                    control=raw_control,
                    start_is_midpoint=start_is_midpoint,
                    end_is_midpoint=end_is_midpoint,
                )
            controls.append(
                _clamp_direct_control_point(
                    raw_control,
                    edge=edge,
                    start=start,
                    end=end,
                    start_is_midpoint=start_is_midpoint,
                    end_is_midpoint=end_is_midpoint,
                    text_left=keepout_left,
                    text_top=keepout_top,
                    text_right=keepout_right,
                    text_bottom=keepout_bottom,
                    clear_x=clear_x,
                    clear_y=clear_y,
                    bubble_width=bubble_width_f,
                    bubble_height=bubble_height_f,
                )
            )
        edges.append({"edge": edge, "midpoints": midpoints, "controls": controls})

    return {
        "kind": "shout_rect",
        "bubble_type": bubble_type,
        "curve_style": curve_style,
        "seed": variant_seed,
        "text_bounds": [text_left, text_top, text_right, text_bottom],
        "keepout_bounds": [keepout_left, keepout_top, keepout_right, keepout_bottom],
        "bubble_box_bounds": [bubble_box_left, bubble_box_top, bubble_box_right, bubble_box_bottom],
        "frame": {
            "left": left_x,
            "top": top_y,
            "right": right_x,
            "bottom": bottom_y,
        },
        "corners": corners,
        "edges": edges,
        "clear_x": clear_x,
        "clear_y": clear_y,
        "view_box": [0.0, 0.0, bubble_width_f, bubble_height_f],
        "inset_anchors": inset_anchors,
        "params": params,
        "font_size": font_size,
    }


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
    bubble_type: str | None = None,
    variant_seed: int | None = None,
    bubble_params: dict[str, Any] | None = None,
    safe_inset: dict[str, float] | None = None,
    safe_padding: dict[str, float] | None = None,
) -> dict[str, int]:
    text_left, text_top, text_right, text_bottom = text_bbox
    text_width = text_right - text_left
    text_height = text_bottom - text_top
    em = max(font_size, 24)
    horizontal_padding = max(outline_width * 6, int(round(em * 1.35)))
    vertical_padding = max(outline_width * 4, int(round(em * 1.0)))
    padding_left = horizontal_padding
    padding_right = horizontal_padding
    padding_top = vertical_padding
    padding_bottom = vertical_padding
    if safe_padding is not None:
        raw_min_px = safe_padding.get("min_px")
        raw_max_px = safe_padding.get("max_px")
        min_px = int(round(float(raw_min_px))) if raw_min_px is not None else None
        max_px = int(round(float(raw_max_px))) if raw_max_px is not None else None
        if min_px is not None and max_px is not None and max_px < min_px:
            max_px = min_px
        def _side_padding(side: str, fallback: int) -> int:
            em_value = float(safe_padding.get(side, 0.0))
            if em_value <= 0.0:
                return fallback
            padding = int(round(em * em_value))
            if min_px is not None:
                padding = max(min_px, padding)
            if max_px is not None:
                padding = min(max_px, padding)
            return padding
        padding_left = _side_padding("left", horizontal_padding)
        padding_right = _side_padding("right", horizontal_padding)
        padding_top = _side_padding("top", vertical_padding)
        padding_bottom = _side_padding("bottom", vertical_padding)
    bubble_width = text_width + padding_left + padding_right
    bubble_height = text_height + padding_top + padding_bottom
    if safe_inset is not None and safe_padding is None:
        inset_left = max(0.0, min(0.45, float(safe_inset.get("left", 0.0))))
        inset_right = max(0.0, min(0.45, float(safe_inset.get("right", 0.0))))
        inset_top = max(0.0, min(0.45, float(safe_inset.get("top", 0.0))))
        inset_bottom = max(0.0, min(0.45, float(safe_inset.get("bottom", 0.0))))
        usable_width_ratio = max(0.05, 1.0 - inset_left - inset_right)
        usable_height_ratio = max(0.05, 1.0 - inset_top - inset_bottom)
        bubble_width = max(bubble_width, int(math.ceil(text_width / usable_width_ratio)))
        bubble_height = max(bubble_height, int(math.ceil(text_height / usable_height_ratio)))

    if safe_padding is not None:
        bubble_left = text_left - padding_left
        bubble_top = text_top - padding_top
    elif safe_inset is not None:
        text_center_x = (text_left + text_right) / 2.0
        text_center_y = (text_top + text_bottom) / 2.0
        usable_width_ratio = max(0.05, 1.0 - inset_left - inset_right)
        usable_height_ratio = max(0.05, 1.0 - inset_top - inset_bottom)
        bubble_left = int(round(text_center_x - bubble_width * (inset_left + usable_width_ratio / 2.0)))
        bubble_top = int(round(text_center_y - bubble_height * (inset_top + usable_height_ratio / 2.0)))
    else:
        horizontal_slack = max(0, bubble_width - text_width)
        vertical_slack = max(0, bubble_height - text_height)
        bubble_left = text_left - horizontal_slack // 2
        bubble_top = text_top - vertical_slack // 2
    bubble_right = bubble_left + bubble_width
    bubble_bottom = bubble_top + bubble_height

    inner_bubble_left = bubble_left
    inner_bubble_top = bubble_top
    inner_bubble_right = bubble_right
    inner_bubble_bottom = bubble_bottom
    inner_bubble_width = bubble_width
    inner_bubble_height = bubble_height

    layout = {
        "bubble_left": bubble_left,
        "bubble_top": bubble_top,
        "bubble_right": bubble_right,
        "bubble_bottom": bubble_bottom,
        "bubble_width": bubble_width,
        "bubble_height": bubble_height,
        "padding_left": padding_left,
        "padding_right": padding_right,
        "padding_top": padding_top,
        "padding_bottom": padding_bottom,
        "outline_width": outline_width,
    }
    if bubble_type and bubble_type.startswith("shout_rect"):
        variant = _shout_rect_variant_spec(bubble_type)
        frame_pad_left = int(round(float(font_size) * float(variant["frame_pad_x_ratio"])))
        frame_pad_right = int(round(float(font_size) * float(variant["frame_pad_x_ratio"])))
        frame_pad_top = int(round(float(font_size) * float(variant["frame_pad_top_ratio"])))
        frame_pad_bottom = int(round(float(font_size) * float(variant["frame_pad_bottom_ratio"])))
        bubble_left = inner_bubble_left - frame_pad_left
        bubble_top = inner_bubble_top - frame_pad_top
        bubble_right = inner_bubble_right + frame_pad_right
        bubble_bottom = inner_bubble_bottom + frame_pad_bottom
        bubble_width = bubble_right - bubble_left
        bubble_height = bubble_bottom - bubble_top
        layout.update(
            {
                "bubble_left": bubble_left,
                "bubble_top": bubble_top,
                "bubble_right": bubble_right,
                "bubble_bottom": bubble_bottom,
                "bubble_width": bubble_width,
                "bubble_height": bubble_height,
                "inner_bubble_left": inner_bubble_left,
                "inner_bubble_top": inner_bubble_top,
                "inner_bubble_right": inner_bubble_right,
                "inner_bubble_bottom": inner_bubble_bottom,
                "inner_bubble_width": inner_bubble_width,
                "inner_bubble_height": inner_bubble_height,
                "frame_padding_left": frame_pad_left,
                "frame_padding_right": frame_pad_right,
                "frame_padding_top": frame_pad_top,
                "frame_padding_bottom": frame_pad_bottom,
            }
        )
        local_text_bbox = (
            int(text_left - bubble_left),
            int(text_top - bubble_top),
            int(text_right - bubble_left),
            int(text_bottom - bubble_top),
        )
        local_bubble_box = (
            frame_pad_left,
            frame_pad_top,
            frame_pad_left + inner_bubble_width,
            frame_pad_top + inner_bubble_height,
        )
        layout["shape_layout"] = compute_shout_rect_layout(
            bubble_type=bubble_type,
            bubble_width=bubble_width,
            bubble_height=bubble_height,
            text_bbox_local=local_text_bbox,
            bubble_box_local=local_bubble_box,
            font_size=font_size,
            variant_seed=variant_seed,
            bubble_params=bubble_params,
        )
    return layout
