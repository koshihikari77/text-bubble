# shout_rect 再設計メモ

## 対象

- 対象は `shout_rect_pointed`
- 対象は `shout_rect_pointed_drop`
- 対象は `shout_rect_pointed_kink`

## 非対象

- `default` は変更しない
- `ellipse` は変更しない
- `square` は変更しない
- `narration` は変更しない
- `wavy`
- `wavy_fine`
- `wavy_polygon`

このメモは `shout_rect` 系だけの再設計方針をまとめる。既存の `default` や `wavy` 系の描画ロジックはここでは触らない。

## 問題

現状の `shout_rect` 系は、固定 shape を伸縮する発想が残っていて、以下の問題を起こしている。

- `text box` ではなく固定 shape 側が主役になっている
- 頂点や中間点が `text box` の内側に入る
- 文の長さで見た目の余白が暴れる
- `viewBox -> warp` によって頂点位置と曲率が壊れる
- 数値確認すると `text fits bubble alpha = False` になる

## 前提

- 配置対象の基準は `text box`
- 余白量の基準は `font size`
- 文の長さでは余白を変えない
- bubble は `text box` の外側にあるべき
- 頂点や中間点は `text box` 内に入ってはいけない
- 後段の `warp` で形を壊さない
- `shout_rect` は最終サイズ上で直接 path を作る

## 外形生成モデル

1. `text_bbox` を取る
2. `font size` 基準で外側基準矩形 `frame` を作る
3. 頂点は `frame` の四隅付近に置く
4. 中間点は各辺ごとに `frame` の辺上の点から text 側へ食い込ませる
5. 各セグメントを `Q` で結ぶ
6. その path をそのまま描画する

重要なのは順番で、`shape -> text` ではなく `text -> frame -> shape` にすること。

## frame の定義

`frame` は `text_bbox` に対して `font size` 基準の padding を足した矩形とする。

- 左右余白: `pad_x = ax * font_size`
- 上下余白: `pad_y = ay * font_size`

`frame` は bubble の禁止境界ではなく、頂点と中間点の基準となる外側矩形である。

## 頂点

- 頂点は `frame` の四隅付近に置く
- 少しだけ deterministic jitter を許容する
- ただし必ず `text_bbox` の外側に置く
- jitter を入れても `text_bbox` 内へ入る頂点は禁止

## 中間点

- 中間点は各辺ごとに `frame` の辺上の点から text 側へ食い込ませる
- 上下左右で深さを変えてよい
- ただし食い込み先は `text_bbox` を超えてはいけない
- 中間点個数は 1 または 2 を許容してよい
- 個数や辺上位置は deterministic seed で揺らしてよい
- ただしどの中間点も `text_bbox` 内に入ってはいけない

## 曲線

各辺はセグメント単位で `Q` を使って結ぶ。

使い分ける曲げ方:

- `smooth`
- `drop`
- `kink`

ルール:

- 同じ bubble の中で `smooth` と `kink` が混ざってよい
- ただし各線分は独立
- 片方を曲げたから片方を直線にする、はしない

## drop の具体ルール

- ベースは縦長長方形
- 上下の食い込みは見えやすく強め
- 左右も頂点として見える程度に入れる
- ただし中間点は `text box` の中へ入れない
- 同じ bubble の中で普通の曲線と kink 強めが混ざってよい

## ランダム性

ランダム性は deterministic seed ベースに限定する。

入れてよいもの:

- 頂点位置の微揺れ
- 中間点の辺上位置
- 中間点個数
- 各セグメントの `smooth/drop/kink` 選択

制約:

- どの頂点も `text_bbox` 内に入らない
- どの中間点も `text_bbox` 内に入らない
- bubble 全体の読みやすさを壊さない

## 余白

- 初期 bubble サイズは `font size` 基準だけで決める
- `text box` 比例の余白は使わない
- `alpha-fit` の倍率拡大は使わない
- 必要なら辺ごとの加算補正にする

例:

- 上辺だけ `+n px`
- 左辺だけ `+n px`

