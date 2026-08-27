from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluation.xero_tb_integrity import run as evaluation_runner

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "evaluation" / "xero_tb_integrity"
RUNNER = PACK / "run.py"
EXPECTED = PACK / "expected_results.json"


class EvaluationPackTest(unittest.TestCase):
    def run_script(
        self, runner: Path, path: str | Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(runner), str(path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_path(self, path: str | Path) -> subprocess.CompletedProcess[str]:
        return self.run_script(RUNNER, path)

    def run_fixture(self, name: str) -> subprocess.CompletedProcess[str]:
        return self.run_path(PACK / "fixtures" / name)

    def assert_path_refused(self, path: str | Path) -> None:
        result = self.run_path(path)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("error:", result.stderr)
        self.assertIn("declared fabricated", result.stderr)

    def test_documented_relative_fixture_paths_are_accepted(self):
        cases = (
            ("passing.csv", 0, "PASS: passing.csv"),
            ("failing_movement.csv", 1, "WARNING: movement"),
            ("failing_ytd.csv", 1, "WARNING: YTD"),
        )
        for name, exit_code, marker in cases:
            with self.subTest(name=name):
                relative = Path("evaluation") / "xero_tb_integrity" / "fixtures" / name
                result = self.run_path(relative)
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                self.assertIn(marker, result.stdout)

    def test_absolute_undeclared_valid_csv_is_refused(self):
        fixture = PACK / "fixtures" / "passing.csv"
        with tempfile.NamedTemporaryFile(
            dir=fixture.parent,
            prefix="undeclared-",
            suffix=".csv",
            delete=False,
        ) as handle:
            undeclared = Path(handle.name)
            handle.write(fixture.read_bytes())
        try:
            self.assert_path_refused(undeclared)
        finally:
            undeclared.unlink()

    def test_traversal_to_undeclared_valid_csv_is_refused(self):
        traversal = (
            "evaluation/xero_tb_integrity/fixtures/../../../samples/sample-output.csv"
        )
        self.assert_path_refused(traversal)

    def test_nested_valid_csv_is_refused(self):
        fixture = PACK / "fixtures" / "passing.csv"
        with tempfile.TemporaryDirectory(dir=fixture.parent) as directory:
            nested = Path(directory) / "passing.csv"
            nested.write_bytes(fixture.read_bytes())
            self.assert_path_refused(nested)

    def test_symlink_escaping_to_undeclared_valid_csv_is_refused(self):
        fixture = PACK / "fixtures" / "passing.csv"
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory) / "passing.csv"
            outside.write_bytes(fixture.read_bytes())
            with tempfile.TemporaryDirectory(dir=fixture.parent) as inside_directory:
                alias = Path(inside_directory) / "passing.csv"
                try:
                    alias.symlink_to(outside)
                except OSError as exc:
                    self.skipTest(f"symlinks unavailable: {exc}")
                self.assert_path_refused(alias)

    def test_symlinked_runner_does_not_reanchor_its_declared_fixtures(self):
        fixture = PACK / "fixtures" / "passing.csv"
        with tempfile.TemporaryDirectory() as directory:
            alias_root = Path(directory)
            runner_alias = alias_root / "run.py"
            try:
                runner_alias.symlink_to(RUNNER)
            except OSError as exc:
                self.skipTest(f"runner-file symlinks unavailable: {exc}")
            fake_fixtures = alias_root / "fixtures"
            fake_fixtures.mkdir()
            fake_passing = fake_fixtures / "passing.csv"
            fake_passing.write_bytes(fixture.read_bytes())

            result = self.run_script(runner_alias, fake_passing)

            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("error:", result.stderr)
            self.assertIn("declared fabricated", result.stderr)

    @unittest.skipUnless(sys.platform == "win32", "Windows path syntax required")
    def test_windows_extended_length_declared_paths_are_accepted(self):
        cases = (
            ("passing.csv", 0, "PASS: passing.csv"),
            ("failing_movement.csv", 1, "WARNING: movement"),
            ("failing_ytd.csv", 1, "WARNING: YTD"),
        )
        for name, exit_code, marker in cases:
            with self.subTest(name=name):
                declared = (PACK / "fixtures" / name).resolve()
                extended = "\\\\?\\" + str(declared)
                result = self.run_path(extended)
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                self.assertIn(marker, result.stdout)

    @unittest.skipUnless(sys.platform == "win32", "Windows path syntax required")
    def test_windows_extended_unc_path_is_normalised_before_identity_check(self):
        fixture_root = r"\\server\share\xero_tb_integrity\fixtures"
        declared = fixture_root + r"\passing.csv"
        extended = r"\\?\UNC\server\share\xero_tb_integrity\fixtures\passing.csv"
        with (
            mock.patch.object(evaluation_runner, "FIXTURE_ROOT", fixture_root),
            mock.patch.object(evaluation_runner, "DECLARED_FIXTURES", (declared,)),
            mock.patch.object(evaluation_runner.os.path, "abspath", side_effect=str),
            mock.patch.object(evaluation_runner.os.path, "realpath", side_effect=str),
            mock.patch.object(evaluation_runner.os.path, "isfile", return_value=True),
        ):
            try:
                resolved = evaluation_runner.declared_fixture_path(extended)
            except ValueError as exc:
                self.fail(str(exc))
        self.assertEqual(str(resolved), declared)

    @unittest.skipUnless(sys.platform == "win32", "Windows path syntax required")
    def test_windows_device_namespace_paths_are_refused(self):
        declared = str((PACK / "fixtures" / "passing.csv").resolve())
        for hostile in (
            "\\\\.\\" + declared,
            "//./" + declared.replace("\\", "/"),
            r"\\?\GLOBALROOT\Device\HarddiskVolume1\passing.csv",
            r"\\?\UNC\.\C$\passing.csv",
        ):
            with self.subTest(hostile=hostile):
                result = self.run_path(hostile)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("error:", result.stderr)
                self.assertIn("device namespace", result.stderr)

    def test_declared_results_are_reproducible(self):
        contract = json.loads(EXPECTED.read_text(encoding="utf-8"))
        for scenario in contract["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                result = self.run_fixture(scenario["fixture"])
                self.assertEqual(result.returncode, scenario["exit_code"])
                combined = result.stdout + result.stderr
                for marker in scenario["output_contains"]:
                    self.assertIn(marker, combined)

    def test_pack_is_a_pinned_cross_repository_conformance_corpus(self):
        contract = json.loads(EXPECTED.read_text(encoding="utf-8"))
        canonical_columns = [
            "ReportDate",
            "Tenant",
            "Section",
            "AccountID",
            "AccountName",
            "AccountCode",
            "Debit",
            "Credit",
            "YTDDebit",
            "YTDCredit",
        ]
        expected = {
            "balanced": (
                "passing.csv",
                "2cbe9997a8e7210936ff3c59b5d3fdb0041c1b375b0f9c88cf9ee30d0f356a09",
                {"accept": True},
            ),
            "movement_break": (
                "failing_movement.csv",
                "702175df967b2854e7897cd27fdc4aca441e21b52438381108fabe88ff3153e4",
                {
                    "accept": False,
                    "error_contains": "movement debit and credit totals",
                },
            ),
            "ytd_break": (
                "failing_ytd.csv",
                "ec757f12d13866360fbab189228ebb425893c6f8b299809c6f8567bf5817c64b",
                {
                    "accept": False,
                    "error_contains": "YTD debit and credit totals",
                },
            ),
        }

        self.assertEqual(contract["schema_version"], 2)
        self.assertEqual(contract["corpus_id"], "xero-tb-csv.v1")
        self.assertEqual(
            contract["owner_repository"],
            "https://github.com/ryanduguid/xero-trial-balance-export",
        )
        self.assertEqual(contract["canonical_columns"], canonical_columns)
        scenarios = {scenario["id"]: scenario for scenario in contract["scenarios"]}
        self.assertEqual(set(scenarios), set(expected))
        for scenario_id, (name, digest, conformance) in expected.items():
            with self.subTest(scenario=scenario_id):
                scenario = scenarios[scenario_id]
                fixture = PACK / "fixtures" / name
                self.assertEqual(scenario["fixture"], name)
                self.assertEqual(scenario["sha256"], digest)
                self.assertEqual(scenario["conformance"], conformance)
                self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), digest)
                with fixture.open(encoding="utf-8-sig", newline="") as source:
                    self.assertEqual(next(csv.reader(source)), canonical_columns)

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
