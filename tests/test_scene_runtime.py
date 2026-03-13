from __future__ import annotations

import unittest

from bubble.models import ReflowBubblePlan, SceneBubblePlan
from bubble.scene_runtime import (
    bundle_from_evaluated_solution,
    compose_scene_bundle,
    deserialize_evaluated_solution,
    resolve_scene_route,
    serialize_evaluated_solution,
)


class SceneRuntimeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
