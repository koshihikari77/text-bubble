# 日本語縦書きレンダリング 現状整理と GPT Pro レビューまとめ

最終更新: 2026-06

`text-bubble` の現状の縦書き経路、JK Gothic L 採用に起因する問題、
GPT Pro に相談して得たレビュー、それに対する評価とアクション計画を 1 本に
まとめたもの。今後の縦書き周りの判断・実装はここを起点にする。

---

## 1. 現状の縦書きレンダリング構造

### 1.1 採用経路

レンダラは **resvg-hybrid** がデフォルト。`<svg>` を組み立てて `resvg` CLI で
PNG 化する。フロー:

```
plan (text, columns[])
  └─ compute_text_layout (bubble/layout.py)
        - HarfBuzz で各列の vertical advance を測って block_width/height を算出
        - canvas_width / canvas_height ベースで outline_width 等も決定
  └─ render_text_overlay_resvg_hybrid (bubble/text_render_resvg_hybrid.py)
        - 列ごとに grapheme 分割 (regex \X)
        - 各 grapheme を vertical_uax.py で 3 値分類
              "safe" / "manual_sideways" / "manual_upright"
        - safe は y_advance ベースで間隔可変、その他は固定 grid_step
        - SVG 要素を組み立てる
              safe          → HarfBuzz shape (direction="ttb", features={vert:1, vrt2:1}) → <path>
              manual_sideways → HarfBuzz shape (direction="ltr") → <path transform=rotate(90)>
              manual_upright  → HarfBuzz shape (direction="ltr") → <path>（正立のまま）
        - resvg CLI で SVG → PNG ラスタライズ
```

ブラウザ経路 (`text_renderer=browser`) は保守用に残しているが、
**resvg-hybrid が現運用デフォルト**。理由は速度と再現性。

### 1.2 重要な実装事実

- `font_path` が渡されている時 (= 常に渡される)、`HarfBuzzGlyphPathRenderer`
  が用意され、**全 action が `<path>` として描かれる**
- `<text>` 経由の SVG テキスト描画は `path_renderer is None` のフォールバック
  にしか出てこない
- つまり本番では **resvg は文字組版エンジンとして使われておらず、純粋に path
  ラスタライザとしてしか動いていない**
- したがって resvg の `text-orientation: mixed` の解釈バグは本番経路には
  影響しない

### 1.3 文字分類ルール (`bubble/vertical_uax.py`)

UAX#50 ベースで 4 値分類した後、3 アクションに落とす。

| UAX#50 orientation | 該当例 | アクション |
|---|---|---|
| `U` (Upright) | ひらがな・カタカナ・漢字・全角 | `safe` |
| `R` (Rotate) | ASCII、半角記号 | `manual_sideways` |
| `Tu` (Transformed Upright) | `、。，．・：；` | `safe` |
| `Tu` (resvg バグ対象) | `？！♡♥❤❥❣` | `manual_upright` |
| `Tr` (Transformed Rotate) | `「」『』（）ー―…〜` | `manual_sideways` (or `safe` if `vert` 適用で別 glyph に切り替わる) |

`Tr` の振り分けは HarfBuzz の `vert/vrt2` feature を試して、適用前後で
glyph ID が変われば `safe`、変わらなければ `manual_sideways` に倒す
(コード: `HarfBuzzVerticalProbe.has_vertical_substitution`)。

### 1.4 JK Gothic L (`assets/JKG-L_3.ttf`) の致命的事情

実測 (uharfbuzz でフォント内部を覗いた):

```
=== Font info ===
  family: JK Gothic L (Light)

=== GSUB features ===
  vert: 1    ← 存在はする
  (vrt2 無し、vrtr 無し)

=== vert 適用前後の glyph ID 比較 ===
'、': off=(15230,) vert=(15230,) differs=False
'「': off=(15240,) vert=(15240,) differs=False
'ー': off=(15426,) vert=(15426,) differs=False
'…': off=(15096,) vert=(15096,) differs=False
'？': off=(15494,) vert=(15494,) differs=False
'♡': off=(1764,) vert=(1764,) differs=False
(調査した全文字で differs=False)
```

判明事項:

