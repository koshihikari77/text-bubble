from __future__ import annotations

from bisect import bisect_right
from math import cos, pi, sin
import json
from typing import Any
import xml.etree.ElementTree as ET

from bubble.models import BUBBLE_FILL_OPACITY, BUBBLE_STROKE_COLOR, SVG_NS


ET.register_namespace("", SVG_NS)


def svg_qname(local: str) -> str:
    return f"{{{SVG_NS}}}{local}"


def _normalize(x: float, y: float) -> tuple[float, float]:
    norm = (x * x + y * y) ** 0.5
    if norm <= 1e-6:
        return 0.0, 0.0
    return x / norm, y / norm


def _rotate(x: float, y: float, angle: float) -> tuple[float, float]:
    c = cos(angle)
    s = sin(angle)
    return x * c - y * s, x * s + y * c


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    ax = a2[0] - a1[0]
    ay = a2[1] - a1[1]
    bx = b2[0] - b1[0]
    by = b2[1] - b1[1]
    denom = _cross(ax, ay, bx, by)
    if abs(denom) < 1e-6:
        return False
    dx = b1[0] - a1[0]
    dy = b1[1] - a1[1]
    t = _cross(dx, dy, bx, by) / denom
    u = _cross(dx, dy, ax, ay) / denom
    eps = 1e-5
    return eps < t < 1.0 - eps and eps < u < 1.0 - eps


def _find_self_intersections(points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    count = len(points)
    hits: list[tuple[int, int]] = []
    for i in range(count):
        a1 = points[i]
        a2 = points[(i + 1) % count]
        for j in range(i + 2, count):
            if j == i or (j + 1) % count == i or (i + 1) % count == j:
                continue
            if i == 0 and j == count - 1:
                continue
            b1 = points[j]
            b2 = points[(j + 1) % count]
            if _segments_intersect(a1, a2, b1, b2):
                hits.append((i, j))
    return hits


def _circular_distance(a: int, b: int, size: int) -> int:
    raw = abs(a - b)
    return min(raw, size - raw)


def _dampen_near_intersections(
    *,
    base_points: list[tuple[float, float]],
    normals: list[tuple[float, float]],
    directions: list[tuple[float, float]],
    distances: list[float],
) -> tuple[list[tuple[float, float]], list[float]]:
    point_count = len(base_points)
    current_dirs = directions[:]
    current_distances = distances[:]

    for _ in range(6):
        points = [
            (
                base_points[i][0] + current_dirs[i][0] * current_distances[i],
                base_points[i][1] + current_dirs[i][1] * current_distances[i],
            )
            for i in range(point_count)
        ]
        hits = _find_self_intersections(points)
        if not hits:
            return current_dirs, current_distances

        severity = [0.0] * point_count
        for seg_a, seg_b in hits:
            pivots = (seg_a, (seg_a + 1) % point_count, seg_b, (seg_b + 1) % point_count)
            for pivot in pivots:
                for idx in range(point_count):
                    dist = _circular_distance(idx, pivot, point_count)
                    if dist > 4:
                        continue
                    severity[idx] = max(severity[idx], (5 - dist) / 5.0)

        next_dirs: list[tuple[float, float]] = []
        next_distances: list[float] = []
        for i in range(point_count):
            s = severity[i]
            normal_x, normal_y = normals[i]
            dir_x, dir_y = current_dirs[i]
            blend = 0.55 * s
            mixed_x = dir_x * (1.0 - blend) + normal_x * blend
            mixed_y = dir_y * (1.0 - blend) + normal_y * blend
            mixed_x, mixed_y = _normalize(mixed_x, mixed_y)
            next_dirs.append((mixed_x, mixed_y))
            next_distances.append(current_distances[i] * (1.0 - 0.24 * s))
        current_dirs = next_dirs
        current_distances = next_distances

    return current_dirs, current_distances


def _closed_catmull_rom_path(points: list[tuple[float, float]], tension: float = 0.72) -> str:
    if len(points) < 3:
        raise RuntimeError("at least 3 points are required")
    commands = [f"M {points[0][0]:.3f} {points[0][1]:.3f}"]
    factor = tension / 6.0
    count = len(points)
    for index in range(count):
        p0 = points[(index - 1) % count]
        p1 = points[index % count]
        p2 = points[(index + 1) % count]
        p3 = points[(index + 2) % count]
        c1x = p1[0] + (p2[0] - p0[0]) * factor
        c1y = p1[1] + (p2[1] - p0[1]) * factor
        c2x = p2[0] - (p3[0] - p1[0]) * factor
        c2y = p2[1] - (p3[1] - p1[1]) * factor
        commands.append(
            f"C {c1x:.3f} {c1y:.3f} {c2x:.3f} {c2y:.3f} {p2[0]:.3f} {p2[1]:.3f}"
        )
    commands.append("Z")
    return " ".join(commands)


def _rounded_bulged_points(
    points: list[tuple[float, float]],
    *,
    center_x: float,
    center_y: float,
    round_ratio: float,
    edge_bulge: list[float],
) -> list[tuple[float, float]]:
    expanded: list[tuple[float, float]] = []
    count = len(points)
    for index in range(count):
        start = points[index]
        end = points[(index + 1) % count]
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        seg_len = (dx * dx + dy * dy) ** 0.5 or 1.0
        ux = dx / seg_len
        uy = dy / seg_len
        nx = -uy
        ny = ux
        inset = seg_len * round_ratio
        entry = start[0] + ux * inset, start[1] + uy * inset
        exit = end[0] - ux * inset, end[1] - uy * inset
        mx = (start[0] + end[0]) / 2.0
        my = (start[1] + end[1]) / 2.0
        outward_sign = 1.0 if (mx - center_x) * nx + (my - center_y) * ny > 0 else -1.0
        bulge = edge_bulge[index % len(edge_bulge)] * outward_sign
        midpoint = mx + nx * bulge, my + ny * bulge
        expanded.extend([entry, midpoint, exit])
    return expanded


def _curved_polygon_path(
    points: list[tuple[float, float]],
    *,
    center_x: float,
    center_y: float,
    curve_depth: float,
    curved_edges: list[int],
) -> str:
    commands = [f"M {points[0][0]:.3f} {points[0][1]:.3f}"]
    curved = set(curved_edges)
    count = len(points)
    for index in range(count):
        start = points[index]
        end = points[(index + 1) % count]
        if index not in curved:
            commands.append(f"L {end[0]:.3f} {end[1]:.3f}")
            continue
        mid_x = (start[0] + end[0]) / 2.0
        mid_y = (start[1] + end[1]) / 2.0
        toward_center_x = center_x - mid_x
        toward_center_y = center_y - mid_y
        toward_len = (toward_center_x * toward_center_x + toward_center_y * toward_center_y) ** 0.5 or 1.0
        seg_len = ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5
        depth = min(curve_depth * seg_len, 12.0)
        control_x = mid_x + toward_center_x / toward_len * depth
        control_y = mid_y + toward_center_y / toward_len * depth
        commands.append(f"Q {control_x:.3f} {control_y:.3f} {end[0]:.3f} {end[1]:.3f}")
    commands.append("Z")
    return " ".join(commands)


def _bubble_svg(path_d: str, *, view_box: list[float]) -> str:
    vb_x, vb_y, vb_w, vb_h = view_box
    root = ET.Element(
        svg_qname("svg"),
        {
            "width": str(vb_w),
            "height": str(vb_h),
            "viewBox": f"{vb_x} {vb_y} {vb_w} {vb_h}",
        },
    )
    defs = ET.SubElement(root, svg_qname("defs"))
    style = ET.SubElement(defs, svg_qname("style"))
    style.text = (
        ":root {"
        f"--stroke: {BUBBLE_STROKE_COLOR};"
        "--strokeW: 6;"
        "--fill: #ffffff;"
        f"--fillOpacity: {BUBBLE_FILL_OPACITY};"
        "}"
        ".bubble {"
        "fill: var(--fill);"
        "fill-opacity: var(--fillOpacity);"
        "stroke: var(--stroke);"
        "stroke-width: var(--strokeW);"
        "stroke-linecap: round;"
        "stroke-linejoin: round;"
        "}"
    )
    ET.SubElement(root, svg_qname("path"), {"class": "bubble", "d": path_d})
    return ET.tostring(root, encoding="unicode")


def _json_list_numbers(value: Any, *, name: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"procedural bubble param '{name}' must be a non-empty list")
    result: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)):
            raise RuntimeError(f"procedural bubble param '{name}' must contain only numbers")
        result.append(float(item))
    return result


