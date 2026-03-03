#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

# Speech-bubble sizing based on the text block.
# The LLM-provided anchor remains the text block's top-right corner.
BUBBLE_INNER_WIDTH_RATIO = 0.62
BUBBLE_INNER_HEIGHT_RATIO = 0.72
TEXT_COLUMN_GAP_RATIO = 0.1
DEFAULT_FONT_DIVISOR = 38
BUBBLE_FILL_OPACITY = 0.88
BUBBLE_FILL_ALPHA_PNG = 232
BUBBLE_STROKE_COLOR = "#111111"
TEXT_COLOR = "#111111"
TEXT_SHADOW = "none"


SYSTEM_PROMPT = """You are a manga vertical speech-bubble planner.
Given one image and one fixed set of Japanese dialogue lines, return exactly one JSON object and nothing else.
Never output markdown fences.
Never output prose outside the JSON object.
Respect every required field in the schema.
The dialogue text is fixed. Do not rewrite, omit, normalize, or add characters.
Choose safe anchor points for vertical text blocks.
Avoid covering faces or the most important action area."""


@dataclass
class BubblePlan:
    anchor_x: float
    anchor_y: float
    sentence_ids: list[int]
    columns: list[str]


@dataclass
class TextRenderResult:
    image: Image.Image
    alpha_bbox: tuple[int, int, int, int]


def bubble_plan_to_dict(plan: BubblePlan) -> dict[str, Any]:
    return {
        "anchor_x": plan.anchor_x,
        "anchor_y": plan.anchor_y,
        "sentence_ids": plan.sentence_ids,
        "columns": plan.columns,
    }


def plans_payload(dialogue_lines: list[str], plans: list[BubblePlan]) -> dict[str, Any]:
    return {
        "dialogue_lines": dialogue_lines,
        "bubbles": [bubble_plan_to_dict(plan) for plan in plans],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate one vertical speech bubble from an image using llama-server.")
    parser.add_argument("--input", required=True, help="Input image path")
    parser.add_argument("--output", required=True, help="Output image path")
    parser.add_argument("--plan-json", help="Optional path to save the raw bubble plan JSON")
    parser.add_argument("--server", default="http://127.0.0.1:8080/v1", help="llama-server base URL")
    parser.add_argument("--model", default="heretic", help="Model alias exposed by llama-server")
    parser.add_argument("--dialogue", required=True, help="Fixed Japanese dialogue to place in one bubble")
    parser.add_argument("--font", help="Font path for bubble text")
    parser.add_argument("--font-family", help="CSS font-family override for browser rendering")
    parser.add_argument("--bubble-asset", help="Bubble image asset path")
    parser.add_argument("--font-size", default=0, type=int, help="Override vertical text font size")
    parser.add_argument("--temperature", default=0.0, type=float, help="Sampling temperature")
    parser.add_argument(
        "--text-renderer",
        choices=["browser", "pango"],
        default="browser",
        help="Backend for vertical text rendering",
    )
    return parser.parse_args()


def pick_font_path(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for candidate in FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def encode_image_as_data_url(image_path: Path) -> str:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def encode_file_as_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def split_dialogue_lines(dialogue: str) -> list[str]:
    lines = [line.strip() for line in dialogue.splitlines() if line.strip()]
    if lines:
        return lines
    stripped = dialogue.strip()
    return [stripped] if stripped else []


def build_user_prompt(dialogue_lines: list[str]) -> str:
    numbered_lines = "\n".join(f"{index}. {line}" for index, line in enumerate(dialogue_lines, start=1))
    return textwrap.dedent(
        f"""
        Analyze this image and place vertical manga speech bubbles for the fixed dialogue lines below.

        Fixed dialogue lines:
        {numbered_lines}

        Requirements:
        - Return one JSON object with exactly one key: "bubbles".
        - "bubbles" must be an array of 1 to {len(dialogue_lines)} bubble objects.
        - Each bubble object must contain exactly: "anchor_x", "anchor_y", "sentence_ids", "columns".
        - "anchor_x" and "anchor_y" are normalized 0.0 to 1.0 image coordinates.
        - The anchor is the top-right corner of the text block, not the bubble outline.
        - "sentence_ids" must contain consecutive 1-based line indices from the list above.
        - Every dialogue line must appear exactly once across all bubbles.
        - Keep the original line order. Do not reorder lines.
        - "columns" must be ordered from right to left.
        - When all strings in "columns" are concatenated, they must exactly equal the assigned dialogue line(s) joined with no separator changes.
        - Split into natural manga-style vertical columns.
        - Prefer columns of roughly 3 to 7 Japanese characters.
        - Avoid over-fragmented output. Do not create many 1 or 2 character columns unless unavoidable.
        - Prefer one line per bubble unless two adjacent short lines clearly fit better together.
        - Do not add extra keys.
        - Do not use markdown code fences.
        """
    ).strip()


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
                    },
                    "required": ["anchor_x", "anchor_y", "sentence_ids", "columns"],
                },
            },
        },
        "required": ["bubbles"],
    }


