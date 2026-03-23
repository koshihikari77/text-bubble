#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bubble.assets import resolve_chromium_executable, resolve_resvg_executable  # noqa: E402
from bubble.render import _ensure_browser_env  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PoC: SVG vertical text rendering with resvg CLI (and browser reference).")
    parser.add_argument("--output-dir", required=True, help="Directory to save rendered images and metrics.")
    parser.add_argument(
        "--cases-file",
        default=str(Path(__file__).resolve().parent / "poc_svg_vertical_cases.json"),
        help="Path to SVG case definitions JSON.",
    )
    parser.add_argument("--case", action="append", default=[], help="Case ID filter. Repeat for multiple cases.")
    parser.add_argument("--font-family", default="Noto Serif JP", help='Font family used in SVG text style.')
    parser.add_argument("--font-size", type=int, default=32, help="SVG text font size in pixels.")
    parser.add_argument("--width", type=int, default=512, help="Output width.")
    parser.add_argument("--height", type=int, default=512, help="Output height.")
    parser.add_argument("--x", type=int, default=450, help="Text x anchor.")
    parser.add_argument("--y", type=int, default=40, help="Text y anchor.")
    parser.add_argument("--text-anchor", default="", help="Optional SVG text-anchor (start|middle|end).")
    parser.add_argument(
        "--dominant-baseline",
        default="",
        help="Optional SVG dominant-baseline (e.g. text-before-edge, hanging).",
    )
    parser.add_argument(
        "--letter-spacing",
        default="",
        help="Optional CSS letter-spacing for <text> (e.g. -1px, 0.5px).",
    )
    parser.add_argument(
        "--word-spacing",
        default="",
        help="Optional CSS word-spacing for <text> (e.g. 4px).",
    )
    parser.add_argument(
        "--anchor-recalc",
        action="store_true",
        help="Recalculate x/y so alpha bbox aligns to target x/y (right/top lock).",
    )
    parser.add_argument(
        "--anchor-recalc-iters",
        type=int,
        default=1,
        help="Max correction iterations for anchor recalculation.",
    )
    parser.add_argument(
        "--trim-bbox",
        action="store_true",
        help="Save trimmed PNGs cropped by alpha bbox.",
    )
    parser.add_argument("--resvg", help="Path to resvg executable. Defaults to auto-detection.")
    parser.add_argument("--skip-browser", action="store_true", help="Skip browser reference rendering.")
    parser.add_argument("--metrics-file", help="Metrics JSON output path.")
    return parser.parse_args()


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _alpha_bbox(path: Path) -> tuple[list[int] | None, bool]:
    image = Image.open(path).convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        return None, False
    left, top, right, bottom = bbox
    width = right - left
    height = bottom - top
    return [int(left), int(top), int(right), int(bottom)], height >= width


def _trim_to_alpha_bbox(src_path: Path, out_path: Path) -> tuple[list[int] | None, list[int] | None]:
    image = Image.open(src_path).convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        return None, None
    left, top, right, bottom = bbox
    trimmed = image.crop((left, top, right, bottom))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    trimmed.save(out_path)
    return [int(left), int(top), int(right), int(bottom)], [int(trimmed.width), int(trimmed.height)]


def _anchor_correction_delta(*, target_x: int, target_y: int, bbox: list[int] | None) -> tuple[int, int]:
    if bbox is None:
        return 0, 0
    # Lock alpha bbox top-right to target(x, y) for vertical-rl use cases.
    current_right = int(bbox[2])
    current_top = int(bbox[1])
    return int(target_x - current_right), int(target_y - current_top)


def _build_svg(
    case: dict[str, Any],
    *,
    width: int,
    height: int,
    font_family: str,
    font_size: int,
    x: int,
    y: int,
    text_anchor: str,
    dominant_baseline: str,
    letter_spacing: str,
    word_spacing: str,
) -> str:
    writing_mode = str(case.get("writingMode") or "vertical-rl")
    text_orientation = str(case.get("textOrientation") or "").strip()
    style_parts = [f"writing-mode:{writing_mode};"]
    if text_orientation:
        style_parts.append(f"text-orientation:{text_orientation};")
    if text_anchor:
        style_parts.append(f"text-anchor:{text_anchor};")
    if dominant_baseline:
        style_parts.append(f"dominant-baseline:{dominant_baseline};")
    if letter_spacing:
        style_parts.append(f"letter-spacing:{letter_spacing};")
    if word_spacing:
        style_parts.append(f"word-spacing:{word_spacing};")
    style = "".join(style_parts)
    text = _escape_xml(str(case.get("text", "")))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
  <style>
    text {{
      font-family: "{_escape_xml(font_family)}";
      font-size: {font_size}px;
      fill: #111111;
    }}
  </style>
  <text x="{x}" y="{y}" style="{style}">{text}</text>
