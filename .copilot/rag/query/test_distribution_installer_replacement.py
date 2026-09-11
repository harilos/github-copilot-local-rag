from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


INSTALLERS_PATH = Path(__file__).resolve().parents[1] / 'source_manager' / 'package_installers.py'
_SPEC = importlib.util.spec_from_file_location('distribution_installers_fixture', INSTALLERS_PATH)
assert _SPEC and _SPEC.loader
_INSTALLERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_INSTALLERS)


@unittest.skipIf(os.name == 'nt', 'POSIX installer requires a POSIX shell')
class PosixDistributionReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'distribution package'
        self.target = self.root / 'installed copilot'
        self.source.mkdir()
        self.target.mkdir()
        self._write(self.source / 'install.sh', _INSTALLERS.INSTALL_SH_TEXT)
        self._write(self.source / '.copilot/rag/query/product.py', 'new product')

    @staticmethod
    def _write(path: Path, value: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        return path

    def _package_db(self, name: str) -> Path:
        db = self.source / '.copilot/rag/dbs' / name
        self._write(db / 'catalog.sqlite', 'new catalog')
        self._write(db / 'lancedb/table/current', 'new vectors')
        return db

    def _existing_db(self, name: str) -> Path:
        db = self.target / 'rag/dbs' / name
        self._write(db / 'catalog.sqlite', 'old catalog')
        self._write(db / 'lancedb/table/stale', 'obsolete vectors')
        self._write(db / 'source-only.dat', 'obsolete metadata')
        return db

    def _run(self, *, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, COPILOT_HOME=str(self.target))
        env.update(environment or {})
        return subprocess.run(
            ['sh', str(self.source / 'install.sh')],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def _snapshot(self, path: Path) -> dict[str, bytes]:
        return {str(item.relative_to(path)): item.read_bytes() for item in path.rglob('*') if item.is_file()}

    def test_replaces_included_db_without_stale_files_or_extra_db_copies(self) -> None:
        supplied = self._package_db('demo-rag')
        included = self._existing_db('demo-rag')
        other = self._existing_db('personal-rag')
        other_before = self._snapshot(other)
        result = self._run()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self._snapshot(supplied), self._snapshot(included))
        self.assertEqual(other_before, self._snapshot(other))
        self.assertEqual({'demo-rag', 'personal-rag'}, {p.name for p in included.parent.iterdir()})
        self.assertEqual('new product', (self.target / 'rag/query/product.py').read_text())
        self.assertFalse(any('backup' in p.name or 'stage' in p.name for p in self.root.rglob('*')))

    def test_source_only_install_leaves_databases_untouched(self) -> None:
        self._existing_db('demo-rag')
        before = self._snapshot(self.target / 'rag/dbs')
        result = self._run()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self._snapshot(self.target / 'rag/dbs'))

    def test_validates_every_database_name_before_deleting_any_database(self) -> None:
        self._package_db('a-rag')
        self._package_db('z-invalid')
        old = self._existing_db('a-rag')
        before = self._snapshot(old)
        result = self._run()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('invalid_database_name', result.stderr)
        self.assertEqual(before, self._snapshot(old))
        self.assertFalse((self.target / 'rag/query/product.py').exists())

    def test_rejects_case_colliding_database_names_before_deletion(self) -> None:
        self._package_db('Demo-rag')
        self._package_db('demo-rag')
        old = self._existing_db('demo-rag')
        before = self._snapshot(old)
        result = self._run()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('duplicate_database_name', result.stderr)
        self.assertEqual(before, self._snapshot(old))

    def test_rejects_payload_link_before_deleting_existing_database(self) -> None:
        new = self._package_db('demo-rag')
        (new / 'escape').symlink_to(self.root / 'elsewhere')
        old = self._existing_db('demo-rag')
        before = self._snapshot(old)
        result = self._run()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('install_link_or_special_file_forbidden', result.stderr)
        self.assertEqual(before, self._snapshot(old))

    def test_rejects_destination_link_before_deleting_any_database(self) -> None:
        self._package_db('a-rag')
        self._package_db('z-rag')
        old = self._existing_db('a-rag')
        before = self._snapshot(old)
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = self._write(outside / 'untouched', 'protected')
        (self.target / 'rag/dbs/z-rag').symlink_to(outside, target_is_directory=True)
        result = self._run()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('install_path_symlink_forbidden', result.stderr)
        self.assertEqual(before, self._snapshot(old))
        self.assertEqual('protected', sentinel.read_text())

    def test_rejects_destination_ancestor_link(self) -> None:
        self._package_db('demo-rag')
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = self._write(outside / 'dbs/demo-rag/old', 'protected')
        (self.target / 'rag').symlink_to(outside, target_is_directory=True)
        result = self._run()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('install_path_symlink_forbidden', result.stderr)
        self.assertEqual('protected', sentinel.read_text())

    def test_rejects_links_inside_existing_database_without_deleting_it(self) -> None:
        self._package_db('demo-rag')
        old = self._existing_db('demo-rag')
        (old / 'unexpected-link').symlink_to(self.root / 'missing')
        result = self._run()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('install_link_or_special_file_forbidden', result.stderr)
        self.assertEqual('old catalog', (old / 'catalog.sqlite').read_text())
        self.assertTrue((old / 'unexpected-link').is_symlink())

    def test_package_inside_existing_database_is_rejected_without_removing_package(self) -> None:
        old = self._existing_db('demo-rag')
        embedded = old / 'package'
        shutil.move(self.source, embedded)
        self.source = embedded
        self._package_db('demo-rag')
        before = self._snapshot(old)
        result = self._run()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('install_source_target_overlap', result.stderr)
        self.assertEqual(before, self._snapshot(old))

    def test_target_inside_package_is_rejected(self) -> None:
        self._package_db('demo-rag')
        self.target = self.source / '.copilot'
        before = self._snapshot(self.target)
        result = self._run()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('install_source_target_overlap', result.stderr)
        self.assertEqual(before, self._snapshot(self.target))

    def test_product_tar_producer_failure_preserves_existing_database(self) -> None:
        self._package_db('demo-rag')
        old = self._existing_db('demo-rag')
        before = self._snapshot(old)
        binaries = self.root / 'bin'
        real_tar = shutil.which('tar')
        self.assertIsNotNone(real_tar)
        wrapper = self._write(
            binaries / 'tar',
            '#!/bin/sh\n'
            f'"{real_tar}" "$@" || exit $?\n'
            'for arg do [ "$arg" != "-cf" ] || exit 23; done\n',
        )
        wrapper.chmod(0o755)
        result = self._run(environment={'PATH': str(binaries) + os.pathsep + os.environ['PATH']})
        self.assertNotEqual(0, result.returncode)
        self.assertIn('product_copy_failed', result.stderr)
        self.assertEqual(before, self._snapshot(old))
        self.assertFalse(list(self.target.glob('.rag-product-copy.*')))

    def test_copy_failure_removes_only_current_partial_db_and_keeps_completed_db(self) -> None:
        completed_source = self._package_db('a-rag')
        self._package_db('b-rag')
        completed_db = self._existing_db('a-rag')
        failed_db = self._existing_db('b-rag')
        other = self._existing_db('personal-rag')
        other_before = self._snapshot(other)
        marker = self._write(self.target / 'rag/query/.venv/.rag-deps-installed', 'old marker')
        binaries = self.root / 'bin'
        real_cp = shutil.which('cp')
        self.assertIsNotNone(real_cp)
        wrapper = self._write(
            binaries / 'cp',
            '#!/bin/sh\n'
            'for last do :; done\n'
            'case "$last" in */b-rag)\n'
            '  mkdir -p "$last"\n'
            '  printf partial > "$last/partial"\n'
            '  exit 23;;\n'
            'esac\n'
            f'exec "{real_cp}" "$@"\n',
        )
        wrapper.chmod(0o755)
        result = self._run(environment={'PATH': str(binaries) + os.pathsep + os.environ['PATH']})
        self.assertNotEqual(0, result.returncode)
        self.assertIn('database_copy_failed', result.stderr)
        self.assertIn('reinstall required', result.stderr)
        self.assertEqual(self._snapshot(completed_source), self._snapshot(completed_db))
        self.assertFalse(failed_db.exists())
        self.assertEqual(other_before, self._snapshot(other))
        self.assertEqual({'a-rag', 'personal-rag'}, {p.name for p in completed_db.parent.iterdir()})
        self.assertFalse(marker.exists())
        marker_backups = list(marker.parent.parent.glob('.rag-deps-installed.legacy.pre-update.*'))
        self.assertEqual(1, len(marker_backups))
        self.assertEqual('old marker', marker_backups[0].read_text())


