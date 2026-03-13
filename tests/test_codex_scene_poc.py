from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bubble.models import ReflowBubblePlan, SceneBubblePlan
from codex_scene_poc import (
    build_codex_cli_command,
    build_codex_cli_output_schema,
    build_editable_scene_template,
    load_scene_edit_json,
    render_mask_composite_image,
)
from cp_sat_scene_solver import evaluate_scene_layout
from beam_search_scene_solver import build_body_regions


class CodexScenePocTests(unittest.TestCase):
    def test_build_editable_scene_template_prefills_existing_scene(self) -> None:
        reflow_plans = [
            ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["夜見の", "どこみて"]),
        ]
        scene_plans = [
            SceneBubblePlan(bubble_id="b1", anchor_x=0.82, anchor_y=0.16, sentence_ids=[1]),
        ]

        payload = build_editable_scene_template(
            planner_mode="cp-sat-codex",
            reflow_plans=reflow_plans,
            scene_plans=scene_plans,
            note="test note",
        )

        self.assertEqual(payload["mode"], "cp-sat-codex")
        self.assertEqual(payload["notes"], "test note")
        self.assertEqual(payload["placements"][0]["bubble_id"], "b1")
        self.assertEqual(payload["placements"][0]["anchor_x"], 0.82)
        self.assertEqual(payload["placements"][0]["columns"], ["夜見の", "どこみて"])

    def test_load_scene_edit_json_requires_all_bubbles(self) -> None:
        reflow_plans = [
            ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["a"]),
            ReflowBubblePlan(bubble_id="b2", sentence_ids=[2], columns=["b"]),
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "edit.json"
            path.write_text(
                json.dumps(
                    {
                        "placements": [
                            {"bubble_id": "b1", "anchor_x": 0.8, "anchor_y": 0.2},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "missing placements"):
                load_scene_edit_json(path, reflow_plans=reflow_plans)

    def test_render_mask_composite_image_preserves_dimensions(self) -> None:
        person_mask = np.zeros((120, 100), dtype=bool)
        person_mask[20:105, 30:70] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[25:45, 38:62] = True
        head_mask = np.zeros_like(person_mask)
        head_mask[18:38, 34:66] = True
        body_regions = build_body_regions(person_mask, face_mask)

        image = render_mask_composite_image(
            image_width=100,
            image_height=120,
            person_mask=person_mask,
            face_mask=face_mask,
            body_regions=body_regions,
            head_mask=head_mask,
        )

        self.assertEqual(image.size, (100, 120))
        rgba = np.asarray(image.convert("RGBA"))
        self.assertGreater(int(np.count_nonzero(np.any(rgba[:, :, :3] != 255, axis=2))), 0)

    def test_evaluate_scene_layout_marks_overlap_as_infeasible(self) -> None:
        image_width = 640
        image_height = 800
        person_mask = np.zeros((image_height, image_width), dtype=bool)
        person_mask[120:760, 220:430] = True
        face_mask = np.zeros_like(person_mask)
        face_mask[150:230, 260:380] = True
        reflow_plans = [
            ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["こんにちは", "なのです"]),
            ReflowBubblePlan(bubble_id="b2", sentence_ids=[2], columns=["そうだね", "わかった"]),
        ]
        scene_plans = [
            SceneBubblePlan(bubble_id="b1", anchor_x=0.82, anchor_y=0.16, sentence_ids=[1]),
            SceneBubblePlan(bubble_id="b2", anchor_x=0.82, anchor_y=0.16, sentence_ids=[2]),
        ]

        solution = evaluate_scene_layout(
            reflow_plans=reflow_plans,
            scene_plans=scene_plans,
            image_width=image_width,
            image_height=image_height,
            face_mask=face_mask,
            person_mask=person_mask,
            font_size=28,
            source="codex-edit",
        )

        self.assertFalse(solution.debug_payload["feasible"])
        hard_conflict_types = {item["type"] for item in solution.debug_payload["hard_conflicts"]}
        self.assertIn("text_overlap", hard_conflict_types)

    def test_build_codex_cli_command_includes_schema_and_images(self) -> None:
        command = build_codex_cli_command(
            command="codex",
            model="gpt-5.4-low",
            cd=Path("/repo"),
            schema_path=Path("/tmp/schema.json"),
            output_path=Path("/tmp/output.json"),
            image_paths=[Path("/tmp/board.png"), Path("/tmp/mask.png")],
        )

        self.assertEqual(command[:6], ["codex", "exec", "-m", "gpt-5.4-low", "-C", "/repo"])
        self.assertIn("--output-schema", command)
        self.assertIn("/tmp/schema.json", command)
        self.assertIn("/tmp/output.json", command)
        self.assertEqual(command.count("-i"), 2)

    def test_build_codex_cli_command_can_use_default_model(self) -> None:
        command = build_codex_cli_command(
            command="codex",
            model=None,
            cd=Path("/repo"),
            schema_path=Path("/tmp/schema.json"),
            output_path=Path("/tmp/output.json"),
            image_paths=[Path("/tmp/board.png")],
        )

        self.assertEqual(command[:4], ["codex", "exec", "-C", "/repo"])
        self.assertNotIn("-m", command)

    def test_codex_cli_output_schema_requires_anchor_fields(self) -> None:
        schema = build_codex_cli_output_schema(["b1", "b2"])

        self.assertEqual(schema["type"], "object")
        self.assertIn("placements", schema["required"])
        self.assertIn("notes", schema["required"])
        placements_schema = schema["properties"]["placements"]
        self.assertEqual(placements_schema["minItems"], 2)
        self.assertEqual(placements_schema["maxItems"], 2)
        item_schema = placements_schema["items"]
        self.assertEqual(item_schema["required"], ["bubble_id", "anchor_x", "anchor_y"])
        self.assertEqual(item_schema["properties"]["bubble_id"]["enum"], ["b1", "b2"])


if __name__ == "__main__":
    unittest.main()
