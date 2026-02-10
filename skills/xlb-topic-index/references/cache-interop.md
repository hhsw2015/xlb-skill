# XLB Cache Interop Contract

This file defines how other skills can consume `xlb-topic-index` cache artifacts.

## 1. Resolve Input to Cache Key

```bash
python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py resolve-input \
  --input "xlb >vibe coding/coding"
```

Returns JSON:
- `title`: resolved query title sent to API
- `hash`: stable cache key (first 16 chars of sha1(title))

## 2. Cache Paths

Given `hash=<HASH>`:
- Raw markdown: `skills/xlb-topic-index/cache/raw/<HASH>.md`
- Meta: `skills/xlb-topic-index/cache/index/<HASH>.meta.json`
- Index DB: value from `db_path` in meta
- Nodes JSONL: value from `nodes_jsonl` in meta
- Topics JSON: value from `topics_json` in meta
- Navigation JSON: value from `navigation_json` in meta
- Optional VFS root (only `storage_profile=full`): value from `vfs_base` in meta

## 3. Read Contract Programmatically

```bash
python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py describe-cache \
  --meta-path "skills/xlb-topic-index/cache/index/<HASH>.meta.json" \
  --format json
```

## 4. Query Existing Index

```bash
python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py search \
  --db-path "<db_path from meta>" \
  --query "codex cli" \
  --limit 8
```

## 5. VFS Conventions

- Default storage profile is `minimal`, so VFS is not materialized unless explicitly enabled.
- When `storage_profile=full`:
  - `data_structure.md`: section-level index
  - `manifest.json`: machine-readable section summary
  - `*.link.md`: link node with metadata + excerpt
  - `*.query.txt`: expandable query command

## 5.1 JSONL Conventions (recommended)

- `nodes_jsonl` is one JSON object per line with fields:
  - `node_id`, `node_type`, `topic`, `section`, `title`, `content`, `url`, `query_cmd`, `source_title`
- Example file search:

```bash
rg -n "\"topic\": \"Vibe Coding\"|\"title\": \".*Codex\"" "<nodes_jsonl from meta>"
```

## 5.2 Navigation Conventions (auto exploration)

- `navigation_json` contains three arrays:
  - `topic_navigation`: related-topic edges (usually from `searchin`)
  - `knowledge_search`: executable search commands (usually from `command`)
  - `other_queries`: uncategorized query edges
- Each edge item includes:
  - `query_cmd`: source markdown command text
  - `query_exec_title`: API-ready command title (can be passed to `title=...`)
  - `query_kind`, `query_source`, `topic`, `section`

Example follow-up from an external skill:

```bash
skills/xlb-topic-index/scripts/retrieve-topic-index.sh "xlb >AI Model/"
skills/xlb-topic-index/scripts/retrieve-topic-index.sh "xlb >vibe coding/vibe"
```

For programmatic callers that prefer stable JSON responses:

```bash
XLB_OUTPUT=json skills/xlb-topic-index/scripts/retrieve-topic-index.sh "xlb >Vibe Coding/"
```

## 6. Artifact Conventions

- Prefetch cache root: `skills/xlb-topic-index/cache/artifacts/`
- File naming: `<url_hash>.<ext>` with `<url_hash>` from URL SHA-based short hash
- HTML links are normalized into:
  - Markdown: `.md` (default)
  - Text: `.txt` (when `XLB_HTML_MODE=text`)
- Sidecar metadata: `<url_hash>.meta.json`
  - `artifact_kind`: `html-markdown` / `html-text` / `binary`
  - `content_type`, `bytes`, `updated_at`

## 6.1 Built-in URL Opener (no external script dependency)

- Open one URL in local app:

```bash
skills/xlb-topic-index/scripts/xlb-open-url.sh "https://example.com" chrome

# or:
python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py open-url \
  --url "https://example.com" \
  --app chrome
```

- Open top links from search result JSON:

```bash
python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py open-hits \
  --hits-json-file /tmp/xlb-search.json \
  --app atlas \
  --limit 3
```

