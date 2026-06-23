# resvg-hybrid 試行錯誤ログ（2026-03）

## 背景
- 目的: Browser 依存を減らしつつ、縦組み日本語の見た目を維持する。
- 前提: `text_renderer=resvg-hybrid` / `bubble_renderer=resvg` を既定化。
- 既知問題: `resvg` の `writing-mode` と `text-orientation` だけでは記号回転や文字間の一致が崩れる。

## 実施した主な試行

1. 固定グリッド + `safe/manual` 混在描画  
- 内容: `safe` は `writing-mode: vertical-rl`, `manual` は `<g rotate(90 ...)>`。  
- 結果: 句読点・長音周辺のズレが目立つ。

2. `safe` を run 単位描画に変更  
- 内容: 文字単位ではなく同一 action 連続区間をまとめて描画。  
- 結果: 微改善。ただし anchor 解釈差は残存。

3. 手動文字を `<text>` からグリフパスへ移行  
- 内容: HarfBuzz + FontTools で path 化し、`manual_sideways/manual_upright` を path 描画。  
- 結果: 手動回転部分は安定化。

4. 全文字 path 化（safe 含む）  
- 内容: `safe` も path 描画へ統一。  
- 結果: 比較指標が悪化するパターンが多く、配置アンカーの取り方で不安定。

5. `safe` の可変 advance（`y_advance`）+ 列高さ再正規化  
- 内容: `safe` だけ HarfBuzz の `y_advance` を使い、列全体高さは固定グリッド相当に再正規化。  
- 結果: 見た目バランスは比較的良好。  
- 採用: **この方式を最終版として採用**。

## 非採用にした案
- `safe` のメトリクス中心アンカー (`metric_center`)  
  - 結果: 全体オフセットが増え、比較指標悪化。
- `safe` の advanceセル中心アンカー (`y_advance/2`) 単独最適化  
  - 結果: 一部改善するが、最終採用版には劣る。

## 最終決定
- `safe`: path 描画 + `y_advance` ベース可変送り + 列高さ再正規化。
- `manual_sideways/manual_upright`: path 描画を維持。
- `browser` レンダリング経路は保守用として残す（`--text-renderer browser`）。

## 参照出力
- 最終版: `out/poc_check/hybrid_cli_final.png`
- 最終比較: `out/poc_check/hybrid_misalignment/hybrid_text_final_cmp.png`
- 採用基準比較: `out/poc_check/hybrid_misalignment/hybrid_text_safe_variable_norm_cmp.png`
