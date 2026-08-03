"""Tests for token persistence and the 429 backoff. Standard library only."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xero_client  # noqa: E402


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(
                f"raise_for_status reached with status {self.status_code}"
            )


class SaveTokensTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.token_file = os.path.join(self.dir, "token.json")
        patcher = mock.patch.object(xero_client, "TOKEN_FILE", self.token_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _temp_files(self):
        return sorted(f for f in os.listdir(self.dir) if f.endswith(".tmp"))

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

    def test_a_half_written_temp_file_is_cleaned_up(self):
        with mock.patch.object(
            xero_client.json, "dump", side_effect=ValueError("boom")
        ):
            with self.assertRaises(ValueError):
                xero_client.save_tokens({"refresh_token": "NEW"})
        self.assertEqual(self._temp_files(), [])


class ParseRetryAfterTest(unittest.TestCase):
    def test_delta_seconds(self):
        self.assertEqual(xero_client.parse_retry_after("17"), 17)
        self.assertEqual(xero_client.parse_retry_after(" 17 "), 17)

    def test_unparseable_values_fall_back_to_the_default(self):
        for raw in (None, "", "soon", "Wed, 21 Oct 2026 07:28:00 GMT", "1.5", "-3"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    xero_client.parse_retry_after(raw),
                    xero_client.RETRY_AFTER_DEFAULT,
                )


class ApiGet429Test(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            xero_client, "get_access_token", return_value="tok"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_junk_header_sleeps_the_default_and_retries(self):
        responses = [
            _Resp(429, {"Retry-After": "who knows"}),
            _Resp(200, payload={"ok": True}),
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
            return_value=_Resp(429, {"Retry-After": str(over)}),
        ), mock.patch.object(xero_client.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as ctx:
                xero_client.api_get("https://example.test", ("id", "secret"))

        message = str(ctx.exception)
        sleep.assert_not_called()
        self.assertTrue(message.startswith("error: "), message)
        self.assertIn(str(over), message)
        self.assertIn("resets at", message)

    def test_a_second_429_exits_instead_of_raising_for_status(self):
        responses = [_Resp(429, {"Retry-After": "5"}), _Resp(429, {"Retry-After": "5"})]
        with mock.patch.object(
            xero_client.requests, "get", side_effect=responses
        ), mock.patch.object(xero_client.time, "sleep"):
            with self.assertRaises(SystemExit) as ctx:
                xero_client.api_get("https://example.test", ("id", "secret"))
        self.assertTrue(str(ctx.exception).startswith("error: "), str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
