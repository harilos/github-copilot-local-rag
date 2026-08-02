from __future__ import annotations

import codecs
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool import catalog, extractors, records, retrieval


class ConverterOutputDecodingTests(unittest.TestCase):
    def test_known_bom_takes_priority_and_is_removed(self) -> None:
        cases = (
            (codecs.BOM_UTF8 + "第一正本句".encode("utf-8"), "第一正本句"),
            ("第一正本句".encode("utf-16"), "第一正本句"),
            ("第一正本句".encode("utf-32"), "第一正本句"),
        )
        for payload, expected in cases:
            with self.subTest(prefix=payload[:4]):
                self.assertEqual(
                    expected,
                    extractors._decode_converter_output(payload),
                )

    def test_strict_utf8_is_accepted(self) -> None:
        payload = "第二正本句".encode("utf-8")

        self.assertEqual(
            "第二正本句",
            extractors._decode_converter_output(payload),
        )

    def test_valid_cp932_is_accepted_after_utf8(self) -> None:
        payload = "第三正本句".encode("cp932")

        self.assertEqual(
            "第三正本句",
            extractors._decode_converter_output(payload),
        )

    def test_unknown_bytes_fail_explicitly(self) -> None:
        for payload in (b"\x81", b"\x80"):
            with self.subTest(payload=payload):
                with self.assertRaises(extractors.ConverterOutputDecodeError):
                    extractors._decode_converter_output(payload)

    def test_textutil_decodes_converter_stdout_bytes(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["textutil"],
            returncode=0,
            stdout="第三正本句".encode("cp932"),
            stderr=b"",
        )
        with (
            mock.patch.object(extractors.shutil, "which", return_value="textutil"),
            mock.patch.object(extractors.subprocess, "run", return_value=completed) as run,
        ):
            text = extractors._convert_doc_with_textutil(Path("fixture.doc"))

        self.assertEqual("第三正本句", text)
        self.assertNotIn("text", run.call_args.kwargs)

    def test_libreoffice_decodes_generated_file_bytes(self) -> None:
        def run_converter(command: list[str], **_kwargs: object) -> object:
            output_dir = Path(command[command.index("--outdir") + 1])
            (output_dir / "fixture.txt").write_bytes(
                "第三正本句".encode("cp932")
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(extractors, "_find_libreoffice", return_value="soffice"),
            mock.patch.object(extractors.subprocess, "run", side_effect=run_converter),
        ):
            text = extractors._convert_with_libreoffice(Path("fixture.doc"))

        self.assertEqual("第三正本句", text)

    def test_libreoffice_invalid_generated_bytes_fail_explicitly(self) -> None:
        def run_converter(command: list[str], **_kwargs: object) -> object:
            output_dir = Path(command[command.index("--outdir") + 1])
            (output_dir / "fixture.txt").write_bytes(b"\x81")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            mock.patch.object(extractors, "_find_libreoffice", return_value="soffice"),
            mock.patch.object(extractors.subprocess, "run", side_effect=run_converter),
            self.assertRaises(extractors.ConverterOutputDecodeError),
        ):
            extractors._convert_with_libreoffice(Path("fixture.doc"))

    def test_markdown_utf8_path_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "control.md"
            path.write_text("Markdown 正常系", encoding="utf-8")
            sections = extractors.extract_sections(path)

        self.assertEqual("Markdown 正常系", sections[0].text)

    def test_docx_path_is_unchanged(self) -> None:
        fake_docx = types.ModuleType("docx")
        fake_docx.Document = lambda _path: types.SimpleNamespace(
            paragraphs=[types.SimpleNamespace(text="DOCX 正常系")],
            tables=[],
        )
        with mock.patch.dict(sys.modules, {"docx": fake_docx}):
            sections = extractors.extract_sections(Path("control.docx"))

        self.assertEqual("DOCX 正常系", sections[0].text)


class LegacyDocAcceptanceTests(unittest.TestCase):
    def test_three_cp932_docs_preserve_phrases_and_hit_top_five(self) -> None:
        fixtures = {
            "alpha.doc": "移行の承認番号は DOC-ALPHA-731 です。",
            "beta.doc": "例外の申請番号は DOC-BETA-842 です。",
            "gamma.doc": "復旧の確認番号は DOC-GAMMA-953 です。",
        }
        queries = {
            "DOC-ALPHA-731 の承認内容は？": "input/alpha.doc",
            "DOC-BETA-842 の申請内容は？": "input/beta.doc",
            "DOC-GAMMA-953 の復旧内容は？": "input/gamma.doc",
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "input"
            root.mkdir()
            for filename in fixtures:
                # The legacy binary is only passed to the converter; it is never decoded.
                (root / filename).write_bytes(b"\xd0\xcf\x11\xe0legacy-office")

            def run_textutil(command: list[str], **_kwargs: object) -> object:
                filename = Path(command[-1]).name
                return subprocess.CompletedProcess(
                    command,
                    0,
                    fixtures[filename].encode("cp932"),
                    b"",
                )

            with (
                mock.patch.object(extractors.platform, "system", return_value="Darwin"),
                mock.patch.object(extractors.shutil, "which", return_value="textutil"),
                mock.patch.object(extractors.subprocess, "run", side_effect=run_textutil),
                mock.patch.dict(
                    os.environ,
                    {"RAG_OUTPUT_ROOT": str(Path(temporary) / "db")},
                    clear=False,
                ),
            ):
                indexed = []
                for filename in fixtures:
                    indexed.extend(
                        records.build_records_for_file(
                            root,
                            root / filename,
                            source_id="legacy-doc-fixture",
                        )
                    )
                catalog.upsert_records(indexed)
                hits = 0
                for question, expected_path in queries.items():
                    result = retrieval.hybrid_query(
                        question,
                        top_k=5,
                        use_dense=False,
                    )
                    if any(
                        (row.get("metadata") or {}).get("path") == expected_path
                        for row in result
                    ):
                        hits += 1

        extracted = "\n".join(str(row["text"]) for row in indexed)
        self.assertNotIn("\ufffd", extracted)
        self.assertTrue(all(phrase in extracted for phrase in fixtures.values()))
        self.assertEqual(3, hits, result)


if __name__ == "__main__":
    unittest.main()
