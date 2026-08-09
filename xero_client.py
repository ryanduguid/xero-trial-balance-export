"""Xero API client: token cache, rotation-safe refresh, authed GET.

Xero refresh tokens are single-use — every refresh returns a NEW refresh
token, and the old one survives only a 30-minute grace period. A script
that fails to persist the new refresh token recovers if it reruns inside
that window and is locked out after it. Two defences here:

1. Tokens are written to disk immediately after every refresh response,
   before any API call is made with the new access token.
2. The write is atomic (temp file + os.replace), so a crash mid-write can't
   leave a corrupt token.json. If the replace itself cannot be completed,
   the temp file holding the new pair is kept and named in the error.
"""

import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")

# Refresh this many seconds before the access token's stated expiry.
EXPIRY_MARGIN = 60

# os.replace onto token.json fails on Windows while any other process holds
# the destination open, and the README tells users a second concurrent run is
# possible. Ride out a brief lock rather than losing the new refresh token.
REPLACE_ATTEMPTS = 5
REPLACE_BACKOFF = 0.2


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> int:
    """Turn an HTTP Retry-After value into a non-negative delay.

    RFC 9110 permits either delay-seconds or an HTTP-date.  Xero normally
    returns seconds, but treating a standards-compliant date as an integer
    used to crash an otherwise recoverable 429 response.
    """
    if not value:
        return 5
    try:
        return max(0, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return 5
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0, math.ceil((retry_at - current).total_seconds()))


def save_tokens(token_response: dict) -> None:
    """Persist a token endpoint response atomically, stamped with obtained_at.

    The temp file is deleted only when it holds nothing worth keeping. Once
    the JSON has been written and flushed, that file is the only copy of the
    freshly issued refresh token: token.json still holds the previous one,
    which Xero has already consumed. If the fsync or the replace cannot be
    made to stick, the temp file survives and its path goes into the error
    message, so the pair can be recovered by hand inside Xero's 30-minute
    grace window.
    """
    data = dict(token_response)
    data["obtained_at"] = time.time()
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(TOKEN_FILE), suffix=".tmp")
    written = False
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            # The flush is what makes the file whole, which is all "written"
            # claims. Past this line the temp file holds the complete new
            # pair and is worth more than token.json, so the finally clause
            # below must never delete it.
            written = True
            # Flushing only reaches the OS page cache; NTFS journals the
            # rename's metadata, not the data behind it. Force the bytes down
            # before os.replace destroys the previous token pair.
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                raise SystemExit(
                    f"error: wrote the new Xero token pair to {tmp_path} but "
                    f"could not flush it to disk ({exc}). {TOKEN_FILE} still "
                    f"holds the refresh token Xero has already consumed, so "
                    f"leave it alone: copy {tmp_path} over {TOKEN_FILE} "
                    "within Xero's 30-minute rotation grace window, or run: "
                    "python auth.py"
                ) from None

        last_error: OSError | None = None
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp_path, TOKEN_FILE)
                return
            except OSError as exc:
                last_error = exc
                if attempt < REPLACE_ATTEMPTS - 1:
                    time.sleep(REPLACE_BACKOFF * (attempt + 1))

        raise SystemExit(
            f"error: wrote the new Xero token pair to {tmp_path} but could not "
            f"move it onto {TOKEN_FILE} after {REPLACE_ATTEMPTS} attempts "
            f"({last_error}). {TOKEN_FILE} still holds the refresh token Xero "
            f"has already consumed, so leave it alone: copy {tmp_path} over "
            f"{TOKEN_FILE} within Xero's 30-minute rotation grace window, or "
            "run: python auth.py"
        )
    finally:
        # A half-written temp file is worthless; a fully written one is the
        # only copy of the new refresh token and must survive.
        if not written and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def load_tokens() -> dict:
    if not os.path.exists(TOKEN_FILE):
        raise SystemExit("No token.json — run: python auth.py")
    with open(TOKEN_FILE) as fh:
        try:
            return json.load(fh)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise SystemExit(
                "token.json is unreadable or corrupt — delete it and "
                "run: python auth.py"
            ) from None


def get_access_token(client_id: str, client_secret: str, force: bool = False) -> str:
    """Return a live access token, refreshing (and re-persisting) if needed.

    force=True skips the local expiry check — for when a cached token looked
    fresh but Xero returned 401 anyway (skewed clock, token.json copied from
    another machine).
    """
    tokens = load_tokens()
    age = time.time() - tokens.get("obtained_at", 0)
    if not force and age < tokens.get("expires_in", 1800) - EXPIRY_MARGIN:
        return tokens["access_token"]

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
        auth=(client_id, client_secret),
        timeout=30,
    )
    if resp.status_code == 400 and "invalid_grant" in resp.text:
        raise SystemExit(
            "Refresh token rejected (already used or expired). "
            "Re-authorise with: python auth.py"
        )
    resp.raise_for_status()
    new_tokens = resp.json()
    save_tokens(new_tokens)  # persist BEFORE using — rotation safety
    return new_tokens["access_token"]


def api_get(
    url: str,
    credentials: tuple[str, str],
    tenant_id: str | None = None,
    params: dict | None = None,
) -> dict:
    """GET a Xero API URL with auth headers. One polite retry on 429.

    The access token is looked up via get_access_token() per call, never
    passed in — a token captured once by a caller goes stale the moment any
    call refreshes it, and every later call then repeats the 401 + forced
    refresh, burning a single-use refresh token each time. A surprise 401
    still gets one forced refresh and retry — the local expiry math can lie
    (skewed clock, stale cache). A second 401 exits with the same
    re-authorise guidance the invalid_grant path gives, instead of a raw
    traceback.
    """
    headers = {
        "Authorization": f"Bearer {get_access_token(*credentials)}",
        "Accept": "application/json",
    }
    if tenant_id:
        headers["Xero-tenant-id"] = tenant_id

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 429:
        wait = retry_after_seconds(resp.headers.get("Retry-After"))
        time.sleep(wait)
        resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 401:
        headers["Authorization"] = f"Bearer {get_access_token(*credentials, force=True)}"
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 401:
            raise SystemExit(
                "Xero rejected the access token even after a forced refresh. "
                "Re-authorise with: python auth.py"
            )
    resp.raise_for_status()
    return resp.json()


def get_connections(credentials: tuple[str, str]) -> list[dict]:
    """Authorised tenants: [{tenantId, tenantName, ...}, ...]."""
    return api_get(CONNECTIONS_URL, credentials)
