from __future__ import annotations

import sqlite3
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.source_paths import (
    SourcePathError,
    canonical_stored_path,
    observed_root_from_paths,
    read_visible_source_paths,
    source_relative_path,
)
from software_rag_tool import catalog


class SourcePathContractTests(unittest.TestCase):
    def test_observed_root_and_source_relative_path(self) -> None:
        paths = [
            "Example Root/docs/one.md",
            r"Example Root\specs\two.md",
        ]
        self.assertEqual(("Example Root/",), observed_root_from_paths(paths))
        self.assertEqual(
            "docs/one.md",
            source_relative_path(paths[0], "Example Root/"),
        )
        self.assertEqual(
            "specs/two.md",
            source_relative_path(paths[1], "Example Root/"),
        )

    def test_zero_and_multiple_observed_roots(self) -> None:
        self.assertEqual((), observed_root_from_paths([]))
        self.assertEqual(
            ("First/", "Second/"),
            observed_root_from_paths(
                ["First/a.txt", "Second/b.txt"],
            ),
        )

    def test_japanese_spaces_and_unicode_form_are_preserved(self) -> None:
        root_nfc = unicodedata.normalize("NFC", "資料ガイド")
        root_nfd = unicodedata.normalize("NFD", "資料ガイド")
        first = f"{root_nfc}/設計 資料.md"
        second = f"{root_nfd}/別資料.md"
        self.assertEqual(first, canonical_stored_path(first))
        self.assertEqual(second, canonical_stored_path(second))
        expected = tuple(sorted({root_nfc + "/", root_nfd + "/"}))
        self.assertEqual(
            expected,
            observed_root_from_paths([first, second]),
        )

    def test_absolute_drive_unc_and_traversal_are_rejected(self) -> None:
        for value in (
            "/absolute/file.txt",
            r"C:\absolute\file.txt",
            r"\\server\share\file.txt",
            "../outside.txt",
            "Root/../outside.txt",
        ):
            with self.subTest(value=value):
                with self.assertRaises(SourcePathError):
                    canonical_stored_path(value)

    def test_root_is_removed_by_component_not_substring(self) -> None:
        self.assertEqual(
            "Root-name/file.txt",
            source_relative_path(
                "Root/Root-name/file.txt",
                "Root/",
            ),
        )
        with self.assertRaises(SourcePathError):
            source_relative_path(
                "Root-name/file.txt",
                "Root/",
            )

    def test_only_current_visible_documents_define_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = sqlite3.connect(root / "catalog.sqlite")
            try:
                connection.execute(
                    """
                    CREATE TABLE document (
                        doc_pk INTEGER PRIMARY KEY,
                        source_id TEXT,
                        path TEXT NOT NULL,
                        visible_until INTEGER
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO document VALUES (?, ?, ?, ?)",
                    (
                        (1, "source-a", "Current/a.txt", None),
                        (2, "source-a", "Superseded/b.txt", 2),
                        (3, "", "Missing/c.txt", None),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(
                {"source-a": ("Current/a.txt",)},
                read_visible_source_paths(root),
            )

    def test_readonly_catalog_uses_unc_path_form_without_uri_authority(
        self,
    ) -> None:
        resolved = mock.Mock()
        resolved.__str__ = mock.Mock(
            return_value=r"\\server\share\folder name\catalog.sqlite"
        )
        path = mock.Mock()
        path.resolve.return_value = resolved
        connection = mock.MagicMock()
        with mock.patch.object(
            catalog.sqlite3,
            "connect",
            return_value=connection,
        ) as connect:
            with catalog.connect_readonly(path):
                pass
        uri = connect.call_args.args[0]
        self.assertEqual(
            "file:////server/share/folder%20name/catalog.sqlite"
            "?mode=ro&immutable=1",
            uri,
        )
        self.assertTrue(connect.call_args.kwargs["uri"])


    def test_immutable_readonly_catalog_does_not_create_wal_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.sqlite"
            connection = sqlite3.connect(path)
            try:
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                self.assertEqual("wal", str(mode).casefold())
                connection.execute("CREATE TABLE demo(value TEXT)")
                connection.execute("INSERT INTO demo VALUES ('ok')")
                connection.commit()
            finally:
                connection.close()
            wal = Path(f"{path}-wal")
            shm = Path(f"{path}-shm")
            wal.unlink(missing_ok=True)
            shm.unlink(missing_ok=True)

            with catalog.connect_readonly(path) as readonly:
                self.assertEqual(
                    "ok",
                    readonly.execute("SELECT value FROM demo").fetchone()[0],
                )

            self.assertFalse(wal.exists())
            self.assertFalse(shm.exists())

    def test_runtime_uses_immutable_read_contract_and_portable_smoke_is_retired(self) -> None:
        runtime = (
            TOOL_ROOT / "software_rag_tool" / "db_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "catalog.readonly_uri(self.context.catalog_path)",
            runtime,
        )
        self.assertFalse((QUERY_ROOT / "portable_db_smoke.py").exists())

if __name__ == "__main__":
    unittest.main()
