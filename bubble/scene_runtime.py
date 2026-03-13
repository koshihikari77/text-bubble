from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bubble.models import (
    AssignmentBubblePlan,
    BubblePlan,
    ReflowBubblePlan,
    SceneBubblePlan,
)
from bubble.validation import compose_bubble_plans


@dataclass(frozen=True)
class LLMRoute:
    server: str
    model: str


@dataclass(frozen=True)
class RenderConfig:
    font_path: str | None
    font_family: str | None
    bubble_asset: Path
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


def _dialogue_text(dialogue_lines: list[str]) -> str:
    return "\n".join(dialogue_lines)


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts"


def _import_cp_sat_scene_solver() -> Any:
    scripts_dir = _scripts_dir()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("cp_sat_scene_solver")


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
    solver_module = _import_cp_sat_scene_solver()

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
        bubble_asset=config.bubble_asset,
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
    face_mask: Any,
    person_mask: Any,
    chest_mask: Any | None,
    lower_mask: Any | None,
    head_mask: Any | None,
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
    _, scene_plans = infer_scene_stage(
        image_path=image_path,
        dialogue_lines=dialogue_lines,
        route=scene_route,
        temperature=temperature,
    )
    if face_mask is None or person_mask is None:
        scene_bundle = compose_scene_bundle(
            dialogue_lines=dialogue_lines,
            reflow_plans=reflow_plans,
            scene_plans=scene_plans,
            source="scene-llm",
        )
    else:
        scene_bundle = materialize_scene_bundle(
            dialogue_lines=dialogue_lines,
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
            source="scene-llm",
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
