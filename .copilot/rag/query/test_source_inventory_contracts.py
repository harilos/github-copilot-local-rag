from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import source_inventory
from software_rag_tool.source_inventory import build_source_inventory
from software_rag_tool.source_links import SCHEMA_VERSION


class SourceInventoryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_root = Path(self.temporary.name) / "sample-rag"
        self.db_root.mkdir()
        self.catalog_path = self.db_root / "catalog.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_visible_catalog_counts_prefixes_samples_and_missing_source(self) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a1", "alpha", "資料ルート/plans/a.md", "A", None),
                (2, "doc-a2", "alpha", "資料ルート/specs/b.md", "B", None),
                (3, "doc-b1", "beta", "別ルート/c.md", "C", None),
                (4, "doc-missing", "", "資料ルート/missing.md", "M", None),
                (5, "doc-hidden", "alpha", "資料ルート/hidden.md", "H", 2),
            ],
            chunks=[
                (1, "a1-1", 1, None),
                (2, "a1-2", 1, None),
                (3, "a1-hidden", 1, 2),
                (4, "b1-1", 3, None),
                (5, "missing-1", 4, None),
                (6, "missing-2", 4, None),
                (7, "hidden-doc-chunk", 5, None),
            ],
        )

        inventory = build_source_inventory(self.db_root)

        self.assertEqual("ready", inventory.catalog_status)
        self.assertEqual(["alpha", "beta"], inventory.source_ids())
        self.assertEqual(4, inventory.document_count)
        self.assertEqual(5, inventory.chunk_count)
        self.assertEqual(1, inventory.missing_source_document_count)
        self.assertEqual(2, inventory.missing_source_chunk_count)
        alpha = inventory.get_source("alpha")
        assert alpha is not None
        self.assertEqual(2, alpha.document_count)
        self.assertEqual(2, alpha.chunk_count)
        self.assertEqual(
            [("資料ルート/", 2)],
            [
                (item.root, item.document_count)
                for item in alpha.observed_roots
            ],
        )
        self.assertEqual(
            ["資料ルート/plans/a.md", "資料ルート/specs/b.md"],
            [item.path for item in alpha.sample_documents],
        )
        self.assertEqual("ready", alpha.observed_root_status)
        self.assertEqual(
            {
                "alpha": [
                    "資料ルート/plans/a.md",
                    "資料ルート/specs/b.md",
                ],
                "beta": ["別ルート/c.md"],
            },
            inventory.observed_paths_by_source(),
        )
        self.assertIn(
            "catalog_documents_missing_source_id",
            [item.code for item in inventory.diagnostics],
        )

    def test_multiple_current_roots_are_reported_for_one_source(self) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a", "alpha", "First Root/a.md", "A", None),
                (2, "doc-b", "alpha", "Second Root/b.md", "B", None),
                (3, "doc-old", "alpha", "Old Root/c.md", "C", 2),
            ],
            chunks=[
                (1, "a-1", 1, None),
                (2, "b-1", 2, None),
                (3, "old-1", 3, None),
            ],
        )
        inventory = build_source_inventory(self.db_root)
        source = inventory.get_source("alpha")
        assert source is not None
        self.assertEqual(
            ("First Root/", "Second Root/"),
            source.observed_stored_roots,
        )
        self.assertEqual(
            "multiple_observed_roots",
            source.observed_root_status,
        )

    def test_windows_separator_is_presented_as_canonical_stored_path(
        self,
    ) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a", "alpha", r"Root\docs\file.md", "A", None),
            ],
            chunks=[
                (1, "a-1", 1, None),
            ],
        )
        inventory = build_source_inventory(self.db_root)
        source = inventory.get_source("alpha")
        assert source is not None
        self.assertEqual(("Root/",), source.observed_stored_roots)
        self.assertEqual(("Root/docs/file.md",), source.document_paths)
        self.assertEqual("ready", source.observed_root_status)

    def test_catalog_is_opened_read_only_without_changing_files(self) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a", "alpha", "Root/a.md", "A", None),
            ],
            chunks=[
                (1, "a-1", 1, None),
            ],
        )
        before = self._catalog_fingerprint()
        original = source_inventory.connect_readonly

        with mock.patch.object(
            source_inventory,
            "connect_readonly",
            wraps=original,
        ) as readonly:
            inventory = build_source_inventory(self.db_root)

        self.assertEqual("ready", inventory.catalog_status)
        readonly.assert_called_once_with(self.catalog_path.resolve())
        self.assertEqual(before, self._catalog_fingerprint())
        self.assertFalse(Path(str(self.catalog_path) + "-wal").exists())
        self.assertFalse(Path(str(self.catalog_path) + "-shm").exists())

    def test_source_link_settings_overlay_and_report_unmatched_sources(self) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a", "alpha", "Root/a.md", "A", None),
            ],
            chunks=[
                (1, "a-1", 1, None),
            ],
        )
        (self.db_root / "source-links.json").write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "revision": 1,
                    "sources": [
                        {
                            "source_id": "alpha",
                            "display_name": "Alpha Source",
                            "source_type": "other",
                            "link": {
                                "enabled": True,
                                "strategy": "home-only",
                                "settings": {
                                    "source_home_url": "https://example.invalid/alpha"
                                },
                            },
                        },
                        {
                            "source_id": "not-indexed",
                            "source_type": "other",
                            "link": {
                                "enabled": False,
                                "strategy": "home-only",
                                "settings": {
                                    "source_home_url": "https://example.invalid/unused"
                                },
                            },
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        inventory = build_source_inventory(
            self.db_root,
            "sample-rag",
        )

        self.assertEqual("configured", inventory.source_links_status)
        alpha = inventory.get_source("alpha")
        assert alpha is not None
        assert alpha.link_setting is not None
        self.assertEqual("other", alpha.link_setting.provider)
        self.assertEqual("Alpha Source", alpha.display_name)
        self.assertTrue(alpha.link_setting.enabled)
        self.assertEqual(1, alpha.link_mapping_count)
        self.assertEqual(
            ["not-indexed"],
            [item.source_id for item in inventory.unmatched_settings],
        )
        payload = inventory.to_dict()
        self.assertEqual(
            "other",
            payload["sources"][0]["source_link_setting"]["provider"],
        )

    def test_malformed_optional_sidecar_does_not_break_inventory(self) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a", "alpha", "Root/a.md", "A", None),
            ],
            chunks=[
                (1, "a-1", 1, None),
            ],
        )
        (self.db_root / "source-links.json").write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "revision": 1,
                    "sources": [
                        {
                            "source_id": "alpha",
                            "enabled": True,
                            "provider": "redmine",
                            "strategy": "regex-template",
                            "settings": {
                                "path_pattern": r"(?P<id>[0-9]+)",
                                "url_template": (
                                    "https://example.invalid/items/{id"
                                ),
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        inventory = build_source_inventory(
            self.db_root,
            "sample-rag",
        )

        self.assertEqual("ready", inventory.catalog_status)
        self.assertEqual("invalid", inventory.source_links_status)
        self.assertEqual(["alpha"], inventory.source_ids())

    def test_deeply_nested_optional_sidecar_does_not_break_inventory(
        self,
    ) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a", "alpha", "Root/a.md", "A", None),
            ],
            chunks=[
                (1, "a-1", 1, None),
            ],
        )
        nested = "{}"
        for _index in range(1_100):
            nested = '{"nested":' + nested + "}"
        raw = (
            '{"schema_version":"rag-source-links-v2",'
            '"revision":1,'
            '"sources":[{"source_id":"alpha","enabled":true,'
            '"provider":"other","strategy":"home-only","settings":'
            + nested
            + "}]}"
        )
        (self.db_root / "source-links.json").write_text(
            raw,
            encoding="utf-8",
        )

        inventory = build_source_inventory(
            self.db_root,
            "sample-rag",
        )

        self.assertEqual("ready", inventory.catalog_status)
        self.assertEqual("invalid", inventory.source_links_status)
        self.assertEqual(["alpha"], inventory.source_ids())

    def test_supplemental_logs_are_non_authoritative_and_diagnostic(self) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a", "alpha", "Root/a.md", "A", None),
            ],
            chunks=[
                (1, "a-1", 1, None),
            ],
        )
        logs = self.db_root / "logs"
        logs.mkdir()
        (logs / "index_state.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "ingestion": {
                        "source_id": "alpha",
                        "resolved_root": "/synthetic/root",
                        "scan_subdir": "plans",
                    },
                    "files": {
                        "alpha:a": {
                            "source_id": "alpha",
                            "status": "indexed",
                            "record_count": 2,
                            "resolved_root": "/synthetic/root",
                            "scan_subdir": "plans",
                        },
                        "alpha:b": {
                            "source_id": "alpha",
                            "status": "error",
                            "record_ids": ["old-1"],
                            "resolved_root": "/synthetic/root",
                            "scan_subdir": "plans",
                        },
                        "ghost:c": {
                            "source_id": "ghost",
                            "status": "indexed",
                            "record_count": 4,
                        },
                        "missing:d": {
                            "source_id": "",
                            "status": "indexed",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        (logs / "progress.json").write_text(
            json.dumps(
                {
                    "source_id": "alpha",
                    "status": "running",
                    "phase": "extract",
                    "operation": "add",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "root": "/synthetic/root",
                    "resolved_root": "/synthetic/root",
                    "scan_subdir": "plans",
                    "scan_root": "/synthetic/root/plans",
                }
            ),
            encoding="utf-8",
        )

        inventory = build_source_inventory(self.db_root)

        alpha = inventory.get_source("alpha")
        assert alpha is not None
        state = alpha.supplemental_state
        self.assertEqual(2, state.state_file_count)
        self.assertEqual(1, state.indexed_files)
        self.assertEqual(1, state.error_files)
        self.assertEqual(3, state.record_count)
        self.assertEqual(("plans",), state.scan_subdirectories)
        self.assertEqual("running", state.progress_status)
        self.assertEqual("extract", state.progress_phase)
        self.assertEqual(
            ["ghost"],
            [
                item.source_id
                for item in inventory.unmatched_state_sources
            ],
        )
        codes = [item.code for item in inventory.diagnostics]
        self.assertIn("index_state_entries_missing_source_id", codes)
        self.assertIn(
            "supplemental_state_without_catalog_source",
            codes,
        )
        self.assertEqual(["alpha"], inventory.source_ids())

    def test_missing_catalog_and_invalid_sidecar_return_diagnostics(self) -> None:
        (self.db_root / "source-links.json").write_text(
            "{",
            encoding="utf-8",
        )

        inventory = build_source_inventory(self.db_root)

        self.assertEqual("missing", inventory.catalog_status)
        self.assertEqual("invalid", inventory.source_links_status)
        self.assertEqual([], inventory.source_ids())
        self.assertEqual(
            {"catalog_missing", "source_links_invalid"},
            {item.code for item in inventory.diagnostics},
        )
        self.assertFalse(self.catalog_path.exists())

    def test_invalid_stored_path_is_not_presented_as_mapping_prefix(self) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a", "alpha", r"C:\\private\\a.md", "A", None),
            ],
            chunks=[
                (1, "a-1", 1, None),
            ],
        )

        inventory = build_source_inventory(self.db_root)

        alpha = inventory.get_source("alpha")
        assert alpha is not None
        self.assertEqual((), alpha.observed_roots)
        self.assertEqual(
            ["invalid_stored_path"],
            [item.code for item in alpha.diagnostics],
        )

    def test_invalid_supplemental_record_count_is_diagnostic_only(self) -> None:
        self._create_catalog(
            documents=[
                (1, "doc-a", "alpha", "Root/a.md", "A", None),
            ],
            chunks=[
                (1, "a-1", 1, None),
            ],
        )
        logs = self.db_root / "logs"
        logs.mkdir()
        (logs / "index_state.json").write_text(
            json.dumps(
                {
                    "files": {
                        "alpha:a": {
                            "source_id": "alpha",
                            "status": "indexed",
                            "record_count": "invalid",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        inventory = build_source_inventory(self.db_root)
        alpha = inventory.get_source("alpha")
        assert alpha is not None
        self.assertEqual(0, alpha.supplemental_state.record_count)
        self.assertIn(
            "index_state_record_count_invalid",
            [item.code for item in inventory.diagnostics],
        )

    def _create_catalog(
        self,
        *,
        documents: list[tuple[int, str, str | None, str, str, int | None]],
        chunks: list[tuple[int, str, int, int | None]],
    ) -> None:
        connection = sqlite3.connect(self.catalog_path)
        try:
            connection.executescript(
                """
                CREATE TABLE document (
                  doc_pk INTEGER PRIMARY KEY,
                  doc_id TEXT NOT NULL UNIQUE,
                  source_id TEXT,
                  path TEXT NOT NULL,
                  title TEXT,
                  visible_until INTEGER
                );
                CREATE TABLE chunk (
                  chunk_pk INTEGER PRIMARY KEY,
                  chunk_uid TEXT NOT NULL UNIQUE,
                  doc_pk INTEGER NOT NULL,
                  visible_until INTEGER
                );
                """
            )
            connection.executemany(
                """
                INSERT INTO document(
                  doc_pk, doc_id, source_id, path, title, visible_until
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                documents,
            )
            connection.executemany(
                """
                INSERT INTO chunk(
                  chunk_pk, chunk_uid, doc_pk, visible_until
                ) VALUES (?, ?, ?, ?)
                """,
                chunks,
            )
            connection.commit()
        finally:
            connection.close()

    def _catalog_fingerprint(self) -> tuple[int, str]:
        raw = self.catalog_path.read_bytes()
        return len(raw), hashlib.sha256(raw).hexdigest()


if __name__ == "__main__":
    unittest.main()
