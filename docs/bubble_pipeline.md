# Bubble Pipeline

現状の吹き出し生成まわりの整理です。対象は [`bubble_pipeline.py`](/storage/projects/text-bubble/bubble_pipeline.py) と、その前後に分けた CLI です。

## 構成

- 推論と描画をまとめて行う: [`bubble_pipeline.py`](/storage/projects/text-bubble/bubble_pipeline.py)
- 推論だけ行う: [`bubble_infer.py`](/storage/projects/text-bubble/bubble_infer.py)
- 描画だけ行う: [`bubble_render.py`](/storage/projects/text-bubble/bubble_render.py)

普段の責務分担はこうです。

1. `Heretic` に画像とセリフ行を渡して `plan JSON` を作る
2. `plan JSON` と元画像から吹き出しを描画する

2026年3月4日時点では、推論を一気にやる形から、段階的に分けて検証できる形へ寄せています。特に `reflow` は画像付き推論から切り離し、テキストだけで列分割を評価できるようにしています。

## 段階分割

現在は次の 4 段を扱えます。

1. `assignment`
   入力文をどの吹き出しへ入れるかを決める。現状は決定論で `1 行 = 1 bubble`。
2. `reflow`
   各吹き出しの本文を、右から左の縦書き列 `columns` に分ける。ここは `1 bubble = 1 request` で LLM を呼ぶ。
3. `scene`
   画像を見て `anchor_x`, `anchor_y` と `sentence_ids` を返す旧来寄りの段。
4. `full`
   画像を見て `anchor_x`, `anchor_y`, `sentence_ids`, `columns` を一度に返す従来フロー。

今の主な関心は `assignment -> reflow` までを別段で安定化することです。`reflow` の品質を prompt と few-shot だけで詰められるようにしてあります。

## Plan JSON

現在の plan は複数吹き出し対応です。

```json
{
  "dialogue_lines": [
    "夜見のどこみてるのー？"
  ],
  "bubbles": [
    {
      "anchor_x": 0.9,
      "anchor_y": 0.1,
      "sentence_ids": [1],
      "columns": ["夜見の", "どこみて", "るのー？"]
    }
  ]
}
```

意味:

- `anchor_x`, `anchor_y`
  画像全体に対する正規化座標。縦書きテキストブロックの右上。
- `sentence_ids`
  どの入力文をその吹き出しに入れるか。1-based。
- `columns`
  右から左に並ぶ縦書き列。

検証ルール:

- `sentence_ids` は元の文順を保つ
- すべての文はちょうど一度だけ使う
- `columns` を連結した文字列は、その吹き出しに割り当てた文と完全一致する

段ごとの JSON は次のように分かれます。

`assignment`:

```json
{
  "dialogue_lines": ["今日はもう帰ろうかな..."],
  "bubbles": [
    {
      "bubble_id": "b1",
      "sentence_ids": [1]
    }
  ]
}
```

`reflow`:

```json
{
  "dialogue_lines": ["今日はもう帰ろうかな..."],
  "bubbles": [
    {
      "bubble_id": "b1",
      "sentence_ids": [1],
      "columns": ["今日はもう", "帰ろうかな..."]
    }
  ]
}
```

## モデルにやらせること

`full` では `Heretic` に次をやらせています。

- 吹き出しごとの `anchor_x`, `anchor_y`
- 文のまとめ方
- その文をどう `columns` に分けるか

一方、現在の段階分割フローでは次のように責務を分けています。

- `assignment`
  ルールベース
- `reflow`
  text-only LLM
- `placement`
  まだ未分離。現時点では `scene` または `full` に含まれる

モデルにやらせていないもの:

- 吹き出しの形
- 吹き出しの最終サイズ
- フォント選択
- 文字の最終描画座標
- しっぽ

## 文字描画

描画 backend は 2 系統あります。

- `browser`
- `pango`

現状の本命は `browser` です。`Playwright + Chromium` で縦書き文字を描画し、その alpha bbox を取得します。

