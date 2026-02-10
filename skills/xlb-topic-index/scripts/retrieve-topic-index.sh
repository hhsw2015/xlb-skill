#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: retrieve-topic-index.sh \"<xlb input>\" [\"<retrieval query>\"]" >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="${ROOT_DIR}/scripts"
CACHE_DIR="${ROOT_DIR}/cache"
RAW_DIR="${CACHE_DIR}/raw"
VFS_DIR="${CACHE_DIR}/vfs"
INDEX_DIR="${CACHE_DIR}/index"
DATASET_DIR="${CACHE_DIR}/dataset"
ARTIFACT_DIR="${CACHE_DIR}/artifacts"
DISCOVER_CACHE_FILE="${CACHE_DIR}/capabilities.json"

mkdir -p "${RAW_DIR}" "${VFS_DIR}" "${INDEX_DIR}" "${DATASET_DIR}" "${ARTIFACT_DIR}"

INPUT="$1"
RETRIEVAL_QUERY="${2:-}"
REQUIRE_NETWORK_CONFIRMATION="${XLB_REQUIRE_NETWORK_CONFIRMATION:-1}"
NETWORK_CONFIRMED="${XLB_NETWORK_CONFIRMED:-0}"
SHOW_CONFIRM_TEMPLATE="${XLB_SHOW_CONFIRM_TEMPLATE:-1}"
RAW_CACHE_TTL_SEC="${XLB_RAW_CACHE_TTL_SEC:-300}"
AUTO_REFRESH_ON_QUERY="${XLB_AUTO_REFRESH_ON_QUERY:-0}"
STORAGE_PROFILE="${XLB_STORAGE_PROFILE:-minimal}"
OUTPUT_MODE="${XLB_OUTPUT:-auto}"
OPEN_HITS="${XLB_OPEN_HITS:-0}"
OPEN_APP="${XLB_OPEN_APP:-chrome}"
OPEN_LIMIT="${XLB_OPEN_LIMIT:-1}"
OPEN_KEEP_FRAGMENT="${XLB_OPEN_KEEP_FRAGMENT:-0}"
OPEN_DRY_RUN="${XLB_OPEN_DRY_RUN:-0}"
OPEN_DELAY_SEC="${XLB_OPEN_DELAY_SEC:-0.2}"
OPEN_STOP_ON_ERROR="${XLB_OPEN_STOP_ON_ERROR:-0}"
OPEN_VERBOSE="${XLB_OPEN_VERBOSE:-0}"
OPEN_ATLAS_APP_PATH="${XLB_OPEN_ATLAS_APP_PATH:-/Applications/ChatGPT Atlas.app}"

ALLOW_NETWORK_ACTIONS=0
if [[ "${REQUIRE_NETWORK_CONFIRMATION}" != "1" || "${NETWORK_CONFIRMED}" == "1" ]]; then
  ALLOW_NETWORK_ACTIONS=1
fi

RESOLVE_JSON="$(
  PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" resolve-input --input "${INPUT}"
)"
TITLE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["title"])' "${RESOLVE_JSON}")"
HASH="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["hash"])' "${RESOLVE_JSON}")"

SNAPSHOT_ID="snap-${HASH}"
RAW_FILE="${RAW_DIR}/${HASH}.md"
DB_FILE="${INDEX_DIR}/${HASH}.db"
META_FILE="${INDEX_DIR}/${HASH}.meta.json"

# Capability discovery runs each time to support routing decisions.
CAP_JSON="$(
  PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" discover \
    --cache-file "${DISCOVER_CACHE_FILE}" \
    --cache-ttl-sec "${XLB_DISCOVER_CACHE_TTL_SEC:-30}"
)"

if [[ "${XLB_SHOW_CAPABILITIES:-0}" == "1" ]]; then
  echo "${CAP_JSON}" >&2
fi

HAS_EXTERNAL="$(
  python3 -c '
import json,sys
data=json.loads(sys.argv[1])
print("1" if data.get("network_skills") else "0")
' "${CAP_JSON}"
)"

if [[ "${HAS_EXTERNAL}" == "1" && -n "${XLB_EXTERNAL_ROUTE_CMD:-}" && "${ALLOW_NETWORK_ACTIONS}" == "1" ]]; then
  if external_out="$("${XLB_EXTERNAL_ROUTE_CMD}" "${INPUT}" "${RETRIEVAL_QUERY}" 2>/dev/null)"; then
    if [[ -n "${external_out}" ]]; then
      printf "%s\n" "${external_out}"
      exit 0
    fi
  fi
