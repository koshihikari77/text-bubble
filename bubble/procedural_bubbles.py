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
    "directional_offset_hill": generate_directional_offset_hill,
}


def generate_procedural_bubble_svg(generator: str, params: dict[str, Any]) -> str:
    builder = PROCEDURAL_GENERATORS.get(generator)
    if builder is None:
        raise RuntimeError(f"unknown procedural bubble generator: {generator}")
    return builder(params)


def procedural_asset_key(generator: str, params: dict[str, Any]) -> str:
    return f"procedural:{generator}:{json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
