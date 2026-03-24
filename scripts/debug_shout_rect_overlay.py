#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bubble.assets import pick_font_path, resolve_bubble_renderable_asset, resolve_resvg_executable  # noqa: E402
from bubble.layout import compute_bubble_layout, compute_text_layout  # noqa: E402
from bubble.models import BubblePlan, DEFAULT_FONT_DIVISOR  # noqa: E402
from bubble.render import (  # noqa: E402
    _bubble_variant_seed,
    _resolve_bubble_image,
    alpha_composite_clipped,
    render_text_overlay,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render one bubble with debug guides.")
    parser.add_argument("--input", required=True, help="Input image path.")
    parser.add_argument("--output", required=True, help="Output image path.")
    parser.add_argument("--output-json", help="Optional JSON debug dump path.")
    parser.add_argument("--bubble-type", default="shout_rect_pointed_drop")
    parser.add_argument("--anchor-x", required=True, type=float)
    parser.add_argument("--anchor-y", required=True, type=float)
    parser.add_argument("--column", action="append", dest="columns", required=True, help="Repeat for each vertical column.")
    parser.add_argument("--font", help="Optional font path.")
    parser.add_argument("--font-family", help="Optional font family override.")
    parser.add_argument("--font-size", type=int, default=0)
    parser.add_argument("--speaker-id", default="")
    parser.add_argument("--sentence-id", type=int, default=1)
    return parser.parse_args()


def _quadratic_point(
    start: tuple[float, float],
    control: tuple[float, float],
    end: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    u = 1.0 - t
    x = u * u * start[0] + 2.0 * u * t * control[0] + t * t * end[0]
    y = u * u * start[1] + 2.0 * u * t * control[1] + t * t * end[1]
    return x, y


def _sample_shape_outline(shape_layout: dict[str, object], *, steps: int = 32) -> list[tuple[float, float]]:
    points = shape_layout.get("points")
    if isinstance(points, list) and points:
        return [tuple(point) for point in points]
    corners = [tuple(point) for point in shape_layout["corners"]]
    sampled: list[tuple[float, float]] = [corners[0]]
    for edge_index, edge in enumerate(shape_layout["edges"]):
        points = [corners[edge_index], *[tuple(point) for point in edge["midpoints"]], corners[(edge_index + 1) % 4]]
        controls = [tuple(point) for point in edge["controls"]]
        start = points[0]
        for control, end in zip(controls, points[1:]):
            for step in range(1, steps + 1):
                sampled.append(_quadratic_point(start, control, end, step / steps))
            start = end
    return sampled


def _translate_points(points: list[tuple[float, float]], dx: int, dy: int) -> list[tuple[float, float]]:
    return [(x + dx, y + dy) for x, y in points]


def _draw_marker(draw: ImageDraw.ImageDraw, point: tuple[float, float], *, color: tuple[int, int, int], radius: int) -> None:
    x, y = point
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline=(255, 255, 255), width=1)


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_json = Path(args.output_json) if args.output_json else output_path.with_suffix(".json")
    if not input_path.exists():
        raise SystemExit(f"input image not found: {input_path}")

    plan = BubblePlan(
        anchor_x=args.anchor_x,
        anchor_y=args.anchor_y,
        sentence_ids=[args.sentence_id],
        columns=list(args.columns),
        speaker_id=args.speaker_id,
        bubble_type=args.bubble_type,
    )

    base = Image.open(input_path).convert("RGBA")
    canvas_width, canvas_height = base.size
    font_path = pick_font_path(args.font)
    resvg_executable = resolve_resvg_executable()
    if not resvg_executable:
        raise SystemExit("resvg executable not found")

    font_size = args.font_size or max(22, min(48, canvas_height // DEFAULT_FONT_DIVISOR))
    text_layout = compute_text_layout(canvas_width, canvas_height, plan, font_size)
    text_overlay = render_text_overlay(
        renderer="resvg-hybrid",
        browser=None,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        plan=plan,
        text_layout=text_layout,
        font_path=font_path,
        font_family=args.font_family,
        resvg_executable=resvg_executable,
        text_letter_spacing="-1px",
        text_word_spacing="0",
        resvg_tu_override=True,
    )
    bubble_asset = resolve_bubble_renderable_asset(None, args.bubble_type, variant_seed=_bubble_variant_seed(plan))
    if bubble_asset is None:
        raise SystemExit(f"bubble asset not found for {args.bubble_type}")

    bubble_layout = compute_bubble_layout(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        text_bbox=text_overlay.alpha_bbox,
        text_layout=text_layout,
        font_size=font_size,
        outline_width=text_layout["outline_width"],
        bubble_type=args.bubble_type,
        variant_seed=_bubble_variant_seed(plan),
        bubble_params=bubble_asset.params,
        safe_inset=bubble_asset.safe_inset,
        safe_padding=bubble_asset.safe_padding,
    )
    local_text_bbox = (
        int(text_overlay.alpha_bbox[0] - bubble_layout["bubble_left"]),
        int(text_overlay.alpha_bbox[1] - bubble_layout["bubble_top"]),
        int(text_overlay.alpha_bbox[2] - bubble_layout["bubble_left"]),
        int(text_overlay.alpha_bbox[3] - bubble_layout["bubble_top"]),
    )
    bubble_image = _resolve_bubble_image(
        bubble_renderer="resvg",
        bubble_asset=bubble_asset,
        bubble_width=bubble_layout["bubble_width"],
        bubble_height=bubble_layout["bubble_height"],
        bubble_layout=bubble_layout,
        local_text_bbox=local_text_bbox,
        browser=None,
        resvg_executable=resvg_executable,
        cache={},
    )

    overlay = base.copy()
    alpha_composite_clipped(overlay, bubble_image, bubble_layout["bubble_left"], bubble_layout["bubble_top"])
    overlay.alpha_composite(text_overlay.image)
    draw = ImageDraw.Draw(overlay, "RGBA")

    text_bbox = text_overlay.alpha_bbox
    bubble_bbox = (
        bubble_layout["bubble_left"],
        bubble_layout["bubble_top"],
        bubble_layout["bubble_right"],
        bubble_layout["bubble_bottom"],
    )
    draw.rectangle(bubble_bbox, outline=(80, 150, 255, 255), width=2)
    draw.rectangle(text_bbox, outline=(255, 80, 80, 255), width=2)

    shape_layout = bubble_layout.get("shape_layout")
    if not isinstance(shape_layout, dict):
        raise SystemExit("shape_layout missing from bubble layout")

    keepout = shape_layout.get("keepout_bounds") or shape_layout.get("bubble_box_bounds")
    if isinstance(keepout, list) and len(keepout) == 4:
        keepout_global = (
            bubble_layout["bubble_left"] + keepout[0],
            bubble_layout["bubble_top"] + keepout[1],
            bubble_layout["bubble_left"] + keepout[2],
            bubble_layout["bubble_top"] + keepout[3],
        )
        draw.rectangle(keepout_global, outline=(255, 160, 0, 220), width=1)
    frame = shape_layout.get("frame")
    if isinstance(frame, dict):
        frame_global = (
            bubble_layout["bubble_left"] + frame["left"],
            bubble_layout["bubble_top"] + frame["top"],
            bubble_layout["bubble_left"] + frame["right"],
            bubble_layout["bubble_top"] + frame["bottom"],
        )
        draw.rectangle(frame_global, outline=(255, 220, 80, 220), width=1)

    outline_points = _translate_points(
        _sample_shape_outline(shape_layout),
        bubble_layout["bubble_left"],
        bubble_layout["bubble_top"],
    )
    if len(outline_points) >= 2:
        draw.line(outline_points + [outline_points[0]], fill=(0, 220, 255, 255), width=2)

    corners_raw = shape_layout.get("corners")
    if isinstance(corners_raw, list):
        corners = _translate_points([tuple(point) for point in corners_raw], bubble_layout["bubble_left"], bubble_layout["bubble_top"])
        for point in corners:
            _draw_marker(draw, point, color=(0, 255, 255), radius=4)

    edges_raw = shape_layout.get("edges")
    if isinstance(edges_raw, list) and isinstance(corners_raw, list):
        for edge_index, edge in enumerate(edges_raw):
            local_points = [
                tuple(corners_raw[edge_index]),
                *[tuple(point) for point in edge["midpoints"]],
                tuple(corners_raw[(edge_index + 1) % 4]),
            ]
            points = _translate_points(local_points, bubble_layout["bubble_left"], bubble_layout["bubble_top"])
            controls = _translate_points([tuple(point) for point in edge["controls"]], bubble_layout["bubble_left"], bubble_layout["bubble_top"])
            for point in points[1:-1]:
                _draw_marker(draw, point, color=(255, 0, 255), radius=4)
            for control in controls:
                _draw_marker(draw, control, color=(0, 220, 0), radius=3)
            for start, control, end in zip(points, controls, points[1:]):
                draw.line([start, control], fill=(0, 220, 0, 140), width=1)
                draw.line([control, end], fill=(0, 220, 0, 140), width=1)
    elif isinstance(shape_layout.get("points"), list):
        point_markers = _translate_points([tuple(point) for point in shape_layout["points"]], bubble_layout["bubble_left"], bubble_layout["bubble_top"])
        stride = max(1, len(point_markers) // 24)
        for point in point_markers[::stride]:
            _draw_marker(draw, point, color=(255, 0, 255), radius=3)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        overlay.convert("RGB").save(output_path, quality=95)
    else:
        overlay.save(output_path)

    payload = {
        "output": str(output_path),
        "input": str(input_path),
        "plan": {
            "anchor_x": args.anchor_x,
            "anchor_y": args.anchor_y,
            "columns": args.columns,
            "bubble_type": args.bubble_type,
            "sentence_id": args.sentence_id,
            "speaker_id": args.speaker_id,
        },
        "font_size": font_size,
        "text_bbox_global": {
            "left": text_bbox[0],
            "top": text_bbox[1],
            "right": text_bbox[2],
            "bottom": text_bbox[3],
        },
        "bubble_bbox_global": {
            "left": bubble_bbox[0],
            "top": bubble_bbox[1],
            "right": bubble_bbox[2],
            "bottom": bubble_bbox[3],
        },
        "local_text_bbox": {
            "left": local_text_bbox[0],
            "top": local_text_bbox[1],
            "right": local_text_bbox[2],
            "bottom": local_text_bbox[3],
        },
        "shape_layout": shape_layout,
        "legend": {
            "blue": "bubble bbox",
            "red": "text bbox",
            "orange": "inner bubble box / keepout",
            "yellow": "outer frame",
            "cyan": "corners",
            "magenta": "midpoints or sampled path points",
            "green": "quadratic controls and handles",
            "outline": "sampled outline",
        },
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "output_json": str(output_json)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
