import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from export_tb import main, output_path


class OutputPathTests(unittest.TestCase):
    def test_uses_the_default_filename_beneath_the_output_root(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(
                output_path(None, "demo-tb-2026-06-30-accrual.csv", root=root),
                os.path.join(os.path.realpath(root), "demo-tb-2026-06-30-accrual.csv"),
            )

    def test_allows_a_nested_relative_csv_path(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(
                output_path("exports/tb-latest.csv", "unused.csv", root=root),
                os.path.join(os.path.realpath(root), "exports", "tb-latest.csv"),
            )

    def test_rejects_parent_directory_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "beneath the current working directory"):
                output_path("../outside.csv", "unused.csv", root=root)

    def test_rejects_an_absolute_path(self):
        with tempfile.TemporaryDirectory() as root:
            outside = os.path.abspath(os.path.join(root, os.pardir, "outside.csv"))
            with self.assertRaisesRegex(ValueError, "beneath the current working directory"):
                output_path(outside, "unused.csv", root=root)

    def test_rejects_a_non_csv_output(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "must name a .csv file"):
                output_path("report.txt", "unused.csv", root=root)

    def test_rejects_an_existing_symlink_that_escapes_the_output_root(self):
        with tempfile.TemporaryDirectory() as parent:
            root = os.path.join(parent, "root")
            outside = os.path.join(parent, "outside")
            os.mkdir(root)
            os.mkdir(outside)
            link = os.path.join(root, "elsewhere")
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("creating directory symlinks is unavailable on this platform")

            with self.assertRaisesRegex(ValueError, "beneath the current working directory"):
                output_path("elsewhere/tb.csv", "unused.csv", root=root)

    def test_main_rejects_an_unsafe_output_before_loading_credentials(self):
        with (
            patch.object(sys, "argv", ["export_tb.py", "--out", "../outside.csv"]),
            patch("export_tb.load_dotenv") as load_dotenv,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            with self.assertRaises(SystemExit) as result:
                main()

        self.assertEqual(result.exception.code, 2)
        self.assertIn("must be a relative path", stderr.getvalue())
        load_dotenv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