- `vert` の lookup が空、または全文字を同じ glyph に戻す
- `vrt2`、`vrtr` テーブル無し
- つまり OpenType 標準の縦書き正立 / 別字形差し替えが **完全に死んでいる**
- `Tr` 振り分けの HarfBuzz probe は常に False を返す → 実質**全 `Tr` 文字が
  `manual_sideways` に倒れる**
- `Tu` 描画は resvg 側の解釈に頼るしかないが、本番では path 経路なので影響なし

注意: GID 比較は調査した数文字に対してで、フォント全体の GSUB lookup が
完全に空かどうかは断定不十分。GSUB 全列挙して確証を取るのは未実施。
ただし運用上問題になっている句読点・括弧・長音で置換が無いなら、
実装上の結論はほぼ同じ。

### 1.5 採用した妥協点 (`resvg_hybrid_iteration_log.md` 抜粋)

1. 固定グリッド + safe/manual 混在 → 句読点周辺ズレ
2. safe を run 単位で描く → 微改善、anchor 解釈差残る
3. manual を path 化 → 安定
4. 全文字 path 化 → アンカー不安定で逆効果
5. **safe は path 化 + y_advance ベース可変送り + 列高さ再正規化** ← 採用

採用版は `safe` のみ可変送り、列全体の高さは固定 grid 相当に再正規化することで
他 bubble との視覚揃いを保つ。

### 1.6 既知の弱点 (相談前にすでに把握していたもの)

- 音符 `♪♫`、矢印 `→←`、星 `★☆`、丸 `●○`、丸数字 `①②` の縦書き挙動が未定義
  (TU_CHARS / TU_RESVG_OVERRIDE_CHARS に登録していない)
- ハイフン (-) や英数字混在の縦書き表現
- 連続する `…` や `‥` が `…` 1 文字として描かれているか
- ルビ・縦中横 (縦書き内の横書き短文) 未対応
- 行間 (`safe` 中心) の最適化が句読点連続時に密集して見えるケースあり

---

## 2. GPT Pro レビュー（全文）

以下、相談に対する回答を編集なしで引用。

