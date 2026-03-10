from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from bubble import __version__
from bubble.assets import pick_font_path, resolve_bubble_asset
from bubble.models import (
    assignment_plans_payload,
    plans_payload,
    reflow_plans_payload,
    save_assignment_plan_json,
    save_plan_json,
    save_reflow_plan_json,
    save_scene_plan_json,
    scene_plans_payload,
)
from bubble.scene_runtime import (
    LLMRoute,
    RenderConfig,
    compose_scene_bundle,
    infer_scene_stage,
    render_scene_bundle,
    resolve_scene_route,
    run_pipeline as run_scene_pipeline,
)
from bubble.validation import (
    load_assignment_plan_json,
    load_plan_json,
    load_reflow_plan_json,
    load_scene_plan_json,
)
from bubble.worker_client import worker_request


DEFAULT_SERVER = "http://127.0.0.1:8080/v1"
DEFAULT_WORKER_MODE = "auto"

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Generate manga-style vertical speech bubbles.")


@dataclass
class AppState:
    workspace: Path
    json_output: bool
    quiet: bool


@dataclass
class WorkspaceFiles:
    metadata: Path
    assignment: Path
    reflow: Path
    scene: Path
    plan: Path


def _workspace_files(workspace: Path) -> WorkspaceFiles:
    return WorkspaceFiles(
        metadata=workspace / "metadata.json",
        assignment=workspace / "assignment.json",
        reflow=workspace / "reflow.json",
        scene=workspace / "scene.json",
        plan=workspace / "plan.json",
    )


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"text-bubble {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    workspace: Path = typer.Option(Path("out/workspace"), "--workspace", "-w", help="Workspace directory."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON to stdout."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress logs."),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        is_eager=True,
        callback=_version_callback,
        help="Show version and exit.",
    ),
) -> None:
    del version
    workspace.mkdir(parents=True, exist_ok=True)
    ctx.obj = AppState(workspace=workspace, json_output=json_output, quiet=quiet)


def _log(state: AppState, message: str) -> None:
    if not state.quiet:
        typer.echo(message, err=True)


def _emit_success(state: AppState, payload: dict[str, Any], summary: str) -> None:
    if state.json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(summary)


def _emit_error(state: AppState, exc: Exception) -> None:
    message = str(exc) if str(exc) else exc.__class__.__name__
    if state.json_output:
        typer.echo(
            json.dumps(
                {
                    "status": "error",
                    "error": exc.__class__.__name__,
                    "message": message,
                },
                ensure_ascii=False,
            )
        )
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _load_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"metadata JSON must be an object: {path}")
    return data


def _save_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_server(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    return os.environ.get("TEXT_BUBBLE_SERVER", DEFAULT_SERVER)


def _split_dialogue_lines(dialogue: str) -> list[str]:
    from bubble.infer import split_dialogue_lines

    return split_dialogue_lines(dialogue)


def _resolve_dialogue_lines(dialogue: str | None, metadata: dict[str, Any]) -> list[str]:
    if dialogue and dialogue.strip():
        lines = _split_dialogue_lines(dialogue)
        if not lines:
            raise RuntimeError("dialogue must contain at least one non-empty line")
        return lines
    existing = metadata.get("dialogue_lines")
    if isinstance(existing, list) and existing and all(isinstance(item, str) and item.strip() for item in existing):
        return [item.strip() for item in existing]
    raise RuntimeError("dialogue is required (pass --dialogue or set metadata.json dialogue_lines)")


def _resolve_input_path(input_path: Path | None, metadata: dict[str, Any]) -> Path:
    candidate: Path | None = None
    if input_path is not None:
        candidate = input_path
    else:
        raw = metadata.get("input_image")
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw)
    if candidate is None:
        raise RuntimeError("input image is required (pass --input or set metadata.json input_image)")
    if not candidate.exists():
        raise RuntimeError(f"input image not found: {candidate}")
    return candidate


def _dialogue_text(dialogue_lines: list[str]) -> str:
    return "\n".join(dialogue_lines)


def _validate_worker_mode(worker_mode: str) -> str:
    normalized = worker_mode.strip().lower()
    if normalized not in {"auto", "on", "off"}:
        raise RuntimeError(f"unsupported worker mode: {worker_mode}")
    return normalized


