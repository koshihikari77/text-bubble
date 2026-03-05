# リファクタリング計画

`bubble_pipeline.py`（1765 行・全責務混在）を整理し、CLI を `03_projects/.agents/CLI_STANDARDS.md` に準拠させる。

CLI_STANDARDS.md の要点:

- CLI フレームワーク: Typer
- `pyproject.toml` の `[project.scripts]` でエントリーポイントを定義
- `uv tool install -e .` で editable インストール
- 全サブコマンドに `--json`（Python 引数名は `json_output`）、`--quiet`、`--version`
- stdout にデータのみ（`--json` 時は成功/失敗とも JSON）、stderr に進捗ログのみ
- 共通引数の命名規則: `-o` / `-i` / `-q` / `-v` 等

## 現状

```
text-bubble/
├── bubble_pipeline.py     # 1765行: モデル+推論+描画+CLI 全部入り
├── bubble_infer.py        # 薄い CLI ラッパー（推論のみ）
├── bubble_render.py       # 薄い CLI ラッパー（描画のみ）
├── prompts/
├── assets/
├── scripts/
└── requirements.txt
```

問題点:

- `bubble_pipeline.py` が 7 つの責務を持つ god module
- CLI が 3 ファイルに分散（`bubble_pipeline.py` 自身 + `bubble_infer.py` + `bubble_render.py`）
- argparse 手書き（CLI_STANDARDS.md は Typer 必須）
- `--json` / `--quiet` / `--version` がない
- 段階ごとにファイルパスを手動指定する必要があり、ミスしやすい

## 整理後の構成

```
text-bubble/
├── pyproject.toml           # エントリーポイント + 依存
├── bubble/
│   ├── __init__.py
│   ├── cli.py               # Typer CLI（唯一のエントリーポイント）
│   ├── models.py            # dataclass + 直列化 + 定数
│   ├── llm.py               # API client + prompt構築 + schema
│   ├── validation.py        # LLM 出力のパース + 検証 + JSON 読み込み
│   ├── infer.py             # 推論オーケストレーション
│   ├── layout.py            # テキスト・吹き出しのレイアウト計算
│   ├── assets.py            # フォント・吹き出し素材・Chromium の解決 + SVG 操作
│   └── render.py            # テキスト描画 + 吹き出し描画 + 合成
├── prompts/                 # 変更なし
├── assets/                  # 変更なし
├── scripts/                 # 変更なし
└── docs/                    # 変更なし
```

削除対象:

- `bubble_pipeline.py` → `bubble/` パッケージに分割
- `bubble_infer.py` → `bubble/cli.py` に統合
- `bubble_render.py` → `bubble/cli.py` に統合
- `requirements.txt` → `pyproject.toml` に移行

## モジュール分割

### `bubble/models.py` (~120 行)

データ構造と直列化。他の全モジュールから参照される基盤。
依存される側なので、他の `bubble/` モジュールを import しない。

```python
# 定数
FONT_CANDIDATES, BUBBLE_FILL_OPACITY, TEXT_COLOR, SVG_NS, ...
PROJECT_ROOT, PROMPTS_DIR

# dataclass
BubblePlan, AssignmentBubblePlan, ReflowBubblePlan, SceneBubblePlan, TextRenderResult

# 直列化（dataclass → dict / JSON 書き出し）
bubble_plan_to_dict, plans_payload
assignment_bubble_plan_to_dict, assignment_plans_payload
reflow_bubble_plan_to_dict, reflow_plans_payload
scene_bubble_plan_to_dict, scene_plans_payload

# 保存（直列化のみ、バリデーション不要）
save_plan_json, save_assignment_plan_json
save_reflow_plan_json, save_scene_plan_json
```

### `bubble/llm.py` (~280 行)

LLM API 通信とプロンプト構築。`urllib` による OpenAI 互換 API 呼び出し。

```python
# プロンプト読み込み
load_prompt_text, load_reflow_examples

# プロンプト構築
build_user_prompt, build_scene_user_prompt, build_reflow_user_prompt

# JSON schema
build_reflow_schema, build_plan_schema, build_scene_plan_schema

# API
post_chat_completion
encode_image_as_data_url, encode_file_as_data_url
```

