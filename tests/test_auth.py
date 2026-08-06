import unittest

from auth import callback_server_config


class CallbackServerConfigTests(unittest.TestCase):
    def test_defaults_an_http_uri_without_a_port_to_port_80(self):
        self.assertEqual(
            callback_server_config("http://localhost/callback"),
            ("localhost", 80, "/callback"),
        )

    def test_preserves_an_explicit_port_and_uses_root_when_path_is_empty(self):
        self.assertEqual(
            callback_server_config("http://127.0.0.1:8400"),
            ("127.0.0.1", 8400, "/"),
        )

    def test_rejects_a_scheme_the_plain_http_listener_cannot_serve(self):
        with self.assertRaisesRegex(ValueError, "must use http"):
            callback_server_config("https://localhost/callback")

    def test_rejects_a_uri_without_a_host(self):
        with self.assertRaisesRegex(ValueError, "must include a host"):
            callback_server_config("http:///callback")

    def test_rejects_port_zero(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            callback_server_config("http://localhost:0/callback")


if __name__ == "__main__":
    unittest.main()
