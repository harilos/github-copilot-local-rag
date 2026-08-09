from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool import catalog, document_extensions, extractors, records
from software_rag_tool.retrieval import _matching_strong_exact_rows


REPRESENTATIVE_FILES = (
    ("ReleaseNotes.TXT", "ASCII release notes", "releasenotes.txt"),
    ("運用手順書.md", "Unicode operation guide", "運用手順書"),
    ("architecture.puml", "@startuml\nAlice -> Bob\n@enduml", "architecture.puml"),
    ("worker.py", "print('worker')", "worker.py"),
    ("settings.json", '{"enabled": true}', "settings.json"),
)


class IngestionLayerInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "documents"
        self.root.mkdir()
        for name, body, _query in REPRESENTATIVE_FILES:
            (self.root / name).write_text(body, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_canonical_registry_enumeration_and_extraction_agree(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                document_extensions.FILE_SELECTION_ENV:
                    document_extensions.FILE_SELECTION_ALL
            },
            clear=False,
        ):
            enumerated = {
                path.name: path for path in records.iter_input_files(self.root)
            }

        for name, body, _query in REPRESENTATIVE_FILES:
            with self.subTest(name=name):
                path = self.root / name
                self.assertIn(path.suffix.lower(), extractors.SUPPORTED_EXTENSIONS)
                self.assertIn(name, enumerated)
                sections = extractors.extract_sections(path)
                self.assertTrue(sections)
                extracted = "\n".join(section.text for section in sections)
                self.assertIn(body.splitlines()[0], extracted)

    def test_document_only_selection_is_derived_from_product_registry(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                document_extensions.FILE_SELECTION_ENV:
                    document_extensions.FILE_SELECTION_DOCUMENTS
            },
            clear=False,
        ):
            actual = {path.name for path in records.iter_input_files(self.root)}

        expected = {
            name
            for name, _body, _query in REPRESENTATIVE_FILES
            if Path(name).suffix.lower()
            in document_extensions.DOCUMENT_ONLY_EXTENSIONS
        }
        self.assertEqual(expected, actual)

    def test_ascii_and_unicode_exact_candidates_keep_expected_identity(self) -> None:
        catalog_path = Path(self.temporary.name) / "catalog.sqlite"
        expected_ids: dict[str, str] = {}
        with catalog.connect(catalog_path) as connection:
            for index, (name, body, query) in enumerate(REPRESENTATIVE_FILES):
                record_id = f"record-{index}"
                expected_ids[query] = record_id
                catalog._insert_record(
                    connection,
                    {
                        "id": record_id,
                        "text": body,
                        "metadata": {
                            "path": f"documents/{name}",
                            "title": Path(name).stem,
                        },
                    },
                    "now",
                )
            connection.commit()

        for _name, _body, query in REPRESENTATIVE_FILES:
            with self.subTest(query=query):
                rows = catalog.exact_search(query, top_k=20, path=catalog_path)
                expected_id = expected_ids[query]
                self.assertIn(expected_id, {str(row["id"]) for row in rows})
                verified = _matching_strong_exact_rows(query, rows)
                self.assertIn(
                    expected_id,
                    {str(row["id"]) for row in verified},
                )


if __name__ == "__main__":
    unittest.main()
