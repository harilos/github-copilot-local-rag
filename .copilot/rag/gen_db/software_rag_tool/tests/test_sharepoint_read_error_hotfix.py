from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool import incremental
from software_rag_tool.embeddings import DocumentTokenBudget
from software_rag_tool.ingestion_paths import resolve_ingestion_scope
from software_rag_tool.records import iter_input_files


class _CharacterTokenizer:
    def __call__(self, text, **_kwargs):
        values = [text] if isinstance(text, str) else list(text)
        encoded = [[1, *range(2, len(value) + 2), 2] for value in values]
        return {"input_ids": encoded[0] if isinstance(text, str) else encoded}


TOKEN_BUDGET = DocumentTokenBudget(
    tokenizer=_CharacterTokenizer(),
    document_prefix="document: ",
    tokenizer_name="sharepoint-hotfix-test-double",
    target_tokens=320,
    max_tokens=384,
)


class SharePointReadErrorHotfixTests(unittest.TestCase):
    def test_office_owner_file_is_excluded_but_dollar_name_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "~$sample.docx").write_bytes(b"locked")
            (root / "budget$2026.docx").write_bytes(b"normal")
            (root / "guide.md").write_text("guide", encoding="utf-8")
            names = [path.name for path in iter_input_files(root)]
        self.assertEqual(["budget$2026.docx", "guide.md"], names)

    def test_hash_permission_error_is_relative_and_preserves_previous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_parent = Path(temporary) / "Secret User"
            root = secret_parent / "Shared Documents"
            root.mkdir(parents=True)
            path = root / "private.docx"
            path.write_bytes(b"new")
            scope = resolve_ingestion_scope(root)
            rel = scope.file(path).stored_path
            previous = {
                "source_id": "src",
                "stored_path": rel,
                "resolved_root": "sha256:old",
                "content_hash": "old-hash",
                "chunker_config": {"version": "old"},
                "record_ids": ["old-vector"],
                "record_count": 1,
                "status": "indexed",
            }
            state = {"files": {f"src:{rel}": previous}}
            denied = PermissionError(13, "denied", str(path))
            with mock.patch.object(
                incremental,
                "file_content_hash",
                side_effect=denied,
            ):
                item = incremental._prepare_file(
                    scope,
                    path,
                    "src",
                    state,
                    retry_errors=False,
                    document_token_budget=TOKEN_BUDGET,
                    current_chunker_config={"version": "new"},
                    persistent_root_identity="sha256:root",
                )
            incremental._record_error(state, item)
            saved = state["files"][f"src:{rel}"]
            serialized = json.dumps(
                {"item": item, "state": saved},
                ensure_ascii=False,
            )
            self.assertEqual("error", item["status"])
            self.assertEqual("input_read", item["error_kind"])
            self.assertEqual("hash-read", item["diagnostic"]["stage"])
            self.assertEqual(13, item["diagnostic"]["errno"])
            self.assertEqual(["old-vector"], saved["record_ids"])
            self.assertEqual("old-hash", saved["content_hash"])
            self.assertNotIn(str(secret_parent), serialized)
            self.assertNotIn("Secret User", serialized)

    def test_windows_sharing_violation_keeps_winerror_without_raw_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "SharePoint"
            root.mkdir()
            path = root / "locked.xlsx"
            path.write_bytes(b"locked")
            scope = resolve_ingestion_scope(root)
            failure = OSError(13, "sharing violation", str(path))
            failure.winerror = 32
            with mock.patch.object(
                incremental,
                "file_content_hash",
                side_effect=failure,
            ):
                item = incremental._prepare_file(
                    scope,
                    path,
                    "src",
                    {"files": {}},
                    retry_errors=True,
                    document_token_budget=TOKEN_BUDGET,
                    current_chunker_config={},
                    persistent_root_identity="sha256:root",
                )
            self.assertEqual(32, item["diagnostic"]["winerror"])
            self.assertEqual(13, item["diagnostic"]["errno"])
            self.assertNotIn(str(root), json.dumps(item, ensure_ascii=False))

    def test_extract_open_oserror_is_retryable_but_parser_error_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "fixture.docx"
            path.write_bytes(b"fixture")
            scope = resolve_ingestion_scope(root)
            for exception, expected_kind in (
                (OSError(121, "cloud hydration failed", str(path)), "input_read"),
                (ValueError("corrupt package"), "extract"),
            ):
                with (
                    self.subTest(exception=type(exception).__name__),
                    mock.patch.object(
                        incremental,
                        "file_content_hash",
                        return_value="hash",
                    ),
                    mock.patch.object(
                        incremental,
                        "build_records_for_file",
                        side_effect=exception,
                    ),
                ):
                    item = incremental._prepare_file(
                        scope,
                        path,
                        "src",
                        {"files": {}},
                        retry_errors=True,
                        document_token_budget=TOKEN_BUDGET,
                        current_chunker_config={},
                        persistent_root_identity="sha256:root",
                    )
                    self.assertEqual(expected_kind, item["error_kind"])
                    self.assertEqual(
                        expected_kind == "input_read",
                        item["retryable"],
                    )

    def test_extractor_workspace_oserror_is_not_a_partial_input_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            workspace = Path(temporary) / "extractor-work"
            root.mkdir()
            path = root / "fixture.docx"
            path.write_bytes(b"fixture")
            scope = resolve_ingestion_scope(root)
            exception = PermissionError(13, "denied", str(workspace))
            with (
                mock.patch.object(
                    incremental, "file_content_hash", return_value="hash"
                ),
                mock.patch.object(
                    incremental,
                    "build_records_for_file",
                    side_effect=exception,
                ),
            ):
                item = incremental._prepare_file(
                    scope,
                    path,
                    "src",
                    {"files": {}},
                    retry_errors=True,
                    document_token_budget=TOKEN_BUDGET,
                    current_chunker_config={},
                    persistent_root_identity="sha256:root",
                )
        self.assertEqual("extract", item["error_kind"])
        self.assertEqual("extract", item["diagnostic"]["stage"])
        self.assertFalse(item["retryable"])
        self.assertNotIn(str(workspace), json.dumps(item, ensure_ascii=False))

    def test_result_classification_separates_success_partial_and_failure(self) -> None:
        base = {"indexed_files": 1, "skipped_files": 0}
        self.assertEqual(
            "success",
            incremental._result_status(
                {**base, "error_files": 0, "extract_error_files": 0}
            ),
        )
        self.assertEqual(
            "partial",
            incremental._result_status(
                {**base, "error_files": 1, "extract_error_files": 0}
            ),
        )
        self.assertEqual(
            "failure",
            incremental._result_status(
                {
                    "indexed_files": 0,
                    "skipped_files": 0,
                    "error_files": 1,
                    "extract_error_files": 0,
                }
            ),
        )
        self.assertEqual(
            "failure",
            incremental._result_status(
                {**base, "error_files": 1, "extract_error_files": 1}
            ),
        )

    def test_source_file_is_read_only_during_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "read-only-source.txt"
            original = b"source data\n"
            path.write_bytes(original)
            before = path.stat()
            item = incremental._prepare_file(
                resolve_ingestion_scope(root),
                path,
                "src",
                {"files": {}},
                retry_errors=True,
                document_token_budget=TOKEN_BUDGET,
                current_chunker_config={},
                persistent_root_identity="sha256:root",
            )
            after = path.stat()
            after_bytes = path.read_bytes()
        self.assertEqual("ready", item["status"])
        self.assertEqual(original, after_bytes)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)

    def test_partial_keeps_old_ids_then_retry_replaces_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source_root = workspace / "Shared Documents"
            output_root = workspace / "fixture-rag"
            source_root.mkdir()
            a = source_root / "a.txt"
            b = source_root / "b.txt"
            c = source_root / "c.txt"
            a.write_text("alpha-v1", encoding="utf-8")
            b.write_text("beta-v1", encoding="utf-8")
            vectors: dict[str, dict] = {}
            catalog: dict[str, dict] = {}

            def build_records(_root, path, *, content_hash, **_kwargs):
                record_id = f"{Path(path).name}:{content_hash[:12]}"
                return [{"id": record_id, "text": Path(path).name}]

            def delete_ids(record_ids):
                deleted = 0
                for record_id in record_ids:
                    if vectors.pop(str(record_id), None) is not None:
                        deleted += 1
                return deleted

            def delete_catalog(record_ids):
                for record_id in record_ids:
                    catalog.pop(str(record_id), None)

            def upsert(records, progress_callback=None):
                for index, record in enumerate(records, start=1):
                    vectors[str(record["id"])] = dict(record)
                    if progress_callback is not None:
                        progress_callback(index, len(records))
                return len(records)

            def upsert_catalog(records):
                for record in records:
                    catalog[str(record["id"])] = dict(record)

            patches = (
                mock.patch.dict(
                    os.environ,
                    {"RAG_OUTPUT_ROOT": str(output_root)},
                    clear=False,
                ),
                mock.patch.object(incremental, "require_index_tokenizer"),
                mock.patch.object(
                    incremental, "validate_existing_index_tokenizer"
                ),
                mock.patch.object(
                    incremental,
                    "build_records_for_file",
                    side_effect=build_records,
                ),
                mock.patch.object(
                    incremental, "delete_ids", side_effect=delete_ids
                ),
                mock.patch.object(
                    incremental,
                    "delete_catalog_chunks",
                    side_effect=delete_catalog,
                ),
                mock.patch.object(
                    incremental, "upsert_records", side_effect=upsert
                ),
                mock.patch.object(
                    incremental,
                    "upsert_catalog_records",
                    side_effect=upsert_catalog,
                ),
                mock.patch.object(
                    incremental,
                    "collection_count",
                    side_effect=lambda: len(vectors),
                ),
                mock.patch.object(incremental, "write_manifest"),
                mock.patch.object(
                    incremental, "update_profile_from_clean", return_value=False
                ),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
                first = incremental.add_or_update_root(
                    source_root,
                    "src_sharepoint",
                    document_token_budget=TOKEN_BUDGET,
                    privacy_safe_root=True,
                )
                first_a = next(value for value in vectors if value.startswith("a.txt:"))

                b.write_text("beta-v2", encoding="utf-8")
                c.write_text("gamma-v1", encoding="utf-8")
                real_hash = incremental.file_content_hash

                def fail_a_and_c(path: Path) -> str:
                    if Path(path).name in {"a.txt", "c.txt"}:
                        raise PermissionError(13, "denied", str(path))
                    return real_hash(Path(path))

                with mock.patch.object(
                    incremental,
                    "file_content_hash",
                    side_effect=fail_a_and_c,
                ):
                    second = incremental.add_or_update_root(
                        source_root,
                        "src_sharepoint",
                        document_token_budget=TOKEN_BUDGET,
                        privacy_safe_root=True,
                    )
                second_ids = set(vectors)
                second_state = json.loads(
                    (output_root / "logs" / "index_state.json").read_text(
                        encoding="utf-8"
                    )
                )
                second_c_state = second_state["files"][
                    f"src_sharepoint:{source_root.name}/c.txt"
                ]

                a.write_text("alpha-v2", encoding="utf-8")
                third = incremental.add_or_update_root(
                    source_root,
                    "src_sharepoint",
                    document_token_budget=TOKEN_BUDGET,
                    privacy_safe_root=True,
                )

            self.assertEqual("success", first["result_status"])
            self.assertEqual("partial", second["result_status"])
            self.assertEqual(2, second["input_error_files"])
            self.assertIn(first_a, second_ids)
            self.assertFalse(any(value.startswith("c.txt:") for value in second_ids))
            self.assertEqual("error", second_c_state["status"])
            self.assertEqual([], second_c_state["record_ids"])
            self.assertEqual("success", third["result_status"])
            self.assertEqual(3, len(vectors))
            self.assertEqual(set(vectors), set(catalog))
            self.assertNotIn(first_a, vectors)
            self.assertEqual(
                1,
                sum(record_id.startswith("a.txt:") for record_id in vectors),
            )
            self.assertEqual(
                1,
                sum(record_id.startswith("c.txt:") for record_id in vectors),
            )

            state = json.loads(
                (output_root / "logs" / "index_state.json").read_text(
                    encoding="utf-8"
                )
            )
            a_state = state["files"][
                f"src_sharepoint:{source_root.name}/a.txt"
            ]
            self.assertEqual("indexed", a_state["status"])
            self.assertEqual(1, len(a_state["record_ids"]))
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (output_root / "logs").iterdir()
                if path.suffix in {".json", ".jsonl"}
            )
            self.assertNotIn(str(source_root), persisted)

    def test_database_write_permission_error_remains_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            output = Path(temporary) / "db"
            root.mkdir()
            (root / "ok.txt").write_text("ok", encoding="utf-8")
            record = {"id": "ok", "text": "ok"}
            with (
                mock.patch.dict(
                    os.environ, {"RAG_OUTPUT_ROOT": str(output)}, clear=False
                ),
                mock.patch.object(incremental, "require_index_tokenizer"),
                mock.patch.object(
                    incremental, "validate_existing_index_tokenizer"
                ),
                mock.patch.object(
                    incremental,
                    "build_records_for_file",
                    return_value=[record],
                ),
                mock.patch.object(incremental, "delete_ids", return_value=0),
                mock.patch.object(incremental, "delete_catalog_chunks"),
                mock.patch.object(
                    incremental,
                    "upsert_records",
                    side_effect=PermissionError(13, "catalog locked"),
                ),
                self.assertRaises(PermissionError),
            ):
                incremental.add_or_update_root(
                    root,
                    "src",
                    document_token_budget=TOKEN_BUDGET,
                )

    def test_disappeared_candidate_is_partial_and_other_file_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            missing = root / "missing.txt"
            good = root / "good.txt"
            good.write_text("good", encoding="utf-8")
            scope = resolve_ingestion_scope(root)
            item = incremental._prepare_file(
                scope,
                missing,
                "src",
                {"files": {}},
                retry_errors=True,
                document_token_budget=TOKEN_BUDGET,
                current_chunker_config={},
                persistent_root_identity="sha256:root",
            )
            self.assertEqual("input_read", item["error_kind"])
            self.assertEqual("enumerate", item["diagnostic"]["stage"])
            self.assertEqual("missing.txt", Path(item["rel"]).name)


if __name__ == "__main__":
    unittest.main()
