#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bubble.evaluate import evaluate_rendered_result  # noqa: E402
from bubble.validation import load_plan_json  # noqa: E402


DEFAULT_RENDERED_CANDIDATES = ("run_output.png", "full_output.png", "output.png")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the evaluate prompt against multiple rendered cases.")
    parser.add_argument("--server", default="http://127.0.0.1:8080/v1", help="llama-server base URL")
    parser.add_argument("--model", default="heretic", help="Model alias exposed by llama-server")
    parser.add_argument("--temperature", default=0.0, type=float, help="Sampling temperature")
    parser.add_argument(
        "--cases-file",
        default=str(ROOT_DIR / "prompts" / "evaluate_test_cases.json"),
        help="JSON file containing an array of evaluate test cases",
    )
    parser.add_argument("--limit", type=int, help="Optional maximum number of cases to run")
    parser.add_argument("--indent", default=2, type=int, help="JSON output indentation")
    return parser.parse_args()


def _resolve_path(raw: str | None, *, base_dir: Path) -> Path | None:
    if raw is None:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return (base_dir / candidate).resolve()


def _resolve_with_fallback(raw: str | None, *, base_dirs: list[Path]) -> Path | None:
    if raw is None:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    for base_dir in base_dirs:
        resolved = (base_dir / candidate).resolve()
        if resolved.exists():
            return resolved
    return (base_dirs[0] / candidate).resolve()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    if not isinstance(data, list):
        raise RuntimeError(f"cases file must be a JSON array: {path}")
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"case {index} must be a JSON object")
        cases.append(item)
    return cases


def _guess_rendered_path(workspace: Path) -> Path | None:
    for name in DEFAULT_RENDERED_CANDIDATES:
        candidate = workspace / name
        if candidate.exists():
            return candidate
    return None


def _load_workspace_metadata(workspace: Path) -> dict[str, Any]:
    metadata_path = workspace / "metadata.json"
    if not metadata_path.exists():
        return {}
    data = _load_json(metadata_path)
    if not isinstance(data, dict):
        raise RuntimeError(f"workspace metadata must be a JSON object: {metadata_path}")
    return data


def _resolve_case_paths(case: dict[str, Any], *, cases_dir: Path) -> dict[str, Path]:
    workspace = _resolve_path(case.get("workspace"), base_dir=cases_dir)
    workspace_metadata = _load_workspace_metadata(workspace) if workspace is not None and workspace.exists() else {}

    plan_path = _resolve_path(case.get("plan_json"), base_dir=cases_dir)
    if plan_path is None and workspace is not None:
        plan_path = workspace / "plan.json"

    original_path = _resolve_path(case.get("original_image"), base_dir=cases_dir)
    if original_path is None:
        metadata_input = workspace_metadata.get("input_image")
        if isinstance(metadata_input, str) and metadata_input.strip():
            base_dirs = [ROOT_DIR]
            if workspace is not None:
                base_dirs.insert(0, workspace)
            original_path = _resolve_with_fallback(metadata_input, base_dirs=base_dirs)

    rendered_path = _resolve_path(case.get("rendered_image"), base_dir=cases_dir)
    if rendered_path is None and workspace is not None:
        rendered_path = _guess_rendered_path(workspace)

    resolved = {
        "plan_json": plan_path,
        "original_image": original_path,
        "rendered_image": rendered_path,
    }
    missing = [key for key, value in resolved.items() if value is None]
    if missing:
        raise RuntimeError(f"missing required paths for case: {', '.join(missing)}")
    for key, value in resolved.items():
        if value is None or not value.exists():
            raise RuntimeError(f"{key} not found: {value}")
    return {key: value for key, value in resolved.items() if value is not None}


def _resolve_dialogue_lines(
    case: dict[str, Any],
    *,
    workspace: Path | None,
    plan_dialogue_lines: list[str],
) -> list[str]:
    explicit = case.get("dialogue_lines")
    if isinstance(explicit, list) and explicit and all(isinstance(item, str) and item.strip() for item in explicit):
        return [item.strip() for item in explicit]
    if workspace is not None and workspace.exists():
        metadata = _load_workspace_metadata(workspace)
        existing = metadata.get("dialogue_lines")
        if isinstance(existing, list) and existing and all(isinstance(item, str) and item.strip() for item in existing):
            return [item.strip() for item in existing]
    return plan_dialogue_lines


def _match_expected_subset(expected: list[str] | None, actual: list[str]) -> bool | None:
    if expected is None:
        return None
    return all(item in actual for item in expected)


