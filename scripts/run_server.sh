#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_BIN="${SERVER_BIN:-${ROOT_DIR}/llama.cpp/build/bin/llama-server}"
MODEL_PATH="${MODEL_PATH:-${ROOT_DIR}/models/heretic/Qwen3.5-27B-heretic.Q4_K_M.gguf}"
MMPROJ_PATH="${MMPROJ_PATH:-${ROOT_DIR}/models/heretic/Qwen3.5-27B-heretic.mmproj-Q8_0.gguf}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
CTX_SIZE="${CTX_SIZE:-8192}"
MODEL_ALIAS="${MODEL_ALIAS:-heretic}"
THREADS="${THREADS:-8}"
PARALLEL="${PARALLEL:-1}"
GPU_LAYERS="${GPU_LAYERS:-all}"
PAPERSPACE_PUBLIC=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --paperspace-public)
      PAPERSPACE_PUBLIC=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./scripts/run_server.sh [--paperspace-public]

Options:
  --paperspace-public  Listen on 0.0.0.0:6006 and print the Paperspace public URL.
  -h, --help           Show this help.
EOF
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "${PAPERSPACE_PUBLIC}" == "1" ]]; then
  HOST="0.0.0.0"
  PORT="6006"
fi

if [[ ! -x "${SERVER_BIN}" ]]; then
  echo "llama-server not found: ${SERVER_BIN}" >&2
  exit 1
fi

if [[ ! -f "${MODEL_PATH}" ]]; then
  echo "model file not found: ${MODEL_PATH}" >&2
  exit 1
fi

if [[ ! -f "${MMPROJ_PATH}" ]]; then
  echo "mmproj file not found: ${MMPROJ_PATH}" >&2
  exit 1
fi

echo "llama-server URL: http://${HOST}:${PORT}"
echo "llama-server API base: http://${HOST}:${PORT}/v1"
if [[ "${PAPERSPACE_PUBLIC}" == "1" ]]; then
  if [[ -n "${PAPERSPACE_FQDN:-}" ]]; then
    echo "Paperspace URL: https://tensorboard-${PAPERSPACE_FQDN}"
    echo "Paperspace API base: https://tensorboard-${PAPERSPACE_FQDN}/v1"
  else
    echo "Paperspace URL: https://tensorboard-\${PAPERSPACE_FQDN}"
    echo "Paperspace API base: https://tensorboard-\${PAPERSPACE_FQDN}/v1"
  fi
fi

exec "${SERVER_BIN}" \
  --model "${MODEL_PATH}" \
  --mmproj "${MMPROJ_PATH}" \
  --alias "${MODEL_ALIAS}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --ctx-size "${CTX_SIZE}" \
  --threads "${THREADS}" \
  --parallel "${PARALLEL}" \
  --gpu-layers "${GPU_LAYERS}" \
  --flash-attn on \
  --reasoning-format none
