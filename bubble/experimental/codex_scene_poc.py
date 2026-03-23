from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from bubble.experimental.beam_search_scene_solver import BodyRegions
from bubble.models import ReflowBubblePlan, SceneBubblePlan


MASK_PERSON_FILL = (214, 231, 255)
MASK_PERSON_OUTLINE = (63, 119, 191)
MASK_FACE_FILL = (255, 168, 168)
MASK_FACE_OUTLINE = (196, 68, 68)
MASK_CHEST_FILL = (255, 230, 145)
MASK_CHEST_OUTLINE = (191, 145, 18)
MASK_LOWER_FILL = (224, 170, 255)
MASK_LOWER_OUTLINE = (133, 58, 171)
MASK_HEAD_FILL = (198, 255, 210)
MASK_HEAD_OUTLINE = (58, 143, 84)
BOARD_BACKGROUND = (242, 239, 233)
BOARD_TEXT = (34, 34, 34)
PANEL_BACKGROUND = (255, 255, 255)
DEFAULT_CODEX_CLI_COMMAND = "codex"


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"scene edit JSON must be an object: {path}")
    return payload


def _mask_outline(mask: np.ndarray) -> np.ndarray:
    if mask.ndim != 2:
        raise RuntimeError("mask outline expects a 2D mask")
    if not np.any(mask):
        return np.zeros_like(mask, dtype=bool)
    padded = np.pad(mask.astype(bool), 1, mode="constant", constant_values=False)
    core = padded[1:-1, 1:-1]
    neighbors_all = (
        padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
        & padded[:-2, :-2]
        & padded[:-2, 2:]
        & padded[2:, :-2]
        & padded[2:, 2:]
    )
    return core & ~neighbors_all


def _apply_fill(rgba: np.ndarray, mask: np.ndarray, color: tuple[int, int, int], alpha: int) -> None:
    if alpha <= 0 or not np.any(mask):
        return
    color_array = np.array(color, dtype=np.uint8)
    mask_bool = mask.astype(bool)
    rgba[mask_bool, :3] = (
        rgba[mask_bool, :3].astype(np.uint16) * (255 - alpha) + color_array.astype(np.uint16) * alpha
    ) // 255


def _apply_outline(rgba: np.ndarray, mask: np.ndarray, color: tuple[int, int, int]) -> None:
    outline = _mask_outline(mask)
    if not np.any(outline):
        return
    rgba[outline, :3] = np.array(color, dtype=np.uint8)
    rgba[outline, 3] = 255


def render_mask_composite_image(
    *,
    image_width: int,
    image_height: int,
    person_mask: np.ndarray,
    face_mask: np.ndarray,
    body_regions: BodyRegions,
    head_mask: np.ndarray | None,
) -> Image.Image:
    rgba = np.full((image_height, image_width, 4), 255, dtype=np.uint8)
    rgba[:, :, 3] = 255

    _apply_fill(rgba, person_mask, MASK_PERSON_FILL, 116)
    _apply_fill(rgba, face_mask, MASK_FACE_FILL, 164)
    _apply_fill(rgba, body_regions.chest_mask, MASK_CHEST_FILL, 156)
    _apply_fill(rgba, body_regions.lower_mask, MASK_LOWER_FILL, 156)
    if head_mask is not None:
        _apply_fill(rgba, head_mask, MASK_HEAD_FILL, 144)

    _apply_outline(rgba, person_mask, MASK_PERSON_OUTLINE)
    _apply_outline(rgba, face_mask, MASK_FACE_OUTLINE)
    _apply_outline(rgba, body_regions.chest_mask, MASK_CHEST_OUTLINE)
    _apply_outline(rgba, body_regions.lower_mask, MASK_LOWER_OUTLINE)
    if head_mask is not None:
        _apply_outline(rgba, head_mask, MASK_HEAD_OUTLINE)

    image = Image.fromarray(rgba, mode="RGBA")
    draw = ImageDraw.Draw(image)
    legend = [
        ("person", MASK_PERSON_OUTLINE, MASK_PERSON_FILL),
        ("face", MASK_FACE_OUTLINE, MASK_FACE_FILL),
        ("chest", MASK_CHEST_OUTLINE, MASK_CHEST_FILL),
        ("lower", MASK_LOWER_OUTLINE, MASK_LOWER_FILL),
    ]
    if head_mask is not None:
        legend.append(("head", MASK_HEAD_OUTLINE, MASK_HEAD_FILL))

    legend_x = 16
    legend_y = 16
    for index, (label, outline_color, fill_color) in enumerate(legend):
        top = legend_y + index * 26
        draw.rectangle((legend_x, top, legend_x + 18, top + 18), fill=fill_color, outline=outline_color, width=2)
        draw.text((legend_x + 28, top + 2), label, fill=BOARD_TEXT)
    return image


