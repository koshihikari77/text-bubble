from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bubble.models import (
    AssignmentBubblePlan,
    BubblePlan,
    DEFAULT_BUBBLE_TYPE,
    ReflowBubblePlan,
    SceneBubblePlan,
)


def _message_to_text(response: dict[str, Any]) -> tuple[str, str]:
    message = response["choices"][0]["message"]["content"]
    if isinstance(message, list):
        parts = [chunk.get("text", "") for chunk in message if isinstance(chunk, dict) and isinstance(chunk.get("text"), str)]
        message = "".join(parts)
    if not isinstance(message, str):
        raise RuntimeError("unexpected response content type")
    raw_message = message
    message = message.strip()
    if message.startswith("```"):
        message = message.strip("`")
        if message.startswith("json"):
            message = message[4:]
        message = message.strip()
    return message, raw_message


def summarize_raw_output(raw_message: str) -> str:
    compact = " ".join(line.strip() for line in raw_message.splitlines() if line.strip())
    if len(compact) > 200:
        compact = compact[:200].rstrip() + "..."
    return compact


def text_for_sentence_ids(dialogue_lines: list[str], sentence_ids: list[int]) -> str:
    return "".join(dialogue_lines[sentence_id - 1] for sentence_id in sentence_ids)


def _normalize_speaker_id(value: Any, *, index: int, fallback_prefix: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"__{fallback_prefix}_{index}"


def _normalize_bubble_type(value: Any, *, index: int, context: str) -> str:
    if value is None:
        return DEFAULT_BUBBLE_TYPE
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise RuntimeError(f"{context} {index} must include a non-empty bubble_type when provided")


def extract_plan(response: dict[str, Any], dialogue_lines: list[str]) -> list[BubblePlan]:
    message, raw_message = _message_to_text(response)
    try:
        data = json.loads(message)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"model returned invalid JSON: {summarize_raw_output(raw_message)}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected JSON payload type: {type(data).__name__}")
    bubbles = data.get("bubbles")
    if not isinstance(bubbles, list) or not bubbles:
        raise RuntimeError("plan must include a non-empty bubbles array")

    used_sentence_ids: list[int] = []
    plans: list[BubblePlan] = []
    for index, bubble in enumerate(bubbles, start=1):
        if not isinstance(bubble, dict):
            raise RuntimeError(f"bubble {index} must be an object")
        columns = bubble.get("columns")
        sentence_ids = bubble.get("sentence_ids")
        if not isinstance(columns, list) or not columns:
            raise RuntimeError(f"bubble {index} must include a non-empty columns array")
        if not isinstance(sentence_ids, list) or not sentence_ids:
            raise RuntimeError(f"bubble {index} must include a non-empty sentence_ids array")
        normalized_columns = [str(item) for item in columns]
        normalized_ids = [int(item) for item in sentence_ids]
        if normalized_ids != list(range(normalized_ids[0], normalized_ids[0] + len(normalized_ids))):
            raise RuntimeError(f"bubble {index} sentence_ids must be consecutive")
        joined_text = "".join(dialogue_lines[sentence_id - 1] for sentence_id in normalized_ids)
        if "".join(normalized_columns) != joined_text:
            raise RuntimeError(f"bubble {index} columns do not reconstruct the assigned dialogue")
        used_sentence_ids.extend(normalized_ids)
        plans.append(
            BubblePlan(
                anchor_x=float(bubble["anchor_x"]),
                anchor_y=float(bubble["anchor_y"]),
                sentence_ids=normalized_ids,
                columns=normalized_columns,
                speaker_id=_normalize_speaker_id(bubble.get("speaker_id"), index=index, fallback_prefix="plan"),
                bubble_type=_normalize_bubble_type(bubble.get("bubble_type"), index=index, context="bubble"),
            )
        )

    expected_ids = list(range(1, len(dialogue_lines) + 1))
    if sorted(used_sentence_ids) != expected_ids:
        raise RuntimeError("bubbles must cover every dialogue line exactly once")
    return plans