def post_chat_completion(
    server: str,
    model: str,
    prompt: str,
    image_data_url: str,
    temperature: float,
    schema: dict[str, Any],
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
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
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


def extract_plan(response: dict[str, Any], dialogue_lines: list[str]) -> list[BubblePlan]:
    message = response["choices"][0]["message"]["content"]
    if isinstance(message, list):
        parts = [chunk.get("text", "") for chunk in message if isinstance(chunk, dict)]
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
            )
        )

    expected_ids = list(range(1, len(dialogue_lines) + 1))
    if sorted(used_sentence_ids) != expected_ids:
        raise RuntimeError("bubbles must cover every dialogue line exactly once")
    return plans


def summarize_raw_output(raw_message: str) -> str:
    compact = " ".join(line.strip() for line in raw_message.splitlines() if line.strip())
    if len(compact) > 200:
        compact = compact[:200].rstrip() + "..."
    return compact


def infer_bubble_plans(
    image_path: Path,
    server: str,
    model: str,
    dialogue: str,
    temperature: float,
) -> tuple[list[str], list[BubblePlan]]:
    dialogue_lines = split_dialogue_lines(dialogue)
    if not dialogue_lines:
        raise RuntimeError("dialogue must contain at least one non-empty line")
    image_data_url = encode_image_as_data_url(image_path)
    prompt = build_user_prompt(dialogue_lines)
    response = post_chat_completion(
        server=server,
        model=model,
        prompt=prompt,
        image_data_url=image_data_url,
        temperature=temperature,
        schema=build_plan_schema(len(dialogue_lines)),
    )
    return dialogue_lines, extract_plan(response, dialogue_lines)