</svg>
"""


def _render_with_resvg_cli(svg_source: str, out_png: Path, executable: str) -> float:
    with tempfile.TemporaryDirectory(prefix="text-bubble-svg-resvg-") as temp_dir:
        svg_path = Path(temp_dir) / "input.svg"
        svg_path.write_text(svg_source, encoding="utf-8")
        begin = time.perf_counter()
        process = subprocess.run(
            [executable, str(svg_path), str(out_png)],
            check=False,
            capture_output=True,
            text=True,
        )
        elapsed = time.perf_counter() - begin
    if process.returncode != 0:
        stderr = process.stderr.strip()
        stdout = process.stdout.strip()
        detail = stderr or stdout or f"exit code {process.returncode}"
        raise RuntimeError(f"resvg failed: {detail}")
    if not out_png.exists():
        raise RuntimeError(f"resvg did not generate output: {out_png}")
    return elapsed


def _render_with_browser(browser: Any, svg_source: str, *, width: int, height: int, out_png: Path) -> float:
    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;width:{width}px;height:{height}px;overflow:hidden;background:transparent">
{svg_source}
</body>
</html>
"""
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
    try:
        begin = time.perf_counter()
        page.set_content(html, wait_until="domcontentloaded", timeout=120000)
        page.evaluate("() => document.fonts ? document.fonts.ready.then(() => true) : true")
        png_data = page.screenshot(omit_background=True)
        elapsed = time.perf_counter() - begin
    finally:
        page.close()
    out_png.write_bytes(png_data)
    return elapsed


def _render_case_with_optional_anchor_recalc(
    *,
    renderer: str,
    browser: Any | None,
    executable: str,
    case: dict[str, Any],
    width: int,
    height: int,
    font_family: str,
    font_size: int,
    base_x: int,
    base_y: int,
    text_anchor: str,
    dominant_baseline: str,
    letter_spacing: str,
    word_spacing: str,
    out_png: Path,
    anchor_recalc: bool,
    anchor_recalc_iters: int,
) -> dict[str, Any]:
    used_x = int(base_x)
    used_y = int(base_y)
    total_sec = 0.0
    last_bbox: list[int] | None = None
    last_vertical = False
    initial_bbox: list[int] | None = None
    corrections: list[dict[str, int]] = []
    iterations = max(0, int(anchor_recalc_iters))

    def _render_once(x_value: int, y_value: int) -> tuple[float, list[int] | None, bool]:
        svg_source = _build_svg(
            case,
            width=width,
            height=height,
            font_family=font_family,
            font_size=font_size,
            x=x_value,
            y=y_value,
            text_anchor=text_anchor,
            dominant_baseline=dominant_baseline,
            letter_spacing=letter_spacing,
            word_spacing=word_spacing,
        )
        if renderer == "resvg":
            elapsed = _render_with_resvg_cli(svg_source, out_png, executable)
        else:
            if browser is None:
                raise RuntimeError("browser renderer requested without browser instance")
            elapsed = _render_with_browser(browser, svg_source, width=width, height=height, out_png=out_png)
        bbox, vertical = _alpha_bbox(out_png)
        return elapsed, bbox, vertical

    elapsed, bbox, vertical = _render_once(used_x, used_y)
    total_sec += elapsed
    last_bbox = bbox
    last_vertical = vertical
    initial_bbox = bbox

    if anchor_recalc and iterations > 0 and bbox is not None:
        for _ in range(iterations):
            dx, dy = _anchor_correction_delta(target_x=base_x, target_y=base_y, bbox=last_bbox)
            if dx == 0 and dy == 0:
                break
            used_x += dx
            used_y += dy
            corrections.append({"dx": dx, "dy": dy, "x": used_x, "y": used_y})
            elapsed, bbox, vertical = _render_once(used_x, used_y)
            total_sec += elapsed
            last_bbox = bbox
            last_vertical = vertical

    return {
        "sec": round(total_sec, 3),
        "bbox": last_bbox,
        "vertical": bool(last_vertical),
        "used_x": used_x,
        "used_y": used_y,
        "anchor_recalc_applied": bool(corrections),
        "anchor_recalc_initial_bbox": initial_bbox,
        "anchor_recalc_corrections": corrections,
    }


