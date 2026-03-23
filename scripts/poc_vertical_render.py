#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bubble.assets import (  # noqa: E402
    browser_font_stack,
    pick_font_path,
    resolve_bubble_asset,
    resolve_chromium_executable,
    resolve_resvg_executable,
)
from bubble.layout import compute_bubble_layout, compute_text_layout  # noqa: E402
from bubble.models import DEFAULT_FONT_DIVISOR, BubblePlan, TextRenderResult  # noqa: E402
from bubble.render import (  # noqa: E402
    _ensure_browser_env,
    _resolve_bubble_image,
    alpha_bbox_or_fail,
    alpha_composite_clipped,
    render_text_overlay,
)
from bubble.validation import load_plan_json  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PoC benchmark for vertical text rendering backends.")
    parser.add_argument("--input", required=True, help="Input image path.")
    parser.add_argument("--plan-json", required=True, help="Plan JSON path.")
    parser.add_argument("--output", required=True, help="Output image path for run #1.")
    parser.add_argument("--output-json", help="Output metrics JSON path. Defaults to <output>.metrics.json")
    parser.add_argument("--renderer", choices=["browser", "pango"], default="browser", help="Text renderer backend.")
    parser.add_argument(
        "--bubble-renderer",
        choices=["resvg", "browser"],
        default="resvg",
        help="Bubble renderer backend.",
    )
    parser.add_argument("--num-bubbles", type=int, default=2, help="Use first N bubbles from plan.json.")
    parser.add_argument("--runs", type=int, default=3, help="Number of runs for benchmark summary.")
    parser.add_argument("--font", help="Font path (best-effort).")
    parser.add_argument("--font-family", help="Font family hint for renderer.")
    parser.add_argument("--font-size", type=int, default=0, help="Override text font size.")
    parser.add_argument("--bubble-asset", help="Bubble asset path.")
    parser.add_argument("--case-id", default="", help="Optional case label in metrics output.")
    return parser.parse_args()


