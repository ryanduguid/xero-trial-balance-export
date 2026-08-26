from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "evaluation" / "xero_tb_integrity"
RUNNER = PACK / "run.py"
EXPECTED = PACK / "expected_results.json"


class EvaluationPackTest(unittest.TestCase):
    def run_fixture(self, name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), str(PACK / "fixtures" / name)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_declared_results_are_reproducible(self):
        contract = json.loads(EXPECTED.read_text(encoding="utf-8"))
        for scenario in contract["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                result = self.run_fixture(scenario["fixture"])
                self.assertEqual(result.returncode, scenario["exit_code"])
                combined = result.stdout + result.stderr
                for marker in scenario["output_contains"]:
                    self.assertIn(marker, combined)

    def test_passing_fixture_proves_both_pairs(self):
        result = self.run_fixture("passing.csv")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("movement and YTD balance", result.stdout)

    def test_failures_identify_the_independent_pair(self):
        movement = self.run_fixture("failing_movement.csv")
        ytd = self.run_fixture("failing_ytd.csv")
        self.assertEqual(movement.returncode, 1)
        self.assertIn("WARNING: movement", movement.stdout)
        self.assertNotIn("WARNING: YTD", movement.stdout)
        self.assertEqual(ytd.returncode, 1)
        self.assertIn("WARNING: YTD", ytd.stdout)
        self.assertNotIn("WARNING: movement", ytd.stdout)

    def test_pack_names_its_limits_sources_and_versions(self):
        contract = json.loads(EXPECTED.read_text(encoding="utf-8"))
        readme = (PACK / "README.md").read_text(encoding="utf-8")
        self.assertEqual(contract["product_release"], "v0.1.4")
        self.assertEqual(contract["fixture_version"], "1")
        self.assertEqual(contract["source_reviewed"], "2026-08-26")
        self.assertIn(contract["human_decision"], readme)
        self.assertIn("fabricated", readme.casefold())
        self.assertNotIn("case study", readme.casefold())

    def test_only_declared_evaluation_csvs_are_allowlisted(self):
        allowed = {
            "evaluation/xero_tb_integrity/fixtures/passing.csv",
            "evaluation/xero_tb_integrity/fixtures/failing_movement.csv",
            "evaluation/xero_tb_integrity/fixtures/failing_ytd.csv",
        }
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for relative in sorted(allowed):
            self.assertIn(f"!{relative}", rules)
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", "--", relative],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(result.returncode, 1, relative)
        refused = subprocess.run(
            [
                "git",
                "check-ignore",
                "--no-index",
                "--quiet",
                "--",
                "evaluation/xero_tb_integrity/fixtures/client.csv",
            ],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(refused.returncode, 0)


if __name__ == "__main__":
    unittest.main()
