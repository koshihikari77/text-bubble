from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from bubble.assets import ResolvedBubbleAsset
from bubble.models import BubblePlan, TextRenderResult
from bubble.render import PreparedBubble, render_bubbles


class RenderShoutRectTests(unittest.TestCase):
    def test_single_procedural_bubble_does_not_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "input.png"
            output_path = Path(tmpdir) / "output.png"
            Image.new("RGBA", (64, 64), (255, 255, 255, 255)).save(image_path)

            plan = BubblePlan(
                anchor_x=0.5,
                anchor_y=0.5,
                sentence_ids=[1],
                columns=["テスト"],
                speaker_id="",
                bubble_type="shout_rect_pointed_drop",
            )
            bubble_asset = ResolvedBubbleAsset(
                bubble_type="shout_rect_pointed_drop",
                source_kind="procedural",
                source_key="test:shout-rect",
            )
            prepared = PreparedBubble(
                plan=plan,
                text_overlay=TextRenderResult(
                    image=Image.new("RGBA", (64, 64), (0, 0, 0, 0)),
                    alpha_bbox=(20, 20, 40, 44),
                    offset_left=0,
                    offset_top=0,
                ),
                bubble_layout={
                    "bubble_left": 16,
                    "bubble_top": 14,
                    "bubble_right": 48,
                    "bubble_bottom": 50,
                    "bubble_width": 32,
                    "bubble_height": 36,
                    "padding_left": 8,
                    "padding_right": 8,
                    "padding_top": 6,
                    "padding_bottom": 6,
                    "outline_width": 2,
                    "shape_layout": {"kind": "shout_rect"},
                },
                local_text_bbox=(8, 6, 24, 30),
                bubble_asset=bubble_asset,
            )
            bubble_image = Image.new("RGBA", (32, 36), (255, 255, 255, 255))

            with (
                patch("bubble.render.resolve_resvg_executable", return_value="/bin/true"),
                patch("bubble.render._prepare_rendered_bubble", return_value=prepared),
                patch("bubble.render._fit_prepared_bubble_to_alpha", return_value=(prepared.bubble_layout, bubble_image)),
                patch("bubble.render._render_merged_group_image", side_effect=AssertionError("merge path should not run")),
            ):
                render_bubbles(
                    image_path=image_path,
                    output_path=output_path,
                    plans=[plan],
                    font_path=None,
                    font_family=None,
                    bubble_asset_override=None,
                    font_size=22,
                    text_renderer="resvg-hybrid",
                    bubble_renderer="resvg",
                    text_letter_spacing="-1px",
                    text_word_spacing="0",
                    resvg_tu_override=True,
                )

            self.assertTrue(output_path.exists())

    def test_grouped_procedural_bubbles_use_merge_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "input.png"
            output_path = Path(tmpdir) / "output.png"
            Image.new("RGBA", (96, 96), (255, 255, 255, 255)).save(image_path)

            bubble_asset = ResolvedBubbleAsset(
                bubble_type="shout_rect_pointed_drop",
                source_kind="procedural",
                source_key="test:shout-rect-group",
            )
            plans = [
                BubblePlan(0.5, 0.4, [1], ["A"], "", "shout_rect_pointed_drop"),
                BubblePlan(0.6, 0.5, [2], ["B"], "", "shout_rect_pointed_drop"),
            ]
            prepared_items = []
            for index, plan in enumerate(plans):
                prepared_items.append(
                    PreparedBubble(
                        plan=plan,
                        text_overlay=TextRenderResult(
                            image=Image.new("RGBA", (96, 96), (0, 0, 0, 0)),
                            alpha_bbox=(24 + index * 18, 20, 40 + index * 18, 44),
                            offset_left=0,
                            offset_top=0,
                        ),
                        bubble_layout={
                            "bubble_left": 18 + index * 18,
                            "bubble_top": 14 + index * 10,
                            "bubble_right": 50 + index * 18,
                            "bubble_bottom": 50 + index * 10,
                            "bubble_width": 32,
                            "bubble_height": 36,
                            "padding_left": 8,
                            "padding_right": 8,
                            "padding_top": 6,
                            "padding_bottom": 6,
                            "outline_width": 2,
                            "shape_layout": {"kind": "shout_rect"},
                        },
                        local_text_bbox=(8, 6, 24, 30),
                        bubble_asset=bubble_asset,
                    )
                )
            bubble_images = [Image.new("RGBA", (32, 36), (255, 255, 255, 255)) for _ in prepared_items]
            merged_image = Image.new("RGBA", (64, 64), (255, 255, 255, 255))

            with (
                patch("bubble.render.resolve_resvg_executable", return_value="/bin/true"),
                patch("bubble.render._prepare_rendered_bubble", side_effect=prepared_items),
                patch(
                    "bubble.render._fit_prepared_bubble_to_alpha",
                    side_effect=[(item.bubble_layout, image) for item, image in zip(prepared_items, bubble_images, strict=True)],
                ),
                patch("bubble.render._group_bubbles_for_merge", side_effect=lambda items: [items]),
                patch("bubble.render._render_merged_group_image", return_value=(merged_image, 0, 0)) as merged_group,
            ):
                render_bubbles(
                    image_path=image_path,
                    output_path=output_path,
                    plans=plans,
                    font_path=None,
                    font_family=None,
                    bubble_asset_override=None,
                    font_size=22,
                    text_renderer="resvg-hybrid",
                    bubble_renderer="resvg",
                    text_letter_spacing="-1px",
                    text_word_spacing="0",
                    resvg_tu_override=True,
                )

            self.assertTrue(output_path.exists())
            merged_group.assert_called_once()


if __name__ == "__main__":
    unittest.main()
