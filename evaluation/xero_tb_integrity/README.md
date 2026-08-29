# Xero trial balance integrity evaluation

## Accounting problem

Xero's Trial Balance endpoint returns current-month movement and year-to-date (YTD) values. Before any CSV write, this repository independently checks both debit and credit pairs, so an imbalance in either pair stops the export.

## Intended reviewer

This pack is for a reviewer who wants to reproduce the offline integrity gate without connecting to Xero or handling client data.

## Fabricated inputs

The three CSVs in `fixtures/` are fabricated output-shape fixtures. They are not Xero API responses and are not client records. No OAuth flow runs in this evaluation.

## Reproduce the result

From the repository root, install the locked dependencies and run the offline pack:

```bash
python -m pip install --require-hashes -r requirements.lock
python -B -m unittest tests.test_evaluation_pack -v
python -B -m unittest discover -s tests -v
```

Dependency installation may download the hash-locked packages. Once dependencies are installed, the three evaluation runner commands are fully offline, make no network request and write no output file:

```bash
python evaluation/xero_tb_integrity/run.py evaluation/xero_tb_integrity/fixtures/passing.csv
python evaluation/xero_tb_integrity/run.py evaluation/xero_tb_integrity/fixtures/failing_movement.csv
python evaluation/xero_tb_integrity/run.py evaluation/xero_tb_integrity/fixtures/failing_ytd.csv
```

They use only the fabricated local fixtures and need no credentials.

## Expected result

[`expected_results.json`](expected_results.json) is the machine-readable contract. `passing.csv` exits 0 and reports that movement and YTD balance. Each failing fixture exits 1, identifies its failed pair and reports that nothing was written.

## Shared conformance corpus

This repository owns the data-only `xero-tb-csv.v1` conformance corpus. The contract records the canonical ten-column order, every fabricated fixture's SHA-256 digest, and whether a conforming consumer must accept or reject it. The corpus files use LF line endings so those byte-level pins are identical on every supported platform. Downstream repositories can vendor these four files at a named commit and verify them without a runtime network dependency.

## Controls triggered

`failing_movement.csv` breaks only the current-month Debit/Credit pair. `failing_ytd.csv` breaks only the YTDDebit/YTDCredit pair. The runner calls the production `check_balanced` gate for each fixture, before any CSV write.

## Primary sources and review date

The contract records Xero's [Accounting API Reports](https://developer.xero.com/documentation/api/accounting/reports) and [OAuth scopes](https://developer.xero.com/documentation/guides/oauth2/scopes/) pages. They were reviewed on 2026-08-26.

## Product and fixture version

`v0.1.4` is the latest published product release. It predates this evaluation directory. This evaluation is protected by the permanent merge-commit link captured after this pull request; the `v0.1.4` tag does not contain this pack. The fabricated fixture version is `1`.

## Human decision

A balanced export passes this integrity control only; a human still decides completeness, classification, accounting treatment and fitness for review.

## Limitations and non-claims

This control does not prove completeness, classification, accounting treatment or client approval. It does not assess source-data accuracy, reporting-period suitability or fitness for a particular client review.