重要なのは、最終的な文字の見た目位置は renderer が決めていて、bubble 計算にはその `alpha bbox` を使うことです。固定の仮想 bbox だけで bubble を決めないようにしています。

## 吹き出し描画

吹き出し素材は次の形式を扱えます。

- PNG
- SVG
- SVG コードを保存した `.txt`

探索順:

- 明示された `--bubble-asset`
- [`assets/bubble_ellipse.svg`](/storage/projects/text-bubble/assets/bubble_ellipse.svg)
- `resources/bubble.svg`
- `resources/bubble.png`
- `resources/bubble_svg.txt`
- `imgs/bubble.svg`
- `imgs/bubble.png`
- `/notebooks/imgs/bubble.svg`
- `/notebooks/imgs/bubble.png`
- `/notebooks/resources/bubble.svg`
- `/notebooks/resources/bubble.png`
- `/notebooks/resources/bubble_svg.txt`

現在の考え方はこうです。

1. 実際に描いた文字の `alpha bbox` を取る
2. 必要ならその矩形に padding を足す
3. その矩形の aspect ratio に合わせて素材 SVG を warp する
4. warp 後の SVG を中心合わせで合成する

つまり、bubble の shape は素材 SVG を毎回比率変換して作っています。別の shape を毎回生成しているわけではありません。

## 現在のレイアウト計算

文字組の基本値は `font_size` 由来です。

- `char_step = em * 1.08` 相当
- `column_width = em * 1.0` 相当
- `column_gap = em * 0.1` 相当

bubble 側は、実際の文字 bbox の周囲に余白を足して target 矩形を決めます。現在の余白係数は [`bubble_pipeline.py`](/storage/projects/text-bubble/bubble_pipeline.py) を参照してください。

## SVG 比率変換

SVG 素材を target ratio に合わせる処理は [`warp_svg_source_to_aspect()`](/storage/projects/text-bubble/bubble_pipeline.py) にあります。

この関数は、

- 元 SVG の `viewBox` を読む
- target aspect に応じて `scale(x, y)` を決める
- 中心を保ったまま `<g transform="...">` を追加する

という形で動きます。

この処理のおかげで、素材 SVG を 1 個持っておけば、縦長にも正方形寄りにもその場で変形できます。

## 実験スクリプト

形状の切り分け用に実験スクリプトもあります。

- SVG そのものを別比率に warp する:
  [`warp_bubble_svg.py`](/storage/projects/text-bubble/scripts/warp_bubble_svg.py)
- 既存 plan で正方形 bubble を試す:
  [`render_square_bubble_experiment.py`](/storage/projects/text-bubble/scripts/render_square_bubble_experiment.py)

これは本線の描画ロジックとは別で、素材や比率の切り分け用です。

## Reflow Prompt

`reflow` 用 prompt はコード内に直書きせず、[`prompts/`](/storage/projects/text-bubble/prompts) 配下へ外出ししています。

- system prompt:
  [`reflow_system.txt`](/storage/projects/text-bubble/prompts/reflow_system.txt)
- user prompt template:
  [`reflow_user.txt`](/storage/projects/text-bubble/prompts/reflow_user.txt)
- few-shot:
  [`reflow_examples.json`](/storage/projects/text-bubble/prompts/reflow_examples.json)
- テストケース:
  [`reflow_test_cases.json`](/storage/projects/text-bubble/prompts/reflow_test_cases.json)

運用上のルール:

- few-shot の例とテストケースは同じ文を使わない
- `reflow` は `1 bubble = 1 request`
- `thinking` を有効にしている
- ただし出力は必ず JSON schema で縛る

現在の prompt 方針:

- 列長の均等化より、文のまとまりの自然さを優先する
- できるだけ切らず、必要なときだけ最小限で切る
- 引用、括弧、語尾、連続記号を自然単位として扱う
- `columns` を連結すると元文と完全一致することを必須にする

