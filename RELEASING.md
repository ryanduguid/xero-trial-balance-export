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
4. Confirm the active `Protect version tags` ruleset matches `refs/tags/v*`, has no bypass actor, allows creation, and blocks tag updates and deletion:

    ```bash
    ruleset_id="$(gh api -H "X-GitHub-Api-Version: 2026-03-10" repos/ryanduguid/xero-trial-balance-export/rulesets --jq '.[] | select(.name == "Protect version tags" and .target == "tag" and .enforcement == "active") | .id')"
    test -n "$ruleset_id"
    gh api -H "X-GitHub-Api-Version: 2026-03-10" "repos/ryanduguid/xero-trial-balance-export/rulesets/$ruleset_id" --jq '{enforcement, bypass_actors, conditions, rules}'
    ```

    Stop unless the returned configuration has an empty `bypass_actors` array, includes only `refs/tags/v*`, and contains active `update` and `deletion` rules but no `creation` rule. This protection is required because immutable-release protection begins only when a draft is published.
5. Confirm `VERSION` and the first line of `RELEASE_NOTES.md` match the intended tag.
6. Fetch current remote `main`, create an annotated tag on that exact commit, for example `git tag -a v0.1.2 -m "v0.1.2"` (or `-s` when signing is configured), then push only that tag.

The workflow installs the hash-locked dependencies, runs the full offline suite and builds deterministic ZIP and tar.gz source archives. The archive helper fixes the timezone to UTC and Git text conversion to LF so the same tagged tree produces the same archive bytes on Linux and Windows. It adds an SPDX 2.3 SBOM, `SHA256SUMS`, GitHub provenance and an SBOM attestation before publishing the completed draft.

The authenticated release inventory must prove that no release or draft already uses the tag. The workflow creates the candidate as a draft, finds that draft through the all-releases API, and addresses it only by release ID. Before publication it verifies the exact notes, asset names and digests, then rechecks that the remote annotated tag and `main` still peel to the tested workflow commit. After publication it checks immutability, latest-release classification, digests and every release attestation. Any failed run leaves its draft for deliberate inspection; do not replace it or rerun blindly. The immediate pre-publication check narrows, but cannot make atomic, the residual race with a concurrent merge to `main`; do not merge other work during a release run, and rely on the no-bypass tag ruleset to prevent tag movement.

Verify the downloaded release with:

```bash
tag=v0.1.2
repo=ryanduguid/xero-trial-balance-export
release_commit="$(git ls-remote "https://github.com/$repo.git" "refs/tags/$tag^{}" | cut -f1)"
test -n "$release_commit"
gh release download "$tag" -R "$repo" --dir "release-$tag"
cd "release-$tag"
sha256sum --check SHA256SUMS
for file in *; do
  gh attestation verify "$file" -R "$repo" \
    --source-digest "$release_commit" \
    --source-ref "refs/tags/$tag" \
    --signer-workflow "$repo/.github/workflows/release.yml"
  gh release verify-asset "$tag" "$file" -R "$repo"
done
gh attestation verify xero-trial-balance-export-0.1.2.zip -R "$repo" \
  --predicate-type https://spdx.dev/Document/v2.3 \
  --source-digest "$release_commit" \
  --source-ref "refs/tags/$tag" \
  --signer-workflow "$repo/.github/workflows/release.yml"
gh release view "$tag" -R "$repo" --json isImmutable,isLatest,tagName \
  | jq -e --arg tag "$tag" \
      '.isImmutable == true and .isLatest == true and .tagName == $tag'
gh release verify "$tag" -R "$repo"
```

If any gate fails, inspect it before touching the tag or draft. Never move a published tag.
