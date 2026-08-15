from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from software_rag_tool import catalog, incremental


def record(
    chunk_id: str,
    *,
    doc_id: str | None = None,
    text: str = "alpha RFC10026",
    index: int = 0,
    title: str | None = None,
) -> dict:
    doc_id = doc_id or chunk_id.split(":", 1)[0]
    return {
        "id": chunk_id,
        "text": text,
        "metadata": {
            "doc_id": doc_id,
            "path": f"docs/{doc_id}.md",
            "title": title or doc_id,
            "source": "fixture",
            "source_id": "fixture-source",
            "source_type": "Other",
            "uri": f"fixture://{doc_id}",
            "language": "en",
            "content_hash": f"content-{doc_id}",
            "chunk_hash": f"chunk-{chunk_id}",
            "text_hash": f"text-{chunk_id}",
            "chunk_index": index,
            "section_path": f"section-{index}",
        },
    }


def logical_dump(path: Path) -> dict[str, list[tuple]]:
    queries = {
        "meta": "SELECT key, value FROM database_meta WHERE key != 'updated_at' ORDER BY key",
        "document": """
            SELECT doc_id, source, source_id, source_type, path, uri, title,
                   language, content_hash, metadata_json, visible_from, visible_until
            FROM document ORDER BY doc_id
        """,
        "chunk": """
            SELECT c.chunk_uid, d.doc_id, c.chunk_index, c.section_path, c.language,
                   c.chunk_hash, c.content_hash, c.text_hash, c.text,
                   c.location_json, c.metadata_json, c.visible_from, c.visible_until
            FROM chunk c JOIN document d ON d.doc_pk = c.doc_pk
            ORDER BY c.chunk_uid
        """,
        "word": """
            SELECT c.chunk_uid, f.heading_tokens, f.body_tokens
            FROM fts_word f JOIN chunk c ON c.chunk_pk = f.rowid
            ORDER BY c.chunk_uid
        """,
        "file": """
            SELECT d.doc_id, f.basename_tokens, f.stem_tokens, f.path_tokens, f.title_tokens
            FROM file_fts f JOIN document d ON d.doc_pk = f.rowid
            ORDER BY d.doc_id
        """,
        "lookup": """
            SELECT l.normalized_value, d.doc_id, l.kind, l.raw_value
            FROM document_lookup l JOIN document d ON d.doc_pk = l.doc_pk
            ORDER BY l.normalized_value, d.doc_id, l.kind
        """,
        "term": """
            SELECT canonical_value, kind, document_frequency, flags
            FROM identifier_term ORDER BY canonical_value
        """,
        "alias": """
            SELECT a.alias_value, t.canonical_value, a.match_kind
            FROM identifier_alias a JOIN identifier_term t ON t.term_id = a.term_id
            ORDER BY a.alias_value, t.canonical_value
        """,
        "posting": """
            SELECT t.canonical_value, c.chunk_uid, p.field, p.count
            FROM identifier_posting p
            JOIN identifier_term t ON t.term_id = p.term_id
            JOIN chunk c ON c.chunk_pk = p.chunk_pk
            ORDER BY t.canonical_value, c.chunk_uid, p.field
        """,
        "suppressed": """
            SELECT canonical_value, kind FROM identifier_suppressed
            ORDER BY canonical_value
        """,
    }
    with catalog.connect_readonly(path) as connection:
        return {
            name: [tuple(row) for row in connection.execute(sql)]
            for name, sql in queries.items()
        }


