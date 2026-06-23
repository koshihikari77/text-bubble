"""Unit tests for vertical orientation classification and path placement math.

P1-12 (GPT Pro 推奨): SVG 生成前に bbox × transform の合成結果がセル中心に
一致することを assert する数値テストを置く。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bubble.ucd_vertical_orientation import vertical_orientation_for_codepoint
from bubble.vertical_uax import (
    VERTICAL_ORIENTATION_OVERRIDES,
    classify_text_clusters,
    vertical_orientation_of,
)


class UnicodeVerticalOrientationTests(unittest.TestCase):
    def test_cjk_upright(self) -> None:
        for ch in "愛あいう":
            self.assertEqual(vertical_orientation_of(ch), "U", msg=ch)

    def test_punctuation_tu(self) -> None:
        for ch in "、。，．？！":
            self.assertEqual(vertical_orientation_of(ch), "Tu", msg=ch)

    def test_brackets_and_dash_tr(self) -> None:
        for ch in "「」（）ー〜":
            self.assertEqual(vertical_orientation_of(ch), "Tr", msg=ch)

    def test_hearts_are_upright_per_ucd(self) -> None:
        for ch in "♡♥❤❥❣":
            self.assertEqual(vertical_orientation_of(ch), "U", msg=ch)

    def test_misc_symbols_upright_per_ucd(self) -> None:
        for ch in "♪♫♬★☆●①":
            self.assertEqual(vertical_orientation_of(ch), "U", msg=ch)

    def test_ascii_rotates(self) -> None:
        for ch in "Hi1!":
            self.assertEqual(vertical_orientation_of(ch), "R", msg=ch)

    def test_arrows_rotate_by_default(self) -> None:
        for ch in "→←↑↓":
            self.assertEqual(vertical_orientation_of(ch), "R", msg=ch)

    def test_override_takes_precedence_over_ucd(self) -> None:
        custom = dict(VERTICAL_ORIENTATION_OVERRIDES)
        custom["↑"] = "U"
        with patch.dict(VERTICAL_ORIENTATION_OVERRIDES, custom, clear=True):
            self.assertEqual(vertical_orientation_of("↑"), "U")
        # back to UCD default after the patch exits
        self.assertEqual(vertical_orientation_of("↑"), "R")

    def test_empty_cluster_returns_R(self) -> None:
        self.assertEqual(vertical_orientation_of(""), "R")


class UcdLookupTests(unittest.TestCase):
    def test_known_hearts_are_upright(self) -> None:
        for cp in (0x2661, 0x2665, 0x2764, 0x2765, 0x2763):
            self.assertEqual(vertical_orientation_for_codepoint(cp), "U")

    def test_known_latin_rotates(self) -> None:
        # U+0041 LATIN CAPITAL LETTER A の VO は R。
        self.assertEqual(vertical_orientation_for_codepoint(0x0041), "R")

    def test_pua_default_upright_per_ucd_block_rule(self) -> None:
        # UCD は U+E000..U+F8FF (Private Use Area) を U として明示している。
        self.assertEqual(vertical_orientation_for_codepoint(0xE000), "U")


class ClassifyClusterActionTests(unittest.TestCase):
    def test_hearts_route_to_safe_without_probe(self) -> None:
        decisions = classify_text_clusters("♡", font_path=None, resvg_tu_override=True)
        self.assertEqual(decisions[0].orientation, "U")
        self.assertEqual(decisions[0].action, "safe")

    def test_full_width_punct_with_override_goes_to_manual_upright(self) -> None:
        for ch in "？！":
            decisions = classify_text_clusters(ch, font_path=None, resvg_tu_override=True)
            self.assertEqual(decisions[0].orientation, "Tu")
            self.assertEqual(decisions[0].action, "manual_upright", msg=ch)

    def test_brackets_route_to_manual_sideways_without_probe(self) -> None:
        decisions = classify_text_clusters("「", font_path=None, resvg_tu_override=True)
        self.assertEqual(decisions[0].orientation, "Tr")
        self.assertEqual(decisions[0].action, "manual_sideways")


def _compose_matrix(
    transform_steps: list[tuple[float, float, float, float, float, float]],
) -> tuple[float, float, float, float, float, float]:
    """Compose 2D affine matrices in SVG order (left-applied first)."""

    result = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for step in transform_steps:
        a1, b1, c1, d1, e1, f1 = result
        a2, b2, c2, d2, e2, f2 = step
        result = (
            a1 * a2 + c1 * b2,
            b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2,
            b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1,
            b1 * e2 + d1 * f2 + f1,
        )
    return result


def _apply_matrix(
    matrix: tuple[float, float, float, float, float, float],
    point: tuple[float, float],
) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    x, y = point
    return a * x + c * y + e, b * x + d * y + f


def _bbox_after_transform(
    matrix: tuple[float, float, float, float, float, float],
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bounds
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    points = [_apply_matrix(matrix, p) for p in corners]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


class PathPlacementMathTests(unittest.TestCase):
    """`_cluster_path_element` の transform 合成と等価な計算でセル中心一致を検証する。"""

    def _placement_matrix(
        self,
        *,
        center_x: float,
        center_y: float,
        font_size: int,
        upem: int,
        bounds: tuple[float, float, float, float],
        rotate_90: bool,
    ) -> tuple[float, float, float, float, float, float]:
        scale = font_size / upem
        bbox_cx = (bounds[0] + bounds[2]) / 2.0
        bbox_cy = (bounds[1] + bounds[3]) / 2.0
        steps: list[tuple[float, float, float, float, float, float]] = [
            (1.0, 0.0, 0.0, 1.0, center_x, center_y),  # translate(center)
        ]
        if rotate_90:
            steps.append((0.0, 1.0, -1.0, 0.0, 0.0, 0.0))  # rotate 90 deg cw
        # scale(s, -s) — Font Y-up -> SVG Y-down flip
        steps.append((scale, 0.0, 0.0, -scale, 0.0, 0.0))
        # translate(-bbox_cx, -bbox_cy)
        steps.append((1.0, 0.0, 0.0, 1.0, -bbox_cx, -bbox_cy))
        return _compose_matrix(steps)

    def test_bbox_centered_on_cell_no_rotation(self) -> None:
        # Glyph bbox shifted off origin (typical for non-symmetric outlines).
        bounds = (100.0, 200.0, 900.0, 800.0)
        upem = 1024
        font_size = 48
        center_x, center_y = 350.0, 420.0
        matrix = self._placement_matrix(
            center_x=center_x, center_y=center_y, font_size=font_size,
            upem=upem, bounds=bounds, rotate_90=False,
        )
        x0, y0, x1, y1 = _bbox_after_transform(matrix, bounds)
        self.assertAlmostEqual((x0 + x1) / 2, center_x, places=4)
        self.assertAlmostEqual((y0 + y1) / 2, center_y, places=4)

    def test_bbox_centered_on_cell_with_90_rotation(self) -> None:
        bounds = (100.0, 0.0, 800.0, 1024.0)
        upem = 1024
        font_size = 32
        center_x, center_y = 200.0, 600.0
        matrix = self._placement_matrix(
            center_x=center_x, center_y=center_y, font_size=font_size,
            upem=upem, bounds=bounds, rotate_90=True,
        )
        x0, y0, x1, y1 = _bbox_after_transform(matrix, bounds)
        self.assertAlmostEqual((x0 + x1) / 2, center_x, places=4)
        self.assertAlmostEqual((y0 + y1) / 2, center_y, places=4)

    def test_scale_preserves_aspect_ratio(self) -> None:
        bounds = (0.0, 0.0, 1024.0, 1024.0)
        upem = 1024
        font_size = 64
        matrix = self._placement_matrix(
            center_x=0.0, center_y=0.0, font_size=font_size,
            upem=upem, bounds=bounds, rotate_90=False,
        )
        x0, y0, x1, y1 = _bbox_after_transform(matrix, bounds)
        self.assertAlmostEqual(x1 - x0, font_size, places=4)
        self.assertAlmostEqual(y1 - y0, font_size, places=4)


if __name__ == "__main__":
    unittest.main()