### `bubble/validation.py` (~280 行)

LLM 出力のパース、整合性の検証、JSON ファイルの読み込みを担当する。
LLM なしでテストできるように `llm.py` とは分離する。
`models.py` のみに依存する（一方向）。

現行の `load_plan_json` は内部で `extract_plan` を呼ぶため、
`load_*_json` を `models.py` に置くと `models → validation → models` の循環依存になる。
これを避けるため、読み込み（パース + 検証）は全てこちらに置く。

```python
# LLM 出力のパース + 検証
extract_plan, extract_scene_plan, extract_reflow_plan
validate_assignment_plans, validate_reflow_plans, _validate_reflow_plans
summarize_raw_output

# JSON ファイル読み込み（パース + 検証を含む）
load_plan_json, load_assignment_plan_json
load_reflow_plan_json, load_scene_plan_json

# plan の合成（reflow + scene → 最終 plan）
compose_bubble_plans
```

依存の流れ:

```
models.py  ← validation.py  ← infer.py  ← cli.py
           ← llm.py ─────────┘            ↑
           ← layout.py ← render.py ───────┘
           ← assets.py ──┘
```

全モジュールが `models.py` に依存し、`models.py` は他を import しない。
`render.py` は `layout.py` と `assets.py` に依存するが、逆方向の依存はない。
`infer.py` と `render.py` は互いに依存しない。

### `bubble/infer.py` (~130 行)

各段の推論を組み立てる公開 API。`cli.py` から呼ばれる。

```python
split_dialogue_lines, text_for_sentence_ids
build_assignment_plans
infer_assignment_plans
infer_reflow_columns_for_bubble, reflow_assignment_plans, infer_reflow_plans
infer_bubble_plans, infer_scene_bubble_plans
```

### `bubble/layout.py` (~100 行)

テキストブロックと吹き出しのレイアウト計算。
純粋な計算のみで I/O や Playwright に依存しない。単体テストしやすい。

```python
build_text_metrics
compute_text_layout
compute_bubble_layout
```

### `bubble/assets.py` (~200 行)

フォント、吹き出し素材、Chromium 実行ファイルの探索と SVG 操作。
ファイルシステムには触るが Playwright には依存しない。

```python
# フォント解決
pick_font_path, browser_font_stack, css_font_literal, build_font_css

# 吹き出し素材解決
resolve_bubble_asset

# Chromium 解決
resolve_chromium_executable

# SVG 操作
warp_svg_source_to_aspect, build_bubble_svg_html
parse_svg_viewbox, svg_qname, load_bubble_svg_source

# PNG 吹き出し変換
bubble_png_to_rgba, flood_fill_outside_open_regions, white_to_transparent
```

### `bubble/render.py` (~300 行)

テキスト描画、吹き出し描画、最終合成。Playwright と Pillow に依存する。
レイアウト計算は `layout.py`、素材解決は `assets.py` に委譲する。

```python
# テキスト描画
render_text_overlay_browser, render_text_overlay
build_render_html, alpha_bbox_or_fail

# 合成
render_bubble, render_bubbles, alpha_composite_clipped
```

## CLI 設計

`CLI_STANDARDS.md` に準拠。Typer でサブコマンドを定義する。

### エントリーポイント

```toml
# pyproject.toml
[project.scripts]
text-bubble = "bubble.cli:app"
```

```bash
uv tool install -e /path/to/text-bubble
```

### サブコマンド一覧

| コマンド | 処理 | LLM | 画像 |
|----------|------|-----|------|
| `text-bubble assign` | セリフ→吹き出し割り当て | 不要 | 不要 |
| `text-bubble reflow` | 列分割 | 要 | 不要 |
| `text-bubble scene` | 位置決め | 要 | 要 |
| `text-bubble render` | 描画 | 不要 | 要 |
| `text-bubble run` | assign→reflow→scene→render を順に実行 | 要 | 要 |
| `text-bubble full` | 1 回の LLM 呼び出しで anchor+columns を一括取得 + 描画 | 要 | 要 |

