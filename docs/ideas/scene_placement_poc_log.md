# Scene Placement PoC Log

## 目的

画像中の単一人物に対して、`reflow.json + image + masks` から `scene.json` を生成し、既存 renderer に接続できる吹き出し配置 PoC を作る。

初期の狙いは次の 2 点だった。

- `scene` を VLM 1 発推論ではなく、制約付き配置問題として扱う
- `person / face / chest / lower` を避けながら、自然な読み順を持つ text 配置を作る

## 配置ヒューリスティックの整理

最初に、自然な吹き出し配置の言語化を行った。

- 読み順は基本的に右から左
- 同じ側に積むときは上から下
- 顔の近くに置きたいが、近すぎても遠すぎてもよくない
- 顔、胸、股間には重ねたくない
- 続く会話は離しすぎたくない
- 画像の端に張り付きすぎず、顔と端のあいだの適切な帯に置きたい

この整理は [bubble_placement_heuristics.md](/mnt/c/Users/inada/obsidian/base/03_projects/.worktrees/text-bubble/feat-scene-placement-optimization/docs/ideas/bubble_placement_heuristics.md) に記録した。

## PoC v1: Bubble-First Greedy

最初の PoC は `bubble` の外形を先に見積もって、その占有を直接解く形だった。

- 新規 PoC 入口: [poc_scene_place_from_masks.py](/mnt/c/Users/inada/obsidian/base/03_projects/.worktrees/text-bubble/feat-scene-placement-optimization/scripts/poc_scene_place_from_masks.py)
- 初期 solver: [beam_search_scene_solver.py](/mnt/c/Users/inada/obsidian/base/03_projects/.worktrees/text-bubble/feat-scene-placement-optimization/scripts/beam_search_scene_solver.py) の前身

この段階では、

- `N` と `rotated-n` のような固定テンプレート
- `bubble` 矩形ベースの overlap 判定
- greedy 配置

を使っていた。

この方式は実装は簡単だったが、次の問題が出た。

- `bubble` を先に占有物として扱うので自由度を潰しやすい
- merge 可能な bubble に対して外形を先に固定するのが不自然
- 5 文や 2 列ケースで、途中配置が後続を詰ませやすい

## PoC v2: Text-First + Beam Search

次に、配置対象を `bubble` ではなく `text box` に切り替えた。

- hard constraint は `text box` 基準
- `bubble` はレンダリング直前の殻として扱う
- greedy をやめて beam search に変更

この変更により、

- 2 列 reflow のような縦に長い text も扱いやすくなった
- `text` 同士の距離や読み順を直接評価できるようになった
- 「顔の近く」「前の発話に近い」をスコアに入れやすくなった

その後、見た目を詰めるために postprocess も入れた。

- 同じ列の text を機械的に詰める
- 左列が右列より上から始まりすぎるのを抑える
- 顔の近くすぎる位置を避ける
- 右から左、上から下の順を守りやすくする

この段階で見た目はかなり改善したが、探索自体はまだ逐次的で、全体最適感は弱かった。

## PoC v3: CP-SAT への移行

beam search では「全体を見ているようで、実際には見切れていない」ケースが残ったため、`OR-Tools CP-SAT` に移行した。

- solver 本体: [cp_sat_scene_solver.py](/mnt/c/Users/inada/obsidian/base/03_projects/.worktrees/text-bubble/feat-scene-placement-optimization/scripts/cp_sat_scene_solver.py)
- 依存追加: `ortools`

基本構成は次の通り。

- 各発話について `text box` の離散候補を生成する
- 各候補は `slot` と位置の組で表す
- `1 bubble = 1 candidate` を選ぶ 0/1 変数を置く
- overlap や読み順を制約化する
- unary / pairwise / global cost の和を最小化する

これで、少なくとも候補の組合せについては全体最適化になった。

## Slot 固定から Slot 選択へ

CP-SAT の最初の版は、まだ slot テンプレートが固定寄りだった。

- 2 文で片側に寄りすぎる
- ケースによっては右列だけを使い続ける

という違和感が出たため、`slot` 自体を solver が選べるように拡張した。

- `top-right`, `mid-right`, `bottom-right`
- `top-left`, `mid-left`, `bottom-left`

を各 bubble の候補として持ち、そこから全体の組合せを選ぶようにした。

この変更で、

- 2 文では左右分割
- 3 文以上では右列を先に使いつつ左にも渡す

という配置を取りやすくなった。

## 外部 mask 対応

当初は `chest/lower` を `person/face` からヒューリスティック生成していたが、後に `bboxseg` で `person / face / chest / lower` mask が利用可能になったため、PoC を更新した。

- 外部 mask があるときはそれを優先
- 無いときだけヒューリスティック生成に fallback

これにより、顔・胸・下半身の回避がより安定した。

## 主要な評価項目の変遷

### 初期

- 画面外
- 顔 overlap
- 胸 overlap
- 下半身 overlap
- bubble overlap

### text-first 化以降

- text 同士の overlap 禁止
- 顔 / 胸 / 下半身への text overlap 禁止
- `person` overlap penalty
- 顔に近すぎる / 遠すぎる penalty
- 前の発話から遠すぎる penalty
- 右へ戻る / 上へ戻る penalty

### CP-SAT 化以降

- unary cost
  - 端マージン
  - 顔距離
  - 人物 overlap
  - slot との距離
- pairwise cost
  - 同列 gap
  - 列移動 gap
  - 連続発話の距離
  - 左列開始高さ
- global cost
  - 左右配分
  - 横方向スパン
  - 縦方向スパン
  - 文数が多いときの 1 文目の右上寄り

## 画像評価の回し方

最終的に、PoC は次の入力群で確認した。

- `test.png`
- `test1.png`
- `test2.png`
- `test3.png`
- `test4.png`

各画像について `1 文` から `5 文` までの reflow を作り、`font-size 22` の `cp-sat` で `scene.json -> rendered.png` まで生成した。

比較しやすいように、各画像について `overview.png` も作成した。

## 現在の到達点

現時点では、PoC は次を満たしている。

- `reflow.json + image + masks` から `scene.json` を作れる
- 既存 renderer と接続して `rendered.png` まで出せる
- `beam` と `cp-sat` を切り替え可能
- `cp-sat` は `slot selectable` で動作する
- 外部 `chest/lower` mask があれば利用できる
- `1 文目` を右上に寄せる global bias を持てる

## 残課題

まだ PoC の限界もはっきりしている。

- 候補点が粗いので、良い位置が候補集合に入っていないことがある
- `slot` 数だけでは左右の自然さを十分に表現できない
- 画像によってはまだ片側寄りや顔寄りが残る
- bubble merge は未実装
- 単一人物前提で、複数人物・話者切替は未対応

次の改善候補は次の通り。

- slot ごとの候補生成をもっと画像依存にする
- 顔まわりの「ちょうどよい帯」をもっと明示的にモデル化する
- 列構成そのものをもう少し構造化して最適化する
- 最後に bubble merge を入れて text-first 配置と接続する
