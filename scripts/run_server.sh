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