`run` と `full` の違い:

- `run`: 段階分割フロー。assign→reflow→scene→render を順に実行する。中間 JSON が workspace に残るので、途中から再実行できる。
- `full`: 現行の `bubble_infer.py --stage full` + `bubble_render.py` 相当。1 回の multimodal LLM 呼び出しで anchor_x, anchor_y, columns を一括取得し、そのまま描画まで行う。高速だが、reflow だけやり直すといった柔軟性はない。

`full` の workspace 更新ルール:

- `metadata.json` に `dialogue_lines` と `input_image` を書く
- `plan.json` を直接書く（`assignment.json` / `reflow.json` / `scene.json` は生成しない）
- `-o` で画像を出力する

### workspace 規約

`-w / --workspace` を全サブコマンドの共通引数にする。
workspace ディレクトリ内のファイル名は固定。

```
workspace/
  metadata.json      # 各コマンドが知っている情報を追記していく
  assignment.json
  reflow.json
  scene.json
  plan.json          # scene + reflow の合成結果
```

`metadata.json` は追記方式で、各コマンドが自分の持つ情報を書き足す:

- `assign` は `metadata.json`（`dialogue_lines`）と `assignment.json` を書く
- `scene` は `metadata.json` に `input_image` を追記し、`scene.json` を書く
- `reflow` は `assignment.json` を読み、`reflow.json` を書く
- `render` は `reflow.json` と `scene.json` を読み、`plan.json` を書き、画像を出力する

`assign` は画像を必要としないため、`input_image` は `assign` 時点では書かない。
`--input` を受け取るコマンド（`scene`, `render`, `run`, `full`）が `metadata.json` に追記する。

workspace 内に `metadata.json` があれば `--dialogue` や `--input` を省略できる。
明示的に引数を渡した場合はそちらが優先され、`metadata.json` も上書き更新する。

### 呼び出し例

```bash
# 段階的に実行（ローカル llama-server）
text-bubble assign -w out/run1 --dialogue "夜見のどこみてるのー？"
text-bubble reflow -w out/run1
text-bubble scene  -w out/run1 -i imgs/00005716.png
text-bubble render -w out/run1 -o out/result.png

# 一括実行
text-bubble run -w out/run1 -i imgs/00005716.png -o out/result.png \
  --dialogue "夜見のどこみてるのー？"

# Paperspace のリモートサーバーを使う場合
export TEXT_BUBBLE_SERVER=https://tensorboard-XXXX.clg07azjl.paperspacegradient.com/v1
text-bubble reflow -w out/run1
text-bubble scene  -w out/run1 -i imgs/00005716.png
```

### CLI 引数（CLI_STANDARDS.md 準拠）

共通引数（`@app.callback()`）:

| CLI名 | 短縮 | 型 | 意味 |
|--------|------|----|------|
| `--workspace` | `-w` | `Path` | workspace ディレクトリ |
| `--json` | なし | `bool` | JSON 形式で stdout に出力 |
| `--quiet` | `-q` | `bool` | 進捗ログを抑制 |
| `--version` | `-V` | なし | バージョン表示 |

推論系サブコマンド（`assign`, `reflow`, `scene`, `run`, `full`）:

| CLI名 | 短縮 | 型 | デフォルト | 意味 |
|--------|------|----|-----------|------|
| `--dialogue` | `-d` | `str` | なし | セリフ（改行区切りで複数行） |
| `--server` | `-s` | `str` | 後述 | llama-server の API base URL |
| `--model` | `-m` | `str` | `heretic` | モデル alias |
| `--temperature` | `-t` | `float` | `0.0` | サンプリング温度 |

#### `--server` の解決順序

1. `--server` 引数で明示指定
2. 環境変数 `TEXT_BUBBLE_SERVER`
3. デフォルト `http://127.0.0.1:8080/v1`

Paperspace の外部 URL を使う場合:

