#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bubble_pipeline import (
    alpha_composite_clipped,
    bubble_png_to_rgba,
    build_bubble_svg_html,
    compute_bubble_layout,
    compute_text_layout,
    load_bubble_svg_source,
    load_plan_json,
    pick_font_path,
    render_text_overlay,
    resolve_bubble_asset,
    resolve_chromium_executable,
    DEFAULT_FONT_DIVISOR,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render an experiment with square bubble boxes using the existing SVG asset.")
    parser.add_argument("--input", required=True, help="Input image path")
    parser.add_argument("--plan-json", required=True, help="Plan JSON path")
    parser.add_argument("--output", required=True, help="Output image path")
    parser.add_argument("--font", help="Font path for bubble text")
    parser.add_argument("--font-family", help="CSS font-family override for browser rendering")
    parser.add_argument("--bubble-asset", help="Bubble image asset path")
    parser.add_argument("--font-size", default=0, type=int, help="Override vertical text font size")
    parser.add_argument(
        "--text-renderer",
        choices=["browser", "pango"],
        default="browser",
        help="Backend for vertical text rendering",
    )
    return parser.parse_args()


def squareify_layout(layout: dict[str, int]) -> dict[str, int]:
    square_size = max(layout["bubble_width"], layout["bubble_height"])
    center_x = (layout["bubble_left"] + layout["bubble_right"]) / 2.0
    center_y = (layout["bubble_top"] + layout["bubble_bottom"]) / 2.0
    bubble_left = int(round(center_x - square_size / 2.0))
    bubble_top = int(round(center_y - square_size / 2.0))
    return {
        **layout,
        "bubble_left": bubble_left,
        "bubble_top": bubble_top,
        "bubble_right": bubble_left + square_size,
        "bubble_bottom": bubble_top + square_size,
        "bubble_width": square_size,
        "bubble_height": square_size,
    }


def render_square_bubble(
    image_path: Path,
    output_path: Path,
    plans: list[Any],
    font_path: str | None,
    font_family: str | None,
    bubble_asset: Path,
    font_size: int,
    text_renderer: str,
) -> None:
    browser_root = Path(__file__).resolve().parent.parent / ".playwright-browsers"
    if browser_root.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
    chromium_executable = resolve_chromium_executable()

    base = Image.open(image_path).convert("RGBA")
    width_px, height_px = base.size

    for plan in plans:
        actual_font_size = font_size or max(22, min(48, height_px // DEFAULT_FONT_DIVISOR))
        text_layout = compute_text_layout(width_px, height_px, plan, actual_font_size)
        text_overlay = render_text_overlay(
            renderer=text_renderer,
            canvas_width=width_px,
            canvas_height=height_px,
            plan=plan,
            text_layout=text_layout,
            font_path=font_path,
            font_family=font_family,
        )
        bubble_layout = compute_bubble_layout(
            canvas_width=width_px,
            canvas_height=height_px,
            text_bbox=text_overlay.alpha_bbox,
            text_layout=text_layout,
            font_size=actual_font_size,
            outline_width=text_layout["outline_width"],
        )
        bubble_layout = squareify_layout(bubble_layout)

        if bubble_asset.suffix.lower() == ".png":
            bubble_image = bubble_png_to_rgba(bubble_asset).resize(
                (bubble_layout["bubble_width"], bubble_layout["bubble_height"]),
                Image.Resampling.LANCZOS,
            )
        else:
            with sync_playwright() as playwright:
                launch_kwargs: dict[str, Any] = {"headless": True}
                if chromium_executable:
                    launch_kwargs["executable_path"] = chromium_executable
                browser = playwright.chromium.launch(**launch_kwargs)
                bubble_page = browser.new_page(
                    viewport={"width": bubble_layout["bubble_width"], "height": bubble_layout["bubble_height"]},
                    device_scale_factor=1,
                )
                bubble_svg = load_bubble_svg_source(bubble_asset)
                bubble_page.set_content(
                    build_bubble_svg_html(
                        svg_source=bubble_svg,
                        width=bubble_layout["bubble_width"],
                        height=bubble_layout["bubble_height"],
                    ),
                    wait_until="load",
                )
                bubble_page.wait_for_load_state("networkidle")
                bubble_page.wait_for_timeout(100)
                bubble_image = Image.open(io.BytesIO(bubble_page.screenshot(omit_background=True))).convert("RGBA").resize(
                    (bubble_layout["bubble_width"], bubble_layout["bubble_height"]),
                    Image.Resampling.LANCZOS,
                )
                bubble_page.close()
                browser.close()

        alpha_composite_clipped(base, bubble_image, bubble_layout["bubble_left"], bubble_layout["bubble_top"])
        base.alpha_composite(text_overlay.image)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        base.convert("RGB").save(output_path, quality=95)
    else:
        base.save(output_path)


def main() -> int:
    args = parse_args()
    image_path = Path(args.input)
    plan_path = Path(args.plan_json)
    output_path = Path(args.output)
    if not image_path.exists():
        print(f"input image not found: {image_path}", file=sys.stderr)
        return 1
    if not plan_path.exists():
        print(f"plan JSON not found: {plan_path}", file=sys.stderr)
        return 1

    font_path = pick_font_path(args.font)
    bubble_asset = resolve_bubble_asset(args.bubble_asset)
    if bubble_asset is None:
        print(f"bubble asset not found: {args.bubble_asset}", file=sys.stderr)
        return 1

    try:
        dialogue_lines, plans = load_plan_json(plan_path)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    render_square_bubble(
        image_path=image_path,
        output_path=output_path,
        plans=plans,
        font_path=font_path,
        font_family=args.font_family,
        bubble_asset=bubble_asset,
        font_size=args.font_size,
        text_renderer=args.text_renderer,
    )
    print(json.dumps({"output": str(output_path), "dialogue_lines": dialogue_lines}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
