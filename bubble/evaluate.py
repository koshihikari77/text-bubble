from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from bubble.llm import encode_image_as_data_url, load_prompt_text
from bubble.models import BubblePlan


ISSUE_TYPES = {"position", "reflow", "overlap", "readability", "size"}
ISSUE_SEVERITIES = {"high", "medium", "low"}
FIX_STAGES = {"scene", "reflow", "assignment", "render"}
EVALUATE_STAGES = {"final", "text"}
SEVERITY_ALIASES = {"critical": "high", "major": "high", "minor": "low", "moderate": "medium"}
TEXT_STAGE_GUIDE_PAD_X = 12
TEXT_STAGE_GUIDE_PAD_Y = 14


def _issue_types_for_stage(stage: str) -> list[str]:
    if stage == "text":
        return ["position", "reflow", "overlap", "readability"]
    return sorted(ISSUE_TYPES)


def build_evaluate_schema(max_bubbles: int, *, stage: str) -> dict[str, Any]:
    del max_bubbles
    return {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["pass", "needs_fix"],
            },
            "score": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bubble_id": {"type": "string"},
                        "type": {"type": "string", "enum": _issue_types_for_stage(stage)},
                        "severity": {"type": "string", "enum": sorted(ISSUE_SEVERITIES)},
                        "description": {"type": "string"},
                        "fix_stage": {"type": "string", "enum": sorted(FIX_STAGES)},
                        "suggestion": {"type": "string"},
                    },
                    "required": ["bubble_id", "type", "severity", "description", "fix_stage", "suggestion"],
                },
            },
        },
        "required": ["verdict", "score", "issues"],
    }


