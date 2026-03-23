#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import shutil
from pathlib import Path
from typing import Any, TYPE_CHECKING

from PIL import Image

from bubble.models import SceneBubblePlan, save_scene_plan_json

if TYPE_CHECKING:
    from bubble.scene_runtime import RenderConfig


SOLVER_MODULES = {
    "beam": "bubble.experimental.beam_search_scene_solver",
    "cp-sat": "bubble.scene_planners.cp_sat",
}
PLANNER_MODES = ("solver", "cp-sat", "cp-sat-codex", "codex-first")
DEFAULT_CODEX_CLI_COMMAND = "codex"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build scene.json from reflow.json and external masks.")
    parser.add_argument("--image", required=True, help="Input image path")
    parser.add_argument("--reflow-json", required=True, help="Reflow JSON path")
    parser.add_argument("--face-mask", required=True, help="Face mask path")
    parser.add_argument("--person-mask", required=True, help="Person mask path")
    parser.add_argument("--chest-mask", help="Chest mask path")
    parser.add_argument("--lower-mask", help="Lower body mask path")
    parser.add_argument("--head-mask", help="Head/hair mask path")
    parser.add_argument("--solver", choices=sorted(SOLVER_MODULES), default="beam", help="Placement solver")
    parser.add_argument(
        "--planner-mode",
        choices=PLANNER_MODES,
        default="solver",
        help="PoC orchestration mode: solver-only, cp-sat, cp-sat plus Codex edits, or Codex-first.",
    )
    parser.add_argument(
        "--codex-edit-json",
        action="append",
        default=[],
        help="Scene edit JSON emitted by Codex. Can be passed multiple times and is applied sequentially.",
    )
    parser.add_argument(
        "--codex-backend",
        choices=("cli", "manual"),
        default="cli",
        help="How codex-first / cp-sat-codex obtains placement edits.",
    )
    parser.add_argument("--codex-command", default=DEFAULT_CODEX_CLI_COMMAND, help="Codex CLI command")
    parser.add_argument("--codex-model", default=None, help="Codex CLI model override")
    parser.add_argument("--codex-passes", type=int, default=1, help="Automatic Codex CLI refinement passes")
    parser.add_argument("--out-dir", required=True, help="Output directory")
    parser.add_argument("--font-size", type=int, default=0, help="Override estimated font size")
    parser.add_argument("--render-output", help="Override final rendered output path")
    parser.add_argument("--font", help="Font file path")
    parser.add_argument("--font-family", help="CSS font-family override")
    parser.add_argument("--bubble-asset", help="Bubble asset path")
    parser.add_argument("--text-renderer", default="resvg-hybrid", help="Text renderer backend")
    parser.add_argument("--bubble-renderer", default="resvg", help="Bubble renderer backend")
    parser.add_argument("--text-letter-spacing", default="-1px", help="Letter spacing for text renderer")
    parser.add_argument("--text-word-spacing", default="0", help="Word spacing for text renderer")
    parser.add_argument(
        "--resvg-tu-override",
        dest="resvg_tu_override",
        action="store_true",
        default=True,
        help="Force manual upright rendering for known Tu punctuation in resvg-hybrid",
    )
    parser.add_argument(
        "--no-resvg-tu-override",
        dest="resvg_tu_override",
        action="store_false",
        help="Disable Tu punctuation override in resvg-hybrid",
    )
    parser.add_argument("--use-worker", choices=("auto", "on", "off"), default="off", help="Worker mode")
    return parser.parse_args(argv)
def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _copy_if_exists(src: Path | None, dst: Path | None) -> None:
    if src is None or dst is None or not src.exists():
        return
    if src.resolve() == dst.resolve():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _planner_solver_name(planner_mode: str, fallback_solver: str) -> str:
    if planner_mode in {"cp-sat", "cp-sat-codex"}:
        return "cp-sat"
    return fallback_solver


def _codex_artifacts_enabled(planner_mode: str) -> bool:
    return planner_mode in {"cp-sat-codex", "codex-first"}


def _codex_scene_poc() -> Any:
    from bubble.experimental import codex_scene_poc

    return codex_scene_poc


def _scene_runtime() -> Any:
    from bubble import scene_runtime

    return scene_runtime


def _assets_module() -> Any:
    return importlib.import_module("bubble.assets")


