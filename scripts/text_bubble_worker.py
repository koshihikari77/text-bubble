#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socketserver
import sys
from pathlib import Path
from typing import Any

from PIL import Image

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bubble.assets import pick_font_path, resolve_bubble_asset  # noqa: E402
from bubble.models import (  # noqa: E402
    assignment_plans_payload,
    plans_payload,
    reflow_plans_payload,
    save_assignment_plan_json,
    save_plan_json,
    save_reflow_plan_json,
    save_scene_plan_json,
    scene_plans_payload,
)
from bubble.scene_runtime import (  # noqa: E402
    LLMRoute,
    RenderConfig,
    compose_scene_bundle,
    infer_scene_stage,
    render_scene_bundle,
    resolve_scene_route,
    run_pipeline,
)
from bubble.validation import load_reflow_plan_json, load_scene_plan_json  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Long-lived local worker for text-bubble scene/runtime tasks.")
    parser.add_argument("--socket", required=True, help="Unix socket path")
    return parser.parse_args()


def _load_mask(path: str | None):
    if not path:
        return None
    scripts_dir = ROOT_DIR / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from cp_sat_scene_solver import load_binary_mask

    return load_binary_mask(Path(path))


def _render_config_from_payload(payload: dict[str, Any]) -> RenderConfig:
    bubble_asset = resolve_bubble_asset(payload.get("bubble_asset"))
    if bubble_asset is None:
        raise RuntimeError(f"bubble asset not found: {payload.get('bubble_asset')}")
    return RenderConfig(
        font_path=pick_font_path(payload.get("font")),
        font_family=payload.get("font_family"),
        bubble_asset=bubble_asset,
        font_size=int(payload.get("font_size", 0)),
        text_renderer=str(payload["text_renderer"]),
        bubble_renderer=str(payload["bubble_renderer"]),
        text_letter_spacing=str(payload["text_letter_spacing"]),
        text_word_spacing=str(payload["text_word_spacing"]),
        resvg_tu_override=bool(payload["resvg_tu_override"]),
    )


def _scene_route_from_payload(payload: dict[str, Any]) -> LLMRoute:
    return resolve_scene_route(
        default_server=str(payload["server"]),
        default_model=str(payload["model"]),
        scene_server=payload.get("scene_server"),
        scene_model=payload.get("scene_model"),
    )


def _scene_command(payload: dict[str, Any]) -> dict[str, Any]:
    dialogue_lines = [str(item) for item in payload["dialogue_lines"]]
    image_path = Path(payload["image_path"])
    output_scene_path = Path(payload["output_scene_path"])
    route = _scene_route_from_payload(payload)
    _, scene_plans = infer_scene_stage(
        image_path=image_path,
        dialogue_lines=dialogue_lines,
        route=route,
        temperature=float(payload["temperature"]),
    )
    save_scene_plan_json(output_scene_path, dialogue_lines, scene_plans)
    return {
        "status": "ok",
        "server": route.server,
        "model": route.model,
        "output_file": str(output_scene_path),
        **scene_plans_payload(dialogue_lines, scene_plans),
    }


def _render_from_scene_command(payload: dict[str, Any]) -> dict[str, Any]:
    image_path = Path(payload["image_path"])
    output_path = Path(payload["output_path"])
    plan_output_path = Path(payload["plan_output_path"])
    dialogue_lines, scene_plans = load_scene_plan_json(Path(payload["scene_path"]))
    reflow_dialogue_lines, reflow_plans = load_reflow_plan_json(Path(payload["reflow_path"]))
    if dialogue_lines != reflow_dialogue_lines:
        raise RuntimeError("scene JSON dialogue_lines do not match reflow JSON dialogue_lines")
    bundle = compose_scene_bundle(
        dialogue_lines=dialogue_lines,
        reflow_plans=reflow_plans,
        scene_plans=scene_plans,
        source="scene-json",
    )
    save_plan_json(plan_output_path, dialogue_lines, bundle.composed_plans)
    config = _render_config_from_payload(payload)
    render_scene_bundle(
        image_path=image_path,
        output_path=output_path,
        bundle=bundle,
        config=config,
    )
    return {
        "status": "ok",
        "output_file": str(output_path),
        "plan_file": str(plan_output_path),
        **plans_payload(dialogue_lines, bundle.composed_plans),
    }


