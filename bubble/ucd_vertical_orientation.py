"""Parse Unicode UCD `VerticalOrientation.txt` and expose orientation lookup.

The bundled `assets/VerticalOrientation.txt` is the authoritative source for
Vertical_Orientation values per UAX #50.  Any codepoint not in the file
defaults to `"R"` (per UAX #50 §4).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from pathlib import Path
from typing import Literal

VerticalOrientation = Literal["U", "R", "Tu", "Tr"]


_UCD_PATH = Path(__file__).resolve().parent.parent / "assets" / "VerticalOrientation.txt"

# parsed sorted disjoint ranges as (start_inclusive, end_inclusive, value)
_RangeTuple = tuple[int, int, VerticalOrientation]
_RANGES: list[_RangeTuple] = []
_STARTS: list[int] = []


def _parse_ucd_file(path: Path) -> list[_RangeTuple]:
    line_pattern = re.compile(
        r"^\s*([0-9A-Fa-f]+)(?:\.\.([0-9A-Fa-f]+))?\s*;\s*([URT][ur]?)\b"
    )
    parsed: list[_RangeTuple] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.lstrip().startswith("#"):
            continue
        match = line_pattern.match(raw_line)
        if match is None:
            continue
        start = int(match.group(1), 16)
        end = int(match.group(2), 16) if match.group(2) else start
        value = match.group(3)
        if value not in {"U", "R", "Tu", "Tr"}:
            continue
        parsed.append((start, end, value))  # type: ignore[arg-type]
    parsed.sort(key=lambda item: item[0])
    return parsed


def _ensure_loaded() -> None:
    global _RANGES, _STARTS
    if _RANGES:
        return
    if not _UCD_PATH.exists():
        raise FileNotFoundError(
            f"VerticalOrientation.txt not found at {_UCD_PATH}. "
            "It should be bundled under assets/."
        )
    _RANGES = _parse_ucd_file(_UCD_PATH)
    _STARTS = [start for start, _, _ in _RANGES]


def vertical_orientation_for_codepoint(codepoint: int) -> VerticalOrientation:
    """Return the UAX #50 Vertical_Orientation value for a single codepoint.

    Defaults to `"R"` when the codepoint is not listed (per UAX #50).
    """

    _ensure_loaded()
    if not _RANGES:
        return "R"
    # bisect_right gives index of the first range with start > cp.
    # The candidate range is the one at index - 1.
    index = bisect_right(_STARTS, codepoint) - 1
    if index < 0:
        return "R"
    start, end, value = _RANGES[index]
    if start <= codepoint <= end:
        return value
    return "R"


def has_explicit_entry(codepoint: int) -> bool:
    """For introspection / testing: True if the UCD file explicitly lists this codepoint."""

    _ensure_loaded()
    if not _RANGES:
        return False
    index = bisect_right(_STARTS, codepoint) - 1
    if index < 0:
        return False
    start, end, _ = _RANGES[index]
    return start <= codepoint <= end


def loaded_range_count() -> int:
    """Number of UCD ranges loaded (testing aid)."""

    _ensure_loaded()
    return len(_RANGES)
