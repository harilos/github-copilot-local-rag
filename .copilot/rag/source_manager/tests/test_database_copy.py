from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from source_manager.database_copy import (
    proposed_copy_name,
    strike_text,
)
from source_manager.database_copy_core import copy_database
from source_manager.database_copy_core import (
    DatabaseCopyError,
    validate_copied_database,
)
from source_manager.database_copy_storage import (
    delete_excluded_sources,
    validate_excluded_vectors,
)


class DatabaseCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-db-copy-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "full-rag"
        (self.source / "index").mkdir(parents=True)
        (self.source / "logs").mkdir()
        (self.source / "sources").mkdir()
        (self.source / "db.json").write_text(
            json.dumps(
                {
                    "db_name": "full-rag",
                    "title": "Full",
                    "collection": "full_rag_ruri3_30m_int8_v1",
                    "profile": "DB_PROFILE.md",
                }
            ),
            encoding="utf-8",
        )
        (self.source / "VERSION.json").write_text(
            json.dumps(
                {
                    "schema": "local-rag.db-version.v1",
                    "db_name": "full-rag",
                    "collection": "full_rag_ruri3_30m_int8_v1",
                    "db_hash": "source-hash",
                }
            ),
            encoding="utf-8",
        )
        (self.source / "DB_PROFILE.md").write_text(
            "# Full\n\n## Query Hint\n\nAll documents\n",
            encoding="utf-8",
        )
        (self.source / "index" / "manifest.json").write_text(
            json.dumps(
                {
                    "collection": "full_rag_ruri3_30m_int8_v1",
                    "record_count": 0,
                }
            ),
            encoding="utf-8",
        )
        (self.source / "logs" / "index_state.json").write_text(
            json.dumps(
                {
                    "ingestion": {
                        "root": str(self.source / "sources"),
                        "resolved_root": str(self.source / "sources"),
                    }
                }
            ),
            encoding="utf-8",
        )
        source_dir = self.source / "sources" / "src_docs-0123456789ab"
        source_dir.mkdir()
        (source_dir / "source.json").write_text(
            json.dumps(
                {
                    "schema_version": "local-rag-source-manager-v1",
                    "local_source_key": "src_docs-0123456789ab",
                    "source_id": "src_docs-0123456789ab",
                    "source_type": "sharepoint",
                    "display_name": "Docs",
                    "fetch": {
                        "root_env": "LOCAL_RAG_SHAREPOINT_ROOT",
                        "relative_path": "Team/Docs",
                    },
                    "ingest": {
                        "work_directory": (
                            "sources/src_docs-0123456789ab/work/ingest/"
                            "src_docs-0123456789ab"
                        ),
                        "logical_root_name": "src_docs-0123456789ab",
                    },
                    "metadata_sync_pending": False,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_copy_rebinds_db_identity_and_preserves_source_location(self) -> None:
        destination = self.root / "public-rag"
        result = copy_database(
            self.source,
            destination,
            destination_name="public-rag",
            title="Public",
            query_hint="Public documents",
            rag_root=self.root,
        )

        self.assertEqual("copied", result["status"])
        config = json.loads((destination / "db.json").read_text(encoding="utf-8"))
        self.assertEqual("public-rag", config["db_name"])
        self.assertEqual(
            "public_rag_ruri3_30m_int8_v1",
            config["collection"],
        )
        copied_source = json.loads(
            (
                destination
                / "sources"
                / "src_docs-0123456789ab"
                / "source.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("Team/Docs", copied_source["fetch"]["relative_path"])
        self.assertEqual(
            "LOCAL_RAG_SHAREPOINT_ROOT",
            copied_source["fetch"]["root_env"],
        )
        state = json.loads(
            (destination / "logs" / "index_state.json").read_text(encoding="utf-8")
        )
        self.assertTrue(state["ingestion"]["root"].startswith(str(destination)))
        self.assertTrue((self.source / "db.json").is_file())

    def test_failed_copy_does_not_publish_destination(self) -> None:
        destination = self.root / "failed-rag"
        with mock.patch(
            "source_manager.database_copy_core.copy_catalog_snapshot",
            side_effect=RuntimeError("synthetic failure"),
        ):
            with self.assertRaises(RuntimeError):
                copy_database(
                    self.source,
                    destination,
                    destination_name="failed-rag",
                    title="Failed",
                    query_hint="",
                    rag_root=self.root,
                )
        self.assertFalse(destination.exists())
        self.assertTrue(self.source.exists())

    def test_excluded_source_uses_destination_vector_collection(self) -> None:
        staging = self.root / "copy-rag"
        source_dir = staging / "sources" / "src_secret-abcdef012345"
        source_dir.mkdir(parents=True)
        (source_dir / "source.json").write_text("{}", encoding="utf-8")
        (staging / "source-links.json.bak").write_text(
            json.dumps({"sources": [{"source_id": "secret-source"}]}),
            encoding="utf-8",
        )
        delete_calls: list[tuple[str, str, str, str]] = []
        source_delete = types.ModuleType("software_rag_tool.source_delete")
        source_delete.delete_source_data = lambda source_id: (
            delete_calls.append(
                (
                    source_id,
                    os.environ["RAG_OUTPUT_ROOT"],
                    os.environ["CHROMA_DIR_V2"],
                    os.environ["CHROMA_COLLECTION"],
                )
            )
            or {"status": "deleted", "source_id": source_id}
        )
        package = types.ModuleType("software_rag_tool")
        package.__path__ = []

        class Loaded:
            payload = {"value": True}
            revision = 1
            etag = "etag"

        class Store:
            def __init__(self, root: Path) -> None:
                self.root = Path(root)

            def read_source(self, key: str) -> Loaded:
                return Loaded()

            def delete_source(self, key: str, **_kwargs: object) -> None:
                import shutil

                shutil.rmtree(self.root / "sources" / key)

        with (
            mock.patch.dict(
                sys.modules,
                {
                    "software_rag_tool": package,
                    "software_rag_tool.source_delete": source_delete,
                },
            ),
            mock.patch("source_manager.database_copy_storage.SourceStore", Store),
            mock.patch(
                "source_manager.database_copy_storage.remove_source_metadata"
            ) as metadata_remove,
        ):
            result = delete_excluded_sources(
                staging,
                [
                    {
                        "source_id": "secret-source",
                        "_local_source_key": "src_secret-abcdef012345",
                        "display_name": "Secret",
                    }
                ],
                destination_name="copy-rag",
                collection="copy_rag_ruri3_30m_int8_v1",
                rag_root=self.root,
                progress_callback=None,
                error_type=RuntimeError,
            )

        self.assertEqual(1, len(result))
        self.assertEqual(
            [
                (
                    "secret-source",
                    str(staging),
                    str(staging / "index" / "chroma"),
                    "copy_rag_ruri3_30m_int8_v1",
                )
            ],
            delete_calls,
        )
        metadata_remove.assert_called_once()
        self.assertFalse(source_dir.exists())
        self.assertFalse((staging / "source-links.json.bak").exists())

    def test_excluded_source_metadata_failure_precedes_index_delete(self) -> None:
        staging = self.root / "copy-rag"
        staging.mkdir()
        delete_calls: list[str] = []
        source_delete = types.ModuleType("software_rag_tool.source_delete")
        source_delete.delete_source_data = lambda source_id: (
            delete_calls.append(source_id)
            or {"status": "deleted", "source_id": source_id}
        )
        package = types.ModuleType("software_rag_tool")
        package.__path__ = []
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "software_rag_tool": package,
                    "software_rag_tool.source_delete": source_delete,
                },
            ),
            mock.patch(
                "source_manager.database_copy_storage.remove_source_metadata",
                side_effect=RuntimeError("synthetic metadata failure"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "failed to remove copied Source Metadata",
            ):
                delete_excluded_sources(
                    staging,
                    [{"source_id": "secret-source"}],
                    destination_name="copy-rag",
                    collection="copy_rag_ruri3_30m_int8_v1",
                    rag_root=self.root,
                    progress_callback=None,
                    error_type=RuntimeError,
                )
        self.assertEqual([], delete_calls)

    def test_copy_validation_checks_every_portable_exclusion_layer(self) -> None:
        excluded = [
            {
                "source_id": "secret-source",
                "_local_source_key": "src_secret-abcdef012345",
            }
        ]

        def make_root(name: str) -> Path:
            root = self.root / name
            (root / "data" / "clean").mkdir(parents=True)
            (root / "logs").mkdir()
            (root / "sources").mkdir()
            (root / "db.json").write_text(
                json.dumps(
                    {
                        "db_name": "copy-rag",
                        "collection": "copy_collection",
                    }
                ),
                encoding="utf-8",
            )
            return root

        def validate(root: Path) -> None:
            with mock.patch(
                "source_manager.database_copy_core.validate_excluded_vectors"
            ) as vector_check:
                validate_copied_database(
                    root,
                    destination_name="copy-rag",
                    collection="copy_collection",
                    excluded_sources=excluded,
                )
            vector_check.assert_called_once_with(
                root,
                collection="copy_collection",
                source_ids={"secret-source"},
                error_type=DatabaseCopyError,
            )

        clean_root = make_root("clean-layer")
        (clean_root / "data" / "clean" / "secret.jsonl").write_text(
            json.dumps(
                {
                    "id": "secret-record",
                    "metadata": {"source_id": "secret-source"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DatabaseCopyError, "clean records"):
            validate(clean_root)

        state_root = make_root("state-layer")
        (state_root / "logs" / "index_state.json").write_text(
            json.dumps(
                {
                    "files": {
                        "secret": {
                            "source_id": "secret-source",
                            "record_ids": ["secret-record"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DatabaseCopyError, "ADD state"):
            validate(state_root)

        metadata_root = make_root("metadata-layer")
        (metadata_root / "source-links.json").write_text(
            json.dumps(
                {
                    "schema_version": "rag-source-metadata-v1",
                    "revision": 1,
                    "sources": [{"source_id": "secret-source"}],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DatabaseCopyError, "Source Metadata"):
            validate(metadata_root)

        backup_root = make_root("metadata-backup-layer")
        (backup_root / "source-links.json.bak").write_text(
            json.dumps(
                {
                    "schema_version": "rag-source-metadata-v1",
                    "revision": 1,
                    "sources": [{"source_id": "secret-source"}],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            DatabaseCopyError,
            "Source Metadata backup",
        ):
            validate(backup_root)

        management_root = make_root("management-layer")
        (
            management_root
            / "sources"
            / "src_secret-abcdef012345"
        ).mkdir()
        with self.assertRaisesRegex(DatabaseCopyError, "management directory"):
            validate(management_root)

        catalog_root = make_root("catalog-layer")
        import sqlite3

        connection = sqlite3.connect(catalog_root / "catalog.sqlite")
        try:
            connection.execute(
                "CREATE TABLE document (doc_id TEXT PRIMARY KEY, source_id TEXT)"
            )
            connection.execute(
                "INSERT INTO document(doc_id, source_id) VALUES (?, ?)",
                ("secret-doc", "secret-source"),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(DatabaseCopyError, "copied catalog"):
            validate(catalog_root)

        valid_root = make_root("all-clear")
        validate(valid_root)

    def test_vector_exclusion_validation_uses_copied_collection(self) -> None:
        vector_root = self.root / "vector-copy"
        (vector_root / "index" / "chroma").mkdir(parents=True)
        observed: list[tuple[str, dict[str, str], int, list[str]]] = []

        class Collection:
            def get(self, *, where, limit, include):
                observed.append(("copy_collection", where, limit, include))
                return {"ids": ["secret-record"]}

        class Client:
            _system = None

            def get_collection(self, *, name):
                self.name = name
                return Collection()

        chromadb = types.ModuleType("chromadb")
        chromadb.PersistentClient = lambda **_kwargs: Client()
        config = types.ModuleType("chromadb.config")
        config.Settings = lambda **_kwargs: object()
        with mock.patch.dict(
            sys.modules,
            {"chromadb": chromadb, "chromadb.config": config},
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "copied Chroma collection",
            ):
                validate_excluded_vectors(
                    vector_root,
                    collection="copy_collection",
                    source_ids={"secret-source"},
                    error_type=RuntimeError,
                )
        self.assertEqual(
            [
                (
                    "copy_collection",
                    {"source_id": "secret-source"},
                    1,
                    [],
                )
            ],
            observed,
        )

    def test_excluded_label_has_strikethrough_fallback(self) -> None:
        plain = types.SimpleNamespace(use_color=False)
        colored = types.SimpleNamespace(use_color=True)
        self.assertEqual("~~Secret~~", strike_text(plain, "Secret"))
        self.assertEqual("\033[9mSecret\033[0m", strike_text(colored, "Secret"))
        self.assertEqual("project-copy-rag", proposed_copy_name("project-rag"))


if __name__ == "__main__":
    unittest.main()
