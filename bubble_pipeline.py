#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import mimetypes
import os
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


SYSTEM_PROMPT = """You are a manga vertical speech-bubble planner.
Given one image and one fixed Japanese line, return exactly one JSON object and nothing else.
Never output markdown fences.
Never output prose outside the JSON object.
Respect every required field in the schema.
The dialogue text is fixed. Do not rewrite, omit, normalize, or add characters.
Split the exact dialogue into natural manga-style vertical columns.
Choose one safe top-right anchor point for the vertical text block.
Avoid covering faces or the most important action area."""


@dataclass
class BubblePlan:
    anchor_x: float
    anchor_y: float
    columns: list[str]


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


def build_user_prompt(dialogue: str) -> str:
    return textwrap.dedent(
        f"""
        Analyze this image and place one vertical manga speech bubble for the fixed dialogue below.

        Fixed dialogue:
        {dialogue}

        Requirements:
        - Return one JSON object with exactly these keys: "anchor_x", "anchor_y", "columns".
        - "anchor_x" and "anchor_y" are normalized 0.0 to 1.0 image coordinates.
        - The anchor is the top-right corner of the text block, not the bubble outline.
        - "columns" must be ordered from right to left.
        - When all strings in "columns" are concatenated, they must exactly equal:
          {dialogue}
        - Split only into natural manga-style vertical columns.
        - Do not add extra keys.
        - Do not use markdown code fences.
        """
    ).strip()


def build_plan_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "anchor_x": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "anchor_y": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "columns": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 12,
                },
            },
        },
        "required": ["anchor_x", "anchor_y", "columns"],
    }