- Wire into retrieval flow:

```bash
XLB_OPEN_HITS=1 \
XLB_OPEN_APP=dia \
XLB_OPEN_LIMIT=2 \
skills/xlb-topic-index/scripts/retrieve-topic-index.sh "<input>" "<query>"
```

## 7. Update Policy

Use:

```bash
skills/xlb-topic-index/scripts/retrieve-topic-index.sh "<input>" "<query>"
```

This performs:
1. raw refresh (if needed)
2. incremental ingest (skip when raw hash unchanged)
3. compact dataset export (`nodes_jsonl` + `topics_json`)
4. search (and optional concurrent prefetch)

## 8. Suggested Auto-Exploration Loop (for external skills)

1. Call:
   - `skills/xlb-topic-index/scripts/retrieve-topic-index.sh "xlb ??<seed>"`
2. Pick one `entry_input` (topic candidate), then call:
   - `skills/xlb-topic-index/scripts/retrieve-topic-index.sh "<entry_input>"`
3. Read `navigation_json` from meta:
   - choose `topic_navigation[*].query_exec_title` to hop topics
   - choose `knowledge_search[*].query_exec_title` to deepen current topic
   - optional fast edge views:
     - `skills/xlb-topic-index/scripts/retrieve-topic-index.sh "xlb ><topic>/searchin:"`
     - `skills/xlb-topic-index/scripts/retrieve-topic-index.sh "xlb ><topic>/command:"`
4. Stop when your skill’s confidence/coverage threshold is satisfied.

Or use built-in helper:

```bash
python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py explore-next \
  --meta-path "skills/xlb-topic-index/cache/index/<HASH>.meta.json" \
  --strategy topic_first \
  --dry-run
```

- remove `--dry-run` to execute selected edge via `retrieve-topic-index.sh`
- add `--visited-file /tmp/xlb-visited.json --update-visited` for de-dup traversal

Full loop with budgets and priority:

```bash
python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py explore-loop \
  --seed-input "xlb >vibe coding/:" \
  --edge-strategy searchin_command_backlink \
  --max-steps 12 \
  --max-depth 4 \
  --max-seconds 90 \
  --visited-file /tmp/xlb-visited.json \
  --visited-topics-file /tmp/xlb-visited-topics.json \
  --update-visited
```

- loop policy default: `searchin -> command -> backlink`
- stop policy: step/depth/time budget
- dedupe policy: visited exec-title + visited topic key

One-click wrapper:

```bash
skills/xlb-topic-index/scripts/xlb-auto-explore.sh "vibe coding"
```

Supported inputs:
- bare topic: `vibe coding`
- xlb topic command: `xlb >vibe coding/:`
- shortcut phrase: `xlb auto vibe coding`

## 9. Graph Backlinks and Neighbors (`->topic/:`)

Use this when an external skill wants graph-style upstream/downstream traversal without hitting API:

```bash
python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py graph-neighbors \
  --index-dir "skills/xlb-topic-index/cache/index" \
  --target-title "->vibe coding/:" \
  --limit 100
```

Important:
- `xlb ->...:` is treated as a server command and should be passed through to API by `retrieve-topic-index.sh`.
- Local graph query is explicit via `graph-neighbors` command above.

Returned JSON includes:
- `upstream_topics`: topics that reference target in their query edges
- `inbound_edges`: raw inbound edges
- `outbound_edges`: edges emitted by target topic
- `follow_up_inputs`: executable next inputs (`xlb >...`) for continued traversal

Optional edge filter:

```bash
python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py graph-neighbors \
  --index-dir "skills/xlb-topic-index/cache/index" \
  --target-title "->vibe coding/:" \
  --query-filter "codex cli"
```

Iterative mode (stop strategy):

```bash
XLB_ITERATIVE_SEARCH=1 \
XLB_MAX_ITER=5 \
XLB_GAIN_THRESHOLD=0.05 \
XLB_LOW_GAIN_ROUNDS=3 \
skills/xlb-topic-index/scripts/retrieve-topic-index.sh "<input>" "<query>"
```
