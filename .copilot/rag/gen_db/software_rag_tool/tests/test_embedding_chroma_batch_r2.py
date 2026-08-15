from __future__ import annotations

import os
import inspect
import unittest
from unittest import mock

import numpy as np

from software_rag_tool import embeddings, store


def records(count: int) -> list[dict]:
    return [
        {
            "id": f"record-{index:03d}",
            "text": f"document {index}",
            "embedding_text": f"embedding {index}",
            "metadata": {"index": index, "path": f"docs/{index}.txt"},
        }
        for index in range(count)
    ]


class FixtureEmbedder:
    def __init__(self, *, dimension: int = 2, count_delta: int = 0) -> None:
        self.dimension = dimension
        self.count_delta = count_delta
        self.calls: list[list[str]] = []

    def encode(self, texts, *, mode):
        self.calls.append(list(texts))
        self.mode = mode
        count = max(0, len(texts) + self.count_delta)
        return [[float(index + 1)] * self.dimension for index in range(count)]


class FixtureCollection:
    def __init__(self, failure: BaseException | None = None, failure_write: int = 0) -> None:
        self.failure = failure
        self.failure_write = failure_write
        self.calls: list[dict] = []
        self.rows: dict[str, tuple[str, dict, list[float]]] = {}

    def upsert(self, *, ids, documents, embeddings, metadatas):
        write_number = len(self.calls) + 1
        if self.failure is not None and write_number == self.failure_write:
            raise self.failure
        call = {
            "ids": list(ids),
            "documents": list(documents),
            "embeddings": [list(value) for value in embeddings],
            "metadatas": [dict(value) for value in metadatas],
        }
        self.calls.append(call)
        for record_id, document, metadata, vector in zip(
            call["ids"], call["documents"], call["metadatas"], call["embeddings"], strict=True
        ):
            self.rows[record_id] = (document, metadata, vector)


