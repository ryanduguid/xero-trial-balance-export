"""Tests for the OAuth callback listener. Standard library only."""

import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CallbackServerBindTest(unittest.TestCase):
    def test_address_reuse_is_off(self):
        self.assertFalse(auth._CallbackServer.allow_reuse_address)

    def test_a_second_listener_on_the_same_port_is_refused(self):
        port = _free_port()
        first = auth._CallbackServer(("127.0.0.1", port), auth._CallbackHandler)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            second = auth._CallbackServer(("127.0.0.1", port), auth._CallbackHandler)
            second.server_close()

    def test_the_port_is_refused_even_to_a_reusing_socket(self):
        """A plain SO_REUSEADDR socket must not be able to steal the port.

        This is the delivery-hijack path: on Windows a second SO_REUSEADDR
        bind onto a live listener succeeds, and the OS may hand the callback
        to either socket.
        """
        port = _free_port()
        server = auth._CallbackServer(("127.0.0.1", port), auth._CallbackHandler)
        self.addCleanup(server.server_close)
        thief = socket.socket()
        self.addCleanup(thief.close)
        thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with self.assertRaises(OSError):
            thief.bind(("127.0.0.1", port))


class _FakeServer:
    """Stands in for the callback server: counts handle_request calls."""

    def __init__(self, code_after=None):
        self.auth_code = None
        self.auth_error = None
        self.code_after = code_after
        self.handled = 0

    def handle_request(self):
        self.handled += 1
        if self.code_after is not None and self.handled >= self.code_after:
            self.auth_code = "irrelevant"


class WaitForCallbackTest(unittest.TestCase):
    def test_returns_once_the_callback_lands(self):
        server = _FakeServer(code_after=2)
        auth.wait_for_callback(server, timeout=30)
        self.assertEqual(server.handled, 2)

    def test_a_never_completed_consent_exits_on_the_deadline(self):
        server = _FakeServer()
        with self.assertRaises(SystemExit) as ctx:
            auth.wait_for_callback(server, timeout=0.05)
        message = str(ctx.exception)
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("no Xero callback arrived", message)

    def test_the_default_deadline_is_bounded(self):
        self.assertTrue(0 < auth.CALLBACK_TIMEOUT <= 600)


class SilentConnectionTest(unittest.TestCase):
    """A half-open connection must not outlast the wall-clock deadline.

    HTTPServer.timeout bounds accept() only. An accepted connection that
    sends nothing blocks handle_request inside rfile.readline(), so without
    a read timeout on the connection the deadline is never re-checked and
    wait_for_callback serves forever. A browser opening a speculative
    connection and abandoning it is enough to cause it.
    """

    def test_a_silent_socket_does_not_outlast_the_deadline(self):
        port = _free_port()
        server = auth._CallbackServer(("127.0.0.1", port), auth._CallbackHandler)
        self.addCleanup(server.server_close)
        server.callback_path = "/callback"
        server.auth_code = None
        server.auth_error = None
        server.returned_state = None
        server.timeout = 0.2

        quiet = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(quiet.close)

        deadline = 1.0
        budget = deadline + auth.CALLBACK_READ_TIMEOUT + 2
        result = {}

        def run():
            start = time.monotonic()
            try:
                auth.wait_for_callback(server, timeout=deadline)
            except BaseException as exc:  # noqa: BLE001 - recorded, not swallowed
                result["raised"] = exc
            result["elapsed"] = time.monotonic() - start

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=budget + 5)

        self.assertFalse(
            worker.is_alive(),
            f"wait_for_callback is still blocked {budget + 5:.0f}s after a "
            f"{deadline}s deadline: the silent connection is holding it",
        )
        self.assertIsInstance(result.get("raised"), SystemExit)
        self.assertIn("no Xero callback arrived", str(result["raised"]))
        self.assertLess(result["elapsed"], budget)


class RealCallbackTest(unittest.TestCase):
    """A genuine redirect must still be caught, read timeout and all."""

    def test_the_callback_query_reaches_the_server(self):
        port = _free_port()
        server = auth._CallbackServer(("127.0.0.1", port), auth._CallbackHandler)
        self.addCleanup(server.server_close)
        server.callback_path = "/callback"
        server.auth_code = None
        server.auth_error = None
        server.returned_state = None
        server.timeout = 0.2

        def send_redirect():
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(
                    b"GET /callback?code=the-code&state=the-state HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
                )
                sock.recv(1024)

        caller = threading.Thread(target=send_redirect, daemon=True)
        caller.start()
        auth.wait_for_callback(server, timeout=10)
        caller.join(timeout=5)

        self.assertEqual(server.auth_code, "the-code")
        self.assertEqual(server.returned_state, "the-state")
        self.assertIsNone(server.auth_error)


class ParseRedirectTest(unittest.TestCase):
    def test_a_usable_uri_is_split(self):
        self.assertEqual(
            auth.parse_redirect("http://localhost:8400/callback"),
            ("localhost", 8400, "/callback"),
        )

    def test_a_uri_without_a_usable_port_is_refused(self):
        """urlparse().port is None or raises here, and None binds as TypeError."""
        for uri in (
            "http://localhost/callback",
            "http://localhost:/callback",
            "http://localhost:eightyfour/callback",
            "localhost:8400/callback",
            "http://:8400/callback",
            "http://localhost:8400",
            "",
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(SystemExit) as ctx:
                    auth.parse_redirect(uri)
                message = str(ctx.exception)
                self.assertTrue(message.startswith("error: "), message)
                self.assertIn("XERO_REDIRECT_URI", message)
                self.assertIn("http://localhost:8400/callback", message)

    def test_main_refuses_a_portless_uri_before_binding(self):
        """main must exit on the URI, not carry a None port down to bind()."""
        env = {
            "XERO_CLIENT_ID": "id-not-a-secret",
            "XERO_CLIENT_SECRET": "secret-not-used",
            "XERO_REDIRECT_URI": "http://localhost/callback",
        }

        def refuse_to_bind(address, handler):
            # A real bind on (host, None) raises TypeError, which the OSError
            # handler in main does not catch. Standing in for it keeps the
            # test off the network and fails loudly if the guard is gone.
            raise AssertionError(f"main tried to bind {address!r}")

        with mock.patch.object(auth, "load_dotenv", lambda *a, **k: None), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(auth, "webbrowser") as browser, \
                mock.patch.object(
                    auth, "_CallbackServer", side_effect=refuse_to_bind
                ) as server_cls:
            with self.assertRaises(SystemExit) as ctx:
                auth.main()
        server_cls.assert_not_called()
        browser.open.assert_not_called()
        self.assertIn("XERO_REDIRECT_URI", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
