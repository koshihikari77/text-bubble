from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
from ortools.sat.python import cp_model
from PIL import Image

from bubble.models import ReflowBubblePlan, SceneBubblePlan
from bubble.experimental.beam_search_scene_solver import (
    BUBBLE_SHELL_CRITICAL_WEIGHT,
    BUBBLE_SHELL_PERSON_WEIGHT,
    FACE_FAR_WEIGHT,
    FACE_NEAR_WEIGHT,
    IDEAL_EDGE_MARGIN_WEIGHT,
    PERSON_OVERLAP_WEIGHT,
    TEXT_CLEARANCE_PX,
    TEXT_EDGE_MARGIN_WEIGHT,
    BodyRegions,
    BubbleDimensions,
    Candidate,
    PlacementChoice,
    PlacementSolution,
    Rect,
    build_body_regions,
    candidate_key,
    default_font_size,
    estimate_bubble_dimensions,
    expand_rect,
    load_binary_mask,
    make_layout_boxes_from_text_position,
    rect_distance,
    rect_mask_overlap_area,
    rects_intersect,
    render_debug_overlay,
    slot_regions,
)


READING_MODEL = "rtl-columns"
OBJECTIVE_SCALE = 100
MAX_SOLVE_SECONDS = float(os.environ.get("TEXT_BUBBLE_CP_SAT_MAX_SECONDS", "20.0"))
NUM_SEARCH_WORKERS = max(1, int(os.environ.get("TEXT_BUBBLE_CP_SAT_WORKERS", "1")))
SEARCH_MAX_DIM = max(1, int(os.environ.get("TEXT_BUBBLE_CP_SAT_SEARCH_MAX_DIM", "768")))
MAX_CANDIDATES_PER_BIN = 8
MAX_CANDIDATES_PER_BUBBLE = 96
COARSE_BIN_DIVISIONS = 3

CONTINUITY_DISTANCE_WEIGHT = 1.8
X_BACKTRACK_WEIGHT = 16.0
SAME_COLUMN_STACK_WEIGHT = 8.0
SAME_COLUMN_GAP_WEIGHT = 4.0
COLUMN_RESET_UPWARD_WEIGHT = 6.4
CROWDING_WEIGHT = 8.0
HORIZONTAL_SPAN_DEFICIT_WEIGHT = 4.0
VERTICAL_SPAN_DEFICIT_WEIGHT = 1.4
VERTICAL_CENTER_SPAN_DEFICIT_WEIGHT = 3.4
CHEST_SHELL_OVERLAP_WEIGHT = 1400.0
CHEST_NEAR_WEIGHT = 18.0
MAX_CHEST_SHELL_OVERLAP_RATIO = 0.015
LARGE_UPWARD_RESET_WEIGHT = 7.5
MAX_LARGE_COLUMN_UPWARD_RESET_RATIO = 0.18
TEXT_PERSON_OVERLAP_MULTIPLIER = 1.35
BUBBLE_SHELL_PERSON_OVERLAP_MULTIPLIER = 1.6
HEAD_SHELL_OVERLAP_WEIGHT = 760.0
HEAD_TEXT_OVERLAP_WEIGHT = 1400.0
MAX_HEAD_TEXT_OVERLAP_RATIO = 0.30
MAX_HEAD_SHELL_OVERLAP_RATIO = 0.50
MAX_SAME_SIDE_UPWARD_RESET_RATIO = 0.14
@dataclass(frozen=True)
class CandidateOption:
    index: int
    candidate: Candidate
    choice: PlacementChoice
    unary_penalties: dict[str, float]
    unary_score: int

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "source": self.candidate.source,
            "text_box": self.choice.text_box.as_dict(),
            "bubble_box": self.choice.bubble_box.as_dict(),
            "anchor_x_px": self.choice.anchor_x_px,
            "anchor_y_px": self.choice.anchor_y_px,
            "unary_score": self.unary_score,
            "derived_region": self.choice.slot,
            "penalties": {key: round(value, 3) for key, value in self.unary_penalties.items()},
        }


@dataclass(frozen=True)
class BinaryMaskStats:
    mask: np.ndarray
    sat: np.ndarray

    @classmethod
    def from_mask(cls, mask: np.ndarray) -> "BinaryMaskStats":
        mask_u8 = mask.astype(np.uint8, copy=False)
        sat = np.pad(mask_u8, ((1, 0), (1, 0)), mode="constant").cumsum(axis=0, dtype=np.uint32).cumsum(
            axis=1,
            dtype=np.uint32,
        )
        return cls(mask=mask.astype(bool, copy=False), sat=sat)

    def overlap_area(self, rect: Rect) -> int:
        clipped = Rect(
            max(0, min(self.mask.shape[1], rect.left)),
            max(0, min(self.mask.shape[0], rect.top)),
            max(0, min(self.mask.shape[1], rect.right)),
            max(0, min(self.mask.shape[0], rect.bottom)),
        )
        if clipped.area <= 0:
            return 0
        return int(
            int(self.sat[clipped.bottom, clipped.right])
            - int(self.sat[clipped.top, clipped.right])
            - int(self.sat[clipped.bottom, clipped.left])
            + int(self.sat[clipped.top, clipped.left])
        )

    def overlap_areas(
        self,
        lefts: np.ndarray,
        tops: np.ndarray,
        rights: np.ndarray,
        bottoms: np.ndarray,
    ) -> np.ndarray:
        width = self.mask.shape[1]
        height = self.mask.shape[0]
        clipped_lefts = np.clip(lefts, 0, width).astype(np.intp, copy=False)
        clipped_tops = np.clip(tops, 0, height).astype(np.intp, copy=False)
        clipped_rights = np.clip(rights, 0, width).astype(np.intp, copy=False)
        clipped_bottoms = np.clip(bottoms, 0, height).astype(np.intp, copy=False)
        valid = (clipped_rights > clipped_lefts) & (clipped_bottoms > clipped_tops)
        out = np.zeros(lefts.shape, dtype=np.int64)
        if not np.any(valid):
            return out
        sat = self.sat
        out[valid] = (
            sat[clipped_bottoms[valid], clipped_rights[valid]].astype(np.int64)
            - sat[clipped_tops[valid], clipped_rights[valid]].astype(np.int64)
            - sat[clipped_bottoms[valid], clipped_lefts[valid]].astype(np.int64)
            + sat[clipped_tops[valid], clipped_lefts[valid]].astype(np.int64)
        )
        return out


@dataclass(frozen=True)
class MaskStatsBundle:
    person: BinaryMaskStats
    face: BinaryMaskStats
    chest: BinaryMaskStats
    lower: BinaryMaskStats
    head: BinaryMaskStats | None


def _scaled_score(value: float) -> int:
    return max(0, int(round(value * OBJECTIVE_SCALE)))


def _slot_row(slot: str) -> str:
    if slot.startswith("top-"):
        return "top"
    if slot.startswith("mid-"):
        return "mid"
    return "bottom"


def _slot_side(slot: str) -> str:
    return "right" if slot.endswith("right") else "left"


def _derive_region(*, text_box: Rect, image_width: int, image_height: int, body_regions: BodyRegions) -> str:
    for name, rect in slot_regions(image_width, image_height, body_regions.person_bbox).items():
        if rect.left <= text_box.center_x < rect.right and rect.top <= text_box.center_y < rect.bottom:
            return name
    row = "top" if text_box.center_y < image_height / 3.0 else "mid" if text_box.center_y < (image_height * 2.0 / 3.0) else "bottom"
    side = "right" if text_box.center_x >= body_regions.person_bbox.center_x else "left"
    return f"{row}-{side}"


def _iter_positions(max_pos: int, step: int) -> list[int]:
    if max_pos <= 0:
        return [0]
    positions = list(range(0, max_pos + 1, max(1, step)))
    if positions[-1] != max_pos:
        positions.append(max_pos)
    return positions


def _coarse_bin_key(*, center_x: float, center_y: float, image_width: int, image_height: int) -> tuple[int, int]:
    cell_x = min(COARSE_BIN_DIVISIONS - 1, max(0, int(center_x * COARSE_BIN_DIVISIONS / max(1, image_width))))
    cell_y = min(COARSE_BIN_DIVISIONS - 1, max(0, int(center_y * COARSE_BIN_DIVISIONS / max(1, image_height))))
    return cell_x, cell_y


def _candidate_limits(total_bubbles: int) -> tuple[int, int]:
    if total_bubbles >= 5:
        return 6, 72
    if total_bubbles >= 4:
        return 6, 72
    return MAX_CANDIDATES_PER_BIN, MAX_CANDIDATES_PER_BUBBLE


def _search_scale(image_width: int, image_height: int) -> float:
    max_dim = max(image_width, image_height)
    if max_dim <= SEARCH_MAX_DIM:
        return 1.0
    return SEARCH_MAX_DIM / max_dim


def _resize_binary_mask(mask: np.ndarray | None, scale: float) -> np.ndarray | None:
    if mask is None or scale >= 0.999:
        return mask
    image = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
    resized = image.resize(
        (
            max(1, int(round(mask.shape[1] * scale))),
            max(1, int(round(mask.shape[0] * scale))),
        ),
        resample=Image.Resampling.NEAREST,
    )
    return np.asarray(resized, dtype=np.uint8) > 0


