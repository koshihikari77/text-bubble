#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from bubble.assets import pick_font_path, resolve_bubble_asset  # noqa: E402
from bubble.models import save_scene_plan_json  # noqa: E402
from bubble.render import render_bubbles  # noqa: E402
from bubble.validation import compose_bubble_plans, load_reflow_plan_json  # noqa: E402


SOLVER_MODULES = {
    "beam": "beam_search_scene_solver",
    "cp-sat": "cp_sat_scene_solver",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build scene.json from reflow.json and external masks.")
    parser.add_argument("--image", required=True, help="Input image path")
    parser.add_argument("--reflow-json", required=True, help="Reflow JSON path")
    parser.add_argument("--face-mask", required=True, help="Face mask path")
    parser.add_argument("--person-mask", required=True, help="Person mask path")
    parser.add_argument("--chest-mask", help="Chest mask path")
    parser.add_argument("--lower-mask", help="Lower body mask path")
    parser.add_argument("--head-mask", help="Head/hair mask path")
    parser.add_argument("--solver", choices=sorted(SOLVER_MODULES), default="beam", help="Placement solver")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument("--font-size", type=int, default=0, help="Override estimated font size")
    parser.add_argument("--render-output", help="Override rendered output path")
    parser.add_argument("--font", help="Font file path")
    parser.add_argument("--font-family", help="CSS font-family override")
    parser.add_argument("--bubble-asset", help="Bubble asset path")
    parser.add_argument("--text-renderer", default="resvg-hybrid", help="Text renderer backend")
    parser.add_argument("--bubble-renderer", default="resvg", help="Bubble renderer backend")
    parser.add_argument("--text-letter-spacing", default="-1px", help="Letter spacing for text renderer")
    parser.add_argument("--text-word-spacing", default="0", help="Word spacing for text renderer")
    parser.add_argument(
        "--resvg-tu-override",
        dest="resvg_tu_override",
        action="store_true",
        default=True,
        help="Force manual upright rendering for known Tu punctuation in resvg-hybrid",
    )
    parser.add_argument(
        "--no-resvg-tu-override",
        dest="resvg_tu_override",
        action="store_false",
        help="Disable Tu punctuation override in resvg-hybrid",
    )
    return parser.parse_args()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    solver_module = importlib.import_module(SOLVER_MODULES[args.solver])
    image_path = Path(args.image).resolve()
    reflow_json_path = Path(args.reflow_json).resolve()
    face_mask_path = Path(args.face_mask).resolve()
    person_mask_path = Path(args.person_mask).resolve()
    chest_mask_path = Path(args.chest_mask).resolve() if args.chest_mask else None
    lower_mask_path = Path(args.lower_mask).resolve() if args.lower_mask else None
    head_mask_path = Path(args.head_mask).resolve() if args.head_mask else None
    out_dir = Path(args.out_dir).resolve()
    scene_path = out_dir / "scene.json"
    debug_overlay_path = out_dir / "debug_overlay.png"
    debug_scores_path = out_dir / "debug_scores.json"
    rendered_output_path = Path(args.render_output).resolve() if args.render_output else out_dir / "rendered.png"

    out_dir.mkdir(parents=True, exist_ok=True)

    image = Image.open(image_path)
    image_width, image_height = image.size
    font_size = args.font_size or solver_module.default_font_size(image_height)
    dialogue_lines, reflow_plans = load_reflow_plan_json(reflow_json_path)

    face_mask = solver_module.load_binary_mask(face_mask_path)
    person_mask = solver_module.load_binary_mask(person_mask_path)
    chest_mask = solver_module.load_binary_mask(chest_mask_path) if chest_mask_path is not None else None
    lower_mask = solver_module.load_binary_mask(lower_mask_path) if lower_mask_path is not None else None
    head_mask = solver_module.load_binary_mask(head_mask_path) if head_mask_path is not None else None
    if face_mask.shape != (image_height, image_width):
        raise RuntimeError("face mask size does not match image size")
    if person_mask.shape != (image_height, image_width):
        raise RuntimeError("person mask size does not match image size")
    if chest_mask is not None and chest_mask.shape != (image_height, image_width):
        raise RuntimeError("chest mask size does not match image size")
    if lower_mask is not None and lower_mask.shape != (image_height, image_width):
        raise RuntimeError("lower mask size does not match image size")
    if head_mask is not None and head_mask.shape != (image_height, image_width):
        raise RuntimeError("head mask size does not match image size")

    body_regions = solver_module.build_body_regions(
        person_mask,
        face_mask,
        chest_mask=chest_mask,
        lower_mask=lower_mask,
    )

    try:
        solution = solver_module.solve_scene_layout(
            reflow_plans=reflow_plans,
            image_width=image_width,
            image_height=image_height,
            face_mask=face_mask,
            person_mask=person_mask,
            chest_mask=chest_mask,
            lower_mask=lower_mask,
            head_mask=head_mask,
            font_size=font_size,
        )
    except Exception as exc:  # noqa: BLE001
        debug_payload: dict[str, Any]
        try:
            debug_payload = json.loads(str(exc))
        except json.JSONDecodeError:
            debug_payload = {"error": str(exc)}
        debug_payload.update(
            {
                "status": "error",
                "solver": args.solver,
                "image": str(image_path),
                "reflow_json": str(reflow_json_path),
                "face_mask": str(face_mask_path),
                "person_mask": str(person_mask_path),
                "chest_mask": str(chest_mask_path) if chest_mask_path is not None else None,
                "lower_mask": str(lower_mask_path) if lower_mask_path is not None else None,
                "head_mask": str(head_mask_path) if head_mask_path is not None else None,
                "font_size": font_size,
                "body_regions": body_regions.to_debug_dict(),
            }
        )
        solver_module.render_debug_overlay(
            image_path=image_path,
            output_path=debug_overlay_path,
            solution=None,
            person_mask=person_mask,
            face_mask=face_mask,
            body_regions=body_regions,
        )
        write_json(debug_scores_path, debug_payload)
        print(str(debug_scores_path), file=sys.stderr)
        return 1

    save_scene_plan_json(scene_path, dialogue_lines, solution.scene_plans)
    solver_module.render_debug_overlay(
        image_path=image_path,
        output_path=debug_overlay_path,
        solution=solution,
        person_mask=person_mask,
        face_mask=face_mask,
        body_regions=body_regions,
    )

    composed_plans = compose_bubble_plans(dialogue_lines, solution.scene_plans, reflow_plans)
    resolved_font_path = pick_font_path(args.font)
    resolved_bubble_asset = resolve_bubble_asset(args.bubble_asset)
    if resolved_bubble_asset is None:
        raise RuntimeError(f"bubble asset not found: {args.bubble_asset}")

    debug_payload = dict(solution.debug_payload)
    debug_payload.update(
        {
            "status": "ok",
            "solver": args.solver,
            "image": str(image_path),
            "reflow_json": str(reflow_json_path),
            "face_mask": str(face_mask_path),
            "person_mask": str(person_mask_path),
            "chest_mask": str(chest_mask_path) if chest_mask_path is not None else None,
            "lower_mask": str(lower_mask_path) if lower_mask_path is not None else None,
            "head_mask": str(head_mask_path) if head_mask_path is not None else None,
            "font_size": font_size,
            "output_scene_json": str(scene_path),
            "render_output": str(rendered_output_path),
            "text_renderer": args.text_renderer,
            "bubble_renderer": args.bubble_renderer,
            "bubble_asset": str(resolved_bubble_asset),
            "font_path": resolved_font_path,
            "font_family": args.font_family,
        }
    )

    try:
        render_bubbles(
            image_path=image_path,
            output_path=rendered_output_path,
            plans=composed_plans,
            font_path=resolved_font_path,
            font_family=args.font_family,
            bubble_asset=resolved_bubble_asset,
            font_size=font_size,
            text_renderer=args.text_renderer,
            bubble_renderer=args.bubble_renderer,
            text_letter_spacing=args.text_letter_spacing,
            text_word_spacing=args.text_word_spacing,
            resvg_tu_override=args.resvg_tu_override,
        )
    except Exception as exc:  # noqa: BLE001
        debug_payload["status"] = "render_error"
        debug_payload["solver"] = args.solver
        debug_payload["render_error"] = str(exc)
        write_json(debug_scores_path, debug_payload)
        print(str(debug_scores_path), file=sys.stderr)
        return 1

    write_json(debug_scores_path, debug_payload)
    print(scene_path)
    print(debug_overlay_path)
    print(debug_scores_path)
    print(rendered_output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
