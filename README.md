# text-bubble

`llama.cpp` 上の `Qwen3.5-27B-heretic` を使って、画像に縦書き吹き出しを合成する CLI ツールです。  
推論は OpenAI 互換の `llama-server` API、描画は `resvg` / `Playwright/Chromium` + Pillow で行います。

## セットアップ

前提:

- Ubuntu 22.04 系
- NVIDIA GPU（推論をローカル実行する場合）
- Python 3.10+
- `uv`

ローカル環境の作成:

```bash
./scripts/setup_local_env.sh
```

再インストールしたい場合:

```bash
./scripts/setup_local_env.sh --reinstall
```

Playwright のシステム依存導入も含める場合:

```bash
./scripts/setup_local_env.sh --with-deps
```

## llama.cpp / モデル / サーバ

`llama.cpp` のビルド:

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cmake -S llama.cpp -B llama.cpp/build -G Ninja -DGGML_CUDA=ON
cmake --build llama.cpp/build --config Release -j 8
```

モデル取得:

```bash
./scripts/download_model.sh
```

サーバ起動:

```bash
./scripts/run_server.sh
```

Paperspace 公開 URL で使う場合:

```bash
./scripts/run_server.sh --paperspace-public
```

`resvg` を使う場合:

```bash
apt-get update
apt-get install -y resvg
```

## 新CLI (`text-bubble`)

共通:

- workspace は `-w/--workspace`（デフォルト `out/workspace`）
- `--json` で機械可読 JSON を stdout に出力
- `--quiet` で進捗ログ抑制
- `--server` 省略時は `TEXT_BUBBLE_SERVER`、未設定なら `http://127.0.0.1:8080/v1`
- `render/run/full` は `--text-renderer resvg-hybrid|browser`（デフォルト `resvg-hybrid`）
- `render/run/full` は `--bubble-renderer resvg|browser`（デフォルト `resvg`）
- `resvg-hybrid` の調整は `--text-letter-spacing`（既定 `-1px`）, `--text-word-spacing`（既定 `0`）, `--resvg-tu-override/--no-resvg-tu-override`
- `reflow/run` は `--reflow-workers` で並列度を指定（デフォルト `4`）

段階実行:

```bash
text-bubble -w out/run1 assign --dialogue "夜見のどこみてるのー？"
text-bubble -w out/run1 reflow
text-bubble -w out/run1 scene  -i imgs/00005716.png
text-bubble -w out/run1 render -o out/result.png
```

`resvg` が未導入で `--text-renderer resvg-hybrid` または `--bubble-renderer resvg` の場合はエラーになる。  
`resvg` なしで進める場合は `--text-renderer browser --bubble-renderer browser` を指定する。

一括実行（段階分割）:

```bash
text-bubble -w out/run1 run \
  -i imgs/00005716.png \
  -o out/result.png \
  --dialogue "夜見のどこみてるのー？"
```

一括実行（`full` 1-shot 推論）:

```bash
text-bubble -w out/run1 full \
  -i imgs/00005716.png \
  -o out/result.png \
  --dialogue "夜見のどこみてるのー？"
```

評価（常に JSON 出力）:

```bash
text-bubble -w out/run1 evaluate \
  --rendered out/result.png \
  --server "$TEXT_BUBBLE_SERVER"
```

補足:

- `evaluate` は `json_schema` 付き1回実行（fallback なし）。
- サーバー実装によっては、複数バブルの評価で `HTTP 500: Failed to parse input at pos 0` が返る場合がある。
- `scene/run` は `--scene-server`, `--scene-model` で scene 段だけ別 routing にできる。
- `scene/run/render` は `--use-worker auto|on|off` でローカル worker 経由の実行を切り替えられる。
- 速度面の最適化 TODO は `docs/ideas/render_perf_todo.md` を参照。

## Scene Placement PoC

`scripts/poc_scene_place_from_masks.py` を起点に、`reflow.json + image + body masks` から `scene.json -> rendered.png` を作る PoC を持っています。  
PoC では `beam`, `cp-sat`, `cp-sat-codex`, `codex-first` を試せますが、2026-03-13 時点では `Codex` 系はまだ experimental で、品質は安定していません。  
本線として見るべきなのは、幾何制約ベースの `cp-sat` と、その runtime/worker 整理です。
`planner-mode=cp-sat` では Codex 用の `codex_board.png` / `editable_scene_template.json` / `prompt_context.json` は生成しません。
`scripts/run_cp_sat_batch.py` は `--jobs N` で batch を並列化でき、`--jobs 1` では subprocess を使わず同一 Python プロセス内で PoC を直接実行します。

## 旧CLI

互換性のため、既存の `bubble_pipeline.py`, `bubble_infer.py`, `bubble_render.py` は残しています。  
新規利用は `text-bubble` を推奨します。

## Reflow Prompt 検証

```bash
uv run python scripts/test_reflow_prompt.py --indent 2
```

## 画像ごとに `.txt` を書く

```bash
python3 scripts/prompt_images.py
```
