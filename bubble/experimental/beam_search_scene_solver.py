from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from bubble.layout import build_text_metrics
from bubble.models import DEFAULT_FONT_DIVISOR, ReflowBubblePlan, SceneBubblePlan


GRID_FRACTIONS_X = (0.08, 0.18, 0.30, 0.42, 0.54, 0.66, 0.78, 0.90)
GRID_FRACTIONS_Y = (0.05, 0.16, 0.28, 0.40, 0.52, 0.64, 0.76, 0.88)
TEMPLATE_SLOTS: dict[str, dict[int, tuple[str, ...]]] = {
    "n": {
        1: ("top-right",),
        2: ("top-right", "bottom-right"),
        3: ("top-right", "bottom-right", "top-left"),
        4: ("top-right", "bottom-right", "top-left", "bottom-left"),
        5: ("top-right", "mid-right", "bottom-right", "top-left", "bottom-left"),
    },
    "rotated-n": {
        1: ("top-right",),
        2: ("top-right", "top-left"),
        3: ("top-right", "top-left", "bottom-right"),
        4: ("top-right", "top-left", "bottom-right", "bottom-left"),
        5: ("top-right", "top-left", "mid-left", "bottom-right", "bottom-left"),
    },
}

PERSON_OVERLAP_WEIGHT = 2200.0
FACE_NEAR_WEIGHT = 28.0
FACE_FAR_WEIGHT = 4.2
EDGE_MARGIN_WEIGHT = 18.0
QUADRANT_MISMATCH_PENALTY = 160.0
QUADRANT_DISTANCE_WEIGHT = 0.18
FLOW_DIRECTION_WEIGHT = 2.8
CONTINUITY_DISTANCE_WEIGHT = 1.8
FACE_SLOT_SIDE_WEIGHT = 1.2
FACE_SLOT_HEIGHT_WEIGHT = 0.8
BUBBLE_SHELL_PERSON_WEIGHT = 220.0
BUBBLE_SHELL_CRITICAL_WEIGHT = 480.0
TEXT_EDGE_MARGIN_WEIGHT = 28.0
TEXT_CLEARANCE_PX = 4
BEAM_WIDTH = 8
READING_RIGHTWARD_WEIGHT = 16.0
READING_UPWARD_WEIGHT = 6.5
READING_COLUMN_RESET_UPWARD_WEIGHT = 2.4
IDEAL_EDGE_MARGIN_WEIGHT = 12.0
SAME_COLUMN_GAP_WEIGHT = 4.0
NEXT_COLUMN_GAP_WEIGHT = 3.0
NEXT_COLUMN_RESET_DOWNWARD_WEIGHT = 2.5
OUTER_EDGE_MARGIN_WEIGHT = 16.0
FACE_SIDE_GAP_WEIGHT = 4.0
SAME_SIDE_ALIGN_WEIGHT = 3.4
LEFT_START_BELOW_RIGHT_WEIGHT = 4.8


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0

    def as_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


@dataclass(frozen=True)
class BubbleDimensions:
    bubble_id: str
    sentence_ids: list[int]
    columns: list[str]
    font_size: int
    text_width: int
    text_height: int
    bubble_width: int
    bubble_height: int
    text_offset_x: int
    text_offset_y: int
    anchor_offset_x: int
    anchor_offset_y: int

    @property
    def bubble_diagonal(self) -> float:
        return math.hypot(self.bubble_width, self.bubble_height)

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "bubble_id": self.bubble_id,
            "sentence_ids": self.sentence_ids,
            "columns": self.columns,
            "font_size": self.font_size,
            "text_width": self.text_width,
            "text_height": self.text_height,
            "bubble_width": self.bubble_width,
            "bubble_height": self.bubble_height,
            "text_offset_x": self.text_offset_x,
            "text_offset_y": self.text_offset_y,
            "anchor_offset_x": self.anchor_offset_x,
            "anchor_offset_y": self.anchor_offset_y,
        }


@dataclass(frozen=True)
class Candidate:
    text_left: int
    text_top: int
    source: str


@dataclass
class BodyRegions:
    person_mask: np.ndarray
    face_mask: np.ndarray
    chest_mask: np.ndarray
    lower_mask: np.ndarray
    person_bbox: Rect
    face_bbox: Rect
    chest_bbox: Rect
    lower_bbox: Rect
    chest_source: str
    lower_source: str

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "person_bbox": self.person_bbox.as_dict(),
            "face_bbox": self.face_bbox.as_dict(),
            "chest_bbox": self.chest_bbox.as_dict(),
            "lower_bbox": self.lower_bbox.as_dict(),
            "chest_source": self.chest_source,
            "lower_source": self.lower_source,
        }


@dataclass
class PlacementChoice:
    bubble_id: str
    sentence_ids: list[int]
    anchor_x_px: int
    anchor_y_px: int
    text_box: Rect
    bubble_box: Rect
    total_score: float
    penalties: dict[str, float]
    source: str
    template: str
    slot: str

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "bubble_id": self.bubble_id,
            "sentence_ids": self.sentence_ids,
            "anchor_x_px": self.anchor_x_px,
            "anchor_y_px": self.anchor_y_px,
            "text_box": self.text_box.as_dict(),
            "bubble_box": self.bubble_box.as_dict(),
            "total_score": round(self.total_score, 3),
            "penalties": {key: round(value, 3) for key, value in self.penalties.items()},
            "source": self.source,
            "template": self.template,
            "slot": self.slot,
        }


@dataclass
class BubbleAttempt:
    bubble_id: str
    slot: str
    total_candidates: int
    valid_candidates: int
    invalid_counts: dict[str, int]
    top_candidates: list[dict[str, Any]]
    selected: dict[str, Any] | None


@dataclass
class TemplateAttempt:
    name: str
    success: bool
    total_score: float | None
    placements: list[PlacementChoice]
    failures: list[str]
    bubbles: list[BubbleAttempt]

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success": self.success,
            "total_score": None if self.total_score is None else round(self.total_score, 3),
            "failures": self.failures,
            "placements": [placement.to_debug_dict() for placement in self.placements],
            "bubbles": [
                {
                    "bubble_id": bubble.bubble_id,
                    "slot": bubble.slot,
                    "total_candidates": bubble.total_candidates,
                    "valid_candidates": bubble.valid_candidates,
                    "invalid_counts": bubble.invalid_counts,
                    "top_candidates": bubble.top_candidates,
                    "selected": bubble.selected,
                }
                for bubble in self.bubbles
            ],
        }


@dataclass
class PlacementSolution:
    selected_template: str
    scene_plans: list[SceneBubblePlan]
    placements: list[PlacementChoice]
    debug_payload: dict[str, Any]


@dataclass
class BeamState:
    placements: list[PlacementChoice]
    text_boxes: list[Rect]
    total_score: float


