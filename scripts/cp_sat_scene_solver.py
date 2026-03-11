from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from ortools.sat.python import cp_model

from bubble.models import ReflowBubblePlan, SceneBubblePlan
from beam_search_scene_solver import (
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
MAX_SOLVE_SECONDS = 10.0
NUM_SEARCH_WORKERS = 8
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
MAX_CHEST_SHELL_OVERLAP_RATIO = 0.03
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
    head_mask: np.ndarray | None,
) -> tuple[PlacementChoice | None, list[str]]:
    text_box, bubble_box, anchor_x, anchor_y = make_layout_boxes_from_text_position(
        dimensions,
        int(round(text_left)),
        int(round(text_top)),
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
    head_text_overlap = rect_mask_overlap_area(head_mask, text_box) / max(1, text_box.area) if head_mask is not None else 0.0
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

    person_overlap_ratio = rect_mask_overlap_area(body_regions.person_mask, text_box) / max(1, text_box.area)
    if person_overlap_ratio > 0:
        penalties["text_person_overlap"] = (
            person_overlap_ratio * PERSON_OVERLAP_WEIGHT * TEXT_PERSON_OVERLAP_MULTIPLIER
        )
    if head_text_overlap > 0:
        penalties["text_head_overlap"] = head_text_overlap * HEAD_TEXT_OVERLAP_WEIGHT

    bubble_person_overlap_ratio = rect_mask_overlap_area(body_regions.person_mask, bubble_box) / max(1, bubble_box.area)
    if bubble_person_overlap_ratio > 0:
        penalties["bubble_shell_person_overlap"] = (
            bubble_person_overlap_ratio
            * BUBBLE_SHELL_PERSON_WEIGHT
            * BUBBLE_SHELL_PERSON_OVERLAP_MULTIPLIER
        )

    face_shell_overlap = rect_mask_overlap_area(body_regions.face_mask, bubble_box) / max(1, bubble_box.area)
    chest_shell_overlap = rect_mask_overlap_area(body_regions.chest_mask, bubble_box) / max(1, bubble_box.area)
    lower_shell_overlap = rect_mask_overlap_area(body_regions.lower_mask, bubble_box) / max(1, bubble_box.area)
    head_shell_overlap = (
        rect_mask_overlap_area(head_mask, bubble_box) / max(1, bubble_box.area) if head_mask is not None else 0.0
    )
    if chest_shell_overlap > MAX_CHEST_SHELL_OVERLAP_RATIO:
        invalid_reasons.append("chest_shell_overlap")
    if head_shell_overlap > MAX_HEAD_SHELL_OVERLAP_RATIO:
        invalid_reasons.append("head_shell_overlap")
    if invalid_reasons:
        return None, invalid_reasons

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


def _collect_candidates(
    *,
    dimensions: BubbleDimensions,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    head_mask: np.ndarray | None,
) -> tuple[list[CandidateOption], dict[str, int], list[dict[str, Any]], dict[str, int]]:
    invalid_counts: dict[str, int] = {}
    candidate_rows: list[tuple[Candidate, PlacementChoice]] = []
    seen: set[tuple[int, int]] = set()

    def add_candidate(text_left: int, text_top: int, source: str) -> None:
        candidate = Candidate(int(round(text_left)), int(round(text_top)), source)
        key = candidate_key(candidate)
        if key in seen:
            return
        seen.add(key)
        choice, invalid_reasons = _build_choice(
            dimensions=dimensions,
            text_left=candidate.text_left,
            text_top=candidate.text_top,
            image_width=image_width,
            image_height=image_height,
            body_regions=body_regions,
            head_mask=head_mask,
        )
        if choice is None:
            for reason in invalid_reasons:
                invalid_counts[reason] = invalid_counts.get(reason, 0) + 1
            return
        choice.source = source
        candidate_rows.append((candidate, choice))

    max_left = max(0, image_width - dimensions.text_width)
    max_top = max(0, image_height - dimensions.text_height)
    x_step = max(12, dimensions.text_width // 2)
    y_step = max(12, dimensions.text_height // 4)
    for text_top in _iter_positions(max_top, y_step):
        for text_left in _iter_positions(max_left, x_step):
            add_candidate(text_left, text_top, "scan-grid")

    for text_left, text_top, source in _edge_seed_positions(
        dimensions=dimensions,
        image_width=image_width,
        image_height=image_height,
        body_regions=body_regions,
    ):
        add_candidate(text_left, text_top, source)

    candidate_rows.sort(
        key=lambda row: (
            row[1].total_score,
            row[1].text_box.top,
            -row[1].text_box.right,
        )
    )

    for candidate, choice in list(candidate_rows[: min(8, len(candidate_rows))]):
        for dx, dy in _local_refinement_offsets(dimensions=dimensions):
            add_candidate(candidate.text_left + dx, candidate.text_top + dy, "local-refine")

    candidate_rows.sort(
        key=lambda row: (
            row[1].total_score,
            row[1].text_box.top,
            -row[1].text_box.right,
        )
    )

    binned: dict[tuple[int, int], list[tuple[Candidate, PlacementChoice]]] = {}
    for row in candidate_rows:
        bin_key = _coarse_bin_key(
            center_x=row[1].text_box.center_x,
            center_y=row[1].text_box.center_y,
            image_width=image_width,
            image_height=image_height,
        )
        binned.setdefault(bin_key, []).append(row)
    for bin_rows in binned.values():
        bin_rows.sort(key=lambda row: (row[1].total_score, row[1].text_box.top, -row[1].text_box.right))

    retained_rows: list[tuple[Candidate, PlacementChoice]] = []
    retained_keys: set[tuple[int, int]] = set()
    bin_counts: dict[str, int] = {}
    for bin_key in sorted(binned):
        keep_rows = binned[bin_key][:MAX_CANDIDATES_PER_BIN]
        bin_counts[f"{bin_key[0]}-{bin_key[1]}"] = len(keep_rows)
        for candidate, choice in keep_rows:
            key = candidate_key(candidate)
            if key in retained_keys:
                continue
            retained_keys.add(key)
            retained_rows.append((candidate, choice))

    for candidate, choice in candidate_rows:
        if len(retained_rows) >= MAX_CANDIDATES_PER_BUBBLE:
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
        options, invalid_counts, top_candidates, bin_counts = _collect_candidates(
            dimensions=dimensions,
            image_width=image_width,
            image_height=image_height,
            body_regions=body_regions,
            head_mask=head_mask,
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
            }
        )
        if not options:
            debug_payload = {
                "solver": "cp-sat",
                "reading_model": READING_MODEL,
                "image_width": image_width,
                "image_height": image_height,
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
                    image_width * 0.18,
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
        selected_left = model.NewIntVar(0, image_width, f"selected_left_{bubble_index}")
        selected_right = model.NewIntVar(0, image_width, f"selected_right_{bubble_index}")
        selected_top = model.NewIntVar(0, image_height, f"selected_top_{bubble_index}")
        selected_bottom = model.NewIntVar(0, image_height, f"selected_bottom_{bubble_index}")
        center_x = model.NewIntVar(0, image_width, f"center_x_{bubble_index}")
        center_y = model.NewIntVar(0, image_height, f"center_y_{bubble_index}")

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
        min_left = model.NewIntVar(0, image_width, "min_text_left")
        max_right = model.NewIntVar(0, image_width, "max_text_right")
        min_top = model.NewIntVar(0, image_height, "min_text_top")
        max_bottom = model.NewIntVar(0, image_height, "max_text_bottom")
        model.AddMinEquality(min_left, selected_lefts)
        model.AddMaxEquality(max_right, selected_rights)
        model.AddMinEquality(min_top, selected_tops)
        model.AddMaxEquality(max_bottom, selected_bottoms)

        horizontal_span = model.NewIntVar(0, image_width, "horizontal_span")
        vertical_span = model.NewIntVar(0, image_height, "vertical_span")
        model.Add(horizontal_span == max_right - min_left)
        model.Add(vertical_span == max_bottom - min_top)

        min_center_y = model.NewIntVar(0, image_height, "min_center_y")
        max_center_y = model.NewIntVar(0, image_height, "max_center_y")
        model.AddMinEquality(min_center_y, center_ys)
        model.AddMaxEquality(max_center_y, center_ys)
        vertical_center_span = model.NewIntVar(0, image_height, "vertical_center_span")
        model.Add(vertical_center_span == max_center_y - min_center_y)

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

        horizontal_span_deficit = model.NewIntVar(0, image_width, "horizontal_span_deficit")
        vertical_span_deficit = model.NewIntVar(0, image_height, "vertical_span_deficit")
        vertical_center_span_deficit = model.NewIntVar(0, image_height, "vertical_center_span_deficit")
        model.Add(horizontal_span_deficit >= target_horizontal_span - horizontal_span)
        model.Add(horizontal_span_deficit >= 0)
        model.Add(vertical_span_deficit >= target_vertical_span - vertical_span)
        model.Add(vertical_span_deficit >= 0)
        target_vertical_center_span = min(
            image_height,
            max(
                int(round(body_regions.person_bbox.height * 0.26)),
                int(round(image_height * 0.18)),
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
            _choice_from_option(option, bubble_id=reflow_plan.bubble_id, sentence_ids=list(reflow_plan.sentence_ids))
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
