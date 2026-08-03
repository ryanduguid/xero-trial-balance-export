"""Tests for the OAuth callback listener. Standard library only."""

import os
import socket
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
