---
name: xlb-topic-index
description: Use when user input contains xlb topic queries (for example "xlb >vibe coding/vib", "xlb ??vibe coding", or "查询xlb vibe coding主题") and the task is to fetch Markdown index from local getPluginInfo API.
---

# Topic Index Command

## Overview
Resolve `xlb` query phrases into a `title` command for the local API, then return Markdown index content.

## When to Use
- User input contains `xlb` as a query trigger.
- User gives `>.../` command directly and expects passthrough.
- User asks for topic index/navigation instead of narrative explanation.
- User wants direct Markdown output from the knowledge source.

## Interaction Rule
- If input matches any trigger pattern below, execute directly without extra questions.
- If input mentions `xlb` but no topic can be extracted, ask:
  - `请发送你要查询的指令，例如：xlb >Vibe Coding/AI超元域 或 xlb ??Vibe Coding`

## Trigger Mapping
- `xlb >vibe coding/vib` -> `title=>vibe coding/vib` (strip `xlb `, payload passthrough)
- `xlb ??vibe coding` -> `title=??vibe coding` (strip `xlb `, payload passthrough)
- `查询xlb vibe coding主题` -> `title=>vibe coding/`
- `>AI Model/` -> `title=>AI Model/` (direct passthrough)

## Execute
Run:

```bash
skills/xlb-topic-index/scripts/fetch-topic-index.sh "xlb ??Vibe Coding"
```

The script performs:
- `POST http://localhost:5000/getPluginInfo`
- form fields:
  - `title=<resolved command>`
  - `url=`
  - `markdown=`

## Output Rule
- Return API response Markdown directly.
- Do not paraphrase unless the user asks for analysis.

## Error Handling
- API unreachable: report connection failure and suggest checking local service on port `5000`.
- Empty response: return a short warning and ask whether to retry with a broader command (for example `>Vibe Coding/`).
- Empty input only: ask user to provide any non-empty title command.
