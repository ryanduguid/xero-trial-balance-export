"""Export a Xero Trial Balance to a tidy, Power BI-ready CSV.

Usage:
    python export_tb.py --date 2026-06-30
    python export_tb.py --date 2026-06-30 --tenant "Demo Company" --out tb.csv

Output: one row per account —
    ReportDate, Tenant, Section, AccountID, AccountName, AccountCode,
    Debit, Credit, YTDDebit, YTDCredit

Column semantics per Xero's report: Debit/Credit are the CURRENT MONTH's
movement up to the report date; YTDDebit/YTDCredit are the cumulative as-at
balances (the pair an accountant means by "trial balance"). AccountID is the
account GUID — the stable join key.

The balance check covers both pairs (movement and YTD) and runs before
anything is written; an unbalanced report (truncated or misparsed) writes
no file and exits non-zero.
"""

import argparse
import csv
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

import requests
from dotenv import load_dotenv

from xero_client import REPLACE_ATTEMPTS, REPLACE_BACKOFF, api_get, get_connections

REPORT_URL = "https://api.xero.com/api.xro/2.0/Reports/TrialBalance"

# "Business Bank Account (090)" -> name + code
ACCOUNT_PATTERN = re.compile(r"^(?P<name>.*?)\s*\((?P<code>[^()]+)\)\s*$")


def is_account_code(value: str) -> bool:
    """A code is alphanumeric, at most 10 characters, with at least one digit
    ("090", "GST1") — the same test the sibling repo's Power Query parser
    applies. Anything else belongs to the name: "Term Deposit (NAB)" and
    "Rent (Sydney)" keep their parenthetical and export an empty code, which
    is what Xero's code-less bank and credit-card accounts carry anyway.
    """
    return value.isalnum() and len(value) <= 10 and any(ch.isdigit() for ch in value)


def flatten_report(report: dict) -> tuple[list[str], list[dict]]:
    """Walk the nested Rows structure into flat account rows.

    Xero reports arrive as: Rows[] where RowType is Header (column titles),
    Section (Title + nested Rows), or Row/SummaryRow. Cell order follows the
    Header titles. SummaryRow (section totals) is skipped — totals are
    recomputed, not trusted.

    Every nested list is checked before it is walked, and so is every scalar
    taken out of one. main() proves the Reports envelope is a list of
    objects, and the strict zip below proves a row's cell count; without
    these checks everything between those two was unguarded, so a Rows, Cells
    or Attributes value that was a string, a mapping or a list of strings
    called .get() on a str and printed a raw AttributeError traceback — after
    the tenant name had gone to stdout and the single-use refresh token
    behind the report call had been spent. cell_text() below draws the same
    line around a cell's Value.
    """
    column_titles: list[str] = []
    flat: list[dict] = []

    def object_list(container: dict, key: str, where: str) -> list[dict]:
        """The nested list of objects the report format promises, or an exit."""
        value = container.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise SystemExit(
                f"error: report {where} has a {key} value that is not a list "
                "of objects. The API shape may have changed, or this is not a "
                "trial balance report."
            )
        return value

    def cell_text(value: object, where: str) -> str:
        """A cell's Value as the text every caller already assumes it is.

        object_list proves the containers; this proves the scalar inside
        one. A missing Value and a JSON null are the blank cell the report
        format uses for a nil balance, and a JSON number is an amount
        written another way — to_number has always coerced one with str().
        Anything else is a shape change, and it used to arrive as a raw
        traceback rather than as an instruction: ACCOUNT_PATTERN.match on a
        mapping raised TypeError, and a Header cell holding a list raised
        "unhashable type" at record[title]. Both landed after the tenant name
        had gone to stdout and the single-use refresh token behind the report
        call had been spent, which is the failure mode every guard in this
        function exists to prevent.
        """
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise SystemExit(
            f"error: report {where} has a cell Value that is not text or a "
            f"number ({type(value).__name__}). The API shape may have "
            "changed, or this is not a trial balance report."
        )

    def cell_values(row: dict, where: str) -> list[str]:
        return [
            cell_text(c.get("Value", ""), where)
            for c in object_list(row, "Cells", where)
        ]

    def account_id(row: dict, where: str) -> str:
        # Every data cell carries Attributes: [{"Value": "<account guid>",
        # "Id": "account"}] — the stable join key; codes and names change.
        cells = object_list(row, "Cells", where)
        if not cells:
            return ""
        for attr in object_list(cells[0], "Attributes", where):
            if attr.get("Id") == "account":
                # Same rule as a cell Value: this one reaches the CSV as the
                # join key the README sells, so a mapping here would be
                # written out as its repr.
                return cell_text(attr.get("Value", ""), where)
        return ""

    for top in object_list(report, "Rows", "top level"):
        row_type = top.get("RowType")
        if row_type == "Header":
            column_titles = cell_values(top, "top level")
        elif row_type == "Section":
            section = top.get("Title", "")
            where = f'section "{_shown(section)}"'
            for row in object_list(top, "Rows", where):
                if row.get("RowType") != "Row":
                    continue  # skip SummaryRow
                values = cell_values(row, where)
                record = {}
                # strict: a row/header length mismatch means the API shape
                # changed — fail loudly instead of exporting silent zeros.
                # The bare ValueError read as a crash: it landed after the
                # tenant name had gone to stdout, so it is reported here the
                # way every other API-shape guard in this file reports.
                try:
                    for title, value in zip(column_titles, values, strict=True):
                        record[title] = value
                except ValueError:
                    raise SystemExit(
                        f'error: report section "{_shown(section)}" has a row of '
                        f"{len(values)} cells under {len(column_titles)} header "
                        "columns. The API shape may have changed, or this is not "
                        "a trial balance report."
                    ) from None
                # synthetic keys set last so they win any header collision
                record["Section"] = section
                record["AccountID"] = account_id(row, where)
                flat.append(record)

    return column_titles, flat


