"""Project-wide review state for the HITL bubble editor.

Stores per-case and per-bubble review comments plus an overall comment in
`<project>/review.json`. Disappeared case/bubble comments are moved to
`orphans` (mirroring reorder_images).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bubble.editor_models import load_case_document, load_project


REVIEW_VERSION = 1
REVIEW_FILE_NAME = "review.json"


def review_path(project_dir: Path) -> Path:
    return project_dir / REVIEW_FILE_NAME


def empty_review(project_dir: Path) -> dict[str, Any]:
    return {
        "version": REVIEW_VERSION,
        "input_dir": str(project_dir),
        "updated_at": None,
        "overall": "",
        "cases": [],
        "orphans": [],
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".review-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_review_raw(project_dir: Path) -> dict[str, Any] | None:
    path = review_path(project_dir)
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"review.json must be an object, got {type(data).__name__}")
    return data


def _current_layout(project_dir: Path) -> dict[str, list[str]]:
    project = load_project(project_dir)
    layout: dict[str, list[str]] = {}
    for case in project.get("cases", []):
        if not isinstance(case, dict):
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str):
            continue
        try:
            document = load_case_document(project_dir, case_id)
        except Exception:  # noqa: BLE001
            layout[case_id] = []
            continue
        bubble_ids = [str(b["bubble_id"]) for b in document.get("bubbles", []) if isinstance(b, dict) and isinstance(b.get("bubble_id"), str)]
        layout[case_id] = bubble_ids
    return layout


def _normalize_bubble_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    bubble_id = entry.get("bubble_id")
    if not isinstance(bubble_id, str) or not bubble_id:
        return None
    comment = entry.get("comment", "") or ""
    if not isinstance(comment, str):
        return None
    return {"bubble_id": bubble_id, "comment": comment}


def _normalize_case_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    case_id = entry.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        return None
    comment = entry.get("comment", "") or ""
    if not isinstance(comment, str):
        return None
    bubbles_raw = entry.get("bubbles") or []
    bubbles: list[dict[str, Any]] = []
    if isinstance(bubbles_raw, list):
        for raw in bubbles_raw:
            normalized = _normalize_bubble_entry(raw)
            if normalized is not None:
                bubbles.append(normalized)
    return {"case_id": case_id, "comment": comment, "bubbles": bubbles}


def _migrate_against_layout(
    raw: dict[str, Any],
    layout: dict[str, list[str]],
    *,
    project_dir: Path,
) -> tuple[dict[str, Any], bool]:
    """Drop empty comments and move disappeared entries to `orphans`.

    Returns the normalized review dict and a `changed` flag.
    """

    if raw.get("version") != REVIEW_VERSION:
        return empty_review(project_dir), True

    overall = raw.get("overall") or ""
    if not isinstance(overall, str):
        overall = ""

    orphans: list[dict[str, Any]] = []
    existing_orphans = raw.get("orphans") or []
    if isinstance(existing_orphans, list):
        for entry in existing_orphans:
            if isinstance(entry, dict) and isinstance(entry.get("comment"), str) and entry["comment"]:
                orphans.append(dict(entry))

    cases_out: list[dict[str, Any]] = []
    changed = False

    cases_raw = raw.get("cases") or []
    if not isinstance(cases_raw, list):
        cases_raw = []

    for entry in cases_raw:
        normalized = _normalize_case_entry(entry)
        if normalized is None:
            changed = True
            continue
        case_id = normalized["case_id"]
        case_comment = normalized["comment"]
        bubble_entries = normalized["bubbles"]

        if case_id not in layout:
            if case_comment:
                orphans.append({
                    "kind": "case",
                    "case_id": case_id,
                    "comment": case_comment,
                    "lost_at": _now_iso(),
                    "reason": "case_missing",
                })
                changed = True
            for bubble_entry in bubble_entries:
                if bubble_entry["comment"]:
                    orphans.append({
                        "kind": "bubble",
                        "case_id": case_id,
                        "bubble_id": bubble_entry["bubble_id"],
                        "comment": bubble_entry["comment"],
                        "lost_at": _now_iso(),
                        "reason": "case_missing",
                    })
                    changed = True
            continue

        valid_bubble_ids = set(layout[case_id])
        kept_bubbles: list[dict[str, Any]] = []
        seen_bubble_ids: set[str] = set()
        for bubble_entry in bubble_entries:
            bubble_id = bubble_entry["bubble_id"]
            comment = bubble_entry["comment"]
            if not comment:
                changed = True
                continue
            if bubble_id in seen_bubble_ids:
                changed = True
                continue
            seen_bubble_ids.add(bubble_id)
            if bubble_id not in valid_bubble_ids:
                orphans.append({
                    "kind": "bubble",
                    "case_id": case_id,
                    "bubble_id": bubble_id,
                    "comment": comment,
                    "lost_at": _now_iso(),
                    "reason": "bubble_missing",
                })
                changed = True
                continue
            kept_bubbles.append({"bubble_id": bubble_id, "comment": comment})

        if case_comment or kept_bubbles:
            cases_out.append({
                "case_id": case_id,
                "comment": case_comment,
                "bubbles": kept_bubbles,
            })
        else:
            # entry exists in raw but had nothing useful → considered cleanup
            changed = True

    result = {
        "version": REVIEW_VERSION,
        "input_dir": str(project_dir),
        "updated_at": raw.get("updated_at"),
        "overall": overall,
        "cases": cases_out,
        "orphans": orphans,
    }
    return result, changed


def load_review(project_dir: Path) -> dict[str, Any]:
    """Load review.json with orphan migration applied. Persists migrations."""

    raw = _read_review_raw(project_dir)
    if raw is None:
        return empty_review(project_dir)
    layout = _current_layout(project_dir)
    migrated, changed = _migrate_against_layout(raw, layout, project_dir=project_dir)
    if changed:
        migrated = save_review(project_dir, migrated, _from_migration=True)
    return migrated


def save_review(
    project_dir: Path,
    body: dict[str, Any],
    *,
    _from_migration: bool = False,
) -> dict[str, Any]:
    """Validate, normalize, and persist a review payload."""

    overall = body.get("overall", "") or ""
    if not isinstance(overall, str):
        raise ValueError("overall must be a string")

    cases_in = body.get("cases") or []
    if not isinstance(cases_in, list):
        raise ValueError("cases must be an array")

    layout = _current_layout(project_dir)

    cases_out: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()

    for entry in cases_in:
        normalized = _normalize_case_entry(entry)
        if normalized is None:
            raise ValueError("cases[] entries must include a non-empty case_id")
        case_id = normalized["case_id"]
        if case_id in seen_case_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen_case_ids.add(case_id)

        if not _from_migration and case_id not in layout:
            raise ValueError(f"unknown case_id: {case_id}")

        valid_bubble_ids: set[str] = set(layout.get(case_id, []))
        kept_bubbles: list[dict[str, Any]] = []
        seen_bubble_ids: set[str] = set()
        for bubble_entry in normalized["bubbles"]:
            bubble_id = bubble_entry["bubble_id"]
            comment = bubble_entry["comment"]
            if not comment:
                continue
            if bubble_id in seen_bubble_ids:
                raise ValueError(f"duplicate bubble_id within {case_id}: {bubble_id}")
            seen_bubble_ids.add(bubble_id)
            if not _from_migration and bubble_id not in valid_bubble_ids:
                raise ValueError(f"unknown bubble_id within {case_id}: {bubble_id}")
            kept_bubbles.append({"bubble_id": bubble_id, "comment": comment})

        if normalized["comment"] or kept_bubbles:
            cases_out.append({
                "case_id": case_id,
                "comment": normalized["comment"],
                "bubbles": kept_bubbles,
            })

    orphans_in = body.get("orphans") or []
    if not isinstance(orphans_in, list):
        raise ValueError("orphans must be an array")

    payload = {
        "version": REVIEW_VERSION,
        "input_dir": str(project_dir),
        "updated_at": _now_iso(),
        "overall": overall,
        "cases": cases_out,
        "orphans": list(orphans_in),
    }
    _atomic_write_json(review_path(project_dir), payload)
    return payload
