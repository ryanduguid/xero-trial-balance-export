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
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")

# Refresh this many seconds before the access token's stated expiry.
EXPIRY_MARGIN = 60

# Xero access tokens last 30 minutes. Used only when a refresh response's own
# expires_in is unusable — the token still works, so the lifetime is guessed
# conservatively rather than the whole rotated pair being thrown away.
DEFAULT_EXPIRES_IN = 1800

# A copied or hand-edited cache must not make an access token appear fresh for
# hours or years. Allow a small amount of ordinary clock skew between writes
# and reads; anything further ahead forces a refresh with the preserved
# refresh token instead of trusting the cached access token.
TOKEN_CLOCK_SKEW = 300

# os.replace fails on Windows while any other process holds the destination
# open. Both durable writes in this project ride out a brief lock rather than
# losing work, and both read these two constants, so retuning them changes
# both: save_tokens onto token.json (the README tells users a second
# concurrent run is possible, and a lost refresh token locks the app out) and
# export_tb's CSV write onto a path Excel or Power BI Desktop may be holding
# open, where the lost work is a report that cannot be re-fetched for free.
REPLACE_ATTEMPTS = 5
REPLACE_BACKOFF = 0.2

# 429 backoff bounds. Xero's per-minute limit resets in under a minute, but
# the daily limit answers with a Retry-After measured in hours. Sleeping on
# that pins a scheduled export for the rest of the day, so cap the wait and
# exit instead.
RETRY_AFTER_DEFAULT = 5
RETRY_AFTER_MAX = 60

# Upper bound on the parsed value. The cap message turns it into a reset
# timestamp, and timedelta(seconds=...) overflows on a large enough number:
# 10**12 raises "date value out of range" and 10**13 raises "Python int too
# large to convert to C int", both as tracebacks. A day is longer than any
# Xero limit window, so anything above it is server junk either way.
RETRY_AFTER_CLAMP = 86400

# RFC 9110 delta-seconds is 1*DIGIT and nothing else. int() is wider than
# that grammar: it takes underscores ("1_0" -> 10), a leading sign ("+7" ->
# 7) and Unicode digits, so a header the spec does not allow would set a
# wait the docstring promises to refuse.
DELTA_SECONDS = re.compile(r"[0-9]+")


def _expires_in_is_usable(value: object) -> bool:
    return type(value) is int and 1 <= value <= 86400


def _require_token_pair(payload: object, *, label: str) -> dict:
    if not isinstance(payload, dict):
        raise SystemExit(f"error: {label} is not a JSON object; no token was saved.")
    for key in ("access_token", "refresh_token"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"error: {label} has no usable {key}; no token was saved.")
    return payload


def validate_rotated_response(payload: object) -> dict:
    """Validate a token endpoint response without discarding the pair in it.

    Both callers are here for the same reason. By the time a refresh response
    arrives the previous refresh token is already spent; by the time a code
    exchange response arrives (auth.py) the single-use authorisation code is
    already spent. Either way the pair in hand is the only way forward short
    of a full browser re-authorisation. access_token and refresh_token must
    be present — there is nothing worth persisting without them. expires_in
    is a local cache hint only, so an unusable one is replaced with the
    conservative default instead of throwing a perfectly good refresh token
    away.

    validate_token_response is the strict sibling and stays that way: it
    guards the token.json read, where refusing a corrupt payload stops the
    run without destroying anything - the file is left exactly as it was.
    """
    label = "Xero token response"
    tokens = dict(_require_token_pair(payload, label=label))
    if not _expires_in_is_usable(tokens.get("expires_in")):
        print(
            f"warning: {label} had an unusable expires_in "
            f"({tokens.get('expires_in')!r}); treating the access token as "
            f"valid for {DEFAULT_EXPIRES_IN}s.",
            file=sys.stderr,
        )
        tokens["expires_in"] = DEFAULT_EXPIRES_IN
    return tokens