def _run_pipeline_command(payload: dict[str, Any]) -> dict[str, Any]:
    image_path = Path(payload["image_path"])
    image_width, image_height = Image.open(image_path).size
    dialogue_lines = [str(item) for item in payload["dialogue_lines"]]
    default_route = LLMRoute(server=str(payload["server"]), model=str(payload["model"]))
    scene_route = _scene_route_from_payload(payload)
    result = run_pipeline(
        image_path=image_path,
        dialogue_lines=dialogue_lines,
        default_route=default_route,
        scene_route=scene_route,
        temperature=float(payload["temperature"]),
        reflow_workers=int(payload["reflow_workers"]),
        image_width=image_width,
        image_height=image_height,
        face_mask=_load_mask(payload.get("face_mask")),
        person_mask=_load_mask(payload.get("person_mask")),
        chest_mask=_load_mask(payload.get("chest_mask")),
        lower_mask=_load_mask(payload.get("lower_mask")),
        head_mask=_load_mask(payload.get("head_mask")),
        font_size=int(payload["font_size"]),
    )
    save_assignment_plan_json(Path(payload["assignment_path"]), dialogue_lines, result.assignment_plans)
    save_reflow_plan_json(Path(payload["reflow_path"]), dialogue_lines, result.reflow_plans)
    save_scene_plan_json(Path(payload["scene_path"]), dialogue_lines, result.scene_bundle.scene_plans)
    save_plan_json(Path(payload["plan_path"]), dialogue_lines, result.scene_bundle.composed_plans)
    render_scene_bundle(
        image_path=image_path,
        output_path=Path(payload["output_path"]),
        bundle=result.scene_bundle,
        config=_render_config_from_payload(payload),
    )
    return {
        "status": "ok",
        "server": result.default_route.server,
        "model": result.default_route.model,
        "scene_server": result.scene_route.server,
        "scene_model": result.scene_route.model,
        "assignment_file": str(payload["assignment_path"]),
        "reflow_file": str(payload["reflow_path"]),
        "scene_file": str(payload["scene_path"]),
        "plan_file": str(payload["plan_path"]),
        "output_file": str(payload["output_path"]),
        "reflow_workers": result.reflow_workers,
        "dialogue_lines": dialogue_lines,
        "assignment": assignment_plans_payload(dialogue_lines, result.assignment_plans)["bubbles"],
        "reflow": reflow_plans_payload(dialogue_lines, result.reflow_plans)["bubbles"],
        "scene": scene_plans_payload(dialogue_lines, result.scene_bundle.scene_plans)["bubbles"],
        "plans": plans_payload(dialogue_lines, result.scene_bundle.composed_plans)["bubbles"],
    }


def _dispatch(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    if command == "ping":
        return {"status": "ok", "message": "pong"}
    if command == "scene_stage":
        return _scene_command(payload)
    if command == "render_from_scene":
        return _render_from_scene_command(payload)
    if command == "run_pipeline":
        return _run_pipeline_command(payload)
    raise RuntimeError(f"unsupported worker command: {command}")


class WorkerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            response = _dispatch(str(request["command"]), dict(request.get("payload", {})))
        except Exception as exc:  # noqa: BLE001
            response = {"status": "error", "message": str(exc) if str(exc) else exc.__class__.__name__}
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")


def main() -> int:
    args = parse_args()
    socket_path = Path(args.socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    server = socketserver.UnixStreamServer(str(socket_path), WorkerHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if socket_path.exists():
            socket_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