# A balance of 1e30 is eighteen orders of magnitude past the largest company
# on earth. The bound exists to keep arithmetic inside Decimal's context and
# the CSV inside one line, not to police the ledger.
MAX_EXPONENT = 30


def _shown(text: object) -> str:
    """The cell as the API sent it, safe for a terminal.

    Escape sequences and newlines never reach it verbatim, same rule auth.py
    applies to the OAuth error code.

    Takes object, not str, and coerces: this runs on values straight off the
    API, and every caller is either building an error message or labelling
    one. A non-string Title used to crash here with a raw traceback, which is
    the failure mode the callers exist to prevent.
    """
    return "".join(ch for ch in str(text)[:40] if ch.isprintable())


def to_number(value: str) -> Decimal:
    """Parse a report cell into an exact Decimal.

    Money never goes through float here. float("0.1") + float("0.2") is not
    0.3, and the error compounds across every row of a trial balance, so a
    report that does not balance can total to zero and one that does can
    total to something else. Decimal holds the digits Xero sent.
    """
    if value is None:
        return Decimal("0")
    text = str(value).strip()
    if text == "":
        return Decimal("0")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        amount = None
    # Finite is not the same as usable. Decimal's default context stops at
    # Emax 999999, so a cell of "1E1000000" parses and is finite, then the
    # first total_debit += debit raises decimal.Overflow - an ArithmeticError,
    # which run() does not catch (it handles transport failures only), so it
    # would print a traceback after the tenant name had already gone to
    # stdout. A shade under that, "1E999999"
    # does not overflow but makes format_amount build a one-million-character
    # CSV field. MAX_EXPONENT is eighteen orders of magnitude past the largest
    # balance sheet on earth, so nothing real is refused here. The mirror
    # bound refuses vanishing exponents ("1E-31") for the same reason: no
    # ledger holds them, and format_amount would build the same absurd field.
    if amount is not None and amount.is_finite():
        if amount.adjusted() > MAX_EXPONENT:
            raise SystemExit(
                f'error: report cell "{_shown(text)}" is {amount.adjusted() + 1} digits '
                "long, which is not a ledger balance. The API shape may have changed, "
                "or this is not a trial balance report."
            )
        if amount.adjusted() < -MAX_EXPONENT:
            raise SystemExit(
                f'error: report cell "{_shown(text)}" is smaller than any ledger '
                "balance. The API shape may have changed, or this is not a trial "
                "balance report."
            )
    # Decimal("NaN") and Decimal("Infinity") parse, and a NaN compares
    # unequal to everything including itself, so it would reach the balance
    # check and the CSV as a number. Neither is an amount.
    if amount is None or not amount.is_finite():
        raise SystemExit(
            f'error: report cell "{_shown(text)}" is not an amount. The API shape '
            "may have changed, or this is not a trial balance report."
        )
    return amount