def post_chat_completion(
    server: str,
    model: str,
    prompt: str,
    image_data_url: str,
    temperature: float,
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
        "json_schema": build_plan_schema(),
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


def extract_plan(response: dict[str, Any], dialogue: str) -> BubblePlan:
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
    columns = data.get("columns")
    if not isinstance(columns, list) or not columns:
        raise RuntimeError("plan must include a non-empty columns array")
    normalized_columns = [str(item) for item in columns]
    if "".join(normalized_columns) != dialogue:
        raise RuntimeError("columns do not reconstruct the exact dialogue")
    return BubblePlan(
        anchor_x=float(data["anchor_x"]),
        anchor_y=float(data["anchor_y"]),
        columns=normalized_columns,
    )


def summarize_raw_output(raw_message: str) -> str:
    compact = " ".join(line.strip() for line in raw_message.splitlines() if line.strip())
    if len(compact) > 200:
        compact = compact[:200].rstrip() + "..."
    return compact


def build_text_metrics(font_size: int, columns: list[str]) -> dict[str, int]:
    em = max(font_size, 24)
    char_step = max(30, int(round(em * 1.25)))
    column_width = max(26, int(round(em * 1.0)))
    column_gap = max(6, int(round(em * 0.28)))
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


def compute_layout(
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

    text_left = anchor_x - block_width
    text_top = anchor_y
    text_right = anchor_x
    text_bottom = anchor_y + block_height
    if text_left < 0 or text_top < 0:
        raise RuntimeError("text block anchor would place vertical text outside the image")

    if anchor_x <= 0 or anchor_y < 0:
        raise RuntimeError("anchor point is outside the image")

    em = max(font_size, 24)
    pad_left = max(22, int(round(em * 1.0)))
    pad_right = max(22, int(round(em * 1.0)))
    pad_top = max(20, int(round(em * 0.9)))
    pad_bottom = max(24, int(round(em * 1.1)))
    bubble_left = text_left - pad_left
    bubble_top = text_top - pad_top
    bubble_right = text_right + pad_right
    bubble_bottom = text_bottom + pad_bottom
    bubble_width = bubble_right - bubble_left
    bubble_height = bubble_bottom - bubble_top
    min_height = int(round(bubble_width * 1.6))
    if bubble_height < min_height:
        extra = min_height - bubble_height
        bubble_top -= extra // 2
        bubble_bottom += extra - extra // 2

    if bubble_left < 0 or bubble_top < 0 or bubble_right > canvas_width or bubble_bottom > canvas_height:
        raise RuntimeError("bubble outline exceeds image bounds")
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
        "bubble_left": bubble_left,
        "bubble_top": bubble_top,
        "bubble_right": bubble_right,
        "bubble_bottom": bubble_bottom,
        "bubble_width": bubble_width,
        "bubble_height": bubble_height,
        "outline_width": max(3, canvas_width // 320),
    }


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
                out[x, y] = (255, 255, 255, 244)
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
    layout: dict[str, int],
    font_stack: str,
    font_css: str,
) -> str:
    text_columns = []
    for index, column in enumerate(plan.columns):
        left = layout["block_width"] - layout["column_width"] - index * (layout["column_width"] + layout["column_gap"])
        text_columns.append(
            (
                '<div class="column" style="left:{left}px;width:{width}px;height:{height}px;line-height:{line_height}px">'
                "{text}</div>"
            ).format(
                left=left,
                width=layout["column_width"],
                height=layout["block_height"],
                line_height=layout["char_step"],
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
  left: {layout["text_left"]}px;
  top: {layout["text_top"]}px;
  width: {layout["block_width"]}px;
  height: {layout["block_height"]}px;
}}
.column {{
  position: absolute;
  top: 0;
  writing-mode: vertical-rl;
  text-orientation: mixed;
  white-space: nowrap;
  font-family: {font_stack};
  font-size: {layout["font_size"]}px;
  font-weight: 500;
  color: #111;
  letter-spacing: 0;
  text-align: start;
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


def render_bubble(
    image_path: Path,
    output_path: Path,
    plan: BubblePlan,
    font_path: str | None,
    font_family: str | None,
    bubble_asset: Path,
    font_size: int,
) -> None:
    browser_root = Path(__file__).resolve().parent / ".playwright-browsers"
    if browser_root.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
    chromium_executable = resolve_chromium_executable()
    image = Image.open(image_path)
    width_px, height_px = image.size
    image.close()
    actual_font_size = font_size or max(26, min(52, height_px // 28))
    layout = compute_layout(width_px, height_px, plan, actual_font_size)
    font_css, font_stack = build_font_css(font_path, font_family)
    html_doc = build_render_html(
        canvas_width=width_px,
        canvas_height=height_px,
        plan=plan,
        layout=layout,
        font_stack=font_stack,
        font_css=font_css,
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
                (layout["bubble_width"], layout["bubble_height"]),
                Image.Resampling.LANCZOS,
            )
        else:
            bubble_page = browser.new_page(
                viewport={"width": layout["bubble_width"], "height": layout["bubble_height"]},
                device_scale_factor=1,
            )
            bubble_svg = load_bubble_svg_source(bubble_asset)
            bubble_page.set_content(
                build_bubble_svg_html(
                    svg_source=bubble_svg,
                    width=layout["bubble_width"],
                    height=layout["bubble_height"],
                ),
                wait_until="load",
            )
            bubble_page.wait_for_load_state("networkidle")
            bubble_page.wait_for_timeout(100)
            bubble_image = Image.open(io.BytesIO(bubble_page.screenshot(omit_background=True))).convert("RGBA").resize(
                (layout["bubble_width"], layout["bubble_height"]),
                Image.Resampling.LANCZOS,
            )
            bubble_page.close()

        base.alpha_composite(bubble_image, (layout["bubble_left"], layout["bubble_top"]))

        page = browser.new_page(viewport={"width": width_px, "height": height_px}, device_scale_factor=1)
        page.set_content(html_doc, wait_until="load")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(150)
        overlay_bytes = page.screenshot(omit_background=True)
        page.close()
        browser.close()

    overlay = Image.open(io.BytesIO(overlay_bytes)).convert("RGBA")
    base.alpha_composite(overlay)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        base.convert("RGB").save(output_path, quality=95)
    else:
        base.save(output_path)


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

    image_data_url = encode_image_as_data_url(image_path)
    prompt = build_user_prompt(args.dialogue)
    response = post_chat_completion(
        server=args.server,
        model=args.model,
        prompt=prompt,
        image_data_url=image_data_url,
        temperature=args.temperature,
    )
    plan = extract_plan(response, args.dialogue)

    if args.plan_json:
        plan_path = Path(args.plan_json)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            json.dumps(
                {
                    "dialogue": args.dialogue,
                    "anchor_x": plan.anchor_x,
                    "anchor_y": plan.anchor_y,
                    "columns": plan.columns,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    render_bubble(
        image_path=image_path,
        output_path=output_path,
        plan=plan,
        font_path=font_path,
        font_family=args.font_family,
        bubble_asset=bubble_asset,
        font_size=args.font_size,
    )

    print(
        json.dumps(
            {
                "dialogue": args.dialogue,
                "anchor_x": plan.anchor_x,
                "anchor_y": plan.anchor_y,
                "columns": plan.columns,
                "output": str(output_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