def save_mask_composite(
    path: Path,
    *,
    image_width: int,
    image_height: int,
    person_mask: np.ndarray,
    face_mask: np.ndarray,
    body_regions: BodyRegions,
    head_mask: np.ndarray | None,
) -> None:
    image = render_mask_composite_image(
        image_width=image_width,
        image_height=image_height,
        person_mask=person_mask,
        face_mask=face_mask,
        body_regions=body_regions,
        head_mask=head_mask,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, compress_level=1, optimize=False)


def _fit_panel(image: Image.Image, cell_width: int, cell_height: int) -> Image.Image:
    panel = Image.new("RGB", (cell_width, cell_height), PANEL_BACKGROUND)
    if image.width == 0 or image.height == 0:
        return panel
    scale = min(cell_width / image.width, cell_height / image.height)
    fitted_width = max(1, int(round(image.width * scale)))
    fitted_height = max(1, int(round(image.height * scale)))
    fitted = image.convert("RGB").resize((fitted_width, fitted_height), resample=Image.Resampling.LANCZOS)
    left = (cell_width - fitted_width) // 2
    top = (cell_height - fitted_height) // 2
    panel.paste(fitted, (left, top))
    return panel


def _placeholder_panel(size: tuple[int, int], title: str, lines: list[str]) -> Image.Image:
    panel = Image.new("RGB", size, PANEL_BACKGROUND)
    draw = ImageDraw.Draw(panel)
    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline=(170, 170, 170), width=2)
    draw.text((20, 20), title, fill=BOARD_TEXT)
    for index, line in enumerate(lines):
        draw.text((20, 54 + index * 22), line, fill=(84, 84, 84))
    return panel


def render_codex_board_image(
    *,
    original_image: Image.Image,
    mask_composite_image: Image.Image,
    rendered_image: Image.Image | None,
    debug_overlay_image: Image.Image | None,
    title: str,
    notes: list[str],
) -> Image.Image:
    cell_width = max(original_image.width, mask_composite_image.width)
    cell_height = max(original_image.height, mask_composite_image.height)
    if rendered_image is not None:
        cell_width = max(cell_width, rendered_image.width)
        cell_height = max(cell_height, rendered_image.height)
    if debug_overlay_image is not None:
        cell_width = max(cell_width, debug_overlay_image.width)
        cell_height = max(cell_height, debug_overlay_image.height)

    header_height = 86
    label_height = 34
    board = Image.new(
        "RGB",
        (cell_width * 2 + 48, header_height + (cell_height + label_height) * 2 + 32),
        BOARD_BACKGROUND,
    )
    draw = ImageDraw.Draw(board)
    draw.text((20, 18), title, fill=BOARD_TEXT)
    for index, line in enumerate(notes[:3]):
        draw.text((20, 44 + index * 18), line, fill=(74, 74, 74))

    panels: list[tuple[str, Image.Image]] = [
        ("Original", _fit_panel(original_image, cell_width, cell_height)),
        ("Mask Composite", _fit_panel(mask_composite_image, cell_width, cell_height)),
        (
            "Rendered",
            _fit_panel(rendered_image, cell_width, cell_height)
            if rendered_image is not None
            else _placeholder_panel((cell_width, cell_height), "Rendered", ["No rendered output yet"]),
        ),
        (
            "Debug Overlay",
            _fit_panel(debug_overlay_image, cell_width, cell_height)
            if debug_overlay_image is not None
            else _placeholder_panel((cell_width, cell_height), "Debug Overlay", ["No placement overlay yet"]),
        ),
    ]

    for index, (label, panel) in enumerate(panels):
        col = index % 2
        row = index // 2
        left = 16 + col * (cell_width + 16)
        top = header_height + row * (cell_height + label_height)
        draw.text((left, top), label, fill=BOARD_TEXT)
        board.paste(panel, (left, top + 20))
        draw.rectangle((left, top + 20, left + cell_width - 1, top + 20 + cell_height - 1), outline=(176, 171, 160))
    return board


