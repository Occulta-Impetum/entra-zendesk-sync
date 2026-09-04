from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lib import zendesk


class ZendeskTokenCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tempdir.name) / "zendesk_oauth_tokens.json"
        self.config = {
            "subdomain": "example",
            "client_id": "client-123",
            "client_secret": "secret",
            "scope": "users:read",
        }
        self.cache_patch = patch.object(zendesk, "TOKEN_CACHE_PATH", self.cache_path)
        self.cache_patch.start()

    def tearDown(self) -> None:
        self.cache_patch.stop()
        self.tempdir.cleanup()

    def _response(self, *, token: str = "new-token", expires_in: int = 1800, scope: str = "users:read") -> Mock:
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "scope": scope,
        }
        return response

    @patch("lib.zendesk.requests.post")
    def test_first_request_is_cached_and_second_request_reuses_token(self, post: Mock) -> None:
        post.return_value = self._response()

        token1, data1 = zendesk.get_access_token(self.config, scope="users:read")
        token2, data2 = zendesk.get_access_token(self.config, scope="users:read")

        self.assertEqual("new-token", token1)
        self.assertEqual("new-token", token2)
        self.assertFalse(data1.get("cached", False))
        self.assertTrue(data2.get("cached"))
        self.assertEqual(1, post.call_count)
        self.assertTrue(self.cache_path.is_file())

    @patch("lib.zendesk.requests.post")
    def test_scope_sets_are_cached_separately(self, post: Mock) -> None:
        post.side_effect = [
            self._response(token="read-token", scope="users:read"),
            self._response(
                token="write-token",
                scope="users:read users:write",
            ),
        ]

        read_token, _ = zendesk.get_access_token(self.config, scope="users:read")
        write_token, _ = zendesk.get_access_token(
            self.config,
            scope="users:write users:read",
        )
        read_token_again, _ = zendesk.get_access_token(self.config, scope="users:read")

        self.assertEqual("read-token", read_token)
        self.assertEqual("write-token", write_token)
        self.assertEqual("read-token", read_token_again)
        self.assertEqual(2, post.call_count)

    @patch("lib.zendesk.requests.post")
    def test_nearly_expired_token_is_not_reused(self, post: Mock) -> None:
        normalized = zendesk._normalize_scope("users:read")
        key = zendesk._token_cache_key(self.config, normalized)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        key: {
                            "access_token": "old-token",
                            "expires_at": time.time() + 30,
                            "scope": normalized,
                            "token_type": "bearer",
                            "subdomain": "example",
                            "client_id": "client-123",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        post.return_value = self._response(token="replacement-token")

        token, _ = zendesk.get_access_token(self.config, scope="users:read")

        self.assertEqual("replacement-token", token)
        self.assertEqual(1, post.call_count)

    @patch("lib.zendesk.requests.post")
    def test_force_new_bypasses_valid_cache(self, post: Mock) -> None:
        post.side_effect = [
            self._response(token="first-token"),
            self._response(token="second-token"),
        ]

        first, _ = zendesk.get_access_token(self.config, scope="users:read")
        second, _ = zendesk.get_access_token(
            self.config,
            scope="users:read",
            force_new=True,
        )

        self.assertEqual("first-token", first)
        self.assertEqual("second-token", second)
        self.assertEqual(2, post.call_count)


if __name__ == "__main__":
    unittest.main()