def extract_scene_plan(response: dict[str, Any], dialogue_lines: list[str]) -> list[SceneBubblePlan]:
    message, raw_message = _message_to_text(response)
    try:
        data = json.loads(message)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"model returned invalid JSON: {summarize_raw_output(raw_message)}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected JSON payload type: {type(data).__name__}")
    bubbles = data.get("bubbles")
    if not isinstance(bubbles, list) or not bubbles:
        raise RuntimeError("scene plan must include a non-empty bubbles array")

    used_sentence_ids: list[int] = []
    seen_bubble_ids: set[str] = set()
    plans: list[SceneBubblePlan] = []
    for index, bubble in enumerate(bubbles, start=1):
        if not isinstance(bubble, dict):
            raise RuntimeError(f"bubble {index} must be an object")
        bubble_id = bubble.get("bubble_id")
        sentence_ids = bubble.get("sentence_ids")
        if not isinstance(bubble_id, str) or not bubble_id.strip():
            raise RuntimeError(f"bubble {index} must include a non-empty bubble_id")
        bubble_id = bubble_id.strip()
        if bubble_id in seen_bubble_ids:
            raise RuntimeError(f"bubble_id must be unique: {bubble_id}")
        seen_bubble_ids.add(bubble_id)
        if not isinstance(sentence_ids, list) or not sentence_ids:
            raise RuntimeError(f"bubble {index} must include a non-empty sentence_ids array")
        normalized_ids = [int(item) for item in sentence_ids]
        if normalized_ids != list(range(normalized_ids[0], normalized_ids[0] + len(normalized_ids))):
            raise RuntimeError(f"bubble {index} sentence_ids must be consecutive")
        used_sentence_ids.extend(normalized_ids)
        plans.append(
            SceneBubblePlan(
                bubble_id=bubble_id,
                anchor_x=float(bubble["anchor_x"]),
                anchor_y=float(bubble["anchor_y"]),
                sentence_ids=normalized_ids,
                speaker_id=_normalize_speaker_id(bubble.get("speaker_id"), index=index, fallback_prefix="scene"),
                bubble_type=_normalize_bubble_type(bubble.get("bubble_type"), index=index, context="bubble"),
            )
        )

    expected_ids = list(range(1, len(dialogue_lines) + 1))
    if sorted(used_sentence_ids) != expected_ids:
        raise RuntimeError("bubbles must cover every dialogue line exactly once")
    return plans


def extract_reflow_plan(
    response: dict[str, Any],
    dialogue_lines: list[str],
    assignment_plan: AssignmentBubblePlan,
) -> ReflowBubblePlan:
    message, raw_message = _message_to_text(response)
    try:
        data = json.loads(message)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"model returned invalid JSON: {summarize_raw_output(raw_message)}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected JSON payload type: {type(data).__name__}")
    bubble_id = data.get("bubble_id")
    columns = data.get("columns")
    if not isinstance(bubble_id, str) or not bubble_id.strip():
        raise RuntimeError("reflow output must include a non-empty bubble_id")
    bubble_id = bubble_id.strip()
    if bubble_id != assignment_plan.bubble_id:
        raise RuntimeError(f"unexpected bubble_id in reflow output: {bubble_id}")
    if not isinstance(columns, list) or not columns or not all(isinstance(item, str) and item for item in columns):
        raise RuntimeError("reflow output must include a non-empty columns array")
    plan = ReflowBubblePlan(
        bubble_id=bubble_id,
        sentence_ids=assignment_plan.sentence_ids,
        columns=columns,
    )
    return _validate_reflow_plans(dialogue_lines, [plan], require_full_coverage=False)[0]


def validate_assignment_plans(dialogue_lines: list[str], plans: list[AssignmentBubblePlan]) -> list[AssignmentBubblePlan]:
    used_sentence_ids: list[int] = []
    seen_bubble_ids: set[str] = set()
    for index, plan in enumerate(plans, start=1):
        if not plan.bubble_id.strip():
            raise RuntimeError(f"bubble {index} must include a non-empty bubble_id")
        if plan.bubble_id in seen_bubble_ids:
            raise RuntimeError(f"bubble_id must be unique: {plan.bubble_id}")
        seen_bubble_ids.add(plan.bubble_id)
        normalized_ids = [int(item) for item in plan.sentence_ids]
        if not normalized_ids:
            raise RuntimeError(f"bubble {index} must include a non-empty sentence_ids array")
        if normalized_ids != list(range(normalized_ids[0], normalized_ids[0] + len(normalized_ids))):
            raise RuntimeError(f"bubble {index} sentence_ids must be consecutive")
        used_sentence_ids.extend(normalized_ids)

    expected_ids = list(range(1, len(dialogue_lines) + 1))
    if sorted(used_sentence_ids) != expected_ids:
        raise RuntimeError("bubbles must cover every dialogue line exactly once")
    return plans


