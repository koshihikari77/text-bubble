# Commands

実行ディレクトリ:

- `/mnt/c/Users/inada/obsidian/base/03_projects/text-bubble`

基本確認:

```bash
.venv/bin/text-bubble --version
```

最小の assignment 動作確認:

```bash
.venv/bin/text-bubble -w out/skill_check --json assign --dialogue "テストです"
```

段階実行:

```bash
.venv/bin/text-bubble -w out/run1 assign --dialogue "夜見のどこみてるのー？"
.venv/bin/text-bubble -w out/run1 reflow
.venv/bin/text-bubble -w out/run1 scene \
  -i imgs/00005716.png \
  --person-mask masks/00005716_person_mask.png \
  --face-mask masks/00005716_face_mask.png
.venv/bin/text-bubble -w out/run1 render -o out/result.png
```

一括実行:

```bash
.venv/bin/text-bubble -w out/run1 run \
  -i imgs/00005716.png \
  -o out/result.png \
  --person-mask masks/00005716_person_mask.png \
  --face-mask masks/00005716_face_mask.png \
  --dialogue "夜見のどこみてるのー？"
```

1-shot 推論:

```bash
.venv/bin/text-bubble -w out/run1 full \
  -i imgs/00005716.png \
  -o out/result.png \
  --dialogue "夜見のどこみてるのー？"
```

evaluate:

```bash
.venv/bin/text-bubble -w out/run1 evaluate \
  --rendered out/result.png \
  --server "$TEXT_BUBBLE_SERVER"
```

補足:

- `run` / `render` / `full` は `--text-renderer resvg-hybrid|browser`、`--bubble-renderer resvg|browser` を切り替えられる
- `scene` / `run` は既定 planner が `cp-sat`
- `--json` は global option なので subcommand より前に置く
