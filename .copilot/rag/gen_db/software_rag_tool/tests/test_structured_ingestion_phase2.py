from __future__ import annotations

import codecs
from concurrent.futures import TimeoutError as FutureTimeoutError
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool import incremental
from software_rag_tool.embeddings import DocumentTokenBudget
from software_rag_tool.extractors import sections_from_extraction
from software_rag_tool.ingestion_paths import resolve_ingestion_scope
from software_rag_tool.ingestion_workers import (
    DoclingExtractionPool,
    choose_worker_plan,
)
from software_rag_tool.manifest import (
    ConfigMismatchError,
    validate_embedding_manifest,
)
from software_rag_tool.pipeline import build_pipeline_contract
from software_rag_tool.records import FileBuildResult
from software_rag_tool.retrieval import (
    _attach_structure_context,
    _pack_protected_rows,
    hybrid_query,
)
from software_rag_tool.structured_extraction import (
    ExtractionResult,
    StructureBlock,
    decode_plain_text,
    docling_pdf_artifacts_identity,
    extract_docling_tree,
    extract_document_structure,
    extract_native_markdown,
    extract_plain_text,
    extract_xlsx_structure,
)


class _CharacterTokenizer:
    def __call__(self, text, *, add_special_tokens=True, **_kwargs):
        values = list(range(len(text)))
        if add_special_tokens:
            values = [1, *values, 2]
        return {"input_ids": values}


TOKEN_BUDGET = DocumentTokenBudget(
    tokenizer=_CharacterTokenizer(),
    document_prefix="doc: ",
    tokenizer_name="phase2-character-fixture",
    target_tokens=80,
    max_tokens=96,
)


