"""Tests for the export pipeline: money precision and CSV output.

Standard library only. The Xero calls are stubbed, so nothing here touches
the network or token.json.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_tb  # noqa: E402

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
    """Runs export_tb.main() against a stubbed Xero."""

    def run_export(self, accounts, date="2026-06-30"):
        """Return (SystemExit or None, stdout, csv bytes or None)."""
        work_dir = tempfile.mkdtemp()
        out_path = os.path.join(work_dir, "tb.csv")
        payload = _report(accounts)
        argv = ["export_tb.py", "--date", date, "--out", out_path]
        env = {
            "XERO_CLIENT_ID": "id-not-a-secret",
            "XERO_CLIENT_SECRET": "secret-not-used",
        }
        connections = [{"tenantId": "tenant-guid", "tenantName": "Sample Trading Pty Ltd"}]
        buffer = io.StringIO()
        raised = None
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


if __name__ == "__main__":
    unittest.main()


class AbsurdMagnitudeTest(unittest.TestCase):
    """Finite is not the same as usable.

    Decimal's default context stops at Emax 999999, so "1E1000000" parses,
    passes the is_finite guard, and then raises decimal.Overflow on the first
    total_debit += debit. Overflow subclasses ArithmeticError, outside the
    CLI's handler tuple, so it printed a traceback after the tenant name had
    already gone to stdout. The float build it replaced exited cleanly on the
    same payload, which makes this a regression the conversion introduced.
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
