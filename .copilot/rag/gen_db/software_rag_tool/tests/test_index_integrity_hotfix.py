from __future__ import annotations

import gc
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool import catalog, incremental, source_delete, store
from software_rag_tool.embeddings import DocumentTokenBudget


class CharacterTokenizer:
    def __call__(self, text, **_kwargs):
        values = [text] if isinstance(text, str) else list(text)
        encoded = [[1, *range(2, len(value) + 2), 2] for value in values]
        return {"input_ids": encoded[0] if isinstance(text, str) else encoded}


def test_budget() -> DocumentTokenBudget:
    return DocumentTokenBudget(
        tokenizer=CharacterTokenizer(),
        document_prefix="document: ",
        tokenizer_name="explicit-test-double",
        target_tokens=320,
        max_tokens=384,
    )


class ResetCollectionContracts(unittest.TestCase):
    def test_absent_directory_is_idempotent_without_client_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            with (
                mock.patch.object(store, "chroma_dir", return_value=missing),
                mock.patch.object(store, "_persistent_client") as client,
            ):
                store.reset_collection()
            client.assert_not_called()

    def test_absent_collection_is_idempotent(self) -> None:
        from chromadb.errors import NotFoundError

        client = mock.Mock()
        client.get_collection.side_effect = NotFoundError("missing")
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(store, "chroma_dir", return_value=Path(temporary)),
            mock.patch.object(store, "_persistent_client", return_value=client),
        ):
            store.reset_collection()
        client.delete_collection.assert_not_called()

    def test_delete_failure_propagates(self) -> None:
        client = mock.Mock()
        client.get_collection.return_value = object()
        client.delete_collection.side_effect = PermissionError("locked")
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(store, "chroma_dir", return_value=Path(temporary)),
            mock.patch.object(store, "_persistent_client", return_value=client),
            self.assertRaisesRegex(PermissionError, "locked"),
        ):
            store.reset_collection()

    def test_real_chroma_reset_removes_old_ids_and_retry_has_no_mixture(self) -> None:
        import chromadb
        from chromadb.api.client import SharedSystemClient
        from chromadb.config import Settings
        from chromadb.errors import NotFoundError

        with tempfile.TemporaryDirectory() as temporary:
            cdir = Path(temporary) / "chroma"
            cdir.mkdir()
            environment = {
                "CHROMA_DIR_V2": str(cdir),
                "CHROMA_COLLECTION": "reset_fixture",
            }
            with mock.patch.dict("os.environ", environment):
                client = chromadb.PersistentClient(
                    path=str(cdir),
                    settings=Settings(anonymized_telemetry=False),
                )
                collection = client.get_or_create_collection("reset_fixture")
                collection.add(
                    ids=["old-a", "old-b"],
                    documents=["old a", "old b"],
                    embeddings=[[1.0, 0.0], [0.0, 1.0]],
                    metadatas=[{"source_id": "old"}, {"source_id": "old"}],
                )
                store.reset_collection()
                with self.assertRaises(NotFoundError):
                    client.get_collection("reset_fixture")
                replacement = client.get_or_create_collection("reset_fixture")
                replacement.add(
                    ids=["new"],
                    documents=["new"],
                    embeddings=[[1.0, 1.0]],
                    metadatas=[{"source_id": "new"}],
                )
                self.assertEqual(["new"], replacement.get()["ids"])
                system = client._system
                del replacement, collection, client
                gc.collect()
                system.stop()
                SharedSystemClient.clear_system_cache()

    def test_incremental_reset_failure_precedes_every_persistent_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "input"
            root.mkdir()
            (root / "document.txt").write_text("body", encoding="utf-8")
            with (
                mock.patch.object(
                    incremental,
                    "reset_collection",
                    side_effect=PermissionError("locked"),
                ),
                mock.patch.object(incremental, "reset_catalog") as reset_catalog,
                mock.patch.object(incremental, "_reset_clean_dir") as reset_clean,
                mock.patch.object(incremental, "_save_state") as save_state,
                mock.patch.object(incremental, "upsert_records") as upsert,
                mock.patch.object(incremental, "write_manifest") as manifest,
                self.assertRaisesRegex(PermissionError, "locked"),
            ):
                incremental.add_or_update_root(
                    root=root,
                    source_id="fixture",
                    reset_db=True,
                    reset_clean=True,
                    document_token_budget=test_budget(),
                )
            reset_catalog.assert_not_called()
            reset_clean.assert_not_called()
            save_state.assert_not_called()
            upsert.assert_not_called()
            manifest.assert_not_called()


