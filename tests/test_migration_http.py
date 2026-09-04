import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import migration_http


def response(status: int, body: dict | None = None, retry_after: str | None = None):
    value = Mock()
    value.status_code = status
    value.reason = "status"
    value.text = "{}"
    value.content = b'{"ok": true}' if body is not None else b""
    value.headers = {"Retry-After": retry_after} if retry_after else {}
    value.json.return_value = body or {}
    value.raise_for_status.side_effect = (
        None if status < 400 else __import__("requests").HTTPError("failure")
    )
    return value


class RequestJsonTests(unittest.TestCase):
    @patch("migration_http.time.sleep")
    def test_retries_rate_limit_with_retry_after(self, sleep):
        session = Mock()
        session.request.side_effect = [response(429, retry_after="3"), response(200, {"ok": True})]

        result = migration_http.request_json(session, "GET", "https://example.test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(session.request.call_count, 2)
        sleep.assert_called_once_with(3)

    @patch("migration_http.time.sleep")
    def test_retries_server_error_with_bounded_backoff(self, sleep):
        session = Mock()
        session.request.side_effect = [response(503), response(200, {"ok": True})]

        result = migration_http.request_json(session, "GET", "https://example.test")

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(2)

    def test_surfaces_context_for_final_error(self):
        session = Mock()
        session.request.return_value = response(400)

        with self.assertRaisesRegex(Exception, "GET https://example.test failed with 400"):
            migration_http.request_json(session, "GET", "https://example.test")

    def test_accepts_successful_empty_response(self):
        session = Mock()
        session.request.return_value = response(200)

        result = migration_http.request_json(session, "DELETE", "https://example.test")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
