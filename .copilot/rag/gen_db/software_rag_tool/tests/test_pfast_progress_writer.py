from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool.progress import (
    ProgressWriter,
    ProgressWriterInUseError,
    write_progress,
)


class PfastProgressWriterTests(unittest.TestCase):
    def test_compact_writer_reads_once_and_preserves_fields_and_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "日本語 DB" / "progress.json"
            initial = {
                "array": [],
                "empty": None,
                "message": "開始😀𠮷",
                "phase": "scan",
            }
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(initial, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with mock.patch(
                "software_rag_tool.progress._read_progress_file",
                wraps=__import__(
                    "software_rag_tool.progress",
                    fromlist=["_read_progress_file"],
                )._read_progress_file,
            ) as reader:
                with ProgressWriter(path, run_id="run-a") as writer:
                    first = writer.write(status="running")
                    second = writer.write(current_file="長い/パス/資料😀.md")
                    self.assertEqual(1, reader.call_count)
                    self.assertEqual(first["message"], second["message"])
                    self.assertEqual(initial["array"], second["array"])
                    self.assertIsNone(second["empty"])

            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertTrue(raw.endswith(b"\n"))
            self.assertNotIn(b"\n  ", raw)
            self.assertEqual(second, json.loads(raw.decode("utf-8")))

    def test_run_and_path_scopes_never_share_cached_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first" / "progress.json"
            second_path = root / "second" / "progress.json"

            with ProgressWriter(first_path, run_id="run-a") as writer:
                writer.write(database="db-a", source="source-a")
            with ProgressWriter(second_path, run_id="run-a") as writer:
                second = writer.write(database="db-b", source="source-b")
            with ProgressWriter(first_path, run_id="run-b") as writer:
                resumed = writer.write(status="resumed")

            self.assertEqual("db-b", second["database"])
            self.assertNotIn("db-a", second.values())
            self.assertEqual("db-a", resumed["database"])
            self.assertEqual("source-a", resumed["source"])
            self.assertNotIn("source-b", resumed.values())

    def test_single_writer_violation_fails_instead_of_last_write_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            with ProgressWriter(path, run_id="run-a"):
                with self.assertRaises(ProgressWriterInUseError):
                    ProgressWriter(path, run_id="run-b")
            with ProgressWriter(path, run_id="run-c") as writer:
                writer.write(status="after-close")

    def test_resolved_path_aliases_share_the_single_writer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "nested" / "progress.json"
            alias = root / "nested" / ".." / "nested" / "progress.json"
            with ProgressWriter(path, run_id="run-a"):
                with self.assertRaises(ProgressWriterInUseError):
                    ProgressWriter(alias, run_id="run-a")

    def test_failures_keep_disk_and_cache_at_last_success(self) -> None:
        failure_methods = ("_serialize", "_write_temporary", "_replace")
        for method in failure_methods:
            with self.subTest(method=method), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "progress.json"
                with ProgressWriter(path, run_id=method) as writer:
                    successful = writer.write(status="old", counter=1)
                    old_bytes = path.read_bytes()
                    old_snapshot = writer.snapshot()
                    with mock.patch.object(
                        writer,
                        method,
                        side_effect=OSError(f"injected {method}"),
                    ):
                        with self.assertRaisesRegex(OSError, f"injected {method}"):
                            writer.write(status="new", counter=2)
                    self.assertEqual(old_bytes, path.read_bytes())
                    self.assertEqual(old_snapshot, writer.snapshot())
                    self.assertEqual(successful, old_snapshot)

    def test_flush_failure_keeps_disk_and_cache_at_last_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            with ProgressWriter(path, run_id="flush") as writer:
                writer.write(status="old", counter=1)
                old_bytes = path.read_bytes()
                old_snapshot = writer.snapshot()
                with mock.patch(
                    "software_rag_tool.progress.os.fsync",
                    side_effect=OSError("injected flush"),
                ):
                    with self.assertRaisesRegex(OSError, "injected flush"):
                        writer.write(status="new", counter=2)
                self.assertEqual(old_bytes, path.read_bytes())
                self.assertEqual(old_snapshot, writer.snapshot())

    def test_exception_exit_releases_writer_and_reloads_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            with self.assertRaisesRegex(RuntimeError, "injected"):
                with ProgressWriter(path, run_id="run-a") as writer:
                    writer.write(status="durable")
                    raise RuntimeError("injected")
            with ProgressWriter(path, run_id="run-b") as writer:
                resumed = writer.write(phase="resume")
            self.assertEqual("durable", resumed["status"])

    def test_feature_off_matches_legacy_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_path = root / "legacy.json"
            path = root / "feature-off.json"
            with mock.patch(
                "software_rag_tool.progress.utc_now",
                return_value="2026-08-04T00:00:00+00:00",
            ):
                with mock.patch(
                    "software_rag_tool.progress.progress_path",
                    return_value=legacy_path,
                ):
                    legacy_value = write_progress(
                        status="running",
                        message="日本語😀",
                        empty=None,
                        array=[],
                    )
                with ProgressWriter(
                    path,
                    run_id="legacy",
                    compact=False,
                ) as writer:
                    value = writer.write(
                        status="running",
                        message="日本語😀",
                        empty=None,
                        array=[],
                    )
            self.assertEqual(legacy_value, value)
            self.assertEqual(legacy_path.read_bytes(), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
