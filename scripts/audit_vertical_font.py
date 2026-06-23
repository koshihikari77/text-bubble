"""Audit a font's capabilities for vertical text rendering.

Prints:
- name records (family, subfamily)
- GSUB / GPOS features
- vmtx / vhea presence
- For a probe set of glyphs: whether outlines exist and whether vert/ttb
  shaping changes the glyph

Usage:
    .venv/bin/python scripts/audit_vertical_font.py [font_path ...]

If no path is given, audits the project default (`pick_font_path(None)`) and
the configured fallback chain.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROBE_CHARS = (
    "あいう愛"          # CJK base (expected: has outline, ttb-shaped)
    "、。「」（）ー…〜"  # vertical-form candidates
    "？！"               # full-width punctuation
    "♡♥❤❥❣"            # hearts (often missing outline in CJK fonts)
    "♪♫★☆●①"          # other symbols
    "Hi1!"              # ASCII
)


def _import_font(path: str):
    from fontTools.ttLib import TTFont

    return TTFont(path, lazy=True)


def _glyph_outline_state(font, codepoint: int) -> str:
    from fontTools.pens.boundsPen import BoundsPen

    cmap = font.getBestCmap()
    name = cmap.get(codepoint)
    if not name:
        return "no-cmap"
    glyph_set = font.getGlyphSet()
    pen = BoundsPen(glyph_set)
    try:
        glyph_set[name].draw(pen)
    except Exception as exc:  # noqa: BLE001
        return f"draw-error:{exc}"
    if pen.bounds is None:
        return "EMPTY"
    return f"glyph={name}"


def _vert_shaping_changes_glyph(font_path: str, cluster: str) -> str:
    try:
        import uharfbuzz as hb
    except ImportError:
        return "uharfbuzz unavailable"

    try:
        data = Path(font_path).read_bytes()
        face = hb.Face(data)
        hb_font = hb.Font(face)
        hb_font.scale = (face.upem, face.upem)
    except Exception as exc:  # noqa: BLE001
        return f"load-error:{exc}"

    def _ids(direction: str) -> tuple[int, ...]:
        buf = hb.Buffer()
        buf.add_str(cluster)
        buf.guess_segment_properties()
        buf.direction = direction
        buf.script = "Jpan"
        buf.language = "ja"
        hb.shape(hb_font, buf, {})
        return tuple(info.codepoint for info in buf.glyph_infos)

    ids_h = _ids("ltr")
    ids_v = _ids("ttb")
    if not ids_h:
        return "empty"
    if ids_h == ids_v:
        return f"same={ids_h}"
    return f"ltr={ids_h} ttb={ids_v}"


def audit(font_path: str) -> None:
    print(f"\n{'=' * 76}")
    print(f"FONT: {font_path}")
    print("=" * 76)

    try:
        font = _import_font(font_path)
    except Exception as exc:  # noqa: BLE001
        print(f"  load failed: {exc}")
        return

    # Name records
    name_table = font.get("name")
    if name_table is not None:
        for record_id in (1, 2, 4, 6):
            try:
                rec = name_table.getDebugName(record_id)
            except Exception:  # noqa: BLE001
                rec = None
            if rec:
                print(f"  name[{record_id}]: {rec}")

    # head / OS/2
    head = font.get("head")
    if head is not None:
        print(f"  upem: {head.unitsPerEm}")

    # Vertical metrics
    has_vmtx = "vmtx" in font
    has_vhea = "vhea" in font
    print(f"  vhea: {has_vhea}  vmtx: {has_vmtx}")

    # GSUB / GPOS features
    for tag in ("GSUB", "GPOS"):
        table = font.get(tag)
        if table is None or table.table is None:
            print(f"  {tag}: absent")
            continue
        feature_list = table.table.FeatureList
        if feature_list is None:
            print(f"  {tag}: (no FeatureList)")
            continue
        counts: dict[str, int] = {}
        for record in feature_list.FeatureRecord:
            counts[record.FeatureTag] = counts.get(record.FeatureTag, 0) + 1
        vertical_tags = {
            "vert", "vrt2", "vrtr", "vchw", "vhal", "vpal", "vkrn",
            "chws", "halt", "palt", "kern",
        }
        sorted_items = sorted(counts.items())
        relevant = [(t, c) for t, c in sorted_items if t in vertical_tags]
        other = [(t, c) for t, c in sorted_items if t not in vertical_tags]
        if relevant:
            print(f"  {tag} (vertical-related):")
            for tag_name, count in relevant:
                print(f"    {tag_name}: {count}")
        if other:
            print(f"  {tag} (other): {', '.join(f'{t}({c})' for t, c in other)}")

    # Glyph probe
    print("  glyph audit:")
    print(f"    {'char':<6} {'cp':<8} {'outline':<26} ttb-shape vs ltr-shape")
    for ch in PROBE_CHARS:
        cp = ord(ch)
        outline = _glyph_outline_state(font, cp)
        shaping = _vert_shaping_changes_glyph(font_path, ch)
        print(f"    {ch!r:<6} U+{cp:04X}  {outline:<26} {shaping}")


def main(argv: list[str]) -> int:
    if argv:
        targets = argv
    else:
        from bubble.assets import pick_fallback_font_paths, pick_font_path

        primary = pick_font_path(None)
        if not primary:
            print("primary font not found via pick_font_path")
            return 1
        fallbacks = pick_fallback_font_paths(primary)
        targets = [primary, *fallbacks]
    for path in targets:
        audit(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
