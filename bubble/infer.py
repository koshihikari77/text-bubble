from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bubble.llm import (
    build_plan_schema,
    build_reflow_schema,
    build_reflow_user_prompt,
    build_scene_plan_schema,
    build_scene_user_prompt,
    build_user_prompt,
    encode_image_as_data_url,
    load_prompt_text,
    post_chat_completion,
)
from bubble.models import AssignmentBubblePlan, BubblePlan, ReflowBubblePlan, SceneBubblePlan
from bubble.validation import (
    extract_plan,
    extract_reflow_plan,
    extract_scene_plan,
    validate_assignment_plans,
    validate_reflow_plans,
)


def split_dialogue_lines(dialogue: str) -> list[str]:
    lines = [line.strip() for line in dialogue.splitlines() if line.strip()]
    if lines:
        return lines
    stripped = dialogue.strip()
    return [stripped] if stripped else []


def text_for_sentence_ids(dialogue_lines: list[str], sentence_ids: list[int]) -> str:
    return "".join(dialogue_lines[sentence_id - 1] for sentence_id in sentence_ids)


def build_assignment_plans(dialogue_lines: list[str]) -> list[AssignmentBubblePlan]:
    return [
        AssignmentBubblePlan(
            bubble_id=f"b{index}",
            sentence_ids=[index],
        )
        for index in range(1, len(dialogue_lines) + 1)
    ]


def infer_reflow_columns_for_bubble(
    server: str,
    model: str,
    dialogue_lines: list[str],
    assignment_plan: AssignmentBubblePlan,
    temperature: float,
) -> ReflowBubblePlan:
    prompt = build_reflow_user_prompt(dialogue_lines, assignment_plan)
    response = post_chat_completion(
        server=server,
        model=model,
        prompt=prompt,
        image_data_url=None,
        temperature=temperature,
        schema=build_reflow_schema(),
        system_prompt=load_prompt_text("reflow_system.txt"),
        enable_thinking=True,
    )
    return extract_reflow_plan(response, dialogue_lines, assignment_plan)


def reflow_assignment_plans(
    dialogue_lines: list[str],
    assignment_plans: list[AssignmentBubblePlan],
    server: str,
    model: str,
    temperature: float,
    workers: int = 4,
) -> list[ReflowBubblePlan]:
    if workers < 1:
        raise RuntimeError("reflow workers must be >= 1")
    if len(assignment_plans) <= 1 or workers == 1:
        reflow_plans: list[ReflowBubblePlan] = []
        for assignment_plan in assignment_plans:
            reflow_plans.append(
                infer_reflow_columns_for_bubble(
                    server=server,
                    model=model,
                    dialogue_lines=dialogue_lines,
                    assignment_plan=assignment_plan,
                    temperature=temperature,
                )
            )
        return validate_reflow_plans(dialogue_lines, reflow_plans)

    max_workers = min(workers, len(assignment_plans))
    by_bubble_id: dict[str, ReflowBubblePlan] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_assignment = {
            executor.submit(
                infer_reflow_columns_for_bubble,
                server,
                model,
                dialogue_lines,
                assignment_plan,
                temperature,
            ): assignment_plan
            for assignment_plan in assignment_plans
        }
        for future in as_completed(future_to_assignment):
            assignment_plan = future_to_assignment[future]
            try:
                plan = future.result()
            except Exception as exc:  # noqa: BLE001
                for pending in future_to_assignment:
                    pending.cancel()
                raise RuntimeError(f"reflow failed for {assignment_plan.bubble_id}: {exc}") from exc
            by_bubble_id[assignment_plan.bubble_id] = plan

    reflow_plans: list[ReflowBubblePlan] = []
    for assignment_plan in assignment_plans:
        reflow_plans.append(
            by_bubble_id[assignment_plan.bubble_id]
        )
    return validate_reflow_plans(dialogue_lines, reflow_plans)


def infer_bubble_plans(
    image_path: Path,
    server: str,
    model: str,
    dialogue: str,
    temperature: float,
) -> tuple[list[str], list[BubblePlan]]:
    dialogue_lines = split_dialogue_lines(dialogue)
    if not dialogue_lines:
        raise RuntimeError("dialogue must contain at least one non-empty line")
    image_data_url = encode_image_as_data_url(image_path)
    prompt = build_user_prompt(dialogue_lines)
    response = post_chat_completion(
        server=server,
        model=model,
        prompt=prompt,
        image_data_url=image_data_url,
        temperature=temperature,
        schema=build_plan_schema(len(dialogue_lines)),
        n_predict=max(220, 96 * len(dialogue_lines)),
    )
    return dialogue_lines, extract_plan(response, dialogue_lines)


def infer_scene_bubble_plans(
    image_path: Path,
    server: str,
    model: str,
    dialogue: str,
    temperature: float,
) -> tuple[list[str], list[SceneBubblePlan]]:
    dialogue_lines = split_dialogue_lines(dialogue)
    if not dialogue_lines:
        raise RuntimeError("dialogue must contain at least one non-empty line")
    image_data_url = encode_image_as_data_url(image_path)
    prompt = build_scene_user_prompt(dialogue_lines)
    response = post_chat_completion(
        server=server,
        model=model,
        prompt=prompt,
        image_data_url=image_data_url,
        temperature=temperature,
        schema=build_scene_plan_schema(len(dialogue_lines)),
        n_predict=max(220, 96 * len(dialogue_lines)),
    )
    return dialogue_lines, extract_scene_plan(response, dialogue_lines)


def infer_assignment_plans(dialogue: str) -> tuple[list[str], list[AssignmentBubblePlan]]:
    dialogue_lines = split_dialogue_lines(dialogue)
    if not dialogue_lines:
        raise RuntimeError("dialogue must contain at least one non-empty line")
    plans = build_assignment_plans(dialogue_lines)
    return dialogue_lines, validate_assignment_plans(dialogue_lines, plans)


def infer_reflow_plans(
    server: str,
    model: str,
    dialogue: str,
    temperature: float,
    assignment_plans: list[AssignmentBubblePlan] | None = None,
    reflow_workers: int = 4,
) -> tuple[list[str], list[ReflowBubblePlan]]:
    dialogue_lines = split_dialogue_lines(dialogue)
    if not dialogue_lines:
        raise RuntimeError("dialogue must contain at least one non-empty line")
    validated_assignments = validate_assignment_plans(
        dialogue_lines,
        assignment_plans if assignment_plans is not None else build_assignment_plans(dialogue_lines),
    )
    plans = reflow_assignment_plans(
        dialogue_lines,
        validated_assignments,
        server=server,
        model=model,
        temperature=temperature,
        workers=reflow_workers,
    )
    return dialogue_lines, validate_reflow_plans(dialogue_lines, plans)
