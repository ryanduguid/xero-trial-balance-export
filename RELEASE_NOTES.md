# v0.1.4

Changes since published `v0.1.3`:

- move the Xero token cache out of site-packages to a user-owned location and validate its path through one guard;
- exercise the DPAPI token round-trip on the Windows CI leg;
- let an explicit `--token-file` beat the `XERO_TOKEN_FILE` environment variable so scripts can pin their credentials source; and
- documentation corrections: the fact-check fixes, the sample organisation name, a DISCLAIMER linked from the README, the provisional-member CA ANZ designation, and retirement of the last codename from user-facing docs.

The annotated `v0.1.2` tag is protected and permanently records commit `bd4cd417b06fb9dba3d6b36fbedbe544b1e0fec7`. [Workflow run 31832080223](https://github.com/ryanduguid/xero-trial-balance-export/actions/runs/31832080223) passed its tests, archives, checksums and attestations, then stopped before draft creation because one step lacked `GH_TOKEN`. No v0.1.2 GitHub release or draft was created. The protected tag must never be moved, deleted or reused; v0.1.3 is the recovery release.

## Security boundary

The release contains source and fabricated samples only: no Xero credentials, tokens or client exports, and it remains read-only against Xero.

Carried from v0.1.3: the complete Windows token cache is protected at rest with current-user DPAPI; a valid legacy plaintext cache is migrated atomically, under the existing cross-process lock, before any Xero request; corrupt, malformed or unknown-version cache envelopes are rejected without a network call or rewrite; the explicit non-Windows compatibility fallback remains plaintext JSON with owner-only `0600` permissions. Current-user DPAPI is a same-user, same-machine control: it does not protect tokens from code already running as that user, an administrator controlling the machine, or a compromised user session.