def _validation_module() -> Any:
    return importlib.import_module("bubble.validation")


def _worker_request(*args: Any, **kwargs: Any) -> Any:
    return importlib.import_module("bubble.worker_client").worker_request(*args, **kwargs)


def _save_mask_composite(*args: Any, **kwargs: Any) -> Any:
    return _codex_scene_poc().save_mask_composite(*args, **kwargs)


def _load_scene_edit_json(*args: Any, **kwargs: Any) -> Any:
    return _codex_scene_poc().load_scene_edit_json(*args, **kwargs)


def _run_codex_cli_scene_edit(*args: Any, **kwargs: Any) -> Any:
    return _codex_scene_poc().run_codex_cli_scene_edit(*args, **kwargs)


def _summarize_debug_payload(*args: Any, **kwargs: Any) -> Any:
    debug_payload = args[0] if args else kwargs["debug_payload"]
    return {
        "feasible": debug_payload.get("feasible"),
        "objective_value": debug_payload.get("objective_value"),
        "hard_conflict_count": len(debug_payload.get("hard_conflicts", [])),
        "invalid_placement_count": len(debug_payload.get("invalid_placements", [])),
        "horizontal_span_px": debug_payload.get("horizontal_span_px"),
        "vertical_span_px": debug_payload.get("vertical_span_px"),
        "min_pair_distance_px": debug_payload.get("min_pair_distance_px"),
    }


def _pick_font_path(font: str | None) -> str | None:
    return _assets_module().pick_font_path(font)


def _resolve_bubble_asset(path: str | None) -> Path | None:
    return _assets_module().resolve_bubble_asset(path)


def _load_reflow_plan_json(path: Path) -> tuple[list[str], list[Any]]:
    return _validation_module().load_reflow_plan_json(path)


def _load_masks(
    *,
    solver_module: Any,
    image_width: int,
    image_height: int,
    face_mask_path: Path,
    person_mask_path: Path,
    chest_mask_path: Path | None,
    lower_mask_path: Path | None,
    head_mask_path: Path | None,
) -> tuple[Any, Any, Any, Any, Any]:
    face_mask = solver_module.load_binary_mask(face_mask_path)
    person_mask = solver_module.load_binary_mask(person_mask_path)
    chest_mask = solver_module.load_binary_mask(chest_mask_path) if chest_mask_path is not None else None
    lower_mask = solver_module.load_binary_mask(lower_mask_path) if lower_mask_path is not None else None
    head_mask = solver_module.load_binary_mask(head_mask_path) if head_mask_path is not None else None
    if face_mask.shape != (image_height, image_width):
        raise RuntimeError("face mask size does not match image size")
    if person_mask.shape != (image_height, image_width):
        raise RuntimeError("person mask size does not match image size")
    if chest_mask is not None and chest_mask.shape != (image_height, image_width):
        raise RuntimeError("chest mask size does not match image size")
    if lower_mask is not None and lower_mask.shape != (image_height, image_width):
        raise RuntimeError("lower mask size does not match image size")
    if head_mask is not None and head_mask.shape != (image_height, image_width):
        raise RuntimeError("head mask size does not match image size")
    return face_mask, person_mask, chest_mask, lower_mask, head_mask


def _iteration_paths(base_dir: Path) -> dict[str, Path]:
    return {
        "scene": base_dir / "scene.json",
        "plan": base_dir / "plan.json",
        "debug_overlay": base_dir / "debug_overlay.png",
        "debug_scores": base_dir / "debug_scores.json",
        "rendered": base_dir / "rendered.png",
        "mask_composite": base_dir / "mask_composite.png",
        "codex_board": base_dir / "codex_board.png",
        "prompt_context": base_dir / "prompt_context.json",
        "editable_scene_template": base_dir / "editable_scene_template.json",
    }


