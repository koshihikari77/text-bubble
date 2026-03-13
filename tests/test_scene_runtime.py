from __future__ import annotations

import unittest

from bubble.models import ReflowBubblePlan, SceneBubblePlan
from bubble.scene_runtime import compose_scene_bundle, resolve_scene_route


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


if __name__ == "__main__":
    unittest.main()