def format_amount(value: Decimal) -> str:
    """Render an amount the way the CSV has always carried it.

    Trailing zeros go, one decimal place always stays: 1200.00 -> "1200.0",
    15234.50 -> "15234.5". That is what str() of the old float gave, so
    existing downstream models keep parsing the same text. Note this is
    formatting only; the arithmetic above it stays exact.
    """
    text = format(value, "f")  # never scientific notation
    if "." not in text:
        return text + ".0"
    text = text.rstrip("0")
    return text + "0" if text.endswith(".") else text


def iso_date(value: str) -> str:
    """argparse type= for --date: reject anything but YYYY-MM-DD up front,
    before an AU-habit 30/06/2026 wastes the API call and then breaks the
    output filename with slashes."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f'"{value}" is not YYYY-MM-DD (e.g. 2026-06-30). Note the order: '
            "ISO year-month-day, not the AU DD/MM/YYYY."
        ) from None


def excel_safe(value: str) -> str:
    """CSV formula-injection guard, covering OWASP's trigger set: = + - @
    plus tab, CR and LF. Xero account and organisation names are free text
    anyone in the org can edit. A leading apostrophe forces Excel to read
    the cell as text; everything else passes through untouched."""
    return "'" + value if str(value)[:1] in ("=", "+", "-", "@", "\t", "\r", "\n") else value


def validated_connections(value: object) -> list[dict]:
    """Validate the remote tenant list before it reaches paths, CSV, or output."""
    if not isinstance(value, list):
        raise SystemExit("error: Xero connections response is not a list.")
    result = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"error: Xero connection {index} is not an object.")
        for field in ("tenantId", "tenantName"):
            text = item.get(field)
            if not isinstance(text, str) or not text.strip() or any(ord(char) < 32 or ord(char) == 127 for char in text):
                raise SystemExit(f"error: Xero connection {index} has an invalid {field}.")
        result.append(item)
    return result


# Every run of characters a filename segment may not carry, collapsed to one
# "-". Used for the org name and for the tenant ID alike - both are remote
# input, and validated_connections only refuses control characters.
FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def default_output_filename(tenant: dict, report_date: str, basis: str) -> str:
    """{entity}-{tenant-id}-{report}-{period-end}-{basis}: matches the file
    convention in the sibling repos, and keeps cash vs accrual runs from
    overwriting each other.

    The entity segment is the org name lowercased with every run of
    characters outside ASCII letters, digits, ".", "_" and "-" collapsed to
    one "-". That transform loses information in more ways than one, and
    every one of them can put two different orgs on the same filename, where
    the second scheduled export silently overwrites the first client's trial
    balance:

      * everything outside ASCII is dropped, so two names differing only in
        their Chinese characters and both ending "Pty Ltd" become "pty-ltd";
      * ASCII punctuation collapses to the same "-" a space does, so "Acme
        (Holdings) Pty Ltd" and "Acme Holdings Pty Ltd" become
        "acme-holdings-pty-ltd";
      * case is folded, so "ACME Pty Ltd" and "Acme Pty Ltd" agree too.

    So the first eight characters of the tenant ID are appended to every
    default filename, not only to the ones that lose a character outside
    ASCII. That earlier rule was the whole defect: it read the first bullet
    as the whole problem and left the other two producing exactly the harm
    it was written to stop. The tenant ID is the only value here that Xero
    guarantees is distinct per organisation, so it is the only thing that can
    keep two orgs apart. It is sanitised like the stem because it is remote
    input as well: unsanitised, a "/" or "\\" in it turns the default
    filename into a path, and main() creates the output directory before
    writing - so the export would silently land in a directory tree nobody
    asked for instead of failing. A name that sanitises away to nothing
    leaves the tenant ID as the whole segment.

    The name is composed to NFC first. "Nga" plus a combining macron and the
    composed "Ngā" are one org name typed two ways, and without this they
    sanitise to two different stems - so the same org would write two
    different files depending on which form the API happened to send.
    """
    name = unicodedata.normalize("NFC", tenant["tenantName"])
    stem = FILENAME_UNSAFE.sub("-", name).strip("-").lower()
    discriminator = FILENAME_UNSAFE.sub("-", tenant["tenantId"])[:8].strip("-")
    entity = "-".join(part for part in (stem, discriminator) if part)
    return f"{entity}-tb-{report_date}-{basis}.csv"


def output_path(value: str | None, default_filename: str, *, root: str | None = None) -> str:
    """Resolve a CSV output path beneath a controlled output root.

    ``--out`` is intentionally useful for scheduled jobs, but it must not let
    command-line input redirect a run to an arbitrary file. Normalize with
    ``realpath`` before checking containment so both ``..`` components and
    existing symlinked directories are unable to escape the working directory.
    A caller that needs a different destination should set its process working
    directory (for example, a Task Scheduler "Start in" value) deliberately.
    """
    output_root = os.path.realpath(root or os.getcwd())
    requested = default_filename if value is None else value
    candidate = os.path.realpath(os.path.join(output_root, requested))

    if os.path.splitext(candidate)[1].lower() != ".csv":
        raise ValueError("--out must name a .csv file")

    # Keep the trailing separator: /exports-old must not be treated as a child
    # of /exports. This is a prefix check only after realpath has normalized
    # traversal and resolved existing links.
    output_root_prefix = os.path.join(output_root, "")
    if candidate.startswith(output_root_prefix):
        return candidate

    raise ValueError("--out must be a relative path beneath the current working directory")


def main() -> None:
    # Non-console stdout on Windows is cp1252, not UTF-8 (PEP 528) — a macron
    # or CJK character in an org name must not abort a redirected or piped run
    # before the report is ever fetched.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    parser = argparse.ArgumentParser(description="Export a Xero Trial Balance to CSV.")
    parser.add_argument("--date", type=iso_date, default=date.today().isoformat(), help="Report date YYYY-MM-DD")
    parser.add_argument("--tenant", default=None, help="Tenant name substring (required when multiple orgs are connected)")
    parser.add_argument(
        "--out",
        default=None,
        help="Output CSV path relative to the current working directory",
    )
    parser.add_argument("--payments-only", action="store_true", help="Cash-basis report")
    args = parser.parse_args()

    # Reject unsafe explicit destinations before looking up credentials or
    # calling Xero. The default filename needs tenant metadata and is resolved
    # later; for a supplied value the fallback is never used.
    if args.out is not None:
        try:
            output_path(args.out, "unused.csv")
        except ValueError as exc:
            parser.error(str(exc))

    load_dotenv()
    client_id = os.environ.get("XERO_CLIENT_ID")
    client_secret = os.environ.get("XERO_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set XERO_CLIENT_ID and XERO_CLIENT_SECRET in .env (see .env.example).")

    # api_get looks the access token up fresh per call — no token is held
    # here, so a mid-run refresh can never leave a later call using the
    # stale one; the creds also cover the surprise-401 forced refresh
    creds = (client_id, client_secret)

    connections = validated_connections(get_connections(creds))
    if not connections:
        sys.exit("No Xero organisations authorised for this app - run auth.py again.")
    if args.tenant:
        matches = [c for c in connections if args.tenant.lower() in c["tenantName"].lower()]
        if not matches:
            names = ", ".join(c["tenantName"] for c in connections)
            sys.exit(f'No tenant matching "{args.tenant}". Connected: {names}')
        if len(matches) > 1:
            names = ", ".join(c["tenantName"] for c in matches)
            sys.exit(f'"{args.tenant}" matches more than one organisation ({names}) - narrow it.')
        tenant = matches[0]
    else:
        if len(connections) > 1:
            names = ", ".join(c["tenantName"] for c in connections)
            sys.exit(f"More than one organisation connected ({names}) - pick one with --tenant.")
        tenant = connections[0]
    print(f"Tenant: {tenant['tenantName']}")

    params = {"date": args.date}
    if args.payments_only:
        params["paymentsOnly"] = "true"

    payload = api_get(REPORT_URL, creds, tenant_id=tenant["tenantId"], params=params)
    if not isinstance(payload, dict):
        sys.exit("error: Xero Trial Balance response is not a JSON object.")
    reports = payload.get("Reports", [])
    if not reports:
        sys.exit("Empty Reports payload - check the date parameter and API scopes.")
    if not isinstance(reports, list) or not isinstance(reports[0], dict):
        sys.exit("error: Xero Trial Balance response has an unexpected Reports shape.")

    column_titles, rows = flatten_report(reports[0])
    if not rows:
        sys.exit("Report contained no account rows - nothing to export.")

    # The strict zip in flatten_report only catches a cell-COUNT change; a
    # retitled column (count unchanged) would slip through and silently zero
    # every value via record.get(). Guard the titles themselves.
    missing = {"Account", "Debit", "Credit", "YTD Debit", "YTD Credit"} - set(column_titles)
    if missing:
        sys.exit(f"Unexpected report columns - missing {sorted(missing)}. Has the API shape changed?")

    basis = "cash" if args.payments_only else "accrual"
    try:
        out_path = output_path(args.out, default_output_filename(tenant, args.date, basis))
    except ValueError as exc:
        parser.error(str(exc))

    fieldnames = [
        "ReportDate", "Tenant", "Section", "AccountID", "AccountName", "AccountCode",
        "Debit", "Credit", "YTDDebit", "YTDCredit",
    ]

    # Build everything in memory and balance-check BEFORE any file exists —
    # a scheduled Power BI refresh reads the path, not the exit code, so an
    # unbalanced export must never reach disk.
    out_rows = []
    total_debit = total_credit = Decimal("0")
    total_ytd_debit = total_ytd_credit = Decimal("0")
    for record in rows:
        account_raw = record.get("Account", "")
        match = ACCOUNT_PATTERN.match(account_raw)
        if match and is_account_code(match.group("code")):
            name, code = match.group("name"), match.group("code")
        else:
            name, code = account_raw, ""

        debit = to_number(record.get("Debit"))
        credit = to_number(record.get("Credit"))
        ytd_debit = to_number(record.get("YTD Debit"))
        ytd_credit = to_number(record.get("YTD Credit"))
        # Decimal construction is exact but arithmetic rounds at the context
        # precision (28 by default). to_number admits cells up to 33
        # significant digits (MAX_EXPONENT plus cents), so default-context
        # totals could silently drop a final cent and pass a report that
        # does not balance. 50 digits covers the admitted bound plus
        # accumulation headroom.
        with localcontext() as exact:
            exact.prec = 50
            total_debit += debit
            total_credit += credit
            total_ytd_debit += ytd_debit
            total_ytd_credit += ytd_credit

        out_rows.append(
            {
                "ReportDate": args.date,
                "Tenant": excel_safe(tenant["tenantName"]),
                "Section": excel_safe(record.get("Section", "")),
                "AccountID": record.get("AccountID", ""),
                "AccountName": excel_safe(name),
                "AccountCode": excel_safe(code),
                "Debit": format_amount(debit),
                "Credit": format_amount(credit),
                "YTDDebit": format_amount(ytd_debit),
                "YTDCredit": format_amount(ytd_credit),
            }
        )

    # Both pairs must balance — the movement columns AND the YTD as-at
    # balances (the pair the README tells users to slice). Either one out
    # means the report is truncated or misparsed.
    #
    # The comparison is exact. The old round(diff, 2) existed to absorb float
    # noise, and it also swallowed real differences under half a cent; with
    # Decimal totals there is no noise to absorb, so any difference at all is
    # a difference Xero did not send.
    unbalanced = False
    for label, debits, credits in (
        ("movement", total_debit, total_credit),
        ("YTD", total_ytd_debit, total_ytd_credit),
    ):
        with localcontext() as exact:
            exact.prec = 50
            diff = debits - credits
        if diff != 0:
            print(
                f"WARNING: {label} debits {debits:,.2f} != credits "
                f"{credits:,.2f} (diff {format_amount(diff)})"
            )
            unbalanced = True
    if unbalanced:
        print("Nothing written - report likely truncated or misparsed.")
        sys.exit(1)

    # Atomic write (temp file + replace), mirroring save_tokens(): a crash
    # or disk-full mid-write must never leave a truncated CSV at the path a
    # scheduled Power BI refresh reads.
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    # output_path accepts a nested relative --out ("exports/tb.csv") whose
    # parent need not exist yet, and mkstemp below raised FileNotFoundError
    # for it - after the report had been fetched and this run's single-use
    # refresh token spent.
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        sys.exit(f"error: cannot create the output directory {out_dir} ({exc}).")
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=".csv.tmp")
    written = False
    try:
        # utf-8-sig: the BOM is what makes Excel's double-click open decode
        # non-ASCII names correctly; Power BI and pandas strip it anyway.
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(out_rows)
            fh.flush()
            # The flush is what makes the file whole, which is all "written"
            # claims - the same line save_tokens draws. Past it the temp file
            # holds the complete, balance-checked export and the API call that
            # produced it cannot be replayed for free. It is worth more than
            # the stale CSV at out_path, so the finally clause below must
            # never delete it.
            written = True
            # Flushing only reaches the OS page cache; NTFS journals the
            # rename's metadata, not the data behind it. Force the bytes down
            # before os.replace destroys the previous export. A failure here
            # is a disk that would not take the write, not a half-built file:
            # setting written above and exiting with the temp path named keeps
            # the export recoverable instead of unlinking it and printing a
            # bare OSError traceback, which run() does not catch.
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                raise SystemExit(
                    f"error: wrote the balanced export to {tmp_path} but could "
                    f"not flush it to disk ({exc}). The file is complete and "
                    f"balance-checked: rename {tmp_path} over {out_path} once "
                    "the disk is writable - the report has been fetched "
                    "already and re-running spends another refresh token."
                ) from None
    finally:
        if not written and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # os.replace onto the destination fails on Windows while Excel or Power BI
    # Desktop holds it open, which is exactly the README's scheduled-refresh
    # recipe. save_tokens rides out the same lock; without the retry here the
    # finally clause above deleted a good export and left the stale file in
    # place. On a permanent failure the temp file survives and is named, so
    # the export can be moved into place by hand.
    last_error: OSError | None = None
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_path, out_path)
            break
        except OSError as exc:
            last_error = exc
            if attempt < REPLACE_ATTEMPTS - 1:
                time.sleep(REPLACE_BACKOFF * (attempt + 1))
    else:
        raise SystemExit(
            f"error: wrote the balanced export to {tmp_path} but could not move "
            f"it onto {out_path} after {REPLACE_ATTEMPTS} attempts "
            f"({last_error}). Close whatever holds {out_path} open (Excel or "
            f"Power BI Desktop keep a lock on it), then rename {tmp_path} over "
            "it - the report has been fetched already and re-running spends "
            "another refresh token."
        )

    print(f"Wrote {len(out_rows)} accounts to {out_path}")
    print(f"Balance check OK: movement debits = credits = {total_debit:,.2f}; YTD = {total_ytd_debit:,.2f}")


def run() -> None:
    """Command-line entry point.

    Every failure this script raises itself is a SystemExit carrying an
    instruction. A transport failure is not: with the machine offline or DNS
    down, requests raises ConnectionError and a scheduled export ended with a
    stack trace in the Task Scheduler log. Turn it into the same one-line
    exit as everything else. Only the transport is caught here - a bug in
    this script must still surface as a traceback.

    The caught set is ConnectionError (which SSLError and the proxy errors
    subclass) and Timeout, not their common base RequestException:
    HTTPError is a RequestException too, and resp.raise_for_status() raises
    it for every status xero_client does not answer itself. Catching the base
    class told the operator of a 403 for a missing
    accounting.reports.trialbalance.read scope, or a 400 for a bad --date,
    to "re-run later" - advice that can never work, because the next run
    fails the same way. Those keep the traceback they had before this
    handler existed, which names the status and the URL.
    """
    try:
        main()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        sys.exit(f"error: could not reach Xero ({exc}) - re-run later.")


if __name__ == "__main__":
    run()
