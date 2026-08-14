"""Executable regressions for bounded draft-release discovery."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import find_draft_release  # noqa: E402


def completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout)


class DraftReleaseDiscoveryTests(unittest.TestCase):
    def test_retries_empty_inventory_then_returns_exact_match(self) -> None:
        runner = Mock(
            side_effect=[
                completed("1\tv0.1.2\tfalse\n"),
                completed("370776891\tv0.1.3\ttrue\n"),
            ]
        )
        sleep = Mock()

        result = find_draft_release.find_draft_release(
            "ryanduguid/example",
            "v0.1.3",
            attempts=3,
            delay_seconds=0.25,
            runner=runner,
            sleep=sleep,
        )

        self.assertEqual(370776891, result)
        self.assertEqual(2, runner.call_count)
        sleep.assert_called_once_with(0.25)

    def test_multiple_matching_drafts_fail_without_retry(self) -> None:
        runner = Mock(
            return_value=completed(
                "11\tv0.1.3\ttrue\n12\tv0.1.3\ttrue\n"
            )
        )
        sleep = Mock()

        with self.assertRaisesRegex(RuntimeError, "multiple draft releases"):
            find_draft_release.find_draft_release(
                "ryanduguid/example",
                "v0.1.3",
                runner=runner,
                sleep=sleep,
            )

        self.assertEqual(1, runner.call_count)
        sleep.assert_not_called()

    def test_zero_results_fail_after_exact_attempt_limit(self) -> None:
        runner = Mock(return_value=completed(""))
        sleep = Mock()

        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            find_draft_release.find_draft_release(
                "ryanduguid/example",
                "v0.1.3",
                attempts=3,
                delay_seconds=0,
                runner=runner,
                sleep=sleep,
            )

        self.assertEqual(3, runner.call_count)
        self.assertEqual(2, sleep.call_count)

    def test_malformed_inventory_fails_closed(self) -> None:
        runner = Mock(return_value=completed("not-a-tab-separated-row\n"))

        with self.assertRaisesRegex(RuntimeError, "malformed release inventory"):
            find_draft_release.find_draft_release(
                "ryanduguid/example", "v0.1.3", runner=runner
            )

    def test_api_failure_is_not_treated_as_absence(self) -> None:
        runner = Mock(
            side_effect=subprocess.CalledProcessError(4, ["gh", "api"])
        )

        with self.assertRaises(subprocess.CalledProcessError):
            find_draft_release.find_draft_release(
                "ryanduguid/example", "v0.1.3", runner=runner
            )

    def test_invalid_retry_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            find_draft_release.find_draft_release(
                "ryanduguid/example", "v0.1.3", attempts=0
            )
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            find_draft_release.find_draft_release(
                "ryanduguid/example", "v0.1.3", delay_seconds=-1
            )


if __name__ == "__main__":
    unittest.main()
