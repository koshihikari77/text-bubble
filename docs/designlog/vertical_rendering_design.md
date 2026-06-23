# 縦書きレンダリング 設計と試行錯誤

最終更新: 2026-06

`text-bubble` の縦書きレンダリング経路、その採用に至った経緯、現存する
弱点と将来の TODO をまとめる。古い試行錯誤ログ
(`resvg_hybrid_iteration_log.md`、`pango_resvg_vertical_validation.md`) と
合わせて読むと文脈が深くなる。

---

## 1. 現在の設計（2026-06 時点）

### 1.1 採用構成

- Text renderer: `resvg-hybrid` (default)
- Bubble renderer: `resvg`
- Browser 経路 (`--text-renderer browser`) は保守用に残置

### 1.2 描画経路

```
plan (text, columns[])
  └─ compute_text_layout (bubble/layout.py)
        - HarfBuzz で各列の vertical advance を測って block_width/height を算出
  └─ render_text_overlay_resvg_hybrid (bubble/text_render_resvg_hybrid.py)
        - 列ごとに grapheme 分割 (regex \X)
        - 各 grapheme を vertical_uax.py で 3 値分類
              "safe" / "manual_sideways" / "manual_upright"
        - safe は y_advance ベースで間隔可変、その他は固定 grid_step
        - SVG 要素を組み立てる（全 cluster が <path> になる）
        - resvg CLI で SVG → PNG ラスタライズ
```

**本番経路では resvg は文字組版エンジンではなく純粋な path ラスタライザ**
としてしか使われない。`HarfBuzzGlyphPathRenderer` が用意できないとき
（= `font_path` が解決できないとき）に限り `<text>` 描画への fallback が走る。

### 1.3 文字分類: 2 レイヤ + UCD

```
1. アプリ tailoring (VERTICAL_ORIENTATION_OVERRIDES)
2. Unicode UCD VerticalOrientation.txt
3. 該当なしは UAX#50 デフォルト "R"
```

`bubble/ucd_vertical_orientation.py` が `assets/VerticalOrientation.txt`
(Unicode 17.0.0 同梱) をパースして codepoint → orientation を返す。
手書きの `TR_CHARS` / `TU_CHARS` / `east_asian_width` フォールバックは廃止
（UCD で完結）。

`VERTICAL_ORIENTATION_OVERRIDES` は作品要件レベルの per-character override
レイヤ。デフォルトは空。「↑↓ を U に倒したい」のような要件はここで管理する。

### 1.4 4 値 orientation → 3 値 action マッピング

| UAX#50 orientation | 該当例 | アクション |
|---|---|---|
| `U` (Upright) | ひらがな・漢字・全角・ハート・音符・星・丸記号 | `safe` |
| `R` (Rotate) | ASCII、矢印、`…`、`‥` | `manual_sideways` |
| `Tu` (Transformed Upright) | `、。，．・：；` | `safe` |
| `Tu` (`MANUAL_UPRIGHT_CHARS`: `？！`) | 全角疑問符・感嘆符 | `manual_upright` |
| `Tr` (Transformed Rotate) | `「」（）ー〜` | font の vert 代替があれば `safe`、無ければ `manual_sideways` |

### 1.5 各アクションのレンダリング

- **safe**: HarfBuzz で `direction="ttb"` shape → 縦書き専用 glyph があれば
  自動適用される (`vert/vrt2` を明示指定しない、これは OpenType 仕様上
  `vrt2` が `vert` の代替モデルだから)。path 化 + `y_advance` ベース可変
  送り + 列高さ再正規化
- **manual_sideways**: LTR shape → glyph path を 90 度 CW 回転して配置
- **manual_upright**: LTR shape → glyph path を回転せず配置（正立）

すべて単一 `translate × scale(s,-s) × translate` 合成で SVG transform
として書き出す（Font Y-up → SVG Y-down 反転は 1 回だけ）。

### 1.6 font fallback chain

JK Gothic L には `♡♥❤❥❣ ♫ 〜` 等の outline が無い（GID は割り当てられて
いるが path が空）。これを救うため `HarfBuzzGlyphPathRenderer` に
fallback chain を実装。

`bubble.assets.pick_fallback_font_paths()` が DejaVu Sans 等の outline を
持つ font を返し、primary で path が空のときに fallback で再 shape する。
DejaVu Sans に ♡♥❤❥❣ ♪♫♬ ★☆ ●○ ①② 等の outline が揃っているため、
JK font + DejaVu fallback で必要な記号は概ねカバーできる。

### 1.7 主要ファイル

