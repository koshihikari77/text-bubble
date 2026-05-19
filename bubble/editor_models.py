from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from bubble.assets import load_bubble_asset_catalog, pick_font_path, resolve_bubble_asset
from bubble.models import (
    AssignmentBubblePlan,
    ReflowBubblePlan,
    SceneBubblePlan,
    save_assignment_plan_json,
    save_plan_json,
    save_reflow_plan_json,
    save_scene_plan_json,
)
from bubble.scene_runtime import RenderConfig, compose_scene_bundle, render_scene_bundle
from bubble.validation import (
    load_assignment_plan_json,
    load_reflow_plan_json,
    load_scene_plan_json,
    text_for_sentence_ids,
)


DOCUMENT_VERSION = 1
PROJECT_VERSION = 1
DEFAULT_RENDER_SETTINGS = {
    "font_size": 0,
    "text_renderer": "resvg-hybrid",
    "bubble_renderer": "resvg",
    "text_letter_spacing": "-1px",
    "text_word_spacing": "0",
    "resvg_tu_override": True,
}


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"JSON must be an object: {path}")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def list_bubble_types() -> list[str]:
    catalog = load_bubble_asset_catalog()
    return sorted(catalog.assets.keys())


def _normalize_bool_map(value: Any) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {
        "text": bool(source.get("text", False)),
        "columns": bool(source.get("columns", False)),
        "bubble_type": bool(source.get("bubble_type", False)),
        "placement": bool(source.get("placement", False)),
    }


