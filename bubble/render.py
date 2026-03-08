from __future__ import annotations

import html
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from bubble.assets import (
    build_merged_bubble_svg_source,
    build_bubble_svg_html,
    build_font_css,
    bubble_png_to_rgba,
    load_bubble_svg_source,
    render_raw_svg_with_resvg,
    render_svg_with_resvg,
    resolve_chromium_executable,
    resolve_resvg_executable,
    warp_svg_source_to_aspect,
)
from bubble.layout import compute_bubble_layout, compute_text_layout
from bubble.models import DEFAULT_FONT_DIVISOR, BubblePlan, TEXT_COLOR, TEXT_SHADOW, TextRenderResult
from bubble.text_render_resvg_hybrid import render_text_overlay_resvg_hybrid


@dataclass
class RenderedBubble:
    plan: BubblePlan
    text_overlay: TextRenderResult
    bubble_layout: dict[str, int]
    bubble_image: Image.Image


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


def _ensure_browser_env() -> None:
    browser_root = Path(__file__).resolve().parent.parent / ".playwright-browsers"
    if browser_root.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))


def render_text_overlay_browser(
    *,
    browser: Any,
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
) -> TextRenderResult:
    font_css, font_stack = build_font_css(font_path, font_family)
    html_doc = build_render_html(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        plan=plan,
        text_layout=text_layout,
        font_stack=font_stack,
        font_css=font_css,
    )

    page = browser.new_page(viewport={"width": canvas_width, "height": canvas_height}, device_scale_factor=1)
    try:
        page.set_content(html_doc, wait_until="domcontentloaded", timeout=120000)
        # Wait only for web fonts used in this page; avoid fixed sleep/network-idle cost.
        page.evaluate("() => document.fonts ? document.fonts.ready.then(() => true) : true")
        overlay_bytes = page.screenshot(omit_background=True)
    finally:
        page.close()

    overlay = Image.open(io.BytesIO(overlay_bytes)).convert("RGBA")
    return TextRenderResult(image=overlay, alpha_bbox=alpha_bbox_or_fail(overlay))


def render_text_overlay(
    renderer: str,
    *,
    browser: Any | None,
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
    resvg_executable: str | None,
    text_letter_spacing: str,
    text_word_spacing: str,
    resvg_tu_override: bool,
) -> TextRenderResult:
    if renderer == "browser":
        if browser is None:
            raise RuntimeError("browser renderer requires an active Chromium session")
        return render_text_overlay_browser(
            browser=browser,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            plan=plan,
            text_layout=text_layout,
            font_path=font_path,
            font_family=font_family,
        )
    if renderer == "resvg-hybrid":
        if not resvg_executable:
            raise RuntimeError("resvg-hybrid renderer requires resvg executable")
        image = render_text_overlay_resvg_hybrid(
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            plan=plan,
            text_layout=text_layout,
            font_path=font_path,
            font_family=font_family,
            text_letter_spacing=text_letter_spacing,
            text_word_spacing=text_word_spacing,
            resvg_tu_override=resvg_tu_override,
            resvg_executable=resvg_executable,
        )
        return TextRenderResult(image=image, alpha_bbox=alpha_bbox_or_fail(image))
    raise RuntimeError(f"unsupported text renderer: {renderer}")


def _render_bubble_svg_browser(
    *,
    browser: Any,
    svg_source: str,
    width: int,
    height: int,
) -> Image.Image:
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
    try:
        page.set_content(
            build_bubble_svg_html(
                svg_source=svg_source,
                width=width,
                height=height,
            ),
            wait_until="domcontentloaded",
            timeout=120000,
        )
        overlay_bytes = page.screenshot(omit_background=True)
    finally:
        page.close()
    return Image.open(io.BytesIO(overlay_bytes)).convert("RGBA")


def _bubble_cache_key(
    *,
    bubble_renderer: str,
    bubble_asset: Path,
    width: int,
    height: int,
) -> tuple[str, str, int, int]:
    return bubble_renderer, str(bubble_asset.resolve()), width, height


