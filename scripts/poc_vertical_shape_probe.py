#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bubble.assets import pick_font_path  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe HarfBuzz shaping for vertical Japanese text.")
    parser.add_argument("--text", required=True, help="Input text to shape.")
    parser.add_argument("--font", help="Font path. Defaults to project fallback font.")
    parser.add_argument("--direction", default="ttb", help="HarfBuzz direction (default: ttb).")
    parser.add_argument("--script", default="Jpan", help="Script tag (default: Jpan).")
    parser.add_argument("--language", default="ja", help="Language tag (default: ja).")
    parser.add_argument("--features", default="vert=1,vrt2=1", help="OpenType features, comma separated.")
    parser.add_argument("--output", help="Write JSON payload to this file.")
    return parser.parse_args()


def _parse_features(raw: str) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        if "=" in token:
            key, value = token.split("=", 1)
            parsed[key.strip()] = int(value.strip())
        else:
            parsed[token] = 1
    return parsed


def _glyph_name(font: Any, codepoint: int) -> str | None:
    try:
        return font.get_glyph_name(codepoint)
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    args = parse_args()

    try:
        import uharfbuzz as hb
    except ModuleNotFoundError:  # pragma: no cover - runtime dependent
        print("uharfbuzz is required for this probe. Install it before running this script.", file=sys.stderr)
        return 1

    font_path = pick_font_path(args.font)
    if not font_path:
        print("font not found; pass --font explicitly", file=sys.stderr)
        return 1
    font_file = Path(font_path)
    if not font_file.exists():
        print(f"font not found: {font_file}", file=sys.stderr)
        return 1

    features = _parse_features(args.features)
    font_data = font_file.read_bytes()
    face = hb.Face(font_data)
    font = hb.Font(face)
    upem = int(face.upem)
    font.scale = (upem, upem)

    buffer = hb.Buffer()
    buffer.add_str(args.text)
    buffer.guess_segment_properties()
    buffer.direction = args.direction
    buffer.script = args.script
    buffer.language = args.language

    hb.shape(font, buffer, features)

    glyph_rows = []
    infos = buffer.glyph_infos
    positions = buffer.glyph_positions
    for idx, (info, pos) in enumerate(zip(infos, positions, strict=True)):
        glyph_rows.append(
            {
                "index": idx,
                "glyph_id": int(info.codepoint),
                "glyph_name": _glyph_name(font, info.codepoint),
                "cluster": int(info.cluster),
                "x_advance": int(pos.x_advance),
                "y_advance": int(pos.y_advance),
                "x_offset": int(pos.x_offset),
                "y_offset": int(pos.y_offset),
            }
        )

    payload = {
        "text": args.text,
        "font_path": str(font_file),
        "upem": upem,
        "direction": args.direction,
        "script": args.script,
        "language": args.language,
        "features": features,
        "glyphs": glyph_rows,
    }

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
