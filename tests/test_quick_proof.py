"""Checks for the fabricated trial-balance proof shown in the README."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import render_quick_proof


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "sample-output.csv"
SVG = ROOT / "assets" / "quick-proof.svg"
TRANSCRIPT = ROOT / "assets" / "quick-proof.md"


class QuickProofTests(unittest.TestCase):
    def _sample_with(self, old: str, new: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        sample = Path(directory.name) / "sample.csv"
        content = SAMPLE.read_text(encoding="utf-8-sig").replace(old, new, 1)
        sample.write_text(content, encoding="utf-8-sig", newline="\n")
        return sample

    def test_renderer_check_accepts_the_committed_assets(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/render_quick_proof.py", "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_proof_is_tied_to_the_fabricated_sample(self) -> None:
        transcript = TRANSCRIPT.read_text(encoding="utf-8")
        self.assertIn("Catherby Fisheries Pty Ltd", transcript)
        self.assertIn("10 account rows", transcript)
        self.assertIn("Movement: debit $5,700.00 | credit $5,700.00 | balanced", transcript)
        self.assertIn("YTD: debit $126,334.50 | credit $126,334.50 | balanced", transcript)
        self.assertIn("samples/sample-output.csv", transcript)
        self.assertTrue(SAMPLE.is_file())

    def test_summary_rejects_multiple_tenants(self) -> None:
        sample = self._sample_with(
            "Catherby Fisheries Pty Ltd,Liabilities",
            "Another Tenant Pty Ltd,Liabilities",
        )

        with mock.patch.object(render_quick_proof, "SAMPLE", sample):
            with self.assertRaisesRegex(ValueError, "multiple tenants"):
                render_quick_proof._summary()

    def test_summary_rejects_multiple_report_dates(self) -> None:
        sample = self._sample_with(
            "2026-06-30,Catherby Fisheries Pty Ltd,Liabilities",
            "2026-06-29,Catherby Fisheries Pty Ltd,Liabilities",
        )

        with mock.patch.object(render_quick_proof, "SAMPLE", sample):
            with self.assertRaisesRegex(ValueError, "multiple report dates"):
                render_quick_proof._summary()

    def test_summary_reports_the_csv_header_width(self) -> None:
        self.assertEqual(render_quick_proof._summary()["columns"], 10)

    def test_summary_rejects_an_unexpected_csv_header(self) -> None:
        sample = self._sample_with(
            "AccountName,AccountCode",
            "Unexpected,AccountCode",
        )

        with mock.patch.object(render_quick_proof, "SAMPLE", sample):
            with self.assertRaisesRegex(ValueError, "unexpected columns"):
                render_quick_proof._summary()

    def test_rendered_shape_and_accessibility_text_use_summary_counts(self) -> None:
        summary = {
            "tenant": "Example Pty Ltd",
            "date": "2026-06-30",
            "rows": 12,
            "columns": 9,
            "movement": "$1.00",
            "ytd": "$2.00",
        }

        transcript = render_quick_proof.render_transcript(summary)
        svg = render_quick_proof.render_svg(summary)

        self.assertIn("12 account rows, 9 columns", transcript)
        self.assertIn("A fabricated 12-row trial balance", svg)
        self.assertIn("12 account rows  |  9 columns", svg)

    def test_readme_places_the_proof_and_check_before_setup(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        proof = "[![Validated fabricated trial balance](assets/quick-proof.svg)](assets/quick-proof.md)"
        command = "python tools/render_quick_proof.py --check"
        self.assertIn(proof, readme)
        self.assertIn(command, readme)
        self.assertLess(readme.index(proof), readme.index("## Setup"))


if __name__ == "__main__":
    unittest.main()