fi

if [[ "${HAS_EXTERNAL}" == "1" && -n "${XLB_EXTERNAL_ROUTE_CMD:-}" && "${ALLOW_NETWORK_ACTIONS}" != "1" ]]; then
  echo "network expansion skipped: set XLB_NETWORK_CONFIRMED=1 to allow external route/prefetch" >&2
fi

RAW_CACHE_EXPIRED=0
if [[ "${AUTO_REFRESH_ON_QUERY}" == "1" && -s "${RAW_FILE}" && "${XLB_FORCE_REFRESH:-0}" != "1" ]]; then
  RAW_CACHE_EXPIRED="$(
    python3 -c '
import os,sys,time
path=sys.argv[1]
ttl_raw=sys.argv[2]
try:
    ttl=float(ttl_raw)
except Exception:
    print("1")
    raise SystemExit(0)
if ttl <= 0:
    print("0")
    raise SystemExit(0)
try:
    age=time.time() - os.path.getmtime(path)
except FileNotFoundError:
    print("1")
    raise SystemExit(0)
print("1" if age >= ttl else "0")
' "${RAW_FILE}" "${RAW_CACHE_TTL_SEC}"
  )"
fi

if [[ ! -s "${RAW_FILE}" || "${XLB_FORCE_REFRESH:-0}" == "1" || "${RAW_CACHE_EXPIRED}" == "1" ]]; then
  "${SCRIPT_DIR}/fetch-topic-index.sh" "${INPUT}" > "${RAW_FILE}"
fi

if [[ "${XLB_FORCE_REINDEX:-0}" == "1" ]]; then
  PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" ingest-if-needed \
    --markdown-file "${RAW_FILE}" \
    --meta-path "${META_FILE}" \
    --title "${TITLE}" \
    --vfs-root "${VFS_DIR}" \
    --dataset-root "${DATASET_DIR}" \
    --storage-profile "${STORAGE_PROFILE}" \
    --snapshot-id "${SNAPSHOT_ID}" \
    --db-path "${DB_FILE}" \
    --force >/dev/null
else
  PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" ingest-if-needed \
    --markdown-file "${RAW_FILE}" \
    --meta-path "${META_FILE}" \
    --title "${TITLE}" \
    --vfs-root "${VFS_DIR}" \
    --dataset-root "${DATASET_DIR}" \
    --storage-profile "${STORAGE_PROFILE}" \
    --snapshot-id "${SNAPSHOT_ID}" \
    --db-path "${DB_FILE}" >/dev/null
fi

if [[ "${STORAGE_PROFILE}" != "full" ]]; then
  STALE_VFS="${VFS_DIR}/${SNAPSHOT_ID}"
  if [[ -d "${STALE_VFS}" ]]; then
    rm -rf "${STALE_VFS}"
  fi
fi

if [[ -z "${RETRIEVAL_QUERY}" ]]; then
  if [[ "${TITLE}" == \?\?* ]]; then
    SUGGEST_QUERY="$(printf '%s' "${TITLE}" | sed 's/^\?\?//;s/^[[:space:]]*//;s/[[:space:]]*$//')"
    PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" topic-suggest \
      --db-path "${DB_FILE}" \
      --query "${SUGGEST_QUERY}" \
      --topic-limit "${XLB_TOPIC_SUGGEST_LIMIT:-10}" \
      --sample-per-topic "${XLB_TOPIC_SUGGEST_SAMPLES:-3}"
    exit 0
  fi
  if [[ "${OUTPUT_MODE}" == "json" ]]; then
    python3 -c '
import json,sys
print(json.dumps({
  "mode": "raw_reference",
  "title": sys.argv[1],
  "hash": sys.argv[2],
  "raw_file": sys.argv[3],
  "meta_file": sys.argv[4],
  "db_path": sys.argv[5],
}, ensure_ascii=False))
' "${TITLE}" "${HASH}" "${RAW_FILE}" "${META_FILE}" "${DB_FILE}"
  else
    cat "${RAW_FILE}"
  fi
  exit 0
fi

