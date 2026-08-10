import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock
from unittest.mock import patch

import xero_client


class _Response:
    def __init__(self, status_code, *, headers=None, payload=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"unexpected HTTP {self.status_code}")


class RetryAfterTests(unittest.TestCase):
    def test_accepts_delay_seconds(self):
        self.assertEqual(xero_client.retry_after_seconds("7"), 7)
        self.assertEqual(xero_client.retry_after_seconds(" 17 "), 17)

    def test_accepts_an_http_date_and_rounds_up(self):
        now = datetime(2030, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc)
        retry_at = now.replace(microsecond=0) + timedelta(seconds=2)
        value = format_datetime(retry_at, usegmt=True)
        self.assertEqual(xero_client.retry_after_seconds(value, now=now), 2)

    def test_malformed_or_missing_values_fall_back_to_a_politeness_delay(self):
        for raw in (None, "", "not-a-date", "soon", "1.5", "-1"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    xero_client.retry_after_seconds(raw),
                    xero_client.RETRY_AFTER_DEFAULT,
                )

    def test_values_outside_the_delta_seconds_grammar_are_refused(self):
        """int() accepts syntax RFC 9110 delta-seconds does not."""
        # The escape below is ARABIC-INDIC DIGIT THREE, which int() reads as 3.
        for raw in ("1_0", "+7", "\u0663", "7 7", "0x10"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    xero_client.retry_after_seconds(raw),
                    xero_client.RETRY_AFTER_DEFAULT,
                )

    def test_an_enormous_value_is_clamped(self):
        self.assertEqual(
            xero_client.retry_after_seconds("1000000000000"),
            xero_client.RETRY_AFTER_CLAMP,
        )
        self.assertEqual(
            xero_client.retry_after_seconds("10" * 400),
            xero_client.RETRY_AFTER_CLAMP,
        )

    def test_a_value_past_the_int_conversion_limit_is_clamped(self):
        """int() raises ValueError above 4300 digits, which nothing catches."""
        self.assertEqual(
            xero_client.retry_after_seconds("9" * 5000),
            xero_client.RETRY_AFTER_CLAMP,
        )

    def test_leading_zeros_do_not_look_like_a_huge_value(self):
        self.assertEqual(xero_client.retry_after_seconds("0000000017"), 17)
        self.assertEqual(xero_client.retry_after_seconds("000"), 0)

    def test_a_far_future_http_date_is_clamped_too(self):
        """The synthesis point: main's HTTP-date branch feeds the same clamp."""
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        retry_at = now + timedelta(days=400)
        value = format_datetime(retry_at, usegmt=True)
        self.assertEqual(
            xero_client.retry_after_seconds(value, now=now),
            xero_client.RETRY_AFTER_CLAMP,
        )

    def test_api_get_retries_a_429_with_an_http_date(self):
        responses = iter(
            (
                _Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
                _Response(200, payload={"ok": True}),
            )
        )
        with (
            patch.object(xero_client, "get_access_token", return_value="access-token"),
            patch.object(xero_client.requests, "get", side_effect=lambda *args, **kwargs: next(responses)) as get,
            patch.object(xero_client.time, "sleep") as sleep,
        ):
            self.assertEqual(xero_client.api_get("https://example.invalid", ("id", "secret")), {"ok": True})

        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(0)


