from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from setup_contract import completion_marker_for
from windows_runtime import is_amd64_pe, is_fixed_windows_runtime


def _pe(machine: int = 0x8664) -> bytes:
    payload = bytearray(128)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 64)
    payload[64:68] = b"PE\0\0"
    struct.pack_into("<H", payload, 68, machine)
    return bytes(payload)


class WindowsRuntimeContractTests(unittest.TestCase):
    def test_direct_amd64_check_does_not_require_an_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "python.exe"
            binary.write_bytes(_pe())
            self.assertTrue(is_amd64_pe(binary))
            binary.write_bytes(_pe(0x014C))
            self.assertFalse(is_amd64_pe(binary))

    def test_only_fixed_embedded_layout_bypasses_managed_setup_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            query = Path(directory) / "query"
            scripts = query / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "python.exe").write_bytes(_pe())
            self.assertFalse(is_fixed_windows_runtime(query))

            path_file = scripts / "python313._pth"
            path_file.write_text(
                "python313.zip\nimport site\n",
                encoding="utf-8",
            )
            self.assertFalse(is_fixed_windows_runtime(query))

            path_file.write_text(
                "python313.zip\n..\\..\nimport site\n",
                encoding="utf-8",
            )
            self.assertTrue(is_fixed_windows_runtime(query))

            (scripts / "python.exe").write_bytes(_pe(0x014C))
            self.assertFalse(is_fixed_windows_runtime(query))

    def test_retired_packaged_artifact_does_not_relocate_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            query = Path(directory) / "query"
            query.mkdir()
            expected = query / ".venv" / ".rag-deps-installed"
            self.assertEqual(expected, completion_marker_for(query))
            (query / ".packaged-runtime.json").write_text(
                "{}\n", encoding="utf-8"
            )
            self.assertEqual(expected, completion_marker_for(query))


if __name__ == "__main__":
    unittest.main()
