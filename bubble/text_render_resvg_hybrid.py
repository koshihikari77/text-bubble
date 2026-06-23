from __future__ import annotations

import html
import re
from pathlib import Path

from PIL import Image

from bubble.assets import css_font_literal, pick_fallback_font_paths, render_raw_svg_with_resvg
from bubble.glyph_paths import HarfBuzzGlyphPathRenderer
from bubble.models import BubblePlan, TEXT_COLOR
from bubble.vertical_uax import classify_text_clusters


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _safe_css_value(value: str | None, fallback: str) -> str:
    raw = (value or "").strip()
    return raw if raw else fallback


def _parse_px(value: str | None, default: float = 0.0) -> float:
    raw = (value or "").strip()
    if not raw:
        return default
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*px", raw)
    if not match:
        return default
    try:
        return float(match.group(1))
    except ValueError:
        return default


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _guess_font_family_from_path(font_path: str | None) -> str | None:
    if not font_path:
        return None
    stem = Path(font_path).stem
    lowered = stem.lower()
    if "jkg" in lowered:
        return "JK Gothic L"
    if "notosanscjk" in lowered:
        return "Noto Sans CJK JP"
    if "notoserifcjk" in lowered:
        return "Noto Serif CJK JP"
    if "ipa" in lowered or "ipag" in lowered:
        return "IPAGothic"
    if "dejavusans" in lowered:
        return "DejaVu Sans"
    return stem


def _shape_advance_px(
    *,
    renderer: HarfBuzzGlyphPathRenderer,
    cluster: str,
    direction: str,
    axis: str,
    font_size: int,
    features: dict[str, int] | None,
    fallback_px: float,
) -> float:
    shaped = renderer.shape_path(
        cluster,
        direction=direction,
        script="Jpan",
        language="ja",
        features=features,
    )
    upem = max(1, int(shaped.upem))
    raw_advance = abs(float(shaped.y_advance if axis == "y" else shaped.x_advance))
    if raw_advance <= 0.0:
        return max(1.0, fallback_px)
    return max(1.0, raw_advance * float(font_size) / float(upem))


def _cluster_path_element(
    *,
    renderer: HarfBuzzGlyphPathRenderer,
    cluster: str,
    center_x: float,
    center_y: float,
    font_size: int,
    direction: str,
    features: dict[str, int] | None = None,
    rotate_90: bool,
) -> str | None:
    shaped = renderer.shape_path(
        cluster,
        direction=direction,
        script="Jpan",
        language="ja",
        features=features,
    )
    if not shaped.d or shaped.bounds is None:
        return None
    x_min, y_min, x_max, y_max = shaped.bounds
    center_u_x = (x_min + x_max) / 2.0
    center_u_y = (y_min + y_max) / 2.0
    upem = max(1, int(shaped.upem))
    scale = float(font_size) / float(upem)
    transform_parts = [
        f"translate({_fmt_num(center_x)} {_fmt_num(center_y)})",
    ]
    if rotate_90:
        transform_parts.append("rotate(90)")
    transform_parts.append(f"scale({_fmt_num(scale)} {_fmt_num(-scale)})")
    transform_parts.append(f"translate({_fmt_num(-center_u_x)} {_fmt_num(-center_u_y)})")
    transform = " ".join(transform_parts)
    return (
        '<path d="{d}" transform="{transform}" fill="{fill}" />'
    ).format(
        d=html.escape(shaped.d, quote=True),
        transform=html.escape(transform, quote=True),
        fill=html.escape(TEXT_COLOR, quote=True),
    )