> ## 結論
>
> 現状の **HarfBuzzでshape → glyph path化 → resvgはpathのラスタライズだけ
> 担当**という方向は、JK Gothic Lの縦組み字形が実質利用できない前提では妥当
> です。レンダラを置き換えるだけでは、存在しない縦字形は復活しません。
>
> ただし、次の3点は先に直した方がよいです。
>
> 1. **`vert` と `vrt2` を同時指定しない**
> 2. **文字分類をEast Asian Widthや手書き集合ではなく、Unicodeの
>    `Vertical_Orientation`から生成する**
> 3. **UAX #50上の意味、フォント能力、実際の配置方法を別々に管理する**
>
> 加えて、説明中に経路の不一致があります。冒頭では「safeはresvgネイティブ」、
> 後半では「safeもHarfBuzzからpath化」となっています。現行版が全カテゴリを
> `<path>`化しているなら、resvgの`text-orientation`バグは本番経路には直接
> 影響せず、resvgは単なるパスラスタライザです。`TU_RESVG_OVERRIDE_CHARS`も、
> 現在の役割に合わせて再整理した方がよいでしょう。
>
> ---
>
> ### 1. `vert`が実質空なら、`vchw`や`vpal`で救えるか
>
> #### 字形については、ほぼ救えません
>
> 理解は概ね正しいです。
>
> `vert`、`vrt2`、`vrtr`は、縦組みに適した**別glyphへの置換**に関係します。
> 一方、
>
> * `vchw`
> * `vhal`
> * `vpal`
> * `vkrn`
>
> などは、主に位置、advance、余白、カーニングを調整する機能です。
> 横組み用の括弧を縦組み用括弧に変えたり、横長の句読点を回転・変形したり
> する機能ではありません。`chws`は横組み側で、縦組みに対応するのが`vchw`
> です。
>
> したがってJK Gothic Lについては、次の切り分けになります。
>
> | 要素               |                  GSUBが実質空でも利用可能か |
> | ---------------- | -------------------------------: |
> | 括弧・長音・波線などの縦専用字形 |                               不可 |
> | 横組みglyphの90度回転   |                          アプリ側で可能 |
> | 句読点のセル内位置調整      |                GPOSが実装されていれば一部可能 |
> | 縦advance         |         `vhea` / `vmtx`が有効なら利用可能 |
> | 句読点列の文脈依存スペーシング  | `vchw`が実装され、run単位でshapeする場合に限り可能 |
>
> 特に重要なのは、現在のように**各graphemeを個別にHarfBuzzへ渡すと、
> 仮に`vchw`が存在していても前後文脈が見えない**ことです。句読点列に対する
> 文脈依存配置を利用したいなら、同じ配置方式の連続部分をrunとしてshape
> する必要があります。
>
> #### `vert`と`vrt2`の同時指定はやめるべき
>
> 現在の
>
> ```python
> features={"vert": 1, "vrt2": 1}
> ```
>
> は推奨できません。
>
> OpenTypeでは`vrt2`は`vert`と併用する追加機能ではなく、別の縦組みモデル
> として扱われます。少なくとも両方を無条件に有効化するべきではありません。
> HarfBuzzはbuffer directionを縦方向にすると、通常の縦組み機能を自動的に
> 適用するため、まずは`direction="ttb"`にして明示指定を外すのが安全です。
>
> 推奨形は次です。
>
> ```python
> # 正立・縦字形を使うrun
> buffer.direction = "ttb"
> hb.shape(font, buffer)  # vertを手動で重複指定しない
>
> # 横組み字形を回転するrun
> buffer.direction = "ltr"
> hb.shape(font, buffer, {"vrtr": True} if supports_vrtr else {})
> # その後、run全体を90度回転
> ```
>
> #### 「フォントの`vert`が空」の表現は少し限定した方がよい
>
> 提示された実測から確実に言えるのは、
>
> > 調査対象の文字について、`vert`適用による有効なglyph置換が確認できなかった
>
> ということです。
>
> 数文字のGID比較だけでは、フォント全体のlookupが完全に空かどうかまでは
> 断定できません。フォント監査では、次を列挙すると明確です。
>
> * GSUBの`vert` / `vrt2` / `vrtr` lookup coverage
> * 置換元GIDと置換先GID
> * GPOSの`vchw` / `vhal` / `vpal` / `vkrn`
> * `vhea` / `vmtx`の有無と内容
>
> とはいえ、今回問題になっている句読点・括弧・長音等で置換がないなら、
> 実装上の結論はほぼ同じです。
>
> ---
>
> ### 2. resvgの`text-orientation: mixed`は仕様準拠か
>
> #### `？！`とハートを横倒しにするなら、仕様上は誤りと考えてよいです
>
> CSS Writing Modesの値は、
>
> ```css
> text-orientation: mixed;
> text-orientation: upright;
> text-orientation: sideways;
> ```
>
> です。`vertical-upright`という値はありません。
>
> `mixed`ではUnicodeのVertical Orientationに従って、正立対象は正立、
> 回転対象はsidewaysとして扱うのが基本です。全角`！`と`？`は`Tu`、ハート類
> は`U`なので、横倒しにするのは期待される挙動ではありません。
>
> resvgには、日本語縦書きで文字方向を誤る問題の報告も残っています。
> したがって、少なくとも「resvgに任せればCSS Writing Modesどおりになる」
> とは置けません。
>
> #### `text-orientation: upright`を指定した場合
>
> ```svg
> <text
>   writing-mode="vertical-rl"
>   text-orientation="upright">
> ```
>
> とすると、ASCIIを含めて原則すべて正立になります。
>
> ただし、これは**横倒しを防ぐ指定**であって、フォントにない縦専用glyphを
> 生成する指定ではありません。JK Gothic Lに縦括弧がなければ、横組み用の
> 括弧を正立させただけの形になる可能性があります。
>
> 対象文字だけに適用するなら概念上は、
>
> ```svg
> <tspan style="text-orientation: upright">？</tspan>
> ```
>
> ですが、resvgの実装差を避けたい現在の設計では、`manual_upright`として
> path化した方が再現性があります。
>
> なお、旧SVGの`glyph-orientation-vertical="0"`は概ね`upright`、`90`は
> `sideways`に相当しますが、現在は`text-orientation`を使う方が適切です。
>
> #### ただし、全path化済みならこの問題は本番経路から消えます
>
> 最終SVGが、
>
> ```svg
> <path d="..."/>
> ```
>
> だけで構成されているなら、resvgは文字を認識しません。
>
> その場合、
>
> * UAX #50判定
> * HarfBuzz shaping
> * 回転
> * advance
> * セル内配置
>
> はすべてアプリ側の責任であり、resvgの`text-orientation`実装は無関係です。
> 比較用の`<text>`経路と本番path経路をドキュメント上で明確に分けるのが
> よいでしょう。
>
> ---
>
> ### 3. SVGで縦中横を表現する標準的方法
>
> CSS上の標準は`text-combine-upright`です。
>
> ```svg
> <text
>   x="100"
>   y="20"
>   style="writing-mode: vertical-rl">
>   今日は
>   <tspan style="text-combine-upright: all">10</tspan>
>   日です
> </text>
> ```
>
> `all`は対象文字列を横組みし、縦組みの1em程度の領域内へまとめるための
> 指定です。
>
> ただし、SVG 2はSVG実装に`text-combine-upright`の対応を必須としていません。
> したがって、ブラウザでは使えてもresvgで同じ結果になる保証はありません。
>
> #### 現在のpathベース実装では、独立したinline objectにするのが最も堅牢
>
> `<tspan rotate>`ではなく、レイアウトモデルに次の種類を追加するのが
> 適切です。
>
> ```python
> class VerticalInlineKind(Enum):
>     NORMAL = "normal"
>     SIDEWAYS_RUN = "sideways_run"
>     TATE_CHU_YOKO = "tate_chu_yoko"
> ```
>
> 縦中横runについては、
>
> 1. 対象文字列をLTRでまとめてshape
> 2. glyph群全体のvisual boundsを計測
> 3. 原則1em × 1emの箱に収める
> 4. 必要なら横方向のみ縮小
> 5. セル中央へ配置
> 6. 縦方向advanceは1セル分
> 7. 1つの`<g>`またはpath集合として出力
>
> とします。
>
> 概念的には次のようになります。
>
> ```svg
> <g transform="translate(cell_x cell_y)">
>   <g transform="translate(center_x center_y) scale(s)">
>     <!-- "10%"をLTR shapeしたpaths -->
>   </g>
> </g>
> ```
>
> 自動検出より、まずは明示markupの方が安全です。
>
> ```text
> 今日は<tcy>10</tcy>日
> 速度は<tcy>100m</tcy>
> ```
>
> 自動検出を入れる場合も、初期規則は「連続する1～2桁」程度に限定した方が
> よいです。`100m`や`10%`を常に1セルへ押し込むと、可読性と倍率が入力依存に
> なりすぎます。
>
> ---
>
> ### 4. `…`と`‥`を縦並びの点にする方法
>
> U+2026 `…`とU+2025 `‥`のVertical Orientationは`R`です。したがって標準的
> なfallbackは、
>
> > 横組みglyphをLTRでshapeし、glyphまたはrun全体を時計回りに90度回転する
>
> です。
>
> 横方向に並んだ点を90度回転すれば縦方向に並ぶため、`manual_sideways`後にも
> 点が水平に見えるなら、分類ではなく次を疑うべきです。
>
> * transformの適用順
> * 回転中心
> * path生成時点ですでに縦方向transformが入っている
> * 親`<g>`と子pathで二重回転している
> * bbox計算前後で軸を取り違えている
>
> 推奨transformは、原点周りに直接回すのではなく、セル中心を基準にします。
>
> ```svg
> <g transform="
>   translate(cell_cx cell_cy)
>   rotate(90)
>   translate(-glyph_cx -glyph_cy)
> ">
>   <path d="..."/>
> </g>
> ```
>
> #### `⋮`への文字置換は基本的に避ける
>
> U+22EE `⋮`は数学用のVERTICAL ELLIPSISです。
>
> * 元テキストの意味が変わる
> * フォントごとにdot間隔や中心が変わる
> * Unicodeの縦方向判定上も、必ずしも「縦組みでそのまま正立」になる記号
>   ではない
>
> ため、テキスト置換よりU+2026/U+2025をpathレベルで回転させる方が安全です。
>
> #### 連続記号はgrapheme単位ではなくrun単位にする
>
> 日本語組版では`……`、`‥‥`、二倍ダーシ相当の連続ダッシュを途中分割しない
> 扱いが一般的です。JLREQでも、連続するリーダーやダッシュを一体として
> 扱う考え方が示されています。
>
> したがって分類前に、
>
> ```text
> ……  → ELLIPSIS_RUN
> ‥‥  → TWO_DOT_LEADER_RUN
> ──  → DASH_RUN
> ```
>
> としてまとめるのがよいです。run全体をshape・回転し、run内部には改行を
> 許さない設計にします。
>
> `〜`はUAX #50では`Tr`です。縦字形があればそれを使用し、なければ回転する
> のが標準fallbackです。作品上「横向きの波線を維持したい」場合は、Unicode
> 標準判定を変えるのではなく、作品スタイルとしてoverrideを持たせる方が
> 整理しやすいです。
>
> ---
>
> ### 5. resvg以外のruntime候補
>
> 優先順位は次のようになります。
>
> #### 1位: 現行のpath生成 + resvgを維持
>
> 最終SVGがすべてpathなら、最も合理的です。
>
> * layoutとorientationを自前で完全制御できる
> * resvgの文字組版実装差を受けない
> * 出力が高速
> * バージョン固定による再現性を取りやすい
>
> この場合、resvgから別runtimeへ替えて変わるのは主に、
>
> * アンチエイリアス
> * hinting
> * stroke/fillの細部
> * filterやmaskの対応
>
> であり、縦組みの正しさではありません。
>
> #### 2位: バージョン固定したHeadless Chromium
>
> CSS Writing Modesと`text-combine-upright`をネイティブに利用したいなら
> 最有力です。
>
> PlaywrightまたはChrome Headlessをコンテナで固定し、フォントも同梱すれば、
> 比較的高い再現性を得られます。
>
> 弱点は、
>
> * 起動・メモリコスト
> * ブラウザ更新による微差
> * sandboxや共有ライブラリ依存
> * PNG取得までのオーバーヘッド
>
> です。
>
> 「ブラウザの正解画像をgolden referenceとして作る」用途には非常に向いて
> います。
>
> #### 3位: Pango + Cairo
>
> HarfBuzzと縦方向gravityを利用でき、テキスト描画系としては成熟しています。
>
> ただし、Pangoの縦書きはCSS Writing ModesやSVGの組版モデルそのものでは
> ありません。UAX #50のtailoring、縦中横、JLREQ風の禁則や記号run処理は、
> 結局アプリ側の設計が必要です。
>
> SVGを介さず直接PNGへ描ける点は利点です。
>
> #### 4位: librsvg / `rsvg-convert`
>
> CLIとしては扱いやすいですが、
>
> ```text
> SVGO → librsvg
> ```
>
> のうちSVGOはSVG最適化ツールであり、文字組版エンジンではありません。
> 縦書き品質を決めるのはlibrsvg側です。
>
> librsvgにも縦書きテキストやanchorに関する実装差・問題報告があるため、
> 「resvgから替えれば直る」とは期待しない方がよいです。
>
> #### 5位: Skia / SkShaper
>
> SVGやCSSに寄らず、glyph shapingと直接描画を自前管理する場合には強力
> です。
>
> ただしSkShaperは低レベルなshaping APIであり、
>
> * UAX #50分類
> * 縦組みrun分割
> * 禁則処理
> * 縦中横
> * punctuation spacing
> * 列レイアウト
>
> まで完成品として提供するものではありません。現在の自前layoutをSkiaへ
> 移植する形になります。
>
> ---
>
> ### 文字分類で直すべき点
>
> `TU_CHARS`やEast Asian Width fallbackより、Unicodeの公式
> `VerticalOrientation.txt`をビルド時に取り込み、範囲テーブルを生成する方が
> よいです。Unicode 17.0のデータでは、挙げられた候補は概ね次の分類です。
>
> | 文字            | VO | 標準fallback |
> | ------------- | -: | ---------- |
> | `♪ ♫ ♬ ♩`     |  U | 正立         |
> | `★ ☆ ✦ ✩`     |  U | 正立         |
> | `● ○ ◎ ◇ ◆`   |  U | 正立         |
> | `① ② ③ ⑩`     |  U | 正立         |
> | `♡ ♥ ❤ ❥ ❣`   |  U | 正立         |
> | `→ ← ↑ ↓ ⇒ ⇐` |  R | 回転         |
> | `╮ ╯ ╭ ╰`     |  R | 回転         |
> | `… ‥`         |  R | 回転         |
> | `？！`          | Tu | 縦字形がなければ正立 |
> | `〜`           | Tr | 縦字形がなければ回転 |
>
> つまり、ハート類を`TU_RESVG_OVERRIDE_CHARS`に入れることは見た目上の対処
> としては機能しますが、意味上は`Tu`ではなく`U`です。
>
> 矢印についてはUAX #50では回転になっても、「上矢印は上を向いたままにしたい」
> という作品要件があり得ます。これはUnicode判定の誤りとしてではなく、
> アプリ側tailoringとして管理するべきです。
>
> ```python
> VERTICAL_ORIENTATION_OVERRIDES = {
>     "↑": "U",
>     "↓": "U",
> }
> ```
>
> ---
>
> ### 分類モデルの再設計案
>
> 現在の`safe` / `manual_sideways` / `manual_upright`は、異なる概念を1つの
> 値にまとめています。
>
> 次の3軸に分けると保守しやすくなります。
>
> ```python
> @dataclass(frozen=True)
> class VerticalGlyphPlan:
>     unicode_orientation: Literal["U", "R", "Tu", "Tr"]
>     shape_direction: Literal["ttb", "ltr"]
>     transform: Literal["none", "rotate_cw_90", "fit_tcy"]
>     advance_policy: Literal[
>         "vertical_metrics",
>         "fixed_cell",
>         "single_tcy_cell",
>     ]
>     use_vertical_alternate: bool
> ```
>
> 基本ルールは次です。
>
> | VO | 縦字形あり   | 縦字形なし        |
> | -- | ------- | ------------ |
> | U  | TTB・正立  | TTBまたはLTR・正立 |
> | Tu | TTB・縦字形 | LTR・正立・セル中央  |
> | Tr | TTB・縦字形 | LTR・90度回転    |
> | R  | 原則不使用   | LTR・90度回転    |
>
> ここで、`TTB`と`縦字形あり`は同義ではありません。GIDが変わらなくても、
> TTB shapingによって`vmtx`や縦GPOSが利用できる可能性があります。
>
> ---
>
> ### 実装上の優先順位
>
> #### P0
>
> * `{vert:1, vrt2:1}`を廃止
> * Unicode `VerticalOrientation.txt`から分類表を自動生成
> * ハートを`Tu`ではなく`U`として扱う
> * `…` / `‥`が回転後も水平ならtransformを検証
> * 本番SVGが全pathか、一部`<text>`かをドキュメントで確定
>
> #### P1
>
> * graphemeごとのshapeから、同じ配置方式の**run単位shape**へ移行
> * `……`、`‥‥`、連続dash、英単語をunbreakable run化
> * layoutとrenderで別々に計算せず、共通の`VerticalGlyphPlan`を生成して利用
> * font hash・Unicode version・renderer versionをgolden testに記録
>
> #### P2
>
> * 縦中横をfirst-class inline objectとして実装
> * 最初は明示markup
> * 1emセルへのfitとcenterをpath配置で実現
>
> #### P3
>
> * JK Gothic Lと、実際に縦GSUBを持つCJKフォントの2系統でテスト
> * `vchw`等を試す場合は句読点runをまとめてshape
> * JLREQ寄りの記号間隔と、現在の固定grid方式を別モードにする
>
> 総合すると、問題の中心はresvgよりも、**「Unicode上どう向けるべきか」
> 「フォントが何を提供できるか」「不足時にどのpath transformへ落とすか」を
> 分離できていないこと**です。ここを整理すれば、JK Gothic L固定運用でも、
> 別フォント運用でも同じレイアウト基盤を使えるようになります。

