from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os

from bubble.models import (
    AssignmentBubblePlan,
    BubblePlan,
    ReflowBubblePlan,
    SceneBubblePlan,
)
from bubble.scene_planners import DEFAULT_SCENE_PLANNER, resolve_scene_planner
from bubble.validation import compose_bubble_plans


@dataclass(frozen=True)
class LLMRoute:
    server: str
    model: str


@dataclass(frozen=True)
class RenderConfig:
    font_path: str | None
    font_family: str | None
    bubble_asset: Path | None
    font_size: int
    text_renderer: str
    bubble_renderer: str
    text_letter_spacing: str
    text_word_spacing: str
    resvg_tu_override: bool


@dataclass
class ScenePlacementBundle:
    scene_plans: list[SceneBubblePlan]
    composed_plans: list[BubblePlan]
    evaluated_solution: Any | None
    debug_payload: dict[str, Any]


@dataclass
class RunPipelineResult:
    dialogue_lines: list[str]
    assignment_plans: list[AssignmentBubblePlan]
    reflow_plans: list[ReflowBubblePlan]
    scene_bundle: ScenePlacementBundle
    default_route: LLMRoute
    scene_route: LLMRoute
    reflow_workers: int


@dataclass(frozen=True)
class MaskBundle:
    face_mask: Any
    person_mask: Any
    chest_mask: Any | None
    lower_mask: Any | None
    head_mask: Any | None
    face_source: str


def default_scene_planner() -> str:
    return os.environ.get("TEXT_BUBBLE_SCENE_PLANNER", DEFAULT_SCENE_PLANNER)


def _dialogue_text(dialogue_lines: list[str]) -> str:
    return "\n".join(dialogue_lines)


def _cp_sat_module() -> Any:
    from bubble.scene_planners import cp_sat

    return cp_sat


def _import_cp_sat_scene_solver() -> Any:
    return _cp_sat_module()


def _mask_shape(mask: Any) -> tuple[int, int]:
    shape = getattr(mask, "shape", None)
    if not isinstance(shape, tuple) or len(shape) != 2:
        raise RuntimeError("mask must be 2-dimensional")
    return int(shape[0]), int(shape[1])


def _non_empty_mask(mask: Any) -> bool:
    cp_sat = _cp_sat_module()
    return bool(cp_sat.np.any(mask))


def _normalize_optional_mask(mask: Any | None, *, name: str, person_mask: Any) -> Any | None:
    if mask is None:
        return None
    cp_sat = _cp_sat_module()
    if _mask_shape(mask) != _mask_shape(person_mask):
        raise RuntimeError(f"{name} mask shape does not match person mask")
    normalized = mask.astype(bool) & person_mask
    if not cp_sat.np.any(normalized):
        return None
    return normalized


def load_mask_bundle(
    *,
    person_mask_path: Path,
    face_mask_path: Path,
    chest_mask_path: Path | None = None,
    lower_mask_path: Path | None = None,
    head_mask_path: Path | None = None,
) -> MaskBundle:
    cp_sat = _cp_sat_module()
    person_mask = cp_sat.load_binary_mask(person_mask_path)
    face_mask = cp_sat.load_binary_mask(face_mask_path)
    head_mask = cp_sat.load_binary_mask(head_mask_path) if head_mask_path is not None else None
    chest_mask = cp_sat.load_binary_mask(chest_mask_path) if chest_mask_path is not None else None
    lower_mask = cp_sat.load_binary_mask(lower_mask_path) if lower_mask_path is not None else None

    if _mask_shape(person_mask) != _mask_shape(face_mask):
        raise RuntimeError("person mask and face mask must have the same shape")
    if not _non_empty_mask(person_mask):
        raise RuntimeError("person mask is empty")
    face_source = "face"
    if not _non_empty_mask(face_mask):
        if head_mask is not None and _non_empty_mask(head_mask):
            face_mask = head_mask
            face_source = "head-fallback"
        else:
            raise RuntimeError("face mask is empty and head fallback is unavailable")
    head_mask = _normalize_optional_mask(head_mask, name="head", person_mask=person_mask)
    chest_mask = _normalize_optional_mask(chest_mask, name="chest", person_mask=person_mask)
    lower_mask = _normalize_optional_mask(lower_mask, name="lower", person_mask=person_mask)
    return MaskBundle(
        face_mask=face_mask,
        person_mask=person_mask,
        chest_mask=chest_mask,
        lower_mask=lower_mask,
        head_mask=head_mask,
        face_source=face_source,
    )


def _solver_module_classes() -> tuple[Any, Any, Any]:
    solver_module = _cp_sat_module()
    return solver_module, solver_module.Rect, solver_module.PlacementChoice


def resolve_scene_route(
    *,
    default_server: str,
    default_model: str,
    scene_server: str | None = None,
    scene_model: str | None = None,
) -> LLMRoute:
    return LLMRoute(
        server=(scene_server.strip() if scene_server and scene_server.strip() else default_server),
        model=(scene_model.strip() if scene_model and scene_model.strip() else default_model),
    )