def save_codex_board(
    path: Path,
    *,
    original_image_path: Path,
    mask_composite_path: Path,
    rendered_path: Path | None,
    debug_overlay_path: Path | None,
    title: str,
    notes: list[str],
) -> None:
    board = render_codex_board_image(
        original_image=Image.open(original_image_path).convert("RGBA"),
        mask_composite_image=Image.open(mask_composite_path).convert("RGBA"),
        rendered_image=Image.open(rendered_path).convert("RGBA") if rendered_path is not None and rendered_path.exists() else None,
        debug_overlay_image=Image.open(debug_overlay_path).convert("RGBA") if debug_overlay_path is not None and debug_overlay_path.exists() else None,
        title=title,
        notes=notes,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    board.save(path, compress_level=1, optimize=False)


def build_editable_scene_template(
    *,
    planner_mode: str,
    reflow_plans: list[ReflowBubblePlan],
    scene_plans: list[SceneBubblePlan] | None,
    note: str | None = None,
) -> dict[str, Any]:
    scene_by_bubble_id = {plan.bubble_id: plan for plan in scene_plans or []}
    placements: list[dict[str, Any]] = []
    for reflow_plan in reflow_plans:
        existing = scene_by_bubble_id.get(reflow_plan.bubble_id)
        placements.append(
            {
                "bubble_id": reflow_plan.bubble_id,
                "anchor_x": None if existing is None else round(existing.anchor_x, 6),
                "anchor_y": None if existing is None else round(existing.anchor_y, 6),
                "sentence_ids": list(reflow_plan.sentence_ids),
                "columns": list(reflow_plan.columns),
            }
        )
    return {
        "mode": planner_mode,
        "notes": note or "Edit anchor_x / anchor_y in normalized [0, 1] coordinates.",
        "placements": placements,
    }


def load_scene_edit_json(path: Path, *, reflow_plans: list[ReflowBubblePlan]) -> tuple[str | None, list[SceneBubblePlan], str | None]:
    payload = _load_json(path)
    mode = payload.get("mode")
    if mode is not None and not isinstance(mode, str):
        raise RuntimeError(f"scene edit mode must be a string: {path}")
    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise RuntimeError(f"scene edit notes must be a string: {path}")

    placements = payload.get("placements")
    if not isinstance(placements, list) or not placements:
        raise RuntimeError(f"scene edit placements must be a non-empty list: {path}")

    expected_by_bubble_id = {plan.bubble_id: plan for plan in reflow_plans}
    seen: set[str] = set()
    scene_plans: list[SceneBubblePlan] = []
    for item in placements:
        if not isinstance(item, dict):
            raise RuntimeError(f"scene edit placement must be an object: {path}")
        bubble_id = item.get("bubble_id")
        anchor_x = item.get("anchor_x")
        anchor_y = item.get("anchor_y")
        if not isinstance(bubble_id, str) or bubble_id not in expected_by_bubble_id:
            raise RuntimeError(f"unknown bubble_id in scene edit JSON: {bubble_id}")
        if bubble_id in seen:
            raise RuntimeError(f"duplicate bubble_id in scene edit JSON: {bubble_id}")
        if not isinstance(anchor_x, (int, float)) or not isinstance(anchor_y, (int, float)):
            raise RuntimeError(f"anchor_x/anchor_y must be numbers for {bubble_id}")
        anchor_x = float(anchor_x)
        anchor_y = float(anchor_y)
        if not (0.0 <= anchor_x <= 1.0) or not (0.0 <= anchor_y <= 1.0):
            raise RuntimeError(f"anchor_x/anchor_y must be within [0, 1] for {bubble_id}")
        seen.add(bubble_id)
        reflow_plan = expected_by_bubble_id[bubble_id]
        scene_plans.append(
            SceneBubblePlan(
                bubble_id=bubble_id,
                anchor_x=anchor_x,
                anchor_y=anchor_y,
                sentence_ids=list(reflow_plan.sentence_ids),
            )
        )

    missing = [plan.bubble_id for plan in reflow_plans if plan.bubble_id not in seen]
    if missing:
        raise RuntimeError(f"scene edit JSON is missing placements for: {', '.join(missing)}")

    scene_by_bubble_id = {plan.bubble_id: plan for plan in scene_plans}
    ordered_scene_plans = [scene_by_bubble_id[plan.bubble_id] for plan in reflow_plans]
    return mode, ordered_scene_plans, notes


def summarize_debug_payload(debug_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "feasible": debug_payload.get("feasible"),
        "objective_value": debug_payload.get("objective_value"),
        "hard_conflict_count": len(debug_payload.get("hard_conflicts", [])),
        "invalid_placement_count": len(debug_payload.get("invalid_placements", [])),
        "horizontal_span_px": debug_payload.get("horizontal_span_px"),
        "vertical_span_px": debug_payload.get("vertical_span_px"),
        "min_pair_distance_px": debug_payload.get("min_pair_distance_px"),
    }


def build_prompt_context(
    *,
    planner_mode: str,
    reflow_json_path: Path,
    image_path: Path,
    image_width: int,
    image_height: int,
    dialogue_lines: list[str],
    reflow_plans: list[ReflowBubblePlan],
    body_regions: BodyRegions,
    current_scene_plans: list[SceneBubblePlan] | None,
    current_debug_payload: dict[str, Any] | None,
    previous_summary: dict[str, Any] | None,
    codex_board_path: Path,
    mask_composite_path: Path,
    debug_overlay_path: Path | None,
    rendered_path: Path | None,
) -> dict[str, Any]:
    current_by_bubble_id = {plan.bubble_id: plan for plan in current_scene_plans or []}
    bubbles: list[dict[str, Any]] = []
    for reflow_plan in reflow_plans:
        current = current_by_bubble_id.get(reflow_plan.bubble_id)
        bubbles.append(
            {
                "bubble_id": reflow_plan.bubble_id,
                "sentence_ids": list(reflow_plan.sentence_ids),
                "columns": list(reflow_plan.columns),
                "current_anchor": None
                if current is None
                else {
                    "anchor_x": round(current.anchor_x, 6),
                    "anchor_y": round(current.anchor_y, 6),
                },
            }
        )
    return {
        "planner_mode": planner_mode,
        "image": {
            "path": str(image_path),
            "width": image_width,
            "height": image_height,
        },
        "reflow_json": str(reflow_json_path),
        "dialogue_lines": dialogue_lines,
        "bubbles": bubbles,
        "keepout_regions": body_regions.to_debug_dict(),
        "current_score_summary": None if current_debug_payload is None else summarize_debug_payload(current_debug_payload),
        "previous_score_summary": previous_summary,
        "artifacts": {
            "codex_board": str(codex_board_path),
            "mask_composite": str(mask_composite_path),
            "debug_overlay": None if debug_overlay_path is None else str(debug_overlay_path),
            "rendered": None if rendered_path is None else str(rendered_path),
        },
        "instructions": [
            "Edit anchor_x and anchor_y only.",
            "Keep placements readable in manga right-to-left flow.",
            "Avoid face, chest, lower-body, and head keepout regions.",
            "Use the codex board and mask composite together when revising placements.",
        ],
    }


def build_codex_cli_output_schema(bubble_ids: list[str] | None = None) -> dict[str, Any]:
    item_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["bubble_id", "anchor_x", "anchor_y"],
        "properties": {
            "bubble_id": {"type": "string"},
            "anchor_x": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "anchor_y": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
    }
    placements_schema: dict[str, Any] = {
        "type": "array",
        "minItems": 1,
        "items": item_schema,
    }
    if bubble_ids:
        item_schema["properties"]["bubble_id"] = {"type": "string", "enum": bubble_ids}
        placements_schema["minItems"] = len(bubble_ids)
        placements_schema["maxItems"] = len(bubble_ids)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["mode", "notes", "placements"],
        "properties": {
            "mode": {"type": "string"},
            "notes": {"type": "string"},
            "placements": placements_schema,
        },
    }


