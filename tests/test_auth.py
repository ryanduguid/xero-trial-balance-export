import io
import os
import socket
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import auth
import xero_client
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


class _StubTokenResponse:
    """Stands in for the token endpoint's answer to the code exchange."""

    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"unexpected HTTP {self.status_code}")

    def json(self):
        return self._payload


class StateCheckTest(unittest.TestCase):
    """The state comparison is the CSRF defence on the callback.

    RealCallbackTest proves the handler records whatever state arrives.
    Nothing exercised main()'s comparison of that value against the one this
    run generated - and without it, a callback carrying someone else's
    authorisation code is exchanged and written to token.json, binding this
    app registration to an org the user never authorised.
    """

    GENERATED = "state-this-run-generated"
    TOKENS = {"access_token": "A", "refresh_token": "R", "expires_in": 1800}

    def _run_main(self, returned_state):
        port = _free_port()
        env = {
            "XERO_CLIENT_ID": "id-not-a-secret",
            "XERO_CLIENT_SECRET": "secret-not-used",
            "XERO_REDIRECT_URI": f"http://localhost:{port}/callback",
        }

        def land_the_callback(server, timeout=auth.CALLBACK_TIMEOUT):
            server.auth_code = "the-code"
            server.auth_error = None
            server.returned_state = returned_state

        raised = None
        with mock.patch.object(auth, "load_dotenv", lambda *a, **k: None), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(auth.secrets, "token_urlsafe", return_value=self.GENERATED), \
                mock.patch.object(auth.webbrowser, "open", return_value=False), \
                mock.patch.object(auth, "wait_for_callback", side_effect=land_the_callback), \
                mock.patch.object(
                    auth.requests, "post",
                    return_value=_StubTokenResponse(dict(self.TOKENS)),
                ) as post, \
                mock.patch.object(auth, "save_tokens") as save_tokens:
            with redirect_stdout(io.StringIO()):
                try:
                    auth.main()
                except SystemExit as exc:
                    raised = exc
        return raised, post, save_tokens

    def test_a_callback_carrying_another_state_is_never_exchanged(self):
        raised, post, save_tokens = self._run_main("attacker-supplied-state")
        self.assertIsInstance(raised, SystemExit)
        self.assertIn("State mismatch", str(raised.code))
        post.assert_not_called()
        save_tokens.assert_not_called()

    def test_a_callback_with_no_state_at_all_is_refused(self):
        raised, post, save_tokens = self._run_main(None)
        self.assertIsInstance(raised, SystemExit)
        self.assertIn("State mismatch", str(raised.code))
        post.assert_not_called()
        save_tokens.assert_not_called()

    def test_the_matching_state_still_completes_the_exchange(self):
        raised, post, save_tokens = self._run_main(self.GENERATED)
        self.assertIsNone(raised, raised)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["data"]["code"], "the-code")
        save_tokens.assert_called_once_with(self.TOKENS)