def validate_token_response(payload: object, *, label: str, cached: bool = False) -> dict:
    """Fail on a token payload nothing is lost by refusing.

    This is the strict half of the pair: an unusable expires_in is fatal
    here. Its caller is the token.json read, where the payload is a local
    cache file rather than a freshly issued pair - a corrupt or hand-edited
    expires_in there is a reason to stop and re-authorise, and refusing it
    destroys nothing, because token.json is left exactly as it was. A
    response carrying a newly issued, un-replayable pair goes through
    validate_rotated_response instead, which never throws that pair away over
    a cache hint.
    """
    _require_token_pair(payload, label=label)
    if not _expires_in_is_usable(payload.get("expires_in")):
        raise SystemExit(f"error: {label} has an invalid expires_in; no token was saved.")
    if cached:
        obtained_at = payload.get("obtained_at")
        if (
            type(obtained_at) not in (int, float)
            or not 0 <= obtained_at < 100_000_000_000
        ):
            raise SystemExit("error: token.json has an invalid obtained_at; run: python auth.py")
    return payload


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> int:
    """Turn an HTTP Retry-After value into a non-negative delay.

    RFC 9110 permits either delta-seconds or an HTTP-date. Xero normally
    returns seconds, but an HTTP-date is standards-compliant and used to
    crash an otherwise recoverable 429 response, so both forms parse here.
    Delta-seconds is held to the RFC's own grammar (digits only); a missing
    header, a signed or underscored number or any other junk lands on
    RETRY_AFTER_DEFAULT rather than raising - the header is server-supplied
    and must never be able to crash the retry.

    Whichever branch parses it, the result is clamped to RETRY_AFTER_CLAMP,
    so every caller can do arithmetic on it without an overflow check of
    its own.
    """
    if value is None:
        return RETRY_AFTER_DEFAULT
    text = str(value).strip()
    if DELTA_SECONDS.fullmatch(text):
        # int() refuses a string of more than 4300 digits (CPython's
        # conversion limit) by raising ValueError, so count digits before
        # converting. More digits than the clamp has means the value is
        # above it whatever it is.
        digits = text.lstrip("0") or "0"
        if len(digits) > len(str(RETRY_AFTER_CLAMP)):
            return RETRY_AFTER_CLAMP
        return min(int(digits), RETRY_AFTER_CLAMP)
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return RETRY_AFTER_DEFAULT
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return min(max(0, math.ceil((retry_at - current).total_seconds())), RETRY_AFTER_CLAMP)


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
    tokens = validate_token_response(load_tokens(), label="token.json", cached=True)
    age = time.time() - tokens["obtained_at"]
    if not force and -TOKEN_CLOCK_SKEW <= age < tokens["expires_in"] - EXPIRY_MARGIN:
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
    # A mistyped XERO_CLIENT_SECRET is the other everyday failure here, and
    # the token endpoint answers it with 401 (OAuth2 invalid_client). Without
    # this branch raise_for_status printed a bare HTTPError traceback naming
    # the identity endpoint, which reads as a Xero outage rather than a typo
    # in .env. The stored refresh token is untouched either way.
    if resp.status_code == 401:
        raise SystemExit(
            "Xero rejected this app's credentials when refreshing the token "
            "(HTTP 401). Check XERO_CLIENT_ID and XERO_CLIENT_SECRET in .env "
            "against the app at developer.xero.com; token.json was left "
            "as it was."
        )
    resp.raise_for_status()
    try:
        new_tokens = resp.json()
    except ValueError:
        raise SystemExit("error: Xero returned a non-JSON token response; the existing token cache was left untouched.") from None
    new_tokens = validate_rotated_response(new_tokens)
    save_tokens(new_tokens)  # persist BEFORE using — rotation safety
    return new_tokens["access_token"]


def api_get(
    url: str,
    credentials: tuple[str, str],
    tenant_id: str | None = None,
    params: dict | None = None,
) -> dict:
    """GET a Xero API URL with auth headers. One capped retry on 429.

    The 429 wait comes from a server-supplied Retry-After, so it is parsed
    defensively and capped: a daily-limit response asking for hours exits
    with the reset time instead of holding the process.

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
        raw_retry_after = resp.headers.get("Retry-After")
        wait = retry_after_seconds(raw_retry_after)
        if wait > RETRY_AFTER_MAX:
            # wait is clamped by retry_after_seconds, so this addition cannot
            # overflow however large the header was.
            reset_at = datetime.now().astimezone() + timedelta(seconds=wait)
            # The header as sent, not the clamped number: reporting the clamp
            # as the server's own figure told anyone debugging the header a
            # value Xero never sent. Truncated and stripped of non-printables
            # because it is remote input on its way to a terminal.
            asked = "".join(ch for ch in str(raw_retry_after)[:40] if ch.isprintable())
            raise SystemExit(
                f"error: Xero rate limit hit and sent Retry-After: {asked}, "
                f"over the {RETRY_AFTER_MAX}s cap this script will sleep for. "
                f"The limit resets at or after "
                f"{reset_at.isoformat(timespec='seconds')} - re-run after that "
                f"(computed from the clamped {wait}s wait; the real reset may "
                f"be later)."
            )
        time.sleep(wait)
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            raise SystemExit(
                f"error: Xero is still rate limiting after a {wait}s wait - "
                "re-run later."
            )
    if resp.status_code == 401:
        headers["Authorization"] = f"Bearer {get_access_token(*credentials, force=True)}"
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 401:
            raise SystemExit(
                "Xero rejected the access token even after a forced refresh. "
                "Re-authorise with: python auth.py"
            )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        raise SystemExit("error: Xero returned a non-JSON API response.") from None


def get_connections(credentials: tuple[str, str]) -> list[dict]:
    """Authorised tenants: [{tenantId, tenantName, ...}, ...]."""
    return api_get(CONNECTIONS_URL, credentials)