def _scale_dimensions(dimensions: BubbleDimensions, scale: float) -> BubbleDimensions:
    if scale >= 0.999:
        return dimensions

    def scaled(value: int) -> int:
        return max(1, int(round(value * scale)))

    return BubbleDimensions(
        bubble_id=dimensions.bubble_id,
        sentence_ids=list(dimensions.sentence_ids),
        columns=list(dimensions.columns),
        font_size=max(1, int(round(dimensions.font_size * scale))),
        text_width=scaled(dimensions.text_width),
        text_height=scaled(dimensions.text_height),
        bubble_width=scaled(dimensions.bubble_width),
        bubble_height=scaled(dimensions.bubble_height),
        text_offset_x=scaled(dimensions.text_offset_x),
        text_offset_y=scaled(dimensions.text_offset_y),
        anchor_offset_x=scaled(dimensions.anchor_offset_x),
        anchor_offset_y=scaled(dimensions.anchor_offset_y),
    )


def _build_mask_stats(body_regions: BodyRegions, head_mask: np.ndarray | None) -> MaskStatsBundle:
    return MaskStatsBundle(
        person=BinaryMaskStats.from_mask(body_regions.person_mask),
        face=BinaryMaskStats.from_mask(body_regions.face_mask),
        chest=BinaryMaskStats.from_mask(body_regions.chest_mask),
        lower=BinaryMaskStats.from_mask(body_regions.lower_mask),
        head=BinaryMaskStats.from_mask(head_mask) if head_mask is not None else None,
    )


def _edge_seed_positions(*, dimensions: BubbleDimensions, image_width: int, image_height: int, body_regions: BodyRegions) -> list[tuple[int, int, str]]:
    max_left = max(0, image_width - dimensions.text_width)
    max_top = max(0, image_height - dimensions.text_height)
    outer_margin = max(24, int(round(min(image_width, image_height) * 0.06)))
    lefts = [
        0,
        min(max_left, outer_margin),
        max(0, min(max_left, image_width - outer_margin - dimensions.text_width)),
        max_left,
    ]
    tops = [
        0,
        min(max_top, outer_margin),
        max(0, min(max_top, image_height - outer_margin - dimensions.text_height)),
        max_top,
    ]

    seeds: list[tuple[int, int, str]] = []
    for text_left in lefts:
        for text_top in tops:
            seeds.append((text_left, text_top, "edge-seed"))

    person = body_regions.person_bbox
    face = body_regions.face_bbox
    person_gap = max(18, int(round(min(image_width, image_height) * 0.04)))
    person_lefts = [
        max(0, person.left - person_gap - dimensions.text_width),
        min(max_left, person.right + person_gap),
    ]
    person_tops = [
        max(0, person.top - person_gap - dimensions.text_height),
        min(max_top, person.bottom + person_gap),
    ]
    for text_left in person_lefts:
        for text_top in tops + person_tops:
            seeds.append((text_left, text_top, "person-periphery"))
    for text_top in person_tops:
        for text_left in lefts:
            seeds.append((text_left, text_top, "person-periphery"))

    face_gap = max(18, int(round(face.height * 0.24)))
    face_lefts = [
        max(0, face.left - face_gap - dimensions.text_width),
        min(max_left, face.right + face_gap),
    ]
    face_tops = [
        max(0, face.top - face_gap - dimensions.text_height),
        min(max_top, face.bottom + face_gap),
    ]
    for text_left in face_lefts:
        for text_top in face_tops:
            seeds.append((text_left, text_top, "face-periphery"))
    return seeds


