from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from bubble.ucd_vertical_orientation import vertical_orientation_for_codepoint

try:
    import regex as _regex
except Exception:  # noqa: BLE001
    _regex = None


VerticalOrientation = Literal["U", "R", "Tu", "Tr"]
ClusterAction = Literal["safe", "manual_sideways", "manual_upright"]

GRAPHEME_PATTERN = _regex.compile(r"\X") if _regex is not None else None

# アプリ側の tailoring override。Unicode UAX#50 の判定とは別レイヤで
# 「作品要件として ↑ や ↓ は U に倒したい」のような per-character override
# を管理する。空 dict なら UCD の判定そのまま。
#
# 例:
#   VERTICAL_ORIENTATION_OVERRIDES["↑"] = "U"
#
# ここに入る文字は UCD の値を上書きするので慎重に。
VERTICAL_ORIENTATION_OVERRIDES: dict[str, VerticalOrientation] = {}

# 「path 経路で LTR shape + 正立配置」する文字集合。
# resvg の <text> 描画では Tu でも誤って横倒しに描かれることがあり、
# その対策として導入された経緯から旧名は TU_RESVG_OVERRIDE_CHARS だったが、
# 現在は本番でも全 path 経路で描画しているため「resvg バグ回避」ではなく
# 「明示的に manual_upright 経路へ送る」役割になっている。
MANUAL_UPRIGHT_CHARS = frozenset({"？", "！"})

# 旧名称（後方互換のため alias を残す）
TU_RESVG_OVERRIDE_CHARS = MANUAL_UPRIGHT_CHARS


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


def vertical_orientation_of(cluster: str) -> VerticalOrientation:
    """grapheme cluster の Vertical_Orientation を決定する。

    優先順:
      1. `VERTICAL_ORIENTATION_OVERRIDES` の per-character tailoring
      2. UCD `VerticalOrientation.txt` の値
      3. 該当エントリ無しは UAX#50 のデフォルト `R`
    """

    if not cluster:
        return "R"
    base = cluster[0]
    override = VERTICAL_ORIENTATION_OVERRIDES.get(base)
    if override is not None:
        return override
    return vertical_orientation_for_codepoint(ord(base))


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
        """フォントに縦書き専用 glyph があるかを LTR vs TTB shape の差で判定する。

        direction="ttb" のとき HarfBuzz は vert / vrt2 等の縦組み feature を
        自動適用するため、`{vert:1, vrt2:1}` を明示する必要はなく、また
        OpenType 仕様上 vrt2 は vert の代替モデルなので同時指定は誤りである。
        """

        hb = self._hb

        def _glyph_ids(direction: str) -> tuple[int, ...]:
            buf = hb.Buffer()
            buf.add_str(cluster)
            buf.guess_segment_properties()
            buf.direction = direction
            buf.script = "Jpan"
            buf.language = "ja"
            hb.shape(self._font, buf, {})
            return tuple(info.codepoint for info in buf.glyph_infos)

        ids_h = _glyph_ids("ltr")
        ids_v = _glyph_ids("ttb")
        return bool(ids_h) and ids_h != ids_v


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
    if orientation == "Tu" and resvg_tu_override and cluster in MANUAL_UPRIGHT_CHARS:
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