ただし全体倍率拡大はしない。

## 描画方針

- `shout_rect` 系だけ direct-size procedural path を使う
- 固定 asset を後で変形しない
- `viewBox -> warp` は使わない
- 線幅は non-scaling を維持する
- `shout` は太めでもよいが、`shout_rect` 系は別管理でよい

## 実装方針

- path 生成は辺ごとに分ける
- 例:
  - `top_edge(...)`
  - `right_edge(...)`
  - `bottom_edge(...)`
  - `left_edge(...)`
- 各 edge は `text_bbox` と `frame` を参照して点列を作る
- 最後に 1 本の path にまとめる

## 完了条件

以下を満たしたら `shout_rect_pointed_drop` の再設計は妥当とみなす。

- 頂点が `text_bbox` 内に入らない
- 中間点が `text_bbox` 内に入らない
- 文の長さで余白が暴れない
- `drop/kink` の見え方が左右上下で破綻しない
- `default` と `wavy` の描画は変更しない

## 現状実装メモ

この節は、2026-03-24 時点でこのセッション中に実際に触った `shout_rect` 実装の記録を残すためのもの。完成案ではなく、現状把握用。

### 現在の対象 asset

- `shout_rect_pointed`
- `shout_rect_pointed_drop`
- `shout_rect_pointed_kink`

### 現在の manifest パラメータ

共通:

- `stroke_width = 2.3`
- `safe_inset.left = 0.15`
- `safe_inset.right = 0.15`
- `safe_inset.top = 0.17`
- `safe_inset.bottom = 0.17`
- `safe_padding.left = 1.3`
- `safe_padding.right = 1.3`
- `safe_padding.top = 0.8`
- `safe_padding.bottom = 0.8`

`shout_rect_pointed`:

- `view_box = [0, 0, 360, 440]`
- `inset_left = 62`
- `inset_right = 48`
- `inset_top = 88`
- `inset_bottom = 96`
- `top_depth = 10`
- `bottom_depth = 9`
- `left_depth = 46`
- `right_depth = 40`
- `pull = 0.54`
- `bow = 28`
- `shoulder = 0.2`
- `side_bow = 20`
- `side_shoulder = 0.42`
- `midpoint_count_min = 1`
- `midpoint_count_max = 1`
- `midpoint_tangent_jitter = 10`
- `midpoint_depth_jitter = 6`
- `bottom_midpoint_vertex_bias = 0.5`
- `corner_tangent_jitter = 8`
- `corner_inward_jitter = 6`

`shout_rect_pointed_drop`:

- `view_box = [0, 0, 360, 440]`
- `inset_left = 62`
- `inset_right = 48`
- `inset_top = 76`
- `inset_bottom = 84`
- `top_depth = 10`
- `bottom_depth = 9`
- `left_depth = 20`
- `right_depth = 16`
- `pull = 0.78`
- `bow = 10`
- `shoulder = 0.08`
- `side_bow = 10`
- `side_shoulder = 0.30`
- `midpoint_count_min = 1`
- `midpoint_count_max = 2`
- `midpoint_tangent_jitter = 18`
- `midpoint_depth_jitter = 10`
- `bottom_midpoint_vertex_bias = 0.5`
- `corner_tangent_jitter = 8`
- `corner_inward_jitter = 6`

`shout_rect_pointed_kink`:

- `view_box = [0, 0, 360, 440]`
- `inset_left = 62`
- `inset_right = 48`
- `inset_top = 76`
- `inset_bottom = 84`
- `top_depth = 10`
- `bottom_depth = 9`
- `left_depth = 20`
- `right_depth = 16`
- `pull = 0.58`
- `bow = 24`
- `shoulder = 0.18`
- `side_bow = 18`
- `side_shoulder = 0.34`
- `midpoint_count_min = 1`
- `midpoint_count_max = 2`
- `midpoint_tangent_jitter = 18`
- `midpoint_depth_jitter = 10`
- `bottom_midpoint_vertex_bias = 0.5`
- `corner_tangent_jitter = 8`
- `corner_inward_jitter = 6`