def save_plan_json(plan_path: Path, dialogue_lines: list[str], plans: list[BubblePlan]) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(plans_payload(dialogue_lines, plans), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
    return dialogue_lines, extract_plan({"choices": [{"message": {"content": json.dumps({"bubbles": bubbles}, ensure_ascii=False)}}]}, dialogue_lines)


def build_text_metrics(font_size: int, columns: list[str]) -> dict[str, int]:
    em = max(font_size, 24)
    char_step = max(24, int(round(em * 1.08)))
    column_width = max(26, int(round(em * 1.0)))
    column_gap = max(4, int(round(em * TEXT_COLUMN_GAP_RATIO)))
    block_width = column_width * len(columns) + column_gap * max(0, len(columns) - 1)
    block_height = char_step * max(len(column) for column in columns)
    return {
        "em": em,
        "char_step": char_step,
        "column_width": column_width,
        "column_gap": column_gap,
        "block_width": block_width,
        "block_height": block_height,
    }


def compute_text_layout(
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    font_size: int,
) -> dict[str, int]:
    metrics = build_text_metrics(font_size, plan.columns)
    anchor_x = int(canvas_width * plan.anchor_x)
    anchor_y = int(canvas_height * plan.anchor_y)
    char_step = metrics["char_step"]
    column_width = metrics["column_width"]
    column_gap = metrics["column_gap"]
    block_width = metrics["block_width"]
    block_height = metrics["block_height"]

    if anchor_x <= 0 or anchor_y < 0:
        raise RuntimeError("anchor point is outside the image")

    em = max(font_size, 24)
    text_left = anchor_x - block_width
    text_top = anchor_y
    text_right = anchor_x
    text_bottom = anchor_y + block_height
    if text_left < 0 or text_top < 0:
        raise RuntimeError("text block anchor would place vertical text outside the image")

    if text_left < 0 or text_top < 0 or text_right > canvas_width or text_bottom > canvas_height:
        raise RuntimeError("text block layout exceeds image bounds")
    return {
        "font_size": font_size,
        "em": em,
        "char_step": char_step,
        "column_width": column_width,
        "column_gap": column_gap,
        "block_width": block_width,
        "block_height": block_height,
        "anchor_x": anchor_x,
        "anchor_y": anchor_y,
        "text_left": text_left,
        "text_top": text_top,
        "text_right": text_right,
        "text_bottom": text_bottom,
        "outline_width": max(3, canvas_width // 320),
    }


def compute_bubble_layout(
    canvas_width: int,
    canvas_height: int,
    text_bbox: tuple[int, int, int, int],
    font_size: int,
    outline_width: int,
) -> dict[str, int]:
    text_left, text_top, text_right, text_bottom = text_bbox
    text_width = text_right - text_left
    text_height = text_bottom - text_top
    em = max(font_size, 24)

    bubble_width = max(
        text_width + max(64, int(round(em * 2.5))),
        int(round(text_width / 0.48)),
    )
    bubble_height = max(
        text_height + max(20, int(round(em * 1.15))),
        int(round(text_height / 0.79)),
        int(round(bubble_width * 1.32)),
    )

    horizontal_slack = max(0, bubble_width - text_width)
    vertical_slack = max(0, bubble_height - text_height)
    bubble_left = text_left - horizontal_slack // 2
    bubble_top = text_top - vertical_slack // 2
    bubble_right = bubble_left + bubble_width
    bubble_bottom = bubble_top + bubble_height

    return {
        "bubble_left": bubble_left,
        "bubble_top": bubble_top,
        "bubble_right": bubble_right,
        "bubble_bottom": bubble_bottom,
        "bubble_width": bubble_width,
        "bubble_height": bubble_height,
        "outline_width": outline_width,
    }


def alpha_composite_clipped(base: Image.Image, overlay: Image.Image, left: int, top: int) -> None:
    src_left = max(0, -left)
    src_top = max(0, -top)
    dst_left = max(0, left)
    dst_top = max(0, top)
    width = min(overlay.width - src_left, base.width - dst_left)
    height = min(overlay.height - src_top, base.height - dst_top)
    if width <= 0 or height <= 0:
        return
    cropped = overlay.crop((src_left, src_top, src_left + width, src_top + height))
    base.alpha_composite(cropped, (dst_left, dst_top))


def browser_font_stack(font_path: str | None) -> str:
    families = []
    if font_path:
        name = Path(font_path).stem
        if "NotoSansCJK" in name:
            families.append('"Noto Sans CJK JP"')
        elif "NotoSerifCJK" in name:
            families.append('"Noto Serif CJK JP"')
        elif "IPA" in name or "ipag" in name.lower():
            families.append('"IPAGothic"')
        elif "DejaVuSans" in name:
            families.append('"DejaVu Sans"')
    families.extend(
        [
            '"BIZ UDPGothic"',
            '"BIZ UDMincho"',
            '"Hiragino Sans"',
            '"Hiragino Mincho ProN"',
            '"Yu Gothic"',
            '"Yu Gothic UI"',
            '"Yu Mincho"',
            '"IPAexGothic"',
            '"IPAGothic"',
            '"Noto Sans CJK JP"',
            '"Noto Serif CJK JP"',
            '"MS PGothic"',
            '"MS Mincho"',
            "sans-serif",
        ]
    )
    return ", ".join(dict.fromkeys(families))


def css_font_literal(value: str) -> str:
    value = value.strip()
    if "," in value or value.startswith(("'", '"')):
        return value
    return f'"{value}"'


def build_font_css(font_path: str | None, font_family: str | None) -> tuple[str, str]:
    fallback = browser_font_stack(font_path)
    if font_path:
        embedded_family = "__BubbleFont__"
        font_css = (
            "@font-face {"
            f"font-family: '{embedded_family}';"
            f"src: url('{encode_file_as_data_url(Path(font_path))}');"
            "font-display: swap;"
            "}"
        )
        return font_css, f'"{embedded_family}", {fallback}'
    if font_family:
        return "", f"{css_font_literal(font_family)}, {fallback}"
    return "", fallback


def resolve_bubble_asset(explicit: str | None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            Path(__file__).resolve().parent / "assets" / "bubble_ellipse.svg",
            Path("/notebooks/imgs/bubble.svg"),
            Path("/notebooks/imgs/bubble.png"),
            Path("/notebooks/resources/bubble.svg"),
            Path("/notebooks/resources/bubble.png"),
            Path("/notebooks/resources/bubble_svg.txt"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def resolve_chromium_executable() -> str | None:
    browser_root = Path(__file__).resolve().parent / ".playwright-browsers"
    candidates = sorted(browser_root.glob("chromium-*/chrome-linux64/chrome"))
    if candidates:
        return str(candidates[-1])
    return None


def bubble_png_to_rgba(asset_path: Path) -> Image.Image:
    asset = Image.open(asset_path).convert("L")
    width, height = asset.size
    pixels = asset.load()
    outline_cutoff = 240
    outside = [[False] * width for _ in range(height)]
    queue: deque[tuple[int, int]] = deque()

    def enqueue_if_open(x: int, y: int) -> None:
        if 0 <= x < width and 0 <= y < height and not outside[y][x] and pixels[x, y] >= outline_cutoff:
            outside[y][x] = True
            queue.append((x, y))

    for x in range(width):
        enqueue_if_open(x, 0)
        enqueue_if_open(x, height - 1)
    for y in range(height):
        enqueue_if_open(0, y)
        enqueue_if_open(width - 1, y)

    while queue:
        x, y = queue.popleft()
        enqueue_if_open(x + 1, y)
        enqueue_if_open(x - 1, y)
        enqueue_if_open(x, y + 1)
        enqueue_if_open(x, y - 1)

    rgba = Image.new("RGBA", asset.size, (0, 0, 0, 0))
    out = rgba.load()
    for y in range(height):
        for x in range(width):
            value = pixels[x, y]
            if value < outline_cutoff:
                out[x, y] = (0, 0, 0, 255)
            elif not outside[y][x]:
                out[x, y] = (255, 255, 255, BUBBLE_FILL_ALPHA_PNG)
            else:
                out[x, y] = (0, 0, 0, 0)

    return rgba


def load_bubble_svg_source(asset_path: Path) -> str:
    if asset_path.suffix.lower() == ".svg":
        return asset_path.read_text(encoding="utf-8")
    if asset_path.suffix.lower() == ".txt":
        return asset_path.read_text(encoding="utf-8")
    raise RuntimeError(f"unsupported SVG asset type: {asset_path}")


def build_bubble_svg_html(svg_source: str, width: int, height: int) -> str:
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html, body {{
  margin: 0;
  width: {width}px;
  height: {height}px;
  overflow: hidden;
  background: transparent !important;
}}
#asset {{
  width: {width}px;
  height: {height}px;
}}
#asset > svg {{
  display: block;
  width: 100%;
  height: 100%;
}}
#asset :root {{
  --stroke: {BUBBLE_STROKE_COLOR};
}}
#asset .bubble {{
  fill-opacity: {BUBBLE_FILL_OPACITY} !important;
  stroke: {BUBBLE_STROKE_COLOR} !important;
}}
</style>
</head>
<body>
  <div id="asset">{svg_source}</div>
