#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


ROOT_DIR = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create heuristic person/face masks for imgs/test.png")
    parser.add_argument("--image", default=str(ROOT_DIR / "imgs" / "test.png"))
    parser.add_argument("--out-dir", default=str(ROOT_DIR / "out" / "test_masks"))
    return parser.parse_args()


def run_grabcut_person_mask(image_bgr: np.ndarray) -> np.ndarray:
    height, width = image_bgr.shape[:2]
    mask = np.zeros((height, width), np.uint8)
    bg_model = np.zeros((1, 65), np.float64)
    fg_model = np.zeros((1, 65), np.float64)

    # Keep a thin border as probable background while including almost the full figure.
    rect = (
        max(4, int(width * 0.03)),
        max(4, int(height * 0.02)),
        max(16, int(width * 0.94)),
        max(16, int(height * 0.96)),
    )
    cv2.grabCut(image_bgr, mask, rect, bg_model, fg_model, 8, cv2.GC_INIT_WITH_RECT)
    person_mask = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)

    kernel = np.ones((7, 7), np.uint8)
    person_mask = cv2.morphologyEx(person_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    person_mask = cv2.morphologyEx(person_mask, cv2.MORPH_OPEN, kernel, iterations=1)

    # Keep only the largest connected component.
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(person_mask)
    if num_labels > 1:
        largest = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
        person_mask = np.where(labels == largest, 255, 0).astype(np.uint8)
    return person_mask


def estimate_face_mask_from_person(person_mask: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    ys, xs = np.where(person_mask > 0)
    if len(xs) == 0:
        raise RuntimeError("person mask is empty")

    left = int(xs.min())
    right = int(xs.max())
    top = int(ys.min())
    bottom = int(ys.max())
    width = right - left + 1
    height = bottom - top + 1

    # Heuristic for this portrait framing: face is upper-middle of the person box.
    center_x = left + int(round(width * 0.50))
    center_y = top + int(round(height * 0.18))
    radius_x = max(12, int(round(width * 0.17)))
    radius_y = max(12, int(round(height * 0.10)))

    face_mask = np.zeros_like(person_mask)
    cv2.ellipse(
        face_mask,
        center=(center_x, center_y),
        axes=(radius_x, radius_y),
        angle=0,
        startAngle=0,
        endAngle=360,
        color=255,
        thickness=-1,
    )
    face_mask = cv2.bitwise_and(face_mask, person_mask)
    bbox = {
        "left_ratio": round((center_x - radius_x) / person_mask.shape[1], 4),
        "top_ratio": round((center_y - radius_y) / person_mask.shape[0], 4),
        "right_ratio": round((center_x + radius_x) / person_mask.shape[1], 4),
        "bottom_ratio": round((center_y + radius_y) / person_mask.shape[0], 4),
    }
    return face_mask, bbox


def alpha_overlay(image_rgb: np.ndarray, mask: np.ndarray, color: tuple[int, int, int], alpha: int) -> np.ndarray:
    result = image_rgb.copy()
    overlay = np.zeros_like(result)
    overlay[:, :] = np.array(color, dtype=np.uint8)
    mask_bool = mask > 0
    result[mask_bool] = (
        result[mask_bool].astype(np.uint16) * (255 - alpha) + overlay[mask_bool].astype(np.uint16) * alpha
    ) // 255
    return result.astype(np.uint8)


def main() -> int:
    args = parse_args()
    image_path = Path(args.image).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    image_bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise RuntimeError(f"failed to read image: {image_path}")
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

    person_mask = run_grabcut_person_mask(image_bgr)
    face_mask, face_bbox = estimate_face_mask_from_person(person_mask)

    person_path = out_dir / "person_mask.png"
    face_path = out_dir / "face_mask.png"
    overlay_path = out_dir / "mask_overlay.png"
    metadata_path = out_dir / "mask_metadata.json"

    Image.fromarray(person_mask, mode="L").save(person_path)
    Image.fromarray(face_mask, mode="L").save(face_path)

    overlay = alpha_overlay(image_rgb, person_mask, (80, 180, 255), 90)
    overlay = alpha_overlay(overlay, face_mask, (255, 80, 80), 130)
    Image.fromarray(overlay, mode="RGB").save(overlay_path)

    ys, xs = np.where(person_mask > 0)
    payload = {
        "image": str(image_path),
        "person_mask": str(person_path),
        "face_mask": str(face_path),
        "overlay": str(overlay_path),
        "person_bbox": {
            "left_ratio": round(float(xs.min()) / person_mask.shape[1], 4),
            "top_ratio": round(float(ys.min()) / person_mask.shape[0], 4),
            "right_ratio": round(float(xs.max()) / person_mask.shape[1], 4),
            "bottom_ratio": round(float(ys.max()) / person_mask.shape[0], 4),
        },
        "face_bbox": face_bbox,
        "method": {
            "person": "opencv grabcut with rectangle init",
            "face": "ellipse heuristic from person bbox upper region",
        },
    }
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(metadata_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