def infer_scene_stage(
    *,
    image_path: Path,
    dialogue_lines: list[str],
    route: LLMRoute,
    temperature: float,
) -> tuple[list[str], list[SceneBubblePlan]]:
    from bubble.infer import infer_scene_bubble_plans

    return infer_scene_bubble_plans(
        image_path=image_path,
        server=route.server,
        model=route.model,
        dialogue=_dialogue_text(dialogue_lines),
        temperature=temperature,
    )


def materialize_scene_bundle(
    *,
    dialogue_lines: list[str],
    reflow_plans: list[ReflowBubblePlan],
    scene_plans: list[SceneBubblePlan],
    image_width: int,
    image_height: int,
    face_mask: Any,
    person_mask: Any,
    chest_mask: Any | None,
    lower_mask: Any | None,
    head_mask: Any | None,
    font_size: int,
    source: str,
) -> ScenePlacementBundle:
    solver_module = _cp_sat_module()

    evaluated_solution = solver_module.evaluate_scene_layout(
        reflow_plans=reflow_plans,
        scene_plans=scene_plans,
        image_width=image_width,
        image_height=image_height,
        face_mask=face_mask,
        person_mask=person_mask,
        chest_mask=chest_mask,
        lower_mask=lower_mask,
        head_mask=head_mask,
        font_size=font_size,
        source=source,
    )
    composed_plans = compose_bubble_plans(dialogue_lines, evaluated_solution.scene_plans, reflow_plans)
    debug_payload = dict(evaluated_solution.debug_payload)
    debug_payload["font_size"] = font_size
    debug_payload["placement_source"] = source
    return ScenePlacementBundle(
        scene_plans=evaluated_solution.scene_plans,
        composed_plans=composed_plans,
        evaluated_solution=evaluated_solution,
        debug_payload=debug_payload,
    )


def solve_cp_sat_scene_bundle(
    *,
    dialogue_lines: list[str],
    reflow_plans: list[ReflowBubblePlan],
    image_width: int,
    image_height: int,
    masks: MaskBundle,
    font_size: int,
    source: str = "cp-sat",
) -> ScenePlacementBundle:
    solver_module = _cp_sat_module()
    evaluated_solution = solver_module.solve_scene_layout(
        reflow_plans=reflow_plans,
        image_width=image_width,
        image_height=image_height,
        face_mask=masks.face_mask,
        person_mask=masks.person_mask,
        chest_mask=masks.chest_mask,
        lower_mask=masks.lower_mask,
        head_mask=masks.head_mask,
        font_size=font_size,
    )
    bundle = bundle_from_evaluated_solution(
        dialogue_lines=dialogue_lines,
        reflow_plans=reflow_plans,
        evaluated_solution=evaluated_solution,
        source=source,
    )
    bundle.debug_payload["face_source"] = masks.face_source
    bundle.debug_payload["font_size"] = font_size
    return bundle


def plan_scene(
    *,
    planner: str,
    image_path: Path,
    dialogue_lines: list[str],
    reflow_plans: list[ReflowBubblePlan],
    route: LLMRoute,
    temperature: float,
    image_width: int,
    image_height: int,
    masks: MaskBundle | None,
    font_size: int,
) -> ScenePlacementBundle:
    resolved_planner = resolve_scene_planner(planner)
    if resolved_planner == "cp-sat":
        if masks is None:
            raise RuntimeError("cp-sat planner requires person and face masks")
        return solve_cp_sat_scene_bundle(
            dialogue_lines=dialogue_lines,
            reflow_plans=reflow_plans,
            image_width=image_width,
            image_height=image_height,
            masks=masks,
            font_size=font_size,
            source="cp-sat",
        )
    _, scene_plans = infer_scene_stage(
        image_path=image_path,
        dialogue_lines=dialogue_lines,
        route=route,
        temperature=temperature,
    )
    if masks is None:
        return compose_scene_bundle(
            dialogue_lines=dialogue_lines,
            reflow_plans=reflow_plans,
            scene_plans=scene_plans,
            source="scene-llm",
        )
    return materialize_scene_bundle(
        dialogue_lines=dialogue_lines,
        reflow_plans=reflow_plans,
        scene_plans=scene_plans,
        image_width=image_width,
        image_height=image_height,
        face_mask=masks.face_mask,
        person_mask=masks.person_mask,
        chest_mask=masks.chest_mask,
        lower_mask=masks.lower_mask,
        head_mask=masks.head_mask,
        font_size=font_size,
        source="scene-llm",
    )


def bundle_from_evaluated_solution(
    *,
    dialogue_lines: list[str],
    reflow_plans: list[ReflowBubblePlan],
    evaluated_solution: Any,
    source: str,
) -> ScenePlacementBundle:
    composed_plans = compose_bubble_plans(dialogue_lines, evaluated_solution.scene_plans, reflow_plans)
    debug_payload = dict(evaluated_solution.debug_payload)
    debug_payload["placement_source"] = source
    return ScenePlacementBundle(
        scene_plans=evaluated_solution.scene_plans,
        composed_plans=composed_plans,
        evaluated_solution=evaluated_solution,
        debug_payload=debug_payload,
    )


