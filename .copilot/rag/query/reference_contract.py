from __future__ import annotations

import copy
import re
from pathlib import PurePosixPath
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit


_RESULT_LISTS = (
    "evidence",
    "contexts",
    "background_context",
    "related_context",
    "document_results",
    "_result_detail_items",
    "expanded_items",
)
_PATCH_MARKER = "_local_rag_reference_contract_v1"
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def preferred_reference_url(item: dict[str, Any]) -> tuple[str, str]:
    """Return exactly one safe answer-facing URL and its source kind."""
    for field, kind in (
        ("source_permalink", "permalink"),
        ("source_url", "source_url"),
    ):
        candidate = str(item.get(field) or "").strip()
        if _safe_http_url(candidate):
            return candidate, kind
    return "", "none"


def reference_metadata(item: dict[str, Any]) -> dict[str, str]:
    """Build a copy-ready reference without constraining answer prose."""
    path = _stored_path(item)
    filename = _reference_filename(item, path)
    url, url_kind = preferred_reference_url(item)
    if url:
        markdown = f"[{_escape_markdown_label(filename)}]({_markdown_url(url)})"
    elif path and path != filename:
        markdown = f"{filename} — `{_escape_code_span(path)}`"
    else:
        markdown = filename
    return {
        "filename": filename,
        "path": path,
        "url": url,
        "url_kind": url_kind,
        "markdown": markdown,
    }


def add_reference_metadata(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return item
    item["reference"] = reference_metadata(item)
    return item


def add_reference_metadata_to_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Add answer-facing reference projections to every public result item."""
    if not isinstance(payload, dict):
        return payload
    for key in _RESULT_LISTS:
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict):
                add_reference_metadata(item)
    initial = payload.get("initial_response")
    if isinstance(initial, dict):
        rules = initial.setdefault("response_rules", {})
        if isinstance(rules, dict):
            rules.update(
                {
                    "material_citations_required": True,
                    "body_urls_forbidden": True,
                    "references_footer_required": True,
                    "one_url_per_reference": True,
                    "preferred_reference_field": "reference.markdown",
                }
            )
    return payload


def install_result_bundle_reference_contract(
    module: ModuleType | Any | None = None,
) -> Any:
    """Patch the public bundle projection without changing retrieval results."""
    if module is None:
        import result_bundle as module  # type: ignore[no-redef]
    if getattr(module, _PATCH_MARKER, False):
        return module

    original_build = module.build_initial_summary
    original_expanded = module._expanded_item

    def build_initial_summary(*args: Any, **kwargs: Any) -> Any:
        summary, details = original_build(*args, **kwargs)
        add_reference_metadata_to_payload(summary)
        for _item_id, _kind, detail in details:
            if isinstance(detail, dict):
                add_reference_metadata(detail)
        return summary, details

    def expanded_item(
        detail: dict[str, Any],
        *,
        detail_level: str,
    ) -> dict[str, Any]:
        expanded = original_expanded(detail, detail_level=detail_level)
        if isinstance(detail.get("reference"), dict):
            expanded["reference"] = copy.deepcopy(detail["reference"])
        else:
            add_reference_metadata(expanded)
        return expanded

    module.build_initial_summary = build_initial_summary
    module._expanded_item = expanded_item
    setattr(module, _PATCH_MARKER, True)
    return module


def install_search_command_reference_contract(module: ModuleType | Any) -> Any:
    """Ensure public stdout JSON also receives the same reference projection."""
    marker = f"{_PATCH_MARKER}_search_command"
    if getattr(module, marker, False):
        return module

    original_print_json = module._print_json

    def print_json(
        payload: dict[str, Any],
        *,
        ascii_safe: bool,
    ) -> Any:
        projected = copy.deepcopy(payload)
        add_reference_metadata_to_payload(projected)
        return original_print_json(projected, ascii_safe=ascii_safe)

    module._print_json = print_json
    setattr(module, marker, True)
    return module


def _stored_path(item: dict[str, Any]) -> str:
    direct = str(item.get("path") or "").strip()
    if direct:
        return direct.replace("\\", "/")
    source = item.get("source")
    if isinstance(source, dict):
        return str(source.get("path") or "").strip().replace("\\", "/")
    return ""


def _reference_filename(item: dict[str, Any], path: str) -> str:
    if path:
        candidate = PurePosixPath(path).name.strip()
        if candidate and candidate not in {".", ".."}:
            return candidate
    source = item.get("source")
    source_title = (
        str(source.get("title") or "").strip()
        if isinstance(source, dict)
        else ""
    )
    for candidate in (
        str(item.get("title") or "").strip(),
        source_title,
        str(item.get("id") or item.get("item_id") or "").strip(),
        "source",
    ):
        if candidate:
            return candidate
    return "source"


def _safe_http_url(value: str) -> bool:
    if not value or _CONTROL_CHARACTERS.search(value):
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme.casefold() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def _markdown_url(value: str) -> str:
    return (
        value.replace("\\", "%5C")
        .replace(" ", "%20")
        .replace("(", "%28")
        .replace(")", "%29")
    )


def _escape_markdown_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _escape_code_span(value: str) -> str:
    return value.replace("`", "ˋ")