def _resolve_bubble_image(
    *,
    bubble_renderer: str,
    bubble_asset: Path,
    bubble_width: int,
    bubble_height: int,
    browser: Any | None,
    resvg_executable: str | None,
    cache: dict[tuple[str, str, int, int], Image.Image],
) -> Image.Image:
    cache_key = _bubble_cache_key(
        bubble_renderer=bubble_renderer,
        bubble_asset=bubble_asset,
        width=bubble_width,
        height=bubble_height,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if bubble_asset.suffix.lower() == ".png":
        image = bubble_png_to_rgba(bubble_asset).resize(
            (bubble_width, bubble_height),
            Image.Resampling.LANCZOS,
        )
        cache[cache_key] = image
        return image

    bubble_svg = warp_svg_source_to_aspect(
        load_bubble_svg_source(bubble_asset),
        bubble_width / max(1, bubble_height),
    )
    if bubble_renderer == "resvg":
        if not resvg_executable:
            raise RuntimeError("resvg not found; install resvg or use --bubble-renderer browser")
        image = render_svg_with_resvg(
            svg_source=bubble_svg,
            width=bubble_width,
            height=bubble_height,
            executable=resvg_executable,
        ).resize((bubble_width, bubble_height), Image.Resampling.LANCZOS)
        cache[cache_key] = image
        return image
    if bubble_renderer == "browser":
        if browser is None:
            raise RuntimeError("browser bubble renderer requires an active Chromium session")
        image = _render_bubble_svg_browser(
            browser=browser,
            svg_source=bubble_svg,
            width=bubble_width,
            height=bubble_height,
        ).resize((bubble_width, bubble_height), Image.Resampling.LANCZOS)
        cache[cache_key] = image
        return image
    raise RuntimeError(f"unsupported bubble renderer: {bubble_renderer}")


def _expanded_box(box: tuple[int, int, int, int], padding: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    return left - padding, top - padding, right + padding, bottom + padding


def _boxes_intersect(left_box: tuple[int, int, int, int], right_box: tuple[int, int, int, int]) -> bool:
    left_a, top_a, right_a, bottom_a = left_box
    left_b, top_b, right_b, bottom_b = right_box
    return not (right_a <= left_b or right_b <= left_a or bottom_a <= top_b or bottom_b <= top_a)


def _bubble_bounds(item: RenderedBubble) -> tuple[int, int, int, int]:
    return (
        item.bubble_layout["bubble_left"],
        item.bubble_layout["bubble_top"],
        item.bubble_layout["bubble_right"],
        item.bubble_layout["bubble_bottom"],
    )


def _merge_gap_px(left: RenderedBubble, right: RenderedBubble) -> int:
    outline = max(left.bubble_layout["outline_width"], right.bubble_layout["outline_width"])
    left_width = left.bubble_layout["bubble_width"]
    right_width = right.bubble_layout["bubble_width"]
    return max(outline * 4, min(left_width, right_width) // 8)


def _has_explicit_speaker_id(value: str) -> bool:
    normalized = value.strip()
    return bool(normalized) and not normalized.startswith("__")


def _should_merge_bubbles(left: RenderedBubble, right: RenderedBubble) -> bool:
    if not _has_explicit_speaker_id(left.plan.speaker_id) or not _has_explicit_speaker_id(right.plan.speaker_id):
        return False
    if left.plan.speaker_id != right.plan.speaker_id:
        return False
    left_ids = left.plan.sentence_ids
    right_ids = right.plan.sentence_ids
    if not left_ids or not right_ids:
        return False
    sentence_gap = min(abs(left_ids[0] - right_ids[-1]), abs(right_ids[0] - left_ids[-1]))
    if sentence_gap > 1:
        return False
    overlap_padding = max(1, _merge_gap_px(left, right) // 6)
    return _boxes_intersect(
        _expanded_box(_bubble_bounds(left), overlap_padding),
        _expanded_box(_bubble_bounds(right), overlap_padding),
    )


def _group_bubbles_for_merge(items: list[RenderedBubble]) -> list[list[RenderedBubble]]:
    if len(items) <= 1:
        return [[item] for item in items]
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left_index: int, right_index: int) -> None:
        root_left = find(left_index)
        root_right = find(right_index)
        if root_left != root_right:
            parent[root_right] = root_left

    for left_index in range(len(items)):
        for right_index in range(left_index + 1, len(items)):
            if _should_merge_bubbles(items[left_index], items[right_index]):
                union(left_index, right_index)

    grouped: dict[int, list[RenderedBubble]] = {}
    for index, item in enumerate(items):
        grouped.setdefault(find(index), []).append(item)
    return [
        sorted(group, key=lambda entry: entry.plan.sentence_ids[0] if entry.plan.sentence_ids else 0)
        for _, group in sorted(grouped.items(), key=lambda row: min(item.plan.sentence_ids[0] for item in row[1]))
    ]


def _render_merged_group_image(
    *,
    group: list[RenderedBubble],
    bubble_renderer: str,
    bubble_asset: Path,
    browser: Any | None,
    resvg_executable: str | None,
) -> tuple[Image.Image, int, int]:
    placements = [
        {
            "left": item.bubble_layout["bubble_left"],
            "top": item.bubble_layout["bubble_top"],
            "width": item.bubble_layout["bubble_width"],
            "height": item.bubble_layout["bubble_height"],
        }
        for item in group
    ]
    merged_svg, left, top, width, height = build_merged_bubble_svg_source(
        bubble_asset=bubble_asset,
        placements=placements,
    )
    if bubble_renderer == "resvg":
        if not resvg_executable:
            raise RuntimeError("resvg not found; install resvg or use --bubble-renderer browser")
        image = render_raw_svg_with_resvg(
            svg_source=merged_svg,
            width=width,
            height=height,
            executable=resvg_executable,
        )
    elif bubble_renderer == "browser":
        if browser is None:
            raise RuntimeError("browser bubble renderer requires an active Chromium session")
        image = _render_bubble_svg_browser(
            browser=browser,
            svg_source=merged_svg,
            width=width,
            height=height,
        )
    else:
        raise RuntimeError(f"unsupported bubble renderer: {bubble_renderer}")
    return image, left, top


def render_bubbles(
    image_path: Path,
    output_path: Path,
    plans: list[BubblePlan],
    font_path: str | None,
    font_family: str | None,
    bubble_asset: Path,
    font_size: int,
    text_renderer: str,
    bubble_renderer: str,
    text_letter_spacing: str,
    text_word_spacing: str,
    resvg_tu_override: bool,
) -> None:
    if not plans:
        raise RuntimeError("no bubble plans to render")
    if text_renderer not in {"browser", "resvg-hybrid"}:
        raise RuntimeError(f"unsupported text renderer: {text_renderer}")
    if bubble_renderer not in {"resvg", "browser"}:
        raise RuntimeError(f"unsupported bubble renderer: {bubble_renderer}")

    base = Image.open(image_path).convert("RGBA")
    width_px, height_px = base.size
    actual_font_size = font_size or max(22, min(48, height_px // DEFAULT_FONT_DIVISOR))
    bubble_cache: dict[tuple[str, str, int, int], Image.Image] = {}
    needs_resvg = bubble_renderer == "resvg" or text_renderer == "resvg-hybrid"
    resvg_executable = resolve_resvg_executable() if needs_resvg else None
    if needs_resvg and not resvg_executable:
        raise RuntimeError("resvg not found; install resvg or use text_renderer=browser with bubble_renderer=browser")

    def _render_with_browser(browser: Any | None) -> None:
        rendered_bubbles: list[RenderedBubble] = []
        for plan in plans:
            text_layout = compute_text_layout(width_px, height_px, plan, actual_font_size)
            text_overlay = render_text_overlay(
                renderer=text_renderer,
                browser=browser,
                canvas_width=width_px,
                canvas_height=height_px,
                plan=plan,
                text_layout=text_layout,
                font_path=font_path,
                font_family=font_family,
                resvg_executable=resvg_executable,
                text_letter_spacing=text_letter_spacing,
                text_word_spacing=text_word_spacing,
                resvg_tu_override=resvg_tu_override,
            )
            bubble_layout = compute_bubble_layout(
                canvas_width=width_px,
                canvas_height=height_px,
                text_bbox=text_overlay.alpha_bbox,
                text_layout=text_layout,
                font_size=actual_font_size,
                outline_width=text_layout["outline_width"],
            )
            bubble_image = _resolve_bubble_image(
                bubble_renderer=bubble_renderer,
                bubble_asset=bubble_asset,
                bubble_width=bubble_layout["bubble_width"],
                bubble_height=bubble_layout["bubble_height"],
                browser=browser,
                resvg_executable=resvg_executable,
                cache=bubble_cache,
            )
            rendered_bubbles.append(
                RenderedBubble(
                    plan=plan,
                    text_overlay=text_overlay,
                    bubble_layout=bubble_layout,
                    bubble_image=bubble_image,
                )
            )

        for group in _group_bubbles_for_merge(rendered_bubbles):
            use_vector_group_render = bubble_asset.suffix.lower() in {".svg", ".txt"}
            if len(group) == 1 and not use_vector_group_render:
                item = group[0]
                alpha_composite_clipped(
                    base,
                    item.bubble_image,
                    item.bubble_layout["bubble_left"],
                    item.bubble_layout["bubble_top"],
                )
                continue
            merged_image, left, top = _render_merged_group_image(
                group=group,
                bubble_renderer=bubble_renderer,
                bubble_asset=bubble_asset,
                browser=browser,
                resvg_executable=resvg_executable,
            )
            alpha_composite_clipped(base, merged_image, left, top)

        for item in rendered_bubbles:
            base.alpha_composite(item.text_overlay.image)

    needs_browser = text_renderer == "browser" or bubble_renderer == "browser"
    if needs_browser:
        from playwright.sync_api import sync_playwright

        _ensure_browser_env()
        chromium_executable = resolve_chromium_executable()
        with sync_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {"headless": True}
            if chromium_executable:
                launch_kwargs["executable_path"] = chromium_executable
            browser = playwright.chromium.launch(**launch_kwargs)
            try:
                _render_with_browser(browser)
            finally:
                browser.close()
    else:
        _render_with_browser(None)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        base.convert("RGB").save(output_path, quality=95)
    else:
        base.save(output_path)