def _plan_for_evaluate(plans: list[BubblePlan]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, plan in enumerate(plans, start=1):
        rows.append(
            {
                "bubble_id": f"B{index}",
                "sentence_ids": plan.sentence_ids,
                "anchor_x": plan.anchor_x,
                "anchor_y": plan.anchor_y,
                "columns": plan.columns,
                "text": "".join(plan.columns),
                "speaker_id": plan.speaker_id,
            }
        )
    return rows


def build_evaluate_user_prompt(
    dialogue_lines: list[str],
    plans: list[BubblePlan],
    *,
    stage: str,
) -> str:
    if stage not in EVALUATE_STAGES:
        raise RuntimeError(f"unsupported evaluate stage: {stage}")
    rows = _plan_for_evaluate(plans)
    numbered_lines = "\n".join(
        f"{index}. {line}"
        for index, line in enumerate(dialogue_lines, start=1)
    )
    bubble_lines = "\n".join(
        (
            f"- {row['bubble_id']}:"
            f" sentence_ids={row['sentence_ids']},"
            f" anchor=({row['anchor_x']:.3f}, {row['anchor_y']:.3f}),"
            f" speaker_id={json.dumps(row['speaker_id'] or '-', ensure_ascii=False)},"
            f" columns={json.dumps(row['columns'], ensure_ascii=False)}"
        )
        for row in rows
    )
    if stage == "text":
        return (
            "これは text stage の確認です。画像内の文字ガイド同士の位置関係と読みやすさだけを見てください。\n"
            "白い角丸の箱は最終的な吹き出しではなく、文字ブロックの目安です。\n"
            "吹き出しの形や大きさはまだ確定していないので評価しないでください。\n"
            "anchor 座標は厳密一致をチェックするためのものではなく、おおよその配置を知るための補助情報です。\n\n"
            "dialogue_lines:\n"
            f"{numbered_lines}\n\n"
            "bubble plans:\n"
            f"{bubble_lines}"
        )
    return (
        "これは final render の確認です。元画像と完成画像を見比べて、実際に見えている問題だけを指摘してください。\n"
        "anchor 座標は厳密一致をチェックするためのものではなく、おおよその配置を知るための補助情報です。\n\n"
        "dialogue_lines:\n"
        f"{numbered_lines}\n\n"
        "bubble plans:\n"
        f"{bubble_lines}"
    )


def _message_to_text(response: dict[str, Any]) -> str:
    message = response["choices"][0]["message"]["content"]
    if isinstance(message, list):
        parts = [chunk.get("text", "") for chunk in message if isinstance(chunk, dict) and isinstance(chunk.get("text"), str)]
        message = "".join(parts)
    if not isinstance(message, str):
        raise RuntimeError("unexpected response content type")
    message = message.strip()
    if message.startswith("```"):
        message = message.strip("`")
        if message.startswith("json"):
            message = message[4:]
        message = message.strip()
    return message


def _summarize_raw_output(raw_message: str) -> str:
    compact = " ".join(line.strip() for line in raw_message.splitlines() if line.strip())
    if len(compact) > 200:
        compact = compact[:200].rstrip() + "..."
    return compact


def _extract_json_object_text(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == "\"":
                in_string = False
            continue
        if ch == "\"":
            in_string = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _normalize_bubble_id(value: Any, max_bubbles: int, issue_index: int) -> str:
    if isinstance(value, str):
        bubble_id_match = re.fullmatch(r"[Bb](\d+)", value.strip())
        if bubble_id_match is not None:
            bubble_index = int(bubble_id_match.group(1))
            if 1 <= bubble_index <= max_bubbles:
                return f"B{bubble_index}"
    raise RuntimeError(f"issue {issue_index} bubble_id must be like B1, B2: {value}")


def _bbox_intersection_area(
    left_a: int,
    top_a: int,
    right_a: int,
    bottom_a: int,
    left_b: int,
    top_b: int,
    right_b: int,
    bottom_b: int,
) -> int:
    inter_left = max(left_a, left_b)
    inter_top = max(top_a, top_b)
    inter_right = min(right_a, right_b)
    inter_bottom = min(bottom_a, bottom_b)
    if inter_right <= inter_left or inter_bottom <= inter_top:
        return 0
    return (inter_right - inter_left) * (inter_bottom - inter_top)


def _expand_text_bbox(bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    return (
        left - TEXT_STAGE_GUIDE_PAD_X,
        top - TEXT_STAGE_GUIDE_PAD_Y,
        right + TEXT_STAGE_GUIDE_PAD_X,
        bottom + TEXT_STAGE_GUIDE_PAD_Y,
    )


def deterministic_text_overlap_issues(
    plans: list[BubblePlan],
    text_bboxes: list[tuple[int, int, int, int]],
) -> list[dict[str, Any]]:
    if len(plans) != len(text_bboxes):
        raise RuntimeError("text_bboxes length must match plans length")
    issues: list[dict[str, Any]] = []
    for left_index in range(len(text_bboxes)):
        left_bbox = _expand_text_bbox(text_bboxes[left_index])
        for right_index in range(left_index + 1, len(text_bboxes)):
            right_bbox = _expand_text_bbox(text_bboxes[right_index])
            overlap_area = _bbox_intersection_area(*left_bbox, *right_bbox)
            if overlap_area <= 0:
                continue
            left_plan = plans[left_index]
            right_plan = plans[right_index]
            left_bubble_id = f"B{left_index + 1}"
            right_bubble_id = f"B{right_index + 1}"
            suggestion = "separate the overlapping text blocks"
            issues.append(
                {
                    "bubble_id": left_bubble_id,
                    "type": "overlap",
                    "severity": "high",
                    "description": (
                        f"text bbox overlaps with {right_bubble_id}; "
                        f"sentence_ids={left_plan.sentence_ids} and {right_plan.sentence_ids}; "
                        f"overlap_area={overlap_area}px"
                    ),
                    "fix_stage": "scene",
                    "suggestion": suggestion,
                }
            )
            issues.append(
                {
                    "bubble_id": right_bubble_id,
                    "type": "overlap",
                    "severity": "high",
                    "description": (
                        f"text bbox overlaps with {left_bubble_id}; "
                        f"sentence_ids={right_plan.sentence_ids} and {left_plan.sentence_ids}; "
                        f"overlap_area={overlap_area}px"
                    ),
                    "fix_stage": "scene",
                    "suggestion": suggestion,
                }
            )
    return issues


def parse_evaluate_result(response: dict[str, Any], max_bubbles: int) -> dict[str, Any]:
    raw_text = _message_to_text(response)
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        extracted = _extract_json_object_text(raw_text)
        if extracted is None:
            raise RuntimeError(f"model returned invalid JSON: {_summarize_raw_output(raw_text)}") from exc
        try:
            data = json.loads(extracted)
        except json.JSONDecodeError as nested_exc:
            raise RuntimeError(f"model returned invalid JSON: {_summarize_raw_output(raw_text)}") from nested_exc
    if not isinstance(data, dict):
        raise RuntimeError("evaluate output must be a JSON object")
    verdict_raw = data.get("verdict")
    verdict = verdict_raw.strip().lower() if isinstance(verdict_raw, str) else verdict_raw
    score_raw = data.get("score")
    issues = data.get("issues")
    if verdict not in {"pass", "needs_fix"}:
        raise RuntimeError("evaluate output must include verdict=pass|needs_fix")
    try:
        score = float(score_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("evaluate output must include score between 0.0 and 1.0") from exc
    if score > 1.0 and score <= 100.0:
        score = score / 100.0
    if not (0.0 <= score <= 1.0):
        raise RuntimeError("evaluate output must include score between 0.0 and 1.0")
    if not isinstance(issues, list):
        raise RuntimeError("evaluate output must include issues array")

    normalized_issues: list[dict[str, Any]] = []
    for idx, issue in enumerate(issues, start=1):
        if not isinstance(issue, dict):
            raise RuntimeError(f"issue {idx} must be an object")
        bubble_id_raw = issue.get("bubble_id")
        issue_type_raw = issue.get("type")
        severity_raw = issue.get("severity")
        fix_stage_raw = issue.get("fix_stage")
        description = issue.get("description")
        suggestion = issue.get("suggestion")
        bubble_id = _normalize_bubble_id(bubble_id_raw, max_bubbles, idx)
        issue_type = issue_type_raw.strip().lower() if isinstance(issue_type_raw, str) else issue_type_raw
        severity = severity_raw.strip().lower() if isinstance(severity_raw, str) else severity_raw
        if isinstance(severity, str):
            severity = SEVERITY_ALIASES.get(severity, severity)
        fix_stage = fix_stage_raw.strip().lower() if isinstance(fix_stage_raw, str) else fix_stage_raw
        if issue_type not in ISSUE_TYPES:
            raise RuntimeError(f"issue {idx} has invalid type: {issue_type}")
        if severity not in ISSUE_SEVERITIES:
            raise RuntimeError(f"issue {idx} has invalid severity: {severity}")
        if fix_stage not in FIX_STAGES:
            raise RuntimeError(f"issue {idx} has invalid fix_stage: {fix_stage}")
        if not isinstance(description, str) or not description.strip():
            raise RuntimeError(f"issue {idx} must include description")
        if not isinstance(suggestion, str) or not suggestion.strip():
            raise RuntimeError(f"issue {idx} must include suggestion")
        normalized_issues.append(
            {
                "bubble_id": bubble_id,
                "type": issue_type,
                "severity": severity,
                "description": description.strip(),
                "fix_stage": fix_stage,
                "suggestion": suggestion.strip(),
            }
        )

    if normalized_issues:
        verdict = "needs_fix"
    else:
        verdict = "pass"

    return {
        "verdict": verdict,
        "score": score,
        "issues": normalized_issues,
    }


def _post_evaluate_chat_completion(
    server: str,
    model: str,
    prompt: str,
    original_image_data_url: str,
    rendered_image_data_url: str,
    temperature: float,
    schema: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    body = {
        "model": model,
        "temperature": temperature,
        "top_k": 1,
        "n_predict": 220,
        "seed": 42,
        "reasoning_format": "none",
        "chat_template_kwargs": {
            "enable_thinking": False,
        },
        "json_schema": schema,
        "messages": [
            {
                "role": "system",
                "content": load_prompt_text("evaluate_text_prompt.md" if stage == "text" else "evaluate_final_prompt.md"),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": original_image_data_url}},
                    {"type": "image_url", "image_url": {"url": rendered_image_data_url}},
                ],
            },
        ],
    }
    request = urllib.request.Request(
        url=f"{server.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"server returned HTTP {exc.code}: {payload}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach llama-server: {exc}") from exc


def evaluate_rendered_result(
    *,
    server: str,
    model: str,
    temperature: float,
    dialogue_lines: list[str],
    plans: list[BubblePlan],
    original_image_path: Path,
    rendered_image_path: Path,
) -> dict[str, Any]:
    return evaluate_preview_result(
        server=server,
        model=model,
        temperature=temperature,
        dialogue_lines=dialogue_lines,
        plans=plans,
        original_image_path=original_image_path,
        preview_image_path=rendered_image_path,
        stage="final",
    )


def evaluate_preview_result(
    *,
    server: str,
    model: str,
    temperature: float,
    dialogue_lines: list[str],
    plans: list[BubblePlan],
    original_image_path: Path,
    preview_image_path: Path,
    stage: str,
    text_bboxes: list[tuple[int, int, int, int]] | None = None,
) -> dict[str, Any]:
    if not plans:
        raise RuntimeError("plan must contain at least one bubble for evaluation")
    if stage not in EVALUATE_STAGES:
        raise RuntimeError(f"unsupported evaluate stage: {stage}")
    if stage == "text":
        overlap_issues = deterministic_text_overlap_issues(plans, text_bboxes or [])
        if overlap_issues:
            return {
                "verdict": "needs_fix",
                "score": 0.0,
                "issues": overlap_issues,
            }
    prompt = build_evaluate_user_prompt(dialogue_lines, plans, stage=stage)
    response = _post_evaluate_chat_completion(
        server=server,
        model=model,
        prompt=prompt,
        original_image_data_url=encode_image_as_data_url(original_image_path),
        rendered_image_data_url=encode_image_as_data_url(preview_image_path),
        temperature=temperature,
        schema=build_evaluate_schema(len(plans), stage=stage),
        stage=stage,
    )
    return parse_evaluate_result(response, max_bubbles=len(plans))
