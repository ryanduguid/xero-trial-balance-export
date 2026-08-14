"""Find one newly-created draft release despite bounded API visibility delay."""

from __future__ import annotations

import argparse
import subprocess
import time
from collections.abc import Callable, Sequence


API_VERSION = "2026-03-10"


def _draft_release_ids(
    repository: str,
    tag: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[int]:
    """Return exact matching draft IDs from an authenticated release inventory."""

    result = runner(
        [
            "gh",
            "api",
            "--paginate",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            f"repos/{repository}/releases?per_page=100",
            "--jq",
            ".[] | [.id, .tag_name, .draft] | @tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    matches: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise RuntimeError("GitHub returned a malformed release inventory row")
        release_id, release_tag, draft = fields
        if release_tag == tag and draft == "true":
            if not release_id.isascii() or not release_id.isdecimal():
                raise RuntimeError("GitHub returned a non-numeric draft release ID")
            matches.append(int(release_id))
    return matches


def find_draft_release(
    repository: str,
    tag: str,
    *,
    attempts: int = 5,
    delay_seconds: float = 5,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Return exactly one draft ID, retrying only an empty inventory result."""

    if attempts < 1:
        raise ValueError("attempts must be at least one")
    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")

    for attempt in range(1, attempts + 1):
        matches = _draft_release_ids(repository, tag, runner=runner)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(f"GitHub returned multiple draft releases for {tag}")
        if attempt < attempts:
            sleep(delay_seconds)

    raise RuntimeError(
        f"GitHub did not expose a draft release for {tag} after {attempts} attempts"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--delay-seconds", type=float, default=5)
    args = parser.parse_args(argv)

    release_id = find_draft_release(
        args.repo,
        args.tag,
        attempts=args.attempts,
        delay_seconds=args.delay_seconds,
    )
    print(release_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