def _json_point_list(value: Any, *, name: str) -> list[tuple[float, float]]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"procedural bubble param '{name}' must be a non-empty list")
    points: list[tuple[float, float]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], (int, float))
            or not isinstance(item[1], (int, float))
        ):
            raise RuntimeError(f"procedural bubble param '{name}' must be a list of [x, y] pairs")
        points.append((float(item[0]), float(item[1])))
    return points


def generate_polygon_wavy_panel(params: dict[str, Any]) -> str:
    view_box = _json_list_numbers(params.get("view_box", [50, 50, 420, 660]), name="view_box")
    if len(view_box) != 4:
        raise RuntimeError("procedural bubble param 'view_box' must contain 4 numbers")
    points = _json_point_list(params["points"], name="points")
    round_ratio = float(params.get("round_ratio", 0.14))
    edge_bulge = _json_list_numbers(params["edge_bulge"], name="edge_bulge")
    tension = float(params.get("tension", 0.72))
    vb_x, vb_y, vb_w, vb_h = view_box
    center_x = vb_x + vb_w / 2.0
    center_y = vb_y + vb_h / 2.0
    expanded = _rounded_bulged_points(
        points,
        center_x=center_x,
        center_y=center_y,
        round_ratio=round_ratio,
        edge_bulge=edge_bulge,
    )
    return _bubble_svg(_closed_catmull_rom_path(expanded, tension=tension), view_box=view_box)


def generate_polygon_shout_panel(params: dict[str, Any]) -> str:
    view_box = _json_list_numbers(params.get("view_box", [50, 50, 420, 660]), name="view_box")
    if len(view_box) != 4:
        raise RuntimeError("procedural bubble param 'view_box' must contain 4 numbers")
    points = _json_point_list(params["points"], name="points")
    curved_edges = [int(value) for value in _json_list_numbers(params.get("curved_edges", []), name="curved_edges")]
    curve_depth = float(params.get("curve_depth", 0.06))
    vb_x, vb_y, vb_w, vb_h = view_box
    center_x = vb_x + vb_w / 2.0
    center_y = vb_y + vb_h / 2.0
    path_d = _curved_polygon_path(
        points,
        center_x=center_x,
        center_y=center_y,
        curve_depth=curve_depth,
        curved_edges=curved_edges,
    )
    return _bubble_svg(path_d, view_box=view_box)


