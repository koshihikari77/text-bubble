#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bubble_pipeline import infer_bubble_plans, save_plan_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Infer bubble plans only and save them as JSON.")
    parser.add_argument("--input", required=True, help="Input image path")
    parser.add_argument("--plan-json", required=True, help="Output plan JSON path")
    parser.add_argument("--server", default="http://127.0.0.1:8080/v1", help="llama-server base URL")
    parser.add_argument("--model", default="heretic", help="Model alias exposed by llama-server")
    parser.add_argument("--dialogue", required=True, help="Dialogue lines separated by newlines")
    parser.add_argument("--temperature", default=0.0, type=float, help="Sampling temperature")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image_path = Path(args.input)
    plan_path = Path(args.plan_json)
    if not image_path.exists():
        print(f"input image not found: {image_path}", file=sys.stderr)
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

    save_plan_json(plan_path, dialogue_lines, plans)
    print(json.dumps({"plan_json": str(plan_path), "dialogue_lines": dialogue_lines}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
