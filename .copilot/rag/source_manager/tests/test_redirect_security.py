from __future__ import annotations

import os
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Mapping
from unittest import mock

from source_manager.execution import _http_get
from source_manager.machine_connections import _default_http_get
from source_manager import networking as networking_module
from source_manager.networking import reject_http_redirects


PRIVATE_TOKEN = "gitlab-private-token-must-not-follow"
REDMINE_API_KEY = "redmine-api-key-must-not-follow"
REQUEST_HEADERS = {
    "PRIVATE-TOKEN": PRIVATE_TOKEN,
    "X-Redmine-API-Key": REDMINE_API_KEY,
}
GITLAB_HEADERS = {"PRIVATE-TOKEN": PRIVATE_TOKEN}
REDMINE_HEADERS = {"X-Redmine-API-Key": REDMINE_API_KEY}


class _RedirectServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, redirect_code: int) -> None:
        super().__init__(("127.0.0.1", 0), _RedirectHandler)
        self.redirect_code = redirect_code
        self.redirect_headers: list[dict[str, str]] = []
        self.target_headers: list[dict[str, str]] = []

    @property
    def redirect_url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}/redirect"


class _RedirectHandler(BaseHTTPRequestHandler):
    server: _RedirectServer

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.server.redirect_headers.append(dict(self.headers.items()))
            body = f"redirect-{self.server.redirect_code}".encode("ascii")
            self.send_response(self.server.redirect_code)
            self.send_header("Location", "/target")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/target":
            self.server.target_headers.append(dict(self.headers.items()))
            body = b"redirect target reached"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, _format: str, *args: Any) -> None:
        del args


@contextmanager
def _running_redirect_server(
    redirect_code: int,
) -> Iterator[_RedirectServer]:
    server = _RedirectServer(redirect_code)
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"redirect-test-{redirect_code}",
        daemon=True,
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _casefold_headers(
    headers: Mapping[str, str],
) -> dict[str, str]:
    return {
        str(name).casefold(): str(value)
        for name, value in headers.items()
    }


class RedirectCredentialSecurityTests(unittest.TestCase):
    def assert_initial_only(
        self,
        server: _RedirectServer,
        *,
        expected_headers: Mapping[str, str] = REQUEST_HEADERS,
    ) -> None:
        self.assertEqual(1, len(server.redirect_headers))
        initial = _casefold_headers(server.redirect_headers[0])
        for name, value in expected_headers.items():
            self.assertEqual(value, initial[str(name).casefold()])
        self.assertEqual([], server.target_headers)

    def test_reject_http_redirects_stops_302_and_307_before_second_request(
        self,
    ) -> None:
        for redirect_code in (302, 307):
            with self.subTest(status=redirect_code):
                with _running_redirect_server(redirect_code) as server:
                    opener = reject_http_redirects(
                        urllib.request.build_opener(
                            urllib.request.ProxyHandler({})
                        )
                    )
                    request = urllib.request.Request(
                        server.redirect_url,
                        headers=REQUEST_HEADERS,
                        method="GET",
                    )

                    with self.assertRaises(
                        urllib.error.HTTPError
                    ) as captured:
                        opener.open(request, timeout=2)

                    self.assertEqual(
                        redirect_code,
                        captured.exception.code,
                    )
                    self.assertEqual(
                        f"redirect-{redirect_code}".encode("ascii"),
                        captured.exception.read(),
                    )
                    self.assertEqual(
                        "/target",
                        captured.exception.headers["Location"],
                    )
                    self.assert_initial_only(server)

    def test_gitlab_http_clients_return_redirect_without_forwarding_token(
        self,
    ) -> None:
        clients = {
            "execution": _http_get,
            "machine_connections": _default_http_get,
        }
        for client_name, client in clients.items():
            for redirect_code in (302, 307):
                with self.subTest(
                    client=client_name,
                    status=redirect_code,
                ):
                    with (
                        mock.patch.dict(os.environ, {}, clear=True),
                        _running_redirect_server(redirect_code) as server,
                    ):
                        status, body, headers = client(
                            server.redirect_url,
                            GITLAB_HEADERS,
                            2,
                        )

                    self.assertEqual(redirect_code, status)
                    self.assertEqual(
                        f"redirect-{redirect_code}".encode("ascii"),
                        body,
                    )
                    self.assertEqual("/target", headers["Location"])
                    self.assert_initial_only(
                        server,
                        expected_headers=GITLAB_HEADERS,
                    )

    def test_execution_redmine_request_keeps_redirect_behavior(self) -> None:
        for redirect_code in (302, 307):
            with self.subTest(status=redirect_code):
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    _running_redirect_server(redirect_code) as server,
                ):
                    status, body, _headers = _http_get(
                        server.redirect_url,
                        REDMINE_HEADERS,
                        2,
                    )

                self.assertEqual(200, status)
                self.assertEqual(b"redirect target reached", body)
                self.assertEqual(1, len(server.redirect_headers))
                self.assertEqual(1, len(server.target_headers))
                target = _casefold_headers(server.target_headers[0])
                self.assertEqual(
                    REDMINE_API_KEY,
                    target["x-redmine-api-key"],
                )
                self.assertNotIn("private-token", target)

    def test_source_route_uses_isolated_opener_only_for_gitlab(self) -> None:
        built_openers: list[urllib.request.OpenerDirector] = []

        def build_opener() -> urllib.request.OpenerDirector:
            value = urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            )
            built_openers.append(value)
            return value

        resolution = SimpleNamespace(
            environment={"ROUTE": "selected"},
            build_url_opener=build_opener,
        )
        network = SimpleNamespace(
            resolve_network_configuration=lambda **_kwargs: resolution,
        )
        with (
            mock.patch.object(
                networking_module,
                "_network_module",
                return_value=network,
            ),
            _running_redirect_server(302) as server,
        ):
            route = networking_module.resolve_source_network_route(
                Path("/synthetic/rag"),
                environment={},
            )
            gitlab_status, _body, _headers = route.http_get(
                server.redirect_url,
                GITLAB_HEADERS,
                2,
            )
            redmine_status, redmine_body, _headers = route.http_get(
                server.redirect_url,
                REDMINE_HEADERS,
                2,
            )

        self.assertEqual(302, gitlab_status)
        self.assertEqual(200, redmine_status)
        self.assertEqual(b"redirect target reached", redmine_body)
        self.assertEqual(2, len(built_openers))
        self.assertIsNot(built_openers[0], built_openers[1])
        self.assertEqual(2, len(server.redirect_headers))
        self.assertEqual(1, len(server.target_headers))
        target = _casefold_headers(server.target_headers[0])
        self.assertEqual(
            REDMINE_API_KEY,
            target["x-redmine-api-key"],
        )
        self.assertNotIn("private-token", target)


if __name__ == "__main__":
    unittest.main()
