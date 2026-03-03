#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/models/heretic}"
HF_REPO="${HF_REPO:-mradermacher/Qwen3.5-27B-heretic-GGUF}"
MODEL_FILE="${MODEL_FILE:-Qwen3.5-27B-heretic.Q4_K_M.gguf}"
MMPROJ_FILE="${MMPROJ_FILE:-Qwen3.5-27B-heretic.mmproj-Q8_0.gguf}"
BASE_URL="https://huggingface.co/${HF_REPO}/resolve/main"

mkdir -p "${OUT_DIR}"

curl_args=(
  --fail
  --location
  --retry 5
  --continue-at -
)

if [[ -n "${HF_TOKEN:-}" ]]; then
  curl_args+=(-H "Authorization: Bearer ${HF_TOKEN}")
fi

download() {
  local filename="$1"
  local output_path="${OUT_DIR}/${filename}"

  echo "downloading ${filename}"
  curl "${curl_args[@]}" \
    --output "${output_path}" \
    "${BASE_URL}/${filename}"
}

download "${MODEL_FILE}"
download "${MMPROJ_FILE}"

echo
echo "model saved under ${OUT_DIR}"