def compose_scene_bundle(
    *,
    dialogue_lines: list[str],
    reflow_plans: list[ReflowBubblePlan],
    scene_plans: list[SceneBubblePlan],
    source: str,
) -> ScenePlacementBundle:
    return ScenePlacementBundle(
        scene_plans=scene_plans,
        composed_plans=compose_bubble_plans(dialogue_lines, scene_plans, reflow_plans),
        evaluated_solution=None,
        debug_payload={"placement_source": source},
    )


def serialize_evaluated_solution(evaluated_solution: Any) -> dict[str, Any]:
    return {
        "selected_template": evaluated_solution.selected_template,
        "scene_plans": [
            {
                "bubble_id": plan.bubble_id,
                "anchor_x": plan.anchor_x,
                "anchor_y": plan.anchor_y,
                "sentence_ids": list(plan.sentence_ids),
            }
            for plan in evaluated_solution.scene_plans
        ],
        "placements": [
            {
                "bubble_id": placement.bubble_id,
                "sentence_ids": list(placement.sentence_ids),
                "anchor_x_px": placement.anchor_x_px,
                "anchor_y_px": placement.anchor_y_px,
                "text_box": placement.text_box.as_dict(),
                "bubble_box": placement.bubble_box.as_dict(),
                "total_score": placement.total_score,
                "penalties": dict(placement.penalties),
                "source": placement.source,
                "template": placement.template,
                "slot": placement.slot,
            }
            for placement in evaluated_solution.placements
        ],
        "debug_payload": dict(evaluated_solution.debug_payload),
    }


def deserialize_evaluated_solution(payload: dict[str, Any]) -> Any:
    solver_module, rect_cls, placement_choice_cls = _solver_module_classes()
    scene_plans = [
        SceneBubblePlan(
            bubble_id=str(plan["bubble_id"]),
            anchor_x=float(plan["anchor_x"]),
            anchor_y=float(plan["anchor_y"]),
            sentence_ids=[int(item) for item in plan["sentence_ids"]],
        )
        for plan in payload["scene_plans"]
    ]
    placements = [
        placement_choice_cls(
            bubble_id=str(placement["bubble_id"]),
            sentence_ids=[int(item) for item in placement["sentence_ids"]],
            anchor_x_px=int(placement["anchor_x_px"]),
            anchor_y_px=int(placement["anchor_y_px"]),
            text_box=rect_cls(**placement["text_box"]),
            bubble_box=rect_cls(**placement["bubble_box"]),
            total_score=float(placement["total_score"]),
            penalties={str(key): float(value) for key, value in dict(placement["penalties"]).items()},
            source=str(placement["source"]),
            template=str(placement["template"]),
            slot=str(placement["slot"]),
        )
        for placement in payload["placements"]
    ]
    return solver_module.PlacementSolution(
        selected_template=str(payload["selected_template"]),
        scene_plans=scene_plans,
        placements=placements,
        debug_payload=dict(payload["debug_payload"]),
    )


def render_scene_bundle(
    *,
    image_path: Path,
    output_path: Path,
    bundle: ScenePlacementBundle,
    config: RenderConfig,
) -> None:
    from bubble.render import render_bubbles

    render_bubbles(
        image_path=image_path,
        output_path=output_path,
        plans=bundle.composed_plans,
        font_path=config.font_path,
        font_family=config.font_family,
        bubble_asset_override=config.bubble_asset,
        font_size=config.font_size,
        text_renderer=config.text_renderer,
        bubble_renderer=config.bubble_renderer,
        text_letter_spacing=config.text_letter_spacing,
        text_word_spacing=config.text_word_spacing,
        resvg_tu_override=config.resvg_tu_override,
    )


def run_pipeline(
    *,
    image_path: Path,
    dialogue_lines: list[str],
    default_route: LLMRoute,
    scene_route: LLMRoute,
    temperature: float,
    reflow_workers: int,
    image_width: int,
    image_height: int,
    planner: str,
    masks: MaskBundle | None,
    font_size: int,
) -> RunPipelineResult:
    from bubble.infer import infer_assignment_plans, infer_reflow_plans

    dialogue_text = _dialogue_text(dialogue_lines)
    _, assignment_plans = infer_assignment_plans(dialogue_text)
    _, reflow_plans = infer_reflow_plans(
        server=default_route.server,
        model=default_route.model,
        dialogue=dialogue_text,
        temperature=temperature,
        assignment_plans=assignment_plans,
        reflow_workers=reflow_workers,
    )
    scene_bundle = plan_scene(
        planner=planner,
        image_path=image_path,
        dialogue_lines=dialogue_lines,
        reflow_plans=reflow_plans,
        route=scene_route,
        temperature=temperature,
        image_width=image_width,
        image_height=image_height,
        masks=masks,
        font_size=font_size,
    )
    return RunPipelineResult(
        dialogue_lines=dialogue_lines,
        assignment_plans=assignment_plans,
        reflow_plans=reflow_plans,
        scene_bundle=scene_bundle,
        default_route=default_route,
        scene_route=scene_route,
        reflow_workers=reflow_workers,
    )
