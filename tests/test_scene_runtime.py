from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from bubble.models import ReflowBubblePlan, SceneBubblePlan
from bubble.scene_runtime import (
    bundle_from_evaluated_solution,
    compose_scene_bundle,
    default_scene_planner,
    deserialize_evaluated_solution,
    load_mask_bundle,
    render_scene_bundle,
    resolve_scene_route,
    serialize_evaluated_solution,
    RenderConfig,
    ScenePlacementBundle,
)


class SceneRuntimeTests(unittest.TestCase):
    def test_default_scene_planner_uses_env_override(self) -> None:
        previous = os.environ.get("TEXT_BUBBLE_SCENE_PLANNER")
        os.environ["TEXT_BUBBLE_SCENE_PLANNER"] = "llm"
        try:
            self.assertEqual(default_scene_planner(), "llm")
        finally:
            if previous is None:
                os.environ.pop("TEXT_BUBBLE_SCENE_PLANNER", None)
            else:
                os.environ["TEXT_BUBBLE_SCENE_PLANNER"] = previous

    def test_resolve_scene_route_defaults_to_main_route(self) -> None:
        route = resolve_scene_route(
            default_server="http://127.0.0.1:8080/v1",
            default_model="main-model",
        )

        self.assertEqual(route.server, "http://127.0.0.1:8080/v1")
        self.assertEqual(route.model, "main-model")

    def test_resolve_scene_route_prefers_scene_override(self) -> None:
        route = resolve_scene_route(
            default_server="http://127.0.0.1:8080/v1",
            default_model="main-model",
            scene_server="http://127.0.0.1:9090/v1",
            scene_model="scene-model",
        )

        self.assertEqual(route.server, "http://127.0.0.1:9090/v1")
        self.assertEqual(route.model, "scene-model")

    def test_compose_scene_bundle_keeps_source_metadata(self) -> None:
        reflow_plans = [
            ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["こんにちは"]),
        ]
        scene_plans = [
            SceneBubblePlan(bubble_id="b1", anchor_x=0.84, anchor_y=0.18, sentence_ids=[1]),
        ]

        bundle = compose_scene_bundle(
            dialogue_lines=["こんにちは"],
            reflow_plans=reflow_plans,
            scene_plans=scene_plans,
            source="scene-json",
        )

        self.assertEqual(bundle.debug_payload["placement_source"], "scene-json")
        self.assertEqual(len(bundle.composed_plans), 1)
        self.assertEqual(bundle.composed_plans[0].columns, ["こんにちは"])
        self.assertEqual(bundle.composed_plans[0].sentence_ids, [1])

    def test_evaluated_solution_round_trip_and_bundle_reuse(self) -> None:
        from bubble.scene_runtime import _import_cp_sat_scene_solver

        solver_module = _import_cp_sat_scene_solver()
        evaluated_solution = solver_module.PlacementSolution(
            selected_template="rtl-columns",
            scene_plans=[
                SceneBubblePlan(bubble_id="b1", anchor_x=0.84, anchor_y=0.18, sentence_ids=[1]),
            ],
            placements=[
                solver_module.PlacementChoice(
                    bubble_id="b1",
                    sentence_ids=[1],
                    anchor_x_px=84,
                    anchor_y_px=18,
                    text_box=solver_module.Rect(left=10, top=20, right=30, bottom=60),
                    bubble_box=solver_module.Rect(left=0, top=10, right=40, bottom=70),
                    total_score=12.5,
                    penalties={"face_too_far": 12.5},
                    source="cp-sat",
                    template="rtl-columns",
                    slot="top-right",
                )
            ],
            debug_payload={"solver": "cp-sat", "objective_value": 12.5},
        )
        serialized = serialize_evaluated_solution(evaluated_solution)
        restored = deserialize_evaluated_solution(serialized)
        bundle = bundle_from_evaluated_solution(
            dialogue_lines=["こんにちは"],
            reflow_plans=[ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["こんにちは"])],
            evaluated_solution=restored,
            source="cp-sat",
        )

        self.assertEqual(restored.scene_plans[0].bubble_id, "b1")
        self.assertEqual(restored.placements[0].text_box.left, 10)
        self.assertEqual(bundle.debug_payload["placement_source"], "cp-sat")
        self.assertEqual(bundle.evaluated_solution.placements[0].slot, "top-right")

    def test_load_mask_bundle_uses_head_fallback_and_drops_empty_optional_masks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            person = np.zeros((6, 6), dtype=np.uint8)
            person[1:5, 1:5] = 255
            face = np.zeros((6, 6), dtype=np.uint8)
            head = np.zeros((6, 6), dtype=np.uint8)
            head[1:3, 2:4] = 255
            chest = np.zeros((6, 6), dtype=np.uint8)
            lower = np.zeros((6, 6), dtype=np.uint8)
            for name, mask in {
                "person": person,
                "face": face,
                "head": head,
                "chest": chest,
                "lower": lower,
            }.items():
                Image.fromarray(mask, mode="L").save(tmp_path / f"{name}.png")

            bundle = load_mask_bundle(
                person_mask_path=tmp_path / "person.png",
                face_mask_path=tmp_path / "face.png",
                chest_mask_path=tmp_path / "chest.png",
                lower_mask_path=tmp_path / "lower.png",
                head_mask_path=tmp_path / "head.png",
            )

            self.assertEqual(bundle.face_source, "head-fallback")
            self.assertIsNone(bundle.chest_mask)
            self.assertIsNone(bundle.lower_mask)
            self.assertTrue(np.any(bundle.face_mask))

    def test_render_scene_bundle_uses_bubble_asset_override_keyword(self) -> None:
        bundle = ScenePlacementBundle(scene_plans=[], composed_plans=[], evaluated_solution=None, debug_payload={})
        config = RenderConfig(
            font_path=None,
            font_family=None,
            bubble_asset=Path("/tmp/custom.svg"),
            font_size=22,
            text_renderer="resvg-hybrid",
            bubble_renderer="resvg",
            text_letter_spacing="-1px",
            text_word_spacing="0",
            resvg_tu_override=True,
        )

        with patch("bubble.render.render_bubbles") as render_bubbles:
            render_scene_bundle(
                image_path=Path("/tmp/input.png"),
                output_path=Path("/tmp/output.png"),
                bundle=bundle,
                config=config,
            )

        _, kwargs = render_bubbles.call_args
        self.assertEqual(kwargs["bubble_asset_override"], Path("/tmp/custom.svg"))


if __name__ == "__main__":
    unittest.main()