- `bubble/vertical_uax.py` — 分類ロジック（UCD lookup + override + 4 値→3 値）
- `bubble/ucd_vertical_orientation.py` — UCD VO テーブル lookup
- `bubble/text_render_resvg_hybrid.py` — SVG 構築の本体
- `bubble/glyph_paths.py` — `HarfBuzzGlyphPathRenderer`（fallback chain あり）
- `bubble/layout.py` — `compute_text_layout` / `build_text_metrics`
- `bubble/assets.py` — fallback 候補、resvg CLI 経由の SVG 描画
- `assets/JKG-L_3.ttf` — 採用 primary フォント
- `assets/VerticalOrientation.txt` — UCD VO テーブル (Unicode 17.0.0)
- `scripts/audit_vertical_font.py` — フォント能力監査ツール
- `scripts/render_vertical_golden.py` — 比較用 golden 画像生成
- `docs/designlog/golden_vertical/` — 11 ケース × 3 サイズ = 33 枚の golden
- `docs/designlog/font_audit_jkg_vs_fallbacks.txt` — 実測スナップショット

---

## 2. ここに至った経緯

### 2.1 2026-03: browser → resvg-hybrid 移行

詳細は `resvg_hybrid_iteration_log.md`。

要点:
- browser (Playwright/Chromium) 依存を減らしつつ縦組み日本語の見た目を保つ
- resvg の `writing-mode` + `text-orientation` だけでは句読点周辺がズレる
- `safe`（縦組みネイティブ）と `manual_*` を path 化する hybrid 構成に
- 5 段階の試行錯誤を経て「safe を path 化 + `y_advance` ベース可変送り +
  列高さ再正規化」を採用

### 2.2 2026-06: P0 改修の動機

ユーザーがハート (`♡`) 等の絵文字記号類を縦書きで使いたいと要望。
当初は `vertical_uax.py` の分類テーブルにハートを追加するだけの簡易対応
（commit `e5879e5`）を行ったが、実際にレンダリングを試したところ
**画面に何も描画されていない**ことが判明。

実測すると JK Gothic L は ♡♥❤❥❣ ♫ 等の outline を持たない
(GID は割り当てられているが SVG path が空、bounds=None)。分類追加だけでは
描けない。

### 2.3 GPT Pro 第 1 ラウンド: アーキテクチャ・レビュー

縦書き経路の現状をまとめて第三者レビューを依頼。主な指摘:

1. **`{vert:1, vrt2:1}` 同時指定は誤り**: `vrt2` は `vert` の代替モデルで
   並列指定するものではない。`direction="ttb"` だけで HarfBuzz が縦組み
   feature を自動適用する
2. **手書き分類テーブルではなく Unicode `VerticalOrientation.txt` から
   生成すべき**: 公式データを一次情報にしたほうが網羅性・正確性が高い
3. **`UAX#50 上の意味 / font 能力 / 配置方法` を分けて管理する**: 現状
   `safe / manual_sideways / manual_upright` の 3 値合成は責務が混じる
4. **ハートは `Tu` ではなく `U`**: UCD 上 U が正
5. **「resvg バグ回避」名義の `TU_RESVG_OVERRIDE_CHARS` を実態に合わせて
   `MANUAL_UPRIGHT_CHARS` にリネーム**: 本番は全 path 経路なので resvg
   バグは無関係、「明示的に正立 path 配置する文字集合」が現在の役割

### 2.4 P0 実装内容 (commit `2233a22` 他)

- `{vert:1, vrt2:1}` 撤廃、`direction="ttb"` のみに統一
- ハート類を `U_FORCE_UPRIGHT_OVERRIDES` に移動 (後に P1 で UCD 化により撤廃)
- `TU_RESVG_OVERRIDE_CHARS` → `MANUAL_UPRIGHT_CHARS` リネーム (旧名は alias)
- `HarfBuzzVerticalProbe` を LTR vs TTB shape の差分比較に修正
- `HarfBuzzGlyphPathRenderer` に **font fallback chain** を追加
  （これが「ハートが描けない」問題の根本解決）
- DejaVu Sans 等を fallback 候補として登録
- `scripts/audit_vertical_font.py` で primary + fallback の能力を監査
- `scripts/render_vertical_golden.py` で 33 枚の golden 画像セット作成

P0 完了時点でハート他の記号類が縦書きで正立描画されるようになった。

### 2.5 GPT Pro 第 2 ラウンド: path 配置の具体実装レビュー

第 1 ラウンド後、「path 化したい個別文字（特に `♡`）が JK font で
うまく縦書き配置できなかったのはなぜか」を追って質問。回答:

> 横書きglyphの原点を、そのまま縦書きセルの中心として扱ったこと
> が原因。LTR shape済み輪郭runの実bbox中心を、単一matrixで縦セル
> 中心へ一致させるのが安定解

照合した結果、`bubble/text_render_resvg_hybrid.py::_cluster_path_element`
と `bubble/glyph_paths.py` は既にこの方針通りに実装されていた:

- HarfBuzz `x_offset / y_offset / advance` を正しく加算 ✓
- `BoundsPen` で実輪郭 bbox を取得 ✓
- bbox 中心 → セル中心の単一 transform ✓
- Y 軸反転は 1 回だけ ✓

つまり**数学的核心は既に正しく**、ハートが描けなかった原因は配置ロジック
ではなく outline 欠落のほうだった、と確認できた。

### 2.6 P1 実装内容 (commit `ce2bd1b`)

