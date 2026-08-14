from __future__ import annotations

import hashlib
import tempfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import windows_package_builder as package_builder


LARGE_FILE_SIZE = 128 * 1024 * 1024
MAX_ALLOWED_SOURCE_READ = 1024 * 1024


def _sha256_stream(source) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(package_builder.ZIP_COPY_BUFFER_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


class _ReadRecorder:
    def __init__(self, source) -> None:
        self._source = source
        self.requests: list[int] = []
        self.returned: list[int] = []

    def __enter__(self):
        self._source.__enter__()
        return self

    def __exit__(self, *args):
        return self._source.__exit__(*args)

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        if size < 0:
            raise AssertionError("ZIP source requested an unbounded read")
        chunk = self._source.read(size)
        self.returned.append(len(chunk))
        return chunk

    def readinto(self, buffer) -> int:
        self.requests.append(len(buffer))
        count = self._source.readinto(buffer)
        self.returned.append(count)
        return count

    def fileno(self) -> int:
        return self._source.fileno()


def _write_legacy_zip(package_root: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(package_root.rglob("*")):
            if not path.is_file():
                continue
            relative = Path(package_root.name) / path.relative_to(package_root)
            info = zipfile.ZipInfo(relative.as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


class ZipStreamingTests(unittest.TestCase):
    def test_small_fixture_matches_legacy_zip_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "local-rag-windows-x64-test"
            (package / "nested").mkdir(parents=True)
            fixtures = {
                package / "alpha.txt": "日本語の検索結果\n".encode(),
                package / "empty.bin": b"",
                package / "nested" / "payload.bin": bytes(range(256)) * 8,
            }
            for path, payload in fixtures.items():
                path.write_bytes(payload)

            legacy = root / "legacy.zip"
            streamed = root / "streamed.zip"
            _write_legacy_zip(package, legacy)
            package_builder._write_deterministic_zip(package, streamed)

            self.assertEqual(legacy.read_bytes(), streamed.read_bytes())

            with (
                zipfile.ZipFile(legacy) as expected,
                zipfile.ZipFile(streamed) as actual,
            ):
                self.assertIsNone(actual.testzip())
                self.assertEqual(expected.namelist(), actual.namelist())
                for name in expected.namelist():
                    expected_info = expected.getinfo(name)
                    actual_info = actual.getinfo(name)
                    self.assertEqual(expected.read(name), actual.read(name))
                    for attribute in (
                        "date_time",
                        "compress_type",
                        "external_attr",
                        "file_size",
                        "compress_size",
                        "flag_bits",
                        "create_system",
                        "create_version",
                        "extract_version",
                        "internal_attr",
                        "extra",
                        "comment",
                        "CRC",
                    ):
                        self.assertEqual(
                            getattr(expected_info, attribute),
                            getattr(actual_info, attribute),
                        )

    def test_large_file_uses_bounded_source_reads_and_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "local-rag-windows-x64-test"
            package.mkdir()
            large_file = package / "large-model.bin"
            with large_file.open("wb") as target:
                target.seek(LARGE_FILE_SIZE - 1)
                target.write(b"\0")

            destination = root / "streamed.zip"
            original_open = Path.open
            recorder: _ReadRecorder | None = None

            def tracked_open(path: Path, *args, **kwargs):
                nonlocal recorder
                source = original_open(path, *args, **kwargs)
                mode = args[0] if args else kwargs.get("mode", "r")
                if path == large_file and mode == "rb":
                    recorder = _ReadRecorder(source)
                    return recorder
                return source

            tracemalloc.start()
            try:
                with (
                    mock.patch.object(
                        Path,
                        "read_bytes",
                        side_effect=AssertionError("full-file read is forbidden"),
                    ),
                    mock.patch.object(Path, "open", new=tracked_open),
                ):
                    package_builder._write_deterministic_zip(package, destination)
                _, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertIsNotNone(recorder)
            assert recorder is not None
            self.assertTrue(recorder.requests)
            self.assertGreater(sum(size > 0 for size in recorder.returned), 1)
            self.assertEqual(LARGE_FILE_SIZE, sum(recorder.returned))
            self.assertLessEqual(max(recorder.requests), MAX_ALLOWED_SOURCE_READ)
            self.assertLessEqual(max(recorder.returned), MAX_ALLOWED_SOURCE_READ)
            self.assertLess(peak_bytes, LARGE_FILE_SIZE // 4)
            with zipfile.ZipFile(destination) as archive, large_file.open(
                "rb"
            ) as source:
                info = archive.getinfo(
                    "local-rag-windows-x64-test/large-model.bin"
                )
                self.assertEqual(LARGE_FILE_SIZE, info.file_size)
                self.assertEqual((2026, 1, 1, 0, 0, 0), info.date_time)
                self.assertEqual(zipfile.ZIP_DEFLATED, info.compress_type)
                self.assertEqual(0o100644 << 16, info.external_attr)
                with archive.open(info) as archived:
                    self.assertEqual(
                        _sha256_stream(source), _sha256_stream(archived)
                    )

            print(
                "ZIP_STREAMING_METRICS "
                f"file_size={LARGE_FILE_SIZE} "
                f"max_read_request={max(recorder.requests)} "
                f"max_read_returned={max(recorder.returned)} "
                f"read_calls={len(recorder.requests)} "
                f"peak_traced_bytes={peak_bytes}"
            )


if __name__ == "__main__":
    unittest.main()
