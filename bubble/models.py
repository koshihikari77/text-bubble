from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"

FONT_CANDIDATES = [
    str(PROJECT_ROOT / "assets" / "JKG-L_3.ttf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

BUBBLE_INNER_WIDTH_RATIO = 0.62
BUBBLE_INNER_HEIGHT_RATIO = 0.72
TEXT_COLUMN_GAP_RATIO = 0.1
DEFAULT_FONT_DIVISOR = 38
BUBBLE_FILL_OPACITY = 0.88
BUBBLE_FILL_ALPHA_PNG = 232
BUBBLE_STROKE_COLOR = "#111111"
TEXT_COLOR = "#111111"
TEXT_SHADOW = "none"
SVG_NS = "http://www.w3.org/2000/svg"
DEFAULT_BUBBLE_TYPE = "ellipse"


@dataclass
class BubblePlan:
    anchor_x: float
    anchor_y: float
    sentence_ids: list[int]
    columns: list[str]
    speaker_id: str = ""
    bubble_type: str = DEFAULT_BUBBLE_TYPE


@dataclass
class AssignmentBubblePlan:
    bubble_id: str
    sentence_ids: list[int]


@dataclass
class ReflowBubblePlan:
    bubble_id: str
    sentence_ids: list[int]
    columns: list[str]
    bubble_type: str | None = None


@dataclass
class SceneBubblePlan:
    bubble_id: str
    anchor_x: float
    anchor_y: float
    sentence_ids: list[int]
    speaker_id: str = ""
    bubble_type: str = DEFAULT_BUBBLE_TYPE


@dataclass
class TextRenderResult:
    image: Image.Image
    alpha_bbox: tuple[int, int, int, int]
    offset_left: int = 0
    offset_top: int = 0


def bubble_plan_to_dict(plan: BubblePlan) -> dict[str, Any]:
    return {
        "anchor_x": plan.anchor_x,
        "anchor_y": plan.anchor_y,
        "sentence_ids": plan.sentence_ids,
        "columns": plan.columns,
        "speaker_id": plan.speaker_id,
        "bubble_type": plan.bubble_type,
    }


def plans_payload(dialogue_lines: list[str], plans: list[BubblePlan]) -> dict[str, Any]:
    return {
        "dialogue_lines": dialogue_lines,
        "bubbles": [bubble_plan_to_dict(plan) for plan in plans],
    }


def assignment_bubble_plan_to_dict(plan: AssignmentBubblePlan) -> dict[str, Any]:
    return {
        "bubble_id": plan.bubble_id,
        "sentence_ids": plan.sentence_ids,
    }


def assignment_plans_payload(dialogue_lines: list[str], plans: list[AssignmentBubblePlan]) -> dict[str, Any]:
    return {
        "dialogue_lines": dialogue_lines,
        "bubbles": [assignment_bubble_plan_to_dict(plan) for plan in plans],
    }


def reflow_bubble_plan_to_dict(plan: ReflowBubblePlan) -> dict[str, Any]:
    payload = {
        "bubble_id": plan.bubble_id,
        "sentence_ids": plan.sentence_ids,
        "columns": plan.columns,
    }
    if plan.bubble_type:
        payload["bubble_type"] = plan.bubble_type
    return payload


def reflow_plans_payload(dialogue_lines: list[str], plans: list[ReflowBubblePlan]) -> dict[str, Any]:
    return {
        "dialogue_lines": dialogue_lines,
        "bubbles": [reflow_bubble_plan_to_dict(plan) for plan in plans],
    }


def scene_bubble_plan_to_dict(plan: SceneBubblePlan) -> dict[str, Any]:
    return {
        "bubble_id": plan.bubble_id,
        "anchor_x": plan.anchor_x,
        "anchor_y": plan.anchor_y,
        "sentence_ids": plan.sentence_ids,
        "speaker_id": plan.speaker_id,
        "bubble_type": plan.bubble_type,
    }


def scene_plans_payload(dialogue_lines: list[str], plans: list[SceneBubblePlan]) -> dict[str, Any]:
    return {
        "dialogue_lines": dialogue_lines,
        "bubbles": [scene_bubble_plan_to_dict(plan) for plan in plans],
    }


def save_plan_json(plan_path: Path, dialogue_lines: list[str], plans: list[BubblePlan]) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(plans_payload(dialogue_lines, plans), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_assignment_plan_json(plan_path: Path, dialogue_lines: list[str], plans: list[AssignmentBubblePlan]) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(assignment_plans_payload(dialogue_lines, plans), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_reflow_plan_json(plan_path: Path, dialogue_lines: list[str], plans: list[ReflowBubblePlan]) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(reflow_plans_payload(dialogue_lines, plans), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_scene_plan_json(plan_path: Path, dialogue_lines: list[str], plans: list[SceneBubblePlan]) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(scene_plans_payload(dialogue_lines, plans), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
