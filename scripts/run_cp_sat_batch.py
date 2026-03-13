#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
DEFAULT_MASK_ROOT = Path(
    "/mnt/c/Users/inada/obsidian/base/03_projects/comfy-agent/outputs/bboxseg_masks_test_to_test4_20260310_180344"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch-run the CP-SAT scene placement PoC and build overview images.")
    parser.add_argument(
        "--images",
        nargs="+",
        default=["test", "test1", "test2", "test3", "test4"],
        help="Image stems under imgs/ without extension.",
    )
    parser.add_argument(
        "--dialogues",
        nargs="+",
        type=int,
        default=[1, 2, 3, 4, 5],
        help="Dialogue indices matching out/font22_dialogue_series/dialogueN/reflow.json.",
    )
    parser.add_argument(
        "--mask-root",
        type=Path,
        default=DEFAULT_MASK_ROOT,
        help="Directory that contains <stem>_face_mask.png and related masks.",
    )
    parser.add_argument(
        "--reflow-root",
        type=Path,
        default=ROOT_DIR / "out" / "font22_dialogue_series",
        help="Directory that contains dialogueN/reflow.json.",
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=ROOT_DIR / "out" / "font22_cp_sat_iter",
        help="Output root for per-image runs and overview.png.",
    )
    parser.add_argument("--font-size", type=int, default=22, help="Font size passed to the PoC.")
    parser.add_argument("--text-renderer", default="resvg-hybrid", help="Text renderer backend.")
    parser.add_argument("--bubble-renderer", default="resvg", help="Bubble renderer backend.")
    parser.add_argument("--bubble-asset", default=None, help="Optional bubble asset override.")
    parser.add_argument(
        "--planner-mode",
        choices=("solver", "cp-sat"),
        default="cp-sat",
        help="PoC planner mode passed to poc_scene_place_from_masks.py.",
    )
    parser.add_argument(
        "--use-worker",
        choices=("auto", "on", "off"),
        default="off",
        help="Worker mode passed to poc_scene_place_from_masks.py.",
    )
    parser.add_argument("--jobs", type=int, default=1, help="Parallel workers. 1 runs in-process without subprocess.")
    return parser.parse_args()


def _mask_arg(mask_root: Path, stem: str, suffix: str) -> Path | None:
    candidate = mask_root / f"{stem}_{suffix}_mask.png"
    return candidate if candidate.exists() else None


def _load_binary_mask(path: Path) -> np.ndarray:
    image = Image.open(path)
    if "A" in image.getbands():
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        return np.any(rgba[:, :, :3] > 0, axis=2) | (rgba[:, :, 3] > 0)
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    return np.any(rgb > 0, axis=2)


def _optional_mask_arg(mask_root: Path, stem: str, suffix: str, person_mask: np.ndarray) -> Path | None:
    candidate = _mask_arg(mask_root, stem, suffix)
    if candidate is None:
        return None
    mask = _load_binary_mask(candidate)
    if mask.shape != person_mask.shape:
        return None
    if not np.any(mask & person_mask):
        return None
    return candidate


def _run_single_payload(args: argparse.Namespace, image_stem: str, dialogue_index: int) -> dict[str, str]:
    image_path = ROOT_DIR / "imgs" / f"{image_stem}.png"
    reflow_path = args.reflow_root / f"dialogue{dialogue_index}" / "reflow.json"
    out_dir = args.out_root / image_stem / f"dialogue{dialogue_index}_cp_sat"
    face_mask = _mask_arg(args.mask_root, image_stem, "face")
    person_mask = _mask_arg(args.mask_root, image_stem, "person")
    if face_mask is None or person_mask is None:
        raise RuntimeError(f"required masks not found for {image_stem}: {args.mask_root}")
    person_mask_array = _load_binary_mask(person_mask)
    chest_mask = _optional_mask_arg(args.mask_root, image_stem, "chest", person_mask_array)
    lower_mask = _optional_mask_arg(args.mask_root, image_stem, "lower", person_mask_array)
    head_mask = _optional_mask_arg(args.mask_root, image_stem, "head", person_mask_array)

    payload = {
        "image": str(image_path),
        "reflow_json": str(reflow_path),
        "face_mask": str(face_mask),
        "person_mask": str(person_mask),
        "solver": "cp-sat",
        "planner_mode": args.planner_mode,
        "out_dir": str(out_dir),
        "font_size": str(args.font_size),
        "text_renderer": args.text_renderer,
        "bubble_renderer": args.bubble_renderer,
        "use_worker": args.use_worker,
        "codex_backend": "manual",
        "codex_command": "codex",
        "codex_model": "",
        "codex_passes": "0",
        "render_output": "",
        "font": "",
        "font_family": "",
        "text_letter_spacing": "-1px",
        "text_word_spacing": "0",
    }
    if chest_mask is not None:
        payload["chest_mask"] = str(chest_mask)
    if lower_mask is not None:
        payload["lower_mask"] = str(lower_mask)
    if head_mask is not None:
        payload["head_mask"] = str(head_mask)
    if args.bubble_asset:
        payload["bubble_asset"] = args.bubble_asset
    return payload


def _namespace_from_payload(payload: dict[str, str]) -> argparse.Namespace:
    return argparse.Namespace(
        image=payload["image"],
        reflow_json=payload["reflow_json"],
        face_mask=payload["face_mask"],
        person_mask=payload["person_mask"],
        chest_mask=payload.get("chest_mask"),
        lower_mask=payload.get("lower_mask"),
        head_mask=payload.get("head_mask"),
        solver=payload["solver"],
        planner_mode=payload["planner_mode"],
        codex_edit_json=[],
        codex_backend=payload.get("codex_backend", "manual"),
        codex_command=payload.get("codex_command", "codex"),
        codex_model=payload.get("codex_model") or None,
        codex_passes=int(payload.get("codex_passes", "0")),
        out_dir=payload["out_dir"],
        font_size=int(payload["font_size"]),
        render_output=payload.get("render_output") or None,
        font=payload.get("font") or None,
        font_family=payload.get("font_family") or None,
        bubble_asset=payload.get("bubble_asset") or None,
        text_renderer=payload["text_renderer"],
        bubble_renderer=payload["bubble_renderer"],
        text_letter_spacing=payload.get("text_letter_spacing", "-1px"),
        text_word_spacing=payload.get("text_word_spacing", "0"),
        resvg_tu_override=True,
        use_worker=payload["use_worker"],
    )


def _run_single_payload_in_process(payload: dict[str, str]) -> tuple[str, Path]:
    poc_module = importlib.import_module("poc_scene_place_from_masks")
    namespace = _namespace_from_payload(payload)
    result = poc_module.run_args(namespace, emit_paths=False)
    if int(result["exit_code"]) != 0:
        previous_max_seconds = os.environ.get("TEXT_BUBBLE_CP_SAT_MAX_SECONDS")
        os.environ["TEXT_BUBBLE_CP_SAT_MAX_SECONDS"] = "30"
        try:
            cp_sat_scene_solver = importlib.import_module("cp_sat_scene_solver")
            importlib.reload(cp_sat_scene_solver)
            result = poc_module.run_args(namespace, emit_paths=False)
        finally:
            if previous_max_seconds is None:
                os.environ.pop("TEXT_BUBBLE_CP_SAT_MAX_SECONDS", None)
            else:
                os.environ["TEXT_BUBBLE_CP_SAT_MAX_SECONDS"] = previous_max_seconds
        if int(result["exit_code"]) != 0:
            raise RuntimeError(f"cp-sat batch case failed: {payload['image']} {payload['reflow_json']}")
    rendered_path = Path(result["final_artifacts"]["rendered"])
    label = f"dialogue{Path(payload['reflow_json']).parent.name.replace('dialogue', '')}"
    return label, rendered_path


def _build_overview(image_stem: str, rendered_items: list[tuple[str, Path]], out_dir: Path) -> None:
    original = Image.open(ROOT_DIR / "imgs" / f"{image_stem}.png").convert("RGB")
    cells = [("original", original)]
    for label, rendered_path in rendered_items:
        cells.append((label, Image.open(rendered_path).convert("RGB")))

    cell_width = max(image.width for _, image in cells)
    cell_height = max(image.height for _, image in cells)
    header_height = 32
    cols = 3
    rows = (len(cells) + cols - 1) // cols
    canvas = Image.new("RGB", (cell_width * cols, (cell_height + header_height) * rows), (235, 233, 230))
    draw = ImageDraw.Draw(canvas)

    for index, (label, image) in enumerate(cells):
        col = index % cols
        row = index // cols
        left = col * cell_width
        top = row * (cell_height + header_height)
        canvas.paste(image, (left, top + header_height))
        draw.text((left + 10, top + 8), label, fill=(40, 40, 40))

    overview_path = out_dir / image_stem / "overview.png"
    overview_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(overview_path)


def main() -> int:
    args = parse_args()
    args.out_root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, str]] = {}
    payloads_by_image: dict[str, list[dict[str, str]]] = {
        image_stem: [_run_single_payload(args, image_stem, dialogue_index) for dialogue_index in args.dialogues]
        for image_stem in args.images
    }

    rendered_by_image: dict[str, list[tuple[str, Path]]] = {image_stem: [] for image_stem in args.images}
    if args.jobs <= 1:
        for image_stem in args.images:
            for payload in payloads_by_image[image_stem]:
                label, rendered_path = _run_single_payload_in_process(payload)
                rendered_by_image[image_stem].append((label, rendered_path))
                manifest.setdefault(image_stem, {})[label] = str(rendered_path)
            _build_overview(image_stem, rendered_by_image[image_stem], args.out_root)
    else:
        future_map: dict[Any, tuple[str, dict[str, str]]] = {}
        with ProcessPoolExecutor(max_workers=args.jobs) as executor:
            for image_stem in args.images:
                for payload in payloads_by_image[image_stem]:
                    future = executor.submit(_run_single_payload_in_process, payload)
                    future_map[future] = (image_stem, payload)
            for future in as_completed(future_map):
                image_stem, payload = future_map[future]
                try:
                    label, rendered_path = future.result()
                except Exception:
                    label, rendered_path = _run_single_payload_in_process(payload)
                rendered_by_image[image_stem].append((label, rendered_path))
                manifest.setdefault(image_stem, {})[label] = str(rendered_path)
        for image_stem in args.images:
            rendered_by_image[image_stem].sort(key=lambda item: item[0])
            _build_overview(image_stem, rendered_by_image[image_stem], args.out_root)

    manifest_path = args.out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
