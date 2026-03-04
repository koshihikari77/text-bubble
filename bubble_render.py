#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bubble_pipeline import (
    compose_bubble_plans,
    load_plan_json,
    load_reflow_plan_json,
    load_scene_plan_json,
    pick_font_path,
    render_bubbles,
    resolve_bubble_asset,
    save_plan_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render bubbles from an existing plan JSON.")
    parser.add_argument("--input", required=True, help="Input image path")
    parser.add_argument("--plan-json", help="Final plan JSON path")
    parser.add_argument("--scene-json", help="Scene plan JSON path")
    parser.add_argument("--reflow-json", help="Reflow plan JSON path")
    parser.add_argument("--save-plan-json", help="Optional path to save the composed final plan JSON")
    parser.add_argument("--output", required=True, help="Output image path")
    parser.add_argument("--font", help="Font path for bubble text")
    parser.add_argument("--font-family", help="CSS font-family override for browser rendering")
    parser.add_argument("--bubble-asset", help="Bubble image asset path")
    parser.add_argument("--font-size", default=0, type=int, help="Override vertical text font size")
    parser.add_argument(
        "--text-renderer",
        choices=["browser", "pango"],
        default="browser",
        help="Backend for vertical text rendering",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image_path = Path(args.input)
    output_path = Path(args.output)
    if not image_path.exists():
        print(f"input image not found: {image_path}", file=sys.stderr)
        return 1
    if args.plan_json:
        plan_path = Path(args.plan_json)
        if not plan_path.exists():
            print(f"plan JSON not found: {plan_path}", file=sys.stderr)
            return 1
    else:
        if not args.scene_json or not args.reflow_json:
            print("either --plan-json or both --scene-json and --reflow-json are required", file=sys.stderr)
            return 1
        scene_path = Path(args.scene_json)
        reflow_path = Path(args.reflow_json)
        if not scene_path.exists():
            print(f"scene plan JSON not found: {scene_path}", file=sys.stderr)
            return 1
        if not reflow_path.exists():
            print(f"reflow plan JSON not found: {reflow_path}", file=sys.stderr)
            return 1

    font_path = pick_font_path(args.font)
    bubble_asset = resolve_bubble_asset(args.bubble_asset)
    if bubble_asset is None:
        print(f"bubble asset not found: {args.bubble_asset}", file=sys.stderr)
        return 1

    try:
        if args.plan_json:
            dialogue_lines, plans = load_plan_json(Path(args.plan_json))
        else:
            scene_dialogue_lines, scene_plans = load_scene_plan_json(Path(args.scene_json))
            reflow_dialogue_lines, reflow_plans = load_reflow_plan_json(Path(args.reflow_json))
            if scene_dialogue_lines != reflow_dialogue_lines:
                raise RuntimeError("scene JSON dialogue_lines do not match reflow JSON dialogue_lines")
            dialogue_lines = scene_dialogue_lines
            plans = compose_bubble_plans(dialogue_lines, scene_plans, reflow_plans)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.save_plan_json:
        save_plan_json(Path(args.save_plan_json), dialogue_lines, plans)

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
    print(json.dumps({"output": str(output_path), "dialogue_lines": dialogue_lines}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
