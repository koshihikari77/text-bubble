from __future__ import annotations

import unittest

from bubble.layout import build_text_metrics, compute_text_layout
from bubble.models import BubblePlan


class LayoutMetricsTests(unittest.TestCase):
    def test_build_text_metrics_reduces_vertical_height_for_japanese_columns(self) -> None:
        columns = ["ふふっ、近くで見ると", "意外と素直なんだね。"]
        metrics = build_text_metrics(30, columns)

        naive_char_step = max(24, int(round(max(30, 24) * 1.08)))
        naive_block_height = naive_char_step * max(len(column) for column in columns)

        self.assertLess(metrics["block_height"], naive_block_height)
        self.assertGreater(metrics["block_height"], 0)
        self.assertGreater(metrics["column_width"], 0)

    def test_compute_text_layout_accepts_metric_options(self) -> None:
        plan = BubblePlan(
            anchor_x=0.88,
            anchor_y=0.10,
            sentence_ids=[1],
            columns=["夜見のどこ", "みてるのー？"],
        )

        layout = compute_text_layout(
            896,
            1152,
            plan,
            30,
            letter_spacing_px=-1.0,
            resvg_tu_override=True,
        )

        self.assertGreater(layout["block_width"], 0)
        self.assertGreater(layout["block_height"], 0)
        self.assertGreaterEqual(layout["text_left"], 0)
        self.assertGreaterEqual(layout["text_top"], 0)
        self.assertLessEqual(layout["text_right"], 896)
        self.assertLessEqual(layout["text_bottom"], 1152)


if __name__ == "__main__":
    unittest.main()