def validate_reflow_plans(dialogue_lines: list[str], plans: list[ReflowBubblePlan]) -> list[ReflowBubblePlan]:
    return _validate_reflow_plans(dialogue_lines, plans, require_full_coverage=True)


def _validate_reflow_plans(
    dialogue_lines: list[str],
    plans: list[ReflowBubblePlan],
    require_full_coverage: bool,
) -> list[ReflowBubblePlan]:
    used_sentence_ids: list[int] = []
    seen_bubble_ids: set[str] = set()
    punctuation_only_chars = set("、。，．？！?!…ー〜・「」『』（）()[]【】〈〉《》")
    for index, plan in enumerate(plans, start=1):
        if not plan.bubble_id.strip():
            raise RuntimeError(f"bubble {index} must include a non-empty bubble_id")
        if plan.bubble_id in seen_bubble_ids:
            raise RuntimeError(f"bubble_id must be unique: {plan.bubble_id}")
        seen_bubble_ids.add(plan.bubble_id)
        normalized_ids = [int(item) for item in plan.sentence_ids]
        if not normalized_ids:
            raise RuntimeError(f"bubble {index} must include a non-empty sentence_ids array")
        if normalized_ids != list(range(normalized_ids[0], normalized_ids[0] + len(normalized_ids))):
            raise RuntimeError(f"bubble {index} sentence_ids must be consecutive")
        text = text_for_sentence_ids(dialogue_lines, normalized_ids)
        if not plan.columns:
            raise RuntimeError(f"bubble {index} must include a non-empty columns array")
        for column in plan.columns:
            if not column.strip():
                raise RuntimeError(f"bubble {index} contains an empty column")
            if all(char in punctuation_only_chars for char in column):
                raise RuntimeError(f"bubble {index} contains a punctuation-only column")
        if "".join(plan.columns) != text:
            raise RuntimeError(f"bubble {index} columns do not reconstruct the assigned dialogue")
        used_sentence_ids.extend(normalized_ids)

    expected_ids = sorted(sentence_id for plan in plans for sentence_id in plan.sentence_ids)
    if sorted(used_sentence_ids) != expected_ids:
        raise RuntimeError("reflow plans contain unexpected sentence_ids")
    if require_full_coverage and expected_ids != list(range(1, len(dialogue_lines) + 1)):
        raise RuntimeError("bubbles must cover every dialogue line exactly once")
    return plans


def load_plan_json(plan_path: Path) -> tuple[list[str], list[BubblePlan]]:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("plan JSON must be an object")
    dialogue_lines = data.get("dialogue_lines")
    bubbles = data.get("bubbles")
    if not isinstance(dialogue_lines, list) or not all(isinstance(item, str) for item in dialogue_lines):
        raise RuntimeError("plan JSON must include dialogue_lines")
    if not isinstance(bubbles, list):
        raise RuntimeError("plan JSON must include bubbles")
    return dialogue_lines, extract_plan(
        {"choices": [{"message": {"content": json.dumps({"bubbles": bubbles}, ensure_ascii=False)}}]},
        dialogue_lines,
    )


