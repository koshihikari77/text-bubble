# Environment Setup

このプロジェクトで現在使っている実行環境の整理です。対象は `llama.cpp + Heretic + text-bubble CLI` です。

## 目的

- `llama.cpp` で `Qwen3.5-27B-heretic` を multimodal で動かす
- `text-bubble` から `llama-server` に画像を投げる
- 吹き出し素材を `resvg` または `Playwright/Chromium`、文字を `resvg-hybrid` または `Playwright/Chromium` で描画する

## ディレクトリ

- プロジェクトルート: このフォルダ自身
- `uv tool` インストール済みコマンド: `text-bubble`
- Playwright ブラウザ: `.playwright-browsers`
- `llama.cpp`: `llama.cpp/`
- モデル: `models/heretic/`

## ツール環境

`uv tool install -e .` で CLI をインストールする。

```bash
uv tool install -e .
```

再インストールする場合:

```bash
uv tool install -e . --reinstall
```

## Playwright

ブラウザはプロジェクトローカルに配置している。

```bash
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  uv tool run --from text-bubble playwright install chromium
```

必要な環境ではシステム依存も追加する。

```bash
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  uv tool run --from text-bubble playwright install-deps chromium
```

`text-bubble` は `PLAYWRIGHT_BROWSERS_PATH` を `.playwright-browsers` に向ける。さらに `headless_shell` ではなく通常の Chromium 実行ファイルを優先して使う。

理由:

- `headless_shell` ではこの環境で HTML テキストが描画されなかった
- 通常の Chromium ではテキスト描画が通った

## resvg

SVG 吹き出しのラスタ化は既定で `resvg` を使う。

```bash
apt-get update
apt-get install -y resvg
```

CLI 上書き:

- `--bubble-renderer resvg`（既定）
- `--bubble-renderer browser`（互換経路）
- `--text-renderer resvg-hybrid`（既定）
- `--text-renderer browser`（互換経路）
- `resvg-hybrid` 調整: `--text-letter-spacing`, `--text-word-spacing`, `--resvg-tu-override/--no-resvg-tu-override`

カスタム実行ファイルを使う場合は `TEXT_BUBBLE_RESVG=/path/to/resvg` を指定する。

## システム依存

ビルドとブラウザ描画で必要だった主要パッケージ:

```bash
apt-get update
apt-get install -y \
  cmake \
  ninja-build \
  libcurl4-openssl-dev \
  resvg \
  fonts-noto-cjk \
  libcairo2 \
  libpango-1.0-0 \
  libpangocairo-1.0-0 \
  libfontconfig1 \
  libharfbuzz0b
```

補足:

- `libcairo2`, `libpango-*`, `libfontconfig1`, `libharfbuzz0b` は通常 Chromium の文字描画に必要だった
- これが無いと Playwright の Chromium 実行時に shared library error が出た
- `resvg` が無い状態で `--bubble-renderer resvg` または `--text-renderer resvg-hybrid` を使うと即エラーになる

## llama.cpp

CUDA 有効でビルドしている。

```bash
cmake -S llama.cpp -B llama.cpp/build -G Ninja -DGGML_CUDA=ON
cmake --build llama.cpp/build --target llama-server -j 8
```

主な成果物:

- [`llama-server`](/storage/projects/text-bubble/llama.cpp/build/bin/llama-server)

## モデル

保存先:

- [`Qwen3.5-27B-heretic.Q4_K_M.gguf`](/storage/projects/text-bubble/models/heretic/Qwen3.5-27B-heretic.Q4_K_M.gguf)
- [`Qwen3.5-27B-heretic.mmproj-Q8_0.gguf`](/storage/projects/text-bubble/models/heretic/Qwen3.5-27B-heretic.mmproj-Q8_0.gguf)

取得スクリプト:

- [`download_model.sh`](/storage/projects/text-bubble/scripts/download_model.sh)

## サーバ起動

起動スクリプト:

- [`run_server.sh`](/storage/projects/text-bubble/scripts/run_server.sh)

前提:

- `llama-server` がビルド済み
- GGUF と `mmproj` が配置済み

## ユーザー素材

現在使っている素材置き場の候補:

- repo 内 `resources/`
- repo 内 `imgs/`
- 互換用の `/notebooks/resources`
- 互換用の `/notebooks/imgs`

## 出力先

移設しやすくするため、現在は repo 内の `imgs/`, `resources/`, `out/` を基本の作業ディレクトリとしている。

例:

- 入力画像: `imgs/00005716.png`
- 出力画像: `out/00005716_bubbled.png`
- plan JSON: `out/00005716_plan.json`

## 補助ツール

現在の補助コマンド/スクリプト:

- 新CLI: `text-bubble`（`assign/reflow/scene/render/run/full`）
- 互換用旧CLI: [`bubble_infer.py`](/storage/projects/text-bubble/bubble_infer.py), [`bubble_render.py`](/storage/projects/text-bubble/bubble_render.py)
- SVG 比率変換実験: [`warp_bubble_svg.py`](/storage/projects/text-bubble/scripts/warp_bubble_svg.py)
- 正方形 bubble 実験: [`render_square_bubble_experiment.py`](/storage/projects/text-bubble/scripts/render_square_bubble_experiment.py)