class SourceDeleteCrossStoreContracts(unittest.TestCase):
    @staticmethod
    def _vector_record(source_id: str, record_id: str) -> dict:
        return {"id": record_id, "metadata": {"source_id": source_id}}

    def test_vector_inventory_failure_precedes_every_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("data/clean", "logs", "index"):
                (root / relative).mkdir(parents=True, exist_ok=True)
            with (
                mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(root)}),
                mock.patch.object(source_delete, "_validate_destructive_storage"),
                mock.patch.object(catalog, "ensure_source_delete_index"),
                mock.patch.object(catalog, "source_chunk_ids", return_value=["a"]),
                mock.patch.object(
                    source_delete,
                    "_inventory_vector_records",
                    side_effect=RuntimeError("filter unsupported"),
                ),
                mock.patch.object(catalog, "delete_source_documents") as delete_catalog,
                mock.patch.object(source_delete, "delete_ids") as delete_vectors,
                mock.patch.object(source_delete, "_apply_clean_deletion") as delete_clean,
                self.assertRaisesRegex(RuntimeError, "filter unsupported"),
            ):
                source_delete.delete_source_data("source-a")
            delete_vectors.assert_not_called()
            delete_catalog.assert_not_called()
            delete_clean.assert_not_called()

    def test_residual_vector_fails_before_catalog_clean_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("data/clean", "logs", "index"):
                (root / relative).mkdir(parents=True, exist_ok=True)
            state_path = root / "logs" / "index_state.json"
            state_path.write_text(
                json.dumps({"version": 2, "files": {"a": {"source_id": "source-a"}}}),
                encoding="utf-8",
            )
            target = self._vector_record("source-a", "a")
            with (
                mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(root)}),
                mock.patch.object(source_delete, "_validate_destructive_storage"),
                mock.patch.object(catalog, "ensure_source_delete_index"),
                mock.patch.object(catalog, "source_chunk_ids", return_value=["a"]),
                mock.patch.object(
                    source_delete,
                    "_inventory_vector_records",
                    side_effect=[[target], [target]],
                ),
                mock.patch.object(source_delete, "_plan_clean_deletion", return_value=[]),
                mock.patch.object(source_delete, "delete_ids", return_value=1),
                mock.patch.object(catalog, "delete_source_documents") as delete_catalog,
                mock.patch.object(source_delete, "_apply_clean_deletion") as delete_clean,
                self.assertRaisesRegex(RuntimeError, "residual"),
            ):
                source_delete.delete_source_data("source-a")
            delete_catalog.assert_not_called()
            delete_clean.assert_not_called()
            self.assertIn("source-a", state_path.read_text(encoding="utf-8"))

    def test_vector_only_orphan_is_deleted_without_catalog_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("data/clean", "logs", "index"):
                (root / relative).mkdir(parents=True, exist_ok=True)
            orphan = self._vector_record("source-a", "orphan")
            with (
                mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(root)}),
                mock.patch.object(source_delete, "_validate_destructive_storage"),
                mock.patch.object(catalog, "ensure_source_delete_index"),
                mock.patch.object(catalog, "source_chunk_ids", return_value=[]),
                mock.patch.object(
                    source_delete,
                    "_inventory_vector_records",
                    side_effect=[[orphan], []],
                ),
                mock.patch.object(source_delete, "_plan_clean_deletion", return_value=[]),
                mock.patch.object(source_delete, "delete_ids", return_value=1) as delete_ids,
                mock.patch.object(
                    catalog,
                    "delete_source_documents",
                    return_value={"documents": 0, "chunks": 0},
                ),
                mock.patch.object(catalog, "chunk_count", return_value=0),
            ):
                result = source_delete.delete_source_data("source-a")
            delete_ids.assert_called_once_with(["orphan"])
            self.assertEqual(1, result["vector_records_deleted"])

    def test_missing_catalog_state_and_clean_each_converge(self) -> None:
        for missing in ("catalog", "state", "clean"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                clean = root / "data" / "clean" / "records"
                logs = root / "logs"
                index = root / "index"
                clean.mkdir(parents=True)
                logs.mkdir()
                index.mkdir()
                record = {
                    "id": "target",
                    "text": "fixture",
                    "metadata": {"source_id": "source-a", "path": "same.md"},
                }
                clean_file = clean / "target.jsonl"
                if missing != "clean":
                    clean_file.write_text(json.dumps(record) + "\n", encoding="utf-8")
                state_path = logs / "index_state.json"
                if missing != "state":
                    state_path.write_text(
                        json.dumps(
                            {
                                "version": 2,
                                "files": {
                                    "target": {
                                        "source_id": "source-a",
                                        "record_ids": ["target"],
                                        "record_count": 1,
                                        "records_path": "records/target.jsonl",
                                    }
                                },
                                "ingestion": {},
                            }
                        ),
                        encoding="utf-8",
                    )
                vector = self._vector_record("source-a", "target")
                catalog_ids = [] if missing == "catalog" else ["target"]
                with (
                    mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(root)}),
                    mock.patch.object(source_delete, "_validate_destructive_storage"),
                    mock.patch.object(catalog, "ensure_source_delete_index"),
                    mock.patch.object(catalog, "source_chunk_ids", return_value=catalog_ids),
                    mock.patch.object(
                        source_delete,
                        "_inventory_vector_records",
                        side_effect=[[vector], []],
                    ),
                    mock.patch.object(source_delete, "delete_ids", return_value=1),
                    mock.patch.object(
                        catalog,
                        "delete_source_documents",
                        return_value={
                            "documents": int(missing != "catalog"),
                            "chunks": int(missing != "catalog"),
                        },
                    ),
                    mock.patch.object(catalog, "chunk_count", return_value=0),
                ):
                    result = source_delete.delete_source_data("source-a")
                self.assertEqual("deleted", result["status"])
                self.assertFalse(clean_file.exists())
                if state_path.exists():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    self.assertEqual({}, state["files"])

    def test_real_sqlite_chroma_catalog_missing_orphan_preserves_sibling(self) -> None:
        import chromadb
        from chromadb.api.client import SharedSystemClient
        from chromadb.config import Settings

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fixture-rag"
            clean = root / "data" / "clean" / "records"
            logs = root / "logs"
            cdir = root / "index" / "chroma"
            clean.mkdir(parents=True)
            logs.mkdir()
            cdir.mkdir(parents=True)
            environment = {
                "RAG_OUTPUT_ROOT": str(root),
                "RAG_DB_NAME": "fixture-rag",
                "CHROMA_DIR_V2": str(cdir),
                "CHROMA_COLLECTION": "fixture_rag_ruri3_30m_int8_v1",
            }
            client = None
            collection = None
            try:
                with mock.patch.dict(os.environ, environment):
                    records = [
                        {
                            "id": "a-catalog",
                            "text": "same content",
                            "metadata": {
                                "doc_id": "doc-a",
                                "source_id": "source-a",
                                "path": "same/document.md",
                            },
                        },
                        {
                            "id": "b-catalog",
                            "text": "same content",
                            "metadata": {
                                "doc_id": "doc-b",
                                "source_id": "source-b",
                                "path": "same/document.md",
                            },
                        },
                    ]
                    catalog.upsert_records(records)
                    mixed = clean / "mixed.jsonl"
                    mixed.write_text(
                        "".join(json.dumps(record) + "\n" for record in records),
                        encoding="utf-8",
                    )
                    (logs / "index_state.json").write_text(
                        json.dumps(
                            {
                                "version": 2,
                                "files": {
                                    "a": {
                                        "source_id": "source-a",
                                        "record_ids": ["a-catalog", "a-orphan"],
                                        "records_path": "records/mixed.jsonl",
                                    },
                                    "b": {
                                        "source_id": "source-b",
                                        "record_ids": ["b-catalog"],
                                        "records_path": "records/mixed.jsonl",
                                    },
                                },
                                "ingestion": {},
                            }
                        ),
                        encoding="utf-8",
                    )
                    (root / "index" / "manifest.json").write_text(
                        json.dumps({"record_count": 3}),
                        encoding="utf-8",
                    )
                    client = chromadb.PersistentClient(
                        path=str(cdir),
                        settings=Settings(anonymized_telemetry=False),
                    )
                    collection = client.get_or_create_collection(
                        "fixture_rag_ruri3_30m_int8_v1"
                    )
                    collection.add(
                        ids=["a-catalog", "a-orphan", "b-catalog"],
                        documents=["same content", "orphan", "same content"],
                        embeddings=[[1.0, 0.0], [1.0, 0.5], [0.0, 1.0]],
                        metadatas=[
                            {"source_id": "source-a", "path": "same/document.md"},
                            {"source_id": "source-a", "path": "orphan.md"},
                            {"source_id": "source-b", "path": "same/document.md"},
                        ],
                    )
                    # Simulate a missing target catalog row while vectors,
                    # clean, and state still retain the Source.
                    catalog.delete_source_documents("source-a")
                    result = source_delete.delete_source_data("source-a")
                    self.assertEqual(2, result["vector_records_deleted"])
                    self.assertEqual([], store.source_records("source-a"))
                    self.assertEqual(
                        ["b-catalog"],
                        [item["id"] for item in store.source_records("source-b")],
                    )
                    self.assertEqual([], catalog.source_chunk_ids("source-a"))
                    self.assertEqual(["b-catalog"], catalog.source_chunk_ids("source-b"))
                    remaining = [
                        json.loads(line)
                        for line in mixed.read_text(encoding="utf-8").splitlines()
                    ]
                    self.assertEqual(["b-catalog"], [item["id"] for item in remaining])
                    state = json.loads((logs / "index_state.json").read_text(encoding="utf-8"))
                    self.assertNotIn("a", state["files"])
                    self.assertIn("b", state["files"])
            finally:
                if client is not None:
                    system = client._system
                    del collection, client
                    gc.collect()
                    system.stop()
                    SharedSystemClient.clear_system_cache()


if __name__ == "__main__":
    unittest.main()