def build_resvg_hybrid_text_svg(
    *,
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
    text_letter_spacing: str,
    text_word_spacing: str,
    resvg_tu_override: bool,
) -> str:
    resolved_family = font_family or _guess_font_family_from_path(font_path)
    family = css_font_literal(resolved_family) if resolved_family else "sans-serif"
    letter_spacing = _safe_css_value(text_letter_spacing, "0")
    word_spacing = _safe_css_value(text_word_spacing, "0")
    letter_spacing_px = _parse_px(letter_spacing, 0.0)
    grid_step = max(16, int(round(text_layout["font_size"] + letter_spacing_px)))
    safe_style = (
        "writing-mode:vertical-rl;"
        "text-orientation:mixed;"
        "white-space:nowrap;"
        f"letter-spacing:{letter_spacing};"
        f"word-spacing:{word_spacing};"
    )
    manual_style = "text-anchor:middle;dominant-baseline:middle;writing-mode:horizontal-tb;"
    path_renderer: HarfBuzzGlyphPathRenderer | None = None
    if font_path:
        try:
            path_renderer = HarfBuzzGlyphPathRenderer(
                font_path,
                fallback_font_paths=pick_fallback_font_paths(font_path),
            )
        except Exception:  # noqa: BLE001
            path_renderer = None

    elements: list[str] = []
    text_left = text_layout["text_left"]
    text_top = text_layout["text_top"]
    block_width = text_layout["block_width"]
    column_width = text_layout["column_width"]
    column_gap = text_layout["column_gap"]

    for index, column in enumerate(plan.columns):
        column_left = text_left + block_width - column_width - index * (column_width + column_gap)
        decisions = classify_text_clusters(
            column,
            font_path=font_path,
            resvg_tu_override=resvg_tu_override,
        )
        if not decisions:
            continue
        center_x = column_left + column_width // 2
        step_sizes: list[float] = [float(grid_step)] * len(decisions)
        if path_renderer is not None:
            safe_indexes = [idx for idx, decision in enumerate(decisions) if decision.action == "safe"]
            for safe_idx in safe_indexes:
                safe_advance = _shape_advance_px(
                    renderer=path_renderer,
                    cluster=decisions[safe_idx].cluster,
                    direction="ttb",
                    axis="y",
                    font_size=text_layout["font_size"],
                    # direction="ttb" 時に HarfBuzz が縦組み feature を
                    # 自動適用するので、vert / vrt2 を明示指定しない。
                    # （OpenType 仕様上 vrt2 は vert の代替モデルで
                    # 併用するものではない）
                    features=None,
                    fallback_px=float(grid_step),
                )
                step_sizes[safe_idx] = max(1.0, safe_advance + letter_spacing_px)

            target_total = float(grid_step) * float(len(decisions))
            current_total = float(sum(step_sizes))
            if safe_indexes and abs(current_total - target_total) > 1e-6:
                delta_per_safe = (target_total - current_total) / float(len(safe_indexes))
                for safe_idx in safe_indexes:
                    step_sizes[safe_idx] = max(1.0, step_sizes[safe_idx] + delta_per_safe)

                adjusted_total = float(sum(step_sizes))
                residue = target_total - adjusted_total
                if abs(residue) > 1e-6:
                    step_sizes[-1] = max(1.0, step_sizes[-1] + residue)

        pen_y = float(text_top)
        for index_decision, decision in enumerate(decisions):
            step = step_sizes[index_decision]
            y_base = pen_y
            center_y = y_base + step / 2.0

            if path_renderer is not None:
                if decision.action == "safe":
                    path_node = _cluster_path_element(
                        renderer=path_renderer,
                        cluster=decision.cluster,
                        center_x=center_x,
                        center_y=center_y,
                        font_size=text_layout["font_size"],
                        direction="ttb",
                        # direction="ttb" の場合は HarfBuzz が縦組み feature を
                        # 自動適用する。vert / vrt2 同時指定は OpenType 仕様上
                        # 望ましくないので明示指定しない
                        features=None,
                        rotate_90=False,
                    )
                elif decision.action == "manual_sideways":
                    path_node = _cluster_path_element(
                        renderer=path_renderer,
                        cluster=decision.cluster,
                        center_x=center_x,
                        center_y=center_y,
                        font_size=text_layout["font_size"],
                        direction="ltr",
                        features=None,
                        rotate_90=True,
                    )
                else:
                    path_node = _cluster_path_element(
                        renderer=path_renderer,
                        cluster=decision.cluster,
                        center_x=center_x,
                        center_y=center_y,
                        font_size=text_layout["font_size"],
                        direction="ltr",
                        features=None,
                        rotate_90=False,
                    )
                if path_node is not None:
                    elements.append(path_node)
                    pen_y += step
                    continue

            if decision.action == "safe":
                elements.append(
                    (
                        '<text x="{x}" y="{y}" style="{style}">{text}</text>'
                    ).format(
                        x=center_x,
                        y=_fmt_num(y_base),
                        style=safe_style,
                        text=_escape_xml(decision.cluster),
                    )
                )
                pen_y += step
                continue
            text_node = (
                '<text x="{x}" y="{y}" style="{style}">{text}</text>'
            ).format(
                x=center_x,
                y=_fmt_num(center_y),
                style=manual_style,
                text=_escape_xml(decision.cluster),
            )
            if decision.action == "manual_sideways":
                text_node = (
                    '<g transform="rotate(90 {x} {y})">{node}</g>'
                ).format(
                    x=center_x,
                    y=_fmt_num(center_y),
                    node=text_node,
                )
            elements.append(text_node)
            pen_y += step

    body = "".join(elements)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{canvas_width}" height="{canvas_height}">
  <style>
    text {{
      font-family: {family};
      font-size: {text_layout["font_size"]}px;
      font-weight: 500;
      fill: {html.escape(TEXT_COLOR)};
    }}
  </style>
  {body}
</svg>
"""


def render_text_overlay_resvg_hybrid(
    *,
    canvas_width: int,
    canvas_height: int,
    plan: BubblePlan,
    text_layout: dict[str, int],
    font_path: str | None,
    font_family: str | None,
    text_letter_spacing: str,
    text_word_spacing: str,
    resvg_tu_override: bool,
    resvg_executable: str,
) -> Image.Image:
    svg_source = build_resvg_hybrid_text_svg(
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        plan=plan,
        text_layout=text_layout,
        font_path=font_path,
        font_family=font_family,
        text_letter_spacing=text_letter_spacing,
        text_word_spacing=text_word_spacing,
        resvg_tu_override=resvg_tu_override,
    )
    font_family_name: str | None = None
    if font_family and font_family.strip():
        font_family_name = font_family.strip()
    elif font_path:
        font_family_name = _guess_font_family_from_path(font_path)
    return render_raw_svg_with_resvg(
        svg_source=svg_source,
        width=canvas_width,
        height=canvas_height,
        executable=resvg_executable,
        font_path=font_path,
        font_family=font_family_name,
    )
