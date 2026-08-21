# xero-trial-balance-export

[![Verify](https://github.com/ryanduguid/xero-trial-balance-export/actions/workflows/verify.yml/badge.svg)](https://github.com/ryanduguid/xero-trial-balance-export/actions/workflows/verify.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) [![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Pull a trial balance straight from the Xero API into a tidy CSV that Power BI (or pandas, or Excel) loads without cleanup. No SDK, no framework, just three readable Python files showing exactly how Xero OAuth2 works, including the part that breaks most scheduled scripts.

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
4. `python auth.py`: browser opens, consent, done. On Windows, `token.json` is protected immediately with current-user DPAPI. Works with Xero's free Demo Company; no paid subscription needed.

## Use

```bash
python export_tb.py --date 2026-06-30
```

Options: `--tenant "name-or-id"` (name substring, or an exact `tenantId` when display names collide), `--out relative/path.csv`, `--payments-only` (cash basis), `--token-file path/to/token.json` (where the token cache lives; the flag beats the `XERO_TOKEN_FILE` environment variable, and the default is `token.json` beside `xero_client.py`). `--out` must be a `.csv` path beneath the process working directory; absolute paths outside the working directory, `..` traversal and paths through an existing symlink that escapes that directory are rejected. A missing parent directory under `--out` is created rather than refused (`--out exports/tb.csv` makes `exports/` if it is not there), so a fetched report is never thrown away for want of a folder. Default filename: `{tenant}-{tenantid8}-tb-{date}-{accrual|cash}.csv`, so the two bases never overwrite each other. The `{tenant}` segment is sanitised for filesystem safety; see the [Filename reference](#filename-reference) appendix for the exact rules and their edge cases.

Every export runs a balance check before anything touches disk. Both pairs must balance (movement **and** YTD), and the expected report columns must all be present; otherwise no file is written and the script exits non-zero, so a truncated or reshaped report can never slip into a refresh pipeline.

The CSV is written as UTF-8 with a BOM (`utf-8-sig`): Excel's double-click open needs the BOM to decode non-ASCII account names correctly, and Power BI and pandas strip it automatically.

## Power BI

1. Get Data → Text/CSV → point at the export. Columns arrive typed and tidy; `Section` and `AccountCode` are ready for slicers and drill-downs.
2. For a zero-click refresh, set the scheduled task's working directory (Windows **Start in**, or cron's `cd`) to the fixed Power BI data directory, then schedule `export_tb.py` with an explicit `--tenant` and relative `--out`, e.g. from `C:\data`: `python C:\path\to\export_tb.py --tenant "Org Name" --out tb-latest.csv`.
3. Run the Windows task in the same Windows user profile that ran `auth.py`: current-user DPAPI is deliberately not a portable cache format, and a non-Windows process cannot decrypt it.
4. Pin the output name with `--out`, as above. The default filename embeds the report date, so a bare scheduled run writes a new file every day while Power BI keeps refreshing the stale one from setup day.

When a run hits a locked destination, a concurrent export, or a disk that refuses the final flush, see the "Power BI failure modes" appendix below.

Two Xero platform limits worth knowing: uncertified apps connect to at most 25 organisations (the Demo Company doesn't count), and going past that requires App Partner certification.

## Scheduled runs

Point the job at a stable token cache first. The cache defaults to `token.json` beside `xero_client.py`, the `XERO_TOKEN_FILE` environment variable overrides that, and an explicit `--token-file` flag overrides both, so a move of the checkout cannot orphan the cache and an operator's flag always wins. The lock file (`<cache>.lock`) always sits beside whichever cache path wins. Run the job as the same user that ran `auth.py` (on Windows this is mandatory: the DPAPI cache only decrypts under that user's profile).

Exit codes: `0` means the export succeeded and the CSV is in place. `1` means the run failed and printed a one-line reason (most failures report on stderr; the balance-check warnings print on stdout, so capture both streams). `2` means a command-line error (a malformed `--date`, an `--out` outside the working directory). Any non-zero exit writes no CSV to the destination, though a locked destination leaves the finished export beside it as a named `*.csv.tmp`.

cron (Linux or macOS), daily at 06:30, with both streams appended to a log:

```cron
30 6 * * * cd /srv/powerbi-data && XERO_TOKEN_FILE=/srv/xero/token.json /usr/bin/python3 /opt/xero-trial-balance-export/export_tb.py --tenant "Org Name" --out tb-latest.csv >> /var/log/xero-export.log 2>&1
```

Windows Task Scheduler: create a task that runs as the Windows user who ran `auth.py`, with "Start in" set to the Power BI data directory. Action program: `cmd.exe`. Arguments:

```
/c ""C:\Python313\python.exe" "C:\tools\xero-trial-balance-export\export_tb.py" --tenant "Org Name" --out tb-latest.csv --token-file "C:\xero\token.json" >> "C:\logs\xero-export.log" 2>&1"
```

Task Scheduler records the exit code as the task's "Last Run Result", so a `1` or `2` there means read the log. The `>>` redirection is what captures the one-line error messages; without it a failed scheduled run leaves nothing to read.

## The refresh-token gotcha

Xero refresh tokens **rotate on use**: every refresh returns a replacement refresh token. If the refresh response does not arrive, Xero permits retrying the previous token for up to a 30-minute grace period; outside that window, the user must re-authorise. The [Xero OAuth FAQ](https://developer.xero.com/faq/oauth2) was checked on 20 August 2026 (`2026-08-20`); recheck it for apps created or used after that date.

[`xero_client.py`](xero_client.py) defends three ways: a cross-process lock covers the cache read, migration, refresh and write; the new token pair is persisted **before** the access token is first used; and the write is atomic (temp file + `os.replace`), so a crash can't half-write `token.json`. On Windows, the complete cache and any fully written recovery temp are DPAPI-protected before bytes reach disk. The first read of a valid older plaintext cache migrates it atomically under the same lock, preserves its original `obtained_at`, and completes before any Xero request. A corrupt cache or unknown envelope version stops without a network call or rewrite. If you still manage to burn the token (e.g. restored an old `token.json` from backup), the script says so plainly and points you back to `auth.py`.

## Files

| File | Purpose |
|---|---|
| [`auth.py`](auth.py) | One-time browser consent → `token.json` |
| [`xero_client.py`](xero_client.py) | Token cache, rotation-safe refresh, authed GET with 429 and 401 retries |
| [`export_tb.py`](export_tb.py) | Fetch report → flatten nested rows → CSV + balance check |

## Scope and disclaimer

Read-only (`accounting.reports.trialbalance.read`); this tool cannot write to any ledger. Web and PKCE apps created on or after 2 March 2026 use granular scopes, while existing apps using the broad `accounting.reports.read` scope must migrate by 13 September 2027. Xero's [OAuth scope list](https://developer.xero.com/documentation/guides/oauth2/scopes/), [Granular Scopes FAQ](https://developer.xero.com/faq/granular-scopes) and [developer changelog](https://developer.xero.com/changelog) were checked on 20 August 2026 (`2026-08-20`); recheck them for apps created or used after that date. `token.json` and `.env` are gitignored. They are credentials, so treat them like passwords.

On Windows, `token.json` uses the operating system's current-user [Data Protection API](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) (`CryptProtectData`/`CryptUnprotectData`) with UI forbidden. It is encrypted at rest and bound to that user's DPAPI security context; there is no repository key and no custom cryptography. Treat the cache as non-portable and re-authorise instead of trying to move it between unrelated accounts or machines. DPAPI does not protect tokens from code already running as that user, an administrator controlling the machine, or a compromised user session. `.env` remains plaintext because it must supply the OAuth client credentials, so keep the clone inside your own user profile. On a shared machine, restrict the clone directory before scheduling anything, for example with `icacls <clone-dir> /inheritance:r /grant:r <your-username>:(OI)(CI)F`.

Python's standard library has no equivalent portable secret store. On non-Windows systems the project therefore retains its existing plaintext JSON cache as an explicit compatibility fallback and forces its mode to owner-read/write only (`0600`) on every save and load. Use a private account and directory, and do not copy a Windows DPAPI envelope to Linux or macOS: it cannot be decrypted there. `token.json.lock` contains only zero-valued lock bytes and no credentials. MIT-licensed utility code, no warranty; outputs feed professional review like any other workpaper input. Not affiliated with or endorsed by Xero.

## Tests

With the dependencies installed, run the offline regression suite from the
repository root:

```bash
python -B -m unittest discover -s tests -v
```

## Power BI failure modes

Concurrent exports using the same checkout serialise their token-cache read, migration, refresh and write through `token.json.lock`; a waiter re-reads the rotated cache instead of spending the same refresh token. The lock coordinates processes using that local cache, not copies of `token.json` on other machines.

If the destination CSV is locked when the export finishes (Excel or Power BI Desktop holding it open), the run retries briefly, then exits non-zero and leaves the finished export beside it as a `*.csv.tmp`, naming that file in the error. Rename it into place rather than re-running, because the report has already been fetched and a re-run spends another refresh token.

A disk that refuses the final flush is handled the same way: once the rows are written the `*.csv.tmp` is complete and balance-checked, so it is kept and named in the error instead of being deleted. Nothing deletes those files, so a scheduled job against a destination that stays locked leaves one per run.

## Filename reference

The `{tenant}` segment of the default filename is the org name lowercased, with every run of characters other than **ASCII** letters, digits, `.`, `_` and `-` collapsed to a single `-` and any leading or trailing `-` trimmed; everything outside ASCII is dropped like punctuation: a macron, an accent, Cyrillic, Chinese, an emoji. That transform also folds case and ASCII punctuation, so it can put two different orgs on one name: "Acme (Holdings) Pty Ltd" and "Acme Holdings Pty Ltd" both sanitise to `acme-holdings-pty-ltd`, "ACME Pty Ltd" and "Acme Pty Ltd" both to `acme-pty-ltd`, and two orgs whose names differ only in their Chinese characters both to `pty-ltd`. The first eight characters of the tenant ID are therefore appended to **every** default filename (sanitised the same way, so the default filename is always one path segment), so "Demo Company (AU)" writes `demo-company-au-{tenantid8}-tb-2026-06-30-accrual.csv`. The tenant ID is used because it is the only value Xero guarantees is distinct per organisation, and nothing narrower keeps two clients' trial balances apart. The name is composed to NFC first, so the same org name typed decomposed writes the same file. An org name that sanitises away to nothing leaves the tenant ID as the whole segment. Every default filename **changed** with this rule, so a refresh pointed at an old default path will keep reading a file nothing writes any more. Pin the destination with `--out`.

## Related

[`accounting-excel-toolkit`](https://github.com/ryanduguid/accounting-excel-toolkit): Power Query parsers for the manual-export path, when API access isn't on the table.

## Author

Ryan Duguid, accountant in Newcastle NSW, CA ANZ Provisional Member.
