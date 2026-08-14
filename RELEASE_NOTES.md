# v0.1.3

Changes since published `v0.1.1`:

- protect the complete Windows token cache at rest with current-user DPAPI and forbid any encryption or decryption UI;
- migrate a valid legacy plaintext Windows cache atomically, under the existing cross-process lock, before any Xero request;
- reject corrupt, malformed or unknown-version cache envelopes without a network call or rewrite;
- retain the explicit non-Windows compatibility fallback as plaintext JSON with owner-only `0600` permissions; and
- pass the scoped GitHub Actions token to every workflow step that invokes GitHub CLI, including the immediate remote tag and `main` recheck.

Current-user DPAPI is a same-user, same-machine control. It does not protect tokens from code already running as that user, an administrator controlling the machine, or a compromised user session, and it is not a portable cache format. The release contains source and fabricated samples only. It contains no Xero credentials, tokens or client exports and remains read-only against Xero.

Release lineage: the annotated `v0.1.2` tag points to `bd4cd417b06fb9dba3d6b36fbedbe544b1e0fec7` and remains protected by the no-bypass tag ruleset. [Workflow run 31832080223](https://github.com/ryanduguid/xero-trial-balance-export/actions/runs/31832080223) passed its tests, archive builds, checksums and attestations, then stopped before draft creation because the remote recheck step did not receive `GH_TOKEN`. No v0.1.2 GitHub release or draft was created. The protected tag is retained as failed pre-publication history and must never be moved, deleted or reused; v0.1.3 is the recovery release.
