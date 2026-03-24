#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBUG_SCRIPT = ROOT / "scripts" / "debug_shout_rect_overlay.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render debug overlays from a per-type summary.json")
    parser.add_argument("--summary", required=True, help="Path to summary.json containing a list of bubble render items")
    parser.add_argument("--output-dir", required=True, help="Directory to write debug JPG/JSON files")
    parser.add_argument("--font-size", type=int, default=22)
    return parser.parse_args()


def _build_output_path(output_dir: Path, item: dict[str, object]) -> Path:
    rendered_output = Path(str(item["output"]))
    stem = rendered_output.stem
    return output_dir / f"{stem}_debug.jpg"


def main() -> int:
    args = parse_args()
    summary_path = Path(args.summary)
    output_dir = Path(args.output_dir)
    if not summary_path.exists():
        raise SystemExit(f"summary not found: {summary_path}")
    if not DEBUG_SCRIPT.exists():
        raise SystemExit(f"debug script not found: {DEBUG_SCRIPT}")

    items = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise SystemExit("summary.json must be a list")

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    for index, item in enumerate(items, start=1):
        input_path = item.get("input")
        if not input_path:
            raise SystemExit(f"summary item missing input: {item}")
        output_path = _build_output_path(output_dir, item)
        output_json = output_path.with_suffix(".json")
        command = [
            sys.executable,
            str(DEBUG_SCRIPT),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--output-json",
            str(output_json),
            "--bubble-type",
            str(item["bubble_type"]),
            "--anchor-x",
            str(item["anchor_x"]),
            "--anchor-y",
            str(item["anchor_y"]),
            "--font-size",
            str(args.font_size),
        ]
        for column in item["columns"]:
            command.extend(["--column", str(column)])
        subprocess.run(command, check=True)
        results.append(
            {
                "output": str(output_path),
                "output_json": str(output_json),
                "input": str(input_path),
                "source_image": item.get("source_image"),
                "bubble_index_in_source": item.get("bubble_index_in_source"),
                "bubble_type": item["bubble_type"],
            }
        )
        print(f"[{index}/{len(items)}] {output_path}")

    (output_dir / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
