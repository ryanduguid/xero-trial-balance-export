import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
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


if __name__ == "__main__":
    unittest.main()
