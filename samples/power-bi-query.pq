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

    // Encoding 65001 is UTF-8. The exporter writes a BOM so Excel decodes
    // non-ASCII account names on a double-click open; Power Query strips it.
    Source = Csv.Document(
        File.Contents(SourcePath),
        [
            Delimiter = ",",
            Columns = List.Count(ExpectedColumns),
            Encoding = 65001,
            QuoteStyle = QuoteStyle.Csv
        ]
    ),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),

    // Fail closed rather than load a half-recognised report. This mirrors the
    // exporter's own balance and column checks: a truncated or reshaped file
    // stops here instead of reaching a refresh pipeline.
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

    // AccountCode stays text on purpose. Xero account codes such as 090 are
    // strings, and a numeric type silently drops the leading zero, which
    // breaks both slicers and any join back to the ledger. AccountID is a
    // GUID, also text.
    //
    // The four money columns use Currency.Type, a fixed decimal, rather than
    // type number: totals of a floating-point type drift, and a trial balance
    // that no longer sums to zero is worse than useless.
    Typed = Table.TransformColumnTypes(
        Checked,
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
