"""Deterministic exact-money properties without widening the runtime lock."""

from __future__ import annotations

import unittest
from decimal import Decimal

from export_tb import build_rows, format_amount, to_number


class ExactMoneyProperties(unittest.TestCase):
    def test_bounded_decimal_grid_round_trips_through_csv_formatting(self):
        """Scientific notation or lost digits in either boundary must break this."""
        coefficients = {
            -10**18,
            -1,
            0,
            1,
            10**18,
            *(range(-1_000_000, 1_000_001, 7_919)),
        }
        for scale in (0, 1, 2, 4, 8):
            for coefficient in coefficients:
                amount = Decimal(coefficient).scaleb(-scale)
                self.assertEqual(
                    to_number(format_amount(amount)), amount, f"round trip lost {amount}"
                )

    def test_constructed_balanced_rows_conserve_both_debit_credit_pairs(self):
        """Dropping a row, column or exact amount from build_rows must break this."""
        tenant = {"tenantName": "Fabricated Company"}
        for count in (1, 2, 5, 13, 29):
            for scale in (0, 2, 4):
                amounts = [
                    Decimal(((index * 7_919 + count * 104_729) % 2_000_000) + 1).scaleb(
                        -scale
                    )
                    for index in range(count)
                ]
                expected = sum(amounts, Decimal("0"))
                rows = []
                for index, amount in enumerate(amounts):
                    rows.extend(
                        [
                            {
                                "Section": "Assets",
                                "Account": f"Debit {index} ({1000 + index})",
                                "AccountID": f"debit-{index}",
                                "Debit": str(amount),
                                "Credit": "0",
                                "YTD Debit": str(amount * 3),
                                "YTD Credit": "0",
                            },
                            {
                                "Section": "Liabilities",
                                "Account": f"Credit {index} ({2000 + index})",
                                "AccountID": f"credit-{index}",
                                "Debit": "0",
                                "Credit": str(amount),
                                "YTD Debit": "0",
                                "YTD Credit": str(amount * 3),
                            },
                        ]
                    )

                out_rows, totals = build_rows(rows, tenant, "2026-06-30")

                self.assertEqual(totals, (expected, expected, expected * 3, expected * 3))
                self.assertEqual(
                    sum((to_number(row["Debit"]) for row in out_rows), Decimal("0")),
                    expected,
                )
                self.assertEqual(
                    sum((to_number(row["Credit"]) for row in out_rows), Decimal("0")),
                    expected,
                )
                self.assertEqual(
                    sum((to_number(row["YTDDebit"]) for row in out_rows), Decimal("0")),
                    expected * 3,
                )
                self.assertEqual(
                    sum((to_number(row["YTDCredit"]) for row in out_rows), Decimal("0")),
                    expected * 3,
                )


if __name__ == "__main__":
    unittest.main()
