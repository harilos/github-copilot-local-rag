from __future__ import annotations

import builtins
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from software_rag_tool import extractors
from software_rag_tool.openxml_text import (
    OpenXmlExtractionError,
    extract_docx_text,
    extract_pptx_slide_texts,
)


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PRESENTATION_NS = (
    "http://schemas.openxmlformats.org/presentationml/2006/main"
)
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
PACKAGE_REL_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
SLIDE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
)


class OpenXmlDirectExtractionTests(unittest.TestCase):
    def test_docx_reads_runs_hyperlinks_controls_and_tables(self) -> None:
        document_xml = f"""\
<w:document xmlns:w="{WORD_NS}" xmlns:r="{OFFICE_REL_NS}">
  <w:body>
    <w:p>
      <w:r><w:t xml:space="preserve">日本語 English 42 ! 😀 </w:t></w:r>
      <w:r><w:t>RUN</w:t></w:r>
      <w:hyperlink r:id="rId1"><w:r><w:t>-LINK</w:t></w:r></w:hyperlink>
      <w:r><w:tab/><w:t>TAB</w:t><w:br/><w:t>BREAK</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p><w:r><w:t>Cell A</w:t></w:r></w:p>
          <w:p><w:r><w:t>Cell A2</w:t></w:r></w:p>
        </w:tc>
        <w:tc><w:p/></w:tc>
        <w:tc><w:p><w:r><w:t>Cell C</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Row2 A</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Row2 B</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mixed.docx"
            _write_package(path, {"word/document.xml": document_xml})
            with _reject_office_library_imports():
                raw_text = extract_docx_text(path)
                sections = extractors.extract_sections(path)

        self.assertIn("RUN-LINK\tTAB\nBREAK", raw_text)
        self.assertEqual(1, len(sections))
        self.assertEqual("mixed.docx", sections[0].title)
        self.assertEqual(
            "日本語 English 42 ! 😀 RUN-LINK TAB\nBREAK\n"
            "Second paragraph\nCell A\nCell A2 | Cell C\nRow2 A | Row2 B",
            sections[0].text,
        )

    def test_pptx_uses_presentation_order_and_reads_drawing_controls(self) -> None:
        presentation_xml = f"""\
<p:presentation xmlns:p="{PRESENTATION_NS}" xmlns:r="{OFFICE_REL_NS}">
  <p:sldIdLst>
    <p:sldId id="256" r:id="rIdSecond"/>
    <p:sldId id="257" r:id="rIdFirst"/>
  </p:sldIdLst>
</p:presentation>
"""
        relationships_xml = f"""\
<Relationships xmlns="{PACKAGE_REL_NS}">
  <Relationship Id="rIdFirst" Type="{SLIDE_REL_TYPE}" Target="slides/slide1.xml"/>
  <Relationship Id="rIdSecond" Type="{SLIDE_REL_TYPE}" Target="slides/slide2.xml"/>
