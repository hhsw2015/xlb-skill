import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xlb_rag_pipeline import (
    PIPELINE_VERSION,
    Node,
    build_explore_candidates,
    build_navigation_candidates,
    build_network_confirmation_template,
    build_index,
    discover_external_capabilities,
    extract_link_urls_from_hits_payload,
    explore_loop,
    fetch_urls_concurrently,
    graph_neighbors,
    graph_neighbors_from_edges,
    iterative_search,
    load_visited_exec_titles,
    normalize_auto_explore_seed,
    open_url_in_local_app,
    open_urls_in_local_app,
    parse_markdown_to_nodes,
    prefetch_from_index,
    resolve_title_from_input,
    root_topic_from_title,
    save_visited_exec_titles,
    search_index,
    suggest_topics_from_query,
    should_ingest,
    write_meta,
    write_navigation_json,
    write_nodes_jsonl,
    write_topics_json,
    write_virtual_tree,
)


SAMPLE_MD = """# Vibe Coding
## github:
### openai/codex
### https://www.github.com/openai/codex#openai/codex
## command:
### CLI
### >>Vibe Coding/CLI
## website:
- https://example.com/page#Example Page
"""


class XlbRagPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="xlb-rag-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_resolve_title_from_input(self) -> None:
        self.assertEqual(resolve_title_from_input("xlb >vibe coding/coding"), ">vibe coding/coding")
        self.assertEqual(resolve_title_from_input("xlb ??vibe coding"), "??vibe coding")
        self.assertEqual(resolve_title_from_input("xlb ->vibe coding/:"), "->vibe coding/:")
        self.assertEqual(resolve_title_from_input("查询xlb vibe coding主题"), ">vibe coding/")

    def test_normalize_auto_explore_seed(self) -> None:
        self.assertEqual(normalize_auto_explore_seed("vibe coding"), "xlb >vibe coding/:")
        self.assertEqual(normalize_auto_explore_seed("auto vibe coding"), "xlb >vibe coding/:")
        self.assertEqual(normalize_auto_explore_seed("xlb auto vibe coding"), "xlb >vibe coding/:")
        self.assertEqual(normalize_auto_explore_seed(">vibe coding/"), "xlb >vibe coding/:")
        self.assertEqual(normalize_auto_explore_seed("xlb >vibe coding/"), "xlb >vibe coding/:")
        self.assertEqual(normalize_auto_explore_seed(""), "")

    def test_extract_link_urls_from_hits_payload(self) -> None:
        payload = {
            "query": "codex",
            "hits": [
                {"node_type": "link", "url": "https://a.example.com/x#frag"},
                {"node_type": "query", "query_cmd": ">A"},
                {"node_type": "link", "url": "https://a.example.com/x#frag"},
                {"node_type": "link", "url": "https://b.example.com/y"},
            ],
        }
        urls = extract_link_urls_from_hits_payload(payload, limit=5)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], "https://a.example.com/x#frag")
        self.assertEqual(urls[1], "https://b.example.com/y")

    def test_open_url_in_local_app_dry_run(self) -> None:
        result = open_url_in_local_app(
            url="https://example.com/page#frag",
            app="chrome",
            strip_fragment=True,
            dry_run=True,
        )
        self.assertEqual(result.get("status"), "dry_run")
        self.assertEqual(result.get("normalized_url"), "https://example.com/page")
        actions = result.get("actions", [])
        self.assertTrue(any(a.get("kind") == "cmd" for a in actions))

    def test_open_urls_in_local_app_dry_run(self) -> None:
        result = open_urls_in_local_app(
            ["https://example.com/a#x", "https://example.com/a#x", "https://example.com/b"],
            app="atlas",
            strip_fragment=True,
            dry_run=True,
        )
        self.assertEqual(result.get("count"), 2)
        self.assertEqual(result.get("errors"), 0)
        self.assertEqual(result.get("opened"), 2)
        first = result.get("results", [])[0]
        self.assertEqual(first.get("status"), "dry_run")

    def test_parse_markdown_to_nodes(self) -> None:
        nodes = parse_markdown_to_nodes(SAMPLE_MD, source_title=">vibe coding/coding")
        self.assertTrue(any(n.node_type == "link" and "openai/codex" in n.title for n in nodes))
        self.assertTrue(any(n.node_type == "query" and n.query_cmd == ">>Vibe Coding/CLI" for n in nodes))
        self.assertTrue(any(n.node_type == "link" and n.url == "https://example.com/page" for n in nodes))

    def test_write_virtual_tree(self) -> None:
        nodes = parse_markdown_to_nodes(SAMPLE_MD, source_title=">vibe coding/coding")
        base = write_virtual_tree(nodes, self.tmp_dir / "vfs", snapshot_id="snap-1")
        self.assertTrue((base / "data_structure.md").exists())
        self.assertTrue(any(p.name.endswith(".link.md") for p in base.rglob("*.link.md")))
        self.assertTrue(any(p.name.endswith(".query.txt") for p in base.rglob("*.query.txt")))
        stale = base / "stale-dir" / "old.txt"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("old", encoding="utf-8")
        self.assertTrue(stale.exists())
        base2 = write_virtual_tree(nodes, self.tmp_dir / "vfs", snapshot_id="snap-1")
        self.assertEqual(base2, base)
        self.assertFalse(stale.exists())

    def test_write_json_dataset(self) -> None:
        nodes = parse_markdown_to_nodes(SAMPLE_MD, source_title=">vibe coding/coding")
        dataset_root = self.tmp_dir / "dataset"
        jsonl = write_nodes_jsonl(nodes, dataset_root, snapshot_id="snap-1")
        topics = write_topics_json(nodes, dataset_root, snapshot_id="snap-1")
        nav = write_navigation_json(nodes, dataset_root, snapshot_id="snap-1")
        self.assertTrue(jsonl.exists())
        self.assertTrue(topics.exists())
        self.assertTrue(nav.exists())
        first_line = jsonl.read_text(encoding="utf-8").splitlines()[0]
        self.assertIn('"node_type"', first_line)
        topics_obj = json.loads(topics.read_text(encoding="utf-8"))
        self.assertGreaterEqual(topics_obj.get("topic_count", 0), 1)
        nav_obj = json.loads(nav.read_text(encoding="utf-8"))
        self.assertIn("topic_navigation", nav_obj)
        self.assertIn("knowledge_search", nav_obj)

    def test_query_edges_are_classified_and_executable(self) -> None:
        md = """# Topic
## searchin:
### AI Model
### >AI Model
## command:
### vibe coding
### search(>vibe coding/vibe)
"""
        nodes = parse_markdown_to_nodes(md, source_title="??topic")
        q_nodes = [n for n in nodes if n.node_type == "query"]
        self.assertEqual(len(q_nodes), 2)
        searchin_node = next(n for n in q_nodes if n.query_source == "searchin")
        command_node = next(n for n in q_nodes if n.query_source == "command")
        self.assertEqual(searchin_node.query_kind, "topic_nav")
        self.assertEqual(searchin_node.query_exec_title, ">AI Model/")
        self.assertEqual(command_node.query_kind, "kb_search")
        self.assertEqual(command_node.query_exec_title, ">vibe coding/vibe")

    def test_build_navigation_candidates_strategy(self) -> None:
        nav = {
            "topic_navigation": [
                {"query_exec_title": ">AI Model/", "query_kind": "topic_nav", "query_source": "searchin"},
                {"query_exec_title": ">Agent Skills/", "query_kind": "topic_nav", "query_source": "searchin"},
            ],
            "knowledge_search": [
                {"query_exec_title": ">vibe coding/vibe", "query_kind": "kb_search", "query_source": "command"},
            ],
            "other_queries": [
                {"query_exec_title": "??vibe coding", "query_kind": "topic_lookup", "query_source": "search"},
            ],
        }
        topic_first = build_navigation_candidates(nav, strategy="topic_first", include_other_queries=False)
        self.assertEqual(topic_first[0]["query_exec_title"], ">AI Model/")
        self.assertEqual(topic_first[1]["query_exec_title"], ">Agent Skills/")
        self.assertEqual(topic_first[2]["query_exec_title"], ">vibe coding/vibe")

        search_first = build_navigation_candidates(nav, strategy="search_first", include_other_queries=False)
        self.assertEqual(search_first[0]["query_exec_title"], ">vibe coding/vibe")
        self.assertEqual(search_first[1]["query_exec_title"], ">AI Model/")

        mixed = build_navigation_candidates(nav, strategy="mixed", include_other_queries=True)
        self.assertEqual(mixed[0]["query_exec_title"], ">AI Model/")
        self.assertEqual(mixed[1]["query_exec_title"], ">vibe coding/vibe")
        self.assertTrue(any(x.get("query_exec_title") == "??vibe coding" for x in mixed))

    def test_root_topic_from_title(self) -> None:
        self.assertEqual(root_topic_from_title(">vibe coding/"), "vibe coding")
        self.assertEqual(root_topic_from_title(">vibe coding/searchin:"), "vibe coding")
        self.assertEqual(root_topic_from_title(">vibe coding/command:"), "vibe coding")
        self.assertEqual(root_topic_from_title("xlb ->vibe coding/:"), "vibe coding")

    def test_build_explore_candidates_priority_and_dedupe(self) -> None:
        searchin_nav = {
            "topic_navigation": [
                {
                    "title": "AI Model",
                    "query_kind": "topic_nav",
                    "query_source": "searchin",
                    "query_cmd": ">AI Model",
                    "query_exec_title": ">AI Model/",
                }
            ],
            "knowledge_search": [],
            "other_queries": [],
        }
        command_nav = {
            "topic_navigation": [],
            "knowledge_search": [
                {
                    "title": "codex cli",
                    "query_kind": "kb_search",
                    "query_source": "command",
                    "query_cmd": "search(>vibe coding/codex cli)",
                    "query_exec_title": ">vibe coding/codex cli",
                }
            ],
            "other_queries": [],
        }
        candidates = build_explore_candidates(
            searchin_navigation=searchin_nav,
            command_navigation=command_nav,
            backlink_inputs=["xlb >Awesome Search/"],
            visited_exec_titles={">ai model/"},
            visited_topic_keys={"awesome search"},
            edge_strategy="searchin_command_backlink",
            max_candidates=10,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].get("query_kind"), "kb_search")
        self.assertEqual(candidates[0].get("input"), "xlb >vibe coding/codex cli")

    def test_explore_loop_budget_and_section_usage(self) -> None:
        calls: list[str] = []

        def fake_run(**kwargs):
            input_text = kwargs.get("input_text", "")
            calls.append(str(input_text))
            normalized = str(input_text).strip().lower()

            nav = {"topic_navigation": [], "knowledge_search": [], "other_queries": []}
            title = ">Seed/:"
            if normalized == "xlb >seed/searchin:":
                title = ">Seed/searchin:"
                nav = {
                    "topic_navigation": [
                        {
                            "title": "Topic A",
                            "query_kind": "topic_nav",
                            "query_source": "searchin",
                            "query_cmd": ">Topic A",
                            "query_exec_title": ">Topic A/",
                        }
                    ],
                    "knowledge_search": [],
                    "other_queries": [],
                }
            elif normalized == "xlb >seed/command:":
                title = ">Seed/command:"
                nav = {
                    "topic_navigation": [],
                    "knowledge_search": [
                        {
                            "title": "seed deep",
                            "query_kind": "kb_search",
                            "query_source": "command",
                            "query_cmd": "search(>seed/deep)",
                            "query_exec_title": ">seed/deep",
                        }
                    ],
                    "other_queries": [],
                }
            elif normalized == "xlb >topic a/searchin:":
                title = ">Topic A/searchin:"
            elif normalized == "xlb >topic a/command:":
                title = ">Topic A/command:"
            elif normalized == "xlb >upstream/searchin:":
                title = ">Upstream/searchin:"
            elif normalized == "xlb >upstream/command:":
                title = ">Upstream/command:"
            elif normalized == "xlb >topic a/":
                title = ">Topic A/"
            elif normalized == "xlb >upstream/":
                title = ">Upstream/"

            return {
                "returncode": 0,
                "stderr": "",
                "stdout": "",
                "parsed_output": {
                    "mode": "raw_reference",
                    "title": title,
                    "meta_file": "",
                    "navigation_payload": nav,
                },
            }

        def fake_graph(index_dir, target_title, *, limit=100, query_filter=""):
            return {
                "follow_up_inputs": {
                    "upstream_topics": ["xlb >Upstream/"],
                    "outbound_queries": [],
                }
            }

        payload = explore_loop(
            seed_input="xlb >Seed/:",
            max_steps=1,
            max_depth=4,
            max_seconds=30,
            edge_strategy="searchin_command_backlink",
            include_backlinks=True,
            max_branching=5,
            visited_exec_titles=set(),
            visited_topic_keys=set(),
            run_fn=fake_run,
            graph_fn=fake_graph,
        )

        self.assertEqual(payload.get("stop_reason"), "step_budget_exhausted")
        self.assertEqual(payload.get("steps_executed"), 1)
        self.assertGreaterEqual(payload.get("frontier_remaining", 0), 1)
        self.assertIn("xlb >Seed/searchin:", calls)
        self.assertIn("xlb >Seed/command:", calls)
        first_hop = payload.get("trace", [])[0]
        enqueued_inputs = [x.get("input") for x in first_hop.get("enqueued", [])]
        self.assertEqual(enqueued_inputs[0], "xlb >Topic A/")
        self.assertIn("xlb >seed/deep", enqueued_inputs)
        self.assertIn("xlb >Upstream/", enqueued_inputs)

    def test_graph_neighbors_from_edges(self) -> None:
        edges = [
            {
                "topic": "Awesome Search",
                "section": "searchin",
                "title": "Vibe Coding",
                "query_cmd": ">Vibe Coding",
                "query_exec_title": ">Vibe Coding/",
                "query_kind": "topic_nav",
                "query_source": "searchin",
            },
            {
                "topic": "Vibe Coding",
                "section": "searchin",
                "title": "AI Model",
                "query_cmd": ">AI Model",
                "query_exec_title": ">AI Model/",
                "query_kind": "topic_nav",
                "query_source": "searchin",
            },
            {
                "topic": "Vibe Coding",
                "section": "command",
                "title": "codex cli",
                "query_cmd": "search(>vibe coding/codex cli)",
                "query_exec_title": ">vibe coding/codex cli",
                "query_kind": "kb_search",
                "query_source": "command",
            },
        ]
        payload = graph_neighbors_from_edges(edges, target_title="->vibe coding/:", limit=20)
        self.assertEqual(payload.get("canonical_target"), ">vibe coding")
        self.assertEqual(payload.get("inbound_edge_count"), 1)
        self.assertEqual(payload.get("outbound_edge_count"), 2)
        self.assertEqual(payload.get("upstream_topics", [])[0].get("topic"), "Awesome Search")
        self.assertIn("xlb >Awesome Search/", payload.get("follow_up_inputs", {}).get("upstream_topics", []))
        self.assertIn("xlb >AI Model/", payload.get("follow_up_inputs", {}).get("outbound_queries", []))
        self.assertIn("xlb >vibe coding/codex cli", payload.get("follow_up_inputs", {}).get("outbound_queries", []))

    def test_graph_neighbors_reads_index_dir(self) -> None:
        md = """# Awesome Search
## searchin:
### Vibe Coding
### >Vibe Coding
# Vibe Coding
## searchin:
### AI Model
### >AI Model
## command:
### codex cli
### search(>vibe coding/codex cli)
"""
        nodes = parse_markdown_to_nodes(md, source_title=">seed/")
        index_dir = self.tmp_dir / "index"
        index_dir.mkdir(parents=True, exist_ok=True)
        db_path = index_dir / "a.db"
        build_index(nodes, db_path)

        payload = graph_neighbors(index_dir, "->vibe coding/:", limit=50)
        self.assertEqual(payload.get("mode"), "graph_neighbors")
        self.assertGreaterEqual(payload.get("edge_pool_size", 0), 3)
        self.assertEqual(payload.get("inbound_edge_count"), 1)
        self.assertEqual(payload.get("outbound_edge_count"), 2)
        self.assertTrue(any(t.get("topic") == "Awesome Search" for t in payload.get("upstream_topics", [])))

    def test_graph_neighbors_cli(self) -> None:
        md = """# Awesome Search
## searchin:
### Vibe Coding
### >Vibe Coding
"""
        nodes = parse_markdown_to_nodes(md, source_title="??vibe coding")
        index_dir = self.tmp_dir / "index-cli"
        index_dir.mkdir(parents=True, exist_ok=True)
        build_index(nodes, index_dir / "one.db")

        import subprocess

        out = subprocess.check_output(
            [
                "python3",
                "skills/xlb-topic-index/scripts/xlb_rag_pipeline.py",
                "graph-neighbors",
                "--index-dir",
                str(index_dir),
                "--target-title",
                "->vibe coding/:",
            ],
            text=True,
        )
        obj = json.loads(out)
        self.assertEqual(obj.get("mode"), "graph_neighbors")
        self.assertEqual(obj.get("inbound_edge_count"), 1)

    def test_visited_exec_titles_roundtrip(self) -> None:
        visited_path = self.tmp_dir / "visited.json"
        save_visited_exec_titles(visited_path, {">ai model/", ">vibe coding/vibe"})
        loaded = load_visited_exec_titles(visited_path)
        self.assertIn(">ai model/", loaded)
        self.assertIn(">vibe coding/vibe", loaded)

    def test_explore_next_dry_run(self) -> None:
        nav = {
            "topic_navigation": [
                {
                    "title": "AI Model",
                    "query_cmd": ">AI Model",
                    "query_exec_title": ">AI Model/",
                    "query_kind": "topic_nav",
                    "query_source": "searchin",
                }
            ],
            "knowledge_search": [
                {
                    "title": "vibe coding",
                    "query_cmd": "search(>vibe coding/vibe)",
                    "query_exec_title": ">vibe coding/vibe",
                    "query_kind": "kb_search",
                    "query_source": "command",
                }
            ],
            "other_queries": [],
        }
        nav_path = self.tmp_dir / "nav.json"
        nav_path.write_text(json.dumps(nav, ensure_ascii=False, indent=2), encoding="utf-8")
        meta_path = self.tmp_dir / "meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "title": ">Vibe Coding/",
                    "snapshot_id": "snap-test",
                    "navigation_json": str(nav_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        import subprocess

        out = subprocess.check_output(
            [
                "python3",
                "skills/xlb-topic-index/scripts/xlb_rag_pipeline.py",
                "explore-next",
                "--meta-path",
                str(meta_path),
                "--dry-run",
                "--strategy",
                "topic_first",
            ],
            text=True,
        )
        obj = json.loads(out)
        self.assertEqual(obj.get("status"), "selected")
        self.assertEqual(obj.get("selected", {}).get("query_exec_title"), ">AI Model/")
        self.assertEqual(obj.get("selected", {}).get("input"), "xlb >AI Model/")

    def test_index_search(self) -> None:
        nodes = parse_markdown_to_nodes(SAMPLE_MD, source_title=">vibe coding/coding")
        db_path = self.tmp_dir / "index.db"
        build_index(nodes, db_path)
        hits = search_index(db_path, "codex", limit=5)
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(any("codex" in (h.get("title") or "").lower() for h in hits))

    def test_index_search_multi_term_fallback(self) -> None:
        md = """# Topic
## searchin:
### Agent Skills
### >Agent Skills
"""
        nodes = parse_markdown_to_nodes(md, source_title=">topic/")
        db_path = self.tmp_dir / "index-multi.db"
        build_index(nodes, db_path)
        hits = search_index(db_path, "agent workflow", limit=5)
        self.assertGreaterEqual(len(hits), 1)

    def test_group_folder_nodes_are_queryable(self) -> None:
        md = """# Topic
## website:
### Vibe Coding Flow/流程/skill
- https://example.com/a#A article
- https://example.com/b#B article
"""
        nodes = parse_markdown_to_nodes(md, source_title=">topic/")
        base = write_virtual_tree(nodes, self.tmp_dir / "vfs", snapshot_id="snap-folder")
        self.assertTrue(any(n.node_type == "category" and "Vibe Coding Flow" in n.title for n in nodes))
        self.assertTrue(any("Vibe Coding Flow" in n.section for n in nodes if n.node_type == "link"))

        db_path = self.tmp_dir / "index-folder.db"
        build_index(nodes, db_path)
        hits = search_index(db_path, "Vibe Coding Flow", limit=10)
        self.assertGreaterEqual(len(hits), 2)
        self.assertTrue(any(h.get("node_type") == "link" for h in hits))
        self.assertTrue((base / "manifest.json").exists())

    def test_html_tags_are_stripped_from_section_and_title(self) -> None:
        md = """# Topic
## y-playlist:
### <i><strong>Vibe Coding</strong></i>
- https://example.com/a#A
"""
        nodes = parse_markdown_to_nodes(md, source_title="??topic")
        self.assertTrue(any(n.node_type == "category" and n.title == "Vibe Coding" for n in nodes))
        self.assertFalse(any("<" in n.title or ">" in n.title for n in nodes))
        self.assertFalse(any("<" in n.section or ">" in n.section for n in nodes))

        base = write_virtual_tree(nodes, self.tmp_dir / "vfs", snapshot_id="snap-clean")
        all_dirs = [str(p) for p in base.rglob("*") if p.is_dir()]
        self.assertFalse(any("i-strong" in p for p in all_dirs))
        self.assertTrue(any("vibe-coding" in p for p in all_dirs))

    def test_direct_hit_keeps_priority_and_includes_section_related(self) -> None:
        md = """# Topic
## website:
### Vibe Coding Flow/流程/skill
- https://example.com/a#Alpha Codex CLI
- https://example.com/b#Beta Agent Workflow
- https://example.com/c#Gamma Product Build
"""
        nodes = parse_markdown_to_nodes(md, source_title=">topic/")
        db_path = self.tmp_dir / "index-related.db"
        build_index(nodes, db_path)
        hits = search_index(db_path, "Codex CLI", limit=3)
        self.assertGreaterEqual(len(hits), 3)
        self.assertEqual(hits[0]["match_type"], "direct")
        self.assertIn("Codex", hits[0]["title"])
        self.assertTrue(any(h.get("match_type") == "section_related" for h in hits[1:]))

    def test_search_related_does_not_cross_topics(self) -> None:
        md = """# Topic A
## section:
### Group
- https://example.com/a#Codex CLI in A
# Topic B
## section:
### Group
- https://example.com/b#Other in B
"""
        nodes = parse_markdown_to_nodes(md, source_title=">topic/")
        db_path = self.tmp_dir / "index-cross-topic.db"
        build_index(nodes, db_path)
        hits = search_index(db_path, "Codex CLI", limit=5)
        self.assertTrue(any(h.get("topic") == "Topic A" for h in hits))
        self.assertFalse(any(h.get("topic") == "Topic B" and h.get("match_type") == "section_related" for h in hits))

    def test_suggest_topics_from_query(self) -> None:
        md = """# Vibe Coding
## searchin:
### Agent Skills
### >Agent Skills
# AI Model
## website:
### Model Zoo
- https://example.com/model#Model
"""
        nodes = parse_markdown_to_nodes(md, source_title=">topic/")
        db_path = self.tmp_dir / "index-topic-suggest.db"
        build_index(nodes, db_path)
        summary = suggest_topics_from_query(db_path, "agent", topic_limit=5, sample_per_topic=2)
        self.assertEqual(summary.get("mode"), "query_suggest")
        self.assertGreaterEqual(summary.get("topic_count", 0), 1)
        self.assertTrue(any(t.get("topic") == "Vibe Coding" for t in summary.get("topics", [])))
        vibe = next(t for t in summary.get("topics", []) if t.get("topic") == "Vibe Coding")
        self.assertEqual(vibe.get("entry_query"), ">Vibe Coding/")
        self.assertEqual(vibe.get("entry_input"), "xlb >Vibe Coding/")

    def test_suggest_topics_from_query_multi_topic(self) -> None:
        md = """# Awesome Search
## command:
### vibe coding
### >awesome search/vibe coding
# Vibe Coding
## github:
### awesome-vibe-coding
### https://example.com/a#awesome vibe coding
# Agent Skills
## command:
### vibe coding agent
### >agent skills/vibe coding
"""
        nodes = parse_markdown_to_nodes(md, source_title="??vibe coding")
        db_path = self.tmp_dir / "index-topic-suggest-multi.db"
        build_index(nodes, db_path)
        summary = suggest_topics_from_query(db_path, "vibe coding", topic_limit=10, sample_per_topic=2)
        self.assertEqual(summary.get("mode"), "query_suggest")
        self.assertGreaterEqual(summary.get("topic_count", 0), 3)
        self.assertEqual(summary.get("recommended_topic"), summary.get("topics", [])[0].get("topic"))
        for topic in summary.get("topics", []):
            self.assertTrue(str(topic.get("entry_query", "")).startswith(">"))
            self.assertTrue(str(topic.get("entry_input", "")).startswith("xlb >"))

    def test_build_index_ignores_duplicate_node_ids(self) -> None:
        duplicated_md = """# Topic
## github:
### openai/codex
### https://www.github.com/openai/codex#openai/codex
### openai/codex
### https://www.github.com/openai/codex#openai/codex
"""
        nodes = parse_markdown_to_nodes(duplicated_md, source_title=">topic/")
        db_path = self.tmp_dir / "index-dupe.db"
        build_index(nodes, db_path)
        hits = search_index(db_path, "codex", limit=10)
        self.assertGreaterEqual(len(hits), 1)

    def test_iterative_search_stops_on_low_gain(self) -> None:
        nodes = parse_markdown_to_nodes(SAMPLE_MD, source_title=">vibe coding/coding")
        db_path = self.tmp_dir / "index.db"
        build_index(nodes, db_path)
        result = iterative_search(
            db_path,
            query="codex",
            limit=2,
            max_iter=5,
            gain_threshold=0.2,
            low_gain_rounds=2,
        )
        self.assertIn(result["stop_reason"], {"low_gain", "no_hits", "no_frontier"})
        self.assertLessEqual(result["iterations"], 5)
        self.assertTrue(result["rounds"])
        self.assertIn("new_hits", result["rounds"][0])

    def test_should_ingest_incremental(self) -> None:
        raw = SAMPLE_MD
        meta = self.tmp_dir / "meta.json"
        needs, raw_sha = should_ingest(raw, meta, force=False)
        self.assertTrue(needs)
        write_meta(
            meta,
            raw_sha=raw_sha,
            title=">vibe",
            snapshot_id="snap-a",
            node_count=3,
            raw_file="/tmp/raw.md",
            vfs_base="/tmp/vfs/topic/snap-a",
            db_path="/tmp/index.db",
        )
        needs2, _ = should_ingest(raw, meta, force=False)
        self.assertFalse(needs2)
        needs3, _ = should_ingest(raw + "\n## new", meta, force=False)
        self.assertTrue(needs3)

        meta_data = json.loads(meta.read_text(encoding="utf-8"))
        meta_data["pipeline_version"] = "legacy"
        meta.write_text(json.dumps(meta_data, ensure_ascii=False, indent=2), encoding="utf-8")
        needs4, _ = should_ingest(raw, meta, force=False, expected_pipeline_version=PIPELINE_VERSION)
        self.assertTrue(needs4)

    def test_fetch_urls_concurrently_with_file_scheme(self) -> None:
        src1 = self.tmp_dir / "a.txt"
        src2 = self.tmp_dir / "b.md"
        src1.write_text("alpha", encoding="utf-8")
        src2.write_text("beta", encoding="utf-8")
        urls = [src1.as_uri(), src2.as_uri()]

        out1 = fetch_urls_concurrently(urls, self.tmp_dir / "artifacts", max_workers=2)
        self.assertEqual(len(out1), 2)
        self.assertTrue(all(r["status"] in {"downloaded", "cached"} for r in out1))

        out2 = fetch_urls_concurrently(urls, self.tmp_dir / "artifacts", max_workers=2)
        self.assertEqual(len(out2), 2)
        self.assertTrue(all(r["status"] == "cached" for r in out2))

    def test_html_is_converted_to_markdown_locally(self) -> None:
        html = self.tmp_dir / "page.html"
        html.write_text("<html><body><h1>Title</h1><p>Hello world</p></body></html>", encoding="utf-8")

        out = fetch_urls_concurrently(
            [html.as_uri()],
            self.tmp_dir / "artifacts",
            max_workers=1,
            html_mode="markdown",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "converted_local")
        self.assertTrue(out[0]["path"].endswith(".md"))
        content = Path(out[0]["path"]).read_text(encoding="utf-8")
        self.assertIn("# Title", content)
        self.assertIn("Hello world", content)

    def test_html_uses_external_converter_when_available(self) -> None:
        html = self.tmp_dir / "page.html"
        html.write_text("<html><body><h1>Will be replaced</h1></body></html>", encoding="utf-8")

        class Proc:
            def __init__(self, code: int, out: str):
                self.returncode = code
                self.stdout = out

        with patch("xlb_rag_pipeline.subprocess.run", return_value=Proc(0, "# External\nbody")) as mocked:
            out = fetch_urls_concurrently(
                [html.as_uri()],
                self.tmp_dir / "artifacts",
                max_workers=1,
                html_mode="markdown",
                html_converter_bin="/tmp/url-convert.sh",
                html_converter_tool_id="url-to-markdown",
            )

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "converted_external")
        content = Path(out[0]["path"]).read_text(encoding="utf-8")
        self.assertIn("# External", content)
        cmd = mocked.call_args[0][0]
        self.assertEqual(cmd[0], "/tmp/url-convert.sh")
        self.assertEqual(cmd[1], html.as_uri())
        self.assertEqual(cmd[2], "url-to-markdown")

    def test_prefetch_requires_confirmation(self) -> None:
        md = """# Topic
## refs:
- https://example.com/a#A
"""
        nodes = parse_markdown_to_nodes(md, source_title=">topic/")
        db_path = self.tmp_dir / "index-prefetch.db"
        build_index(nodes, db_path)
        out = prefetch_from_index(
            db_path,
            query="A",
            artifact_root=self.tmp_dir / "artifacts",
            require_confirmation=True,
            network_confirmed=False,
        )
        self.assertTrue(out.get("skipped"))
        self.assertEqual(out.get("skip_reason"), "network_confirmation_required")

    def test_build_network_confirmation_template(self) -> None:
        hits = [
            {"node_type": "link", "url": "https://example.com/a", "title": "A"},
            {"node_type": "link", "url": "https://example.com/b", "title": "B"},
            {"node_type": "query", "query_cmd": ">A"},
        ]
        msg = build_network_confirmation_template(
            input_text="xlb >vibe coding/",
            query="codex cli",
            hits=hits,
            prefetch_enabled=True,
            has_external_route=True,
        )
        self.assertIn("网络扩展需确认", msg)
        self.assertIn("可抓取URL数: 2", msg)
        self.assertIn("XLB_NETWORK_CONFIRMED=1", msg)
        self.assertIn("https://example.com/a", msg)
        self.assertIn("skills/xlb-topic-index/scripts/retrieve-topic-index.sh", msg)
        self.assertIn("'xlb >vibe coding/'", msg)
        self.assertIn("'codex cli'", msg)

    def test_discover_external_capabilities(self) -> None:
        class Proc:
            def __init__(self, code: int, out: str):
                self.returncode = code
                self.stdout = out

        outputs = [
            Proc(0, "Global Skills\n\ntavily-web ~/.agents/skills/tavily-web\n"),
            Proc(0, "Project Skills\n\nxlb-topic-index ~/.xlb-env/xlinkBook-skill/skills/xlb-topic-index\n"),
            Proc(0, ""),
            Proc(0, ""),
        ]

        with patch("xlb_rag_pipeline.subprocess.run", side_effect=outputs):
            result = discover_external_capabilities()

        self.assertIn("tavily-web", result["skills"])
        self.assertIn("tavily-web", result["network_skills"])
        self.assertEqual(result["mcp_hint"], "prefer_skill")

    def test_discover_external_capabilities_cache(self) -> None:
        class Proc:
            def __init__(self, code: int, out: str):
                self.returncode = code
                self.stdout = out

        cache_path = self.tmp_dir / "caps.json"
        outputs = [
            Proc(0, "Global Skills\n\ntavily-web ~/.agents/skills/tavily-web\n"),
            Proc(0, ""),
        ]

        with patch("xlb_rag_pipeline.subprocess.run", side_effect=outputs):
            result1 = discover_external_capabilities(cache_file=cache_path, cache_ttl_sec=60)
        self.assertEqual(result1.get("source"), "live")
        self.assertTrue(cache_path.exists())

        with patch("xlb_rag_pipeline.subprocess.run", side_effect=AssertionError("should not run")):
            result2 = discover_external_capabilities(cache_file=cache_path, cache_ttl_sec=60)
        self.assertEqual(result2.get("source"), "cache")
        self.assertIn("tavily-web", result2["skills"])

    def test_describe_cache_json(self) -> None:
        meta = self.tmp_dir / "meta.json"
        raw_sha = "abc"
        write_meta(
            meta,
            raw_sha=raw_sha,
            title=">demo/",
            snapshot_id="snap-demo",
            node_count=2,
            raw_file=str(self.tmp_dir / "raw.md"),
            vfs_base=str(self.tmp_dir / "vfs"),
            db_path=str(self.tmp_dir / "index.db"),
        )
        import subprocess

        out = subprocess.check_output(
            [
                "python3",
                "skills/xlb-topic-index/scripts/xlb_rag_pipeline.py",
                "describe-cache",
                "--meta-path",
                str(meta),
                "--format",
                "json",
            ],
            text=True,
        )
        self.assertIn('"title": ">demo/"', out)


if __name__ == "__main__":
    unittest.main()
