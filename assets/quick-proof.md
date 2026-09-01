# Validated fabricated trial balance

Source: [`samples/sample-output.csv`](../samples/sample-output.csv)

- Tenant: Catherby Fisheries Pty Ltd
- Report date: 2026-06-30
- Shape: 10 account rows, 10 columns
- Movement: debit $5,700.00 | credit $5,700.00 | balanced
- YTD: debit $126,334.50 | credit $126,334.50 | balanced

Reproduce the card and verify that both committed assets are current:

```bash
python tools/render_quick_proof.py --check
```

The source is fabricated. No Xero credentials or organisation data are used.
