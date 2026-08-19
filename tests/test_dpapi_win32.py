"""Windows-only DPAPI round-trip tests for the token cache primitives.

These tests call the real CryptProtectData/CryptUnprotectData functions
through xero_client's own wrappers. They must never be mocked: the point
is that the windows-latest CI leg exercises the live API, while every
other platform collects and skips them.
"""

import sys
import unittest

import xero_client


@unittest.skipUnless(sys.platform == "win32", "requires the real Windows DPAPI")
class WindowsDpapiRoundTripTest(unittest.TestCase):
    def test_protect_then_unprotect_returns_the_original_bytes(self):
        plaintext = b"synthetic-win32-round-trip-7302 \x00\xff binary tail"
        protected = xero_client._dpapi_protect(plaintext)
        self.assertEqual(xero_client._dpapi_unprotect(protected), plaintext)

    def test_protected_bytes_are_not_the_plaintext(self):
        plaintext = b"synthetic-win32-ciphertext-check-5518"
        protected = xero_client._dpapi_protect(plaintext)
        self.assertNotEqual(protected, plaintext)
        self.assertNotIn(plaintext, protected)

    def test_unprotect_of_garbage_raises_the_documented_error(self):
        with self.assertRaises(SystemExit) as raised:
            xero_client._dpapi_unprotect(b"not-a-dpapi-envelope")
        self.assertIn("corrupt or cannot be opened", str(raised.exception))

    def test_unprotect_of_a_flipped_bit_fails_closed(self):
        protected = bytearray(
            xero_client._dpapi_protect(b"synthetic-win32-tamper-check-9944")
        )
        protected[-1] ^= 0x01
        with self.assertRaises(SystemExit) as raised:
            xero_client._dpapi_unprotect(bytes(protected))
        self.assertIn("corrupt or cannot be opened", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
