from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


SCRIPT_PATH = Path(__file__).with_name("run_source_link_e2e.py")
SPEC = importlib.util.spec_from_file_location(
    "run_source_link_e2e",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
e2e = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = e2e
SPEC.loader.exec_module(e2e)


class _FixtureHandler(BaseHTTPRequestHandler):
    marker = e2e.GITHUB_MARKER.encode("utf-8")
    issue_marker = e2e.REDMINE_MARKER

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        if path == "/ok":
            self._send(200, self.marker, "text/plain")
            return
        if path.endswith("/missing-fixture.txt"):
            self._send(404, b"missing", "text/plain")
            return
        if "/blob/" in path:
            self._send(200, self.marker, "text/plain")
            return
        if path == "/issues/123.json":
            self._send(
                200,
                json.dumps(
                    {
                        "issue": {
                            "id": 123,
                            "subject": self.issue_marker,
                        }
                    }
                ).encode("utf-8"),
                "application/json",
            )
            return
        if path == "/issues/123":
            self._send(
                200,
                self.issue_marker.encode("utf-8"),
                "text/html",
            )
            return
        if path.startswith("/issues/"):
            self._send(404, b"missing", "text/plain")
            return
        self._send(404, b"missing", "text/plain")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SourceLinkE2ERunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _FixtureHandler,
        )
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.thread.join(timeout=3)
        cls.server.server_close()

    def test_http_get_redirect_and_missing_contract(self) -> None:
        success = e2e.fetch_url(self.base_url + "/ok")
        redirected = e2e.fetch_url(self.base_url + "/redirect")
        missing = e2e.fetch_url(self.base_url + "/missing")
        self.assertEqual(200, success.status)
        self.assertEqual(200, redirected.status)
        self.assertEqual(1, redirected.redirects)
        self.assertEqual(404, missing.status)

    def test_github_suite_has_six_passing_cases(self) -> None:
        args = argparse.Namespace(
            repository_url=self.base_url + "/owner/repository",
            ref="source-link-fixture/v1",
            commit="a" * 40,
            repository_path_prefix=e2e.FIXTURE_REPOSITORY_PATH,
            url_reporting="redacted",
        )
        records = e2e.run_github(args)
        self.assertEqual(6, len(records))
        self.assertTrue(all(record["passed"] for record in records))
        self.assertFalse(any("generated_url" in record for record in records))

    def test_redmine_suite_has_five_passing_cases(self) -> None:
        args = argparse.Namespace(
            redmine_base_url=self.base_url,
            issue_id=123,
            issue_marker=e2e.REDMINE_MARKER,
            url_reporting="redacted",
        )
        records = e2e.run_redmine(args)
        self.assertEqual(5, len(records))
        self.assertTrue(all(record["passed"] for record in records))
        self.assertFalse(any("generated_url" in record for record in records))


if __name__ == "__main__":
    unittest.main()
