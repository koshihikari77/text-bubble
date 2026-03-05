from __future__ import annotations

import html
import io
import os
from pathlib import Path
from typing import Any

from PIL import Image

from bubble.assets import (
    build_bubble_svg_html,
    build_font_css,
    bubble_png_to_rgba,
    load_bubble_svg_source,
    resolve_chromium_executable,
    warp_svg_source_to_aspect,
)
from bubble.layout import compute_bubble_layout, compute_text_layout
from bubble.models import DEFAULT_FONT_DIVISOR, BubblePlan, TEXT_COLOR, TEXT_SHADOW, TextRenderResult


def alpha_composite_clipped(base: Image.Image, overlay: Image.Image, left: int, top: int) -> None:
    src_left = max(0, -left)
    src_top = max(0, -top)
    dst_left = max(0, left)
    dst_top = max(0, top)
    width = min(overlay.width - src_left, base.width - dst_left)
    height = min(overlay.height - src_top, base.height - dst_top)
    if width <= 0 or height <= 0:
        return
    cropped = overlay.crop((src_left, src_top, src_left + width, src_top + height))
    base.alpha_composite(cropped, (dst_left, dst_top))


def build_render_html(
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_stack: str,
    font_css: str,
) -> str:
    text_columns = []
    for index, column in enumerate(plan.columns):
        left = text_layout["block_width"] - text_layout["column_width"] - index * (
            text_layout["column_width"] + text_layout["column_gap"]
        )
        text_columns.append(
            (
                '<div class="column" style="left:{left}px;width:{width}px;height:{height}px;line-height:{line_height}px">'
                "{text}</div>"
            ).format(
                left=left,
                width=text_layout["column_width"],
                height=text_layout["block_height"],
                line_height=text_layout["char_step"],
                text=html.escape(column),
            )
        )
    columns_html = "".join(text_columns)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
{font_css}
html, body {{
  margin: 0;
  width: {canvas_width}px;
  height: {canvas_height}px;
  overflow: hidden;
  background: transparent !important;
}}
.stage {{
  position: relative;
  width: {canvas_width}px;
  height: {canvas_height}px;
  overflow: hidden;
  background: transparent;
}}
.text-block {{
  position: absolute;
  z-index: 1;
  left: {text_layout["text_left"]}px;
  top: {text_layout["text_top"]}px;
  width: {text_layout["block_width"]}px;
  height: {text_layout["block_height"]}px;
}}
.column {{
  position: absolute;
  top: 0;
  writing-mode: vertical-rl;
  text-orientation: mixed;
  white-space: nowrap;
  font-family: {font_stack};
  font-size: {text_layout["font_size"]}px;
  font-weight: 500;
  color: {TEXT_COLOR};
  letter-spacing: 0;
  text-align: start;
  text-shadow: {TEXT_SHADOW};
}}
</style>
</head>
<body>
  <div class="stage">
    <div class="text-block">{columns_html}</div>
  </div>
</body>
</html>
"""


def alpha_bbox_or_fail(image: Image.Image) -> tuple[int, int, int, int]:
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        raise RuntimeError("text renderer produced an empty alpha layer")
    left, top, right, bottom = bbox
    return int(left), int(top), int(right), int(bottom)


def render_text_overlay_browser(
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
) -> TextRenderResult:
    from playwright.sync_api import sync_playwright

    browser_root = Path(__file__).resolve().parent.parent / ".playwright-browsers"
    if browser_root.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
    chromium_executable = resolve_chromium_executable()
    font_css, font_stack = build_font_css(font_path, font_family)
    html_doc = build_render_html(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        plan=plan,
        text_layout=text_layout,
        font_stack=font_stack,
        font_css=font_css,
    )

    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {"headless": True}
        if chromium_executable:
            launch_kwargs["executable_path"] = chromium_executable
        browser = playwright.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": canvas_width, "height": canvas_height}, device_scale_factor=1)
        page.set_content(html_doc, wait_until="load")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(150)
        overlay_bytes = page.screenshot(omit_background=True)
        page.close()
        browser.close()

    overlay = Image.open(io.BytesIO(overlay_bytes)).convert("RGBA")
    return TextRenderResult(image=overlay, alpha_bbox=alpha_bbox_or_fail(overlay))


def render_text_overlay(
    renderer: str,
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
) -> TextRenderResult:
    if renderer != "browser":
        raise RuntimeError(f"unsupported text renderer: {renderer}")
    return render_text_overlay_browser(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        plan=plan,
        text_layout=text_layout,
        font_path=font_path,
        font_family=font_family,
    )


def render_bubble(
    image_path: Path,
    output_path: Path,
    plan: BubblePlan,
    font_path: str | None,
    font_family: str | None,
    bubble_asset: Path,
    font_size: int,
    text_renderer: str,
) -> None:
    from playwright.sync_api import sync_playwright

    browser_root = Path(__file__).resolve().parent.parent / ".playwright-browsers"
    if browser_root.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
    chromium_executable = resolve_chromium_executable()
    image = Image.open(image_path)
    width_px, height_px = image.size
    image.close()
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
    output_path.parent.mkdir(parents=True, exist_ok=True)

    base = Image.open(image_path).convert("RGBA")

    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {"headless": True}
        if chromium_executable:
            launch_kwargs["executable_path"] = chromium_executable
        browser = playwright.chromium.launch(**launch_kwargs)
        if bubble_asset.suffix.lower() == ".png":
            bubble_image = bubble_png_to_rgba(bubble_asset).resize(
                (bubble_layout["bubble_width"], bubble_layout["bubble_height"]),
                Image.Resampling.LANCZOS,
            )
        else:
            bubble_page = browser.new_page(
                viewport={"width": bubble_layout["bubble_width"], "height": bubble_layout["bubble_height"]},
                device_scale_factor=1,
            )
            bubble_svg = warp_svg_source_to_aspect(
                load_bubble_svg_source(bubble_asset),
                bubble_layout["bubble_width"] / max(1, bubble_layout["bubble_height"]),
            )
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
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        base.convert("RGB").save(output_path, quality=95)
    else:
        base.save(output_path)


def render_bubbles(
    image_path: Path,
    output_path: Path,
    plans: list[BubblePlan],
    font_path: str | None,
    font_family: str | None,
    bubble_asset: Path,
    font_size: int,
    text_renderer: str,
) -> None:
    current_input = image_path
    if not plans:
        raise RuntimeError("no bubble plans to render")
    if len(plans) == 1:
        render_bubble(
            image_path=image_path,
            output_path=output_path,
            plan=plans[0],
            font_path=font_path,
            font_family=font_family,
            bubble_asset=bubble_asset,
            font_size=font_size,
            text_renderer=text_renderer,
        )
        return

    temp_dir = output_path.parent / ".tmp-bubble-render"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_paths: list[Path] = []
    for index, plan in enumerate(plans, start=1):
        target = output_path if index == len(plans) else temp_dir / f"render_{index:02d}.png"
        render_bubble(
            image_path=current_input,
            output_path=target,
            plan=plan,
            font_path=font_path,
            font_family=font_family,
            bubble_asset=bubble_asset,
            font_size=font_size,
            text_renderer=text_renderer,
        )
        if target != output_path:
            temp_paths.append(target)
            current_input = target

    for temp_path in temp_paths:
        if temp_path.exists():
            temp_path.unlink()
    if temp_dir.exists():
        try:
            temp_dir.rmdir()
        except OSError:
            pass
