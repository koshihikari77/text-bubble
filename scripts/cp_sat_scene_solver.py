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
    CONTINUITY_DISTANCE_WEIGHT,
    FACE_FAR_WEIGHT,
    FACE_NEAR_WEIGHT,
    FACE_SLOT_HEIGHT_WEIGHT,
    FACE_SLOT_SIDE_WEIGHT,
    FACE_SIDE_GAP_WEIGHT,
    FLOW_DIRECTION_WEIGHT,
    IDEAL_EDGE_MARGIN_WEIGHT,
    LEFT_START_BELOW_RIGHT_WEIGHT,
    NEXT_COLUMN_GAP_WEIGHT,
    NEXT_COLUMN_RESET_DOWNWARD_WEIGHT,
    OUTER_EDGE_MARGIN_WEIGHT,
    PERSON_OVERLAP_WEIGHT,
    QUADRANT_DISTANCE_WEIGHT,
    QUADRANT_MISMATCH_PENALTY,
    READING_COLUMN_RESET_UPWARD_WEIGHT,
    READING_RIGHTWARD_WEIGHT,
    READING_UPWARD_WEIGHT,
    SAME_COLUMN_GAP_WEIGHT,
    SAME_SIDE_ALIGN_WEIGHT,
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
    evaluate_candidate,
    expand_rect,
    generate_candidates,
    load_binary_mask,
    make_layout_boxes_from_text_position,
    rect_distance,
    rect_mask_overlap_area,
    rects_intersect,
    render_debug_overlay,
    point_in_rect,
    slot_regions,
    slot_side,
)


ALL_SLOTS: tuple[str, ...] = (
    "top-right",
    "mid-right",
    "bottom-right",
    "top-left",
    "mid-left",
    "bottom-left",
)
READING_MODEL = "rtl-columns"
OBJECTIVE_SCALE = 100
MAX_SOLVE_SECONDS = 10.0
NUM_SEARCH_WORKERS = 8
MAX_CANDIDATES_PER_SLOT = 8
MAX_CANDIDATES_PER_BUBBLE = 42
TWO_BUBBLE_SAME_SIDE_PENALTY = 420.0
FACE_SIDE_NEAR_BAND_WEIGHT = 9.0
FACE_SIDE_FAR_BAND_WEIGHT = 3.2
SIDE_BALANCE_WEIGHT = 2200.0
HORIZONTAL_SPAN_DEFICIT_WEIGHT = 4.0
VERTICAL_SPAN_DEFICIT_WEIGHT = 1.4
FIRST_BUBBLE_RIGHTWARD_WEIGHT = 1.8
FIRST_BUBBLE_TOPWARD_WEIGHT = 1.2
FIRST_BUBBLE_SLOT_PENALTY = 220.0
SAME_SLOT_REPEAT_PENALTY = 640.0
SAME_ROW_REPEAT_PENALTY = 180.0
SAME_SIDE_MIN_TOP_STEP_WEIGHT = 8.0
LEFT_START_BELOW_RIGHT_WEIGHT = 7.4
NEXT_COLUMN_RESET_DOWNWARD_WEIGHT = 3.8
LATER_BUBBLE_TOP_ROW_PENALTY = 950.0


def _slot_row(slot: str) -> str:
    if slot.startswith("top-"):
        return "top"
    if slot.startswith("mid-"):
        return "mid"
    return "bottom"


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
            "penalties": {key: round(value, 3) for key, value in self.unary_penalties.items()},
        }


def _scaled_score(value: float) -> int:
    return max(0, int(round(value * OBJECTIVE_SCALE)))


def _face_side_gap(choice: PlacementChoice, body_regions: BodyRegions) -> int:
    if slot_side(choice.slot) == "right":
        return choice.text_box.left - body_regions.face_bbox.right
    return body_regions.face_bbox.left - choice.text_box.right


