# bubble_pipeline.py

[`bubble_pipeline.py`](/storage/projects/text-bubble/bubble_pipeline.py) の現状仕様の整理です。

## 役割

このスクリプトは次の 2 段をつなぐ。

1. `Heretic` に画像を見せて、吹き出し用の縦書きテキストブロック計画を JSON で返させる
2. 返ってきた計画を使って、吹き出し素材と縦書き文字を画像に合成する

## モデルにやらせていること

モデル出力は 1 個の吹き出しだけ。

返す JSON:

```json
{
  "anchor_x": 0.9,
  "anchor_y": 0.1,
  "columns": ["夜見の", "どこみてる", "のー？"]
}
```

意味:

- `anchor_x`
  画像全体に対する正規化座標。縦書きテキストブロックの右上 x。
- `anchor_y`
  画像全体に対する正規化座標。縦書きテキストブロックの右上 y。
- `columns`
  右から左に並ぶ縦書き列。

前提:

- セリフ本文は `--dialogue` でコード側から与える
- モデルはセリフを改変してはいけない
- `"".join(columns)` が `--dialogue` と完全一致しない場合は失敗

## モデルにやらせていないこと

モデルには次はやらせていない。

- 吹き出しのしっぽ
- 吹き出し outline の形状生成
- pixel 単位の矩形生成
- 文字の最終描画座標
- フォント選択

これらはすべてコード側が処理する。

## 入力

主要引数:

- `--input`
- `--output`
- `--plan-json`
- `--server`
- `--model`
- `--dialogue`
- `--font`
- `--font-family`
- `--bubble-asset`
- `--font-size`
- `--temperature`

例:

```bash
./.venv/bin/python bubble_pipeline.py \
  --input /notebooks/imgs/00005716.png \
  --output /notebooks/imgs/00005716_bubbled.png \
  --plan-json /notebooks/imgs/00005716_plan.json \
  --dialogue "夜見のどこみてるのー？" \
  --font /notebooks/resources/JKG-L_3.ttf \
  --bubble-asset /notebooks/resources/bubble.svg
```

## 生成フロー

流れはこの順。

1. 入力画像を data URL 化
2. `llama-server` の `/chat/completions` に送信
3. `json_schema` で `anchor_x`, `anchor_y`, `columns` を要求
4. 返却 JSON を検証
5. テキストブロックの寸法を計算
6. 吹き出し素材をレンダリング
7. 縦書き文字をレンダリング
8. 元画像に合成して保存

## レイアウト計算

縦書き文字組はコード側で計算する。

主な考え方:

- 列順は `columns[0]` が最右列
- 列頭は同じ `anchor_y` で揃える
- 文字は固定セルベースで積む
- 吹き出しは文字ブロックを包むように作る

現状の主要パラメータ:

- `char_step = round(em * 1.25)`
- `column_width = round(em * 1.0)`
- `column_gap = round(em * 0.28)`
- `pad_left = round(em * 1.0)`
- `pad_right = round(em * 1.0)`
- `pad_top = round(em * 0.9)`
- `pad_bottom = round(em * 1.1)`

さらに吹き出しは縦長になるよう、最低でも `height >= width * 1.6` を満たすように補正する。

## 描画方式

描画は `Pillow` 単体ではなく `SVG + Playwright/Chromium` を使う。

理由:

- 日本語縦書きの文字描画をブラウザに寄せたかった
- 吹き出し素材として `bubble.svg` をそのまま使いたかった
- 透明背景合成が必要だった

描画対象は 2 レイヤー:

1. 吹き出し素材
2. 縦書きテキスト

その後に元画像へ `alpha_composite` で重ねる。

## 吹き出し素材の対応

現状対応しているのは次の 3 種類。

- PNG
- SVG
- `.txt` に保存された SVG 文字列

探索順:

- 明示された `--bubble-asset`
- `/notebooks/imgs/bubble.svg`
- `/notebooks/imgs/bubble.png`
- `/notebooks/resources/bubble.svg`
- `/notebooks/resources/bubble.png`
- `/notebooks/resources/bubble_svg.txt`

## フォント

フォントは `@font-face` で data URL 埋め込みにしてブラウザへ渡す。

現状の主用途:

- [JKG-L_3.ttf](/notebooks/resources/JKG-L_3.ttf)

フォント未指定時はシステム側の候補を順に探す。

## Playwright 周りの注意

重要な実装メモ:

- `headless_shell` では HTML テキストが消えた
- そのため通常の Chromium 実行ファイルを優先している
- Chromium 実行には `libcairo2`, `libpango-*`, `libfontconfig1`, `libharfbuzz0b` が必要だった

## 現状の制約

いまの `bubble_pipeline.py` はまだ最小版で、制約も多い。

- 吹き出しは 1 個だけ
- セリフ本文は固定で外から与える
- モデルは自己評価ループを回していない
- 文字詰め、約物、縦中横などの日本語組版は未調整
- 吹き出し素材の形に合わせた padding 最適化も未調整

## 直近の次候補

- 吹き出し素材の形に合わせて文字ブロック余白を再調整
- `ー`, `？`, `！`, `…` などの縦書き見た目を改善
- 複数候補を生成して `Heretic` に評価させるループを追加