def _validate_text_renderer(text_renderer: str) -> str:
    normalized = text_renderer.strip().lower()
    if normalized not in {"browser", "resvg-hybrid"}:
        raise RuntimeError(f"unsupported text renderer: {text_renderer}")
    return normalized


def _validate_bubble_renderer(bubble_renderer: str) -> str:
    normalized = bubble_renderer.strip().lower()
    if normalized not in {"resvg", "browser"}:
        raise RuntimeError(f"unsupported bubble renderer: {bubble_renderer}")
    return normalized


def _validate_reflow_workers(reflow_workers: int) -> int:
    if reflow_workers < 1:
        raise RuntimeError("reflow workers must be >= 1")
    return reflow_workers


def _validate_evaluate_stage(stage: str) -> str:
    normalized = stage.strip().lower()
    if normalized not in {"final", "text"}:
        raise RuntimeError(f"unsupported evaluate stage: {stage}")
    return normalized


def _default_route(server: str, model: str) -> LLMRoute:
    return LLMRoute(server=server, model=model)


def _render_config(
    *,
    font: Path | None,
    font_family: str | None,
    bubble_asset: Path | None,
    font_size: int,
    text_renderer: str,
    bubble_renderer: str,
    text_letter_spacing: str,
    text_word_spacing: str,
    resvg_tu_override: bool,
) -> RenderConfig:
    resolved_bubble_asset = resolve_bubble_asset(str(bubble_asset) if bubble_asset is not None else None)
    if resolved_bubble_asset is None:
        raise RuntimeError(f"bubble asset not found: {bubble_asset}")
    return RenderConfig(
        font_path=pick_font_path(str(font) if font is not None else None),
        font_family=font_family,
        bubble_asset=resolved_bubble_asset,
        font_size=font_size,
        text_renderer=text_renderer,
        bubble_renderer=bubble_renderer,
        text_letter_spacing=text_letter_spacing,
        text_word_spacing=text_word_spacing,
        resvg_tu_override=resvg_tu_override,
    )


