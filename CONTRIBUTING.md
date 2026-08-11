# Contributing

This exporter reads a Xero trial balance and writes a validated CSV. It is read-only against the Xero API and should stay that way: no contribution should give it the ability to post, approve, or modify anything in an organisation.

## Data boundary

- Never commit credentials. The `.gitignore` blocks `.env*` except `.env.example`, plus `token.json`, `*.json`, and every common export extension. Keep `.env.example` to placeholder values.
- Do not commit an exported trial balance, an account list, or a tenant identifier from a real organisation. Sample data belongs in `samples/`.
- Token files are written with a restrictive ACL, but the directory ACL is re-inherited on every refresh — do not rely on file permissions alone as the security story.

## Correctness rules worth preserving

- The export refuses to write when debits do not equal credits. A scheduled refresh reads the output path, not the exit code, so a wrong file is worse than no file.
- Refresh tokens rotate and are single-use. A refresh path must persist the new token before validating anything cosmetic about the response, or a good token is discarded and the user is forced to re-authenticate.
- A bare multi-organisation run is refused deliberately: picking the first connection can export the wrong entity.
- The trial balance is the year-to-date column pair. The Debit/Credit pair is current-month movement.

## Local verification

Python 3.10 or newer. Dependencies are pinned with hashes.

```bash
python -m pip install --require-hashes -r requirements.lock
python -m unittest discover -s tests -v
```

## Pull requests

Say which failure mode the change closes and include the test that reproduces it. For anything touching the OAuth flow, state what happens when the token endpoint returns an error mid-rotation.

For a potential security vulnerability, follow [SECURITY.md](SECURITY.md) rather than opening an issue.