def load_assignment_plan_json(plan_path: Path) -> tuple[list[str], list[AssignmentBubblePlan]]:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("assignment plan JSON must be an object")
    dialogue_lines = data.get("dialogue_lines")
    bubbles = data.get("bubbles")
    if not isinstance(dialogue_lines, list) or not all(isinstance(item, str) for item in dialogue_lines):
        raise RuntimeError("assignment plan JSON must include dialogue_lines")
    if not isinstance(bubbles, list):
        raise RuntimeError("assignment plan JSON must include bubbles")
    plans: list[AssignmentBubblePlan] = []
    for index, bubble in enumerate(bubbles, start=1):
        if not isinstance(bubble, dict):
            raise RuntimeError(f"bubble {index} must be an object")
        bubble_id = bubble.get("bubble_id")
        sentence_ids = bubble.get("sentence_ids")
        if not isinstance(bubble_id, str):
            raise RuntimeError(f"bubble {index} must include bubble_id")
        if not isinstance(sentence_ids, list):
            raise RuntimeError(f"bubble {index} must include sentence_ids")
        plans.append(AssignmentBubblePlan(bubble_id=bubble_id, sentence_ids=[int(item) for item in sentence_ids]))
    return dialogue_lines, validate_assignment_plans(dialogue_lines, plans)


def load_reflow_plan_json(plan_path: Path) -> tuple[list[str], list[ReflowBubblePlan]]:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("reflow plan JSON must be an object")
    dialogue_lines = data.get("dialogue_lines")
    bubbles = data.get("bubbles")
    if not isinstance(dialogue_lines, list) or not all(isinstance(item, str) for item in dialogue_lines):
        raise RuntimeError("reflow plan JSON must include dialogue_lines")
    if not isinstance(bubbles, list):
        raise RuntimeError("reflow plan JSON must include bubbles")
    plans: list[ReflowBubblePlan] = []
    for index, bubble in enumerate(bubbles, start=1):
        if not isinstance(bubble, dict):
            raise RuntimeError(f"bubble {index} must be an object")
        bubble_id = bubble.get("bubble_id")
        sentence_ids = bubble.get("sentence_ids")
        columns = bubble.get("columns")
        if not isinstance(bubble_id, str):
            raise RuntimeError(f"bubble {index} must include bubble_id")
        if not isinstance(sentence_ids, list):
            raise RuntimeError(f"bubble {index} must include sentence_ids")
        if not isinstance(columns, list) or not all(isinstance(item, str) for item in columns):
            raise RuntimeError(f"bubble {index} must include columns")
        plans.append(
            ReflowBubblePlan(
                bubble_id=bubble_id,
                sentence_ids=[int(item) for item in sentence_ids],
                columns=columns,
            )
        )
    return dialogue_lines, validate_reflow_plans(dialogue_lines, plans)


def load_scene_plan_json(plan_path: Path) -> tuple[list[str], list[SceneBubblePlan]]:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("scene plan JSON must be an object")
    dialogue_lines = data.get("dialogue_lines")
    bubbles = data.get("bubbles")
    if not isinstance(dialogue_lines, list) or not all(isinstance(item, str) for item in dialogue_lines):
        raise RuntimeError("scene plan JSON must include dialogue_lines")
    if not isinstance(bubbles, list):
        raise RuntimeError("scene plan JSON must include bubbles")
    return dialogue_lines, extract_scene_plan(
        {"choices": [{"message": {"content": json.dumps({"bubbles": bubbles}, ensure_ascii=False)}}]},
        dialogue_lines,
    )


def compose_bubble_plans(
    dialogue_lines: list[str],
    scene_plans: list[SceneBubblePlan],
    reflow_plans: list[ReflowBubblePlan],
) -> list[BubblePlan]:
    validate_reflow_plans(dialogue_lines, reflow_plans)
    reflow_by_sentence_ids: dict[tuple[int, ...], ReflowBubblePlan] = {
        tuple(plan.sentence_ids): plan for plan in reflow_plans
    }
    composed: list[BubblePlan] = []
    for index, scene_plan in enumerate(scene_plans, start=1):
        key = tuple(scene_plan.sentence_ids)
        reflow_plan = reflow_by_sentence_ids.get(key)
        if reflow_plan is None:
            raise RuntimeError(
                f"scene bubble {index} has sentence_ids with no matching reflow bubble: {list(scene_plan.sentence_ids)}"
            )
        composed.append(
            BubblePlan(
                anchor_x=scene_plan.anchor_x,
                anchor_y=scene_plan.anchor_y,
                sentence_ids=scene_plan.sentence_ids,
                columns=reflow_plan.columns,
                speaker_id=scene_plan.speaker_id,
                bubble_type=scene_plan.bubble_type,
            )
        )
    return sorted(composed, key=lambda plan: plan.sentence_ids[0])