def load_binary_mask(path: Path) -> np.ndarray:
    image = Image.open(path)
    if "A" in image.getbands():
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        return np.any(rgba[:, :, :3] > 0, axis=2) | (rgba[:, :, 3] > 0)
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    return np.any(rgb > 0, axis=2)


def bbox_from_mask(mask: np.ndarray) -> Rect:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        raise RuntimeError("mask is empty")
    return Rect(int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def create_rect_mask(shape: tuple[int, int], rect: Rect) -> np.ndarray:
    height, width = shape
    clipped = clip_rect(rect, width, height)
    mask = np.zeros((height, width), dtype=bool)
    if clipped.area <= 0:
        return mask
    mask[clipped.top : clipped.bottom, clipped.left : clipped.right] = True
    return mask


def create_ellipse_mask(shape: tuple[int, int], rect: Rect) -> np.ndarray:
    height, width = shape
    clipped = clip_rect(rect, width, height)
    mask = np.zeros((height, width), dtype=bool)
    if clipped.area <= 0:
        return mask

    center_x = (clipped.left + clipped.right - 1) / 2.0
    center_y = (clipped.top + clipped.bottom - 1) / 2.0
    radius_x = max(1.0, clipped.width / 2.0)
    radius_y = max(1.0, clipped.height / 2.0)
    ys, xs = np.ogrid[clipped.top : clipped.bottom, clipped.left : clipped.right]
    ellipse = ((xs - center_x) / radius_x) ** 2 + ((ys - center_y) / radius_y) ** 2 <= 1.0
    mask[clipped.top : clipped.bottom, clipped.left : clipped.right] = ellipse
    return mask


def _validate_primary_masks(person_mask: np.ndarray, face_mask: np.ndarray) -> None:
    if person_mask.shape != face_mask.shape:
        raise RuntimeError("person mask and face mask must have the same shape")
    if person_mask.ndim != 2 or face_mask.ndim != 2:
        raise RuntimeError("masks must be 2-dimensional")
    if not np.any(person_mask):
        raise RuntimeError("person mask is empty")
    if not np.any(face_mask):
        raise RuntimeError("face mask is empty")


def _normalize_region_mask(
    mask: np.ndarray | None,
    *,
    name: str,
    shape: tuple[int, int],
    person_mask: np.ndarray,
) -> tuple[np.ndarray | None, str]:
    if mask is None:
        return None, "heuristic"
    if mask.shape != shape:
        raise RuntimeError(f"{name} mask must have the same shape as person mask")
    normalized = mask.astype(bool) & person_mask
    if not np.any(normalized):
        raise RuntimeError(f"{name} mask is empty after clipping to person mask")
    return normalized, "external"


def build_body_regions(
    person_mask: np.ndarray,
    face_mask: np.ndarray,
    chest_mask: np.ndarray | None = None,
    lower_mask: np.ndarray | None = None,
) -> BodyRegions:
    _validate_primary_masks(person_mask, face_mask)
    chest_mask_override, chest_source = _normalize_region_mask(
        chest_mask,
        name="chest",
        shape=person_mask.shape,
        person_mask=person_mask,
    )
    lower_mask_override, lower_source = _normalize_region_mask(
        lower_mask,
        name="lower",
        shape=person_mask.shape,
        person_mask=person_mask,
    )

    person_bbox = bbox_from_mask(person_mask)
    face_bbox = bbox_from_mask(face_mask)
    height = person_bbox.height
    width = person_bbox.width
    center_x = int(round(person_bbox.center_x))

    chest_half_width = max(face_bbox.width // 2, int(round(width * 0.18)))
    chest_height = max(face_bbox.height, int(round(height * 0.16)))
    chest_center_y = max(
        face_bbox.bottom + chest_height // 2 + max(4, height // 80),
        int(round(person_bbox.top + height * 0.34)),
    )
    chest_rect = Rect(
        center_x - chest_half_width,
        chest_center_y - chest_height // 2,
        center_x + chest_half_width,
        chest_center_y + chest_height // 2,
    )

    lower_half_width = max(face_bbox.width // 3, int(round(width * 0.12)))
    lower_height = max(face_bbox.height // 2, int(round(height * 0.13)))
    lower_center_y = int(round(person_bbox.top + height * 0.74))
    lower_rect = Rect(
        center_x - lower_half_width,
        lower_center_y - lower_height // 2,
        center_x + lower_half_width,
        lower_center_y + lower_height // 2,
    )

    resolved_chest_mask = chest_mask_override
    if resolved_chest_mask is None:
        resolved_chest_mask = create_ellipse_mask(person_mask.shape, chest_rect) & person_mask
        if not np.any(resolved_chest_mask):
            resolved_chest_mask = create_rect_mask(person_mask.shape, chest_rect) & person_mask
    resolved_lower_mask = lower_mask_override
    if resolved_lower_mask is None:
        resolved_lower_mask = create_ellipse_mask(person_mask.shape, lower_rect) & person_mask
        if not np.any(resolved_lower_mask):
            resolved_lower_mask = create_rect_mask(person_mask.shape, lower_rect) & person_mask
    chest_bbox = bbox_from_mask(resolved_chest_mask)
    lower_bbox = bbox_from_mask(resolved_lower_mask)

    return BodyRegions(
        person_mask=person_mask,
        face_mask=face_mask,
        chest_mask=resolved_chest_mask,
        lower_mask=resolved_lower_mask,
        person_bbox=person_bbox,
        face_bbox=face_bbox,
        chest_bbox=chest_bbox,
        lower_bbox=lower_bbox,
        chest_source=chest_source,
        lower_source=lower_source,
    )


def estimate_bubble_dimensions(
    plan: ReflowBubblePlan,
    *,
    image_width: int,
    image_height: int,
    font_size: int,
) -> BubbleDimensions:
    metrics = build_text_metrics(font_size, plan.columns)
    outline_width = max(3, image_width // 320)
    em = max(font_size, 24)
    horizontal_padding = max(outline_width * 6, int(round(em * 1.35)))
    vertical_padding = max(outline_width * 4, int(round(em * 1.0)))
    bubble_width = metrics["block_width"] + horizontal_padding * 2
    bubble_height = metrics["block_height"] + vertical_padding * 2
    return BubbleDimensions(
        bubble_id=plan.bubble_id,
        sentence_ids=list(plan.sentence_ids),
        columns=list(plan.columns),
        font_size=font_size,
        text_width=metrics["block_width"],
        text_height=metrics["block_height"],
        bubble_width=bubble_width,
        bubble_height=bubble_height,
        text_offset_x=horizontal_padding,
        text_offset_y=vertical_padding,
        anchor_offset_x=horizontal_padding + metrics["block_width"],
        anchor_offset_y=vertical_padding,
    )


def clip_rect(rect: Rect, width: int, height: int) -> Rect:
    return Rect(
        max(0, min(width, rect.left)),
        max(0, min(height, rect.top)),
        max(0, min(width, rect.right)),
        max(0, min(height, rect.bottom)),
    )


def make_layout_boxes_from_bubble_position(
    dimensions: BubbleDimensions,
    bubble_left: int,
    bubble_top: int,
) -> tuple[Rect, Rect, int, int]:
    bubble_box = Rect(
        bubble_left,
        bubble_top,
        bubble_left + dimensions.bubble_width,
        bubble_top + dimensions.bubble_height,
    )
    text_left = bubble_left + dimensions.text_offset_x
    text_top = bubble_top + dimensions.text_offset_y
    text_box = Rect(
        text_left,
        text_top,
        text_left + dimensions.text_width,
        text_top + dimensions.text_height,
    )
    anchor_x = bubble_left + dimensions.anchor_offset_x
    anchor_y = bubble_top + dimensions.anchor_offset_y
    return text_box, bubble_box, anchor_x, anchor_y


def make_layout_boxes_from_text_position(
    dimensions: BubbleDimensions,
    text_left: int,
    text_top: int,
) -> tuple[Rect, Rect, int, int]:
    bubble_left = text_left - dimensions.text_offset_x
    bubble_top = text_top - dimensions.text_offset_y
    return make_layout_boxes_from_bubble_position(dimensions, bubble_left, bubble_top)


def rects_intersect(left: Rect, right: Rect) -> bool:
    return (
        left.left < right.right
        and left.right > right.left
        and left.top < right.bottom
        and left.bottom > right.top
    )


def rect_intersection_area(left: Rect, right: Rect) -> int:
    inter_left = max(left.left, right.left)
    inter_top = max(left.top, right.top)
    inter_right = min(left.right, right.right)
    inter_bottom = min(left.bottom, right.bottom)
    if inter_left >= inter_right or inter_top >= inter_bottom:
        return 0
    return (inter_right - inter_left) * (inter_bottom - inter_top)


def rect_mask_overlap_area(mask: np.ndarray, rect: Rect) -> int:
    clipped = clip_rect(rect, mask.shape[1], mask.shape[0])
    if clipped.area <= 0:
        return 0
    return int(mask[clipped.top : clipped.bottom, clipped.left : clipped.right].sum())


def rect_distance(left: Rect, right: Rect) -> float:
    dx = max(left.left - right.right, right.left - left.right, 0)
    dy = max(left.top - right.bottom, right.top - left.bottom, 0)
    return math.hypot(dx, dy)


def expand_rect(rect: Rect, padding: int) -> Rect:
    return Rect(
        rect.left - padding,
        rect.top - padding,
        rect.right + padding,
        rect.bottom + padding,
    )


def slot_regions(image_width: int, image_height: int, person_bbox: Rect) -> dict[str, Rect]:
    center_x = int(round(person_bbox.center_x))
    center_y = int(round(person_bbox.center_y))
    band_half_height = max(40, image_height // 8)
    return {
        "top-right": Rect(center_x, 0, image_width, center_y),
        "mid-right": Rect(center_x, max(0, center_y - band_half_height), image_width, min(image_height, center_y + band_half_height)),
        "bottom-right": Rect(center_x, center_y, image_width, image_height),
        "top-left": Rect(0, 0, center_x, center_y),
        "mid-left": Rect(0, max(0, center_y - band_half_height), center_x, min(image_height, center_y + band_half_height)),
        "bottom-left": Rect(0, center_y, center_x, image_height),
    }


def slot_expected_relation(prev_slot: str, current_slot: str) -> str:
    prev_row = 0 if "top" in prev_slot else 1 if "mid" in prev_slot else 2
    curr_row = 0 if "top" in current_slot else 1 if "mid" in current_slot else 2
    prev_col = 1 if "right" in prev_slot else 0
    curr_col = 1 if "right" in current_slot else 0
    if curr_row > prev_row:
        return "down"
    if curr_col < prev_col:
        return "left"
    return "diagonal"


def point_in_rect(x: float, y: float, rect: Rect) -> bool:
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


def candidate_key(candidate: Candidate) -> tuple[int, int]:
    return candidate.text_left, candidate.text_top


def slot_side(slot: str) -> str:
    return "right" if "right" in slot else "left"


def ideal_side_text_left(
    *,
    preferred_slot: str,
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
) -> int:
    outer_margin = max(56, int(round(min(image_width, image_height) * 0.09)))
    if slot_side(preferred_slot) == "right":
        return image_width - outer_margin - dimensions.text_width
    return outer_margin


def generate_state_candidates(
    *,
    base_candidates: list[Candidate],
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    preferred_slot: str,
    placed_choices: list[PlacementChoice],
) -> list[Candidate]:
    if not placed_choices:
        return base_candidates

    candidates = list(base_candidates)
    seen = {candidate_key(candidate) for candidate in candidates}

    def add_candidate(text_left: int, text_top: int, source: str) -> None:
        candidate = Candidate(int(round(text_left)), int(round(text_top)), source)
        key = candidate_key(candidate)
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    previous_choice = placed_choices[-1]
    preferred_side = slot_side(preferred_slot)
    previous_side = slot_side(previous_choice.slot)
    aligned_left = ideal_side_text_left(
        preferred_slot=preferred_slot,
        dimensions=dimensions,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
    )
    tight_gap = max(6, int(round(min(previous_choice.text_box.height, dimensions.text_height) * 0.04)))

    if preferred_side == previous_side:
        if preferred_side == "right":
            stack_left = previous_choice.text_box.right - dimensions.text_width
        else:
            stack_left = previous_choice.text_box.left
        stack_top = previous_choice.text_box.bottom + tight_gap
        add_candidate(stack_left, stack_top, "state-stack")
        add_candidate(aligned_left, stack_top, "state-stack-balanced")
    elif preferred_side == "left":
        right_choices = [choice for choice in placed_choices if slot_side(choice.slot) == "right"]
        if right_choices:
            top_anchor = min(choice.text_box.top for choice in right_choices)
            bottom_anchor = max(choice.text_box.bottom for choice in right_choices)
            add_candidate(aligned_left, top_anchor + tight_gap, "state-left-below-right-top")
            add_candidate(aligned_left, bottom_anchor + tight_gap, "state-left-below-right-bottom")
    return candidates


def validate_postprocess_position(
    *,
    text_left: int,
    text_top: int,
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    placed_text_boxes: list[Rect],
    preferred_slot: str,
    enforce_face_side_gap: bool,
) -> PlacementChoice | None:
    text_box, bubble_box, anchor_x, anchor_y = make_layout_boxes_from_text_position(
        dimensions,
        text_left,
        text_top,
    )
    if text_box.left < 0 or text_box.top < 0 or text_box.right > image_width or text_box.bottom > image_height:
        return None
    if rect_mask_overlap_area(body_regions.face_mask, text_box) > 0:
        return None
    if rect_mask_overlap_area(body_regions.chest_mask, text_box) > 0:
        return None
    if rect_mask_overlap_area(body_regions.lower_mask, text_box) > 0:
        return None
    expanded_text_box = expand_rect(text_box, TEXT_CLEARANCE_PX)
    if any(rects_intersect(expanded_text_box, expand_rect(other, TEXT_CLEARANCE_PX)) for other in placed_text_boxes):
        return None
    if enforce_face_side_gap:
        min_side_gap = max(20, int(round(body_regions.face_bbox.width * 0.24)))
        if slot_side(preferred_slot) == "right":
            side_gap = text_box.left - body_regions.face_bbox.right
        else:
            side_gap = body_regions.face_bbox.left - text_box.right
        if side_gap < min_side_gap:
            return None
    return PlacementChoice(
        bubble_id=dimensions.bubble_id,
        sentence_ids=list(dimensions.sentence_ids),
        anchor_x_px=anchor_x,
        anchor_y_px=anchor_y,
        text_box=text_box,
        bubble_box=bubble_box,
        total_score=0.0,
        penalties={},
        source="postprocess",
        template="postprocess",
        slot=preferred_slot,
    )


def search_postprocess_position(
    *,
    target_left: int,
    target_top: int,
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    placed_text_boxes: list[Rect],
    preferred_slot: str,
) -> PlacementChoice | None:
    dxs = (0, -8, 8, -16, 16, -24, 24, -40, 40, -56, 56, -72, 72)
    dys = (0, -8, 8, -16, 16, -24, 24, -40, 40, -56, 56, -72, 72, -96, 96)
    for enforce_face_side_gap in (True, False):
        best_choice: PlacementChoice | None = None
        best_cost: float | None = None
        for dy in dys:
            for dx in dxs:
                choice = validate_postprocess_position(
                    text_left=int(round(target_left + dx)),
                    text_top=int(round(target_top + dy)),
                    dimensions=dimensions,
                    image_width=image_width,
                    image_height=image_height,
                    body_regions=body_regions,
                    placed_text_boxes=placed_text_boxes,
                    preferred_slot=preferred_slot,
                    enforce_face_side_gap=enforce_face_side_gap,
                )
                if choice is None:
                    continue
                cost = abs(dx) * 1.2 + abs(dy)
                if best_cost is None or cost < best_cost:
                    best_choice = choice
                    best_cost = cost
        if best_choice is not None:
            return best_choice
    return None


def postprocess_placements(
    *,
    placements: list[PlacementChoice],
    dimensions_by_bubble_id: dict[str, BubbleDimensions],
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
) -> list[PlacementChoice]:
    adjusted: list[PlacementChoice] = []
    right_tops: list[int] = []
    first_left_min_top: int | None = None

    for choice in placements:
        dimensions = dimensions_by_bubble_id[choice.bubble_id]
        preferred_side = slot_side(choice.slot)
        ideal_left = ideal_side_text_left(
            preferred_slot=choice.slot,
            dimensions=dimensions,
            image_width=image_width,
            image_height=image_height,
            body_regions=body_regions,
        )
        target_left = ideal_left
        target_top = choice.text_box.top

        previous_same_side = next(
            (placed for placed in reversed(adjusted) if slot_side(placed.slot) == preferred_side),
            None,
        )
        if previous_same_side is not None:
            tight_gap = max(4, int(round(min(previous_same_side.text_box.height, dimensions.text_height) * 0.03)))
            if choice.text_box.top >= previous_same_side.text_box.bottom:
                target_top = previous_same_side.text_box.bottom + tight_gap
            else:
                target_top = choice.text_box.top
            if preferred_side == "right":
                target_left = previous_same_side.text_box.right - dimensions.text_width
            else:
                target_left = previous_same_side.text_box.left
        elif preferred_side == "left" and right_tops:
            first_left_min_top = min(right_tops) + max(12, int(round(body_regions.face_bbox.height * 0.18)))
            target_top = max(target_top, first_left_min_top)

        searched = search_postprocess_position(
            target_left=target_left,
            target_top=target_top,
            dimensions=dimensions,
            image_width=image_width,
            image_height=image_height,
            body_regions=body_regions,
            placed_text_boxes=[placed.text_box for placed in adjusted],
            preferred_slot=choice.slot,
        )
        final_choice = searched
        if final_choice is None:
            fallback = validate_postprocess_position(
                text_left=choice.text_box.left,
                text_top=choice.text_box.top,
                dimensions=dimensions,
                image_width=image_width,
                image_height=image_height,
                body_regions=body_regions,
                placed_text_boxes=[placed.text_box for placed in adjusted],
                preferred_slot=choice.slot,
                enforce_face_side_gap=False,
            )
            final_choice = fallback if fallback is not None else choice

        if (
            previous_same_side is not None
            and preferred_side == "right"
            and final_choice.text_box.right > previous_same_side.text_box.right
        ):
            aligned_left = previous_same_side.text_box.right - dimensions.text_width
            corrected: PlacementChoice | None = None
            for dy in (0, -8, 8, -16, 16, -24, 24, -40, 40):
                for dx in (0, 8, 16, 24, 32, 40, 48, 56, 64, 72):
                    candidate = validate_postprocess_position(
                        text_left=aligned_left - dx,
                        text_top=final_choice.text_box.top + dy,
                        dimensions=dimensions,
                        image_width=image_width,
                        image_height=image_height,
                        body_regions=body_regions,
                        placed_text_boxes=[placed.text_box for placed in adjusted],
                        preferred_slot=choice.slot,
                        enforce_face_side_gap=False,
                    )
                    if candidate is not None:
                        corrected = candidate
                        break
                if corrected is not None:
                    break
            if corrected is not None:
                final_choice = corrected

        if (
            previous_same_side is not None
            and final_choice.text_box.top < previous_same_side.text_box.top
        ):
            tight_gap = max(4, int(round(min(previous_same_side.text_box.height, dimensions.text_height) * 0.03)))
            corrected = None
            desired_top = previous_same_side.text_box.bottom + tight_gap
            desired_left = final_choice.text_box.left
            if preferred_side == "right":
                desired_left = min(desired_left, previous_same_side.text_box.right - dimensions.text_width)
            elif preferred_side == "left":
                desired_left = max(desired_left, previous_same_side.text_box.left)
            for dy in (0, 8, 16, 24, 32, 40, 56, 72, 96):
                for dx in (0, -8, 8, -16, 16, -24, 24, -40, 40):
                    candidate = validate_postprocess_position(
                        text_left=desired_left + dx,
                        text_top=desired_top + dy,
                        dimensions=dimensions,
                        image_width=image_width,
                        image_height=image_height,
                        body_regions=body_regions,
                        placed_text_boxes=[placed.text_box for placed in adjusted],
                        preferred_slot=choice.slot,
                        enforce_face_side_gap=False,
                    )
                    if candidate is not None:
                        corrected = candidate
                        break
                if corrected is not None:
                    break
            if corrected is not None:
                final_choice = corrected

        final_choice.slot = choice.slot
        final_choice.template = choice.template
        final_choice.bubble_id = choice.bubble_id
        final_choice.sentence_ids = list(choice.sentence_ids)
        final_choice.source = f"{choice.source}+postprocess"
        final_choice.total_score = choice.total_score
        final_choice.penalties = dict(choice.penalties)
        adjusted.append(final_choice)
        if preferred_side == "right":
            right_tops.append(final_choice.text_box.top)

    return adjusted


def generate_candidates(
    *,
    image_width: int,
    image_height: int,
    dimensions: BubbleDimensions,
    body_regions: BodyRegions,
    preferred_slot: str,
) -> list[Candidate]:
    desired_margin = max(32, int(round(min(image_width, image_height) * 0.065)))
    candidates: list[Candidate] = []
    seen: set[tuple[int, int]] = set()

    def add_candidate(text_left: int, text_top: int, source: str) -> None:
        candidate = Candidate(int(round(text_left)), int(round(text_top)), source)
        key = candidate_key(candidate)
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    usable_width = max(1, image_width - dimensions.text_width)
    usable_height = max(1, image_height - dimensions.text_height)
    for fraction_y in GRID_FRACTIONS_Y:
        for fraction_x in GRID_FRACTIONS_X:
            add_candidate(usable_width * fraction_x, usable_height * fraction_y, "grid")

    regions = slot_regions(image_width, image_height, body_regions.person_bbox)
    preferred_quadrant = regions[preferred_slot]
    quadrant_center_left = int(round((preferred_quadrant.left + preferred_quadrant.right - dimensions.text_width) / 2.0))
    quadrant_center_top = int(round((preferred_quadrant.top + preferred_quadrant.bottom - dimensions.text_height) / 2.0))
    if "right" in preferred_slot:
        quadrant_left = preferred_quadrant.right - dimensions.text_width - desired_margin
    else:
        quadrant_left = preferred_quadrant.left + desired_margin
    if "bottom" in preferred_slot:
        quadrant_top = preferred_quadrant.bottom - dimensions.text_height - desired_margin
    else:
        quadrant_top = preferred_quadrant.top + desired_margin
    add_candidate(quadrant_left, quadrant_top, "quadrant-corner")
    add_candidate(quadrant_center_left, quadrant_center_top, "quadrant-center")

    face = body_regions.face_bbox
    face_gap = max(14, int(round(face.height * 0.28)))
    face_center_x = int(round(face.center_x))
    face_center_y = int(round(face.center_y))
    upper_bias = int(round(dimensions.text_height * 0.18))
    add_candidate(face.right + face_gap, face.top - upper_bias, "face-right-upper")
    add_candidate(face.left - face_gap - dimensions.text_width, face.top - upper_bias, "face-left-upper")
    add_candidate(face_center_x - dimensions.text_width // 2, face.top - face_gap - dimensions.text_height, "face-top")
    add_candidate(face.right + face_gap, face_center_y - dimensions.text_height // 2, "face-right")
    add_candidate(face.left - face_gap - dimensions.text_width, face_center_y - dimensions.text_height // 2, "face-left")
    add_candidate(face_center_x - dimensions.text_width // 2, face.bottom + face_gap, "face-bottom")
    return candidates


def evaluate_candidate(
    *,
    candidate: Candidate,
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    preferred_slot: str,
    template_name: str,
    previous_choice: PlacementChoice | None,
    placed_choices: list[PlacementChoice],
    placed_text_boxes: list[Rect],
) -> tuple[PlacementChoice | None, list[str]]:
    text_box, bubble_box, anchor_x, anchor_y = make_layout_boxes_from_text_position(
        dimensions,
        candidate.text_left,
        candidate.text_top,
    )
    invalid_reasons: list[str] = []
    if text_box.left < 0 or text_box.top < 0 or text_box.right > image_width or text_box.bottom > image_height:
        invalid_reasons.append("out_of_bounds")
    if rect_mask_overlap_area(body_regions.face_mask, text_box) > 0:
        invalid_reasons.append("face_overlap")
    if rect_mask_overlap_area(body_regions.chest_mask, text_box) > 0:
        invalid_reasons.append("chest_overlap")
    if rect_mask_overlap_area(body_regions.lower_mask, text_box) > 0:
        invalid_reasons.append("lower_overlap")
    expanded_text_box = expand_rect(text_box, TEXT_CLEARANCE_PX)
    if any(rects_intersect(expanded_text_box, expand_rect(other, TEXT_CLEARANCE_PX)) for other in placed_text_boxes):
        invalid_reasons.append("text_overlap")
    if invalid_reasons:
        return None, invalid_reasons

    penalties: dict[str, float] = {}
    desired_margin = max(32, int(round(min(image_width, image_height) * 0.065)))
    ideal_margin = max(56, int(round(min(image_width, image_height) * 0.11)))
    edge_deficit = (
        max(0, desired_margin - text_box.left)
        + max(0, desired_margin - text_box.top)
        + max(0, desired_margin - (image_width - text_box.right))
        + max(0, desired_margin - (image_height - text_box.bottom))
    )
    if edge_deficit > 0:
        penalties["text_edge_margin"] = edge_deficit * TEXT_EDGE_MARGIN_WEIGHT
    edge_ideal_deficit = (
        max(0, ideal_margin - text_box.left)
        + max(0, ideal_margin - text_box.top)
        + max(0, ideal_margin - (image_width - text_box.right))
        + max(0, ideal_margin - (image_height - text_box.bottom))
    )
    if edge_ideal_deficit > 0:
        penalties["text_edge_ideal_margin"] = edge_ideal_deficit * IDEAL_EDGE_MARGIN_WEIGHT
    outer_edge_margin = image_width - text_box.right if "right" in preferred_slot else text_box.left
    outer_edge_target = max(56, int(round(min(image_width, image_height) * 0.09)))
    if outer_edge_margin < outer_edge_target:
        penalties["outer_edge_margin"] = (outer_edge_target - outer_edge_margin) * OUTER_EDGE_MARGIN_WEIGHT

    person_overlap_ratio = rect_mask_overlap_area(body_regions.person_mask, text_box) / max(1, text_box.area)
    if person_overlap_ratio > 0:
        penalties["text_person_overlap"] = person_overlap_ratio * PERSON_OVERLAP_WEIGHT

    bubble_person_overlap_ratio = rect_mask_overlap_area(body_regions.person_mask, bubble_box) / max(1, bubble_box.area)
    if bubble_person_overlap_ratio > 0:
        penalties["bubble_shell_person_overlap"] = bubble_person_overlap_ratio * BUBBLE_SHELL_PERSON_WEIGHT

    bubble_face_overlap_ratio = rect_mask_overlap_area(body_regions.face_mask, bubble_box) / max(1, bubble_box.area)
    bubble_chest_overlap_ratio = rect_mask_overlap_area(body_regions.chest_mask, bubble_box) / max(1, bubble_box.area)
    bubble_lower_overlap_ratio = rect_mask_overlap_area(body_regions.lower_mask, bubble_box) / max(1, bubble_box.area)
    critical_shell_overlap = bubble_face_overlap_ratio + bubble_chest_overlap_ratio + bubble_lower_overlap_ratio
    if critical_shell_overlap > 0:
        penalties["bubble_shell_critical_overlap"] = critical_shell_overlap * BUBBLE_SHELL_CRITICAL_WEIGHT

    face_gap = rect_distance(text_box, body_regions.face_bbox)
    min_face_gap = max(14, int(round(body_regions.face_bbox.height * 0.20)))
    max_face_gap = max(
        int(round(body_regions.face_bbox.height * 1.7)),
        int(round(max(dimensions.text_width, dimensions.text_height) * 1.15)),
    )
    if face_gap < min_face_gap:
        penalties["face_too_near"] = (min_face_gap - face_gap) * FACE_NEAR_WEIGHT
    if face_gap > max_face_gap:
        penalties["face_too_far"] = (face_gap - max_face_gap) * FACE_FAR_WEIGHT
    if "right" in preferred_slot:
        side_gap = text_box.left - body_regions.face_bbox.right
    else:
        side_gap = body_regions.face_bbox.left - text_box.right
    desired_face_side_gap = max(18, int(round(body_regions.face_bbox.width * 0.22)))
    if side_gap > desired_face_side_gap:
        penalties["face_side_gap"] = (side_gap - desired_face_side_gap) * FACE_SIDE_GAP_WEIGHT
    quadrants = slot_regions(image_width, image_height, body_regions.person_bbox)
    quadrant = quadrants[preferred_slot]
    if not point_in_rect(text_box.center_x, text_box.center_y, quadrant):
        penalties["quadrant_mismatch"] = QUADRANT_MISMATCH_PENALTY
    penalties["quadrant_distance"] = (
        math.hypot(
            text_box.center_x - quadrant.center_x,
            text_box.center_y - quadrant.center_y,
        )
        * QUADRANT_DISTANCE_WEIGHT
    )

    face_side_target = 1 if "right" in preferred_slot else -1
    face_vertical_target = -1 if "top" in preferred_slot else 1 if "bottom" in preferred_slot else 0
    side_offset = text_box.center_x - body_regions.face_bbox.center_x
    vertical_offset = text_box.center_y - body_regions.face_bbox.center_y
    if side_offset * face_side_target < 0:
        penalties["face_side_bias"] = abs(side_offset) * FACE_SLOT_SIDE_WEIGHT
    if face_vertical_target != 0 and vertical_offset * face_vertical_target < 0:
        penalties["face_height_bias"] = abs(vertical_offset) * FACE_SLOT_HEIGHT_WEIGHT

    if previous_choice is not None:
        target_distance = max(previous_choice.text_box.height, text_box.height) * 0.68
        actual_distance = math.hypot(
            text_box.center_x - previous_choice.text_box.center_x,
            text_box.center_y - previous_choice.text_box.center_y,
        )
        if actual_distance > target_distance:
            penalties["continuity"] = (actual_distance - target_distance) * CONTINUITY_DISTANCE_WEIGHT
        relation = slot_expected_relation(previous_choice.slot, preferred_slot)
        tolerance = max(18, min(text_box.width, text_box.height) // 8)
        if relation == "down":
            drift = previous_choice.text_box.center_y + tolerance - text_box.center_y
            if drift > 0:
                penalties["flow_direction"] = drift * FLOW_DIRECTION_WEIGHT
        elif relation == "left":
            drift = text_box.center_x - (previous_choice.text_box.center_x - tolerance)
            if drift > 0:
                penalties["flow_direction"] = drift * FLOW_DIRECTION_WEIGHT
        else:
            if text_box.center_x > previous_choice.text_box.center_x + tolerance:
                penalties["flow_direction"] = (
                    text_box.center_x - previous_choice.text_box.center_x - tolerance
                ) * FLOW_DIRECTION_WEIGHT

        x_tolerance = max(previous_choice.text_box.width, text_box.width) * 0.35
        y_tolerance = max(previous_choice.text_box.height, text_box.height) * 0.16
        x_delta = previous_choice.text_box.center_x - text_box.center_x
        y_delta = text_box.center_y - previous_choice.text_box.center_y
        horizontal_gap = previous_choice.text_box.left - text_box.right
        vertical_gap = text_box.top - previous_choice.text_box.bottom
        same_side = slot_side(previous_choice.slot) == slot_side(preferred_slot)
        if x_delta < -x_tolerance:
            penalties["reading_rightward"] = (-x_delta - x_tolerance) * READING_RIGHTWARD_WEIGHT
        same_column = abs(x_delta) <= max(previous_choice.text_box.width, text_box.width) * 0.7
        if same_column:
            if y_delta < -y_tolerance:
                penalties["reading_upward"] = (-y_delta - y_tolerance) * READING_UPWARD_WEIGHT
            desired_vertical_gap = max(4, int(round(min(previous_choice.text_box.height, text_box.height) * 0.04)))
            if vertical_gap > desired_vertical_gap:
                penalties["same_column_gap"] = (vertical_gap - desired_vertical_gap) * SAME_COLUMN_GAP_WEIGHT
        elif x_delta > x_tolerance:
            allowed_reset = max(previous_choice.text_box.height, text_box.height) * 0.35
            if y_delta < -allowed_reset:
                penalties["reading_column_reset_upward"] = (
                    -y_delta - allowed_reset
                ) * READING_COLUMN_RESET_UPWARD_WEIGHT
            desired_horizontal_gap = max(6, int(round(min(previous_choice.text_box.width, text_box.width) * 0.10)))
            if horizontal_gap > desired_horizontal_gap:
                penalties["next_column_gap"] = (horizontal_gap - desired_horizontal_gap) * NEXT_COLUMN_GAP_WEIGHT
            allowed_reset_downward = max(previous_choice.text_box.height, text_box.height) * 0.20
            reset_downward = text_box.top - previous_choice.text_box.top
            if reset_downward > allowed_reset_downward:
                penalties["next_column_reset_downward"] = (
                    reset_downward - allowed_reset_downward
                ) * NEXT_COLUMN_RESET_DOWNWARD_WEIGHT
        if same_side:
            if slot_side(preferred_slot) == "right":
                alignment_gap = abs(text_box.right - previous_choice.text_box.right)
            else:
                alignment_gap = abs(text_box.left - previous_choice.text_box.left)
            if alignment_gap > 0:
                penalties["same_side_alignment"] = alignment_gap * SAME_SIDE_ALIGN_WEIGHT
            if text_box.top < previous_choice.text_box.top:
                penalties["same_side_start_above"] = (
                    previous_choice.text_box.top - text_box.top
                ) * READING_UPWARD_WEIGHT

    if slot_side(preferred_slot) == "left":
        right_choices = [choice for choice in placed_choices if slot_side(choice.slot) == "right"]
        if right_choices:
            right_top = min(choice.text_box.top for choice in right_choices)
            if text_box.top < right_top:
                penalties["left_starts_above_right"] = (
                    right_top - text_box.top
                ) * LEFT_START_BELOW_RIGHT_WEIGHT

    total_score = sum(penalties.values())
    return (
        PlacementChoice(
            bubble_id=dimensions.bubble_id,
            sentence_ids=list(dimensions.sentence_ids),
            anchor_x_px=anchor_x,
            anchor_y_px=anchor_y,
            text_box=text_box,
            bubble_box=bubble_box,
            total_score=total_score,
            penalties=penalties,
            source=candidate.source,
            template=template_name,
            slot=preferred_slot,
        ),
        [],
    )


def choose_template_attempt(
    *,
    template_name: str,
    reflow_plans: list[ReflowBubblePlan],
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    font_size: int,
) -> TemplateAttempt:
    slots = TEMPLATE_SLOTS[template_name].get(len(reflow_plans))
    if slots is None:
        raise RuntimeError(f"unsupported bubble count for template {template_name}: {len(reflow_plans)}")
    bubble_attempts: list[BubbleAttempt] = []
    beam: list[BeamState] = [BeamState(placements=[], text_boxes=[], total_score=0.0)]

    for index, plan in enumerate(reflow_plans):
        slot = slots[index]
        dimensions = estimate_bubble_dimensions(
            plan,
            image_width=image_width,
            image_height=image_height,
            font_size=font_size,
        )
        base_candidates = generate_candidates(
            image_width=image_width,
            image_height=image_height,
            dimensions=dimensions,
            body_regions=body_regions,
            preferred_slot=slot,
        )
        invalid_counts: dict[str, int] = {}
        expanded_states: list[BeamState] = []
        valid_choices: list[PlacementChoice] = []

        for state in beam:
            state_candidates = generate_state_candidates(
                base_candidates=base_candidates,
                dimensions=dimensions,
                image_width=image_width,
                image_height=image_height,
                body_regions=body_regions,
                preferred_slot=slot,
                placed_choices=state.placements,
            )
            for candidate in state_candidates:
                choice, invalid_reasons = evaluate_candidate(
                    candidate=candidate,
                    dimensions=dimensions,
                    image_width=image_width,
                    image_height=image_height,
                    body_regions=body_regions,
                    preferred_slot=slot,
                    template_name=template_name,
                    previous_choice=state.placements[-1] if state.placements else None,
                    placed_choices=state.placements,
                    placed_text_boxes=state.text_boxes,
                )
                if choice is None:
                    for reason in invalid_reasons:
                        invalid_counts[reason] = invalid_counts.get(reason, 0) + 1
                    continue
                total_score = state.total_score + choice.total_score
                expanded_states.append(
                    BeamState(
                        placements=[*state.placements, choice],
                        text_boxes=[*state.text_boxes, choice.text_box],
                        total_score=total_score,
                    )
                )
                valid_choices.append(
                    PlacementChoice(
                        bubble_id=choice.bubble_id,
                        sentence_ids=list(choice.sentence_ids),
                        anchor_x_px=choice.anchor_x_px,
                        anchor_y_px=choice.anchor_y_px,
                        text_box=choice.text_box,
                        bubble_box=choice.bubble_box,
                        total_score=total_score,
                        penalties=dict(choice.penalties),
                        source=choice.source,
                        template=choice.template,
                        slot=choice.slot,
                    )
                )

        expanded_states.sort(
            key=lambda state: (
                state.total_score,
                state.placements[-1].text_box.top,
                -state.placements[-1].text_box.left,
            )
        )
        beam = expanded_states[:BEAM_WIDTH]
        valid_choices.sort(key=lambda item: (item.total_score, item.text_box.top, -item.text_box.left))
        top_candidates = [
            {
                "source": choice.source,
                "text_box": choice.text_box.as_dict(),
                "bubble_box": choice.bubble_box.as_dict(),
                "anchor_x_px": choice.anchor_x_px,
                "anchor_y_px": choice.anchor_y_px,
                "total_score": round(choice.total_score, 3),
                "penalties": {key: round(value, 3) for key, value in choice.penalties.items()},
            }
            for choice in valid_choices[:5]
        ]
        if not valid_choices:
            bubble_attempts.append(
                BubbleAttempt(
                    bubble_id=plan.bubble_id,
                    slot=slot,
                    total_candidates=len(base_candidates),
                    valid_candidates=0,
                    invalid_counts=invalid_counts,
                    top_candidates=[],
                    selected=None,
                )
            )
            return TemplateAttempt(
                name=template_name,
                success=False,
                total_score=None,
                placements=beam[0].placements if beam else [],
                failures=[f"no valid candidate for {plan.bubble_id} in slot {slot}"],
                bubbles=bubble_attempts,
            )

        selected = beam[0].placements[-1]
        bubble_attempts.append(
                BubbleAttempt(
                    bubble_id=plan.bubble_id,
                    slot=slot,
                    total_candidates=len(base_candidates),
                    valid_candidates=len(valid_choices),
                invalid_counts=invalid_counts,
                top_candidates=top_candidates,
                selected=selected.to_debug_dict(),
            )
        )

    return TemplateAttempt(
        name=template_name,
        success=True,
        total_score=beam[0].total_score,
        placements=beam[0].placements,
        failures=[],
        bubbles=bubble_attempts,
    )


def solve_scene_layout(
    *,
    reflow_plans: list[ReflowBubblePlan],
    image_width: int,
    image_height: int,
    face_mask: np.ndarray,
    person_mask: np.ndarray,
    chest_mask: np.ndarray | None = None,
    lower_mask: np.ndarray | None = None,
    font_size: int,
) -> PlacementSolution:
    if not reflow_plans:
        raise RuntimeError("reflow plans are required")
    if len(reflow_plans) > 5:
        raise RuntimeError("PoC supports at most 5 bubbles")

    body_regions = build_body_regions(person_mask, face_mask, chest_mask=chest_mask, lower_mask=lower_mask)
    dimensions_by_bubble_id = {
        plan.bubble_id: estimate_bubble_dimensions(
            plan,
            image_width=image_width,
            image_height=image_height,
            font_size=font_size,
        )
        for plan in reflow_plans
    }
    attempts = [
        choose_template_attempt(
            template_name=template_name,
            reflow_plans=reflow_plans,
            image_width=image_width,
            image_height=image_height,
            body_regions=body_regions,
            font_size=font_size,
        )
        for template_name in TEMPLATE_SLOTS
    ]
    successful = [attempt for attempt in attempts if attempt.success and attempt.total_score is not None]
    if not successful:
        debug_payload = {
            "image_width": image_width,
            "image_height": image_height,
            "font_size": font_size,
            "body_regions": body_regions.to_debug_dict(),
            "dimensions": [
                estimate_bubble_dimensions(
                    plan,
                    image_width=image_width,
                    image_height=image_height,
                    font_size=font_size,
                ).to_debug_dict()
                for plan in reflow_plans
            ],
            "templates": [attempt.to_debug_dict() for attempt in attempts],
        }
        raise RuntimeError(json.dumps(debug_payload, ensure_ascii=False, indent=2))

    best_attempt = min(successful, key=lambda attempt: (attempt.total_score, attempt.name))
    final_placements = postprocess_placements(
        placements=best_attempt.placements,
        dimensions_by_bubble_id=dimensions_by_bubble_id,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
    )
    scene_plans = [
        SceneBubblePlan(
            bubble_id=choice.bubble_id,
            anchor_x=choice.anchor_x_px / image_width,
            anchor_y=choice.anchor_y_px / image_height,
            sentence_ids=list(choice.sentence_ids),
        )
        for choice in final_placements
    ]
    debug_payload = {
        "selected_template": best_attempt.name,
        "image_width": image_width,
        "image_height": image_height,
        "font_size": font_size,
        "body_regions": body_regions.to_debug_dict(),
        "dimensions": [
            estimate_bubble_dimensions(
                plan,
                image_width=image_width,
                image_height=image_height,
                font_size=font_size,
            ).to_debug_dict()
            for plan in reflow_plans
        ],
        "templates": [attempt.to_debug_dict() for attempt in attempts],
        "postprocessed_placements": [placement.to_debug_dict() for placement in final_placements],
    }
    return PlacementSolution(
        selected_template=best_attempt.name,
        scene_plans=scene_plans,
        placements=best_attempt.placements,
        debug_payload=debug_payload,
    )


def render_debug_overlay(
    *,
    image_path: Path,
    output_path: Path,
    solution: PlacementSolution | None,
    person_mask: np.ndarray,
    face_mask: np.ndarray,
    body_regions: BodyRegions,
    head_mask: np.ndarray | None = None,
) -> None:
    base = Image.open(image_path).convert("RGBA")
    overlay = base.copy()
    rgba = np.asarray(overlay, dtype=np.uint8).copy()

    def apply_mask(mask: np.ndarray, color: tuple[int, int, int], alpha: int) -> None:
        color_array = np.array(color, dtype=np.uint8)
        mask_bool = mask.astype(bool)
        rgba[mask_bool, :3] = (
            rgba[mask_bool, :3].astype(np.uint16) * (255 - alpha) + color_array.astype(np.uint16) * alpha
        ) // 255

    apply_mask(person_mask, (70, 150, 255), 48)
    apply_mask(face_mask, (255, 70, 70), 90)
    apply_mask(body_regions.chest_mask, (255, 215, 0), 90)
    apply_mask(body_regions.lower_mask, (190, 40, 220), 90)
    if head_mask is not None:
        apply_mask(head_mask, (40, 210, 110), 86)
    overlay = Image.fromarray(rgba, mode="RGBA")

    draw = ImageDraw.Draw(overlay)
    draw.rectangle(body_regions.person_bbox.as_tuple(), outline=(70, 150, 255, 220), width=3)
    draw.rectangle(body_regions.face_bbox.as_tuple(), outline=(255, 70, 70, 240), width=3)
    draw.rectangle(body_regions.chest_bbox.as_tuple(), outline=(255, 215, 0, 240), width=3)
    draw.rectangle(body_regions.lower_bbox.as_tuple(), outline=(190, 40, 220, 240), width=3)
    if head_mask is not None and np.any(head_mask):
        draw.rectangle(bbox_from_mask(head_mask).as_tuple(), outline=(40, 210, 110, 240), width=3)

    if solution is not None:
        for index, placement in enumerate(solution.placements, start=1):
            draw.rounded_rectangle(
                (
                    placement.bubble_box.left,
                    placement.bubble_box.top,
                    placement.bubble_box.right,
                    placement.bubble_box.bottom,
                ),
                radius=16,
                outline=(20, 220, 90, 255),
                width=4,
            )
            draw.rectangle(
                (
                    placement.text_box.left,
                    placement.text_box.top,
                    placement.text_box.right,
                    placement.text_box.bottom,
                ),
                outline=(20, 20, 20, 255),
                width=2,
            )
            label = f"{index}:{placement.bubble_id} {placement.slot}"
            draw.text(
                (placement.bubble_box.left + 8, max(0, placement.bubble_box.top - 18)),
                label,
                fill=(20, 20, 20, 255),
            )
            draw.ellipse(
                (
                    placement.anchor_x_px - 4,
                    placement.anchor_y_px - 4,
                    placement.anchor_x_px + 4,
                    placement.anchor_y_px + 4,
                ),
                fill=(20, 220, 90, 255),
                outline=(20, 20, 20, 255),
                width=1,
            )
            draw.text(
                (placement.text_box.left, min(overlay.height - 18, placement.text_box.bottom + 4)),
                f"a=({placement.anchor_x_px},{placement.anchor_y_px})",
                fill=(20, 20, 20, 255),
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    overlay.save(output_path, compress_level=1, optimize=False)


def solve_from_paths(
    *,
    image_path: Path,
    reflow_plans: list[ReflowBubblePlan],
    face_mask_path: Path,
    person_mask_path: Path,
    chest_mask_path: Path | None,
    lower_mask_path: Path | None,
    font_size: int,
) -> tuple[PlacementSolution, BodyRegions, np.ndarray, np.ndarray]:
    image = Image.open(image_path)
    image_width, image_height = image.size
    face_mask = load_binary_mask(face_mask_path)
    person_mask = load_binary_mask(person_mask_path)
    chest_mask = load_binary_mask(chest_mask_path) if chest_mask_path is not None else None
    lower_mask = load_binary_mask(lower_mask_path) if lower_mask_path is not None else None
    if face_mask.shape != (image_height, image_width):
        raise RuntimeError("face mask size does not match image size")
    if person_mask.shape != (image_height, image_width):
        raise RuntimeError("person mask size does not match image size")
    if chest_mask is not None and chest_mask.shape != (image_height, image_width):
        raise RuntimeError("chest mask size does not match image size")
    if lower_mask is not None and lower_mask.shape != (image_height, image_width):
        raise RuntimeError("lower mask size does not match image size")
    body_regions = build_body_regions(person_mask, face_mask, chest_mask=chest_mask, lower_mask=lower_mask)
    solution = solve_scene_layout(
        reflow_plans=reflow_plans,
        image_width=image_width,
        image_height=image_height,
        face_mask=face_mask,
        person_mask=person_mask,
        chest_mask=chest_mask,
        lower_mask=lower_mask,
        font_size=font_size,
    )
    return solution, body_regions, person_mask, face_mask


def default_font_size(image_height: int) -> int:
    return max(22, min(48, image_height // DEFAULT_FONT_DIVISOR))