```bash
# 環境変数で設定（推奨）
export TEXT_BUBBLE_SERVER=https://tensorboard-XXXX.clg07azjl.paperspacegradient.com/v1

# または引数で直接指定
text-bubble reflow -w out/run1 -s https://tensorboard-XXXX.clg07azjl.paperspacegradient.com/v1
```

`run_server.sh --paperspace-public` で起動した場合、
サーバーは `0.0.0.0:6006` で待ち受け、外部からは
`https://tensorboard-${PAPERSPACE_FQDN}/v1` でアクセスできる。

描画系サブコマンド（`render`, `run`, `full`）:

| CLI名 | 短縮 | 型 | 意味 |
|--------|------|----|------|
| `--input` | `-i` | `Path` | 入力画像 |
| `--output` | `-o` | `Path` | 出力画像 |
| `--font` | なし | `Path` | フォントファイル |
| `--font-family` | なし | `str` | CSS font-family |
| `--bubble-asset` | なし | `Path` | 吹き出し素材 |
| `--font-size` | なし | `int` | フォントサイズ |
| `--text-renderer` | なし | `str` | `browser`（現状 browser のみ） |

### 出力ルール

- **stdout**: `--json` 時は JSON、それ以外は人間向けサマリー
- **stderr**: 進捗ログのみ（`--quiet` で抑制）

`--json` 時の正常出力例:

```json
{
  "stage": "reflow",
  "workspace": "out/run1",
  "output_file": "out/run1/reflow.json",
  "dialogue_lines": ["夜見のどこみてるのー？"],
  "bubbles": [
    {
      "bubble_id": "b1",
      "columns": ["夜見の", "どこみて", "るのー？"]
    }
  ]
}
```

`--json` 時のエラー出力例:

```json
{
  "status": "error",
  "error": "RuntimeError",
  "message": "columns do not reconstruct the assigned dialogue"
}
```

## pyproject.toml

```toml
[project]
name = "text-bubble"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "typer>=0.15",
    "Pillow>=10.4.0",
    "numpy>=1.26.0",
    "playwright>=1.52.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project.scripts]
text-bubble = "bubble.cli:app"
```

pango 関連（`cairocffi`, `pangocffi`, `pangocairocffi`）は削除する。
`render_text_overlay_pango`, `resolve_pango_family`, `register_font_with_fontconfig` も移行しない。
テキスト描画は browser（Playwright/Chromium）一本にする。

## 環境構築

全て `uv` で行う。`pip` / `venv` は使わない。

### インストール

```bash
uv tool install -e .
```

これで `text-bubble` コマンドが使えるようになる。
`uv tool install` は専用の仮想環境を自動で作成し、
`pyproject.toml` の `dependencies` を全てインストールする。

### Playwright ブラウザの導入

`uv tool install -e .` だけでは Chromium がインストールされない。
`uv tool run` で tool 環境内の playwright を実行する:

```bash
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  uv tool run --from text-bubble playwright install chromium
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  uv tool run --from text-bubble playwright install-deps chromium
```

### システム依存（Ubuntu / Paperspace）

```bash
apt-get update
apt-get install -y \
  cmake ninja-build libcurl4-openssl-dev \
  fonts-noto-cjk \
  libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
  libfontconfig1 libharfbuzz0b
```

### セットアップスクリプト

`scripts/setup_local_env.sh` を以下に更新する:

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