### 参考リンク（GPT Pro レビュー内）

- OpenType registered features: <https://learn.microsoft.com/en-us/typography/opentype/spec/features_uz>
- CSS Writing Modes Level 4: <https://www.w3.org/TR/css-writing-modes-4/>
- resvg vertical text issue: <https://github.com/linebender/resvg/issues/890>
- SVG 2 Text: <https://www.w3.org/TR/SVG2/text.html>
- Unicode Vertical Orientation 17.0: <https://www.unicode.org/Public/17.0.0/ucd/VerticalOrientation.txt>
- JLREQ: <https://www.w3.org/TR/jlreq/>
- Chrome Headless: <https://developer.chrome.com/docs/chromium/headless>
- librsvg vertical text bug: <https://gitlab.gnome.org/GNOME/librsvg/-/issues/364>
- Skia Text Overview: <https://skia.org/docs/dev/design/text_overview/>

---

## 3. レビューに対する評価

### 3.1 正しい指摘 (重要度順)

#### A. doc の経路矛盾は本物 ★★★

私の原稿では「safe は resvg にネイティブで描かせる」と書いたが、実際の
コードは `path_renderer` が用意できれば **全 action を `<path>` 化** する
経路だけが本番で動いている。`<text>` フォールバックは `font_path=None` の
時だけ。font は常に渡されるので、現状の本番は **常に path 経路**。

