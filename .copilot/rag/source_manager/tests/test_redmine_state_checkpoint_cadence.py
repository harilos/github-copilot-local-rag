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
from source_manager.redmine_contract import (
    REDMINE_ADD_BATCH_SIZE,
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


class RedmineStateCheckpointCadenceTests(unittest.TestCase):
    def test_contract_keeps_outer_fifty_and_internal_five(self) -> None:
        self.assertEqual(50, REDMINE_ADD_BATCH_SIZE)
        self.assertEqual(5, REDMINE_STATE_CHECKPOINT_SIZE)
        self.assertEqual(
            [False, False, False, False, True, False, False, False, False, True],
            [is_redmine_state_checkpoint(value) for value in range(1, 11)],
        )

    def test_all_required_boundaries_persist_fives_and_final_tail(self) -> None:
        cases = (0, 1, 2, 3, 4, 5, 6, 9, 10, 11, 49, 50, 51, 53)
        for count in cases:
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
                    payload = _summary(source_id, count)
                    return SimpleNamespace(
                        returncode=0,
                        stdout="@@LOCAL_RAG_RESULT_V1@@" + json.dumps(payload),
                        stderr="",
                    )

                with mock.patch.object(
                    SourceStore,
                    "save_state",
                    new=recording_save,
                ):
                    result = register_source(
                        db_root,
                        source_type="redmine",
                        display_name="Cadence fixture",
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

                pending_fetches = [
                    int(value.get("fetched_count") or 0)
                    for value in observed
                    if value.get("phase") == "fetch"
                    and int(value.get("pending_count") or 0) > 0
                ]
                self.assertEqual(
                    list(range(5, count + 1, 5)),
                    pending_fetches,
                )
                reflect_starts = [
                    int(value.get("fetched_count") or 0)
                    for value in observed
                    if value.get("phase") == "reflect"
                    and int(value.get("pending_count") or 0) > 0
                ]
                expected_reflects = list(
                    range(REDMINE_ADD_BATCH_SIZE, count + 1, REDMINE_ADD_BATCH_SIZE)
                )
                if count and (not expected_reflects or expected_reflects[-1] != count):
                    expected_reflects.append(count)
                self.assertEqual(expected_reflects, reflect_starts)
                self.assertEqual((count + 49) // 50, add_calls)
                self.assertEqual(count, int(result.get("indexed_confirmed_count") or 0))
                final_state = SourceStore(db_root).read_state(
                    result["local_source_key"]
                ).payload
                self.assertEqual("complete", final_state["status"])
                self.assertEqual(count, int(final_state.get("fetched_count") or 0))
                self.assertEqual(
                    count,
                    int(final_state.get("indexed_confirmed_count") or 0),
                )

    def test_hard_kill_style_resume_replays_at_most_four_details(self) -> None:
        cases = tuple((completed, 11) for completed in range(5, 10)) + (
            (52, 53),
        )
        for completed_before_failure, total in cases:
            with (
                self.subTest(completed=completed_before_failure),
                tempfile.TemporaryDirectory() as temporary,
            ):
                db_root = Path(temporary) / "fixture-rag"
                db_root.mkdir()
                failing_id = completed_before_failure + 1
                first_calls: list[int] = []
                resume_calls: list[int] = []

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

                failing_getter = self._getter(
                    total,
                    detail_calls=first_calls,
                    fail_issue=failing_id,
                )
                with self.assertRaises(SourceManagerError):
                    register_source(
                        db_root,
                        source_type="redmine",
                        display_name="Resume fixture",
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
                        http_get=failing_getter,
                        environment={"REDMINE_TEST_KEY": "fixture"},
                        metadata_publisher=lambda *_args: None,
                    )

                source = list_sources(db_root)[0]
                state = SourceStore(db_root).read_state(
                    source["local_source_key"]
                ).payload
                persisted = int(state.get("fetched_count") or 0)
                self.assertLessEqual(completed_before_failure - persisted, 4)
                self.assertEqual(
                    completed_before_failure // 5 * 5,
                    persisted,
                )

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
                self.assertEqual(list(range(persisted + 1, total + 1)), resume_calls)
                replayed_completed = set(first_calls[:-1]).intersection(resume_calls)
                self.assertEqual(
                    completed_before_failure - persisted,
                    len(replayed_completed),
                )
                self.assertLessEqual(len(replayed_completed), 4)
                self.assertEqual(total, result["indexed_confirmed_count"])

    @staticmethod
    def _getter(
        count: int,
        *,
        detail_calls: list[int] | None = None,
        fail_issue: int | None = None,
    ):
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
            if issue_id == fail_issue:
                raise RuntimeError("injected detail failure")
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
