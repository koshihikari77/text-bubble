from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from bubble import __version__
from bubble.assets import pick_font_path, resolve_bubble_asset
from bubble.evaluate import evaluate_rendered_result
from bubble.infer import (
    infer_assignment_plans,
    infer_bubble_plans,
    infer_reflow_plans,
    infer_scene_bubble_plans,
    split_dialogue_lines,
)
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
from bubble.render import render_bubbles
from bubble.validation import (
    compose_bubble_plans,
    load_assignment_plan_json,
    load_plan_json,
    load_reflow_plan_json,
    load_scene_plan_json,
)


DEFAULT_SERVER = "http://127.0.0.1:8080/v1"

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


def _resolve_server(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    return os.environ.get("TEXT_BUBBLE_SERVER", DEFAULT_SERVER)


def _resolve_dialogue_lines(dialogue: str | None, metadata: dict[str, Any]) -> list[str]:
    if dialogue and dialogue.strip():
        lines = split_dialogue_lines(dialogue)
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
    temperature: float = typer.Option(0.0, "--temperature", "-t", help="Sampling temperature."),
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        metadata = _load_metadata(files.metadata)
        dialogue_lines = _resolve_dialogue_lines(dialogue, metadata)
        image_path = _resolve_input_path(input_path, metadata)
        resolved_server = _resolve_server(server)
        _log(state, f"running scene via {resolved_server}")
        _, plans = infer_scene_bubble_plans(
            image_path=image_path,
            server=resolved_server,
            model=model,
            dialogue=_dialogue_text(dialogue_lines),
            temperature=temperature,
        )
        save_scene_plan_json(files.scene, dialogue_lines, plans)
        metadata["dialogue_lines"] = dialogue_lines
        metadata["input_image"] = str(image_path)
        _save_metadata(files.metadata, metadata)
        payload = {
            "stage": "scene",
            "workspace": str(state.workspace),
            "output_file": str(files.scene),
            "server": resolved_server,
            "model": model,
            **scene_plans_payload(dialogue_lines, plans),
        }
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
    bubble_asset: Path | None = typer.Option(None, "--bubble-asset", help="Bubble asset path."),
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
        plans = compose_bubble_plans(dialogue_lines, scene_plans, reflow_plans)
        save_plan_json(files.plan, dialogue_lines, plans)
        resolved_font_path = pick_font_path(str(font) if font is not None else None)
        resolved_bubble_asset = resolve_bubble_asset(str(bubble_asset) if bubble_asset is not None else None)
        if resolved_bubble_asset is None:
            raise RuntimeError(f"bubble asset not found: {bubble_asset}")
        _log(state, "rendering bubbles")
        render_bubbles(
            image_path=image_path,
            output_path=output_path,
            plans=plans,
            font_path=resolved_font_path,
            font_family=font_family,
            bubble_asset=resolved_bubble_asset,
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
    rendered_path: Path = typer.Option(..., "--rendered", help="Rendered image path."),
    plan_json: Path | None = typer.Option(None, "--plan-json", help="Bubble plan JSON path."),
    input_path: Path | None = typer.Option(None, "--input", "-i", help="Input image path."),
    dialogue: str | None = typer.Option(None, "--dialogue", "-d", help="Dialogue text. Defaults to metadata/plan."),
    server: str | None = typer.Option(None, "--server", "-s", help="llama-server API base URL."),
    model: str = typer.Option("heretic", "--model", "-m", help="Model alias exposed by llama-server."),
    temperature: float = typer.Option(0.0, "--temperature", "-t", help="Sampling temperature."),
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        metadata = _load_metadata(files.metadata)
        if not rendered_path.exists():
            raise RuntimeError(f"rendered image not found: {rendered_path}")
        image_path = _resolve_input_path(input_path, metadata)
        plan_path = plan_json if plan_json is not None else files.plan
        if not plan_path.exists():
            raise RuntimeError(f"plan JSON not found: {plan_path}")
        plan_dialogue_lines, plans = load_plan_json(plan_path)

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
        _log(state, f"running evaluate via {resolved_server}")
        evaluation = evaluate_rendered_result(
            server=resolved_server,
            model=model,
            temperature=temperature,
            dialogue_lines=dialogue_lines,
            plans=plans,
            original_image_path=image_path,
            rendered_image_path=rendered_path,
        )
        payload = {
            "stage": "evaluate",
            "workspace": str(state.workspace),
            "input_image": str(image_path),
            "rendered_image": str(rendered_path),
            "plan_file": str(plan_path),
            "server": resolved_server,
            "model": model,
            "dialogue_lines": dialogue_lines,
            **evaluation,
        }
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
    temperature: float = typer.Option(0.0, "--temperature", "-t", help="Sampling temperature."),
    font: Path | None = typer.Option(None, "--font", help="Font file path."),
    font_family: str | None = typer.Option(None, "--font-family", help="CSS font-family override."),
    bubble_asset: Path | None = typer.Option(None, "--bubble-asset", help="Bubble asset path."),
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
) -> None:
    state: AppState = ctx.obj
    files = _workspace_files(state.workspace)
    try:
        validated_text_renderer = _validate_text_renderer(text_renderer)
        validated_bubble_renderer = _validate_bubble_renderer(bubble_renderer)
        validated_reflow_workers = _validate_reflow_workers(reflow_workers)
        metadata = _load_metadata(files.metadata)
        dialogue_lines = _resolve_dialogue_lines(dialogue, metadata)
        image_path = _resolve_input_path(input_path, metadata)
        resolved_server = _resolve_server(server)

        _log(state, "running assignment")
        _, assignment_plans = infer_assignment_plans(_dialogue_text(dialogue_lines))
        save_assignment_plan_json(files.assignment, dialogue_lines, assignment_plans)

        _log(state, f"running reflow via {resolved_server}")
        _, reflow_plans = infer_reflow_plans(
            server=resolved_server,
            model=model,
            dialogue=_dialogue_text(dialogue_lines),
            temperature=temperature,
            assignment_plans=assignment_plans,
            reflow_workers=validated_reflow_workers,
        )
        save_reflow_plan_json(files.reflow, dialogue_lines, reflow_plans)

        _log(state, f"running scene via {resolved_server}")
        _, scene_plans = infer_scene_bubble_plans(
            image_path=image_path,
            server=resolved_server,
            model=model,
            dialogue=_dialogue_text(dialogue_lines),
            temperature=temperature,
        )
        save_scene_plan_json(files.scene, dialogue_lines, scene_plans)

        plans = compose_bubble_plans(dialogue_lines, scene_plans, reflow_plans)
        save_plan_json(files.plan, dialogue_lines, plans)

        resolved_font_path = pick_font_path(str(font) if font is not None else None)
        resolved_bubble_asset = resolve_bubble_asset(str(bubble_asset) if bubble_asset is not None else None)
        if resolved_bubble_asset is None:
            raise RuntimeError(f"bubble asset not found: {bubble_asset}")
        _log(state, "rendering bubbles")
        render_bubbles(
            image_path=image_path,
            output_path=output_path,
            plans=plans,
            font_path=resolved_font_path,
            font_family=font_family,
            bubble_asset=resolved_bubble_asset,
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
            "stage": "run",
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
            "reflow_workers": validated_reflow_workers,
            **plans_payload(dialogue_lines, plans),
        }
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
    bubble_asset: Path | None = typer.Option(None, "--bubble-asset", help="Bubble asset path."),
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
        _, plans = infer_bubble_plans(
            image_path=image_path,
            server=resolved_server,
            model=model,
            dialogue=_dialogue_text(dialogue_lines),
            temperature=temperature,
        )
        save_plan_json(files.plan, dialogue_lines, plans)

        resolved_font_path = pick_font_path(str(font) if font is not None else None)
        resolved_bubble_asset = resolve_bubble_asset(str(bubble_asset) if bubble_asset is not None else None)
        if resolved_bubble_asset is None:
            raise RuntimeError(f"bubble asset not found: {bubble_asset}")
        _log(state, "rendering bubbles")
        render_bubbles(
            image_path=image_path,
            output_path=output_path,
            plans=plans,
            font_path=resolved_font_path,
            font_family=font_family,
            bubble_asset=resolved_bubble_asset,
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