def _prepare_iteration_artifacts(
    *,
    planner_mode: str,
    iteration_name: str,
    iteration_dir: Path,
    image_path: Path,
    reflow_json_path: Path,
    dialogue_lines: list[str],
    reflow_plans: list[Any],
    scene_bundle: Any | None,
    debug_payload_seed: dict[str, Any] | None,
    previous_summary: dict[str, Any] | None,
    image_width: int,
    image_height: int,
    body_regions: Any,
    person_mask: Any,
    face_mask: Any,
    head_mask: Any,
    solver_module: Any,
    root_mask_composite_path: Path,
    render_output_path: Path,
    render_config: "RenderConfig" | None,
    worker_mode: str,
    reflow_json_path_for_worker: Path,
    prompt_note: str,
    source_edit_json: str | None,
) -> dict[str, Any]:
    iteration_dir.mkdir(parents=True, exist_ok=True)
    paths = _iteration_paths(iteration_dir)
    _copy_if_exists(root_mask_composite_path, paths["mask_composite"])

    scene_plans = scene_bundle.scene_plans if scene_bundle is not None else None
    debug_payload = dict(scene_bundle.debug_payload if scene_bundle is not None else (debug_payload_seed or {}))
    render_error: str | None = None
    if scene_plans is not None:
        save_scene_plan_json(paths["scene"], dialogue_lines, scene_plans)
    solver_module.render_debug_overlay(
        image_path=image_path,
        output_path=paths["debug_overlay"],
        solution=scene_bundle.evaluated_solution if scene_bundle is not None else None,
        person_mask=person_mask,
        face_mask=face_mask,
        body_regions=body_regions,
        head_mask=head_mask,
    )
    if scene_bundle is not None:
        if render_config is None:
            render_error = "bubble asset not found"
        else:
            try:
                response = _worker_request(
                    "render_from_scene",
                    {
                        "image_path": str(image_path),
                        "scene_path": str(paths["scene"]),
                        "reflow_path": str(reflow_json_path_for_worker),
                        "output_path": str(paths["rendered"]),
                        "plan_output_path": str(paths["plan"]),
                        "font": render_config.font_path,
                        "font_family": render_config.font_family,
                        "bubble_asset": str(render_config.bubble_asset),
                        "font_size": render_config.font_size,
                        "text_renderer": render_config.text_renderer,
                        "bubble_renderer": render_config.bubble_renderer,
                        "text_letter_spacing": render_config.text_letter_spacing,
                        "text_word_spacing": render_config.text_word_spacing,
                        "resvg_tu_override": render_config.resvg_tu_override,
                    },
                    mode=worker_mode,
                )
                if response is None:
                    _scene_runtime().render_scene_bundle(
                        image_path=image_path,
                        output_path=paths["rendered"],
                        bundle=scene_bundle,
                        config=render_config,
                    )
            except Exception as exc:  # noqa: BLE001
                render_error = str(exc)

    if scene_plans is None:
        debug_payload.update(
            {
                "status": "awaiting_codex_scene_edit",
                "planner_mode": planner_mode,
                "iteration": iteration_name,
                "placement_source": "codex-template",
                "font_size": debug_payload.get("font_size"),
            }
        )
    else:
        debug_payload.update(
            {
                "status": "render_error" if render_error is not None else "ok",
                "planner_mode": planner_mode,
                "iteration": iteration_name,
                "placement_source": debug_payload.get("placement_source", planner_mode),
                "source_edit_json": source_edit_json,
            }
        )
        if render_error is not None:
            debug_payload["render_error"] = render_error

    if _codex_artifacts_enabled(planner_mode):
        codex_scene_poc = _codex_scene_poc()
        template_payload = codex_scene_poc.build_editable_scene_template(
            planner_mode=planner_mode,
            reflow_plans=reflow_plans,
            scene_plans=scene_plans,
            note=prompt_note,
        )
        write_json(paths["editable_scene_template"], template_payload)

        codex_scene_poc.save_codex_board(
            paths["codex_board"],
            original_image_path=image_path,
            mask_composite_path=paths["mask_composite"],
            rendered_path=paths["rendered"] if paths["rendered"].exists() else None,
            debug_overlay_path=paths["debug_overlay"] if paths["debug_overlay"].exists() else None,
            title=f"{planner_mode} {iteration_name}",
            notes=[
                prompt_note,
                f"reflow: {reflow_json_path.name}",
                "Edit anchor_x / anchor_y only in editable_scene_template.json",
            ],
        )

        prompt_context = codex_scene_poc.build_prompt_context(
            planner_mode=planner_mode,
            reflow_json_path=reflow_json_path,
            image_path=image_path,
            image_width=image_width,
            image_height=image_height,
            dialogue_lines=dialogue_lines,
            reflow_plans=reflow_plans,
            body_regions=body_regions,
            current_scene_plans=scene_plans,
            current_debug_payload=debug_payload if scene_bundle is not None else None,
            previous_summary=previous_summary,
            codex_board_path=paths["codex_board"],
            mask_composite_path=paths["mask_composite"],
            debug_overlay_path=paths["debug_overlay"] if paths["debug_overlay"].exists() else None,
            rendered_path=paths["rendered"] if paths["rendered"].exists() else None,
        )
        write_json(paths["prompt_context"], prompt_context)
    write_json(paths["debug_scores"], debug_payload)
    return {
        "name": iteration_name,
        "dir": str(iteration_dir),
        "scene": str(paths["scene"]) if paths["scene"].exists() else None,
        "debug_overlay": str(paths["debug_overlay"]) if paths["debug_overlay"].exists() else None,
        "debug_scores": str(paths["debug_scores"]),
        "rendered": str(paths["rendered"]) if paths["rendered"].exists() else None,
        "mask_composite": str(paths["mask_composite"]),
        "codex_board": str(paths["codex_board"]) if paths["codex_board"].exists() else None,
        "prompt_context": str(paths["prompt_context"]) if paths["prompt_context"].exists() else None,
        "editable_scene_template": str(paths["editable_scene_template"]) if paths["editable_scene_template"].exists() else None,
        "status": debug_payload["status"],
        "summary": _summarize_debug_payload(debug_payload) if scene_bundle is not None else None,
    }


