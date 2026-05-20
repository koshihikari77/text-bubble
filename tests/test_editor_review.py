from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from bubble.editor_models import add_workspace_case
from bubble.editor_review import empty_review, load_review, review_path, save_review
from bubble.models import (
    AssignmentBubblePlan,
    ReflowBubblePlan,
    SceneBubblePlan,
    save_assignment_plan_json,
    save_reflow_plan_json,
    save_scene_plan_json,
)


def _write_workspace(workspace: Path, image_path: Path, *, bubble_id: str = "b1") -> None:
    dialogue_lines = ["こんにちは"]
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "metadata.json").write_text(
        json.dumps({"dialogue_lines": dialogue_lines, "input_image": str(image_path)}, ensure_ascii=False),
        encoding="utf-8",
    )
    save_assignment_plan_json(
        workspace / "assignment.json",
        dialogue_lines,
        [AssignmentBubblePlan(bubble_id=bubble_id, sentence_ids=[1])],
    )
    save_reflow_plan_json(
        workspace / "reflow.json",
        dialogue_lines,
        [ReflowBubblePlan(bubble_id=bubble_id, sentence_ids=[1], columns=["こんにちは"], bubble_type="ellipse")],
    )
    save_scene_plan_json(
        workspace / "scene.json",
        dialogue_lines,
        [
            SceneBubblePlan(
                bubble_id=bubble_id,
                sentence_ids=[1],
                anchor_x=0.5,
                anchor_y=0.5,
                speaker_id="speaker",
                bubble_type="ellipse",
            )
        ],
    )


def _build_project(tmp: Path) -> Path:
    image_path = tmp / "image.png"
    Image.new("RGB", (20, 20), "white").save(image_path)
    project = tmp / "project"
    workspace_a = tmp / "ws_a"
    workspace_b = tmp / "ws_b"
    _write_workspace(workspace_a, image_path, bubble_id="b1")
    _write_workspace(workspace_b, image_path, bubble_id="b1")
    add_workspace_case(project_dir=project, case_id="caseA", workspace=workspace_a)
    add_workspace_case(project_dir=project, case_id="caseB", workspace=workspace_b)
    return project


class EditorReviewTests(unittest.TestCase):
    def test_empty_review_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = _build_project(Path(tmp_dir))
            review = load_review(project)
            self.assertEqual(review["overall"], "")
            self.assertEqual(review["cases"], [])

    def test_save_round_trip_keeps_known_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = _build_project(Path(tmp_dir))
            saved = save_review(
                project,
                {
                    "overall": "全体所感",
                    "cases": [
                        {"case_id": "caseA", "comment": "caseA メモ", "bubbles": [{"bubble_id": "b1", "comment": "b1 メモ"}]},
                        {"case_id": "caseB", "comment": "", "bubbles": []},
                    ],
                },
            )
            self.assertEqual(saved["overall"], "全体所感")
            self.assertEqual(saved["cases"][0]["case_id"], "caseA")
            self.assertEqual(saved["cases"][0]["bubbles"][0]["comment"], "b1 メモ")
            self.assertNotIn({"case_id": "caseB", "comment": "", "bubbles": []}, saved["cases"])
            reloaded = load_review(project)
            self.assertEqual(reloaded["overall"], "全体所感")
            self.assertEqual(reloaded["cases"][0]["bubbles"][0]["comment"], "b1 メモ")

    def test_unknown_case_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = _build_project(Path(tmp_dir))
            with self.assertRaisesRegex(ValueError, "unknown case_id"):
                save_review(project, {"overall": "", "cases": [{"case_id": "missing", "comment": "x", "bubbles": []}]})

    def test_unknown_bubble_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = _build_project(Path(tmp_dir))
            with self.assertRaisesRegex(ValueError, "unknown bubble_id"):
                save_review(
                    project,
                    {
                        "overall": "",
                        "cases": [
                            {"case_id": "caseA", "comment": "", "bubbles": [{"bubble_id": "ghost", "comment": "x"}]}
                        ],
                    },
                )

    def test_disappeared_case_moves_to_orphans_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project = _build_project(Path(tmp_dir))
            # write a raw review.json that includes a stale case
            stale = {
                "version": 1,
                "input_dir": str(project),
                "updated_at": "2026-05-21T00:00:00Z",
                "overall": "",
                "cases": [
                    {"case_id": "caseA", "comment": "live", "bubbles": []},
                    {"case_id": "ghost", "comment": "orphaned-case", "bubbles": [{"bubble_id": "b1", "comment": "orphaned-bubble"}]},
                ],
                "orphans": [],
            }
            review_path(project).parent.mkdir(parents=True, exist_ok=True)
            review_path(project).write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

            review = load_review(project)
            self.assertEqual(len(review["cases"]), 1)
            self.assertEqual(review["cases"][0]["case_id"], "caseA")
            kinds = {entry["kind"] for entry in review["orphans"]}
            self.assertIn("case", kinds)
            self.assertIn("bubble", kinds)


if __name__ == "__main__":
    unittest.main()
