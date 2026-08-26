from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from source_manager.confluence import (
    ConfluenceEndpoint,
    confluence_page_relative_path,
    fetch_confluence,
    request_confluence_json,
    resolve_confluence_endpoint,
    storage_xhtml_to_markdown,
    validate_confluence_work_tree,
    _validated_next_url,
)
from source_manager.errors import SourceManagerError


SITE = "https://docs.example.invalid"
SPACE = "ENG"
SPACE_ID = "10001"
TOKEN = "confluence-secret-must-not-leak"


def _settings(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "connection_id": "confluence-fixture",
        "space_key": SPACE,
        "scope": "space",
        "attachments": "metadata",
    }
    value.update(overrides)
    return value


def _credentials(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "connection_id": "confluence-fixture",
        "deployment": "cloud",
        "cloud_scope": "unscoped",
        "site_url": SITE,
        "context_path": "/wiki",
        "api_root": f"{SITE}/wiki/api/v2",
        "auth_type": "basic",
        "username": "reader@example.invalid",
        "api_token": TOKEN,
    }
    value.update(overrides)
    return value


def _response(payload: Any, **headers: str):
    return 200, json.dumps(payload).encode("utf-8"), headers


def _inventory(
    page_ids: list[int],
    *,
    next_link: str | None = None,
) -> dict[str, Any]:
    links: dict[str, str] = {}
    if next_link is not None:
        links["next"] = next_link
    result: dict[str, Any] = {
        "results": [
            {
                "id": str(page_id),
                "type": "page",
                "title": f"Page {page_id}",
                "spaceId": SPACE_ID,
                "version": {"number": 1},
            }
            for page_id in page_ids
        ],
        "_links": links,
    }
    return result


def _detail(page_id: int, *, body: str | None = None) -> dict[str, Any]:
    return {
        "id": str(page_id),
        "type": "page",
        "status": "current",
        "title": f"Page {page_id}",
        "spaceId": SPACE_ID,
        "version": {
            "number": 3,
            "createdAt": "2026-08-26T00:00:00.000Z",
            "authorId": "fixture-author-id",
        },
        "body": {
            "storage": {
                "representation": "storage",
                "value": body or f"<p>Body {page_id}</p>",
            }
        },
        "_links": {
            "self": f"{SITE}/wiki/api/v2/pages/{page_id}",
            "base": f"{SITE}/wiki",
            "webui": f"/spaces/{SPACE}/pages/{page_id}/Page+{page_id}",
        },
    }