def _publish_iteration(iteration_dir: Path, out_dir: Path, render_output_path: Path) -> dict[str, Path | None]:
    root_paths = _iteration_paths(out_dir)
    iteration_paths = _iteration_paths(iteration_dir)
    for key in ("debug_overlay", "debug_scores", "mask_composite", "codex_board", "prompt_context", "editable_scene_template"):
        _copy_if_exists(iteration_paths[key], root_paths[key])
    _copy_if_exists(iteration_paths["scene"], root_paths["scene"])
    _copy_if_exists(iteration_paths["rendered"], render_output_path)
    return {
        "scene": root_paths["scene"] if root_paths["scene"].exists() else None,
        "debug_overlay": root_paths["debug_overlay"] if root_paths["debug_overlay"].exists() else None,
        "debug_scores": root_paths["debug_scores"] if root_paths["debug_scores"].exists() else None,
        "mask_composite": root_paths["mask_composite"] if root_paths["mask_composite"].exists() else None,
        "codex_board": root_paths["codex_board"] if root_paths["codex_board"].exists() else None,
        "prompt_context": root_paths["prompt_context"] if root_paths["prompt_context"].exists() else None,
        "editable_scene_template": root_paths["editable_scene_template"] if root_paths["editable_scene_template"].exists() else None,
        "rendered": render_output_path if render_output_path.exists() else None,
    }


