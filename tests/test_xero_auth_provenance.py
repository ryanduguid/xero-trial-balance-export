"""Fail closed if the documented Xero OAuth contracts change."""

from __future__ import annotations

import ast
import hashlib
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUTH_PATH = ROOT / "auth.py"
README_PATH = ROOT / "README.md"

RUNTIME_SCOPES = "offline_access accounting.reports.trialbalance.read"
README_SHA256 = "AF6C4484FDFD5788F9EC8572593962769CA1F14039CB1E915C3D7DD6E0A3DF4D"
AUTH_SHA256 = "F9EC9B4F8A497C518034E7432E1A3AF78AB58D7DF19C9A4321F1FAC3FAA75F5A"
AUTH_AST_SHA256 = "B5901FF2BAFF884BDA5D5404E38EA5DAF4804E18529704862D0A844C349AEAF4"

SCOPES_URL = "https://developer.xero.com/documentation/guides/oauth2/scopes/"
GRANULAR_FAQ_URL = "https://developer.xero.com/faq/granular-scopes"
OAUTH_FAQ_URL = "https://developer.xero.com/faq/oauth2"
CHANGELOG_URL = "https://developer.xero.com/changelog"

SCOPE_PARAGRAPH = (
    "Read-only (`accounting.reports.trialbalance.read`); this tool cannot write "
    "to any ledger. Web and PKCE apps created on or after 2 March 2026 use "
    "granular scopes, while existing apps using the broad "
    "`accounting.reports.read` scope must migrate by 13 September 2027. "
    f"Xero's [OAuth scope list]({SCOPES_URL}), "
    f"[Granular Scopes FAQ]({GRANULAR_FAQ_URL}) and "
    f"[developer changelog]({CHANGELOG_URL}) were checked on 20 August 2026 "
    "(`2026-08-20`); recheck them for apps created or used after that date. "
    "`token.json` and `.env` are gitignored. They are credentials, so treat "
    "them like passwords."
)
REFRESH_PARAGRAPH = (
    "Xero refresh tokens **rotate on use**: every refresh returns a replacement "
    "refresh token. If the refresh response does not arrive, Xero permits "
    "retrying the previous token for up to a 30-minute grace period; outside "
    "that window, the user must re-authorise. "
    f"The [Xero OAuth FAQ]({OAUTH_FAQ_URL}) was checked on 20 August 2026 "
    "(`2026-08-20`); recheck it for apps created or used after that date."
)


def _canonical_lf(raw: bytes) -> bytes:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError("UTF-8 BOM is not permitted")
    canonical = raw.replace(b"\r\n", b"\n")
    if b"\r" in canonical:
        raise AssertionError("bare carriage returns are not permitted")
    try:
        canonical.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AssertionError("source must be UTF-8") from exc
    return canonical


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _require_digest(raw: bytes, expected: str, name: str) -> bytes:
    canonical = _canonical_lf(raw)
    actual = _sha256(canonical)
    if actual != expected:
        raise AssertionError(
            f"{name} changed: expected canonical SHA-256 {expected}, found {actual}"
        )
    return canonical


def _stable_ast(value: Any) -> Any:
    """Return a Python-version-stable executable AST projection."""
    if isinstance(value, ast.AST):
        fields = []
        for name, item in ast.iter_fields(value):
            # ``type_params`` was added in Python 3.12. Empty/None fields do not
            # affect this module's executable tree and can vary by Python minor.
            if name == "type_params" or item is None or item == []:
                continue
            fields.append((name, _stable_ast(item)))
        return type(value).__name__, tuple(fields)
    if isinstance(value, list):
        return tuple(_stable_ast(item) for item in value)
    return value


def _scopes_assignment(tree: ast.Module) -> str:
    assignments: list[ast.Assign | ast.AnnAssign] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SCOPES"
            for target in node.targets
        ):
            assignments.append(node)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "SCOPES"
        ):
            assignments.append(node)

    if len(assignments) != 1:
        raise AssertionError(
            f"expected one top-level SCOPES assignment, found {len(assignments)}"
        )
    try:
        value = ast.literal_eval(assignments[0].value)
    except (TypeError, ValueError) as exc:
        raise AssertionError("SCOPES must remain a literal string") from exc
    if not isinstance(value, str):
        raise AssertionError("SCOPES must remain a literal string")
    return value