class ApiGet429Test(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            xero_client, "get_access_token", return_value="tok"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_junk_header_sleeps_the_default_and_retries(self):
        responses = [
            _Response(429, headers={"Retry-After": "who knows"}),
            _Response(200, payload={"ok": True}),
        ]
        with mock.patch.object(
            xero_client.requests, "get", side_effect=responses
        ) as get, mock.patch.object(xero_client.time, "sleep") as sleep:
            result = xero_client.api_get("https://example.test", ("id", "secret"))

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(xero_client.RETRY_AFTER_DEFAULT)
        self.assertEqual(get.call_count, 2)

    def test_a_wait_over_the_cap_exits_without_sleeping(self):
        over = xero_client.RETRY_AFTER_MAX + 1
        with mock.patch.object(
            xero_client.requests,
            "get",
            return_value=_Response(429, headers={"Retry-After": str(over)}),
        ), mock.patch.object(xero_client.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as ctx:
                xero_client.api_get("https://example.test", ("id", "secret"))

        message = str(ctx.exception)
        sleep.assert_not_called()
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn(str(over), message)
        self.assertIn("resets at", message)

    def test_an_http_date_past_the_cap_exits_too(self):
        """The synthesis point: main's HTTP-date parse feeds the same cap."""
        retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
        value = format_datetime(retry_at, usegmt=True)
        with mock.patch.object(
            xero_client.requests,
            "get",
            return_value=_Response(429, headers={"Retry-After": value}),
        ), mock.patch.object(xero_client.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as ctx:
                xero_client.api_get("https://example.test", ("id", "secret"))

        message = str(ctx.exception)
        sleep.assert_not_called()
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("resets at", message)
        self.assertIn("GMT", message)  # the header itself is quoted back

    def test_an_enormous_retry_after_exits_instead_of_overflowing(self):
        """The cap message does date arithmetic on the parsed wait.

        Unclamped, timedelta(seconds=10**12) raises OverflowError as a
        traceback, which is the opposite of the clean exit the cap promises.
        """
        with mock.patch.object(
            xero_client.requests,
            "get",
            return_value=_Response(429, headers={"Retry-After": "1000000000000"}),
        ), mock.patch.object(xero_client.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as ctx:
                xero_client.api_get("https://example.test", ("id", "secret"))

        message = str(ctx.exception)
        sleep.assert_not_called()
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("resets at", message)

    def test_a_second_429_exits_instead_of_raising_for_status(self):
        responses = [
            _Response(429, headers={"Retry-After": "5"}),
            _Response(429, headers={"Retry-After": "5"}),
        ]
        with mock.patch.object(
            xero_client.requests, "get", side_effect=responses
        ), mock.patch.object(xero_client.time, "sleep"):
            with self.assertRaises(SystemExit) as ctx:
                xero_client.api_get("https://example.test", ("id", "secret"))
        self.assertTrue(str(ctx.exception).startswith("error: "), str(ctx.exception))


class RetryAfterMessageTruthTest(unittest.TestCase):
    """The cap message must report the header, not the clamp.

    retry_after_seconds clamps, and a message that then printed the clamped
    number as the value the server asked for would tell anyone debugging the
    header a figure Xero never sent.
    """

    def setUp(self):
        patcher = mock.patch.object(
            xero_client, "get_access_token", return_value="tok"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _exit_message(self, header):
        with mock.patch.object(
            xero_client.requests,
            "get",
            return_value=_Response(429, headers={"Retry-After": header}),
        ), mock.patch.object(xero_client.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as ctx:
                xero_client.api_get("https://example.test", ("id", "secret"))
        sleep.assert_not_called()
        return str(ctx.exception)

    def test_the_header_the_server_sent_is_quoted_back(self):
        message = self._exit_message("1000000000000")
        self.assertIn("1000000000000", message)
        self.assertIn(f"{xero_client.RETRY_AFTER_MAX}s cap", message)

    def test_the_clamp_is_named_as_a_clamp_not_as_the_reset(self):
        message = self._exit_message("1000000000000")
        self.assertIn("clamp", message)
        self.assertIn("may", message)

    def test_a_hostile_header_never_gets_as_far_as_the_message(self):
        """retry_after_seconds refuses anything outside the grammar and
        returns the default, so a header carrying escape sequences takes the
        retry path instead of the cap message. The strip in that message is
        defence in depth for a future grammar change, not a live path - said
        here so the next reader does not take it for a proven one."""
        responses = [
            _Response(429, headers={"Retry-After": "999999\x1b[2J\nWARNING: fake"}),
            _Response(200, payload={"ok": True}),
        ]
        with mock.patch.object(
            xero_client.requests, "get", side_effect=responses
        ), mock.patch.object(xero_client.time, "sleep") as sleep:
            result = xero_client.api_get("https://example.test", ("id", "secret"))
        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(xero_client.RETRY_AFTER_DEFAULT)


class ApiGet401Test(unittest.TestCase):
    """The surprise-401 path: the local expiry math can lie (skewed clock, a
    token.json copied between machines), so one forced refresh and one retry
    stand between that and a traceback."""

    def test_a_surprise_401_forces_a_refresh_and_retries_once(self):
        responses = [_Response(401), _Response(200, payload={"ok": True})]
        tokens = iter(["stale-token", "fresh-token"])
        with mock.patch.object(
            xero_client, "get_access_token", side_effect=lambda *a, **k: next(tokens)
        ) as get_token, mock.patch.object(
            xero_client.requests, "get", side_effect=responses
        ) as get:
            result = xero_client.api_get(
                "https://example.test", ("id", "secret"), tenant_id="tenant-guid"
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get_token.call_count, 2)
        self.assertNotIn("force", get_token.call_args_list[0].kwargs)
        self.assertIs(get_token.call_args_list[1].kwargs.get("force"), True)
        self.assertEqual(
            get.call_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer fresh-token",
        )

    def test_a_second_401_exits_with_reauthorise_guidance(self):
        with mock.patch.object(xero_client, "get_access_token", return_value="tok"), \
                mock.patch.object(
                    xero_client.requests, "get", side_effect=[_Response(401), _Response(401)]
                ):
            with self.assertRaises(SystemExit) as ctx:
                xero_client.api_get("https://example.test", ("id", "secret"))
        self.assertIn("python auth.py", str(ctx.exception))

    def test_every_request_carries_the_tenant_header_and_a_timeout(self):
        with mock.patch.object(xero_client, "get_access_token", return_value="tok"), \
                mock.patch.object(
                    xero_client.requests, "get", return_value=_Response(200, payload={"ok": True})
                ) as get:
            xero_client.api_get(
                "https://example.test", ("id", "secret"), tenant_id="tenant-guid"
            )
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Xero-tenant-id"], "tenant-guid")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(kwargs["timeout"], 30)


class SaveTokensTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.token_file = os.path.join(self.dir, "token.json")
        patcher = mock.patch.object(xero_client, "TOKEN_FILE", self.token_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _temp_files(self):
        return sorted(f for f in os.listdir(self.dir) if f.endswith(".tmp"))

    def _write_cache(self):
        cached = {
            "access_token": "OLD-A",
            "refresh_token": "OLD-R",
            "expires_in": 1800,
            "obtained_at": 0.0,
        }
        with open(self.token_file, "w") as destination:
            json.dump(cached, destination)
        with open(self.token_file, "rb") as source:
            return source.read()

    def test_refresh_without_a_usable_pair_leaves_the_cache_byte_identical(self):
        # Driven through get_access_token, not the validator, so the test fails
        # if validation is ever dropped from the refresh path.
        for payload in (None, {"access_token": "A", "expires_in": 1800}, {"refresh_token": "R"}):
            with self.subTest(payload=payload):
                before = self._write_cache()
                response = _Response(200, payload=payload)
                with mock.patch.object(xero_client.time, "time", return_value=10_000.0), \
                        mock.patch.object(xero_client.requests, "post", return_value=response), \
                        self.assertRaises(SystemExit):
                    xero_client.get_access_token("client", "secret")
                with open(self.token_file, "rb") as source:
                    self.assertEqual(source.read(), before)
                self.assertEqual(self._temp_files(), [])

    def test_refresh_with_an_unusable_expires_in_still_persists_the_rotated_pair(self):
        # Xero has already spent OLD-R by the time this response lands, so
        # discarding NEW-R over a bad cache hint would lock the account out.
        for bad in ("1800", 0, None, True, 999_999):
            with self.subTest(expires_in=bad):
                self._write_cache()
                refreshed = {"access_token": "NEW-A", "refresh_token": "NEW-R", "expires_in": bad}
                response = _Response(200, payload=refreshed)
                with mock.patch.object(xero_client.time, "time", return_value=10_000.0), \
                        mock.patch.object(xero_client.requests, "post", return_value=response):
                    access_token = xero_client.get_access_token("client", "secret")
                self.assertEqual(access_token, "NEW-A")
                with open(self.token_file) as source:
                    saved = json.load(source)
                self.assertEqual(saved["refresh_token"], "NEW-R")
                self.assertEqual(saved["expires_in"], xero_client.DEFAULT_EXPIRES_IN)

    def test_malformed_cached_token_is_rejected_before_any_network_call(self):
        for cached in (
            None,
            {"access_token": "A", "expires_in": 1800, "obtained_at": 0.0},
            {"access_token": "A", "refresh_token": "R", "expires_in": "1800", "obtained_at": 0.0},
            {"access_token": "A", "refresh_token": "R", "expires_in": 1800},
        ):
            with self.subTest(cached=cached):
                with open(self.token_file, "w") as destination:
                    json.dump(cached, destination)
                with mock.patch.object(xero_client.requests, "post") as post, \
                        self.assertRaises(SystemExit):
                    xero_client.get_access_token("client", "secret")
                post.assert_not_called()

    def test_well_formed_network_and_cached_tokens_are_accepted(self):
        token = {"access_token": "A", "refresh_token": "R", "expires_in": 1800}
        self.assertIs(xero_client.validate_token_response(token, label="test"), token)
        token["obtained_at"] = 1.0
        self.assertIs(xero_client.validate_token_response(token, label="test", cached=True), token)

    def test_future_cached_timestamp_forces_a_refresh_instead_of_looking_fresh(self):
        cached = {
            "access_token": "A",
            "refresh_token": "R",
            "expires_in": 1800,
            "obtained_at": 10_000.0,
        }
        with open(self.token_file, "w") as destination:
            json.dump(cached, destination)
        refreshed = {"access_token": "NEW-A", "refresh_token": "NEW-R", "expires_in": 1800}
        response = _Response(200, payload=refreshed)

        with mock.patch.object(xero_client.time, "time", return_value=1_000.0), \
                mock.patch.object(xero_client.requests, "post", return_value=response) as post:
            access_token = xero_client.get_access_token("client", "secret")

        self.assertEqual(access_token, "NEW-A")
        post.assert_called_once()
        with open(self.token_file) as source:
            saved = json.load(source)
        self.assertEqual(saved["refresh_token"], "NEW-R")
        self.assertEqual(saved["obtained_at"], 1_000.0)

    def test_success_writes_token_file_and_leaves_no_temp(self):
        xero_client.save_tokens({"refresh_token": "NEW", "access_token": "A"})
        with open(self.token_file) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["refresh_token"], "NEW")
        self.assertIn("obtained_at", saved)
        self.assertEqual(self._temp_files(), [])

    def test_replace_retries_and_succeeds_on_a_transient_lock(self):
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst)

        with mock.patch.object(xero_client.os, "replace", side_effect=flaky), \
                mock.patch.object(xero_client.time, "sleep") as sleep:
            xero_client.save_tokens({"refresh_token": "NEW"})

        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [c.args[0] for c in sleep.call_args_list],
            [xero_client.REPLACE_BACKOFF * 1, xero_client.REPLACE_BACKOFF * 2],
        )
        with open(self.token_file) as fh:
            self.assertEqual(json.load(fh)["refresh_token"], "NEW")
        self.assertEqual(self._temp_files(), [])

    def test_permanent_replace_failure_keeps_the_new_token_on_disk(self):
        with open(self.token_file, "w") as fh:
            json.dump({"refresh_token": "OLD-CONSUMED"}, fh)

        err = PermissionError(32, "The process cannot access the file")
        with mock.patch.object(xero_client.os, "replace", side_effect=err), \
                mock.patch.object(xero_client.time, "sleep"):
            with self.assertRaises(SystemExit) as ctx:
                xero_client.save_tokens({"refresh_token": "NEW"})

        message = str(ctx.exception)
        leftovers = self._temp_files()
        self.assertEqual(
            len(leftovers), 1, "the only copy of the new refresh token was deleted"
        )
        tmp_path = os.path.join(self.dir, leftovers[0])
        with open(tmp_path) as fh:
            self.assertEqual(json.load(fh)["refresh_token"], "NEW")
        # token.json must be left holding the old pair, untouched.
        with open(self.token_file) as fh:
            self.assertEqual(json.load(fh)["refresh_token"], "OLD-CONSUMED")
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn(tmp_path, message)
        self.assertNotIn("NEW", message)

    def test_an_fsync_failure_keeps_the_new_token_on_disk(self):
        """By the flush the temp file is whole, so it must outlive the fsync.

        Deleting it here loses the only copy of the refresh token Xero just
        issued, and leaves token.json holding the one Xero has consumed.
        """
        with open(self.token_file, "w") as fh:
            json.dump({"refresh_token": "OLD-CONSUMED"}, fh)

        err = OSError(5, "Input/output error")
        with mock.patch.object(xero_client.os, "fsync", side_effect=err):
            with self.assertRaises(SystemExit) as ctx:
                xero_client.save_tokens({"refresh_token": "NEW"})

        message = str(ctx.exception)
        leftovers = self._temp_files()
        self.assertEqual(
            len(leftovers), 1, "the only copy of the new refresh token was deleted"
        )
        tmp_path = os.path.join(self.dir, leftovers[0])
        with open(tmp_path) as fh:
            self.assertEqual(json.load(fh)["refresh_token"], "NEW")
        with open(self.token_file) as fh:
            self.assertEqual(json.load(fh)["refresh_token"], "OLD-CONSUMED")
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn(tmp_path, message)
        self.assertIn("python auth.py", message)
        self.assertNotIn("NEW", message)

    def test_a_half_written_temp_file_is_cleaned_up(self):
        with mock.patch.object(
            xero_client.json, "dump", side_effect=ValueError("boom")
        ):
            with self.assertRaises(ValueError):
                xero_client.save_tokens({"refresh_token": "NEW"})
        self.assertEqual(self._temp_files(), [])


class RefreshRejectionTest(unittest.TestCase):
    """The two everyday refresh failures must read as instructions, not as
    HTTPError tracebacks out of a scheduled task's log."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.token_file = os.path.join(self.dir, "token.json")
        patcher = mock.patch.object(xero_client, "TOKEN_FILE", self.token_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        cached = {
            "access_token": "OLD-A",
            "refresh_token": "OLD-R",
            "expires_in": 1800,
            "obtained_at": 0.0,
        }
        with open(self.token_file, "w") as destination:
            json.dump(cached, destination)
        with open(self.token_file, "rb") as source:
            self.before = source.read()

    def _refresh(self, response):
        with mock.patch.object(xero_client.time, "time", return_value=10_000.0), \
                mock.patch.object(xero_client.requests, "post", return_value=response):
            with self.assertRaises(SystemExit) as ctx:
                xero_client.get_access_token("client", "secret")
        with open(self.token_file, "rb") as source:
            self.assertEqual(source.read(), self.before, "the token cache was rewritten")
        return str(ctx.exception)

    def test_a_spent_refresh_token_says_so_and_points_at_auth(self):
        message = self._refresh(
            _Response(400, text='{"error":"invalid_grant"}')
        )
        self.assertIn("Refresh token rejected", message)
        self.assertIn("python auth.py", message)

    def test_a_rejected_client_secret_points_at_the_env_file(self):
        message = self._refresh(_Response(401, text=""))
        self.assertIn("XERO_CLIENT_SECRET", message)
        self.assertIn(".env", message)
        self.assertNotIn("OLD-R", message)


if __name__ == "__main__":
    unittest.main()
