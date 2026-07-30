from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOL_ROOT = (
    Path(__file__).resolve().parents[1]
    / "gen_db"
    / "software_rag_tool"
)
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import source_delete  # noqa: E402
from software_rag_tool import catalog  # noqa: E402
from software_rag_tool import store  # noqa: E402


class SourceDeleteContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-source-delete-"
        )
        self.root = Path(self.temporary.name)
        self.clean = self.root / "data" / "clean"
        self.logs = self.root / "logs"
        self.index = self.root / "index"
        (self.clean / "records").mkdir(parents=True)
        self.logs.mkdir()
        self.index.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _record(source_id: str, chunk_id: str) -> dict[str, object]:
        return {
            "id": chunk_id,
            "text": "fixture",
            "metadata": {
                "source_id": source_id,
                "path": f"{source_id}/document.md",
            },
        }

    def test_exact_source_delete_preserves_sibling_clean_and_state(self) -> None:
        source_a = self.clean / "records" / "a.jsonl"
        source_b = self.clean / "records" / "b.jsonl"
        source_a.write_text(
            json.dumps(self._record("source-a", "a-chunk")) + "\n",
            encoding="utf-8",
        )
        source_b.write_text(
            json.dumps(self._record("source-b", "b-chunk")) + "\n",
            encoding="utf-8",
        )
        (self.logs / "index_state.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "files": {
                        "source-a:file": {
                            "source_id": "source-a",
                            "record_ids": ["a-chunk"],
                            "record_count": 1,
                            "records_path": "records/a.jsonl",
                        },
                        "source-b:file": {
                            "source_id": "source-b",
                            "record_ids": ["b-chunk"],
                            "record_count": 1,
                            "records_path": "records/b.jsonl",
                        },
                    },
                    "ingestion": {"source_id": "source-a"},
                }
            ),
            encoding="utf-8",
        )
        (self.index / "manifest.json").write_text(
            json.dumps({"record_count": 2, "collection": "fixture"}),
            encoding="utf-8",
        )
        with (
            mock.patch.object(source_delete, "clean_dir", return_value=self.clean),
            mock.patch.object(source_delete, "logs_dir", return_value=self.logs),
            mock.patch.object(source_delete, "index_dir", return_value=self.index),
            mock.patch.object(source_delete, "_validate_destructive_storage"),
            mock.patch.object(
                source_delete.catalog,
                "ensure_source_delete_index",
            ),
            mock.patch.object(
                source_delete.catalog,
                "source_chunk_ids",
                side_effect=[["a-chunk"], []],
            ),
            mock.patch.object(
                source_delete.catalog,
                "delete_source_documents",
                side_effect=[
                    {"documents": 1, "chunks": 1},
                    {"documents": 0, "chunks": 0},
                ],
            ),
            mock.patch.object(
                source_delete.catalog,
                "chunk_count",
                return_value=1,
            ),
            mock.patch.object(
                source_delete,
                "delete_ids",
                return_value=1,
            ) as delete_ids,
        ):
            result = source_delete.delete_source_data("source-a")
            repeated = source_delete.delete_source_data("source-a")

        self.assertEqual("deleted", result["status"])
        self.assertEqual("deleted", repeated["status"])
        self.assertEqual(0, repeated["chunks_deleted"])
        self.assertFalse(source_a.exists())
        self.assertTrue(source_b.exists())
        remaining = json.loads(
            (self.logs / "index_state.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("source-a:file", remaining["files"])
        self.assertIn("source-b:file", remaining["files"])
        self.assertEqual({}, remaining["ingestion"])
        delete_ids.assert_called_once_with(["a-chunk"])
        manifest = json.loads(
            (self.index / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, manifest["record_count"])

    def test_catalog_delete_uses_exact_source_id(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RAG_OUTPUT_ROOT": str(self.root)},
        ):
            catalog.upsert_records(
                [
                    {
                        "id": "a-chunk",
                        "text": "SHARED-123 TARGET-999",
                        "metadata": {
                            "doc_id": "doc-a",
                            "source_id": "source-a",
                            "path": "same/document.md",
                        },
                    },
                    {
                        "id": "b-chunk",
                        "text": "SHARED-123",
                        "metadata": {
                            "doc_id": "doc-b",
                            "source_id": "source-b",
                            "path": "same/document.md",
                        },
                    },
                ]
            )
            self.assertEqual(
                ["a-chunk"],
                catalog.source_chunk_ids("source-a"),
            )
            deleted = catalog.delete_source_documents("source-a")
            self.assertEqual({"documents": 1, "chunks": 1}, deleted)
            self.assertEqual([], catalog.source_chunk_ids("source-a"))
            self.assertEqual(
                ["b-chunk"],
                catalog.source_chunk_ids("source-b"),
            )
            with catalog.connect_readonly(catalog.catalog_path()) as connection:
                self.assertEqual(
                    (1, 1),
                    (
                        int(connection.execute(
                            "SELECT COUNT(*) FROM fts_word"
                        ).fetchone()[0]),
                        int(connection.execute(
                            "SELECT COUNT(*) FROM file_fts"
                        ).fetchone()[0]),
                    ),
                )
                lookup_sources = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT d.source_id
                        FROM document_lookup dl
                        JOIN document d ON d.doc_pk = dl.doc_pk
                        """
                    ).fetchall()
                }
                shared = connection.execute(
                    """
                    SELECT document_frequency
                    FROM identifier_term
                    WHERE canonical_value = 'shared-123'
                    """
                ).fetchone()
                target_only = connection.execute(
                    """
                    SELECT 1 FROM identifier_term
                    WHERE canonical_value = 'target-999'
                    """
                ).fetchone()
                integrity = str(
                    connection.execute("PRAGMA integrity_check").fetchone()[0]
                )
            self.assertIsNotNone(shared)
            self.assertEqual(1, int(shared[0]))
            self.assertIsNone(target_only)
            self.assertEqual("ok", integrity.casefold())
            self.assertEqual({"source-b"}, lookup_sources)
            self.assertEqual(
                {"documents": 0, "chunks": 0},
                catalog.delete_source_documents("source-a"),
            )

    def test_old_catalog_installs_source_index_before_delete_plan(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RAG_OUTPUT_ROOT": str(self.root)},
        ):
            catalog.upsert_records(
                [
                    {
                        "id": "a-chunk",
                        "text": "fixture",
                        "metadata": {
                            "doc_id": "doc-a",
                            "source_id": "source-a",
                            "path": "source-a/document.md",
                        },
                    }
                ]
            )
            with catalog.connect() as connection:
                connection.execute("DROP INDEX idx_document_source_id")
            catalog.ensure_source_delete_index()
            with catalog.connect_readonly(catalog.catalog_path()) as connection:
                indexes = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA index_list(document)"
                    ).fetchall()
                }
                query_plan = " ".join(
                    str(row[3])
                    for row in connection.execute(
                        """
                        EXPLAIN QUERY PLAN
                        SELECT c.chunk_uid
                        FROM document d
                        JOIN chunk c ON c.doc_pk = d.doc_pk
                        WHERE d.source_id = ?
                        """,
                        ("source-a",),
                    ).fetchall()
                )
            self.assertIn("idx_document_source_id", indexes)
            self.assertIn("idx_document_source_id", query_plan)

    def test_current_state_fast_path_opens_only_target_jsonl(self) -> None:
        target = self.clean / "records" / "target.jsonl"
        sibling = self.clean / "records" / "sibling.jsonl"
        target.write_text(
            json.dumps(self._record("source-a", "a-chunk")) + "\n",
            encoding="utf-8",
        )
        sibling.write_text(
            json.dumps(self._record("source-b", "b-chunk")) + "\n",
            encoding="utf-8",
        )
        state = {
            "version": 2,
            "files": {
                "a": {
                    "source_id": "source-a",
                    "record_ids": ["a-chunk"],
                    "record_count": 1,
                    "records_path": "records/target.jsonl",
                },
                "b": {
                    "source_id": "source-b",
                    "record_ids": ["b-chunk"],
                    "record_count": 1,
                    "records_path": "records/sibling.jsonl",
                },
            },
        }
        opened: list[Path] = []
        original = source_delete.read_jsonl

        def observed(path: Path):
            opened.append(Path(path))
            return original(path)

        with (
            mock.patch.object(source_delete, "clean_dir", return_value=self.clean),
            mock.patch.object(source_delete, "read_jsonl", side_effect=observed),
        ):
            actions = source_delete._plan_clean_deletion(
                "source-a",
                state=state,
                matching_state_keys=["a"],
                expected_record_ids={"a-chunk"},
            )

        self.assertEqual([(target, None)], actions)
        self.assertEqual([target], opened)

    def test_mixed_jsonl_falls_back_and_is_atomically_rewritten(self) -> None:
        mixed = self.clean / "records" / "mixed.jsonl"
        mixed.write_text(
            json.dumps(self._record("source-a", "a-chunk"))
            + "\n"
            + json.dumps(self._record("source-b", "b-chunk"))
            + "\n",
            encoding="utf-8",
        )
        state = {
            "version": 2,
            "files": {
                "a": {
                    "source_id": "source-a",
                    "record_ids": ["a-chunk"],
                    "record_count": 1,
                    "records_path": "records/mixed.jsonl",
                },
            },
        }
        with mock.patch.object(
            source_delete,
            "clean_dir",
            return_value=self.clean,
        ):
            actions = source_delete._plan_clean_deletion(
                "source-a",
                state=state,
                matching_state_keys=["a"],
                expected_record_ids={"a-chunk"},
            )
            deleted, rewritten = source_delete._apply_clean_deletion(actions)

        self.assertEqual((0, 1), (deleted, rewritten))
        remaining = list(source_delete.read_jsonl(mixed))
        self.assertEqual(["source-b"], [
            source_delete._record_source_id(record) for record in remaining
        ])
        self.assertEqual([], list(mixed.parent.glob(".*.tmp")))

    def test_chroma_delete_is_deduplicated_and_batched(self) -> None:
        batches: list[list[str]] = []
        progress: list[tuple[int, int]] = []
        collection = mock.Mock()
        collection.delete.side_effect = lambda *, ids: batches.append(list(ids))
        values = [f"id-{index}" for index in range(4_501)] + ["id-0"]
        with mock.patch.object(
            store,
            "_get_or_create_collection",
            return_value=collection,
        ) as get_collection:
            deleted = store.delete_ids(
                values,
                progress_callback=lambda done, total: progress.append(
                    (done, total)
                ),
                batch_size=2_000,
            )

        self.assertEqual(4_501, deleted)
        self.assertEqual([2_000, 2_000, 501], [len(value) for value in batches])
        flattened = [item for batch in batches for item in batch]
        self.assertEqual([f"id-{index}" for index in range(4_501)], flattened)
        self.assertEqual([(2_000, 4_501), (4_000, 4_501), (4_501, 4_501)], progress)
        get_collection.assert_called_once_with()

    def test_catalog_source_delete_rolls_back_all_sets_on_error(self) -> None:
        with mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(self.root)}):
            catalog.upsert_records(
                [
                    {
                        "id": "a-chunk",
                        "text": "ROLLBACK-123",
                        "metadata": {
                            "doc_id": "doc-a",
                            "source_id": "source-a",
                            "path": "a/document.md",
                        },
                    }
                ]
            )
            with catalog.connect() as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_source_delete
                    BEFORE DELETE ON document
                    BEGIN
                      SELECT RAISE(ABORT, 'synthetic rollback');
                    END
                    """
                )
            with self.assertRaises(sqlite3.IntegrityError):
                catalog.delete_source_documents("source-a")
            self.assertEqual(
                ["a-chunk"],
                catalog.source_chunk_ids("source-a"),
            )
            with catalog.connect_readonly(catalog.catalog_path()) as connection:
                self.assertEqual(
                    1,
                    int(connection.execute(
                        "SELECT COUNT(*) FROM identifier_posting"
                    ).fetchone()[0]),
                )

    def test_weak_acronym_global_threshold_is_reapplied(self) -> None:
        records: list[dict[str, object]] = []
        for index in range(500):
            target = index < 100
            text = "ordinary fixture"
            if 100 <= index < 109:
                text = "XYZ"
            records.append(
                {
                    "id": f"chunk-{index}",
                    "text": text,
                    "metadata": {
                        "doc_id": f"doc-{index}",
                        "source_id": "source-a" if target else "source-b",
                        "path": f"docs/{index}.md",
                    },
                }
            )
        with mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(self.root)}):
            catalog.upsert_records(records)
            with catalog.connect_readonly(catalog.catalog_path()) as connection:
                before = connection.execute(
                    """
                    SELECT document_frequency
                    FROM identifier_term
                    WHERE canonical_value = 'xyz'
                    """
                ).fetchone()
            self.assertIsNotNone(before)
            self.assertEqual(9, int(before[0]))
            catalog.delete_source_documents("source-a")
            with catalog.connect_readonly(catalog.catalog_path()) as connection:
                remaining = connection.execute(
                    """
                    SELECT 1 FROM identifier_term
                    WHERE canonical_value = 'xyz'
                    """
                ).fetchone()
                suppressed = connection.execute(
                    """
                    SELECT 1 FROM identifier_suppressed
                    WHERE canonical_value = 'xyz'
                    """
                ).fetchone()
            self.assertIsNone(remaining)
            self.assertIsNotNone(suppressed)

    def test_delete_source_cli_manager_protocol_and_legacy_stdout(self) -> None:
        rag_root = Path(__file__).resolve().parents[1]
        runtime = rag_root / "query" / ".venv" / "bin" / "python"
        if not runtime.is_file():
            runtime = Path(sys.executable)
        dbs = self.root / "dbs"
        (dbs / "fixture-rag").mkdir(parents=True)
        environment = {
            **os.environ,
            "RAG_DBS_ROOT": str(dbs),
            "PYTHONIOENCODING": "cp932",
            "PYTHONUTF8": "0",
        }
        base = [
            str(runtime),
            str(rag_root / "gen_db" / "delete_source.py"),
            "--db",
            "fixture-rag",
            "--source-id",
            "日本語ソース",
        ]
        legacy = subprocess.run(
            base,
            cwd=rag_root,
            env=environment,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, legacy.returncode, legacy.stderr.decode("utf-8"))
        legacy_text = legacy.stdout.decode("utf-8")
        self.assertNotIn("@@LOCAL_RAG_RESULT_V1@@", legacy_text)
        self.assertEqual(
            "日本語ソース",
            json.loads(legacy_text)["source_id"],
        )

        framed = subprocess.run(
            [*base, "--manager-protocol-v1"],
            cwd=rag_root,
            env=environment,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, framed.returncode, framed.stderr.decode("utf-8"))
        stdout_lines = framed.stdout.decode("utf-8").splitlines()
        self.assertEqual(1, len(stdout_lines))
        prefix = "@@LOCAL_RAG_RESULT_V1@@"
        self.assertTrue(stdout_lines[0].startswith(prefix))
        result = json.loads(stdout_lines[0][len(prefix) :])
        self.assertEqual("deleted", result["status"])
        self.assertEqual("日本語ソース", result["source_id"])
        phases = [
            json.loads(line.split("@@", 2)[-1]).get("phase")
            for line in framed.stderr.decode("utf-8").splitlines()
            if line.startswith("@@LOCAL_RAG_PROGRESS_V1@@")
        ]
        self.assertEqual(
            [
                "delete.verify",
                "delete.vector",
                "delete.catalog",
                "delete.catalog",
                "delete.clean",
                "delete.state",
                "delete.state",
                "delete.complete",
            ],
            phases,
        )

    def test_reparse_point_is_rejected_by_clean_safety_helper(self) -> None:
        metadata = mock.Mock(
            st_mode=0,
            st_file_attributes=0x400,
        )
        with mock.patch.object(
            source_delete.stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
            create=True,
        ):
            self.assertTrue(
                source_delete._is_link_or_reparse(
                    metadata,
                    self.clean,
                )
            )

    def test_phase_failures_converge_when_same_delete_is_retried(self) -> None:
        original_apply = source_delete._apply_clean_deletion
        original_atomic = source_delete._atomic_json_write
        original_manifest = source_delete._update_manifest_count
        for failed_phase in ("vector", "catalog", "clean", "state", "manifest"):
            with self.subTest(failed_phase=failed_phase):
                case = self.root / failed_phase
                clean = case / "data" / "clean" / "records"
                logs = case / "logs"
                index = case / "index"
                clean.mkdir(parents=True)
                logs.mkdir()
                index.mkdir()
                target_paths = [
                    clean / "target-1.jsonl",
                    clean / "target-2.jsonl",
                ]
                sibling = clean / "sibling.jsonl"
                for number, path in enumerate(target_paths, start=1):
                    path.write_text(
                        json.dumps(
                            self._record("source-a", f"a-{number}")
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                sibling.write_text(
                    json.dumps(self._record("source-b", "b-1")) + "\n",
                    encoding="utf-8",
                )
                state = {
                    "version": 2,
                    "files": {
                        "a-1": {
                            "source_id": "source-a",
                            "record_ids": ["a-1"],
                            "record_count": 1,
                            "records_path": "records/target-1.jsonl",
                        },
                        "a-2": {
                            "source_id": "source-a",
                            "record_ids": ["a-2"],
                            "record_count": 1,
                            "records_path": "records/target-2.jsonl",
                        },
                        "b-1": {
                            "source_id": "source-b",
                            "record_ids": ["b-1"],
                            "record_count": 1,
                            "records_path": "records/sibling.jsonl",
                        },
                    },
                    "ingestion": {"source_id": "source-a"},
                }
                state_path = logs / "index_state.json"
                state_path.write_text(json.dumps(state), encoding="utf-8")
                (index / "manifest.json").write_text(
                    json.dumps({"record_count": 3}),
                    encoding="utf-8",
                )
                vector_calls = 0
                catalog_calls = 0
                apply_calls = 0
                state_write_calls = 0
                manifest_calls = 0

                def vector(ids):
                    nonlocal vector_calls
                    vector_calls += 1
                    if failed_phase == "vector" and vector_calls == 1:
                        raise RuntimeError("synthetic vector failure")
                    return len(ids)

                def delete_catalog(_source_id):
                    nonlocal catalog_calls
                    catalog_calls += 1
                    if failed_phase == "catalog" and catalog_calls == 1:
                        raise RuntimeError("synthetic catalog failure")
                    return {"documents": 2, "chunks": 2}

                def apply(actions, **kwargs):
                    nonlocal apply_calls
                    apply_calls += 1
                    if failed_phase == "clean" and apply_calls == 1:
                        planned = list(actions)
                        planned[0][0].unlink()
                        raise RuntimeError("synthetic clean failure")
                    return original_apply(actions, **kwargs)

                def atomic(path, payload):
                    nonlocal state_write_calls
                    if Path(path).name == "index_state.json":
                        state_write_calls += 1
                        if failed_phase == "state" and state_write_calls == 1:
                            raise RuntimeError("synthetic state failure")
                    return original_atomic(path, payload)

                def manifest_count(count):
                    nonlocal manifest_calls
                    manifest_calls += 1
                    if failed_phase == "manifest" and manifest_calls == 1:
                        raise RuntimeError("synthetic manifest failure")
                    return original_manifest(count)

                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(
                        source_delete, "clean_dir", return_value=case / "data" / "clean"
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete, "logs_dir", return_value=logs
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete, "index_dir", return_value=index
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete,
                        "_validate_destructive_storage",
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete.catalog,
                        "ensure_source_delete_index",
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete.catalog,
                        "source_chunk_ids",
                        return_value=["a-1", "a-2"],
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete.catalog,
                        "delete_source_documents",
                        side_effect=delete_catalog,
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete.catalog,
                        "chunk_count",
                        return_value=1,
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete,
                        "delete_ids",
                        side_effect=vector,
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete,
                        "_apply_clean_deletion",
                        side_effect=apply,
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete,
                        "_atomic_json_write",
                        side_effect=atomic,
                    ))
                    stack.enter_context(mock.patch.object(
                        source_delete,
                        "_update_manifest_count",
                        side_effect=manifest_count,
                    ))
                    with self.assertRaisesRegex(
                        RuntimeError,
                        f"synthetic {failed_phase} failure",
                    ):
                        source_delete.delete_source_data("source-a")
                    result = source_delete.delete_source_data("source-a")

                self.assertEqual("deleted", result["status"])
                self.assertTrue(sibling.exists())
                self.assertTrue(all(not path.exists() for path in target_paths))
                remaining = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(["b-1"], list(remaining["files"]))

    def test_corrupt_state_id_never_deletes_sibling_vector(self) -> None:
        target = self.clean / "records" / "target.jsonl"
        sibling = self.clean / "records" / "sibling.jsonl"
        target.write_text(
            json.dumps(self._record("source-a", "a-chunk")) + "\n",
            encoding="utf-8",
        )
        sibling.write_text(
            json.dumps(self._record("source-b", "b-chunk")) + "\n",
            encoding="utf-8",
        )
        (self.logs / "index_state.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "files": {
                        "corrupt": {
                            "source_id": "source-a",
                            "record_ids": ["b-chunk"],
                            "record_count": 1,
                            "records_path": "records/target.jsonl",
                        }
                    },
                    "ingestion": {},
                }
            ),
            encoding="utf-8",
        )
        with (
            mock.patch.object(source_delete, "clean_dir", return_value=self.clean),
            mock.patch.object(source_delete, "logs_dir", return_value=self.logs),
            mock.patch.object(source_delete, "index_dir", return_value=self.index),
            mock.patch.object(source_delete, "_validate_destructive_storage"),
            mock.patch.object(
                source_delete.catalog,
                "ensure_source_delete_index",
            ),
            mock.patch.object(
                source_delete.catalog,
                "source_chunk_ids",
                return_value=["a-chunk"],
            ),
            mock.patch.object(
                source_delete.catalog,
                "delete_source_documents",
                return_value={"documents": 1, "chunks": 1},
            ),
            mock.patch.object(
                source_delete.catalog,
                "chunk_count",
                return_value=1,
            ),
            mock.patch.object(
                source_delete,
                "delete_ids",
                return_value=1,
            ) as delete_ids,
        ):
            source_delete.delete_source_data("source-a")

        delete_ids.assert_called_once_with(["a-chunk"])
        self.assertTrue(sibling.exists())

    def test_internal_catalog_symlink_is_rejected_before_deletion(self) -> None:
        db_root = self.root / "db"
        db_root.mkdir()
        outside = self.root / "outside.sqlite"
        outside.write_bytes(b"outside-catalog-sentinel")
        link = db_root / "catalog.sqlite"
        try:
            link.symlink_to(outside)
        except (NotImplementedError, OSError):
            self.skipTest("file symlinks are unavailable")
        before = outside.read_bytes()
        with mock.patch.dict(
            os.environ,
            {"RAG_OUTPUT_ROOT": str(db_root), "RAG_DB_NAME": "fixture-rag"},
        ):
            with self.assertRaisesRegex(RuntimeError, "must not contain links"):
                source_delete.delete_source_data("source-a")
        self.assertEqual(before, outside.read_bytes())

    def test_vector_target_ignores_ambient_chroma_environment(self) -> None:
        (self.index / "chroma").mkdir()
        observed: list[tuple[str, str]] = []

        def vector(ids):
            observed.append(
                (
                    os.environ["CHROMA_DIR_V2"],
                    os.environ["CHROMA_COLLECTION"],
                )
            )
            return len(ids)

        environment = {
            "RAG_OUTPUT_ROOT": str(self.root),
            "RAG_DB_NAME": "fixture-rag",
            "CHROMA_DIR_V2": str(self.root.parent / "external-chroma"),
            "CHROMA_COLLECTION": "wrong_collection",
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(
                source_delete.catalog,
                "ensure_source_delete_index",
            ),
            mock.patch.object(
                source_delete.catalog,
                "source_chunk_ids",
                return_value=["a-chunk"],
            ),
            mock.patch.object(
                source_delete.catalog,
                "delete_source_documents",
                return_value={"documents": 1, "chunks": 1},
            ),
            mock.patch.object(
                source_delete.catalog,
                "chunk_count",
                return_value=0,
            ),
            mock.patch.object(source_delete, "delete_ids", side_effect=vector),
        ):
            source_delete.delete_source_data("source-a")
            self.assertEqual(
                str(self.root.parent / "external-chroma"),
                os.environ["CHROMA_DIR_V2"],
            )
            self.assertEqual(
                "wrong_collection",
                os.environ["CHROMA_COLLECTION"],
            )
        self.assertEqual(
            [
                (
                    str(self.index / "chroma"),
                    "fixture_rag_ruri3_30m_int8_v1",
                )
            ],
            observed,
        )

    def test_retry_after_catalog_commit_keeps_clean_fast_path(self) -> None:
        target = self.clean / "records" / "target.jsonl"
        sibling = self.clean / "records" / "sibling.jsonl"
        target.write_text(
            json.dumps(self._record("source-a", "a-chunk")) + "\n",
            encoding="utf-8",
        )
        sibling.write_text(
            json.dumps(self._record("source-b", "b-chunk")) + "\n",
            encoding="utf-8",
        )
        (self.logs / "index_state.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "files": {
                        "a": {
                            "source_id": "source-a",
                            "record_ids": ["a-chunk"],
                            "record_count": 1,
                            "records_path": "records/target.jsonl",
                        },
                        "b": {
                            "source_id": "source-b",
                            "record_ids": ["b-chunk"],
                            "record_count": 1,
                            "records_path": "records/sibling.jsonl",
                        },
                    },
                    "ingestion": {},
                }
            ),
            encoding="utf-8",
        )
        opened: list[Path] = []
        original_read = source_delete.read_jsonl
        original_apply = source_delete._apply_clean_deletion
        apply_calls = 0

        def read(path):
            opened.append(Path(path))
            return original_read(path)

        def apply(actions, **kwargs):
            nonlocal apply_calls
            apply_calls += 1
            if apply_calls == 1:
                raise RuntimeError("after catalog commit")
            return original_apply(actions, **kwargs)

        with (
            mock.patch.object(source_delete, "clean_dir", return_value=self.clean),
            mock.patch.object(source_delete, "logs_dir", return_value=self.logs),
            mock.patch.object(source_delete, "index_dir", return_value=self.index),
            mock.patch.object(source_delete, "_validate_destructive_storage"),
            mock.patch.object(source_delete, "read_jsonl", side_effect=read),
            mock.patch.object(
                source_delete.catalog,
                "ensure_source_delete_index",
            ),
            mock.patch.object(
                source_delete.catalog,
                "source_chunk_ids",
                side_effect=[["a-chunk"], []],
            ),
            mock.patch.object(
                source_delete.catalog,
                "delete_source_documents",
                side_effect=[
                    {"documents": 1, "chunks": 1},
                    {"documents": 0, "chunks": 0},
                ],
            ),
            mock.patch.object(
                source_delete.catalog,
                "chunk_count",
                return_value=1,
            ),
            mock.patch.object(source_delete, "delete_ids", return_value=1),
            mock.patch.object(
                source_delete,
                "_apply_clean_deletion",
                side_effect=apply,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "after catalog commit"):
                source_delete.delete_source_data("source-a")
            result = source_delete.delete_source_data("source-a")

        self.assertEqual("deleted", result["status"])
        self.assertEqual([target, target], opened)
        self.assertTrue(sibling.exists())


if __name__ == "__main__":
    unittest.main()
