from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from source_manager import build_fetch_plan, execute_fetch_plan


class RedmineAddBatchingTests(unittest.TestCase):
    def test_add_callbacks_use_fifty_with_one_final_remainder(self) -> None:
        cases = {
            0: [],
            1: [1],
            49: [49],
            50: [50],
            53: [50, 3],
            100: [50, 50],
        }
        for issue_count, expected_batches in cases.items():
            with self.subTest(issue_count=issue_count):
                with tempfile.TemporaryDirectory() as temporary:
                    work = Path(temporary) / "work"
                    work.mkdir()
                    source_key = "src_redmine-batch-0123456789ab"
                    work_path = (
                        f"sources/{source_key}/work/ingest/{source_key}"
                    )
                    plan = build_fetch_plan(
                        source_key=source_key,
                        provider="redmine",
                        settings={
                            "project_url": (
                                "http://localhost:3000/projects/project"
                            ),
                            "updated_within_days": None,
                            "api_key_env": "REDMINE_TEST_KEY",
                        },
                        logical_root=work_path,
                        work_path=work_path,
                    ).to_dict()
                    observed_batches: list[int] = []
                    last_completed = 0

                    def getter(url, _headers, _timeout):
                        issue_id = int(
                            urlsplit(url).path.rsplit("/", 1)[-1]
                            .removesuffix(".json")
                        )
                        return 200, json.dumps(
                            {
                                "issue": {
                                    "id": issue_id,
                                    "subject": f"Issue {issue_id}",
                                    "description": "fixture",
                                    "journals": [],
                                }
                            }
                        ).encode("utf-8")

                    def batch(completed: int, _issue_id: int) -> None:
                        nonlocal last_completed
                        observed_batches.append(completed - last_completed)
                        last_completed = completed

                    outcome = execute_fetch_plan(
                        plan,
                        work,
                        {},
                        http_get=getter,
                        environment={"REDMINE_TEST_KEY": "fixture"},
                        stable_issue_ids=list(range(1, issue_count + 1)),
                        batch_callback=batch,
                    )

                    self.assertEqual(issue_count, outcome["documents"])
                    self.assertEqual(expected_batches, observed_batches)
                    self.assertEqual(
                        issue_count,
                        len(list((work / "issues").glob("*.md"))),
                    )


if __name__ == "__main__":
    unittest.main()
