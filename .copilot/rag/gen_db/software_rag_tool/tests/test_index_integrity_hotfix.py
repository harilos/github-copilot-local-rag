from __future__ import annotations

import gc
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool import (
    catalog,
    db_runtime,
    incremental,
    manifest,
    source_delete,
    store,
    tokenize,
)
from software_rag_tool.embeddings import DocumentTokenBudget
from software_rag_tool.tokenize import (
    TokenizerFingerprintError,
    TokenizerUnavailableError,
    tokenize_for_fts,
    tokenizer_fingerprint,
    tokens_for_fts,
)


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
    @staticmethod
    def _vector_record(record_id: str, text: str) -> dict:
        return {
            "id": record_id,
            "text": text,
            "embedding_text": text,
            "metadata": {
                "doc_id": record_id,
                "path": f"{record_id}.txt",
                "source_id": "fixture-source",
            },
        }

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

    def test_upsert_recreates_collection_after_successful_reset(self) -> None:
        from chromadb.api.client import SharedSystemClient

        class FixtureEmbedder:
            def encode(self, texts, *, mode):
                self.mode = mode
                return [[float(index + 1), 1.0] for index, _ in enumerate(texts)]

        temporary = tempfile.TemporaryDirectory()
        try:
            cdir = Path(temporary.name) / "chroma"
            environment = {
                "CHROMA_DIR_V2": str(cdir),
                "CHROMA_COLLECTION": "fresh_upsert_fixture",
            }
            records = [
                self._vector_record("new-a", "new alpha"),
                self._vector_record("new-b", "new beta"),
            ]
            embedder = FixtureEmbedder()
            with (
                mock.patch.dict(os.environ, environment),
                mock.patch.object(store, "get_embedder", return_value=embedder),
                mock.patch.object(
                    store,
                    "embedding_fingerprint",
                    return_value={
                        "embedding_model": "fixture-model",
                        "embedding_dimension": 2,
                    },
                ),
            ):
                store.reset_collection()
                self.assertEqual(2, store.upsert_records(records))
                client = store._persistent_client(cdir)
                collection = client.get_collection("fresh_upsert_fixture")
                self.assertIsNotNone(collection)
                self.assertEqual(["new-a", "new-b"], sorted(collection.get()["ids"]))
                self.assertEqual("document", embedder.mode)
                system = client._system
                del collection, client
                gc.collect()
                system.stop()
        finally:
            SharedSystemClient.clear_system_cache()
            temporary.cleanup()

    def test_build_index_reset_replaces_old_collection_with_clean_records(self) -> None:
        import chromadb
        from chromadb.api.client import SharedSystemClient
        from chromadb.config import Settings

        class FixtureEmbedder:
            def encode(self, texts, *, mode):
                del mode
                return [[float(index + 1), 1.0] for index, _ in enumerate(texts)]

        temporary = tempfile.TemporaryDirectory()
        try:
            output_root = Path(temporary.name) / "fixture-rag"
            clean = output_root / "data" / "clean" / "records"
            clean.mkdir(parents=True)
            records = [
                self._vector_record("clean-a", "clean alpha"),
                self._vector_record("clean-b", "clean beta"),
            ]
            (clean / "fixture.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            cdir = output_root / "index" / "chroma"
            cdir.mkdir(parents=True)
            environment = {
                "RAG_OUTPUT_ROOT": str(output_root),
                "CHROMA_DIR_V2": str(cdir),
                "CHROMA_COLLECTION": "build_reset_fixture",
            }
            with mock.patch.dict(os.environ, environment):
                client = chromadb.PersistentClient(
                    path=str(cdir),
                    settings=Settings(anonymized_telemetry=False),
                )
                old = client.get_or_create_collection("build_reset_fixture")
                old.add(
                    ids=["old"],
                    documents=["old"],
                    embeddings=[[1.0, 0.0]],
                    metadatas=[{"source_id": "old"}],
                )
                with (
                    mock.patch.object(store, "require_index_tokenizer"),
                    mock.patch.object(store, "get_embedder", return_value=FixtureEmbedder()),
                    mock.patch.object(
                        store,
                        "embedding_fingerprint",
                        return_value={
                            "embedding_model": "fixture-model",
                            "embedding_dimension": 2,
                        },
                    ),
                    mock.patch.object(store, "write_manifest"),
                ):
                    self.assertEqual(2, store.build_index(reset=True))
                replacement = client.get_collection("build_reset_fixture")
                self.assertEqual(["clean-a", "clean-b"], sorted(replacement.get()["ids"]))
                self.assertNotIn("old", replacement.get()["ids"])
                system = client._system
                del replacement, old, client
                gc.collect()
                system.stop()
        finally:
            SharedSystemClient.clear_system_cache()
            temporary.cleanup()

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


class Bm25TermFrequencyContracts(unittest.TestCase):
    @staticmethod
    def _record(record_id: str, text: str) -> dict:
        return {
            "id": record_id,
            "text": text,
            "metadata": {
                "doc_id": record_id,
                "path": f"{record_id}.txt",
                "source_id": "fixture-source",
            },
        }

    def test_query_tokens_remain_unique_while_index_tokens_keep_occurrences(self) -> None:
        text = "alpha alpha beta alpha"
        alpha = tokens_for_fts("alpha")[0]
        beta = tokens_for_fts("beta")[0]
        self.assertEqual([alpha, beta], tokens_for_fts(text))
        self.assertEqual(
            [alpha, alpha, beta, alpha],
            tokens_for_fts(text, preserve_occurrences=True),
        )
        self.assertEqual(
            " ".join([alpha, alpha, beta]),
            tokenize_for_fts(
                text,
                max_tokens=3,
                preserve_occurrences=True,
            ),
        )

    def test_catalog_index_stores_repeated_term_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": temporary}):
                catalog.upsert_records(
                    [self._record("repeat", "alpha alpha alpha beta")]
                )
                with catalog.connect_readonly(catalog.catalog_path()) as connection:
                    stored = connection.execute(
                        "SELECT body_tokens FROM fts_word"
                    ).fetchone()[0]
            alpha = tokens_for_fts("alpha")[0]
            beta = tokens_for_fts("beta")[0]
            self.assertEqual(" ".join([alpha, alpha, alpha, beta]), stored)

    def test_repeated_term_outranks_equal_length_single_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": temporary}):
                catalog.upsert_records(
                    [
                        self._record(
                            "repeat",
                            "alpha alpha alpha beta gamma delta epsilon",
                        ),
                        self._record(
                            "single",
                            "alpha zeta eta beta gamma delta epsilon",
                        ),
                    ]
                )
                rows = catalog.bm25_search("alpha", top_k=2)
            self.assertEqual(["repeat", "single"], [row["id"] for row in rows])
            self.assertLess(rows[0]["score"], rows[1]["score"])

    def test_manifest_and_catalog_share_term_frequency_fingerprint(self) -> None:
        fingerprint = tokenizer_fingerprint()
        self.assertIn("-v3-tf", fingerprint)
        self.assertEqual(fingerprint, manifest.build_manifest(0)["tokenizer"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.sqlite"
            with catalog.connect(path):
                pass
            self.assertEqual(fingerprint, catalog.counts(path)["tokenizer"])


class TokenizerRuntimeIntegrityContracts(unittest.TestCase):
    def tearDown(self) -> None:
        tokenize._sudachi.cache_clear()

    def test_sudachi_import_dictionary_and_initialization_fail_closed_before_writes(
        self,
    ) -> None:
        failures = (
            ImportError("private import details"),
            FileNotFoundError("private dictionary path"),
            RuntimeError("private initialization details"),
        )
        for failure in failures:
            with self.subTest(error=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "input"
                root.mkdir()
                (root / "document.txt").write_text("body", encoding="utf-8")
                tokenize._sudachi.cache_clear()
                with (
                    mock.patch.dict(
                        os.environ,
                        {tokenize.TOKENIZER_MODE_ENV: "sudachi"},
                    ),
                    mock.patch.object(
                        tokenize,
                        "_load_sudachi",
                        side_effect=failure,
                    ),
                    mock.patch.object(incremental, "reset_collection") as reset_vectors,
                    mock.patch.object(incremental, "reset_catalog") as reset_catalog,
                    mock.patch.object(incremental, "_save_state") as save_state,
                    mock.patch.object(incremental, "write_progress") as progress,
                    self.assertRaisesRegex(
                        TokenizerUnavailableError,
                        "sudachi_tokenizer_unavailable",
                    ),
                ):
                    incremental.add_or_update_root(
                        root=root,
                        source_id="fixture",
                        reset_db=True,
                        reset_clean=True,
                        document_token_budget=test_budget(),
                    )
                reset_vectors.assert_not_called()
                reset_catalog.assert_not_called()
                save_state.assert_not_called()
                progress.assert_not_called()

    def test_direct_catalog_build_failure_does_not_create_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tokenize._sudachi.cache_clear()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "RAG_OUTPUT_ROOT": temporary,
                        tokenize.TOKENIZER_MODE_ENV: "sudachi",
                    },
                ),
                mock.patch.object(
                    tokenize,
                    "_load_sudachi",
                    side_effect=RuntimeError("private initialization details"),
                ),
                self.assertRaises(TokenizerUnavailableError),
            ):
                catalog.upsert_records(
                    [Bm25TermFrequencyContracts._record("record", "body")]
                )
            self.assertFalse((Path(temporary) / "catalog.sqlite").exists())

    def test_explicit_fallback_build_and_runtime_match_only_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chroma = root / "index" / "chroma"
            chroma.mkdir(parents=True)
            with mock.patch.dict(
                os.environ,
                {
                    "RAG_OUTPUT_ROOT": str(root),
                    tokenize.TOKENIZER_MODE_ENV: "fallback",
                },
            ):
                catalog.upsert_records(
                    [Bm25TermFrequencyContracts._record("record", "alpha beta")]
                )
                fallback_fingerprint = tokenizer_fingerprint()
                context = db_runtime.DbContext(
                    name="fixture-rag",
                    root=root,
                    catalog_path=root / "catalog.sqlite",
                    chroma_dir=chroma,
                    collection_name="fixture_collection",
                    db_config={},
                    version={},
                    manifest={"tokenizer": fallback_fingerprint},
                    profile_hint="",
                    embedding_fingerprint={},
                )
                runtime_store = db_runtime.DbStore(context)
                self.assertEqual(
                    ["record"],
                    [
                        row["id"]
                        for row in runtime_store.bm25_search(
                            "alpha",
                            top_k=1,
                        )
                    ],
                )
            with (
                mock.patch.dict(
                    os.environ,
                    {tokenize.TOKENIZER_MODE_ENV: "sudachi"},
                ),
                self.assertRaises(TokenizerFingerprintError),
            ):
                runtime_store.bm25_search("alpha", top_k=1)
            runtime_store.close()

    def test_existing_index_mismatch_fails_before_incremental_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "db"
            input_root = root / "input"
            input_root.mkdir()
            (input_root / "document.txt").write_text("body", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "RAG_OUTPUT_ROOT": str(output),
                    tokenize.TOKENIZER_MODE_ENV: "fallback",
                },
            ):
                catalog.upsert_records(
                    [Bm25TermFrequencyContracts._record("old", "alpha beta")]
                )
                manifest.write_manifest(1)
            tokenize._sudachi.cache_clear()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "RAG_OUTPUT_ROOT": str(output),
                        tokenize.TOKENIZER_MODE_ENV: "sudachi",
                    },
                ),
                mock.patch.object(incremental, "_load_state") as load_state,
                mock.patch.object(incremental, "delete_ids") as delete_vectors,
                mock.patch.object(incremental, "upsert_records") as upsert_vectors,
                mock.patch.object(incremental, "_save_state") as save_state,
                self.assertRaises(TokenizerFingerprintError),
            ):
                incremental.add_or_update_root(
                    root=input_root,
                    source_id="fixture",
                    document_token_budget=test_budget(),
                )
            load_state.assert_not_called()
            delete_vectors.assert_not_called()
            upsert_vectors.assert_not_called()
            save_state.assert_not_called()

    def test_direct_catalog_mismatch_does_not_relabel_or_mix_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict(
                os.environ,
                {
                    "RAG_OUTPUT_ROOT": str(root),
                    tokenize.TOKENIZER_MODE_ENV: "fallback",
                },
            ):
                catalog.upsert_records(
                    [Bm25TermFrequencyContracts._record("old", "alpha beta")]
                )
                fallback_fingerprint = tokenizer_fingerprint()
            tokenize._sudachi.cache_clear()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "RAG_OUTPUT_ROOT": str(root),
                        tokenize.TOKENIZER_MODE_ENV: "sudachi",
                    },
                ),
                self.assertRaises(TokenizerFingerprintError),
            ):
                catalog.upsert_records(
                    [Bm25TermFrequencyContracts._record("new", "alpha beta")]
                )
            with catalog.connect_readonly(root / "catalog.sqlite") as connection:
                ids = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT chunk_uid FROM chunk ORDER BY chunk_uid"
                    )
                ]
                stored_fingerprint = str(
                    connection.execute(
                        "SELECT value FROM database_meta WHERE key = 'tokenizer'"
                    ).fetchone()[0]
                )
            self.assertEqual(["old"], ids)
            self.assertEqual(fallback_fingerprint, stored_fingerprint)

    def test_sudachi_fingerprint_identifies_offline_dictionary_and_settings(self) -> None:
        tokenize._sudachi.cache_clear()
        with mock.patch.dict(
            os.environ,
            {tokenize.TOKENIZER_MODE_ENV: "sudachi"},
        ):
            descriptor = tokenize.tokenizer_runtime_descriptor()
        self.assertEqual("sudachi", descriptor["mode"])
        self.assertEqual("sudachipy", descriptor["implementation"])
        self.assertEqual("A", descriptor["split_mode"])
        self.assertNotEqual("unknown", descriptor["implementation_version"])
        self.assertNotEqual("custom", descriptor["dictionary"])
        self.assertNotEqual("unknown", descriptor["dictionary_version"])


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