def _validate_readme(raw: bytes) -> None:
    _require_digest(raw, README_SHA256, "README.md")


def _validate_auth(raw: bytes) -> None:
    canonical = _canonical_lf(raw)
    try:
        tree = ast.parse(canonical.decode("utf-8"))
    except SyntaxError as exc:
        raise AssertionError("auth.py must remain valid Python") from exc

    scopes = _scopes_assignment(tree)
    if scopes != RUNTIME_SCOPES:
        raise AssertionError(
            f"runtime SCOPES changed: expected {RUNTIME_SCOPES!r}, found {scopes!r}"
        )
    ast_digest = _sha256(repr(_stable_ast(tree)).encode("utf-8"))
    if ast_digest != AUTH_AST_SHA256:
        raise AssertionError("auth.py executable AST changed")
    _require_digest(canonical, AUTH_SHA256, "auth.py")


def _validate_owned_documents(documents: Mapping[str, bytes]) -> None:
    _validate_readme(documents["README.md"])
    _validate_auth(documents["auth.py"])


def _replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise AssertionError(f"expected one mutation target, found {count}: {old!r}")
    return text.replace(old, new, 1)


def _wrap_across_heading(
    markdown: str,
    heading: str,
    next_heading: str,
    opener: str,
    closer: str,
) -> str:
    wrapped = _replace_once(markdown, f"{heading}\n", f"{opener}\n{heading}\n")
    return _replace_once(
        wrapped,
        f"{next_heading}\n",
        f"{next_heading}\n{closer}\n",
    )


class XeroAuthProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = _canonical_lf(AUTH_PATH.read_bytes())
        self.readme = _canonical_lf(README_PATH.read_bytes())
        self.auth_text = self.auth.decode("utf-8")
        self.readme_text = self.readme.decode("utf-8")

    def assert_rejected(self, cases) -> None:
        for label, validator, source in cases:
            with self.subTest(variant=label):
                with self.assertRaises(AssertionError):
                    validator(source)

    def test_exact_documents_accept_lf_crlf_and_an_unrelated_file_change(self) -> None:
        for label, newline in (("LF", b"\n"), ("CRLF", b"\r\n")):
            with self.subTest(newlines=label):
                _validate_readme(self.readme.replace(b"\n", newline))
                _validate_auth(self.auth.replace(b"\n", newline))

        contributing = (ROOT / "CONTRIBUTING.md").read_bytes()
        documents = {
            "README.md": self.readme,
            "auth.py": self.auth,
            "CONTRIBUTING.md": contributing + b"\nSafe in-memory control.\n",
        }
        self.assertNotEqual(documents["CONTRIBUTING.md"], contributing)
        _validate_owned_documents(documents)

    def test_encoding_content_and_whitespace_mutations_are_rejected(self) -> None:
        cases = []
        for name, validator, source in (
            ("README", _validate_readme, self.readme),
            ("auth", _validate_auth, self.auth),
        ):
            cases.extend(
                (
                    (f"{name} UTF-8 BOM", validator, b"\xef\xbb\xbf" + source),
                    (
                        f"{name} lone carriage return",
                        validator,
                        source.replace(b"\n", b"\r", 1),
                    ),
                    (f"{name} added content", validator, source + b"changed\n"),
                    (f"{name} added blank line", validator, source + b"\n"),
                    (
                        f"{name} trailing whitespace",
                        validator,
                        source.replace(b"\n", b" \n", 1),
                    ),
                )
            )
        self.assert_rejected(cases)

    def test_all_reviewer_readme_mutations_are_rejected(self) -> None:
        scope_html = _wrap_across_heading(
            self.readme_text,
            "## Scope and disclaimer",
            "## Tests",
            "<!--",
            "-->",
        )
        scope_fence = _wrap_across_heading(
            self.readme_text,
            "## Scope and disclaimer",
            "## Tests",
            "```text",
            "```",
        )
        refresh_html = _wrap_across_heading(
            self.readme_text,
            "## The refresh-token gotcha",
            "## Files",
            "<!--",
            "-->",
        )
        refresh_fence = _wrap_across_heading(
            self.readme_text,
            "## The refresh-token gotcha",
            "## Files",
            "```text",
            "```",
        )

        replacements = (
            (
                "scope claim hidden in HTML",
                "Read-only (`accounting.reports.trialbalance.read`);",
                "<!-- Read-only (`accounting.reports.trialbalance.read`); -->",
            ),
            (
                "visible deadline stale with correct decoy",
                "13 September 2027",
                "12 September 2027 <!-- 13 September 2027 -->",
            ),
            (
                "visible checked date stale with correct decoy",
                "were checked on 20 August 2026 (`2026-08-20`)",
                (
                    "were checked on 19 August 2026 (`2026-08-19`) "
                    "<!-- 20 August 2026 (`2026-08-20`) -->"
                ),
            ),
            (
                "visible grace stale with correct decoy",
                "30-minute grace period",
                "60-minute grace period <!-- 30-minute grace period -->",
            ),
            ("granular scope negated", "use granular scopes", "do not use granular scopes"),
            ("migration negated", "must migrate", "must not migrate"),
            ("scope relationship removed", "use granular", "have granular"),
            ("migration obligation removed", "must migrate", "faces"),
            ("retry forbidden", "permits retrying", "forbids retrying"),
            ("retry permission removed", "permits retrying", "mentions"),
            (
                "later Scope contradiction",
                "On Windows, `token.json` uses",
                "Web and PKCE apps do not use granular scopes. On Windows, `token.json` uses",
            ),
            (
                "scope URL moved to a decoy",
                "https://developer.xero.com/faq/granular-scopes",
                "https://example.invalid/\n\nDecoy: https://developer.xero.com/faq/granular-scopes",
            ),
            (
                "OAuth FAQ moved to a decoy",
                "https://developer.xero.com/faq/oauth2",
                "https://example.invalid/\n\nDecoy: https://developer.xero.com/faq/oauth2",
            ),
            (
                "broad report scope restored",
                "accounting.reports.trialbalance.read",
                "accounting.reports.read",
            ),
            ("single-use label restored", "**rotate on use**", "**are single-use**"),
            (
                "retry condition removed",
                "If the refresh response does not arrive, ",
                "",
            ),
            ("previous-token subject removed", "the previous token", "a token"),
            (
                "replacement-token result removed",
                "returns a replacement refresh token",
                "returns a token",
            ),
            (
                "re-authorisation outcome removed",
                "the user must re-authorise",
                "the user may continue",
            ),
        )
        cases = [
            ("scope balanced HTML wrapper", _validate_readme, scope_html.encode()),
            ("scope cross-heading fence wrapper", _validate_readme, scope_fence.encode()),
            ("refresh balanced HTML wrapper", _validate_readme, refresh_html.encode()),
            ("refresh cross-heading fence wrapper", _validate_readme, refresh_fence.encode()),
            (
                "entire scope claim hidden in HTML",
                _validate_readme,
                _replace_once(
                    self.readme_text,
                    SCOPE_PARAGRAPH,
                    f"<!-- {SCOPE_PARAGRAPH} -->",
                ).encode(),
            ),
            (
                "scope heading and claim inside a local fence",
                _validate_readme,
                _replace_once(
                    self.readme_text,
                    f"## Scope and disclaimer\n\n{SCOPE_PARAGRAPH}",
                    f"```text\n## Scope and disclaimer\n\n{SCOPE_PARAGRAPH}\n```",
                ).encode(),
            ),
            (
                "entire refresh claim hidden in HTML",
                _validate_readme,
                _replace_once(
                    self.readme_text,
                    REFRESH_PARAGRAPH,
                    f"<!-- {REFRESH_PARAGRAPH} -->",
                ).encode(),
            ),
            (
                "refresh heading and claim inside a local fence",
                _validate_readme,
                _replace_once(
                    self.readme_text,
                    f"## The refresh-token gotcha\n\n{REFRESH_PARAGRAPH}",
                    f"```text\n## The refresh-token gotcha\n\n{REFRESH_PARAGRAPH}\n```",
                ).encode(),
            ),
        ]
        cases.extend(
            (
                label,
                _validate_readme,
                _replace_once(self.readme_text, old, new).encode("utf-8"),
            )
            for label, old, new in replacements
        )

        without_scope = _replace_once(
            self.readme_text,
            f"{SCOPE_PARAGRAPH}\n\n",
            "",
        )
        without_refresh = _replace_once(
            self.readme_text,
            f"{REFRESH_PARAGRAPH}\n\n",
            "",
        )
        cases.extend(
            (
                (
                    "scope claim moved to Files",
                    _validate_readme,
                    _replace_once(
                        without_scope,
                        "## Files\n\n",
                        f"## Files\n\n{SCOPE_PARAGRAPH}\n\n",
                    ).encode(),
                ),
                (
                    "refresh claim moved to Files",
                    _validate_readme,
                    _replace_once(
                        without_refresh,
                        "## Files\n\n",
                        f"## Files\n\n{REFRESH_PARAGRAPH}\n\n",
                    ).encode(),
                ),
                (
                    "contradictory scope clause appended",
                    _validate_readme,
                    _replace_once(
                        self.readme_text,
                        SCOPE_PARAGRAPH,
                        SCOPE_PARAGRAPH + " Web apps do not use granular scopes.",
                    ).encode(),
                ),
                (
                    "contradictory scope paragraph appended",
                    _validate_readme,
                    _replace_once(
                        self.readme_text,
                        f"{SCOPE_PARAGRAPH}\n\n",
                        (
                            f"{SCOPE_PARAGRAPH}\n\n"
                            "Web apps do not use granular scopes.\n\n"
                        ),
                    ).encode(),
                ),
                (
                    "contradictory refresh paragraph appended",
                    _validate_readme,
                    _replace_once(
                        self.readme_text,
                        f"{REFRESH_PARAGRAPH}\n\n",
                        (
                            f"{REFRESH_PARAGRAPH}\n\n"
                            "Xero forbids retrying the previous token.\n\n"
                        ),
                    ).encode(),
                ),
            )
        )

        for url in (SCOPES_URL, CHANGELOG_URL):
            cases.append(
                (
                    f"scope URL moved to a decoy: {url}",
                    _validate_readme,
                    (
                        _replace_once(self.readme_text, f"]({url})", "](https://example.invalid/)")
                        + f"\n\nDecoy: {url}\n"
                    ).encode(),
                )
            )
        self.assert_rejected(cases)

    def test_auth_contract_and_executable_guard_are_fail_closed(self) -> None:
        assignment = f'SCOPES = "{RUNTIME_SCOPES}"'
        cases = [
            (
                "broadened runtime scope",
                _validate_auth,
                _replace_once(
                    self.auth_text,
                    assignment,
                    'SCOPES = "offline_access accounting.reports.read"',
                ).encode(),
            ),
            (
                "computed runtime scope",
                _validate_auth,
                _replace_once(
                    self.auth_text,
                    assignment,
                    'SCOPES = "offline_access " + "accounting.reports.trialbalance.read"',
                ).encode(),
            ),
            (
                "duplicate runtime scope",
                _validate_auth,
                self.auth + f"\n{assignment}\n".encode(),
            ),
            (
                "executable statement changed",
                _validate_auth,
                _replace_once(
                    self.auth_text,
                    "server.timeout = 1",
                    "server.timeout = 2",
                ).encode(),
            ),
            (
                "auth checked date stale",
                _validate_auth,
                _replace_once(self.auth_text, "2026-08-20", "2026-08-19").encode(),
            ),
            (
                "auth deadline vague",
                _validate_auth,
                _replace_once(self.auth_text, "13 September 2027", "September 2027").encode(),
            ),
            (
                "auth granular scope negated",
                _validate_auth,
                _replace_once(
                    self.auth_text,
                    "use granular scopes",
                    "do not use granular scopes",
                ).encode(),
            ),
            (
                "auth migration negated",
                _validate_auth,
                _replace_once(self.auth_text, "must migrate", "must not migrate").encode(),
            ),
        ]
        for url in (SCOPES_URL, GRANULAR_FAQ_URL, CHANGELOG_URL):
            cases.append(
                (
                    f"auth URL moved to a decoy: {url}",
                    _validate_auth,
                    (
                        _replace_once(self.auth_text, f"# {url}", "#")
                        + f"\n# Decoy: {url}\n"
                    ).encode(),
                )
            )
        self.assert_rejected(cases)


if __name__ == "__main__":
    unittest.main()