意味するところ:
- resvg の `text-orientation` バグは本番では起きない (resvg は path の
  ラスタライザにしか使われていない)
- `TU_RESVG_OVERRIDE_CHARS` の役割は「resvg バグ回避」ではなく「path 経路で
  `vert/vrt2` 適用 path にするか LTR 素 path にするかの選択」
- 命名を実態に合わせて変える必要がある

#### B. `{"vert": 1, "vrt2": 1}` 同時指定は誤り ★★★

OpenType 仕様上 `vrt2` は `vert` の代替モデル (同時指定するものではない)。
`buffer.direction = "ttb"` にすれば HarfBuzz が縦組み機能を自動適用する
ので明示指定は外すのが正しい。

JK Gothic L では結果同じ (両 feature 共に空) だが、Noto CJK 等の正しい
フォントを使うと挙動が変わる可能性。簡単に直せる。

#### C. ハートは `Tu` ではなく `U` ★★★

Unicode `VerticalOrientation.txt` でハート類は U (Upright)。挙動上は変わら
ない (どちらも path で正立配置) が、意味的に間違っている。意味モデルが
歪むと将来の拡張がブレる。

#### D. `VerticalOrientation.txt` ベースの分類テーブル化 ★★

UAX#50 公式データから生成すれば、`TU_CHARS` / `TR_CHARS` の手書き集合は
不要。`unicodedata.east_asian_width` フォールバックも要らなくなる。
データは UCD のシンプルな range ファイル、数 KB。やる価値あり。