class PowerShellDistributionReplacementContractTests(unittest.TestCase):
    def test_preflights_then_replaces_only_included_databases_and_cleans_failed_copy(self) -> None:
        script = _INSTALLERS.INSTALL_PS1_TEXT
        preflight = script.index('Assert-PlainTree $Payload')
        product_copy = script.index('$ProductItems | ForEach-Object')
        replacement = script.index('foreach ($Database in $DatabaseSources)', product_copy)
        self.assertLess(preflight, product_copy)
        self.assertLess(product_copy, replacement)
        self.assertIn('install_source_target_overlap', script[:product_copy])
        self.assertIn('ReparsePoint', script[:product_copy])
        self.assertIn('invalid_database_name', script[:product_copy])
        self.assertIn('duplicate_database_name', script[:product_copy])
        self.assertIn('Assert-PlainTree $DatabaseDestination', script[:product_copy])
        self.assertIn('Remove-Item -LiteralPath $DatabaseDestination -Recurse -Force', script[replacement:])
        self.assertIn('Copy-Item -LiteralPath $Database.FullName -Destination $DatabaseDestination -Recurse -Force', script[replacement:])
        self.assertIn('database_copy_failed: previous DB removed; reinstall required', script[replacement:])
        self.assertNotIn('Move-Item', script[replacement:])
        self.assertNotIn('DatabaseBackup', script)
        self.assertNotIn('DatabaseStage', script)


if __name__ == '__main__':
    unittest.main()
