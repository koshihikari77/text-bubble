#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bubble.assets import pick_font_path  # noqa: E402
from bubble.models import BubblePlan  # noqa: E402
from bubble.render import render_bubbles  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Re-render shout_rect_test outputs from summary.json")
    parser.add_argument("--summary", required=True, help="Path to shout_rect_test summary.json")
    parser.add_argument("--image-root", required=True, help="Directory containing pixiv/raw source images")
    parser.add_argument("--font", help="Optional font path")
    parser.add_argument("--font-family", help="Optional font family")
    parser.add_argument("--font-size", type=int, default=22)
    parser.add_argument("--text-renderer", default="resvg-hybrid", choices=["browser", "resvg-hybrid"])
    parser.add_argument("--bubble-renderer", default="resvg", choices=["browser", "resvg"])
    parser.add_argument("--text-letter-spacing", default="-1px")
    parser.add_argument("--text-word-spacing", default="0")
    parser.add_argument("--resvg-tu-override", action="store_true", default=True)
    return parser.parse_args()


def _resolve_source_image(image_root: Path, source_image: str) -> Path:
    candidates = [
        image_root / "raw" / f"{source_image}.jpg",
        image_root / "raw" / f"{source_image}.png",
        image_root / "pixiv" / f"{source_image}.jpg",
        image_root / "pixiv" / f"{source_image}.png",
        image_root / f"{source_image}.jpg",
        image_root / f"{source_image}.png",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"source image not found for {source_image}")


def main() -> int:
    args = parse_args()
    summary_path = Path(args.summary)
    image_root = Path(args.image_root)
    if not summary_path.exists():
        raise SystemExit(f"summary not found: {summary_path}")
    if not image_root.exists():
        raise SystemExit(f"image root not found: {image_root}")

    font_path = pick_font_path(args.font)
    items = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise SystemExit("summary.json must be a list")

    results: list[dict[str, object]] = []
    for index, item in enumerate(items, start=1):
        source_image = str(item["source_image"])
        output_path = Path(str(item["output"]))
        input_path = _resolve_source_image(image_root, source_image)
        bubble_index = int(item.get("bubble_index_in_source", index))
        plan = BubblePlan(
            anchor_x=float(item["anchor_x"]),
            anchor_y=float(item["anchor_y"]),
            sentence_ids=[bubble_index],
            columns=[str(column) for column in item["columns"]],
            speaker_id="",
            bubble_type=str(item.get("bubble_type") or "shout_rect_pointed_drop"),
        )
        render_bubbles(
            image_path=input_path,
            output_path=output_path,
            plans=[plan],
            font_path=font_path,
            font_family=args.font_family,
            bubble_asset_override=None,
            font_size=args.font_size,
            text_renderer=args.text_renderer,
            bubble_renderer=args.bubble_renderer,
            text_letter_spacing=args.text_letter_spacing,
            text_word_spacing=args.text_word_spacing,
            resvg_tu_override=args.resvg_tu_override,
        )
        results.append(
            {
                "output": str(output_path),
                "input": str(input_path),
                "source_image": source_image,
                "bubble_index_in_source": bubble_index,
            }
        )
        print(f"[{index}/{len(items)}] {output_path}")

    print(json.dumps({"count": len(results), "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
