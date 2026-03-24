from __future__ import annotations

import html
import io
import os
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from bubble.assets import (
    ResolvedBubbleAsset,
    build_merged_bubble_svg_source,
    build_merged_svg_source_from_svg_sources,
    build_bubble_svg_html,
    build_font_css,
    bubble_png_to_rgba,
    load_bubble_svg_source_from_asset,
    render_raw_svg_with_resvg,
    render_svg_with_resvg,
    resolve_bubble_renderable_asset,
    resolve_chromium_executable,
    resolve_resvg_executable,
    warp_svg_source_to_aspect,
)
from bubble.layout import compute_bubble_layout, compute_text_layout
from bubble.models import DEFAULT_FONT_DIVISOR, BubblePlan, TEXT_COLOR, TEXT_SHADOW, TextRenderResult
from bubble.procedural_bubbles import generate_procedural_bubble_svg
from bubble.text_render_resvg_hybrid import render_text_overlay_resvg_hybrid


TEXT_STAGE_GUIDE_FILL = (255, 255, 255, 232)
TEXT_STAGE_GUIDE_OUTLINE = (17, 17, 17, 180)
TEXT_STAGE_GUIDE_PAD_X = 12
TEXT_STAGE_GUIDE_PAD_Y = 14
TEXT_STAGE_GUIDE_RADIUS = 12
TEXT_STAGE_GUIDE_OUTLINE_WIDTH = 2
LOCAL_TEXT_STAGE_PADDING_RATIO = 1.2
_TEXT_OVERLAY_CACHE: dict[tuple[Any, ...], TextRenderResult] = {}
BUBBLE_TEXT_ALPHA_THRESHOLD = 96
BUBBLE_TEXT_SAFE_MARGIN_PX = 4
SAFE_INSET_GROWTH_FACTORS = (1.0, 1.05, 1.12, 1.22, 1.34, 1.48, 1.64)


def _parse_letter_spacing_px(value: str | None, default: float = -1.0) -> float:
    raw = (value or "").strip()
    if not raw:
        return default
    if raw.endswith("px"):
        raw = raw[:-2].strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _bubble_variant_seed(plan: BubblePlan) -> int:
    payload = "|".join(
        [
            plan.bubble_type,
            ",".join(str(sentence_id) for sentence_id in plan.sentence_ids),
            "\n".join(plan.columns),
            plan.speaker_id,
        ]
    ).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:4], "big")


@dataclass
class RenderedBubble:
    plan: BubblePlan
    text_overlay: TextRenderResult
    bubble_layout: dict[str, int]
    bubble_image: Image.Image
    bubble_asset: ResolvedBubbleAsset
    local_text_bbox: tuple[int, int, int, int]


@dataclass
class PreparedBubble:
    plan: BubblePlan
    text_overlay: TextRenderResult
    bubble_layout: dict[str, int]
    local_text_bbox: tuple[int, int, int, int]
    bubble_asset: ResolvedBubbleAsset


def _layout_with_safe_inset_growth(
    *,
    text_bbox: tuple[int, int, int, int],
    base_layout: dict[str, int],
    safe_inset: dict[str, float],
    scale: float,
) -> dict[str, int]:
    text_left, text_top, text_right, text_bottom = text_bbox
    inset_left = float(safe_inset.get("left", 0.0))
    inset_right = float(safe_inset.get("right", 0.0))
    inset_top = float(safe_inset.get("top", 0.0))
    inset_bottom = float(safe_inset.get("bottom", 0.0))
    usable_width_ratio = max(0.05, 1.0 - inset_left - inset_right)
    usable_height_ratio = max(0.05, 1.0 - inset_top - inset_bottom)
    bubble_width = max(1, int(round(base_layout["bubble_width"] * scale)))
    bubble_height = max(1, int(round(base_layout["bubble_height"] * scale)))
    text_center_x = (text_left + text_right) / 2.0
    text_center_y = (text_top + text_bottom) / 2.0
    bubble_left = int(round(text_center_x - bubble_width * (inset_left + usable_width_ratio / 2.0)))
    bubble_top = int(round(text_center_y - bubble_height * (inset_top + usable_height_ratio / 2.0)))
    bubble_right = bubble_left + bubble_width
    bubble_bottom = bubble_top + bubble_height
    return {
        **base_layout,
        "bubble_left": bubble_left,
        "bubble_top": bubble_top,
        "bubble_right": bubble_right,
        "bubble_bottom": bubble_bottom,
        "bubble_width": bubble_width,
        "bubble_height": bubble_height,
    }


