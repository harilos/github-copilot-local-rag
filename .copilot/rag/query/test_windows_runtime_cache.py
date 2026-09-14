from __future__ import annotations

import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from source_manager import windows_distribution as distribution
from source_manager.errors import SourceManagerError


def _fake_runtime(_home: Path, runtime: Path, *, emit: object) -> None:
    scripts = runtime / "Scripts"
    scripts.mkdir(parents=True)
    executable = bytearray(256)
    executable[:2] = b"MZ"
    struct.pack_into("<I", executable, 0x3C, 0x80)
    executable[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", executable, 0x84, 0x8664)
    (scripts / "python.exe").write_bytes(executable)
    (scripts / "python313._pth").write_text("python313.zip\n.\n", encoding="ascii")
    (scripts / "python313.zip").write_bytes(b"stdlib")
    (scripts / "demo.py").write_text("VALUE = 1\n", encoding="ascii")


class WindowsRuntimeCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.inputs = []
        for attribute in ("LOCK_PATH", "SEARCH_REQUIREMENTS", "__file__"):
            source = self.home / attribute
            source.write_text(attribute, encoding="ascii")
            self.inputs.append(source)
            patcher = mock.patch.object(distribution, attribute, source)
            patcher.start()
            self.addCleanup(patcher.stop)
        builder_patch = mock.patch.object(
            distribution, "_prepare_runtime", side_effect=_fake_runtime
        )
        self.builder = builder_patch.start()
        self.addCleanup(builder_patch.stop)
        self.cache = self.home / "rag" / "cache" / "windows-portable" / "runtime"

    def _runtime(self):
        return distribution._cached_runtime(self.home, emit=lambda _message: None)

    def test_reuses_completed_runtime_directly_without_build_or_copy(self) -> None:
        with self._runtime() as first:
            first_files = {path.relative_to(first) for path in first.rglob("*")}
        with mock.patch.object(shutil, "copytree", side_effect=AssertionError):
            with self._runtime() as second:
                self.assertEqual(first, second)
                self.assertEqual(second.parent, self.cache)
                entries = distribution._runtime_entries(second)
                self.assertTrue(all(entry.source_root == second for entry in entries))
                self.assertEqual(
                    first_files, {path.relative_to(second) for path in second.rglob("*")}
                )
        self.assertEqual(self.builder.call_count, 1)

    def test_lock_requirements_and_build_code_changes_replace_one_generation(self) -> None:
        with self._runtime() as previous:
            pass
        unrelated = self.cache / "keep.txt"
        unrelated.write_text("user file", encoding="ascii")
        for count, source in enumerate(self.inputs, start=2):
            with self.subTest(source=source.name):
                source.write_text(source.name + " changed", encoding="ascii")
                with self._runtime() as current:
                    self.assertNotEqual(current, previous)
                    self.assertFalse(previous.exists())
                    self.assertEqual(self.builder.call_count, count)
                    self.assertEqual(
                        [item for item in self.cache.iterdir() if item.is_dir()],
                        [current],
                    )
                previous = current
        self.assertEqual(unrelated.read_text(encoding="ascii"), "user file")

    def test_failed_build_keeps_previous_and_never_publishes_partial(self) -> None:
        with self._runtime() as previous:
            pass
        self.inputs[0].write_text("changed", encoding="ascii")

        def fail(home: Path, runtime: Path, *, emit: object) -> None:
            _fake_runtime(home, runtime, emit=emit)
            raise distribution.packages.PackageError("test_build_failed")

        self.builder.side_effect = fail
        with self.assertRaisesRegex(distribution.packages.PackageError, "test_build_failed"):
            with self._runtime():
                self.fail("failed build was exposed")
        self.assertEqual(list(self.cache.iterdir()), [previous])
        self.builder.side_effect = _fake_runtime
        with self._runtime() as current:
            self.assertNotEqual(previous, current)
            self.assertFalse(previous.exists())

    def test_interrupted_partial_is_not_reused_and_invalid_runtime_is_rebuilt(self) -> None:
        self.cache.mkdir(parents=True)
        partial = self.cache / (".runtime-" + "a" * 32 + ".partial")
        _fake_runtime(self.home, partial, emit=None)
        with self._runtime() as current:
            self.assertNotEqual(partial, current)
            self.assertFalse(partial.exists())
            (current / "Scripts" / "python.exe").unlink()
        with self._runtime() as repaired:
            self.assertEqual(current, repaired)
            self.assertTrue((repaired / "Scripts" / "python.exe").is_file())
        self.assertEqual(self.builder.call_count, 2)

    def test_runtime_links_are_rejected_without_deleting_their_targets(self) -> None:
        with self._runtime() as runtime:
            pass
        external = self.home / "external"
        external.mkdir()
        protected = external / "keep.txt"
        protected.write_text("keep", encoding="ascii")
        link = runtime / "linked"
        try:
            link.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlink unavailable")
        with self.assertRaisesRegex(distribution.packages.PackageError, "link_forbidden"):
            with self._runtime():
                self.fail("linked cache reused")
        self.assertEqual(protected.read_text(encoding="ascii"), "keep")
        self.assertEqual(self.builder.call_count, 1)

    def test_active_runtime_cannot_be_removed_by_concurrent_builder(self) -> None:
        def competing_build() -> None:
            with self._runtime():
                self.fail("concurrent cache lease acquired")

        with self._runtime() as active:
            self.inputs[0].write_text("next generation", encoding="ascii")
            with ThreadPoolExecutor(max_workers=1) as executor:
                with self.assertRaises(SourceManagerError):
                    executor.submit(competing_build).result(timeout=5)
            self.assertTrue((active / "Scripts" / "python.exe").is_file())
        with self._runtime() as next_runtime:
            self.assertNotEqual(active, next_runtime)
            self.assertFalse(active.exists())

    def test_windows_package_collects_only_its_search_model(self) -> None:
        models = self.home / "rag" / "models"
        selected = models / distribution.MODEL_NAME
        selected.mkdir(parents=True)
        for filename in (*distribution.MODEL_REQUIRED, "tokenizer.json"):
            (selected / filename).write_text("{}", encoding="ascii")
        other = models / "unused-builder-model"
        other.mkdir()
        (other / "large-model.bin").write_bytes(b"must not be packaged")
        output = self.home / "search.zip"
        with (
            mock.patch.object(distribution.sys, "platform", "win32"),
            mock.patch.object(distribution.packages, "_product_entries", return_value=[]) as product,
            mock.patch.object(distribution.packages, "_database_entries", return_value=([], [])),
            mock.patch.object(distribution, "_generated_installer_entries", return_value=[]),
        ):
            distribution.create_windows_distribution_package(self.home, output, db_names=())
        product.assert_called_once_with(self.home, admin=False, include_models=False)
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
        self.assertIn(f".copilot/rag/models/{distribution.MODEL_NAME}/model.onnx", names)
        self.assertFalse(any("unused-builder-model" in name for name in names))


if __name__ == "__main__":
    unittest.main()
