from __future__ import annotations

from importlib import import_module
from typing import Any


DEFAULT_SCENE_PLANNER = "cp-sat"
SUPPORTED_SCENE_PLANNERS = ("cp-sat", "llm")


def resolve_scene_planner(planner: str) -> str:
    normalized = planner.strip().lower()
    if normalized not in SUPPORTED_SCENE_PLANNERS:
        raise RuntimeError(f"unsupported scene planner: {planner}")
    return normalized


def load_scene_planner_module(planner: str) -> Any:
    resolved = resolve_scene_planner(planner)
    if resolved == "cp-sat":
        return import_module("bubble.scene_planners.cp_sat")
    raise RuntimeError(f"planner module is not package-backed: {planner}")
