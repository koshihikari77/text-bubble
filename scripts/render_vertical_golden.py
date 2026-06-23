"""縦書きレンダリングの比較用 golden 画像セットを書き出す。

`docs/designlog/golden_vertical/` 配下に複数 font size で出力する。
P0 の変更後・将来の変更前後でこのフォルダの内容を目視 / pixel 比較する。

Usage:
    .venv/bin/python scripts/render_vertical_golden.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from bubble.assets import pick_font_path, resolve_resvg_executable
from bubble.layout import compute_text_layout
from bubble.models import BubblePlan
from bubble.text_render_resvg_hybrid import render_text_overlay_resvg_hybrid


CASES: dict[str, list[str]] = {
    "hearts_only": ["♡♥❤❥❣"],
    "hearts_with_text": ["あ♡い", "愛♡愛"],
    "hearts_with_punct": ["？♡！"],
    "hearts_triple": ["♡♡♡"],
    "symbols_misc": ["♪♫♬★☆●○◎"],
    "circled_numbers": ["①②③④⑤"],
    "ellipsis": ["…‥", "あ…い"],
    "dashes": ["──", "あ──い"],
    "ascii_mix": ["Hi123!"],
    "punctuation": ["、。「」（）ー〜"],
    "longer_text": ["夜見のどこ", "みてるのー？"],
}

FONT_SIZES = (28, 48, 72)


def render_case(
    *,
    name: str,
    columns: list[str],
    font_size: int,
    font_path: str,
    resvg_executable: str,
    out_dir: Path,
) -> Path:
    plan = BubblePlan(
        anchor_x=0.5,
        anchor_y=0.5,
        sentence_ids=[1],
        columns=columns,
        speaker_id="t",
        bubble_type="ellipse",
    )
    canvas_w, canvas_h = 600, 900
    text_layout = compute_text_layout(
        canvas_w, canvas_h, plan, font_size, font_path=font_path
    )
    img = render_text_overlay_resvg_hybrid(
        canvas_width=canvas_w,
        canvas_height=canvas_h,
        plan=plan,
        text_layout=text_layout,
        font_path=font_path,
        font_family=None,
        text_letter_spacing="-1px",
        text_word_spacing="0",
        resvg_tu_override=True,
        resvg_executable=resvg_executable,
    )
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    bg.alpha_composite(img)
    out_path = out_dir / f"{name}_fs{font_size}.png"
    bg.convert("RGB").save(out_path)
    return out_path


def main() -> int:
    font_path = pick_font_path(None)
    if not font_path:
        print("primary font not found")
        return 1
    resvg = resolve_resvg_executable()
    if resvg is None:
        print("resvg executable not found")
        return 1

    out_dir = Path("docs/designlog/golden_vertical")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"writing to {out_dir}/")
    count = 0
    for name, columns in CASES.items():
        for size in FONT_SIZES:
            try:
                path = render_case(
                    name=name,
                    columns=columns,
                    font_size=size,
                    font_path=font_path,
                    resvg_executable=resvg,
                    out_dir=out_dir,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL {name} fs={size}: {exc}")
                continue
            print(f"  {path.name}")
            count += 1
    print(f"\nwrote {count} images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
