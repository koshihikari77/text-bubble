from __future__ import annotations

import math
import re
import unittest
import xml.etree.ElementTree as ET

from bubble.assets import resolve_bubble_renderable_asset
from bubble.layout import compute_bubble_layout
from bubble.procedural_bubbles import _build_direct_shout_rect_geometry, generate_procedural_bubble_svg
from bubble.render import _bubble_cache_key


_COMMAND_RE = re.compile(r"([A-Z])|(-?\d+(?:\.\d+)?)")


def _path_endpoints(svg_source: str) -> list[tuple[float, float]]:
    root = ET.fromstring(svg_source)
    path = root.find("{http://www.w3.org/2000/svg}path")
    if path is None:
        raise AssertionError("expected a path element in procedural bubble svg")
    d = path.attrib["d"]
    tokens = _COMMAND_RE.findall(d)
    endpoints: list[tuple[float, float]] = []
    current_command = ""
    values: list[float] = []
    for command, number in tokens:
        if command:
            current_command = command
            values = []
            continue
        values.append(float(number))
        if current_command == "M" and len(values) == 2:
            endpoints.append((values[0], values[1]))
            values = []
        elif current_command == "Q" and len(values) == 4:
            endpoints.append((values[2], values[3]))
            values = []
    return endpoints


def _assert_outside_text_box(
    *,
    test_case: unittest.TestCase,
    points: list[tuple[float, float]],
    text_left: float,
    text_top: float,
    text_right: float,
    text_bottom: float,
) -> None:
    for x, y in points:
        inside_x = text_left < x < text_right
        inside_y = text_top < y < text_bottom
        test_case.assertFalse(
            inside_x and inside_y,
            msg=f"point unexpectedly entered text box: ({x:.3f}, {y:.3f})",
        )


