from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

import numpy as np
from PIL import Image

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bubble.models import ReflowBubblePlan
from beam_search_scene_solver import (
    bbox_from_mask,
    build_body_regions,
    estimate_bubble_dimensions,
    load_binary_mask,
    rect_mask_overlap_area,
    rects_intersect,
    solve_scene_layout,
)


class PlacementOptTests(unittest.TestCase):
    def test_load_binary_mask_treats_non_black_pixels_as_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "mask.png"
            image = np.zeros((6, 6, 3), dtype=np.uint8)
            image[2:4, 1:5] = np.array([255, 0, 0], dtype=np.uint8)
            Image.fromarray(image, mode="RGB").save(path)
            mask = load_binary_mask(path)

        self.assertEqual(mask.dtype, np.bool_)
        self.assertEqual(mask.shape, (6, 6))
        self.assertEqual(int(mask.sum()), 8)

    def test_body_regions_are_inside_person_mask(self) -> None:
        person_mask = np.zeros((200, 160), dtype=bool)
        person_mask[20:190, 35:125] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[25:55, 55:105] = True

        body_regions = build_body_regions(person_mask, face_mask)

        self.assertTrue(np.all(body_regions.chest_mask <= person_mask))
        self.assertTrue(np.all(body_regions.lower_mask <= person_mask))
        self.assertGreater(body_regions.chest_bbox.top, body_regions.face_bbox.bottom - 4)
        self.assertGreater(body_regions.lower_bbox.top, body_regions.chest_bbox.bottom)

    def test_external_chest_and_lower_masks_override_heuristics(self) -> None:
        person_mask = np.zeros((120, 120), dtype=bool)
        person_mask[10:110, 20:100] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[14:30, 44:76] = True
        chest_mask = np.zeros_like(person_mask)
        chest_mask[40:50, 25:40] = True
        lower_mask = np.zeros_like(person_mask)
        lower_mask[85:95, 70:90] = True

        body_regions = build_body_regions(person_mask, face_mask, chest_mask=chest_mask, lower_mask=lower_mask)

        self.assertEqual(body_regions.chest_source, "external")
        self.assertEqual(body_regions.lower_source, "external")
        self.assertEqual(body_regions.chest_bbox, bbox_from_mask(chest_mask))
        self.assertEqual(body_regions.lower_bbox, bbox_from_mask(lower_mask))

    def test_estimate_bubble_dimensions_grows_with_text(self) -> None:
        short_plan = ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["短文"])
        long_plan = ReflowBubblePlan(bubble_id="b2", sentence_ids=[2], columns=["これは", "長めの", "セリフです"])

        short_dims = estimate_bubble_dimensions(short_plan, image_width=896, image_height=1152, font_size=30)
        long_dims = estimate_bubble_dimensions(long_plan, image_width=896, image_height=1152, font_size=30)

        self.assertGreater(long_dims.bubble_width, short_dims.bubble_width)
        self.assertGreater(long_dims.bubble_height, short_dims.bubble_height)

    def test_solver_returns_non_overlapping_layout(self) -> None:
        image_width = 640
        image_height = 800
        person_mask = np.zeros((image_height, image_width), dtype=bool)
        person_mask[120:760, 220:430] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[150:230, 260:380] = True
        plans = [
            ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["こんにちは", "なのです"]),
            ReflowBubblePlan(bubble_id="b2", sentence_ids=[2], columns=["そうだね", "わかった"]),
        ]

        solution = solve_scene_layout(
            reflow_plans=plans,
            image_width=image_width,
            image_height=image_height,
            face_mask=face_mask,
            person_mask=person_mask,
            font_size=28,
        )
        body_regions = build_body_regions(person_mask, face_mask)

        self.assertEqual(len(solution.scene_plans), 2)
        self.assertIn(solution.selected_template, {"n", "rotated-n"})
        first, second = solution.placements
        self.assertFalse(rects_intersect(first.text_box, second.text_box))
        self.assertEqual(rect_mask_overlap_area(body_regions.face_mask, first.bubble_box), 0)
        self.assertEqual(rect_mask_overlap_area(body_regions.face_mask, second.bubble_box), 0)
        self.assertGreaterEqual(first.anchor_x_px, 0)
        self.assertGreaterEqual(first.anchor_y_px, 0)
        self.assertLessEqual(second.anchor_x_px, image_width)
        self.assertLessEqual(second.anchor_y_px, image_height)

    def test_solver_supports_five_bubbles(self) -> None:
        image_width = 700
        image_height = 900
        person_mask = np.zeros((image_height, image_width), dtype=bool)
        person_mask[120:860, 250:470] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[150:240, 285:430] = True
        plans = [
            ReflowBubblePlan(bubble_id=f"b{index}", sentence_ids=[index], columns=[f"台詞{index}", "だよ"])
            for index in range(1, 6)
        ]

        solution = solve_scene_layout(
            reflow_plans=plans,
            image_width=image_width,
            image_height=image_height,
            face_mask=face_mask,
            person_mask=person_mask,
            font_size=26,
        )

        self.assertEqual(len(solution.scene_plans), 5)
        self.assertIn(solution.selected_template, {"n", "rotated-n"})

    def test_solver_rejects_more_than_five_bubbles(self) -> None:
        image_width = 300
        image_height = 400
        person_mask = np.zeros((image_height, image_width), dtype=bool)
        person_mask[80:380, 100:210] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[90:150, 120:190] = True
        plans = [
            ReflowBubblePlan(bubble_id=f"b{index}", sentence_ids=[index], columns=[f"t{index}"])
            for index in range(1, 7)
        ]

        with self.assertRaisesRegex(RuntimeError, "at most 5 bubbles"):
            solve_scene_layout(
                reflow_plans=plans,
                image_width=image_width,
                image_height=image_height,
                face_mask=face_mask,
                person_mask=person_mask,
                font_size=24,
            )


if __name__ == "__main__":
    unittest.main()
