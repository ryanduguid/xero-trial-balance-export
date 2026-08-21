"""One-time Xero OAuth2 authorisation.

Opens the Xero consent page in your browser, catches the redirect on a local
HTTP server, exchanges the authorisation code for tokens, and saves them to
token.json. Run this once; export_tb.py refreshes tokens automatically after
that.

Prerequisite: an app at developer.xero.com with redirect URI matching
XERO_REDIRECT_URI (default http://localhost:8400/callback). The scopes
below are requested automatically at consent time; there is nothing to
configure in the developer portal.
"""

import os
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv

import xero_client
from xero_client import save_tokens, validate_rotated_response

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
# Web and PKCE apps created on or after 2 March 2026 use granular scopes.
# Existing apps using accounting.reports.read must migrate by 13 September 2027.
# This exporter needs only offline_access and accounting.reports.trialbalance.read.
# Xero contract checked 2026-08-20 (20 August 2026):
# https://developer.xero.com/documentation/guides/oauth2/scopes/
# https://developer.xero.com/faq/granular-scopes
# https://developer.xero.com/changelog
# Recheck these pages for apps created or used after that date.
SCOPES = "offline_access accounting.reports.trialbalance.read"

# An RFC 6749 error code is a single ASCII word. The callback query is
# whatever the browser was pointed at, so anything else (escape sequences,
# newlines, a fake instruction) never reaches the terminal verbatim.
ERROR_CODE = re.compile(r"[A-Za-z0-9_]{1,64}")

# Wall-clock budget for the browser round trip. Without it a consent the
# user never finishes, or one whose callback went somewhere else, leaves the
# script serving forever.
CALLBACK_TIMEOUT = 300

# Read budget for one accepted connection. HTTPServer.timeout bounds accept()
# only, so it does nothing for a connection that is accepted and then stays
# silent: rfile.readline() blocks inside handle_request and the wall-clock
# deadline never gets looked at again. Browsers open speculative connections
# and abandon them, which is enough to trigger it. A redirect from the
# browser on loopback arrives in one packet, so two seconds is generous.
CALLBACK_READ_TIMEOUT = 2

# Total budget for one accepted connection, whatever it does inside it. The
# read timeout above resets on every byte received, so a peer sending one
# byte per second holds the connection open indefinitely and wait_for_callback
# never gets to re-test its own deadline. This one does not reset. A real
# browser redirect is finished inside a few milliseconds.
CALLBACK_CONNECTION_TIMEOUT = 10


