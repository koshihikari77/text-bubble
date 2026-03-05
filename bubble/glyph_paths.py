from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class ShapedPath:
    d: str
    bounds: tuple[float, float, float, float] | None
    x_advance: float
    y_advance: float
    upem: int


class HarfBuzzGlyphPathRenderer:
    def __init__(self, font_path: str) -> None:
        import uharfbuzz as hb
        from fontTools.pens.boundsPen import BoundsPen
        from fontTools.pens.svgPathPen import SVGPathPen
        from fontTools.pens.transformPen import TransformPen
        from fontTools.ttLib import TTFont

        font_file = Path(font_path)
        if not font_file.exists():
            raise RuntimeError(f"font file not found: {font_path}")

        data = font_file.read_bytes()
        face = hb.Face(data)
        hb_font = hb.Font(face)
        upem = int(face.upem)
        hb_font.scale = (upem, upem)

        tt_font = TTFont(str(font_file), lazy=True)
        glyph_set = tt_font.getGlyphSet()

        self._hb = hb
        self._hb_font = hb_font
        self._tt_font = tt_font
        self._glyph_set = glyph_set
        self._upem = upem
        self._SVGPathPen = SVGPathPen
        self._BoundsPen = BoundsPen
        self._TransformPen = TransformPen

    def shape_path(
        self,
        text: str,
        *,
        direction: str = "ltr",
        script: str = "Jpan",
        language: str = "ja",
        features: dict[str, int] | None = None,
    ) -> ShapedPath:
        feature_key = tuple(sorted((features or {}).items()))
        return self._shape_path_cached(text, direction, script, language, feature_key)

    @lru_cache(maxsize=8192)
    def _shape_path_cached(
        self,
        text: str,
        direction: str,
        script: str,
        language: str,
        feature_key: tuple[tuple[str, int], ...],
    ) -> ShapedPath:
        buffer = self._hb.Buffer()
        buffer.add_str(text)
        buffer.guess_segment_properties()
        buffer.direction = direction
        buffer.script = script
        buffer.language = language

        features = {key: int(value) for key, value in feature_key}
        self._hb.shape(self._hb_font, buffer, features)

        infos = buffer.glyph_infos
        positions = buffer.glyph_positions
        if not infos:
            return ShapedPath(
                d="",
                bounds=None,
                x_advance=0.0,
                y_advance=0.0,
                upem=self._upem,
            )

        svg_pen = self._SVGPathPen(self._glyph_set)
        bounds_pen = self._BoundsPen(self._glyph_set)
        cursor_x = 0
        cursor_y = 0
        for info, pos in zip(infos, positions, strict=True):
            glyph_name = self._tt_font.getGlyphName(int(info.codepoint))
            glyph = self._glyph_set[glyph_name]
            dx = cursor_x + int(pos.x_offset)
            dy = cursor_y + int(pos.y_offset)
            matrix = (1, 0, 0, 1, dx, dy)
            glyph.draw(self._TransformPen(svg_pen, matrix))
            glyph.draw(self._TransformPen(bounds_pen, matrix))
            cursor_x += int(pos.x_advance)
            cursor_y += int(pos.y_advance)
        return ShapedPath(
            d=svg_pen.getCommands(),
            bounds=bounds_pen.bounds,
            x_advance=float(cursor_x),
            y_advance=float(cursor_y),
            upem=self._upem,
        )
