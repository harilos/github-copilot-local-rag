from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from unittest import mock
from urllib.parse import parse_qs, quote, urlsplit

from source_manager import gitlab_issues as gitlab_issues_module
from source_manager.errors import SourceManagerError, sanitize_diagnostic
from source_manager.execution import execute_fetch_plan
from source_manager.gitlab_issues import (
    GITLAB_ISSUES_CUTOFF_STATE_KEY,
    GitLabIssueInventoryItem,
    GitLabProject,
    _atomic_write_text,
    _changed_issue_iids,
    _fetch_project_identity,
    _fetch_inventory,
    _format_timestamp,
    fetch_gitlab_issues,
    gitlab_token_env,
    gitlab_issues_updated_after,
    parse_gitlab_project,
)


GITLAB_URL = "https://gitlab.example.invalid/gitlab"
PROJECT_URL = f"{GITLAB_URL}/group/subgroup/project"
API_BASE_URL = f"{GITLAB_URL}/api/v4"
PROJECT_PATH = "group/subgroup/project"
PROJECT_ID = 101
PROJECT_LOOKUP_URL = (
    f"{API_BASE_URL}/projects/{quote(PROJECT_PATH, safe='')}"
)
TOKEN_ENV = gitlab_token_env(GITLAB_URL)
TOKEN = "GITLAB-PRIVATE-TOKEN-DO-NOT-LEAK"


def _settings(**overrides: Any) -> dict[str, Any]:
    value = {
        "gitlab_url": GITLAB_URL,
        "project_url": PROJECT_URL,
        "updated_within_days": None,
        "token_env": TOKEN_ENV,
    }
    value.update(overrides)
    return value


def _json_response(
    payload: Any,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, bytes, Mapping[str, str]]:
    return (
        200,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        dict(headers or {}),
    )


def _summary(
    iid: int,
    *,
    updated_at: str = "2026-07-30T00:00:00.100Z",
    state: str = "opened",
    notes: int = 0,
    project_id: int = PROJECT_ID,
) -> dict[str, Any]:
    return {
        "id": 10_000 + iid,
        "iid": iid,
        "project_id": project_id,
        "title": f"Issue {iid}",
        "state": state,
        "updated_at": updated_at,
        "user_notes_count": notes,
    }


def _detail(
    iid: int,
    *,
    updated_at: str = "2026-07-30T00:00:00.100Z",
    state: str = "opened",
    notes: int = 0,
    project_id: int = PROJECT_ID,
    description: str | None = None,
) -> dict[str, Any]:
    return {
        **_summary(
            iid,
            updated_at=updated_at,
            state=state,
            notes=notes,
            project_id=project_id,
        ),
        "description": description or f"Description {iid}",
        "created_at": "2026-07-01T00:00:00.000Z",
        "author": {"name": "Author", "username": "author"},
        "assignees": [{"name": "Assignee", "username": "assignee"}],
        "labels": ["bug", "fixture"],
        "web_url": f"{PROJECT_URL}/-/issues/{iid}",
    }


def _discussion(
    discussion_id: str,
    note_id: int,
    body: str,
    *,
    created_at: str,
    system: bool = False,
) -> dict[str, Any]:
    return {
        "id": discussion_id,
        "project_id": PROJECT_ID,
        "individual_note": True,
        "notes": [
            {
                "id": note_id,
                "project_id": PROJECT_ID,
                "body": body,
                "created_at": created_at,
                "system": system,
                "author": {"name": "Commenter", "username": "commenter"},
            }
        ],
    }


