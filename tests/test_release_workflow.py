"""Fail-closed regression gates for the operator-controlled release path."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE_WORKFLOW = WORKFLOWS / "release.yml"
RELEASE_PROCEDURE = ROOT / "RELEASING.md"
RELEASE_NOTES = ROOT / "RELEASE_NOTES.md"
ACTION_REFERENCE = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
FULL_SHA_REFERENCE = re.compile(r"^[^@]+@[0-9a-f]{40}$")


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_metadata_is_exactly_v013(self) -> None:
        self.assertEqual("0.1.3\n", (ROOT / "VERSION").read_text(encoding="utf-8"))
        notes = RELEASE_NOTES.read_text(encoding="utf-8")
        self.assertTrue(
            notes.startswith("# v0.1.3\n\nChanges since published `v0.1.1`:")
        )

    def test_notes_record_dpapi_and_plaintext_fallback_boundaries(self) -> None:
        notes = " ".join(RELEASE_NOTES.read_text(encoding="utf-8").split())
        for phrase in (
            "current-user DPAPI",
            "before any Xero request",
            "without a network call or rewrite",
            "plaintext JSON",
            "`0600`",
            "same-user, same-machine",
            "administrator controlling the machine",
            "compromised user session",
            "no Xero credentials, tokens or client exports",
            "remains read-only against Xero",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, notes)

    def test_notes_preserve_the_failed_v012_release_lineage(self) -> None:
        notes = " ".join(RELEASE_NOTES.read_text(encoding="utf-8").split())
        for phrase in (
            "`v0.1.2` tag",
            "bd4cd417b06fb9dba3d6b36fbedbe544b1e0fec7",
            "workflow run 31832080223",
            "stopped before draft creation",
            "No v0.1.2 GitHub release or draft was created",
            "must never be moved, deleted or reused",
            "v0.1.3 is the recovery release",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), notes.lower())

    def test_every_step_that_invokes_gh_receives_the_scoped_token(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        step_blocks = re.findall(
            r"(?ms)^      - name: .*?(?=^      - name: |\Z)", workflow
        )
        gh_steps = [block for block in step_blocks if re.search(r"\bgh\s+", block)]
        self.assertEqual(3, len(gh_steps))
        for block in gh_steps:
            name = block.splitlines()[0].removeprefix("      - name: ")
            with self.subTest(step=name):
                metadata, separator, _ = block.partition("        run:")
                self.assertTrue(separator, f"{name} has no run block")
                self.assertIn("GH_TOKEN: ${{ github.token }}", metadata)

    def test_every_external_action_is_pinned_to_a_full_commit_sha(self) -> None:
        references: list[tuple[str, str]] = []
        for workflow in sorted({*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")}):
            for reference in ACTION_REFERENCE.findall(
                workflow.read_text(encoding="utf-8")
            ):
                if not reference.startswith("./"):
                    references.append((workflow.name, reference))
        self.assertGreater(len(references), 0)
        for workflow, reference in references:
            with self.subTest(workflow=workflow, action=reference):
                self.assertRegex(reference, FULL_SHA_REFERENCE)

    def test_release_is_tag_only_annotated_and_exact_main(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        trigger = workflow.split("permissions:", maxsplit=1)[0]
        self.assertIn('tags:\n      - "v*.*.*"', trigger)
        self.assertNotIn("workflow_dispatch", trigger)
        self.assertNotIn("pull_request", trigger)
        self.assertIn('git cat-file -t "refs/tags/$GITHUB_REF_NAME"', workflow)
        self.assertIn('refs/tags/$GITHUB_REF_NAME^{commit}', workflow)
        self.assertIn("git/ref/heads/main", workflow)
        self.assertIn("--verify-tag", workflow)
        self.assertNotIn("--clobber", workflow)

    def test_existing_release_check_is_authenticated_and_fail_closed(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        preflight = workflow.partition(
            "- name: Require a release-ready tag and repository"
        )[2].partition("- name: Install hash-locked dependencies")[0]
        self.assertIn("GH_TOKEN: ${{ github.token }}", preflight)
        self.assertIn("releases?per_page=100", preflight)
        self.assertIn("existing_release_ids", preflight)
        self.assertNotIn("gh release view", preflight)
        self.assertNotIn(">/dev/null 2>&1", preflight)

    def test_draft_is_verified_and_published_only_by_release_id(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        publication = workflow.partition(
            "- name: Create, verify and publish the complete release"
        )[2]
        create = publication.index('gh release create "$TAG" dist/*')
        lookup = publication.index("draft_release_ids=")
        draft_verify = publication.index("/tmp/draft-release.json")
        asset_verify = publication.index("The draft release does not contain the exact asset set")
        digest_verify = publication.index("/tmp/draft-digests")
        publish = publication.index("--method PATCH")
        immutable = publication.index(".immutable == true")
        self.assertLess(create, lookup)
        self.assertLess(lookup, draft_verify)
        self.assertLess(draft_verify, asset_verify)
        self.assertLess(asset_verify, digest_verify)
        self.assertLess(digest_verify, publish)
        self.assertLess(publish, immutable)
        before_publish = publication[:publish]
        self.assertNotIn('releases/tags/$TAG', before_publish)
        self.assertNotIn("gh release upload", publication)
        self.assertNotIn("gh release edit", publication)
        self.assertIn('releases/$release_id', publication)

    def test_remote_tag_and_main_are_rechecked_before_publication(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        prepublication = workflow.partition(
            "- name: Re-check the remote tag and main immediately before publication"
        )[2].partition(
            "- name: Create, verify and publish the complete release"
        )[0]
        self.assertIn('refs/tags/$TAG^{}', prepublication)
        self.assertIn("git/ref/heads/main", prepublication)
        self.assertGreaterEqual(prepublication.count('= "$EXPECTED_COMMIT"'), 3)
        publication = workflow.partition(
            "- name: Create, verify and publish the complete release"
        )[2]
        before_patch = publication.partition("--method PATCH")[0]
        self.assertIn('refs/tags/$TAG^{}', before_patch)
        self.assertIn("git/ref/heads/main", before_patch)

    def test_published_release_and_each_asset_are_verified(self) -> None:
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(".immutable == true", workflow)
        self.assertIn(".isLatest == true", workflow)
        self.assertIn("/tmp/published-digests", workflow)
        self.assertIn('gh release verify "$TAG"', workflow)
        self.assertIn('gh release verify-asset "$TAG" "$file"', workflow)

    def test_operator_process_documents_strong_consumer_verification(self) -> None:
        process = RELEASE_PROCEDURE.read_text(encoding="utf-8")
        self.assertIn("tag=v0.1.3", process)
        self.assertNotIn("tag=v0.1.2", process)
        self.assertIn("Protect version tags", process)
        self.assertIn("/rulesets", process)
        self.assertIn("bypass_actors", process)
        self.assertIn("residual race", process)
        self.assertIn("only by release ID", process)
        self.assertIn("gh release verify", process)
        self.assertIn("gh release verify-asset", process)
        self.assertIn("--source-digest", process)
        self.assertIn('--source-ref "refs/tags/$tag"', process)
        self.assertIn("--signer-workflow", process)
        self.assertIn("--predicate-type https://spdx.dev/Document/v2.3", process)
        self.assertIn("Protected v0.1.2 failed tag", process)
        self.assertIn("run 31832080223", process)
        self.assertIn("no v0.1.2 release or draft exists", process)
        self.assertIn("Do not move, delete or reuse `v0.1.2`", process)
        self.assertIn("`v0.1.3` is the recovery version", process)


if __name__ == "__main__":
    unittest.main()