def _face_side_band_penalties(
    *,
    choice: PlacementChoice,
    body_regions: BodyRegions,
    image_width: int,
) -> dict[str, float]:
    side_gap = _face_side_gap(choice, body_regions)
    if slot_side(choice.slot) == "right":
        available_span = image_width - body_regions.face_bbox.right - choice.text_box.width
    else:
        available_span = body_regions.face_bbox.left - choice.text_box.width
    available_span = max(24, available_span)
    band_low = max(18, int(round(body_regions.face_bbox.width * 0.18)))
    band_high = max(
        band_low + 16,
        min(
            max(40, int(round(body_regions.face_bbox.width * 0.42))),
            int(round(available_span * 0.62)),
        ),
    )
    penalties: dict[str, float] = {}
    if side_gap < band_low:
        penalties["face_side_too_near_band"] = (band_low - side_gap) * FACE_SIDE_NEAR_BAND_WEIGHT
    if side_gap > band_high:
        penalties["face_side_too_far_band"] = (side_gap - band_high) * FACE_SIDE_FAR_BAND_WEIGHT
    return penalties


def _first_bubble_penalties(
    *,
    choice: PlacementChoice,
    body_regions: BodyRegions,
    image_width: int,
    image_height: int,
    bubble_index: int,
    bubble_count: int,
) -> dict[str, float]:
    if bubble_index != 0 or bubble_count < 4:
        return {}
    penalties: dict[str, float] = {}
    if choice.slot != "top-right":
        penalties["first_bubble_slot"] = FIRST_BUBBLE_SLOT_PENALTY
    right_target = min(
        image_width - max(28, int(round(image_width * 0.035))),
        max(
            body_regions.face_bbox.right + max(36, int(round(body_regions.face_bbox.width * 0.55))),
            int(round(image_width * 0.86)),
        ),
    )
    right_deficit = max(0, right_target - choice.text_box.right)
    if right_deficit > 0:
        penalties["first_bubble_rightward"] = right_deficit * FIRST_BUBBLE_RIGHTWARD_WEIGHT
    top_target = max(18, int(round(image_height * 0.08)))
    if choice.text_box.top > top_target:
        penalties["first_bubble_topward"] = (choice.text_box.top - top_target) * FIRST_BUBBLE_TOPWARD_WEIGHT
    return penalties


def _later_bubble_row_penalties(
    *,
    choice: PlacementChoice,
    bubble_index: int,
    bubble_count: int,
) -> dict[str, float]:
    if bubble_count < 4 or bubble_index < 2:
        return {}
    penalties: dict[str, float] = {}
    row = _slot_row(choice.slot)
    if row == "top":
        penalties["later_bubble_top_row"] = (bubble_index - 1) * LATER_BUBBLE_TOP_ROW_PENALTY
    return penalties


