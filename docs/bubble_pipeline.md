# Bubble Pipeline

現状の吹き出し生成まわりの整理です。対象は [`bubble_pipeline.py`](/storage/projects/text-bubble/bubble_pipeline.py) と、その前後に分けた CLI です。

## 構成

- 推論と描画をまとめて行う: [`bubble_pipeline.py`](/storage/projects/text-bubble/bubble_pipeline.py)
- 推論だけ行う: [`bubble_infer.py`](/storage/projects/text-bubble/bubble_infer.py)
- 描画だけ行う: [`bubble_render.py`](/storage/projects/text-bubble/bubble_render.py)

普段の責務分担はこうです。

1. `Heretic` に画像とセリフ行を渡して `plan JSON` を作る
2. `plan JSON` と元画像から吹き出しを描画する

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

## モデルにやらせること

`Heretic` にやらせているのは次だけです。

- 吹き出しごとの `anchor_x`, `anchor_y`
- 文のまとめ方
- その文をどう `columns` に分けるか

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

## 実行例

推論:

```bash
./.venv/bin/python bubble_infer.py \
  --input /notebooks/imgs/00005716.png \
  --plan-json /notebooks/imgs/00005716_separate_plan.json \
  --dialogue "夜見のどこみてるのー？"
```

描画:

```bash
./.venv/bin/python bubble_render.py \
  --input /notebooks/imgs/00005716.png \
  --plan-json /notebooks/imgs/00005716_separate_plan.json \
  --output /notebooks/imgs/00005716_bubbled.png \
  --font /notebooks/resources/JKG-L_3.ttf \
  --bubble-asset /storage/projects/text-bubble/assets/bubble_ellipse.svg \
  --text-renderer browser
```
