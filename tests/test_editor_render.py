from __future__ import annotations

import io
import unittest

from PIL import Image

from bubble.editor_render import (
    BubbleSprite,
    render_single_bubble_sprite,
    sprite_version_hash,
)
from bubble.scene_runtime import RenderConfig


def _bubble(**overrides) -> dict:
    bubble = {
        "bubble_id": "b1",
        "sentence_ids": [1],
        "text": "こんにちは",
        "columns": ["こんにちは"],
        "bubble_type": "ellipse",
        "speaker_id": "speaker-a",
        "placement": {"anchor_x": 0.5, "anchor_y": 0.5},
    }
    bubble.update(overrides)
    return bubble


def _config() -> RenderConfig:
    return RenderConfig(
        font_path=None,
        font_family=None,
        bubble_asset=None,
        font_size=0,
        text_renderer="resvg-hybrid",
        bubble_renderer="resvg",
        text_letter_spacing="-1px",
        text_word_spacing="0",
        resvg_tu_override=True,
    )


class EditorSpriteTests(unittest.TestCase):
    def test_sprite_version_hash_changes_with_inputs(self) -> None:
        base_hash = sprite_version_hash(
            bubble=_bubble(),
            canvas_width=800,
            canvas_height=600,
            render_settings={
                "font_size": 0,
                "text_renderer": "resvg-hybrid",
                "bubble_renderer": "resvg",
                "text_letter_spacing": "-1px",
                "text_word_spacing": "0",
                "resvg_tu_override": True,
            },
        )
        type_hash = sprite_version_hash(
            bubble=_bubble(bubble_type="shout"),
            canvas_width=800,
            canvas_height=600,
            render_settings={
                "font_size": 0,
                "text_renderer": "resvg-hybrid",
                "bubble_renderer": "resvg",
                "text_letter_spacing": "-1px",
                "text_word_spacing": "0",
                "resvg_tu_override": True,
            },
        )
        self.assertNotEqual(base_hash, type_hash)

    def test_renders_decodable_png_with_positive_size(self) -> None:
        sprite = render_single_bubble_sprite(
            bubble=_bubble(),
            canvas_width=800,
            canvas_height=600,
            render_config=_config(),
        )
        self.assertIsInstance(sprite, BubbleSprite)
        self.assertGreater(sprite.width_px, 0)
        self.assertGreater(sprite.height_px, 0)
        image = Image.open(io.BytesIO(sprite.png_bytes))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.size, (sprite.width_px, sprite.height_px))
        alpha_min, alpha_max = image.getchannel("A").getextrema()
        self.assertEqual(alpha_min, 0)
        self.assertGreater(alpha_max, 0)

    def test_anchor_offset_is_inside_sprite(self) -> None:
        sprite = render_single_bubble_sprite(
            bubble=_bubble(),
            canvas_width=800,
            canvas_height=600,
            render_config=_config(),
        )
        self.assertGreaterEqual(sprite.anchor_offset_x, 0)
        self.assertLessEqual(sprite.anchor_offset_x, sprite.width_px)
        self.assertGreaterEqual(sprite.anchor_offset_y, 0)
        self.assertLessEqual(sprite.anchor_offset_y, sprite.height_px)

    def test_cache_returns_same_bytes_for_repeated_call(self) -> None:
        first = render_single_bubble_sprite(
            bubble=_bubble(columns=["キャッシュ", "テスト"]),
            canvas_width=800,
            canvas_height=600,
            render_config=_config(),
        )
        second = render_single_bubble_sprite(
            bubble=_bubble(columns=["キャッシュ", "テスト"]),
            canvas_width=800,
            canvas_height=600,
            render_config=_config(),
        )
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
