from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool.records import (
    FILE_HASH_BUFFER_SIZE,
    file_content_hash,
    sha256_bytes,
)


class _ReadSizeTracker:
    def __init__(self, stream, read_sizes: list[int]) -> None:
        self._stream = stream
        self._read_sizes = read_sizes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stream.close()

    def read(self, size: int = -1) -> bytes:
        self._read_sizes.append(size)
        return self._stream.read(size)


class FileContentHashStreamingTests(unittest.TestCase):
    def test_digest_matches_previous_whole_file_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "fixture.bin"
            source.write_bytes(b"small fixture\x00with binary data\xff")

            expected = sha256_bytes(source.read_bytes())

            self.assertEqual(expected, file_content_hash(source))

    def test_large_file_never_requests_more_than_the_hash_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "large.bin"
            large_file_size = 128 * 1024 * 1024 + 1
            with source.open("wb") as stream:
                stream.seek(large_file_size - 1)
                stream.write(b"\x01")

            expected = hashlib.sha256()
            zero_buffer = b"\x00" * FILE_HASH_BUFFER_SIZE
            for _ in range(large_file_size // FILE_HASH_BUFFER_SIZE):
                expected.update(zero_buffer)
            expected.update(b"\x01")

            original_open = Path.open
            read_sizes: list[int] = []

            def tracked_open(path: Path, *args, **kwargs):
                return _ReadSizeTracker(original_open(path, *args, **kwargs), read_sizes)

            with mock.patch.object(Path, "open", tracked_open):
                actual = file_content_hash(source)

            self.assertEqual(expected.hexdigest(), actual)
            self.assertTrue(read_sizes)
            self.assertNotIn(-1, read_sizes)
            self.assertLessEqual(max(read_sizes), FILE_HASH_BUFFER_SIZE)


if __name__ == "__main__":
    unittest.main()
