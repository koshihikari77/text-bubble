#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.svgLib.path import parse_path
import pathops

from bubble.assets import (
    _extract_bubble_path_specs,
    load_bubble_svg_source,
    render_raw_svg_with_resvg,
    resolve_bubble_asset,
    resolve_resvg_executable,
    warp_svg_source_to_aspect,
)

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "out" / "union_backend_compare"
PATHKIT_SPAWN_SCRIPT = ROOT / "scripts" / "pathkit_poc" / "union_paths.js"
PATHKIT_SERVER_SCRIPT = ROOT / "scripts" / "pathkit_poc" / "union_server.js"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare PathKit spawn/daemon and skia-pathops for bubble unions.")
    parser.add_argument("--iterations", type=int, default=30, help="Benchmark iterations per backend/case.")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Output directory.")
    return parser.parse_args()


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


def build_union_request(*, asset_path: Path, placements: list[dict[str, int]]) -> tuple[dict[str, object], float]:
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


def run_pathkit_spawn(payload: dict[str, object]) -> dict[str, object]:
    completed = subprocess.run(
        ["node", str(PATHKIT_SPAWN_SCRIPT)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        cwd=str(ROOT),
        check=True,
    )
    return json.loads(completed.stdout)


@dataclass
class PathKitDaemon:
    process: subprocess.Popen[str]

    @classmethod
    def start(cls) -> "PathKitDaemon":
        process = subprocess.Popen(
            ["node", str(PATHKIT_SERVER_SCRIPT)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return cls(process=process)

    def request(self, payload: dict[str, object]) -> dict[str, object]:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("PathKit daemon pipes are not available")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        raw = self.process.stdout.readline()
        if not raw:
            stderr = ""
            if self.process.stderr is not None:
                stderr = self.process.stderr.read().strip()
            raise RuntimeError(f"PathKit daemon exited unexpectedly: {stderr}")
        packet = json.loads(raw)
        if not packet.get("ok"):
            raise RuntimeError(packet.get("error", "unknown daemon error"))
        return packet["result"]

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.write("quit\n")
                self.process.stdin.flush()
            except BrokenPipeError:
                pass
            self.process.stdin.close()
        self.process.wait(timeout=5)


def run_skia_pathops(payload: dict[str, object]) -> dict[str, object]:
    merged: pathops.Path | None = None
    for spec in payload["paths"]:
        current = pathops.Path()
        parse_path(spec["d"], current.getPen())
        matrix = spec["matrix"]
        transformed = current.transform(matrix[0], matrix[3], matrix[1], matrix[4], matrix[2], matrix[5])
        if merged is None:
            merged = transformed
        else:
            merged = pathops.op(merged, transformed, pathops.PathOp.UNION)
    if merged is None:
        raise RuntimeError("no merged path was created")
    pen = SVGPathPen(None)
    merged.draw(pen)
    left, top, right, bottom = merged.bounds
    return {
        "d": pen.getCommands(),
        "bounds": {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
        },
        "tight_bounds": {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
        },
    }


def render_case(
    *,
    output_dir: Path,
    result: dict[str, object],
    stroke_width: float,
    resvg_executable: str,
) -> dict[str, object]:
    bounds = result["bounds"]
    padding = max(2.0, stroke_width * 1.5)
    left = math.floor(float(bounds["left"]) - padding)
    top = math.floor(float(bounds["top"]) - padding)
    right = math.ceil(float(bounds["right"]) + padding)
    bottom = math.ceil(float(bounds["bottom"]) + padding)
    width = max(1, right - left)
    height = max(1, bottom - top)
    svg_source = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <path d="{result["d"]}"
        transform="translate({-left} {-top})"
        fill="#ffffff"
        fill-opacity="0.88"
        stroke="#111"
        stroke-width="{stroke_width:.3f}"
        stroke-linecap="round"
        stroke-linejoin="round" />
</svg>"""
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "merged.svg").write_text(svg_source, encoding="utf-8")
    (output_dir / "union.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    image = render_raw_svg_with_resvg(
        svg_source=svg_source,
        width=width,
        height=height,
        executable=resvg_executable,
    )
    image.save(output_dir / "merged.png")
    return {"left": left, "top": top, "width": width, "height": height}


def benchmark_case(fn, payload: dict[str, object], iterations: int) -> dict[str, float]:
    fn(payload)
    started = time.perf_counter()
    for _ in range(iterations):
        fn(payload)
    elapsed = time.perf_counter() - started
    return {
        "iterations": iterations,
        "total_sec": elapsed,
        "avg_ms": (elapsed / iterations) * 1000.0,
    }


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

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

    daemon = PathKitDaemon.start()
    try:
        summary: dict[str, object] = {"output_dir": str(output_dir), "iterations": args.iterations, "cases": {}}
        for case_name, placements in cases.items():
            payload, stroke_width = build_union_request(asset_path=asset_path, placements=placements)
            case_dir = output_dir / case_name
            case_summary = {
                "placements": placements,
                "stroke_width": stroke_width,
                "backends": {},
            }

            backends = {
                "pathkit_spawn": lambda current: run_pathkit_spawn(current),
                "pathkit_daemon": lambda current: daemon.request(current),
                "skia_pathops": lambda current: run_skia_pathops(current),
            }

            canonical_svg: str | None = None
            for backend_name, backend_fn in backends.items():
                result = backend_fn(payload)
                render_meta = render_case(
                    output_dir=case_dir / backend_name,
                    result=result,
                    stroke_width=stroke_width,
                    resvg_executable=resvg_executable,
                )
                benchmark = benchmark_case(backend_fn, payload, args.iterations)
                svg_path = case_dir / backend_name / "merged.svg"
                svg_source = svg_path.read_text(encoding="utf-8")
                if canonical_svg is None:
                    canonical_svg = svg_source
                case_summary["backends"][backend_name] = {
                    "bounds": result["bounds"],
                    "tight_bounds": result["tight_bounds"],
                    "render_box": render_meta,
                    "benchmark": benchmark,
                    "svg_matches_first_backend": svg_source == canonical_svg,
                }

            summary["cases"][case_name] = case_summary

        (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        daemon.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
