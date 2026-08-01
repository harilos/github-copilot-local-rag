from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from source_manager import build_fetch_plan, execute_fetch_plan
from source_manager.progress import ProgressRenderer
from source_manager.runner import _redmine_reflect_batch
from source_manager.store import StoredJson


class RedmineProgressCadenceTests(unittest.TestCase):
    def test_human_log_uses_five_ten_and_fifty_cadence(self) -> None:
        output: list[str] = []
        now = [10.0]
        renderer = ProgressRenderer(
            output.append,
            operation="Source追加",
            provider="Redmine",
            is_tty=False,
            clock=lambda: now[0],
        )

        for position in range(1, 54):
            if position % 10 == 0:
                renderer(
                    {
                        "event": "redmine.item",
                        "provider": "redmine",
                        "phase": "redmine.detail",
                        "current_index": position,
                        "total": 53,
                        "current_item": (
                            f"Issue #{position}「対象 {position}」"
                            "— Markdown生成開始"
                        ),
                        "status": "started",
                    }
                )
            renderer(
                {
                    "provider": "redmine",
                    "phase": "redmine.detail",
                    "completed": position,
                    "total": 53,
                    "status": "running",
                    "checkpoint_saved": True,
                }
            )
            if position in {50, 53}:
                previous = 0 if position == 50 else 50
                renderer(
                    {
                        "event": "redmine.add_batch",
                        "provider": "redmine",
                        "phase": "redmine.reflect",
                        "completed": previous,
                        "current_index": position,
                        "total": 53,
                        "status": "started",
                    }
                )
                now[0] += 2.0
                renderer(
                    {
                        "event": "redmine.add_batch",
                        "provider": "redmine",
                        "phase": "redmine.reflect",
                        "completed": position,
                        "total": 53,
                        "documents": position - previous,
                        "status": "success",
                        "checkpoint_saved": True,
                    }
                )

        self.assertEqual(10, output.count("."))
        self.assertEqual(
            5,
            sum("Markdown生成開始" in line for line in output),
        )
        self.assertEqual(2, sum("DB反映開始" in line for line in output))
        success = [line for line in output if "ADD成功" in line]
        self.assertEqual(2, len(success))
        self.assertIn("ADD成功（対象50）", success[0])
        self.assertIn("ADD成功（対象3）", success[1])
        self.assertTrue(all("state保存済み" in line for line in success))

        before = len(output)
        renderer(
            {
                "provider": "redmine",
                "phase": "redmine.detail",
                "completed": 5,
                "total": 53,
                "status": "replayed",
                "checkpoint_saved": True,
            }
        )
        self.assertEqual(before, len(output))

    def test_tenth_issue_event_is_sanitized_before_markdown_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "work"
            work.mkdir()
            source_key = "src_redmine-log-0123456789ab"
            work_path = f"sources/{source_key}/work/ingest/{source_key}"
            plan = build_fetch_plan(
                source_key=source_key,
                provider="redmine",
                settings={
                    "project_url": "http://localhost:3000/projects/project",
                    "updated_within_days": None,
                    "api_key_env": "REDMINE_TEST_KEY",
                },
                logical_root=work_path,
                work_path=work_path,
            ).to_dict()
            events: list[dict[str, object]] = []

            def getter(url, _headers, _timeout):
                issue_id = int(
                    urlsplit(url).path.rsplit("/", 1)[-1]
                    .removesuffix(".json")
                )
                subject = (
                    "通常題名"
                    if issue_id != 10
                    else "長い題名\nAPI_KEY=do-not-log " + "あ" * 120
                )
                return 200, json.dumps(
                    {
                        "issue": {
                            "id": issue_id,
                            "subject": subject,
                            "description": "fixture",
                            "journals": [],
                        }
                    }
                ).encode("utf-8")

            execute_fetch_plan(
                plan,
                work,
                {},
                http_get=getter,
                environment={"REDMINE_TEST_KEY": "fixture"},
                stable_issue_ids=list(range(1, 11)),
                progress_callback=events.append,
            )

            targets = [
                event
                for event in events
                if event.get("event") == "redmine.item"
            ]
            self.assertEqual(1, len(targets))
            rendered = str(targets[0]["current_item"])
            self.assertIn("Issue #10", rendered)
            self.assertIn("Markdown生成開始", rendered)
            self.assertIn("<REDACTED>", rendered)
            self.assertNotIn("do-not-log", rendered)
            self.assertNotIn("\n", rendered)
            self.assertLessEqual(len(rendered), 130)

    def test_add_success_is_not_emitted_when_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            work.mkdir()
            source_key = "src_redmine-state-0123456789ab"

            class Store:
                db_root = root

                @staticmethod
                def ensure_work_directory(_key: str) -> Path:
                    return work

                @staticmethod
                def save_state(*_args, **_kwargs):
                    raise RuntimeError("state save failed")

            source = StoredJson(
                {
                    "local_source_key": source_key,
                    "source_id": "existing-source-id",
                },
                1,
                "source-etag",
                root / "source.json",
            )
            state = StoredJson(
                {
                    "fetched_count": 50,
                    "indexed_confirmed_count": 0,
                    "last_completed_item": 50,
                    "redmine_issue_ids": list(range(1, 51)),
                },
                1,
                "state-etag",
                root / "state.json",
            )
            events: list[dict[str, object]] = []

            with mock.patch(
                "source_manager.runner._execute_add",
                return_value={"source_id": "source-id", "summary": {}},
            ):
                with self.assertRaisesRegex(RuntimeError, "state save failed"):
                    _redmine_reflect_batch(
                        Store(),
                        source,
                        state,
                        python_executable=root / "python.exe",
                        rag_root=root / "rag",
                        command_runner=None,
                        progress_callback=events.append,
                    )

            batch_events = [
                event
                for event in events
                if event.get("event") == "redmine.add_batch"
            ]
            self.assertEqual(["started"], [e["status"] for e in batch_events])


if __name__ == "__main__":
    unittest.main()
