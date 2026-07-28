from __future__ import annotations

import json
from typing import Any


def payload_to_text(
    payload: dict[str, Any],
    output_format: str,
    *,
    explain: bool = False,
) -> str:
    if output_format == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return payload_to_prompt(payload, explain=explain)


def payload_to_prompt(
    payload: dict[str, Any],
    *,
    explain: bool = False,
) -> str:
    question = str(payload.get("query") or "")
    db_name = str(payload.get("selected_db") or payload.get("db") or "")
    evidence = list(payload.get("evidence") or payload.get("contexts") or [])
    background = list(payload.get("background_context") or [])
    related = list(payload.get("related_context") or [])
    documents = list(payload.get("document_results") or [])
    warnings = [str(value) for value in payload.get("warnings") or [] if value]
    unmatched = [str(value) for value in payload.get("unmatched_identifiers") or []]
    lines = ["## Retrieved evidence", f"Database: {db_name}", ""]
    if payload.get("db_hint"):
        lines.extend(["## DB hint", str(payload["db_hint"]), ""])
    if warnings:
        lines.extend(["## Warnings", *[f"- {value}" for value in warnings], ""])
    if unmatched:
        lines.extend(
            [
                "## Identifier notice",
                "No verified literal occurrence was found for: "
                + ", ".join(unmatched),
                "Related results do not prove those identifiers.",
                "",
            ]
        )
    if not evidence:
        lines.extend(
            [
                f"Status: {payload.get('status') or 'no_hit'}",
                "Do not make unsupported claims.",
                "",
            ]
        )
    for item in evidence:
        source = item.get("source") or {}
        location = item.get("location") or {}
        section = location.get("section") or ""
        lines.append(
            f"[{item.get('id')}] {source.get('path') or ''}"
            + (f" - {section}" if section else "")
        )
        source_link = _preferred_source_link(item)
        if source_link:
            lines.append(f"Source link: {source_link}")
        if explain and item.get("source_link_status"):
            lines.append(
                "Source link status: "
                + str(item["source_link_status"])
            )
        if item.get("context_before"):
            lines.append(
                "Context before: " + str(item["context_before"])
            )
        lines.append(
            str(item.get("matched_excerpt") or item.get("text") or "")
        )
        if item.get("context_after"):
            lines.append(
                "Context after: " + str(item["context_after"])
            )
        if explain and item.get("debug"):
            lines.append(
                "debug="
                + json.dumps(item["debug"], ensure_ascii=False)
            )
        lines.append("")
    for heading, items in (
        ("Background context (not direct evidence)", background),
        ("Related search candidates (not direct evidence)", related),
    ):
        if not items:
            continue
        lines.extend([f"## {heading}", ""])
        for item in items:
            source = item.get("source") or {}
            lines.append(
                f"[{item.get('id')}] {source.get('path') or ''}"
            )
            source_link = _preferred_source_link(item)
            if source_link:
                lines.append(f"Source link: {source_link}")
            if explain and item.get("source_link_status"):
                lines.append(
                    "Source link status: "
                    + str(item["source_link_status"])
                )
            lines.extend([str(item.get("text") or ""), ""])
    if documents:
        lines.extend(["## Related documents (discovery results)", ""])
        for item in documents:
            path = str(item.get("path") or "")
            title = str(item.get("title") or path)
            lines.append(
                f"- [{item.get('support_level') or 'weak'}] {title} ({path})"
            )
            source_link = _preferred_source_link(item)
            if source_link:
                lines.append(f"  Source link: {source_link}")
            if explain and item.get("source_link_status"):
                lines.append(
                    "  Source link status: "
                    + str(item["source_link_status"])
                )
            if item.get("relationship"):
                lines.append(f"  {item['relationship']}")
            if item.get("preview"):
                lines.append(f"  {item['preview']}")
        lines.extend(
            [
                "",
                "Discovery results are research leads, not direct proof.",
                "",
            ]
        )
    lines.extend(
        [
            "Use only direct evidence for factual claims.",
            "",
            "# Question",
            "",
            question,
        ]
    )
    return "\n".join(lines)


def _preferred_source_link(item: dict[str, Any]) -> str:
    return str(item.get("uri") or "")
