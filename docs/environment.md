# Environment Setup

このプロジェクトで現在使っている実行環境の整理です。対象は `llama.cpp + Heretic + bubble_pipeline.py` です。

## 目的

- `llama.cpp` で `Qwen3.5-27B-heretic` を multimodal で動かす
- `bubble_pipeline.py` から `llama-server` に画像を投げる
- 吹き出し素材と縦書き文字を `SVG + Playwright/Chromium` で描画する

## ディレクトリ

- プロジェクトルート: `/storage/projects/text-bubble`
- Python 仮想環境: [`/.venv`](/storage/projects/text-bubble/.venv)
- Playwright ブラウザ: [`/.playwright-browsers`](/storage/projects/text-bubble/.playwright-browsers)
- `llama.cpp`: [`/llama.cpp`](/storage/projects/text-bubble/llama.cpp)
- モデル: [`/models/heretic`](/storage/projects/text-bubble/models/heretic)

## Python 環境

`uv` で仮想環境を作成している。

```bash
python3 -m pip install --user uv
~/.local/bin/uv venv .venv
~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt
```

現状 `requirements.txt` に入っている主要依存:

- `Pillow`
- `playwright`

## Playwright

ブラウザはプロジェクトローカルに配置している。

```bash
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  ~/.local/bin/uv run --python .venv/bin/python playwright install chromium
```

`bubble_pipeline.py` は `PLAYWRIGHT_BROWSERS_PATH` を `.playwright-browsers` に向ける。さらに `headless_shell` ではなく通常の Chromium 実行ファイルを優先して使う。

理由:

- `headless_shell` ではこの環境で HTML テキストが描画されなかった
- 通常の Chromium ではテキスト描画が通った

## システム依存

ビルドとブラウザ描画で必要だった主要パッケージ:

```bash
apt-get update
apt-get install -y \
  cmake \
  ninja-build \
  libcurl4-openssl-dev \
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

現在使っている外部素材:

- 吹き出し PNG: [bubble.png](/notebooks/resources/bubble.png)
- 吹き出し SVG: [bubble.svg](/notebooks/resources/bubble.svg)
- 吹き出し SVG コード: [bubble_svg.txt](/notebooks/resources/bubble_svg.txt)
- フォント: [JKG-L_3.ttf](/notebooks/resources/JKG-L_3.ttf)

## 出力先

ユーザー確認用の入出力は `/notebooks/imgs` を使う運用にしている。

例:

- 入力画像: [00005716.png](/notebooks/imgs/00005716.png)
- 出力画像: [00005716_bubbled.png](/notebooks/imgs/00005716_bubbled.png)
- plan JSON: [00005716_plan.json](/notebooks/imgs/00005716_plan.json)
