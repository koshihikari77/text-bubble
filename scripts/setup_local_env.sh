#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="${UV_BIN:-}"
REINSTALL=0
WITH_DEPS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reinstall)
      REINSTALL=1
      shift
      ;;
    --with-deps)
      WITH_DEPS=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./scripts/setup_local_env.sh [--reinstall] [--with-deps]

Options:
  --reinstall   Reinstall the text-bubble tool even if already installed.
  --with-deps   Run Playwright system dependency installer (requires root).
  -h, --help    Show this help.
EOF
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 1
      ;;
  esac
done

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

if [[ "${REINSTALL}" == "1" ]]; then
  "${UV_BIN}" tool install -e . --reinstall
else
  if "${UV_BIN}" tool list | grep -q '^text-bubble '; then
    echo "text-bubble already installed. Use --reinstall to refresh." >&2
  else
    "${UV_BIN}" tool install -e .
  fi
fi

PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
  "${UV_BIN}" tool run --from text-bubble playwright install chromium

if [[ "${WITH_DEPS}" == "1" ]]; then
  PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
    "${UV_BIN}" tool run --from text-bubble playwright install-deps chromium
fi

mkdir -p imgs resources out

cat <<'EOF'
Environment is ready.
- text-bubble command is available
- Playwright browsers: .playwright-browsers
- Working directories: imgs resources out
EOF
