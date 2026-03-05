# Pango/HarfBuzz/Cairo + resvg 縦書き PoC

目的: ブラウザ依存を下げられるかを、PoCで可否判定する。  
対象: Paperspace Linux。  
方針: JS必須にはせず、`resvg` は CLI で検証する。

## 合格条件

- 文字別ハックなし（`ー` などの手動回転を入れない）
- 2文/5文で `browser` 比の `total_sec` が同等以下
- 記号を含む縦書きケースで視覚的な破綻がない

## 準備

```bash
apt-get update
apt-get install -y \
  libcairo2 \
  libpango-1.0-0 \
  libpangocairo-1.0-0 \
  libharfbuzz0b \
  fonts-noto-cjk \
  pango1.0-tools

uv pip install cairocffi pangocffi pangocairocffi uharfbuzz
```

`resvg` は別途インストール済み前提（`which resvg` で確認）。

## 1) baseline / pango 比較

2文 baseline:

```bash
uv run python scripts/poc_vertical_render.py \
  --renderer browser \
  --bubble-renderer resvg \
  --input imgs/test.png \
  --plan-json out/test_5lines_n3/plan.json \
  --num-bubbles 2 \
  --runs 3 \
  --output out/poc/vertical/browser_n2.png
```

2文 pango:

```bash
uv run python scripts/poc_vertical_render.py \
  --renderer pango \
  --bubble-renderer resvg \
  --input imgs/test.png \
  --plan-json out/test_5lines_n3/plan.json \
  --num-bubbles 2 \
  --runs 3 \
  --output out/poc/vertical/pango_n2.png
```

同様に `--num-bubbles 5` で 5文を測定。

## 2) HarfBuzz shaping probe

```bash
uv run python scripts/poc_vertical_shape_probe.py \
  --text "夜見のどこみてるのー？" \
  --direction ttb \
  --features vert=1,vrt2=1 \
  --output out/poc/vertical/shape_probe.json
```

## 3) SVG縦書き + resvg CLI 検証

```bash
uv run python scripts/poc_resvg_vertical_cli.py \
  --output-dir out/poc/resvg_vertical \
  --cases-file scripts/poc_svg_vertical_cases.json
```

ケース定義は `scripts/poc_svg_vertical_cases.json`。  
出力はケースごとの `.svg/.png` と `metrics_resvg_vertical_cli.json`。

## 出力物

- `out/poc/vertical/*.png`
- `out/poc/vertical/*.metrics.json`
- `out/poc/vertical/shape_probe.json`
- `out/poc/resvg_vertical/*.png`
- `out/poc/resvg_vertical/metrics_resvg_vertical_cli.json`

## 判定

- `pass_quality_all_runs` / `pass_quality_all_cases` を確認
- `summary.total_sec.mean` を baseline と比較
- 破綻時は `shape_probe.json` で `vert/vrt2` の適用を確認し、フォント要因か実装要因か切り分ける