# uv の検出
UV_BIN="${UV_BIN:-}"
if [[ -z "${UV_BIN}" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  elif [[ -x "${HOME}/.local/bin/uv" ]]; then
    UV_BIN="${HOME}/.local/bin/uv"
  else
    echo "uv not found. Install uv first or set UV_BIN." >&2
    exit 1
  fi
fi

# パッケージインストール
if [[ "${REINSTALL}" == "1" ]]; then
  "${UV_BIN}" tool install -e . --reinstall
else
  if "${UV_BIN}" tool list | grep -q '^text-bubble '; then
    echo "text-bubble already installed. Use --reinstall to refresh." >&2
  else
    "${UV_BIN}" tool install -e .
  fi
fi

# Playwright Chromium
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  "${UV_BIN}" tool run --from text-bubble playwright install chromium

if [[ "${WITH_DEPS}" == "1" ]]; then
  PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
    "${UV_BIN}" tool run --from text-bubble playwright install-deps chromium
fi

# 作業ディレクトリ
mkdir -p imgs resources out

cat <<'EOF'
Environment is ready.
- text-bubble command is available
- Playwright browsers: .playwright-browsers
- Working directories: imgs resources out
EOF
```

### 開発用（editable + テスト）

テストを実行する場合は `uv run` を使う:

```bash
uv run pytest tests/
uv run python scripts/test_reflow_prompt.py --indent 2
```

`uv run` はプロジェクトの `pyproject.toml` に基づいて仮想環境を自動管理する。

## 移行手順

1. `bubble/` パッケージを作成し、モジュールを分割する
2. `bubble/cli.py` を Typer で実装する（workspace 規約込み）
3. `pyproject.toml` を作成する
4. `scripts/setup_local_env.sh` を更新する
5. `uv tool install -e .` + Playwright 導入で動作確認する
6. テスト計画（後述）を実施する
7. 旧ファイル（`bubble_pipeline.py`, `bubble_infer.py`, `bubble_render.py`, `requirements.txt`）を削除する
8. `README.md` と `docs/bubble_pipeline.md` を更新する

## テスト計画

移行後に以下を確認する。

### 1. モジュール分割の整合性

- 循環 import がないこと: `python -c "from bubble import cli"` が通る
- `validation.py` が `llm.py` に依存しないこと

### 2. CLI 動作

- `text-bubble --help` でサブコマンド一覧が出る
- 各サブコマンドの `--help` が出る
- `text-bubble --version` が出る

### 3. workspace I/O

- `assign → reflow → scene → render` の順で workspace を経由して最終画像が出る
- `metadata.json` が各ステップで正しく追記される
- 2 回目の `scene` で `scene.json` が上書きされる（やり直し）
- `--dialogue` を省略しても `metadata.json` から復元される

### 4. 既存機能の退行確認

- `full` コマンドが現行の `bubble_infer.py --stage full` と同じ結果を返す
- `scripts/test_reflow_prompt.py` の import を更新して通る
- `render` で画像が生成され、既存の出力と目視で同等

### 5. エラー処理

- `--json` 時にエラーが JSON 形式で stdout に出る
- workspace に `assignment.json` がない状態で `reflow` を実行するとエラーが出る

## 拡張の指針

新しい処理を追加する場合のガイドライン。

### 新しいパイプラインステージを追加する場合

例: `evaluate`（描画結果を VLM で評価する段）を追加するとき。

1. `bubble/evaluate.py` を作る（`models.py` と `llm.py` に依存）
2. workspace に `evaluation.json` を書くようにする
3. `cli.py` に `@app.command()` で `evaluate` サブコマンドを追加する
4. 既存モジュールは変更不要

### 新しい描画バックエンドを追加する場合

例: Canvas API ベースの描画を追加するとき。

1. `bubble/render.py` に `render_text_overlay_canvas()` を追加する
2. `render_text_overlay()` のディスパッチに分岐を足す
3. `layout.py` と `assets.py` は変更不要（レイアウト計算は共通）

### 新しいアセット形式を追加する場合

例: Lottie アニメーション素材を追加するとき。

1. `bubble/assets.py` に読み込み関数を追加する
2. `resolve_bubble_asset()` の探索リストに拡張子を追加する
3. `render.py` に対応する描画パスを追加する

### やってはいけないこと

- `models.py` から他の `bubble/` モジュールを import しない（循環依存の起点になる）
- `layout.py` に I/O や Playwright 依存を入れない（純粋な計算に保つ）
- `infer.py` と `render.py` を互いに依存させない（推論と描画は独立）

## 互換性への配慮

- `prompts/`, `assets/`, `scripts/` は移動しない
- `PROJECT_ROOT` の解決は `bubble/models.py` で行い、パッケージルートの親を指す
- `scripts/setup_local_env.sh` は `uv tool install -e .` + Playwright 導入を含む形で更新する