</body>
</html>
"""


def white_to_transparent(image: Image.Image, cutoff: int = 248) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    for y in range(height):
        for x in range(width):
            r, g, b, _ = pixels[x, y]
            if r >= cutoff and g >= cutoff and b >= cutoff:
                pixels[x, y] = (255, 255, 255, 0)
            else:
                pixels[x, y] = (r, g, b, 255)
    return rgba


def build_render_html(
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_stack: str,
    font_css: str,
) -> str:
    text_columns = []
    for index, column in enumerate(plan.columns):
        left = text_layout["block_width"] - text_layout["column_width"] - index * (
            text_layout["column_width"] + text_layout["column_gap"]
        )
        text_columns.append(
            (
                '<div class="column" style="left:{left}px;width:{width}px;height:{height}px;line-height:{line_height}px">'
                "{text}</div>"
            ).format(
                left=left,
                width=text_layout["column_width"],
                height=text_layout["block_height"],
                line_height=text_layout["char_step"],
                text=html.escape(column),
            )
        )
    columns_html = "".join(text_columns)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
{font_css}
html, body {{
  margin: 0;
  width: {canvas_width}px;
  height: {canvas_height}px;
  overflow: hidden;
  background: transparent !important;
}}
.stage {{
  position: relative;
  width: {canvas_width}px;
  height: {canvas_height}px;
  overflow: hidden;
  background: transparent;
}}
.text-block {{
  position: absolute;
  z-index: 1;
  left: {text_layout["text_left"]}px;
  top: {text_layout["text_top"]}px;
  width: {text_layout["block_width"]}px;
  height: {text_layout["block_height"]}px;
}}
.column {{
  position: absolute;
  top: 0;
  writing-mode: vertical-rl;
  text-orientation: mixed;
  white-space: nowrap;
  font-family: {font_stack};
  font-size: {text_layout["font_size"]}px;
  font-weight: 500;
  color: {TEXT_COLOR};
  letter-spacing: 0;
  text-align: start;
  text-shadow: {TEXT_SHADOW};
}}
</style>
</head>
<body>
  <div class="stage">
    <div class="text-block">{columns_html}</div>
  </div>
</body>
</html>
"""


