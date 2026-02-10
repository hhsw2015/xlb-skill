#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import time
from pathlib import Path


def estimate_tokens(output_bytes: int) -> int:
    if output_bytes <= 0:
        return 0
    return int(math.ceil(output_bytes / 4.0))


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(ordered[lo])
    frac = rank - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def summarize_mode(runs: list[dict]) -> dict:
    if not runs:
        return {
            "count": 0,
            "latency_ms_avg": 0.0,
            "latency_ms_p50": 0.0,
            "latency_ms_p95": 0.0,
            "output_bytes_avg": 0.0,
            "estimated_tokens_avg": 0.0,
            "success_rate": 0.0,
        }

    latencies = [float(r.get("latency_ms", 0.0)) for r in runs]
    output_bytes = [int(r.get("output_bytes", 0)) for r in runs]
    tokens = [int(r.get("estimated_tokens", 0)) for r in runs]
    successes = [1 for r in runs if int(r.get("returncode", 1)) == 0]

    return {
        "count": len(runs),
        "latency_ms_avg": float(statistics.fmean(latencies)),
        "latency_ms_p50": percentile(latencies, 50),
        "latency_ms_p95": percentile(latencies, 95),
        "output_bytes_avg": float(statistics.fmean(output_bytes)),
        "estimated_tokens_avg": float(statistics.fmean(tokens)),
        "success_rate": float(len(successes) / len(runs)),
    }


def _safe_pct_reduction(before: float, after: float) -> float:
    if before <= 0:
        return 0.0
    return (before - after) / before


def run_command(cmd: list[str], *, timeout_sec: int, env: dict[str, str]) -> dict:
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout_sec, env=env)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    output = proc.stdout or ""
    output_bytes = len(output.encode("utf-8"))
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "latency_ms": latency_ms,
        "output_bytes": output_bytes,
        "estimated_tokens": estimate_tokens(output_bytes),
        "stderr": (proc.stderr or "")[:5000],
    }


def parse_query_line(line: str) -> tuple[str, str]:
    text = line.strip()
    if not text or text.startswith("#"):
        return "", ""
    if "\t" in text:
        left, right = text.split("\t", 1)
        return left.strip(), right.strip()
    if " | " in text:
        left, right = text.split(" | ", 1)
        return left.strip(), right.strip()
    return text, ""


def load_queries(path: Path) -> list[dict]:
    items = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        xlb_input, retrieval_query = parse_query_line(raw)
        if not xlb_input:
            continue
        items.append({"input": xlb_input, "query": retrieval_query})
    return items


def build_commands(repo_root: Path, xlb_input: str, retrieval_query: str) -> tuple[list[str], list[str]]:
    fetch = repo_root / "skills/xlb-topic-index/scripts/fetch-topic-index.sh"
    retrieve = repo_root / "skills/xlb-topic-index/scripts/retrieve-topic-index.sh"
    raw_cmd = [str(fetch), xlb_input]
    rag_cmd = [str(retrieve), xlb_input]
    if retrieval_query:
        rag_cmd.append(retrieval_query)
    return raw_cmd, rag_cmd


def render_markdown_report(result: dict) -> str:
    raw = result.get("summary", {}).get("raw", {})
    rag = result.get("summary", {}).get("rag", {})
    compare = result.get("comparison", {})

    lines = [
        "# XLB Benchmark Report",
        "",
        f"- generated_at: `{result.get('generated_at', '')}`",
        f"- queries: `{result.get('query_count', 0)}`",
        f"- runs_per_query: `{result.get('runs_per_query', 0)}`",
        "",
        "## Summary",
        "",
        "| mode | count | success_rate | latency_p50_ms | latency_p95_ms | avg_output_bytes | avg_tokens |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| raw | {raw.get('count', 0)} | {raw.get('success_rate', 0.0):.2f} | {raw.get('latency_ms_p50', 0.0):.2f} | {raw.get('latency_ms_p95', 0.0):.2f} | {raw.get('output_bytes_avg', 0.0):.2f} | {raw.get('estimated_tokens_avg', 0.0):.2f} |",
        f"| rag | {rag.get('count', 0)} | {rag.get('success_rate', 0.0):.2f} | {rag.get('latency_ms_p50', 0.0):.2f} | {rag.get('latency_ms_p95', 0.0):.2f} | {rag.get('output_bytes_avg', 0.0):.2f} | {rag.get('estimated_tokens_avg', 0.0):.2f} |",
        "",
        "## Comparison",
        "",
        f"- token_reduction_pct: `{compare.get('token_reduction_pct', 0.0):.2%}`",
        f"- latency_p50_reduction_pct: `{compare.get('latency_p50_reduction_pct', 0.0):.2%}`",
        f"- latency_p95_reduction_pct: `{compare.get('latency_p95_reduction_pct', 0.0):.2%}`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark raw vs xlb RAG retrieval")
    parser.add_argument("--queries-file", default="skills/xlb-topic-index/bench/queries.txt")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout-sec", type=int, default=60)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-json", default="skills/xlb-topic-index/bench/report.json")
    parser.add_argument("--output-markdown", default="skills/xlb-topic-index/bench/report.md")
    parser.add_argument("--topk", type=int, default=8)
    parser.add_argument("--iterative", action="store_true", help="Enable iterative retrieval mode for rag runs")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    queries = load_queries(Path(args.queries_file))
    raw_runs: list[dict] = []
    rag_runs: list[dict] = []

    for item in queries:
        xlb_input = item["input"]
        retrieval_query = item["query"]
        raw_cmd, rag_cmd = build_commands(repo_root, xlb_input, retrieval_query)
        for idx in range(args.runs):
            env_raw = dict(os.environ)
            raw_result = run_command(raw_cmd, timeout_sec=args.timeout_sec, env=env_raw)
            raw_result.update({"mode": "raw", "input": xlb_input, "query": retrieval_query, "run": idx + 1})
            raw_runs.append(raw_result)

            env_rag = dict(os.environ)
            env_rag["XLB_TOPK"] = str(args.topk)
            if args.iterative:
                env_rag["XLB_ITERATIVE_SEARCH"] = "1"
            rag_result = run_command(rag_cmd, timeout_sec=args.timeout_sec, env=env_rag)
            rag_result.update({"mode": "rag", "input": xlb_input, "query": retrieval_query, "run": idx + 1})
            rag_runs.append(rag_result)

    raw_summary = summarize_mode(raw_runs)
    rag_summary = summarize_mode(rag_runs)
    comparison = {
        "token_reduction_pct": _safe_pct_reduction(raw_summary["estimated_tokens_avg"], rag_summary["estimated_tokens_avg"]),
        "latency_p50_reduction_pct": _safe_pct_reduction(raw_summary["latency_ms_p50"], rag_summary["latency_ms_p50"]),
        "latency_p95_reduction_pct": _safe_pct_reduction(raw_summary["latency_ms_p95"], rag_summary["latency_ms_p95"]),
    }
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query_count": len(queries),
        "runs_per_query": args.runs,
        "summary": {"raw": raw_summary, "rag": rag_summary},
        "comparison": comparison,
        "runs": {"raw": raw_runs, "rag": rag_runs},
    }

    out_json = Path(args.output_json)
    out_md = Path(args.output_markdown)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_markdown_report(result), encoding="utf-8")
    print(json.dumps({"output_json": str(out_json), "output_markdown": str(out_md)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