def _text_fits_bubble_alpha(
    *,
    bubble_image: Image.Image,
    bubble_layout: dict[str, int],
    text_bbox: tuple[int, int, int, int],
) -> bool:
    alpha = bubble_image.getchannel("A")
    rel_left = text_bbox[0] - bubble_layout["bubble_left"]
    rel_top = text_bbox[1] - bubble_layout["bubble_top"]
    rel_right = text_bbox[2] - bubble_layout["bubble_left"]
    rel_bottom = text_bbox[3] - bubble_layout["bubble_top"]
    rel_left -= BUBBLE_TEXT_SAFE_MARGIN_PX
    rel_top -= BUBBLE_TEXT_SAFE_MARGIN_PX
    rel_right += BUBBLE_TEXT_SAFE_MARGIN_PX
    rel_bottom += BUBBLE_TEXT_SAFE_MARGIN_PX
    if rel_left < 0 or rel_top < 0 or rel_right > alpha.width or rel_bottom > alpha.height:
        return False
    crop = alpha.crop((rel_left, rel_top, rel_right, rel_bottom))
    return crop.getextrema()[0] >= BUBBLE_TEXT_ALPHA_THRESHOLD


def _fit_prepared_bubble_to_alpha(
    *,
    prepared: PreparedBubble,
    bubble_renderer: str,
    browser: Any | None,
    resvg_executable: str | None,
    cache: dict[tuple[str, str, int, int], Image.Image],
) -> tuple[dict[str, int], Image.Image]:
    bubble_layout = dict(prepared.bubble_layout)
    bubble_image = _resolve_bubble_image(
        bubble_renderer=bubble_renderer,
        bubble_asset=prepared.bubble_asset,
        bubble_width=bubble_layout["bubble_width"],
        bubble_height=bubble_layout["bubble_height"],
        bubble_layout=bubble_layout,
        local_text_bbox=prepared.local_text_bbox,
        browser=browser,
        resvg_executable=resvg_executable,
        cache=cache,
    )
    if prepared.plan.bubble_type.startswith("shout_rect"):
        return bubble_layout, bubble_image
    safe_inset = prepared.bubble_asset.safe_inset
    if not safe_inset:
        return bubble_layout, bubble_image
    if _text_fits_bubble_alpha(
        bubble_image=bubble_image,
        bubble_layout=bubble_layout,
        text_bbox=prepared.text_overlay.alpha_bbox,
    ):
        return bubble_layout, bubble_image

    for scale in SAFE_INSET_GROWTH_FACTORS[1:]:
        grown_layout = _layout_with_safe_inset_growth(
            text_bbox=prepared.text_overlay.alpha_bbox,
            base_layout=prepared.bubble_layout,
            safe_inset=safe_inset,
            scale=scale,
        )
        grown_image = _resolve_bubble_image(
            bubble_renderer=bubble_renderer,
            bubble_asset=prepared.bubble_asset,
            bubble_width=grown_layout["bubble_width"],
            bubble_height=grown_layout["bubble_height"],
            bubble_layout=grown_layout,
            local_text_bbox=prepared.local_text_bbox,
            browser=browser,
            resvg_executable=resvg_executable,
            cache=cache,
        )
        if _text_fits_bubble_alpha(
            bubble_image=grown_image,
            bubble_layout=grown_layout,
            text_bbox=prepared.text_overlay.alpha_bbox,
        ):
            return grown_layout, grown_image
        bubble_layout = grown_layout
        bubble_image = grown_image
    return bubble_layout, bubble_image


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


