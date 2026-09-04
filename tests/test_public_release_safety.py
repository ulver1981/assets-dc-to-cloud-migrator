import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import demo_offline
import migration_http
import release_readiness


class PublicReleaseSafetyTests(unittest.TestCase):
    def test_readiness_accepts_generic_placeholder_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env.template").write_text(
                "DC_TOKEN=YOUR_DC_PERSONAL_ACCESS_TOKEN\n"
                "DC_BASE_URL=https://jira.example.com/rest/insight/1.0\n",
                encoding="utf-8",
            )
            self.assertEqual(release_readiness.inspect_release_tree(root), [])

    def test_readiness_reports_paths_without_secret_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "logs").mkdir()
            (root / "logs" / "run.log").write_text(
                "DC_TOKEN=" + "synthetic-secret-value", encoding="utf-8"
            )
            findings = release_readiness.inspect_release_tree(root)
            self.assertEqual([(item.path.as_posix(), item.category) for item in findings],
                             [("logs", "prohibited path"),
                              ("logs/run.log", "prohibited path")])

    def test_readiness_detects_non_generic_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.txt").write_text(
                "DC_BASE_URL=https" + "://jira.internal.example/rest", encoding="utf-8"
            )
            findings = release_readiness.inspect_release_tree(root)
            self.assertEqual(findings[0].category, "non-generic endpoint")

    def test_http_errors_redact_embedded_credentials_query_and_body(self):
        session = Mock()
        response = Mock()
        response.status_code = 401
        response.reason = "Unauthorized"
        response.text = "Bearer " + "response-token-must-not-appear"
        response.raise_for_status.side_effect = __import__("requests").HTTPError()
        session.request.return_value = response

        with self.assertRaises(__import__("requests").HTTPError) as raised:
            migration_http.request_json(
                session, "GET",
                "https" + "://operator:api-token@example.com/path?token=query-secret",
            )

        message = str(raised.exception)
        self.assertIn("GET https://example.com/path failed with 401 Unauthorized", message)
        self.assertNotIn("api-token", message)
        self.assertNotIn("query-secret", message)
        self.assertNotIn("response-token", message)

    def test_offline_demo_runs_without_configuration_or_network(self):
        report = demo_offline.run_demo()
        self.assertEqual(report["counts"]["object_types_in_schema"], 3)
        self.assertEqual(report["counts"]["csv_recursive_files"], 3)


if __name__ == "__main__":
    unittest.main()