#### E. アプリ tailoring を別レイヤに ★★

「↑↓ は U に倒したい」みたいな**作品要件**は Unicode 判定を書き換えるの
ではなく override table で持つべき、というのは正しい設計指針。

### 3.2 微妙 / 部分同意

#### F. run 単位 shaping への移行 ★

正論ではあるが、JK Gothic L は `vchw` も無いので **現状は性能差が見えない**。
将来別フォントに対応したくなった時に効いてくる。**今は P1 で先送り推奨**。

#### G. 縦中横 (tate-chu-yoko) を first-class に ☆

仕様としては正しいが、ユーザーが要求していない。dialogue 内に `100m` みたい
な表記が出る場面が多いなら入れる価値あり。今は不要。

#### H. SkShaper / Pango 代替 ☆

現状 path 経路の改善余地が十分大きいので、runtime を替えてもメリット薄い。
GPT Pro も「runtime 替えても縦組み正しさは変わらない」と認めている。
**保留**。

### 3.3 チェックすべき仮説 (GPT Pro 指摘で気になった点)

- **`…` `‥` の transform 問題**: 分類は R (rotate) で合っているはずなので、
  もし「縦書きで点が水平に並んで見える」なら transform バグ。原稿では
  「分類問題」と書いたが、実は実装側のバグかもしれない。要画像確認。