class EmbeddingChromaBatchR2(unittest.TestCase):
    def _run(
        self,
        values: list[dict],
        *,
        inference: int = 8,
        write: int = 128,
        collection: FixtureCollection | None = None,
        embedder: FixtureEmbedder | None = None,
        progress: list[tuple[int, int]] | None = None,
    ) -> tuple[FixtureCollection, FixtureEmbedder, int]:
        collection = collection or FixtureCollection()
        embedder = embedder or FixtureEmbedder()
        with (
            mock.patch.dict(os.environ, {"EMBED_BATCH_SIZE": str(inference)}, clear=False),
            mock.patch.object(store, "_CHROMA_WRITE_BATCH_SIZE", write),
            mock.patch.object(store, "_get_or_create_collection", return_value=collection),
            mock.patch.object(store, "get_embedder", return_value=embedder),
            mock.patch.object(store, "embedding_fingerprint", return_value={"embedding_dimension": 2}),
        ):
            total = store.upsert_records(
                values,
                progress_callback=(
                    (lambda done, expected: progress.append((done, expected)))
                    if progress is not None else None
                ),
            )
        return collection, embedder, total

    def test_default_is_private_128_without_new_environment_contract(self) -> None:
        self.assertEqual(128, store._CHROMA_WRITE_BATCH_SIZE)
        self.assertNotIn("EMBED_WRITE_BATCH_SIZE", inspect.getsource(store.upsert_records))

    def test_inference_and_write_batches_preserve_order_and_final_partial(self) -> None:
        progress: list[tuple[int, int]] = []
        collection, embedder, total = self._run(records(259), progress=progress)
        self.assertEqual(259, total)
        self.assertEqual([8] * 32 + [3], [len(value) for value in embedder.calls])
        self.assertEqual([128, 128, 3], [len(value["ids"]) for value in collection.calls])
        self.assertEqual(
            [f"record-{index:03d}" for index in range(259)],
            [record_id for call in collection.calls for record_id in call["ids"]],
        )
        self.assertEqual([(128, 259), (256, 259), (259, 259)], progress)
        self.assertEqual("document", embedder.mode)

    def test_eight_by_eight_reference_shape_is_available_to_focused_tests(self) -> None:
        collection, _, total = self._run(records(17), write=8)
        self.assertEqual(17, total)
        self.assertEqual([8, 8, 1], [len(value["ids"]) for value in collection.calls])

    def test_count_and_dimension_mismatch_fail_before_any_write_or_progress(self) -> None:
        for embedder, message in (
            (FixtureEmbedder(count_delta=-1), "count mismatch"),
            (FixtureEmbedder(dimension=3), "dimension mismatch"),
        ):
            with self.subTest(message=message):
                collection = FixtureCollection()
                progress: list[tuple[int, int]] = []
                with self.assertRaisesRegex(RuntimeError, message):
                    self._run(
                        records(3), collection=collection, embedder=embedder, progress=progress
                    )
                self.assertEqual([], collection.calls)
                self.assertEqual([], progress)

    def test_failure_and_keyboard_interrupt_report_only_confirmed_writes_and_retry(self) -> None:
        for failure in (RuntimeError("write failed"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                collection = FixtureCollection(failure=failure, failure_write=2)
                progress: list[tuple[int, int]] = []
                with self.assertRaises(type(failure)):
                    self._run(records(259), collection=collection, progress=progress)
                self.assertEqual([(128, 259)], progress)
                self.assertEqual(128, len(collection.rows))
                collection.failure = None
                retry_progress: list[tuple[int, int]] = []
                _, _, total = self._run(
                    records(259), collection=collection, progress=retry_progress
                )
                self.assertEqual(259, total)
                self.assertEqual(259, len(collection.rows))
                self.assertEqual([(128, 259), (256, 259), (259, 259)], retry_progress)

    def test_manifest_is_published_only_after_complete_vector_and_catalog_success(self) -> None:
        values = records(3)
        common = (
            mock.patch.object(store, "require_index_tokenizer"),
            mock.patch.object(store, "load_records", return_value=values),
            mock.patch.object(store, "reset_collection"),
            mock.patch.object(store, "reset_catalog"),
            mock.patch.object(store, "collection_count", return_value=3),
            mock.patch.object(store, "upsert_catalog_records"),
            mock.patch.object(store, "write_manifest"),
        )
        with common[0], common[1], common[2], common[3], common[4], common[5] as catalog, common[6] as publish:
            with mock.patch.object(store, "upsert_records", side_effect=RuntimeError("Chroma failed")):
                with self.assertRaisesRegex(RuntimeError, "Chroma failed"):
                    store.build_index(reset=True)
            catalog.assert_not_called()
            publish.assert_not_called()
        with (
            mock.patch.object(store, "require_index_tokenizer"),
            mock.patch.object(store, "load_records", return_value=values),
            mock.patch.object(store, "reset_collection"),
            mock.patch.object(store, "reset_catalog"),
            mock.patch.object(store, "upsert_records", return_value=3),
            mock.patch.object(store, "upsert_catalog_records"),
            mock.patch.object(store, "collection_count", return_value=3),
            mock.patch.object(store, "write_manifest") as publish,
        ):
            self.assertEqual(3, store.build_index(reset=True))
            publish.assert_called_once_with(3)


class FixtureTokenizer:
    pad_token_id = 0

    def __init__(self, counts: list[int], *, include_mask: bool = True) -> None:
        self.counts = counts
        self.include_mask = include_mask
        self.calls: list[dict] = []

    def __call__(self, texts, **kwargs):
        values = list(texts)
        self.calls.append({"texts": values, **kwargs})
        width = max(self.counts, default=0)
        input_ids = np.zeros((len(values), width), dtype="int64")
        attention_mask = np.zeros((len(values), width), dtype="int64")
        for index, count in enumerate(self.counts):
            input_ids[index, :count] = np.arange(1, count + 1)
            attention_mask[index, :count] = 1
        result = {"input_ids": input_ids}
        if self.include_mask:
            result["attention_mask"] = attention_mask
        return result


class FixtureSession:
    def __init__(self, dimension: int = 3) -> None:
        self.dimension = dimension
        self.calls = 0

    def run(self, _outputs, inputs):
        self.calls += 1
        rows, width = inputs["input_ids"].shape
        return [np.ones((rows, width, self.dimension), dtype="float32")]


def onnx_embedder(tokenizer: FixtureTokenizer, *, max_length: int = 4):
    value = embeddings.OnnxRuntimeEmbedder.__new__(embeddings.OnnxRuntimeEmbedder)
    value._np = np
    value.document_prefix = "document: "
    value.query_prefix = "query: "
    value.max_length = max_length
    value._tokenizer = tokenizer
    value._session = FixtureSession()
    value._input_names = {"input_ids", "attention_mask"}
    value._output_names = ["last_hidden_state"]
    return value


class OnnxDocumentBatchContracts(unittest.TestCase):
    def test_document_tokenizes_once_for_unicode_and_mixed_lengths(self) -> None:
        tokenizer = FixtureTokenizer([1, 4, 2])
        embedder = onnx_embedder(tokenizer)
        vectors = embedder.encode(["", "日本語 résumé", "short"], mode="document")
        self.assertEqual(1, len(tokenizer.calls))
        self.assertFalse(tokenizer.calls[0]["truncation"])
        self.assertEqual([3, 3, 3], [len(vector) for vector in vectors])
        self.assertEqual(1, embedder._session.calls)

    def test_just_over_limit_fails_before_onnx_without_silent_truncation(self) -> None:
        tokenizer = FixtureTokenizer([4, 5])
        embedder = onnx_embedder(tokenizer)
        with self.assertRaisesRegex(
            embeddings.DocumentEmbeddingTokenLimitError, r"limit=4 index=1 tokens=5"
        ):
            embedder.encode(["under", "over"], mode="document")
        self.assertEqual(1, len(tokenizer.calls))
        self.assertEqual(0, embedder._session.calls)

    def test_extreme_document_length_fails_in_the_single_tokenizer_pass(self) -> None:
        tokenizer = FixtureTokenizer([10_000])
        embedder = onnx_embedder(tokenizer)
        with self.assertRaisesRegex(
            embeddings.DocumentEmbeddingTokenLimitError, r"limit=4 index=0 tokens=10000"
        ):
            embedder.encode(["極端長 " * 10_000], mode="document")
        self.assertEqual(1, len(tokenizer.calls))
        self.assertEqual(0, embedder._session.calls)

    def test_missing_attention_mask_uses_non_pad_input_ids(self) -> None:
        tokenizer = FixtureTokenizer([2, 5], include_mask=False)
        embedder = onnx_embedder(tokenizer)
        embedder._input_names = {"input_ids"}
        with self.assertRaisesRegex(
            embeddings.DocumentEmbeddingTokenLimitError, r"index=1 tokens=5"
        ):
            embedder.encode(["under", "over"], mode="document")

    def test_empty_batch_skips_tokenizer_and_onnx(self) -> None:
        tokenizer = FixtureTokenizer([])
        embedder = onnx_embedder(tokenizer)
        self.assertEqual([], embedder.encode([], mode="document"))
        self.assertEqual([], tokenizer.calls)
        self.assertEqual(0, embedder._session.calls)

    def test_query_path_keeps_bounded_truncation(self) -> None:
        tokenizer = FixtureTokenizer([9])
        embedder = onnx_embedder(tokenizer)
        self.assertEqual(1, len(embedder.encode(["query text"], mode="query")))
        self.assertTrue(tokenizer.calls[0]["truncation"])
        self.assertEqual(4, tokenizer.calls[0]["max_length"])


if __name__ == "__main__":
    unittest.main()
