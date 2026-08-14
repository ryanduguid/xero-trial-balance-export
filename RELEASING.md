# Releasing

Releases are built by GitHub Actions from an annotated tag on the exact `main` commit. Do not create or upload release assets by hand.

Before tagging:

1. Merge the release pull request and require every `main` check to pass.
2. Enable release immutability in the repository settings.
3. From an operator session authenticated with repository Administration read access, run:

    ```bash
    gh api -H "X-GitHub-Api-Version: 2026-03-10" repos/ryanduguid/xero-trial-balance-export/immutable-releases --jq .enabled
    ```

    Do not push the tag unless the output is exactly `true`. The Actions `GITHUB_TOKEN` cannot be granted repository Administration read access, so the tag workflow cannot perform this preflight itself.
4. Confirm `VERSION` and the first line of `RELEASE_NOTES.md` match the intended tag.
5. Create an annotated tag on current remote `main`, for example `git tag -a v0.1.1 -m "v0.1.1"` (or `-s` when signing is configured), then push only that tag.

The workflow installs the hash-locked dependencies, runs the full offline suite and builds deterministic ZIP and tar.gz source archives. The archive helper fixes the timezone to UTC and Git text conversion to LF so the same tagged tree produces the same archive bytes on Linux and Windows. It adds an SPDX 2.3 SBOM, `SHA256SUMS`, GitHub provenance and an SBOM attestation before publishing the completed draft. Existing releases are refused rather than overwritten.

Verify the downloaded release with:

```bash
gh release download v0.1.1 -R ryanduguid/xero-trial-balance-export --dir release-v0.1.1
cd release-v0.1.1
sha256sum --check SHA256SUMS
gh attestation verify xero-trial-balance-export-0.1.1.zip -R ryanduguid/xero-trial-balance-export
gh attestation verify xero-trial-balance-export-0.1.1.zip -R ryanduguid/xero-trial-balance-export --predicate-type https://spdx.dev/Document/v2.3
gh release view v0.1.1 -R ryanduguid/xero-trial-balance-export --json isImmutable
gh release verify v0.1.1 -R ryanduguid/xero-trial-balance-export
gh release verify-asset v0.1.1 xero-trial-balance-export-0.1.1.zip -R ryanduguid/xero-trial-balance-export
```

If any gate fails, inspect it before touching the tag or draft. Never move a published tag.