def _load_cases(path: Path, only_ids: set[str]) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError(f"cases JSON must be an array: {path}")
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"case {idx} must be an object")
        case_id = str(row.get("id", "")).strip()
        text = str(row.get("text", "")).strip()
        if not case_id:
            raise RuntimeError(f"case {idx} missing id")
        if not text:
            raise RuntimeError(f"case {case_id} missing text")
        if only_ids and case_id not in only_ids:
            continue
        rows.append(row)
    if only_ids:
        loaded_ids = {str(row["id"]) for row in rows}
        missing = sorted(only_ids - loaded_ids)
        if missing:
            raise RuntimeError(f"case id not found in {path}: {', '.join(missing)}")
    if not rows:
        raise RuntimeError("no cases selected")
    return rows


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "mean": round(statistics.mean(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cases_path = Path(args.cases_file)
    if not cases_path.exists():
        print(f"cases file not found: {cases_path}", file=sys.stderr)
        return 1
    selected_ids = {case_id.strip() for case_id in args.case if case_id.strip()}
    try:
        cases = _load_cases(cases_path, selected_ids)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    resvg_executable = args.resvg.strip() if args.resvg else (resolve_resvg_executable() or "")
    if not resvg_executable:
        print("resvg executable not found; pass --resvg or install resvg", file=sys.stderr)
        return 1

    browser = None
    playwright = None
    browser_startup_sec = 0.0
    if not args.skip_browser:
        from playwright.sync_api import sync_playwright

        _ensure_browser_env()
        launch_kwargs: dict[str, Any] = {"headless": True}
        chromium_executable = resolve_chromium_executable()
        if chromium_executable:
            launch_kwargs["executable_path"] = chromium_executable
        begin = time.perf_counter()
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(**launch_kwargs)
        browser_startup_sec = time.perf_counter() - begin

    rows: list[dict[str, Any]] = []
    try:
        for case in cases:
            case_id = str(case["id"])
            static_svg_source = _build_svg(
                case,
                width=args.width,
                height=args.height,
                font_family=args.font_family,
                font_size=args.font_size,
                x=args.x,
                y=args.y,
                text_anchor=args.text_anchor.strip(),
                dominant_baseline=args.dominant_baseline.strip(),
                letter_spacing=args.letter_spacing.strip(),
                word_spacing=args.word_spacing.strip(),
            )
            (output_dir / f"{case_id}.svg").write_text(static_svg_source, encoding="utf-8")

            resvg_png = output_dir / f"resvg_cli_{case_id}.png"
            browser_png = output_dir / f"browser_svg_{case_id}.png"

            resvg_result = _render_case_with_optional_anchor_recalc(
                renderer="resvg",
                browser=None,
                executable=resvg_executable,
                case=case,
                width=args.width,
                height=args.height,
                font_family=args.font_family,
                font_size=args.font_size,
                base_x=args.x,
                base_y=args.y,
                text_anchor=args.text_anchor.strip(),
                dominant_baseline=args.dominant_baseline.strip(),
                letter_spacing=args.letter_spacing.strip(),
                word_spacing=args.word_spacing.strip(),
                out_png=resvg_png,
                anchor_recalc=args.anchor_recalc,
                anchor_recalc_iters=args.anchor_recalc_iters,
            )
            resvg_bbox = resvg_result["bbox"]
            resvg_vertical = bool(resvg_result["vertical"])

            browser_sec: float | None = None
            browser_bbox: list[int] | None = None
            browser_vertical: bool | None = None
            browser_result: dict[str, Any] | None = None
            if browser is not None:
                browser_result = _render_case_with_optional_anchor_recalc(
                    renderer="browser",
                    browser=browser,
                    executable="",
                    case=case,
                    width=args.width,
                    height=args.height,
                    font_family=args.font_family,
                    font_size=args.font_size,
                    base_x=args.x,
                    base_y=args.y,
                    text_anchor=args.text_anchor.strip(),
                    dominant_baseline=args.dominant_baseline.strip(),
                    letter_spacing=args.letter_spacing.strip(),
                    word_spacing=args.word_spacing.strip(),
                    out_png=browser_png,
                    anchor_recalc=args.anchor_recalc,
                    anchor_recalc_iters=args.anchor_recalc_iters,
                )
                browser_sec = float(browser_result["sec"])
                browser_bbox = browser_result["bbox"]
                browser_vertical = bool(browser_result["vertical"])

            resvg_trim_bbox: list[int] | None = None
            resvg_trim_size: list[int] | None = None
            resvg_trim_path: str | None = None
            browser_trim_bbox: list[int] | None = None
            browser_trim_size: list[int] | None = None
            browser_trim_path: str | None = None
            if args.trim_bbox:
                trim_path = output_dir / f"resvg_cli_{case_id}.trim.png"
                resvg_trim_bbox, resvg_trim_size = _trim_to_alpha_bbox(resvg_png, trim_path)
                resvg_trim_path = str(trim_path) if resvg_trim_bbox is not None else None
                if browser is not None:
                    trim_path = output_dir / f"browser_svg_{case_id}.trim.png"
                    browser_trim_bbox, browser_trim_size = _trim_to_alpha_bbox(browser_png, trim_path)
                    browser_trim_path = str(trim_path) if browser_trim_bbox is not None else None

            row = {
                "case_id": case_id,
                "text": str(case.get("text", "")),
                "writing_mode": str(case.get("writingMode") or "vertical-rl"),
                "text_orientation": str(case.get("textOrientation") or ""),
                "text_anchor": args.text_anchor.strip() or None,
                "dominant_baseline": args.dominant_baseline.strip() or None,
                "letter_spacing": args.letter_spacing.strip() or None,
                "word_spacing": args.word_spacing.strip() or None,
                "resvg_cli_sec": float(resvg_result["sec"]),
                "resvg_cli_png": str(resvg_png),
                "resvg_bbox": resvg_bbox,
                "resvg_vertical": resvg_vertical,
                "resvg_used_x": int(resvg_result["used_x"]),
                "resvg_used_y": int(resvg_result["used_y"]),
                "resvg_anchor_recalc_applied": bool(resvg_result["anchor_recalc_applied"]),
                "resvg_anchor_recalc_initial_bbox": resvg_result["anchor_recalc_initial_bbox"],
                "resvg_anchor_recalc_corrections": resvg_result["anchor_recalc_corrections"],
                "resvg_trim_png": resvg_trim_path,
                "resvg_trim_bbox": resvg_trim_bbox,
                "resvg_trim_size": resvg_trim_size,
                "browser_sec": round(browser_sec, 3) if browser_sec is not None else None,
                "browser_png": str(browser_png) if browser_sec is not None else None,
                "browser_bbox": browser_bbox,
                "browser_vertical": browser_vertical,
                "browser_used_x": int(browser_result["used_x"]) if browser_result is not None else None,
                "browser_used_y": int(browser_result["used_y"]) if browser_result is not None else None,
                "browser_anchor_recalc_applied": (
                    bool(browser_result["anchor_recalc_applied"]) if browser_result is not None else None
                ),
                "browser_anchor_recalc_initial_bbox": (
                    browser_result["anchor_recalc_initial_bbox"] if browser_result is not None else None
                ),
                "browser_anchor_recalc_corrections": (
                    browser_result["anchor_recalc_corrections"] if browser_result is not None else None
                ),
                "browser_trim_png": browser_trim_path,
                "browser_trim_bbox": browser_trim_bbox,
                "browser_trim_size": browser_trim_size,
                "pass_quality": resvg_vertical if browser_vertical is None else (resvg_vertical and browser_vertical),
            }
            rows.append(row)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()

    resvg_times = [row["resvg_cli_sec"] for row in rows]
    browser_times = [row["browser_sec"] for row in rows if row["browser_sec"] is not None]

    payload = {
        "cases_file": str(cases_path),
        "output_dir": str(output_dir),
        "resvg_executable": resvg_executable,
        "font_family": args.font_family,
        "font_size": args.font_size,
        "text_anchor": args.text_anchor.strip() or None,
        "dominant_baseline": args.dominant_baseline.strip() or None,
        "letter_spacing": args.letter_spacing.strip() or None,
        "word_spacing": args.word_spacing.strip() or None,
        "anchor_recalc": bool(args.anchor_recalc),
        "anchor_recalc_iters": int(args.anchor_recalc_iters),
        "trim_bbox": bool(args.trim_bbox),
        "canvas_size": {"width": args.width, "height": args.height},
        "browser_startup_sec": round(browser_startup_sec, 3),
        "results": rows,
        "summary": {
            "resvg_cli_sec": _summary(resvg_times),
            "browser_sec": _summary(browser_times),
            "pass_quality_all_cases": all(bool(row["pass_quality"]) for row in rows),
        },
    }

    metrics_path = Path(args.metrics_file) if args.metrics_file else output_dir / "metrics_resvg_vertical_cli.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
