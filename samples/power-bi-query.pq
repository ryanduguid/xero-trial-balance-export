// Load a xero-trial-balance-export CSV into Power BI with the exported schema
// intact.
//
// To use it: Power BI Desktop, Home > Transform data > New Source > Blank
// Query, then Home > Advanced Editor, and replace the whole window with this.
//
// Nothing here contacts Xero. There is no client id, no token and no ledger
// access in this query, so pointing it at the committed fabricated sample
// exercises the entire load path with no Xero connection at all.

let
    // SET THIS before running. Point it at your own export, or at the
    // fabricated sample committed in this repository to try it offline:
    // "C:\path\to\xero-trial-balance-export\samples\sample-output.csv".
    SourcePath = "C:\CHANGE-ME\tb-latest.csv",

    // The exporter's column contract, in order. export_tb.py refuses to write
    // a reshaped report, so a mismatch here means the file did not come from
    // this tool and should not be loaded blind.
    ExpectedColumns = {
        "ReportDate",
        "Tenant",
        "Section",
        "AccountID",
        "AccountName",
        "AccountCode",
        "Debit",
        "Credit",
        "YTDDebit",
        "YTDCredit"
    },

    // Read one column wider than the contract, on purpose.
    //
    // Csv.Document normalises to whatever column count it is given: it drops
    // fields beyond that count and pads shorter rows with null. Asking for
    // exactly ten would therefore reshape a malformed file into the expected
    // shape before any check could see it, and an eleven-column export would
    // sail through the header comparison below. Reading eleven leaves the
    // evidence intact: in a well-formed file the eleventh column is null in
    // every row, so any value there proves the file is wider than the ten
    // columns export_tb.py writes.
    //
    // Encoding 65001 is UTF-8. The exporter writes a BOM so Excel decodes
    // non-ASCII account names on a double-click open; Power Query strips it.
    OverflowColumn = "Column" & Text.From(List.Count(ExpectedColumns) + 1),
    Probe = Csv.Document(
        File.Contents(SourcePath),
        [
            Delimiter = ",",
            Columns = List.Count(ExpectedColumns) + 1,
            Encoding = 65001,
            QuoteStyle = QuoteStyle.Csv
        ]
    ),
    Overflow = List.RemoveNulls(Table.Column(Probe, OverflowColumn)),
    Narrowed =
        if List.IsEmpty(Overflow) then
            Table.RemoveColumns(Probe, {OverflowColumn})
        else
            error Error.Record(
                "Unexpected trial balance schema",
                "This file carries more than the ten columns export_tb.py writes.",
                "First value found past column "
                    & Text.From(List.Count(ExpectedColumns))
                    & ": "
                    & Text.From(List.First(Overflow))
            ),
    Promoted = Table.PromoteHeaders(Narrowed, [PromoteAllScalars = true]),

    // Fail closed rather than load a half-recognised report. This mirrors the
    // exporter's own balance and column checks: a reshaped file stops here
    // instead of reaching a refresh pipeline.
    Checked =
        if Table.ColumnNames(Promoted) = ExpectedColumns then
            Promoted
        else
            error Error.Record(
                "Unexpected trial balance schema",
                "This file does not have the ten columns export_tb.py writes.",
                "Expected: "
                    & Text.Combine(ExpectedColumns, ", ")
                    & " | Got: "
                    & Text.Combine(Table.ColumnNames(Promoted), ", ")
            ),

    // A truncated row is the other half of the same problem: Csv.Document
    // padded it with null to reach the column count, so the header check
    // above cannot see it. The exporter writes every column on every row and
    // never leaves one empty, so a null here means a damaged file. Wholly
    // blank lines are ignored rather than rejected, since a trailing newline
    // is not a defect.
    Populated = Table.SelectRows(
        Checked,
        each not List.IsEmpty(List.RemoveNulls(Record.FieldValues(_)))
    ),
    Gaps = Table.SelectRows(Populated, each List.Contains(Record.FieldValues(_), null)),
    Complete =
        if Table.IsEmpty(Gaps) then
            Populated
        else
            error Error.Record(
                "Incomplete trial balance row",
                "A row does not carry all ten fields export_tb.py writes.",
                Text.From(Table.RowCount(Gaps)) & " row(s) are missing at least one field."
            ),

    // AccountCode stays text on purpose. Xero account codes such as 090 are
    // strings, and a numeric type silently drops the leading zero, which
    // breaks both slicers and any join back to the ledger. AccountID is a
    // GUID, also text.
    //
    // The four money columns use Currency.Type, a fixed decimal, rather than
    // type number: totals of a floating-point type drift, and a trial balance
    // that no longer sums to zero is worse than useless.
    Typed = Table.TransformColumnTypes(
        Complete,
        {
            {"ReportDate", type date},
            {"Tenant", type text},
            {"Section", type text},
            {"AccountID", type text},
            {"AccountName", type text},
            {"AccountCode", type text},
            {"Debit", Currency.Type},
            {"Credit", Currency.Type},
            {"YTDDebit", Currency.Type},
            {"YTDCredit", Currency.Type}
        },
        "en-AU"
    )
in
    Typed
