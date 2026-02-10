#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: xlb-open-url.sh \"<url>\" [chrome|dia|atlas|default]" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="${ROOT_DIR}/scripts"

URL="$1"
APP="${2:-${XLB_OPEN_APP:-chrome}}"
KEEP_FRAGMENT="${XLB_OPEN_KEEP_FRAGMENT:-0}"
DRY_RUN="${XLB_OPEN_DRY_RUN:-0}"
ATLAS_APP_PATH="${XLB_OPEN_ATLAS_APP_PATH:-/Applications/ChatGPT Atlas.app}"

ARGS=(
  --url "${URL}"
  --app "${APP}"
  --atlas-app-path "${ATLAS_APP_PATH}"
)
if [[ "${KEEP_FRAGMENT}" == "1" ]]; then
  ARGS+=(--keep-fragment)
fi
if [[ "${DRY_RUN}" == "1" ]]; then
  ARGS+=(--dry-run)
fi

PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" open-url "${ARGS[@]}"
