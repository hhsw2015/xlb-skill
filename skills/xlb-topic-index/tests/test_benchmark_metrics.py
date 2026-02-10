import math
import unittest
from pathlib import Path
import sys


BENCH_DIR = Path("skills/xlb-topic-index/bench").resolve()
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from run_benchmark import estimate_tokens, summarize_mode  # noqa: E402


class BenchmarkMetricsTests(unittest.TestCase):
    def test_estimate_tokens(self) -> None:
        self.assertEqual(estimate_tokens(0), 0)
        self.assertEqual(estimate_tokens(1), 1)
        self.assertEqual(estimate_tokens(4), 1)
        self.assertEqual(estimate_tokens(5), 2)
        self.assertEqual(estimate_tokens(17), math.ceil(17 / 4))

    def test_summarize_mode(self) -> None:
        runs = [
            {"latency_ms": 50.0, "output_bytes": 100, "estimated_tokens": estimate_tokens(100)},
            {"latency_ms": 100.0, "output_bytes": 200, "estimated_tokens": estimate_tokens(200)},
            {"latency_ms": 80.0, "output_bytes": 120, "estimated_tokens": estimate_tokens(120)},
        ]
        summary = summarize_mode(runs)
        self.assertEqual(summary["count"], 3)
        self.assertGreaterEqual(summary["latency_ms_p95"], summary["latency_ms_p50"])
        self.assertEqual(summary["output_bytes_avg"], (100 + 200 + 120) / 3)


if __name__ == "__main__":
    unittest.main()
