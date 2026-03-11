#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


ROOT_DIR = Path(__file__).resolve().parents[1]
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


def _run_single(args: argparse.Namespace, image_stem: str, dialogue_index: int) -> Path:
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

    cmd = [
        sys.executable,
        str(ROOT_DIR / "scripts" / "poc_scene_place_from_masks.py"),
        "--image",
        str(image_path),
        "--reflow-json",
        str(reflow_path),
        "--face-mask",
        str(face_mask),
        "--person-mask",
        str(person_mask),
        "--solver",
        "cp-sat",
        "--out-dir",
        str(out_dir),
        "--font-size",
        str(args.font_size),
        "--text-renderer",
        args.text_renderer,
        "--bubble-renderer",
        args.bubble_renderer,
    ]
    if chest_mask is not None:
        cmd.extend(["--chest-mask", str(chest_mask)])
    if lower_mask is not None:
        cmd.extend(["--lower-mask", str(lower_mask)])
    if args.bubble_asset:
        cmd.extend(["--bubble-asset", args.bubble_asset])
    subprocess.run(cmd, check=True, cwd=ROOT_DIR)
    return out_dir / "rendered.png"


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
    for image_stem in args.images:
        rendered_items: list[tuple[str, Path]] = []
        for dialogue_index in args.dialogues:
            rendered_path = _run_single(args, image_stem, dialogue_index)
            rendered_items.append((f"dialogue{dialogue_index}", rendered_path))
            manifest.setdefault(image_stem, {})[f"dialogue{dialogue_index}"] = str(rendered_path)
        _build_overview(image_stem, rendered_items, args.out_root)

    manifest_path = args.out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
