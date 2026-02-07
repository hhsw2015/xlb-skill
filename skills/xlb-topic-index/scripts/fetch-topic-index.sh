#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: fetch-topic-index.sh \"<raw input or xlb query>\"" >&2
  exit 2
fi

trim() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

strip_trailing_topic_word() {
  printf '%s' "$1" | sed 's/[[:space:]]*主题[[:space:]]*$//'
}

to_topic_title() {
  local topic
  topic="$(trim "$1")"
  topic="$(strip_trailing_topic_word "$topic")"
  topic="$(trim "$topic")"
  if [[ -z "$topic" ]]; then
    return 1
  fi
  printf '>%s/' "$topic"
}

resolve_title() {
  local input payload topic
  input="$(trim "$1")"

  if [[ -z "$input" ]]; then
    return 1
  fi

  # Explicit passthrough command
  if [[ "$input" == '>'* ]]; then
    printf '%s' "$input"
    return 0
  fi

  # xlb explicit trigger forms: strip trigger, passthrough payload as-is
  if [[ "$input" =~ ^[Xx][Ll][Bb][[:space:]]+(.+)$ ]]; then
    payload="$(trim "${BASH_REMATCH[1]}")"
    if [[ -z "$payload" ]]; then
      return 1
    fi
    printf '%s' "$payload"
    return 0
  fi

  # Implicit trigger: 查询xlb <topic>主题
  if [[ "$input" =~ 查询[[:space:]]*[Xx][Ll][Bb][[:space:]]+(.+)$ ]]; then
    topic="$(trim "${BASH_REMATCH[1]}")"
    to_topic_title "$topic"
    return 0
  fi

  # Fallback: raw passthrough to preserve previous behavior
  printf '%s' "$input"
}

raw_input="$*"
title="$(resolve_title "$raw_input" || true)"

if [[ -z "$(printf '%s' "$title" | tr -d '[:space:]')" ]]; then
  echo "error: empty input" >&2
  exit 2
fi

curl -sS -X POST "http://localhost:5000/getPluginInfo" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "title=${title}" \
  --data-urlencode "url=" \
  --data-urlencode "markdown="
