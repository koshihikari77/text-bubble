from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bubble.models import ReflowBubblePlan
from beam_search_scene_solver import build_body_regions, rect_mask_overlap_area, rects_intersect
from cp_sat_scene_solver import READING_MODEL, solve_scene_layout


def _pair_distances(placements: list) -> list[float]:
    distances: list[float] = []
    for left_index, left in enumerate(placements):
        for right in placements[left_index + 1 :]:
            distances.append(
                math.hypot(
                    left.text_box.center_x - right.text_box.center_x,
                    left.text_box.center_y - right.text_box.center_y,
                )
            )
    return distances


class CpSatSceneSolverTests(unittest.TestCase):
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

        self.assertEqual(solution.selected_template, READING_MODEL)
        self.assertEqual(len(solution.scene_plans), 2)
        first, second = solution.placements
        self.assertFalse(rects_intersect(first.text_box, second.text_box))
        self.assertEqual(rect_mask_overlap_area(body_regions.face_mask, first.text_box), 0)
        self.assertEqual(rect_mask_overlap_area(body_regions.face_mask, second.text_box), 0)
        self.assertGreaterEqual(first.text_box.center_x, second.text_box.center_x - 8)

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

        self.assertEqual(solution.selected_template, READING_MODEL)
        self.assertEqual(len(solution.scene_plans), 5)
        self.assertIsNotNone(solution.debug_payload["min_pair_distance_px"])

    def test_three_bubbles_have_horizontal_spread(self) -> None:
        image_width = 896
        image_height = 1152
        person_mask = np.zeros((image_height, image_width), dtype=bool)
        person_mask[180:1010, 280:620] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[210:360, 350:560] = True
        plans = [
            ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["夜見のどこ", "みてるのー？"]),
            ReflowBubblePlan(bubble_id="b2", sentence_ids=[2], columns=["そんなに見つめられると、", "ちょっと照れるんだけど。"]),
            ReflowBubblePlan(bubble_id="b3", sentence_ids=[3], columns=["ふふっ、近くで見ると", "意外と素直なんだね。"]),
        ]

        solution = solve_scene_layout(
            reflow_plans=plans,
            image_width=image_width,
            image_height=image_height,
            face_mask=face_mask,
            person_mask=person_mask,
            font_size=22,
        )

        self.assertGreaterEqual(solution.debug_payload["horizontal_span_px"], int(image_width * 0.34))

    def test_four_bubbles_have_vertical_spread(self) -> None:
        image_width = 896
        image_height = 1152
        person_mask = np.zeros((image_height, image_width), dtype=bool)
        person_mask[180:1010, 280:620] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[210:360, 350:560] = True
        plans = [
            ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["夜見のどこ", "みてるのー？"]),
            ReflowBubblePlan(bubble_id="b2", sentence_ids=[2], columns=["そんなに見つめられると、", "ちょっと照れるんだけど。"]),
            ReflowBubblePlan(bubble_id="b3", sentence_ids=[3], columns=["ふふっ、近くで見ると", "意外と素直なんだね。"]),
            ReflowBubblePlan(bubble_id="b4", sentence_ids=[4], columns=["ねえ、今の顔、", "かなり好きかも。"]),
        ]

        solution = solve_scene_layout(
            reflow_plans=plans,
            image_width=image_width,
            image_height=image_height,
            face_mask=face_mask,
            person_mask=person_mask,
            font_size=22,
        )

        self.assertGreaterEqual(solution.debug_payload["vertical_span_px"], int(image_height * 0.24))

    def test_five_bubbles_keep_pair_distance(self) -> None:
        image_width = 768
        image_height = 960
        person_mask = np.zeros((image_height, image_width), dtype=bool)
        person_mask[150:860, 220:540] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[180:320, 290:490] = True
        plans = [
            ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["今日はね", "少しだけ"]),
            ReflowBubblePlan(bubble_id="b2", sentence_ids=[2], columns=["聞いてほしい", "ことがあるの"]),
            ReflowBubblePlan(bubble_id="b3", sentence_ids=[3], columns=["そんな顔で", "見られるとさ"]),
            ReflowBubblePlan(bubble_id="b4", sentence_ids=[4], columns=["なんだか", "照れちゃうね"]),
            ReflowBubblePlan(bubble_id="b5", sentence_ids=[5], columns=["でも続きは", "ちゃんと言うよ"]),
        ]

        solution = solve_scene_layout(
            reflow_plans=plans,
            image_width=image_width,
            image_height=image_height,
            face_mask=face_mask,
            person_mask=person_mask,
            font_size=22,
        )

        distances = _pair_distances(solution.placements)
        self.assertTrue(distances)
        self.assertGreaterEqual(min(distances), 40.0)
        self.assertGreaterEqual(solution.debug_payload["horizontal_span_px"], int(image_width * 0.36))

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
