---
name: text-bubble-overview
description: 画像に縦書き吹き出しを合成し、assign / reflow / scene / render / run / full の各段階で text-bubble CLI を使いたいときに使う。mask、renderer、workspace の入口もこの skill から辿れる。llama-server は現運用では使わない（cp-sat planner ベース）。
---

# text-bubble Overview

## この Repo でできること

- 画像に縦書きの漫画風吹き出しを合成できる
- `assign`、`reflow`、`scene`、`render`、`run`、`full` の段階実行と一括実行ができる
- `cp-sat` / `llm` planner、`resvg` / browser renderer、worker 経由の実行を切り替えられる
- **現運用では `cp-sat` planner のみ使用**。`llm` planner は llama-server を要するが運用していない

## この Skill が向いている依頼

- text-bubble で吹き出し合成を実行したい
- reflow や scene placement の入口を確認したい
- workspace、mask、renderer の前提を素早く見たい

## この Repo の責務

- 吹き出し合成 CLI と scene placement 実装
- dialogue から bubble plan を作る LLM / planner 接続
- 吹き出し描画と text rendering の実装

## この Repo が責務として持たないもの

- 元画像の生成。これは主に `../comfyui/` の責務
- mask 自体の生成。これは主に `../comfy-agent/` の責務

## 主要成果物

- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/bubble/cli.py` - `text-bubble` CLI の入口
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/bubble/scene_runtime.py` - scene planning / rendering の本体
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/assets/bubble_assets.json` - bubble type と asset manifest
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/out/` - workspace と描画結果の既定出力先

## 典型的なワークフロー

1. `text-bubble` CLI か `.venv/bin/text-bubble` を使える状態にする
2. `assign` で dialogue を bubble 単位に分ける
3. `reflow` で列分割を決める
4. `scene` で mask 前提の配置を決める
5. `render` または `run` / `full` で画像を書き出す

## 受け渡し点

- 入力: 元画像、dialogue、mask
- 出力: workspace JSON 群、描画済み画像、評価 JSON
- 備考: `llm` planner を使う場合のみ llama-server が必要。現運用では未使用

## 必要に応じて読む references

- `references/key-files.md` - 重要ファイルと読む順番を確認したいとき
- `references/commands.md` - 実行コマンドを確認したいとき
- `references/pitfalls.md` - renderer や mask で詰まったとき
