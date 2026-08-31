"""Render the README proof from the fabricated sample trial balance."""

from __future__ import annotations

import argparse
import csv
import html
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "sample-output.csv"
SVG = ROOT / "assets" / "quick-proof.svg"
TRANSCRIPT = ROOT / "assets" / "quick-proof.md"


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _summary() -> dict[str, str | int]:
    with SAMPLE.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("the fabricated sample has no account rows")

    totals = {
        column: sum((Decimal(row[column]) for row in rows), Decimal())
        for column in ("Debit", "Credit", "YTDDebit", "YTDCredit")
    }
    if totals["Debit"] != totals["Credit"]:
        raise ValueError("the fabricated movement columns do not balance")
    if totals["YTDDebit"] != totals["YTDCredit"]:
        raise ValueError("the fabricated YTD columns do not balance")
    return {
        "tenant": rows[0]["Tenant"],
        "date": rows[0]["ReportDate"],
        "rows": len(rows),
        "movement": _money(totals["Debit"]),
        "ytd": _money(totals["YTDDebit"]),
    }


def render_transcript(summary: dict[str, str | int]) -> str:
    return f"""# Validated fabricated trial balance

Source: [`samples/sample-output.csv`](../samples/sample-output.csv)

- Tenant: {summary['tenant']}
- Report date: {summary['date']}
- Shape: {summary['rows']} account rows, 10 columns
- Movement: debit {summary['movement']} | credit {summary['movement']} | balanced
- YTD: debit {summary['ytd']} | credit {summary['ytd']} | balanced

Reproduce the card and verify that both committed assets are current:

```bash
python tools/render_quick_proof.py --check
```

The source is fabricated. No Xero credentials or organisation data are used.
"""


def render_svg(summary: dict[str, str | int]) -> str:
    tenant = html.escape(str(summary["tenant"]))
    date = html.escape(str(summary["date"]))
    movement = html.escape(str(summary["movement"]))
    ytd = html.escape(str(summary["ytd"]))
    rows = summary["rows"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="420" viewBox="0 0 1000 420" role="img" aria-labelledby="title desc">
  <title id="title">Validated fabricated Xero trial balance</title>
  <desc id="desc">A fabricated ten-row trial balance with balanced movement and year-to-date totals.</desc>
  <rect width="1000" height="420" rx="20" fill="#07051a"/>
  <rect x="34" y="34" width="932" height="352" rx="14" fill="#100d29" stroke="#6155a6"/>
  <text x="68" y="84" fill="#f4f1ff" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="28" font-weight="700">xero-trial-balance-export</text>
  <text x="68" y="116" fill="#9e96c8" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="15">FABRICATED OFFLINE PROOF  |  {date}</text>
  <line x1="68" y1="142" x2="932" y2="142" stroke="#38305f"/>
  <text x="68" y="184" fill="#b9b3d8" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18">tenant</text>
  <text x="260" y="184" fill="#ffffff" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18">{tenant}</text>
  <text x="68" y="224" fill="#b9b3d8" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18">shape</text>
  <text x="260" y="224" fill="#ffffff" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18">{rows} account rows  |  10 columns</text>
  <text x="68" y="264" fill="#b9b3d8" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18">movement</text>
  <text x="260" y="264" fill="#ffffff" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18">DR {movement}  |  CR {movement}</text>
  <text x="68" y="304" fill="#b9b3d8" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18">year to date</text>
  <text x="260" y="304" fill="#ffffff" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18">DR {ytd}  |  CR {ytd}</text>
  <rect x="742" y="238" width="168" height="58" rx="29" fill="#183d35" stroke="#54d3ae"/>
  <text x="826" y="274" text-anchor="middle" fill="#8ff0d2" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18" font-weight="700">BALANCED</text>
  <line x1="68" y1="330" x2="932" y2="330" stroke="#38305f"/>
  <text x="68" y="360" fill="#9e96c8" font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="15">python tools/render_quick_proof.py --check</text>
</svg>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    summary = _summary()
    expected = {
        SVG: render_svg(summary),
        TRANSCRIPT: render_transcript(summary),
    }

    if args.check:
        stale = [path for path, text in expected.items() if not path.is_file() or path.read_text(encoding="utf-8") != text]
        if stale:
            for path in stale:
                print(f"stale: {path.relative_to(ROOT)}")
            return 1
        print("quick proof is current")
        return 0

    for path, text in expected.items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