- **`vert` lookup が本当に空か**: GID 比較 1 ペアでは断定不十分。GSUB lookup
  coverage を全部列挙して確認すれば、より自信を持って言える。

---

## 4. アクション計画

### 4.1 P0 (即実施推奨、1〜2 時間)

1. doc の経路矛盾を訂正
   - 本番は全 path 経路と明記
   - `<text>` 経路は font 解決失敗時の fallback と明記
2. `bubble/text_render_resvg_hybrid.py` の `_cluster_path_element` 呼び出し
   から `features={"vert": 1, "vrt2": 1}` を撤廃、`direction="ttb"` のみ
   にする
3. `bubble/vertical_uax.py`:
   - ハート類 (`♡♥❤❥❣`) を `TU_CHARS` から外し、`U` 扱いになる経路を作る
     - もしくは新規 `U_FORCE_UPRIGHT_OVERRIDES` で明示
   - `TU_RESVG_OVERRIDE_CHARS` を `MANUAL_UPRIGHT_CHARS` 等にリネーム
     (現在の役割: 「`<path>` で LTR shape + 正立配置」する文字集合)
4. `…` `‥` の現状出力を実画像で確認、必要なら transform を修正
5. JK Gothic L の GSUB lookup を全列挙してフォント能力を doc に正確に記録
   (`fontTools` で簡単に取れる)

