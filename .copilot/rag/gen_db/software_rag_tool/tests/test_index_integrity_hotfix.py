from __future__ import annotations

import tempfile
import unittest
import gc
from pathlib import Path
from unittest import mock

from software_rag_tool import incremental, store
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


if __name__ == "__main__":
    unittest.main()
