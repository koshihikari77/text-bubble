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


def build_evaluate_schema(max_bubbles: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
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
                "maxItems": max(1, max_bubbles * 4),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "bubble_id": {"type": "string", "minLength": 2, "maxLength": 32},
                        "type": {"type": "string", "enum": sorted(ISSUE_TYPES)},
                        "severity": {"type": "string", "enum": sorted(ISSUE_SEVERITIES)},
                        "description": {"type": "string", "minLength": 1, "maxLength": 300},
                        "fix_stage": {"type": "string", "enum": sorted(FIX_STAGES)},
                        "suggestion": {"type": "string", "minLength": 1, "maxLength": 300},
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
) -> str:
    rows = _plan_for_evaluate(plans)
    numbered_lines = "\n".join(f"{index}. {line}" for index, line in enumerate(dialogue_lines, start=1))
    plan_json = json.dumps(rows, ensure_ascii=False, indent=2)
    return (
        "以下を評価してください。\n\n"
        "## dialogue_lines\n"
        f"{numbered_lines}\n\n"
        "## bubbles (plan)\n"
        f"{plan_json}\n\n"
        "元画像とレンダリング後画像を比較し、必要なら issues を返してください。"
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

    if verdict == "pass" and normalized_issues:
        raise RuntimeError("evaluate verdict=pass must not include issues")
    if verdict == "needs_fix" and not normalized_issues:
        raise RuntimeError("evaluate verdict=needs_fix must include at least one issue")

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
) -> dict[str, Any]:
    body = {
        "model": model,
        "temperature": temperature,
        "top_k": 1,
        "n_predict": 420,
        "seed": 42,
        "reasoning_format": "none",
        "chat_template_kwargs": {
            "enable_thinking": True,
        },
        "json_schema": schema,
        "messages": [
            {"role": "system", "content": load_prompt_text("evaluate_prompt.md")},
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
    if not plans:
        raise RuntimeError("plan must contain at least one bubble for evaluation")
    prompt = build_evaluate_user_prompt(dialogue_lines, plans)
    response = _post_evaluate_chat_completion(
        server=server,
        model=model,
        prompt=prompt,
        original_image_data_url=encode_image_as_data_url(original_image_path),
        rendered_image_data_url=encode_image_as_data_url(rendered_image_path),
        temperature=temperature,
        schema=build_evaluate_schema(len(plans)),
    )
    return parse_evaluate_result(response, max_bubbles=len(plans))
