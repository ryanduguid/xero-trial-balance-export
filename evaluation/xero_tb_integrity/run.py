from __future__ import annotations

import csv
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from export_tb import check_balanced

HEADER = [
    "ReportDate",
    "Tenant",
    "Section",
    "AccountID",
    "AccountName",
    "AccountCode",
    "Debit",
    "Credit",
    "YTDDebit",
    "YTDCredit",
]


def totals(path: Path) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != HEADER:
            raise SystemExit("error: fixture does not have the exact exporter header")
        rows = list(reader)
    if not rows:
        raise SystemExit("error: fixture has no account rows")
    return tuple(
        sum((Decimal(row[column]) for row in rows), Decimal("0"))
        for column in ("Debit", "Credit", "YTDDebit", "YTDCredit")
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python evaluation/xero_tb_integrity/run.py FIXTURE.csv", file=sys.stderr)
        return 2
    path = Path(argv[0])
    check_balanced(totals(path))
    print(f"PASS: {path.name} movement and YTD balance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
