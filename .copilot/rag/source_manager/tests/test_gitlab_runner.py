from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from source_manager import (
    SourceManagerError,
    SourceStore,
    register_source,
    update_source,
    update_source_configuration,
)
from source_manager.gitlab_issues import (
    GITLAB_ISSUE_IDS_STATE_KEY,
    GITLAB_PROJECT_ID_STATE_KEY,
)
from source_manager.machine_connections import gitlab_token_env
from source_manager.metadata import _canonical_source
from source_manager.tests.test_gitlab_issues import (
    GITLAB_URL,
    PROJECT_ID,
    PROJECT_URL,
    TOKEN,
    _GitLabApi,
    _settings,
    _summary,
)


RAG_ROOT = Path(__file__).resolve().parents[2]
TOKEN_ENV = gitlab_token_env(GITLAB_URL)


def _add_summary(source_id: str, file_count: int) -> dict[str, Any]:
    return {
        "operation": "add",
        "source_id": source_id,
        "file_count": file_count,
        "indexed_files": file_count,
        "skipped_files": 0,
        "error_files": 0,
        "upserted_records": file_count,
        "deleted_records": 0,
    }


class _AddRunner:
    def __init__(
        self,
        *,
        fail_calls: set[int] | None = None,
        partial_error_calls: set[int] | None = None,
    ) -> None:
        self.fail_calls = set(fail_calls or ())
        self.partial_error_calls = set(partial_error_calls or ())
        self.calls: list[dict[str, Any]] = []

    def __call__(self, arguments: list[str]) -> SimpleNamespace:
        root = Path(arguments[arguments.index("--root") + 1])
        source_id = arguments[arguments.index("--source-id") + 1]
        issue_iids = sorted(
            int(path.stem) for path in root.glob("issues/*.md")
        )
        call_number = len(self.calls) + 1
        self.calls.append(
            {
                "arguments": list(arguments),
                "source_id": source_id,
                "issue_iids": issue_iids,
            }
        )
        if call_number in self.fail_calls:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f"fixture ADD failure {call_number}",
            )
        summary = _add_summary(source_id, len(issue_iids))
        if call_number in self.partial_error_calls:
            summary["error_files"] = 1
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                summary,
                ensure_ascii=False,
            ),
            stderr="",
        )


class GitLabIssueRunnerContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-gitlab-runner-"
        )
        self.root = Path(self.temporary.name)
        self.db_root = self.root / "fixture-rag"
        self.db_root.mkdir()
        self.python_executable = self.root / "venv-python"
        self.store = SourceStore(self.db_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self) -> dict[str, Any]:
        return register_source(
            self.db_root,
            source_type="gitlab_issues",
            display_name="GitLab tickets",
            fetch=_settings(token_env=TOKEN_ENV),
        )

    @staticmethod
    def getter(api: _GitLabApi):
        def get(
            url: str,
            headers: Mapping[str, str],
            _timeout: float,
        ):
            return api(url, headers)

        return get

    def update(
        self,
        local_source_key: str,
        api: _GitLabApi,
        add: _AddRunner,
        *,
        metadata_publisher: Any = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        return update_source(
            self.db_root,
            local_source_key,
            python_executable=self.python_executable,
            rag_root=RAG_ROOT,
            command_runner=add,
            http_get=self.getter(api),
            environment={TOKEN_ENV: TOKEN},
            metadata_publisher=(
                metadata_publisher
                if metadata_publisher is not None
                else lambda *_args: None
            ),
            progress_callback=progress_callback,
        )

    def state(self, local_source_key: str) -> dict[str, Any]:
        return self.store.read_state(local_source_key).payload

    def test_initial_seven_issues_reflect_at_five_and_final_seven(
        self,
    ) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        api = _GitLabApi(
            {1: [_summary(iid) for iid in range(1, 8)]}
        )
        add = _AddRunner()

        result = self.update(key, api, add)

        self.assertEqual("updated", result["status"])
        self.assertEqual(7, result["indexed_confirmed_count"])
        self.assertEqual(
            [list(range(1, 6)), list(range(1, 8))],
            [call["issue_iids"] for call in add.calls],
        )
        self.assertEqual(list(range(1, 8)), api.detail_iids())
        state = self.state(key)
        self.assertEqual("complete", state["phase"])
        self.assertFalse(state["can_resume"])
        self.assertEqual(
            list(range(1, 8)),
            state[GITLAB_ISSUE_IDS_STATE_KEY],
        )
        self.assertNotIn(GITLAB_PROJECT_ID_STATE_KEY, state)
        self.assertEqual(7, state["indexed_confirmed_count"])
        self.assertEqual(0, state["pending_count"])

    def test_add_failure_resumes_frozen_inventory_at_first_unfetched_issue(
        self,
    ) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        api = _GitLabApi(
            {1: [_summary(iid) for iid in range(1, 8)]}
        )
        add = _AddRunner(fail_calls={1})

        with self.assertRaisesRegex(SourceManagerError, "ADD failed"):
            self.update(key, api, add)

        interrupted = self.state(key)
        self.assertEqual("reflect", interrupted["phase"])
        self.assertTrue(interrupted["can_resume"])
        self.assertEqual(5, interrupted["fetched_count"])
        self.assertEqual(0, interrupted["indexed_confirmed_count"])
        self.assertEqual(5, interrupted["pending_count"])
        self.assertEqual(
            list(range(1, 8)),
            interrupted[GITLAB_ISSUE_IDS_STATE_KEY],
        )
        self.assertEqual(
            PROJECT_ID,
            interrupted[GITLAB_PROJECT_ID_STATE_KEY],
        )
        self.assertEqual(list(range(1, 6)), api.detail_iids())
        self.assertEqual(1, len(api.inventory_urls()))

        api.calls.clear()
        result = self.update(key, api, add)

        self.assertEqual("updated", result["status"])
        self.assertEqual([], api.inventory_urls())
        self.assertEqual([6, 7], api.detail_iids())
        self.assertEqual(
            [
                list(range(1, 6)),
                list(range(1, 6)),
                list(range(1, 8)),
            ],
            [call["issue_iids"] for call in add.calls],
        )
        final = self.state(key)
        self.assertEqual("complete", final["phase"])
        self.assertFalse(final["can_resume"])
        self.assertNotIn(GITLAB_PROJECT_ID_STATE_KEY, final)
        self.assertEqual(7, final["indexed_confirmed_count"])
        self.assertEqual(0, final["pending_count"])

    def test_partial_add_errors_stay_resumable_and_retry_errors_is_used(
        self,
    ) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        api = _GitLabApi({1: [_summary(1)]})
        add = _AddRunner(partial_error_calls={1})

        with self.assertRaisesRegex(SourceManagerError, "抽出に失敗"):
            self.update(key, api, add)

        interrupted = self.state(key)
        self.assertEqual("reflect", interrupted["phase"])
        self.assertTrue(interrupted["can_resume"])
        self.assertEqual(1, interrupted["pending_count"])
        self.assertEqual(0, interrupted["indexed_confirmed_count"])
        self.assertIn("--retry-errors", add.calls[0]["arguments"])

        api.calls.clear()
        result = self.update(key, api, add)

        self.assertEqual("updated", result["status"])
        self.assertEqual([], api.inventory_urls())
        self.assertEqual([], api.detail_iids())
        self.assertEqual(2, len(add.calls))
        final = self.state(key)
        self.assertEqual("complete", final["phase"])
        self.assertEqual(1, final["indexed_confirmed_count"])
        self.assertEqual(0, final["pending_count"])

    def test_remote_deletion_keeps_historical_issue_without_add(
        self,
    ) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        add = _AddRunner()
        self.update(
            key,
            _GitLabApi({1: [_summary(1)]}),
            add,
        )
        work = self.store.paths(key).absolute(
            self.db_root,
            self.store.paths(key).work_directory,
        )
        self.assertTrue((work / "issues" / "1.md").is_file())

        deleted_api = _GitLabApi({1: []})
        result = self.update(key, deleted_api, add)

        self.assertEqual("updated", result["status"])
        self.assertEqual(1, len(deleted_api.inventory_urls()))
        self.assertEqual([], deleted_api.detail_iids())
        self.assertEqual(1, len(add.calls))
        self.assertTrue((work / "issues" / "1.md").is_file())
        final = self.state(key)
        self.assertEqual("complete", final["phase"])
        self.assertEqual([], final[GITLAB_ISSUE_IDS_STATE_KEY])

    def test_unavailable_issue_checkpoint_resumes_at_the_next_issue(
        self,
    ) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        add = _AddRunner()
        self.update(
            key,
            _GitLabApi({1: [_summary(1)]}),
            add,
        )

        interrupted_api = _GitLabApi(
            {
                1: [
                    _summary(
                        1,
                        updated_at="2026-07-30T00:00:00.200Z",
                    ),
                    _summary(
                        2,
                        updated_at="2026-07-30T01:00:00Z",
                    ),
                ]
            },
            detail_statuses={
                1: 404,
                2: 500,
            },
        )
        with self.assertRaises(SourceManagerError):
            self.update(key, interrupted_api, add)

        interrupted = self.state(key)
        self.assertEqual("fetch", interrupted["phase"])
        self.assertEqual(1, interrupted["indexed_confirmed_count"])
        self.assertEqual(0, interrupted["pending_count"])
        self.assertEqual(1, len(add.calls))

        resumed_api = _GitLabApi(
            {
                1: [
                    _summary(
                        1,
                        updated_at="2026-07-30T00:00:00.200Z",
                    ),
                    _summary(
                        2,
                        updated_at="2026-07-30T01:00:00Z",
                    ),
                ]
            }
        )
        result = self.update(key, resumed_api, add)

        self.assertEqual("updated", result["status"])
        self.assertEqual([], resumed_api.inventory_urls())
        self.assertEqual([2], resumed_api.detail_iids())
        self.assertEqual(2, len(add.calls))
        self.assertEqual(
            [1, 2],
            add.calls[-1]["issue_iids"],
        )

    def test_unchanged_inventory_does_not_run_add(self) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        add = _AddRunner()
        self.update(
            key,
            _GitLabApi({1: [_summary(1)]}),
            add,
        )
        self.assertEqual(1, len(add.calls))

        unchanged_api = _GitLabApi({1: [_summary(1)]})
        result = self.update(key, unchanged_api, add)

        self.assertEqual("updated", result["status"])
        self.assertEqual(1, len(add.calls))
        self.assertEqual([], unchanged_api.detail_iids())
        self.assertEqual(1, len(unchanged_api.inventory_urls()))

    def test_indexed_project_is_immutable_but_update_window_can_change(
        self,
    ) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        self.update(
            key,
            _GitLabApi({1: [_summary(1)]}),
            _AddRunner(),
        )

        with self.assertRaisesRegex(
            SourceManagerError,
            "add_new_source",
        ):
            update_source_configuration(
                self.db_root,
                key,
                fetch={
                    **_settings(),
                    "project_url": f"{GITLAB_URL}/group/another-project",
                },
            )

        update_source_configuration(
            self.db_root,
            key,
            fetch={
                **_settings(),
                "updated_within_days": 90,
            },
        )
        saved = self.store.read_source(key).payload["fetch"]
        self.assertEqual(PROJECT_URL, saved["project_url"])
        self.assertEqual(90, saved["updated_within_days"])

    def test_metadata_publication_contains_gitlab_issue_link(
        self,
    ) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        published: list[dict[str, Any]] = []

        def publish(
            _db_root: Path,
            source: Mapping[str, Any],
            _rag_root: Path,
        ) -> None:
            published.append(_canonical_source(source))

        result = self.update(
            key,
            _GitLabApi({1: [_summary(1)]}),
            _AddRunner(),
            metadata_publisher=publish,
        )

        self.assertEqual("updated", result["status"])
        self.assertEqual(1, len(published))
        self.assertEqual(
            {
                "source_id": key,
                "display_name": "GitLab tickets",
                "source_type": "gitlab_issues",
                "link": {
                    "enabled": True,
                    "strategy": "regex-template",
                    "settings": {
                        "path_pattern": (
                            r"^issues/(?P<issue_iid>[0-9]+)\.md$"
                        ),
                        "url_template": (
                            f"{PROJECT_URL}/-/issues/{{issue_iid}}"
                        ),
                    },
                },
            },
            published[0],
        )
        source = self.store.read_source(key).payload
        self.assertEqual(key, source["source_id"])
        self.assertFalse(source["metadata_sync_pending"])
        self.assertNotIn("pending_metadata", source)

    def test_metadata_only_resume_clears_transient_project_checkpoint(
        self,
    ) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        add = _AddRunner()

        def fail_publish(*_args: Any) -> None:
            raise RuntimeError("fixture metadata failure")

        interrupted_result = self.update(
            key,
            _GitLabApi({1: [_summary(1)]}),
            add,
            metadata_publisher=fail_publish,
        )

        self.assertEqual(
            "metadata_sync_pending",
            interrupted_result["status"],
        )
        interrupted = self.state(key)
        self.assertEqual("metadata", interrupted["phase"])
        self.assertEqual(
            PROJECT_ID,
            interrupted[GITLAB_PROJECT_ID_STATE_KEY],
        )

        resumed = self.update(
            key,
            _GitLabApi({1: []}),
            add,
            metadata_publisher=lambda *_args: None,
        )

        self.assertEqual(
            "metadata_sync",
            resumed["resumed_operation"],
        )
        self.assertEqual(1, len(add.calls))
        completed = self.state(key)
        self.assertEqual("complete", completed["phase"])
        self.assertNotIn(GITLAB_PROJECT_ID_STATE_KEY, completed)

    def test_http_events_never_persist_or_emit_the_private_token(
        self,
    ) -> None:
        registered = self.register()
        key = registered["local_source_key"]
        progress: list[dict[str, Any]] = []
        raw_headers: list[dict[str, str]] = []

        def unauthorized(
            _url: str,
            headers: Mapping[str, str],
            _timeout: float,
        ):
            raw_headers.append(dict(headers))
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

        with self.assertRaises(SourceManagerError):
            update_source(
                self.db_root,
                key,
                python_executable=self.python_executable,
                rag_root=RAG_ROOT,
                command_runner=_AddRunner(),
                http_get=unauthorized,
                environment={TOKEN_ENV: TOKEN},
                metadata_publisher=lambda *_args: None,
                progress_callback=lambda event: progress.append(
                    dict(event)
                ),
            )

        self.assertEqual(TOKEN, raw_headers[0]["PRIVATE-TOKEN"])
        paths = self.store.paths(key)
        events_path = paths.absolute(self.db_root, paths.events_jsonl)
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]
        http_events = [
            event
            for event in events
            if event.get("event") == "gitlab_issues.http_attempt"
        ]
        self.assertEqual(1, len(http_events))
        details = http_events[0]["details"]
        self.assertEqual(401, details["status"])
        self.assertEqual(1, details["request_headers_redacted_count"])
        self.assertNotIn("PRIVATE-TOKEN", details["request_headers"])

        serialized_events = json.dumps(events, ensure_ascii=False)
        serialized_progress = json.dumps(progress, ensure_ascii=False)
        self.assertNotIn(TOKEN, serialized_events)
        self.assertNotIn(TOKEN, serialized_progress)
        self.assertIn("<REDACTED>", serialized_progress)


if __name__ == "__main__":
    unittest.main()
