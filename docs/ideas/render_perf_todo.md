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
