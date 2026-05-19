# Key Files

- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/README.md` - 新 CLI の使い方、workspace、planner、renderer の概要
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/pyproject.toml` - `text-bubble` entrypoint と Python 依存
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/bubble/cli.py` - `assign/reflow/scene/render/run/full/evaluate/experimental` の定義
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/bubble/scene_runtime.py` - mask bundle、planner、render pipeline の本体
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/bubble/scene_planners/cp_sat.py` - 標準 planner の制約実装
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/bubble/text_render_resvg_hybrid.py` - 既定 text renderer の実装
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/assets/bubble_assets.json` - bubble type manifest
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/docs/environment.md` - `llama.cpp`、Playwright、`resvg`、モデル配置の環境前提
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/docs/bubble_pipeline.md` - 旧 pipeline を含む処理の流れ
- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble/tests/test_cp_sat_scene_solver.py` - cp-sat scene solver の挙動確認
