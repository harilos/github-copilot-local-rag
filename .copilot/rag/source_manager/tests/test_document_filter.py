from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from source_manager import providers, runner
from source_manager.document_filter import (
    FILE_SELECTION_ALL,
    FILE_SELECTION_DOCUMENTS,
    FILE_SELECTION_KEY,
)
from software_rag_tool import document_extensions, extractors, records


class DocumentFilterTests(unittest.TestCase):
    def test_file_provider_defaults_to_all_supported(self) -> None:
        normalized = providers.validate_provider_config(
            "github",
            {"repository_url": "https://github.com/example/project.git"},
        )
        self.assertEqual(FILE_SELECTION_ALL, normalized[FILE_SELECTION_KEY])

    def test_documents_only_is_persisted_for_sharepoint(self) -> None:
        normalized = providers.validate_provider_config(
            "sharepoint",
            {
                "root_env": "LOCAL_RAG_SHAREPOINT_ROOT",
                "relative_path": "Team/Documents",
                FILE_SELECTION_KEY: FILE_SELECTION_DOCUMENTS,
            },
        )
        self.assertEqual(FILE_SELECTION_DOCUMENTS, normalized[FILE_SELECTION_KEY])
        self.assertEqual("Team/Documents", normalized["relative_path"])

    def test_redmine_does_not_accept_file_selection(self) -> None:
        with self.assertRaises(Exception):
            providers.validate_provider_config(
                "redmine",
                {
                    "project_url": "https://redmine.example/projects/example",
                    FILE_SELECTION_KEY: FILE_SELECTION_DOCUMENTS,
                },
            )

    def test_documents_only_keeps_astah_and_plantuml_but_not_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (
                "guide.md",
                "diagram.pu",
                "diagram.puml",
                "model.asta",
                "application.py",
                "settings.yaml",
            ):
                (root / name).write_bytes(b"example document text")

            with mock.patch.dict(
                os.environ,
                {
                    document_extensions.FILE_SELECTION_ENV:
                        document_extensions.FILE_SELECTION_DOCUMENTS
                },
                clear=False,
            ):
                discovered = [path.name for path in records.iter_input_files(root)]

        self.assertEqual(
            ["diagram.pu", "diagram.puml", "guide.md", "model.asta"],
            discovered,
        )

    def test_astah_fallback_indexes_filename_and_readable_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "system-model.asta"
            path.write_bytes(
                b"binary\x00OrderService\x00Customer Account\x00Sequence Diagram"
            )
            sections = extractors.extract_sections(
                path,
                chunk_max_chars=500,
                chunk_overlap=20,
            )
        text = "\n".join(section.text for section in sections)
        self.assertIn("system-model.asta", text)
        self.assertIn("OrderService", text)
        self.assertIn("Sequence Diagram", text)

    def test_document_source_uses_document_only_add_entry_point(self) -> None:
        source_key = "src_docs-0123456789ab"
        commands: list[list[str]] = []

        def command_runner(arguments: list[str]) -> object:
            commands.append(list(arguments))
            summary = {
                "operation": "add",
                "source_id": source_key,
                "file_count": 0,
                "indexed_files": 0,
                "skipped_files": 0,
                "error_files": 0,
                "upserted_records": 0,
                "deleted_records": 0,
            }
            import json

            return types.SimpleNamespace(
                returncode=0,
                stdout="@@LOCAL_RAG_RESULT_V1@@" + json.dumps(summary),
                stderr="",
            )

        runner._execute_add(
            db_root=Path("/tmp/example-rag"),
            source={
                "local_source_key": source_key,
                "fetch": {FILE_SELECTION_KEY: FILE_SELECTION_DOCUMENTS},
            },
            work=Path("/tmp/source-work"),
            python_executable=Path("/usr/bin/python3"),
            rag_root=Path("/tmp/rag"),
            command_runner=command_runner,
            progress_callback=None,
        )

        self.assertEqual(1, len(commands))
        self.assertIn("add_data_documents_only.py", commands[0][1])
        self.assertNotIn("add_data.py", commands[0][1])


if __name__ == "__main__":
    unittest.main()
