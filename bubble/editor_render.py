"""Render a single bubble as a transparent PNG for the HITL editor."""

from __future__ import annotations

import hashlib
import io
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from PIL import Image

from bubble.assets import pick_font_path, resolve_resvg_executable
from bubble.layout import compute_text_layout
from bubble.models import DEFAULT_FONT_DIVISOR, BubblePlan
from bubble.render import (
    _fit_prepared_bubble_to_alpha,
    _parse_letter_spacing_px,
    _prepare_rendered_bubble,
)
from bubble.scene_runtime import RenderConfig


@dataclass(frozen=True)
class BubbleSprite:
    """Transparent bubble sprite ready to drop onto a Konva stage."""

    png_bytes: bytes
    width_px: int
    height_px: int
    anchor_offset_x: int
    anchor_offset_y: int
    version_hash: str


def _bubble_to_plan(bubble: dict[str, Any]) -> BubblePlan:
    return BubblePlan(
        anchor_x=0.5,
        anchor_y=0.5,
        sentence_ids=list(bubble["sentence_ids"]),
        columns=list(bubble["columns"]),
        speaker_id=str(bubble.get("speaker_id") or ""),
        bubble_type=str(bubble["bubble_type"]),
    )


def _effective_font_size(font_size: int, canvas_height: int) -> int:
    return font_size if font_size > 0 else max(22, min(48, canvas_height // DEFAULT_FONT_DIVISOR))


def _hash_payload(*parts: Any) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:16]


class _LRUCache:
    def __init__(self, max_entries: int = 128) -> None:
        self._lock = Lock()
        self._max = max_entries
        self._data: OrderedDict[str, BubbleSprite] = OrderedDict()

    def get(self, key: str) -> BubbleSprite | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: str, value: BubbleSprite) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


_SPRITE_CACHE = _LRUCache()


def sprite_version_hash(
    *,
    bubble: dict[str, Any],
    canvas_width: int,
    canvas_height: int,
    render_settings: dict[str, Any],
) -> str:
    return _hash_payload(
        bubble["bubble_type"],
        tuple(bubble["columns"]),
        canvas_width,
        canvas_height,
        render_settings.get("font_size", 0),
        render_settings.get("text_renderer", "resvg-hybrid"),
        render_settings.get("bubble_renderer", "resvg"),
        render_settings.get("text_letter_spacing", "-1px"),
        render_settings.get("text_word_spacing", "0"),
        render_settings.get("resvg_tu_override", True),
        render_settings.get("font"),
        render_settings.get("font_family"),
        render_settings.get("bubble_asset"),
    )


def render_single_bubble_sprite(
    *,
    bubble: dict[str, Any],
    canvas_width: int,
    canvas_height: int,
    render_config: RenderConfig,
) -> BubbleSprite:
    """Render the bubble's procedural shape + vertical text on a transparent canvas.

    The anchor is forced to the canvas center to keep the layout independent of the
    document's stored placement — the sprite is positioned by the editor frontend.
    """

    if canvas_width <= 0 or canvas_height <= 0:
        raise RuntimeError("canvas size must be positive")
    if render_config.text_renderer != "resvg-hybrid":
        raise RuntimeError("editor sprite requires text_renderer=resvg-hybrid")
    if render_config.bubble_renderer != "resvg":
        raise RuntimeError("editor sprite requires bubble_renderer=resvg")

    version_hash = sprite_version_hash(
        bubble=bubble,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        render_settings={
            "font_size": render_config.font_size,
            "text_renderer": render_config.text_renderer,
            "bubble_renderer": render_config.bubble_renderer,
            "text_letter_spacing": render_config.text_letter_spacing,
            "text_word_spacing": render_config.text_word_spacing,
            "resvg_tu_override": render_config.resvg_tu_override,
            "font": render_config.font_path,
            "font_family": render_config.font_family,
            "bubble_asset": str(render_config.bubble_asset) if render_config.bubble_asset else None,
        },
    )
    cached = _SPRITE_CACHE.get(version_hash)
    if cached is not None:
        return cached

    resvg_executable = resolve_resvg_executable()
    if resvg_executable is None:
        raise RuntimeError("resvg executable not found; install resvg")

    plan = _bubble_to_plan(bubble)
    actual_font_size = _effective_font_size(render_config.font_size, canvas_height)
    font_path = render_config.font_path or pick_font_path(None)

    text_layout = compute_text_layout(
        canvas_width,
        canvas_height,
        plan,
        actual_font_size,
        font_path=font_path,
        letter_spacing_px=_parse_letter_spacing_px(render_config.text_letter_spacing),
        resvg_tu_override=render_config.resvg_tu_override,
    )

    prepared = _prepare_rendered_bubble(
        plan=plan,
        width_px=canvas_width,
        height_px=canvas_height,
        actual_font_size=actual_font_size,
        browser=None,
        text_renderer=render_config.text_renderer,
        font_path=font_path,
        font_family=render_config.font_family,
        bubble_asset_override=Path(render_config.bubble_asset) if render_config.bubble_asset else None,
        resvg_executable=resvg_executable,
        text_letter_spacing=render_config.text_letter_spacing,
        text_word_spacing=render_config.text_word_spacing,
        resvg_tu_override=render_config.resvg_tu_override,
    )

    bubble_cache: dict[tuple[str, str, int, int], Image.Image] = {}
    bubble_layout, bubble_image = _fit_prepared_bubble_to_alpha(
        prepared=prepared,
        bubble_renderer=render_config.bubble_renderer,
        browser=None,
        resvg_executable=resvg_executable,
        cache=bubble_cache,
    )

    sprite = bubble_image.convert("RGBA").copy()
    text_image = prepared.text_overlay.image.convert("RGBA")
    text_left_in_canvas = prepared.text_overlay.offset_left
    text_top_in_canvas = prepared.text_overlay.offset_top
    paste_x = int(text_left_in_canvas - bubble_layout["bubble_left"])
    paste_y = int(text_top_in_canvas - bubble_layout["bubble_top"])
    sprite.alpha_composite(text_image, (paste_x, paste_y))

    anchor_offset_x = int(text_layout["anchor_x"] - bubble_layout["bubble_left"])
    anchor_offset_y = int(text_layout["anchor_y"] - bubble_layout["bubble_top"])

    buffer = io.BytesIO()
    sprite.save(buffer, format="PNG", optimize=False)

    result = BubbleSprite(
        png_bytes=buffer.getvalue(),
        width_px=int(bubble_layout["bubble_width"]),
        height_px=int(bubble_layout["bubble_height"]),
        anchor_offset_x=anchor_offset_x,
        anchor_offset_y=anchor_offset_y,
        version_hash=version_hash,
    )
    _SPRITE_CACHE.put(version_hash, result)
    return result
