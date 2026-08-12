# xero-trial-balance-export

[![Verify](https://github.com/ryanduguid/xero-trial-balance-export/actions/workflows/verify.yml/badge.svg)](https://github.com/ryanduguid/xero-trial-balance-export/actions/workflows/verify.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) [![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Pull a trial balance straight from the Xero API into a tidy CSV that Power BI (or pandas, or Excel) loads without cleanup. No SDK, no framework, just three short, readable Python files showing exactly how Xero OAuth2 works, including the part that breaks most scheduled scripts.

## Why

The manual path (Reports → Trial Balance → Export → fix the header rows → fix the account codes) burns 10 minutes per entity per month and produces a slightly different file each time. The API path produces the same tidy shape every run:

```
ReportDate, Tenant, Section, AccountID, AccountName, AccountCode, Debit, Credit, YTDDebit, YTDCredit
```

Column semantics, straight from Xero's report: `Debit`/`Credit` are the **current month's movement** up to the report date; `YTDDebit`/`YTDCredit` are the **cumulative as-at balances**, the pair an accountant means by "the trial balance". Slice year-end numbers on the YTD pair. `AccountID` is the account's stable GUID, the join key that survives code and name changes.

See [`samples/sample-output.csv`](samples/sample-output.csv) for the exact output shape (fabricated entity).

## Setup (once, ~5 minutes)

1. Create an app at [developer.xero.com](https://developer.xero.com/app/manage) → New app → Web app. Redirect URI: `http://localhost:8400/callback`. This script intentionally accepts `localhost` only: it runs a local plain-HTTP callback and does not expose an OAuth listener to your LAN.
2. `python -m pip install --require-hashes -r requirements.lock` (Python 3.10 or newer)
3. Copy `.env.example` to `.env`, fill in the app's client ID and secret
4. `python auth.py`: browser opens, consent, done. Works with Xero's free Demo Company; no paid subscription needed.

## Use

```bash
python export_tb.py --date 2026-06-30
```

Options: `--tenant "name"` (substring match when multiple orgs are connected), `--out relative/path.csv`, `--payments-only` (cash basis). `--out` must be a `.csv` path beneath the process working directory; absolute paths outside the working directory, `..` traversal and paths through an existing symlink that escapes that directory are rejected. A missing parent directory under `--out` is created rather than refused (`--out exports/tb.csv` makes `exports/` if it is not there), so a fetched report is never thrown away for want of a folder. Default filename: `{tenant}-{tenantid8}-tb-{date}-{accrual|cash}.csv`, so the two bases never overwrite each other. The `{tenant}` segment is the org name lowercased, with every run of characters other than **ASCII** letters, digits, `.`, `_` and `-` collapsed to a single `-` and any leading or trailing `-` trimmed; everything outside ASCII is dropped like punctuation: a macron, an accent, Cyrillic, Chinese, an emoji. That transform also folds case and ASCII punctuation, so it can put two different orgs on one name: "Acme (Holdings) Pty Ltd" and "Acme Holdings Pty Ltd" both sanitise to `acme-holdings-pty-ltd`, "ACME Pty Ltd" and "Acme Pty Ltd" both to `acme-pty-ltd`, and two orgs whose names differ only in their Chinese characters both to `pty-ltd`. The first eight characters of the tenant ID are therefore appended to **every** default filename (sanitised the same way, so the default filename is always one path segment), so "Demo Company (AU)" writes `demo-company-au-{tenantid8}-tb-2026-06-30-accrual.csv`. The tenant ID is used because it is the only value Xero guarantees is distinct per organisation, and nothing narrower keeps two clients' trial balances apart. The name is composed to NFC first, so the same org name typed decomposed writes the same file. An org name that sanitises away to nothing leaves the tenant ID as the whole segment. Every default filename **changed** with this rule, so a refresh pointed at an old default path will keep reading a file nothing writes any more. Pin the destination with `--out`.

Every export runs a balance check before anything touches disk. Both pairs must balance (movement **and** YTD), and the expected report columns must all be present; otherwise no file is written and the script exits non-zero, so a truncated or reshaped report can never slip into a refresh pipeline.

The CSV is written as UTF-8 with a BOM (`utf-8-sig`): Excel's double-click open needs the BOM to decode non-ASCII account names correctly, and Power BI and pandas strip it automatically.

## Power BI

Get Data → Text/CSV → point at the export. Columns arrive typed and tidy; `Section` and `AccountCode` are ready for slicers and drill-downs. For a zero-click refresh, set the scheduled task's working directory (Windows **Start in**, or cron's `cd`) to the fixed Power BI data directory, then schedule `export_tb.py` with an explicit `--tenant` and relative `--out`, e.g. from `C:\data`: `python C:\path\to\export_tb.py --tenant "Org Name" --out tb-latest.csv`. The default filename embeds the report date, so a bare scheduled run writes a new file every day while Power BI keeps refreshing the stale one from setup day. Don't run two exports concurrently. They share `token.json`, and overlapping refreshes can burn the token chain. If the destination CSV is locked when the export finishes (Excel or Power BI Desktop holding it open), the run retries briefly, then exits non-zero and leaves the finished export beside it as a `*.csv.tmp`, naming that file in the error. Rename it into place rather than re-running, because the report has already been fetched and a re-run spends another refresh token. A disk that refuses the final flush is handled the same way: once the rows are written the `*.csv.tmp` is complete and balance-checked, so it is kept and named in the error instead of being deleted. Nothing deletes those files, so a scheduled job against a destination that stays locked leaves one per run.

Two Xero platform limits worth knowing: uncertified apps connect to at most 25 organisations (the Demo Company doesn't count), and going past that requires App Partner certification.

## The refresh-token gotcha

Xero refresh tokens are **single-use**: every refresh returns a new refresh token, and the old one stays valid only for a 30-minute grace period. A script that crashes before saving the new refresh token recovers if it reruns within that window, and is locked out if it reruns tomorrow. That's the classic reason "it worked yesterday" Xero scripts die.

[`xero_client.py`](xero_client.py) defends both ways: the new token pair is persisted **before** the access token is first used, and the write is atomic (temp file + `os.replace`), so a crash can't half-write `token.json`. If you still manage to burn the token (e.g. restored an old `token.json` from backup), the script says so plainly and points you back to `auth.py`.

## Files

| File | Purpose |
|---|---|
| [`auth.py`](auth.py) | One-time browser consent → `token.json` |
| [`xero_client.py`](xero_client.py) | Token cache, rotation-safe refresh, authed GET with 429 and 401 retries |
| [`export_tb.py`](export_tb.py) | Fetch report → flatten nested rows → CSV + balance check |

## Scope and disclaimer

Read-only (`accounting.reports.trialbalance.read`, the granular scope new Xero apps require); this tool cannot write to any ledger. `token.json` and `.env` are gitignored. They are credentials, so treat them like passwords. Both sit in the clone directory and inherit its Windows permissions, so keep the clone inside your own user profile; a clone under a shared path like `C:\Tools` hands the live refresh token and client secret to every local user. On a shared machine, strip the inherited access on the clone directory itself before scheduling anything, with `icacls <clone-dir> /inheritance:r /grant:r <your-username>:(OI)(CI)F`, because every token refresh rewrites `token.json` as a new file that re-inherits the directory's permissions, so hardening the file alone lasts until the next run. MIT-licensed utility code, no warranty; outputs feed professional review like any other workpaper input. Not affiliated with or endorsed by Xero.

## Tests

With the dependencies installed, run the offline regression suite from the
repository root:

```bash
python -B -m unittest discover -s tests -v
```

## Related

[`accounting-excel-toolkit`](https://github.com/ryanduguid/accounting-excel-toolkit): Power Query parsers for the manual-export path, when API access isn't on the table.

## Author

Ryan Duguid, accountant in Newcastle NSW, CA ANZ Provisional Member.
