#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bubble.assets import pick_font_path  # noqa: E402
from bubble.models import BubblePlan  # noqa: E402
from bubble.render import render_bubbles  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render one shout_rect bubble without debug overlay.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bubble-type", default="shout_rect_pointed_drop")
    parser.add_argument("--anchor-x", required=True, type=float)
    parser.add_argument("--anchor-y", required=True, type=float)
    parser.add_argument("--column", action="append", dest="columns", required=True)
    parser.add_argument("--font", help="Optional font path")
    parser.add_argument("--font-family", help="Optional font family override")
    parser.add_argument("--font-size", type=int, default=22)
    parser.add_argument("--sentence-id", type=int, default=1)
    parser.add_argument("--speaker-id", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    font_path = pick_font_path(args.font)
    plan = BubblePlan(
        anchor_x=args.anchor_x,
        anchor_y=args.anchor_y,
        sentence_ids=[args.sentence_id],
        columns=list(args.columns),
        speaker_id=args.speaker_id,
        bubble_type=args.bubble_type,
    )
    render_bubbles(
        image_path=Path(args.input),
        output_path=Path(args.output),
        plans=[plan],
        font_path=font_path,
        font_family=args.font_family,
        bubble_asset_override=None,
        font_size=args.font_size,
        text_renderer="resvg-hybrid",
        bubble_renderer="resvg",
        text_letter_spacing="-1px",
        text_word_spacing="0",
        resvg_tu_override=True,
    )
    print(Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