def generate_bowed_rect_panel(params: dict[str, Any]) -> str:
    view_box = _json_list_numbers(params.get("view_box", [0, 0, 360, 600]), name="view_box")
    if len(view_box) != 4:
        raise RuntimeError("procedural bubble param 'view_box' must contain 4 numbers")
    vb_x, vb_y, vb_w, vb_h = view_box
    inset_x = float(params.get("inset_x", vb_w * 0.2))
    inset_y = float(params.get("inset_y", vb_h * 0.14))
    top_bulge = float(params.get("top_bulge", vb_h * 0.12))
    bottom_bulge = float(params.get("bottom_bulge", vb_h * 0.12))
    side_bulge = float(params.get("side_bulge", vb_w * 0.045))

    left = vb_x + inset_x
    right = vb_x + vb_w - inset_x
    top = vb_y + inset_y
    bottom = vb_y + vb_h - inset_y
    center_x = vb_x + vb_w / 2.0
    center_y = vb_y + vb_h / 2.0

    commands = [
        f"M {left:.3f} {top:.3f}",
        f"Q {center_x:.3f} {top + top_bulge:.3f} {right:.3f} {top:.3f}",
        f"Q {right - side_bulge:.3f} {center_y:.3f} {right:.3f} {bottom:.3f}",
        f"Q {center_x:.3f} {bottom - bottom_bulge:.3f} {left:.3f} {bottom:.3f}",
        f"Q {left + side_bulge:.3f} {center_y:.3f} {left:.3f} {top:.3f}",
        "Z",
    ]
    return _bubble_svg(" ".join(commands), view_box=view_box)


def _inset_point_rect_anchors(params: dict[str, Any]) -> tuple[list[float], list[tuple[float, float]]]:
    view_box = _json_list_numbers(params.get("view_box", [0, 0, 360, 600]), name="view_box")
    if len(view_box) != 4:
        raise RuntimeError("procedural bubble param 'view_box' must contain 4 numbers")
    vb_x, vb_y, vb_w, vb_h = view_box
    inset_left = float(params.get("inset_left", params.get("inset_x", vb_w * 0.16)))
    inset_right = float(params.get("inset_right", params.get("inset_x", vb_w * 0.16)))
    inset_top = float(params.get("inset_top", params.get("inset_y", vb_h * 0.1)))
    inset_bottom = float(params.get("inset_bottom", params.get("inset_y", vb_h * 0.1)))
    top_depth = float(params.get("top_depth", vb_h * 0.12))
    bottom_depth = float(params.get("bottom_depth", vb_h * 0.12))
    left_depth = float(params.get("left_depth", vb_w * 0.08))
    right_depth = float(params.get("right_depth", vb_w * 0.08))
    left = vb_x + inset_left
    right = vb_x + vb_w - inset_right
    top = vb_y + inset_top
    bottom = vb_y + vb_h - inset_bottom
    center_x = vb_x + vb_w / 2.0
    center_y = vb_y + vb_h / 2.0

    anchors = [
        (left, top),
        (center_x, top + top_depth),
        (right, top),
        (right - right_depth, center_y),
        (right, bottom),
        (center_x, bottom - bottom_depth),
        (left, bottom),
        (left + left_depth, center_y),
    ]
    return view_box, anchors


def _rng_for_params(params: dict[str, Any]) -> Any | None:
    seed = params.get("seed")
    if seed is None:
        return None
    import random

    return random.Random(int(seed))


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


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


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
                y = min(y, text_top - clear_y)
                y = max(y, min(start[1], end[1]) + clear_y * 0.5)
            else:
                y = max(y, text_bottom + clear_y)
                y = min(y, max(start[1], end[1]) - clear_y * 0.5)
            points.append((x, y))
        else:
            x = inset_value
            y = start[1] + (end[1] - start[1]) * fraction
            if rng is not None:
                x += rng.uniform(-depth_jitter, depth_jitter)
                y += rng.uniform(-tangent_jitter, tangent_jitter)
            y = _clamp(y, min(start[1], end[1]) + clear_y, max(start[1], end[1]) - clear_y)
            if edge == "left":
                x = min(x, text_left - clear_x)
                x = max(x, min(start[0], end[0]) + clear_x * 0.5)
            else:
                x = max(x, text_right + clear_x)
                x = min(x, max(start[0], end[0]) - clear_x * 0.5)
            points.append((x, y))
    return points


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
        x = min(x, text_left - clear_x)
    else:
        x = max(x, text_right + clear_x)
    if "top" in orientation:
        y = min(y, text_top - clear_y)
    else:
        y = max(y, text_bottom + clear_y)
    x = _clamp(x, 2.0, bubble_width - 2.0)
    y = _clamp(y, 2.0, bubble_height - 2.0)
    return x, y


def _direct_text_bounds(params: dict[str, Any], *, bubble_width: float, bubble_height: float) -> tuple[float, float, float, float]:
    text_left = params.get("text_left")
    text_top = params.get("text_top")
    text_right = params.get("text_right")
    text_bottom = params.get("text_bottom")
    if all(value is not None for value in (text_left, text_top, text_right, text_bottom)):
        return float(text_left), float(text_top), float(text_right), float(text_bottom)

    padding_left = float(params["padding_left"])
    padding_right = float(params["padding_right"])
    padding_top = float(params["padding_top"])
    padding_bottom = float(params["padding_bottom"])
    return (
        padding_left,
        padding_top,
        bubble_width - padding_right,
        bubble_height - padding_bottom,
    )