def run_args(args: argparse.Namespace, *, emit_paths: bool = True) -> dict[str, Any]:
    if args.codex_passes < 0:
        raise RuntimeError("--codex-passes must be >= 0")

    result_payload: dict[str, Any]
    solver_name = _planner_solver_name(args.planner_mode, args.solver)
    solver_module = importlib.import_module(SOLVER_MODULES[solver_name])

    image_path = Path(args.image).resolve()
    reflow_json_path = Path(args.reflow_json).resolve()
    face_mask_path = Path(args.face_mask).resolve()
    person_mask_path = Path(args.person_mask).resolve()
    chest_mask_path = Path(args.chest_mask).resolve() if args.chest_mask else None
    lower_mask_path = Path(args.lower_mask).resolve() if args.lower_mask else None
    head_mask_path = Path(args.head_mask).resolve() if args.head_mask else None
    out_dir = Path(args.out_dir).resolve()
    iterations_dir = out_dir / "iterations"
    render_output_path = Path(args.render_output).resolve() if args.render_output else out_dir / "rendered.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    iterations_dir.mkdir(parents=True, exist_ok=True)

    image = Image.open(image_path)
    image_width, image_height = image.size
    font_size = args.font_size or solver_module.default_font_size(image_height)
    dialogue_lines, reflow_plans = _load_reflow_plan_json(reflow_json_path)

    face_mask, person_mask, chest_mask, lower_mask, head_mask = _load_masks(
        solver_module=solver_module,
        image_width=image_width,
        image_height=image_height,
        face_mask_path=face_mask_path,
        person_mask_path=person_mask_path,
        chest_mask_path=chest_mask_path,
        lower_mask_path=lower_mask_path,
        head_mask_path=head_mask_path,
    )
    body_regions = solver_module.build_body_regions(
        person_mask,
        face_mask,
        chest_mask=chest_mask,
        lower_mask=lower_mask,
    )

    root_mask_composite_path = out_dir / "mask_composite.png"
    _save_mask_composite(
        root_mask_composite_path,
        image_width=image_width,
        image_height=image_height,
        person_mask=person_mask,
        face_mask=face_mask,
        body_regions=body_regions,
        head_mask=head_mask,
    )

    resolved_font_path = _pick_font_path(args.font)
    resolved_bubble_asset = _resolve_bubble_asset(args.bubble_asset)
    if resolved_bubble_asset is None and args.planner_mode != "codex-first":
        raise RuntimeError(f"bubble asset not found: {args.bubble_asset}")
    render_config = None
    if resolved_bubble_asset is not None:
        render_config = _scene_runtime().RenderConfig(
            font_path=resolved_font_path,
            font_family=args.font_family,
            bubble_asset=resolved_bubble_asset,
            font_size=font_size,
            text_renderer=args.text_renderer,
            bubble_renderer=args.bubble_renderer,
            text_letter_spacing=args.text_letter_spacing,
            text_word_spacing=args.text_word_spacing,
            resvg_tu_override=args.resvg_tu_override,
        )

    manifest_entries: list[dict[str, Any]] = []
    previous_summary: dict[str, Any] | None = None
    final_iteration_dir: Path | None = None

    def _apply_edit(edit_path: Path, *, edit_index: int, prompt_note_default: str) -> Path:
        nonlocal previous_summary, final_iteration_dir

        _, edited_scene_plans, edit_notes = _load_scene_edit_json(edit_path, reflow_plans=reflow_plans)
        edit_bundle = _scene_runtime().materialize_scene_bundle(
            dialogue_lines=dialogue_lines,
            reflow_plans=reflow_plans,
            scene_plans=edited_scene_plans,
            image_width=image_width,
            image_height=image_height,
            face_mask=face_mask,
            person_mask=person_mask,
            chest_mask=chest_mask,
            lower_mask=lower_mask,
            head_mask=head_mask,
            font_size=font_size,
            source="codex-edit",
        )
        iteration_name = f"iter{edit_index:02d}"
        iteration_dir = iterations_dir / iteration_name
        manifest_entries.append(
            _prepare_iteration_artifacts(
                planner_mode=args.planner_mode,
                iteration_name=iteration_name,
                iteration_dir=iteration_dir,
                image_path=image_path,
                reflow_json_path=reflow_json_path,
                dialogue_lines=dialogue_lines,
                reflow_plans=reflow_plans,
                scene_bundle=edit_bundle,
                debug_payload_seed=None,
                previous_summary=previous_summary,
                image_width=image_width,
                image_height=image_height,
                body_regions=body_regions,
                person_mask=person_mask,
                face_mask=face_mask,
                head_mask=head_mask,
                solver_module=solver_module,
                root_mask_composite_path=root_mask_composite_path,
                render_output_path=render_output_path,
                render_config=render_config,
                worker_mode=args.use_worker,
                reflow_json_path_for_worker=reflow_json_path,
                prompt_note=edit_notes or prompt_note_default,
                source_edit_json=str(edit_path),
            )
        )
        previous_summary = _summarize_debug_payload(edit_bundle.debug_payload)
        final_iteration_dir = iteration_dir
        return iteration_dir

    if args.planner_mode in {"solver", "cp-sat", "cp-sat-codex"}:
        try:
            initial_solution = None
            if solver_name == "cp-sat":
                response = _worker_request(
                    "solve_cp_sat_scene",
                    {
                        "image_path": str(image_path),
                        "reflow_path": str(reflow_json_path),
                        "face_mask": str(face_mask_path),
                        "person_mask": str(person_mask_path),
                        "chest_mask": None if chest_mask_path is None else str(chest_mask_path),
                        "lower_mask": None if lower_mask_path is None else str(lower_mask_path),
                        "head_mask": None if head_mask_path is None else str(head_mask_path),
                        "font_size": font_size,
                    },
                    mode=args.use_worker,
                )
                if response is not None:
                    initial_solution = _scene_runtime().deserialize_evaluated_solution(dict(response["solution"]))
            if initial_solution is None:
                initial_solution = solver_module.solve_scene_layout(
                    reflow_plans=reflow_plans,
                    image_width=image_width,
                    image_height=image_height,
                    face_mask=face_mask,
                    person_mask=person_mask,
                    chest_mask=chest_mask,
                    lower_mask=lower_mask,
                    head_mask=head_mask,
                    font_size=font_size,
                )
        except Exception as exc:  # noqa: BLE001
            debug_payload: dict[str, Any]
            try:
                debug_payload = json.loads(str(exc))
            except json.JSONDecodeError:
                debug_payload = {"error": str(exc)}
            debug_payload.update(
                {
                    "status": "error",
                    "planner_mode": args.planner_mode,
                    "solver": solver_name,
                    "image": str(image_path),
                    "reflow_json": str(reflow_json_path),
                    "face_mask": str(face_mask_path),
                    "person_mask": str(person_mask_path),
                    "chest_mask": str(chest_mask_path) if chest_mask_path is not None else None,
                    "lower_mask": str(lower_mask_path) if lower_mask_path is not None else None,
                    "head_mask": str(head_mask_path) if head_mask_path is not None else None,
                    "font_size": font_size,
                    "body_regions": body_regions.to_debug_dict(),
                }
            )
            error_dir = iterations_dir / "iter00"
            error_dir.mkdir(parents=True, exist_ok=True)
            error_paths = _iteration_paths(error_dir)
            _copy_if_exists(root_mask_composite_path, error_paths["mask_composite"])
            solver_module.render_debug_overlay(
                image_path=image_path,
                output_path=error_paths["debug_overlay"],
                solution=None,
                person_mask=person_mask,
                face_mask=face_mask,
                body_regions=body_regions,
                head_mask=head_mask,
            )
            write_json(error_paths["debug_scores"], debug_payload)
            if _codex_artifacts_enabled(args.planner_mode):
                _codex_scene_poc().save_codex_board(
                    error_paths["codex_board"],
                    original_image_path=image_path,
                    mask_composite_path=error_paths["mask_composite"],
                    rendered_path=None,
                    debug_overlay_path=error_paths["debug_overlay"],
                    title=f"{args.planner_mode} iter00 error",
                    notes=["Initial placement failed", "See debug_scores.json for details"],
                )
            _publish_iteration(error_dir, out_dir, render_output_path)
            result_payload = {
                "exit_code": 1,
                "planner_mode": args.planner_mode,
                "solver": solver_name,
                "iterations_manifest": str(iterations_dir / "manifest.json"),
                "final_artifacts": {
                    "debug_scores": str(error_paths["debug_scores"]),
                },
            }
            if emit_paths:
                print(str(error_paths["debug_scores"]), file=sys.stderr)
            return result_payload

        initial_bundle = _scene_runtime().bundle_from_evaluated_solution(
            dialogue_lines=dialogue_lines,
            reflow_plans=reflow_plans,
            evaluated_solution=initial_solution,
            source=solver_name,
        )
        initial_dir = iterations_dir / "iter00"
        manifest_entries.append(
            _prepare_iteration_artifacts(
                planner_mode=args.planner_mode,
                iteration_name="iter00",
                iteration_dir=initial_dir,
                image_path=image_path,
                reflow_json_path=reflow_json_path,
                dialogue_lines=dialogue_lines,
                reflow_plans=reflow_plans,
                scene_bundle=initial_bundle,
                debug_payload_seed=None,
                previous_summary=None,
                image_width=image_width,
                image_height=image_height,
                body_regions=body_regions,
                person_mask=person_mask,
                face_mask=face_mask,
                head_mask=head_mask,
                solver_module=solver_module,
                root_mask_composite_path=root_mask_composite_path,
                render_output_path=render_output_path,
                render_config=render_config,
                worker_mode=args.use_worker,
                reflow_json_path_for_worker=reflow_json_path,
                prompt_note="Initial solver placement. Edit anchors if you want Codex refinement.",
                source_edit_json=None,
            )
        )
        previous_summary = _summarize_debug_payload(initial_bundle.debug_payload)
        final_iteration_dir = initial_dir

    if args.planner_mode == "codex-first":
        template_dir = iterations_dir / "iter00"
        waiting_debug_payload = {
            "solver": "codex-template",
            "font_size": font_size,
            "placement_source": "codex-first",
        }
        manifest_entries.append(
            _prepare_iteration_artifacts(
                planner_mode=args.planner_mode,
                iteration_name="iter00",
                iteration_dir=template_dir,
                image_path=image_path,
                reflow_json_path=reflow_json_path,
                dialogue_lines=dialogue_lines,
                reflow_plans=reflow_plans,
                scene_bundle=None,
                debug_payload_seed=waiting_debug_payload,
                previous_summary=None,
                image_width=image_width,
                image_height=image_height,
                body_regions=body_regions,
                person_mask=person_mask,
                face_mask=face_mask,
                head_mask=head_mask,
                solver_module=solver_module,
                root_mask_composite_path=root_mask_composite_path,
                render_output_path=render_output_path,
                render_config=render_config,
                worker_mode=args.use_worker,
                reflow_json_path_for_worker=reflow_json_path,
                prompt_note="Codex-first mode. Fill editable_scene_template.json with initial anchors.",
                source_edit_json=None,
            )
        )
        final_iteration_dir = template_dir

    next_edit_index = 1
    if args.planner_mode in {"cp-sat-codex", "codex-first"} and args.codex_backend == "cli":
        for _ in range(args.codex_passes):
            if final_iteration_dir is None:
                raise RuntimeError("no iteration available for Codex CLI planning")
            current_paths = _iteration_paths(final_iteration_dir)
            edit_path = out_dir / f"codex_edit_iter{next_edit_index:02d}.json"
            _run_codex_cli_scene_edit(
                planner_mode=args.planner_mode,
                command=args.codex_command,
                model=args.codex_model,
                repo_root=ROOT_DIR,
                prompt_context_path=current_paths["prompt_context"],
                editable_template_path=current_paths["editable_scene_template"],
                codex_board_path=current_paths["codex_board"],
                mask_composite_path=current_paths["mask_composite"],
                output_path=edit_path,
            )
            _apply_edit(
                edit_path,
                edit_index=next_edit_index,
                prompt_note_default="Codex CLI revision applied. Inspect this board and iterate if needed.",
            )
            next_edit_index += 1

    for raw_edit_path in args.codex_edit_json:
        edit_path = Path(raw_edit_path).resolve()
        _apply_edit(
            edit_path,
            edit_index=next_edit_index,
            prompt_note_default="Codex revision applied. Inspect this board and iterate if needed.",
        )
        next_edit_index += 1

    if final_iteration_dir is None:
        raise RuntimeError("failed to materialize any PoC iteration")

    published_paths = _publish_iteration(final_iteration_dir, out_dir, render_output_path)
    manifest_payload = {
        "planner_mode": args.planner_mode,
        "solver": solver_name,
        "image": str(image_path),
        "reflow_json": str(reflow_json_path),
        "iterations": manifest_entries,
        "final_iteration": manifest_entries[-1]["name"],
        "final_artifacts": {key: (None if value is None else str(value)) for key, value in published_paths.items()},
    }
    write_json(iterations_dir / "manifest.json", manifest_payload)

    result_payload = {
        "exit_code": 0,
        "planner_mode": args.planner_mode,
        "solver": solver_name,
        "iterations_manifest": str(iterations_dir / "manifest.json"),
        "final_artifacts": {key: (None if value is None else str(value)) for key, value in published_paths.items()},
    }
    for key in ("scene", "debug_overlay", "mask_composite", "codex_board", "prompt_context", "editable_scene_template", "debug_scores", "rendered"):
        path = published_paths.get(key)
        if emit_paths and path is not None and Path(path).exists():
            print(str(path))
    if emit_paths:
        print(str(iterations_dir / "manifest.json"))
    return result_payload


def main(argv: list[str] | None = None) -> int:
    result = run_args(parse_args(argv), emit_paths=True)
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