@app.command()
def assign(
    ctx: typer.Context,
    dialogue: str | None = typer.Option(None, "--dialogue", "-d", help="Dialogue text. Newlines create multiple lines."),
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        metadata = _load_metadata(files.metadata)
        dialogue_lines = _resolve_dialogue_lines(dialogue, metadata)
        _log(state, "running assignment")
        from bubble.infer import infer_assignment_plans

        _, plans = infer_assignment_plans(_dialogue_text(dialogue_lines))
        save_assignment_plan_json(files.assignment, dialogue_lines, plans)
        metadata["dialogue_lines"] = dialogue_lines
        _save_metadata(files.metadata, metadata)
        payload = {
            "stage": "assignment",
            "workspace": str(state.workspace),
            "output_file": str(files.assignment),
            **assignment_plans_payload(dialogue_lines, plans),
        }
        _emit_success(state, payload, f"assignment saved: {files.assignment}")
    except Exception as exc:  # noqa: BLE001
        _emit_error(state, exc)


@app.command()
def reflow(
    ctx: typer.Context,
    dialogue: str | None = typer.Option(None, "--dialogue", "-d", help="Dialogue text. Defaults to workspace metadata."),
    server: str | None = typer.Option(None, "--server", "-s", help="llama-server API base URL."),
    model: str = typer.Option("heretic", "--model", "-m", help="Model alias exposed by llama-server."),
    temperature: float = typer.Option(0.0, "--temperature", "-t", help="Sampling temperature."),
    reflow_workers: int = typer.Option(4, "--reflow-workers", help="Parallel workers for reflow requests."),
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        if not files.assignment.exists():
            raise RuntimeError(f"assignment plan JSON not found: {files.assignment}")
        metadata = _load_metadata(files.metadata)
        dialogue_lines = _resolve_dialogue_lines(dialogue, metadata)
        assignment_dialogue_lines, assignment_plans = load_assignment_plan_json(files.assignment)
        if assignment_dialogue_lines != dialogue_lines:
            raise RuntimeError("dialogue does not match assignment JSON dialogue_lines")
        validated_reflow_workers = _validate_reflow_workers(reflow_workers)
        resolved_server = _resolve_server(server)
        _log(state, f"running reflow via {resolved_server}")
        from bubble.infer import infer_reflow_plans

        _, plans = infer_reflow_plans(
            server=resolved_server,
            model=model,
            dialogue=_dialogue_text(dialogue_lines),
            temperature=temperature,
            assignment_plans=assignment_plans,
            reflow_workers=validated_reflow_workers,
        )
        save_reflow_plan_json(files.reflow, dialogue_lines, plans)
        metadata["dialogue_lines"] = dialogue_lines
        _save_metadata(files.metadata, metadata)
        payload = {
            "stage": "reflow",
            "workspace": str(state.workspace),
            "output_file": str(files.reflow),
            "server": resolved_server,
            "model": model,
            "reflow_workers": validated_reflow_workers,
            **reflow_plans_payload(dialogue_lines, plans),
        }
        _emit_success(state, payload, f"reflow saved: {files.reflow}")
    except Exception as exc:  # noqa: BLE001
        _emit_error(state, exc)


@app.command()
def scene(
    ctx: typer.Context,
    input_path: Path | None = typer.Option(None, "--input", "-i", help="Input image path."),
    dialogue: str | None = typer.Option(None, "--dialogue", "-d", help="Dialogue text. Defaults to workspace metadata."),
    server: str | None = typer.Option(None, "--server", "-s", help="llama-server API base URL."),
    model: str = typer.Option("heretic", "--model", "-m", help="Model alias exposed by llama-server."),
    scene_server: str | None = typer.Option(None, "--scene-server", help="Override scene-stage llama-server API base URL."),
    scene_model: str | None = typer.Option(None, "--scene-model", help="Override scene-stage model alias."),
    temperature: float = typer.Option(0.0, "--temperature", "-t", help="Sampling temperature."),
    use_worker: str = typer.Option(DEFAULT_WORKER_MODE, "--use-worker", help="Worker mode: auto, on, or off."),
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        metadata = _load_metadata(files.metadata)
        dialogue_lines = _resolve_dialogue_lines(dialogue, metadata)
        image_path = _resolve_input_path(input_path, metadata)
        resolved_server = _resolve_server(server)
        worker_mode = _validate_worker_mode(use_worker)
        scene_route = resolve_scene_route(
            default_server=resolved_server,
            default_model=model,
            scene_server=scene_server,
            scene_model=scene_model,
        )
        _log(state, f"running scene via {scene_route.server}")
        response = worker_request(
            "scene_stage",
            {
                "image_path": str(image_path),
                "dialogue_lines": dialogue_lines,
                "server": resolved_server,
                "model": model,
                "scene_server": scene_server,
                "scene_model": scene_model,
                "temperature": temperature,
                "output_scene_path": str(files.scene),
            },
            mode=worker_mode,
        )
        if response is None:
            _, plans = infer_scene_stage(
                image_path=image_path,
                dialogue_lines=dialogue_lines,
                route=scene_route,
                temperature=temperature,
            )
            save_scene_plan_json(files.scene, dialogue_lines, plans)
            payload = {
                "stage": "scene",
                "workspace": str(state.workspace),
                "output_file": str(files.scene),
                "server": scene_route.server,
                "model": scene_route.model,
                **scene_plans_payload(dialogue_lines, plans),
            }
        else:
            payload = {
                "stage": "scene",
                "workspace": str(state.workspace),
                **{key: value for key, value in response.items() if key != "status"},
            }
            _, plans = load_scene_plan_json(files.scene)
        metadata["dialogue_lines"] = dialogue_lines
        metadata["input_image"] = str(image_path)
        _save_metadata(files.metadata, metadata)
        _emit_success(state, payload, f"scene saved: {files.scene}")
    except Exception as exc:  # noqa: BLE001
        _emit_error(state, exc)


@app.command()
def render(
    ctx: typer.Context,
    output_path: Path = typer.Option(..., "--output", "-o", help="Output image path."),
    input_path: Path | None = typer.Option(None, "--input", "-i", help="Input image path."),
    font: Path | None = typer.Option(None, "--font", help="Font file path."),
    font_family: str | None = typer.Option(None, "--font-family", help="CSS font-family override."),
    bubble_asset: Path | None = typer.Option(None, "--bubble-asset", help="Override bubble asset path for all bubble types."),
    font_size: int = typer.Option(0, "--font-size", help="Override vertical text font size."),
    text_renderer: str = typer.Option("resvg-hybrid", "--text-renderer", help="Text renderer backend."),
    bubble_renderer: str = typer.Option("resvg", "--bubble-renderer", help="Bubble renderer backend."),
    text_letter_spacing: str = typer.Option("-1px", "--text-letter-spacing", help="Letter spacing for text renderer."),
    text_word_spacing: str = typer.Option("0", "--text-word-spacing", help="Word spacing for text renderer."),
    resvg_tu_override: bool = typer.Option(
        True,
        "--resvg-tu-override/--no-resvg-tu-override",
        help="Force manual upright rendering for known Tu punctuation in resvg-hybrid.",
    ),
    use_worker: str = typer.Option(DEFAULT_WORKER_MODE, "--use-worker", help="Worker mode: auto, on, or off."),
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        validated_text_renderer = _validate_text_renderer(text_renderer)
        validated_bubble_renderer = _validate_bubble_renderer(bubble_renderer)
        worker_mode = _validate_worker_mode(use_worker)
        if not files.scene.exists():
            raise RuntimeError(f"scene plan JSON not found: {files.scene}")
        if not files.reflow.exists():
            raise RuntimeError(f"reflow plan JSON not found: {files.reflow}")
        metadata = _load_metadata(files.metadata)
        image_path = _resolve_input_path(input_path, metadata)
        scene_dialogue_lines, scene_plans = load_scene_plan_json(files.scene)
        reflow_dialogue_lines, reflow_plans = load_reflow_plan_json(files.reflow)
        if scene_dialogue_lines != reflow_dialogue_lines:
            raise RuntimeError("scene JSON dialogue_lines do not match reflow JSON dialogue_lines")
        dialogue_lines = scene_dialogue_lines
        _log(state, "rendering bubbles")
        response = worker_request(
            "render_from_scene",
            {
                "image_path": str(image_path),
                "scene_path": str(files.scene),
                "reflow_path": str(files.reflow),
                "output_path": str(output_path),
                "plan_output_path": str(files.plan),
                "font": str(font) if font is not None else None,
                "font_family": font_family,
                "bubble_asset": str(bubble_asset) if bubble_asset is not None else None,
                "font_size": font_size,
                "text_renderer": validated_text_renderer,
                "bubble_renderer": validated_bubble_renderer,
                "text_letter_spacing": text_letter_spacing,
                "text_word_spacing": text_word_spacing,
                "resvg_tu_override": resvg_tu_override,
            },
            mode=worker_mode,
        )
        if response is None:
            bundle = compose_scene_bundle(
                dialogue_lines=dialogue_lines,
                reflow_plans=reflow_plans,
                scene_plans=scene_plans,
                source="scene-json",
            )
            save_plan_json(files.plan, dialogue_lines, bundle.composed_plans)
            render_scene_bundle(
                image_path=image_path,
                output_path=output_path,
                bundle=bundle,
                config=_render_config(
                    font=font,
                    font_family=font_family,
                    bubble_asset=bubble_asset,
                    font_size=font_size,
                    text_renderer=validated_text_renderer,
                    bubble_renderer=validated_bubble_renderer,
                    text_letter_spacing=text_letter_spacing,
                    text_word_spacing=text_word_spacing,
                    resvg_tu_override=resvg_tu_override,
                ),
            )
            plans = bundle.composed_plans
        else:
            _, plans = load_plan_json(files.plan)
        metadata["dialogue_lines"] = dialogue_lines
        metadata["input_image"] = str(image_path)
        _save_metadata(files.metadata, metadata)
        payload = {
            "stage": "render",
            "workspace": str(state.workspace),
            "input_image": str(image_path),
            "output_file": str(output_path),
            "plan_file": str(files.plan),
            "text_renderer": validated_text_renderer,
            "bubble_renderer": validated_bubble_renderer,
            "text_letter_spacing": text_letter_spacing,
            "text_word_spacing": text_word_spacing,
            "resvg_tu_override": resvg_tu_override,
            **plans_payload(dialogue_lines, plans),
        }
        _emit_success(state, payload, f"rendered image: {output_path}")
    except Exception as exc:  # noqa: BLE001
        _emit_error(state, exc)


@app.command()
def evaluate(
    ctx: typer.Context,
    rendered_path: Path | None = typer.Option(None, "--rendered", help="Rendered image path for final-stage evaluation."),
    plan_json: Path | None = typer.Option(None, "--plan-json", help="Bubble plan JSON path."),
    input_path: Path | None = typer.Option(None, "--input", "-i", help="Input image path."),
    dialogue: str | None = typer.Option(None, "--dialogue", "-d", help="Dialogue text. Defaults to metadata/plan."),
    server: str | None = typer.Option(None, "--server", "-s", help="llama-server API base URL."),
    model: str = typer.Option("heretic", "--model", "-m", help="Model alias exposed by llama-server."),
    temperature: float = typer.Option(0.0, "--temperature", "-t", help="Sampling temperature."),
    stage: str = typer.Option("final", "--stage", help="Evaluation stage: final or text."),
    text_renderer: str = typer.Option("resvg-hybrid", "--text-renderer", help="Text renderer for text-stage preview."),
    font: Path | None = typer.Option(None, "--font", help="Font file path."),
    font_family: str | None = typer.Option(None, "--font-family", help="CSS font-family override."),
    font_size: int = typer.Option(0, "--font-size", help="Override vertical text font size."),
    text_letter_spacing: str = typer.Option("-1px", "--text-letter-spacing", help="Letter spacing for text renderer."),
    text_word_spacing: str = typer.Option("0", "--text-word-spacing", help="Word spacing for text renderer."),
    resvg_tu_override: bool = typer.Option(
        True,
        "--resvg-tu-override/--no-resvg-tu-override",
        help="Force manual upright rendering for known Tu punctuation in resvg-hybrid.",
    ),
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        metadata = _load_metadata(files.metadata)
        image_path = _resolve_input_path(input_path, metadata)
        plan_path = plan_json if plan_json is not None else files.plan
        if not plan_path.exists():
            raise RuntimeError(f"plan JSON not found: {plan_path}")
        plan_dialogue_lines, plans = load_plan_json(plan_path)
        evaluated_stage = _validate_evaluate_stage(stage)

        dialogue_lines: list[str]
        if dialogue and dialogue.strip():
            dialogue_lines = _resolve_dialogue_lines(dialogue, metadata)
        else:
            existing = metadata.get("dialogue_lines")
            if isinstance(existing, list) and existing and all(isinstance(item, str) and item.strip() for item in existing):
                dialogue_lines = [item.strip() for item in existing]
            else:
                dialogue_lines = plan_dialogue_lines

        if plan_dialogue_lines != dialogue_lines:
            raise RuntimeError("dialogue does not match plan JSON dialogue_lines")

        resolved_server = _resolve_server(server)
        validated_text_renderer = _validate_text_renderer(text_renderer)
        from bubble.evaluate import evaluate_preview_result
        from bubble.render import render_text_stage_preview

        preview_path: Path
        text_bboxes: list[tuple[int, int, int, int]] | None = None
        if evaluated_stage == "final":
            if rendered_path is None:
                raise RuntimeError("--rendered is required when --stage final")
            if not rendered_path.exists():
                raise RuntimeError(f"rendered image not found: {rendered_path}")
            preview_path = rendered_path
            _log(state, f"running final-stage evaluate via {resolved_server}")
        else:
            preview_path = state.workspace / "evaluate_text_stage.png"
            _log(state, f"rendering text-stage preview: {preview_path}")
            text_bboxes = render_text_stage_preview(
                image_path=image_path,
                output_path=preview_path,
                plans=plans,
                font_path=pick_font_path(font),
                font_family=font_family,
                font_size=font_size,
                text_renderer=validated_text_renderer,
                text_letter_spacing=text_letter_spacing,
                text_word_spacing=text_word_spacing,
                resvg_tu_override=resvg_tu_override,
            )
            _log(state, f"running text-stage evaluate via {resolved_server}")
        evaluation = evaluate_preview_result(
            server=resolved_server,
            model=model,
            temperature=temperature,
            dialogue_lines=dialogue_lines,
            plans=plans,
            original_image_path=image_path,
            preview_image_path=preview_path,
            stage=evaluated_stage,
            text_bboxes=text_bboxes if evaluated_stage == "text" else None,
        )
        result_path = state.workspace / f"evaluate_{evaluated_stage}_result.json"
        payload = {
            "stage": "evaluate",
            "workspace": str(state.workspace),
            "input_image": str(image_path),
            "evaluate_stage": evaluated_stage,
            "rendered_image": str(preview_path),
            "plan_file": str(plan_path),
            "result_file": str(result_path),
            "server": resolved_server,
            "model": model,
            "dialogue_lines": dialogue_lines,
            **evaluation,
        }
        _save_json(result_path, payload)
        typer.echo(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        message = str(exc) if str(exc) else exc.__class__.__name__
        typer.echo(
            json.dumps(
                {
                    "stage": "evaluate",
                    "status": "error",
                    "error": exc.__class__.__name__,
                    "message": message,
                },
                ensure_ascii=False,
            )
        )
        raise typer.Exit(code=1)


@app.command()
def run(
    ctx: typer.Context,
    output_path: Path = typer.Option(..., "--output", "-o", help="Output image path."),
    input_path: Path | None = typer.Option(None, "--input", "-i", help="Input image path."),
    dialogue: str | None = typer.Option(None, "--dialogue", "-d", help="Dialogue text. Defaults to workspace metadata."),
    server: str | None = typer.Option(None, "--server", "-s", help="llama-server API base URL."),
    model: str = typer.Option("heretic", "--model", "-m", help="Model alias exposed by llama-server."),
    scene_server: str | None = typer.Option(None, "--scene-server", help="Override scene-stage llama-server API base URL."),
    scene_model: str | None = typer.Option(None, "--scene-model", help="Override scene-stage model alias."),
    temperature: float = typer.Option(0.0, "--temperature", "-t", help="Sampling temperature."),
    font: Path | None = typer.Option(None, "--font", help="Font file path."),
    font_family: str | None = typer.Option(None, "--font-family", help="CSS font-family override."),
    bubble_asset: Path | None = typer.Option(None, "--bubble-asset", help="Override bubble asset path for all bubble types."),
    font_size: int = typer.Option(0, "--font-size", help="Override vertical text font size."),
    text_renderer: str = typer.Option("resvg-hybrid", "--text-renderer", help="Text renderer backend."),
    bubble_renderer: str = typer.Option("resvg", "--bubble-renderer", help="Bubble renderer backend."),
    text_letter_spacing: str = typer.Option("-1px", "--text-letter-spacing", help="Letter spacing for text renderer."),
    text_word_spacing: str = typer.Option("0", "--text-word-spacing", help="Word spacing for text renderer."),
    resvg_tu_override: bool = typer.Option(
        True,
        "--resvg-tu-override/--no-resvg-tu-override",
        help="Force manual upright rendering for known Tu punctuation in resvg-hybrid.",
    ),
    reflow_workers: int = typer.Option(4, "--reflow-workers", help="Parallel workers for reflow requests."),
    use_worker: str = typer.Option(DEFAULT_WORKER_MODE, "--use-worker", help="Worker mode: auto, on, or off."),
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        validated_text_renderer = _validate_text_renderer(text_renderer)
        validated_bubble_renderer = _validate_bubble_renderer(bubble_renderer)
        validated_reflow_workers = _validate_reflow_workers(reflow_workers)
        worker_mode = _validate_worker_mode(use_worker)
        metadata = _load_metadata(files.metadata)
        dialogue_lines = _resolve_dialogue_lines(dialogue, metadata)
        image_path = _resolve_input_path(input_path, metadata)
        resolved_server = _resolve_server(server)
        scene_route = resolve_scene_route(
            default_server=resolved_server,
            default_model=model,
            scene_server=scene_server,
            scene_model=scene_model,
        )
        render_config = _render_config(
            font=font,
            font_family=font_family,
            bubble_asset=bubble_asset,
            font_size=font_size,
            text_renderer=validated_text_renderer,
            bubble_renderer=validated_bubble_renderer,
            text_letter_spacing=text_letter_spacing,
            text_word_spacing=text_word_spacing,
            resvg_tu_override=resvg_tu_override,
        )
        response = worker_request(
            "run_pipeline",
            {
                "image_path": str(image_path),
                "dialogue_lines": dialogue_lines,
                "server": resolved_server,
                "model": model,
                "scene_server": scene_server,
                "scene_model": scene_model,
                "temperature": temperature,
                "reflow_workers": validated_reflow_workers,
                "font": str(font) if font is not None else None,
                "font_family": font_family,
                "bubble_asset": str(bubble_asset) if bubble_asset is not None else None,
                "font_size": font_size,
                "text_renderer": validated_text_renderer,
                "bubble_renderer": validated_bubble_renderer,
                "text_letter_spacing": text_letter_spacing,
                "text_word_spacing": text_word_spacing,
                "resvg_tu_override": resvg_tu_override,
                "assignment_path": str(files.assignment),
                "reflow_path": str(files.reflow),
                "scene_path": str(files.scene),
                "plan_path": str(files.plan),
                "output_path": str(output_path),
            },
            mode=worker_mode,
        )
        if response is None:
            result = run_scene_pipeline(
                image_path=image_path,
                dialogue_lines=dialogue_lines,
                default_route=_default_route(resolved_server, model),
                scene_route=scene_route,
                temperature=temperature,
                reflow_workers=validated_reflow_workers,
                image_width=0,
                image_height=0,
                face_mask=None,
                person_mask=None,
                chest_mask=None,
                lower_mask=None,
                head_mask=None,
                font_size=font_size,
            )
            save_assignment_plan_json(files.assignment, dialogue_lines, result.assignment_plans)
            save_reflow_plan_json(files.reflow, dialogue_lines, result.reflow_plans)
            save_scene_plan_json(files.scene, dialogue_lines, result.scene_bundle.scene_plans)
            save_plan_json(files.plan, dialogue_lines, result.scene_bundle.composed_plans)
            _log(state, "rendering bubbles")
            render_scene_bundle(
                image_path=image_path,
                output_path=output_path,
                bundle=result.scene_bundle,
                config=render_config,
            )
            plans = result.scene_bundle.composed_plans
            payload = {
                "stage": "run",
                "workspace": str(state.workspace),
                "output_file": str(output_path),
                "assignment_file": str(files.assignment),
                "reflow_file": str(files.reflow),
                "scene_file": str(files.scene),
                "plan_file": str(files.plan),
                "server": result.default_route.server,
                "model": result.default_route.model,
                "scene_server": result.scene_route.server,
                "scene_model": result.scene_route.model,
                "text_renderer": validated_text_renderer,
                "bubble_renderer": validated_bubble_renderer,
                "text_letter_spacing": text_letter_spacing,
                "text_word_spacing": text_word_spacing,
                "resvg_tu_override": resvg_tu_override,
                "reflow_workers": result.reflow_workers,
                **plans_payload(dialogue_lines, plans),
            }
        else:
            _, plans = load_plan_json(files.plan)
            payload = {
                "stage": "run",
                "workspace": str(state.workspace),
                "text_renderer": validated_text_renderer,
                "bubble_renderer": validated_bubble_renderer,
                "text_letter_spacing": text_letter_spacing,
                "text_word_spacing": text_word_spacing,
                "resvg_tu_override": resvg_tu_override,
                **{key: value for key, value in response.items() if key != "status"},
            }
        metadata["dialogue_lines"] = dialogue_lines
        metadata["input_image"] = str(image_path)
        _save_metadata(files.metadata, metadata)
        _emit_success(state, payload, f"run completed: {output_path}")
    except Exception as exc:  # noqa: BLE001
        _emit_error(state, exc)


@app.command()
def full(
    ctx: typer.Context,
    output_path: Path = typer.Option(..., "--output", "-o", help="Output image path."),
    input_path: Path | None = typer.Option(None, "--input", "-i", help="Input image path."),
    dialogue: str | None = typer.Option(None, "--dialogue", "-d", help="Dialogue text. Defaults to workspace metadata."),
    server: str | None = typer.Option(None, "--server", "-s", help="llama-server API base URL."),
    model: str = typer.Option("heretic", "--model", "-m", help="Model alias exposed by llama-server."),
    temperature: float = typer.Option(0.0, "--temperature", "-t", help="Sampling temperature."),
    font: Path | None = typer.Option(None, "--font", help="Font file path."),
    font_family: str | None = typer.Option(None, "--font-family", help="CSS font-family override."),
    bubble_asset: Path | None = typer.Option(None, "--bubble-asset", help="Override bubble asset path for all bubble types."),
    font_size: int = typer.Option(0, "--font-size", help="Override vertical text font size."),
    text_renderer: str = typer.Option("resvg-hybrid", "--text-renderer", help="Text renderer backend."),
    bubble_renderer: str = typer.Option("resvg", "--bubble-renderer", help="Bubble renderer backend."),
    text_letter_spacing: str = typer.Option("-1px", "--text-letter-spacing", help="Letter spacing for text renderer."),
    text_word_spacing: str = typer.Option("0", "--text-word-spacing", help="Word spacing for text renderer."),
    resvg_tu_override: bool = typer.Option(
        True,
        "--resvg-tu-override/--no-resvg-tu-override",
        help="Force manual upright rendering for known Tu punctuation in resvg-hybrid.",
    ),
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        validated_text_renderer = _validate_text_renderer(text_renderer)
        validated_bubble_renderer = _validate_bubble_renderer(bubble_renderer)
        metadata = _load_metadata(files.metadata)
        dialogue_lines = _resolve_dialogue_lines(dialogue, metadata)
        image_path = _resolve_input_path(input_path, metadata)
        resolved_server = _resolve_server(server)
        _log(state, f"running full plan via {resolved_server}")
        from bubble.infer import infer_bubble_plans
        from bubble.render import render_bubbles

        _, plans = infer_bubble_plans(
            image_path=image_path,
            server=resolved_server,
            model=model,
            dialogue=_dialogue_text(dialogue_lines),
            temperature=temperature,
        )
        save_plan_json(files.plan, dialogue_lines, plans)

        resolved_font_path = pick_font_path(str(font) if font is not None else None)
        bubble_asset_override = resolve_bubble_asset(str(bubble_asset)) if bubble_asset is not None else None
        if bubble_asset is not None and bubble_asset_override is None:
            raise RuntimeError(f"bubble asset not found: {bubble_asset}")
        _log(state, "rendering bubbles")
        render_bubbles(
            image_path=image_path,
            output_path=output_path,
            plans=plans,
            font_path=resolved_font_path,
            font_family=font_family,
            bubble_asset_override=bubble_asset_override,
            font_size=font_size,
            text_renderer=validated_text_renderer,
            bubble_renderer=validated_bubble_renderer,
            text_letter_spacing=text_letter_spacing,
            text_word_spacing=text_word_spacing,
            resvg_tu_override=resvg_tu_override,
        )

        metadata["dialogue_lines"] = dialogue_lines
        metadata["input_image"] = str(image_path)
        _save_metadata(files.metadata, metadata)
        payload = {
            "stage": "full",
            "workspace": str(state.workspace),
            "output_file": str(output_path),
            "plan_file": str(files.plan),
            "server": resolved_server,
            "model": model,
            "text_renderer": validated_text_renderer,
            "bubble_renderer": validated_bubble_renderer,
            "text_letter_spacing": text_letter_spacing,
            "text_word_spacing": text_word_spacing,
            "resvg_tu_override": resvg_tu_override,
            **plans_payload(dialogue_lines, plans),
        }
        _emit_success(state, payload, f"full completed: {output_path}")
    except Exception as exc:  # noqa: BLE001
        _emit_error(state, exc)


def main_entry() -> None:
    app()


if __name__ == "__main__":
    main_entry()
