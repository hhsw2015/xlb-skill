#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import platform
import re
import shutil
import shlex
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

PIPELINE_VERSION = "8"


@dataclass
class Node:
    node_id: str
    node_type: str
    topic: str
    section: str
    title: str
    content: str
    url: str = ""
    query_cmd: str = ""
    query_exec_title: str = ""
    query_kind: str = ""
    query_source: str = ""
    source_title: str = ""


def _clean_label(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text or "", flags=re.IGNORECASE | re.DOTALL)
    cleaned = unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _hash(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def title_hash(title: str) -> str:
    return _sha1_text(title)[:16]


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "item"


def _split_url_and_anchor(text: str) -> tuple[str, str]:
    raw = text.strip()
    if "#" not in raw:
        return raw, ""
    url, anchor = raw.split("#", 1)
    return url.strip(), anchor.strip()


def _join_section_path(section: str, group: str) -> str:
    base = (section or "").strip()
    grp = (group or "").strip()
    if not grp:
        return base or "root"
    if not base:
        return grp
    return f"{base}/{grp}"


def resolve_title_from_input(raw_input: str) -> str:
    text = raw_input.strip()
    if not text:
        raise ValueError("empty input")

    if text.startswith(">"):
        return text

    m = re.match(r"^[Xx][Ll][Bb]\s+(.+)$", text)
    if m:
        payload = m.group(1).strip()
        if not payload:
            raise ValueError("empty xlb payload")
        return payload

    m = re.match(r"^查询\s*[Xx][Ll][Bb]\s+(.+)$", text)
    if m:
        topic = re.sub(r"\s*主题\s*$", "", m.group(1).strip())
        if not topic:
            raise ValueError("empty implicit topic")
        return f">{topic}/"

    return text


def _normalize_query_exec_title(raw_query: str, *, section: str = "", fallback_title: str = "") -> str:
    value = (raw_query or "").strip()
    if not value:
        value = (fallback_title or "").strip()
        if value:
            value = f">{value}"
    if not value:
        return ""

    m = re.search(r"\(\s*((?:\?\?|>{1,2})[^)]+)\s*\)", value)
    if m:
        value = m.group(1).strip()

    if value.startswith(">>"):
        value = ">" + value[2:].lstrip()

    if value.startswith(">"):
        body = value[1:].strip()
        if body and "/" not in body and not body.endswith("/"):
            return f">{body}/"
        return f">{body}" if body else ">"

    if value.startswith("??"):
        return value

    sec = (section or "").strip().lower()
    if sec.startswith("searchin"):
        title = _clean_label(fallback_title)
        if title:
            return f">{title}/"
    return ""


def _classify_query_kind(section: str, exec_title: str) -> str:
    sec = (section or "").strip().lower()
    if sec.startswith("searchin"):
        return "topic_nav"
    if sec.startswith("command"):
        return "kb_search"
    if exec_title.startswith("??"):
        return "topic_lookup"
    if exec_title.startswith(">"):
        return "query"
    return "unknown"


def _is_query_payload(payload_raw: str) -> bool:
    if payload_raw.startswith((">", ">>", "??")):
        return True
    return bool(re.search(r"\(\s*(?:\?\?|>{1,2})[^)]+\s*\)", payload_raw))


def parse_markdown_to_nodes(markdown: str, source_title: str = "") -> list[Node]:
    nodes: list[Node] = []
    topic = ""
    section = "root"
    current_group = ""
    pending_title = ""

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("# "):
            topic = _clean_label(line[2:].strip())
            continue

        if line.startswith("## "):
            section = _clean_label(line[3:].strip().rstrip(":"))
            current_group = ""
            pending_title = ""
            continue

        if line.startswith("### "):
            payload_raw = line[4:].strip()
            payload = _clean_label(payload_raw)
            section_path = _join_section_path(section, current_group)
            if payload_raw.startswith(("http://", "https://")):
                url, anchor = _split_url_and_anchor(payload_raw)
                anchor = _clean_label(anchor)
                title = _clean_label(pending_title or anchor or url)
                nodes.append(
                    Node(
                        node_id=_hash(topic, section_path, "link", title, url),
                        node_type="link",
                        topic=topic or "unknown-topic",
                        section=section_path,
                        title=title,
                        content=_clean_label(anchor or title),
                        url=url,
                        source_title=source_title,
                    )
                )
                pending_title = ""
                continue

            if _is_query_payload(payload_raw):
                query_cmd = payload_raw
                query_exec_title = _normalize_query_exec_title(query_cmd, section=section, fallback_title=pending_title)
                query_kind = _classify_query_kind(section, query_exec_title)
                nodes.append(
                    Node(
                        node_id=_hash(topic, section_path, "query", query_cmd),
                        node_type="query",
                        topic=topic or "unknown-topic",
                        section=section_path,
                        title=_clean_label(pending_title or query_cmd),
                        content=_clean_label(f"{section_path} {query_cmd}"),
                        query_cmd=query_cmd,
                        query_exec_title=query_exec_title,
                        query_kind=query_kind,
                        query_source=(section or "").strip().lower(),
                        source_title=source_title,
                    )
                )
                pending_title = ""
                continue

            current_group = payload
            pending_title = payload
            group_path = _join_section_path(section, current_group)
            nodes.append(
                Node(
                    node_id=_hash(topic, group_path, "category", payload),
                    node_type="category",
                    topic=topic or "unknown-topic",
                    section=group_path,
                    title=payload,
                    content=f"category {payload}",
                    source_title=source_title,
                )
            )
            continue

        if line.startswith("- http://") or line.startswith("- https://"):
            payload = line[2:].strip()
            url, anchor = _split_url_and_anchor(payload)
            anchor = _clean_label(anchor)
            title = _clean_label(anchor or pending_title or url)
            section_path = _join_section_path(section, current_group)
            nodes.append(
                Node(
                    node_id=_hash(topic, section_path, "link", title, url),
                    node_type="link",
                    topic=topic or "unknown-topic",
                    section=section_path,
                    title=title,
                    content=_clean_label(f"{section_path} {anchor or title}"),
                    url=url,
                    source_title=source_title,
                )
            )
            pending_title = ""
            continue

    return nodes


def _group_nodes_by_topic(nodes: Iterable[Node]) -> dict[str, list[Node]]:
    grouped: dict[str, list[Node]] = {}
    for node in nodes:
        topic = (node.topic or "unknown-topic").strip() or "unknown-topic"
        grouped.setdefault(topic, []).append(node)
    return grouped


def should_ingest(
    raw_text: str,
    meta_path: Path,
    force: bool = False,
    expected_pipeline_version: str = PIPELINE_VERSION,
) -> tuple[bool, str]:
    raw_sha = _sha1_text(raw_text)
    if force:
        return True, raw_sha
    if not meta_path.exists():
        return True, raw_sha
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return True, raw_sha
    prev_sha = str(meta.get("raw_sha", ""))
    prev_pipeline = str(meta.get("pipeline_version", ""))
    if prev_pipeline != expected_pipeline_version:
        return True, raw_sha
    return prev_sha != raw_sha, raw_sha


def write_meta(
    meta_path: Path,
    *,
    raw_sha: str,
    title: str,
    snapshot_id: str,
    node_count: int,
    raw_file: str,
    vfs_base: str = "",
    db_path: str = "",
    nodes_jsonl: str = "",
    topics_json: str = "",
    navigation_json: str = "",
    storage_profile: str = "full",
    pipeline_version: str = PIPELINE_VERSION,
) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "raw_sha": raw_sha,
        "title": title,
        "snapshot_id": snapshot_id,
        "node_count": node_count,
        "raw_file": raw_file,
        "vfs_base": vfs_base,
        "db_path": db_path,
        "nodes_jsonl": nodes_jsonl,
        "topics_json": topics_json,
        "navigation_json": navigation_json,
        "storage_profile": storage_profile,
        "pipeline_version": pipeline_version,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_nodes_jsonl(nodes: Iterable[Node], dataset_root: Path, snapshot_id: str) -> Path:
    dataset_root.mkdir(parents=True, exist_ok=True)
    out_path = dataset_root / f"{snapshot_id}.nodes.jsonl"
    tmp_path = dataset_root / f"{snapshot_id}.nodes.jsonl.tmp"
    with tmp_path.open("w", encoding="utf-8") as fh:
        for node in nodes:
            fh.write(json.dumps(asdict(node), ensure_ascii=False) + "\n")
    tmp_path.replace(out_path)
    return out_path


def write_topics_json(nodes: Iterable[Node], dataset_root: Path, snapshot_id: str) -> Path:
    dataset_root.mkdir(parents=True, exist_ok=True)
    groups = _group_nodes_by_topic(nodes)
    payload = {
        "snapshot_id": snapshot_id,
        "topic_count": len(groups),
        "topics": [
            {"topic": topic, "node_count": len(items)}
            for topic, items in sorted(groups.items(), key=lambda x: x[0].lower())
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path = dataset_root / f"{snapshot_id}.topics.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def write_navigation_json(nodes: Iterable[Node], dataset_root: Path, snapshot_id: str) -> Path:
    dataset_root.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for node in nodes:
        if node.node_type != "query":
            continue
        exec_title = (node.query_exec_title or "").strip()
        if not exec_title:
            continue
        key = (node.query_kind or "", exec_title, node.topic or "", node.section or "")
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "node_id": node.node_id,
                "topic": node.topic,
                "section": node.section,
                "title": node.title,
                "query_cmd": node.query_cmd,
                "query_exec_title": exec_title,
                "query_kind": node.query_kind or "unknown",
                "query_source": node.query_source or "unknown",
            }
        )

    payload = {
        "snapshot_id": snapshot_id,
        "topic_navigation": [i for i in items if i.get("query_kind") == "topic_nav"],
        "knowledge_search": [i for i in items if i.get("query_kind") == "kb_search"],
        "other_queries": [i for i in items if i.get("query_kind") not in {"topic_nav", "kb_search"}],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path = dataset_root / f"{snapshot_id}.navigation.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _canonical_edge_key(exec_title: str) -> str:
    value = (exec_title or "").strip()
    if value.endswith(":"):
        value = value[:-1].rstrip()
    return value.lower()


def load_visited_exec_titles(path: Path) -> set[str]:
    if not str(path):
        return set()
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    items: list[str] = []
    if isinstance(payload, list):
        items = [str(x) for x in payload]
    elif isinstance(payload, dict):
        raw = payload.get("visited_exec_titles", [])
        if isinstance(raw, list):
            items = [str(x) for x in raw]
    return {_canonical_edge_key(x) for x in items if str(x).strip()}


def save_visited_exec_titles(path: Path, keys: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "visited_exec_titles": sorted([k for k in keys if k]),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_visited_topic_keys(path: Path) -> set[str]:
    if not str(path):
        return set()
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    items: list[str] = []
    if isinstance(payload, list):
        items = [str(x) for x in payload]
    elif isinstance(payload, dict):
        raw = payload.get("visited_topics", [])
        if isinstance(raw, list):
            items = [str(x) for x in raw]
    return {_canonical_topic_key(x) for x in items if str(x).strip()}


def save_visited_topic_keys(path: Path, keys: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "visited_topics": sorted([k for k in keys if k]),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def root_topic_from_title(raw_title: str) -> str:
    text = (raw_title or "").strip()
    if not text:
        return ""
    if re.match(r"^[Xx][Ll][Bb]\s+", text):
        try:
            text = resolve_title_from_input(text)
        except Exception:
            return ""
    if text.startswith("->"):
        text = text[2:].lstrip()
    elif text.startswith(">"):
        text = text[1:].lstrip()
    elif text.startswith("??"):
        text = text[2:].lstrip()
    text = text.rstrip(":").strip()
    if not text:
        return ""
    parts = [p.strip() for p in text.split("/") if p.strip()]
    if not parts:
        return ""
    return parts[0]


def topic_section_inputs(topic_root: str) -> dict:
    topic = (topic_root or "").strip()
    if not topic:
        return {}
    return {
        "searchin": f"xlb >{topic}/searchin:",
        "command": f"xlb >{topic}/command:",
        "backlink": f"xlb ->{topic}/:",
    }


def _navigation_from_run_result(run_result: dict) -> tuple[dict, str, str]:
    parsed = run_result.get("parsed_output")
    if not isinstance(parsed, dict):
        return {}, "", ""
    nav_payload = parsed.get("navigation_payload")
    if isinstance(nav_payload, dict):
        return nav_payload, str(parsed.get("meta_file", "")).strip(), ""

    meta_file = str(parsed.get("meta_file", "")).strip()
    if not meta_file:
        return {}, "", ""
    meta = _load_json(Path(meta_file))
    nav_file = str(meta.get("navigation_json", "")).strip()
    if not nav_file:
        return {}, meta_file, ""
    nav_path = Path(nav_file)
    if not nav_path.exists():
        return {}, meta_file, nav_file
    return _load_json(nav_path), meta_file, nav_file


def build_explore_candidates(
    *,
    searchin_navigation: dict,
    command_navigation: dict,
    backlink_inputs: list[str],
    visited_exec_titles: set[str] | None = None,
    visited_topic_keys: set[str] | None = None,
    edge_strategy: str = "searchin_command_backlink",
    include_other_queries: bool = False,
    max_candidates: int = 10,
) -> list[dict]:
    visited_exec = visited_exec_titles or set()
    visited_topics = visited_topic_keys or set()

    if edge_strategy == "command_searchin_backlink":
        source_priority = {"command_section": 0, "searchin_section": 1, "backlink": 2}
    elif edge_strategy == "mixed_backlink":
        source_priority = {"searchin_section": 0, "command_section": 0, "backlink": 1}
    else:
        source_priority = {"searchin_section": 0, "command_section": 1, "backlink": 2}

    out: list[dict] = []
    seen_exec: set[str] = set()

    def _append_nav(nav: dict, *, source: str) -> None:
        for item in build_navigation_candidates(nav, strategy="topic_first", include_other_queries=include_other_queries):
            exec_title = str(item.get("query_exec_title", "")).strip()
            if not exec_title:
                continue
            exec_key = _canonical_edge_key(exec_title)
            if not exec_key or exec_key in seen_exec or exec_key in visited_exec:
                continue
            topic_key = _canonical_topic_key(root_topic_from_title(exec_title))
            kind = str(item.get("query_kind", "")).strip() or "unknown"
            if kind in {"topic_nav", "backlink"} and topic_key and topic_key in visited_topics:
                continue
            seen_exec.add(exec_key)
            out.append(
                {
                    "title": str(item.get("title", "")),
                    "query_kind": kind,
                    "query_source": str(item.get("query_source", "")).strip() or "unknown",
                    "query_cmd": str(item.get("query_cmd", "")).strip(),
                    "query_exec_title": exec_title,
                    "input": _to_input_from_exec_title(exec_title),
                    "source": source,
                    "priority": source_priority.get(source, 9),
                    "topic_key": topic_key,
                }
            )

    def _append_backlinks(inputs: list[str]) -> None:
        for inp in inputs:
            text = str(inp).strip()
            if not text:
                continue
            try:
                title = resolve_title_from_input(text)
            except Exception:
                title = text
            exec_key = _canonical_edge_key(title)
            if not exec_key or exec_key in seen_exec or exec_key in visited_exec:
                continue
            topic_key = _canonical_topic_key(root_topic_from_title(title))
            if topic_key and topic_key in visited_topics:
                continue
            seen_exec.add(exec_key)
            out.append(
                {
                    "title": root_topic_from_title(title),
                    "query_kind": "backlink",
                    "query_source": "graph",
                    "query_cmd": "",
                    "query_exec_title": title,
                    "input": _to_input_from_exec_title(title),
                    "source": "backlink",
                    "priority": source_priority.get("backlink", 9),
                    "topic_key": topic_key,
                }
            )

    _append_nav(searchin_navigation or {}, source="searchin_section")
    _append_nav(command_navigation or {}, source="command_section")
    _append_backlinks(backlink_inputs or [])

    out.sort(
        key=lambda x: (
            int(x.get("priority", 9)),
            str(x.get("query_kind", "")).lower(),
            str(x.get("query_exec_title", "")).lower(),
        )
    )
    return out[: max(1, int(max_candidates))]


def build_navigation_candidates(
    navigation_payload: dict,
    *,
    strategy: str = "topic_first",
    include_other_queries: bool = False,
) -> list[dict]:
    topic_nav = list(navigation_payload.get("topic_navigation", []) or [])
    kb_search = list(navigation_payload.get("knowledge_search", []) or [])
    other = list(navigation_payload.get("other_queries", []) or [])

    ordered: list[dict] = []
    if strategy == "search_first":
        ordered.extend(kb_search)
        ordered.extend(topic_nav)
    elif strategy == "mixed":
        max_len = max(len(topic_nav), len(kb_search))
        for i in range(max_len):
            if i < len(topic_nav):
                ordered.append(topic_nav[i])
            if i < len(kb_search):
                ordered.append(kb_search[i])
    else:
        ordered.extend(topic_nav)
        ordered.extend(kb_search)

    if include_other_queries:
        ordered.extend(other)

    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for item in ordered:
        if not isinstance(item, dict):
            continue
        exec_title = str(item.get("query_exec_title", "")).strip()
        if not exec_title:
            continue
        query_kind = str(item.get("query_kind", "")).strip() or "unknown"
        query_source = str(item.get("query_source", "")).strip() or "unknown"
        key = (query_kind, query_source, _canonical_edge_key(exec_title))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _to_input_from_exec_title(exec_title: str) -> str:
    value = (exec_title or "").strip()
    if not value:
        return ""
    if re.match(r"^[Xx][Ll][Bb]\s+", value):
        return value
    if value.startswith((">", "??")):
        return f"xlb {value}"
    return f"xlb >{value}"


def run_retrieve_for_explore(
    *,
    input_text: str,
    retrieval_query: str = "",
    output_mode: str = "json",
    storage_profile: str = "minimal",
    network_confirmed: bool = False,
) -> dict:
    script_path = Path(__file__).with_name("retrieve-topic-index.sh")
    cmd = [str(script_path), input_text]
    if retrieval_query:
        cmd.append(retrieval_query)

    env = dict(os.environ)
    env["XLB_OUTPUT"] = output_mode
    env["XLB_STORAGE_PROFILE"] = storage_profile
    if network_confirmed:
        env["XLB_NETWORK_CONFIRMED"] = "1"

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    stdout_text = proc.stdout.strip()
    parsed: object
    try:
        parsed = json.loads(stdout_text) if stdout_text else {}
    except Exception:
        parsed = {"raw_stdout": stdout_text}
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": stdout_text,
        "stderr": proc.stderr.strip(),
        "parsed_output": parsed,
    }


def _canonical_topic_key(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip().lower())
    return text.rstrip("/")


def _canonical_exec_title_key(value: str) -> str:
    text = (value or "").strip()
    if text.endswith(":"):
        text = text[:-1].rstrip()
    if not text:
        return ""
    if text.startswith("->"):
        text = ">" + text[2:].lstrip()
    if text.startswith(">>"):
        text = ">" + text[2:].lstrip()
    if text.startswith(">"):
        body = _canonical_topic_key(text[1:])
        return f">{body}" if body else ">"
    if text.startswith("??"):
        body = _canonical_topic_key(text[2:])
        return f"??{body}" if body else "??"
    return _canonical_topic_key(text)


def _normalize_graph_target_title(raw: str) -> str:
    text = (raw or "").strip()
    if text.endswith(":"):
        text = text[:-1].rstrip()
    if text.startswith("->"):
        text = ">" + text[2:].lstrip()
    if text.endswith(":"):
        text = text[:-1].rstrip()
    normalized = _normalize_query_exec_title(text, section="", fallback_title="")
    if normalized:
        return normalized
    if text.startswith(">") or text.startswith("??"):
        return text
    return f">{text}" if text else ""


def _topic_key_from_exec_title(exec_title: str) -> str:
    text = (exec_title or "").strip()
    if text.endswith(":"):
        text = text[:-1].rstrip()
    if not text.startswith(">"):
        return ""
    return _canonical_topic_key(text[1:])


def collect_query_edges_from_index_dir(index_dir: Path, query_filter: str = "") -> list[dict]:
    index_dir = Path(index_dir)
    if not index_dir.exists():
        return []

    filter_key = (query_filter or "").strip().lower()
    edges: list[dict] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    for db_path in sorted(index_dir.glob("*.db")):
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
        except Exception:
            continue
        try:
            cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(nodes)").fetchall()
                if isinstance(r["name"], str)
            }
            required = {"query_exec_title", "query_kind", "query_source"}
            if not required.issubset(cols):
                continue
            rows = conn.execute(
                """
                SELECT node_id, topic, section, title, query_cmd, query_exec_title, query_kind, query_source, source_title
                FROM nodes
                WHERE node_type='query' AND query_exec_title != ''
                """
            ).fetchall()
            for row in rows:
                item = dict(row)
                haystack = " ".join(
                    [
                        str(item.get("topic", "")),
                        str(item.get("section", "")),
                        str(item.get("title", "")),
                        str(item.get("query_cmd", "")),
                        str(item.get("query_exec_title", "")),
                    ]
                ).lower()
                if filter_key and filter_key not in haystack:
                    continue
                key = (
                    str(item.get("topic", "")),
                    str(item.get("section", "")),
                    str(item.get("query_exec_title", "")),
                    str(item.get("query_kind", "")),
                    str(item.get("query_source", "")),
                )
                if key in seen:
                    continue
                seen.add(key)
                item["db_path"] = str(db_path)
                edges.append(item)
        finally:
            conn.close()
    return edges


def graph_neighbors_from_edges(
    edges: list[dict],
    *,
    target_title: str,
    limit: int = 100,
) -> dict:
    normalized_target = _normalize_graph_target_title(target_title)
    canonical_target = _canonical_exec_title_key(normalized_target)

    inbound = [
        e
        for e in edges
        if _canonical_exec_title_key(str(e.get("query_exec_title", ""))) == canonical_target
    ]
    topic_key = _topic_key_from_exec_title(normalized_target)
    outbound = [
        e
        for e in edges
        if topic_key and _canonical_topic_key(str(e.get("topic", ""))) == topic_key
    ]

    inbound.sort(key=lambda x: (str(x.get("topic", "")).lower(), str(x.get("query_source", "")).lower()))
    outbound.sort(key=lambda x: (str(x.get("query_kind", "")).lower(), str(x.get("query_exec_title", "")).lower()))
    inbound = inbound[: max(1, int(limit))]
    outbound = outbound[: max(1, int(limit))]

    by_topic: dict[str, dict] = {}
    for item in inbound:
        topic = str(item.get("topic", "")).strip() or "unknown-topic"
        entry = by_topic.setdefault(topic, {"topic": topic, "count": 0, "samples": []})
        entry["count"] += 1
        cmd = str(item.get("query_cmd", "")).strip()
        if cmd and cmd not in entry["samples"] and len(entry["samples"]) < 3:
            entry["samples"].append(cmd)

    upstream_topics = sorted(by_topic.values(), key=lambda x: (-int(x["count"]), str(x["topic"]).lower()))

    upstream_followups = []
    for t in upstream_topics:
        topic_name = str(t.get("topic", "")).strip()
        if topic_name:
            upstream_followups.append(f"xlb >{topic_name}/")

    outbound_followups = []
    seen_out: set[str] = set()
    for item in outbound:
        exec_title = str(item.get("query_exec_title", "")).strip()
        if not exec_title:
            continue
        inp = _to_input_from_exec_title(exec_title)
        key = inp.lower()
        if key in seen_out:
            continue
        seen_out.add(key)
        outbound_followups.append(inp)

    return {
        "mode": "graph_neighbors",
        "target_input": target_title,
        "target_exec_title": normalized_target,
        "canonical_target": canonical_target,
        "inbound_edge_count": len(inbound),
        "outbound_edge_count": len(outbound),
        "upstream_topic_count": len(upstream_topics),
        "upstream_topics": upstream_topics,
        "inbound_edges": inbound,
        "outbound_edges": outbound,
        "follow_up_inputs": {
            "upstream_topics": upstream_followups[:20],
            "outbound_queries": outbound_followups[:20],
        },
    }


def graph_neighbors(index_dir: Path, target_title: str, *, limit: int = 100, query_filter: str = "") -> dict:
    edges = collect_query_edges_from_index_dir(index_dir, query_filter=query_filter)
    payload = graph_neighbors_from_edges(edges, target_title=target_title, limit=limit)
    payload["index_dir"] = str(index_dir)
    payload["edge_pool_size"] = len(edges)
    payload["query_filter"] = query_filter
    return payload


def _default_index_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "cache" / "index"


def _normalize_seed_input(seed_input: str) -> str:
    text = (seed_input or "").strip()
    if not text:
        return ""
    if re.match(r"^[Xx][Ll][Bb]\s+", text):
        return text
    try:
        title = resolve_title_from_input(text)
    except Exception:
        title = text
    return _to_input_from_exec_title(title)


def normalize_auto_explore_seed(raw_input: str) -> str:
    text = (raw_input or "").strip()
    if not text:
        return ""

    m = re.match(r"^[Xx][Ll][Bb]\s+auto\s+(.+)$", text)
    if m:
        text = m.group(1).strip()
    else:
        m2 = re.match(r"^[Aa][Uu][Tt][Oo]\s+(.+)$", text)
        if m2:
            text = m2.group(1).strip()
    if not text:
        return ""

    if re.match(r"^[Xx][Ll][Bb]\s+", text):
        try:
            title = resolve_title_from_input(text)
        except Exception:
            title = text
    elif text.startswith((">", "??", "->")):
        title = text
    else:
        title = f">{text}/:"

    if title.startswith((">", "->")) and not title.endswith(":"):
        title = f"{title}:"
    return _to_input_from_exec_title(title)


def explore_loop(
    *,
    seed_input: str,
    max_steps: int = 12,
    max_depth: int = 4,
    max_seconds: float = 90.0,
    edge_strategy: str = "searchin_command_backlink",
    include_backlinks: bool = True,
    include_other_queries: bool = False,
    max_branching: int = 6,
    backlink_limit: int = 30,
    backlink_filter: str = "",
    visited_exec_titles: set[str] | None = None,
    visited_topic_keys: set[str] | None = None,
    storage_profile: str = "minimal",
    network_confirmed: bool = False,
    index_dir: Path | None = None,
    run_fn=None,
    graph_fn=None,
) -> dict:
    run_impl = run_fn or run_retrieve_for_explore
    graph_impl = graph_fn or graph_neighbors

    normalized_seed = _normalize_seed_input(seed_input)
    if not normalized_seed:
        return {
            "mode": "explore_loop",
            "status": "invalid_seed",
            "seed_input": seed_input,
            "error": "empty_seed_input",
        }

    visited_exec = set(visited_exec_titles or set())
    visited_topics = set(visited_topic_keys or set())
    queue: list[dict] = [{"input": normalized_seed, "depth": 0, "via": "seed"}]
    trace: list[dict] = []

    idx_dir = Path(index_dir) if index_dir else _default_index_dir()
    start_ts = time.monotonic()
    steps_executed = 0
    aux_fetch_count = 0
    stop_reason = "frontier_exhausted"

    while queue:
        elapsed = time.monotonic() - start_ts
        if max_seconds > 0 and elapsed >= max_seconds:
            stop_reason = "time_budget_exhausted"
            break
        if steps_executed >= max_steps:
            stop_reason = "step_budget_exhausted"
            break

        item = queue.pop(0)
        input_text = str(item.get("input", "")).strip()
        depth = int(item.get("depth", 0))
        via = str(item.get("via", "")).strip()
        if not input_text:
            continue

        try:
            input_title = resolve_title_from_input(input_text)
        except Exception:
            input_title = input_text
        input_key = _canonical_edge_key(input_title)
        if input_key and input_key in visited_exec:
            trace.append(
                {
                    "input": input_text,
                    "depth": depth,
                    "via": via,
                    "status": "skipped_visited_exec",
                    "resolved_title": input_title,
                }
            )
            continue

        run_result = run_impl(
            input_text=input_text,
            output_mode="json",
            storage_profile=storage_profile,
            network_confirmed=network_confirmed,
        )
        steps_executed += 1
        parsed = run_result.get("parsed_output")
        if not isinstance(parsed, dict):
            parsed = {}
        resolved_title = str(parsed.get("title", "")).strip() or input_title
        resolved_key = _canonical_edge_key(resolved_title)
        if input_key:
            visited_exec.add(input_key)
        if resolved_key:
            visited_exec.add(resolved_key)
        root_topic = root_topic_from_title(resolved_title)
        root_topic_key = _canonical_topic_key(root_topic)
        if root_topic_key:
            visited_topics.add(root_topic_key)

        hop: dict = {
            "input": input_text,
            "depth": depth,
            "via": via,
            "resolved_title": resolved_title,
            "root_topic": root_topic,
            "returncode": int(run_result.get("returncode", 1)),
        }

        if int(run_result.get("returncode", 1)) != 0:
            hop["status"] = "execute_failed"
            hop["stderr"] = str(run_result.get("stderr", ""))
            trace.append(hop)
            continue

        if depth >= max_depth:
            hop["status"] = "depth_budget_reached"
            trace.append(hop)
            continue

        if not root_topic:
            hop["status"] = "no_topic_root"
            trace.append(hop)
            continue

        section_inputs = topic_section_inputs(root_topic)
        searchin_nav: dict = {}
        command_nav: dict = {}
        section_meta: dict = {}

        searchin_run = run_impl(
            input_text=section_inputs["searchin"],
            output_mode="json",
            storage_profile=storage_profile,
            network_confirmed=network_confirmed,
        )
        aux_fetch_count += 1
        searchin_nav, searchin_meta, searchin_nav_file = _navigation_from_run_result(searchin_run)
        section_meta["searchin"] = {
            "input": section_inputs["searchin"],
            "meta_file": searchin_meta,
            "navigation_file": searchin_nav_file,
            "returncode": int(searchin_run.get("returncode", 1)),
        }

        command_run = run_impl(
            input_text=section_inputs["command"],
            output_mode="json",
            storage_profile=storage_profile,
            network_confirmed=network_confirmed,
        )
        aux_fetch_count += 1
        command_nav, command_meta, command_nav_file = _navigation_from_run_result(command_run)
        section_meta["command"] = {
            "input": section_inputs["command"],
            "meta_file": command_meta,
            "navigation_file": command_nav_file,
            "returncode": int(command_run.get("returncode", 1)),
        }

        effective_index_dir = idx_dir
        db_path_raw = str(parsed.get("db_path", "")).strip()
        if db_path_raw:
            db_parent = Path(db_path_raw).parent
            if db_parent.exists():
                effective_index_dir = db_parent

        backlink_inputs: list[str] = []
        backlink_meta: dict = {}
        if include_backlinks:
            backlink_result = graph_impl(
                effective_index_dir,
                section_inputs["backlink"].replace("xlb ", "", 1),
                limit=max(1, int(backlink_limit)),
                query_filter=backlink_filter,
            )
            raw_followups = (
                backlink_result.get("follow_up_inputs", {}).get("upstream_topics", [])
                if isinstance(backlink_result, dict)
                else []
            )
            backlink_inputs = [str(x).strip() for x in raw_followups if str(x).strip()]
            backlink_meta = {
                "input": section_inputs["backlink"],
                "index_dir": str(effective_index_dir),
                "upstream_topic_count": len(backlink_inputs),
            }

        candidates = build_explore_candidates(
            searchin_navigation=searchin_nav,
            command_navigation=command_nav,
            backlink_inputs=backlink_inputs,
            visited_exec_titles=visited_exec,
            visited_topic_keys=visited_topics,
            edge_strategy=edge_strategy,
            include_other_queries=include_other_queries,
            max_candidates=max(1, int(max_branching)),
        )

        enqueued: list[dict] = []
        for cand in candidates:
            queue.append({"input": cand["input"], "depth": depth + 1, "via": cand.get("source", "unknown")})
            enqueued.append(
                {
                    "input": cand.get("input", ""),
                    "query_exec_title": cand.get("query_exec_title", ""),
                    "query_kind": cand.get("query_kind", ""),
                    "source": cand.get("source", ""),
                }
            )

        hop["status"] = "expanded"
        hop["section_meta"] = section_meta
        hop["backlink_meta"] = backlink_meta
        hop["candidate_count"] = len(candidates)
        hop["enqueued"] = enqueued
        trace.append(hop)

    elapsed_total = time.monotonic() - start_ts
    if not queue and stop_reason not in {"time_budget_exhausted", "step_budget_exhausted"}:
        stop_reason = "frontier_exhausted"

    return {
        "mode": "explore_loop",
        "status": "ok",
        "seed_input": seed_input,
        "normalized_seed_input": normalized_seed,
        "edge_strategy": edge_strategy,
        "include_backlinks": bool(include_backlinks),
        "include_other_queries": bool(include_other_queries),
        "budgets": {
            "max_steps": int(max_steps),
            "max_depth": int(max_depth),
            "max_seconds": float(max_seconds),
            "max_branching": int(max_branching),
        },
        "steps_executed": steps_executed,
        "aux_fetch_count": aux_fetch_count,
        "elapsed_seconds": round(elapsed_total, 3),
        "stop_reason": stop_reason,
        "frontier_remaining": len(queue),
        "queue_preview": [
            {"input": str(i.get("input", "")), "depth": int(i.get("depth", 0)), "via": str(i.get("via", ""))}
            for i in queue[:20]
        ],
        "visited_exec_count": len(visited_exec),
        "visited_topic_count": len(visited_topics),
        "visited_exec_titles": sorted(list(visited_exec))[:200],
        "visited_topic_keys": sorted(list(visited_topics))[:200],
        "trace": trace,
    }


def write_virtual_tree(nodes: Iterable[Node], vfs_root: Path, snapshot_id: str) -> Path:
    nodes = list(nodes)
    base = vfs_root / snapshot_id
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)

    grouped = _group_nodes_by_topic(nodes)
    topic_items: list[dict] = []

    for topic, topic_nodes in sorted(grouped.items(), key=lambda x: x[0].lower()):
        topic_slug = slugify(topic)
        topic_base = base / topic_slug
        topic_base.mkdir(parents=True, exist_ok=True)
        sections: dict[str, int] = {}

        for node in topic_nodes:
            section_parts = [p.strip() for p in str(node.section).split("/") if p.strip()]
            if not section_parts:
                section_parts = ["root"]
            sec_dir = topic_base
            for part in section_parts:
                sec_dir = sec_dir / slugify(part)
            sec_dir.mkdir(parents=True, exist_ok=True)
            sections[node.section] = sections.get(node.section, 0) + 1

            stem = slugify(node.title or node.url or node.query_cmd)
            filename_hash = _hash(node.node_id)
            if node.node_type == "query":
                path = sec_dir / f"{stem}-{filename_hash}.query.txt"
                path.write_text(node.query_cmd, encoding="utf-8")
            elif node.node_type in {"category", "topic"}:
                path = sec_dir / f"{stem}-{filename_hash}.category.md"
                path.write_text(
                    "\n".join(
                        [
                            f"# {node.title}",
                            f"- type: {node.node_type}",
                            f"- section: {node.section}",
                            f"- source_title: {node.source_title}",
                            "",
                            node.content or "",
                        ]
                    ),
                    encoding="utf-8",
                )
            else:
                path = sec_dir / f"{stem}-{filename_hash}.link.md"
                path.write_text(
                    "\n".join(
                        [
                            f"# {node.title}",
                            f"- type: {node.node_type}",
                            f"- section: {node.section}",
                            f"- url: {node.url}",
                            f"- source_title: {node.source_title}",
                            "",
                            node.content or "",
                        ]
                    ),
                    encoding="utf-8",
                )

        ds_lines = [f"# {topic}", "", "## Sections"]
        for sec, count in sorted(sections.items(), key=lambda x: x[0].lower()):
            ds_lines.append(f"- {sec}/ ({count})")
        (topic_base / "data_structure.md").write_text("\n".join(ds_lines) + "\n", encoding="utf-8")
        topic_manifest = {
            "topic": topic,
            "topic_slug": topic_slug,
            "snapshot_id": snapshot_id,
            "node_count": len(topic_nodes),
            "sections": [{"name": sec, "slug": slugify(sec), "count": count} for sec, count in sorted(sections.items())],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        (topic_base / "manifest.json").write_text(json.dumps(topic_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        topic_items.append({"topic": topic, "topic_slug": topic_slug, "node_count": len(topic_nodes), "path": str(topic_base)})

    root_lines = ["# Topic Index", "", "## Topics"]
    for item in topic_items:
        root_lines.append(f"- {item['topic']} ({item['node_count']}) -> {item['topic_slug']}/")
    (base / "data_structure.md").write_text("\n".join(root_lines) + "\n", encoding="utf-8")
    root_manifest = {
        "snapshot_id": snapshot_id,
        "topic_count": len(topic_items),
        "node_count": len(nodes),
        "topics": topic_items,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (base / "manifest.json").write_text(json.dumps(root_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return base


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _create_tables(conn: sqlite3.Connection) -> bool:
    conn.execute("DROP TABLE IF EXISTS nodes")
    conn.execute(
        """
        CREATE TABLE nodes (
          node_id TEXT PRIMARY KEY,
          node_type TEXT NOT NULL,
          topic TEXT NOT NULL,
          section TEXT NOT NULL,
          title TEXT NOT NULL,
          content TEXT NOT NULL,
          url TEXT NOT NULL,
          query_cmd TEXT NOT NULL,
          query_exec_title TEXT NOT NULL,
          query_kind TEXT NOT NULL,
          query_source TEXT NOT NULL,
          source_title TEXT NOT NULL
        )
        """
    )
    try:
        conn.execute("DROP TABLE IF EXISTS nodes_fts")
        conn.execute("CREATE VIRTUAL TABLE nodes_fts USING fts5(title, section, content, topic, query_exec_title, query_cmd)")
        return True
    except sqlite3.OperationalError:
        return False


def build_index(nodes: Iterable[Node], db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        has_fts = _create_tables(conn)
        seen_ids: set[str] = set()
        for node in nodes:
            if node.node_id in seen_ids:
                continue
            seen_ids.add(node.node_id)
            conn.execute(
                """
                INSERT INTO nodes(node_id, node_type, topic, section, title, content, url, query_cmd, query_exec_title, query_kind, query_source, source_title)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.node_id,
                    node.node_type,
                    node.topic,
                    node.section,
                    node.title,
                    node.content,
                    node.url,
                    node.query_cmd,
                    node.query_exec_title,
                    node.query_kind,
                    node.query_source,
                    node.source_title,
                ),
            )
            if has_fts:
                conn.execute(
                    "INSERT INTO nodes_fts(rowid, title, section, content, topic, query_exec_title, query_cmd) VALUES((SELECT rowid FROM nodes WHERE node_id = ?), ?, ?, ?, ?, ?, ?)",
                    (node.node_id, node.title, node.section, node.content, node.topic, node.query_exec_title, node.query_cmd),
                )
        conn.commit()
    finally:
        conn.close()


def _search_with_like_tokens(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    uniq = _prepare_search_tokens(query)
    if not uniq:
        return []

    clause_parts = []
    params: list[str] = []
    for t in uniq:
        like = f"%{t}%"
        clause_parts.append("(title LIKE ? OR section LIKE ? OR content LIKE ? OR topic LIKE ? OR query_exec_title LIKE ? OR query_cmd LIKE ?)")
        params.extend([like, like, like, like, like, like])

    sql = f"""
        SELECT node_id, node_type, topic, section, title, content, url, query_cmd, query_exec_title, query_kind, query_source, source_title
        FROM nodes
        WHERE {' OR '.join(clause_parts)}
        ORDER BY length(content) ASC
        LIMIT ?
    """
    params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


def _prepare_search_tokens(text: str) -> list[str]:
    raw = text.strip()
    if not raw:
        return []

    token_parts = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", raw)
    tokens = [raw] + token_parts
    uniq: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        value = token.strip()
        if len(value) < 2:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(value)
    return uniq


def _build_topic_entry_query(topic: str) -> str:
    clean = topic.strip()
    if not clean:
        return ">unknown-topic/"
    return f">{clean}/"


def _build_topic_entry_input(topic: str) -> str:
    return f"xlb {_build_topic_entry_query(topic)}"


def search_index(
    db_path: Path,
    query: str,
    limit: int = 8,
    expand_categories: bool = True,
    expand_limit_per_category: int = 200,
    expand_related_sections: bool = True,
    related_limit_per_section: int = 20,
) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        has_fts = bool(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nodes_fts'").fetchone())
        if has_fts and query.strip():
            try:
                rows = conn.execute(
                    """
                    SELECT n.node_id, n.node_type, n.topic, n.section, n.title, n.content, n.url, n.query_cmd, n.query_exec_title, n.query_kind, n.query_source, n.source_title
                    FROM nodes_fts
                    JOIN nodes n ON n.rowid = nodes_fts.rowid
                    WHERE nodes_fts MATCH ?
                    ORDER BY bm25(nodes_fts), length(n.content) ASC
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = _search_with_like_tokens(conn, query, limit)
            if not rows:
                rows = _search_with_like_tokens(conn, query, limit)
        else:
            rows = _search_with_like_tokens(conn, query, limit)
        hits = [dict(r) for r in rows]
        for h in hits:
            h["match_type"] = "direct"
        if not expand_categories:
            return hits

        category_keys = [
            (str(h.get("topic", "")), str(h.get("section", "")))
            for h in hits
            if h.get("node_type") == "category"
        ]
        seen_ids = {str(h.get("node_id", "")) for h in hits}
        merged = list(hits)

        def _append_section_children(topic_name: str, section_name: str, per_section_limit: int, match_type: str) -> None:
            if not section_name or not topic_name:
                return
            child_rows = conn.execute(
                """
                SELECT node_id, node_type, topic, section, title, content, url, query_cmd, query_exec_title, query_kind, query_source, source_title
                FROM nodes
                WHERE topic = ? AND section = ? AND node_type != 'category'
                ORDER BY length(content) ASC
                LIMIT ?
                """,
                (topic_name, section_name, max(1, int(per_section_limit))),
            ).fetchall()
            for row in child_rows:
                data = dict(row)
                node_id = str(data.get("node_id", ""))
                if not node_id or node_id in seen_ids:
                    continue
                seen_ids.add(node_id)
                data["match_type"] = match_type
                merged.append(data)

        for topic_name, section in category_keys:
            _append_section_children(topic_name, section, expand_limit_per_category, "category_child")

        if expand_related_sections:
            related_keys = {
                (str(h.get("topic", "")), str(h.get("section", "")))
                for h in hits
                if str(h.get("node_type", "")) == "link"
            }
            for topic_name, section in sorted(related_keys):
                _append_section_children(topic_name, section, related_limit_per_section, "section_related")
        return merged
    finally:
        conn.close()


def _extract_hits_from_result_payload(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        raw_hits = payload.get("hits", [])
        if isinstance(raw_hits, list):
            return [h for h in raw_hits if isinstance(h, dict)]
    if isinstance(payload, list):
        return [h for h in payload if isinstance(h, dict)]
    return []


def extract_link_urls_from_hits_payload(payload: object, *, limit: int = 3) -> list[str]:
    hits = _extract_hits_from_result_payload(payload)
    urls: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if str(hit.get("node_type", "")) != "link":
            continue
        url = str(hit.get("url", "")).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= max(1, int(limit)):
            break
    return urls


def _normalize_open_url(url: str, *, strip_fragment: bool = True) -> str:
    out = (url or "").strip()
    if strip_fragment and "#" in out:
        out = out.split("#", 1)[0].strip()
    return out


def _build_open_actions(
    *,
    url: str,
    app: str,
    atlas_app_path: str,
) -> list[dict]:
    app_key = (app or "chrome").strip().lower()
    if app_key == "atlas":
        script = """tell application "ChatGPT Atlas" to activate
delay 0.15
tell application "System Events"
    try
        set frontmost of process "ChatGPT Atlas" to true
    end try
    keystroke "l" using command down
    delay 0.15
    keystroke "v" using command down
    delay 0.1
    key code 36
end tell"""
        return [
            {"kind": "cmd", "cmd": ["pbcopy"], "input": url},
            {"kind": "cmd", "cmd": ["open", atlas_app_path]},
            {"kind": "sleep", "seconds": 0.2},
            {"kind": "cmd", "cmd": ["/usr/bin/osascript", "-e", script]},
        ]
    if app_key == "dia":
        script = """tell application "Dia" to activate
tell application "System Events"
    keystroke "e" using command down
end tell"""
        return [
            {"kind": "cmd", "cmd": ["open", "-a", "Dia", url]},
            {"kind": "sleep", "seconds": 1.0},
            {"kind": "cmd", "cmd": ["/usr/bin/osascript", "-e", script]},
        ]
    if app_key == "chrome":
        script = """tell application "Google Chrome" to activate
tell application "System Events"
    keystroke "e" using option down
end tell"""
        return [
            {"kind": "cmd", "cmd": ["open", "-a", "Google Chrome", url]},
            {"kind": "sleep", "seconds": 1.0},
            {"kind": "cmd", "cmd": ["/usr/bin/osascript", "-e", script]},
        ]
    return [{"kind": "cmd", "cmd": ["open", url]}]


def open_url_in_local_app(
    *,
    url: str,
    app: str = "chrome",
    strip_fragment: bool = True,
    dry_run: bool = False,
    atlas_app_path: str = "/Applications/ChatGPT Atlas.app",
) -> dict:
    normalized_url = _normalize_open_url(url, strip_fragment=strip_fragment)
    if not normalized_url:
        return {
            "mode": "open_url",
            "status": "error",
            "error": "empty_url",
            "url": url,
            "normalized_url": normalized_url,
            "app": app,
        }

    actions = _build_open_actions(url=normalized_url, app=app, atlas_app_path=atlas_app_path)
    if dry_run:
        return {
            "mode": "open_url",
            "status": "dry_run",
            "url": url,
            "normalized_url": normalized_url,
            "app": app,
            "actions": actions,
        }

    if platform.system() != "Darwin":
        return {
            "mode": "open_url",
            "status": "error",
            "error": "unsupported_platform",
            "platform": platform.system(),
            "url": url,
            "normalized_url": normalized_url,
            "app": app,
        }

    step_results: list[dict] = []
    for action in actions:
        kind = str(action.get("kind", "")).strip()
        if kind == "sleep":
            sec = float(action.get("seconds", 0.0) or 0.0)
            if sec > 0:
                time.sleep(sec)
            step_results.append({"kind": "sleep", "seconds": sec, "returncode": 0})
            continue
        cmd = action.get("cmd", [])
        if not isinstance(cmd, list) or not cmd:
            step_results.append({"kind": "cmd", "cmd": cmd, "returncode": 1, "stderr": "invalid_command"})
            return {
                "mode": "open_url",
                "status": "error",
                "error": "invalid_command",
                "url": url,
                "normalized_url": normalized_url,
                "app": app,
                "steps": step_results,
            }
        proc = subprocess.run(
            [str(x) for x in cmd],
            input=str(action.get("input", "")) if "input" in action else None,
            text=True,
            capture_output=True,
            check=False,
        )
        step = {
            "kind": "cmd",
            "cmd": [str(x) for x in cmd],
            "returncode": int(proc.returncode),
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
        step_results.append(step)
        if proc.returncode != 0:
            return {
                "mode": "open_url",
                "status": "error",
                "error": "command_failed",
                "url": url,
                "normalized_url": normalized_url,
                "app": app,
                "steps": step_results,
            }

    return {
        "mode": "open_url",
        "status": "opened",
        "url": url,
        "normalized_url": normalized_url,
        "app": app,
        "steps": step_results,
    }


def open_urls_in_local_app(
    urls: Iterable[str],
    *,
    app: str = "chrome",
    strip_fragment: bool = True,
    dry_run: bool = False,
    atlas_app_path: str = "/Applications/ChatGPT Atlas.app",
    delay_between_sec: float = 0.0,
    stop_on_error: bool = False,
) -> dict:
    uniq: list[str] = []
    seen: set[str] = set()
    for u in urls:
        raw = str(u or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        uniq.append(raw)

    results: list[dict] = []
    for i, u in enumerate(uniq):
        res = open_url_in_local_app(
            url=u,
            app=app,
            strip_fragment=strip_fragment,
            dry_run=dry_run,
            atlas_app_path=atlas_app_path,
        )
        results.append(res)
        failed = str(res.get("status", "")) == "error"
        if failed and stop_on_error:
            break
        if i < len(uniq) - 1 and delay_between_sec > 0:
            time.sleep(delay_between_sec)

    return {
        "mode": "open_urls",
        "app": app,
        "count": len(uniq),
        "opened": sum(1 for r in results if r.get("status") in {"opened", "dry_run"}),
        "errors": sum(1 for r in results if r.get("status") == "error"),
        "results": results,
    }


def build_network_confirmation_template(
    *,
    input_text: str,
    query: str,
    hits: list[dict],
    prefetch_enabled: bool,
    has_external_route: bool,
    preview_limit: int = 3,
) -> str:
    urls: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if str(hit.get("node_type", "")) != "link":
            continue
        url = str(hit.get("url", "")).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)

    lines = [
        "【网络扩展需确认】",
        "已完成本地索引检索；默认未执行网络抓取/网页下载。",
        "",
        f"- 输入指令: {input_text}",
        f"- 检索关键词: {query or '(空)'}",
        f"- 本地命中数: {len(hits)}",
        f"- 可抓取URL数: {len(urls)}",
    ]
    if has_external_route:
        lines.append("- 检测到可用外部路由能力: 是")
    if prefetch_enabled:
        lines.append("- 已开启预取配置: 是（当前未确认，未执行）")

    if urls:
        lines.extend(["", "URL示例:"])
        for idx, url in enumerate(urls[: max(1, int(preview_limit))], start=1):
            lines.append(f"{idx}. {url}")

    cmd_parts = ["skills/xlb-topic-index/scripts/retrieve-topic-index.sh", shlex.quote(input_text)]
    if query:
        cmd_parts.append(shlex.quote(query))
    local_cmd = " ".join(cmd_parts)
    confirmed_cmd = f"XLB_NETWORK_CONFIRMED=1 XLB_PREFETCH_ARTIFACTS={1 if prefetch_enabled else 0} {local_cmd}"
    lines.extend(
        [
            "",
            "如需继续执行耗时网络扩展，请明确确认后重试：",
            confirmed_cmd,
            "",
            "仅继续使用本地索引（不抓取网络）：",
            local_cmd,
        ]
    )
    return "\n".join(lines)


def summarize_topics(nodes: list[dict], limit: int = 10, sample_per_topic: int = 3) -> dict:
    by_topic: dict[str, list[dict]] = {}
    for node in nodes:
        topic = str(node.get("topic", "")).strip() or "unknown-topic"
        by_topic.setdefault(topic, []).append(node)

    items = []
    for topic, arr in by_topic.items():
        samples = []
        for node in arr:
            title = str(node.get("title", "")).strip()
            if title and title not in samples:
                samples.append(title)
            if len(samples) >= max(1, sample_per_topic):
                break
        items.append(
            {
                "topic": topic,
                "count": len(arr),
                "samples": samples,
                "entry_query": _build_topic_entry_query(topic),
                "entry_input": _build_topic_entry_input(topic),
            }
        )
    items.sort(key=lambda x: (-x["count"], x["topic"].lower()))
    top_items = items[: max(1, limit)]
    recommended_topic = top_items[0]["topic"] if top_items else ""
    return {
        "topic_count": len(by_topic),
        "topics": top_items,
        "recommended_topic": recommended_topic,
    }


def suggest_topics_from_query(
    db_path: Path,
    query: str,
    *,
    hit_limit: int = 200,
    topic_limit: int = 10,
    sample_per_topic: int = 3,
) -> dict:
    seed = query.strip()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not seed:
            rows = conn.execute(
                """
                SELECT topic, count(*) as c
                FROM nodes
                GROUP BY topic
                ORDER BY c DESC, topic ASC
                LIMIT ?
                """,
                (max(1, topic_limit),),
            ).fetchall()
            topics = []
            for row in rows:
                topic = str(row["topic"])
                topics.append(
                    {
                        "topic": topic,
                        "count": int(row["c"]),
                        "samples": [],
                        "entry_query": _build_topic_entry_query(topic),
                        "entry_input": _build_topic_entry_input(topic),
                    }
                )
            return {
                "query": seed,
                "mode": "all_topics",
                "topic_count": len(topics),
                "topics": topics,
                "recommended_topic": topics[0]["topic"] if topics else "",
            }

        tokens = _prepare_search_tokens(seed)
        if not tokens:
            return {
                "query": seed,
                "mode": "query_suggest",
                "topic_count": 0,
                "topics": [],
                "hit_count": 0,
                "recommended_topic": "",
            }

        clause_parts = []
        params: list[str] = []
        for token in tokens:
            like = f"%{token}%"
            clause_parts.append("(title LIKE ? OR section LIKE ? OR content LIKE ? OR topic LIKE ? OR query_exec_title LIKE ? OR query_cmd LIKE ?)")
            params.extend([like, like, like, like, like, like])
        where_clause = " OR ".join(clause_parts)

        topic_rows = conn.execute(
            f"""
            SELECT topic, COUNT(*) as c
            FROM nodes
            WHERE {where_clause}
            GROUP BY topic
            ORDER BY c DESC, topic ASC
            LIMIT ?
            """,
            tuple(params + [max(1, topic_limit)]),
        ).fetchall()

        topics = []
        total_hits = 0
        for row in topic_rows:
            topic = str(row["topic"])
            count = int(row["c"])
            total_hits += count
            sample_rows = conn.execute(
                f"""
                SELECT title
                FROM nodes
                WHERE topic = ? AND ({where_clause})
                ORDER BY CASE node_type WHEN 'category' THEN 0 WHEN 'query' THEN 1 ELSE 2 END, length(content) ASC
                LIMIT ?
                """,
                tuple([topic] + params + [max(1, sample_per_topic)]),
            ).fetchall()
            samples: list[str] = []
            for srow in sample_rows:
                title = str(srow["title"]).strip()
                if title and title not in samples:
                    samples.append(title)
            topics.append(
                {
                    "topic": topic,
                    "count": count,
                    "samples": samples,
                    "entry_query": _build_topic_entry_query(topic),
                    "entry_input": _build_topic_entry_input(topic),
                }
            )

        return {
            "query": seed,
            "mode": "query_suggest",
            "topic_count": len(topics),
            "topics": topics,
            "hit_count": total_hits,
            "recommended_topic": topics[0]["topic"] if topics else "",
        }
    finally:
        conn.close()


def iterative_search(
    db_path: Path,
    *,
    query: str,
    limit: int = 8,
    max_iter: int = 5,
    gain_threshold: float = 0.05,
    low_gain_rounds: int = 3,
    follow_query_nodes: bool = True,
) -> dict:
    seed_query = query.strip()
    if not seed_query:
        return {
            "seed_query": query,
            "iterations": 0,
            "stop_reason": "empty_query",
            "unique_hits": 0,
            "rounds": [],
            "frontier_remaining": 0,
        }

    frontier = [seed_query]
    seen_node_ids: set[str] = set()
    seen_query_cmds: set[str] = set()
    rounds: list[dict] = []
    low_gain_streak = 0
    stop_reason = "max_iter"

    for _ in range(max_iter):
        if not frontier:
            stop_reason = "no_frontier"
            break

        current_query = frontier.pop(0)
        hits = search_index(db_path, current_query, limit=limit)
        new_hits = [h for h in hits if h.get("node_id") not in seen_node_ids]
        for h in new_hits:
            node_id = h.get("node_id")
            if node_id:
                seen_node_ids.add(node_id)

        expanded = []
        if follow_query_nodes:
            for h in hits:
                if h.get("node_type") != "query":
                    continue
                cmd = str(h.get("query_cmd", "")).strip()
                if not cmd or cmd in seen_query_cmds:
                    continue
                seen_query_cmds.add(cmd)
                frontier.append(cmd)
                expanded.append(cmd)

        gain = (len(new_hits) / float(limit)) if limit > 0 else 0.0
        rounds.append(
            {
                "iteration": len(rounds) + 1,
                "query": current_query,
                "hits": len(hits),
                "new_hits": len(new_hits),
                "gain": gain,
                "expanded_queries": expanded,
                "top_titles": [str(h.get("title", "")) for h in hits[:3]],
            }
        )

        if not hits:
            stop_reason = "no_hits"
            break

        if gain < gain_threshold:
            low_gain_streak += 1
        else:
            low_gain_streak = 0

        if low_gain_streak >= low_gain_rounds:
            stop_reason = "low_gain"
            break

    return {
        "seed_query": seed_query,
        "iterations": len(rounds),
        "stop_reason": stop_reason,
        "unique_hits": len(seen_node_ids),
        "rounds": rounds,
        "frontier_remaining": len(frontier),
    }


def _choose_suffix(url: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    guessed = Path(parsed.path).suffix
    if guessed:
        return guessed.lower()
    ctype = (content_type or "").split(";")[0].strip().lower()
    mapped = mimetypes.guess_extension(ctype) if ctype else None
    if mapped:
        return mapped
    return ".bin"


def _strip_html_tags(text: str) -> str:
    stripped = re.sub(r"<[^>]+>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    stripped = unescape(stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _strip_html_tags_keep_newlines(text: str) -> str:
    stripped = re.sub(r"<[^>]+>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    stripped = unescape(stripped)
    stripped = stripped.replace("\r\n", "\n").replace("\r", "\n")
    stripped = re.sub(r"[ \t\f\v]+", " ", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped


def _normalize_lines(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines) + ("\n" if lines else "")


def _html_to_text(markup: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", markup, flags=re.DOTALL)
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*(p|div|section|article|li|tr|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = _strip_html_tags_keep_newlines(text)
    return _normalize_lines(text)


def _html_to_markdown(markup: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", markup, flags=re.DOTALL)
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.IGNORECASE)

    for level in range(1, 7):
        pattern = re.compile(fr"<\s*h{level}[^>]*>(.*?)</\s*h{level}\s*>", flags=re.IGNORECASE | re.DOTALL)
        text = pattern.sub(lambda m: f"\n{'#' * level} {_strip_html_tags(m.group(1))}\n", text)

    li_pattern = re.compile(r"<\s*li[^>]*>(.*?)</\s*li\s*>", flags=re.IGNORECASE | re.DOTALL)
    text = li_pattern.sub(lambda m: f"\n- {_strip_html_tags(m.group(1))}\n", text)

    block_pattern = re.compile(
        r"<\s*(p|div|section|article|tr)[^>]*>(.*?)</\s*\1\s*>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = block_pattern.sub(lambda m: f"\n{_strip_html_tags(m.group(2))}\n", text)

    text = _strip_html_tags_keep_newlines(text)
    return _normalize_lines(text)


def _is_html_content(url: str, content_type: str, suffix: str) -> bool:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in {"text/html", "application/xhtml+xml"}:
        return True
    parsed = urlparse(url)
    ext = suffix or Path(parsed.path).suffix.lower()
    return ext in {".html", ".htm", ".xhtml"}


def _run_external_html_converter(
    url: str,
    *,
    converter_bin: str,
    converter_tool_id: str,
    timeout_sec: int,
) -> str:
    if not converter_bin.strip():
        return ""
    cmd = [converter_bin.strip(), url]
    if converter_tool_id.strip():
        cmd.append(converter_tool_id.strip())
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout_sec)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _download_one(
    url: str,
    artifact_root: Path,
    timeout_sec: int = 10,
    max_bytes: int = 5_000_000,
    html_mode: str = "markdown",
    html_converter_bin: str = "",
    html_converter_tool_id: str = "url-to-markdown",
    html_convert_timeout_sec: int = 20,
) -> dict:
    artifact_root.mkdir(parents=True, exist_ok=True)
    url_hash = _hash(url)
    meta_path = artifact_root / f"{url_hash}.meta.json"

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            file_path = artifact_root / str(meta.get("file_name", ""))
            cached_mode = str(meta.get("html_mode", ""))
            mode_mismatch = bool(cached_mode and cached_mode != html_mode)
            if file_path.exists() and not mode_mismatch:
                return {
                    "url": url,
                    "status": "cached",
                    "path": str(file_path),
                    "bytes": file_path.stat().st_size,
                }
        except Exception:
            pass

    req = Request(url, headers={"User-Agent": "xlb-topic-index/1.0"})
    with urlopen(req, timeout=timeout_sec) as resp:
        payload = resp.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError(f"artifact too large (> {max_bytes} bytes)")
        ctype = ""
        try:
            ctype = resp.headers.get("Content-Type", "")
        except Exception:
            ctype = ""

    suffix = _choose_suffix(url, ctype)
    if _is_html_content(url, ctype, suffix):
        mode = html_mode if html_mode in {"markdown", "text"} else "markdown"
        external_text = _run_external_html_converter(
            url,
            converter_bin=html_converter_bin,
            converter_tool_id=html_converter_tool_id,
            timeout_sec=html_convert_timeout_sec,
        )
        status = "converted_external"
        if external_text:
            output_text = external_text
        else:
            status = "converted_local"
            raw_html = payload.decode("utf-8", errors="replace")
            output_text = _html_to_markdown(raw_html) if mode == "markdown" else _html_to_text(raw_html)

        html_suffix = ".md" if mode == "markdown" else ".txt"
        file_name = f"{url_hash}{html_suffix}"
        file_path = artifact_root / file_name
        file_path.write_text(output_text, encoding="utf-8")
        byte_count = len(output_text.encode("utf-8"))
        meta = {
            "url": url,
            "hash": url_hash,
            "file_name": file_name,
            "content_type": ctype,
            "bytes": byte_count,
            "artifact_kind": f"html-{mode}",
            "html_mode": mode,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"url": url, "status": status, "path": str(file_path), "bytes": byte_count}

    file_name = f"{url_hash}{suffix}"
    file_path = artifact_root / file_name
    file_path.write_bytes(payload)
    meta = {
        "url": url,
        "hash": url_hash,
        "file_name": file_name,
        "content_type": ctype,
        "bytes": len(payload),
        "artifact_kind": "binary",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"url": url, "status": "downloaded", "path": str(file_path), "bytes": len(payload)}


def fetch_urls_concurrently(
    urls: Iterable[str],
    artifact_root: Path,
    *,
    max_workers: int = 6,
    timeout_sec: int = 10,
    max_bytes: int = 5_000_000,
    html_mode: str = "markdown",
    html_converter_bin: str = "",
    html_converter_tool_id: str = "url-to-markdown",
    html_convert_timeout_sec: int = 20,
) -> list[dict]:
    uniq = []
    seen = set()
    for u in urls:
        uu = (u or "").strip()
        if not uu or uu in seen:
            continue
        seen.add(uu)
        uniq.append(uu)

    if not uniq:
        return []

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {
            pool.submit(
                _download_one,
                url,
                artifact_root,
                timeout_sec,
                max_bytes,
                html_mode,
                html_converter_bin,
                html_converter_tool_id,
                html_convert_timeout_sec,
            ): url
            for url in uniq
        }
        for fut in as_completed(fut_map):
            url = fut_map[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({"url": url, "status": "error", "error": str(exc)})
    return sorted(results, key=lambda x: x.get("url", ""))


def prefetch_from_index(
    db_path: Path,
    query: str,
    artifact_root: Path,
    *,
    limit: int = 8,
    max_workers: int = 6,
    timeout_sec: int = 10,
    max_bytes: int = 5_000_000,
    html_mode: str = "markdown",
    html_converter_bin: str = "",
    html_converter_tool_id: str = "url-to-markdown",
    html_convert_timeout_sec: int = 20,
    require_confirmation: bool = False,
    network_confirmed: bool = False,
) -> dict:
    if require_confirmation and not network_confirmed:
        return {
            "query": query,
            "urls": 0,
            "results": [],
            "downloaded": 0,
            "converted_local": 0,
            "converted_external": 0,
            "cached": 0,
            "errors": 0,
            "skipped": True,
            "skip_reason": "network_confirmation_required",
        }

    hits = search_index(db_path, query, limit=limit)
    urls = [h.get("url", "") for h in hits if h.get("node_type") == "link" and h.get("url")]
    fetched = fetch_urls_concurrently(
        urls,
        artifact_root,
        max_workers=max_workers,
        timeout_sec=timeout_sec,
        max_bytes=max_bytes,
        html_mode=html_mode,
        html_converter_bin=html_converter_bin,
        html_converter_tool_id=html_converter_tool_id,
        html_convert_timeout_sec=html_convert_timeout_sec,
    )
    return {
        "query": query,
        "urls": len(urls),
        "results": fetched,
        "downloaded": sum(1 for r in fetched if r.get("status") == "downloaded"),
        "converted_local": sum(1 for r in fetched if r.get("status") == "converted_local"),
        "converted_external": sum(1 for r in fetched if r.get("status") == "converted_external"),
        "cached": sum(1 for r in fetched if r.get("status") == "cached"),
        "errors": sum(1 for r in fetched if r.get("status") == "error"),
    }


def _load_capability_cache(cache_file: Path, cache_ttl_sec: float) -> dict | None:
    if cache_ttl_sec <= 0:
        return None
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    updated_at = str(data.get("updated_at", ""))
    if not updated_at:
        return None
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_sec = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    if age_sec > cache_ttl_sec:
        return None
    data["source"] = "cache"
    return data


def discover_external_capabilities(cache_file: Path | None = None, cache_ttl_sec: float = 0.0) -> dict:
    if cache_file:
        cached = _load_capability_cache(cache_file, cache_ttl_sec)
        if cached:
            return cached

    result = {"skills": [], "network_skills": [], "mcp_hint": "unknown", "source": "live"}

    def _collect(commands: list[list[str]]) -> tuple[set[str], bool]:
        names: set[str] = set()
        had_runnable = False
        for cmd in commands:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=2)
                had_runnable = True
                if p.returncode != 0:
                    continue
                for line in p.stdout.splitlines():
                    m = re.search(r"\x1b\[[0-9;]*m", line)
                    if m:
                        line = re.sub(r"\x1b\[[0-9;]*m", "", line)
                    m2 = re.match(r"^\s*([a-z0-9][a-z0-9\-]+)\s+~", line.strip())
                    if m2:
                        names.add(m2.group(1))
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                continue
        return names, had_runnable

    direct_commands = [
        ["skills", "list", "--global"],
        ["skills", "list"],
    ]
    fallback_commands = [
        ["npx", "-y", "skills", "list", "--global"],
    ]

    names, had_runnable = _collect(direct_commands)
    if not had_runnable:
        names, _ = _collect(fallback_commands)

    result["skills"] = sorted(names)
    preferred = ["tavily-web", "exa-search", "agent-browser"]
    result["network_skills"] = [n for n in preferred if n in names]
    if result["network_skills"]:
        result["mcp_hint"] = "prefer_skill"
    result["updated_at"] = datetime.now(timezone.utc).isoformat()

    if cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return result


def _cmd_ingest(args: argparse.Namespace) -> int:
    md_text = Path(args.markdown_file).read_text(encoding="utf-8")
    nodes = parse_markdown_to_nodes(md_text, source_title=args.title)
    dataset_root = Path(args.dataset_root) if args.dataset_root else Path(args.db_path).parent
    nodes_jsonl_path = write_nodes_jsonl(nodes, dataset_root, args.snapshot_id)
    topics_json_path = write_topics_json(nodes, dataset_root, args.snapshot_id)
    navigation_json_path = write_navigation_json(nodes, dataset_root, args.snapshot_id)
    vfs_base = ""
    if args.storage_profile == "full":
        if not args.vfs_root:
            raise ValueError("--vfs-root is required when --storage-profile=full")
        vfs_base = str(write_virtual_tree(nodes, Path(args.vfs_root), args.snapshot_id))
    build_index(nodes, Path(args.db_path))
    print(
        json.dumps(
            {
                "nodes": len(nodes),
                "storage_profile": args.storage_profile,
                "vfs_base": vfs_base,
                "db_path": args.db_path,
                "nodes_jsonl": str(nodes_jsonl_path),
                "topics_json": str(topics_json_path),
                "navigation_json": str(navigation_json_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_ingest_if_needed(args: argparse.Namespace) -> int:
    md_file = Path(args.markdown_file)
    meta_path = Path(args.meta_path)
    raw_text = md_file.read_text(encoding="utf-8")
    should, raw_sha = should_ingest(raw_text, meta_path, force=args.force)
    if should:
        nodes = parse_markdown_to_nodes(raw_text, source_title=args.title)
        dataset_root = Path(args.dataset_root) if args.dataset_root else Path(args.db_path).parent
        nodes_jsonl_path = write_nodes_jsonl(nodes, dataset_root, args.snapshot_id)
        topics_json_path = write_topics_json(nodes, dataset_root, args.snapshot_id)
        navigation_json_path = write_navigation_json(nodes, dataset_root, args.snapshot_id)
        vfs_base = ""
        if args.storage_profile == "full":
            if not args.vfs_root:
                raise ValueError("--vfs-root is required when --storage-profile=full")
            vfs_base = str(write_virtual_tree(nodes, Path(args.vfs_root), args.snapshot_id))
        build_index(nodes, Path(args.db_path))
        write_meta(
            meta_path,
            raw_sha=raw_sha,
            title=args.title,
            snapshot_id=args.snapshot_id,
            node_count=len(nodes),
            raw_file=str(md_file),
            vfs_base=vfs_base,
            db_path=str(Path(args.db_path)),
            nodes_jsonl=str(nodes_jsonl_path),
            topics_json=str(topics_json_path),
            navigation_json=str(navigation_json_path),
            storage_profile=args.storage_profile,
        )
        print(
            json.dumps(
                {
                    "ingested": True,
                    "raw_sha": raw_sha,
                    "nodes": len(nodes),
                    "storage_profile": args.storage_profile,
                    "vfs_base": vfs_base,
                    "db_path": args.db_path,
                    "nodes_jsonl": str(nodes_jsonl_path),
                    "topics_json": str(topics_json_path),
                    "navigation_json": str(navigation_json_path),
                    "meta_path": str(meta_path),
                },
                ensure_ascii=False,
            )
        )
    else:
        meta = _load_json(meta_path)
        print(
            json.dumps(
                {
                    "ingested": False,
                    "raw_sha": raw_sha,
                    "db_path": meta.get("db_path", args.db_path),
                    "meta_path": str(meta_path),
                    "vfs_base": meta.get("vfs_base", ""),
                    "nodes_jsonl": meta.get("nodes_jsonl", ""),
                    "topics_json": meta.get("topics_json", ""),
                    "navigation_json": meta.get("navigation_json", ""),
                    "storage_profile": meta.get("storage_profile", args.storage_profile),
                    "raw_file": meta.get("raw_file", str(md_file)),
                },
                ensure_ascii=False,
            )
        )
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    hits = search_index(
        Path(args.db_path),
        args.query,
        args.limit,
        expand_categories=not args.no_expand_categories,
        expand_limit_per_category=args.expand_limit_per_category,
        expand_related_sections=not args.no_expand_related_sections,
        related_limit_per_section=args.related_limit_per_section,
    )
    print(json.dumps({"query": args.query, "hits": hits}, ensure_ascii=False))
    return 0


def _cmd_confirmation_template(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.hits_json_file).read_text(encoding="utf-8"))
    hits = _extract_hits_from_result_payload(payload)
    message = build_network_confirmation_template(
        input_text=args.input,
        query=args.query,
        hits=hits,
        prefetch_enabled=args.prefetch_enabled,
        has_external_route=args.has_external_route,
        preview_limit=args.preview_limit,
    )
    print(message)
    return 0


def _cmd_topic_suggest(args: argparse.Namespace) -> int:
    result = suggest_topics_from_query(
        Path(args.db_path),
        args.query,
        hit_limit=args.hit_limit,
        topic_limit=args.topic_limit,
        sample_per_topic=args.sample_per_topic,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_iterative_search(args: argparse.Namespace) -> int:
    result = iterative_search(
        Path(args.db_path),
        query=args.query,
        limit=args.limit,
        max_iter=args.max_iter,
        gain_threshold=args.gain_threshold,
        low_gain_rounds=args.low_gain_rounds,
        follow_query_nodes=args.follow_query_nodes,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_prefetch(args: argparse.Namespace) -> int:
    result = prefetch_from_index(
        Path(args.db_path),
        args.query,
        Path(args.artifact_root),
        limit=args.limit,
        max_workers=args.max_workers,
        timeout_sec=args.timeout_sec,
        max_bytes=args.max_bytes,
        html_mode=args.html_mode,
        html_converter_bin=args.html_converter_bin,
        html_converter_tool_id=args.html_converter_tool_id,
        html_convert_timeout_sec=args.html_convert_timeout_sec,
        require_confirmation=args.require_confirmation,
        network_confirmed=args.network_confirmed,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cmd_open_url(args: argparse.Namespace) -> int:
    result = open_url_in_local_app(
        url=args.url,
        app=args.app,
        strip_fragment=not args.keep_fragment,
        dry_run=bool(args.dry_run),
        atlas_app_path=args.atlas_app_path,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in {"opened", "dry_run"} else 1


def _cmd_open_hits(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.hits_json_file).read_text(encoding="utf-8"))
    urls = extract_link_urls_from_hits_payload(payload, limit=max(1, int(args.limit)))
    result = open_urls_in_local_app(
        urls,
        app=args.app,
        strip_fragment=not args.keep_fragment,
        dry_run=bool(args.dry_run),
        atlas_app_path=args.atlas_app_path,
        delay_between_sec=float(args.delay_between_sec),
        stop_on_error=bool(args.stop_on_error),
    )
    result["hits_json_file"] = args.hits_json_file
    print(json.dumps(result, ensure_ascii=False))
    return 0 if int(result.get("errors", 0)) == 0 else 1


def _cmd_discover(args: argparse.Namespace) -> int:
    cache_file = Path(args.cache_file) if args.cache_file else None
    payload = discover_external_capabilities(cache_file=cache_file, cache_ttl_sec=args.cache_ttl_sec)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _cmd_describe_cache(args: argparse.Namespace) -> int:
    meta = _load_json(Path(args.meta_path))
    if not meta:
        print(json.dumps({"error": "meta_not_found", "meta_path": args.meta_path}, ensure_ascii=False))
        return 1

    if args.format == "json":
        print(json.dumps(meta, ensure_ascii=False))
        return 0

    lines = [
        "# XLB Cache Contract",
        "",
        f"- title: `{meta.get('title', '')}`",
        f"- snapshot_id: `{meta.get('snapshot_id', '')}`",
        f"- storage_profile: `{meta.get('storage_profile', 'full')}`",
        f"- raw_file: `{meta.get('raw_file', '')}`",
        f"- vfs_base: `{meta.get('vfs_base', '')}`",
        f"- db_path: `{meta.get('db_path', '')}`",
        f"- nodes_jsonl: `{meta.get('nodes_jsonl', '')}`",
        f"- topics_json: `{meta.get('topics_json', '')}`",
        f"- navigation_json: `{meta.get('navigation_json', '')}`",
        "",
        "## How Other Skills Can Use",
        "1. Read `nodes_jsonl` (JSONL, one node per line) for file-based grep and generic program integration.",
        "2. Read `topics_json` for topic-level summary and fast routing.",
        "3. Read `navigation_json` for executable discovery edges:",
        "   - `topic_navigation`: next-topic commands (usually from `searchin`)",
        "   - `knowledge_search`: in-topic search commands (usually from `command`)",
        "4. Query sqlite index for fast retrieval:",
        f"   - `python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py search --db-path \"{meta.get('db_path', '')}\" --query \"<keyword>\" --limit 8`",
        "5. Auto exploration helper:",
        f"   - `python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py explore-next --meta-path \"{args.meta_path}\" --strategy topic_first --dry-run`",
        "6. Graph backlink helper (`->topic/:`):",
        f"   - `python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py graph-neighbors --index-dir \"{Path(meta.get('db_path', '')).parent}\" --target-title \"-><topic>/:\"`",
        "7. Budgeted explore loop helper:",
        "   - `python3 skills/xlb-topic-index/scripts/xlb_rag_pipeline.py explore-loop --seed-input \"xlb ><topic>/:\" --edge-strategy searchin_command_backlink --max-steps 12 --max-depth 4 --max-seconds 90`",
        "8. Optional: if `storage_profile=full`, use `vfs_base` for folder-style navigation.",
    ]
    print("\n".join(lines))
    return 0


def _cmd_explore_next(args: argparse.Namespace) -> int:
    meta = _load_json(Path(args.meta_path))
    if not meta:
        print(json.dumps({"error": "meta_not_found", "meta_path": args.meta_path}, ensure_ascii=False))
        return 1

    navigation_path_raw = str(meta.get("navigation_json", "")).strip()
    if not navigation_path_raw:
        print(
            json.dumps(
                {
                    "error": "navigation_json_missing",
                    "meta_path": args.meta_path,
                    "hint": "re-ingest with current pipeline version to generate navigation.json",
                },
                ensure_ascii=False,
            )
        )
        return 1

    navigation_path = Path(navigation_path_raw)
    if not navigation_path.exists():
        print(
            json.dumps(
                {
                    "error": "navigation_json_not_found",
                    "navigation_json": str(navigation_path),
                    "meta_path": args.meta_path,
                },
                ensure_ascii=False,
            )
        )
        return 1

    nav_payload = _load_json(navigation_path)
    candidates = build_navigation_candidates(
        nav_payload,
        strategy=args.strategy,
        include_other_queries=args.include_other_queries,
    )

    visited_path = Path(args.visited_file) if args.visited_file else None
    visited = load_visited_exec_titles(visited_path) if visited_path else set()
    filtered: list[dict] = []
    for item in candidates:
        key = _canonical_edge_key(str(item.get("query_exec_title", "")))
        if key and key in visited:
            continue
        filtered.append(item)

    result = {
        "meta_path": args.meta_path,
        "navigation_json": str(navigation_path),
        "strategy": args.strategy,
        "include_other_queries": bool(args.include_other_queries),
        "candidate_count": len(filtered),
        "candidates": [
            {
                "title": str(c.get("title", "")),
                "query_kind": str(c.get("query_kind", "")),
                "query_source": str(c.get("query_source", "")),
                "query_cmd": str(c.get("query_cmd", "")),
                "query_exec_title": str(c.get("query_exec_title", "")),
                "input": _to_input_from_exec_title(str(c.get("query_exec_title", ""))),
            }
            for c in filtered[: max(1, args.max_candidates)]
        ],
        "dry_run": bool(args.dry_run),
    }

    if not filtered:
        result["status"] = "no_candidate"
        print(json.dumps(result, ensure_ascii=False))
        return 0

    idx = max(0, int(args.select_index))
    if idx >= len(filtered):
        result["status"] = "invalid_select_index"
        result["selected_index"] = idx
        result["max_index"] = len(filtered) - 1
        print(json.dumps(result, ensure_ascii=False))
        return 1

    selected = filtered[idx]
    exec_title = str(selected.get("query_exec_title", "")).strip()
    next_input = _to_input_from_exec_title(exec_title)
    selected_payload = {
        "title": str(selected.get("title", "")),
        "query_kind": str(selected.get("query_kind", "")),
        "query_source": str(selected.get("query_source", "")),
        "query_cmd": str(selected.get("query_cmd", "")),
        "query_exec_title": exec_title,
        "input": next_input,
    }
    result["selected_index"] = idx
    result["selected"] = selected_payload

    if args.dry_run:
        result["status"] = "selected"
        print(json.dumps(result, ensure_ascii=False))
        return 0

    run_result = run_retrieve_for_explore(
        input_text=next_input,
        output_mode=args.output_mode,
        storage_profile=args.storage_profile,
        network_confirmed=args.network_confirmed,
    )
    result["execute"] = run_result
    result["status"] = "executed" if int(run_result.get("returncode", 1)) == 0 else "execute_failed"

    if visited_path and args.update_visited and run_result.get("returncode", 1) == 0:
        key = _canonical_edge_key(exec_title)
        if key:
            visited.add(key)
            save_visited_exec_titles(visited_path, visited)
            result["visited_file"] = str(visited_path)
            result["visited_count"] = len(visited)

    print(json.dumps(result, ensure_ascii=False))
    return 0 if run_result.get("returncode", 1) == 0 else 1


def _cmd_resolve_input(args: argparse.Namespace) -> int:
    title = resolve_title_from_input(args.input)
    payload = {"input": args.input, "title": title, "hash": title_hash(title)}
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _cmd_graph_neighbors(args: argparse.Namespace) -> int:
    payload = graph_neighbors(
        Path(args.index_dir),
        args.target_title,
        limit=max(1, int(args.limit)),
        query_filter=str(args.query_filter or ""),
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _cmd_explore_loop(args: argparse.Namespace) -> int:
    visited_exec_path = Path(args.visited_file) if args.visited_file else None
    visited_topic_path = Path(args.visited_topics_file) if args.visited_topics_file else None
    visited_exec = load_visited_exec_titles(visited_exec_path) if visited_exec_path else set()
    visited_topics = load_visited_topic_keys(visited_topic_path) if visited_topic_path else set()

    idx_dir = Path(args.index_dir) if args.index_dir else None
    payload = explore_loop(
        seed_input=args.seed_input,
        max_steps=max(1, int(args.max_steps)),
        max_depth=max(0, int(args.max_depth)),
        max_seconds=float(args.max_seconds),
        edge_strategy=args.edge_strategy,
        include_backlinks=bool(args.include_backlinks),
        include_other_queries=bool(args.include_other_queries),
        max_branching=max(1, int(args.max_branching)),
        backlink_limit=max(1, int(args.backlink_limit)),
        backlink_filter=str(args.backlink_filter or ""),
        visited_exec_titles=visited_exec,
        visited_topic_keys=visited_topics,
        storage_profile=args.storage_profile,
        network_confirmed=args.network_confirmed,
        index_dir=idx_dir,
    )

    if args.update_visited:
        if visited_exec_path:
            save_visited_exec_titles(visited_exec_path, set(payload.get("visited_exec_titles", [])))
            payload["visited_file"] = str(visited_exec_path)
        if visited_topic_path:
            save_visited_topic_keys(visited_topic_path, set(payload.get("visited_topic_keys", [])))
            payload["visited_topics_file"] = str(visited_topic_path)

    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _cmd_auto_explore(args: argparse.Namespace) -> int:
    seed_input = normalize_auto_explore_seed(args.input)
    if not seed_input:
        print(
            json.dumps(
                {"mode": "auto_explore", "status": "invalid_input", "input": args.input, "error": "empty_auto_input"},
                ensure_ascii=False,
            )
        )
        return 1

    visited_exec_path = Path(args.visited_file) if args.visited_file else None
    visited_topic_path = Path(args.visited_topics_file) if args.visited_topics_file else None
    visited_exec = load_visited_exec_titles(visited_exec_path) if visited_exec_path else set()
    visited_topics = load_visited_topic_keys(visited_topic_path) if visited_topic_path else set()
    idx_dir = Path(args.index_dir) if args.index_dir else None

    payload = explore_loop(
        seed_input=seed_input,
        max_steps=max(1, int(args.max_steps)),
        max_depth=max(0, int(args.max_depth)),
        max_seconds=float(args.max_seconds),
        edge_strategy=args.edge_strategy,
        include_backlinks=bool(args.include_backlinks),
        include_other_queries=bool(args.include_other_queries),
        max_branching=max(1, int(args.max_branching)),
        backlink_limit=max(1, int(args.backlink_limit)),
        backlink_filter=str(args.backlink_filter or ""),
        visited_exec_titles=visited_exec,
        visited_topic_keys=visited_topics,
        storage_profile=args.storage_profile,
        network_confirmed=args.network_confirmed,
        index_dir=idx_dir,
    )
    payload["mode"] = "auto_explore"
    payload["auto_input"] = args.input
    payload["auto_seed_input"] = seed_input

    if args.update_visited:
        if visited_exec_path:
            save_visited_exec_titles(visited_exec_path, set(payload.get("visited_exec_titles", [])))
            payload["visited_file"] = str(visited_exec_path)
        if visited_topic_path:
            save_visited_topic_keys(visited_topic_path, set(payload.get("visited_topic_keys", [])))
            payload["visited_topics_file"] = str(visited_topic_path)

    print(json.dumps(payload, ensure_ascii=False))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="XLB markdown -> VFS/Index pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest")
    ingest.add_argument("--markdown-file", required=True)
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--vfs-root", default="")
    ingest.add_argument("--dataset-root", default="")
    ingest.add_argument("--storage-profile", choices=["minimal", "full"], default="minimal")
    ingest.add_argument("--snapshot-id", required=True)
    ingest.add_argument("--db-path", required=True)
    ingest.set_defaults(func=_cmd_ingest)

    ingest_if_needed = sub.add_parser("ingest-if-needed")
    ingest_if_needed.add_argument("--markdown-file", required=True)
    ingest_if_needed.add_argument("--meta-path", required=True)
    ingest_if_needed.add_argument("--title", required=True)
    ingest_if_needed.add_argument("--vfs-root", default="")
    ingest_if_needed.add_argument("--dataset-root", default="")
    ingest_if_needed.add_argument("--storage-profile", choices=["minimal", "full"], default="minimal")
    ingest_if_needed.add_argument("--snapshot-id", required=True)
    ingest_if_needed.add_argument("--db-path", required=True)
    ingest_if_needed.add_argument("--force", action="store_true")
    ingest_if_needed.set_defaults(func=_cmd_ingest_if_needed)

    search = sub.add_parser("search")
    search.add_argument("--db-path", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--no-expand-categories", action="store_true")
    search.add_argument("--expand-limit-per-category", type=int, default=200)
    search.add_argument("--no-expand-related-sections", action="store_true")
    search.add_argument("--related-limit-per-section", type=int, default=20)
    search.set_defaults(func=_cmd_search)

    confirmation = sub.add_parser("confirmation-template")
    confirmation.add_argument("--input", required=True)
    confirmation.add_argument("--query", default="")
    confirmation.add_argument("--hits-json-file", required=True)
    confirmation.add_argument("--prefetch-enabled", action="store_true")
    confirmation.add_argument("--has-external-route", action="store_true")
    confirmation.add_argument("--preview-limit", type=int, default=3)
    confirmation.set_defaults(func=_cmd_confirmation_template)

    suggest = sub.add_parser("topic-suggest")
    suggest.add_argument("--db-path", required=True)
    suggest.add_argument("--query", default="")
    suggest.add_argument("--hit-limit", type=int, default=200)
    suggest.add_argument("--topic-limit", type=int, default=10)
    suggest.add_argument("--sample-per-topic", type=int, default=3)
    suggest.set_defaults(func=_cmd_topic_suggest)

    iterative = sub.add_parser("iterative-search")
    iterative.add_argument("--db-path", required=True)
    iterative.add_argument("--query", required=True)
    iterative.add_argument("--limit", type=int, default=8)
    iterative.add_argument("--max-iter", type=int, default=5)
    iterative.add_argument("--gain-threshold", type=float, default=0.05)
    iterative.add_argument("--low-gain-rounds", type=int, default=3)
    iterative.add_argument("--follow-query-nodes", action="store_true", default=True)
    iterative.add_argument("--no-follow-query-nodes", dest="follow_query_nodes", action="store_false")
    iterative.set_defaults(func=_cmd_iterative_search)

    prefetch = sub.add_parser("prefetch")
    prefetch.add_argument("--db-path", required=True)
    prefetch.add_argument("--query", required=True)
    prefetch.add_argument("--artifact-root", required=True)
    prefetch.add_argument("--limit", type=int, default=8)
    prefetch.add_argument("--max-workers", type=int, default=6)
    prefetch.add_argument("--timeout-sec", type=int, default=10)
    prefetch.add_argument("--max-bytes", type=int, default=5_000_000)
    prefetch.add_argument("--html-mode", choices=["markdown", "text"], default="markdown")
    prefetch.add_argument("--html-converter-bin", default="")
    prefetch.add_argument("--html-converter-tool-id", default="url-to-markdown")
    prefetch.add_argument("--html-convert-timeout-sec", type=int, default=20)
    prefetch.add_argument("--require-confirmation", action="store_true")
    prefetch.add_argument("--network-confirmed", action="store_true")
    prefetch.set_defaults(func=_cmd_prefetch)

    open_url = sub.add_parser("open-url")
    open_url.add_argument("--url", required=True)
    open_url.add_argument("--app", choices=["chrome", "dia", "atlas", "default"], default="chrome")
    open_url.add_argument("--atlas-app-path", default="/Applications/ChatGPT Atlas.app")
    open_url.add_argument("--keep-fragment", action="store_true")
    open_url.add_argument("--dry-run", action="store_true")
    open_url.set_defaults(func=_cmd_open_url)

    open_hits = sub.add_parser("open-hits")
    open_hits.add_argument("--hits-json-file", required=True)
    open_hits.add_argument("--limit", type=int, default=3)
    open_hits.add_argument("--app", choices=["chrome", "dia", "atlas", "default"], default="chrome")
    open_hits.add_argument("--atlas-app-path", default="/Applications/ChatGPT Atlas.app")
    open_hits.add_argument("--keep-fragment", action="store_true")
    open_hits.add_argument("--dry-run", action="store_true")
    open_hits.add_argument("--delay-between-sec", type=float, default=0.2)
    open_hits.add_argument("--stop-on-error", action="store_true")
    open_hits.set_defaults(func=_cmd_open_hits)

    discover = sub.add_parser("discover")
    discover.add_argument("--cache-file", default="")
    discover.add_argument("--cache-ttl-sec", type=float, default=0.0)
    discover.set_defaults(func=_cmd_discover)

    describe = sub.add_parser("describe-cache")
    describe.add_argument("--meta-path", required=True)
    describe.add_argument("--format", choices=["json", "markdown"], default="markdown")
    describe.set_defaults(func=_cmd_describe_cache)

    explore = sub.add_parser("explore-next")
    explore.add_argument("--meta-path", required=True)
    explore.add_argument("--strategy", choices=["topic_first", "search_first", "mixed"], default="topic_first")
    explore.add_argument("--include-other-queries", action="store_true")
    explore.add_argument("--visited-file", default="")
    explore.add_argument("--update-visited", action="store_true")
    explore.add_argument("--select-index", type=int, default=0)
    explore.add_argument("--max-candidates", type=int, default=10)
    explore.add_argument("--dry-run", action="store_true")
    explore.add_argument("--output-mode", choices=["json", "auto"], default="json")
    explore.add_argument("--storage-profile", choices=["minimal", "full"], default="minimal")
    explore.add_argument("--network-confirmed", action="store_true")
    explore.set_defaults(func=_cmd_explore_next)

    graph = sub.add_parser("graph-neighbors")
    graph.add_argument("--index-dir", required=True)
    graph.add_argument("--target-title", required=True)
    graph.add_argument("--limit", type=int, default=100)
    graph.add_argument("--query-filter", default="")
    graph.set_defaults(func=_cmd_graph_neighbors)

    loop = sub.add_parser("explore-loop")
    loop.add_argument("--seed-input", required=True)
    loop.add_argument(
        "--edge-strategy",
        choices=["searchin_command_backlink", "command_searchin_backlink", "mixed_backlink"],
        default="searchin_command_backlink",
    )
    loop.add_argument("--max-steps", type=int, default=12)
    loop.add_argument("--max-depth", type=int, default=4)
    loop.add_argument("--max-seconds", type=float, default=90.0)
    loop.add_argument("--max-branching", type=int, default=6)
    loop.add_argument("--backlink-limit", type=int, default=30)
    loop.add_argument("--backlink-filter", default="")
    loop.add_argument("--include-other-queries", action="store_true")
    loop.add_argument("--include-backlinks", action="store_true", default=True)
    loop.add_argument("--no-backlinks", dest="include_backlinks", action="store_false")
    loop.add_argument("--visited-file", default="")
    loop.add_argument("--visited-topics-file", default="")
    loop.add_argument("--update-visited", action="store_true")
    loop.add_argument("--storage-profile", choices=["minimal", "full"], default="minimal")
    loop.add_argument("--network-confirmed", action="store_true")
    loop.add_argument("--index-dir", default="")
    loop.set_defaults(func=_cmd_explore_loop)

    auto = sub.add_parser("auto-explore")
    auto.add_argument("--input", required=True)
    auto.add_argument(
        "--edge-strategy",
        choices=["searchin_command_backlink", "command_searchin_backlink", "mixed_backlink"],
        default="searchin_command_backlink",
    )
    auto.add_argument("--max-steps", type=int, default=12)
    auto.add_argument("--max-depth", type=int, default=4)
    auto.add_argument("--max-seconds", type=float, default=90.0)
    auto.add_argument("--max-branching", type=int, default=6)
    auto.add_argument("--backlink-limit", type=int, default=30)
    auto.add_argument("--backlink-filter", default="")
    auto.add_argument("--include-other-queries", action="store_true")
    auto.add_argument("--include-backlinks", action="store_true", default=True)
    auto.add_argument("--no-backlinks", dest="include_backlinks", action="store_false")
    auto.add_argument("--visited-file", default="")
    auto.add_argument("--visited-topics-file", default="")
    auto.add_argument("--update-visited", action="store_true")
    auto.add_argument("--storage-profile", choices=["minimal", "full"], default="minimal")
    auto.add_argument("--network-confirmed", action="store_true")
    auto.add_argument("--index-dir", default="")
    auto.set_defaults(func=_cmd_auto_explore)

    resolve = sub.add_parser("resolve-input")
    resolve.add_argument("--input", required=True)
    resolve.set_defaults(func=_cmd_resolve_input)
    return p


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
