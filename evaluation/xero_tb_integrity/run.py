from __future__ import annotations

import csv
import os
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

FIXTURE_ROOT = os.path.realpath(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "fixtures"))
)
DECLARED_FIXTURES = tuple(
    os.path.join(FIXTURE_ROOT, name)
    for name in ("passing.csv", "failing_movement.csv", "failing_ytd.csv")
)


def declared_fixture_path(path: str | Path) -> Path:
    """Return a module-owned path for one declared regular fixture."""
    candidate = os.path.realpath(os.path.abspath(path))
    fixture_root_prefix = os.path.join(FIXTURE_ROOT, "")
    if not candidate.startswith(fixture_root_prefix):
        raise ValueError("fixture path must name a declared fabricated CSV fixture")
    for declared in DECLARED_FIXTURES:
        if candidate == declared and os.path.isfile(declared):
            return Path(declared)
    raise ValueError("fixture path must name a declared fabricated CSV fixture")


def totals(path: str | Path) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    path = declared_fixture_path(path)
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
    try:
        path = declared_fixture_path(argv[0])
        fixture_totals = totals(path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    check_balanced(fixture_totals)
    print(f"PASS: {path.name} movement and YTD balance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
