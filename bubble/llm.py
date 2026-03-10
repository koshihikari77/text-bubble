from __future__ import annotations

import base64
import json
import mimetypes
import textwrap
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

from bubble.models import AssignmentBubblePlan, PROMPTS_DIR


@lru_cache(maxsize=None)
def load_prompt_text(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def load_reflow_examples() -> list[dict[str, Any]]:
    path = PROMPTS_DIR / "reflow_examples.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("reflow_examples.json must be an array")
    return data


def split_dialogue_lines(dialogue: str) -> list[str]:
    lines = [line.strip() for line in dialogue.splitlines() if line.strip()]
    if lines:
        return lines
    stripped = dialogue.strip()
    return [stripped] if stripped else []


def build_user_prompt(dialogue_lines: list[str]) -> str:
    numbered_lines = "\n".join(f"{index}. {line}" for index, line in enumerate(dialogue_lines, start=1))
    template = load_prompt_text("planner_user.txt")
    return template.format(numbered_lines=numbered_lines, num_lines=len(dialogue_lines))


def build_scene_user_prompt(dialogue_lines: list[str]) -> str:
    numbered_lines = "\n".join(f"{index}. {line}" for index, line in enumerate(dialogue_lines, start=1))
    template = load_prompt_text("scene_user.txt")
    return template.format(numbered_lines=numbered_lines, num_lines=len(dialogue_lines))


def _text_for_sentence_ids(dialogue_lines: list[str], sentence_ids: list[int]) -> str:
    return "".join(dialogue_lines[sentence_id - 1] for sentence_id in sentence_ids)


def build_reflow_user_prompt(
    dialogue_lines: list[str],
    assignment_plan: AssignmentBubblePlan,
) -> str:
    text = _text_for_sentence_ids(dialogue_lines, assignment_plan.sentence_ids)
    sentence_ids = ", ".join(str(item) for item in assignment_plan.sentence_ids)
    bubble_text = textwrap.dedent(
        f"""
        - bubble_id: {assignment_plan.bubble_id}
        - sentence_ids: [{sentence_ids}]
        - text: {text}
        """
    ).strip()
    examples = []
    for example in load_reflow_examples():
        examples.append(
            textwrap.dedent(
                f"""
                Input text: {example["text"]}
                Output columns: {json.dumps(example["columns"], ensure_ascii=False)}
                """
            ).strip()
        )
    examples_text = "\n\n".join(examples)
    template = load_prompt_text("reflow_user.txt")
    return template.format(examples_text=examples_text, bubble_text=bubble_text)


def build_reflow_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "bubble_id": {"type": "string", "minLength": 1, "maxLength": 32},
            "columns": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {"type": "string", "minLength": 1, "maxLength": 32},
            },
        },
        "required": ["bubble_id", "columns"],
    }


def build_plan_schema(num_lines: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "bubbles": {
                "type": "array",
                "minItems": 1,
                "maxItems": num_lines,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "anchor_x": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "anchor_y": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "sentence_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": num_lines,
                            "items": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": num_lines,
                            },
                        },
                        "columns": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 12,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 24,
                            },
                        },
                        "speaker_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                        },
                        "bubble_type": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                        },
                    },
                    "required": ["anchor_x", "anchor_y", "sentence_ids", "columns"],
                },
            },
        },
        "required": ["bubbles"],
    }


def build_scene_plan_schema(num_lines: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "bubbles": {
                "type": "array",
                "minItems": 1,
                "maxItems": num_lines,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "bubble_id": {
                            "type": "string",
                            "minLength": 2,
                            "maxLength": 32,
                        },
                        "anchor_x": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "anchor_y": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "sentence_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": num_lines,
                            "items": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": num_lines,
                            },
                        },
                        "speaker_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                        },
                        "bubble_type": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                        },
                    },
                    "required": ["bubble_id", "anchor_x", "anchor_y", "sentence_ids"],
                },
            },
        },
        "required": ["bubbles"],
    }


def encode_image_as_data_url(image_path: Path) -> str:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def encode_file_as_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def post_chat_completion(
    server: str,
    model: str,
    prompt: str,
    image_data_url: str | None,
    temperature: float,
    schema: dict[str, Any],
    system_prompt: str | None = None,
    enable_thinking: bool = False,
    n_predict: int = 220,
) -> dict[str, Any]:
    if system_prompt is None:
        system_prompt = load_prompt_text("planner_system.txt")
    user_content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_data_url is not None:
        user_content.append({"type": "image_url", "image_url": {"url": image_data_url}})
    body = {
        "model": model,
        "temperature": temperature,
        "top_k": 1,
        "n_predict": n_predict,
        "seed": 42,
        "reasoning_format": "none",
        "chat_template_kwargs": {
            "enable_thinking": enable_thinking,
        },
        "json_schema": schema,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": user_content,
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
