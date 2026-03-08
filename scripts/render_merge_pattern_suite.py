#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bubble.assets import pick_font_path, resolve_bubble_asset, resolve_resvg_executable  # noqa: E402
from bubble.layout import compute_bubble_layout, compute_text_layout  # noqa: E402
from bubble.models import BubblePlan, DEFAULT_FONT_DIVISOR, plans_payload, save_plan_json  # noqa: E402
from bubble.render import (  # noqa: E402
    RenderedBubble,
    _group_bubbles_for_merge,
    _resolve_bubble_image,
    render_bubbles,
    render_text_overlay,
)


CANVAS_WIDTH = 900
CANVAS_HEIGHT = 1280


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render synthetic merge test patterns on a blank image.")
    parser.add_argument("--output-dir", default=str(ROOT / "out" / "merge_pattern_suite"), help="Output directory.")
    parser.add_argument("--font", help="Optional font path.")
    parser.add_argument("--bubble-asset", help="Optional bubble asset path.")
    parser.add_argument("--font-size", type=int, default=0, help="Override font size.")
    parser.add_argument("--text-renderer", default="resvg-hybrid", choices=["browser", "resvg-hybrid"])
    parser.add_argument("--bubble-renderer", default="resvg", choices=["browser", "resvg"])
    return parser.parse_args()


def _make_plan(anchor_x: float, anchor_y: float, text: str, speaker_id: str, sentence_id: int) -> BubblePlan:
    return BubblePlan(
        anchor_x=anchor_x,
        anchor_y=anchor_y,
        sentence_ids=[sentence_id],
        columns=[text],
        speaker_id=speaker_id,
    )


def build_cases() -> list[dict[str, object]]:
    return [
        {
            "name": "vertical_pair_same_speaker",
            "description": "same speaker, vertically overlapping pair",
            "expected_groups": [[[1], [2]]],
            "plans": [
                _make_plan(0.82, 0.12, "今日はいいね。", "speaker_a", 1),
                _make_plan(0.74, 0.24, "空がきれいだよ。", "speaker_a", 2),
            ],
        },
        {
            "name": "vertical_pair_diff_speaker",
            "description": "same geometry, different speakers should not merge",
            "expected_groups": [[[1]], [[2]]],
            "plans": [
                _make_plan(0.82, 0.12, "今日はいいね。", "speaker_a", 1),
                _make_plan(0.74, 0.24, "空がきれいだよ。", "speaker_b", 2),
            ],
        },
        {
            "name": "triple_chain_same_speaker",
            "description": "three stacked bubbles from one speaker",
            "expected_groups": [[[1], [2], [3]]],
            "plans": [
                _make_plan(0.24, 0.12, "ねえ見て。", "speaker_a", 1),
                _make_plan(0.18, 0.24, "空がきれい。", "speaker_a", 2),
                _make_plan(0.24, 0.36, "少し歩こう。", "speaker_a", 3),
            ],
        },
        {
            "name": "left_right_far_same_speaker",
            "description": "same speaker but far apart should stay separate",
            "expected_groups": [[[1]], [[2]]],
            "plans": [
                _make_plan(0.20, 0.18, "左側の吹き出しです。", "speaker_a", 1),
                _make_plan(0.82, 0.18, "右側の吹き出しです。", "speaker_a", 2),
            ],
        },
        {
            "name": "two_columns_two_groups",
            "description": "two merged groups split by side",
            "expected_groups": [[[1], [2]], [[3], [4]]],
            "plans": [
                _make_plan(0.24, 0.12, "左上です。", "speaker_a", 1),
                _make_plan(0.18, 0.24, "左下です。", "speaker_a", 2),
                _make_plan(0.84, 0.12, "右上です。", "speaker_a", 3),
                _make_plan(0.78, 0.24, "右下です。", "speaker_a", 4),
            ],
        },
        {
            "name": "diagonal_pair_same_speaker",
            "description": "diagonal nearby pair from same speaker",
            "expected_groups": [[[1], [2]]],
            "plans": [
                _make_plan(0.75, 0.58, "少し寄って話そう。", "speaker_a", 1),
                _make_plan(0.68, 0.68, "うん聞こえてるよ。", "speaker_a", 2),
            ],
        },
        {
            "name": "staircase_quad_same_speaker",
            "description": "four bubbles that merge into a single staircase-shaped chain",
            "expected_groups": [[[1], [2], [3], [4]]],
            "plans": [
                _make_plan(0.28, 0.10, "ひとつ目。", "speaker_a", 1),
                _make_plan(0.21, 0.21, "ふたつ目。", "speaker_a", 2),
                _make_plan(0.28, 0.32, "みっつ目。", "speaker_a", 3),
                _make_plan(0.21, 0.43, "よっつ目。", "speaker_a", 4),
            ],
        },
        {
            "name": "triple_chain_middle_split",
            "description": "three stacked bubbles where middle speaker breaks the chain",
            "expected_groups": [[[1]], [[2]], [[3]]],
            "plans": [
                _make_plan(0.72, 0.12, "上です。", "speaker_a", 1),
                _make_plan(0.66, 0.24, "中央です。", "speaker_b", 2),
                _make_plan(0.72, 0.36, "下です。", "speaker_a", 3),
            ],
        },
    ]


def create_base_image(path: Path, width: int, height: int) -> None:
    image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 100):
        color = (235, 235, 235, 255) if x % 200 else (225, 225, 225, 255)
        draw.line([(x, 0), (x, height)], fill=color, width=1)
    for y in range(0, height, 100):
        color = (235, 235, 235, 255) if y % 200 else (225, 225, 225, 255)
        draw.line([(0, y), (width, y)], fill=color, width=1)
    image.save(path)


