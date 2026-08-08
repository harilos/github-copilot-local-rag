from __future__ import annotations

import codecs
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from software_rag_tool import catalog, extractors, records, retrieval
from software_rag_tool.embeddings import DocumentTokenBudget


class _CharacterTokenizer:
    def __call__(
        self,
        text: str,
        *,
        add_special_tokens: bool = True,
        **_kwargs: object,
    ) -> dict[str, list[int]]:
        tokens = list(range(len(text)))
        if add_special_tokens:
            tokens = [10_001, *tokens, 10_002]
        return {"input_ids": tokens}


_TEST_TOKEN_BUDGET = DocumentTokenBudget(
    tokenizer=_CharacterTokenizer(),
    document_prefix="doc: ",
    tokenizer_name="explicit-character-test-double",
    target_tokens=320,
    max_tokens=384,
)


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

    def test_control_characters_fail_for_every_decode_path(self) -> None:
        cases = (
            b"abc\x00def",
            codecs.BOM_UTF8 + b"abc\x00def",
            "abc\x00def".encode("utf-16"),
            "あ\x00".encode("cp932"),
        )
        for payload in cases:
            with self.subTest(prefix=payload[:4]):
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

    def test_docx_path_is_independent_from_legacy_converter(self) -> None:
        document_xml = """\
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>DOCX 正常系</w:t></w:r></w:p></w:body>
</w:document>
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "control.docx"
            with zipfile.ZipFile(path, "w") as package:
                package.writestr("word/document.xml", document_xml)
            with mock.patch.object(
                extractors,
                "_convert_with_libreoffice",
                side_effect=AssertionError("DOCX used the legacy converter"),
            ):
                sections = extractors.extract_sections(path)

        self.assertEqual("DOCX 正常系", sections[0].text)


class LegacyDocAcceptanceTests(unittest.TestCase):
    def test_three_cp932_docs_preserve_phrases_and_hit_top_five(self) -> None:
        fixtures = {
            "alpha.doc": "移行の承認合言葉は青空航路です。管理番号 DOC-ALPHA-731。",
            "beta.doc": "例外の申請合言葉は白銀灯台です。管理番号 DOC-BETA-842。",
            "gamma.doc": "復旧の確認合言葉は紅葉渓谷です。管理番号 DOC-GAMMA-953。",
        }
        queries = {
            "青空航路の承認内容は？": "input/alpha.doc",
            "白銀灯台の申請内容は？": "input/beta.doc",
            "紅葉渓谷の復旧内容は？": "input/gamma.doc",
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
                            document_token_budget=_TEST_TOKEN_BUDGET,
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