def _normalize_expected_strings(value: Any, *, label: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RuntimeError(f"{label} must be an array of non-empty strings")
    return [item.strip() for item in value]


def _check_expectations(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    actual_types = [str(item["type"]) for item in result["issues"]]
    actual_fix_stages = [str(item["fix_stage"]) for item in result["issues"]]

    expected_verdict = case.get("expected_verdict")
    if expected_verdict is not None and not isinstance(expected_verdict, str):
        raise RuntimeError("expected_verdict must be a string")

    min_score = case.get("min_score")
    max_score = case.get("max_score")
    if min_score is not None:
        min_score = float(min_score)
    if max_score is not None:
        max_score = float(max_score)

    expected_issue_types = _normalize_expected_strings(case.get("expected_issue_types"), label="expected_issue_types")
    expected_fix_stages = _normalize_expected_strings(case.get("expected_fix_stages"), label="expected_fix_stages")

    checks: dict[str, Any] = {
        "verdict_matches": None if expected_verdict is None else result["verdict"] == expected_verdict.strip().lower(),
        "min_score_ok": None if min_score is None else result["score"] >= min_score,
        "max_score_ok": None if max_score is None else result["score"] <= max_score,
        "issue_types_match": _match_expected_subset(expected_issue_types, actual_types),
        "fix_stages_match": _match_expected_subset(expected_fix_stages, actual_fix_stages),
    }
    meaningful = [value for value in checks.values() if value is not None]
    checks["all_passed"] = None if not meaningful else all(bool(value) for value in meaningful)
    return checks


def run_case(
    case: dict[str, Any],
    *,
    cases_dir: Path,
    server: str,
    model: str,
    temperature: float,
) -> dict[str, Any]:
    workspace = _resolve_path(case.get("workspace"), base_dir=cases_dir)
    paths = _resolve_case_paths(case, cases_dir=cases_dir)
    plan_dialogue_lines, plans = load_plan_json(paths["plan_json"])
    dialogue_lines = _resolve_dialogue_lines(case, workspace=workspace, plan_dialogue_lines=plan_dialogue_lines)

    started_at = time.monotonic()
    result = evaluate_rendered_result(
        server=server,
        model=model,
        temperature=temperature,
        dialogue_lines=dialogue_lines,
        plans=plans,
        original_image_path=paths["original_image"],
        rendered_image_path=paths["rendered_image"],
    )
    elapsed_sec = round(time.monotonic() - started_at, 3)

    return {
        "name": case.get("name") or paths["rendered_image"].stem,
        "status": "ok",
        "elapsed_sec": elapsed_sec,
        "workspace": str(workspace) if workspace is not None else None,
        "plan_json": str(paths["plan_json"]),
        "original_image": str(paths["original_image"]),
        "rendered_image": str(paths["rendered_image"]),
        "result": result,
        "checks": _check_expectations(case, result),
    }


def main() -> int:
    args = parse_args()
    cases_path = Path(args.cases_file).resolve()
    if not cases_path.is_file():
        raise RuntimeError(f"cases file not found: {cases_path}")

    cases = load_cases(cases_path)
    if args.limit is not None:
        cases = cases[: max(0, args.limit)]
    if not cases:
        raise RuntimeError("no test cases to run")

    results: list[dict[str, Any]] = []
    matched_cases = 0
    unchecked_cases = 0
    error_cases = 0
    for case in cases:
        try:
            entry = run_case(
                case,
                cases_dir=cases_path.parent,
                server=args.server,
                model=args.model,
                temperature=args.temperature,
            )
            if entry["checks"]["all_passed"] is True:
                matched_cases += 1
            elif entry["checks"]["all_passed"] is None:
                unchecked_cases += 1
        except Exception as exc:  # noqa: BLE001
            error_cases += 1
            entry = {
                "name": case.get("name") if isinstance(case.get("name"), str) else None,
                "status": "error",
                "error": exc.__class__.__name__,
                "message": str(exc) if str(exc) else exc.__class__.__name__,
            }
        results.append(entry)

    print(
        json.dumps(
            {
                "server": args.server,
                "model": args.model,
                "temperature": args.temperature,
                "cases_file": str(cases_path),
                "count": len(results),
                "matched_cases": matched_cases,
                "unchecked_cases": unchecked_cases,
                "error_cases": error_cases,
                "results": results,
            },
            ensure_ascii=False,
            indent=args.indent,
        )
    )
    return 0 if error_cases == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
