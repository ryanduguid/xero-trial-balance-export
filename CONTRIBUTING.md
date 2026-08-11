# Contributing

This exporter reads a Xero trial balance and writes a validated CSV. It stays read-only against the Xero API: no contribution should give it the ability to post, approve, or modify anything in an organisation.

## Data boundary

- Never commit credentials. The `.gitignore` blocks `.env*` except `.env.example`, plus `token.json`, `*.json`, and the common export extensions. Keep `.env.example` to placeholder values.
- Keep exported trial balances, account lists and tenant identifiers from a real organisation out of the repository. Sample data belongs in `samples/`.
- The code writes token files with a restrictive ACL, but every refresh rewrites the file and re-inherits the directory ACL. Do not treat file permissions as the security story.

## Correctness rules worth preserving

- The export refuses to write when debits do not equal credits. A scheduled refresh reads the output path and ignores the exit code, so writing a wrong file causes more damage than writing nothing.
- Refresh tokens rotate and Xero accepts each one once. Persist the new token before you validate anything cosmetic about the response, or you throw away a working token and force the user through a fresh sign-in.
- The exporter refuses a bare multi-organisation run on purpose. Picking the first connection can export the wrong entity.
- The trial balance is the year-to-date column pair. The Debit/Credit pair shows current-month movement.

## Local verification

Python 3.10 or newer. The lock file pins dependencies with hashes.

```bash
python -m pip install --require-hashes -r requirements.lock
python -m unittest discover -s tests -v
```

## Pull requests

Say which failure mode your change closes and include the test that reproduces it. For anything touching the OAuth flow, describe what happens when the token endpoint returns an error mid-rotation.

For a potential security vulnerability, follow [SECURITY.md](SECURITY.md) rather than opening an issue.
