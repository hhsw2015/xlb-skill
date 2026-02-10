#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: xlb-auto-explore.sh \"<topic or xlb input>\"" >&2
  echo "example: xlb-auto-explore.sh \"vibe coding\"" >&2
  echo "example: xlb-auto-explore.sh \"xlb >vibe coding/:\"" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="${ROOT_DIR}/scripts"
CACHE_DIR="${ROOT_DIR}/cache"
INDEX_DIR_DEFAULT="${CACHE_DIR}/index"
mkdir -p "${CACHE_DIR}" "${INDEX_DIR_DEFAULT}"

INPUT="$*"
EDGE_STRATEGY="${XLB_AUTO_EDGE_STRATEGY:-searchin_command_backlink}"
MAX_STEPS="${XLB_AUTO_MAX_STEPS:-12}"
MAX_DEPTH="${XLB_AUTO_MAX_DEPTH:-4}"
MAX_SECONDS="${XLB_AUTO_MAX_SECONDS:-90}"
MAX_BRANCHING="${XLB_AUTO_MAX_BRANCHING:-6}"
BACKLINK_LIMIT="${XLB_AUTO_BACKLINK_LIMIT:-30}"
BACKLINK_FILTER="${XLB_AUTO_BACKLINK_FILTER:-}"
INCLUDE_OTHER_QUERIES="${XLB_AUTO_INCLUDE_OTHER_QUERIES:-0}"
INCLUDE_BACKLINKS="${XLB_AUTO_INCLUDE_BACKLINKS:-1}"
UPDATE_VISITED="${XLB_AUTO_UPDATE_VISITED:-1}"
NETWORK_CONFIRMED="${XLB_NETWORK_CONFIRMED:-0}"
STORAGE_PROFILE="${XLB_STORAGE_PROFILE:-minimal}"
VISITED_FILE="${XLB_AUTO_VISITED_FILE:-${CACHE_DIR}/visited_exec_titles.json}"
VISITED_TOPICS_FILE="${XLB_AUTO_VISITED_TOPICS_FILE:-${CACHE_DIR}/visited_topic_keys.json}"
INDEX_DIR="${XLB_AUTO_INDEX_DIR:-${INDEX_DIR_DEFAULT}}"

CMD=(
  python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" auto-explore
  --input "${INPUT}"
  --edge-strategy "${EDGE_STRATEGY}"
  --max-steps "${MAX_STEPS}"
  --max-depth "${MAX_DEPTH}"
  --max-seconds "${MAX_SECONDS}"
  --max-branching "${MAX_BRANCHING}"
  --backlink-limit "${BACKLINK_LIMIT}"
  --backlink-filter "${BACKLINK_FILTER}"
  --storage-profile "${STORAGE_PROFILE}"
  --visited-file "${VISITED_FILE}"
  --visited-topics-file "${VISITED_TOPICS_FILE}"
  --index-dir "${INDEX_DIR}"
)

if [[ "${INCLUDE_OTHER_QUERIES}" == "1" ]]; then
  CMD+=(--include-other-queries)
fi
if [[ "${INCLUDE_BACKLINKS}" != "1" ]]; then
  CMD+=(--no-backlinks)
fi
if [[ "${UPDATE_VISITED}" == "1" ]]; then
  CMD+=(--update-visited)
fi
if [[ "${NETWORK_CONFIRMED}" == "1" ]]; then
  CMD+=(--network-confirmed)
fi

PYTHONPATH="${SCRIPT_DIR}" "${CMD[@]}"
