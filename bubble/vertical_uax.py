from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
import unicodedata

try:
    import regex as _regex
except Exception:  # noqa: BLE001
    _regex = None


VerticalOrientation = Literal["U", "R", "Tu", "Tr"]
ClusterAction = Literal["safe", "manual_sideways", "manual_upright"]

GRAPHEME_PATTERN = _regex.compile(r"\X") if _regex is not None else None

# Common vertical forms that should rotate in traditional Japanese layout when no vertical glyph exists.
TR_CHARS = frozenset(
    "「」『』【】〔〕〖〗〈〉《》"
    "（）()［］[]｛｝{}"
    "ー―…‥〜～〰"
)

# Common upright punctuation in vertical writing.
TU_CHARS = frozenset(
    "、。，．・：；？！"
    "｡､･"
    "♡♥❤❥❣"  # hearts (upright in vertical text)
)

# Characters that are "Tu" in Unicode but currently render incorrectly in resvg for our workload.
TU_RESVG_OVERRIDE_CHARS = frozenset({"？", "！", "♡", "♥", "❤", "❥", "❣"})


@dataclass(frozen=True)
class ClusterDecision:
    cluster: str
    orientation: VerticalOrientation
    action: ClusterAction


@dataclass(frozen=True)
class ClusterRun:
    action: ClusterAction
    start: int
    length: int
    text: str


def split_graphemes(text: str) -> list[str]:
    if not text:
        return []
    if GRAPHEME_PATTERN is None:
        return list(text)
    return [chunk for chunk in GRAPHEME_PATTERN.findall(text) if chunk]


def _is_ascii_cluster(cluster: str) -> bool:
    return bool(cluster) and all(ord(ch) < 0x80 for ch in cluster)


def _is_cjk_upright_codepoint(codepoint: int) -> bool:
    return (
        0x3040 <= codepoint <= 0x309F  # Hiragana
        or 0x30A0 <= codepoint <= 0x30FF  # Katakana
        or 0x31F0 <= codepoint <= 0x31FF  # Katakana Phonetic Extensions
        or 0x3400 <= codepoint <= 0x4DBF  # CJK Unified Ideographs Extension A
        or 0x4E00 <= codepoint <= 0x9FFF  # CJK Unified Ideographs
        or 0xF900 <= codepoint <= 0xFAFF  # CJK Compatibility Ideographs
    )


def vertical_orientation_of(cluster: str) -> VerticalOrientation:
    if not cluster:
        return "R"
    base = cluster[0]
    if base in TR_CHARS:
        return "Tr"
    if base in TU_CHARS:
        return "Tu"
    if _is_ascii_cluster(cluster):
        return "R"

    cp = ord(base)
    if _is_cjk_upright_codepoint(cp):
        return "U"

    # Wide/full-width characters are typically upright in Japanese vertical text.
    if unicodedata.east_asian_width(base) in {"W", "F"}:
        return "U"
    return "R"


class HarfBuzzVerticalProbe:
    def __init__(self, font_path: str) -> None:
        import uharfbuzz as hb

        data = Path(font_path).read_bytes()
        face = hb.Face(data)
        font = hb.Font(face)
        font.scale = (face.upem, face.upem)
        self._hb = hb
        self._font = font

    @lru_cache(maxsize=8192)
    def has_vertical_substitution(self, cluster: str) -> bool:
        hb = self._hb

        def _glyph_ids(features: dict[str, int]) -> tuple[int, ...]:
            buf = hb.Buffer()
            buf.add_str(cluster)
            buf.guess_segment_properties()
            buf.direction = "ttb"
            buf.script = "Jpan"
            buf.language = "ja"
            hb.shape(self._font, buf, features)
            return tuple(info.codepoint for info in buf.glyph_infos)

        ids_off = _glyph_ids({})
        ids_vert = _glyph_ids({"vert": 1, "vrt2": 1})
        return bool(ids_off) and ids_off != ids_vert


def classify_cluster_action(
    cluster: str,
    orientation: VerticalOrientation,
    probe: HarfBuzzVerticalProbe | None,
    *,
    resvg_tu_override: bool,
) -> ClusterAction:
    if orientation == "R":
        return "manual_sideways"
    if orientation == "Tr":
        if probe and probe.has_vertical_substitution(cluster):
            return "safe"
        return "manual_sideways"
    if orientation == "Tu" and resvg_tu_override and cluster in TU_RESVG_OVERRIDE_CHARS:
        return "manual_upright"
    return "safe"


def classify_text_clusters(
    text: str,
    *,
    font_path: str | None,
    resvg_tu_override: bool,
) -> list[ClusterDecision]:
    clusters = split_graphemes(text)
    if not clusters:
        return []

    probe: HarfBuzzVerticalProbe | None = None
    if font_path:
        try:
            probe = HarfBuzzVerticalProbe(font_path)
        except Exception:  # noqa: BLE001
            probe = None
    decisions: list[ClusterDecision] = []
    for cluster in clusters:
        orientation = vertical_orientation_of(cluster)
        action = classify_cluster_action(cluster, orientation, probe, resvg_tu_override=resvg_tu_override)
        decisions.append(ClusterDecision(cluster=cluster, orientation=orientation, action=action))
    return decisions


def segment_runs(decisions: list[ClusterDecision]) -> list[ClusterRun]:
    if not decisions:
        return []
    runs: list[ClusterRun] = []
    start = 0
    current_action = decisions[0].action
    for index in range(1, len(decisions)):
        if decisions[index].action == current_action:
            continue
        text = "".join(item.cluster for item in decisions[start:index])
        runs.append(ClusterRun(action=current_action, start=start, length=index - start, text=text))
        start = index
        current_action = decisions[index].action
    text = "".join(item.cluster for item in decisions[start:])
    runs.append(ClusterRun(action=current_action, start=start, length=len(decisions) - start, text=text))
    return runs
