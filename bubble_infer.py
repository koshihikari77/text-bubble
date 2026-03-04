#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bubble_pipeline import (
    infer_assignment_plans,
    infer_bubble_plans,
    infer_reflow_plans,
    infer_scene_bubble_plans,
    load_assignment_plan_json,
    save_assignment_plan_json,
    save_plan_json,
    save_reflow_plan_json,
    save_scene_plan_json,
    split_dialogue_lines,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Infer bubble plans only and save them as JSON.")
    parser.add_argument("--input", help="Input image path")
    parser.add_argument("--plan-json", required=True, help="Output plan JSON path")
    parser.add_argument("--assignment-json", help="Existing assignment JSON to use as input for --stage reflow")
    parser.add_argument("--server", default="http://127.0.0.1:8080/v1", help="llama-server base URL")
    parser.add_argument("--model", default="heretic", help="Model alias exposed by llama-server")
    parser.add_argument("--dialogue", required=True, help="Dialogue lines separated by newlines")
    parser.add_argument("--temperature", default=0.0, type=float, help="Sampling temperature")
    parser.add_argument(
        "--stage",
        choices=["assignment", "reflow", "full", "scene"],
        default="full",
        help="Stage to run: assignment is deterministic, reflow/full/scene call llama-server",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_path = Path(args.plan_json)
    image_path = Path(args.input) if args.input else None
    if args.stage in {"full", "scene"}:
        if image_path is None:
            print("--input is required for full and scene stages", file=sys.stderr)
            return 1
        if not image_path.exists():
            print(f"input image not found: {image_path}", file=sys.stderr)
            return 1

    try:
        if args.stage == "assignment":
            dialogue_lines, plans = infer_assignment_plans(dialogue=args.dialogue)
        elif args.stage == "reflow":
            assignment_plans = None
            if args.assignment_json:
                assignment_dialogue_lines, assignment_plans = load_assignment_plan_json(Path(args.assignment_json))
                if assignment_dialogue_lines != split_dialogue_lines(args.dialogue):
                    raise RuntimeError("dialogue does not match assignment JSON dialogue_lines")
            dialogue_lines, plans = infer_reflow_plans(
                server=args.server,
                model=args.model,
                dialogue=args.dialogue,
                temperature=args.temperature,
                assignment_plans=assignment_plans,
            )
        elif args.stage == "scene":
            dialogue_lines, plans = infer_scene_bubble_plans(
                image_path=image_path,
                server=args.server,
                model=args.model,
                dialogue=args.dialogue,
                temperature=args.temperature,
            )
        else:
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

    if args.stage == "assignment":
        save_assignment_plan_json(plan_path, dialogue_lines, plans)
    elif args.stage == "reflow":
        save_reflow_plan_json(plan_path, dialogue_lines, plans)
    elif args.stage == "scene":
        save_scene_plan_json(plan_path, dialogue_lines, plans)
    else:
        save_plan_json(plan_path, dialogue_lines, plans)
    print(json.dumps({"plan_json": str(plan_path), "dialogue_lines": dialogue_lines, "stage": args.stage}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
