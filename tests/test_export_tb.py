import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import export_tb
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


HEADER = ["Account", "Debit", "Credit", "YTD Debit", "YTD Credit"]


def _report(accounts, section="Assets"):
    """Build a Trial Balance payload.

    accounts: [(account_label, debit, credit, ytd_debit, ytd_credit), ...]
    """
    rows = [
        {
            "RowType": "Header",
            "Cells": [{"Value": title} for title in HEADER],
        }
    ]
    data_rows = []
    for index, (label, debit, credit, ytd_debit, ytd_credit) in enumerate(accounts, 1):
        guid = f"00000000-0000-0000-0000-{index:012d}"
        data_rows.append(
            {
                "RowType": "Row",
                "Cells": [
                    {"Value": label, "Attributes": [{"Value": guid, "Id": "account"}]},
                    {"Value": debit},
                    {"Value": credit},
                    {"Value": ytd_debit},
                    {"Value": ytd_credit},
                ],
            }
        )
    data_rows.append(
        {
            "RowType": "SummaryRow",
            "Cells": [{"Value": "Total"}, {"Value": "0"}, {"Value": "0"},
                      {"Value": "0"}, {"Value": "0"}],
        }
    )
    rows.append({"RowType": "Section", "Title": section, "Rows": data_rows})
    return {"Reports": [{"Rows": rows}]}


class _ExportCase(unittest.TestCase):
    """Runs export_tb.main() against a stubbed Xero.

    output_path confines --out beneath the process working directory, so the
    run is chdir'd into a scratch directory and --out stays relative - the
    same shape a scheduled job uses.
    """

    def run_export(self, accounts, date="2026-06-30"):
        """Return (SystemExit or None, stdout, csv bytes or None)."""
        work_dir = tempfile.mkdtemp()
        out_path = os.path.join(work_dir, "tb.csv")
        payload = _report(accounts)
        argv = ["export_tb.py", "--date", date, "--out", "tb.csv"]
        env = {
            "XERO_CLIENT_ID": "id-not-a-secret",
            "XERO_CLIENT_SECRET": "secret-not-used",
        }
        connections = [{"tenantId": "tenant-guid", "tenantName": "Sample Trading Pty Ltd"}]
        buffer = io.StringIO()
        raised = None
        previous_dir = os.getcwd()
        os.chdir(work_dir)
        try:
            with mock.patch.object(export_tb, "load_dotenv", lambda *a, **k: None), \
                    mock.patch.object(export_tb, "get_connections", return_value=connections), \
                    mock.patch.object(export_tb, "api_get", return_value=payload), \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(sys, "argv", argv):
                try:
                    with redirect_stdout(buffer):
                        export_tb.main()
                except SystemExit as exc:
                    raised = exc
        finally:
            os.chdir(previous_dir)
        data = None
        if os.path.exists(out_path):
            with open(out_path, "rb") as fh:
                data = fh.read()
        return raised, buffer.getvalue(), data


class CsvOutputTest(_ExportCase):
    def test_a_balanced_report_writes_the_expected_bytes(self):
        """Byte-for-byte guard on the CSV a working input has always produced."""
        raised, _, data = self.run_export(
            [
                ("Business Bank Account (090)", "1200.00", "", "15234.50", ""),
                ("Trade Debtors (610)", "", "1200.00", "", "15234.50"),
            ]
        )
        self.assertIsNone(raised)
        expected = (
            b"\xef\xbb\xbfReportDate,Tenant,Section,AccountID,AccountName,"
            b"AccountCode,Debit,Credit,YTDDebit,YTDCredit\r\n"
            b"2026-06-30,Sample Trading Pty Ltd,Assets,"
            b"00000000-0000-0000-0000-000000000001,Business Bank Account,090,"
            b"1200.0,0.0,15234.5,0.0\r\n"
            b"2026-06-30,Sample Trading Pty Ltd,Assets,"
            b"00000000-0000-0000-0000-000000000002,Trade Debtors,610,"
            b"0.0,1200.0,0.0,15234.5\r\n"
        )
        self.assertEqual(data, expected)


