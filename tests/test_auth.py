import socket
import threading
import time
import unittest

import auth
from auth import callback_server_config


class CallbackServerConfigTests(unittest.TestCase):
    def test_defaults_an_http_uri_without_a_port_to_port_80(self):
        self.assertEqual(
            callback_server_config("http://localhost/callback"),
            ("localhost", 80, "/callback"),
        )

    def test_preserves_an_explicit_localhost_port_and_uses_root_when_path_is_empty(self):
        self.assertEqual(
            callback_server_config("http://localhost:8400"),
            ("localhost", 8400, "/"),
        )

    def test_rejects_a_scheme_the_plain_http_listener_cannot_serve(self):
        with self.assertRaisesRegex(ValueError, "must use http"):
            callback_server_config("https://localhost/callback")

    def test_rejects_a_uri_without_a_host(self):
        with self.assertRaisesRegex(ValueError, "must include a host"):
            callback_server_config("http:///callback")

    def test_rejects_non_localhost_callback_hosts(self):
        for redirect_uri in (
            "http://127.0.0.1:8400/callback",
            "http://0.0.0.0:8400/callback",
            "http://192.0.2.1:8400/callback",
            "http://[::1]:8400/callback",
        ):
            with self.subTest(redirect_uri=redirect_uri):
                with self.assertRaisesRegex(ValueError, "must use localhost"):
                    callback_server_config(redirect_uri)

    def test_rejects_port_zero(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            callback_server_config("http://localhost:0/callback")


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


class DribblingConnectionTest(unittest.TestCase):
    """A slow trickle must not outlast the wall-clock deadline either.

    CALLBACK_READ_TIMEOUT bounds one recv, and every byte received resets it.
    A peer sending one byte per second therefore held handle_request open for
    as long as it liked: measured at 26s against a 300s CALLBACK_TIMEOUT, it
    never returned. SilentConnectionTest cannot see this - it sends nothing,
    which the per-read timeout does close.
    """

    def test_a_dribbling_socket_does_not_outlast_the_deadline(self):
        port = _free_port()
        server = auth._CallbackServer(("127.0.0.1", port), auth._CallbackHandler)
        self.addCleanup(server.server_close)
        server.callback_path = "/callback"
        server.auth_code = None
        server.auth_error = None
        server.returned_state = None
        server.timeout = 0.2

        stop = threading.Event()
        self.addCleanup(stop.set)

        def dribble():
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                # Never a newline, so rfile.readline() cannot return on its own.
                while not stop.is_set():
                    try:
                        sock.sendall(b"G")
                    except OSError:
                        return
                    stop.wait(0.25)

        trickler = threading.Thread(target=dribble, daemon=True)
        trickler.start()

        deadline = 1.0
        budget = deadline + auth.CALLBACK_CONNECTION_TIMEOUT + 5
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
        worker.join(timeout=budget + 10)

        self.assertFalse(
            worker.is_alive(),
            "wait_for_callback is still blocked: the dribbling connection is "
            "resetting the read timeout forever",
        )
        self.assertIsInstance(result.get("raised"), SystemExit)
        self.assertIn("no Xero callback arrived", str(result["raised"]))
        self.assertLess(result["elapsed"], budget)

    def test_the_connection_budget_does_not_reset_on_a_byte(self):
        """The point of the second timeout: it is absolute, not per-read."""
        self.assertGreater(
            auth.CALLBACK_CONNECTION_TIMEOUT, auth.CALLBACK_READ_TIMEOUT
        )
        self.assertLess(auth.CALLBACK_CONNECTION_TIMEOUT, auth.CALLBACK_TIMEOUT)


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


if __name__ == "__main__":
    unittest.main()
