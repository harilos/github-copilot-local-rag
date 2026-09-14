from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from source_manager import packages  # noqa: E402


class PackageBuildStreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.bin"
        self.payload = b"search-index\x00" * 200000
        self.source.write_bytes(self.payload)

    def stage(self) -> dict:
        return packages._stage_package(
            self.root / "stage",
            [packages._Entry(self.source, "payload.bin", source_root=self.root)],
            kind=packages._DISTRIBUTION_KIND,
            databases=[],
            created="2026-09-14T00:00:00Z",
            tool_version="test",
        )

    def archive(self, manifest: dict, entries=None) -> Path:
        archive = self.root / "package.zip"
        with zipfile.ZipFile(archive, "w") as writer:
            writer.writestr("manifest.json", json.dumps(manifest))
            for name, payload in entries or [("payload.bin", self.payload)]:
                writer.writestr(name, payload)
        return archive

    def test_stage_copy_uses_bounded_reads_and_reuses_digest(self) -> None:
        with (
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded")),
            mock.patch.object(packages, "_sha256", side_effect=AssertionError("reread")),
        ):
            manifest = self.stage()
        self.assertEqual(hashlib.sha256(self.payload).hexdigest(), manifest["files"][0]["sha256"])
        self.assertEqual(self.payload, (self.root / "stage/payload.bin").read_bytes())
        self.assertEqual(manifest, packages.validate_package_tree(self.root / "stage"))

    def test_source_change_after_copy_is_rejected(self) -> None:
        copy = packages._copy_stable_regular_file

        def change_source(*args, **kwargs):
            digest = copy(*args, **kwargs)
            self.source.write_bytes(b"changed")
            return digest

        with mock.patch.object(packages, "_copy_stable_regular_file", side_effect=change_source):
            with self.assertRaisesRegex(packages.PackageError, "package_source_changed"):
                self.stage()

    def test_source_change_during_read_is_rejected(self) -> None:
        check = packages._assert_regular_source
        calls = 0

        def change_source(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.source.write_bytes(b"changed")
            return check(*args, **kwargs)

        with mock.patch.object(packages, "_assert_regular_source", side_effect=change_source):
            with self.assertRaisesRegex(packages.PackageError, "package_source_changed"):
                self.stage()

    def test_streaming_retains_key_and_git_credential_guards(self) -> None:
        for filename, payload, error in (
            ("source.bin", b"-----BEGIN OPENSSH PRIVATE KEY-----", "forbidden_package_source"),
            (".gitmodules", b'[submodule "x"]\nurl=https://user:pass@example.com/repo\n', "credential_configuration_detected"),
            (".gitmodules", b"#" * (packages._MAX_TEXT_CONFIG_BYTES + 1), "credential_configuration_detected"),
        ):
            with self.subTest(filename=filename, error=error):
                self.source = self.root / filename
                self.source.write_bytes(payload)
                with mock.patch.object(packages, "_BUFFER_SIZE", 4096):
                    with self.assertRaisesRegex(packages.PackageError, error):
                        self.stage()

    def test_streaming_rejects_symlink_source(self) -> None:
        linked = self.root / "link.bin"
        try:
            linked.symlink_to(self.source)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.source = linked
        with self.assertRaisesRegex(packages.PackageError, "package_symlink_forbidden"):
            self.stage()

    def test_zip_validation_streams_without_extracting_and_import_still_extracts(self) -> None:
        manifest = self.stage()
        archive = self.archive(manifest)
        read = zipfile.ZipExtFile.read

        def bounded_read(source, size=-1):
            if source.name != packages.MANIFEST_NAME:
                self.assertGreater(size, 0)
                self.assertLessEqual(size, packages._BUFFER_SIZE)
            return read(source, size)

        with (
            mock.patch.object(packages, "_extract_distribution_zip", side_effect=AssertionError("extract")),
            mock.patch.object(packages.tempfile, "TemporaryDirectory", side_effect=AssertionError("tempdir")),
            mock.patch.object(zipfile.ZipExtFile, "read", bounded_read),
        ):
            self.assertEqual(manifest, packages.validate_distribution_zip(archive))
        extracted = self.root / "extracted"
        extracted.mkdir()
        self.assertEqual(manifest, packages._extract_distribution_zip(archive, extracted, expected_kind="distribution"))
        self.assertEqual(self.payload, (extracted / "payload.bin").read_bytes())

    def test_zip_checksum_coverage_and_totals_remain_required(self) -> None:
        manifest = self.stage()
        for entries, error in (
            ([("payload.bin", b"changed")], "package_checksum_mismatch"),
            ([("unlisted.bin", self.payload)], "package_manifest_coverage_mismatch"),
        ):
            with self.subTest(error=error):
                with self.assertRaisesRegex(packages.PackageError, error):
                    packages.validate_distribution_zip(self.archive(manifest, entries))
        manifest["total"]["bytes"] += 1
        with self.assertRaisesRegex(packages.PackageError, "package_total_mismatch"):
            packages.validate_distribution_zip(self.archive(manifest))

    def test_zip_corrupt_crc_is_rejected(self) -> None:
        archive = self.archive(self.stage())
        with zipfile.ZipFile(archive) as reader:
            info = reader.getinfo("payload.bin")
            offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        with archive.open("r+b") as writer:
            writer.seek(offset)
            writer.write(b"X")
        with self.assertRaisesRegex(packages.PackageError, "package_archive_invalid"):
            packages.validate_distribution_zip(archive)

    def test_zip_paths_duplicates_and_types_are_rejected(self) -> None:
        manifest = self.stage()
        symlink = zipfile.ZipInfo("linked")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        fifo = zipfile.ZipInfo("fifo")
        fifo.create_system = 3
        fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
        for extra, error in (
            ([("../outside", b"x")], "package_path_invalid"),
            ([("payload.bin", b"x")], "package_archive_duplicate_path"),
            ([("payload.bin/child", b"x")], "package_archive_invalid"),
            ([(symlink, b"payload.bin")], "package_symlink_forbidden"),
            ([(fifo, b"")], "package_special_file_forbidden"),
        ):
            with self.subTest(error=error), warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive = self.archive(manifest, [("payload.bin", self.payload)] + extra)
                with self.assertRaisesRegex(packages.PackageError, error):
                    packages.validate_distribution_zip(archive)

    def test_zip_manifest_kind_and_duplicate_records_are_rejected(self) -> None:
        manifest = self.stage()
        manifest["files"].append(manifest["files"][0])
        with self.assertRaisesRegex(packages.PackageError, "package_manifest_path_invalid"):
            packages.validate_distribution_zip(self.archive(manifest))
        manifest["files"].pop()
        manifest["kind"] = "admin-transfer"
        with self.assertRaisesRegex(packages.PackageError, "package_manifest_kind_mismatch"):
            packages.validate_distribution_zip(self.archive(manifest))

    def test_model_traversal_is_optional_and_enabled_by_default(self) -> None:
        home = self.root / ".copilot"
        model = home / "rag/models/unused/model.onnx"
        model.parent.mkdir(parents=True)
        model.write_bytes(b"model")
        for name in packages._PROJECT_SKILLS:
            skill = home / "skills" / name / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("skill", encoding="utf-8")
        # Only runtime preflight requires real product files; use a small tree.
        with mock.patch.object(packages, "_add_file"):
            default_entries = packages._product_entries(home, admin=False)
            optional_entries = packages._product_entries(home, admin=False, include_models=False)
        self.assertTrue(any(entry.source == model for entry in default_entries))
        self.assertFalse(any(entry.source == model for entry in optional_entries))


if __name__ == "__main__":
    unittest.main()
