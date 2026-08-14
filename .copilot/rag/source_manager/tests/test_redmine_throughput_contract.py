from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from source_manager import (
    SourceStore,
    list_sources,
    register_source,
    update_source,
)
from source_manager.errors import SourceManagerError
from source_manager.checkpoints import new_run_state
from source_manager.redmine_contract import (
    REDMINE_STATE_CHECKPOINT_SIZE,
    is_redmine_state_checkpoint,
)


def _summary(source_id: str, file_count: int) -> dict[str, object]:
    return {
        "operation": "add",
        "source_id": source_id,
        "file_count": file_count,
        "indexed_files": file_count,
        "skipped_files": 0,
        "error_files": 0,
        "input_error_files": 0,
        "extract_error_files": 0,
        "error_details": [],
        "upserted_records": file_count,
        "deleted_records": 0,
        "result_status": "success",
    }


class RedmineThroughputContractTests(unittest.TestCase):
    def test_checkpoint_contract_includes_fives_and_exact_tail(self) -> None:
        self.assertEqual(5, REDMINE_STATE_CHECKPOINT_SIZE)
        self.assertEqual(
            [False, False, False, False, True, False, True],
            [is_redmine_state_checkpoint(value, 7) for value in range(1, 8)],
        )

    def test_zero_five_fifty_fifty_one_and_four_hundred_use_one_add(self) -> None:
        for count in (0, 5, 50, 51, 400):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                db_root = Path(temporary) / "fixture-rag"
                db_root.mkdir()
                observed: list[dict[str, object]] = []
                add_calls = 0
                original_save = SourceStore.save_state

                def recording_save(store, key, payload, **kwargs):
                    observed.append(copy.deepcopy(dict(payload)))
                    return original_save(store, key, payload, **kwargs)

                def add(arguments):
                    nonlocal add_calls
                    add_calls += 1
                    source_id = arguments[arguments.index("--source-id") + 1]
                    return SimpleNamespace(
                        returncode=0,
                        stdout=(
                            "@@LOCAL_RAG_RESULT_V1@@"
                            + json.dumps(_summary(source_id, count))
                        ),
                        stderr="",
                    )

                with mock.patch.object(SourceStore, "save_state", new=recording_save):
                    result = register_source(
                        db_root,
                        source_type="redmine",
                        display_name="Throughput fixture",
                        fetch={
                            "project_url": (
                                "https://issues.example.invalid/projects/fixture"
                            ),
                            "updated_within_days": None,
                            "api_key_env": "REDMINE_TEST_KEY",
                        },
                        start=True,
                        python_executable=Path(temporary) / "python.exe",
                        rag_root=Path(temporary) / "rag",
                        command_runner=add,
                        http_get=self._getter(count),
                        environment={"REDMINE_TEST_KEY": "fixture"},
                        metadata_publisher=lambda *_args: None,
                    )

                fetch_checkpoints = [
                    int(value.get("fetched_count") or 0)
                    for value in observed
                    if value.get("phase") == "fetch"
                    and int(value.get("pending_count") or 0) > 0
                ]
                expected = list(range(5, count + 1, 5))
                if count and (not expected or expected[-1] != count):
                    expected.append(count)
                self.assertEqual(expected, fetch_checkpoints)
                reflect_starts = [
                    int(value.get("fetched_count") or 0)
                    for value in observed
                    if value.get("phase") == "reflect"
                    and int(value.get("pending_count") or 0) > 0
                ]
                self.assertEqual([count] if count else [], reflect_starts)
                self.assertEqual(1 if count else 0, add_calls)
                self.assertEqual(count, int(result.get("fetched_count") or 0))
                self.assertEqual(
                    count,
                    int(result.get("indexed_confirmed_count") or 0),
                )

    def test_failed_fifth_checkpoint_replays_at_most_five_details(self) -> None:
        total = 10
        first_calls: list[int] = []
        resume_calls: list[int] = []
        failed = False

        with tempfile.TemporaryDirectory() as temporary:
            db_root = Path(temporary) / "fixture-rag"
            db_root.mkdir()
            original_save = SourceStore.save_state

            def failing_save(store, key, payload, **kwargs):
                nonlocal failed
                if (
                    not failed
                    and payload.get("phase") == "fetch"
                    and int(payload.get("fetched_count") or 0) == 5
                ):
                    failed = True
                    raise OSError("injected checkpoint write failure")
                return original_save(store, key, payload, **kwargs)

            def add(arguments):
                source_id = arguments[arguments.index("--source-id") + 1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "@@LOCAL_RAG_RESULT_V1@@"
                        + json.dumps(_summary(source_id, total))
                    ),
                    stderr="",
                )

            with (
                mock.patch.object(SourceStore, "save_state", new=failing_save),
                self.assertRaises(OSError),
            ):
                register_source(
                    db_root,
                    source_type="redmine",
                    display_name="Checkpoint failure fixture",
                    fetch={
                        "project_url": (
                            "https://issues.example.invalid/projects/fixture"
                        ),
                        "updated_within_days": None,
                        "api_key_env": "REDMINE_TEST_KEY",
                    },
                    start=True,
                    python_executable=Path(temporary) / "python.exe",
                    rag_root=Path(temporary) / "rag",
                    command_runner=add,
                    http_get=self._getter(total, detail_calls=first_calls),
                    environment={"REDMINE_TEST_KEY": "fixture"},
                    metadata_publisher=lambda *_args: None,
                )

            source = list_sources(db_root)[0]
            result = update_source(
                db_root,
                source["local_source_key"],
                python_executable=Path(temporary) / "python.exe",
                rag_root=Path(temporary) / "rag",
                command_runner=add,
                http_get=self._getter(total, detail_calls=resume_calls),
                environment={"REDMINE_TEST_KEY": "fixture"},
                metadata_publisher=lambda *_args: None,
            )
            replayed = set(first_calls).intersection(resume_calls)
            self.assertEqual({1, 2, 3, 4, 5}, replayed)
            self.assertLessEqual(len(replayed), 5)
            self.assertEqual(total, result["indexed_confirmed_count"])

    def test_exact_tail_pending_zero_resumes_without_http(self) -> None:
        total = 5
        add_calls = 0
        fail_add = True

        with tempfile.TemporaryDirectory() as temporary:
            db_root = Path(temporary) / "fixture-rag"
            db_root.mkdir()

            def add(arguments):
                nonlocal add_calls
                add_calls += 1
                if fail_add:
                    return SimpleNamespace(returncode=1, stdout="", stderr="fail")
                source_id = arguments[arguments.index("--source-id") + 1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "@@LOCAL_RAG_RESULT_V1@@"
                        + json.dumps(_summary(source_id, total))
                    ),
                    stderr="",
                )

            with self.assertRaises(SourceManagerError):
                register_source(
                    db_root,
                    source_type="redmine",
                    display_name="Exact tail fixture",
                    fetch={
                        "project_url": (
                            "https://issues.example.invalid/projects/fixture"
                        ),
                        "updated_within_days": None,
                        "api_key_env": "REDMINE_TEST_KEY",
                    },
                    start=True,
                    python_executable=Path(temporary) / "python.exe",
                    rag_root=Path(temporary) / "rag",
                    command_runner=add,
                    http_get=self._getter(total),
                    environment={"REDMINE_TEST_KEY": "fixture"},
                    metadata_publisher=lambda *_args: None,
                )

            source = list_sources(db_root)[0]
            store = SourceStore(db_root)
            stored = store.read_state(source["local_source_key"])
            exact_tail = copy.deepcopy(stored.payload)
            exact_tail.update(
                {
                    "status": "interrupted",
                    "phase": "fetch",
                    "pending_count": 0,
                    "last_error": None,
                }
            )
            store.save_state(
                source["local_source_key"],
                exact_tail,
                expected_revision=stored.revision,
                expected_etag=stored.etag,
            )

            fail_add = False

            def no_http(*_args):
                raise AssertionError("exact-tail resume must not issue HTTP")

            result = update_source(
                db_root,
                source["local_source_key"],
                python_executable=Path(temporary) / "python.exe",
                rag_root=Path(temporary) / "rag",
                command_runner=add,
                http_get=no_http,
                environment={"REDMINE_TEST_KEY": "fixture"},
                metadata_publisher=lambda *_args: None,
            )
            self.assertEqual(2, add_calls)
            self.assertEqual("updated", result["status"])
            self.assertEqual(total, result["indexed_confirmed_count"])

    def test_add_is_replayed_when_trusted_confirmation_write_fails(self) -> None:
        total = 5
        add_calls = 0
        confirmation_failed = False

        with tempfile.TemporaryDirectory() as temporary:
            db_root = Path(temporary) / "fixture-rag"
            db_root.mkdir()
            original_save = SourceStore.save_state

            def failing_confirmation(store, key, payload, **kwargs):
                nonlocal confirmation_failed
                if (
                    not confirmation_failed
                    and payload.get("phase") == "fetch"
                    and int(payload.get("fetched_count") or 0) == total
                    and int(payload.get("indexed_confirmed_count") or 0) == total
                    and int(payload.get("pending_count") or 0) == 0
                ):
                    confirmation_failed = True
                    raise OSError("injected trusted-confirmation write failure")
                return original_save(store, key, payload, **kwargs)

            def add(arguments):
                nonlocal add_calls
                add_calls += 1
                source_id = arguments[arguments.index("--source-id") + 1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "@@LOCAL_RAG_RESULT_V1@@"
                        + json.dumps(_summary(source_id, total))
                    ),
                    stderr="",
                )

            with (
                mock.patch.object(
                    SourceStore,
                    "save_state",
                    new=failing_confirmation,
                ),
                self.assertRaises(OSError),
            ):
                register_source(
                    db_root,
                    source_type="redmine",
                    display_name="Confirmation failure fixture",
                    fetch={
                        "project_url": (
                            "https://issues.example.invalid/projects/fixture"
                        ),
                        "updated_within_days": None,
                        "api_key_env": "REDMINE_TEST_KEY",
                    },
                    start=True,
                    python_executable=Path(temporary) / "python.exe",
                    rag_root=Path(temporary) / "rag",
                    command_runner=add,
                    http_get=self._getter(total),
                    environment={"REDMINE_TEST_KEY": "fixture"},
                    metadata_publisher=lambda *_args: None,
                )

            source = list_sources(db_root)[0]

            def no_http(*_args):
                raise AssertionError("durable exact tail must be reused")

            result = update_source(
                db_root,
                source["local_source_key"],
                python_executable=Path(temporary) / "python.exe",
                rag_root=Path(temporary) / "rag",
                command_runner=add,
                http_get=no_http,
                environment={"REDMINE_TEST_KEY": "fixture"},
                metadata_publisher=lambda *_args: None,
            )
            self.assertEqual(2, add_calls)
            self.assertEqual("updated", result["status"])
            self.assertEqual(total, result["indexed_confirmed_count"])

    def test_legacy_partial_reflect_state_defers_to_one_exact_tail_add(self) -> None:
        total = 53
        add_calls = 0
        detail_calls: list[int] = []

        with tempfile.TemporaryDirectory() as temporary:
            db_root = Path(temporary) / "fixture-rag"
            db_root.mkdir()

            def add(arguments):
                nonlocal add_calls
                add_calls += 1
                source_id = arguments[arguments.index("--source-id") + 1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "@@LOCAL_RAG_RESULT_V1@@"
                        + json.dumps(_summary(source_id, total))
                    ),
                    stderr="",
                )

            source = register_source(
                db_root,
                source_type="redmine",
                display_name="Legacy partial reflect fixture",
                fetch={
                    "project_url": (
                        "https://issues.example.invalid/projects/fixture"
                    ),
                    "updated_within_days": None,
                    "api_key_env": "REDMINE_TEST_KEY",
                },
                start=False,
            )
            source_key = str(source["local_source_key"])
            store = SourceStore(db_root)
            issues = store.ensure_work_directory(source_key) / "issues"
            issues.mkdir()
            for issue_id in range(1, 51):
                (issues / f"{issue_id}.md").write_text(
                    f"# Issue {issue_id}\n",
                    encoding="utf-8",
                )
            stored = store.read_state(source_key)
            legacy = new_run_state(store.plan(store.read_source(source_key).payload))
            legacy.update(
                {
                    "status": "interrupted",
                    "phase": "reflect",
                    "redmine_issue_ids": list(range(1, total + 1)),
                    "fetched_count": 50,
                    "indexed_confirmed_count": 0,
                    "pending_count": 50,
                    "last_completed_item": 50,
                    "can_resume": True,
                }
            )
            store.save_state(
                source_key,
                legacy,
                expected_revision=stored.revision,
                expected_etag=stored.etag,
            )

            result = update_source(
                db_root,
                source_key,
                python_executable=Path(temporary) / "python.exe",
                rag_root=Path(temporary) / "rag",
                command_runner=add,
                http_get=self._getter(total, detail_calls=detail_calls),
                environment={"REDMINE_TEST_KEY": "fixture"},
                metadata_publisher=lambda *_args: None,
            )
            self.assertEqual([51, 52, 53], detail_calls)
            self.assertEqual(1, add_calls)
            self.assertEqual(total, result["indexed_confirmed_count"])

    @staticmethod
    def _getter(count: int, *, detail_calls: list[int] | None = None):
        def getter(url, _headers, _timeout):
            split = urlsplit(url)
            if split.path == "/issues.json":
                query = parse_qs(split.query)
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
                ids = list(range(1, count + 1))[offset : offset + limit]
                return 200, json.dumps(
                    {
                        "issues": [{"id": value} for value in ids],
                        "total_count": count,
                    }
                ).encode()
            issue_id = int(split.path.rsplit("/", 1)[-1].split(".")[0])
            if detail_calls is not None:
                detail_calls.append(issue_id)
            return 200, json.dumps(
                {
                    "issue": {
                        "id": issue_id,
                        "subject": f"Issue {issue_id}",
                        "description": "fixture",
                        "journals": [],
                    }
                }
            ).encode()

        return getter


if __name__ == "__main__":
    unittest.main()
