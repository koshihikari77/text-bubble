# HITL Bubble Editor 実装計画

## Summary

`cp-sat` で初期配置を生成し、その結果を `document.json` に取り込んで GUI で手動修正する。GUI は `document.json` を正規データとして編集し、render 時に既存互換の `generated/reflow.json` / `generated/scene.json` を出力して既存 renderer を使う。

## Key Changes

### 初期生成フロー

- 既存の `assign -> reflow -> scene --planner cp-sat` を実行する。
- `scene.json` の `anchor_x` / `anchor_y` を `document.json.bubbles[].placement` に取り込む。
- `reflow.json` の `columns` と `bubble_type` を `document.json.bubbles[]` に取り込む。
- GUI はこの cp-sat 初期配置を starting point として表示する。

### Editor Project 構成

```text
out/project1/
  project.json
  cases/
    img001/
      document.json
      generated/
        metadata.json
        assignment.json
        reflow.json
        scene.json
        plan.json
      renders/
        latest.png
```

- `project.json`: 複数画像 case の一覧、status、document/rendered の相対パス。
- `cases/<case_id>/document.json`: GUI が編集する正規データ。
- `cases/<case_id>/generated/`: cp-sat / reflow / render 互換 JSON を保存。
- `cases/<case_id>/renders/latest.png`: 最新 preview。

### Document Schema

`document.json` の bubble は次を持つ。

- `bubble_id`
- `sentence_ids`
- `text`
- `columns`
- `bubble_type`
- `speaker_id`
- `placement.anchor_x`
- `placement.anchor_y`
- `manual.columns`
- `manual.bubble_type`
- `manual.placement`
- `source.placement = "cp-sat" | "manual" | "imported"`

### GUI v1

v1 で編集する範囲は次に限定する。

- anchor drag による位置修正
- `bubble_type` 変更
- `columns` 変更
- render preview 更新

本文そのものの自由編集は次フェーズに回す。

### cp-sat 再実行

- v1 では「全 bubble 再生成」だけ提供する。
- `manual.placement=true` の bubble がある場合、再生成前に確認を出す。
- lock 付き部分再配置は次フェーズに回す。

### Render

- `document.json -> generated/reflow.json + generated/scene.json` に export する。
- `generated/` を workspace として既存 renderer を呼ぶ。
- 出力を `renders/latest.png` に保存する。

## Implementation Outline

### Python

- `bubble/editor_models.py` を追加する。
  - `ProjectDocument`
  - `CaseDocument`
  - `EditorBubble`
  - load/save/validate
  - workspace import
  - generated JSON export
- `bubble/editor_server.py` を追加する。
  - FastAPI app
  - project / case document API
  - image / rendered file serving
  - export / render endpoint
- `bubble/cli.py` に `text-bubble editor serve` を追加する。
- `pyproject.toml` に `fastapi` と `uvicorn` を追加する。

### Web

- `web/editor/` に React + Konva frontend を追加する。
- 画面構成:
  - case list sidebar
  - source image canvas
  - draggable bubble anchor markers
  - selected bubble property panel
  - save / render buttons
- `bubble_type` は `/api/bubble-types` から取得する。

## API

FastAPI 側の最小 API:

- `GET /api/project`
- `GET /api/cases/{case_id}/document`
- `PUT /api/cases/{case_id}/document`
- `POST /api/cases/{case_id}/export`
- `POST /api/cases/{case_id}/render`
- `GET /api/cases/{case_id}/image`
- `GET /api/cases/{case_id}/rendered`
- `GET /api/bubble-types`

file serving は project 内 document/render/image/rendered の解決済みパスだけ許可する。

## Test Plan

- `cp-sat` で生成された `reflow.json` / `scene.json` から `document.json` を作れる。
- `document.json` の placement が `generated/scene.json` に正しく export される。
- GUI で anchor を動かすと `manual.placement=true` になり、render に反映される。
- `columns` が `text` を復元しない場合は保存エラー。
- `bubble_type` が manifest に存在しない場合は保存エラー。
- render 後に `renders/latest.png` と `generated/plan.json` が作られる。

## Assumptions

- 初期配置は cp-sat を標準とする。
- GUI は cp-sat 結果の修正ツールであり、ゼロから配置するツールではない。
- v1 の再配置は全体再実行のみ。
- 手動修正済み bubble を固定した部分再配置は後で設計する。
- 既存 `text-bubble render` / `run` は壊さず、editor は追加機能として実装する。
