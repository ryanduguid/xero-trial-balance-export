"""Resolve the Xero token-cache path off the install tree.

The historical default lived beside xero_client.py. After pip install that
is site-packages. The default is now the per-user state directory; a
module-adjacent token.json is used only when that file already exists.
"""
from __future__ import annotations

import os
import tempfile

# Historical cache: next to this module. Used only when that file already
# exists so existing installs keep working after the default moved to the
# per-user state directory.
LEGACY_MODULE_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "token.json"
)


def _state_home_token_file() -> str:
    home = os.path.abspath(os.path.expanduser("~"))
    if os.name == "nt":
        return os.path.join(home, "AppData", "Local", "xero-trial-balance-export", "token.json")
    return os.path.join(home, ".local", "state", "xero-trial-balance-export", "token.json")


def _allowed_token_roots() -> tuple[str, ...]:
    """Directories the token cache may live under.

    Resolved on every call, not once at import, so a process that changes
    its working directory is judged against the directory it is in now.
    """
    return (
        os.path.realpath(os.path.abspath(os.path.expanduser("~"))),
        os.path.realpath(os.path.abspath(os.getcwd())),
        os.path.realpath(os.path.abspath(tempfile.gettempdir())),
        os.path.realpath(os.path.abspath(os.path.dirname(__file__))),
    )


def safe_token_path(path: str) -> str:
    """Return an absolute path allowed to hold the Xero token cache.

    The cache must be named token.json and must stay under the home
    directory, the process working directory, the system temp directory,
    or the install directory. abspath + prefix check is the CodeQL
    sanitizer for path injection.

    Keep the prefix check as a written-out loop that returns the checked
    value. Folding it into any() over a generator hides the barrier from
    the analyser, which then reports every path built from this result.
    """
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if os.path.basename(candidate) != "token.json":
        raise SystemExit("error: token cache path must be named token.json")
    for root in _allowed_token_roots():
        if candidate == root or candidate.startswith(root + os.sep):
            return candidate
    raise SystemExit(
        "error: token cache path must stay under the home directory, "
        "the process working directory, the system temp directory, "
        "or the install directory."
    )


DEFAULT_TOKEN_FILE = safe_token_path(_state_home_token_file())


def resolve_token_file(cli_value: str | None = None) -> str:
    """Resolve the token cache path.

    Order: an explicit command-line value (export_tb.py's --token-file),
    then the XERO_TOKEN_FILE environment variable, then an existing
    module-adjacent token.json, then the per-user state directory.
    """
    if cli_value is not None and cli_value.strip():
        return safe_token_path(cli_value)
    env_value = os.environ.get("XERO_TOKEN_FILE")
    if env_value is not None and env_value.strip():
        return safe_token_path(env_value)
    if os.path.isfile(LEGACY_MODULE_TOKEN_FILE):
        return safe_token_path(LEGACY_MODULE_TOKEN_FILE)
    return DEFAULT_TOKEN_FILE


# Module-level for the existing callers and tests that patch it. Entry points
# that parse a command line or load .env re-resolve after doing so.
TOKEN_FILE = resolve_token_file()
