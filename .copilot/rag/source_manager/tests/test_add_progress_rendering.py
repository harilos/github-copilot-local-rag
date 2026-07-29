from __future__ import annotations

import unittest

from source_manager.progress import ProgressRenderer


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class AddProgressRenderingTests(unittest.TestCase):
    def test_exact_add_progress_shows_total_current_file_and_eta(self) -> None:
        lines: list[str] = []
        clock = _Clock()
        renderer = ProgressRenderer(
            lines.append,
            operation="Source追加",
            provider="github",
            is_tty=False,
            clock=clock,
        )

        renderer(
            {
                "event": "add.file_progress",
                "phase": "reflect",
                "label_ja": "ADD検索反映",
                "status": "running",
                "completed": 2,
                "total": 20,
                "total_kind": "exact",
                "current_index": 3,
                "current_item": "docs/design.pdf",
                "eta_seconds": 120,
                "elapsed_seconds": 30,
            }
        )

        rendered = lines[-1]
        self.assertIn("全20件中、今3ファイル目", rendered)
        self.assertIn("対象: docs/design.pdf", rendered)
        self.assertIn("残り約2分", rendered)
        self.assertIn("経過30秒", rendered)

    def test_preparation_shows_approximate_total_and_time_range(self) -> None:
        lines: list[str] = []
        renderer = ProgressRenderer(
            lines.append,
            operation="Source追加",
            provider="sharepoint",
            is_tty=False,
            clock=_Clock(),
        )

        renderer(
            {
                "event": "add.file_progress",
                "phase": "reflect",
                "label_ja": "ADD検索反映",
                "status": "running",
                "completed": 0,
                "total": 10,
                "total_kind": "estimated",
                "current_index": 0,
                "remaining_seconds_min": 600,
                "remaining_seconds_max": 3000,
            }
        )

        rendered = lines[-1]
        self.assertIn("準備中（約10件）", rendered)
        self.assertIn("残り目安約10分～50分", rendered)

    def test_before_first_exact_file_keeps_one_to_five_minute_range(self) -> None:
        lines: list[str] = []
        renderer = ProgressRenderer(
            lines.append,
            operation="Source追加",
            provider="sharepoint",
            is_tty=False,
            clock=_Clock(),
        )

        renderer(
            {
                "event": "add.file_progress",
                "phase": "reflect",
                "label_ja": "ADD検索反映",
                "status": "running",
                "completed": 0,
                "total": 10,
                "total_kind": "exact",
                "current_index": 1,
                "remaining_seconds_min": 600,
                "remaining_seconds_max": 3000,
            }
        )

        rendered = lines[-1]
        self.assertIn("全10件中、今1ファイル目", rendered)
        self.assertIn("残り目安約10分～50分", rendered)

    def test_subprocess_heartbeat_replays_last_add_file_instead_of_hiding_it(self) -> None:
        lines: list[str] = []
        clock = _Clock()
        renderer = ProgressRenderer(
            lines.append,
            operation="Source更新",
            provider="redmine",
            is_tty=False,
            clock=clock,
        )
        renderer(
            {
                "event": "add.file_progress",
                "phase": "reflect",
                "label_ja": "ADD検索反映",
                "status": "running",
                "completed": 4,
                "total": 9,
                "total_kind": "exact",
                "current_index": 5,
                "current_item": "issues/123.md",
                "eta_seconds": 75,
                "elapsed_seconds": 20,
            }
        )
        clock.value = 5.0
        renderer({"event": "heartbeat", "elapsed_seconds": 25})

        rendered = lines[-1]
        self.assertIn("全9件中、今5ファイル目", rendered)
        self.assertIn("issues/123.md", rendered)
        self.assertNotIn("処理継続中", rendered)

    def test_non_add_exact_progress_keeps_count_format(self) -> None:
        lines: list[str] = []
        renderer = ProgressRenderer(
            lines.append,
            operation="Source更新",
            provider="redmine",
            is_tty=False,
            clock=_Clock(),
        )
        renderer(
            {
                "event": "provider.page",
                "phase": "redmine.inventory",
                "status": "running",
                "completed": 2,
                "total": 10,
                "total_kind": "exact",
                "unit": "件",
            }
        )

        rendered = lines[-1]
        self.assertIn("20%（2/10件）", rendered)
        self.assertNotIn("ファイル目", rendered)


if __name__ == "__main__":
    unittest.main()