def alpha_bbox_or_fail(image: Image.Image) -> tuple[int, int, int, int]:
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        raise RuntimeError("text renderer produced an empty alpha layer")
    left, top, right, bottom = bbox
    return int(left), int(top), int(right), int(bottom)


def register_font_with_fontconfig(font_path: str | None) -> str | None:
    if not font_path:
        return None
    source = Path(font_path)
    if not source.exists():
        return None
    font_dir = Path.home() / ".local" / "share" / "fonts" / "text-bubble"
    font_dir.mkdir(parents=True, exist_ok=True)
    target = font_dir / source.name
    if not target.exists() or target.read_bytes() != source.read_bytes():
        shutil.copy2(source, target)
        subprocess.run(["fc-cache", "-f", str(font_dir)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        result = subprocess.run(
            ["fc-scan", "--format", "%{family[0]}\n", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    family = result.stdout.strip().splitlines()
    return family[0].strip() if family else None


def resolve_pango_family(font_path: str | None, font_family: str | None) -> str:
    if font_family:
        return font_family
    registered = register_font_with_fontconfig(font_path)
    if registered:
        return registered
    fallback = browser_font_stack(font_path).split(",")
    return fallback[0].strip().strip('"') if fallback else "sans-serif"


def render_text_overlay_browser(
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
) -> TextRenderResult:
    browser_root = Path(__file__).resolve().parent / ".playwright-browsers"
    if browser_root.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
    chromium_executable = resolve_chromium_executable()
    font_css, font_stack = build_font_css(font_path, font_family)
    html_doc = build_render_html(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        plan=plan,
        text_layout=text_layout,
        font_stack=font_stack,
        font_css=font_css,
    )

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {"headless": True}
        if chromium_executable:
            launch_kwargs["executable_path"] = chromium_executable
        browser = playwright.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": canvas_width, "height": canvas_height}, device_scale_factor=1)
        page.set_content(html_doc, wait_until="load")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(150)
        overlay_bytes = page.screenshot(omit_background=True)
        page.close()
        browser.close()

    overlay = Image.open(io.BytesIO(overlay_bytes)).convert("RGBA")
    return TextRenderResult(image=overlay, alpha_bbox=alpha_bbox_or_fail(overlay))


def render_text_overlay_pango(
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
) -> TextRenderResult:
    import cairocffi as cairo
    import pangocairocffi
    import pangocffi

    family = resolve_pango_family(font_path, font_family)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas_width, canvas_height)
    ctx = cairo.Context(surface)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()

    layout = pangocairocffi.create_layout(ctx)
    desc = pangocffi.FontDescription()
    desc.family = family
    desc.set_absolute_size(pangocffi.units_from_double(text_layout["font_size"]))
    layout.font_description = desc

    for column_index, column in enumerate(plan.columns):
        column_left = text_layout["text_left"] + text_layout["block_width"] - text_layout["column_width"] - column_index * (
            text_layout["column_width"] + text_layout["column_gap"]
        )
        for char_index, glyph in enumerate(column):
            cell_top = text_layout["text_top"] + char_index * text_layout["char_step"]
            layout.text = glyph
            ink_rect, logical_rect = layout.get_extents()
            logical_x = pangocffi.units_to_double(logical_rect.x)
            logical_y = pangocffi.units_to_double(logical_rect.y)
            logical_w = pangocffi.units_to_double(logical_rect.width)
            logical_h = pangocffi.units_to_double(logical_rect.height)
            draw_x = column_left + (text_layout["column_width"] - logical_w) / 2.0 - logical_x
            draw_y = cell_top + (text_layout["char_step"] - logical_h) / 2.0 - logical_y

            ctx.save()
            if glyph == "ー":
                cell_cx = column_left + text_layout["column_width"] / 2.0
                cell_cy = cell_top + text_layout["char_step"] / 2.0
                ctx.translate(cell_cx, cell_cy)
                ctx.rotate(-1.5707963267948966)
                draw_x = -logical_w / 2.0 - logical_x
                draw_y = -logical_h / 2.0 - logical_y
            else:
                ctx.translate(draw_x, draw_y)
                draw_x = 0.0
                draw_y = 0.0
            ctx.set_source_rgba(0.067, 0.067, 0.067, 1.0)
            ctx.move_to(draw_x, draw_y)
            pangocairocffi.update_layout(ctx, layout)
            pangocairocffi.show_layout(ctx, layout)
            ctx.restore()

    png_buffer = io.BytesIO()
    surface.write_to_png(png_buffer)
    overlay = Image.open(io.BytesIO(png_buffer.getvalue())).convert("RGBA")
    return TextRenderResult(image=overlay, alpha_bbox=alpha_bbox_or_fail(overlay))


def render_text_overlay(
    renderer: str,
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
) -> TextRenderResult:
    if renderer == "browser":
        return render_text_overlay_browser(
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            plan=plan,
            text_layout=text_layout,
            font_path=font_path,
            font_family=font_family,
        )
    if renderer == "pango":
        return render_text_overlay_pango(
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            plan=plan,
            text_layout=text_layout,
            font_path=font_path,
            font_family=font_family,
        )
    raise RuntimeError(f"unsupported text renderer: {renderer}")


def render_bubble(
    image_path: Path,
    output_path: Path,
    plan: BubblePlan,
    font_path: str | None,
    font_family: str | None,
    bubble_asset: Path,
    font_size: int,
    text_renderer: str,
) -> None:
    browser_root = Path(__file__).resolve().parent / ".playwright-browsers"
    if browser_root.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
    chromium_executable = resolve_chromium_executable()
    image = Image.open(image_path)
    width_px, height_px = image.size
    image.close()
    actual_font_size = font_size or max(22, min(48, height_px // DEFAULT_FONT_DIVISOR))
    text_layout = compute_text_layout(width_px, height_px, plan, actual_font_size)
    text_overlay = render_text_overlay(
        renderer=text_renderer,
        canvas_width=width_px,
        canvas_height=height_px,
        plan=plan,
        text_layout=text_layout,
        font_path=font_path,
        font_family=font_family,
    )
    bubble_layout = compute_bubble_layout(
        canvas_width=width_px,
        canvas_height=height_px,
        text_bbox=text_overlay.alpha_bbox,
        font_size=actual_font_size,
        outline_width=text_layout["outline_width"],
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    base = Image.open(image_path).convert("RGBA")

    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {"headless": True}
        if chromium_executable:
            launch_kwargs["executable_path"] = chromium_executable
        browser = playwright.chromium.launch(**launch_kwargs)
        if bubble_asset.suffix.lower() == ".png":
            bubble_image = bubble_png_to_rgba(bubble_asset).resize(
                (bubble_layout["bubble_width"], bubble_layout["bubble_height"]),
                Image.Resampling.LANCZOS,
            )
        else:
            bubble_page = browser.new_page(
                viewport={"width": bubble_layout["bubble_width"], "height": bubble_layout["bubble_height"]},
                device_scale_factor=1,
            )
            bubble_svg = load_bubble_svg_source(bubble_asset)
            bubble_page.set_content(
                build_bubble_svg_html(
                    svg_source=bubble_svg,
                    width=bubble_layout["bubble_width"],
                    height=bubble_layout["bubble_height"],
                ),
                wait_until="load",
            )
            bubble_page.wait_for_load_state("networkidle")
            bubble_page.wait_for_timeout(100)
            bubble_image = Image.open(io.BytesIO(bubble_page.screenshot(omit_background=True))).convert("RGBA").resize(
                (bubble_layout["bubble_width"], bubble_layout["bubble_height"]),
                Image.Resampling.LANCZOS,
            )
            bubble_page.close()
        browser.close()

    alpha_composite_clipped(base, bubble_image, bubble_layout["bubble_left"], bubble_layout["bubble_top"])
    base.alpha_composite(text_overlay.image)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        base.convert("RGB").save(output_path, quality=95)
    else:
        base.save(output_path)


def render_bubbles(
    image_path: Path,
    output_path: Path,
    plans: list[BubblePlan],
    font_path: str | None,
    font_family: str | None,
    bubble_asset: Path,
    font_size: int,
    text_renderer: str,
) -> None:
    current_input = image_path
    if not plans:
        raise RuntimeError("no bubble plans to render")
    if len(plans) == 1:
        render_bubble(
            image_path=image_path,
            output_path=output_path,
            plan=plans[0],
            font_path=font_path,
            font_family=font_family,
            bubble_asset=bubble_asset,
            font_size=font_size,
            text_renderer=text_renderer,
        )
        return

    temp_dir = output_path.parent / ".tmp-bubble-render"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_paths: list[Path] = []
    for index, plan in enumerate(plans, start=1):
        target = output_path if index == len(plans) else temp_dir / f"render_{index:02d}.png"
        render_bubble(
            image_path=current_input,
            output_path=target,
            plan=plan,
            font_path=font_path,
            font_family=font_family,
            bubble_asset=bubble_asset,
            font_size=font_size,
            text_renderer=text_renderer,
        )
        if target != output_path:
            temp_paths.append(target)
            current_input = target

    for temp_path in temp_paths:
        if temp_path.exists():
            temp_path.unlink()
    if temp_dir.exists():
        try:
            temp_dir.rmdir()
        except OSError:
            pass


def main() -> int:
    args = parse_args()
    image_path = Path(args.input)
    output_path = Path(args.output)

    if not image_path.exists():
        print(f"input image not found: {image_path}", file=sys.stderr)
        return 1

    font_path = pick_font_path(args.font)
    if font_path is None and not args.font_family:
        print("warning: no Japanese-capable font found, install fonts-noto-cjk or pass --font", file=sys.stderr)
    bubble_asset = resolve_bubble_asset(args.bubble_asset)
    if bubble_asset is None:
        print(f"bubble asset not found: {bubble_asset}", file=sys.stderr)
        return 1

    try:
        dialogue_lines, plans = infer_bubble_plans(
            image_path=image_path,
            server=args.server,
            model=args.model,
            dialogue=args.dialogue,
            temperature=args.temperature,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.plan_json:
        save_plan_json(Path(args.plan_json), dialogue_lines, plans)

    render_bubbles(
        image_path=image_path,
        output_path=output_path,
        plans=plans,
        font_path=font_path,
        font_family=args.font_family,
        bubble_asset=bubble_asset,
        font_size=args.font_size,
        text_renderer=args.text_renderer,
    )

    print(
        json.dumps(
            {
                **plans_payload(dialogue_lines, plans),
                "output": str(output_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
