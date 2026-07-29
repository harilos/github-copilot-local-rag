from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from source_manager.redmine_incremental import (
    _changed_issue_ids,
    _local_issue_timestamp,
)


class RedmineIncrementalRefreshTests(unittest.TestCase):
    def test_existing_markdown_updated_on_skips_unchanged_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            issues = Path(temporary)
            (issues / "10.md").write_text(
                '# Issue 10\n\n```json\n{"id":10,"updated_on":"2026-07-29T01:02:03Z"}\n```\n',
                encoding="utf-8",
            )
            changed = _changed_issue_ids(
                [
                    (
                        10,
                        datetime(
                            2026, 7, 29, 1, 2, 3, tzinfo=timezone.utc
                        ),
                    )
                ],
                issues,
            )
            self.assertEqual([], changed)

    def test_newer_remote_issue_is_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            issues = Path(temporary)
            (issues / "11.md").write_text(
                '{"id":11,"updated_on":"2026-07-29T01:00:00Z"}\n',
                encoding="utf-8",
            )
            changed = _changed_issue_ids(
                [
                    (
                        11,
                        datetime(
                            2026, 7, 29, 1, 0, 1, tzinfo=timezone.utc
                        ),
                    )
                ],
                issues,
            )
            self.assertEqual([11], changed)

    def test_missing_local_issue_is_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            issues = Path(temporary)
            changed = _changed_issue_ids(
                [
                    (
                        12,
                        datetime(
                            2026, 7, 29, 1, 0, 0, tzinfo=timezone.utc
                        ),
                    )
                ],
                issues,
            )
            self.assertEqual([12], changed)

    def test_legacy_markdown_uses_file_time_as_bootstrap_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "13.md"
            path.write_text("# legacy issue\n", encoding="utf-8")
            timestamp = datetime(
                2026, 7, 29, 2, 0, 0, tzinfo=timezone.utc
            ).timestamp()
            os.utime(path, (timestamp, timestamp))
            self.assertEqual(
                datetime(2026, 7, 29, 2, 0, 0, tzinfo=timezone.utc),
                _local_issue_timestamp(path),
            )
            self.assertEqual(
                [],
                _changed_issue_ids(
                    [
                        (
                            13,
                            datetime(
                                2026,
                                7,
                                29,
                                1,
                                59,
                                59,
                                tzinfo=timezone.utc,
                            ),
                        )
                    ],
                    path.parent,
                ),
            )


if __name__ == "__main__":
    unittest.main()