class CatalogWriteProductizationR2(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "RAG_OUTPUT_ROOT": str(self.root),
                "LOCAL_RAG_LEXICAL_TOKENIZER": "fallback",
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def test_insert_record_keeps_three_argument_compatibility(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with catalog.connect() as connection:
            catalog._insert_record(connection, record("compat:0"), now)
        self.assertEqual({"compat:0"}, set(catalog.fetch_rows_by_ids(["compat:0"])))

    def test_canonical_parity_for_add_grow_shrink_and_ten_percent_replace(self) -> None:
        initial = [record(f"d{i}:0", doc_id=f"d{i}") for i in range(10)]
        catalog.upsert_records(initial)
        catalog.upsert_records(
            [
                record("d3:0", doc_id="d3", text="changed RFC30003", index=0),
                record("d3:1", doc_id="d3", text="tail RFC30003", index=1),
            ],
            delete_ids=["d3:0", "d3:0", "missing"],
        )
        catalog.upsert_records(
            [record("d3:1", doc_id="d3", text="final RFC30003", index=0)],
            delete_ids=["d3:0", "d3:1", "missing"],
        )
        actual = logical_dump(catalog.catalog_path())

        expected_root = self.root / "expected"
        final = [
            record(f"d{i}:0", doc_id=f"d{i}")
            for i in range(10)
            if i != 3
        ] + [record("d3:1", doc_id="d3", text="final RFC30003", index=0)]
        with mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(expected_root)}):
            catalog.upsert_records(final)
            expected = logical_dump(catalog.catalog_path())
        self.assertEqual(expected, actual)

    def test_document_is_written_once_in_first_seen_order_with_last_metadata(self) -> None:
        records = [
            record("a:0", doc_id="a", title="a-first"),
            record("b:0", doc_id="b", title="b-only"),
            record("a:1", doc_id="a", title="a-last", index=1),
        ]
        order: list[str] = []
        original = catalog._upsert_document

        def observed(connection, item, metadata, doc_id, now):
            order.append(doc_id)
            return original(connection, item, metadata, doc_id, now)

        with mock.patch.object(catalog, "_upsert_document", side_effect=observed):
            catalog.upsert_records(records)
        self.assertEqual(["a", "b"], order)
        with catalog.connect_readonly(catalog.catalog_path()) as connection:
            titles = dict(connection.execute("SELECT doc_id, title FROM document"))
            chunks = [
                row[0]
                for row in connection.execute("SELECT chunk_uid FROM chunk ORDER BY chunk_pk")
            ]
        self.assertEqual({"a": "a-last", "b": "b-only"}, titles)
        self.assertEqual(["a:0", "b:0", "a:1"], chunks)

    def test_replay_after_missed_state_save_converges_and_delete_only_accepts_duplicates(self) -> None:
        old = [record("a:0"), record("b:0")]
        new = [record("a:0", text="changed RFC20000")]
        catalog.upsert_records(old)
        catalog.upsert_records(new, delete_ids=["a:0", "b:0", "b:0", "missing"])
        first = logical_dump(catalog.catalog_path())
        catalog.upsert_records(new, delete_ids=["a:0", "b:0", "b:0", "missing"])
        self.assertEqual(first, logical_dump(catalog.catalog_path()))
        self.assertEqual(0, catalog.upsert_records([], delete_ids=["a:0", "a:0", "missing"]))
        self.assertEqual({}, catalog.fetch_rows_by_ids(["a:0", "b:0"]))

    def test_search_results_and_integrity_survive_replacement(self) -> None:
        records = [
            record("guide:0", doc_id="guide", text="alpha alpha RFC10026", title="Alpha Guide"),
            record("other:0", doc_id="other", text="alpha RFC20000", title="Other"),
        ]
        catalog.upsert_records(records)

        def search_dump() -> dict:
            return {
                "exact": catalog.exact_search("RFC10026", top_k=5),
                "bm25": catalog.bm25_search("alpha", top_k=5),
                "metadata": catalog.metadata_search("Alpha Guide", top_k=5),
                "fetch": catalog.fetch_rows_by_ids(["guide:0", "other:0"]),
            }

        before = search_dump()
        catalog.upsert_records(records, delete_ids=[item["id"] for item in records])
        after = search_dump()
        for lane in ("exact", "metadata"):
            self.assertEqual(before[lane], after[lane])
        self.assertEqual(before["fetch"], after["fetch"])
        self.assertEqual(
            [(row["rank"], row["id"], row["signals"], row["text"], row["metadata"]) for row in before["bm25"]],
            [(row["rank"], row["id"], row["signals"], row["text"], row["metadata"]) for row in after["bm25"]],
        )
        for old, new in zip(before["bm25"], after["bm25"], strict=True):
            self.assertLessEqual(abs(float(old["score"]) - float(new["score"])), 1e-12)
        with catalog.connect_readonly(catalog.catalog_path()) as connection:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_weak_acronym_document_df_threshold_multichunk_and_exclusions(self) -> None:
        eight = [record(f"d{i}:0", doc_id=f"d{i}", text="XYZ") for i in range(8)]
        catalog.upsert_records(eight + [record("multi:0", doc_id="multi", text="QRS ACS"), record("multi:1", doc_id="multi", text="QRS RAC", index=1)])
        with catalog.connect_readonly(catalog.catalog_path()) as connection:
            values = dict(connection.execute("SELECT canonical_value, document_frequency FROM identifier_term"))
        self.assertEqual(8, values["xyz"])
        self.assertEqual(1, values["qrs"])
        self.assertNotIn("acs", values)
        self.assertNotIn("rac", values)
        catalog.upsert_records([record("d8:0", doc_id="d8", text="XYZ")])
        with catalog.connect_readonly(catalog.catalog_path()) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM identifier_term WHERE canonical_value='xyz'").fetchone())
            self.assertIsNotNone(connection.execute("SELECT 1 FROM identifier_suppressed WHERE canonical_value='xyz'").fetchone())
        catalog.upsert_records([record("again:0", doc_id="again", text="XYZ")])
        self.assertEqual([], catalog.exact_search("XYZ", top_k=20))

    def test_legacy_catalog_normalizes_once_then_only_impacted_terms(self) -> None:
        catalog.upsert_records([record("a:0", text="RFC10026"), record("b:0", text="RFC20000")])
        with catalog.connect() as connection:
            connection.execute("DELETE FROM database_meta WHERE key = ?", (catalog._WRITE_NORMALIZATION_META,))
            connection.execute("UPDATE identifier_term SET document_frequency=99")
        catalog.upsert_records([record("c:0", text="RFC30000")])
        with catalog.connect_readonly(catalog.catalog_path()) as connection:
            frequencies = dict(connection.execute("SELECT canonical_value, document_frequency FROM identifier_term"))
            marker = connection.execute("SELECT value FROM database_meta WHERE key = ?", (catalog._WRITE_NORMALIZATION_META,)).fetchone()[0]
        self.assertEqual(1, frequencies["rfc10026"])
        self.assertEqual(1, frequencies["rfc20000"])
        self.assertEqual(1, frequencies["rfc30000"])
        self.assertEqual(3, frequencies["section-0"])
        self.assertEqual("1", marker)
        with catalog.connect() as connection:
            connection.execute("UPDATE identifier_term SET document_frequency=77 WHERE canonical_value='rfc10026'")
        catalog.upsert_records([record("d:0", text="RFC40000")])
        with catalog.connect_readonly(catalog.catalog_path()) as connection:
            unchanged = connection.execute("SELECT document_frequency FROM identifier_term WHERE canonical_value='rfc10026'").fetchone()[0]
        self.assertEqual(77, unchanged)

    def test_legacy_marker_rolls_back_with_failed_first_upsert(self) -> None:
        catalog.upsert_records([record("old:0", text="RFC10026")])
        with catalog.connect() as connection:
            connection.execute("DELETE FROM database_meta WHERE key = ?", (catalog._WRITE_NORMALIZATION_META,))
            connection.execute("UPDATE identifier_term SET document_frequency=99")
        with mock.patch.object(catalog, "_upsert_document", side_effect=RuntimeError("document")):
            with self.assertRaisesRegex(RuntimeError, "document"):
                catalog.upsert_records([record("new:0", text="RFC20000")])
        with catalog.connect_readonly(catalog.catalog_path()) as connection:
            marker = connection.execute("SELECT value FROM database_meta WHERE key = ?", (catalog._WRITE_NORMALIZATION_META,)).fetchone()
            frequency = connection.execute("SELECT document_frequency FROM identifier_term WHERE canonical_value='rfc10026'").fetchone()[0]
        self.assertIsNone(marker)
        self.assertEqual(99, frequency)

    def test_each_transaction_failure_rolls_back_and_rerun_converges(self) -> None:
        initial = [record("a:0", text="RFC10026")]
        replacement = [record("a:0", text="RFC20000")]
        catalog.upsert_records(initial)
        original_dump = logical_dump(catalog.catalog_path())
        targets = (
            "_stage_catalog_write",
            "_delete_staged_chunks",
            "_upsert_document",
            "_insert_identifiers",
            "_refresh_staged_identifier_stats",
        )
        for target in targets:
            with self.subTest(target=target):
                with mock.patch.object(catalog, target, side_effect=RuntimeError(target)):
                    with self.assertRaisesRegex(RuntimeError, target):
                        catalog.upsert_records(replacement, delete_ids=["a:0"])
                self.assertEqual(original_dump, logical_dump(catalog.catalog_path()))
        original_set_meta = catalog._set_meta

        def fail_precommit(connection, key, value):
            if key == "updated_at":
                raise RuntimeError("precommit")
            return original_set_meta(connection, key, value)

        with mock.patch.object(catalog, "_set_meta", side_effect=fail_precommit):
            with self.assertRaisesRegex(RuntimeError, "precommit"):
                catalog.upsert_records(replacement, delete_ids=["a:0"])
        self.assertEqual(original_dump, logical_dump(catalog.catalog_path()))
        catalog.upsert_records(replacement, delete_ids=["a:0"])
        final = logical_dump(catalog.catalog_path())
        catalog.upsert_records(replacement, delete_ids=["a:0"])
        self.assertEqual(final, logical_dump(catalog.catalog_path()))

    def test_incremental_batch_wires_one_catalog_transaction(self) -> None:
        source = Path(incremental.__file__).read_text(encoding="utf-8")
        flush = source[source.index("def _flush_batch("):source.index("def _record_error(")]
        self.assertNotIn("delete_catalog_chunks(", flush)
        self.assertIn("upsert_catalog_records(records, delete_ids=delete_targets)", flush)


if __name__ == "__main__":
    unittest.main()