def build_codex_cli_prompt(
    *,
    planner_mode: str,
    prompt_context: dict[str, Any],
    editable_template: dict[str, Any],
) -> str:
    placement_count = len(editable_template.get("placements", []))
    return (
        "You are placing manga-style vertical speech bubbles.\n"
        "Return JSON only. Follow the output schema exactly.\n"
        "Rules:\n"
        "- Edit anchor_x and anchor_y only.\n"
        "- Return placements for every bubble_id exactly once.\n"
        f"- Return exactly {placement_count} placements in the same bubble order as the template.\n"
        "- Keep reading flow right-to-left and top-to-bottom within a column.\n"
        "- Avoid face, chest, lower-body, and head keepout regions.\n"
        "- Bubble overlap is acceptable; prioritize text readability and keep text apart.\n"
        "- Preserve the overall intent of the current placement if one already exists.\n"
        f"- planner_mode: {planner_mode}\n\n"
        "Prompt context JSON:\n"
        f"{json.dumps(prompt_context, ensure_ascii=False, indent=2)}\n\n"
        "Editable template JSON:\n"
        f"{json.dumps(editable_template, ensure_ascii=False, indent=2)}\n"
    )


def build_codex_cli_command(
    *,
    command: str,
    model: str | None,
    cd: Path,
    schema_path: Path,
    output_path: Path,
    image_paths: list[Path],
) -> list[str]:
    argv = [
        command,
        "exec",
        "-C",
        str(cd),
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
    if model is not None and model.strip():
        argv[2:2] = ["-m", model]
    for image_path in image_paths:
        argv.extend(["-i", str(image_path)])
    argv.append("-")
    return argv


def run_codex_cli_scene_edit(
    *,
    planner_mode: str,
    command: str,
    model: str | None,
    repo_root: Path,
    prompt_context_path: Path,
    editable_template_path: Path,
    codex_board_path: Path,
    mask_composite_path: Path | None,
    output_path: Path,
) -> Path:
    prompt_context = _load_json(prompt_context_path)
    editable_template = _load_json(editable_template_path)
    bubble_ids = [
        str(item["bubble_id"])
        for item in editable_template.get("placements", [])
        if isinstance(item, dict) and isinstance(item.get("bubble_id"), str)
    ]
    schema_path = output_path.with_suffix(".schema.json")
    prompt_path = output_path.with_suffix(".prompt.txt")
    stderr_path = output_path.with_suffix(".stderr.txt")
    command_path = output_path.with_suffix(".command.json")

    schema_path.write_text(
        json.dumps(build_codex_cli_output_schema(bubble_ids), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    prompt = build_codex_cli_prompt(
        planner_mode=planner_mode,
        prompt_context=prompt_context,
        editable_template=editable_template,
    )
    prompt_path.write_text(prompt, encoding="utf-8")
    image_paths = [codex_board_path]
    if mask_composite_path is not None and mask_composite_path.exists():
        image_paths.append(mask_composite_path)
    argv = build_codex_cli_command(
        command=command,
        model=model,
        cd=repo_root,
        schema_path=schema_path,
        output_path=output_path,
        image_paths=image_paths,
    )
    command_path.write_text(json.dumps(argv, ensure_ascii=False, indent=2), encoding="utf-8")
    result = subprocess.run(
        argv,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=repo_root,
        check=False,
    )
    stderr_path.write_text(result.stderr or "", encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(f"codex exec failed with exit code {result.returncode}: {stderr_path}")
    if not output_path.exists():
        raise RuntimeError(f"codex exec did not produce output JSON: {output_path}")
    return output_path