def _local_refinement_offsets(*, dimensions: BubbleDimensions) -> list[tuple[int, int]]:
    step_x = max(8, dimensions.text_width // 3)
    step_y = max(8, dimensions.text_height // 6)
    return [
        (-step_x, 0),
        (step_x, 0),
        (0, -step_y),
        (0, step_y),
        (-step_x, -step_y),
        (step_x, -step_y),
        (-step_x, step_y),
        (step_x, step_y),
    ]


def _build_choice(
    *,
    dimensions: BubbleDimensions,
    text_left: int,
    text_top: int,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    mask_stats: MaskStatsBundle,
) -> tuple[PlacementChoice | None, list[str]]:
    text_box, bubble_box, anchor_x, anchor_y = make_layout_boxes_from_text_position(
        dimensions,
        int(round(text_left)),
        int(round(text_top)),
    )
    invalid_reasons: list[str] = []
    if text_box.left < 0 or text_box.top < 0 or text_box.right > image_width or text_box.bottom > image_height:
        invalid_reasons.append("out_of_bounds")
    if mask_stats.face.overlap_area(text_box) > 0:
        invalid_reasons.append("face_overlap")
    if mask_stats.chest.overlap_area(text_box) > 0:
        invalid_reasons.append("chest_overlap")
    if mask_stats.lower.overlap_area(text_box) > 0:
        invalid_reasons.append("lower_overlap")
    head_text_overlap = mask_stats.head.overlap_area(text_box) / max(1, text_box.area) if mask_stats.head is not None else 0.0
    if head_text_overlap > MAX_HEAD_TEXT_OVERLAP_RATIO:
        invalid_reasons.append("head_keepout_overlap")
    if invalid_reasons:
        return None, invalid_reasons

    penalties: dict[str, float] = {}
    desired_margin = max(24, int(round(min(image_width, image_height) * 0.05)))
    ideal_margin = max(40, int(round(min(image_width, image_height) * 0.09)))
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

    person_overlap_ratio = mask_stats.person.overlap_area(text_box) / max(1, text_box.area)
    if person_overlap_ratio > 0:
        penalties["text_person_overlap"] = (
            person_overlap_ratio * PERSON_OVERLAP_WEIGHT * TEXT_PERSON_OVERLAP_MULTIPLIER
        )
    if head_text_overlap > 0:
        penalties["text_head_overlap"] = head_text_overlap * HEAD_TEXT_OVERLAP_WEIGHT

    bubble_person_overlap_ratio = mask_stats.person.overlap_area(bubble_box) / max(1, bubble_box.area)
    if bubble_person_overlap_ratio > 0:
        penalties["bubble_shell_person_overlap"] = (
            bubble_person_overlap_ratio
            * BUBBLE_SHELL_PERSON_WEIGHT
            * BUBBLE_SHELL_PERSON_OVERLAP_MULTIPLIER
        )

    face_shell_overlap = mask_stats.face.overlap_area(bubble_box) / max(1, bubble_box.area)
    chest_shell_overlap = mask_stats.chest.overlap_area(bubble_box) / max(1, bubble_box.area)
    lower_shell_overlap = mask_stats.lower.overlap_area(bubble_box) / max(1, bubble_box.area)
    head_shell_overlap = mask_stats.head.overlap_area(bubble_box) / max(1, bubble_box.area) if mask_stats.head is not None else 0.0
    if chest_shell_overlap > MAX_CHEST_SHELL_OVERLAP_RATIO:
        invalid_reasons.append("chest_shell_overlap")
    if head_shell_overlap > MAX_HEAD_SHELL_OVERLAP_RATIO:
        invalid_reasons.append("head_shell_overlap")
    if invalid_reasons:
        return None, invalid_reasons

    chest_gap = rect_distance(bubble_box, body_regions.chest_bbox)
    min_chest_gap = max(
        12,
        int(round(body_regions.chest_bbox.height * 0.10)),
        int(round(max(dimensions.text_width, dimensions.text_height) * 0.05)),
    )
    if chest_shell_overlap > 0:
        penalties["bubble_shell_chest_overlap"] = chest_shell_overlap * CHEST_SHELL_OVERLAP_WEIGHT
    if chest_gap < min_chest_gap:
        penalties["bubble_shell_chest_near"] = (min_chest_gap - chest_gap) * CHEST_NEAR_WEIGHT

    critical_shell_overlap = face_shell_overlap + chest_shell_overlap + lower_shell_overlap
    if critical_shell_overlap > 0:
        penalties["bubble_shell_critical_overlap"] = critical_shell_overlap * BUBBLE_SHELL_CRITICAL_WEIGHT
    if head_shell_overlap > 0:
        penalties["bubble_shell_head_overlap"] = head_shell_overlap * HEAD_SHELL_OVERLAP_WEIGHT

    face_gap = rect_distance(text_box, body_regions.face_bbox)
    min_face_gap = max(12, int(round(body_regions.face_bbox.height * 0.18)))
    max_face_gap = max(
        int(round(body_regions.face_bbox.height * 1.75)),
        int(round(max(dimensions.text_width, dimensions.text_height) * 1.10)),
    )
    if face_gap < min_face_gap:
        penalties["face_too_near"] = (min_face_gap - face_gap) * FACE_NEAR_WEIGHT
    if face_gap > max_face_gap:
        penalties["face_too_far"] = (face_gap - max_face_gap) * FACE_FAR_WEIGHT

    region = _derive_region(
        text_box=text_box,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
    )
    return (
        PlacementChoice(
            bubble_id=dimensions.bubble_id,
            sentence_ids=list(dimensions.sentence_ids),
            anchor_x_px=anchor_x,
            anchor_y_px=anchor_y,
            text_box=text_box,
            bubble_box=bubble_box,
            total_score=sum(penalties.values()),
            penalties=penalties,
            source="candidate",
            template=READING_MODEL,
            slot=region,
        ),
        [],
    )


def _merge_invalid_counts(target: dict[str, int], extra: dict[str, int]) -> None:
    for key, value in extra.items():
        if value <= 0:
            continue
        target[key] = target.get(key, 0) + value


def _vectorized_regions(
    *,
    center_xs: np.ndarray,
    center_ys: np.ndarray,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
) -> np.ndarray:
    person_bbox = body_regions.person_bbox
    row_top = center_ys < (image_height / 3.0)
    row_mid = (~row_top) & (center_ys < (image_height * 2.0 / 3.0))
    row_bottom = ~(row_top | row_mid)
    side_right = center_xs >= person_bbox.center_x
    out = np.empty(center_xs.shape, dtype=object)
    out[row_top & side_right] = "top-right"
    out[row_top & ~side_right] = "top-left"
    out[row_mid & side_right] = "mid-right"
    out[row_mid & ~side_right] = "mid-left"
    out[row_bottom & side_right] = "bottom-right"
    out[row_bottom & ~side_right] = "bottom-left"
    return out


def _vectorized_candidates_from_arrays(
    *,
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    mask_stats: MaskStatsBundle,
    text_lefts: np.ndarray,
    text_tops: np.ndarray,
    sources: np.ndarray,
) -> tuple[list[tuple[Candidate, PlacementChoice]], dict[str, int]]:
    text_rights = text_lefts + dimensions.text_width
    text_bottoms = text_tops + dimensions.text_height

    bubble_lefts = text_lefts - dimensions.text_offset_x
    bubble_tops = text_tops - dimensions.text_offset_y
    bubble_rights = bubble_lefts + dimensions.bubble_width
    bubble_bottoms = bubble_tops + dimensions.bubble_height
    anchor_xs = bubble_lefts + dimensions.anchor_offset_x
    anchor_ys = bubble_tops + dimensions.anchor_offset_y

    text_areas = np.maximum(1, (text_rights - text_lefts) * (text_bottoms - text_tops)).astype(np.float64)
    bubble_areas = np.maximum(1, (bubble_rights - bubble_lefts) * (bubble_bottoms - bubble_tops)).astype(np.float64)

    invalid_counts: dict[str, int] = {}
    out_of_bounds = (
        (text_lefts < 0)
        | (text_tops < 0)
        | (text_rights > image_width)
        | (text_bottoms > image_height)
    )
    invalid_counts["out_of_bounds"] = int(out_of_bounds.sum())

    face_text_overlap = mask_stats.face.overlap_areas(text_lefts, text_tops, text_rights, text_bottoms)
    chest_text_overlap = mask_stats.chest.overlap_areas(text_lefts, text_tops, text_rights, text_bottoms)
    lower_text_overlap = mask_stats.lower.overlap_areas(text_lefts, text_tops, text_rights, text_bottoms)
    head_text_overlap_area = (
        mask_stats.head.overlap_areas(text_lefts, text_tops, text_rights, text_bottoms)
        if mask_stats.head is not None
        else np.zeros(text_lefts.shape, dtype=np.int64)
    )

    face_overlap = face_text_overlap > 0
    chest_overlap = chest_text_overlap > 0
    lower_overlap = lower_text_overlap > 0
    head_text_overlap_ratio = head_text_overlap_area / text_areas
    head_keepout_overlap = head_text_overlap_ratio > MAX_HEAD_TEXT_OVERLAP_RATIO

    invalid_counts["face_overlap"] = int(face_overlap.sum())
    invalid_counts["chest_overlap"] = int(chest_overlap.sum())
    invalid_counts["lower_overlap"] = int(lower_overlap.sum())
    invalid_counts["head_keepout_overlap"] = int(head_keepout_overlap.sum())

    valid_stage1 = ~(out_of_bounds | face_overlap | chest_overlap | lower_overlap | head_keepout_overlap)
    if not np.any(valid_stage1):
        return [], invalid_counts

    face_shell_overlap_area = mask_stats.face.overlap_areas(bubble_lefts, bubble_tops, bubble_rights, bubble_bottoms)
    chest_shell_overlap_area = mask_stats.chest.overlap_areas(bubble_lefts, bubble_tops, bubble_rights, bubble_bottoms)
    lower_shell_overlap_area = mask_stats.lower.overlap_areas(bubble_lefts, bubble_tops, bubble_rights, bubble_bottoms)
    head_shell_overlap_area = (
        mask_stats.head.overlap_areas(bubble_lefts, bubble_tops, bubble_rights, bubble_bottoms)
        if mask_stats.head is not None
        else np.zeros(text_lefts.shape, dtype=np.int64)
    )

    chest_shell_overlap_ratio = chest_shell_overlap_area / bubble_areas
    head_shell_overlap_ratio = head_shell_overlap_area / bubble_areas
    chest_shell_invalid = valid_stage1 & (chest_shell_overlap_ratio > MAX_CHEST_SHELL_OVERLAP_RATIO)
    head_shell_invalid = valid_stage1 & (head_shell_overlap_ratio > MAX_HEAD_SHELL_OVERLAP_RATIO)

    invalid_counts["chest_shell_overlap"] = int(chest_shell_invalid.sum())
    invalid_counts["head_shell_overlap"] = int(head_shell_invalid.sum())

    valid = valid_stage1 & ~chest_shell_invalid & ~head_shell_invalid
    if not np.any(valid):
        return [], invalid_counts

    desired_margin = max(24, int(round(min(image_width, image_height) * 0.05)))
    ideal_margin = max(40, int(round(min(image_width, image_height) * 0.09)))
    edge_deficit = (
        np.maximum(0, desired_margin - text_lefts)
        + np.maximum(0, desired_margin - text_tops)
        + np.maximum(0, desired_margin - (image_width - text_rights))
        + np.maximum(0, desired_margin - (image_height - text_bottoms))
    )
    edge_ideal_deficit = (
        np.maximum(0, ideal_margin - text_lefts)
        + np.maximum(0, ideal_margin - text_tops)
        + np.maximum(0, ideal_margin - (image_width - text_rights))
        + np.maximum(0, ideal_margin - (image_height - text_bottoms))
    )

    person_text_overlap_ratio = mask_stats.person.overlap_areas(text_lefts, text_tops, text_rights, text_bottoms) / text_areas
    person_bubble_overlap_ratio = (
        mask_stats.person.overlap_areas(bubble_lefts, bubble_tops, bubble_rights, bubble_bottoms) / bubble_areas
    )
    face_shell_overlap_ratio = face_shell_overlap_area / bubble_areas
    lower_shell_overlap_ratio = lower_shell_overlap_area / bubble_areas
    critical_shell_overlap_ratio = face_shell_overlap_ratio + chest_shell_overlap_ratio + lower_shell_overlap_ratio

    face_bbox = body_regions.face_bbox
    chest_bbox = body_regions.chest_bbox
    text_dx = np.maximum.reduce(
        [
            face_bbox.left - text_rights,
            text_lefts - face_bbox.right,
            np.zeros(text_lefts.shape, dtype=np.int32),
        ]
    )
    text_dy = np.maximum.reduce(
        [
            face_bbox.top - text_bottoms,
            text_tops - face_bbox.bottom,
            np.zeros(text_tops.shape, dtype=np.int32),
        ]
    )
    face_gap = np.hypot(text_dx, text_dy)

    bubble_dx = np.maximum.reduce(
        [
            chest_bbox.left - bubble_rights,
            bubble_lefts - chest_bbox.right,
            np.zeros(bubble_lefts.shape, dtype=np.int32),
        ]
    )
    bubble_dy = np.maximum.reduce(
        [
            chest_bbox.top - bubble_bottoms,
            bubble_tops - chest_bbox.bottom,
            np.zeros(bubble_tops.shape, dtype=np.int32),
        ]
    )
    chest_gap = np.hypot(bubble_dx, bubble_dy)

    min_chest_gap = max(
        12,
        int(round(body_regions.chest_bbox.height * 0.10)),
        int(round(max(dimensions.text_width, dimensions.text_height) * 0.05)),
    )
    min_face_gap = max(12, int(round(body_regions.face_bbox.height * 0.18)))
    max_face_gap = max(
        int(round(body_regions.face_bbox.height * 1.75)),
        int(round(max(dimensions.text_width, dimensions.text_height) * 1.10)),
    )

    total_scores = np.zeros(text_lefts.shape, dtype=np.float64)
    total_scores += edge_deficit * TEXT_EDGE_MARGIN_WEIGHT
    total_scores += edge_ideal_deficit * IDEAL_EDGE_MARGIN_WEIGHT
    total_scores += person_text_overlap_ratio * PERSON_OVERLAP_WEIGHT * TEXT_PERSON_OVERLAP_MULTIPLIER
    total_scores += head_text_overlap_ratio * HEAD_TEXT_OVERLAP_WEIGHT
    total_scores += person_bubble_overlap_ratio * BUBBLE_SHELL_PERSON_WEIGHT * BUBBLE_SHELL_PERSON_OVERLAP_MULTIPLIER
    total_scores += chest_shell_overlap_ratio * CHEST_SHELL_OVERLAP_WEIGHT
    total_scores += np.maximum(0.0, min_chest_gap - chest_gap) * CHEST_NEAR_WEIGHT
    total_scores += critical_shell_overlap_ratio * BUBBLE_SHELL_CRITICAL_WEIGHT
    total_scores += head_shell_overlap_ratio * HEAD_SHELL_OVERLAP_WEIGHT
    total_scores += np.maximum(0.0, min_face_gap - face_gap) * FACE_NEAR_WEIGHT
    total_scores += np.maximum(0.0, face_gap - max_face_gap) * FACE_FAR_WEIGHT

    region_labels = _vectorized_regions(
        center_xs=(text_lefts + text_rights) / 2.0,
        center_ys=(text_tops + text_bottoms) / 2.0,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
    )

    candidate_rows: list[tuple[Candidate, PlacementChoice]] = []
    for idx in np.flatnonzero(valid):
        text_box = Rect(
            int(text_lefts[idx]),
            int(text_tops[idx]),
            int(text_rights[idx]),
            int(text_bottoms[idx]),
        )
        bubble_box = Rect(
            int(bubble_lefts[idx]),
            int(bubble_tops[idx]),
            int(bubble_rights[idx]),
            int(bubble_bottoms[idx]),
        )
        penalties: dict[str, float] = {}
        if edge_deficit[idx] > 0:
            penalties["text_edge_margin"] = float(edge_deficit[idx] * TEXT_EDGE_MARGIN_WEIGHT)
        if edge_ideal_deficit[idx] > 0:
            penalties["text_edge_ideal_margin"] = float(edge_ideal_deficit[idx] * IDEAL_EDGE_MARGIN_WEIGHT)
        if person_text_overlap_ratio[idx] > 0:
            penalties["text_person_overlap"] = float(
                person_text_overlap_ratio[idx] * PERSON_OVERLAP_WEIGHT * TEXT_PERSON_OVERLAP_MULTIPLIER
            )
        if head_text_overlap_ratio[idx] > 0:
            penalties["text_head_overlap"] = float(head_text_overlap_ratio[idx] * HEAD_TEXT_OVERLAP_WEIGHT)
        if person_bubble_overlap_ratio[idx] > 0:
            penalties["bubble_shell_person_overlap"] = float(
                person_bubble_overlap_ratio[idx] * BUBBLE_SHELL_PERSON_WEIGHT * BUBBLE_SHELL_PERSON_OVERLAP_MULTIPLIER
            )
        if chest_shell_overlap_ratio[idx] > 0:
            penalties["bubble_shell_chest_overlap"] = float(chest_shell_overlap_ratio[idx] * CHEST_SHELL_OVERLAP_WEIGHT)
        if chest_gap[idx] < min_chest_gap:
            penalties["bubble_shell_chest_near"] = float((min_chest_gap - chest_gap[idx]) * CHEST_NEAR_WEIGHT)
        if critical_shell_overlap_ratio[idx] > 0:
            penalties["bubble_shell_critical_overlap"] = float(
                critical_shell_overlap_ratio[idx] * BUBBLE_SHELL_CRITICAL_WEIGHT
            )
        if head_shell_overlap_ratio[idx] > 0:
            penalties["bubble_shell_head_overlap"] = float(head_shell_overlap_ratio[idx] * HEAD_SHELL_OVERLAP_WEIGHT)
        if face_gap[idx] < min_face_gap:
            penalties["face_too_near"] = float((min_face_gap - face_gap[idx]) * FACE_NEAR_WEIGHT)
        if face_gap[idx] > max_face_gap:
            penalties["face_too_far"] = float((face_gap[idx] - max_face_gap) * FACE_FAR_WEIGHT)
        candidate_rows.append(
            (
                Candidate(int(text_lefts[idx]), int(text_tops[idx]), str(sources[idx])),
                PlacementChoice(
                    bubble_id=dimensions.bubble_id,
                    sentence_ids=list(dimensions.sentence_ids),
                    anchor_x_px=int(anchor_xs[idx]),
                    anchor_y_px=int(anchor_ys[idx]),
                    text_box=text_box,
                    bubble_box=bubble_box,
                    total_score=float(total_scores[idx]),
                    penalties=penalties,
                    source=str(sources[idx]),
                    template=READING_MODEL,
                    slot=str(region_labels[idx]),
                ),
            )
        )
    return candidate_rows, invalid_counts


def _vectorized_scan_grid_candidates(
    *,
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    mask_stats: MaskStatsBundle,
    x_step: int,
    y_step: int,
) -> tuple[list[tuple[Candidate, PlacementChoice]], dict[str, int]]:
    max_left = max(0, image_width - dimensions.text_width)
    max_top = max(0, image_height - dimensions.text_height)
    left_positions = np.asarray(_iter_positions(max_left, x_step), dtype=np.int32)
    top_positions = np.asarray(_iter_positions(max_top, y_step), dtype=np.int32)
    left_grid, top_grid = np.meshgrid(left_positions, top_positions)
    text_lefts = left_grid.reshape(-1)
    text_tops = top_grid.reshape(-1)
    sources = np.full(text_lefts.shape, "scan-grid", dtype=object)
    return _vectorized_candidates_from_arrays(
        dimensions=dimensions,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
        mask_stats=mask_stats,
        text_lefts=text_lefts,
        text_tops=text_tops,
        sources=sources,
    )


def _sorted_candidate_rows(candidate_rows: list[tuple[Candidate, PlacementChoice]]) -> list[tuple[Candidate, PlacementChoice]]:
    if not candidate_rows:
        return []
    scores = np.asarray([row[1].total_score for row in candidate_rows], dtype=np.float64)
    tops = np.asarray([row[1].text_box.top for row in candidate_rows], dtype=np.int32)
    rights = np.asarray([row[1].text_box.right for row in candidate_rows], dtype=np.int32)
    order = np.lexsort((-rights, tops, scores))
    return [candidate_rows[int(idx)] for idx in order]


def _collect_candidates(
    *,
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    mask_stats: MaskStatsBundle,
    total_bubbles: int,
) -> tuple[list[CandidateOption], dict[str, int], list[dict[str, Any]], dict[str, int]]:
    invalid_counts: dict[str, int] = {}
    candidate_rows: list[tuple[Candidate, PlacementChoice]] = []
    seen: set[tuple[int, int]] = set()
    max_candidates_per_bin, max_candidates_per_bubble = _candidate_limits(total_bubbles)

    def add_vectorized_candidates(text_lefts: np.ndarray, text_tops: np.ndarray, sources: np.ndarray) -> None:
        if text_lefts.size == 0:
            return
        dedup_lefts: list[int] = []
        dedup_tops: list[int] = []
        dedup_sources: list[str] = []
        for left, top, source in zip(text_lefts.tolist(), text_tops.tolist(), sources.tolist(), strict=True):
            candidate = Candidate(int(round(left)), int(round(top)), str(source))
            key = candidate_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            dedup_lefts.append(candidate.text_left)
            dedup_tops.append(candidate.text_top)
            dedup_sources.append(candidate.source)
        if not dedup_lefts:
            return
        rows, extra_invalid_counts = _vectorized_candidates_from_arrays(
            dimensions=dimensions,
            image_width=image_width,
            image_height=image_height,
            body_regions=body_regions,
            mask_stats=mask_stats,
            text_lefts=np.asarray(dedup_lefts, dtype=np.int32),
            text_tops=np.asarray(dedup_tops, dtype=np.int32),
            sources=np.asarray(dedup_sources, dtype=object),
        )
        candidate_rows.extend(rows)
        _merge_invalid_counts(invalid_counts, extra_invalid_counts)

    max_left = max(0, image_width - dimensions.text_width)
    max_top = max(0, image_height - dimensions.text_height)
    if total_bubbles == 1:
        x_step = max(24, dimensions.text_width)
        y_step = max(24, dimensions.text_height // 2)
    else:
        x_step = max(12, dimensions.text_width // 2)
        y_step = max(12, dimensions.text_height // 4)
    scan_rows, scan_invalid_counts = _vectorized_scan_grid_candidates(
        dimensions=dimensions,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
        mask_stats=mask_stats,
        x_step=x_step,
        y_step=y_step,
    )
    candidate_rows.extend(scan_rows)
    for candidate, _ in scan_rows:
        seen.add(candidate_key(candidate))
    _merge_invalid_counts(invalid_counts, scan_invalid_counts)

    seed_positions = _edge_seed_positions(
        dimensions=dimensions,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
    )
    if seed_positions:
        seed_lefts = np.asarray([item[0] for item in seed_positions], dtype=np.int32)
        seed_tops = np.asarray([item[1] for item in seed_positions], dtype=np.int32)
        seed_sources = np.asarray([item[2] for item in seed_positions], dtype=object)
        add_vectorized_candidates(seed_lefts, seed_tops, seed_sources)

    candidate_rows = _sorted_candidate_rows(candidate_rows)

    refinement_seed_count = 8
    refinement_base_rows = candidate_rows[: min(refinement_seed_count, len(candidate_rows))]
    if refinement_base_rows:
        offsets = np.asarray(_local_refinement_offsets(dimensions=dimensions), dtype=np.int32)
        base_lefts = np.asarray([row[0].text_left for row in refinement_base_rows], dtype=np.int32)
        base_tops = np.asarray([row[0].text_top for row in refinement_base_rows], dtype=np.int32)
        refine_lefts = (base_lefts[:, None] + offsets[None, :, 0]).reshape(-1)
        refine_tops = (base_tops[:, None] + offsets[None, :, 1]).reshape(-1)
        refine_sources = np.full(refine_lefts.shape, "local-refine", dtype=object)
        add_vectorized_candidates(refine_lefts, refine_tops, refine_sources)

    candidate_rows = _sorted_candidate_rows(candidate_rows)

    retained_rows: list[tuple[Candidate, PlacementChoice]] = []
    retained_keys: set[tuple[int, int]] = set()
    bin_counts: dict[str, int] = {}
    if candidate_rows:
        center_xs = np.asarray([row[1].text_box.center_x for row in candidate_rows], dtype=np.float64)
        center_ys = np.asarray([row[1].text_box.center_y for row in candidate_rows], dtype=np.float64)
        bin_x = np.clip((center_xs * COARSE_BIN_DIVISIONS / max(1, image_width)).astype(np.int32), 0, COARSE_BIN_DIVISIONS - 1)
        bin_y = np.clip((center_ys * COARSE_BIN_DIVISIONS / max(1, image_height)).astype(np.int32), 0, COARSE_BIN_DIVISIONS - 1)
        bin_ids = bin_x * COARSE_BIN_DIVISIONS + bin_y
    else:
        bin_ids = np.asarray([], dtype=np.int32)

    # Keep a few geometric extremes so the global solver can still build wide/tall spreads
    # even after aggressive candidate pruning for 4-5 bubble cases.
    priority_indices: list[int] = []
    if candidate_rows:
        scores = np.asarray([row[1].total_score for row in candidate_rows], dtype=np.float64)
        lefts = np.asarray([row[1].text_box.left for row in candidate_rows], dtype=np.int32)
        rights = np.asarray([row[1].text_box.right for row in candidate_rows], dtype=np.int32)
        tops = np.asarray([row[1].text_box.top for row in candidate_rows], dtype=np.int32)
        bottoms = np.asarray([row[1].text_box.bottom for row in candidate_rows], dtype=np.int32)
        for order in (
            np.lexsort((tops, lefts, scores)),
            np.lexsort((tops, -rights, scores)),
            np.lexsort((-rights, tops, scores)),
            np.lexsort((-rights, -bottoms, scores)),
        ):
            priority_indices.extend(order[:2].tolist())
    for idx in priority_indices:
        candidate, choice = candidate_rows[idx]
        if len(retained_rows) >= max_candidates_per_bubble:
            break
        key = candidate_key(candidate)
        if key in retained_keys:
            continue
        retained_keys.add(key)
        retained_rows.append((candidate, choice))

    if candidate_rows:
        for bin_id in np.unique(bin_ids):
            member_indices = np.flatnonzero(bin_ids == bin_id)
            keep_indices = member_indices[:max_candidates_per_bin]
            bin_key = (int(bin_id // COARSE_BIN_DIVISIONS), int(bin_id % COARSE_BIN_DIVISIONS))
            bin_counts[f"{bin_key[0]}-{bin_key[1]}"] = int(len(keep_indices))
            for idx in keep_indices:
                candidate, choice = candidate_rows[int(idx)]
                if len(retained_rows) >= max_candidates_per_bubble:
                    break
                key = candidate_key(candidate)
                if key in retained_keys:
                    continue
                retained_keys.add(key)
                retained_rows.append((candidate, choice))
            if len(retained_rows) >= max_candidates_per_bubble:
                break

    for candidate, choice in candidate_rows:
        if len(retained_rows) >= max_candidates_per_bubble:
            break
        key = candidate_key(candidate)
        if key in retained_keys:
            continue
        retained_keys.add(key)
        retained_rows.append((candidate, choice))

    options: list[CandidateOption] = []
    for index, (candidate, choice) in enumerate(retained_rows):
        options.append(
            CandidateOption(
                index=index,
                candidate=candidate,
                choice=choice,
                unary_penalties=dict(choice.penalties),
                unary_score=_scaled_score(choice.total_score),
            )
        )

    top_candidates = [option.to_debug_dict() for option in options[:5]]
    return options, invalid_counts, top_candidates, bin_counts


def _column_tolerance(prev_choice: PlacementChoice, curr_choice: PlacementChoice) -> float:
    return max(prev_choice.text_box.width, curr_choice.text_box.width) * 0.75


def _pairwise_penalties(
    prev_choice: PlacementChoice,
    curr_choice: PlacementChoice,
) -> dict[str, float]:
    penalties: dict[str, float] = {}
    distance = math.hypot(
        curr_choice.text_box.center_x - prev_choice.text_box.center_x,
        curr_choice.text_box.center_y - prev_choice.text_box.center_y,
    )

    target_distance = max(prev_choice.text_box.height, curr_choice.text_box.height) * 0.68
    if distance > target_distance:
        penalties["continuity"] = (distance - target_distance) * CONTINUITY_DISTANCE_WEIGHT

    tol_x = max(prev_choice.text_box.width, curr_choice.text_box.width) * 0.35
    center_dx = curr_choice.text_box.center_x - prev_choice.text_box.center_x
    if center_dx > tol_x:
        penalties["x_backtrack"] = (center_dx - tol_x) * X_BACKTRACK_WEIGHT

    column_tol = _column_tolerance(prev_choice, curr_choice)
    if abs(curr_choice.text_box.center_x - prev_choice.text_box.center_x) <= column_tol:
        min_gap = max(6, int(round(min(prev_choice.text_box.height, curr_choice.text_box.height) * 0.06)))
        gap_deficit = prev_choice.text_box.bottom + min_gap - curr_choice.text_box.top
        if gap_deficit > 0:
            penalties["same_column_stack"] = gap_deficit * SAME_COLUMN_STACK_WEIGHT
        else:
            penalties["same_column_gap"] = abs(gap_deficit) * SAME_COLUMN_GAP_WEIGHT
    else:
        reset_tol = max(prev_choice.text_box.height, curr_choice.text_box.height) * 0.25
        upward_reset = prev_choice.text_box.top - (curr_choice.text_box.top + reset_tol)
        if upward_reset > 0:
            penalties["column_reset_upward"] = upward_reset * COLUMN_RESET_UPWARD_WEIGHT
            large_reset_threshold = max(prev_choice.text_box.height, curr_choice.text_box.height) * 0.35
            if upward_reset > large_reset_threshold:
                penalties["column_reset_upward_large"] = (
                    upward_reset - large_reset_threshold
                ) * LARGE_UPWARD_RESET_WEIGHT

    crowding_target = max(96.0, min(prev_choice.text_box.height, curr_choice.text_box.height) * 1.05)
    if distance < crowding_target:
        penalties["crowding"] = (crowding_target - distance) * CROWDING_WEIGHT

    return penalties


def _choice_from_option(option: CandidateOption, *, bubble_id: str, sentence_ids: list[int]) -> PlacementChoice:
    return PlacementChoice(
        bubble_id=bubble_id,
        sentence_ids=list(sentence_ids),
        anchor_x_px=option.choice.anchor_x_px,
        anchor_y_px=option.choice.anchor_y_px,
        text_box=option.choice.text_box,
        bubble_box=option.choice.bubble_box,
        total_score=option.choice.total_score,
        penalties=dict(option.unary_penalties),
        source=option.candidate.source,
        template=READING_MODEL,
        slot=option.choice.slot,
    )


def _manual_choice_from_anchor(
    *,
    scene_plan: SceneBubblePlan,
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    mask_stats: MaskStatsBundle,
    source: str,
) -> tuple[PlacementChoice, list[str]]:
    anchor_x_px = int(round(scene_plan.anchor_x * image_width))
    anchor_y_px = int(round(scene_plan.anchor_y * image_height))
    text_left = anchor_x_px - dimensions.text_width
    text_top = anchor_y_px
    choice, invalid_reasons = _build_choice(
        dimensions=dimensions,
        text_left=text_left,
        text_top=text_top,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
        mask_stats=mask_stats,
    )
    if choice is not None:
        choice.source = source
        return choice, []

    text_box, bubble_box, _, _ = make_layout_boxes_from_text_position(dimensions, text_left, text_top)
    region = _derive_region(
        text_box=text_box,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
    )
    return (
        PlacementChoice(
            bubble_id=scene_plan.bubble_id,
            sentence_ids=list(scene_plan.sentence_ids),
            anchor_x_px=anchor_x_px,
            anchor_y_px=anchor_y_px,
            text_box=text_box,
            bubble_box=bubble_box,
            total_score=0.0,
            penalties={},
            source=source,
            template=READING_MODEL,
            slot=region,
        ),
        invalid_reasons,
    )


def _rebuild_choice_at_original_scale(
    *,
    selected_option: CandidateOption,
    original_dimensions: BubbleDimensions,
    original_image_width: int,
    original_image_height: int,
    original_body_regions: BodyRegions,
    original_mask_stats: MaskStatsBundle,
    search_scale: float,
    bubble_id: str,
    sentence_ids: list[int],
) -> PlacementChoice:
    if search_scale >= 0.999:
        return _choice_from_option(selected_option, bubble_id=bubble_id, sentence_ids=sentence_ids)

    original_text_left = int(round(selected_option.choice.text_box.left / search_scale))
    original_text_top = int(round(selected_option.choice.text_box.top / search_scale))
    rebuilt_choice, invalid_reasons = _build_choice(
        dimensions=original_dimensions,
        text_left=original_text_left,
        text_top=original_text_top,
        image_width=original_image_width,
        image_height=original_image_height,
        body_regions=original_body_regions,
        mask_stats=original_mask_stats,
    )
    if rebuilt_choice is None:
        text_box, bubble_box, anchor_x, anchor_y = make_layout_boxes_from_text_position(
            original_dimensions,
            original_text_left,
            original_text_top,
        )
        rebuilt_choice = PlacementChoice(
            bubble_id=bubble_id,
            sentence_ids=list(sentence_ids),
            anchor_x_px=anchor_x,
            anchor_y_px=anchor_y,
            text_box=text_box,
            bubble_box=bubble_box,
            total_score=selected_option.choice.total_score,
            penalties={
                **dict(selected_option.unary_penalties),
                "scaled_rebuild_invalid": float(len(invalid_reasons)),
            },
            source=selected_option.candidate.source,
            template=READING_MODEL,
            slot=_derive_region(
                text_box=text_box,
                image_width=original_image_width,
                image_height=original_image_height,
                body_regions=original_body_regions,
            ),
        )
    else:
        rebuilt_choice.source = selected_option.candidate.source
        rebuilt_choice.bubble_id = bubble_id
        rebuilt_choice.sentence_ids = list(sentence_ids)
    return rebuilt_choice


def evaluate_scene_layout(
    *,
    reflow_plans: list[ReflowBubblePlan],
    scene_plans: list[SceneBubblePlan],
    image_width: int,
    image_height: int,
    face_mask: np.ndarray,
    person_mask: np.ndarray,
    chest_mask: np.ndarray | None = None,
    lower_mask: np.ndarray | None = None,
    head_mask: np.ndarray | None = None,
    font_size: int,
    source: str = "manual",
) -> PlacementSolution:
    if not reflow_plans:
        raise RuntimeError("reflow plans are required")
    if len(reflow_plans) > 5:
        raise RuntimeError("PoC supports at most 5 bubbles")

    scene_by_bubble_id = {plan.bubble_id: plan for plan in scene_plans}
    missing_bubbles = [plan.bubble_id for plan in reflow_plans if plan.bubble_id not in scene_by_bubble_id]
    if missing_bubbles:
        raise RuntimeError(f"scene plans missing bubbles: {', '.join(missing_bubbles)}")
    extra_bubbles = [plan.bubble_id for plan in scene_plans if plan.bubble_id not in {item.bubble_id for item in reflow_plans}]
    if extra_bubbles:
        raise RuntimeError(f"scene plans contain unknown bubbles: {', '.join(extra_bubbles)}")

    body_regions = build_body_regions(person_mask, face_mask, chest_mask=chest_mask, lower_mask=lower_mask)
    mask_stats = _build_mask_stats(body_regions, head_mask)
    dimensions_by_bubble_id: dict[str, BubbleDimensions] = {}
    selected_placements: list[PlacementChoice] = []
    invalid_placements: list[dict[str, Any]] = []

    for reflow_plan in reflow_plans:
        dimensions = estimate_bubble_dimensions(
            reflow_plan,
            image_width=image_width,
            image_height=image_height,
            font_size=font_size,
        )
        scene_plan = scene_by_bubble_id[reflow_plan.bubble_id]
        choice, invalid_reasons = _manual_choice_from_anchor(
            scene_plan=scene_plan,
            dimensions=dimensions,
            image_width=image_width,
            image_height=image_height,
            body_regions=body_regions,
            mask_stats=mask_stats,
            source=source,
        )
        choice.bubble_id = reflow_plan.bubble_id
        choice.sentence_ids = list(reflow_plan.sentence_ids)
        dimensions_by_bubble_id[reflow_plan.bubble_id] = dimensions
        selected_placements.append(choice)
        if invalid_reasons:
            invalid_placements.append(
                {
                    "bubble_id": reflow_plan.bubble_id,
                    "reasons": invalid_reasons,
                    "placement": choice.to_debug_dict(),
                }
            )

    hard_conflicts: list[dict[str, Any]] = []
    expanded_boxes = [expand_rect(placement.text_box, TEXT_CLEARANCE_PX) for placement in selected_placements]
    for left_index, left in enumerate(selected_placements):
        for right_index in range(left_index + 1, len(selected_placements)):
            right = selected_placements[right_index]
            if rects_intersect(expanded_boxes[left_index], expanded_boxes[right_index]):
                hard_conflicts.append(
                    {
                        "type": "text_overlap",
                        "left_bubble_id": left.bubble_id,
                        "right_bubble_id": right.bubble_id,
                    }
                )

    pairwise_debug: list[dict[str, Any]] = []
    pairwise_total = 0.0
    for bubble_index in range(1, len(selected_placements)):
        prev_choice = selected_placements[bubble_index - 1]
        curr_choice = selected_placements[bubble_index]
        center_tol_x = max(prev_choice.text_box.width, curr_choice.text_box.width)
        if curr_choice.text_box.center_x > prev_choice.text_box.center_x + center_tol_x:
            hard_conflicts.append(
                {
                    "type": "reading_x_backtrack_hard",
                    "previous_bubble_id": prev_choice.bubble_id,
                    "current_bubble_id": curr_choice.bubble_id,
                }
            )

        same_column_tol = _column_tolerance(prev_choice, curr_choice)
        if (
            abs(curr_choice.text_box.center_x - prev_choice.text_box.center_x) <= same_column_tol
            and curr_choice.text_box.top < prev_choice.text_box.top
        ):
            hard_conflicts.append(
                {
                    "type": "same_column_upward_reset_hard",
                    "previous_bubble_id": prev_choice.bubble_id,
                    "current_bubble_id": curr_choice.bubble_id,
                }
            )

        same_side_upward_limit = max(
            16,
            int(
                round(
                    max(prev_choice.text_box.height, curr_choice.text_box.height) * MAX_SAME_SIDE_UPWARD_RESET_RATIO
                )
            ),
        )
        if (
            _slot_side(prev_choice.slot) == _slot_side(curr_choice.slot)
            and curr_choice.text_box.top + same_side_upward_limit < prev_choice.text_box.top
        ):
            hard_conflicts.append(
                {
                    "type": "same_side_upward_reset_hard",
                    "previous_bubble_id": prev_choice.bubble_id,
                    "current_bubble_id": curr_choice.bubble_id,
                }
            )

        large_column_shift = abs(curr_choice.text_box.center_x - prev_choice.text_box.center_x) > max(
            same_column_tol * 2.0,
            image_width * 0.18,
        )
        upward_reset_limit = max(
            24,
            int(
                round(
                    max(prev_choice.text_box.height, curr_choice.text_box.height)
                    * MAX_LARGE_COLUMN_UPWARD_RESET_RATIO
                )
            ),
        )
        if large_column_shift and curr_choice.text_box.top + upward_reset_limit < prev_choice.text_box.top:
            hard_conflicts.append(
                {
                    "type": "large_column_shift_upward_reset_hard",
                    "previous_bubble_id": prev_choice.bubble_id,
                    "current_bubble_id": curr_choice.bubble_id,
                }
            )

        penalties = _pairwise_penalties(prev_choice, curr_choice)
        pairwise_total += sum(penalties.values())
        pairwise_debug.append(
            {
                "previous_bubble_id": prev_choice.bubble_id,
                "current_bubble_id": curr_choice.bubble_id,
                "penalties": {key: round(value, 3) for key, value in penalties.items()},
            }
        )

    global_penalties: dict[str, float] = {}
    if len(selected_placements) >= 2:
        horizontal_span = max(placement.text_box.right for placement in selected_placements) - min(
            placement.text_box.left for placement in selected_placements
        )
        vertical_span = max(placement.text_box.bottom for placement in selected_placements) - min(
            placement.text_box.top for placement in selected_placements
        )
        vertical_center_span = int(round(max(placement.text_box.center_y for placement in selected_placements))) - int(
            round(min(placement.text_box.center_y for placement in selected_placements))
        )

        target_horizontal_span = min(
            image_width,
            max(
                int(round(body_regions.person_bbox.width * 0.78)),
                int(round(image_width * 0.52)),
            ),
        )
        target_vertical_span = min(
            image_height,
            max(
                int(round(body_regions.person_bbox.height * 0.40)),
                int(round(image_height * 0.28)),
            ),
        )
        target_vertical_center_span = min(
            image_height,
            max(
                int(round(body_regions.person_bbox.height * 0.26)),
                int(round(image_height * 0.18)),
            ),
        )
        horizontal_span_deficit = max(0, target_horizontal_span - horizontal_span)
        vertical_span_deficit = max(0, target_vertical_span - vertical_span)
        vertical_center_span_deficit = max(0, target_vertical_center_span - vertical_center_span)
        if horizontal_span_deficit > 0:
            global_penalties["horizontal_span_deficit"] = horizontal_span_deficit * HORIZONTAL_SPAN_DEFICIT_WEIGHT
        if vertical_span_deficit > 0:
            global_penalties["vertical_span_deficit"] = vertical_span_deficit * VERTICAL_SPAN_DEFICIT_WEIGHT
        if vertical_center_span_deficit > 0:
            global_penalties["vertical_center_span_deficit"] = (
                vertical_center_span_deficit * VERTICAL_CENTER_SPAN_DEFICIT_WEIGHT
            )

    scene_plans_ordered = [scene_by_bubble_id[plan.bubble_id] for plan in reflow_plans]
    centers = [(placement.text_box.center_x, placement.text_box.center_y) for placement in selected_placements]
    pair_center_distances = [
        round(
            math.hypot(
                left.text_box.center_x - right.text_box.center_x,
                left.text_box.center_y - right.text_box.center_y,
            ),
            3,
        )
        for left_index, left in enumerate(selected_placements)
        for right in selected_placements[left_index + 1 :]
    ]
    unary_total = sum(placement.total_score for placement in selected_placements)
    objective_value = unary_total + pairwise_total + sum(global_penalties.values())
    debug_payload = {
        "solver": "manual-eval",
        "selected_template": READING_MODEL,
        "reading_model": READING_MODEL,
        "image_width": image_width,
        "image_height": image_height,
        "font_size": font_size,
        "body_regions": body_regions.to_debug_dict(),
        "dimensions": [item.to_debug_dict() for item in dimensions_by_bubble_id.values()],
        "placements": [placement.to_debug_dict() for placement in selected_placements],
        "center_points": [[round(x, 3), round(y, 3)] for x, y in centers],
        "pair_center_distances": pair_center_distances,
        "pairwise_penalties": pairwise_debug,
        "hard_conflicts": hard_conflicts,
        "invalid_placements": invalid_placements,
        "global_penalties": {key: round(value, 3) for key, value in global_penalties.items()},
        "unary_total": round(unary_total, 3),
        "pairwise_total": round(pairwise_total, 3),
        "objective_value": round(objective_value, 3),
        "feasible": not invalid_placements and not hard_conflicts,
        "horizontal_span_px": max(placement.text_box.right for placement in selected_placements)
        - min(placement.text_box.left for placement in selected_placements),
        "vertical_span_px": max(placement.text_box.bottom for placement in selected_placements)
        - min(placement.text_box.top for placement in selected_placements),
        "min_pair_distance_px": min(pair_center_distances) if pair_center_distances else None,
    }
    return PlacementSolution(
        selected_template=READING_MODEL,
        scene_plans=scene_plans_ordered,
        placements=selected_placements,
        debug_payload=debug_payload,
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
    head_mask: np.ndarray | None = None,
    font_size: int,
) -> PlacementSolution:
    if not reflow_plans:
        raise RuntimeError("reflow plans are required")
    if len(reflow_plans) > 5:
        raise RuntimeError("PoC supports at most 5 bubbles")

    body_regions = build_body_regions(person_mask, face_mask, chest_mask=chest_mask, lower_mask=lower_mask)
    mask_stats = _build_mask_stats(body_regions, head_mask)
    search_scale = _search_scale(image_width, image_height)
    search_person_mask = _resize_binary_mask(person_mask, search_scale)
    search_face_mask = _resize_binary_mask(face_mask, search_scale)
    search_chest_mask = _resize_binary_mask(chest_mask, search_scale)
    search_lower_mask = _resize_binary_mask(lower_mask, search_scale)
    search_head_mask = _resize_binary_mask(head_mask, search_scale)
    search_body_regions = build_body_regions(
        search_person_mask,
        search_face_mask,
        chest_mask=search_chest_mask,
        lower_mask=search_lower_mask,
    )
    search_mask_stats = _build_mask_stats(search_body_regions, search_head_mask)
    search_image_height, search_image_width = search_person_mask.shape
    dimensions_by_bubble_id: dict[str, BubbleDimensions] = {}
    options_by_bubble: list[list[CandidateOption]] = []
    candidate_debug: list[dict[str, Any]] = []

    for reflow_plan in reflow_plans:
        dimensions = estimate_bubble_dimensions(
            reflow_plan,
            image_width=image_width,
            image_height=image_height,
            font_size=font_size,
        )
        search_dimensions = _scale_dimensions(dimensions, search_scale)
        options, invalid_counts, top_candidates, bin_counts = _collect_candidates(
            dimensions=search_dimensions,
            image_width=search_image_width,
            image_height=search_image_height,
            body_regions=search_body_regions,
            mask_stats=search_mask_stats,
            total_bubbles=len(reflow_plans),
        )
        dimensions_by_bubble_id[reflow_plan.bubble_id] = dimensions
        candidate_debug.append(
            {
                "bubble_id": reflow_plan.bubble_id,
                "coarse_bin_counts": bin_counts,
                "total_candidates": sum(bin_counts.values()),
                "valid_candidates": len(options),
                "invalid_counts": invalid_counts,
                "top_candidates": top_candidates,
                "search_scale": round(search_scale, 4),
            }
        )
        if not options:
            debug_payload = {
                "solver": "cp-sat",
                "reading_model": READING_MODEL,
                "image_width": image_width,
                "image_height": image_height,
                "search_image_width": search_image_width,
                "search_image_height": search_image_height,
                "search_scale": round(search_scale, 4),
                "font_size": font_size,
                "body_regions": body_regions.to_debug_dict(),
                "dimensions": [item.to_debug_dict() for item in dimensions_by_bubble_id.values()],
                "candidates": candidate_debug,
                "failures": [f"no valid candidate for {reflow_plan.bubble_id}"],
            }
            raise RuntimeError(json.dumps(debug_payload, ensure_ascii=False, indent=2))
        options_by_bubble.append(options)

    model = cp_model.CpModel()
    x_vars: list[list[cp_model.IntVar]] = []
    objective_terms: list[cp_model.LinearExpr] = []
    pair_vars: list[tuple[int, int, int, int, cp_model.IntVar, dict[str, float]]] = []

    for bubble_index, options in enumerate(options_by_bubble):
        row_vars: list[cp_model.IntVar] = []
        for option in options:
            var = model.NewBoolVar(f"x_{bubble_index}_{option.index}")
            row_vars.append(var)
            if option.unary_score > 0:
                objective_terms.append(var * option.unary_score)
        model.AddExactlyOne(row_vars)
        x_vars.append(row_vars)

    expanded_boxes: list[list[Rect]] = [
        [expand_rect(option.choice.text_box, TEXT_CLEARANCE_PX) for option in options]
        for options in options_by_bubble
    ]
    for left_index in range(len(options_by_bubble)):
        for right_index in range(left_index + 1, len(options_by_bubble)):
            for left_option_index in range(len(options_by_bubble[left_index])):
                for right_option_index in range(len(options_by_bubble[right_index])):
                    if rects_intersect(
                        expanded_boxes[left_index][left_option_index],
                        expanded_boxes[right_index][right_option_index],
                    ):
                        model.Add(x_vars[left_index][left_option_index] + x_vars[right_index][right_option_index] <= 1)

    for bubble_index in range(1, len(options_by_bubble)):
        prev_options = options_by_bubble[bubble_index - 1]
        curr_options = options_by_bubble[bubble_index]
        for prev_option_index, prev_option in enumerate(prev_options):
            for curr_option_index, curr_option in enumerate(curr_options):
                center_tol_x = max(prev_option.choice.text_box.width, curr_option.choice.text_box.width)
                if curr_option.choice.text_box.center_x > prev_option.choice.text_box.center_x + center_tol_x:
                    model.Add(x_vars[bubble_index - 1][prev_option_index] + x_vars[bubble_index][curr_option_index] <= 1)
                    continue
                same_column_tol = _column_tolerance(prev_option.choice, curr_option.choice)
                if (
                    abs(curr_option.choice.text_box.center_x - prev_option.choice.text_box.center_x) <= same_column_tol
                    and curr_option.choice.text_box.top < prev_option.choice.text_box.top
                ):
                    model.Add(x_vars[bubble_index - 1][prev_option_index] + x_vars[bubble_index][curr_option_index] <= 1)
                    continue
                same_side_upward_limit = max(
                    16,
                    int(
                        round(
                            max(prev_option.choice.text_box.height, curr_option.choice.text_box.height)
                            * MAX_SAME_SIDE_UPWARD_RESET_RATIO
                        )
                    ),
                )
                if (
                    _slot_side(prev_option.choice.slot) == _slot_side(curr_option.choice.slot)
                    and curr_option.choice.text_box.top + same_side_upward_limit < prev_option.choice.text_box.top
                ):
                    model.Add(x_vars[bubble_index - 1][prev_option_index] + x_vars[bubble_index][curr_option_index] <= 1)
                    continue
                large_column_shift = abs(curr_option.choice.text_box.center_x - prev_option.choice.text_box.center_x) > max(
                    same_column_tol * 2.0,
                    search_image_width * 0.18,
                )
                upward_reset_limit = max(
                    24,
                    int(
                        round(
                            max(prev_option.choice.text_box.height, curr_option.choice.text_box.height)
                            * MAX_LARGE_COLUMN_UPWARD_RESET_RATIO
                        )
                    ),
                )
                if (
                    large_column_shift
                    and curr_option.choice.text_box.top + upward_reset_limit < prev_option.choice.text_box.top
                ):
                    model.Add(x_vars[bubble_index - 1][prev_option_index] + x_vars[bubble_index][curr_option_index] <= 1)
                    continue
    for bubble_index in range(1, len(options_by_bubble)):
        for prev_option_index, prev_option in enumerate(options_by_bubble[bubble_index - 1]):
            for curr_option_index, curr_option in enumerate(options_by_bubble[bubble_index]):
                penalties = _pairwise_penalties(prev_option.choice, curr_option.choice)
                pair_score = _scaled_score(sum(penalties.values()))
                if pair_score <= 0:
                    continue
                pair_var = model.NewBoolVar(f"pair_{bubble_index-1}_{prev_option.index}_{bubble_index}_{curr_option.index}")
                model.Add(pair_var <= x_vars[bubble_index - 1][prev_option_index])
                model.Add(pair_var <= x_vars[bubble_index][curr_option_index])
                model.Add(pair_var >= x_vars[bubble_index - 1][prev_option_index] + x_vars[bubble_index][curr_option_index] - 1)
                objective_terms.append(pair_var * pair_score)
                pair_vars.append(
                    (
                        bubble_index - 1,
                        prev_option_index,
                        bubble_index,
                        curr_option_index,
                        pair_var,
                        penalties,
                    )
                )

    selected_lefts: list[cp_model.IntVar] = []
    selected_rights: list[cp_model.IntVar] = []
    selected_tops: list[cp_model.IntVar] = []
    selected_bottoms: list[cp_model.IntVar] = []
    center_xs: list[cp_model.IntVar] = []
    center_ys: list[cp_model.IntVar] = []
    for bubble_index, options in enumerate(options_by_bubble):
        selected_left = model.NewIntVar(0, search_image_width, f"selected_left_{bubble_index}")
        selected_right = model.NewIntVar(0, search_image_width, f"selected_right_{bubble_index}")
        selected_top = model.NewIntVar(0, search_image_height, f"selected_top_{bubble_index}")
        selected_bottom = model.NewIntVar(0, search_image_height, f"selected_bottom_{bubble_index}")
        center_x = model.NewIntVar(0, search_image_width, f"center_x_{bubble_index}")
        center_y = model.NewIntVar(0, search_image_height, f"center_y_{bubble_index}")

        model.Add(selected_left == sum(x_vars[bubble_index][option_index] * option.choice.text_box.left for option_index, option in enumerate(options)))
        model.Add(selected_right == sum(x_vars[bubble_index][option_index] * option.choice.text_box.right for option_index, option in enumerate(options)))
        model.Add(selected_top == sum(x_vars[bubble_index][option_index] * option.choice.text_box.top for option_index, option in enumerate(options)))
        model.Add(selected_bottom == sum(x_vars[bubble_index][option_index] * option.choice.text_box.bottom for option_index, option in enumerate(options)))
        model.Add(center_x == sum(x_vars[bubble_index][option_index] * int(round(option.choice.text_box.center_x)) for option_index, option in enumerate(options)))
        model.Add(center_y == sum(x_vars[bubble_index][option_index] * int(round(option.choice.text_box.center_y)) for option_index, option in enumerate(options)))

        selected_lefts.append(selected_left)
        selected_rights.append(selected_right)
        selected_tops.append(selected_top)
        selected_bottoms.append(selected_bottom)
        center_xs.append(center_x)
        center_ys.append(center_y)

    if len(reflow_plans) >= 2:
        min_left = model.NewIntVar(0, search_image_width, "min_text_left")
        max_right = model.NewIntVar(0, search_image_width, "max_text_right")
        min_top = model.NewIntVar(0, search_image_height, "min_text_top")
        max_bottom = model.NewIntVar(0, search_image_height, "max_text_bottom")
        model.AddMinEquality(min_left, selected_lefts)
        model.AddMaxEquality(max_right, selected_rights)
        model.AddMinEquality(min_top, selected_tops)
        model.AddMaxEquality(max_bottom, selected_bottoms)

        horizontal_span = model.NewIntVar(0, search_image_width, "horizontal_span")
        vertical_span = model.NewIntVar(0, search_image_height, "vertical_span")
        model.Add(horizontal_span == max_right - min_left)
        model.Add(vertical_span == max_bottom - min_top)

        min_center_y = model.NewIntVar(0, search_image_height, "min_center_y")
        max_center_y = model.NewIntVar(0, search_image_height, "max_center_y")
        model.AddMinEquality(min_center_y, center_ys)
        model.AddMaxEquality(max_center_y, center_ys)
        vertical_center_span = model.NewIntVar(0, search_image_height, "vertical_center_span")
        model.Add(vertical_center_span == max_center_y - min_center_y)

        target_horizontal_span = min(
            search_image_width,
            max(
                int(round(search_body_regions.person_bbox.width * 0.78)),
                int(round(search_image_width * 0.52)),
            ),
        )
        target_vertical_span = min(
            search_image_height,
            max(
                int(round(search_body_regions.person_bbox.height * 0.40)),
                int(round(search_image_height * 0.28)),
            ),
        )

        horizontal_span_deficit = model.NewIntVar(0, search_image_width, "horizontal_span_deficit")
        vertical_span_deficit = model.NewIntVar(0, search_image_height, "vertical_span_deficit")
        vertical_center_span_deficit = model.NewIntVar(0, search_image_height, "vertical_center_span_deficit")
        model.Add(horizontal_span_deficit >= target_horizontal_span - horizontal_span)
        model.Add(horizontal_span_deficit >= 0)
        model.Add(vertical_span_deficit >= target_vertical_span - vertical_span)
        model.Add(vertical_span_deficit >= 0)
        target_vertical_center_span = min(
            search_image_height,
            max(
                int(round(search_body_regions.person_bbox.height * 0.26)),
                int(round(search_image_height * 0.18)),
            ),
        )
        model.Add(vertical_center_span_deficit >= target_vertical_center_span - vertical_center_span)
        model.Add(vertical_center_span_deficit >= 0)
        objective_terms.append(horizontal_span_deficit * _scaled_score(HORIZONTAL_SPAN_DEFICIT_WEIGHT))
        objective_terms.append(vertical_span_deficit * _scaled_score(VERTICAL_SPAN_DEFICIT_WEIGHT))
        objective_terms.append(vertical_center_span_deficit * _scaled_score(VERTICAL_CENTER_SPAN_DEFICIT_WEIGHT))

    model.Minimize(sum(objective_terms) if objective_terms else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = MAX_SOLVE_SECONDS
    solver.parameters.num_search_workers = NUM_SEARCH_WORKERS
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        debug_payload = {
            "solver": "cp-sat",
            "reading_model": READING_MODEL,
            "image_width": image_width,
            "image_height": image_height,
            "search_image_width": search_image_width,
            "search_image_height": search_image_height,
            "search_scale": round(search_scale, 4),
            "font_size": font_size,
            "body_regions": body_regions.to_debug_dict(),
            "dimensions": [item.to_debug_dict() for item in dimensions_by_bubble_id.values()],
            "candidates": candidate_debug,
            "solve_status": solver.StatusName(status),
            "failures": ["cp-sat found no feasible layout"],
        }
        raise RuntimeError(json.dumps(debug_payload, ensure_ascii=False, indent=2))

    selected_options: list[CandidateOption] = []
    selected_placements: list[PlacementChoice] = []
    for bubble_index, (reflow_plan, options) in enumerate(zip(reflow_plans, options_by_bubble, strict=True)):
        selected_index = next(
            option_index
            for option_index in range(len(options))
            if solver.Value(x_vars[bubble_index][option_index]) == 1
        )
        option = options[selected_index]
        selected_options.append(option)
        selected_placements.append(
            _rebuild_choice_at_original_scale(
                selected_option=option,
                original_dimensions=dimensions_by_bubble_id[reflow_plan.bubble_id],
                original_image_width=image_width,
                original_image_height=image_height,
                original_body_regions=body_regions,
                original_mask_stats=mask_stats,
                search_scale=search_scale,
                bubble_id=reflow_plan.bubble_id,
                sentence_ids=list(reflow_plan.sentence_ids),
            )
        )

    pairwise_debug: list[dict[str, Any]] = []
    for prev_bubble_index, prev_option_index, curr_bubble_index, curr_option_index, pair_var, penalties in pair_vars:
        if solver.Value(pair_var) != 1:
            continue
        pairwise_debug.append(
            {
                "previous_bubble_id": reflow_plans[prev_bubble_index].bubble_id,
                "current_bubble_id": reflow_plans[curr_bubble_index].bubble_id,
                "previous_candidate_index": options_by_bubble[prev_bubble_index][prev_option_index].index,
                "current_candidate_index": options_by_bubble[curr_bubble_index][curr_option_index].index,
                "penalties": {key: round(value, 3) for key, value in penalties.items()},
            }
        )

    for debug_entry, option in zip(candidate_debug, selected_options, strict=True):
        debug_entry["selected"] = option.to_debug_dict()

    scene_plans = [
        SceneBubblePlan(
            bubble_id=placement.bubble_id,
            anchor_x=placement.anchor_x_px / image_width,
            anchor_y=placement.anchor_y_px / image_height,
            sentence_ids=list(placement.sentence_ids),
        )
        for placement in selected_placements
    ]

    centers = [(placement.text_box.center_x, placement.text_box.center_y) for placement in selected_placements]
    pair_center_distances = [
        round(
            math.hypot(
                left.text_box.center_x - right.text_box.center_x,
                left.text_box.center_y - right.text_box.center_y,
            ),
            3,
        )
        for left_index, left in enumerate(selected_placements)
        for right in selected_placements[left_index + 1 :]
    ]
    debug_payload = {
        "solver": "cp-sat",
        "selected_template": READING_MODEL,
        "reading_model": READING_MODEL,
        "image_width": image_width,
        "image_height": image_height,
        "font_size": font_size,
        "body_regions": body_regions.to_debug_dict(),
        "dimensions": [item.to_debug_dict() for item in dimensions_by_bubble_id.values()],
        "candidates": candidate_debug,
        "placements": [placement.to_debug_dict() for placement in selected_placements],
        "center_points": [[round(x, 3), round(y, 3)] for x, y in centers],
        "pair_center_distances": pair_center_distances,
        "pairwise_penalties": pairwise_debug,
        "solve_status": solver.StatusName(status),
        "objective_value": round(solver.ObjectiveValue() / OBJECTIVE_SCALE, 3),
        "best_objective_bound": round(solver.BestObjectiveBound() / OBJECTIVE_SCALE, 3),
        "horizontal_span_px": max(placement.text_box.right for placement in selected_placements)
        - min(placement.text_box.left for placement in selected_placements),
        "vertical_span_px": max(placement.text_box.bottom for placement in selected_placements)
        - min(placement.text_box.top for placement in selected_placements),
        "min_pair_distance_px": min(pair_center_distances) if pair_center_distances else None,
        "max_time_in_seconds": MAX_SOLVE_SECONDS,
        "num_search_workers": NUM_SEARCH_WORKERS,
    }
    return PlacementSolution(
        selected_template=READING_MODEL,
        scene_plans=scene_plans,
        placements=selected_placements,
        debug_payload=debug_payload,
    )