def _normalize_render_settings(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    settings = dict(DEFAULT_RENDER_SETTINGS)
    settings.update({key: item for key, item in source.items() if item is not None})
    if settings["text_renderer"] not in {"browser", "resvg-hybrid"}:
        raise RuntimeError(f"unsupported text_renderer: {settings['text_renderer']}")
    if settings["bubble_renderer"] not in {"browser", "resvg"}:
        raise RuntimeError(f"unsupported bubble_renderer: {settings['bubble_renderer']}")
    settings["font_size"] = int(settings.get("font_size", 0) or 0)
    settings["text_letter_spacing"] = str(settings.get("text_letter_spacing", "-1px"))
    settings["text_word_spacing"] = str(settings.get("text_word_spacing", "0"))
    settings["resvg_tu_override"] = bool(settings.get("resvg_tu_override", True))
    if settings.get("font") is not None:
        settings["font"] = str(settings["font"])
    if settings.get("font_family") is not None:
        settings["font_family"] = str(settings["font_family"])
    if settings.get("bubble_asset") is not None:
        settings["bubble_asset"] = str(settings["bubble_asset"])
    return settings


def validate_document(document: dict[str, Any]) -> dict[str, Any]:
    if int(document.get("version", DOCUMENT_VERSION)) != DOCUMENT_VERSION:
        raise RuntimeError(f"unsupported document version: {document.get('version')}")
    case_id = document.get("case_id")
    image = document.get("image")
    dialogue_lines = document.get("dialogue_lines")
    bubbles = document.get("bubbles")
    if not isinstance(case_id, str) or not case_id.strip():
        raise RuntimeError("document must include non-empty case_id")
    if not isinstance(image, str) or not image.strip():
        raise RuntimeError("document must include non-empty image")
    if not isinstance(dialogue_lines, list) or not all(isinstance(item, str) for item in dialogue_lines):
        raise RuntimeError("document must include dialogue_lines")
    if not isinstance(bubbles, list) or not bubbles:
        raise RuntimeError("document must include non-empty bubbles")

    known_types = set(list_bubble_types())
    used_sentence_ids: list[int] = []
    seen_bubble_ids: set[str] = set()
    normalized_bubbles: list[dict[str, Any]] = []
    for index, raw_bubble in enumerate(bubbles, start=1):
        if not isinstance(raw_bubble, dict):
            raise RuntimeError(f"bubble {index} must be an object")
        bubble_id = raw_bubble.get("bubble_id")
        if not isinstance(bubble_id, str) or not bubble_id.strip():
            raise RuntimeError(f"bubble {index} must include non-empty bubble_id")
        bubble_id = bubble_id.strip()
        if bubble_id in seen_bubble_ids:
            raise RuntimeError(f"bubble_id must be unique: {bubble_id}")
        seen_bubble_ids.add(bubble_id)

        sentence_ids_raw = raw_bubble.get("sentence_ids")
        if not isinstance(sentence_ids_raw, list) or not sentence_ids_raw:
            raise RuntimeError(f"bubble {index} must include sentence_ids")
        sentence_ids = [int(item) for item in sentence_ids_raw]
        if sentence_ids != list(range(sentence_ids[0], sentence_ids[0] + len(sentence_ids))):
            raise RuntimeError(f"bubble {index} sentence_ids must be consecutive")
        if sentence_ids[0] < 1 or sentence_ids[-1] > len(dialogue_lines):
            raise RuntimeError(f"bubble {index} sentence_ids are out of dialogue range")
        expected_text = text_for_sentence_ids(dialogue_lines, sentence_ids)
        text = raw_bubble.get("text", expected_text)
        if not isinstance(text, str):
            raise RuntimeError(f"bubble {index} text must be a string")
        if text != expected_text:
            raise RuntimeError(f"bubble {index} text does not match dialogue_lines")

        columns = raw_bubble.get("columns")
        if not isinstance(columns, list) or not columns or not all(isinstance(item, str) for item in columns):
            raise RuntimeError(f"bubble {index} must include non-empty string columns")
        if "".join(columns) != text:
            raise RuntimeError(f"bubble {index} columns do not reconstruct text")

        bubble_type = raw_bubble.get("bubble_type")
        if not isinstance(bubble_type, str) or not bubble_type.strip():
            raise RuntimeError(f"bubble {index} must include bubble_type")
        bubble_type = bubble_type.strip()
        if bubble_type not in known_types:
            raise RuntimeError(f"unknown bubble_type: {bubble_type}")

        placement = raw_bubble.get("placement")
        if not isinstance(placement, dict):
            raise RuntimeError(f"bubble {index} must include placement")
        anchor_x = float(placement.get("anchor_x"))
        anchor_y = float(placement.get("anchor_y"))
        if not 0.0 <= anchor_x <= 1.0 or not 0.0 <= anchor_y <= 1.0:
            raise RuntimeError(f"bubble {index} placement anchors must be within 0..1")

        source = raw_bubble.get("source") if isinstance(raw_bubble.get("source"), dict) else {}
        placement_source = source.get("placement", "imported")
        normalized_bubbles.append(
            {
                "bubble_id": bubble_id,
                "sentence_ids": sentence_ids,
                "text": text,
                "columns": list(columns),
                "bubble_type": bubble_type,
                "speaker_id": str(raw_bubble.get("speaker_id") or f"__scene_{index}"),
                "placement": {"anchor_x": anchor_x, "anchor_y": anchor_y},
                "manual": _normalize_bool_map(raw_bubble.get("manual")),
                "source": {"placement": str(placement_source)},
            }
        )
        used_sentence_ids.extend(sentence_ids)

    expected_ids = list(range(1, len(dialogue_lines) + 1))
    if sorted(used_sentence_ids) != expected_ids:
        raise RuntimeError("bubbles must cover every dialogue line exactly once")

    return {
        "version": DOCUMENT_VERSION,
        "case_id": case_id.strip(),
        "image": image.strip(),
        "dialogue_lines": list(dialogue_lines),
        "bubbles": normalized_bubbles,
        "render": _normalize_render_settings(document.get("render")),
    }


def initialize_project(project_dir: Path) -> dict[str, Any]:
    project_dir.mkdir(parents=True, exist_ok=True)
    project_path = project_dir / "project.json"
    if project_path.exists():
        return read_json(project_path)
    payload = {"version": PROJECT_VERSION, "cases": []}
    write_json(project_path, payload)
    return payload


def load_project(project_dir: Path) -> dict[str, Any]:
    return initialize_project(project_dir)


def save_project(project_dir: Path, project: dict[str, Any]) -> None:
    version = int(project.get("version", PROJECT_VERSION))
    if version != PROJECT_VERSION:
        raise RuntimeError(f"unsupported project version: {version}")
    cases = project.get("cases")
    if not isinstance(cases, list):
        raise RuntimeError("project must include cases")
    write_json(project_dir / "project.json", {"version": PROJECT_VERSION, "cases": cases})


def _case_document_path(project_dir: Path, case_id: str) -> Path:
    project = load_project(project_dir)
    for case in project.get("cases", []):
        if isinstance(case, dict) and case.get("case_id") == case_id:
            raw = case.get("document")
            if not isinstance(raw, str) or not raw.strip():
                raise RuntimeError(f"case is missing document path: {case_id}")
            return project_dir / raw
    raise RuntimeError(f"case not found: {case_id}")


def load_case_document(project_dir: Path, case_id: str) -> dict[str, Any]:
    return validate_document(read_json(_case_document_path(project_dir, case_id)))


def save_case_document(project_dir: Path, case_id: str, document: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_document(document)
    if normalized["case_id"] != case_id:
        raise RuntimeError("document case_id does not match URL case_id")
    write_json(_case_document_path(project_dir, case_id), normalized)
    return normalized


def resolve_document_image(document: dict[str, Any], *, project_dir: Path) -> Path:
    raw_path = Path(str(document["image"]))
    candidates = [raw_path] if raw_path.is_absolute() else [project_dir / raw_path, Path.cwd() / raw_path]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[-1].resolve()


def document_to_stage_files(document: dict[str, Any], generated_dir: Path) -> dict[str, Path]:
    normalized = validate_document(document)
    dialogue_lines = normalized["dialogue_lines"]
    assignment_plans: list[AssignmentBubblePlan] = []
    reflow_plans: list[ReflowBubblePlan] = []
    scene_plans: list[SceneBubblePlan] = []
    for bubble in normalized["bubbles"]:
        assignment_plans.append(
            AssignmentBubblePlan(
                bubble_id=bubble["bubble_id"],
                sentence_ids=list(bubble["sentence_ids"]),
            )
        )
        reflow_plans.append(
            ReflowBubblePlan(
                bubble_id=bubble["bubble_id"],
                sentence_ids=list(bubble["sentence_ids"]),
                columns=list(bubble["columns"]),
                bubble_type=bubble["bubble_type"],
            )
        )
        scene_plans.append(
            SceneBubblePlan(
                bubble_id=bubble["bubble_id"],
                anchor_x=float(bubble["placement"]["anchor_x"]),
                anchor_y=float(bubble["placement"]["anchor_y"]),
                sentence_ids=list(bubble["sentence_ids"]),
                speaker_id=bubble["speaker_id"],
                bubble_type=bubble["bubble_type"],
            )
        )

    generated_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = generated_dir / "metadata.json"
    assignment_path = generated_dir / "assignment.json"
    reflow_path = generated_dir / "reflow.json"
    scene_path = generated_dir / "scene.json"
    plan_path = generated_dir / "plan.json"
    write_json(metadata_path, {"dialogue_lines": dialogue_lines, "input_image": normalized["image"]})
    save_assignment_plan_json(assignment_path, dialogue_lines, assignment_plans)
    save_reflow_plan_json(reflow_path, dialogue_lines, reflow_plans)
    save_scene_plan_json(scene_path, dialogue_lines, scene_plans)
    bundle = compose_scene_bundle(
        dialogue_lines=dialogue_lines,
        reflow_plans=reflow_plans,
        scene_plans=scene_plans,
        source="editor-document",
    )
    save_plan_json(plan_path, dialogue_lines, bundle.composed_plans)
    return {
        "metadata": metadata_path,
        "assignment": assignment_path,
        "reflow": reflow_path,
        "scene": scene_path,
        "plan": plan_path,
    }


def workspace_to_document(
    *,
    workspace: Path,
    case_id: str,
    image_path: Path | None = None,
    placement_source: str = "cp-sat",
) -> dict[str, Any]:
    metadata = read_json(workspace / "metadata.json") if (workspace / "metadata.json").exists() else {}
    raw_image = str(image_path) if image_path is not None else metadata.get("input_image")
    if not isinstance(raw_image, str) or not raw_image.strip():
        raise RuntimeError("workspace metadata must include input_image or pass image_path")
    dialogue_lines, reflow_plans = load_reflow_plan_json(workspace / "reflow.json")
    scene_dialogue_lines, scene_plans = load_scene_plan_json(workspace / "scene.json")
    if scene_dialogue_lines != dialogue_lines:
        raise RuntimeError("workspace reflow and scene dialogue_lines do not match")
    assignment_path = workspace / "assignment.json"
    if assignment_path.exists():
        assignment_dialogue_lines, _ = load_assignment_plan_json(assignment_path)
        if assignment_dialogue_lines != dialogue_lines:
            raise RuntimeError("workspace assignment dialogue_lines do not match")

    scene_by_ids = {tuple(plan.sentence_ids): plan for plan in scene_plans}
    bubbles: list[dict[str, Any]] = []
    for index, reflow_plan in enumerate(reflow_plans, start=1):
        scene_plan = scene_by_ids.get(tuple(reflow_plan.sentence_ids))
        if scene_plan is None:
            raise RuntimeError(f"reflow bubble has no matching scene bubble: {reflow_plan.bubble_id}")
        bubble_type = reflow_plan.bubble_type or scene_plan.bubble_type
        bubbles.append(
            {
                "bubble_id": reflow_plan.bubble_id,
                "sentence_ids": list(reflow_plan.sentence_ids),
                "text": text_for_sentence_ids(dialogue_lines, reflow_plan.sentence_ids),
                "columns": list(reflow_plan.columns),
                "bubble_type": bubble_type,
                "speaker_id": scene_plan.speaker_id,
                "placement": {
                    "anchor_x": float(scene_plan.anchor_x),
                    "anchor_y": float(scene_plan.anchor_y),
                },
                "manual": {
                    "text": False,
                    "columns": False,
                    "bubble_type": False,
                    "placement": False,
                },
                "source": {"placement": placement_source},
            }
        )
    return validate_document(
        {
            "version": DOCUMENT_VERSION,
            "case_id": case_id,
            "image": raw_image,
            "dialogue_lines": dialogue_lines,
            "bubbles": bubbles,
            "render": dict(DEFAULT_RENDER_SETTINGS),
        }
    )


def add_workspace_case(
    *,
    project_dir: Path,
    case_id: str,
    workspace: Path,
    image_path: Path | None = None,
    copy_generated: bool = True,
) -> dict[str, Any]:
    document = workspace_to_document(workspace=workspace, case_id=case_id, image_path=image_path)
    case_dir = project_dir / "cases" / case_id
    document_path = case_dir / "document.json"
    generated_dir = case_dir / "generated"
    render_path = case_dir / "renders" / "latest.png"
    if copy_generated:
        generated_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("metadata.json", "assignment.json", "reflow.json", "scene.json", "plan.json"):
            source = workspace / filename
            if source.exists():
                shutil.copy2(source, generated_dir / filename)
    write_json(document_path, document)
    project = load_project(project_dir)
    cases = [case for case in project.get("cases", []) if not (isinstance(case, dict) and case.get("case_id") == case_id)]
    cases.append(
        {
            "case_id": case_id,
            "image": document["image"],
            "document": str(document_path.relative_to(project_dir)),
            "status": "needs_review",
            "rendered": str(render_path.relative_to(project_dir)),
        }
    )
    save_project(project_dir, {"version": PROJECT_VERSION, "cases": cases})
    return document


def find_workspaces(scan_dir: Path) -> list[Path]:
    """Return subdirectories of `scan_dir` that look like text-bubble workspaces."""

    if not scan_dir.exists() or not scan_dir.is_dir():
        return []
    candidates: list[Path] = []
    for entry in sorted(scan_dir.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "reflow.json").is_file() and (entry / "scene.json").is_file():
            candidates.append(entry)
    return candidates


def generated_dir_for_case(project_dir: Path, case_id: str) -> Path:
    return project_dir / "cases" / case_id / "generated"


def rendered_path_for_case(project_dir: Path, case_id: str) -> Path:
    return project_dir / "cases" / case_id / "renders" / "latest.png"


def export_case_document(project_dir: Path, case_id: str) -> dict[str, str]:
    document = load_case_document(project_dir, case_id)
    paths = document_to_stage_files(document, generated_dir_for_case(project_dir, case_id))
    return {key: str(path) for key, path in paths.items()}


def render_case_document(project_dir: Path, case_id: str) -> Path:
    document = load_case_document(project_dir, case_id)
    paths = document_to_stage_files(document, generated_dir_for_case(project_dir, case_id))
    dialogue_lines, reflow_plans = load_reflow_plan_json(paths["reflow"])
    _, scene_plans = load_scene_plan_json(paths["scene"])
    bundle = compose_scene_bundle(
        dialogue_lines=dialogue_lines,
        reflow_plans=reflow_plans,
        scene_plans=scene_plans,
        source="editor-document",
    )
    render_settings = _normalize_render_settings(document.get("render"))
    font_path = pick_font_path(render_settings.get("font"))
    bubble_asset = None
    if render_settings.get("bubble_asset"):
        bubble_asset = resolve_bubble_asset(str(render_settings["bubble_asset"]))
        if bubble_asset is None:
            raise RuntimeError(f"bubble asset not found: {render_settings['bubble_asset']}")
    output_path = rendered_path_for_case(project_dir, case_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    render_scene_bundle(
        image_path=resolve_document_image(document, project_dir=project_dir),
        output_path=output_path,
        bundle=bundle,
        config=RenderConfig(
            font_path=font_path,
            font_family=render_settings.get("font_family"),
            bubble_asset=bubble_asset,
            font_size=int(render_settings["font_size"]),
            text_renderer=str(render_settings["text_renderer"]),
            bubble_renderer=str(render_settings["bubble_renderer"]),
            text_letter_spacing=str(render_settings["text_letter_spacing"]),
            text_word_spacing=str(render_settings["text_word_spacing"]),
            resvg_tu_override=bool(render_settings["resvg_tu_override"]),
        ),
    )
    project = load_project(project_dir)
    for case in project.get("cases", []):
        if isinstance(case, dict) and case.get("case_id") == case_id:
            case["rendered"] = str(output_path.relative_to(project_dir))
            case["status"] = "rendered"
    save_project(project_dir, project)
    return output_path
