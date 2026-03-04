# text-bubble

`llama.cpp` で `Qwen3.5-27B-heretic` を動かし、画像から固定セリフ入りの縦書き吹き出し画像を生成する最小パイプラインです。描画は `SVG + Playwright/Chromium` で行います。

2026年3月3日時点では、最新の `llama.cpp` ソースに `qwen3_5` / `qwen3vl` 系の実装が入っているため、Heretic の GGUF + `mmproj` を使う前提はあります。ここでは次の流れを作っています。

1. `llama.cpp` を CUDA でビルド
2. Heretic の GGUF と `mmproj` を取得
3. `llama-server` を multimodal で起動
4. 画像を投げて、テキストブロックの右上座標と縦書きの列分割を JSON で生成
5. Pillow で楕円のしっぽなし吹き出しを描画して画像を書き出し

## 前提

- Ubuntu 22.04 系
- NVIDIA GPU
- CUDA Toolkit
- Python 3.10+
- `uv`

システム依存を入れます。

```bash
apt-get update
apt-get install -y cmake ninja-build libcurl4-openssl-dev fonts-noto-cjk
```

## Python 環境

```bash
python3 -m pip install --user uv
~/.local/bin/uv venv .venv
~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  ~/.local/bin/uv run --python .venv/bin/python playwright install chromium
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  ~/.local/bin/uv run --python .venv/bin/python playwright install-deps chromium
```

同じ環境を手早く作り直す場合は次でも構いません。

```bash
./scripts/setup_local_env.sh
```

## llama.cpp のビルド

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cmake -S llama.cpp -B llama.cpp/build -G Ninja -DGGML_CUDA=ON
cmake --build llama.cpp/build --config Release -j 8
```

## モデル取得

デフォルトでは `mradermacher/Qwen3.5-27B-heretic-GGUF` の `Q4_K_M` と `mmproj-Q8_0` を取得します。

```bash
./scripts/download_model.sh
```

保存先:

- `models/heretic/Qwen3.5-27B-heretic.Q4_K_M.gguf`
- `models/heretic/Qwen3.5-27B-heretic.mmproj-Q8_0.gguf`

必要なら環境変数で上書きできます。

```bash
MODEL_FILE=Qwen3.5-27B-heretic.Q5_K_M.gguf \
MMPROJ_FILE=Qwen3.5-27B-heretic.mmproj-f16.gguf \
./scripts/download_model.sh
```

## サーバ起動

```bash
./scripts/run_server.sh
```

主な上書き変数:

```bash
PORT=8081 \
CTX_SIZE=8192 \
MODEL_ALIAS=heretic \
./scripts/run_server.sh
```

## `/notebooks` へ移すとき

このプロジェクトは基本的に repo 相対パスで動くので、`/notebooks/text-bubble` のような別パスへ移しても動かせます。移設時の注意点は次です。

- `.venv` は移動に弱いので、移設先で作り直す
- `imgs/`, `resources/`, `out/` を repo 配下の作業ディレクトリとして使う
- bubble 素材は `assets/` のほか `resources/` や `imgs/` に置いても拾える

移設後は次を実行します。

```bash
cd /notebooks/text-bubble
./scripts/setup_local_env.sh
```

## 画像から吹き出し生成

```bash
./.venv/bin/python bubble_pipeline.py \
  --input samples/input.png \
  --output out/bubbled.png \
  --plan-json out/plan.json \
  --dialogue "夜見のどこみてるのー？"
```

出力される JSON は `anchor_x`, `anchor_y`, `columns` を持ちます。`columns` は右から左へ並ぶ縦書き列です。`"".join(columns)` が `--dialogue` と完全一致しない場合は失敗します。

段階的に確認したい場合は `bubble_infer.py` の `--stage assignment|reflow|scene|full` を使います。`reflow` は text-only で列分割だけを検証できます。

主なオプション:

- `--server http://127.0.0.1:8080/v1`
- `--model heretic`
- `--dialogue "夜見のどこみてるのー？"`
- `--font /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`

## Reflow Prompt の検証

`reflow` 用 prompt は [`prompts/`](/storage/projects/text-bubble/prompts) に分けてあり、few-shot とテストケースも別ファイルにしています。

```bash
python3 scripts/test_reflow_prompt.py --indent 2
```

このスクリプトは [`prompts/reflow_test_cases.json`](/storage/projects/text-bubble/prompts/reflow_test_cases.json) を読み、各文を `1 bubble = 1 request` で `reflow` して結果を JSON で出します。

## 画像ごとに `.txt` を書く

`system.txt` と `user.txt` をファイルから読み、その内容を見ずに画像ごとの説明結果を同名 `.txt` へ保存する用途です。

```bash
python3 scripts/prompt_images.py
```

主なオプション:

- `--dir /path/to/imgs`
- `--system /path/to/system.txt`
- `--user /path/to/user.txt`
- `--overwrite`
- `--include 00005716`

## 補足

- Heretic は multimodal モデルなので `mmproj` が必要です。
- この実装は「漫画風の縦書きしっぽなし吹き出しを自動で置く」ための最小版です。厳密な人物追跡や口元推定はしていません。
- 文字列の列分割とテキストブロックの右上座標は Heretic が決め、吹き出し形状とレンダリングはブラウザ側で行います。
- 吹き出し位置はモデルの推論結果に依存します。品質を上げるなら、顔検出やセグメンテーションを別段で足すのが次の一手です。
