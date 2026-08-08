from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree


class OpenXmlExtractionError(RuntimeError):
    """Raised when a required Open XML package part cannot be read."""


_WORDPROCESSINGML_NS = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
)
_PRESENTATIONML_NS = (
    "http://schemas.openxmlformats.org/presentationml/2006/main"
)
_DRAWINGML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_OFFICE_RELATIONSHIPS_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_RELATIONSHIPS_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)

_DOCX_DOCUMENT_PART = "word/document.xml"
_PPTX_PRESENTATION_PART = "ppt/presentation.xml"
_PPTX_PRESENTATION_RELS_PART = "ppt/_rels/presentation.xml.rels"


def extract_docx_text(path: Path) -> str:
    """Read top-level DOCX paragraphs and tables directly from the package."""

    try:
        with zipfile.ZipFile(path) as package:
            root = _read_xml_part(package, _DOCX_DOCUMENT_PART)
    except zipfile.BadZipFile as exc:
        raise OpenXmlExtractionError(f"invalid DOCX package: {path}") from exc

    body = root.find(_qualified(_WORDPROCESSINGML_NS, "body"))
    if body is None:
        raise OpenXmlExtractionError(
            f"missing w:body in required Open XML part: {_DOCX_DOCUMENT_PART}"
        )

    paragraphs: list[str] = []
    table_rows: list[str] = []
    paragraph_tag = _qualified(_WORDPROCESSINGML_NS, "p")
    table_tag = _qualified(_WORDPROCESSINGML_NS, "tbl")
    for child in body:
        if child.tag == paragraph_tag:
            text = _word_paragraph_text(child)
            if text.strip():
                paragraphs.append(text)
        elif child.tag == table_tag:
            table_rows.extend(_word_table_rows(child))

    return "\n".join([*paragraphs, *table_rows])


def extract_pptx_slide_texts(path: Path) -> list[str]:
    """Read PPTX slide text in the order stored by the presentation."""

    try:
        with zipfile.ZipFile(path) as package:
            presentation = _read_xml_part(package, _PPTX_PRESENTATION_PART)
            relationships = _read_xml_part(
                package,
                _PPTX_PRESENTATION_RELS_PART,
            )
            relationship_by_id = _presentation_relationships(relationships)
            slide_parts = _ordered_slide_parts(
                presentation,
                relationship_by_id,
            )
            return [
                _pptx_slide_text(_read_xml_part(package, part_name))
                for part_name in slide_parts
            ]
    except zipfile.BadZipFile as exc:
        raise OpenXmlExtractionError(f"invalid PPTX package: {path}") from exc


def _read_xml_part(
    package: zipfile.ZipFile,
    part_name: str,
) -> ElementTree.Element:
    try:
        payload = package.read(part_name)
    except KeyError as exc:
        raise OpenXmlExtractionError(
            f"missing required Open XML part: {part_name}"
        ) from exc
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise OpenXmlExtractionError(
            f"malformed XML in Open XML part: {part_name}"
        ) from exc


def _word_paragraph_text(paragraph: ElementTree.Element) -> str:
    text_tag = _qualified(_WORDPROCESSINGML_NS, "t")
    tab_tag = _qualified(_WORDPROCESSINGML_NS, "tab")
    break_tags = {
        _qualified(_WORDPROCESSINGML_NS, "br"),
        _qualified(_WORDPROCESSINGML_NS, "cr"),
    }
    parts: list[str] = []
    for element in paragraph.iter():
        if element.tag == text_tag:
            parts.append(element.text or "")
        elif element.tag == tab_tag:
            parts.append("\t")
        elif element.tag in break_tags:
            parts.append("\n")
    return "".join(parts)


def _word_table_rows(table: ElementTree.Element) -> list[str]:
    row_tag = _qualified(_WORDPROCESSINGML_NS, "tr")
    cell_tag = _qualified(_WORDPROCESSINGML_NS, "tc")
    paragraph_tag = _qualified(_WORDPROCESSINGML_NS, "p")
    rows: list[str] = []
    for row in table.findall(row_tag):
        cells: list[str] = []
        for cell in row.findall(cell_tag):
            paragraphs = [
                _word_paragraph_text(paragraph)
                for paragraph in cell.findall(paragraph_tag)
            ]
            cell_text = "\n".join(
                text for text in paragraphs if text.strip()
            ).strip()
            if cell_text:
                cells.append(cell_text)
        if cells:
            rows.append(" | ".join(cells))
    return rows


def _presentation_relationships(
    relationships: ElementTree.Element,
) -> dict[str, ElementTree.Element]:
    relationship_tag = _qualified(_PACKAGE_RELATIONSHIPS_NS, "Relationship")
    return {
        relationship_id: relationship
        for relationship in relationships.findall(relationship_tag)
        if (relationship_id := relationship.get("Id"))
    }


def _ordered_slide_parts(
    presentation: ElementTree.Element,
    relationships: dict[str, ElementTree.Element],
) -> list[str]:
    slide_id_list = presentation.find(
        f".//{_qualified(_PRESENTATIONML_NS, 'sldIdLst')}"
    )
    if slide_id_list is None:
        return []

    relationship_id_attribute = _qualified(_OFFICE_RELATIONSHIPS_NS, "id")
    slide_id_tag = _qualified(_PRESENTATIONML_NS, "sldId")
    parts: list[str] = []
    for slide_id in slide_id_list.findall(slide_id_tag):
        relationship_id = slide_id.get(relationship_id_attribute)
        if not relationship_id:
            raise OpenXmlExtractionError(
                "presentation slide entry is missing its relationship id"
            )
        relationship = relationships.get(relationship_id)
        if relationship is None:
            raise OpenXmlExtractionError(
                f"missing presentation relationship: {relationship_id}"
            )
        if relationship.get("TargetMode", "").lower() == "external":
            raise OpenXmlExtractionError(
                f"external slide relationship is not supported: {relationship_id}"
            )
        relationship_type = relationship.get("Type", "")
        if not relationship_type.endswith("/slide"):
            raise OpenXmlExtractionError(
                f"presentation relationship is not a slide: {relationship_id}"
            )
        target = relationship.get("Target")
        if not target:
            raise OpenXmlExtractionError(
                f"slide relationship has no target: {relationship_id}"
            )
        parts.append(_resolve_package_part(_PPTX_PRESENTATION_PART, target))
    return parts


def _resolve_package_part(base_part: str, target: str) -> str:
    normalized_target = target.replace("\\", "/")
    if normalized_target.startswith("/"):
        resolved = posixpath.normpath(normalized_target.lstrip("/"))
    else:
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(base_part), normalized_target)
        )
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise OpenXmlExtractionError(
            f"slide relationship escapes the package: {target}"
        )
    return resolved


def _pptx_slide_text(slide: ElementTree.Element) -> str:
    paragraph_tag = _qualified(_DRAWINGML_NS, "p")
    paragraphs = [
        _drawing_paragraph_text(paragraph)
        for paragraph in slide.iter(paragraph_tag)
    ]
    return "\n".join(text for text in paragraphs if text.strip())


def _drawing_paragraph_text(paragraph: ElementTree.Element) -> str:
    text_tag = _qualified(_DRAWINGML_NS, "t")
    tab_tag = _qualified(_DRAWINGML_NS, "tab")
    break_tag = _qualified(_DRAWINGML_NS, "br")
    parts: list[str] = []
    for element in paragraph.iter():
        if element.tag == text_tag:
            parts.append(element.text or "")
        elif element.tag == tab_tag:
            parts.append("\t")
        elif element.tag == break_tag:
            parts.append("\n")
    return "".join(parts)


def _qualified(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"
