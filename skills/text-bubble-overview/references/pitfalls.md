# Pitfalls

- `--json` を subcommand の後ろに置くと `No such option: --json` になる
  - `--json` は global option
  - `text-bubble --json assign ...` の形にする

- `resvg` 未導入で既定 renderer を使うと失敗する
  - `--text-renderer resvg-hybrid` と `--bubble-renderer resvg` が既定
  - `resvg` を入れるか、両方 `browser` に切り替える

- `cp-sat` planner で mask が不足すると `scene` / `run` が失敗する
  - `--person-mask` と `--face-mask` は必須
  - `face` が空なら `head` fallback を使うが、両方空だとエラー

- `assign --help` や root `--help` が環境によって重く見えることがある
  - 少なくとも `.venv/bin/text-bubble --version` と最小 `assign` 実行で entrypoint を先に確認すると切り分けしやすい

- browser renderer は Playwright / Chromium 前提
  - `.playwright-browsers` や必要な shared library が足りないと描画で落ちる
  - 詳細は `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/docs/environment.md` を見る
