# Render Performance TODO

描画系のボトルネック対策メモ。  
2026-03-05 時点で 1〜5 は実装済み。必要に応じて再調整する。

## Checklist

- [x] 1. Chromium を render 全体で使い回す
  - 期待効果: 起動コストの削減
  - 実装: `bubble/render.py` で `render_bubbles` 内 1 回起動に統合

- [x] 2. `networkidle + fixed timeout` 待機を削減
  - 期待効果: バブルごとの固定待ち時間削減
  - 実装: `domcontentloaded` + `document.fonts.ready` に置換

- [x] 3. 中間 PNG のディスク I/O を排除
  - 期待効果: `.tmp-bubble-render` 連鎖書き込みの削減
  - 実装: メモリ上で順次合成し、最後に 1 回だけ保存

- [x] 4. 吹き出しラスタ結果のキャッシュ
  - 期待効果: 同サイズ bubble の再ラスタ化回避
  - 実装: `(renderer, asset, width, height)` キーの in-memory cache

- [x] 5. reflow API 呼び出しの並列化
  - 期待効果: 複数バブル時の推論待ち短縮
  - 実装: `ThreadPoolExecutor` + `--reflow-workers`（既定 4）

## Follow-up

- [ ] render 時間の再計測（1文/5文/10文）
- [ ] `reflow-workers` の推奨値をサーバースペック別に整理
- [ ] 必要なら bubble キャッシュを LRU 化

## Scene Placement E2E

`1画像 + 1ケース` の `cp-sat -> scene.json -> rendered.png` 実測は、2026-03-13 時点でおおむね `15-18s`、重いケースでは `30s+`。  
プロファイル上、律速は `cp-sat solve` 単体ではなく、起動コスト・再計算・描画/保存を含む end-to-end 全体にある。

### 観測された内訳

- cold start/import
  - `ortools` と依存 import が重く、単発でも数秒食う
  - batch はケースごとに Python を起動し直しているため、ここが素直に積み上がる
- `cp-sat solve`
  - 単発ケースでは数秒未満から数秒台
  - 難ケースではここも伸びるが、全体の唯一の律速ではない
- render / PNG save / debug board
  - `rendered.png`, `debug_overlay.png`, `codex_board.png` の生成と保存が重い
  - 特に PoC のように artifact を複数出す経路では solver 以外の時間が大きい

### 実運用前提で効く高速化案

- 長寿命 worker 化
  - CLI 1 回 1 プロセスをやめ、常駐 Python worker に `image + masks + reflow` を投げる
  - import と font/renderer 初期化を 1 回で済ませる
- solver / eval / render のデータ共有
  - `cp-sat` が選んだ候補から確定した `text_box`, `bubble_box`, penalties を、scorer と renderer がそのまま共有する
  - solve 後の再評価・再レイアウトを減らす
- ページ全体 1 回 render
  - bubble ごとに個別 render/合成する代わりに、最終ページを 1 枚の SVG/HTML にして 1 回で描画する
  - 外部 renderer 呼び出し回数と画像合成回数を減らす
- PNG 保存の軽量化
  - `compress_level`, `optimize` を見直す
  - 見た目を変えずに保存時間を削る
- text metric / glyph path のキャッシュ
  - 同一 `font + size + text + options` に対する HarfBuzz/path 計算を使い回す
  - 同じ画像・同じ dialogue 系を何度も試す運用で効く
- candidate 生成の高品質化
  - 候補数を雑に減らすのではなく、`person/head/chest/lower` 距離場ベースで筋の悪い候補を最初から作らない
  - `cp-sat` に渡すモデルサイズを減らしつつ、必要な多様性は維持する
- dominated candidate の事前除去
  - 同じ coarse cell 内で位置も penalty もほぼ劣後する候補を落とす
  - pairwise 制約の組数を減らす
- renderer の常駐化
  - 可能なら `resvg`/browser を都度起動せず、長寿命プロセスまたは in-process API で呼ぶ

### 優先順位

1. 長寿命 worker 化
2. solver / eval / render の内部データ共有
3. ページ全体 1 回 render
4. candidate 生成の高品質化
5. PNG 保存最適化と text metric cache

PoC artifact を減らして速くするのではなく、実際に使う本番フローそのものを速くするなら、まずこの順で触るのが筋。
