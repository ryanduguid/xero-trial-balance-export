import argparse
import io
import os
import sys
import tempfile
import unicodedata
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


def _report(accounts, section="Assets", header=HEADER):
    """Build a Trial Balance payload.

    accounts: [(account_label, debit, credit, ytd_debit, ytd_credit), ...]
    """
    rows = [
        {
            "RowType": "Header",
            "Cells": [{"Value": title} for title in header],
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


_UNSET = object()  # "no payload override", so None stays a testable payload


class _ExportCase(unittest.TestCase):
    """Runs export_tb.main() against a stubbed Xero.

    output_path confines --out beneath the process working directory, so the
    run is chdir'd into a scratch directory and --out stays relative - the
    same shape a scheduled job uses.
    """

    def run_export(
        self,
        accounts,
        date="2026-06-30",
        *,
        out="tb.csv",
        tenant_name="Sample Trading Pty Ltd",
        tenant_id="tenant-guid",
        section="Assets",
        header=HEADER,
        payload=_UNSET,
    ):
        """Return (SystemExit or None, stdout, csv bytes or None).

        out=None omits --out entirely, which is the path that resolves the
        default filename; the scratch directory is left on self.work_dir so a
        caller can inspect a filename it did not choose.

        payload= replaces the whole stubbed Xero response, for the guards that
        fire before a report can be built out of accounts at all.
        """
        work_dir = tempfile.mkdtemp()
        self.work_dir = work_dir
        out_path = None if out is None else os.path.join(work_dir, out)
        if payload is _UNSET:
            payload = _report(accounts, section=section, header=header)
        argv = ["export_tb.py", "--date", date]
        if out is not None:
            argv += ["--out", out]
        env = {
            "XERO_CLIENT_ID": "id-not-a-secret",
            "XERO_CLIENT_SECRET": "secret-not-used",
        }
        connections = [{"tenantId": tenant_id, "tenantName": tenant_name}]
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
        if out_path is not None and os.path.exists(out_path):
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


class ReportShapeTest(unittest.TestCase):
    def test_a_malformed_reports_value_exits_cleanly_instead_of_raising(self):
        connections = [{"tenantId": "tenant-guid", "tenantName": "Sample Trading Pty Ltd"}]
        env = {"XERO_CLIENT_ID": "id-not-a-secret", "XERO_CLIENT_SECRET": "secret-not-used"}
        for payload in (
            {"Reports": {"Rows": []}},
            {"Reports": "TrialBalance"},
            {"Reports": ["TrialBalance"]},
            {"Reports": [["Rows"]]},
        ):
            with self.subTest(payload=payload):
                work_dir = tempfile.mkdtemp()
                argv = ["export_tb.py", "--date", "2026-06-30", "--out", "tb.csv"]
                previous_dir = os.getcwd()
                os.chdir(work_dir)
                try:
                    with mock.patch.object(export_tb, "load_dotenv", lambda *a, **k: None), \
                            mock.patch.object(export_tb, "get_connections", return_value=connections), \
                            mock.patch.object(export_tb, "api_get", return_value=payload), \
                            mock.patch.dict(os.environ, env, clear=False), \
                            mock.patch.object(sys, "argv", argv):
                        with redirect_stdout(io.StringIO()):
                            with self.assertRaises(SystemExit) as caught:
                                export_tb.main()
                finally:
                    os.chdir(previous_dir)
                self.assertIn("Reports shape", str(caught.exception))


class ConnectionValidationTest(unittest.TestCase):
    def test_remote_connection_metadata_must_be_safe_and_complete(self):
        for payload in (
            {"tenantId": "id", "tenantName": "name"},
            ["not-an-object"],
            [{"tenantId": "id"}],
            [{"tenantId": "id", "tenantName": "Injected\nWARNING"}],
        ):
            with self.subTest(payload=payload), self.assertRaises(SystemExit):
                export_tb.validated_connections(payload)

    def test_main_puts_the_connections_response_through_the_validator(self):
        """The function above is only worth testing if main() calls it.

        Without the call, a tenantName carrying a newline reaches stdout, the
        Tenant column of the CSV and the default filename; the test above
        passes either way, so it is the wiring that has to be pinned. The
        report call must not happen at all - it costs this run's single-use
        refresh token.
        """
        env = {"XERO_CLIENT_ID": "id-not-a-secret", "XERO_CLIENT_SECRET": "secret-not-used"}
        for payload, expected in (
            ([{"tenantId": "id", "tenantName": "Injected\nWARNING: fake"}], "tenantName"),
            ([{"tenantId": "id\x1b[2J", "tenantName": "Sample"}], "tenantId"),
            ([{"tenantId": "id"}], "tenantName"),
            (["not-an-object"], "is not an object"),
            ({"tenantId": "id", "tenantName": "Sample"}, "is not a list"),
        ):
            with self.subTest(payload=payload):
                work_dir = tempfile.mkdtemp()
                argv = ["export_tb.py", "--date", "2026-06-30", "--out", "tb.csv"]
                previous_dir = os.getcwd()
                os.chdir(work_dir)
                try:
                    with mock.patch.object(export_tb, "load_dotenv", lambda *a, **k: None), \
                            mock.patch.object(export_tb, "get_connections", return_value=payload), \
                            mock.patch.object(export_tb, "api_get") as api_get, \
                            mock.patch.dict(os.environ, env, clear=False), \
                            mock.patch.object(sys, "argv", argv):
                        with redirect_stdout(io.StringIO()) as out:
                            with self.assertRaises(SystemExit) as ctx:
                                export_tb.main()
                finally:
                    os.chdir(previous_dir)
                message = str(ctx.exception)
                self.assertTrue(message.startswith("error: "), message)
                self.assertIn(expected, message)
                self.assertNotIn("WARNING: fake", out.getvalue())
                api_get.assert_not_called()
                self.assertEqual(os.listdir(work_dir), [])


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


class BalanceCheckTest(_ExportCase):
    """Both pairs are checked, not just the movement pair.

    Every other unbalanced fixture in this file moves the movement pair and
    the YTD pair by the same amount, so the movement branch alone satisfies
    them and the YTD tuple in the loop could be deleted unnoticed. The YTD
    columns are the ones the README tells users to slice for a year-end.
    """

    def test_a_balanced_movement_does_not_excuse_an_unbalanced_ytd(self):
        raised, out, data = self.run_export(
            [
                ("Cash (090)", "100.00", "", "100.00", ""),
                ("Equity (960)", "", "100.00", "", "90.00"),
            ]
        )
        self.assertIsInstance(raised, SystemExit)
        self.assertEqual(raised.code, 1)
        self.assertIn("WARNING: YTD debits", out)
        self.assertIn("diff 10.0", out)
        self.assertNotIn("WARNING: movement debits", out)
        self.assertIsNone(data, "an unbalanced YTD pair must not reach disk")

    def test_a_difference_under_half_a_cent_is_still_a_difference(self):
        """The comparison is exact, and only an exact one can say so.

        The old round(diff, 2) existed to absorb float noise. The totals are
        Decimal now, so there is no noise to absorb and the rounding only
        swallowed real differences: a tenth of a cent out is a report Xero
        did not send, and under the rounded comparison it was written to the
        path a refresh reads with "Balance check OK" printed over it.
        """
        raised, out, data = self.run_export(
            [
                ("Cash (090)", "100.001", "", "100.001", ""),
                ("Equity (960)", "", "100.000", "", "100.000"),
            ]
        )
        self.assertIsInstance(raised, SystemExit)
        self.assertEqual(raised.code, 1)
        self.assertIn("WARNING: movement debits", out)
        self.assertIn("diff 0.001", out)
        self.assertNotIn("Balance check OK", out)
        self.assertIsNone(data, "a sub-cent difference reached disk")

    def test_a_balanced_ytd_does_not_excuse_an_unbalanced_movement(self):
        raised, out, data = self.run_export(
            [
                ("Cash (090)", "100.00", "", "100.00", ""),
                ("Equity (960)", "", "90.00", "", "100.00"),
            ]
        )
        self.assertIsInstance(raised, SystemExit)
        self.assertEqual(raised.code, 1)
        self.assertIn("WARNING: movement debits", out)
        self.assertNotIn("WARNING: YTD debits", out)
        self.assertIsNone(data)


class ExcelSafeTest(unittest.TestCase):
    """Formula injection: account and org names are free text anyone in the
    client's Xero org can edit, and the CSV is built for double-click opening
    in Excel."""

    def test_every_owasp_trigger_is_forced_to_text(self):
        for trigger in ("=", "+", "-", "@", "\t", "\r", "\n"):
            with self.subTest(trigger=trigger):
                self.assertEqual(
                    export_tb.excel_safe(trigger + "cmd|'/c calc'!A1"),
                    "'" + trigger + "cmd|'/c calc'!A1",
                )

    def test_ordinary_values_pass_through_untouched(self):
        for value in ("", "090", "Business Bank Account", "Rent (Sydney)",
                      "Smith & Co. Pty Ltd", "Sales =revenue"):
            with self.subTest(value=value):
                self.assertEqual(export_tb.excel_safe(value), value)


class ExcelInjectionExportTest(_ExportCase):
    def test_the_apostrophe_reaches_the_csv_for_every_free_text_column(self):
        raised, _, data = self.run_export(
            [
                ("=cmd|'/c calc'!A1 (090)", "1200.00", "", "1200.00", ""),
                ("Trade Debtors (610)", "", "1200.00", "", "1200.00"),
            ],
            tenant_name='=HYPERLINK("http://x")',
            section="+Assets",
        )
        self.assertIsNone(raised)
        text = data.decode("utf-8-sig")
        self.assertIn("'=cmd|'/c calc'!A1", text)  # AccountName
        self.assertIn("'+Assets", text)  # Section
        self.assertIn("'=HYPERLINK", text)  # Tenant
        self.assertNotIn(",=cmd", text)


class DefaultFilenameTest(unittest.TestCase):
    """The sanitiser drops everything outside ASCII, folds ASCII punctuation
    onto "-" and folds case, so it can collapse two different orgs onto one
    filename and let the second export overwrite the first client's numbers.
    The tenant ID is the only per-org value in the name, so every default
    filename carries it."""

    ID = "abcdef12-3456-7890-1234-567890abcdef"

    def _name(self, tenant_name, tenant_id=None):
        tenant = {"tenantName": tenant_name, "tenantId": tenant_id or self.ID}
        return export_tb.default_output_filename(tenant, "2026-06-30", "accrual")

    def test_two_orgs_differing_only_outside_ascii_get_different_filenames(self):
        first = self._name("美食餐厅 Pty Ltd", "11111111-aaaa")
        second = self._name("东方贸易 Pty Ltd", "22222222-bbbb")
        self.assertEqual(first, "pty-ltd-11111111-tb-2026-06-30-accrual.csv")
        self.assertEqual(second, "pty-ltd-22222222-tb-2026-06-30-accrual.csv")
        self.assertNotEqual(first, second)

    def test_a_dropped_letter_is_enough_to_add_the_discriminator(self):
        self.assertEqual(
            self._name("Ngā Taonga Ltd"),
            "ng-taonga-ltd-abcdef12-tb-2026-06-30-accrual.csv",
        )

    def test_names_differing_only_in_a_non_alphanumeric_character_still_split(self):
        """The collision does not need a letter. An emoji, a fullwidth comma
        and a combining macron are all str.isalnum() == False, and all three
        collapse to "-" like any other character outside ASCII."""
        for first_name, second_name, stem in (
            ("\U0001F40D Pty Ltd", "\U0001F986 Pty Ltd", "pty-ltd"),
            ("Wong，Li Pty Ltd", "Wong；Li Pty Ltd", "wong-li-pty-ltd"),
        ):
            with self.subTest(names=(first_name, second_name)):
                first = self._name(first_name, "11111111-aaaa")
                second = self._name(second_name, "22222222-bbbb")
                self.assertEqual(first, f"{stem}-11111111-tb-2026-06-30-accrual.csv")
                self.assertEqual(second, f"{stem}-22222222-tb-2026-06-30-accrual.csv")
                self.assertNotEqual(first, second)

    def test_a_decomposed_name_writes_the_same_file_as_its_composed_form(self):
        """Same org, same file, whichever normalisation form the API sends."""
        composed = "Ngā Taonga Ltd"
        decomposed = unicodedata.normalize("NFD", composed)
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(self._name(decomposed), self._name(composed))

    def test_a_tenant_id_cannot_turn_the_default_name_into_a_path(self):
        """The discriminator is remote input as well, and main() creates the
        output directory before writing - so an unsanitised separator in it
        would put the export in a directory tree nobody asked for."""
        for tenant_id in ("aa/bb/cc-dd", "aa\\bb\\cc-dd", "../../etc"):
            with self.subTest(tenant_id=tenant_id):
                filename = self._name("美食 Pty Ltd", tenant_id)
                self.assertNotIn("/", filename)
                self.assertNotIn("\\", filename)
                self.assertEqual(os.path.basename(filename), filename)
        self.assertEqual(
            self._name("美食 Pty Ltd", "aa/bb/cc-dd"),
            "pty-ltd-aa-bb-cc-tb-2026-06-30-accrual.csv",
        )

    def test_names_differing_only_in_ascii_punctuation_or_case_still_split(self):
        """The collision does not need a character outside ASCII either.

        Parentheses, an ampersand, a slash and an uppercase letter all end up
        as the same "-" or the same lowercase letter, so the earlier
        non-ASCII-only rule left every pair below sharing one filename - the
        exact harm the rule was written to stop, one class of character
        further along.
        """
        for first_name, second_name, stem in (
            ("Acme (Holdings) Pty Ltd", "Acme Holdings Pty Ltd", "acme-holdings-pty-ltd"),
            ("Smith & Co Pty Ltd", "Smith Co Pty Ltd", "smith-co-pty-ltd"),
            ("Jones/Brown Trust", "Jones Brown Trust", "jones-brown-trust"),
            ("ACME Pty Ltd", "Acme Pty Ltd", "acme-pty-ltd"),
        ):
            with self.subTest(names=(first_name, second_name)):
                first = self._name(first_name, "11111111-aaaa")
                second = self._name(second_name, "22222222-bbbb")
                self.assertEqual(first, f"{stem}-11111111-tb-2026-06-30-accrual.csv")
                self.assertEqual(second, f"{stem}-22222222-tb-2026-06-30-accrual.csv")
                self.assertNotEqual(first, second)

    def test_an_all_ascii_org_name_carries_the_tenant_id_too(self):
        self.assertEqual(
            self._name("Demo Company (AU)"),
            "demo-company-au-abcdef12-tb-2026-06-30-accrual.csv",
        )
        self.assertEqual(
            self._name("Smith & Co. Pty Ltd"),
            "smith-co.-pty-ltd-abcdef12-tb-2026-06-30-accrual.csv",
        )

    def test_a_name_that_sanitises_away_falls_back_to_the_tenant_id(self):
        self.assertEqual(self._name("***"), "abcdef12-tb-2026-06-30-accrual.csv")
        self.assertEqual(
            self._name("美食"), "abcdef12-tb-2026-06-30-accrual.csv"
        )

    def test_cash_and_accrual_runs_do_not_overwrite_each_other(self):
        tenant = {"tenantName": "Demo Company", "tenantId": self.ID}
        self.assertNotEqual(
            export_tb.default_output_filename(tenant, "2026-06-30", "accrual"),
            export_tb.default_output_filename(tenant, "2026-06-30", "cash"),
        )


class DefaultFilenameExportTest(_ExportCase):
    def test_a_run_without_out_writes_the_discriminated_default_name(self):
        raised, out, _ = self.run_export(
            [
                ("Cash (090)", "100.00", "", "100.00", ""),
                ("Equity (960)", "", "100.00", "", "100.00"),
            ],
            out=None,
            tenant_name="Ngā Taonga Ltd",
            tenant_id="abcdef12-3456-7890",
        )
        self.assertIsNone(raised)
        self.assertEqual(
            sorted(os.listdir(self.work_dir)),
            ["ng-taonga-ltd-abcdef12-tb-2026-06-30-accrual.csv"],
        )
        self.assertIn("ng-taonga-ltd-abcdef12-tb-2026-06-30-accrual.csv", out)

    def test_a_default_run_creates_no_directory_under_the_working_directory(self):
        """A run with no --out must write one CSV in the working directory.
        The output directory is created before the write, so a separator in
        the tenant ID would otherwise materialise a tree here."""
        raised, out, _ = self.run_export(
            [
                ("Cash (090)", "100.00", "", "100.00", ""),
                ("Equity (960)", "", "100.00", "", "100.00"),
            ],
            out=None,
            tenant_name="美食 Pty Ltd",
            tenant_id="aa/bb/cc-dd",
        )
        self.assertIsNone(raised)
        entries = sorted(os.listdir(self.work_dir))
        self.assertEqual(entries, ["pty-ltd-aa-bb-cc-tb-2026-06-30-accrual.csv"])
        self.assertFalse(
            [e for e in entries if os.path.isdir(os.path.join(self.work_dir, e))],
            "the default filename created a directory tree",
        )
        self.assertIn("pty-ltd-aa-bb-cc-tb-2026-06-30-accrual.csv", out)


class NestedOutputDirectoryTest(_ExportCase):
    """--out exports/tb.csv is accepted by output_path, so the parent may not
    exist when the CSV is written - which used to be a FileNotFoundError
    traceback raised after the report had been fetched and this run's
    single-use refresh token spent."""

    def test_a_missing_parent_directory_is_created_rather_than_crashing(self):
        raised, out, data = self.run_export(
            [
                ("Cash (090)", "100.00", "", "100.00", ""),
                ("Equity (960)", "", "100.00", "", "100.00"),
            ],
            out="exports/tb.csv",
        )
        self.assertIsNone(raised)
        self.assertIsNotNone(data, "the export did not reach the nested path")
        self.assertIn(b"Cash", data)
        self.assertEqual(os.listdir(os.path.join(self.work_dir, "exports")), ["tb.csv"])


class OutputReplaceLockTest(_ExportCase):
    """Windows refuses os.replace onto a CSV that Excel or Power BI Desktop
    holds open, which is the README's own scheduled-refresh recipe. The
    finished, balance-checked export must survive that."""

    BALANCED = [
        ("Cash (090)", "100.00", "", "100.00", ""),
        ("Equity (960)", "", "100.00", "", "100.00"),
    ]

    def test_a_transient_lock_is_ridden_out(self):
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)

        with mock.patch.object(export_tb.os, "replace", side_effect=flaky), \
                mock.patch.object(export_tb.time, "sleep") as sleep:
            raised, _, data = self.run_export(self.BALANCED)

        self.assertIsNone(raised)
        self.assertEqual(len(calls), 3)
        self.assertIsNotNone(data)
        self.assertEqual(
            [c.args[0] for c in sleep.call_args_list],
            [export_tb.REPLACE_BACKOFF * 1, export_tb.REPLACE_BACKOFF * 2],
        )
        self.assertEqual(
            [f for f in os.listdir(self.work_dir) if f.endswith(".tmp")], []
        )

    def test_a_held_destination_keeps_the_finished_export_and_names_it(self):
        err = PermissionError(13, "Access is denied")
        with mock.patch.object(export_tb.os, "replace", side_effect=err), \
                mock.patch.object(export_tb.time, "sleep") as sleep:
            raised, _, data = self.run_export(self.BALANCED)

        self.assertIsInstance(raised, SystemExit)
        message = str(raised.code)
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("tb.csv", message)
        self.assertEqual(sleep.call_count, export_tb.REPLACE_ATTEMPTS - 1)
        self.assertIsNone(data, "os.replace never ran, so nothing is at --out")

        leftovers = [f for f in os.listdir(self.work_dir) if f.endswith(".tmp")]
        self.assertEqual(
            len(leftovers), 1, "the completed export was deleted, not kept"
        )
        tmp_path = os.path.join(self.work_dir, leftovers[0])
        self.assertIn(tmp_path, message)
        with open(tmp_path, "rb") as fh:
            recovered = fh.read()
        self.assertIn(b"Cash", recovered)
        self.assertIn(b"ReportDate,Tenant,Section", recovered)


class OutputFsyncFailureTest(_ExportCase):
    """By the flush the temp file is whole, so it must outlive the fsync.

    This is save_tokens' rule applied to the export: deleting the temp file
    here throws away a finished, balance-checked report whose API call cannot
    be replayed for free, and an uncaught OSError out of os.fsync is a bare
    traceback in a scheduled task's log - run() catches transport failures
    only. The sibling guard is
    test_xero_client.SaveTokensTest.test_an_fsync_failure_keeps_the_new_token_on_disk.
    """

    BALANCED = [
        ("Cash (090)", "100.00", "", "100.00", ""),
        ("Equity (960)", "", "100.00", "", "100.00"),
    ]

    def test_an_fsync_failure_keeps_the_finished_export_and_names_it(self):
        err = OSError(5, "Input/output error")
        with mock.patch.object(export_tb.os, "fsync", side_effect=err):
            raised, _, data = self.run_export(self.BALANCED)

        self.assertIsInstance(
            raised, SystemExit, "the OSError reached the operator as a traceback"
        )
        message = str(raised.code)
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("Input/output error", message)
        self.assertIsNone(data, "os.replace never ran, so nothing is at --out")

        leftovers = [f for f in os.listdir(self.work_dir) if f.endswith(".tmp")]
        self.assertEqual(
            len(leftovers), 1, "the completed export was deleted, not kept"
        )
        tmp_path = os.path.join(self.work_dir, leftovers[0])
        self.assertIn(tmp_path, message)
        with open(tmp_path, "rb") as fh:
            recovered = fh.read()
        self.assertIn(b"Cash", recovered)
        self.assertIn(b"ReportDate,Tenant,Section", recovered)

    def test_a_half_written_export_is_still_cleaned_up(self):
        """The other side of the same flag: nothing is whole before the flush,
        so a failure there must still leave no temp file behind."""
        with mock.patch.object(
            export_tb.csv.DictWriter, "writerows", side_effect=ValueError("boom")
        ):
            with self.assertRaises(ValueError):
                self.run_export(self.BALANCED)
        self.assertEqual(
            [f for f in os.listdir(self.work_dir) if f.endswith(".tmp")], []
        )


class FlattenReportShapeTest(unittest.TestCase):
    """A cell-count change and a missing Header row both hit the strict zip.
    Neither is a bug in this script, so neither should print a traceback."""

    def _payload(self, cells, header=HEADER, title="Assets"):
        rows = []
        if header is not None:
            rows.append(
                {"RowType": "Header", "Cells": [{"Value": t} for t in header]}
            )
        rows.append(
            {
                "RowType": "Section",
                "Title": title,
                "Rows": [{"RowType": "Row", "Cells": [{"Value": v} for v in cells]}],
            }
        )
        return {"Rows": rows}

    def test_a_non_string_section_title_does_not_crash_the_export(self):
        """The section label is built for every Section, before any error.

        _shown() slices its argument, so a Title the API sent as null or a
        number used to raise a raw TypeError here - after the tenant name had
        reached stdout and this run's single-use refresh token was spent, and
        with the balanced report thrown away. A label is not worth a run.
        """
        for title in (None, 7, True, {"nested": 1}, ["a"]):
            with self.subTest(title=title):
                titles, flat = export_tb.flatten_report(
                    self._payload(["Cash (090)", "1200.00", "", "1200.00", ""], title=title)
                )
                self.assertEqual(titles, HEADER)
                self.assertEqual(flat[0]["Account"], "Cash (090)")
                self.assertEqual(flat[0]["Section"], title)

    def test_a_non_string_section_title_is_still_shown_in_a_shape_error(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.flatten_report(
                self._payload(["Cash (090)", "1200.00", "", "extra"], title=None)
            )
        message = str(ctx.exception)
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("None", message)

    def test_a_non_string_cell_value_exits_instead_of_crashing(self):
        """The cell the section Title labels was left unguarded when the
        Title itself was fixed.

        ACCOUNT_PATTERN.match on a mapping raises "expected string or
        bytes-like object", and a Header cell holding a list raises
        "unhashable type" at record[title]. Both are raw tracebacks, printed
        after the tenant name has reached stdout and after the single-use
        refresh token behind the report call has been spent.
        """
        for value in ({"nested": 1}, ["Cash"], True):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as ctx:
                    export_tb.flatten_report(
                        self._payload([value, "1200.00", "", "1200.00", ""])
                    )
                message = str(ctx.exception)
                self.assertTrue(message.startswith("error: "), message)
                self.assertIn("not text or a number", message)
                self.assertIn("Assets", message)

    def test_a_non_string_header_title_exits_instead_of_crashing(self):
        for title in ({"nested": 1}, ["Account"]):
            with self.subTest(title=title):
                with self.assertRaises(SystemExit) as ctx:
                    export_tb.flatten_report(
                        self._payload(
                            ["Cash (090)", "1200.00", "", "1200.00", ""],
                            header=[title, "Debit", "Credit", "YTD Debit", "YTD Credit"],
                        )
                    )
                message = str(ctx.exception)
                self.assertTrue(message.startswith("error: "), message)
                self.assertIn("not text or a number", message)

    def test_a_null_or_numeric_cell_value_is_still_read(self):
        """The two JSON shapes that are not a shape change.

        A null cell is the blank the report format uses for a nil balance -
        the same thing a missing Value key has always meant here, and what
        to_number reads as zero. A JSON number is an amount written another
        way, which to_number has always coerced with str(). Refusing either
        would throw away a fetched report over a cell that carries no
        ambiguity.
        """
        _, flat = export_tb.flatten_report(self._payload([None, 1200, 0.0, 1200, 0.0]))
        self.assertEqual(flat[0]["Account"], "")
        self.assertEqual(flat[0]["Debit"], "1200")
        self.assertEqual(export_tb.to_number(flat[0]["Credit"]), Decimal("0"))

    def test_a_matching_row_still_flattens(self):
        titles, flat = export_tb.flatten_report(
            self._payload(["Cash (090)", "1200.00", "", "1200.00", ""])
        )
        self.assertEqual(titles, HEADER)
        self.assertEqual(flat[0]["Account"], "Cash (090)")
        self.assertEqual(flat[0]["Section"], "Assets")

    def test_an_extra_cell_exits_cleanly_instead_of_raising_valueerror(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.flatten_report(
                self._payload(["Cash (090)", "1200.00", "", "1200.00", "", "extra"])
            )
        message = str(ctx.exception)
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("6 cells", message)
        self.assertIn("5 header columns", message)
        self.assertIn("Assets", message)

    def test_a_missing_cell_is_refused_too(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.flatten_report(self._payload(["Cash (090)", "1200.00"]))
        self.assertIn("2 cells", str(ctx.exception))

    def test_a_report_with_no_header_row_exits_cleanly(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.flatten_report(
                self._payload(["Cash (090)", "1200.00", "", "1200.00", ""], header=None)
            )
        self.assertIn("0 header columns", str(ctx.exception))

    def test_the_join_key_comes_from_the_account_attribute(self):
        """AccountID is the stable join key the README sells; a data cell can
        carry more than one attribute, and only the account one is the GUID."""
        payload = {
            "Rows": [
                {"RowType": "Header", "Cells": [{"Value": t} for t in HEADER]},
                {
                    "RowType": "Section",
                    "Title": "Assets",
                    "Rows": [
                        {
                            "RowType": "Row",
                            "Cells": [
                                {
                                    "Value": "Cash (090)",
                                    "Attributes": [
                                        {"Value": "not-the-guid", "Id": "rowType"},
                                        {"Value": "the-account-guid", "Id": "account"},
                                    ],
                                },
                                {"Value": "1.00"},
                                {"Value": ""},
                                {"Value": "1.00"},
                                {"Value": ""},
                            ],
                        }
                    ],
                },
            ]
        }
        _, flat = export_tb.flatten_report(payload)
        self.assertEqual(flat[0]["AccountID"], "the-account-guid")

    def test_a_non_string_account_attribute_never_reaches_the_csv(self):
        """The join key takes the same rule as a cell Value.

        AccountID is written out verbatim, so an attribute Value the API sent
        as an object would land in the column the README sells as a GUID -
        as its Python repr, with no error anywhere.
        """
        payload = {
            "Rows": [
                {"RowType": "Header", "Cells": [{"Value": t} for t in HEADER]},
                {
                    "RowType": "Section",
                    "Title": "Assets",
                    "Rows": [
                        {
                            "RowType": "Row",
                            "Cells": [
                                {
                                    "Value": "Cash (090)",
                                    "Attributes": [
                                        {"Value": {"nested": 1}, "Id": "account"}
                                    ],
                                },
                                {"Value": "1.00"},
                                {"Value": ""},
                                {"Value": "1.00"},
                                {"Value": ""},
                            ],
                        }
                    ],
                },
            ]
        }
        with self.assertRaises(SystemExit) as ctx:
            export_tb.flatten_report(payload)
        message = str(ctx.exception)
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("not text or a number", message)

    def test_every_nested_list_must_hold_objects(self):
        """main() proves the Reports envelope is a list of objects and the
        strict zip proves a row's cell count; everything between them was
        unguarded, so a Rows, Cells or Attributes value that was a string, a
        mapping or a list of strings called .get() on a str and printed a raw
        AttributeError - after the tenant name had gone to stdout and the
        single-use refresh token behind the report call had been spent."""
        section = {"RowType": "Section", "Title": "Assets", "Rows": []}
        row = {"RowType": "Row", "Cells": [{"Value": "Cash (090)"}]}
        for payload, key in (
            ({"Rows": "junk"}, "Rows"),
            ({"Rows": {"Rows": []}}, "Rows"),
            ({"Rows": ["Assets"]}, "Rows"),
            ({"Rows": [{"RowType": "Header", "Cells": "junk"}]}, "Cells"),
            ({"Rows": [{"RowType": "Header", "Cells": ["Account"]}]}, "Cells"),
            ({"Rows": [dict(section, Rows="junk")]}, "Rows"),
            ({"Rows": [dict(section, Rows=["Cash (090)"])]}, "Rows"),
            ({"Rows": [dict(section, Rows=[dict(row, Cells="junk")])]}, "Cells"),
            ({"Rows": [dict(section, Rows=[dict(row, Cells=["Cash"])])]}, "Cells"),
            (
                {
                    "Rows": [
                        {"RowType": "Header", "Cells": [{"Value": "Account"}]},
                        dict(
                            section,
                            Rows=[
                                {
                                    "RowType": "Row",
                                    "Cells": [
                                        {"Value": "Cash (090)", "Attributes": "junk"}
                                    ],
                                }
                            ],
                        ),
                    ]
                },
                "Attributes",
            ),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(SystemExit) as ctx:
                    export_tb.flatten_report(payload)
                message = str(ctx.exception)
                self.assertTrue(message.startswith("error: "), message)
                self.assertIn(f"{key} value that is not a list of objects", message)

    def test_the_shape_message_names_the_section_and_strips_it(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.flatten_report(
                {
                    "Rows": [
                        {
                            "RowType": "Section",
                            "Title": "\x1b[2JAssets\nWARNING: fake",
                            "Rows": "junk",
                        }
                    ]
                }
            )
        message = str(ctx.exception)
        self.assertIn("Assets", message)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\n", message)

    def test_a_report_with_no_rows_key_is_still_empty_rather_than_an_error(self):
        self.assertEqual(export_tb.flatten_report({}), ([], []))
        self.assertEqual(export_tb.flatten_report({"Rows": []}), ([], []))

    def test_the_section_title_is_stripped_before_it_reaches_the_terminal(self):
        with self.assertRaises(SystemExit) as ctx:
            export_tb.flatten_report(
                self._payload(["Cash (090)"], title="\x1b[2JAssets\nWARNING: fake")
            )
        message = str(ctx.exception)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\n", message)


class NonStringCellExportTest(_ExportCase):
    """The same guard end to end, where the cost is visible.

    By the time a cell is read the report has been fetched, the tenant name
    is on stdout and this run's single-use refresh token is spent, so a cell
    Value the API sent as an object has to read as an instruction rather than
    as a TypeError traceback in a scheduled task's log.
    """

    def test_an_object_in_the_account_cell_exits_with_an_instruction(self):
        raised, out, data = self.run_export(
            [
                ({"nested": 1}, "100.00", "", "100.00", ""),
                ("Equity (960)", "", "100.00", "", "100.00"),
            ]
        )
        self.assertIsInstance(raised, SystemExit)
        message = str(raised.code)
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("not text or a number", message)
        self.assertIn("API shape may have changed", message)
        self.assertIn("Tenant: Sample Trading Pty Ltd", out)
        self.assertIsNone(data, "a report this script could not read reached disk")


class RawPayloadGuardTest(_ExportCase):
    """Two guards between api_get and the flattener.

    Both fire after the report call is spent, and the second one is what
    stops a header-only CSV landing at the path a scheduled Power BI refresh
    reads.
    """

    def test_a_response_that_is_not_a_json_object_exits_cleanly(self):
        for payload in ("TrialBalance", ["Reports"], 7, None):
            with self.subTest(payload=payload):
                raised, _, data = self.run_export([], payload=payload)
                self.assertIsInstance(raised, SystemExit)
                message = str(raised.code)
                self.assertTrue(message.startswith("error: "), message)
                self.assertIn("not a JSON object", message)
                self.assertIsNone(data)

    def test_a_header_only_report_writes_no_csv(self):
        payload = {
            "Reports": [
                {"Rows": [{"RowType": "Header", "Cells": [{"Value": t} for t in HEADER]}]}
            ]
        }
        raised, out, data = self.run_export([], payload=payload)
        self.assertIsInstance(raised, SystemExit)
        self.assertIn("no account rows", str(raised.code))
        self.assertNotIn("Balance check OK", out)
        self.assertIsNone(data, "a header-only CSV reached the refresh path")


class ColumnTitleGuardTest(_ExportCase):
    """The strict zip catches a cell-COUNT change only; a retitled column
    keeps the count and would zero every value through record.get()."""

    def test_a_retitled_column_stops_the_export_before_the_balance_check(self):
        raised, out, data = self.run_export(
            [
                ("Cash (090)", "100.00", "", "100.00", ""),
                ("Equity (960)", "", "100.00", "", "100.00"),
            ],
            header=["Account", "Debit", "Credit", "YTD Dr", "YTD Credit"],
        )
        self.assertIsInstance(raised, SystemExit)
        message = str(raised.code)
        self.assertIn("Unexpected report columns", message)
        self.assertIn("YTD Debit", message)
        self.assertNotIn("WARNING", out)
        self.assertIsNone(data)


class AccountCodeTest(unittest.TestCase):
    """"Business Bank Account (090)" splits; "Rent (Sydney)" must not, or it
    collides with the real Rent account on any name-keyed join and puts text
    in a column typed as a code."""

    def test_a_code_needs_at_least_one_digit(self):
        self.assertTrue(export_tb.is_account_code("090"))
        self.assertTrue(export_tb.is_account_code("GST1"))
        self.assertFalse(export_tb.is_account_code("Sydney"))
        self.assertFalse(export_tb.is_account_code("NAB"))

    def test_a_code_is_at_most_ten_characters(self):
        self.assertTrue(export_tb.is_account_code("1234567890"))
        self.assertFalse(export_tb.is_account_code("12345678901"))

    def test_a_code_is_alphanumeric(self):
        for value in ("09-0", "09 0", "090.1", ""):
            with self.subTest(value=value):
                self.assertFalse(export_tb.is_account_code(value))


class ParentheticalAccountExportTest(_ExportCase):
    def test_a_code_less_parenthetical_keeps_its_whole_name(self):
        raised, _, data = self.run_export(
            [
                ("Rent (Sydney)", "100.00", "", "100.00", ""),
                ("Term Deposit (NAB12345678901)", "0.00", "", "0.00", ""),
                ("Equity (960)", "", "100.00", "", "100.00"),
            ]
        )
        self.assertIsNone(raised)
        text = data.decode("utf-8-sig")
        self.assertIn(",Rent (Sydney),,", text)
        self.assertIn(",Term Deposit (NAB12345678901),,", text)
        self.assertIn(",Equity,960,", text)


class TenantSelectionTest(unittest.TestCase):
    """Picking the wrong org exports one client's numbers under another's
    name, so an ambiguous --tenant must stop before the report call."""

    def _run(self, connections, extra_argv):
        work_dir = tempfile.mkdtemp()
        argv = ["export_tb.py", "--date", "2026-06-30", "--out", "tb.csv"] + extra_argv
        env = {
            "XERO_CLIENT_ID": "id-not-a-secret",
            "XERO_CLIENT_SECRET": "secret-not-used",
        }
        previous_dir = os.getcwd()
        os.chdir(work_dir)
        try:
            with mock.patch.object(export_tb, "load_dotenv", lambda *a, **k: None), \
                    mock.patch.object(export_tb, "get_connections", return_value=connections), \
                    mock.patch.object(export_tb, "api_get") as api_get, \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(sys, "argv", argv):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        export_tb.main()
        finally:
            os.chdir(previous_dir)
        return str(ctx.exception), api_get

    TWO = [
        {"tenantId": "aaaa", "tenantName": "Acme Trading Pty Ltd"},
        {"tenantId": "bbbb", "tenantName": "Acme Holdings Pty Ltd"},
    ]

    def test_an_ambiguous_substring_stops_before_the_report_is_fetched(self):
        message, api_get = self._run(self.TWO, ["--tenant", "Acme"])
        self.assertIn("matches more than one organisation", message)
        self.assertIn("Acme Trading Pty Ltd", message)
        self.assertIn("Acme Holdings Pty Ltd", message)
        api_get.assert_not_called()

    def test_a_unique_substring_still_selects_its_org(self):
        connections = self.TWO
        work_dir = tempfile.mkdtemp()
        argv = [
            "export_tb.py", "--date", "2026-06-30", "--out", "tb.csv",
            "--tenant", "Holdings",
        ]
        env = {
            "XERO_CLIENT_ID": "id-not-a-secret",
            "XERO_CLIENT_SECRET": "secret-not-used",
        }
        payload = _report([("Cash (090)", "1.00", "", "1.00", ""),
                           ("Equity (960)", "", "1.00", "", "1.00")])
        previous_dir = os.getcwd()
        os.chdir(work_dir)
        buffer = io.StringIO()
        try:
            with mock.patch.object(export_tb, "load_dotenv", lambda *a, **k: None), \
                    mock.patch.object(export_tb, "get_connections", return_value=connections), \
                    mock.patch.object(export_tb, "api_get", return_value=payload) as api_get, \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(sys, "argv", argv):
                with redirect_stdout(buffer):
                    export_tb.main()
        finally:
            os.chdir(previous_dir)
        self.assertIn("Tenant: Acme Holdings Pty Ltd", buffer.getvalue())
        self.assertEqual(api_get.call_args.kwargs["tenant_id"], "bbbb")

    def test_two_orgs_and_no_tenant_flag_stops_before_the_report(self):
        message, api_get = self._run(self.TWO, [])
        self.assertIn("More than one organisation connected", message)
        api_get.assert_not_called()

    def test_a_substring_matching_nothing_names_the_connected_orgs(self):
        message, api_get = self._run(self.TWO, ["--tenant", "Beta"])
        self.assertIn('No tenant matching "Beta"', message)
        self.assertIn("Acme Trading Pty Ltd", message)
        api_get.assert_not_called()


class StreamEncodingTest(unittest.TestCase):
    """Non-console stdout on Windows is cp1252 (PEP 528), so a macron or a
    CJK character in an org name aborted a redirected or scheduled run before
    the report was ever fetched."""

    def test_both_streams_are_reconfigured_before_anything_is_printed(self):
        stdout, stderr = mock.Mock(), mock.Mock()
        with mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr), \
                mock.patch.object(sys, "argv", ["export_tb.py", "--out", "../outside.csv"]):
            with self.assertRaises(SystemExit):
                export_tb.main()
        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_a_stream_without_reconfigure_does_not_stop_the_run(self):
        """redirect_stdout hands main() a StringIO, and a pipe on an older
        interpreter has no reconfigure either."""
        with mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(sys, "stderr", io.StringIO()), \
                mock.patch.object(sys, "argv", ["export_tb.py", "--out", "../outside.csv"]):
            with self.assertRaises(SystemExit) as ctx:
                export_tb.main()
        self.assertEqual(ctx.exception.code, 2)


class TransportFailureTest(unittest.TestCase):
    """A scheduled export on a machine that is offline, or behind failing DNS,
    ended with a requests traceback in the Task Scheduler log. Every other
    failure in this script is a one-line exit carrying an instruction."""

    def test_a_network_failure_exits_with_one_line(self):
        import requests

        failure = requests.exceptions.ConnectionError("getaddrinfo failed")
        with mock.patch.object(export_tb, "main", side_effect=failure):
            with self.assertRaises(SystemExit) as ctx:
                export_tb.run()
        message = str(ctx.exception)
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("could not reach Xero", message)
        self.assertIn("getaddrinfo failed", message)

    def test_a_read_timeout_is_a_transport_failure_too(self):
        import requests

        failure = requests.exceptions.ReadTimeout("timed out")
        with mock.patch.object(export_tb, "main", side_effect=failure):
            with self.assertRaises(SystemExit) as ctx:
                export_tb.run()
        self.assertIn("could not reach Xero", str(ctx.exception))

    def test_an_http_status_is_not_relabelled_as_unreachability(self):
        """raise_for_status raises HTTPError, a RequestException, for every
        status xero_client does not answer itself. A 403 for a missing
        accounting.reports.trialbalance.read scope and a 400 for a bad date
        both fail again on the next run, so neither may be reported as a
        transport failure with "re-run later" attached."""
        import requests

        for status, reason in ((403, "Forbidden"), (400, "Bad Request"), (500, "Server Error")):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                response.url = "https://api.xero.com/api.xro/2.0/Reports/TrialBalance"
                failure = requests.exceptions.HTTPError(
                    f"{status} Client Error: {reason} for url: {response.url}",
                    response=response,
                )
                with mock.patch.object(export_tb, "main", side_effect=failure):
                    with self.assertRaises(requests.exceptions.HTTPError):
                        export_tb.run()

    def test_a_bug_in_this_script_still_surfaces_as_a_traceback(self):
        with mock.patch.object(export_tb, "main", side_effect=KeyError("boom")):
            with self.assertRaises(KeyError):
                export_tb.run()

    def test_a_clean_run_passes_through(self):
        with mock.patch.object(export_tb, "main") as main_fn:
            export_tb.run()
        main_fn.assert_called_once_with()


class IsoDateTest(unittest.TestCase):
    def test_an_iso_date_passes_through(self):
        self.assertEqual(export_tb.iso_date("2026-06-30"), "2026-06-30")

    def test_the_au_date_habit_is_refused_with_the_order_spelled_out(self):
        for raw in ("30/06/2026", "30-06-2026", "not-a-date", "", "2026-02-30"):
            with self.subTest(raw=raw):
                with self.assertRaises(argparse.ArgumentTypeError) as ctx:
                    export_tb.iso_date(raw)
                self.assertIn("YYYY-MM-DD", str(ctx.exception))


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