def callback_server_config(redirect_uri: str) -> tuple[str, int, str]:
    """Return the local HTTP listener configuration for a registered redirect.

    ``http://localhost/callback`` is a valid URI and carries the default HTTP
    port.  Passing its ``None`` port directly to HTTPServer raised a TypeError
    before the browser could be opened.  This script deliberately implements a
    plain local HTTP callback, so HTTPS redirect URIs need a different server
    rather than a misleading listener that cannot complete TLS.
    """
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http":
        raise ValueError("XERO_REDIRECT_URI must use http for this local callback server")
    if not parsed.hostname:
        raise ValueError("XERO_REDIRECT_URI must include a host")
    if parsed.hostname != "localhost":
        raise ValueError(
            "XERO_REDIRECT_URI must use localhost for this local callback server"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("XERO_REDIRECT_URI has an invalid port") from exc
    if port is None:
        port = 80
    if port <= 0:
        raise ValueError("XERO_REDIRECT_URI port must be between 1 and 65535")
    return parsed.hostname, port, parsed.path or "/"


class _CallbackServer(HTTPServer):
    """Holds the callback port exclusively.

    HTTPServer sets allow_reuse_address = 1. On Windows that means
    SO_REUSEADDR, which lets bind() succeed on a port another process is
    already listening on; whichever socket the OS picks then receives the
    authorisation code, and it may not be this one. Refusing the port is the
    only safe answer, so allow_reuse_address is off and, on Windows,
    SO_EXCLUSIVEADDRUSE is set before the bind. A port already in use now
    raises OSError up front.
    """

    allow_reuse_address = False

    def server_bind(self):
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if os.name == "nt" and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


def wait_for_callback(server, timeout: float = CALLBACK_TIMEOUT) -> None:
    """Serve requests until the callback lands or the deadline passes."""
    deadline = time.monotonic() + timeout
    while server.auth_code is None and server.auth_error is None:
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"error: no Xero callback arrived within {int(timeout)} seconds. "
                "Nothing was saved. Run again and complete the consent in the "
                "browser."
            )
        server.handle_request()


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches exactly one OAuth callback; ignores favicon and other noise.

    timeout is read by StreamRequestHandler.setup, which applies it to the
    accepted connection. Without it a peer that connects and sends nothing
    parks handle_request in rfile.readline() for as long as it likes, and
    wait_for_callback's deadline never comes around again.

    That bounds one recv, not the connection. A peer that dribbles one byte
    every second resets the per-read timeout forever and holds handle_request
    open past any wall-clock deadline: measured at 26s against a 300s
    CALLBACK_TIMEOUT, it never returned. So the connection also gets a hard
    deadline - a timer that shuts the socket down underneath any pending
    read, which makes rfile.readline() return and hands control back to
    wait_for_callback so it can re-test its own deadline.
    """

    timeout = CALLBACK_READ_TIMEOUT

    def setup(self):
        super().setup()
        self._expiry = threading.Timer(CALLBACK_CONNECTION_TIMEOUT, self._expire)
        self._expiry.daemon = True
        self._expiry.start()

    def _expire(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            # Already closed, or closing as we fire. Either way it is gone.
            pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except OSError:
            # The shutdown above surfaces here as ConnectionAbortedError on
            # Windows and ConnectionResetError elsewhere. socketserver would
            # print the traceback through handle_error, which reads as a crash
            # when it is this class doing exactly what it was asked to do.
            self.close_connection = True

    def finish(self):
        expiry = getattr(self, "_expiry", None)
        if expiry is not None:
            expiry.cancel()
        try:
            super().finish()
        except OSError:
            # The shutdown above can land mid-response; the callback either
            # arrived or it did not, and wait_for_callback decides which.
            pass

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urlparse(self.path)
        if parsed.path != self.server.callback_path:
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        self.server.auth_code = params.get("code", [None])[0]
        self.server.auth_error = params.get("error", [None])[0]
        self.server.returned_state = params.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if self.server.auth_error:
            self.wfile.write(b"<h3>Authorisation was not completed. You can close this tab.</h3>")
        else:
            self.wfile.write(b"<h3>Authorised. You can close this tab.</h3>")

    def log_message(self, *args):  # silence request logging
        pass


def main() -> None:
    load_dotenv()
    # Re-resolve after load_dotenv: a XERO_TOKEN_FILE set in .env is not in
    # the environment when xero_client is imported.
    xero_client.TOKEN_FILE = xero_client.resolve_token_file()
    client_id = os.environ.get("XERO_CLIENT_ID")
    client_secret = os.environ.get("XERO_CLIENT_SECRET")
    redirect_uri = os.environ.get("XERO_REDIRECT_URI", "http://localhost:8400/callback")
    if not client_id or not client_secret:
        sys.exit("Set XERO_CLIENT_ID and XERO_CLIENT_SECRET in .env (see .env.example).")

    try:
        callback_host, callback_port, callback_path = callback_server_config(redirect_uri)
    except ValueError as exc:
        sys.exit(f"Invalid XERO_REDIRECT_URI: {exc}")
    state = secrets.token_urlsafe(16)

    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
        }
    )
    url = f"{AUTHORIZE_URL}?{query}"

    try:
        server = _CallbackServer((callback_host, callback_port), _CallbackHandler)
    except OSError as exc:
        sys.exit(
            f"error: cannot listen on {callback_host}:{callback_port} for the "
            f"OAuth callback ({exc}). Something else holds that port. Close "
            "it, or point XERO_REDIRECT_URI at a free port and add the same "
            "URI to the app at developer.xero.com."
        )
    server.callback_path = callback_path
    server.auth_code = None
    server.auth_error = None
    server.returned_state = None
    server.timeout = 1  # keeps Ctrl-C responsive while waiting

    if webbrowser.open(url):
        print("Opening browser for Xero consent...")
        print("If the browser did not open, paste this URL into one:")
    else:
        print("Could not open a browser (SSH or headless session?). Paste this URL into one:")
    print(f"  {url}")

    try:
        wait_for_callback(server)
    finally:
        server.server_close()

    # State first: neither the code nor the error is worth trusting until the
    # callback is proved to be the one this run started.
    if server.returned_state != state:
        sys.exit("State mismatch - possible CSRF or stale callback. Run again.")

    if server.auth_error:
        if ERROR_CODE.fullmatch(server.auth_error):
            sys.exit(f"Xero returned '{server.auth_error}' - consent was denied or cancelled. Run again.")
        sys.exit("Xero returned an error code this script could not read - consent was denied or cancelled. Run again.")

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": server.auth_code,
            "redirect_uri": redirect_uri,
        },
        auth=(client_id, client_secret),
        timeout=30,
    )
    # The two everyday identity answers here must read as instructions, not
    # as an HTTPError traceback - the same rule the refresh path in
    # xero_client.py applies. invalid_grant is an authorisation code already
    # spent or expired; 401 is a mistyped XERO_CLIENT_SECRET (OAuth2
    # invalid_client). Nothing has been saved yet either way, so both exits
    # simply say what to fix and neither prints the response body.
    if resp.status_code == 400 and "invalid_grant" in resp.text:
        sys.exit(
            "Xero rejected the authorisation code (already used or expired). "
            "Nothing was saved. Run again and complete the consent promptly."
        )
    if resp.status_code == 401:
        sys.exit(
            "Xero rejected this app's credentials when exchanging the "
            "authorisation code (HTTP 401). Check XERO_CLIENT_ID and "
            "XERO_CLIENT_SECRET in .env against the app at "
            "developer.xero.com. Nothing was saved."
        )
    resp.raise_for_status()
    try:
        tokens = resp.json()
    except ValueError:
        sys.exit("error: Xero returned a non-JSON token response. Nothing was saved.")
    # The authorisation code behind this response is single-use and was just
    # spent, so the pair in hand is the only thing standing between the user
    # and another round of browser consent. validate_rotated_response keeps
    # the pair whatever expires_in says (it is a local cache hint, and the
    # access token works regardless) and still refuses a response carrying no
    # usable access_token or refresh_token, where there is nothing to save.
    tokens = validate_rotated_response(tokens)
    save_tokens(tokens)
    print("Tokens saved to token.json. Next: python export_tb.py --date 2026-06-30")


if __name__ == "__main__":
    main()