## Reflow 検証

`reflow` は専用スクリプトで回せます。

```bash
python3 scripts/test_reflow_prompt.py --indent 2
```

このスクリプトは次を行います。

1. テスト文から `assignment` を作る
2. 各 bubble ごとに `reflow` を 1 回呼ぶ
3. `bubble_id`, `columns`, 再構成文字列、経過時間を JSON で出す

これで、画像や placement を触らずに `reflow` prompt だけを繰り返し調整できます。

2026年3月4日時点の観察:

- 短文は 1 列に保つ方向へ改善した
- `じゃん`, `の...？`, `でしょ` のような文末は few-shot 次第で挙動がかなり変わる
- 括弧や引用のまとまりは比較的安定している
- `って` や読点の直後でどこまでまとめるかはまだ揺れる

実装上の注意:

- `reflow` は `1 bubble = 1 request` で回すので、単体 bubble の検証ではその bubble 自身の整合だけを見る
- 複数 bubble 全体の被覆チェックは、`reflow` 結果をまとめた最終検証で行う
- `scene` / `full` は文数が増えると JSON 出力が長くなるので、`n_predict` は文数に応じて増やしている
- `scene` の anchor が少し外れても、文字 block 自体が画像より大きくない限り、描画時に画像内へ収まる範囲まで最小限クランプする

## 実行例

推論:

```bash
./.venv/bin/python bubble_infer.py \
  --input imgs/00005716.png \
  --plan-json out/00005716_separate_plan.json \
  --dialogue "夜見のどこみてるのー？"
```

段ごとの実行例:

```bash
./.venv/bin/python bubble_infer.py \
  --stage assignment \
  --plan-json out/assignment.json \
  --dialogue "今日はもう帰ろうかな..." \
  --dialogue "「ちょっと待って」と言った"
```

```bash
./.venv/bin/python bubble_infer.py \
  --stage reflow \
  --assignment-json out/assignment.json \
  --plan-json out/reflow.json \
  --dialogue "今日はもう帰ろうかな..." \
  --dialogue "「ちょっと待って」と言った"
```

描画:

```bash
./.venv/bin/python bubble_render.py \
  --input imgs/00005716.png \
  --plan-json out/00005716_separate_plan.json \
  --output out/00005716_bubbled.png \
  --font assets/JKG-L_3.ttf \
  --bubble-asset assets/bubble_ellipse.svg \
  --text-renderer browser
```

`reflow` 済みから続ける場合は、`scene` を取ってから `bubble_render.py` に両方渡せます。

```bash
./.venv/bin/python bubble_infer.py \
  --stage scene \
  --input imgs/00005716.png \
  --plan-json out/scene.json \
  --dialogue "今日はもう帰ろうかな..."
```

```bash
./.venv/bin/python bubble_render.py \
  --input imgs/00005716.png \
  --scene-json out/scene.json \
  --reflow-json out/reflow.json \
  --save-plan-json out/final_plan.json \
  --output out/00005716_bubbled.png \
  --font assets/JKG-L_3.ttf \
  --bubble-asset assets/bubble_ellipse.svg \
  --text-renderer browser
```

Paperspace で外部公開したい場合は、`llama-server` を次で起動できます。

```bash
./scripts/run_server.sh --paperspace-public
```

このモードでは `0.0.0.0:6006` で待ち受け、`PAPERSPACE_FQDN` があれば
`https://tensorboard-${PAPERSPACE_FQDN}` と API base の
`https://tensorboard-${PAPERSPACE_FQDN}/v1` を表示します。

## Diagnose 用の例

[`docs/dialogue_examples.txt`](/storage/projects/text-bubble/docs/dialogue_examples.txt) に 1 文から 5 文までの投入例を置いています。

2026年3月4日時点では、`../imgs/00005716.png` に対してこの 1-5 文の例を `assignment -> reflow -> scene -> render` で通せることを確認しました。