def _attachments(page_id: int, *, title: str | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if title is not None:
        results.append(
            {
                "id": f"9{page_id}",
                "type": "attachment",
                "title": title,
                "extensions": {
                    "mediaType": "application/pdf",
                    "fileSize": 1234,
                },
                "version": {"number": 2},
                "_links": {
                    "webui": f"/download/attachments/{page_id}/{title}",
                    "download": f"/download/attachments/{page_id}/{title}",
                },
            }
        )
    return {
        "results": results,
        "_links": {},
    }


class _ConfluenceApi:
    def __init__(
        self,
        page_ids: list[int],
        *,
        inventory_pages: list[dict[str, Any]] | None = None,
        detail_overrides: Mapping[int, Any] | None = None,
        attachment_titles: Mapping[int, str] | None = None,
        labels: Mapping[int, list[dict[str, Any]]] | None = None,
        ancestors: Mapping[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.page_ids = list(page_ids)
        self.inventory_pages = list(
            inventory_pages or [_inventory(self.page_ids)]
        )
        self.detail_overrides = dict(detail_overrides or {})
        self.attachment_titles = dict(attachment_titles or {})
        self.labels = dict(labels or {})
        self.ancestors = dict(ancestors or {})
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ):
        self.calls.append((url, dict(headers), timeout))
        split = urlsplit(url)
        path = split.path
        query = parse_qs(split.query)
        if path.endswith("/spaces"):
            return _response(
                {
                    "results": [{"id": SPACE_ID, "key": SPACE}],
                    "_links": {},
                }
            )
        if path.endswith("/pages") or path.endswith("/descendants"):
            index = 1 if query.get("cursor") else 0
            if index >= len(self.inventory_pages):
                raise AssertionError(f"unexpected inventory cursor: {url}")
            return _response(self.inventory_pages[index])
        if "/attachments" in path:
            page_id = int(path.split("/pages/", 1)[1].split("/", 1)[0])
            return _response(
                _attachments(page_id, title=self.attachment_titles.get(page_id))
            )
        if path.endswith("/labels"):
            page_id = int(path.split("/pages/", 1)[1].split("/", 1)[0])
            return _response({"results": self.labels.get(page_id, []), "_links": {}})
        if path.endswith("/ancestors"):
            page_id = int(path.split("/pages/", 1)[1].split("/", 1)[0])
            return _response(
                {"results": self.ancestors.get(page_id, []), "_links": {}}
            )
        if "/pages/" in path:
            page_id = int(path.rsplit("/", 1)[-1])
            override = self.detail_overrides.get(page_id)
            if override is not None:
                return override
            return _response(_detail(page_id))
        raise AssertionError(f"unexpected request: {url}")

    def inventory_calls(self) -> list[str]:
        return [
            url
            for url, _headers, _timeout in self.calls
            if urlsplit(url).path.endswith(("/pages", "/descendants"))
        ]

    def detail_ids(self) -> list[int]:
        output: list[int] = []
        for url, _headers, _timeout in self.calls:
            path = urlsplit(url).path
            if "/pages/" in path and not path.endswith(
                ("/attachments", "/descendants", "/labels", "/ancestors")
            ):
                output.append(int(path.rsplit("/", 1)[-1]))
        return output


class ConfluenceEndpointTests(unittest.TestCase):
    def test_cloud_unscoped_scoped_and_data_center_roots_are_explicit(self) -> None:
        unscoped = resolve_confluence_endpoint(_credentials())
        self.assertEqual("cloud", unscoped.deployment)
        self.assertEqual("unscoped", unscoped.cloud_scope)
        self.assertEqual(f"{SITE}/wiki/api/v2", unscoped.api_root)
        self.assertEqual(f"{SITE}/wiki", unscoped.web_root)

        scoped = resolve_confluence_endpoint(
            _credentials(
                cloud_scope="scoped",
                cloud_id="2f75f928-48e2-4c5b-b047-0f58a3f6be64",
                api_root=(
                    "https://api.atlassian.com/ex/confluence/"
                    "2f75f928-48e2-4c5b-b047-0f58a3f6be64/wiki/api/v2"
                ),
            )
        )
        self.assertEqual("scoped", scoped.cloud_scope)
        self.assertIn(scoped.cloud_id or "", scoped.api_root)
        self.assertEqual(f"{SITE}/wiki", scoped.web_root)

        data_center = resolve_confluence_endpoint(
            _credentials(
                deployment="data_center",
                cloud_scope=None,
                site_url="https://kb.example.invalid",
                context_path="/confluence",
                api_root="https://kb.example.invalid/confluence/rest/api",
            )
        )
        self.assertEqual("data_center", data_center.deployment)
        self.assertEqual(
            "https://kb.example.invalid/confluence/rest/api",
            data_center.api_root,
        )

    def test_implicit_or_mismatched_endpoint_configuration_fails_closed(self) -> None:
        cases = [
            {**_credentials(), "deployment": None},
            {**_credentials(), "cloud_scope": None},
            {**_credentials(), "context_path": "/wiki/../admin"},
            {**_credentials(), "api_root": f"{SITE}/rest/api"},
            {
                **_credentials(),
                "cloud_scope": "scoped",
                "cloud_id": None,
                "api_root": "https://api.atlassian.com/ex/confluence/x/wiki/api/v2",
            },
        ]
        for settings in cases:
            with self.subTest(settings=settings), self.assertRaises(
                SourceManagerError
            ):
                resolve_confluence_endpoint(settings)

    def test_page_id_path_is_canonical_and_injection_safe(self) -> None:
        self.assertEqual("pages/123.md", confluence_page_relative_path("123"))
        self.assertEqual(
            "pages/9223372036854775807.md",
            confluence_page_relative_path("9223372036854775807"),
        )
        for value in (
            "",
            "0",
            "-1",
            "../1",
            "1/2",
            "9223372036854775808",
            True,
        ):
            with self.subTest(value=value), self.assertRaises(
                SourceManagerError
            ):
                confluence_page_relative_path(value)


class ConfluenceHttpTests(unittest.TestCase):
    def test_data_center_context_relative_next_link_is_preserved(self) -> None:
        endpoint = resolve_confluence_endpoint(
            _credentials(
                deployment="data_center",
                cloud_scope=None,
                site_url="https://kb.example.invalid",
                context_path="/confluence",
                api_root="https://kb.example.invalid/confluence/rest/api",
            )
        )
        self.assertEqual(
            "https://kb.example.invalid/confluence/rest/api/content?limit=2&start=2",
            _validated_next_url(
                endpoint,
                endpoint.api_root + "/content?limit=2&start=0",
                "/rest/api/content?limit=2&start=2",
                current_start=0,
            ),
        )

    def test_encoded_or_raw_path_traversal_is_rejected_before_get(self) -> None:
        endpoint = resolve_confluence_endpoint(_credentials())
        called: list[str] = []

        def should_not_run(url: str, _headers: Mapping[str, str], _timeout: float):
            called.append(url)
            return _response({})

        for suffix in (
            "/%2e%2e/admin",
            "/%252e%252e/admin",
            "/%2fadmin",
            "/%255cadmin",
            "/..\\admin",
            "/%broken",
        ):
            with self.subTest(suffix=suffix), self.assertRaises(SourceManagerError):
                request_confluence_json(
                    endpoint,
                    endpoint.api_root + suffix,
                    should_not_run,
                    _credentials(),
                )
        self.assertEqual([], called)

    def test_basic_and_bearer_auth_stay_in_memory_and_get_retries_max_three(
        self,
    ) -> None:
        endpoint = resolve_confluence_endpoint(_credentials())
        calls: list[dict[str, str]] = []

        def transient(_url: str, headers: Mapping[str, str], _timeout: float):
            calls.append(dict(headers))
            return 503, b'{"error":"busy"}', {"Retry-After": "0"}

        with self.assertRaises(SourceManagerError):
            request_confluence_json(
                endpoint,
                f"{endpoint.api_root}/pages",
                transient,
                _credentials(),
                sleep=lambda _seconds: None,
            )
        self.assertEqual(3, len(calls))
        expected = "Basic " + base64.b64encode(
            f"reader@example.invalid:{TOKEN}".encode("utf-8")
        ).decode("ascii")
        self.assertEqual(expected, calls[0]["Authorization"])

        bearer_headers: list[dict[str, str]] = []

        def ok(_url: str, headers: Mapping[str, str], _timeout: float):
            bearer_headers.append(dict(headers))
            return _response({"ok": True})

        data_center = resolve_confluence_endpoint(
            _credentials(
                deployment="data_center",
                cloud_scope=None,
                site_url="https://kb.example.invalid",
                context_path="/confluence",
                api_root="https://kb.example.invalid/confluence/rest/api",
            )
        )
        value = request_confluence_json(
            data_center,
            f"{data_center.api_root}/content",
            ok,
            {"auth_type": "bearer", "token": TOKEN},
        )
        self.assertEqual({"ok": True}, value)
        self.assertEqual(f"Bearer {TOKEN}", bearer_headers[0]["Authorization"])
        with self.assertRaisesRegex(SourceManagerError, "requires basic"):
            request_confluence_json(
                endpoint,
                f"{endpoint.api_root}/pages",
                ok,
                {"auth_type": "bearer", "token": TOKEN},
            )
        with self.assertRaisesRegex(SourceManagerError, "requires bearer"):
            request_confluence_json(
                data_center,
                f"{data_center.api_root}/content",
                ok,
                _credentials(),
            )

    def test_redirect_and_cross_origin_or_context_pagination_fail_closed(
        self,
    ) -> None:
        endpoint = resolve_confluence_endpoint(_credentials())

        def redirect(_url: str, _headers: Mapping[str, str], _timeout: float):
            return 302, b"", {"Location": "https://evil.invalid/steal"}

        with self.assertRaisesRegex(SourceManagerError, "redirect"):
            request_confluence_json(
                endpoint,
                f"{endpoint.api_root}/pages",
                redirect,
                _credentials(),
            )

        for next_link in (
            "https://evil.invalid/wiki/api/v2/pages?cursor=next",
            f"{SITE}/admin/pages?cursor=next",
            f"{SITE}/wiki/api/v2/pages?space-id={SPACE_ID}&limit=100",
        ):
            api = _ConfluenceApi(
                [1],
                inventory_pages=[
                    _inventory([1], next_link=next_link)
                ],
            )
            with tempfile.TemporaryDirectory() as temporary:
                with self.subTest(next_link=next_link), self.assertRaises(
                    SourceManagerError
                ):
                    fetch_confluence(
                        _settings(),
                        Path(temporary),
                        credentials=_credentials(),
                        http_get=api,
                    )


class ConfluenceXhtmlTests(unittest.TestCase):
    def test_storage_xhtml_is_deterministic_and_strips_active_content(self) -> None:
        xhtml = """
        <h1>Heading</h1>
        <p>Hello <strong>world</strong> &amp; <a href="https://safe.invalid/x">link</a>.</p>
        <ul><li>One</li><li>Two</li></ul>
        <ac:structured-macro ac:name="info"><ac:rich-text-body><p>Macro text</p></ac:rich-text-body></ac:structured-macro>
        <table><tbody><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></tbody></table>
        <script>SECRET_SCRIPT()</script><style>.hidden{}</style>
        """
        rendered = storage_xhtml_to_markdown(xhtml)
        self.assertIn("# Heading", rendered)
        self.assertIn("**world**", rendered)
        self.assertIn("[link](https://safe.invalid/x)", rendered)
        self.assertIn("- One", rendered)
        self.assertIn("Confluence macro: info", rendered)
        self.assertIn("| A | B |", rendered)
        self.assertNotIn("SECRET_SCRIPT", rendered)
        self.assertNotIn(".hidden", rendered)
        self.assertEqual(rendered, storage_xhtml_to_markdown(xhtml))

    def test_native_confluence_links_preserve_target_identity_without_slug(self) -> None:
        rendered = storage_xhtml_to_markdown(
            '<p><ac:link><ri:page ri:space-key="ENG" ri:content-title="Runbook"/>'
            '<ac:plain-text-link-body>Open runbook</ac:plain-text-link-body></ac:link> '
            '<ac:link><ri:url ri:value="https://safe.invalid/runbook"/>'
            '<ac:plain-text-link-body>External</ac:plain-text-link-body></ac:link></p>'
        )
        self.assertIn("Open runbook [Confluence page: ENG:Runbook]", rendered)
        self.assertIn("[External](https://safe.invalid/runbook)", rendered)
        self.assertNotIn("Page+Runbook", rendered)

    def test_storage_xhtml_rejects_unsafe_links_and_bounded_input(self) -> None:
        rendered = storage_xhtml_to_markdown(
            '<p><a href="javascript:alert(1)">click</a></p>'
        )
        self.assertIn("click", rendered)
        self.assertNotIn("javascript", rendered)
        with self.assertRaises(SourceManagerError):
            storage_xhtml_to_markdown("x" * (4 * 1024 * 1024 + 1))


class ConfluenceFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-confluence-")
        self.work = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_inventory_is_complete_before_details_and_map_is_exact(self) -> None:
        second = f"{SITE}/wiki/api/v2/pages?space-id={SPACE_ID}&limit=100&cursor=next"
        api = _ConfluenceApi(
            [1, 2, 3],
            inventory_pages=[
                _inventory([1, 2], next_link=second),
                _inventory([3]),
            ],
            attachment_titles={1: "design.pdf"},
            labels={1: [{"id": "l1", "name": "runbook", "prefix": "global"}]},
            ancestors={1: [{"id": "99", "type": "page", "title": "Engineering"}]},
        )
        frozen: list[list[str]] = []
        outcome = fetch_confluence(
            _settings(),
            self.work,
            credentials=_credentials(),
            http_get=api,
            inventory_callback=lambda values: frozen.append(list(values)),
        )

        self.assertEqual([["1", "2", "3"]], frozen)
        call_paths = [urlsplit(call[0]).path for call in api.calls]
        last_inventory = max(
            index for index, path in enumerate(call_paths) if path.endswith("/pages")
        )
        first_detail = min(
            index
            for index, path in enumerate(call_paths)
            if "/pages/" in path and not path.endswith(("/attachments", "/descendants"))
        )
        self.assertLess(last_inventory, first_detail)
        self.assertEqual(3, outcome["documents"])
        self.assertEqual(
            f"{SITE}/wiki/spaces/{SPACE}/pages/1/Page+1",
            outcome["api_webui_map"][f"{SITE}/wiki/api/v2/pages/1"],
        )
        self.assertEqual(
            f"{SITE}/wiki/spaces/{SPACE}/pages/1/Page+1",
            outcome["page_urls"]["1"],
        )
        self.assertEqual("pages/1.md", outcome["page_links"][0]["path"])
        text = (self.work / "pages" / "1.md").read_text(encoding="utf-8")
        self.assertIn("design.pdf", text)
        self.assertIn("application/pdf", text)
        self.assertIn("- Status: current", text)
        self.assertIn("- Updated: 2026-08-26T00:00:00.000Z", text)
        self.assertIn("- Author: fixture-author-id", text)
        self.assertIn("- Labels: global:runbook", text)
        self.assertIn("Engineering (id=99)", text)
        self.assertNotIn(TOKEN, json.dumps(outcome) + text)
        self.assertFalse(any("download" in url for url, _headers, _ in api.calls))

    def test_subtree_inventory_includes_root_and_uses_descendant_api(self) -> None:
        api = _ConfluenceApi([12, 13])
        outcome = fetch_confluence(
            _settings(scope="subtree", root_page_id="11"),
            self.work,
            credentials=_credentials(),
            http_get=api,
        )
        self.assertEqual([11, 12, 13], api.detail_ids())
        self.assertEqual(3, outcome["documents"])
        inventory_url = api.inventory_calls()[0]
        self.assertIn("/pages/11/descendants", inventory_url)

    def test_five_item_batch_interrupt_and_resume_skip_frozen_work(self) -> None:
        page_ids = list(range(1, 8))
        first_api = _ConfluenceApi(page_ids)
        batches: list[tuple[int, str]] = []
        inventory_checkpoints: list[tuple[list[str], str]] = []

        class PauseAfterFive(RuntimeError):
            pass

        def pause(completed: int, page_id: str) -> None:
            batches.append((completed, page_id))
            if completed == 5:
                raise PauseAfterFive("fixture interruption")

        with self.assertRaises(PauseAfterFive):
            fetch_confluence(
                _settings(),
                self.work,
                credentials=_credentials(),
                http_get=first_api,
                inventory_etag_callback=lambda ids, etag: inventory_checkpoints.append(
                    (list(ids), etag)
                ),
                batch_callback=pause,
            )
        self.assertEqual([(5, "5")], batches)
        self.assertEqual(
            [1, 2, 3, 4, 5],
            sorted(int(path.stem) for path in (self.work / "pages").glob("*.md")),
        )

        resumed_api = _ConfluenceApi(page_ids)
        resumed_batches: list[tuple[int, str]] = []
        outcome = fetch_confluence(
            _settings(),
            self.work,
            credentials=_credentials(),
            http_get=resumed_api,
            stable_page_ids=[str(value) for value in page_ids],
            resume_count=5,
            resume_inventory_etag=inventory_checkpoints[0][1],
            batch_callback=lambda completed, page_id: resumed_batches.append(
                (completed, page_id)
            ),
        )
        self.assertEqual([], resumed_api.inventory_calls())
        self.assertEqual([6, 7], resumed_api.detail_ids())
        self.assertEqual([(7, "7")], resumed_batches)
        self.assertEqual(7, outcome["documents"])

    def test_detail_or_parse_failure_preserves_old_files_and_never_deletes(
        self,
    ) -> None:
        initial = _ConfluenceApi([1, 2])
        fetch_confluence(
            _settings(), self.work, credentials=_credentials(), http_get=initial
        )
        old_one = (self.work / "pages" / "1.md").read_bytes()
        old_two = (self.work / "pages" / "2.md").read_bytes()

        failure = _ConfluenceApi(
            [1],
            detail_overrides={
                1: _response(
                    _detail(1, body="x" * (4 * 1024 * 1024 + 1))
                )
            },
        )
        with self.assertRaises(SourceManagerError):
            fetch_confluence(
                _settings(), self.work, credentials=_credentials(), http_get=failure
            )
        self.assertEqual(old_one, (self.work / "pages" / "1.md").read_bytes())
        self.assertEqual(old_two, (self.work / "pages" / "2.md").read_bytes())

    def test_full_success_deletes_only_owned_pages_missing_from_inventory(
        self,
    ) -> None:
        fetch_confluence(
            _settings(),
            self.work,
            credentials=_credentials(),
            http_get=_ConfluenceApi([1, 2]),
        )
        outcome = fetch_confluence(
            _settings(),
            self.work,
            credentials=_credentials(),
            http_get=_ConfluenceApi([1]),
        )
        self.assertEqual(1, outcome["deleted_this_run"])
        self.assertTrue((self.work / "pages" / "1.md").is_file())
        self.assertFalse((self.work / "pages" / "2.md").exists())

    def test_resume_partial_inventory_without_matching_etag_cannot_delete(self) -> None:
        initial = fetch_confluence(
            _settings(),
            self.work,
            credentials=_credentials(),
            http_get=_ConfluenceApi([1, 2]),
        )
        with self.assertRaisesRegex(SourceManagerError, "checkpoint etag"):
            fetch_confluence(
                _settings(),
                self.work,
                credentials=_credentials(),
                http_get=_ConfluenceApi([1]),
                stable_page_ids=["1"],
                resume_count=1,
                resume_inventory_etag=initial["inventory_etag"],
            )
        self.assertTrue((self.work / "pages" / "2.md").is_file())

    def test_work_tree_validator_requires_exact_count_and_checkpoint(self) -> None:
        fetch_confluence(
            _settings(),
            self.work,
            credentials=_credentials(),
            http_get=_ConfluenceApi([1]),
        )
        validate_confluence_work_tree(self.work, expected_documents=1)
        with self.assertRaises(SourceManagerError):
            validate_confluence_work_tree(self.work, expected_documents=2)

    def test_final_batch_runs_after_stale_reconcile_including_empty_inventory(
        self,
    ) -> None:
        fetch_confluence(
            _settings(),
            self.work,
            credentials=_credentials(),
            http_get=_ConfluenceApi([1, 2]),
        )
        observations: list[tuple[int, str | None, list[str]]] = []

        outcome = fetch_confluence(
            _settings(),
            self.work,
            credentials=_credentials(),
            http_get=_ConfluenceApi([]),
            batch_callback=lambda completed, page_id: observations.append(
                (
                    completed,
                    page_id,
                    sorted(path.name for path in (self.work / "pages").glob("*.md"))
                    if (self.work / "pages").exists()
                    else [],
                )
            ),
        )

        self.assertEqual([(0, None, [])], observations)
        self.assertEqual(0, outcome["documents"])
        self.assertEqual(2, outcome["deleted_this_run"])


if __name__ == "__main__":
    unittest.main()
