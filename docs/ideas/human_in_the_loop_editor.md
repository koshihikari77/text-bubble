# Human In The Loop Bubble Editor

## 目的

`reflow` / `scene` の生成結果と最終 `render` の間に、人間が確認・修正できる編集ステップを追加する。

目指す体験は JSON 手編集ではなく、小さな GUI エディタに近いもの。

- 元画像を表示する
- 生成された吹き出しと縦書きテキスト候補を画像上に重ねる
- 吹き出し位置をドラッグで調整する
- 吹き出し種類を変更する
- 改行、つまり `columns` を変更する
- 必要なら文字列も修正する
- 修正後に再レンダリングする

## 現状パイプラインとの相性

現状の CLI には、すでに自然な差し込み点がある。

`text-bubble render` は次を読む。

- `reflow.json`
- `scene.json`
- `metadata.json` または `--input` で指定された元画像

その後、これらを `plan.json` に合成して最終画像を描画する。

つまり最初の GUI は、既存 workspace ファイルを編集するだけで成立する。

- 位置編集: `scene.json` の `anchor_x` / `anchor_y` を更新する
- 吹き出し種類編集: `reflow.json` の `bubble_type` を更新する
- 改行編集: `reflow.json` の `columns` を更新する
- 最終描画: 既存の `text-bubble render` をそのまま使う

この形なら、最初から renderer を大きく変えずに始められる。

## 重要な制約

文字列そのものの編集は、位置調整や改行調整より難しい。

現状の validation は、`dialogue_lines`、`sentence_ids`、`columns` が同じ本文を復元することを要求する。GUI で本文を書き換えるなら、少なくとも次のファイルの整合性を同時に保つ必要がある。

- `metadata.json`
- `assignment.json`
- `reflow.json`
- `scene.json`

最初の版では、文字編集は次のどちらかに寄せるのが安全。

- `columns` の結合結果が元テキストと同じになる範囲で、改行だけを編集する
- 「本文を書き換える」専用操作を用意し、関係する JSON をまとめて更新する

## 推奨 MVP

既存 workspace 形式の上に、ローカル Web GUI を作る。

最初の範囲:

1. workspace ディレクトリを開く
2. `metadata.json`、`reflow.json`、`scene.json` を読む
3. 元画像を表示する
4. 各 `anchor_x` / `anchor_y` の位置に編集可能なハンドルを表示する
5. canvas またはサイドリストから bubble を選択する
6. 次を編集する
   - `anchor_x`
   - `anchor_y`
   - `bubble_type`
   - `columns`
7. `columns` が担当テキストを復元できるか検証する
8. `reflow.json` と `scene.json` に保存する
9. 既存 renderer を呼び、プレビューを更新する

最初の版ではやらないこと:

- bubble 輪郭の自由なベクター編集
- 手描き風 tail の直接編集
- bubble ごとの font size
- sentence assignment をまたぐ自由な本文編集
- renderer の置き換え

## 実装案

### Backend

既存の `bubble` モジュールを import する小さな Python サービスを置く。

責務:

- workspace JSON を読む
- 既存 validation 関数で編集内容を検証する
- 更新後の `reflow.json` / `scene.json` を保存する
- 既存 render 経路を呼ぶ
- プレビュー画像を返す

候補:

- FastAPI
- Flask
- ローカル Web アプリを起動する Typer command

frontend を別に作るなら FastAPI が自然。

### Frontend

raw DOM ではなく canvas library を使う。

候補:

- React + Konva
- Fabric.js

Konva は、選択可能な canvas object、ドラッグ、transform、画像 layer、独自 shape を扱いやすいので、この用途に合う。Fabric.js も object model と serialization があり十分候補になる。

最初の canvas 表示は、最終 render と完全一致しなくてもよい。

表示できればよいもの:

- 元画像
- draggable anchor marker
- おおよその bubble rectangle / preview
- 選択中 bubble の outline

正確な見た目は、保存後または手動更新で既存 renderer に描かせる。

## データモデル方針

短期的には現状の分割で足りる。

- `reflow.json`: テキストの列分割と bubble type
- `scene.json`: 位置

ただし GUI を前提にすると、`reflow.json` / `scene.json` を直接の正規データにするのは弱い。

理由:

- GUI は bubble 単位で本文、改行、種類、位置、手動修正状態をまとめて扱いたい
- `reflow.json` は本来「列分割」なのに、今は `bubble_type` も持っていて責務が混ざっている
- `scene.json` と `reflow.json` を `sentence_ids` で突き合わせる必要があり、GUI 側の状態管理が面倒
- 画像ごとに workspace が散ると、複数画像レビューの一覧や進捗管理がしにくい

そのため、GUI 用には別の正規データを置き、既存 stage JSON は互換用・生成物として扱うのがよい。

## 推奨ファイル構成

複数画像を扱う単位を project とし、画像ごとに case を切る。

```text
out/project1/
  project.json
  cases/
    img001/
      document.json
      generated/
        assignment.json
        reflow.json
        scene.json
        plan.json
      renders/
        latest.png
    img002/
      document.json
      generated/
        assignment.json
        reflow.json
        scene.json
        plan.json
      renders/
        latest.png
```

`project.json` は一覧と進捗管理だけにする。

```json
{
  "version": 1,
  "cases": [
    {
      "case_id": "img001",
      "image": "imgs/img001.png",
      "document": "cases/img001/document.json",
      "status": "needs_review",
      "rendered": "cases/img001/renders/latest.png"
    }
  ]
}
```

