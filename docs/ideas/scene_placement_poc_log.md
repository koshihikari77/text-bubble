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

## Template/Slot 選択から Free-Space Candidate Selection へ

ただし、`slot selectable` 版でも本質的にはまだ「限られた template/slot の中から選ぶ」発想が強かった。

この設計だと、

- `slot` 自体が画像の空き領域を十分に表現できない
- `test2/dialogue5` のように、右側に空きがあっても自然な候補が入っていない
- `5 文なら right 2 / left 3` のような例依存ルールを追加したくなりがち

という問題が残った。

そのため、その後の PoC では `cp-sat` の主対象を `slot/template` から `free-space candidate` に移した。

- 画像全体を scan して `text box` 候補を作る
- person / face / chest / lower / head との関係で候補を落とす
- 外周や人物周辺の seed を加える
- coarse bin ごとに候補を残し、そこから `cp-sat` が全体最適で選ぶ

この段階からは、`slot` は最適化の主役ではなくなった。

- `slot` は debug 表示や大まかな region の説明用に後付け分類する
- objective は `slot 名` より `text box` 同士の幾何関係で評価する

つまり設計としては、

- 旧: `template/slot` を選ぶ solver
- 今: `画像上の自由空間候補` を選ぶ solver

へ移った、という理解が正しい。

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
- `test5.png`
- `test6.png`
- `test7.png`

各画像について `1 文` から `5 文` までの reflow を作り、`font-size 22` の `cp-sat` で `scene.json -> rendered.png` まで生成した。

比較しやすいように、各画像について `overview.png` も作成した。

## 追試: Slot 依存の弱化と全体最適化の見直し

その後、`font22_cp_sat_global_v2_all_images` を起点に、`examples/` を見ながら `cp-sat` を反復的に調整した。

ここで分かったのは、単純な `right_count` や `1 文目は右上` のようなルール追加だけでは、画像ごとの空き領域の違いを吸収しきれないことだった。

そのため `cp-sat` は次の方向に寄せた。

- 候補生成を `slot` 固定中心から、画像全体 scan + 周辺 seed に拡張
- unary / pairwise / global cost を、`slot 名` より `text box` の幾何関係中心に寄せる
- `person overlap` を強めに見て、空きスペースを優先的に使う
- `same column` や `large column shift` の上戻りを明示的に抑える

この変更で、`test2/dialogue5` のような「右にスペースがあるのに左へ詰まる」ケースはかなり改善した。

一方で、`right 2 / left 3` のようなテンプレートを hard-code するのではなく、候補集合と幾何制約の側で自然な 2 列配置を作る方針に切り替えたため、解けるかどうかは候補生成の質に強く依存することも分かった。

## 追試: head mask の追加

次に、`face` の真上の髪部分に `text` が乗る問題に対処するため、`head mask` を追加した。

試した案は 3 段階ある。

1. `head` を soft penalty のみで扱う
2. 自動生成 keepout を作る
3. 外部 `head mask` を使い、`text_box` 側は hard 寄りに扱う

このうち、最終的に採用したのは 3 である。

- `*_head_mask.png` を外部入力として読む
- `text_box` の `head overlap ratio > 0.30` は候補を reject
- `bubble shell` の `head overlap` は引き続き penalty として残す

soft penalty のみでは、`test5/dialogue5` のように `text` が head に深く乗るケースを止めきれなかった。逆に hard reject を入れると候補集合が急に細くなり、一部画像が `INFEASIBLE` になった。

この問題は制約を戻すのではなく、候補保持数を増やすことで解消した。

- `MAX_CANDIDATES_PER_BIN = 8`
- `MAX_CANDIDATES_PER_BUBBLE = 96`

つまり今回の学びは、「head を避ける hard 制約は必要だが、それを支えるだけの候補多様性も同時に必要」という点だった。

## 追試: 「前より上に戻る」退行への対処

途中の一般化で、以前の `font22_cp_sat_global_v3_all_images` では抑えられていた「後続 bubble が前より高い位置へ戻る」退行が一部で再発した。

