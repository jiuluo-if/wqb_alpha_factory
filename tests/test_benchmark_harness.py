"""Smoke test for the offline local-IO benchmark harness.

Long benchmarks never belong in the normal unit-test lane; this only proves the
documented entry point runs offline and prints the stable output columns.
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestBenchmarkHarnessSmoke(unittest.TestCase):
    def test_harness_runs_offline_and_prints_columns(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "benchmark_local_io.py"),
                "--rows",
                "200",
                "--repeat",
                "2",
                "--workloads",
                "trajectory_load,artifacts_iter_jsonl,trial_ledger_append",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("workload", result.stdout)
        self.assertIn("median_ms", result.stdout)
        self.assertIn("p95_ms", result.stdout)
        self.assertIn("trajectory_load", result.stdout)
        self.assertIn("artifacts_iter_jsonl", result.stdout)
        self.assertIn("trial_ledger_append", result.stdout)