GUI が編集する正規データは `document.json` にまとめる。

```json
{
  "version": 1,
  "case_id": "img001",
  "image": "imgs/img001.png",
  "dialogue_lines": [
    "夜見のどこみてるのー？"
  ],
  "bubbles": [
    {
      "bubble_id": "b1",
      "sentence_ids": [1],
      "text": "夜見のどこみてるのー？",
      "columns": [
        "夜見のどこみてるのー？"
      ],
      "bubble_type": "ellipse",
      "speaker_id": "__scene_1",
      "placement": {
        "anchor_x": 0.35,
        "anchor_y": 0.35
      },
      "manual": {
        "text": false,
        "columns": false,
        "bubble_type": false,
        "placement": true
      }
    }
  ],
  "render": {
    "font_size": 0,
    "text_renderer": "resvg-hybrid",
    "bubble_renderer": "resvg",
    "text_letter_spacing": "-1px",
    "text_word_spacing": "0"
  }
}
```

この形のメリット:

- GUI は基本的に `document.json` だけ読めば編集画面を作れる
- bubble ごとに必要な情報がまとまる
- 手動修正済みかどうかを bubble 単位・項目単位で持てる
- `generated/` に元の LLM / solver 出力を残せる
- 既存 CLI とは export / import adapter でつなげられる

## `reflow` の扱い

`reflow` は本来、画像ではなくテキストと描画条件に依存する。

そのため、同じ dialogue を複数画像に当てる場合は `columns` を共通化できる可能性がある。一方で `bubble_type` は画像の雰囲気や構図と関係するので、`reflow` に入れるより bubble の style 属性として `document.json` に置く方が自然。

整理すると次の扱いがよい。

- `columns`: text layout / reflow の結果
- `bubble_type`: bubble style
- `anchor_x` / `anchor_y`: placement
- `speaker_id`: scene / merge 制御
- `manual`: GUI 編集状態

既存互換のため、当面は `document.json` から `reflow.json` と `scene.json` を export する。

export 先:

- `generated/reflow.json`
  - `bubble_id`
  - `sentence_ids`
  - `columns`
  - 互換用に `bubble_type` も出す
- `generated/scene.json`
  - `bubble_id`
  - `sentence_ids`
  - `anchor_x`
  - `anchor_y`
  - `speaker_id`
  - 互換用に `bubble_type` も出せる

将来的には renderer が `document.json` を直接読めるようにして、stage JSON は debug / 互換出力に下げる。

## 将来の拡張項目

将来ほしくなりそうな項目:

- bubble ごとの `font_size`
- bubble ごとの `scale`
- locked / hidden flag
- tail direction や tail control points
- manual override marker
- editor 専用メモ
- 元候補との diff
- regenerate 対象から除外する lock
- mask overlay の表示設定

## 既存ソフト調査

この pipeline にそのまま差し込める既存ソフトは見当たらない。

### Label Studio / CVAT

画像アノテーション系の workflow には近い。

- 矩形
- label
- 属性
- text field
- human review

ただし、漫画風の縦書き吹き出し、procedural bubble shape、この repo の `reflow.json` / `scene.json` 形式を自然には扱えない。annotation UI として使い、converter を書くことはできるが、専用 editor を作る方がたぶん素直。

### Clip Studio Paint / Krita / Inkscape

手作業の漫画・画像編集ツールとしては強いが、この pipeline の JSON と round-trip する用途には向かない。

編集体験の参考にはなる。

- 選択 / 移動
- テキスト編集
- 吹き出し形状選択
- layer 的な workflow

ただし、自動生成 workspace を編集する土台としては重い。

### Gradio ImageEditor

簡単な PoC UI には使えるが、bubble object 単位で選択・属性編集・JSON 保存する GUI には弱い。

### Konva / Fabric.js / tldraw

完成アプリというより、専用 GUI を作るための土台。

- Konva: React ベースの専用 canvas editor に向く
- Fabric.js: object model と serialization が強く、こちらも候補
- tldraw: whiteboard SDK として完成度は高いが、この用途にはやや大きい可能性がある

## 推奨の進め方

専用のローカル Web editor から始める。

段階:

1. Workspace inspector
   - workspace を開く
   - 元画像を表示する
   - bubble 一覧を表示する
   - anchor を表示する

2. 最小編集
   - anchor をドラッグする
   - `bubble_type` を変える
   - `columns` を編集する
   - JSON に保存する

3. Render preview
   - 既存 render 経路を呼ぶ
   - 出力画像を更新する

4. 安全な本文編集
   - 本文書き換え専用操作を作る
   - 関連 JSON をまとめて更新する

5. 高度な編集
   - bubble ごとの size
   - lock / selected bubble regeneration
   - mask overlay
   - before / after 表示

## 参考

- Konva docs: https://konvajs.org/docs/index.html
- Fabric.js docs: https://fabricjs.com/docs/
- tldraw SDK: https://tldraw.dev/
- Label Studio tags: https://labelstud.io/tags/
- CVAT docs: https://docs.cvat.ai/
- Gradio ImageEditor: https://www.gradio.app/docs/gradio/imageeditor
- Clip Studio Paint balloons: https://help.clip-studio.com/en-us/manual_en/540_comic/Balloons.htm
- Krita features: https://krita.org/en/features/