def _translate_bbox(box: tuple[int, int, int, int], dx: int, dy: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    return left + dx, top + dy, right + dx, bottom + dy


def _clone_text_render_result(result: TextRenderResult) -> TextRenderResult:
    return TextRenderResult(
        image=result.image.copy(),
        alpha_bbox=result.alpha_bbox,
        offset_left=result.offset_left,
        offset_top=result.offset_top,
    )


def _text_overlay_cache_key(
    *,
    renderer: str,
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
    text_letter_spacing: str,
    text_word_spacing: str,
    resvg_tu_override: bool,
) -> tuple[Any, ...]:
    return (
        renderer,
        canvas_width,
        canvas_height,
        tuple(plan.columns),
        plan.speaker_id,
        text_layout["font_size"],
        text_layout["block_width"],
        text_layout["block_height"],
        text_layout["column_width"],
        text_layout["column_gap"],
        text_layout["char_step"],
        text_layout["text_left"],
        text_layout["text_top"],
        font_path,
        font_family,
        text_letter_spacing,
        text_word_spacing,
        resvg_tu_override,
    )


def _local_text_stage(
    *,
    canvas_width: int,
    canvas_height: int,
    text_layout: dict[str, int],
    font_size: int,
) -> tuple[int, int, int, int, dict[str, int]]:
    pad = max(
        24,
        int(round(text_layout["outline_width"] * 8)),
        int(round(max(font_size, 24) * LOCAL_TEXT_STAGE_PADDING_RATIO)),
    )
    stage_left = max(0, text_layout["text_left"] - pad)
    stage_top = max(0, text_layout["text_top"] - pad)
    stage_right = min(canvas_width, text_layout["text_right"] + pad)
    stage_bottom = min(canvas_height, text_layout["text_bottom"] + pad)
    local_layout = dict(text_layout)
    local_layout["anchor_x"] = text_layout["anchor_x"] - stage_left
    local_layout["anchor_y"] = text_layout["anchor_y"] - stage_top
    local_layout["text_left"] = text_layout["text_left"] - stage_left
    local_layout["text_top"] = text_layout["text_top"] - stage_top
    local_layout["text_right"] = text_layout["text_right"] - stage_left
    local_layout["text_bottom"] = text_layout["text_bottom"] - stage_top
    return stage_left, stage_top, stage_right, stage_bottom, local_layout


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
    bubble_asset: ResolvedBubbleAsset,
    width: int,
    height: int,
    bubble_layout: dict[str, int] | None = None,
    local_text_bbox: tuple[int, int, int, int] | None = None,
) -> tuple[Any, ...]:
    if bubble_asset.source_kind == "procedural" and bubble_layout is not None:
        return (
            bubble_renderer,
            bubble_asset.source_key,
            width,
            height,
            repr(bubble_layout.get("shape_layout")),
        )
    return bubble_renderer, bubble_asset.source_key, width, height


def _resolve_bubble_svg_source(
    *,
    bubble_asset: ResolvedBubbleAsset,
    bubble_width: int,
    bubble_height: int,
    bubble_layout: dict[str, int] | None,
    local_text_bbox: tuple[int, int, int, int] | None,
) -> str:
    if bubble_asset.source_kind == "procedural":
        if bubble_asset.generator is None:
            raise RuntimeError("procedural bubble asset is missing generator")
        procedural_params = dict(bubble_asset.params or {})
        procedural_params.update(
            {
                "bubble_width": bubble_width,
                "bubble_height": bubble_height,
            }
        )
        if bubble_layout is not None:
            procedural_params["shape_layout"] = bubble_layout.get("shape_layout")
            for key in ("padding_left", "padding_right", "padding_top", "padding_bottom"):
                if key in bubble_layout:
                    procedural_params[key] = int(bubble_layout[key])
        if local_text_bbox is not None:
            procedural_params.update(
                {
                    "text_left": int(local_text_bbox[0]),
                    "text_top": int(local_text_bbox[1]),
                    "text_right": int(local_text_bbox[2]),
                    "text_bottom": int(local_text_bbox[3]),
                }
            )
        return generate_procedural_bubble_svg(bubble_asset.generator, procedural_params)
    return warp_svg_source_to_aspect(
        load_bubble_svg_source_from_asset(bubble_asset),
        bubble_width / max(1, bubble_height),
    )


