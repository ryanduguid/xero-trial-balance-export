import json
import multiprocessing
import os
import stat
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


class _NonJsonResponse(_Response):
    """A 200 whose body is not JSON.

    What a captive portal, a proxy sign-in page or an HTML maintenance page
    looks like from here: the status says success and resp.json() raises
    ValueError. requests raises its own JSONDecodeError, which subclasses
    ValueError, so this stands in for it without importing the private class.
    """

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def _concurrent_refresh_worker(
    token_file,
    worker_started,
    refresh_started,
    allow_refresh,
    post_calls,
    results,
):
    """Run one synthetic refresh in a spawned process."""
    xero_client.TOKEN_FILE = token_file

    def post(*args, **kwargs):
        with post_calls.get_lock():
            post_calls.value += 1
        refresh_started.set()
        if not allow_refresh.wait(10):
            raise RuntimeError("test timed out waiting to finish the refresh")
        return _Response(
            200,
            payload={
                "access_token": "NEW-A",
                "refresh_token": "NEW-R",
                "expires_in": 1800,
            },
        )

    xero_client.requests.post = post
    worker_started.set()
    try:
        token = xero_client.get_access_token("client", "secret")
    except BaseException as exc:
        results.put(("error", type(exc).__name__))
    else:
        results.put(("ok", token))


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

    def test_a_value_just_over_the_clamp_is_clamped_too(self):
        """The digit-count preflight only refuses more digits than the clamp
        has, so everything from 86401 to 99999 reaches the min() - which is
        the only thing holding those values down."""
        self.assertEqual(
            xero_client.retry_after_seconds("86401"), xero_client.RETRY_AFTER_CLAMP
        )
        self.assertEqual(
            xero_client.retry_after_seconds("99999"), xero_client.RETRY_AFTER_CLAMP
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

    def _read_cache(self, path=None):
        with open(path or self.token_file, "rb") as source:
            return xero_client._decode_token_cache(source.read())[0]

    def _write_cache(self):
        cached = {
            "access_token": "OLD-A",
            "refresh_token": "OLD-R",
            "expires_in": 1800,
            "obtained_at": 0.0,
        }
        xero_client._persist_token_cache_unlocked(cached, legacy_migration=False)
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
                saved = self._read_cache()
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
        saved = self._read_cache()
        self.assertEqual(saved["refresh_token"], "NEW-R")
        self.assertEqual(saved["obtained_at"], 1_000.0)

    def test_success_writes_token_file_and_leaves_no_temp(self):
        xero_client.save_tokens({"refresh_token": "NEW", "access_token": "A"})
        saved = self._read_cache()
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
        self.assertEqual(self._read_cache()["refresh_token"], "NEW")
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
        self.assertEqual(self._read_cache(tmp_path)["refresh_token"], "NEW")
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
        self.assertEqual(self._read_cache(tmp_path)["refresh_token"], "NEW")
        with open(self.token_file) as fh:
            self.assertEqual(json.load(fh)["refresh_token"], "OLD-CONSUMED")
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn(tmp_path, message)
        self.assertIn("python auth.py", message)
        self.assertNotIn("NEW", message)

    def test_a_half_written_temp_file_is_cleaned_up(self):
        with mock.patch.object(
            xero_client.json, "dumps", side_effect=ValueError("boom")
        ):
            with self.assertRaises(ValueError):
                xero_client.save_tokens({"refresh_token": "NEW"})
        self.assertEqual(self._temp_files(), [])


class TokenCacheProtectionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.token_file = os.path.join(self.temp_dir.name, "token.json")
        patcher = mock.patch.object(xero_client, "TOKEN_FILE", self.token_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tokens = {
            "access_token": "SYNTHETIC-ACCESS-UNIQUE-2904",
            "refresh_token": "SYNTHETIC-REFRESH-UNIQUE-8173",
            "expires_in": 1800,
            "obtained_at": 1_000.25,
        }

    def _write_legacy(self):
        raw = json.dumps(self.tokens).encode("utf-8")
        with open(self.token_file, "wb") as destination:
            destination.write(raw)
        return raw

    def _raw(self, path=None):
        with open(path or self.token_file, "rb") as source:
            return source.read()

    @unittest.skipUnless(os.name == "nt", "requires real Windows DPAPI")
    def test_real_dpapi_round_trip_and_wrong_payloads_fail_closed(self):
        plaintext = b"synthetic-dpapi-round-trip-4591"
        protected = xero_client._dpapi_protect(plaintext)
        self.assertNotEqual(protected, plaintext)
        self.assertNotIn(plaintext, protected)
        self.assertEqual(xero_client._dpapi_unprotect(protected), plaintext)

        mutated = bytearray(protected)
        mutated[-1] ^= 0x01
        for wrong in (b"not-a-dpapi-payload", bytes(mutated)):
            with self.subTest(length=len(wrong)), self.assertRaises(SystemExit) as raised:
                xero_client._dpapi_unprotect(wrong)
            self.assertIn("corrupt or cannot be opened", str(raised.exception))

    @unittest.skipUnless(os.name == "nt", "Windows cache is DPAPI-protected")
    def test_windows_cache_and_recovery_temp_never_hold_plaintext_tokens(self):
        xero_client._persist_token_cache_unlocked(
            self.tokens, legacy_migration=False
        )
        raw = self._raw()
        self.assertNotIn(self.tokens["access_token"].encode(), raw)
        self.assertNotIn(self.tokens["refresh_token"].encode(), raw)
        envelope = json.loads(raw)
        self.assertEqual(envelope["format"], xero_client.TOKEN_CACHE_FORMAT)
        self.assertEqual(envelope["version"], xero_client.TOKEN_CACHE_VERSION)
        self.assertEqual(envelope["protection"], xero_client.TOKEN_CACHE_PROTECTION)

        old_raw = raw
        replacement = dict(self.tokens)
        replacement["access_token"] = "SYNTHETIC-REPLACEMENT-ACCESS-6428"
        replacement["refresh_token"] = "SYNTHETIC-REPLACEMENT-REFRESH-1736"
        with mock.patch.object(
            xero_client.os,
            "replace",
            side_effect=PermissionError(32, "destination is held"),
        ), mock.patch.object(xero_client.time, "sleep"):
            with self.assertRaises(SystemExit):
                xero_client._persist_token_cache_unlocked(
                    replacement, legacy_migration=False
                )

        self.assertEqual(self._raw(), old_raw)
        temps = [
            os.path.join(self.temp_dir.name, name)
            for name in os.listdir(self.temp_dir.name)
            if name.endswith(".tmp")
        ]
        self.assertEqual(len(temps), 1)
        temp_raw = self._raw(temps[0])
        for value in (
            self.tokens["access_token"],
            self.tokens["refresh_token"],
            replacement["access_token"],
            replacement["refresh_token"],
        ):
            self.assertNotIn(value.encode(), temp_raw)
        decoded, legacy = xero_client._decode_token_cache(temp_raw)
        self.assertFalse(legacy)
        self.assertEqual(decoded, replacement)

    @unittest.skipUnless(os.name == "nt", "legacy migration targets Windows")
    def test_valid_legacy_cache_migrates_before_any_network_call(self):
        original_obtained_at = self.tokens["obtained_at"]
        legacy_raw = self._write_legacy()

        with mock.patch.object(xero_client.time, "time", return_value=1_100.0), \
                mock.patch.object(xero_client.requests, "post") as post:
            access_token = xero_client.get_access_token("client", "secret")

        self.assertEqual(access_token, self.tokens["access_token"])
        post.assert_not_called()
        migrated = self._raw()
        self.assertNotEqual(migrated, legacy_raw)
        self.assertNotIn(self.tokens["access_token"].encode(), migrated)
        self.assertNotIn(self.tokens["refresh_token"].encode(), migrated)
        decoded, legacy = xero_client._decode_token_cache(migrated)
        self.assertFalse(legacy)
        self.assertEqual(decoded["obtained_at"], original_obtained_at)
        self.assertEqual(decoded, self.tokens)

    @unittest.skipUnless(os.name == "nt", "legacy migration targets Windows")
    def test_failed_legacy_migration_preserves_source_and_makes_no_request(self):
        legacy_raw = self._write_legacy()
        error = PermissionError(32, "destination is held")
        with mock.patch.object(xero_client.os, "replace", side_effect=error), \
                mock.patch.object(xero_client.time, "sleep"), \
                mock.patch.object(xero_client.requests, "post") as post, \
                self.assertRaises(SystemExit) as raised:
            xero_client.get_access_token("client", "secret")

        post.assert_not_called()
        self.assertEqual(self._raw(), legacy_raw)
        self.assertIn("no Xero request was made", str(raised.exception))
        temps = [
            os.path.join(self.temp_dir.name, name)
            for name in os.listdir(self.temp_dir.name)
            if name.endswith(".tmp")
        ]
        self.assertEqual(len(temps), 1)
        temp_raw = self._raw(temps[0])
        self.assertNotIn(self.tokens["access_token"].encode(), temp_raw)
        self.assertNotIn(self.tokens["refresh_token"].encode(), temp_raw)
        self.assertEqual(xero_client._decode_token_cache(temp_raw)[0], self.tokens)

    def test_unknown_or_corrupt_envelopes_stop_before_the_network(self):
        documents = [
            {
                "format": xero_client.TOKEN_CACHE_FORMAT,
                "version": True,
                "protection": xero_client.TOKEN_CACHE_PROTECTION,
                "payload": "AA==",
            },
            {
                "format": xero_client.TOKEN_CACHE_FORMAT,
                "version": 999,
                "protection": xero_client.TOKEN_CACHE_PROTECTION,
                "payload": "AA==",
            },
            {
                "format": xero_client.TOKEN_CACHE_FORMAT,
                "version": xero_client.TOKEN_CACHE_VERSION,
                "protection": xero_client.TOKEN_CACHE_PROTECTION,
                "payload": "not base64!",
            },
            {
                "format": xero_client.TOKEN_CACHE_FORMAT,
                "version": xero_client.TOKEN_CACHE_VERSION,
                "protection": xero_client.TOKEN_CACHE_PROTECTION,
                "payload": "AA==",
                "unexpected": True,
            },
        ]
        for document in documents:
            with self.subTest(document=document):
                raw = json.dumps(document).encode("utf-8")
                with open(self.token_file, "wb") as destination:
                    destination.write(raw)
                with mock.patch.object(xero_client.requests, "post") as post, \
                        self.assertRaises(SystemExit):
                    xero_client.get_access_token("client", "secret")
                post.assert_not_called()
                self.assertEqual(self._raw(), raw)

    def test_a_windows_envelope_is_not_treated_as_plaintext_elsewhere(self):
        document = {
            "format": xero_client.TOKEN_CACHE_FORMAT,
            "version": xero_client.TOKEN_CACHE_VERSION,
            "protection": xero_client.TOKEN_CACHE_PROTECTION,
            "payload": "AA==",
        }
        with mock.patch.object(xero_client, "_is_windows", return_value=False), \
                self.assertRaises(SystemExit) as raised:
            xero_client._decode_token_cache(json.dumps(document).encode("utf-8"))
        self.assertIn("can only be opened by the Windows user", str(raised.exception))

    @unittest.skipIf(os.name == "nt", "POSIX permission semantics required")
    def test_posix_plaintext_fallback_is_and_remains_owner_only(self):
        xero_client._persist_token_cache_unlocked(
            self.tokens, legacy_migration=False
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(self.token_file).st_mode),
            stat.S_IRUSR | stat.S_IWUSR,
        )
        self.assertIn(self.tokens["access_token"].encode(), self._raw())

        os.chmod(self.token_file, 0o644)
        self.assertEqual(xero_client.load_tokens(), self.tokens)
        self.assertEqual(
            stat.S_IMODE(os.stat(self.token_file).st_mode),
            stat.S_IRUSR | stat.S_IWUSR,
        )

    def test_a_partial_temp_write_is_removed(self):
        real_fdopen = os.fdopen

        class PartialWriter:
            def __init__(self, fd, mode):
                self._file = real_fdopen(fd, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self._file.close()

            def write(self, value):
                self._file.write(value[: max(1, len(value) // 2)])
                raise OSError(5, "synthetic partial write")

            def flush(self):
                self._file.flush()

            def fileno(self):
                return self._file.fileno()

        with mock.patch.object(xero_client.os, "fdopen", side_effect=PartialWriter), \
                self.assertRaises(OSError):
            xero_client._persist_token_cache_unlocked(
                self.tokens, legacy_migration=False
            )
        self.assertFalse(os.path.exists(self.token_file))
        self.assertFalse(
            any(name.endswith(".tmp") for name in os.listdir(self.temp_dir.name))
        )


class TokenCacheConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.token_file = os.path.join(self.temp_dir.name, "token.json")
        with open(self.token_file, "w") as destination:
            json.dump(
                {
                    "access_token": "OLD-A",
                    "refresh_token": "OLD-R",
                    "expires_in": 1800,
                    "obtained_at": 0.0,
                },
                destination,
            )

    def test_lock_timeout_stops_before_the_cache_is_read(self):
        with open(self.token_file, "rb") as source:
            before = source.read()

        with mock.patch.object(xero_client, "TOKEN_LOCK_TIMEOUT", 0), \
                mock.patch.object(xero_client, "_try_token_lock", return_value=False), \
                mock.patch.object(xero_client, "_load_tokens_unlocked") as load_tokens:
            with self.assertRaises(SystemExit) as raised:
                xero_client.get_access_token("client", "secret")

        load_tokens.assert_not_called()
        self.assertIn("another process held the token cache lock", str(raised.exception))
        with open(self.token_file, "rb") as source:
            self.assertEqual(source.read(), before)
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.temp_dir.name)))

    def test_concurrent_processes_spend_one_refresh_token(self):
        """A waiter must re-read the pair written by the lock holder."""
        context = multiprocessing.get_context("spawn")
        first_started = context.Event()
        second_started = context.Event()
        refresh_started = context.Event()
        allow_refresh = context.Event()
        post_calls = context.Value("i", 0)
        results = context.Queue()
        first = context.Process(
            target=_concurrent_refresh_worker,
            args=(
                self.token_file,
                first_started,
                refresh_started,
                allow_refresh,
                post_calls,
                results,
            ),
        )
        second = context.Process(
            target=_concurrent_refresh_worker,
            args=(
                self.token_file,
                second_started,
                refresh_started,
                allow_refresh,
                post_calls,
                results,
            ),
        )
        processes = (first, second)

        try:
            first.start()
            self.assertTrue(first_started.wait(10), "first worker did not start")
            self.assertTrue(refresh_started.wait(10), "first worker did not begin refresh")

            second.start()
            self.assertTrue(second_started.wait(10), "second worker did not start")
            second.join(0.5)
            self.assertTrue(second.is_alive(), "second worker did not wait for the cache lock")
            with post_calls.get_lock():
                self.assertEqual(
                    post_calls.value,
                    1,
                    "both processes spent the same cached refresh token",
                )

            allow_refresh.set()
            outcomes = [results.get(timeout=10), results.get(timeout=10)]
            for process in processes:
                process.join(10)

            self.assertEqual([process.exitcode for process in processes], [0, 0])
            self.assertEqual(sorted(outcomes), [("ok", "NEW-A"), ("ok", "NEW-A")])
            with post_calls.get_lock():
                self.assertEqual(post_calls.value, 1)

            with mock.patch.object(xero_client, "TOKEN_FILE", self.token_file):
                saved = xero_client.load_tokens()
            self.assertEqual(saved["refresh_token"], "NEW-R")
            with open(f"{self.token_file}.lock", "rb") as source:
                lock_bytes = source.read()
            self.assertTrue(lock_bytes)
            self.assertEqual(set(lock_bytes), {0})
        finally:
            allow_refresh.set()
            for process in processes:
                if process.pid is None:
                    continue
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join(5)

            results.close()
            results.join_thread()


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
        xero_client._persist_token_cache_unlocked(cached, legacy_migration=False)
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

    def test_a_non_json_refresh_response_leaves_the_cache_untouched(self):
        """A 200 that is not JSON is a captive portal or a proxy page, not a
        rotation. resp.json() raises ValueError, which nothing above this
        catches, so it used to be a traceback - and the refresh token in
        token.json is still the one to use, because Xero never issued a new
        pair. The _refresh helper proves the file is byte-identical after."""
        message = self._refresh(_NonJsonResponse(200))
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("non-JSON token response", message)
        self.assertIn("left untouched", message)


class ApiGetNonJsonTest(unittest.TestCase):
    """The report call's own copy of the same guard. api_get returns straight
    into export_tb's flattener, so an HTML body has to stop here rather than
    reach it as a ValueError traceback."""

    def test_a_non_json_api_response_exits_with_one_line(self):
        with mock.patch.object(xero_client, "get_access_token", return_value="tok"), \
                mock.patch.object(
                    xero_client.requests, "get", return_value=_NonJsonResponse(200)
                ):
            with self.assertRaises(SystemExit) as ctx:
                xero_client.api_get("https://example.invalid", ("id", "secret"))
        message = str(ctx.exception)
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn("non-JSON API response", message)


class ResolveTokenFileTest(unittest.TestCase):
    """resolve_token_file: XERO_TOKEN_FILE wins, then the CLI value, then
    the module-relative default. The result is always absolute so the
    ``<cache>.lock`` sibling path stays beside the cache regardless of the
    process working directory."""

    def test_env_var_overrides_everything(self):
        with mock.patch.dict(os.environ, {"XERO_TOKEN_FILE": "/tmp/env-cache/token.json"}):
            resolved = xero_client.resolve_token_file("/tmp/cli-cache/token.json")
        self.assertEqual(resolved, os.path.abspath("/tmp/env-cache/token.json"))

    def test_cli_value_used_when_env_var_absent(self):
        env = {k: v for k, v in os.environ.items() if k != "XERO_TOKEN_FILE"}
        with mock.patch.dict(os.environ, env, clear=True):
            resolved = xero_client.resolve_token_file("/tmp/cli-cache/token.json")
        self.assertEqual(resolved, os.path.abspath("/tmp/cli-cache/token.json"))

    def test_default_is_module_relative(self):
        env = {k: v for k, v in os.environ.items() if k != "XERO_TOKEN_FILE"}
        with mock.patch.dict(os.environ, env, clear=True):
            resolved = xero_client.resolve_token_file()
        self.assertEqual(resolved, xero_client.DEFAULT_TOKEN_FILE)
        self.assertEqual(
            os.path.dirname(resolved),
            os.path.dirname(os.path.abspath(xero_client.__file__)),
        )

    def test_blank_env_var_falls_through(self):
        with mock.patch.dict(os.environ, {"XERO_TOKEN_FILE": "   "}):
            resolved = xero_client.resolve_token_file("/tmp/cli-cache/token.json")
        self.assertEqual(resolved, os.path.abspath("/tmp/cli-cache/token.json"))

    def test_relative_env_value_is_made_absolute(self):
        with mock.patch.dict(os.environ, {"XERO_TOKEN_FILE": "caches/token.json"}):
            resolved = xero_client.resolve_token_file()
        self.assertTrue(os.path.isabs(resolved))
        self.assertEqual(resolved, os.path.abspath("caches/token.json"))

    def test_env_override_places_lock_beside_resolved_cache(self):
        """The lock is opened at f"{TOKEN_FILE}.lock"; with the cache resolved
        through the env var the lock lands beside it, not beside the module."""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "token.json")
            with mock.patch.dict(os.environ, {"XERO_TOKEN_FILE": cache_path}):
                resolved = xero_client.resolve_token_file()
            with mock.patch.object(xero_client, "TOKEN_FILE", resolved):
                with xero_client._token_cache_lock():
                    pass
            self.assertTrue(os.path.exists(cache_path + ".lock"))


if __name__ == "__main__":
    unittest.main()