def _collect_unique_candidates(
    *,
    image_width: int,
    image_height: int,
    dimensions: BubbleDimensions,
    body_regions: BodyRegions,
    preferred_slot: str,
) -> list[Candidate]:
    base_candidates = generate_candidates(
        image_width=image_width,
        image_height=image_height,
        dimensions=dimensions,
        body_regions=body_regions,
        preferred_slot=preferred_slot,
    )
    candidates = list(base_candidates)
    seen = {candidate_key(candidate) for candidate in candidates}
    desired_margin = max(32, int(round(min(image_width, image_height) * 0.065)))
    outer_margin = max(56, int(round(min(image_width, image_height) * 0.09)))
    slot_rect = slot_regions(image_width, image_height, body_regions.person_bbox)[preferred_slot]

    def add_candidate(text_left: int, text_top: int, source: str) -> None:
        candidate = Candidate(int(round(text_left)), int(round(text_top)), source)
        key = candidate_key(candidate)
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    if slot_side(preferred_slot) == "right":
        side_positions = (
            image_width - outer_margin - dimensions.text_width,
            image_width - outer_margin - dimensions.text_width - max(16, dimensions.text_width // 3),
        )
    else:
        side_positions = (
            outer_margin,
            outer_margin + max(16, dimensions.text_width // 3),
        )
    vertical_positions = (
        slot_rect.top + desired_margin,
        max(slot_rect.top, int(round(slot_rect.center_y - dimensions.text_height / 2.0))),
        slot_rect.bottom - desired_margin - dimensions.text_height,
    )
    for text_left in side_positions:
        for text_top in vertical_positions:
            add_candidate(text_left, text_top, "cp-sat-side")
    return candidates


def _prepare_candidate_options(
    *,
    reflow_plan: ReflowBubblePlan,
    image_width: int,
    image_height: int,
    body_regions: BodyRegions,
    font_size: int,
    bubble_index: int,
    bubble_count: int,
) -> tuple[BubbleDimensions, list[CandidateOption], dict[str, int], list[dict[str, Any]], dict[str, int]]:
    dimensions = estimate_bubble_dimensions(
        reflow_plan,
        image_width=image_width,
        image_height=image_height,
        font_size=font_size,
    )
    invalid_counts: dict[str, int] = {}
    options: list[CandidateOption] = []
    per_slot_counts: dict[str, int] = {}
    option_index = 0
    for slot in ALL_SLOTS:
        slot_rect = slot_regions(image_width, image_height, body_regions.person_bbox)[slot]
        slot_options: list[CandidateOption] = []
        for candidate in _collect_unique_candidates(
            image_width=image_width,
            image_height=image_height,
            dimensions=dimensions,
            body_regions=body_regions,
            preferred_slot=slot,
        ):
            choice, invalid_reasons = evaluate_candidate(
                candidate=candidate,
                dimensions=dimensions,
                image_width=image_width,
                image_height=image_height,
                body_regions=body_regions,
                preferred_slot=slot,
                template_name=READING_MODEL,
                previous_choice=None,
                placed_choices=[],
                placed_text_boxes=[],
            )
            if choice is None:
                for reason in invalid_reasons:
                    invalid_counts[reason] = invalid_counts.get(reason, 0) + 1
                continue
            if not point_in_rect(choice.text_box.center_x, choice.text_box.center_y, slot_rect):
                invalid_counts["slot_region_mismatch"] = invalid_counts.get("slot_region_mismatch", 0) + 1
                continue
            extra_penalties = _face_side_band_penalties(
                choice=choice,
                body_regions=body_regions,
                image_width=image_width,
            )
            extra_penalties.update(
                _first_bubble_penalties(
                    choice=choice,
                    body_regions=body_regions,
                    image_width=image_width,
                    image_height=image_height,
                    bubble_index=bubble_index,
                    bubble_count=bubble_count,
                )
            )
            extra_penalties.update(
                _later_bubble_row_penalties(
                    choice=choice,
                    bubble_index=bubble_index,
                    bubble_count=bubble_count,
                )
            )
            if extra_penalties:
                choice.penalties.update(extra_penalties)
                choice.total_score += sum(extra_penalties.values())
            slot_options.append(
                CandidateOption(
                    index=option_index,
                    candidate=candidate,
                    choice=choice,
                    unary_penalties=dict(choice.penalties),
                    unary_score=_scaled_score(choice.total_score),
                )
            )
            option_index += 1
        slot_options.sort(key=lambda item: (item.unary_score, item.choice.text_box.top, -item.choice.text_box.left))
        retained_slot_options = slot_options[:MAX_CANDIDATES_PER_SLOT]
        per_slot_counts[slot] = len(retained_slot_options)
        options.extend(retained_slot_options)

    options.sort(
        key=lambda item: (
            item.unary_score,
            0 if slot_side(item.choice.slot) == "right" else 1,
            item.choice.text_box.top,
            -item.choice.text_box.left,
        )
    )
    retained_options = options[:MAX_CANDIDATES_PER_BUBBLE]
    top_candidates = [option.to_debug_dict() for option in retained_options[:5]]
    return dimensions, retained_options, invalid_counts, top_candidates, per_slot_counts


def _pairwise_penalties(
    prev_choice: PlacementChoice,
    curr_choice: PlacementChoice,
    *,
    bubble_count: int,
) -> dict[str, float]:
    penalties: dict[str, float] = {}
    target_distance = max(prev_choice.text_box.height, curr_choice.text_box.height) * 0.68
    actual_distance = math.hypot(
        curr_choice.text_box.center_x - prev_choice.text_box.center_x,
        curr_choice.text_box.center_y - prev_choice.text_box.center_y,
    )
    if actual_distance > target_distance:
        penalties["continuity"] = (actual_distance - target_distance) * CONTINUITY_DISTANCE_WEIGHT

    x_tolerance = max(prev_choice.text_box.width, curr_choice.text_box.width) * 0.35
    y_tolerance = max(prev_choice.text_box.height, curr_choice.text_box.height) * 0.16
    x_delta = prev_choice.text_box.center_x - curr_choice.text_box.center_x
    y_delta = curr_choice.text_box.center_y - prev_choice.text_box.center_y
    horizontal_gap = prev_choice.text_box.left - curr_choice.text_box.right
    vertical_gap = curr_choice.text_box.top - prev_choice.text_box.bottom
    same_side = slot_side(prev_choice.slot) == slot_side(curr_choice.slot)
    top_step = curr_choice.text_box.top - prev_choice.text_box.top

    if x_delta < -x_tolerance:
        penalties["reading_rightward"] = (-x_delta - x_tolerance) * READING_RIGHTWARD_WEIGHT

    if same_side:
        min_top_step = max(14, int(round(min(prev_choice.text_box.height, curr_choice.text_box.height) * 0.16)))
        if top_step < min_top_step:
            penalties["same_side_min_top_step"] = (min_top_step - top_step) * SAME_SIDE_MIN_TOP_STEP_WEIGHT
        if y_delta < -y_tolerance:
            penalties["reading_upward"] = (-y_delta - y_tolerance) * READING_UPWARD_WEIGHT
        desired_vertical_gap = max(4, int(round(min(prev_choice.text_box.height, curr_choice.text_box.height) * 0.04)))
        if vertical_gap > desired_vertical_gap:
            penalties["same_column_gap"] = (vertical_gap - desired_vertical_gap) * SAME_COLUMN_GAP_WEIGHT
        if slot_side(curr_choice.slot) == "right":
            alignment_gap = abs(curr_choice.text_box.right - prev_choice.text_box.right)
        else:
            alignment_gap = abs(curr_choice.text_box.left - prev_choice.text_box.left)
        if alignment_gap > 0:
            penalties["same_side_alignment"] = alignment_gap * SAME_SIDE_ALIGN_WEIGHT
        if prev_choice.slot == curr_choice.slot:
            penalties["same_slot_repeat"] = SAME_SLOT_REPEAT_PENALTY
        elif _slot_row(prev_choice.slot) == _slot_row(curr_choice.slot):
            penalties["same_row_repeat"] = SAME_ROW_REPEAT_PENALTY
        if bubble_count == 2:
            penalties["two_bubble_same_side"] = TWO_BUBBLE_SAME_SIDE_PENALTY
    else:
        allowed_reset = max(prev_choice.text_box.height, curr_choice.text_box.height) * 0.35
        if y_delta < -allowed_reset:
            penalties["reading_column_reset_upward"] = (
                -y_delta - allowed_reset
            ) * READING_COLUMN_RESET_UPWARD_WEIGHT
        desired_horizontal_gap = max(6, int(round(min(prev_choice.text_box.width, curr_choice.text_box.width) * 0.10)))
        if horizontal_gap > desired_horizontal_gap:
            penalties["next_column_gap"] = (horizontal_gap - desired_horizontal_gap) * NEXT_COLUMN_GAP_WEIGHT
        allowed_reset_downward = max(prev_choice.text_box.height, curr_choice.text_box.height) * 0.20
        reset_downward = curr_choice.text_box.top - prev_choice.text_box.top
        if reset_downward > allowed_reset_downward:
            penalties["next_column_reset_downward"] = (
                reset_downward - allowed_reset_downward
            ) * NEXT_COLUMN_RESET_DOWNWARD_WEIGHT
        if slot_side(curr_choice.slot) == "left" and curr_choice.text_box.top < prev_choice.text_box.top:
            penalties["left_starts_above_right"] = (
                prev_choice.text_box.top - curr_choice.text_box.top
            ) * LEFT_START_BELOW_RIGHT_WEIGHT
        drift = curr_choice.text_box.center_x - (prev_choice.text_box.center_x - max(18, min(curr_choice.text_box.width, curr_choice.text_box.height) // 8))
        if drift > 0:
            penalties["flow_direction"] = drift * FLOW_DIRECTION_WEIGHT

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

    for bubble_index, reflow_plan in enumerate(reflow_plans):
        dimensions, options, invalid_counts, top_candidates, per_slot_counts = _prepare_candidate_options(
            reflow_plan=reflow_plan,
            image_width=image_width,
            image_height=image_height,
            body_regions=body_regions,
            font_size=font_size,
            bubble_index=bubble_index,
            bubble_count=len(reflow_plans),
        )
        dimensions_by_bubble_id[reflow_plan.bubble_id] = dimensions
        candidate_debug.append(
            {
                "bubble_id": reflow_plan.bubble_id,
                "slots": list(ALL_SLOTS),
                "slot_valid_counts": per_slot_counts,
                "total_candidates": sum(per_slot_counts.values()),
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
                "dimensions": [dimensions.to_debug_dict() for dimensions in dimensions_by_bubble_id.values()],
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
            for left_option_index, left_option in enumerate(options_by_bubble[left_index]):
                for right_option_index, right_option in enumerate(options_by_bubble[right_index]):
                    if rects_intersect(
                        expanded_boxes[left_index][left_option_index],
                        expanded_boxes[right_index][right_option_index],
                    ):
                        model.Add(x_vars[left_index][left_option_index] + x_vars[right_index][right_option_index] <= 1)
                    left_side = slot_side(left_option.choice.slot)
                    right_side = slot_side(right_option.choice.slot)
                    if left_side == "left" and right_side == "right":
                        model.Add(x_vars[left_index][left_option_index] + x_vars[right_index][right_option_index] <= 1)
                    if len(options_by_bubble) == 2 and left_side == right_side:
                        model.Add(x_vars[left_index][left_option_index] + x_vars[right_index][right_option_index] <= 1)
                    elif left_side == right_side:
                        if right_option.choice.text_box.top < left_option.choice.text_box.top:
                            model.Add(x_vars[left_index][left_option_index] + x_vars[right_index][right_option_index] <= 1)

    for bubble_index in range(1, len(options_by_bubble)):
        for prev_option_index, prev_option in enumerate(options_by_bubble[bubble_index - 1]):
            for curr_option_index, curr_option in enumerate(options_by_bubble[bubble_index]):
                penalties = _pairwise_penalties(
                    prev_option.choice,
                    curr_option.choice,
                    bubble_count=len(reflow_plans),
                )
                pair_score = _scaled_score(sum(penalties.values()))
                if pair_score <= 0:
                    continue
                pair_var = model.NewBoolVar(f"pair_{bubble_index-1}_{prev_option.index}_{bubble_index}_{curr_option.index}")
                model.Add(pair_var <= x_vars[bubble_index - 1][prev_option_index])
                model.Add(pair_var <= x_vars[bubble_index][curr_option_index])
                model.Add(
                    pair_var
                    >= x_vars[bubble_index - 1][prev_option_index] + x_vars[bubble_index][curr_option_index] - 1
                )
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
    right_count_terms: list[cp_model.IntVar] = []
    for bubble_index, options in enumerate(options_by_bubble):
        selected_left = model.NewIntVar(0, image_width, f"selected_left_{bubble_index}")
        selected_right = model.NewIntVar(0, image_width, f"selected_right_{bubble_index}")
        model.Add(
            selected_left
            == sum(x_vars[bubble_index][option_index] * option.choice.text_box.left for option_index, option in enumerate(options))
        )
        model.Add(
            selected_right
            == sum(
                x_vars[bubble_index][option_index] * option.choice.text_box.right for option_index, option in enumerate(options)
            )
        )
        selected_lefts.append(selected_left)
        selected_rights.append(selected_right)
        right_count_terms.extend(
            x_vars[bubble_index][option_index]
            for option_index, option in enumerate(options)
            if slot_side(option.choice.slot) == "right"
        )

    if len(reflow_plans) >= 3:
        target_right_count = (len(reflow_plans) + 1) // 2
        right_count = model.NewIntVar(0, len(reflow_plans), "right_count")
        model.Add(right_count == sum(right_count_terms))
        right_count_deviation = model.NewIntVar(0, len(reflow_plans), "right_count_deviation")
        model.AddAbsEquality(right_count_deviation, right_count - target_right_count)
        objective_terms.append(right_count_deviation * _scaled_score(SIDE_BALANCE_WEIGHT))

    if len(reflow_plans) >= 2:
        min_left = model.NewIntVar(0, image_width, "min_text_left")
        max_right = model.NewIntVar(0, image_width, "max_text_right")
        model.AddMinEquality(min_left, selected_lefts)
        model.AddMaxEquality(max_right, selected_rights)
        span = model.NewIntVar(0, image_width, "horizontal_span")
        model.Add(span == max_right - min_left)
        target_span = min(
            image_width,
            max(
                int(round(body_regions.person_bbox.width * 0.78)),
                int(round(image_width * 0.52)),
            ),
        )
        span_deficit = model.NewIntVar(0, image_width, "horizontal_span_deficit")
        model.Add(span_deficit >= target_span - span)
        model.Add(span_deficit >= 0)
        objective_terms.append(span_deficit * _scaled_score(HORIZONTAL_SPAN_DEFICIT_WEIGHT))

        selected_tops: list[cp_model.IntVar] = []
        selected_bottoms: list[cp_model.IntVar] = []
        for bubble_index, options in enumerate(options_by_bubble):
            selected_top = model.NewIntVar(0, image_height, f"selected_top_{bubble_index}")
            selected_bottom = model.NewIntVar(0, image_height, f"selected_bottom_{bubble_index}")
            model.Add(
                selected_top
                == sum(x_vars[bubble_index][option_index] * option.choice.text_box.top for option_index, option in enumerate(options))
            )
            model.Add(
                selected_bottom
                == sum(
                    x_vars[bubble_index][option_index] * option.choice.text_box.bottom
                    for option_index, option in enumerate(options)
                )
            )
            selected_tops.append(selected_top)
            selected_bottoms.append(selected_bottom)
        min_top = model.NewIntVar(0, image_height, "min_text_top")
        max_bottom = model.NewIntVar(0, image_height, "max_text_bottom")
        model.AddMinEquality(min_top, selected_tops)
        model.AddMaxEquality(max_bottom, selected_bottoms)
        vertical_span = model.NewIntVar(0, image_height, "vertical_span")
        model.Add(vertical_span == max_bottom - min_top)
        target_vertical_span = min(
            image_height,
            max(
                int(round(body_regions.person_bbox.height * 0.40)),
                int(round(image_height * 0.28)),
            ),
        )
        vertical_span_deficit = model.NewIntVar(0, image_height, "vertical_span_deficit")
        model.Add(vertical_span_deficit >= target_vertical_span - vertical_span)
        model.Add(vertical_span_deficit >= 0)
        objective_terms.append(vertical_span_deficit * _scaled_score(VERTICAL_SPAN_DEFICIT_WEIGHT))

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
            "dimensions": [dimensions.to_debug_dict() for dimensions in dimensions_by_bubble_id.values()],
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
            for option_index, _ in enumerate(options)
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
    debug_payload = {
        "solver": "cp-sat",
        "selected_template": READING_MODEL,
        "reading_model": READING_MODEL,
        "image_width": image_width,
        "image_height": image_height,
        "font_size": font_size,
        "body_regions": body_regions.to_debug_dict(),
        "dimensions": [dimensions.to_debug_dict() for dimensions in dimensions_by_bubble_id.values()],
        "candidates": candidate_debug,
        "placements": [placement.to_debug_dict() for placement in selected_placements],
        "pairwise_penalties": pairwise_debug,
        "solve_status": solver.StatusName(status),
        "objective_value": round(solver.ObjectiveValue() / OBJECTIVE_SCALE, 3),
        "best_objective_bound": round(solver.BestObjectiveBound() / OBJECTIVE_SCALE, 3),
        "selected_slot_counts": {
            "right": sum(1 for placement in selected_placements if slot_side(placement.slot) == "right"),
            "left": sum(1 for placement in selected_placements if slot_side(placement.slot) == "left"),
        },
        "horizontal_span_px": max(placement.text_box.right for placement in selected_placements)
        - min(placement.text_box.left for placement in selected_placements),
        "vertical_span_px": max(placement.text_box.bottom for placement in selected_placements)
        - min(placement.text_box.top for placement in selected_placements),
        "max_time_in_seconds": MAX_SOLVE_SECONDS,
        "num_search_workers": NUM_SEARCH_WORKERS,
    }
    return PlacementSolution(
        selected_template=READING_MODEL,
        scene_plans=scene_plans,
        placements=selected_placements,
        debug_payload=debug_payload,
    )