- **UCD ベース VO 分類** (P1-8): `bubble/ucd_vertical_orientation.py` 追加、
  `assets/VerticalOrientation.txt` 同梱、`vertical_uax.py` の手書きテーブル
  完全撤廃
- **アプリ tailoring override レイヤ** (P1-9): `VERTICAL_ORIENTATION_OVERRIDES`
- **数値テスト** (P1-12): bbox × transform 合成の中心一致を SVG 生成前に
  保証する unit test を 18 件追加

副作用として 9 枚の golden 画像が変わった。中身は全て改善:

- ♪♫♬ ★☆ ●○◎ ①②③ などが従来 `east_asian_width` フォールバックで R に
  分類され横倒しになっていた。UCD 上は U なので**正立配置に変更**
- `…` `‥` は UCD で R。従来は手書き `Tr` で font の vert glyph 経路を
  使っていたが LTR 90° 回転に統一（差は軽微）

---

## 3. 残課題

優先度低い順に並べる。

### 3.1 deferred と判断した P1 残

| ID | 内容 | 見送り理由 |
|---|---|---|
| P1-10 | `VerticalGlyphPlan` のような分離 dataclass | 現状の `ClusterDecision` で十分 |
| P1-11 | run-based shaping (`……`、`──` を 1 run で shape) | JK font に `vchw` 等が無く可視メリット無し |
| P1-13 | `safe` 配置方針の実画像比較（bbox 中心 vs advance+offset） | 視覚的リグレッションリスク > benefit |
| P1-14 | layout / placement の完全分離 | 同上 |

別 font を導入したくなった時点で再評価。

### 3.2 P2 (見た目を磨きたくなったら)

- **optical alignment overrides**: ハート等が視覚的に上下にズレて見える
  場合の em 比率での per-char 微調整テーブル
  ```python
  MANUAL_UPRIGHT_OPTICAL_SHIFT_EM = {"♡": (0.0, 0.0), ...}
  ```
- **デバッグ SVG オプション**: セル枠 / ink bounds / 中心線を一時的に
  可視化するスイッチ（今後の調整作業を効率化）
- **縦中横 (tate-chu-yoko) first-class inline object**: `<tcy>10</tcy>`
  風の明示 markup + 1em × 1em セル内 fit + center 配置

### 3.3 P3 (大改修・要件が固まったら)

- **Noto Sans CJK JP との 2 系統テスト**: 正しい `vert/vrt2` を持つフォント
  での挙動が変わらないことを golden として担保
- **`vchw` 等 GPOS feature の効果検証**: 句読点 run をまとめて shape した
  ときの効果（JK font には無いので Noto CJK 検証時に同時実施）
- **JLREQ ベース記号間隔モード切替**: 現在の固定 grid モードと別に提供
- **font hash / Unicode version / renderer version を golden test に記録**:
  再現性の担保
- **single `matrix()` 形式への統一**: 現状の `translate / scale / translate`
  合成を 1 つの `matrix()` に統合（コスメ）
- **runtime 代替検討**: Chrome Headless（golden 用）/ Pango / Skia
  （メリット薄、優先度最低）

### 3.4 既知の小さい弱点

- 半角英数字混在の `manual_sideways` baseline が東アジア advance と
  揃わない
- ルビ・傍点・圏点は未対応
- フォントが提供する縦書き metrics（vmtx）の精度に依存。JK font の
  vmtx 信頼性は audit 結果（`docs/designlog/font_audit_jkg_vs_fallbacks.txt`）
  参照

---

## 4. 参考

### Unicode / OpenType

- UAX#50 Unicode Vertical Text Layout: <https://www.unicode.org/reports/tr50/>
- VerticalOrientation.txt: <https://www.unicode.org/Public/UNIDATA/VerticalOrientation.txt>
- CSS Writing Modes Level 4: <https://www.w3.org/TR/css-writing-modes-4/>
- OpenType registered features: <https://learn.microsoft.com/en-us/typography/opentype/spec/features_uz>
- JLREQ 日本語組版処理の要件: <https://www.w3.org/TR/jlreq/>

### 実装系

- HarfBuzz hb-buffer / hb-font: <https://harfbuzz.github.io/>
- fontTools svgPathPen / boundsPen: <https://fonttools.readthedocs.io/en/stable/pens/>
- resvg vertical text issue: <https://github.com/linebender/resvg/issues/890>
- librsvg vertical text bug: <https://gitlab.gnome.org/GNOME/librsvg/-/issues/364>

### 関連 designlog

- `pango_resvg_vertical_validation.md` — Pango/HarfBuzz/Cairo + resvg 検証
  (2026-03 PoC)
- `resvg_hybrid_iteration_log.md` — 採用方式に至った試行錯誤履歴 (2026-03,
  2026-06 追記あり)
- `font_audit_jkg_vs_fallbacks.txt` — 採用 font + fallback の能力監査結果
- `golden_vertical/` — 11 ケース × 3 サイズ = 33 枚の比較用画像