class DecimalMoneyTest(_ExportCase):
    """The money path must be exact, not float.

    The figures below are chosen to sit either side of 2**53, where a float
    can no longer hold every cent and the arithmetic silently rounds. That
    is the property under test; the magnitudes are extreme so the difference
    shows up in two rows instead of a million.
    """

    def test_an_unbalanced_report_is_not_totalled_into_balance(self):
        debits = ["9007199254740992.00", "1.00"]
        credits = ["9007199254740992.00"]
        # Float agrees with itself and is wrong: 9007199254740992.0 + 1.0
        # rounds straight back to 9007199254740992.0, so the missing dollar
        # vanishes and the report looks balanced.
        self.assertEqual(
            float(debits[0]) + float(debits[1]), float(credits[0]),
            "the fixture no longer exercises float's rounding",
        )
        self.assertNotEqual(
            Decimal(debits[0]) + Decimal(debits[1]), Decimal(credits[0])
        )

        raised, out, data = self.run_export(
            [
                ("Cash (090)", debits[0], "", debits[0], ""),
                ("Rounding (091)", debits[1], "", debits[1], ""),
                ("Equity (960)", "", credits[0], "", credits[0]),
            ]
        )
        self.assertIsInstance(raised, SystemExit)
        self.assertEqual(raised.code, 1)
        self.assertIn("WARNING: movement debits", out)
        self.assertIn("diff 1.0", out)
        self.assertIsNone(data, "an unbalanced report must not reach disk")

    def test_a_cent_at_the_admitted_magnitude_bound_still_unbalances(self):
        # to_number admits up to 33 significant digits; Decimal arithmetic
        # rounds at the context precision (28 by default), which would drop
        # this cent from the totals and print Balance check OK. The totals
        # run under a 50-digit local context so the cent survives.
        raised, out, data = self.run_export(
            [
                ("Cash (090)", "1000000000000000000000000000000.01", "",
                 "1000000000000000000000000000000.01", ""),
                ("Equity (960)", "", "1000000000000000000000000000000.00",
                 "", "1000000000000000000000000000000.00"),
            ]
        )
        self.assertIsInstance(raised, SystemExit)
        self.assertEqual(raised.code, 1)
        self.assertIn("WARNING: movement debits", out)
        self.assertIn("diff 0.01", out)
        self.assertIsNone(data, "an unbalanced report must not reach disk")

    def test_a_vanishing_exponent_is_refused_with_sensible_wording(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.to_number("1E-31")
        message = str(ctx.exception)
        self.assertIn("smaller than any ledger balance", message)
        self.assertNotIn("digits long", message)

    def test_an_exported_amount_keeps_the_digits_xero_sent(self):
        odd = "9007199254740993.00"  # 2**53 + 1, not representable as a float
        self.assertEqual(float(odd), 9007199254740992.0)

        raised, _, data = self.run_export(
            [
                ("Cash (090)", odd, "", odd, ""),
                ("Equity (960)", "", odd, "", odd),
            ]
        )
        self.assertIsNone(raised)
        self.assertIn(b"9007199254740993.0", data)
        self.assertNotIn(b"9007199254740992.0", data)


class ToNumberTest(unittest.TestCase):
    def test_blank_cells_are_zero(self):
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(export_tb.to_number(raw), Decimal("0"))

    def test_amounts_parse_exactly(self):
        self.assertEqual(export_tb.to_number("0.1"), Decimal("0.1"))
        self.assertEqual(export_tb.to_number(" -1234.56 "), Decimal("-1234.56"))
        self.assertNotEqual(export_tb.to_number("0.1"), Decimal(0.1))

    def test_junk_and_non_finite_cells_exit_cleanly(self):
        """float() takes "nan" and "inf"; neither is an amount."""
        for raw in ("n/a", "1,200.00", "NaN", "Infinity", "-inf"):
            with self.subTest(raw=raw):
                with self.assertRaises(SystemExit) as ctx:
                    export_tb.to_number(raw)
                self.assertTrue(
                    str(ctx.exception).startswith("error: "), str(ctx.exception)
                )

    def test_a_junk_cell_cannot_write_control_characters_to_the_terminal(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.to_number("\x1b[2Jwiped\nSet XERO_CLIENT_SECRET=")
        message = str(ctx.exception)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\n", message)
        self.assertIn("wiped", message)


class FormatAmountTest(unittest.TestCase):
    def test_matches_what_the_float_path_wrote(self):
        cases = {
            "1200.00": "1200.0",
            "15234.50": "15234.5",
            "0.00": "0.0",
            "-5.00": "-5.0",
            "1000": "1000.0",
            "0.125": "0.125",
            "1E+3": "1000.0",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(export_tb.format_amount(Decimal(raw)), expected)


class AbsurdMagnitudeTest(unittest.TestCase):
    """Finite is not the same as usable.

    Decimal's default context stops at Emax 999999, so "1E1000000" parses,
    passes the is_finite guard, and then raises decimal.Overflow on the first
    total_debit += debit. Overflow subclasses ArithmeticError, so it would
    surface as a traceback after the tenant name had already gone to stdout.
    The float path it replaces exited cleanly on the same payload, so without
    the guard the conversion would introduce a regression.
    """

    def test_an_exponent_past_the_decimal_context_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.to_number("1E1000000")
        self.assertIn("digits long", str(ctx.exception))
        self.assertTrue(str(ctx.exception).startswith("error: "))

    def test_the_arithmetic_that_used_to_overflow_now_never_runs(self):
        """The proof the guard is in the right place: without it, summing
        two of these raises decimal.Overflow rather than reaching any check."""
        from decimal import Overflow

        with self.assertRaises(Overflow):
            Decimal("1E1000000") + Decimal("1E1000000")
        with self.assertRaises(SystemExit):
            export_tb.to_number("1E1000000")

    def test_a_million_digit_cell_is_refused_before_it_reaches_the_csv(self):
        """Just under the overflow, "1E999999" totals fine and then makes
        format_amount build a one-million-character CSV field."""
        with self.assertRaises(SystemExit):
            export_tb.to_number("1E999999")
        with self.assertRaises(SystemExit):
            export_tb.to_number("9" * 1000001)

    def test_the_cell_is_stripped_before_it_reaches_the_terminal(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.to_number("1E1000000\x1b[2J\nWARNING: fake")
        message = str(ctx.exception)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\n", message.split("The API shape")[0])

    def test_every_amount_a_real_ledger_holds_still_parses(self):
        for cell in ("0", "0.00", "-1234.56", "1200.00", "1E20", "-1E29",
                     "999999999999999999999999999999"):
            with self.subTest(cell=cell):
                self.assertIsInstance(export_tb.to_number(cell), Decimal)


if __name__ == "__main__":
    unittest.main()