def _summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "mean": round(statistics.mean(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def _set_field_or_call(obj: Any, name: str, value: Any) -> bool:
    if value is None:
        return False
    if hasattr(obj, name):
        try:
            setattr(obj, name, value)
            return True
        except Exception:  # noqa: BLE001
            pass
    for setter in (f"set_{name}", f"set{name[0].upper()}{name[1:]}"):
        fn = getattr(obj, setter, None)
        if callable(fn):
            try:
                fn(value)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _load_pango_modules() -> tuple[Any, Any, Any]:
    try:
        import cairocffi as cairo
        import pangocairocffi
        import pangocffi
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError(
            "pango backend requires cairocffi/pangocffi/pangocairocffi. "
            "Install them before running --renderer pango."
        ) from exc
    return cairo, pangocffi, pangocairocffi


def _resolve_pango_family(font_path: str | None, font_family: str | None) -> str:
    if font_family and font_family.strip():
        return font_family.strip()
    fallback = browser_font_stack(font_path).split(",")
    if not fallback:
        return "sans-serif"
    return fallback[0].strip().strip('"')


def _render_text_overlay_pango(
    *,
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
    modules: tuple[Any, Any, Any],
) -> TextRenderResult:
    cairo, pangocffi, pangocairocffi = modules

    family = _resolve_pango_family(font_path, font_family)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas_width, canvas_height)
    ctx = cairo.Context(surface)
    ctx.set_source_rgba(0, 0, 0, 0)
    ctx.paint()

    layout = pangocairocffi.create_layout(ctx)
    desc = pangocffi.FontDescription()
    desc.family = family
    desc.set_absolute_size(pangocffi.units_from_double(text_layout["font_size"]))
    layout.font_description = desc

    pango_context = getattr(layout, "context", None)
    gravity = getattr(getattr(pangocffi, "Gravity", object), "EAST", None)
    gravity_hint = getattr(getattr(pangocffi, "GravityHint", object), "STRONG", None)
    if pango_context is not None:
        _set_field_or_call(pango_context, "base_gravity", gravity)
        _set_field_or_call(pango_context, "gravity_hint", gravity_hint)
    _set_field_or_call(layout, "single_paragraph_mode", True)

    width_units = pangocffi.units_from_double(text_layout["column_width"])
    _set_field_or_call(layout, "width", width_units)

    text_top = text_layout["text_top"]
    for column_index, column in enumerate(plan.columns):
        column_left = text_layout["text_left"] + text_layout["block_width"] - text_layout["column_width"] - column_index * (
            text_layout["column_width"] + text_layout["column_gap"]
        )
        layout.text = column
        _, logical_rect = layout.get_extents()
        logical_x = pangocffi.units_to_double(logical_rect.x)
        logical_y = pangocffi.units_to_double(logical_rect.y)
        logical_w = pangocffi.units_to_double(logical_rect.width)

        draw_x = column_left + (text_layout["column_width"] - logical_w) / 2.0 - logical_x
        draw_y = text_top - logical_y

        ctx.save()
        ctx.translate(draw_x, draw_y)
        ctx.set_source_rgba(0.067, 0.067, 0.067, 1.0)
        ctx.move_to(0.0, 0.0)
        pangocairocffi.update_layout(ctx, layout)
        pangocairocffi.show_layout(ctx, layout)
        ctx.restore()

    png_buffer = io.BytesIO()
    surface.write_to_png(png_buffer)
    overlay = Image.open(io.BytesIO(png_buffer.getvalue())).convert("RGBA")
    return TextRenderResult(image=overlay, alpha_bbox=alpha_bbox_or_fail(overlay))


def _run_once(
    *,
    image_path: Path,
    plans: list[BubblePlan],
    renderer: str,
    bubble_renderer: str,
    font_path: str | None,
    font_family: str | None,
    bubble_asset: Path,
    font_size: int,
    output_path: Path | None,
) -> dict[str, Any]:
    if not plans:
        raise RuntimeError("no bubble plans to render")
    if renderer not in {"browser", "pango"}:
        raise RuntimeError(f"unsupported renderer: {renderer}")
    if bubble_renderer not in {"resvg", "browser"}:
        raise RuntimeError(f"unsupported bubble renderer: {bubble_renderer}")

    base = Image.open(image_path).convert("RGBA")
    width_px, height_px = base.size
    actual_font_size = font_size or max(22, min(48, height_px // DEFAULT_FONT_DIVISOR))

    resvg_executable = resolve_resvg_executable() if bubble_renderer == "resvg" else None
    if bubble_renderer == "resvg" and not resvg_executable:
        raise RuntimeError("resvg not found; install resvg or use --bubble-renderer browser")

    startup_begin = time.perf_counter()
    pango_modules: tuple[Any, Any, Any] | None = None
    if renderer == "pango":
        pango_modules = _load_pango_modules()

    browser = None
    playwright = None
    needs_browser = renderer == "browser" or bubble_renderer == "browser"
    if needs_browser:
        from playwright.sync_api import sync_playwright

        _ensure_browser_env()
        chromium_executable = resolve_chromium_executable()
        launch_kwargs: dict[str, Any] = {"headless": True}
        if chromium_executable:
            launch_kwargs["executable_path"] = chromium_executable
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(**launch_kwargs)
    startup_sec = time.perf_counter() - startup_begin

    breakdown = {
        "text_layout_sec": 0.0,
        "text_render_sec": 0.0,
        "bubble_layout_sec": 0.0,
        "bubble_render_sec": 0.0,
        "composite_sec": 0.0,
    }
    vertical_checks: list[bool] = []

    draw_begin = time.perf_counter()
    bubble_cache: dict[tuple[str, str, int, int], Image.Image] = {}
    canvas = base.copy()
    try:
        for plan in plans:
            t0 = time.perf_counter()
            text_layout = compute_text_layout(width_px, height_px, plan, actual_font_size)
            breakdown["text_layout_sec"] += time.perf_counter() - t0

            t0 = time.perf_counter()
            if renderer == "browser":
                text_overlay = render_text_overlay(
                    renderer="browser",
                    browser=browser,
                    canvas_width=width_px,
                    canvas_height=height_px,
                    plan=plan,
                    text_layout=text_layout,
                    font_path=font_path,
                    font_family=font_family,
                )
            else:
                if pango_modules is None:
                    raise RuntimeError("pango modules are not initialized")
                text_overlay = _render_text_overlay_pango(
                    canvas_width=width_px,
                    canvas_height=height_px,
                    plan=plan,
                    text_layout=text_layout,
                    font_path=font_path,
                    font_family=font_family,
                    modules=pango_modules,
                )
            breakdown["text_render_sec"] += time.perf_counter() - t0

            bbox_left, bbox_top, bbox_right, bbox_bottom = text_overlay.alpha_bbox
            vertical_checks.append((bbox_bottom - bbox_top) >= (bbox_right - bbox_left))

            t0 = time.perf_counter()
            bubble_layout = compute_bubble_layout(
                canvas_width=width_px,
                canvas_height=height_px,
                text_bbox=text_overlay.alpha_bbox,
                text_layout=text_layout,
                font_size=actual_font_size,
                outline_width=text_layout["outline_width"],
            )
            breakdown["bubble_layout_sec"] += time.perf_counter() - t0

            t0 = time.perf_counter()
            bubble_image = _resolve_bubble_image(
                bubble_renderer=bubble_renderer,
                bubble_asset=bubble_asset,
                bubble_width=bubble_layout["bubble_width"],
                bubble_height=bubble_layout["bubble_height"],
                browser=browser,
                resvg_executable=resvg_executable,
                cache=bubble_cache,
            )
            breakdown["bubble_render_sec"] += time.perf_counter() - t0

            t0 = time.perf_counter()
            alpha_composite_clipped(canvas, bubble_image, bubble_layout["bubble_left"], bubble_layout["bubble_top"])
            canvas.alpha_composite(text_overlay.image)
            breakdown["composite_sec"] += time.perf_counter() - t0
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()

    draw_sec = time.perf_counter() - draw_begin
    total_sec = startup_sec + draw_sec

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix.lower() in {".jpg", ".jpeg"}:
            canvas.convert("RGB").save(output_path, quality=95)
        else:
            canvas.save(output_path)

    return {
        "startup_sec": round(startup_sec, 3),
        "draw_sec": round(draw_sec, 3),
        "total_sec": round(total_sec, 3),
        "breakdown_sec": {key: round(value, 3) for key, value in breakdown.items()},
        "pass_quality": all(vertical_checks) if vertical_checks else False,
        "vertical_checks": vertical_checks,
        "bubble_count": len(plans),
        "font_size": actual_font_size,
    }


def main() -> int:
    args = parse_args()

    input_path = Path(args.input)
    plan_path = Path(args.plan_json)
    output_path = Path(args.output)
    if not input_path.exists():
        print(f"input image not found: {input_path}", file=sys.stderr)
        return 1
    if not plan_path.exists():
        print(f"plan JSON not found: {plan_path}", file=sys.stderr)
        return 1
    if args.num_bubbles < 1:
        print("--num-bubbles must be >= 1", file=sys.stderr)
        return 1
    if args.runs < 1:
        print("--runs must be >= 1", file=sys.stderr)
        return 1

    _, loaded_plans = load_plan_json(plan_path)
    plans = loaded_plans[: args.num_bubbles]
    if not plans:
        print("no bubble plans selected", file=sys.stderr)
        return 1

    font_path = pick_font_path(args.font)
    bubble_asset = resolve_bubble_asset(args.bubble_asset)
    if bubble_asset is None:
        print(f"bubble asset not found: {args.bubble_asset}", file=sys.stderr)
        return 1

    run_results: list[dict[str, Any]] = []
    try:
        for index in range(args.runs):
            save_to = output_path if index == 0 else None
            run_results.append(
                _run_once(
                    image_path=input_path,
                    plans=plans,
                    renderer=args.renderer,
                    bubble_renderer=args.bubble_renderer,
                    font_path=font_path,
                    font_family=args.font_family,
                    bubble_asset=bubble_asset,
                    font_size=args.font_size,
                    output_path=save_to,
                )
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    startup_values = [row["startup_sec"] for row in run_results]
    draw_values = [row["draw_sec"] for row in run_results]
    total_values = [row["total_sec"] for row in run_results]
    summary = {
        "startup_sec": _summarize(startup_values),
        "draw_sec": _summarize(draw_values),
        "total_sec": _summarize(total_values),
        "pass_quality_all_runs": all(bool(row["pass_quality"]) for row in run_results),
    }

    case_id = args.case_id.strip() or f"n{len(plans)}"
    payload = {
        "case_id": case_id,
        "renderer": args.renderer,
        "bubble_renderer": args.bubble_renderer,
        "runs": args.runs,
        "num_bubbles": len(plans),
        "input_image": str(input_path),
        "plan_json": str(plan_path),
        "output_image": str(output_path),
        "font_path": font_path,
        "font_family": args.font_family,
        "bubble_asset": str(bubble_asset),
        "results": run_results,
        "summary": summary,
    }

    output_json = Path(args.output_json) if args.output_json else output_path.with_suffix(".metrics.json")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