class ExchangeResponseTest(unittest.TestCase):
    """The code exchange holds an un-replayable pair, exactly like a refresh.

    The authorisation code behind this response is single-use, so refusing
    the response costs the whole browser consent round trip. expires_in is a
    local cache hint the client can do without, and xero_client already has
    the rule for that case - validate_rotated_response substitutes the
    30-minute default and keeps the pair. Applying the strict validator here
    instead threw a freshly issued refresh token away over the hint. The
    sibling guard on the refresh path is SaveTokensTest's
    test_refresh_with_an_unusable_expires_in_still_persists_the_rotated_pair,
    and the ('1800', 0, None, True, 999999) set below is the one it uses.
    """

    GENERATED = "state-this-run-generated"

    def _run_main(self, payload, *, json_error=False):
        port = _free_port()
        env = {
            "XERO_CLIENT_ID": "id-not-a-secret",
            "XERO_CLIENT_SECRET": "secret-not-used",
            "XERO_REDIRECT_URI": f"http://localhost:{port}/callback",
        }

        def land_the_callback(server, timeout=auth.CALLBACK_TIMEOUT):
            server.auth_code = "the-code"
            server.auth_error = None
            server.returned_state = self.GENERATED

        class _Response(_StubTokenResponse):
            def json(self):
                if json_error:
                    raise ValueError("Expecting value: line 1 column 1 (char 0)")
                return payload

        raised = None
        stderr = io.StringIO()
        with mock.patch.object(auth, "load_dotenv", lambda *a, **k: None), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(auth.secrets, "token_urlsafe", return_value=self.GENERATED), \
                mock.patch.object(auth.webbrowser, "open", return_value=False), \
                mock.patch.object(auth, "wait_for_callback", side_effect=land_the_callback), \
                mock.patch.object(auth.requests, "post", return_value=_Response(payload)), \
                mock.patch.object(auth, "save_tokens") as save_tokens:
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                try:
                    auth.main()
                except SystemExit as exc:
                    raised = exc
        return raised, save_tokens, stderr.getvalue()

    def test_an_unusable_expires_in_still_persists_the_issued_pair(self):
        for hint in ("1800", 0, None, True, 999999, 1800.0):
            with self.subTest(expires_in=hint):
                payload = {"access_token": "A", "refresh_token": "R", "expires_in": hint}
                raised, save_tokens, stderr = self._run_main(payload)
                self.assertIsNone(raised, raised)
                save_tokens.assert_called_once()
                saved = save_tokens.call_args.args[0]
                self.assertEqual(saved["access_token"], "A")
                self.assertEqual(saved["refresh_token"], "R")
                self.assertEqual(saved["expires_in"], xero_client.DEFAULT_EXPIRES_IN)
                self.assertIn("unusable expires_in", stderr)

    def test_a_missing_expires_in_is_defaulted_rather_than_refused(self):
        raised, save_tokens, _ = self._run_main({"access_token": "A", "refresh_token": "R"})
        self.assertIsNone(raised, raised)
        self.assertEqual(
            save_tokens.call_args.args[0]["expires_in"], xero_client.DEFAULT_EXPIRES_IN
        )

    def test_a_usable_expires_in_is_kept_exactly_as_sent(self):
        raised, save_tokens, stderr = self._run_main(
            {"access_token": "A", "refresh_token": "R", "expires_in": 1800}
        )
        self.assertIsNone(raised, raised)
        save_tokens.assert_called_once_with(
            {"access_token": "A", "refresh_token": "R", "expires_in": 1800}
        )
        self.assertEqual(stderr, "")

    def test_a_response_with_no_usable_pair_saves_nothing(self):
        """There is nothing worth persisting without both tokens, so this
        half of the validator stays fatal."""
        for payload in (
            {"refresh_token": "R", "expires_in": 1800},
            {"access_token": "A", "expires_in": 1800},
            {"access_token": "", "refresh_token": "R", "expires_in": 1800},
            {"access_token": "A", "refresh_token": "   ", "expires_in": 1800},
            {"access_token": "A", "refresh_token": ["R"], "expires_in": 1800},
            ["access_token", "refresh_token"],
        ):
            with self.subTest(payload=payload):
                raised, save_tokens, _ = self._run_main(payload)
                self.assertIsInstance(raised, SystemExit)
                message = str(raised.code)
                self.assertTrue(message.startswith("error: "), message)
                self.assertIn("no token was saved", message)
                save_tokens.assert_not_called()

    def test_a_non_json_token_response_saves_nothing(self):
        raised, save_tokens, _ = self._run_main({}, json_error=True)
        self.assertIsInstance(raised, SystemExit)
        self.assertIn("non-JSON token response", str(raised.code))
        self.assertIn("Nothing was saved", str(raised.code))
        save_tokens.assert_not_called()


class ExchangeRejectionTest(unittest.TestCase):
    """The two everyday identity answers to the code exchange must read as
    instructions, not as HTTPError tracebacks - the same rule
    RefreshRejectionTest pins for the refresh path in test_xero_client.py.
    Without the branches, the most common first-run failures (a wrong
    XERO_CLIENT_SECRET, an expired or reused code) printed a raw traceback
    naming the identity endpoint."""

    GENERATED = "state-this-run-generated"

    def _run_main(self, response):
        port = _free_port()
        env = {
            "XERO_CLIENT_ID": "id-not-a-secret",
            "XERO_CLIENT_SECRET": "secret-not-used",
            "XERO_REDIRECT_URI": f"http://localhost:{port}/callback",
        }

        def land_the_callback(server, timeout=auth.CALLBACK_TIMEOUT):
            server.auth_code = "the-code"
            server.auth_error = None
            server.returned_state = self.GENERATED

        raised = None
        with mock.patch.object(auth, "load_dotenv", lambda *a, **k: None), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(auth.secrets, "token_urlsafe", return_value=self.GENERATED), \
                mock.patch.object(auth.webbrowser, "open", return_value=False), \
                mock.patch.object(auth, "wait_for_callback", side_effect=land_the_callback), \
                mock.patch.object(auth.requests, "post", return_value=response), \
                mock.patch.object(auth, "save_tokens") as save_tokens:
            with redirect_stdout(io.StringIO()):
                try:
                    auth.main()
                except SystemExit as exc:
                    raised = exc
        return raised, save_tokens

    def test_a_spent_or_expired_code_says_so_and_saves_nothing(self):
        raised, save_tokens = self._run_main(
            _StubTokenResponse({}, status_code=400, text='{"error":"invalid_grant"}')
        )
        self.assertIsInstance(raised, SystemExit)
        message = str(raised.code)
        self.assertIn("authorisation code", message)
        self.assertIn("Run again", message)
        self.assertNotIn("the-code", message)
        save_tokens.assert_not_called()

    def test_a_rejected_client_secret_points_at_the_env_file(self):
        raised, save_tokens = self._run_main(
            _StubTokenResponse({}, status_code=401)
        )
        self.assertIsInstance(raised, SystemExit)
        message = str(raised.code)
        self.assertIn("XERO_CLIENT_SECRET", message)
        self.assertIn(".env", message)
        self.assertNotIn("secret-not-used", message)
        save_tokens.assert_not_called()

    def test_any_other_http_failure_keeps_its_traceback(self):
        """Only the two identity answers are translated; a 500 is not an
        instruction anyone can follow, so raise_for_status still reports it."""
        with self.assertRaises(RuntimeError):
            self._run_main(_StubTokenResponse({}, status_code=500))


if __name__ == "__main__":
    unittest.main()