def _path_from_shout_rect_layout(shape_layout: dict[str, Any]) -> str:
    corners = [tuple(point) for point in shape_layout["corners"]]
    commands = [f"M {corners[0][0]:.3f} {corners[0][1]:.3f}"]
    for edge_index, edge in enumerate(shape_layout["edges"]):
        points = [corners[edge_index], *[tuple(point) for point in edge["midpoints"]], corners[(edge_index + 1) % 4]]
        controls = [tuple(point) for point in edge["controls"]]
        for control, end in zip(controls, points[1:]):
            commands.append(f"Q {control[0]:.3f} {control[1]:.3f} {end[0]:.3f} {end[1]:.3f}")
    commands.append("Z")
    return " ".join(commands)


def _clamp_direct_control_point(
    control: tuple[float, float],
    *,
    edge: str,
    start: tuple[float, float],
    end: tuple[float, float],
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
        x = _clamp(x, min(start[0], end[0]) + clear_x * 0.25, max(start[0], end[0]) - clear_x * 0.25)
        y = min(y, text_top - clear_y)
        y = max(y, min(start[1], end[1]) + clear_y * 0.35)
    elif edge == "bottom":
        x = _clamp(x, min(start[0], end[0]) + clear_x * 0.25, max(start[0], end[0]) - clear_x * 0.25)
        y = max(y, text_bottom + clear_y)
        y = min(y, max(start[1], end[1]) - clear_y * 0.35)
    elif edge == "left":
        y = _clamp(y, min(start[1], end[1]) + clear_y * 0.25, max(start[1], end[1]) - clear_y * 0.25)
        x = min(x, text_left - clear_x)
        x = max(x, min(start[0], end[0]) + clear_x * 0.35)
    else:
        y = _clamp(y, min(start[1], end[1]) + clear_y * 0.25, max(start[1], end[1]) - clear_y * 0.25)
        x = max(x, text_right + clear_x)
        x = min(x, max(start[0], end[0]) - clear_x * 0.35)
    x = _clamp(x, 2.0, bubble_width - 2.0)
    y = _clamp(y, 2.0, bubble_height - 2.0)
    return x, y


def _build_direct_shout_rect_geometry(params: dict[str, Any], *, curve_style: str) -> dict[str, Any]:
    bubble_width = float(params["bubble_width"])
    bubble_height = float(params["bubble_height"])
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
    rng = _rng_for_params(params)

    text_left, text_top, text_right, text_bottom = _direct_text_bounds(
        params,
        bubble_width=bubble_width,
        bubble_height=bubble_height,
    )
    outer_left_gap = text_left
    outer_right_gap = bubble_width - text_right
    outer_top_gap = text_top
    outer_bottom_gap = bubble_height - text_bottom

    clear_x = max(4.0, min(outer_left_gap, outer_right_gap) * 0.12)
    clear_y = max(4.0, min(outer_top_gap, outer_bottom_gap) * 0.12)

    top_y = max(2.0, min(text_top - clear_y * 2.0, outer_top_gap * 0.22))
    bottom_y = min(bubble_height - 2.0, max(text_bottom + clear_y * 2.0, bubble_height - outer_bottom_gap * 0.22))
    left_x = max(2.0, min(text_left - clear_x * 2.0, outer_left_gap * 0.22))
    right_x = min(bubble_width - 2.0, max(text_right + clear_x * 2.0, bubble_width - outer_right_gap * 0.22))

    top_mid_y = _clamp(text_top - max(clear_y, outer_top_gap * 0.32), top_y + clear_y, text_top - clear_y)
    bottom_mid_y = _clamp(text_bottom + max(clear_y, outer_bottom_gap * 0.32), text_bottom + clear_y, bottom_y - clear_y)
    left_mid_x = _clamp(text_left - max(clear_x, outer_left_gap * 0.32), left_x + clear_x, text_left - clear_x)
    right_mid_x = _clamp(text_right + max(clear_x, outer_right_gap * 0.32), text_right + clear_x, right_x - clear_x)

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
        corners.append(
            _clamp_direct_corner(
                corner,
                orientation=orientation,
                text_left=text_left,
                text_top=text_top,
                text_right=text_right,
                text_bottom=text_bottom,
                clear_x=clear_x,
                clear_y=clear_y,
                bubble_width=bubble_width,
                bubble_height=bubble_height,
            )
        )

    edge_specs = [
        ("top", corners[0], corners[1], top_mid_y),
        ("right", corners[1], corners[2], right_mid_x),
        ("bottom", corners[2], corners[3], bottom_mid_y),
        ("left", corners[3], corners[0], left_mid_x),
    ]

    commands = [f"M {corners[0][0]:.3f} {corners[0][1]:.3f}"]
    edges_debug: list[dict[str, Any]] = []
    for edge_index, (edge, corner_a, corner_b, inset_value) in enumerate(edge_specs):
        midpoints = _edge_midpoints_direct(
            edge=edge,
            start=corner_a,
            end=corner_b,
            inset_value=inset_value,
            rng=rng,
            min_count=midpoint_count_min,
            max_count=midpoint_count_max,
            tangent_jitter=midpoint_tangent_jitter,
            depth_jitter=midpoint_depth_jitter,
            bottom_vertex_bias=bottom_midpoint_vertex_bias,
            text_left=text_left,
            text_top=text_top,
            text_right=text_right,
            text_bottom=text_bottom,
            clear_x=clear_x,
            clear_y=clear_y,
        )
        current = corner_a
        edge_debug: dict[str, Any] = {"edge": edge, "midpoints": [], "controls": []}
        for midpoint_index, midpoint in enumerate(midpoints):
            kinked_here = curve_style == "kinked"
            if curve_style == "mixed-kink":
                kinked_here = rng.random() < 0.55 if rng is not None else (edge_index + midpoint_index) % 2 == 0
            is_side = edge in {"left", "right"}
            local_bow = side_bow if is_side else bow
            if kinked_here:
                local_pull = max(0.18, min(0.86, pull - 0.06 if len(midpoints) == 1 else pull))
                local_bow_enter = local_bow * (1.95 if len(midpoints) == 1 else 1.35)
                local_bow_leave = local_bow * (2.25 if len(midpoints) == 1 else 1.60)
            else:
                local_pull = pull
                local_bow_enter = local_bow
                local_bow_leave = local_bow
            cx, cy = _segment_control_point(current, midpoint, pull=local_pull, bow=local_bow_enter)
            cx, cy = _clamp_direct_control_point(
                (cx, cy),
                edge=edge,
                start=current,
                end=midpoint,
                text_left=text_left,
                text_top=text_top,
                text_right=text_right,
                text_bottom=text_bottom,
                clear_x=clear_x,
                clear_y=clear_y,
                bubble_width=bubble_width,
                bubble_height=bubble_height,
            )
            commands.append(f"Q {cx:.3f} {cy:.3f} {midpoint[0]:.3f} {midpoint[1]:.3f}")
            edge_debug["midpoints"].append(midpoint)
            edge_debug["controls"].append((cx, cy))
            current = midpoint
        cx, cy = _segment_control_point(current, corner_b, pull=max(0.18, pull - 0.08), bow=local_bow_leave if midpoints else bow)
        cx, cy = _clamp_direct_control_point(
            (cx, cy),
            edge=edge,
            start=current,
            end=corner_b,
            text_left=text_left,
            text_top=text_top,
            text_right=text_right,
            text_bottom=text_bottom,
            clear_x=clear_x,
            clear_y=clear_y,
            bubble_width=bubble_width,
            bubble_height=bubble_height,
        )
        commands.append(f"Q {cx:.3f} {cy:.3f} {corner_b[0]:.3f} {corner_b[1]:.3f}")
        edge_debug["controls"].append((cx, cy))
        edges_debug.append(edge_debug)
    commands.append("Z")
    return {
        "path_d": " ".join(commands),
        "view_box": [0.0, 0.0, bubble_width, bubble_height],
        "text_bounds": (text_left, text_top, text_right, text_bottom),
        "corners": corners,
        "edges": edges_debug,
        "clear_x": clear_x,
        "clear_y": clear_y,
    }


def _direct_shout_rect_svg(params: dict[str, Any], *, curve_style: str) -> str:
    shape_layout = params.get("shape_layout")
    if isinstance(shape_layout, dict):
        return _bubble_svg(
            _path_from_shout_rect_layout(shape_layout),
            view_box=[float(value) for value in shape_layout["view_box"]],
        )
    geometry = _build_direct_shout_rect_geometry(params, curve_style=curve_style)
    return _bubble_svg(geometry["path_d"], view_box=geometry["view_box"])


def _edge_midpoints(
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
) -> list[tuple[float, float]]:
    count = min_count
    if rng is not None and max_count > min_count:
        count = rng.randint(min_count, max_count)
    count = max(1, count)
    if count == 1:
        if rng is None:
            fractions = [0.5]
        else:
            fractions = [rng.uniform(0.22, 0.78)]
    else:
        if rng is None:
            fractions = [1.0 / 3.0, 2.0 / 3.0]
        else:
            left = rng.uniform(0.18, 0.42)
            right = rng.uniform(0.58, 0.82)
            fractions = sorted([left, right])
    points: list[tuple[float, float]] = []
    for fraction in fractions:
        if edge in {"top", "bottom"}:
            x = start[0] + (end[0] - start[0]) * fraction
            y = inset_anchor[1]
            if edge == "bottom":
                vertex_y = start[1]
                y = y + (vertex_y - y) * bottom_vertex_bias
            if rng is not None:
                x += rng.uniform(-tangent_jitter, tangent_jitter)
                y += rng.uniform(-depth_jitter, depth_jitter)
            points.append((x, y))
        else:
            x = inset_anchor[0]
            y = start[1] + (end[1] - start[1]) * fraction
            if rng is not None:
                x += rng.uniform(-depth_jitter, depth_jitter)
                y += rng.uniform(-tangent_jitter, tangent_jitter)
            points.append((x, y))
    return points


def _curved_pointed_rect_path(
    anchors: list[tuple[float, float]],
    *,
    pull: float,
    bow: float,
    shoulder: float,
    side_bow: float | None = None,
    side_shoulder: float | None = None,
    rng: Any | None = None,
    midpoint_count_min: int = 1,
    midpoint_count_max: int = 1,
    midpoint_tangent_jitter: float = 0.0,
    midpoint_depth_jitter: float = 0.0,
    corner_tangent_jitter: float = 0.0,
    corner_inward_jitter: float = 0.0,
    curve_style: str = "smooth",
    bottom_midpoint_vertex_bias: float = 0.0,
) -> str:
    pull = max(0.05, min(0.95, pull))
    bow = max(0.0, bow)
    shoulder = max(0.0, min(0.45, shoulder))
    resolved_side_bow = max(0.0, side_bow if side_bow is not None else bow)
    resolved_side_shoulder = max(0.0, min(0.45, side_shoulder if side_shoulder is not None else shoulder))
    side_pull_bias = 0.12
    side_bow_scale = 0.58
    jittered_corners = [
        _jitter_corner(anchors[0], orientation="top-left", rng=rng, tangent_jitter=corner_tangent_jitter, inward_jitter=corner_inward_jitter),
        _jitter_corner(anchors[2], orientation="top-right", rng=rng, tangent_jitter=corner_tangent_jitter, inward_jitter=corner_inward_jitter),
        _jitter_corner(anchors[4], orientation="bottom-right", rng=rng, tangent_jitter=corner_tangent_jitter, inward_jitter=corner_inward_jitter),
        _jitter_corner(anchors[6], orientation="bottom-left", rng=rng, tangent_jitter=corner_tangent_jitter, inward_jitter=corner_inward_jitter),
    ]
    commands = [f"M {jittered_corners[0][0]:.3f} {jittered_corners[0][1]:.3f}"]

    for index in range(0, len(anchors), 2):
        is_side_edge = index in {2, 6}
        local_bow = resolved_side_bow if is_side_edge else bow
        local_shoulder = resolved_side_shoulder if is_side_edge else shoulder
        corner_a = jittered_corners[index // 2]
        inset = anchors[(index + 1) % len(anchors)]
        corner_b = jittered_corners[(index // 2 + 1) % 4]
        edge = "top" if index == 0 else "right" if index == 2 else "bottom" if index == 4 else "left"
        midpoints = _edge_midpoints(
            edge=edge,
            start=corner_a,
            end=corner_b,
            inset_anchor=inset,
            rng=rng,
            min_count=midpoint_count_min,
            max_count=midpoint_count_max,
            tangent_jitter=midpoint_tangent_jitter,
            depth_jitter=midpoint_depth_jitter,
            bottom_vertex_bias=bottom_midpoint_vertex_bias,
        )
        current = corner_a
        last_leaving_pull = pull
        last_leaving_bow = local_bow * (0.72 + local_shoulder * 0.5)
        for midpoint_index, midpoint in enumerate(midpoints):
            kinked_here = curve_style == "kinked"
            if curve_style == "mixed-kink":
                if rng is None:
                    kinked_here = (midpoint_index + index // 2) % 2 == 0
                else:
                    kinked_here = rng.random() < 0.55
            if kinked_here:
                if len(midpoints) == 1:
                    entering_pull = min(0.84, pull + 0.02)
                    entering_bow = local_bow * 1.85
                    last_leaving_pull = max(0.18, pull - 0.20)
                    last_leaving_bow = local_bow * 2.15
                else:
                    entering_pull = min(0.86, pull + 0.05)
                    entering_bow = local_bow * 1.30
                    last_leaving_pull = max(0.16, pull - 0.14)
                    last_leaving_bow = local_bow * 1.55
            else:
                entering_pull = pull
                entering_bow = local_bow * (0.72 + local_shoulder * 0.5)
                last_leaving_pull = pull
                last_leaving_bow = local_bow * (0.72 + local_shoulder * 0.5)
            if is_side_edge:
                entering_pull = min(0.92, entering_pull + side_pull_bias)
                last_leaving_pull = min(0.92, last_leaving_pull + side_pull_bias)
                if kinked_here:
                    entering_bow *= 1.18 if len(midpoints) == 1 else 1.04
                    last_leaving_bow *= 1.28 if len(midpoints) == 1 else 1.12
                else:
                    entering_bow *= side_bow_scale
                    last_leaving_bow *= side_bow_scale
            control_x, control_y = _segment_control_point(
                current,
                midpoint,
                pull=entering_pull,
                bow=entering_bow,
            )
            commands.append(f"Q {control_x:.3f} {control_y:.3f} {midpoint[0]:.3f} {midpoint[1]:.3f}")
            current = midpoint
        control_x, control_y = _segment_control_point(
            current,
            corner_b,
            pull=last_leaving_pull,
            bow=last_leaving_bow,
        )
        commands.append(f"Q {control_x:.3f} {control_y:.3f} {corner_b[0]:.3f} {corner_b[1]:.3f}")

    commands.append("Z")
    return " ".join(commands)


def generate_pointed_rect_panel(params: dict[str, Any]) -> str:
    if "bubble_width" in params and "bubble_height" in params:
        return _direct_shout_rect_svg(params, curve_style="smooth")
    view_box, anchors = _inset_point_rect_anchors(params)
    pull = float(params.get("pull", 0.48))
    bow = float(params.get("bow", 22.0))
    shoulder = float(params.get("shoulder", 0.18))
    side_bow = float(params.get("side_bow", bow))
    side_shoulder = float(params.get("side_shoulder", shoulder))
    midpoint_count_min = int(params.get("midpoint_count_min", 1))
    midpoint_count_max = int(params.get("midpoint_count_max", 2))
    midpoint_tangent_jitter = float(params.get("midpoint_tangent_jitter", 10.0))
    midpoint_depth_jitter = float(params.get("midpoint_depth_jitter", 5.0))
    corner_tangent_jitter = float(params.get("corner_tangent_jitter", 4.0))
    corner_inward_jitter = float(params.get("corner_inward_jitter", 3.0))
    bottom_midpoint_vertex_bias = float(params.get("bottom_midpoint_vertex_bias", 0.42))
    return _bubble_svg(
        _curved_pointed_rect_path(
            anchors,
            pull=pull,
            bow=bow,
            shoulder=shoulder,
            side_bow=side_bow,
            side_shoulder=side_shoulder,
            rng=_rng_for_params(params),
            midpoint_count_min=midpoint_count_min,
            midpoint_count_max=midpoint_count_max,
            midpoint_tangent_jitter=midpoint_tangent_jitter,
            midpoint_depth_jitter=midpoint_depth_jitter,
            corner_tangent_jitter=corner_tangent_jitter,
            corner_inward_jitter=corner_inward_jitter,
            curve_style="smooth",
            bottom_midpoint_vertex_bias=bottom_midpoint_vertex_bias,
        ),
        view_box=view_box,
    )


def generate_pointed_rect_drop_panel(params: dict[str, Any]) -> str:
    if "bubble_width" in params and "bubble_height" in params:
        return _direct_shout_rect_svg(params, curve_style="kinked")
    view_box, anchors = _inset_point_rect_anchors(params)
    pull = float(params.get("pull", 0.82))
    bow = float(params.get("bow", 16.0))
    shoulder = float(params.get("shoulder", 0.1))
    side_bow = float(params.get("side_bow", bow))
    side_shoulder = float(params.get("side_shoulder", shoulder))
    midpoint_count_min = int(params.get("midpoint_count_min", 1))
    midpoint_count_max = int(params.get("midpoint_count_max", 2))
    midpoint_tangent_jitter = float(params.get("midpoint_tangent_jitter", 10.0))
    midpoint_depth_jitter = float(params.get("midpoint_depth_jitter", 5.0))
    corner_tangent_jitter = float(params.get("corner_tangent_jitter", 4.0))
    corner_inward_jitter = float(params.get("corner_inward_jitter", 3.0))
    bottom_midpoint_vertex_bias = float(params.get("bottom_midpoint_vertex_bias", 0.42))
    return _bubble_svg(
        _curved_pointed_rect_path(
            anchors,
            pull=pull,
            bow=bow,
            shoulder=shoulder,
            side_bow=side_bow,
            side_shoulder=side_shoulder,
            rng=_rng_for_params(params),
            midpoint_count_min=midpoint_count_min,
            midpoint_count_max=midpoint_count_max,
            midpoint_tangent_jitter=midpoint_tangent_jitter,
            midpoint_depth_jitter=midpoint_depth_jitter,
            corner_tangent_jitter=corner_tangent_jitter,
            corner_inward_jitter=corner_inward_jitter,
            curve_style="kinked",
            bottom_midpoint_vertex_bias=bottom_midpoint_vertex_bias,
        ),
        view_box=view_box,
    )


def generate_pointed_rect_kink_panel(params: dict[str, Any]) -> str:
    if "bubble_width" in params and "bubble_height" in params:
        return _direct_shout_rect_svg(params, curve_style="kinked")
    view_box, anchors = _inset_point_rect_anchors(params)
    pull = float(params.get("pull", 0.58))
    bow = float(params.get("bow", 24.0))
    shoulder = float(params.get("shoulder", 0.18))
    side_bow = float(params.get("side_bow", bow))
    side_shoulder = float(params.get("side_shoulder", shoulder))
    midpoint_count_min = int(params.get("midpoint_count_min", 1))
    midpoint_count_max = int(params.get("midpoint_count_max", 2))
    midpoint_tangent_jitter = float(params.get("midpoint_tangent_jitter", 10.0))
    midpoint_depth_jitter = float(params.get("midpoint_depth_jitter", 5.0))
    corner_tangent_jitter = float(params.get("corner_tangent_jitter", 4.0))
    corner_inward_jitter = float(params.get("corner_inward_jitter", 3.0))
    bottom_midpoint_vertex_bias = float(params.get("bottom_midpoint_vertex_bias", 0.42))
    return _bubble_svg(
        _curved_pointed_rect_path(
            anchors,
            pull=pull,
            bow=bow,
            shoulder=shoulder,
            side_bow=side_bow,
            side_shoulder=side_shoulder,
            rng=_rng_for_params(params),
            midpoint_count_min=midpoint_count_min,
            midpoint_count_max=midpoint_count_max,
            midpoint_tangent_jitter=midpoint_tangent_jitter,
            midpoint_depth_jitter=midpoint_depth_jitter,
            corner_tangent_jitter=corner_tangent_jitter,
            corner_inward_jitter=corner_inward_jitter,
            curve_style="kinked",
            bottom_midpoint_vertex_bias=bottom_midpoint_vertex_bias,
        ),
        view_box=view_box,
    )


def generate_directional_offset_hill(params: dict[str, Any]) -> str:
    view_box = _json_list_numbers(params.get("view_box", [0, 0, 360, 600]), name="view_box")
    if len(view_box) != 4:
        raise RuntimeError("procedural bubble param 'view_box' must contain 4 numbers")
    vb_x, vb_y, vb_w, vb_h = view_box
    cx = float(params.get("center_x", vb_x + vb_w / 2.0))
    cy = float(params.get("center_y", vb_y + vb_h / 2.0))
    rx = float(params.get("radius_x", 95.0))
    ry = float(params.get("radius_y", 210.0))
    n = int(params.get("samples", 128))
    phase = float(params.get("phase", 0.4))
    asymmetry = float(params.get("asymmetry", 0.15))
    radial_blend = float(params.get("radial_blend", 0.0))
    amp = float(params["amp"])
    freq = int(params["freq"])
    seed = int(params.get("seed", 9))

    import random

    rng = random.Random(seed)
    lobe_amp = [rng.uniform(0.62, 1.38) for _ in range(freq)]
    lobe_phase = [rng.uniform(-0.28, 0.28) for _ in range(freq)]
    lobe_width = [rng.uniform(0.78, 1.32) for _ in range(freq)]

    rng2 = random.Random(seed + 101)
    lobe_skew = [rng2.uniform(-0.18, 0.18) for _ in range(freq)]
    lobe_floor = [rng2.uniform(0.58, 0.78) for _ in range(freq)]
    lobe_angle = [rng2.uniform(-0.65, 0.65) for _ in range(freq)]
    lobe_tangent = [rng2.uniform(-0.08, 0.08) for _ in range(freq)]

    width_total = sum(lobe_width)
    width_cumulative = [0.0]
    acc = 0.0
    for width_scale in lobe_width:
        acc += width_scale / width_total * freq
        width_cumulative.append(acc)

    base_points: list[tuple[float, float]] = []
    normals: list[tuple[float, float]] = []
    directions: list[tuple[float, float]] = []
    distances: list[float] = []
    for i in range(n):
        t = 2 * pi * i / n
        x0 = rx * cos(t)
        y0 = ry * sin(t)
        base_points.append((cx + x0, cy + y0))

        nx, ny = _normalize(cos(t) / rx, sin(t) / ry)
        rx_dir, ry_dir = _normalize(x0, y0)
        tx, ty = -ny, nx
        normals.append((nx, ny))

        local_amp = amp * (0.78 + 0.22 * abs(sin(t)))
        side_envelope = 1.0 + asymmetry * (0.65 * sin(t - 0.45) + 0.35 * sin(2.0 * t + 1.2))
        phase_warp = asymmetry * 0.30 * sin(t + 0.6)

        lobe_pos = ((freq * t) / (2.0 * pi)) % freq
        lobe_slot = bisect_right(width_cumulative, lobe_pos) - 1
        lobe_index = max(0, min(freq - 1, lobe_slot))
        next_index = (lobe_index + 1) % freq
        span_start = width_cumulative[lobe_index]
        span_end = width_cumulative[lobe_index + 1]
        span = max(1e-6, span_end - span_start)
        mix = (lobe_pos - span_start) / span
        smooth_mix = mix * mix * (3.0 - 2.0 * mix)

        amp_scale = lobe_amp[lobe_index] * (1.0 - smooth_mix) + lobe_amp[next_index] * smooth_mix
        phase_offset = lobe_phase[lobe_index] * (1.0 - smooth_mix) + lobe_phase[next_index] * smooth_mix

        skew_mix = max(0.0, min(1.0, mix + lobe_skew[lobe_index]))
        hill = sin(pi * skew_mix)
        hill = max(0.0, hill) ** 1.55
        hill_floor = lobe_floor[lobe_index] * (1.0 - smooth_mix) + lobe_floor[next_index] * smooth_mix
        wave = hill_floor + (1.0 - hill_floor) * hill
        wave = wave * 2.0 - 1.0
        distance = local_amp * amp_scale * side_envelope * (0.70 + 0.42 * wave)

        direction_x = nx * (1.0 - radial_blend) + rx_dir * radial_blend
        direction_y = ny * (1.0 - radial_blend) + ry_dir * radial_blend
        direction_x, direction_y = _normalize(direction_x, direction_y)

        local_angle = lobe_angle[lobe_index] * (1.0 - smooth_mix) + lobe_angle[next_index] * smooth_mix
        local_angle += 0.10 * sin(3.0 * t - 0.6) + 0.05 * sin(5.0 * t + 0.9)
        direction_x, direction_y = _rotate(direction_x, direction_y, local_angle + phase_offset * 0.2)

        tangent_mix = lobe_tangent[lobe_index] * (1.0 - smooth_mix) + lobe_tangent[next_index] * smooth_mix
        direction_x += tx * tangent_mix
        direction_y += ty * tangent_mix
        direction_x, direction_y = _normalize(direction_x, direction_y)

        directions.append((direction_x, direction_y))
        distances.append(rx * distance)

    corrected_dirs, corrected_distances = _dampen_near_intersections(
        base_points=base_points,
        normals=normals,
        directions=directions,
        distances=distances,
    )
    points = [
        (
            base_points[i][0] + corrected_dirs[i][0] * corrected_distances[i],
            base_points[i][1] + corrected_dirs[i][1] * corrected_distances[i],
        )
        for i in range(n)
    ]
    path_d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in points) + " Z"
    return _bubble_svg(path_d, view_box=view_box)


PROCEDURAL_GENERATORS: dict[str, Any] = {
    "polygon_wavy_panel": generate_polygon_wavy_panel,
    "polygon_shout_panel": generate_polygon_shout_panel,
    "bowed_rect_panel": generate_bowed_rect_panel,
    "pointed_rect_panel": generate_pointed_rect_panel,
    "pointed_rect_drop_panel": generate_pointed_rect_drop_panel,
    "pointed_rect_kink_panel": generate_pointed_rect_kink_panel,
    "directional_offset_hill": generate_directional_offset_hill,
}


def generate_procedural_bubble_svg(generator: str, params: dict[str, Any]) -> str:
    builder = PROCEDURAL_GENERATORS.get(generator)
    if builder is None:
        raise RuntimeError(f"unknown procedural bubble generator: {generator}")
    return builder(params)


def procedural_asset_key(generator: str, params: dict[str, Any]) -> str:
    return f"procedural:{generator}:{json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
