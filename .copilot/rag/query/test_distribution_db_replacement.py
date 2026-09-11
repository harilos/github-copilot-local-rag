from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from source_manager import copy_only_packages, packages


class DistributionDatabaseReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self, label: str, names: tuple[str, ...] = ("one-rag", "two-rag")):
        root = self.root / label
        source = root / "source"
        source.mkdir(parents=True)
        entries = []
        for name in names:
            data = source / name
            data.write_text("new-" + name, encoding="utf-8")
            entries.append(packages._Entry(data, f".copilot/rag/dbs/{name}/db.json"))
        product = source / "product"
        product.write_text("product", encoding="utf-8")
        entries.append(packages._Entry(product, ".copilot/rag/query/new.py"))
        package = root / "package"
        entries.append(packages._Entry(None, "bootstrap.py", mode="bootstrap"))
        packages._stage_package(
            package, entries, kind=packages._DISTRIBUTION_KIND,
            databases=[{"name": name, "content_snapshot_at": None, "content_snapshot_reason": "unknown"} for name in names],
            created="2026-09-11T00:00:00Z", tool_version="fixture",
        )
        target = root / "target"
        for name in (*names, "unrelated-rag"):
            db = target / "rag" / "dbs" / name
            db.mkdir(parents=True)
            (db / "old.txt").write_text("old-" + name, encoding="utf-8")
        namespace = {"__name__": "fixture_bootstrap", "__file__": str(package / "bootstrap.py")}
        exec(compile(packages._BOOTSTRAP_TEXT, "bootstrap.py", "exec"), namespace)
        return package, target, namespace

    def run_install(self, engine, package, target, namespace):
        if engine == "manager":
            return packages.import_package(package, target)
        with mock.patch.object(sys, "argv", [str(package / "bootstrap.py"), str(target), "--skip-runtime-setup"]):
            return namespace["main"]()

    def copy_patch(self, engine, namespace, replacement):
        if engine == "manager":
            return mock.patch.object(copy_only_packages, "_copy_atomic", side_effect=replacement)
        return mock.patch.dict(namespace, {"copy_atomic": replacement})

    def test_direct_replacement_never_retains_old_or_stages_db(self):
        for engine in ("manager", "bootstrap"):
            with self.subTest(engine=engine):
                package, target, namespace = self.fixture(engine)
                original = copy_only_packages._copy_atomic if engine == "manager" else namespace["copy_atomic"]
                observed = []

                def inspect_copy(source, destination):
                    dbs = target / "rag" / "dbs"
                    self.assertEqual({"one-rag", "two-rag", "unrelated-rag"}, {p.name for p in dbs.iterdir()})
                    if "dbs" in destination.parts:
                        name = destination.relative_to(dbs).parts[0]
                        self.assertFalse((dbs / name / "old.txt").exists())
                        self.assertEqual(dbs / name / "db.json", destination)
                        observed.append(name)
                    original(source, destination)

                with self.copy_patch(engine, namespace, inspect_copy):
                    self.run_install(engine, package, target, namespace)
                self.assertEqual(["one-rag", "two-rag"], observed)
                for name in observed:
                    self.assertEqual(["db.json"], [p.name for p in (target / "rag/dbs" / name).iterdir()])
                self.assertEqual("old-unrelated-rag", (target / "rag/dbs/unrelated-rag/old.txt").read_text())

    def test_all_destinations_checked_before_first_database_deletion(self):
        for engine in ("manager", "bootstrap"):
            with self.subTest(engine=engine):
                package, target, namespace = self.fixture(engine)
                second = target / "rag/dbs/two-rag"
                shutil.rmtree(second)
                second.write_text("not-a-directory", encoding="utf-8")
                with self.assertRaisesRegex((packages.PackageError, SystemExit), "install_target_path_invalid"):
                    self.run_install(engine, package, target, namespace)
                self.assertTrue((target / "rag/dbs/one-rag/old.txt").is_file())
                self.assertEqual("not-a-directory", second.read_text())

    def test_invalid_payload_does_not_delete_current_databases(self):
        for engine in ("manager", "bootstrap"):
            with self.subTest(engine=engine):
                package, target, namespace = self.fixture(engine)
                (package / ".copilot/rag/dbs/two-rag/db.json").write_text("corrupt")
                with self.assertRaisesRegex((packages.PackageError, SystemExit), "checksum_mismatch"):
                    self.run_install(engine, package, target, namespace)
                for name in ("one-rag", "two-rag"):
                    self.assertTrue((target / "rag/dbs" / name / "old.txt").is_file())

    def test_overlapping_package_inside_replaced_database_is_rejected(self):
        for engine in ("manager", "bootstrap"):
            with self.subTest(engine=engine):
                package, target, namespace = self.fixture(engine)
                nested = target / "rag/dbs/one-rag/package"
                shutil.move(package, nested)
                namespace["__file__"] = str(nested / "bootstrap.py")
                with self.assertRaisesRegex((packages.PackageError, SystemExit), "install_source_target_overlap"):
                    self.run_install(engine, nested, target, namespace)
                self.assertTrue((target / "rag/dbs/one-rag/old.txt").is_file())
                self.assertTrue((nested / "manifest.json").is_file())

    def test_copy_failure_cleans_only_failed_database_without_restoration(self):
        for engine in ("manager", "bootstrap"):
            with self.subTest(engine=engine):
                package, target, namespace = self.fixture(engine)
                original = copy_only_packages._copy_atomic if engine == "manager" else namespace["copy_atomic"]

                def fail_second(source, destination):
                    original(source, destination)
                    if "two-rag" in destination.parts:
                        raise OSError("disk full")

                with self.copy_patch(engine, namespace, fail_second):
                    with self.assertRaisesRegex((packages.PackageError, SystemExit), "install_database_failed_reinstall_required"):
                        self.run_install(engine, package, target, namespace)
                self.assertEqual("new-one-rag", (target / "rag/dbs/one-rag/db.json").read_text())
                self.assertFalse((target / "rag/dbs/one-rag/old.txt").exists())
                self.assertFalse((target / "rag/dbs/two-rag").exists())
                self.assertTrue((target / "rag/dbs/unrelated-rag/old.txt").is_file())
                self.assertEqual({"one-rag", "unrelated-rag"}, {p.name for p in (target / "rag/dbs").iterdir()})

    def test_source_only_package_preserves_databases(self):
        for engine in ("manager", "bootstrap"):
            with self.subTest(engine=engine):
                package, target, namespace = self.fixture(engine, names=())
                self.run_install(engine, package, target, namespace)
                self.assertEqual("old-unrelated-rag", (target / "rag/dbs/unrelated-rag/old.txt").read_text())
                self.assertEqual({"unrelated-rag"}, {p.name for p in (target / "rag/dbs").iterdir()})

    def test_destination_parent_symlink_is_rejected_before_database_delete(self):
        for engine in ("manager", "bootstrap"):
            with self.subTest(engine=engine):
                package, target, namespace = self.fixture(engine)
                linked = target / "rag/query"
                outside = self.root / (engine + "-outside")
                outside.mkdir()
                try:
                    linked.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    self.skipTest(f"symlinks unavailable: {exc}")
                with self.assertRaisesRegex((packages.PackageError, SystemExit), "install_target_symlink_forbidden"):
                    self.run_install(engine, package, target, namespace)
                self.assertTrue((target / "rag/dbs/one-rag/old.txt").is_file())
                self.assertEqual([], list(outside.iterdir()))


if __name__ == "__main__":
    unittest.main()