def compute_group_summary(
    *,
    image_path: Path,
    plans: list[BubblePlan],
    font_path: str | None,
    bubble_asset: Path,
    font_size: int,
    text_renderer: str,
    bubble_renderer: str,
) -> dict[str, object]:
    base = Image.open(image_path).convert("RGBA")
    width_px, height_px = base.size
    actual_font_size = font_size or max(22, min(48, height_px // DEFAULT_FONT_DIVISOR))
    resvg_executable = resolve_resvg_executable() if (bubble_renderer == "resvg" or text_renderer == "resvg-hybrid") else None
    bubble_cache: dict[tuple[str, str, int, int], Image.Image] = {}
    rendered: list[RenderedBubble] = []

    for plan in plans:
        text_layout = compute_text_layout(width_px, height_px, plan, actual_font_size)
        text_overlay = render_text_overlay(
            renderer=text_renderer,
            browser=None,
            canvas_width=width_px,
            canvas_height=height_px,
            plan=plan,
            text_layout=text_layout,
            font_path=font_path,
            font_family=None,
            resvg_executable=resvg_executable,
            text_letter_spacing="-1px",
            text_word_spacing="0",
            resvg_tu_override=True,
        )
        bubble_layout = compute_bubble_layout(
            canvas_width=width_px,
            canvas_height=height_px,
            text_bbox=text_overlay.alpha_bbox,
            text_layout=text_layout,
            font_size=actual_font_size,
            outline_width=text_layout["outline_width"],
        )
        bubble_image = _resolve_bubble_image(
            bubble_renderer=bubble_renderer,
            bubble_asset=bubble_asset,
            bubble_width=bubble_layout["bubble_width"],
            bubble_height=bubble_layout["bubble_height"],
            browser=None,
            resvg_executable=resvg_executable,
            cache=bubble_cache,
        )
        rendered.append(
            RenderedBubble(
                plan=plan,
                text_overlay=text_overlay,
                bubble_layout=bubble_layout,
                bubble_image=bubble_image,
            )
        )

    groups = _group_bubbles_for_merge(rendered)
    return {
        "group_count": len(groups),
        "groups": [[item.plan.sentence_ids for item in group] for group in groups],
        "speaker_ids": [plan.speaker_id for plan in plans],
        "anchors": [
            {
                "sentence_ids": plan.sentence_ids,
                "anchor_x": plan.anchor_x,
                "anchor_y": plan.anchor_y,
            }
            for plan in plans
        ],
    }


def build_contact_sheet(output_dir: Path, cases: list[dict[str, object]]) -> None:
    images: list[tuple[str, Image.Image]] = []
    for case in cases:
        image_path = output_dir / str(case["name"]) / "render.png"
        if image_path.exists():
            images.append((str(case["name"]), Image.open(image_path).convert("RGBA")))
    if not images:
        return

    thumb_width = 320
    thumb_height = 455
    cols = 2
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * thumb_width, rows * (thumb_height + 24)), (250, 250, 250, 255))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (name, image) in enumerate(images):
        row = index // cols
        col = index % cols
        x = col * thumb_width
        y = row * (thumb_height + 24)
        fitted = image.copy()
        fitted.thumbnail((thumb_width, thumb_height))
        paste_x = x + (thumb_width - fitted.width) // 2
        sheet.paste(fitted, (paste_x, y))
        draw.text((x + 8, y + thumb_height + 4), name, fill=(0, 0, 0, 255), font=font)
    sheet.save(output_dir / "contact_sheet.png")


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    font_path = pick_font_path(args.font)
    bubble_asset = resolve_bubble_asset(args.bubble_asset)
    if bubble_asset is None:
        raise RuntimeError(f"bubble asset not found: {args.bubble_asset}")

    base_image_path = output_dir / "base.png"
    create_base_image(base_image_path, CANVAS_WIDTH, CANVAS_HEIGHT)

    cases = build_cases()
    summary_cases: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for case in cases:
        case_name = str(case["name"])
        case_dir = output_dir / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        plans = list(case["plans"])  # shallow copy for typing
        dialogue_lines = ["".join(plan.columns) for plan in plans]
        save_plan_json(case_dir / "plan.json", dialogue_lines, plans)
        render_bubbles(
            image_path=base_image_path,
            output_path=case_dir / "render.png",
            plans=plans,
            font_path=font_path,
            font_family=None,
            bubble_asset=bubble_asset,
            font_size=args.font_size,
            text_renderer=args.text_renderer,
            bubble_renderer=args.bubble_renderer,
            text_letter_spacing="-1px",
            text_word_spacing="0",
            resvg_tu_override=True,
        )
        merge_summary = compute_group_summary(
            image_path=base_image_path,
            plans=plans,
            font_path=font_path,
            bubble_asset=bubble_asset,
            font_size=args.font_size,
            text_renderer=args.text_renderer,
            bubble_renderer=args.bubble_renderer,
        )
        expected_groups = case.get("expected_groups")
        case_summary = {
            "name": case_name,
            "description": case["description"],
            "plan": plans_payload(dialogue_lines, plans),
            "expected_groups": expected_groups,
            "merge_summary": merge_summary,
            "matches_expected": expected_groups == merge_summary["groups"],
        }
        summary_cases.append(case_summary)
        if not case_summary["matches_expected"]:
            failures.append(
                {
                    "name": case_name,
                    "expected_groups": expected_groups,
                    "actual_groups": merge_summary["groups"],
                }
            )

    summary = {
        "base_image": str(base_image_path),
        "output_dir": str(output_dir),
        "count": len(summary_cases),
        "cases": summary_cases,
        "failures": failures,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    build_contact_sheet(output_dir, summary_cases)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