class ProceduralBubbleTests(unittest.TestCase):
    def test_direct_shout_rect_points_stay_outside_text_box(self) -> None:
        params = {
            "bubble_width": 240,
            "bubble_height": 360,
            "padding_left": 20,
            "padding_right": 20,
            "padding_top": 20,
            "padding_bottom": 20,
            "text_left": 46,
            "text_top": 34,
            "text_right": 198,
            "text_bottom": 320,
            "midpoint_count_min": 2,
            "midpoint_count_max": 2,
            "midpoint_tangent_jitter": 18,
            "midpoint_depth_jitter": 10,
            "corner_tangent_jitter": 14,
            "corner_inward_jitter": 12,
            "bottom_midpoint_vertex_bias": 0.45,
            "seed": 7,
        }
        text_left = float(params["text_left"])
        text_top = float(params["text_top"])
        text_right = float(params["text_right"])
        text_bottom = float(params["text_bottom"])

        for generator in (
            "pointed_rect_panel",
            "pointed_rect_drop_panel",
            "pointed_rect_kink_panel",
        ):
            svg_source = generate_procedural_bubble_svg(generator, params)
            endpoints = _path_endpoints(svg_source)
            self.assertGreaterEqual(len(endpoints), 9)
            _assert_outside_text_box(
                test_case=self,
                points=endpoints,
                text_left=text_left,
                text_top=text_top,
                text_right=text_right,
                text_bottom=text_bottom,
            )

    def test_direct_shout_rect_geometry_keeps_corners_midpoints_and_controls_outside_text_box(self) -> None:
        params = {
            "bubble_width": 240,
            "bubble_height": 360,
            "text_left": 46,
            "text_top": 34,
            "text_right": 198,
            "text_bottom": 320,
            "midpoint_count_min": 2,
            "midpoint_count_max": 2,
            "midpoint_tangent_jitter": 18,
            "midpoint_depth_jitter": 10,
            "corner_tangent_jitter": 14,
            "corner_inward_jitter": 12,
            "bottom_midpoint_vertex_bias": 0.45,
            "seed": 7,
        }
        text_left = float(params["text_left"])
        text_top = float(params["text_top"])
        text_right = float(params["text_right"])
        text_bottom = float(params["text_bottom"])

        geometry = _build_direct_shout_rect_geometry(params, curve_style="kinked")
        corners = geometry["corners"]
        self.assertEqual(len(corners), 4)
        self.assertLessEqual(corners[0][1], text_top)
        self.assertLessEqual(corners[1][1], text_top)
        self.assertGreaterEqual(corners[2][1], text_bottom)
        self.assertGreaterEqual(corners[3][1], text_bottom)

        for edge in geometry["edges"]:
            _assert_outside_text_box(
                test_case=self,
                points=list(edge["midpoints"]) + list(edge["controls"]),
                text_left=text_left,
                text_top=text_top,
                text_right=text_right,
                text_bottom=text_bottom,
            )

    def test_kink_variant_seed_changes_source_key(self) -> None:
        first = resolve_bubble_renderable_asset(None, "shout_rect_pointed_kink", variant_seed=11)
        second = resolve_bubble_renderable_asset(None, "shout_rect_pointed_kink", variant_seed=19)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None
        assert second is not None
        self.assertNotEqual(first.source_key, second.source_key)

    def test_builtin_default_like_assets_resolve_as_procedural(self) -> None:
        for bubble_type in ("ellipse", "square", "narration", "wavy", "shout"):
            asset = resolve_bubble_renderable_asset(None, bubble_type, variant_seed=11)
            self.assertIsNotNone(asset)
            assert asset is not None
            self.assertEqual(asset.source_kind, "procedural")
            self.assertIsNotNone(asset.generator)

    def test_shout_rect_cache_key_depends_on_shape_layout(self) -> None:
        asset = resolve_bubble_renderable_asset(None, "shout_rect_pointed_drop", variant_seed=11)
        self.assertIsNotNone(asset)
        assert asset is not None

        first = _bubble_cache_key(
            bubble_renderer="resvg",
            bubble_asset=asset,
            width=220,
            height=340,
            bubble_layout={"bubble_width": 220, "bubble_height": 340, "shape_layout": {"corners": [(1, 2)]}},
            local_text_bbox=(40, 30, 180, 300),
        )
        second = _bubble_cache_key(
            bubble_renderer="resvg",
            bubble_asset=asset,
            width=220,
            height=340,
            bubble_layout={"bubble_width": 220, "bubble_height": 340, "shape_layout": {"corners": [(2, 3)]}},
            local_text_bbox=(36, 30, 176, 300),
        )

        self.assertNotEqual(first, second)

    def test_compute_bubble_layout_emits_shout_rect_shape_layout(self) -> None:
        layout = compute_bubble_layout(
            canvas_width=400,
            canvas_height=500,
            text_bbox=(120, 80, 240, 300),
            text_layout={"outline_width": 3},
            font_size=22,
            outline_width=3,
            bubble_type="shout_rect_pointed_drop",
            variant_seed=7,
            bubble_params={
                "pull": 0.78,
                "bow": 10,
                "side_bow": 10,
                "midpoint_count_min": 2,
                "midpoint_count_max": 2,
                "midpoint_tangent_jitter": 18,
                "midpoint_depth_jitter": 10,
                "corner_tangent_jitter": 8,
                "corner_inward_jitter": 6,
                "bottom_midpoint_vertex_bias": 0.5,
            },
            safe_padding={"left": 1.3, "right": 1.3, "top": 0.8, "bottom": 0.8},
        )

        self.assertIn("shape_layout", layout)
        shape_layout = layout["shape_layout"]
        self.assertEqual(shape_layout["kind"], "shout_rect")
        self.assertEqual(shape_layout["bubble_type"], "shout_rect_pointed_drop")
        self.assertEqual(len(shape_layout["corners"]), 4)
        self.assertEqual(len(shape_layout["edges"]), 4)
        self.assertEqual(
            shape_layout["keepout_bounds"],
            [
                float(layout["frame_padding_left"]),
                float(layout["frame_padding_top"]),
                float(layout["frame_padding_left"] + layout["inner_bubble_width"]),
                float(layout["frame_padding_top"] + layout["inner_bubble_height"]),
            ],
        )
        self.assertLess(layout["bubble_left"], layout["inner_bubble_left"])
        self.assertLess(layout["bubble_top"], layout["inner_bubble_top"])
        self.assertGreater(layout["bubble_right"], layout["inner_bubble_right"])
        self.assertGreater(layout["bubble_bottom"], layout["inner_bubble_bottom"])

    def test_compute_bubble_layout_emits_generic_shape_layout_for_ellipse(self) -> None:
        layout = compute_bubble_layout(
            canvas_width=400,
            canvas_height=500,
            text_bbox=(120, 80, 240, 300),
            text_layout={"outline_width": 3},
            font_size=22,
            outline_width=3,
            bubble_type="ellipse",
            variant_seed=7,
            bubble_params=None,
        )

        self.assertIn("shape_layout", layout)
        shape_layout = layout["shape_layout"]
        self.assertEqual(shape_layout["kind"], "ellipse")
        self.assertEqual(shape_layout["bubble_type"], "ellipse")
        self.assertEqual(
            shape_layout["view_box"],
            [0.0, 0.0, float(layout["bubble_width"]), float(layout["bubble_height"])],
        )
        self.assertIn("path_d", shape_layout)

    def test_compute_bubble_layout_emits_wavy_shape_layout_with_font_based_outer_frame(self) -> None:
        params = {
            "view_box": [0, 0, 360, 600],
            "center_x": 180,
            "center_y": 300,
            "radius_x": 95,
            "radius_y": 210,
            "samples": 64,
            "amp": 0.4,
            "freq": 7,
            "seed": 9,
            "phase": 0.4,
            "asymmetry": 0.15,
            "radial_blend": 0.0,
        }
        layout = compute_bubble_layout(
            canvas_width=400,
            canvas_height=500,
            text_bbox=(120, 80, 240, 300),
            text_layout={"outline_width": 3},
            font_size=22,
            outline_width=3,
            bubble_type="wavy",
            variant_seed=7,
            bubble_params=params,
        )

        shape_layout = layout["shape_layout"]
        self.assertEqual(shape_layout["kind"], "directional_hill")
        self.assertIn("path_d", shape_layout)
        self.assertLess(layout["bubble_left"], layout["inner_bubble_left"])
        self.assertLess(layout["bubble_top"], layout["inner_bubble_top"])
        self.assertGreater(layout["bubble_right"], layout["inner_bubble_right"])
        self.assertGreater(layout["bubble_bottom"], layout["inner_bubble_bottom"])
        self.assertEqual(
            shape_layout["bubble_box_bounds"],
            [
                float(layout["frame_padding_left"]),
                float(layout["frame_padding_top"]),
                float(layout["frame_padding_left"] + layout["inner_bubble_width"]),
                float(layout["frame_padding_top"] + layout["inner_bubble_height"]),
            ],
        )

    def test_wavy_shape_layout_phase_and_points_change_with_seed(self) -> None:
        params = {
            "view_box": [0, 0, 360, 600],
            "center_x": 180,
            "center_y": 300,
            "radius_x": 95,
            "radius_y": 210,
            "samples": 64,
            "amp": 0.4,
            "freq": 7,
            "seed": 9,
            "phase": 0.4,
            "asymmetry": 0.15,
            "radial_blend": 0.0,
        }
        first = compute_bubble_layout(
            canvas_width=400,
            canvas_height=500,
            text_bbox=(120, 80, 240, 300),
            text_layout={"outline_width": 3},
            font_size=22,
            outline_width=3,
            bubble_type="wavy",
            variant_seed=7,
            bubble_params=params,
        )["shape_layout"]
        second = compute_bubble_layout(
            canvas_width=400,
            canvas_height=500,
            text_bbox=(120, 80, 240, 300),
            text_layout={"outline_width": 3},
            font_size=22,
            outline_width=3,
            bubble_type="wavy",
            variant_seed=19,
            bubble_params=params,
        )["shape_layout"]

        self.assertNotEqual(first["phase_shift"], second["phase_shift"])
        self.assertNotEqual(first["points"][0], second["points"][0])

    def test_compute_bubble_layout_does_not_force_top_midpoint_count_from_variant(self) -> None:
        layout = compute_bubble_layout(
            canvas_width=400,
            canvas_height=500,
            text_bbox=(120, 80, 240, 300),
            text_layout={"outline_width": 3},
            font_size=22,
            outline_width=3,
            bubble_type="shout_rect_pointed_kink",
            variant_seed=7,
            bubble_params={
                "pull": 0.78,
                "bow": 10,
                "side_bow": 10,
                "midpoint_count_min": 2,
                "midpoint_count_max": 2,
                "midpoint_tangent_jitter": 18,
                "midpoint_depth_jitter": 10,
                "corner_tangent_jitter": 8,
                "corner_inward_jitter": 6,
                "bottom_midpoint_vertex_bias": 0.5,
            },
            safe_padding={"left": 1.3, "right": 1.3, "top": 0.8, "bottom": 0.8},
        )

        shape_layout = layout["shape_layout"]
        self.assertEqual(shape_layout["curve_style"], "kinked")
        top_edge = next(edge for edge in shape_layout["edges"] if edge["edge"] == "top")
        self.assertEqual(len(top_edge["midpoints"]), 2)
        self.assertEqual(len(top_edge["controls"]), 3)

    def test_compute_bubble_layout_kink_controls_bias_toward_midpoint_endpoints(self) -> None:
        layout = compute_bubble_layout(
            canvas_width=400,
            canvas_height=500,
            text_bbox=(120, 80, 240, 300),
            text_layout={"outline_width": 3},
            font_size=22,
            outline_width=3,
            bubble_type="shout_rect_pointed_kink",
            variant_seed=7,
            bubble_params={
                "pull": 0.78,
                "bow": 10,
                "side_bow": 10,
                "midpoint_count_min": 1,
                "midpoint_count_max": 1,
                "midpoint_tangent_jitter": 0,
                "midpoint_depth_jitter": 0,
                "corner_tangent_jitter": 0,
                "corner_inward_jitter": 0,
                "bottom_midpoint_vertex_bias": 0.5,
            },
            safe_padding={"left": 1.3, "right": 1.3, "top": 0.8, "bottom": 0.8},
        )

        shape_layout = layout["shape_layout"]
        top_edge = next(edge for edge in shape_layout["edges"] if edge["edge"] == "top")
        self.assertEqual(len(top_edge["midpoints"]), 1)
        self.assertEqual(len(top_edge["controls"]), 2)

        midpoint = top_edge["midpoints"][0]
        left_corner, right_corner = shape_layout["corners"][:2]
        left_control, right_control = top_edge["controls"]

        self.assertGreater(left_control[0], midpoint[0])
        self.assertGreater(right_control[0], midpoint[0])
        self.assertLess(left_control[0], right_control[0])

        left_cross = (midpoint[0] - left_corner[0]) * (left_control[1] - left_corner[1]) - (
            midpoint[1] - left_corner[1]
        ) * (left_control[0] - left_corner[0])
        right_cross = (right_corner[0] - midpoint[0]) * (right_control[1] - midpoint[1]) - (
            right_corner[1] - midpoint[1]
        ) * (right_control[0] - midpoint[0])

        self.assertGreater(abs(left_cross), 1.0)
        self.assertGreater(abs(right_cross), 1.0)
        tangent_in = (midpoint[0] - left_control[0], midpoint[1] - left_control[1])
        tangent_out = (right_control[0] - midpoint[0], right_control[1] - midpoint[1])
        angle_in = math.degrees(math.atan2(tangent_in[1], tangent_in[0]))
        angle_out = math.degrees(math.atan2(tangent_out[1], tangent_out[0]))
        turn = angle_out - angle_in
        while turn <= -180.0:
            turn += 360.0
        while turn > 180.0:
            turn -= 360.0
        self.assertGreater(abs(turn), 1.0)

    def test_compute_bubble_layout_midpoints_keep_top_and_bottom_within_outer_thirty_percent(self) -> None:
        layout = compute_bubble_layout(
            canvas_width=400,
            canvas_height=500,
            text_bbox=(120, 80, 240, 300),
            text_layout={"outline_width": 3},
            font_size=22,
            outline_width=3,
            bubble_type="shout_rect_pointed_kink",
            variant_seed=7,
            bubble_params={
                "pull": 0.78,
                "bow": 10,
                "side_bow": 10,
                "midpoint_count_min": 2,
                "midpoint_count_max": 2,
                "midpoint_tangent_jitter": 18,
                "midpoint_depth_jitter": 10,
                "corner_tangent_jitter": 8,
                "corner_inward_jitter": 6,
                "bottom_midpoint_vertex_bias": 0.5,
            },
            safe_padding={"left": 1.0, "right": 1.0, "top": 0.8, "bottom": 0.8},
        )

        shape_layout = layout["shape_layout"]
        frame = shape_layout["frame"]
        bubble_box_left, bubble_box_top, bubble_box_right, bubble_box_bottom = shape_layout["bubble_box_bounds"]

        top_limit = frame["top"] + (bubble_box_top - frame["top"]) * 0.3
        bottom_limit = frame["bottom"] - (frame["bottom"] - bubble_box_bottom) * 0.3
        left_limit = (frame["left"] + bubble_box_left) * 0.5
        right_limit = (frame["right"] + bubble_box_right) * 0.5

        top_edge = next(edge for edge in shape_layout["edges"] if edge["edge"] == "top")
        bottom_edge = next(edge for edge in shape_layout["edges"] if edge["edge"] == "bottom")
        left_edge = next(edge for edge in shape_layout["edges"] if edge["edge"] == "left")
        right_edge = next(edge for edge in shape_layout["edges"] if edge["edge"] == "right")

        for point in top_edge["midpoints"]:
            self.assertLessEqual(point[1], top_limit)
        for point in bottom_edge["midpoints"]:
            self.assertGreaterEqual(point[1], bottom_limit)
        for point in left_edge["midpoints"]:
            self.assertLessEqual(point[0], left_limit)
        for point in right_edge["midpoints"]:
            self.assertGreaterEqual(point[0], right_limit)


if __name__ == "__main__":
    unittest.main()
