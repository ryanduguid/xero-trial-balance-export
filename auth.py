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

from xero_client import save_tokens

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
# Granular scope - required for apps created on or after 2 March 2026.
# (The old broad accounting.reports.read only works on pre-existing apps
# and retires in September 2027.)
SCOPES = "offline_access accounting.reports.trialbalance.read"

# An RFC 6749 error code is a single ASCII word. The callback query is
# whatever the browser was pointed at, so anything else - escape sequences,
# newlines, a fake instruction - never reaches the terminal verbatim.
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


def parse_redirect(redirect_uri: str):
    """Split XERO_REDIRECT_URI into (hostname, port, path) or exit.

    urlparse accepts anything and defers its complaints: .port returns None
    when the URI carries no port and raises ValueError when the port is not
    a number. None then reaches socket.bind() as a TypeError, which is not
    an OSError and so escapes the bind handler as a raw traceback.
    """
    parsed = urlparse(redirect_uri)
    try:
        port = parsed.port
    except ValueError:
        port = None
    # Port 0 is the one number urlparse returns that this script cannot use:
    # bind() takes it and the OS hands back a random ephemeral port, so the
    # browser is sent to port 0, no callback can arrive, and the user waits
    # out the full CALLBACK_TIMEOUT for an error blaming the consent flow.
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or not port
        or not parsed.path.startswith("/")
    ):
        # https is named separately because it is the plausible mistake, and
        # it fails in the most confusing way available: the local callback
        # server speaks plain HTTP, so the browser's TLS handshake fails, no
        # callback arrives, and the user waits out the full CALLBACK_TIMEOUT
        # for an error blaming the consent flow.
        if parsed.scheme == "https":
            raise SystemExit(
                f"error: XERO_REDIRECT_URI is set to '{redirect_uri}', but the "
                "callback server this script runs speaks plain HTTP. An https "
                "URI fails the browser's TLS handshake and no callback ever "
                "arrives. Use http://localhost:8400/callback here and on the "
                "app at developer.xero.com; Xero allows http for localhost."
            )
        raise SystemExit(
            f"error: XERO_REDIRECT_URI is set to '{redirect_uri}', which this "
            "script cannot listen on. It needs the http scheme, an explicit "
            "host:port and a path, as in http://localhost:8400/callback. Use "
            "the same URI here and on the app at developer.xero.com."
        )
    return parsed.hostname, port, parsed.path


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
    client_id = os.environ.get("XERO_CLIENT_ID")
    client_secret = os.environ.get("XERO_CLIENT_SECRET")
    redirect_uri = os.environ.get("XERO_REDIRECT_URI", "http://localhost:8400/callback")
    if not client_id or not client_secret:
        sys.exit("Set XERO_CLIENT_ID and XERO_CLIENT_SECRET in .env (see .env.example).")

    redirect_host, redirect_port, redirect_path = parse_redirect(redirect_uri)
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
        server = _CallbackServer((redirect_host, redirect_port), _CallbackHandler)
    except OSError as exc:
        sys.exit(
            f"error: cannot listen on {redirect_host}:{redirect_port} for the "
            f"OAuth callback ({exc}). Something else holds that port. Close "
            "it, or point XERO_REDIRECT_URI at a free port and add the same "
            "URI to the app at developer.xero.com."
        )
    server.callback_path = redirect_path
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
    resp.raise_for_status()
    save_tokens(resp.json())
    print("Tokens saved to token.json. Next: python export_tb.py --date 2026-06-30")


if __name__ == "__main__":
    main()
