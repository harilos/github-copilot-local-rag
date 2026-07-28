from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import source_links


ADD_CLI = RAG_ROOT / "gen_db" / "add_data.py"


def _write_catalog(db_root: Path) -> None:
    connection = sqlite3.connect(db_root / "catalog.sqlite")
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
        connection.execute(
            """
            INSERT INTO document(doc_pk, source_id, path, visible_until)
            VALUES (1, 'source-a', 'Example Root/docs/a.md', NULL)
            """
        )
        connection.commit()
    finally:
        connection.close()


class SourceMetadataContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-source-metadata-"
        )
        self.db_root = Path(self.temporary.name) / "example-rag"
        self.db_root.mkdir()
        _write_catalog(self.db_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_source_type_and_link_are_independently_optional(self) -> None:
        normalized = source_links.validate_source_links(
            {
                "schema_version": source_links.SCHEMA_VERSION,
                "revision": 1,
                "sources": [
                    {"source_id": "source-a"},
                    {"source_id": "source-b", "source_type": "github"},
                ],
            },
            allow_unmatched_sources=True,
        )
        self.assertNotIn("source_type", normalized["sources"][0])
        self.assertNotIn("link", normalized["sources"][0])
        self.assertEqual("github", normalized["sources"][1]["source_type"])
        self.assertNotIn("link", normalized["sources"][1])

    def test_new_link_without_source_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            source_links.SourceLinkError,
            "requires source_type",
        ):
            source_links.validate_source_links(
                {
                    "schema_version": source_links.SCHEMA_VERSION,
                    "revision": 1,
                    "sources": [
                        {
                            "source_id": "source-a",
                            "link": {
                                "enabled": True,
                                "strategy": "home-only",
                                "settings": {
                                    "source_home_url": (
                                        "https://docs.example.invalid"
                                    )
                                },
                            },
                        }
                    ],
                }
            )

    def test_only_legacy_sharepoint_has_a_compatibility_reader(self) -> None:
        path = self.db_root / source_links.SIDECAR_NAME
        legacy_sharepoint = {
            "schema_version": source_links.LEGACY_V2_SCHEMA_VERSION,
            "revision": 2,
            "sources": [
                {
                    "source_id": "source-a",
                    "provider": "sharepoint",
                    "enabled": True,
                    "strategy": "append-relative-path",
                    "settings": {
                        "source_web_root": (
                            "https://sharepoint.example.invalid/sites/"
                            "project/Shared%20Documents"
                        )
                    },
                }
            ],
        }
        original = json.dumps(legacy_sharepoint).encode("utf-8")
        path.write_bytes(original)
        loaded = source_links.load_source_links(
            self.db_root,
            self.db_root.name,
        )
        self.assertIsNotNone(loaded.payload)
        self.assertTrue(loaded.migration_required)
        self.assertEqual("sharepoint", loaded.payload["sources"][0]["source_type"])
        self.assertEqual(original, path.read_bytes())

        legacy_sharepoint["sources"][0]["provider"] = "github"
        path.write_text(json.dumps(legacy_sharepoint), encoding="utf-8")
        rejected = source_links.load_source_links(
            self.db_root,
            self.db_root.name,
        )
        self.assertIsNone(rejected.payload)
        self.assertEqual("manual_required", rejected.status)
        self.assertEqual(
            "legacy_non_sharepoint_manual_required",
            rejected.error_kind,
        )

    def test_atomic_save_never_creates_a_lock_file(self) -> None:
        payload = {
            "schema_version": source_links.SCHEMA_VERSION,
            "revision": 1,
            "sources": [
                {
                    "source_id": "source-a",
                    "source_type": "github",
                    "link": {
                        "enabled": True,
                        "strategy": "github-blob",
                        "settings": {
                            "repository_url": (
                                "https://github.example.invalid/org/repo"
                            ),
                            "ref": "main",
                            "permalink_enabled": False,
                        },
                    },
                }
            ],
        }
        source_links.save_source_links(
            self.db_root,
            payload,
            db_name=self.db_root.name,
            existing_sources={"source-a"},
            expected_revision=0,
            expected_etag="missing",
        )
        self.assertFalse((self.db_root / ".source-links.lock").exists())
        self.assertTrue((self.db_root / source_links.SIDECAR_NAME).is_file())

    def test_add_cli_does_not_gain_source_type_metadata_options(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ADD_CLI), "--help"],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("--source-type", completed.stdout)


if __name__ == "__main__":
    unittest.main()
