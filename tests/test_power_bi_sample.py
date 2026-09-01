"""Contract checks for the committed Power Query sample.

The sample is only useful while it agrees with the CSV the exporter actually
writes. These tests pin that agreement, the typing decisions that make a trial
balance survive the trip into Power BI, and the README link that leads people
to it.
"""

from __future__ import annotations

import csv
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUERY_PATH = ROOT / "samples" / "power-bi-query.pq"
SAMPLE_PATH = ROOT / "samples" / "sample-output.csv"
README_PATH = ROOT / "README.md"

QUERY = QUERY_PATH.read_text(encoding="utf-8")

TEXT_COLUMNS = ("Tenant", "Section", "AccountID", "AccountName", "AccountCode")
MONEY_COLUMNS = ("Debit", "Credit", "YTDDebit", "YTDCredit")


def _declared_columns() -> list[str]:
    """The ExpectedColumns list the query fails closed against."""
    block = re.search(r"ExpectedColumns = \{(.*?)\}", QUERY, re.DOTALL)
    assert block is not None, "the query must declare ExpectedColumns"
    return re.findall(r'"([^"]+)"', block.group(1))


def _assigned_types() -> dict[str, str]:
    """Every {"Column", type} pair in the Table.TransformColumnTypes step."""
    return {
        name: assigned.strip()
        for name, assigned in re.findall(r'\{"(\w+)",\s*([^}]+)\}', QUERY)
    }


def _code_only() -> str:
    """The query with its `//` commentary removed.

    The comments talk about tokens and credentials to explain their absence,
    so a scan for those words has to read the code rather than the prose.
    """
    return "\n".join(
        line for line in QUERY.splitlines() if not line.lstrip().startswith("//")
    )


def _sample_header() -> list[str]:
    with SAMPLE_PATH.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle).fieldnames or ())


class PowerBiSampleTests(unittest.TestCase):
    def test_declared_columns_match_the_committed_sample_header(self) -> None:
        """The whole point of the sample: if the exporter's shape changes, this
        query is wrong and must be updated with it."""
        self.assertEqual(_declared_columns(), _sample_header())

    def test_every_column_gets_an_explicit_type(self) -> None:
        self.assertEqual(sorted(_assigned_types()), sorted(_sample_header()))

    def test_the_report_date_is_a_date(self) -> None:
        self.assertEqual(_assigned_types()["ReportDate"], "type date")

    def test_identifiers_and_codes_stay_text(self) -> None:
        """AccountCode is the one that bites: 090 is not 90."""
        types = _assigned_types()
        for column in TEXT_COLUMNS:
            self.assertEqual(types[column], "type text", column)

    def test_money_columns_use_a_fixed_decimal(self) -> None:
        types = _assigned_types()
        for column in MONEY_COLUMNS:
            self.assertEqual(types[column], "Currency.Type", column)

    def test_the_committed_sample_really_has_a_leading_zero_code(self) -> None:
        """Guards the reason AccountCode is text, not just the decision."""
        with SAMPLE_PATH.open(encoding="utf-8-sig", newline="") as handle:
            codes = [row["AccountCode"] for row in csv.DictReader(handle)]
        self.assertTrue(
            any(code.startswith("0") and len(code) > 1 for code in codes),
            "the fabricated sample no longer demonstrates a leading-zero code",
        )

    def test_the_parse_is_wider_than_the_contract(self) -> None:
        """Csv.Document normalises to the column count it is handed, dropping
        extra fields and padding short rows. Asking for exactly ten would
        reshape a malformed file into the expected shape before the header
        check could see it, so the query reads one column wider and treats
        anything in that column as proof the file is too wide."""
        code = _code_only()
        self.assertIn("Columns = List.Count(ExpectedColumns) + 1", code)
        self.assertNotIn("Columns = List.Count(ExpectedColumns),", code)

    def test_each_malformed_shape_has_its_own_refusal(self) -> None:
        code = _code_only()
        for reason in (
            "carries more than the ten columns",
            "does not have the ten columns",
            "does not carry all ten fields",
        ):
            self.assertIn(reason, code)
        self.assertEqual(code.count("error Error.Record("), 3)

    def test_the_typed_step_reads_the_fully_checked_table(self) -> None:
        """Every refusal has to sit upstream of the types, or it is decorative."""
        code = _code_only()
        self.assertIn("Table.TransformColumnTypes(\n        Complete,", code)

    def test_a_blank_trailing_line_is_not_treated_as_a_damaged_row(self) -> None:
        self.assertIn("List.IsEmpty(List.RemoveNulls(Record.FieldValues(_)))", _code_only())

    def test_the_source_path_is_an_unusable_placeholder(self) -> None:
        """A path the reader must replace, not one that quietly half-works."""
        self.assertIn("CHANGE-ME", QUERY)

    def test_the_query_reaches_no_further_than_a_local_file(self) -> None:
        """No Xero login: the sample loads a committed CSV and nothing else."""
        code = _code_only()
        for forbidden in ("Web.Contents", "OData.Feed", "client_id", "client_secret", "token"):
            self.assertNotIn(forbidden, code)
        self.assertIn("File.Contents", code)

    def test_readme_links_the_sample_from_the_power_bi_section(self) -> None:
        readme = README_PATH.read_text(encoding="utf-8")
        link = "[`samples/power-bi-query.pq`](samples/power-bi-query.pq)"
        self.assertIn(link, readme)
        self.assertLess(readme.index("## Power BI"), readme.index(link))
        self.assertLess(readme.index(link), readme.index("## Scheduled runs"))


if __name__ == "__main__":
    unittest.main()