</Relationships>
"""
        slide_one = _slide_xml("FILE-ONE", "Shape two")
        slide_two = _slide_xml(
            "日本語 English 42 ! 😀 ",
            "RUN-LINK",
            controls=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ordered.pptx"
            _write_package(
                path,
                {
                    "ppt/presentation.xml": presentation_xml,
                    "ppt/_rels/presentation.xml.rels": relationships_xml,
                    "ppt/slides/slide1.xml": slide_one,
                    "ppt/slides/slide2.xml": slide_two,
                },
            )
            with _reject_office_library_imports():
                raw_slides = extract_pptx_slide_texts(path)
                sections = extractors.extract_sections(path)

        self.assertIn("😀 \tTAB\nBREAK", raw_slides[0])
        self.assertEqual(["Slide 1", "Slide 2"], [item.title for item in sections])
        self.assertEqual(
            "日本語 English 42 ! 😀 TAB\nBREAK\nRUN-LINK",
            sections[0].text,
        )
        self.assertEqual("FILE-ONE\nShape two", sections[1].text)

    def test_non_zip_packages_fail_without_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for extension in (".docx", ".pptx"):
                with self.subTest(extension=extension):
                    path = root / f"broken{extension}"
                    path.write_bytes(b"not a zip package")
                    with (
                        _reject_office_library_imports(),
                        self.assertRaisesRegex(
                            OpenXmlExtractionError,
                            f"invalid {extension[1:].upper()} package",
                        ),
                    ):
                        extractors.extract_sections(path)

    def test_missing_required_parts_and_malformed_xml_fail(self) -> None:
        cases = (
            ("missing.docx", {}, "missing required Open XML part"),
            (
                "malformed.docx",
                {"word/document.xml": "<w:document"},
                "malformed XML",
            ),
            ("missing.pptx", {}, "missing required Open XML part"),
            (
                "malformed.pptx",
                {"ppt/presentation.xml": "<p:presentation"},
                "malformed XML",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for filename, parts, message in cases:
                with self.subTest(filename=filename):
                    path = root / filename
                    _write_package(path, parts)
                    with self.assertRaisesRegex(OpenXmlExtractionError, message):
                        extractors.extract_sections(path)

    def test_pptx_relationship_and_slide_failures_are_explicit(self) -> None:
        presentation_xml = f"""\
<p:presentation xmlns:p="{PRESENTATION_NS}" xmlns:r="{OFFICE_REL_NS}">
  <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
</p:presentation>
"""
        empty_relationships = f'<Relationships xmlns="{PACKAGE_REL_NS}"/>'
        missing_slide_relationships = f"""\
<Relationships xmlns="{PACKAGE_REL_NS}">
  <Relationship Id="rId1" Type="{SLIDE_REL_TYPE}" Target="slides/missing.xml"/>
</Relationships>
"""
        external_relationships = f"""\
<Relationships xmlns="{PACKAGE_REL_NS}">
  <Relationship Id="rId1" Type="{SLIDE_REL_TYPE}" Target="https://example.invalid/slide.xml" TargetMode="External"/>
</Relationships>
"""
        cases = (
            (empty_relationships, "missing presentation relationship"),
            (missing_slide_relationships, "missing required Open XML part"),
            (external_relationships, "external slide relationship"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (relationships, message) in enumerate(cases):
                with self.subTest(message=message):
                    path = root / f"relationship-{index}.pptx"
                    _write_package(
                        path,
                        {
                            "ppt/presentation.xml": presentation_xml,
                            "ppt/_rels/presentation.xml.rels": relationships,
                        },
                    )
                    with self.assertRaisesRegex(OpenXmlExtractionError, message):
                        extractors.extract_sections(path)


def _write_package(path: Path, parts: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for part_name, payload in parts.items():
            package.writestr(part_name, payload.encode("utf-8"))


def _slide_xml(
    first: str,
    second: str,
    *,
    controls: bool = False,
) -> str:
    first_runs = f"<a:r><a:t>{first}</a:t></a:r>"
    if controls:
        first_runs += (
            "<a:tab/><a:r><a:t>TAB</a:t></a:r>"
            "<a:br/><a:r><a:t>BREAK</a:t></a:r>"
        )
    return f"""\
<p:sld xmlns:p="{PRESENTATION_NS}" xmlns:a="{DRAWING_NS}">
  <p:cSld><p:spTree>
    <p:sp><p:txBody><a:p>{first_runs}</a:p></p:txBody></p:sp>
    <p:sp><p:txBody><a:p><a:r><a:t>{second}</a:t></a:r></a:p></p:txBody></p:sp>
  </p:spTree></p:cSld>
</p:sld>
"""


def _reject_office_library_imports() -> mock._patch:
    real_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "docx" or name.startswith("docx."):
            raise AssertionError("production DOCX extraction imported python-docx")
        if name == "pptx" or name.startswith("pptx."):
            raise AssertionError("production PPTX extraction imported python-pptx")
        return real_import(name, *args, **kwargs)

    return mock.patch("builtins.__import__", side_effect=guarded_import)


if __name__ == "__main__":
    unittest.main()