### セッション中の実装変遷

#### 1. 固定 viewBox ベース

最初の `shout_rect` は、固定 `viewBox` 上に

- 四隅の頂点
- 上下左右の中間点

を置いて、それを `Q` でつなぐ実装だった。

問題:

- `text box` を見ていない
- 頂点や中間点が `text box` の内側に入る
- 文長で見た目の余白が暴れる

#### 2. `kink` 強化

`drop` と `kink` では、セグメントごとに `pull` と `bow` を変えて、

- `smooth`
- `drop`
- `kink`

の差を強める調整を何度か行った。

このときの考え方:

- `midpoints == 1` のときは `kink` を強く
- 左右辺でも `kink` が見えるように side 補正を強める

ただし、shape 生成の基準自体が固定 `viewBox` だったので、根本解決にはならなかった。

### `kink` の詳細メモ

このセッションで一番揉めたのは `kink` の作り方だった。ここでは現状実装の考え方と、どこがズレたかを明示する。

#### 現状の `kink` 実装

現状の `kink` は、各辺を

- `corner -> midpoint`
- `midpoint -> next corner`

の 2 本以上の `Q` セグメントでつないでいる。

`smooth` / `drop` / `kink` の違いは、主に各セグメントの制御点計算に使う

- `pull`
- `bow`

の係数差で出している。

使っている主関数:

- `_segment_control_point(start, end, pull, bow)`
- `_curved_pointed_rect_path(...)`
- `_build_direct_shout_rect_geometry(...)`

#### `pull` の意味

`pull` は、制御点を

- 始点寄りに置くか
- 終点寄りに置くか

を決める係数。

大きいほど制御点は終点寄りに寄る。

#### `bow` の意味

`bow` は、始点と終点を結ぶ線分に対する法線方向のふくらみ量。

大きいほど外へ張る。

#### `smooth`

`smooth` は

- `pull` をそのまま使う
- `bow` もそのまま使う

という扱いで、前半と後半をほぼ同じ曲率でつなぐ。

見た目:

- なめらか
- 中間点が頂点というより、曲線の途中に見えやすい

#### `drop`

`drop` は、もともとは

- 中間点へ向かって少し強く落ちる

を狙っていた。

ただし途中で実装がぶれて、

- `corner -> midpoint` が直線っぽくなりすぎる
- `midpoint -> next corner` だけ曲がる

ような失敗が一度起きた。

これはユーザーの意図と違っていて、

- 各線分は独立
- 片側を曲げたから片側を直線にする、はしない

が正しい整理だった。

最終的には、

- 各 `Q` は独立
- ただし `midpoints == 1` のときは `kink` を強める

方向に修正した。

#### `kink`

`kink` は、

- 同じ bubble の中で
  - 普通の曲線
  - 強く折れ込む曲線
  が混ざる

という意図で作っていた。

このセッション中には 2 種類の意味が混ざっていた。

1. 辺の中で片側だけ強く曲げる
2. 各線分ごとに独立して曲げ方を変える

最終的に正しい整理は `2` だった。

つまり、

- `corner -> midpoint`
- `midpoint -> next corner`

の各 `Q` がそれぞれ独立して `kink` になれるべきで、
「前半を直線気味にしたから後半を強くする」といった連動は不要だった。

#### セッション中にやった `kink` 調整

特に `midpoints == 1` のときに `kink` が弱く見えたため、`drop` / `kink` では次のような係数強化を入れた。

概念的には:

- `entering_bow` を増やす
- `leaving_bow` をさらに増やす
- `pull` は少しだけ始点側へ戻す

目的:

- 中間点 1 個のときでも中間点が頂点っぽく見えるようにする
- 左右辺でも `kink` が見えるようにする

ただしこれはあくまで「曲率の調整」であって、幾何の正しさを保証するものではない。

#### 左右辺の `kink`

左右辺が頂点に見えない問題に対しては、何度も調整が入った。

このときにわかったこと:

