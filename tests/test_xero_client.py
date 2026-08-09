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
    def __init__(self, status_code, *, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"unexpected HTTP {self.status_code}")


class RetryAfterTests(unittest.TestCase):
    def test_accepts_delay_seconds(self):
        self.assertEqual(xero_client.retry_after_seconds("7"), 7)

    def test_accepts_an_http_date_and_rounds_up(self):
        now = datetime(2030, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc)
        retry_at = now.replace(microsecond=0) + timedelta(seconds=2)
        value = format_datetime(retry_at, usegmt=True)
        self.assertEqual(xero_client.retry_after_seconds(value, now=now), 2)

    def test_malformed_or_missing_values_fall_back_to_a_politeness_delay(self):
        self.assertEqual(xero_client.retry_after_seconds(None), 5)
        self.assertEqual(xero_client.retry_after_seconds("not-a-date"), 5)
        self.assertEqual(xero_client.retry_after_seconds("-1"), 0)

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


if __name__ == "__main__":
    unittest.main()