これに対して、最終的には soft penalty ではなく hard constraint を追加した。

- 同じ column と見なせる場合は、後続 `text_box.top < 前の text_box.top` を禁止
- 同じ side では、一定量以上の upward reset を禁止
- 大きな column shift のときも、強い upward reset を禁止

ただし、これも強くしすぎると `dialogue5` 系で解が消えるので、`head mask` と同様に最終的には「制約を戻す」のではなく「候補保持を増やす」ことで両立させた。

## 今回の最終出力

2026-03-11 時点の PoC 出力は次である。

- 基準比較用: [font22_cp_sat_global_v3_all_images](/mnt/c/Users/inada/obsidian/base/03_projects/.worktrees/text-bubble/feat-scene-placement-optimization/out/font22_cp_sat_global_v3_all_images)
- `head mask` hard 対応後の最終版: [font22_cp_sat_global_v3_headmask_hard_all_images](/mnt/c/Users/inada/obsidian/base/03_projects/.worktrees/text-bubble/feat-scene-placement-optimization/out/font22_cp_sat_global_v3_headmask_hard_all_images)
- manifest: [manifest.json](/mnt/c/Users/inada/obsidian/base/03_projects/.worktrees/text-bubble/feat-scene-placement-optimization/out/font22_cp_sat_global_v3_headmask_hard_all_images/manifest.json)

最終版は `test`, `test1`-`test7` の各画像について `dialogue1`-`dialogue5` を生成しており、合計 40 ケースそろっている。

比較上、特に見やすいのは次のケースである。

- `test/dialogue5`
- `test2/dialogue5`
- `test5/dialogue5`
- `test6/dialogue5`
- `test7/dialogue5`

## 現時点の所感

今回の PoC でかなり明確になったことは次の通り。

- `cp-sat` だけでも、`examples/` に寄せる方向の改善は十分できる
- ただし効くのは重み調整そのものより、候補生成と hard constraint の設計
- `head`, `chest`, `lower` のような critical region は、soft penalty だけでは弱い
- 一方で hard reject は、候補集合が細いとすぐ infeasible を生む
- そのため「critical region は hard 寄り」「代わりに候補多様性を増やす」が今回の有効パターンだった

今回の最終版でもまだ完璧ではない。

- `bubble shell` の軽い `head overlap` は残ることがある
- 左列内での上下関係はかなり改善したが、完全には消えていない
- 画像によっては `person overlap` がまだ大きい bubble が残る
- `head` を避けるため、逆にやや高めの左列に寄るケースもある

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

## やってほしいこと
- examplesに例として吹き出いのはいっている画像を入れているのでこれぐらいのクオリティをめざしたい
- 1. CP-SATでいろいろ試してみてexamplesぐらいを目指す。
    - CP-SATがこれ以上改善できそうになくなったら
- 2. codex自身が画像を見て吹き出しを配置することを試す
    - そのさいnsfw画像を扱うのでmask画像を統合したものにふきだしを配置し、それを確認しながら位置調整する
    - mask画像の統合はpersonから輪郭線をとる。そこにface, chest, lowerを統合みたいな感じでいいのでは
    - 一色じゃわからなければカラーにしてもいいかも
    - codexが修正しやすいようにふきだしとかテキストにidをいれてそれも一緒にrenderingするのもいい
    - rendering -> 確認 -> 位置調整　のループをcodexが回す
    - examplesのクオリティに近づける
  3. 1,2が終わったらさらにいいアルゴリズムもしくは方法がないか考えて試す。
    - 古典的なアルゴリズムでもいいし、codexのagent loopでもいい

## 2026-03-13 時点の扱い

- `cp-sat` は引き続きこのブランチの主対象
- `scene` 周りの runtime 共通化と local worker 化は本線に寄せる
- `codex-first` / `cp-sat-codex` は PoC として残す
- `Codex` は board / mask / edit JSON の流れまでは整備したが、配置品質はまだ不安定
- そのため Codex 系は production path にはまだ入れず、`scripts/poc_scene_place_from_masks.py` 系の experimental 扱いにとどめる
