# Contributing

This exporter reads a Xero trial balance and writes a validated CSV. It stays read-only against the Xero API: no contribution should give it the ability to post, approve, or modify anything in an organisation.

## Data boundary

- Never commit credentials. The `.gitignore` blocks `.env*` except `.env.example`, plus `token.json`, its credential-free lock file, `*.json`, and the common export extensions. Keep `.env.example` to placeholder values.
- Keep exported trial balances, account lists and tenant identifiers from a real organisation out of the repository. Sample data belongs in `samples/`.
- On Windows, token-cache changes must preserve current-user DPAPI protection, the versioned envelope, UI-forbidden operation and atomic plaintext migration before any network call. Do not add a repository key or custom cryptography. On non-Windows systems the documented compatibility fallback stays plaintext and must remain mode `0600` where POSIX permissions are supported.

## Correctness rules worth preserving

- The export refuses to write when debits do not equal credits. A scheduled refresh reads the output path and ignores the exit code, so writing a wrong file causes more damage than writing nothing.
- Refresh tokens rotate and Xero accepts each one once. Validate the usable pair, then persist its DPAPI envelope on Windows (or the mode-`0600` fallback elsewhere) before using the new access token. Do not throw away a working rotated token over cosmetic response data or write plaintext token bytes to a Windows recovery temp.
- The exporter refuses a bare multi-organisation run on purpose. Picking the first connection can export the wrong entity.
- The trial balance is the year-to-date column pair. The Debit/Credit pair shows current-month movement.

## Local verification

Python 3.10 or newer. The lock file pins dependencies with hashes.

```bash
python -m pip install --require-hashes -r requirements.lock
python -m unittest discover -s tests -v
```

## Pull requests

Say which failure mode your change closes and include the test that reproduces it. For anything touching the OAuth or token-cache flow, describe what happens when the token endpoint returns an error mid-rotation, DPAPI refuses a payload, or an atomic migration/write fails.

For a potential security vulnerability, follow [SECURITY.md](SECURITY.md) rather than opening an issue.