if [[ "${XLB_PREFETCH_ARTIFACTS:-0}" == "1" ]]; then
  if [[ "${ALLOW_NETWORK_ACTIONS}" == "1" ]]; then
    PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" prefetch \
      --db-path "${DB_FILE}" \
      --query "${RETRIEVAL_QUERY}" \
      --artifact-root "${ARTIFACT_DIR}" \
      --limit "${XLB_PREFETCH_LIMIT:-10}" \
      --max-workers "${XLB_MAX_WORKERS:-6}" \
      --timeout-sec "${XLB_FETCH_TIMEOUT_SEC:-10}" \
      --max-bytes "${XLB_MAX_BYTES:-5000000}" \
      --html-mode "${XLB_HTML_MODE:-markdown}" \
      --html-converter-bin "${XLB_HTML_CONVERTER_BIN:-}" \
      --html-converter-tool-id "${XLB_HTML_CONVERTER_TOOL_ID:-url-to-markdown}" \
      --html-convert-timeout-sec "${XLB_HTML_CONVERT_TIMEOUT_SEC:-20}" \
      --require-confirmation \
      --network-confirmed >/dev/null
  else
    echo "prefetch skipped: set XLB_NETWORK_CONFIRMED=1 to enable network fetching" >&2
  fi
fi

if [[ "${XLB_ITERATIVE_SEARCH:-0}" == "1" ]]; then
  SEARCH_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/xlb-search.XXXXXX.json")"
  trap 'rm -f "${SEARCH_OUTPUT_FILE}"' EXIT
  PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" iterative-search \
    --db-path "${DB_FILE}" \
    --query "${RETRIEVAL_QUERY}" \
    --limit "${XLB_TOPK:-8}" \
    --max-iter "${XLB_MAX_ITER:-5}" \
    --gain-threshold "${XLB_GAIN_THRESHOLD:-0.05}" \
    --low-gain-rounds "${XLB_LOW_GAIN_ROUNDS:-3}" > "${SEARCH_OUTPUT_FILE}"
else
  SEARCH_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/xlb-search.XXXXXX.json")"
  trap 'rm -f "${SEARCH_OUTPUT_FILE}"' EXIT
  PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" search \
    --db-path "${DB_FILE}" \
    --query "${RETRIEVAL_QUERY}" \
    --limit "${XLB_TOPK:-8}" > "${SEARCH_OUTPUT_FILE}"
fi

if [[ "${ALLOW_NETWORK_ACTIONS}" != "1" && "${SHOW_CONFIRM_TEMPLATE}" == "1" ]]; then
  CONFIRM_ARGS=(
    --input "${INPUT}"
    --query "${RETRIEVAL_QUERY}"
    --hits-json-file "${SEARCH_OUTPUT_FILE}"
  )
  if [[ "${XLB_PREFETCH_ARTIFACTS:-0}" == "1" ]]; then
    CONFIRM_ARGS+=(--prefetch-enabled)
  fi
  if [[ "${HAS_EXTERNAL}" == "1" && -n "${XLB_EXTERNAL_ROUTE_CMD:-}" ]]; then
    CONFIRM_ARGS+=(--has-external-route)
  fi
  PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" confirmation-template "${CONFIRM_ARGS[@]}" >&2 || true
fi

if [[ "${OPEN_HITS}" == "1" ]]; then
  OPEN_ARGS=(
    --hits-json-file "${SEARCH_OUTPUT_FILE}"
    --limit "${OPEN_LIMIT}"
    --app "${OPEN_APP}"
    --atlas-app-path "${OPEN_ATLAS_APP_PATH}"
    --delay-between-sec "${OPEN_DELAY_SEC}"
  )
  if [[ "${OPEN_KEEP_FRAGMENT}" == "1" ]]; then
    OPEN_ARGS+=(--keep-fragment)
  fi
  if [[ "${OPEN_DRY_RUN}" == "1" ]]; then
    OPEN_ARGS+=(--dry-run)
  fi
  if [[ "${OPEN_STOP_ON_ERROR}" == "1" ]]; then
    OPEN_ARGS+=(--stop-on-error)
  fi
  if OPEN_JSON="$(
    PYTHONPATH="${SCRIPT_DIR}" python3 "${SCRIPT_DIR}/xlb_rag_pipeline.py" open-hits "${OPEN_ARGS[@]}"
  )"; then
    if [[ "${OPEN_VERBOSE}" == "1" ]]; then
      echo "${OPEN_JSON}" >&2
    fi
  else
    if [[ "${OPEN_VERBOSE}" == "1" ]]; then
      echo "open-hits failed" >&2
    fi
  fi
fi

cat "${SEARCH_OUTPUT_FILE}"