### 4.2 P1 (必要に応じて、半日〜1 日)

6. `VerticalOrientation.txt` ベースの自動生成テーブル
   - ビルド時または初回 import 時に `unicodedata` 同等のテーブルを生成
   - 手書きの `TR_CHARS` / `TU_CHARS` 集合は撤去
7. アプリ tailoring の override テーブルを別レイヤとして用意
   ```python
   VERTICAL_ORIENTATION_OVERRIDES: dict[str, VerticalOrientation] = {
       "↑": "U",
       "↓": "U",
   }
   ```
8. `VerticalGlyphPlan` のような分離 dataclass で
   `(orientation, shape_direction, transform, advance_policy,
   use_vertical_alternate)` を保持
9. 連続記号 (`……`、`‥‥`、`──`) を run としてまとめる
   - run 単位の shape を実装
   - run 内部での改行禁止

### 4.3 P2 / 後回し

- 縦中横 first-class (要件待ち)
- runtime 置き換え (メリット不明)
- 縦中横 SVG `text-combine-upright` (resvg 非サポート)

### 4.4 P3 / 余裕があれば

- Noto Sans CJK JP のような「正しい vert/vrt2 を持つフォント」での
  比較テスト
- `vchw` 等の GPOS feature を句読点 run でまとめて shape したときの効果検証
- JLREQ ベースの記号間隔モードと、現在の固定 grid モードを切替可能にする

---

## 5. 次のステップ

1. **画像確認**: 直近の編集で `…` `‥` の縦書き挙動が問題ないか確認
2. **GSUB 全列挙スクリプトを書く**: フォント能力の正確な記録のため
3. **P0 を実装**: doc 訂正 + `vrt2` 撤廃 + ハート再分類 + 命名整理
4. **golden test**: 上記変更前後で出力差分が出ないことを確認
   (JK font では結果同じになるはず。差分が出るなら何かが期待と異なる)
5. **fontTools / HarfBuzz の version 固定**: 再現性の担保

---

## 6. 関連ファイル

- `bubble/vertical_uax.py` — 分類ロジックの本体
- `bubble/text_render_resvg_hybrid.py` — SVG 構築
- `bubble/glyph_paths.py` — HarfBuzz + FontTools で path を取り出す
- `bubble/layout.py` — `compute_text_layout` / `build_text_metrics`
- `bubble/assets.py` — resvg CLI 経由の SVG 描画
- `assets/JKG-L_3.ttf` — 採用フォント
- `docs/ideas/pango_resvg_vertical_validation.md` — Pango/HarfBuzz/Cairo +
  resvg 検証
- `docs/ideas/resvg_hybrid_iteration_log.md` — 採用方式に至った試行錯誤履歴

最終 PNG 例: `out/poc_check/hybrid_cli_final.png`、最終比較:
`out/poc_check/hybrid_misalignment/hybrid_text_final_cmp.png`