class NativeMarkdownStructureTests(unittest.TestCase):
    def test_front_matter_headings_fence_and_indent_are_preserved(self) -> None:
        source = """---
owner: rag
---
# 第一章

本文です。

```python
  # fence内は見出しではない
  value = 1
```

第二節
------

  - indented item
\t- tab item
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.md"
            path.write_text(source, encoding="utf-8")
            result = extract_native_markdown(path)

        self.assertEqual("indexed", result.status)
        self.assertEqual("native_markdown", result.structure_origin)
        self.assertEqual("yaml", result.blocks[0].kind)
        titles = [block.title for block in result.blocks]
        self.assertIn("第一章", titles)
        self.assertIn("第一章 > 第二節", titles)
        self.assertFalse(any("fence内" in title for title in titles))
        body = "\n".join(block.text for block in result.blocks)
        self.assertIn("  value = 1", body)
        self.assertIn("\t- tab item", body)

        sections = sections_from_extraction(
            result,
            chunk_max_chars=160,
            chunk_overlap=20,
            token_budget=TOKEN_BUDGET,
            embedding_path="fixture.md",
        )
        self.assertTrue(sections)
        self.assertTrue(all(section.structure_id for section in sections))
        self.assertIn("  value = 1", "\n".join(s.text for s in sections))
        self.assertTrue(
            all(
                TOKEN_BUDGET.count_document(
                    "fixture.md", section.title, section.text
                )
                <= TOKEN_BUDGET.max_tokens
                for section in sections
            )
        )

    def test_unclosed_fence_keeps_tail_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unclosed.md"
            path.write_text(
                "# Heading\n```text\n# literal\nTAIL-ANCHOR-991\n",
                encoding="utf-8",
            )
            result = extract_native_markdown(path)
        self.assertIn(
            "TAIL-ANCHOR-991",
            "\n".join(block.text for block in result.blocks),
        )

    def test_fence_like_code_does_not_close_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fence.md"
            path.write_text(
                "# Heading\n```text\n`literal-token\n# not-a-heading\n```\n",
                encoding="utf-8",
            )
            result = extract_native_markdown(path)
        self.assertEqual(1, len(result.blocks))
        self.assertEqual("code", result.blocks[0].kind)
        self.assertIn("# not-a-heading", result.blocks[0].text)

    def test_horizontal_rules_are_not_misclassified_as_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rules.md"
            path.write_text(
                "---\nordinary paragraph\n\n---\n# Real heading\nbody\n",
                encoding="utf-8",
            )
            result = extract_native_markdown(path)
        self.assertFalse(any(block.kind == "yaml" for block in result.blocks))
        self.assertIn(
            "ordinary paragraph",
            "\n".join(
                f"{block.title}\n{block.text}" for block in result.blocks
            ),
        )


class EncodingAndStatusTests(unittest.TestCase):
    def test_deterministic_encoding_policy(self) -> None:
        cases = (
            ("UTF8".encode("utf-8"), "utf-8"),
            (codecs.BOM_UTF8 + "BOM".encode("utf-8"), "utf-8-sig"),
            ("UTF16".encode("utf-16"), "utf-16"),
            ("日本語".encode("cp932"), "cp932"),
        )
        for payload, expected_encoding in cases:
            with self.subTest(expected_encoding=expected_encoding):
                text, encoding, _reason = decode_plain_text(payload)
                self.assertTrue(text)
                self.assertEqual(expected_encoding, encoding)
                self.assertNotIn("\ufffd", text)

    def test_invalid_bytes_and_empty_file_are_not_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            invalid = Path(temporary) / "invalid.txt"
            invalid.write_bytes(b"\x81")
            empty = Path(temporary) / "empty.txt"
            empty.write_bytes(b"")
            invalid_result = extract_plain_text(invalid)
            empty_result = extract_plain_text(empty)
        self.assertEqual("extract_error", invalid_result.status)
        self.assertEqual("zero_text", empty_result.status)
        self.assertTrue(invalid_result.retryable)
        self.assertTrue(empty_result.retryable)

    def test_zero_text_pdf_is_explicit(self) -> None:
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with path.open("wb") as output:
                writer.write(output)
            result = extract_document_structure(path, backend_policy="legacy")
        self.assertEqual("zero_text", result.status)
        self.assertFalse(result.is_indexed)

    def test_auto_pdf_without_artifacts_never_calls_docling(self) -> None:
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with path.open("wb") as output:
                writer.write(output)
            with (
                mock.patch.dict(
                    os.environ,
                    {"RAG_DOCLING_ARTIFACTS_PATH": ""},
                    clear=False,
                ),
                mock.patch(
                    "software_rag_tool.structured_extraction.docling_available",
                    return_value=True,
                ),
                mock.patch(
                    "software_rag_tool.structured_extraction.extract_docling_tree",
                ) as docling,
            ):
                result = extract_document_structure(path, backend_policy="auto")
        docling.assert_not_called()
        self.assertEqual("zero_text", result.status)
        self.assertEqual(
            "docling_pdf_artifacts_unavailable_offline",
            result.fallback_reason,
        )

    def test_structured_markdown_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.md"
            path.write_text("# Heading\n\nbody\n", encoding="utf-8")
            default_result = extract_document_structure(path)
            opted_in_result = extract_document_structure(
                path,
                backend_policy="auto",
            )
        self.assertEqual("legacy_flat", default_result.structure_origin)
        self.assertEqual(1, len(default_result.blocks))
        self.assertEqual("native_markdown", opted_in_result.structure_origin)

    def test_pdf_artifact_identity_changes_with_local_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "model.bin"
            artifact.write_bytes(b"first")
            with mock.patch.dict(
                os.environ,
                {"RAG_DOCLING_ARTIFACTS_PATH": str(root)},
                clear=False,
            ):
                first = docling_pdf_artifacts_identity()
                artifact.write_bytes(b"other")
                second = docling_pdf_artifacts_identity()
        self.assertTrue(first.startswith("sha256:"))
        self.assertNotEqual(first, second)


class XlsxStructureTests(unittest.TestCase):
    def test_sheet_regions_and_missing_formula_cache_are_diagnosed(self) -> None:
        from openpyxl import Workbook

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "table.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "品質表"
            sheet.append(["項目", "値"])
            sheet.append(["alpha", 3])
            sheet.append([])
            sheet.append(["計算", "結果"])
            sheet.append(["sum", "=SUM(B2:B2)"])
            workbook.save(path)
            workbook.close()
            result = extract_xlsx_structure(path)

        self.assertEqual("extract_error", result.status)
        self.assertTrue(result.retryable)
        self.assertFalse(result.is_indexed)
        self.assertEqual(2, len(result.blocks))
        self.assertTrue(all(block.sheet == "品質表" for block in result.blocks))
        self.assertEqual(1, result.diagnostics["formula_cache_missing"])
        self.assertIn("=SUM(B2:B2)", "\n".join(b.text for b in result.blocks))


class DoclingTreeContractTests(unittest.TestCase):
    def test_document_tree_is_consumed_without_markdown_roundtrip(self) -> None:
        paragraph = types.SimpleNamespace(
            text="本文の根拠",
            label="paragraph",
            prov=[types.SimpleNamespace(page_no=2)],
        )
        heading = types.SimpleNamespace(
            text="構造見出し",
            label="section_header",
            level=1,
            prov=[types.SimpleNamespace(page_no=2)],
        )
        table = types.SimpleNamespace(
            text="",
            label="table",
            data=types.SimpleNamespace(
                grid=[
                    [types.SimpleNamespace(text="A"), types.SimpleNamespace(text="B")],
                    [types.SimpleNamespace(text="1"), types.SimpleNamespace(text="2")],
                ]
            ),
            prov=[types.SimpleNamespace(page_no=2)],
        )

        class FakeDocument:
            def iterate_items(self):
                return iter(((heading, 1), (paragraph, 2), (table, 2)))

        converter = mock.Mock()
        converter.convert.return_value = types.SimpleNamespace(
            document=FakeDocument(), status="success"
        )
        with mock.patch(
            "software_rag_tool.structured_extraction._get_docling_converter",
            return_value=converter,
        ):
            result = extract_docling_tree(Path("fixture.pdf"), threads=1)

        self.assertEqual("indexed", result.status)
        self.assertEqual("docling_tree", result.structure_origin)
        self.assertIn("構造見出し", result.blocks[0].breadcrumb)
        combined = "\n".join(block.text for block in result.blocks)
        self.assertIn("本文の根拠", combined)
        self.assertIn("A\tB", combined)
        self.assertEqual(0, result.blocks[0].source_start)
        self.assertTrue(
            all(
                current.source_start > previous.source_end
                for previous, current in zip(
                    result.blocks,
                    result.blocks[1:],
                )
            )
        )
        converter.convert.assert_called_once()


class PipelineAndWorkerContracts(unittest.TestCase):
    def test_pipeline_fingerprint_covers_persisted_semantics(self) -> None:
        base = build_pipeline_contract(
            chunker={"version": "v1", "max": 384},
            lexical_tokenizer="fixture-lexical",
        )
        changed = build_pipeline_contract(
            chunker={"version": "v2", "max": 384},
            lexical_tokenizer="fixture-lexical",
        )
        descriptor = base["descriptor"]
        self.assertNotEqual(base["fingerprint"], changed["fingerprint"])
        self.assertIn("encoding_policy", descriptor)
        self.assertIn("extraction_status_schema", descriptor)
        self.assertIn("docling", descriptor)
        self.assertIn("tokenizer", descriptor)
        self.assertIn("embedding", descriptor)
        self.assertIn("embedding_artifact", descriptor["embedding"])
        self.assertIn("pooling", descriptor["embedding"])

    def test_missing_manifest_uses_state_fingerprint_as_generation_gate(self) -> None:
        state = {
            "ingestion": {"pipeline_fingerprint": "old"},
            "files": {"one": {"pipeline_fingerprint": "old"}},
        }
        with mock.patch.object(incremental, "read_manifest", return_value={}):
            self.assertTrue(
                incremental._pipeline_migration_required(state, "new")
            )
            self.assertFalse(
                incremental._pipeline_migration_required(state, "old")
            )

    def test_old_manifest_missing_embedding_generation_fields_is_rejected(self) -> None:
        current = {
            "embedding_model": "fixture",
            "embedding_dimension": 3,
            "embedding_backend": "onnx-int8",
            "quantization": "int8",
            "document_prefix": "doc: ",
            "query_prefix": "query: ",
            "embedding_artifact": "sha256:new",
            "pooling": "attention-mean+l2",
        }
        old_manifest = {
            key: value
            for key, value in current.items()
            if key not in {"embedding_artifact", "pooling"}
        }
        old_manifest["collection"] = "fixture-collection"
        with mock.patch(
            "software_rag_tool.manifest.embedding_fingerprint",
            return_value=current,
        ):
            with self.assertRaises(ConfigMismatchError):
                validate_embedding_manifest(
                    old_manifest,
                    collection="fixture-collection",
                )

    def test_pipeline_auto_rebuild_rejects_multi_source_state(self) -> None:
        scope = {
            "source_id": "current",
            "scan_subdir": "",
            "resolved_root": "/fixture/current",
        }
        state = {
            "ingestion": dict(scope),
            "files": {
                "current:a": {"source_id": "current"},
                "sibling:b": {"source_id": "sibling"},
            },
        }
        self.assertFalse(
            incremental._auto_pipeline_rebuild_safe(
                state,
                persistent_scope_fields=scope,
            )
        )
        del state["files"]["sibling:b"]
        self.assertTrue(
            incremental._auto_pipeline_rebuild_safe(
                state,
                persistent_scope_fields=scope,
            )
        )

    def test_worker_plan_honors_1_2_3_4_and_avoids_oversubscription(self) -> None:
        with (
            mock.patch(
                "software_rag_tool.ingestion_workers.docling_available",
                return_value=True,
            ),
            mock.patch("software_rag_tool.ingestion_workers.os.cpu_count", return_value=8),
            mock.patch(
                "software_rag_tool.ingestion_workers._available_memory_bytes",
                return_value=16 * 1024**3,
            ),
        ):
            for workers in (1, 2, 3, 4):
                plan = choose_worker_plan(4, requested_workers=workers)
                self.assertEqual(workers, plan.workers)
                self.assertLessEqual(
                    plan.workers * plan.threads_per_worker,
                    plan.logical_cpus,
                )

    def test_single_worker_still_isolates_docling_from_parent(self) -> None:
        with mock.patch(
            "software_rag_tool.ingestion_workers.docling_available",
            return_value=True,
        ):
            plan = choose_worker_plan(1, requested_workers=1)
            self.assertTrue(DoclingExtractionPool(plan).enabled)
            self.assertFalse(
                DoclingExtractionPool(
                    plan,
                    allow_docling=False,
                ).enabled
            )

    def test_worker_timeout_aborts_pool_and_returns_retryable_errors(self) -> None:
        plan = choose_worker_plan(2, requested_workers=1)
        pool = DoclingExtractionPool(
            plan,
            file_timeout_seconds=0.01,
        )
        executor = mock.Mock()
        executor._processes = {}
        future = mock.Mock()
        future.result.side_effect = FutureTimeoutError()
        executor.submit.return_value = future
        pool._executor = executor
        results = pool.extract([Path("a.docx"), Path("b.docx")])
        self.assertEqual(
            {"docling_worker_timeout"},
            {result.reason for result in results.values()},
        )
        self.assertTrue(all(result.retryable for result in results.values()))
        executor.shutdown.assert_called_once_with(
            wait=False,
            cancel_futures=True,
        )
        with mock.patch(
            "software_rag_tool.ingestion_workers.extract_document_structure"
        ) as parent_extract:
            later = pool.extract([Path("later.docx")])
        parent_extract.assert_not_called()
        self.assertEqual(
            "docling_worker_timeout",
            later[Path("later.docx")].reason,
        )

    def test_nonindexed_extraction_persists_retryable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "blank.pdf"
            path.write_bytes(b"fixture")
            scope = resolve_ingestion_scope(root)
            extraction = ExtractionResult(
                status="zero_text",
                backend="fixture",
                backend_version="1",
                source_format="pdf",
                structure_origin="docling_tree",
                retryable=True,
                reason="zero_text",
            )
            build = FileBuildResult(
                records=[],
                extraction=extraction,
                pipeline={"fingerprint": "fixture", "descriptor": {}},
            )
            with (
                mock.patch.object(
                    incremental, "file_content_hash", return_value="hash"
                ),
                mock.patch.object(
                    incremental,
                    "build_records_for_file",
                    return_value=build,
                ),
            ):
                item = incremental._prepare_file(
                    scope,
                    path,
                    "src",
                    {"files": {}},
                    retry_errors=False,
                    document_token_budget=TOKEN_BUDGET,
                    current_chunker_config={"version": "fixture"},
                )
        self.assertEqual("error", item["status"])
        self.assertEqual("zero_text", item["extraction_status"])
        self.assertTrue(item["retryable"])

    def test_unchanged_nonretryable_extraction_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "unsupported.pdf"
            path.write_bytes(b"fixture")
            scope = resolve_ingestion_scope(root)
            relative = f"{root.name}/unsupported.pdf"
            state = {
                "files": {
                    f"src:{relative}": {
                        "status": "unsupported",
                        "failed_content_hash": "hash",
                        "chunker_config": {"version": "fixture"},
                        "pipeline_fingerprint": "pipeline",
                        "retryable": False,
                    }
                }
            }
            with (
                mock.patch.object(
                    incremental, "file_content_hash", return_value="hash"
                ),
                mock.patch.object(
                    incremental,
                    "build_records_for_file",
                ) as build,
            ):
                item = incremental._prepare_file(
                    scope,
                    path,
                    "src",
                    state,
                    retry_errors=False,
                    document_token_budget=TOKEN_BUDGET,
                    current_chunker_config={"version": "fixture"},
                    current_pipeline_contract={
                        "fingerprint": "pipeline",
                        "descriptor": {},
                    },
                )
        self.assertEqual("skip", item["status"])
        build.assert_not_called()

    def test_query_import_does_not_import_docling(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        code = (
            "import sys; import software_rag_tool.search_api; "
            "print(int(any(n == 'docling' or n.startswith('docling.') "
            "for n in sys.modules)))"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(tool_root)
        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual("0", completed.stdout.strip())

    def test_worker_init_failure_uses_explicit_legacy_fallback(self) -> None:
        fallback = ExtractionResult(
            status="indexed",
            backend="legacy-pdf",
            backend_version="1",
            source_format="pdf",
            structure_origin="legacy",
            blocks=(StructureBlock(title="p1", text="body"),),
        )
        with (
            mock.patch(
                "software_rag_tool.ingestion_workers._WORKER_INIT_ERROR",
                "worker_init_RuntimeError",
            ),
            mock.patch(
                "software_rag_tool.ingestion_workers.extract_document_structure",
                return_value=fallback,
            ) as extract,
        ):
            from software_rag_tool.ingestion_workers import _extract_worker

            result = _extract_worker("fixture.pdf", "auto")
        self.assertEqual("indexed", result.status)
        self.assertEqual("legacy_fallback", result.structure_origin)
        self.assertEqual("worker_init_RuntimeError", result.fallback_reason)
        extract.assert_called_once_with(
            Path("fixture.pdf"),
            backend_policy="legacy",
        )


class SmallToBigAblationTests(unittest.TestCase):
    def test_parent_expansion_is_opt_in_and_budgeted(self) -> None:
        primary = {
            "id": "c2",
            "text": "selected small chunk",
            "signals": ["dense"],
            "metadata": {
                "source_id": "s",
                "doc_id": "d",
                "path": "fixture.pdf",
                "chunk_index": 2,
                "section_path": "child-b",
                "parent_section_id": "parent-1",
            },
        }
        neighbors = [
            {
                "id": "c1",
                "text": "parent context before",
                "metadata": {
                    "source_id": "s",
                    "doc_id": "d",
                    "path": "fixture.pdf",
                    "chunk_index": 1,
                    "section_path": "child-a",
                    "parent_section_id": "parent-1",
                },
            },
            primary,
            {
                "id": "c3",
                "text": "parent context after",
                "metadata": {
                    "source_id": "s",
                    "doc_id": "d",
                    "path": "fixture.pdf",
                    "chunk_index": 3,
                    "section_path": "child-c",
                    "parent_section_id": "parent-1",
                },
            },
        ]
        backend = mock.Mock()
        backend.get_neighbor_rows.return_value = neighbors

        ordinary, _ = _attach_structure_context(
            [primary],
            question="what is selected",
            backend=backend,
            context_budget_tokens=100,
            verified_anchor_ids=set(),
            small_to_big=False,
        )
        expanded, diagnostics = _attach_structure_context(
            [primary],
            question="what is selected",
            backend=backend,
            context_budget_tokens=100,
            verified_anchor_ids=set(),
            small_to_big=True,
        )
        self.assertNotIn("context_before", ordinary[0])
        self.assertEqual("parent context before", expanded[0]["context_before"])
        self.assertEqual("parent context after", expanded[0]["context_after"])
        self.assertEqual(
            {"parent_section_context"},
            {item["support_kind"] for item in diagnostics},
        )
        self.assertEqual(4, backend.get_neighbor_rows.call_args_list[-1].kwargs["window"])

    def test_low_budget_keeps_highest_ranked_primary(self) -> None:
        rows = [
            {
                "id": f"row-{index}",
                "text": "evidence " * 30,
                "metadata": {"chunk_index": index},
            }
            for index in range(8)
        ]
        packed = _pack_protected_rows(
            rows,
            question="evidence",
            budget_tokens=60,
        )
        self.assertTrue(packed)
        self.assertEqual("row-0", packed[0]["id"])

    def test_tiny_budgets_never_emit_empty_primary_text(self) -> None:
        row = {
            "id": "row-0",
            "text": "evidence anchor remains visible",
            "metadata": {"chunk_index": 0},
        }
        for budget in range(1, 12):
            packed = _pack_protected_rows(
                [row],
                question="anchor",
                budget_tokens=budget,
            )
            self.assertTrue(packed, budget)
            self.assertTrue(packed[0]["text"], budget)

    def test_small_to_big_is_disabled_by_default(self) -> None:
        backend = mock.Mock()
        backend.vector_query.return_value = []
        backend.bm25_search.return_value = []
        backend.exact_search.return_value = []
        backend.metadata_search.return_value = []
        backend.anchor_lexical_search.return_value = []
        with mock.patch(
            "software_rag_tool.retrieval._expand_and_pack",
            return_value=[],
        ) as expand:
            hybrid_query("question", top_k=8, backend=backend)
        self.assertFalse(expand.call_args.kwargs["small_to_big"])


class MultiSourceResetSafetyTests(unittest.TestCase):
    def test_single_source_reset_refuses_to_delete_sibling_sources(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(
                incremental,
                "_saved_state_source_ids_for_reset",
                return_value={"source-a", "source-b"},
            ),
            mock.patch.object(incremental, "reset_collection") as reset_vector,
            mock.patch.object(incremental, "reset_catalog") as reset_catalog,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "single-source reset refused",
            ):
                incremental.add_or_update_root(
                    Path(temporary),
                    "source-a",
                    reset_db=True,
                )
        reset_vector.assert_not_called()
        reset_catalog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
