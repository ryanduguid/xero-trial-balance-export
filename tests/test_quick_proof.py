"""Checks for the fabricated trial-balance proof shown in the README."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "sample-output.csv"
SVG = ROOT / "assets" / "quick-proof.svg"
TRANSCRIPT = ROOT / "assets" / "quick-proof.md"


class QuickProofTests(unittest.TestCase):
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

    def test_readme_places_the_proof_and_check_before_setup(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        proof = "[![Validated fabricated trial balance](assets/quick-proof.svg)](assets/quick-proof.md)"
        command = "python tools/render_quick_proof.py --check"
        self.assertIn(proof, readme)
        self.assertIn(command, readme)
        self.assertLess(readme.index(proof), readme.index("## Setup"))


if __name__ == "__main__":
    unittest.main()
