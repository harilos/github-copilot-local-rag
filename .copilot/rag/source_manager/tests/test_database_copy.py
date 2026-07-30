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
    choose_excluded_sources,
    proposed_copy_name,
    strike_text,
)
from source_manager.database_copy_core import copy_database
from source_manager.database_copy_storage import delete_excluded_sources
from source_manager.store import SourceStore


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
        classifications = SourceStore(staging).read_source_classifications()
        SourceStore(staging).save_source_classification(
            "secret-source",
            "secret",
            expected_revision=classifications.revision,
            expected_etag=classifications.etag,
        )
        delete_calls: list[tuple[str, str, str]] = []
        source_delete = types.ModuleType("software_rag_tool.source_delete")
        source_delete.delete_source_data = lambda source_id: (
            delete_calls.append(
                (
                    source_id,
                    os.environ["RAG_OUTPUT_ROOT"],
                    os.environ["CHROMA_COLLECTION"],
                )
            )
            or {"status": "deleted"}
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
                    "copy_rag_ruri3_30m_int8_v1",
                )
            ],
            delete_calls,
        )
        metadata_remove.assert_called_once()
        self.assertFalse(source_dir.exists())
        remaining = SourceStore(staging).read_source_classifications()
        self.assertEqual([], remaining.payload["sources"])

    def test_excluded_label_has_strikethrough_fallback(self) -> None:
        plain = types.SimpleNamespace(use_color=False)
        colored = types.SimpleNamespace(use_color=True)
        self.assertEqual("~~Secret~~", strike_text(plain, "Secret"))
        self.assertEqual("\033[9mSecret\033[0m", strike_text(colored, "Secret"))
        self.assertEqual("project-copy-rag", proposed_copy_name("project-rag"))

    def test_secret_source_is_excluded_by_default_in_manager_choice(self) -> None:
        output: list[str] = []
        answers = iter(["c"])

        class Manager:
            use_color = False

            @staticmethod
            def _print_screen_header(*_args: object, **_kwargs: object) -> None:
                return None

            @staticmethod
            def _print_info(*_args: object, **_kwargs: object) -> None:
                return None

            @staticmethod
            def _invalid_selection(*_args: object, **_kwargs: object) -> None:
                raise AssertionError("unexpected invalid selection")

            @staticmethod
            def _ask(_prompt: str) -> str:
                return next(answers)

            @staticmethod
            def output(value: str) -> None:
                output.append(value)

        selected = choose_excluded_sources(
            Manager(),
            "master-rag",
            [
                {
                    "source_id": "public",
                    "display_name": "Public",
                },
                {
                    "source_id": "secret",
                    "display_name": "Secret",
                    "classification": "secret",
                },
                {
                    "source_id": "unset",
                    "display_name": "Unset",
                },
            ],
        )

        self.assertEqual({"secret"}, selected)
        self.assertIn("[秘密]", "\n".join(output))

    def test_secret_source_can_be_explicitly_included(self) -> None:
        answers = iter(["1", "c"])

        class Manager:
            use_color = False

            @staticmethod
            def _print_screen_header(*_args: object, **_kwargs: object) -> None:
                return None

            @staticmethod
            def _print_info(*_args: object, **_kwargs: object) -> None:
                return None

            @staticmethod
            def _invalid_selection(*_args: object, **_kwargs: object) -> None:
                raise AssertionError("unexpected invalid selection")

            @staticmethod
            def _ask(_prompt: str) -> str:
                return next(answers)

            @staticmethod
            def output(_value: str) -> None:
                return None

        selected = choose_excluded_sources(
            Manager(),
            "master-rag",
            [
                {
                    "source_id": "secret",
                    "display_name": "Secret",
                    "classification": "secret",
                }
            ],
        )

        self.assertEqual(set(), selected)


if __name__ == "__main__":
    unittest.main()
