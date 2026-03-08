#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from bubble.assets import (
    _extract_bubble_path_specs,
    load_bubble_svg_source,
    render_raw_svg_with_resvg,
    resolve_bubble_asset,
    resolve_resvg_executable,
    warp_svg_source_to_aspect,
)

ROOT = Path(__file__).resolve().parent.parent
POC_DIR = ROOT / "scripts" / "pathkit_poc"
NODE_SCRIPT = POC_DIR / "union_paths.js"
OUTPUT_DIR = ROOT / "out" / "pathkit_union_poc"


def _pathkit_matrix(
    *,
    source_matrix: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    vb_x: float,
    vb_y: float,
    scale_x: float,
    scale_y: float,
    bubble_left: int,
    bubble_top: int,
) -> list[float]:
    a = source_matrix[0][0] * scale_x
    b = source_matrix[1][0] * scale_y
    c = source_matrix[0][1] * scale_x
    d = source_matrix[1][1] * scale_y
    e = bubble_left + (source_matrix[0][2] - vb_x) * scale_x
    f = bubble_top + (source_matrix[1][2] - vb_y) * scale_y
    return [a, c, e, b, d, f, 0.0, 0.0, 1.0]


def _build_union_request(*, asset_path: Path, placements: list[dict[str, int]]) -> tuple[dict[str, object], float]:
    asset_svg = load_bubble_svg_source(asset_path)
    request_paths: list[dict[str, object]] = []
    stroke_widths: list[float] = []
    for placement in placements:
        bubble_width = int(placement["width"])
        bubble_height = int(placement["height"])
        bubble_left = int(placement["left"])
        bubble_top = int(placement["top"])
        warped_svg = warp_svg_source_to_aspect(asset_svg, bubble_width / max(1, bubble_height))
        viewbox, path_specs, stroke_width = _extract_bubble_path_specs(warped_svg)
        vb_x, vb_y, vb_w, vb_h = viewbox
        scale_x = bubble_width / max(vb_w, 1e-6)
        scale_y = bubble_height / max(vb_h, 1e-6)
        stroke_widths.append(stroke_width * ((abs(scale_x) + abs(scale_y)) / 2.0))
        for d_value, source_matrix in path_specs:
            request_paths.append(
                {
                    "d": d_value,
                    "matrix": _pathkit_matrix(
                        source_matrix=source_matrix,
                        vb_x=vb_x,
                        vb_y=vb_y,
                        scale_x=scale_x,
                        scale_y=scale_y,
                        bubble_left=bubble_left,
                        bubble_top=bubble_top,
                    ),
                }
            )
    avg_stroke_width = sum(stroke_widths) / max(1, len(stroke_widths))
    return {"paths": request_paths}, avg_stroke_width


def _run_pathkit_union(payload: dict[str, object]) -> dict[str, object]:
    completed = subprocess.run(
        ["node", str(NODE_SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        cwd=str(ROOT),
    )
    return json.loads(completed.stdout)


def _write_case(
    *,
    case_name: str,
    union_result: dict[str, object],
    stroke_width: float,
    resvg_executable: str,
) -> None:
    case_dir = OUTPUT_DIR / case_name
    case_dir.mkdir(parents=True, exist_ok=True)
    bounds = union_result["bounds"]
    padding = max(2.0, stroke_width * 1.5)
    left = math.floor(bounds["left"] - padding)
    top = math.floor(bounds["top"] - padding)
    right = math.ceil(bounds["right"] + padding)
    bottom = math.ceil(bounds["bottom"] + padding)
    width = max(1, right - left)
    height = max(1, bottom - top)
    svg_source = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <path d="{union_result["d"]}"
        transform="translate({-left} {-top})"
        fill="#ffffff"
        fill-opacity="0.88"
        stroke="#111"
        stroke-width="{stroke_width:.3f}"
        stroke-linecap="round"
        stroke-linejoin="round"
        shape-rendering="geometricPrecision" />
</svg>"""
    (case_dir / "union.json").write_text(json.dumps(union_result, ensure_ascii=False, indent=2), encoding="utf-8")
    (case_dir / "merged.svg").write_text(svg_source, encoding="utf-8")
    image = render_raw_svg_with_resvg(
        svg_source=svg_source,
        width=width,
        height=height,
        executable=resvg_executable,
    )
    image.save(case_dir / "merged.png")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    asset_path = resolve_bubble_asset(None)
    if asset_path is None:
        raise RuntimeError("bubble asset not found")
    resvg_executable = resolve_resvg_executable()
    if not resvg_executable:
        raise RuntimeError("resvg not found")

    cases = {
        "vertical_pair": [
            {"left": 180, "top": 80, "width": 120, "height": 220},
            {"left": 150, "top": 190, "width": 120, "height": 220},
        ],
        "staircase_quad": [
            {"left": 120, "top": 70, "width": 120, "height": 220},
            {"left": 90, "top": 180, "width": 120, "height": 220},
            {"left": 120, "top": 290, "width": 120, "height": 220},
            {"left": 90, "top": 400, "width": 120, "height": 220},
        ],
    }

    summary: dict[str, object] = {"output_dir": str(OUTPUT_DIR), "cases": {}}
    for case_name, placements in cases.items():
        payload, stroke_width = _build_union_request(asset_path=asset_path, placements=placements)
        union_result = _run_pathkit_union(payload)
        _write_case(
            case_name=case_name,
            union_result=union_result,
            stroke_width=stroke_width,
            resvg_executable=resvg_executable,
        )
        summary["cases"][case_name] = {
            "placements": placements,
            "stroke_width": stroke_width,
            "bounds": union_result["bounds"],
            "tight_bounds": union_result["tight_bounds"],
        }

    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
