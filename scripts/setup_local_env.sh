#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="${UV_BIN:-}"

if [[ -z "${UV_BIN}" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  elif [[ -x "${HOME}/.local/bin/uv" ]]; then
    UV_BIN="${HOME}/.local/bin/uv"
  else
    echo "uv not found. Install uv first or set UV_BIN." >&2
    exit 1
  fi
fi

cd "${ROOT_DIR}"

"${UV_BIN}" venv .venv
"${UV_BIN}" pip install --python .venv/bin/python -r requirements.txt

PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  "${UV_BIN}" run --python .venv/bin/python playwright install chromium

mkdir -p imgs resources out

cat <<'EOF'
Local environment is ready.
- Python venv: .venv
- Playwright browsers: .playwright-browsers
- Working directories: imgs resources out

If this project was moved to a new path such as /notebooks/text-bubble,
re-run this script there to rebuild the local venv for the new location.
EOF
