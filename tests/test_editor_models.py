from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from bubble.editor_models import (
    add_workspace_case,
    document_to_stage_files,
    render_case_document,
    validate_document,
)
from bubble.models import (
    AssignmentBubblePlan,
    ReflowBubblePlan,
    SceneBubblePlan,
    save_assignment_plan_json,
    save_reflow_plan_json,
    save_scene_plan_json,
)
from bubble.validation import load_reflow_plan_json, load_scene_plan_json


def _write_workspace(workspace: Path, image_path: Path) -> None:
    dialogue_lines = ["こんにちは"]
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "metadata.json").write_text(
        json.dumps({"dialogue_lines": dialogue_lines, "input_image": str(image_path)}, ensure_ascii=False),
        encoding="utf-8",
    )
    save_assignment_plan_json(
        workspace / "assignment.json",
        dialogue_lines,
        [AssignmentBubblePlan(bubble_id="b1", sentence_ids=[1])],
    )
    save_reflow_plan_json(
        workspace / "reflow.json",
        dialogue_lines,
        [ReflowBubblePlan(bubble_id="b1", sentence_ids=[1], columns=["こんにちは"], bubble_type="wavy")],
    )
    save_scene_plan_json(
        workspace / "scene.json",
        dialogue_lines,
        [
            SceneBubblePlan(
                bubble_id="b1",
                sentence_ids=[1],
                anchor_x=0.25,
                anchor_y=0.75,
                speaker_id="speaker-a",
                bubble_type="ellipse",
            )
        ],
    )


class EditorModelTests(unittest.TestCase):
    def test_import_workspace_creates_document_and_project_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_path = root / "image.png"
            Image.new("RGB", (20, 20), "white").save(image_path)
            workspace = root / "workspace"
            project = root / "project"
            _write_workspace(workspace, image_path)

            document = add_workspace_case(project_dir=project, case_id="case1", workspace=workspace)

            self.assertEqual(document["case_id"], "case1")
            self.assertEqual(document["bubbles"][0]["bubble_type"], "wavy")
            self.assertEqual(document["bubbles"][0]["placement"], {"anchor_x": 0.25, "anchor_y": 0.75})
            self.assertEqual(document["bubbles"][0]["source"]["placement"], "cp-sat")
            self.assertTrue((project / "project.json").exists())
            self.assertTrue((project / "cases" / "case1" / "document.json").exists())

    def test_document_exports_reflow_and_scene_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            document = validate_document(
                {
                    "version": 1,
                    "case_id": "case1",
                    "image": str(root / "image.png"),
                    "dialogue_lines": ["こんにちは"],
                    "bubbles": [
                        {
                            "bubble_id": "b1",
                            "sentence_ids": [1],
                            "text": "こんにちは",
                            "columns": ["こん", "にちは"],
                            "bubble_type": "shout",
                            "speaker_id": "speaker-a",
                            "placement": {"anchor_x": 0.6, "anchor_y": 0.2},
                        }
                    ],
                }
            )

            paths = document_to_stage_files(document, root / "generated")
            _, reflow_plans = load_reflow_plan_json(paths["reflow"])
            _, scene_plans = load_scene_plan_json(paths["scene"])

            self.assertEqual(reflow_plans[0].columns, ["こん", "にちは"])
            self.assertEqual(reflow_plans[0].bubble_type, "shout")
            self.assertEqual(scene_plans[0].anchor_x, 0.6)
            self.assertEqual(scene_plans[0].speaker_id, "speaker-a")

    def test_validate_document_rejects_columns_that_do_not_match_text(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "columns do not reconstruct text"):
            validate_document(
                {
                    "version": 1,
                    "case_id": "case1",
                    "image": "image.png",
                    "dialogue_lines": ["こんにちは"],
                    "bubbles": [
                        {
                            "bubble_id": "b1",
                            "sentence_ids": [1],
                            "text": "こんにちは",
                            "columns": ["こんばんは"],
                            "bubble_type": "ellipse",
                            "placement": {"anchor_x": 0.5, "anchor_y": 0.5},
                        }
                    ],
                }
            )

    def test_validate_document_rejects_unknown_bubble_type(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unknown bubble_type"):
            validate_document(
                {
                    "version": 1,
                    "case_id": "case1",
                    "image": "image.png",
                    "dialogue_lines": ["こんにちは"],
                    "bubbles": [
                        {
                            "bubble_id": "b1",
                            "sentence_ids": [1],
                            "text": "こんにちは",
                            "columns": ["こんにちは"],
                            "bubble_type": "nonexistent_type",
                            "placement": {"anchor_x": 0.5, "anchor_y": 0.5},
                        }
                    ],
                }
            )

    def test_render_case_document_uses_document_placement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_path = root / "image.png"
            Image.new("RGB", (20, 20), "white").save(image_path)
            workspace = root / "workspace"
            project = root / "project"
            _write_workspace(workspace, image_path)
            document = add_workspace_case(project_dir=project, case_id="case1", workspace=workspace)
            document["bubbles"][0]["placement"] = {"anchor_x": 0.8, "anchor_y": 0.1}
            (project / "cases" / "case1" / "document.json").write_text(
                json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch("bubble.editor_models.render_scene_bundle") as render_scene_bundle:
                output_path = render_case_document(project, "case1")

            self.assertEqual(output_path, project / "cases" / "case1" / "renders" / "latest.png")
            _, scene_plans = load_scene_plan_json(project / "cases" / "case1" / "generated" / "scene.json")
            self.assertEqual(scene_plans[0].anchor_x, 0.8)
            render_scene_bundle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
