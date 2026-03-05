# evaluate ツール設計案

レンダリング済み画像を VLM で評価し、修正指示を構造化 JSON で出力するツール。

## 設計方針

### 「直す」vs「修正指示を出す」

| アプローチ | 仕組み | 判定 |
|-----------|--------|------|
| 直す (VLM が修正済みパラメータ出力) | 評価と修正が混ざり失敗原因が追えない。VLM の数値修正精度は低い | ✗ |
| 修正指示 (構造化 JSON) | 関心分離。人が途中で見れる。デバッグしやすい | ✓ |
| 判定のみ (pass/fail + 理由) | シンプルだが自動化しにくい | △ |

**採用: 修正指示（構造化 JSON）+ 判定スコア のハイブリッド。**

数値の直接修正は VLM に任せず、該当ステージの再推論（scene / reflow 再実行）に任せる。

### 出力スキーマ

```json
{
  "verdict": "needs_fix",
  "score": 0.6,
  "issues": [
    {
      "bubble_id": "B1",
      "type": "position",
      "severity": "high",
      "description": "吹き出しがキャラの顔を覆っている",
      "fix_stage": "scene",
      "suggestion": "上方向に移動"
    },
    {
      "bubble_id": "B2",
      "type": "reflow",
      "severity": "medium",
      "description": "3列は多い、2列が自然に読める",
      "fix_stage": "reflow",
      "suggestion": "列数を2に減らす"
    }
  ]
}
```

フィールド:

- `verdict`: `"pass"` | `"needs_fix"` — 自動ループの終了条件に使う
- `score`: 0.0–1.0 — 品質スコア（閾値ベースの判定にも使える）
- `issues[].type`: `"position"` | `"reflow"` | `"overlap"` | `"readability"` | `"size"`
- `issues[].severity`: `"high"` | `"medium"` | `"low"`
- `issues[].fix_stage`: `"scene"` | `"reflow"` | `"assignment"` | `"render"` — どのステージを再実行すべきか
- `issues[].suggestion`: 自然言語の修正方向性（パラメータ値ではなく方針）

## VLM への入力

以下の4つを渡すことで「意図された配置」と「実際の見た目」を比較判断できる:

1. **元画像** — metadata.json の `input_image`
2. **レンダリング後画像** — `--rendered` で指定
3. **現在の plan.json** — 吹き出し配置情報
4. **dialogue_lines** — 元のセリフ

## 修正の適用方法

evaluate の出力にある `fix_stage` に基づき、該当ステージを再実行する。
再実行時に **feedback プロンプト注入** で修正方向を伝える:

```
前回のレンダリング結果に以下の問題がありました：
- B1: 吹き出しがキャラの顔を覆っている。上方向に移動してください。

以下の制約を踏まえて再配置してください。
```

実装上は `infer_scene_bubble_plans` / `infer_reflow_plans` に `feedback: str | None` 引数を追加するだけ。

| issue の fix_stage | やること |
|-------------------|---------|
| `scene`           | scene を再実行（suggestion をプロンプトに追加） |
| `reflow`          | reflow を再実行（同上） |
| `assignment`      | assignment から再実行（稀） |
| `render`          | font_size / bubble_asset 等のパラメータ変更して render だけ再実行 |

## ループ設計

```
render → evaluate → 問題あり？
                      ├─ No  → 完了
                      └─ Yes → fix_stage を特定
                                → 該当ステージ再実行（feedback 付き）
                                → render → evaluate → ...
```

### ループの主体

| 方式 | 説明 | タイミング |
|------|------|-----------|
| coding agent がループ | agent が evaluate の JSON を読み判断して再実行 | **今（Stage 3）** |
| CLI に `--max-retries` 内蔵 | `run --max-retries 3` で自動ループ | まだ早い |
| Python API でループ | `while score < 0.8: ...` を自分で書く | Stage 4 向け |

**現段階: evaluate を独立 CLI コマンドとして作り、ループは coding agent / 人に任せる。**

理由:
- 「この程度のズレは許容する」「B2 は直すが B1 は無視」等の判断は人が入る方が品質が高い
- CLI コマンドとして独立していれば後から自動ループに組み込める
- coding agent なら JSON 出力を読んで自分で判断できる

## CLI インターフェース

```bash
text-bubble -w out/workspace evaluate \
  --rendered out/workspace/output.png \
  --server "$TEXT_BUBBLE_SERVER"
```

workspace 内の metadata.json / plan.json を自動参照。
出力は stdout に JSON（`--json` フラグ不要、evaluate は常に JSON）。

## 実装構成

```
bubble/
  evaluate.py          # 評価ロジック（プロンプト構築、VLM 呼び出し、出力パース）
  cli.py               # evaluate コマンド追加

prompts/
  evaluate_prompt.md   # 評価用システムプロンプト
```

## 将来の拡張

- evaluate の JSON は機械可読なので、後から `run --auto-fix --max-retries 3` のような自動ループを追加可能
- score の閾値や issue の severity による自動判定ロジックも追加しやすい
- 複数の評価観点（配置、可読性、スタイル）を別々の評価プロンプトに分離することも可能

## 現状メモ（2026-03-05）

- `text-bubble evaluate` は実装済み（`bubble/evaluate.py` + `bubble/cli.py`）。
- `evaluate` の出力は常に JSON（`--json` なし）。
- fallback（schema なし再試行）は入れていない。`json_schema` 付き1回実行のみ。
- 一部サーバー構成では、複数バブル評価時に `HTTP 500: Failed to parse input at pos 0` が発生することがある（サーバー側挙動）。
- 体感上のボトルネックは `render`。複数バブルで Playwright/Chromium の起動コストが積み上がるため、5文ケースで数分かかることがある。
