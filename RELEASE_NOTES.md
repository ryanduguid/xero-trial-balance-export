# v0.1.4

The annotated `v0.1.2` tag remains protected, unpublished failed pre-publication history and must never be moved, deleted or reused; `v0.1.3` was the recovery release.

Changes since published `v0.1.3`:

- move the Xero token cache out of site-packages to a user-owned location and validate its path through one guard;
- exercise the DPAPI token round-trip on the Windows CI leg;
- let an explicit `--token-file` beat the `XERO_TOKEN_FILE` environment variable so scripts can pin their credentials source; and
- serialise concurrent token-cache refreshes under the existing cross-process lock (carried from hardening already verified in v0.1.3's lineage), plus documentation corrections: the fact-check fixes, the sample organisation name, a DISCLAIMER linked from the README, the provisional-member CA ANZ designation, and retirement of the last codename from user-facing docs.

## Release lineage

- The protected `v0.1.2` tag points at `bd4cd417b06fb9dba3d6b36fbedbe544b1e0fec7`. [Workflow run 31832080223](https://github.com/ryanduguid/xero-trial-balance-export/actions/runs/31832080223) passed tests, archives, checksums and attestations, then stopped before draft creation because one step lacked `GH_TOKEN`. No release or draft exists for it.
- `v0.1.3` is the recovery release and the last published version before this one.

## Security boundary

The tool stays read-only against Xero and ships source and fabricated samples only: no credentials, tokens or client exports. The Windows token cache is protected with current-user DPAPI, which does not protect tokens from code already running as that user, an administrator controlling the machine, or a compromised user session.