- 上下と左右で見え方が違う
- 同じ `Q-Q` でも、左右辺は変位が埋もれやすい
- だから左右だけ `bow` を強めたくなる

ただし本質は `kink` の強弱ではなく、

- その中間点が `text box` に対してどこにあるか

だった。

要するに、

- 左右辺の `kink` が見えない

の原因は純粋な曲率不足ではなく、

- 座標配置自体が悪い

ことも大きかった。

#### `mixed-kink`

`mixed-kink` は、

- セグメントごとに `smooth` と `kink` を混ぜる

モードとして入れた。

意図:

- 同じ bubble の中に、普通の辺と強く折れる辺が共存する感じを出す

deterministic seed があるときは、

- `rng.random() < 0.55`

のような条件で各セグメントのスタイルを決める。

seed がないときは、

- 辺 index と midpoint index の偶奇

で決めるようにした。

#### `kink` 実装の問題点

このセッション時点の `kink` は、曲率の味付けとしては使えても、次の問題を抱えている。

- 座標制約より曲率調整が先に来ている
- `text box` 外側制約を満たしていなくても `kink` が作れてしまう
- 中間点の位置が悪いと、どれだけ `kink` を強くしても見た目は破綻する
- `kink` の良し悪しを geometry ではなく印象で調整してしまいがち

結論として、

- `kink` は大事
- でも `kink` を詰める前に、corner / midpoint の座標制約を text 基準で固める必要がある

#### 次回の `kink` 実装ルール

次に作り直すときは、`kink` について以下を守る。

- `kink` は geometry 制約の後段で効かせる
- 先に `text box` の外側に corner / midpoint を確定させる
- その後で各セグメントの `pull / bow` を変えて `smooth/drop/kink` を作る
- 各線分は独立
- 片方の線分の都合で他方を直線化しない
- 左右辺だけ別係数にしてよいが、座標制約は共通

#### 3. `font size` 基準の padding

`shout_rect` 系については

- `text box` 比例の余白

ではなく

- `font size` 基準の余白

に寄せるため、`safe_padding` を `em` ベースにした。

この時点の方針:

- 左右 `1.3em`
- 上下 `0.8em`
- `min_px/max_px` の clamp は使わない

#### 4. `alpha-fit` の無効化

`shout_rect` 系だけは `alpha-fit` を切った。

理由:

- 文の長さに応じた倍率拡大が入る
- そのせいで余白が一定にならない

このセッション中の render では、

- `prepared.plan.bubble_type.startswith("shout_rect")`

のときは `_fit_prepared_bubble_to_alpha()` を素通しする形にした。

#### 5. direct-size procedural path の試作

途中で `shout_rect` 系だけ、

- 最終 `bubble_width / bubble_height`
- `padding_left/right/top/bottom`

を受けて直接 path を作る実装を試した。

追加した補助ロジック:

- `_edge_midpoints_direct(...)`
- `_direct_text_bounds(...)`
- `_path_from_shout_rect_layout(...)`
- `_clamp_direct_control_point(...)`
- `_build_direct_shout_rect_geometry(...)`

方向性:

- `warp` を避ける
- `padding` から `text box` を復元する
- そこから corner / midpoint / control point を置く

ただし、この試作もまだ `text -> frame -> shape` を完全に守れていない。

### 現状の失敗点

このセッションで数値確認した結果、現状の `shout_rect_pointed_drop` は全サンプルで

- `text fits bubble alpha = False`

だった。

つまり現状実装は、見た目以前に幾何として壊れている。

失敗点を整理すると:

- `text box` を shape 生成の主役にし切れていない
- 依然として `bubble box` 側の都合が強い
- 頂点と中間点の制約が弱く、`text box` 内侵入を止められていない
- `kink` の見た目調整はしても、座標制約が壊れているので意味が薄い

### このメモの扱い

この節は「いま何が入っているか」の記録であり、そのまま採用すべき設計ではない。

次の実装では、この現状実装を前提に微調整するのではなく、

- `text -> frame -> shape`

へちゃんと作り直すことを前提にする。