def _resolve_bubble_image(
    *,
    bubble_renderer: str,
    bubble_asset: ResolvedBubbleAsset,
    bubble_width: int,
    bubble_height: int,
    bubble_layout: dict[str, int] | None,
    local_text_bbox: tuple[int, int, int, int] | None,
    browser: Any | None,
    resvg_executable: str | None,
    cache: dict[tuple[Any, ...], Image.Image],
) -> Image.Image:
    cache_key = _bubble_cache_key(
        bubble_renderer=bubble_renderer,
        bubble_asset=bubble_asset,
        width=bubble_width,
        height=bubble_height,
        bubble_layout=bubble_layout,
        local_text_bbox=local_text_bbox,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if bubble_asset.source_kind == "png":
        if bubble_asset.asset_path is None:
            raise RuntimeError("png bubble asset is missing asset_path")
        image = bubble_png_to_rgba(bubble_asset.asset_path).resize(
            (bubble_width, bubble_height),
            Image.Resampling.LANCZOS,
        )
        cache[cache_key] = image
        return image

    bubble_svg = _resolve_bubble_svg_source(
        bubble_asset=bubble_asset,
        bubble_width=bubble_width,
        bubble_height=bubble_height,
        bubble_layout=bubble_layout,
        local_text_bbox=local_text_bbox,
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
    if left.plan.bubble_type != right.plan.bubble_type:
        return False
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
    browser: Any | None,
    resvg_executable: str | None,
) -> tuple[Image.Image, int, int]:
    bubble_asset = group[0].bubble_asset
    if bubble_asset.source_kind == "procedural":
        placements = []
        for item in group:
            placements.append(
                {
                    "left": item.bubble_layout["bubble_left"],
                    "top": item.bubble_layout["bubble_top"],
                    "width": item.bubble_layout["bubble_width"],
                    "height": item.bubble_layout["bubble_height"],
                    "svg_source": _resolve_bubble_svg_source(
                        bubble_asset=item.bubble_asset,
                        bubble_width=item.bubble_layout["bubble_width"],
                        bubble_height=item.bubble_layout["bubble_height"],
                        bubble_layout=item.bubble_layout,
                        local_text_bbox=item.local_text_bbox,
                    ),
                }
            )
        merged_svg, left, top, width, height = build_merged_svg_source_from_svg_sources(
            placements=placements,
            stroke_width=bubble_asset.stroke_width,
        )
    else:
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


def _prepare_rendered_bubble(
    *,
    plan: BubblePlan,
    width_px: int,
    height_px: int,
    actual_font_size: int,
    browser: Any | None,
    text_renderer: str,
    font_path: str | None,
    font_family: str | None,
    bubble_asset_override: Path | None,
    resvg_executable: str | None,
    text_letter_spacing: str,
    text_word_spacing: str,
    resvg_tu_override: bool,
) -> PreparedBubble:
    bubble_asset = resolve_bubble_renderable_asset(
        str(bubble_asset_override) if bubble_asset_override is not None else None,
        bubble_type=plan.bubble_type,
        variant_seed=_bubble_variant_seed(plan),
    )
    if bubble_asset is None:
        raise RuntimeError(f"bubble asset not found for type: {plan.bubble_type}")
    text_layout = compute_text_layout(
        width_px,
        height_px,
        plan,
        actual_font_size,
        font_path=font_path,
        letter_spacing_px=_parse_letter_spacing_px(text_letter_spacing),
        resvg_tu_override=resvg_tu_override,
    )
    stage_left, stage_top, stage_right, stage_bottom, local_text_layout = _local_text_stage(
        canvas_width=width_px,
        canvas_height=height_px,
        text_layout=text_layout,
        font_size=actual_font_size,
    )
    local_canvas_width = stage_right - stage_left
    local_canvas_height = stage_bottom - stage_top
    cache_key = None
    if browser is None:
        cache_key = _text_overlay_cache_key(
            renderer=text_renderer,
            canvas_width=local_canvas_width,
            canvas_height=local_canvas_height,
            plan=plan,
            text_layout=local_text_layout,
            font_path=font_path,
            font_family=font_family,
            text_letter_spacing=text_letter_spacing,
            text_word_spacing=text_word_spacing,
            resvg_tu_override=resvg_tu_override,
        )
        cached_overlay = _TEXT_OVERLAY_CACHE.get(cache_key)
        if cached_overlay is not None:
            text_overlay = _clone_text_render_result(cached_overlay)
        else:
            text_overlay = render_text_overlay(
                renderer=text_renderer,
                browser=browser,
                canvas_width=local_canvas_width,
                canvas_height=local_canvas_height,
                plan=plan,
                text_layout=local_text_layout,
                font_path=font_path,
                font_family=font_family,
                resvg_executable=resvg_executable,
                text_letter_spacing=text_letter_spacing,
                text_word_spacing=text_word_spacing,
                resvg_tu_override=resvg_tu_override,
            )
            _TEXT_OVERLAY_CACHE[cache_key] = _clone_text_render_result(text_overlay)
    else:
        text_overlay = render_text_overlay(
            renderer=text_renderer,
            browser=browser,
            canvas_width=local_canvas_width,
            canvas_height=local_canvas_height,
            plan=plan,
            text_layout=local_text_layout,
            font_path=font_path,
            font_family=font_family,
            resvg_executable=resvg_executable,
            text_letter_spacing=text_letter_spacing,
            text_word_spacing=text_word_spacing,
            resvg_tu_override=resvg_tu_override,
        )
    global_text_bbox = _translate_bbox(text_overlay.alpha_bbox, stage_left, stage_top)
    if text_overlay.offset_left or text_overlay.offset_top:
        raise RuntimeError("unexpected nested text overlay offset")
    text_overlay = TextRenderResult(
        image=text_overlay.image,
        alpha_bbox=global_text_bbox,
        offset_left=stage_left,
        offset_top=stage_top,
    )
    bubble_layout = compute_bubble_layout(
        canvas_width=width_px,
        canvas_height=height_px,
        text_bbox=text_overlay.alpha_bbox,
        text_layout=text_layout,
        font_size=actual_font_size,
        outline_width=text_layout["outline_width"],
        bubble_type=plan.bubble_type,
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
    return PreparedBubble(
        plan=plan,
        text_overlay=text_overlay,
        bubble_layout=bubble_layout,
        local_text_bbox=local_text_bbox,
        bubble_asset=bubble_asset,
    )


def render_bubbles(
    image_path: Path,
    output_path: Path,
    plans: list[BubblePlan],
    font_path: str | None,
    font_family: str | None,
    bubble_asset_override: Path | None,
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
        bubble_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        text_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        if browser is None and len(plans) > 1:
            worker_count = min(4, len(plans))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                prepared_bubbles = list(
                    executor.map(
                        lambda plan: _prepare_rendered_bubble(
                            plan=plan,
                            width_px=width_px,
                            height_px=height_px,
                            actual_font_size=actual_font_size,
                            browser=None,
                            text_renderer=text_renderer,
                            font_path=font_path,
                            font_family=font_family,
                            bubble_asset_override=bubble_asset_override,
                            resvg_executable=resvg_executable,
                            text_letter_spacing=text_letter_spacing,
                            text_word_spacing=text_word_spacing,
                            resvg_tu_override=resvg_tu_override,
                        ),
                        plans,
                    )
                )
        else:
            prepared_bubbles = [
                _prepare_rendered_bubble(
                    plan=plan,
                    width_px=width_px,
                    height_px=height_px,
                    actual_font_size=actual_font_size,
                    browser=browser,
                    text_renderer=text_renderer,
                    font_path=font_path,
                    font_family=font_family,
                    bubble_asset_override=bubble_asset_override,
                    resvg_executable=resvg_executable,
                    text_letter_spacing=text_letter_spacing,
                    text_word_spacing=text_word_spacing,
                    resvg_tu_override=resvg_tu_override,
                )
                for plan in plans
            ]

        rendered_bubbles: list[RenderedBubble] = []
        for prepared in prepared_bubbles:
            bubble_asset = prepared.bubble_asset
            bubble_layout, bubble_image = _fit_prepared_bubble_to_alpha(
                prepared=prepared,
                bubble_renderer=bubble_renderer,
                browser=browser,
                resvg_executable=resvg_executable,
                cache=bubble_cache,
            )
            rendered_bubbles.append(
                RenderedBubble(
                    plan=prepared.plan,
                    text_overlay=prepared.text_overlay,
                    bubble_layout=bubble_layout,
                    bubble_image=bubble_image,
                    bubble_asset=bubble_asset,
                    local_text_bbox=prepared.local_text_bbox,
                )
            )

        for group in _group_bubbles_for_merge(rendered_bubbles):
            group_asset = group[0].bubble_asset
            use_vector_group_render = group_asset.source_kind in {"svg", "procedural"}
            if len(group) == 1:
                item = group[0]
                alpha_composite_clipped(
                    bubble_layer,
                    item.bubble_image,
                    item.bubble_layout["bubble_left"],
                    item.bubble_layout["bubble_top"],
                )
                continue
            merged_image, left, top = _render_merged_group_image(
                group=group,
                bubble_renderer=bubble_renderer,
                browser=browser,
                resvg_executable=resvg_executable,
            )
            alpha_composite_clipped(bubble_layer, merged_image, left, top)

        for item in rendered_bubbles:
            alpha_composite_clipped(
                text_layer,
                item.text_overlay.image,
                item.text_overlay.offset_left,
                item.text_overlay.offset_top,
            )
        base.alpha_composite(bubble_layer)
        base.alpha_composite(text_layer)

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
        base.save(output_path, compress_level=1, optimize=False)


def render_text_stage_preview(
    image_path: Path,
    output_path: Path,
    plans: list[BubblePlan],
    font_path: str | None,
    font_family: str | None,
    font_size: int,
    text_renderer: str,
    text_letter_spacing: str,
    text_word_spacing: str,
    resvg_tu_override: bool,
) -> list[tuple[int, int, int, int]]:
    if not plans:
        raise RuntimeError("no bubble plans to render")
    if text_renderer not in {"browser", "resvg-hybrid"}:
        raise RuntimeError(f"unsupported text renderer: {text_renderer}")

    base = Image.open(image_path).convert("RGBA")
    width_px, height_px = base.size
    actual_font_size = font_size or max(22, min(48, height_px // DEFAULT_FONT_DIVISOR))
    resvg_executable = resolve_resvg_executable() if text_renderer == "resvg-hybrid" else None
    if text_renderer == "resvg-hybrid" and not resvg_executable:
        raise RuntimeError("resvg not found; install resvg or use text_renderer=browser")

    text_bboxes: list[tuple[int, int, int, int]] = []

    def _render_with_browser(browser: Any | None) -> None:
        draw = ImageDraw.Draw(base, "RGBA")
        for plan in plans:
            text_layout = compute_text_layout(
                width_px,
                height_px,
                plan,
                actual_font_size,
                font_path=font_path,
                letter_spacing_px=_parse_letter_spacing_px(text_letter_spacing),
                resvg_tu_override=resvg_tu_override,
            )
            stage_left, stage_top, stage_right, stage_bottom, local_text_layout = _local_text_stage(
                canvas_width=width_px,
                canvas_height=height_px,
                text_layout=text_layout,
                font_size=actual_font_size,
            )
            text_overlay = render_text_overlay(
                renderer=text_renderer,
                browser=browser,
                canvas_width=stage_right - stage_left,
                canvas_height=stage_bottom - stage_top,
                plan=plan,
                text_layout=local_text_layout,
                font_path=font_path,
                font_family=font_family,
                resvg_executable=resvg_executable,
                text_letter_spacing=text_letter_spacing,
                text_word_spacing=text_word_spacing,
                resvg_tu_override=resvg_tu_override,
            )
            global_alpha_bbox = _translate_bbox(text_overlay.alpha_bbox, stage_left, stage_top)
            left, top, right, bottom = global_alpha_bbox
            text_bboxes.append(global_alpha_bbox)
            guide_box = (
                max(0, left - TEXT_STAGE_GUIDE_PAD_X),
                max(0, top - TEXT_STAGE_GUIDE_PAD_Y),
                min(width_px - 1, right + TEXT_STAGE_GUIDE_PAD_X),
                min(height_px - 1, bottom + TEXT_STAGE_GUIDE_PAD_Y),
            )
            draw.rounded_rectangle(
                guide_box,
                radius=TEXT_STAGE_GUIDE_RADIUS,
                fill=TEXT_STAGE_GUIDE_FILL,
                outline=TEXT_STAGE_GUIDE_OUTLINE,
                width=TEXT_STAGE_GUIDE_OUTLINE_WIDTH,
            )
            alpha_composite_clipped(base, text_overlay.image, stage_left, stage_top)

    if text_renderer == "browser":
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
    return text_bboxes