def _marker_markdown(
    iid: int,
    updated_at: str,
    notes: int = 0,
    *,
    issue_id: int | None = None,
    body: str = "previous complete document",
) -> str:
    marker = json.dumps(
        {
            "iid": iid,
            "issue_id": issue_id if issue_id is not None else 10_000 + iid,
            "updated_at": updated_at,
            "user_notes_count": notes,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"# GitLab Issue #{iid}\n\n"
        f"<!-- local-rag-gitlab-issue: {marker} -->\n\n"
        f"{body}\n"
    )


def _http_error(status: int, message: str) -> SourceManagerError:
    error = SourceManagerError(message, stage="fetch.gitlab_issues")
    error.diagnostic = {
        "event": "gitlab_issues.http_attempt",
        "status": status,
        "retry": False,
    }
    return error


class _GitLabApi:
    def __init__(
        self,
        inventory_pages: Mapping[int, list[Mapping[str, Any]]],
        *,
        details: Mapping[int, Mapping[str, Any]] | None = None,
        discussion_pages: (
            Mapping[int, Mapping[int, list[Mapping[str, Any]]]] | None
        ) = None,
        detail_statuses: Mapping[int, int] | None = None,
        detail_errors: Mapping[int, BaseException] | None = None,
        discussion_errors: Mapping[int, BaseException] | None = None,
        project_payload: Mapping[str, Any] | None = None,
        include_pagination_headers: bool = True,
    ) -> None:
        self.inventory_pages = {
            int(page): [dict(item) for item in values]
            for page, values in inventory_pages.items()
        }
        self.summaries = {
            int(item["iid"]): dict(item)
            for values in self.inventory_pages.values()
            for item in values
            if isinstance(item, Mapping) and str(item.get("iid") or "").isdigit()
        }
        self.details = {
            int(key): dict(value) for key, value in (details or {}).items()
        }
        self.discussion_pages = {
            int(iid): {
                int(page): [dict(item) for item in values]
                for page, values in pages.items()
            }
            for iid, pages in (discussion_pages or {}).items()
        }
        self.detail_statuses = {
            int(key): int(value)
            for key, value in (detail_statuses or {}).items()
        }
        self.detail_errors = dict(detail_errors or {})
        self.discussion_errors = dict(discussion_errors or {})
        self.project_payload = dict(
            project_payload
            or {
                "id": PROJECT_ID,
                "web_url": PROJECT_URL,
                "path_with_namespace": PROJECT_PATH,
            }
        )
        self.include_pagination_headers = include_pagination_headers
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> tuple[int, bytes, Mapping[str, str]]:
        self.calls.append((url, dict(headers)))
        split = urlsplit(url)
        query = parse_qs(split.query)

        if url == PROJECT_LOOKUP_URL:
            return _json_response(self.project_payload)

        issues_path = f"/gitlab/api/v4/projects/{PROJECT_ID}/issues"
        if split.path == issues_path:
            page = int(query.get("page", ["1"])[0])
            values = self.inventory_pages.get(page, [])
            return _json_response(
                values,
                self._page_headers(
                    page,
                    self.inventory_pages,
                    sum(len(items) for items in self.inventory_pages.values()),
                ),
            )

        prefix = issues_path + "/"
        if split.path.startswith(prefix):
            suffix = split.path[len(prefix):]
            if suffix.endswith("/discussions"):
                iid_text = suffix.removesuffix("/discussions")
                iid = int(iid_text)
                if iid in self.discussion_errors:
                    raise self.discussion_errors[iid]
                pages = self.discussion_pages.get(iid, {1: []})
                page = int(query.get("page", ["1"])[0])
                return _json_response(
                    pages.get(page, []),
                    self._page_headers(
                        page,
                        pages,
                        sum(len(items) for items in pages.values()),
                    ),
                )
            iid = int(suffix)
            if iid in self.detail_statuses:
                return (
                    self.detail_statuses[iid],
                    b'{"message":"fixture detail response"}',
                    {},
                )
            if iid in self.detail_errors:
                raise self.detail_errors[iid]
            if iid in self.details:
                return _json_response(self.details[iid])
            summary = self.summaries[iid]
            return _json_response(
                _detail(
                    iid,
                    updated_at=str(summary["updated_at"]),
                    state=str(summary.get("state") or "opened"),
                    notes=int(summary.get("user_notes_count") or 0),
                    project_id=int(summary.get("project_id") or PROJECT_ID),
                )
            )

        raise AssertionError(f"unexpected GitLab request: {url}")

    def _page_headers(
        self,
        page: int,
        pages: Mapping[int, list[Mapping[str, Any]]],
        total: int,
    ) -> dict[str, str]:
        if not self.include_pagination_headers:
            return {}
        later_pages = sorted(value for value in pages if value > page)
        return {
            "X-Total": str(total),
            "X-Next-Page": str(later_pages[0]) if later_pages else "",
        }

    def urls(self) -> list[str]:
        return [url for url, _headers in self.calls]

    def inventory_urls(self) -> list[str]:
        expected = f"/gitlab/api/v4/projects/{PROJECT_ID}/issues"
        return [
            url
            for url in self.urls()
            if urlsplit(url).path == expected
        ]

    def detail_iids(self) -> list[int]:
        expected = f"/gitlab/api/v4/projects/{PROJECT_ID}/issues/"
        values: list[int] = []
        for url in self.urls():
            path = urlsplit(url).path
            if not path.startswith(expected) or path.endswith("/discussions"):
                continue
            values.append(int(path[len(expected):]))
        return values


class GitLabIssueSourceContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-gitlab-issues-"
        )
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fetch(
        self,
        request: Any,
        *,
        environment: Mapping[str, str] | None = None,
        item_callback: Any = None,
        batch_callback: Any = None,
        no_change_callback: Any = None,
        resume_count: int = 0,
        stable_issue_ids: list[int] | None = None,
        stable_project_id: int | None = None,
        inventory_snapshot_callback: Any = None,
        updated_after: str | None = None,
        progress_callback: Any = None,
        settings: Mapping[str, Any] | None = None,
        force_full_materialization: bool = False,
    ) -> dict[str, Any]:
        return fetch_gitlab_issues(
            dict(settings or _settings()),
            self.work,
            request,
            {TOKEN_ENV: TOKEN} if environment is None else environment,
            item_callback=item_callback,
            batch_callback=batch_callback,
            resume_count=resume_count,
            stable_issue_ids=stable_issue_ids,
            stable_project_id=stable_project_id,
            inventory_snapshot_callback=inventory_snapshot_callback,
            updated_after=updated_after,
            progress_callback=progress_callback,
            no_change_callback=no_change_callback,
            _force_full_materialization=force_full_materialization,
        )

    def test_force_full_materialization_fetches_unchanged_inventory(self) -> None:
        first = _GitLabApi({1: [_summary(1)]})
        self.fetch(first)
        unchanged = _GitLabApi({1: [_summary(1)]})
        self.fetch(unchanged)
        self.assertEqual([], unchanged.detail_iids())
        forced = _GitLabApi({1: [_summary(1)]})
        result = self.fetch(forced, force_full_materialization=True)
        self.assertEqual([1], forced.detail_iids())
        self.assertEqual(1, result["fetched_this_run"])

    def test_project_path_is_encoded_and_open_and_closed_inventory_is_complete(
        self,
    ) -> None:
        closed_timestamp = "2026-07-29T12:00:00.500Z"
        issues = self.work / "issues"
        issues.mkdir()
        closed = issues / "2.md"
        closed.write_text(
            _marker_markdown(
                2,
                closed_timestamp,
                body="closed issue must be retained",
            ),
            encoding="utf-8",
        )
        api = _GitLabApi(
            {
                1: [_summary(1)],
                2: [
                    _summary(
                        2,
                        updated_at=closed_timestamp,
                        state="closed",
                    )
                ],
            }
        )

        outcome = self.fetch(api)

        self.assertEqual(PROJECT_LOOKUP_URL, api.urls()[0])
        self.assertEqual(2, len(api.inventory_urls()))
        for url in api.inventory_urls():
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(["all"], query.get("scope"))
            self.assertEqual(["all"], query.get("state"))
            self.assertEqual(["100"], query.get("per_page"))
        self.assertEqual([1], api.detail_iids())
        self.assertTrue((issues / "1.md").is_file())
        self.assertEqual(
            "closed issue must be retained",
            closed.read_text(encoding="utf-8").splitlines()[-1],
        )
        self.assertEqual(2, outcome["inventory_documents"])
        for _url, headers in api.calls:
            self.assertEqual(TOKEN, headers.get("PRIVATE-TOKEN"))

    def test_project_identity_accepts_dual_hostname_metadata(self) -> None:
        cases = (
            (
                "P1-same-host",
                "https://git-e.example/gitlab",
                "group/project",
                "https://git-e.example/gitlab/group/project",
            ),
            (
                "P2-dual-host",
                "https://git-e.example/gitlab",
                "group/project",
                "https://git-p.example/group/project",
            ),
            (
                "P3-dual-host-subpath",
                "https://git-e.example/internal-gitlab",
                "group/project",
                "https://git-p.example/external-gitlab/group/project",
            ),
            (
                "P4-nested-namespace",
                "https://git-e.example/gitlab",
                "group/subgroup/project",
                "https://git-p.example/group/subgroup/project",
            ),
        )
        for label, gitlab_url, project_path, returned_web_url in cases:
            with self.subTest(case=label):
                project = parse_gitlab_project(
                    f"{gitlab_url}/{project_path}",
                    gitlab_url,
                )
                calls: list[tuple[str, dict[str, str]]] = []

                def request(url, headers):
                    calls.append((url, dict(headers)))
                    return _json_response(
                        {
                            "id": PROJECT_ID,
                            "web_url": returned_web_url,
                            "path_with_namespace": project_path,
                        }
                    )

                verified = _fetch_project_identity(
                    project,
                    request,
                    {"PRIVATE-TOKEN": TOKEN},
                )

                self.assertEqual(
                    (
                        project.gitlab_url,
                        project.api_base_url,
                        project.project_url,
                        project.project_path,
                    ),
                    (
                        verified.gitlab_url,
                        verified.api_base_url,
                        verified.project_url,
                        verified.project_path,
                    ),
                )
                self.assertEqual(PROJECT_ID, verified.project_id)
                self.assertEqual(
                    [(project.project_api_url, {"PRIVATE-TOKEN": TOKEN})],
                    calls,
                )
                self.assertNotIn("git-p.example", calls[0][0])

    def test_project_identity_requires_exact_canonical_response_identity(
        self,
    ) -> None:
        project = parse_gitlab_project(
            "https://git-e.example/gitlab/group/project",
            "https://git-e.example/gitlab",
        )
        invalid_paths = (
            None,
            "",
            "   ",
            123,
            " group/project",
            "group/project ",
            "other/group/project",
            "group/project-extra",
            "group/project/child",
            "group%2Fproject",
            "group\\project",
            "group/../project",
            "group∕project",
        )
        for response_path in invalid_paths:
            with self.subTest(path=response_path):
                def request(_url, _headers):
                    return _json_response(
                        {
                            "id": PROJECT_ID,
                            "web_url": (
                                "https://git-p.example/group/project"
                            ),
                            "path_with_namespace": response_path,
                        }
                    )

                with self.assertRaisesRegex(
                    SourceManagerError,
                    "wrong path identity",
                ):
                    _fetch_project_identity(
                        project,
                        request,
                        {"PRIVATE-TOKEN": TOKEN},
                    )

        pinned_project = GitLabProject(
            gitlab_url=project.gitlab_url,
            api_base_url=project.api_base_url,
            project_url=project.project_url,
            project_path=project.project_path,
            project_id=PROJECT_ID,
        )

        def wrong_id_request(_url, _headers):
            return _json_response(
                {
                    "id": PROJECT_ID + 1,
                    "web_url": "https://git-p.example/group/project",
                    "path_with_namespace": project.project_path,
                }
            )

        with self.assertRaisesRegex(
            SourceManagerError,
            "wrong identity",
        ):
            _fetch_project_identity(
                pinned_project,
                wrong_id_request,
                {"PRIVATE-TOKEN": TOKEN},
            )

    def test_dual_hostname_fetch_keeps_all_token_requests_on_access_host(
        self,
    ) -> None:
        api = _GitLabApi(
            {1: [_summary(1, notes=1)]},
            discussion_pages={
                1: {
                    1: [
                        _discussion(
                            "discussion-1",
                            1,
                            "internal transport only",
                            created_at="2026-07-30T01:00:00.000Z",
                        )
                    ]
                }
            },
            project_payload={
                "id": PROJECT_ID,
                "web_url": (
                    "https://gitlab-public.example.invalid/"
                    "external/group/subgroup/project"
                ),
                "path_with_namespace": PROJECT_PATH,
            },
        )

        self.fetch(api)

        self.assertTrue(api.inventory_urls())
        self.assertEqual([1], api.detail_iids())
        self.assertTrue(
            any(
                urlsplit(url).path.endswith("/discussions")
                for url in api.urls()
            )
        )
        for url, headers in api.calls:
            self.assertEqual("gitlab.example.invalid", urlsplit(url).hostname)
            self.assertTrue(urlsplit(url).path.startswith("/gitlab/api/v4/"))
            self.assertEqual(TOKEN, headers.get("PRIVATE-TOKEN"))
            self.assertNotIn("gitlab-public.example.invalid", url)
        markdown = (self.work / "issues" / "1.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(PROJECT_URL, markdown)
        self.assertNotIn("gitlab-public.example.invalid", markdown)

    def test_inventory_reads_a_following_page_when_pagination_headers_are_absent(
        self,
    ) -> None:
        first_page = [_summary(iid) for iid in range(1, 101)]
        second_page = [_summary(101)]
        project = GitLabProject(
            gitlab_url=GITLAB_URL,
            api_base_url=API_BASE_URL,
            project_url=PROJECT_URL,
            project_path=PROJECT_PATH,
            project_id=PROJECT_ID,
        )
        calls: list[str] = []

        def request(
            url: str,
            _headers: Mapping[str, str],
        ) -> tuple[int, bytes, Mapping[str, str]]:
            calls.append(url)
            page = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
            return _json_response(
                first_page if page == 1 else second_page if page == 2 else []
            )

        inventory = _fetch_inventory(
            project,
            request,
            {"PRIVATE-TOKEN": TOKEN},
            progress_callback=None,
        )

        self.assertEqual(101, len(inventory))
        self.assertEqual([1, 2], [
            int(parse_qs(urlsplit(url).query)["page"][0])
            for url in calls
        ])
        for url in calls:
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(["all"], query.get("scope"))
            self.assertEqual(["all"], query.get("state"))

    def test_all_discussion_pages_are_written_in_stable_note_order(self) -> None:
        first = _discussion(
            "discussion-a",
            20,
            "second chronological comment",
            created_at="2026-07-30T02:00:00.000Z",
        )
        second = _discussion(
            "discussion-b",
            10,
            "first chronological comment",
            created_at="2026-07-30T01:00:00.000Z",
            system=True,
        )
        api = _GitLabApi(
            {1: [_summary(1, notes=2)]},
            discussion_pages={1: {1: [first], 2: [second]}},
        )

        self.fetch(api)

        text = (self.work / "issues" / "1.md").read_text(encoding="utf-8")
        self.assertIn("first chronological comment", text)
        self.assertIn("second chronological comment", text)
        self.assertNotIn('"project_id"', text)
        self.assertLess(
            text.index("first chronological comment"),
            text.index("second chronological comment"),
        )
        self.assertIn("システム履歴", text)
        discussion_urls = [
            url for url in api.urls() if "/discussions?" in url
        ]
        self.assertEqual(2, len(discussion_urls))
        self.assertEqual(
            [1, 2],
            [
                int(parse_qs(urlsplit(url).query)["page"][0])
                for url in discussion_urls
            ],
        )

    def test_private_token_is_redacted_from_headers_body_and_diagnostics(
        self,
    ) -> None:
        sanitized = sanitize_diagnostic(
            'PRIVATE-TOKEN: GITLAB-PRIVATE-TOKEN-DO-NOT-LEAK\n'
            '{"private_token":"GITLAB-PRIVATE-TOKEN-DO-NOT-LEAK"}',
            max_chars=65_536,
        )
        self.assertNotIn(TOKEN, sanitized)
        self.assertIn("<REDACTED>", sanitized)

        captured_headers: list[dict[str, str]] = []

        def getter(
            _url: str,
            headers: Mapping[str, str],
            _timeout: float,
        ) -> tuple[int, bytes, Mapping[str, str]]:
            captured_headers.append(dict(headers))
            return (
                401,
                json.dumps(
                    {
                        "private_token": TOKEN,
                        "message": "401 Unauthorized",
                    }
                ).encode("utf-8"),
                {
                    "Content-Type": "application/json",
                    "Set-Cookie": f"session={TOKEN}",
                },
            )

        plan = {
            "provider": "gitlab_issues",
            "steps": [{"parameters": _settings()}],
        }
        with self.assertRaises(SourceManagerError) as captured:
            execute_fetch_plan(
                plan,
                self.work,
                {},
                http_get=getter,
                environment={TOKEN_ENV: TOKEN},
            )

        self.assertEqual(TOKEN, captured_headers[0]["PRIVATE-TOKEN"])
        diagnostic = json.dumps(
            getattr(captured.exception, "diagnostic", {}),
            ensure_ascii=False,
        ) + str(captured.exception)
        self.assertNotIn(TOKEN, diagnostic)
        self.assertIn("<REDACTED>", diagnostic)

    def test_fractional_seconds_are_not_collapsed_for_change_detection(
        self,
    ) -> None:
        first = datetime.fromisoformat(
            "2026-07-30T00:00:00.100+00:00"
        )
        second = datetime.fromisoformat(
            "2026-07-30T00:00:00.900+00:00"
        )
        first_text = _format_timestamp(first)
        second_text = _format_timestamp(second)
        self.assertNotEqual(first_text, second_text)

        issues = self.work / "issues"
        issues.mkdir()
        (issues / "5.md").write_text(
            _marker_markdown(5, first_text),
            encoding="utf-8",
        )
        inventory = [
            GitLabIssueInventoryItem(
                iid=5,
                issue_id=10_005,
                updated_at=second,
                updated_at_text=second_text,
                user_notes_count=0,
            )
        ]
        self.assertEqual(
            [5],
            _changed_issue_iids(
                inventory,
                issues,
                updated_after=None,
            ),
        )

    def test_local_marker_iid_must_match_the_file_and_remote_issue(self) -> None:
        timestamp = datetime(
            2026,
            7,
            30,
            tzinfo=timezone.utc,
        )
        timestamp_text = _format_timestamp(timestamp)
        issues = self.work / "issues"
        issues.mkdir()
        (issues / "5.md").write_text(
            _marker_markdown(99, timestamp_text),
            encoding="utf-8",
        )
        inventory = [
            GitLabIssueInventoryItem(
                iid=5,
                issue_id=10_005,
                updated_at=timestamp,
                updated_at_text=timestamp_text,
                user_notes_count=0,
            )
        ]

        self.assertEqual(
            [5],
            _changed_issue_iids(
                inventory,
                issues,
                updated_after=None,
            ),
        )

    def test_updated_after_cutoff_is_utc_and_resume_stable(self) -> None:
        started_at = "2026-07-30T12:34:56Z"
        first = gitlab_issues_updated_after(
            30,
            {"started_at": started_at},
        )
        self.assertEqual("2026-06-30T12:34:56Z", first)
        resumed = gitlab_issues_updated_after(
            30,
            {
                "started_at": "2027-01-01T00:00:00Z",
                GITLAB_ISSUES_CUTOFF_STATE_KEY: first,
            },
        )
        self.assertEqual(first, resumed)
        self.assertIsNone(
            gitlab_issues_updated_after(
                None,
                {"started_at": started_at},
            )
        )

    def test_updated_after_is_sent_to_the_gitlab_inventory_api(self) -> None:
        cutoff = "2026-07-01T12:34:56Z"
        api = _GitLabApi({1: [_summary(1)]})

        self.fetch(api, updated_after=cutoff)

        inventory_url = api.inventory_urls()[0]
        self.assertEqual(
            [cutoff],
            parse_qs(urlsplit(inventory_url).query)["updated_after"],
        )

    def test_inventory_snapshot_is_frozen_and_resume_skips_inventory(self) -> None:
        issues = self.work / "issues"
        issues.mkdir()
        (issues / "99.md").write_text(
            _marker_markdown(
                99,
                "2026-07-01T00:00:00.000Z",
            ),
            encoding="utf-8",
        )
        api = _GitLabApi(
            {1: [_summary(iid) for iid in range(1, 8)]}
        )
        snapshots: list[tuple[int, list[int]]] = []

        class PauseAfterFive(RuntimeError):
            pass

        def pause(completed: int, _iid: int) -> None:
            if completed == 5:
                raise PauseAfterFive("fixture interruption")

        def snapshot(project_id: int, changed: list[int]) -> None:
            snapshots.append((project_id, list(changed)))

        with self.assertRaises(PauseAfterFive):
            self.fetch(
                api,
                item_callback=pause,
                inventory_snapshot_callback=snapshot,
            )

        self.assertEqual(
            [(PROJECT_ID, list(range(1, 8)))],
            snapshots,
        )
        self.assertEqual(list(range(1, 6)), api.detail_iids())
        self.assertTrue((issues / "99.md").exists())

        api.calls.clear()
        resumed_items: list[tuple[int, int]] = []
        resumed_batches: list[tuple[int, int]] = []
        result = self.fetch(
            api,
            item_callback=lambda completed, iid: resumed_items.append(
                (completed, iid)
            ),
            batch_callback=lambda completed, iid: resumed_batches.append(
                (completed, iid)
            ),
            resume_count=5,
            stable_issue_ids=list(range(1, 8)),
            stable_project_id=PROJECT_ID,
            inventory_snapshot_callback=lambda *_args: self.fail(
                "resume must not replace the frozen inventory snapshot"
            ),
        )

        self.assertEqual([], api.inventory_urls())
        self.assertEqual([6, 7], api.detail_iids())
        self.assertEqual([(6, 6), (7, 7)], resumed_items)
        self.assertEqual([(7, 7)], resumed_batches)
        self.assertEqual(7, result["documents"])

    def test_resume_rejects_a_different_project_identity_before_mutation(
        self,
    ) -> None:
        api = _GitLabApi({1: [_summary(1)]})

        with self.assertRaisesRegex(
            SourceManagerError,
            "resume project identity",
        ):
            self.fetch(
                api,
                resume_count=0,
                stable_issue_ids=[1],
                stable_project_id=PROJECT_ID + 1,
            )

        self.assertEqual([PROJECT_LOOKUP_URL], api.urls())
        self.assertFalse((self.work / "issues").exists())

    def test_reflection_callback_runs_for_each_five_and_final_partial_batch(
        self,
    ) -> None:
        api = _GitLabApi(
            {1: [_summary(iid) for iid in range(1, 13)]}
        )
        items: list[tuple[int, int]] = []
        batches: list[tuple[int, int]] = []

        self.fetch(
            api,
            item_callback=lambda completed, iid: items.append(
                (completed, iid)
            ),
            batch_callback=lambda completed, iid: batches.append(
                (completed, iid)
            ),
        )

        self.assertEqual(
            [(iid, iid) for iid in range(1, 13)],
            items,
        )
        self.assertEqual([(5, 5), (10, 10), (12, 12)], batches)
        for iid in range(1, 13):
            detail_fragment = f"/issues/{iid}"
            discussion_fragment = f"/issues/{iid}/discussions"
            detail_index = next(
                index
                for index, url in enumerate(api.urls())
                if urlsplit(url).path.endswith(detail_fragment)
            )
            discussion_index = next(
                index
                for index, url in enumerate(api.urls())
                if discussion_fragment in urlsplit(url).path
            )
            self.assertLess(detail_index, discussion_index)
            if iid < 12:
                next_detail_index = next(
                    index
                    for index, url in enumerate(api.urls())
                    if urlsplit(url).path.endswith(f"/issues/{iid + 1}")
                )
                self.assertLess(discussion_index, next_detail_index)

    def test_remote_deletion_keeps_historical_markdown_without_reflection(
        self,
    ) -> None:
        issues = self.work / "issues"
        issues.mkdir()
        target = issues / "99.md"
        target.write_text(
            _marker_markdown(
                99,
                "2026-07-01T00:00:00.000Z",
            ),
            encoding="utf-8",
        )
        api = _GitLabApi({1: []})
        snapshots: list[tuple[int, list[int]]] = []
        batches: list[tuple[int, int]] = []

        outcome = self.fetch(
            api,
            batch_callback=lambda completed, iid: batches.append(
                (completed, iid)
            ),
            inventory_snapshot_callback=lambda project_id, changed: snapshots.append(
                (project_id, list(changed))
            ),
        )

        self.assertEqual([(PROJECT_ID, [])], snapshots)
        self.assertTrue(target.exists())
        self.assertEqual(1, outcome["local_documents"])
        self.assertNotIn("deleted_this_run", outcome)
        self.assertEqual([], batches)

    def test_detail_404_keeps_the_old_issue_and_continues(self) -> None:
        issues = self.work / "issues"
        issues.mkdir()
        target = issues / "3.md"
        target.write_text(
            _marker_markdown(
                3,
                "2026-07-01T00:00:00.000Z",
            ),
            encoding="utf-8",
        )
        api = _GitLabApi(
            {1: [_summary(3)]},
            detail_errors={
                3: _http_error(404, "Issue disappeared during refresh")
            },
        )
        items: list[tuple[int, int]] = []
        batches: list[tuple[int, int]] = []
        no_changes: list[tuple[int, int]] = []

        outcome = self.fetch(
            api,
            item_callback=lambda completed, iid: items.append(
                (completed, iid)
            ),
            batch_callback=lambda completed, iid: batches.append(
                (completed, iid)
            ),
            no_change_callback=lambda completed, iid: no_changes.append(
                (completed, iid)
            ),
        )

        self.assertTrue(target.exists())
        self.assertEqual(1, outcome["unavailable_this_run"])
        self.assertEqual(0, outcome["fetched_this_run"])
        self.assertNotIn("deleted_this_run", outcome)
        self.assertEqual([(1, 3)], items)
        self.assertEqual([(1, 3)], no_changes)
        self.assertEqual([], batches)

    def test_reflection_batch_counts_written_issues_not_unavailable_issues(
        self,
    ) -> None:
        api = _GitLabApi(
            {1: [_summary(iid) for iid in range(1, 7)]},
            detail_errors={
                1: _http_error(404, "Issue is no longer visible")
            },
        )
        batches: list[tuple[int, int]] = []

        outcome = self.fetch(
            api,
            batch_callback=lambda completed, iid: batches.append(
                (completed, iid)
            ),
        )

        self.assertEqual(5, outcome["fetched_this_run"])
        self.assertEqual(1, outcome["unavailable_this_run"])
        self.assertEqual([(6, 6)], batches)

    def test_discussion_failure_keeps_previous_complete_markdown_unchanged(
        self,
    ) -> None:
        for status in (404, 500):
            with self.subTest(status=status):
                case_work = self.root / f"work-{status}"
                case_work.mkdir()
                issues = case_work / "issues"
                issues.mkdir()
                target = issues / "4.md"
                previous = _marker_markdown(
                    4,
                    "2026-07-01T00:00:00.000Z",
                    body=f"complete previous content for {status}",
                )
                target.write_text(previous, encoding="utf-8")
                api = _GitLabApi(
                    {1: [_summary(4)]},
                    discussion_errors={
                        4: _http_error(
                            status,
                            "discussion retrieval failed",
                        )
                    },
                )

                with self.assertRaises(SourceManagerError):
                    fetch_gitlab_issues(
                        _settings(),
                        case_work,
                        api,
                        {TOKEN_ENV: TOKEN},
                        item_callback=None,
                        batch_callback=None,
                        resume_count=0,
                        stable_issue_ids=None,
                        stable_project_id=None,
                        inventory_snapshot_callback=None,
                        updated_after=None,
                        progress_callback=None,
                    )

                self.assertEqual(
                    previous,
                    target.read_text(encoding="utf-8"),
                )

    def test_atomic_publish_failure_keeps_old_file_and_cleans_temporary(
        self,
    ) -> None:
        target = self.work / "issue.md"
        target.write_text("old complete content\n", encoding="utf-8")

        with (
            mock.patch.object(
                gitlab_issues_module.os,
                "replace",
                side_effect=PermissionError("replace denied"),
            ),
            self.assertRaises(PermissionError),
        ):
            _atomic_write_text(target, "new incomplete content\n")

        self.assertEqual(
            "old complete content\n",
            target.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            ["issue.md"],
            sorted(path.name for path in self.work.iterdir()),
        )

    def test_wrong_project_identity_stops_before_inventory_or_local_mutation(
        self,
    ) -> None:
        issues = self.work / "issues"
        issues.mkdir()
        target = issues / "8.md"
        previous = _marker_markdown(
            8,
            "2026-07-01T00:00:00.000Z",
        )
        target.write_text(previous, encoding="utf-8")
        api = _GitLabApi(
            {1: []},
            project_payload={
                "id": 202,
                "web_url": (
                    f"{GITLAB_URL}/other-group/other-project"
                ),
                "path_with_namespace": "other-group/other-project",
            },
        )

        with self.assertRaises(SourceManagerError):
            self.fetch(api)

        self.assertEqual([PROJECT_LOOKUP_URL], api.urls())
        self.assertEqual(previous, target.read_text(encoding="utf-8"))

    def test_wrong_inventory_project_and_invalid_schema_never_start_details(
        self,
    ) -> None:
        wrong_project = _GitLabApi(
            {1: [_summary(1, project_id=999)]}
        )
        with self.assertRaises(SourceManagerError):
            self.fetch(wrong_project)
        self.assertEqual([], wrong_project.detail_iids())

        malformed_calls: list[str] = []

        def malformed(
            url: str,
            _headers: Mapping[str, str],
        ) -> tuple[int, bytes, Mapping[str, str]]:
            malformed_calls.append(url)
            if url == PROJECT_LOOKUP_URL:
                return _json_response(
                    {
                        "id": PROJECT_ID,
                        "web_url": PROJECT_URL,
                        "path_with_namespace": PROJECT_PATH,
                    }
                )
            return _json_response({"issues": []})

        with self.assertRaises(SourceManagerError):
            self.fetch(malformed)
        self.assertEqual(2, len(malformed_calls))

    def test_unsafe_work_paths_and_issue_ids_cannot_escape(self) -> None:
        external = self.root / "external"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("do not change\n", encoding="utf-8")
        linked_work = self.root / "linked-work"
        linked_work.mkdir()
        try:
            (linked_work / "issues").symlink_to(
                external,
                target_is_directory=True,
            )
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        api = _GitLabApi({1: []})

        with self.assertRaises(SourceManagerError):
            fetch_gitlab_issues(
                _settings(),
                linked_work,
                api,
                {TOKEN_ENV: TOKEN},
                item_callback=None,
                batch_callback=None,
                resume_count=0,
                stable_issue_ids=None,
                stable_project_id=None,
                inventory_snapshot_callback=None,
                updated_after=None,
                progress_callback=None,
            )
        self.assertEqual(
            "do not change\n",
            sentinel.read_text(encoding="utf-8"),
        )

        invalid = _GitLabApi(
            {
                1: [
                    {
                        **_summary(1),
                        "iid": "../escape",
                    }
                ]
            }
        )
        with self.assertRaises(SourceManagerError):
            self.fetch(invalid)
        self.assertFalse((self.root / "escape.md").exists())

    def test_project_url_must_be_a_top_page_under_the_gitlab_mount(self) -> None:
        unsafe_settings = (
            _settings(
                project_url=f"{PROJECT_URL}/-/issues/1",
            ),
            _settings(
                project_url=(
                    "https://user:secret@gitlab.example.invalid/"
                    "gitlab/group/subgroup/project"
                ),
            ),
            _settings(
                project_url=(
                    "https://other.example.invalid/"
                    "gitlab/group/subgroup/project"
                ),
            ),
        )
        for settings in unsafe_settings:
            with self.subTest(settings=settings):
                calls: list[str] = []

                def request(
                    url: str,
                    _headers: Mapping[str, str],
                ) -> tuple[int, bytes, Mapping[str, str]]:
                    calls.append(url)
                    raise AssertionError("unsafe settings reached the network")

                with self.assertRaises(SourceManagerError):
                    self.fetch(request, settings=settings)
                self.assertEqual([], calls)

    def test_missing_or_blank_token_fails_before_any_network_request(
        self,
    ) -> None:
        for environment in ({}, {TOKEN_ENV: "   "}):
            with self.subTest(environment=environment):
                calls: list[str] = []

                def request(
                    url: str,
                    _headers: Mapping[str, str],
                ) -> tuple[int, bytes, Mapping[str, str]]:
                    calls.append(url)
                    raise AssertionError("missing token reached the network")

                with self.assertRaisesRegex(
                    SourceManagerError,
                    "token",
                ):
                    self.fetch(
                        request,
                        environment=environment,
                    )
                self.assertEqual([], calls)

    def test_token_environment_is_bound_before_environment_lookup(
        self,
    ) -> None:
        calls: list[str] = []

        def request(
            url: str,
            _headers: Mapping[str, str],
        ) -> tuple[int, bytes, Mapping[str, str]]:
            calls.append(url)
            raise AssertionError("untrusted token_env reached the network")

        with self.assertRaisesRegex(
            SourceManagerError,
            "does not match",
        ):
            self.fetch(
                request,
                settings=_settings(token_env="AWS_SECRET_ACCESS_KEY"),
                environment={
                    "AWS_SECRET_ACCESS_KEY": "UNRELATED-SECRET",
                },
            )
        self.assertEqual([], calls)

    def test_invalid_json_diagnostic_redacts_token_shaped_body(
        self,
    ) -> None:
        leaked = "INVALID-JSON-GITLAB-SECRET"

        def request(
            _url: str,
            _headers: Mapping[str, str],
        ) -> tuple[int, bytes, Mapping[str, str]]:
            return (
                200,
                f"PRIVATE-TOKEN={leaked} not-json".encode("utf-8"),
                {},
            )

        with self.assertRaises(SourceManagerError) as captured:
            self.fetch(request)
        diagnostic = getattr(captured.exception, "diagnostic", {})
        self.assertNotIn(leaked, json.dumps(diagnostic))
        self.assertNotIn("body_preview", diagnostic)
        self.assertEqual(
            64,
            len(str(diagnostic.get("body_sha256") or "")),
        )


if __name__ == "__main__":
    unittest.main()
