# v0.1.2

Changes since `v0.1.1`:

- protect the complete Windows token cache at rest with current-user DPAPI and forbid any encryption or decryption UI;
- migrate a valid legacy plaintext Windows cache atomically, under the existing cross-process lock, before any Xero request;
- reject corrupt, malformed or unknown-version cache envelopes without a network call or rewrite; and
- retain the explicit non-Windows compatibility fallback as plaintext JSON with owner-only `0600` permissions.

Current-user DPAPI is a same-user, same-machine control. It does not protect tokens from code already running as that user, an administrator controlling the machine, or a compromised user session, and it is not a portable cache format. The release contains source and fabricated samples only. It contains no Xero credentials, tokens or client exports and remains read-only against Xero.
