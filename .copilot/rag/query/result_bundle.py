from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


RESULT_POINTER_SCHEMA = "rag-result-pointer-v1"
SUMMARY_SCHEMA = "rag-initial-answer-v1"
MANIFEST_SCHEMA = "rag-result-manifest-v1"
DETAIL_ITEM_SCHEMA = "rag-detail-item-v1"
EXPANDED_SCHEMA = "rag-expanded-answer-v1"
DETAIL_POINTER_SCHEMA = "rag-detail-pointer-v1"

DEFAULT_TTL_SECONDS = 60 * 60
SLIDING_EXTENSION_SECONDS = 30 * 60
HARD_LIFETIME_SECONDS = 4 * 60 * 60
STALE_TMP_SECONDS = 2 * 60
ORPHAN_GRACE_SECONDS = 2 * 60
MAX_RESULT_SETS = 100
MAX_SPOOL_BYTES = 128 * 1024 * 1024
MAX_ITEMS_PER_RESULT = 20
MAX_EXPANDED_RESPONSES_PER_RESULT = 20
LOCK_STALE_SECONDS = 10
LOCK_WAIT_SECONDS = 0.1
_ITEM_ID_RE = re.compile(r"^[ED]\d{1,2}$")


def result_spool_root() -> Path:
    return (
        Path(tempfile.gettempdir())
        / "GitHubCopilotLocalRAG"
        / "results"
    )


def publish_result_bundle(
    payload: dict[str, Any],
    *,
    spool_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = _managed_root(spool_root)
    cleanup_result_spool(spool_root=root, now=now)
    current = _utc_now(now)
    result_set_id = str(uuid.uuid4())
    result_dir = root / result_set_id
    _ensure_managed_child(result_dir, root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    result_dir.mkdir(mode=0o700)
    items_dir = result_dir / "items"
    items_dir.mkdir(mode=0o700)

    expires = current + timedelta(seconds=DEFAULT_TTL_SECONDS)
    hard_expires = current + timedelta(seconds=HARD_LIFETIME_SECONDS)
    try:
        summary, detail_items = build_initial_summary(
            payload,
            result_set_id=result_set_id,
            expires_at=expires,
        )
        entries: dict[str, dict[str, str]] = {}
        for logical_id, kind, detail in detail_items[:MAX_ITEMS_PER_RESULT]:
            storage_id = str(uuid.uuid4())
            relative_file = f"items/{storage_id}.json"
            detail_path = result_dir / relative_file
            _atomic_write_json(detail_path, detail)
            entries[logical_id] = {
                "kind": kind,
                "storage_id": storage_id,
                "file": relative_file,
            }

        available = [
            item_id
            for item_id in summary["follow_up"]["available_item_ids"]
            if item_id in entries
        ]
        defaults = [
            item_id
            for item_id in summary["follow_up"]["default_item_ids"]
            if item_id in entries
        ]
        summary["follow_up"]["available_item_ids"] = available
        summary["follow_up"]["default_item_ids"] = defaults
        summary["follow_up"]["detail_available"] = bool(available)

        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "result_set_id": result_set_id,
            "created_at": _iso_z(current),
            "expires_at": _iso_z(expires),
            "items": entries,
        }
        _atomic_write_json(result_dir / "manifest.json", manifest)
        _atomic_write_json(result_dir / "summary.json", summary)
        meta = {
            "schema_version": "rag-result-meta-v1",
            "result_set_id": result_set_id,
            "ready": True,
            "created_at": _iso_z(current),
            "last_access_at": _iso_z(current),
            "expires_at": _iso_z(expires),
            "hard_expires_at": _iso_z(hard_expires),
            "selected_db": str(
                payload.get("selected_db") or payload.get("db") or ""
            ),
            "item_count": len(entries),
        }
        _atomic_write_json(result_dir / "meta.json", meta)
        bundle_bytes = _tree_size(result_dir)
        meta["bundle_bytes"] = bundle_bytes
        _atomic_write_json(result_dir / "meta.json", meta)
        summary_path = result_dir / "summary.json"
        pointer = {
            "status": "written",
            "schema_version": RESULT_POINTER_SCHEMA,
            "result_set_id": result_set_id,
            "summary_file": str(summary_path),
            "expires_at": _iso_z(expires),
            "bytes": summary_path.stat().st_size,
        }
    except Exception:
        _remove_result_dir(result_dir, root)
        raise
    return pointer


def build_initial_summary(
    payload: dict[str, Any],
    *,
    result_set_id: str,
    expires_at: datetime,
) -> tuple[
    dict[str, Any],
    list[tuple[str, str, dict[str, Any]]],
]:
    status = str(payload.get("status") or "error")
    answerability = str(payload.get("answerability") or "none")
    selected_db = str(
        payload.get("selected_db") or payload.get("db") or ""
    )
    warnings = _unique_strings(payload.get("warnings") or [], limit=20)
    evidence, evidence_details = _summary_evidence(
        payload.get("evidence") or [],
        result_set_id=result_set_id,
    )
    background = _summary_background(
        payload.get("background_context") or []
    )
    documents, document_details = _summary_documents(
        payload.get("document_results") or [],
        internal_details=payload.get("_result_detail_items") or [],
        result_set_id=result_set_id,
    )
    key_points = _extractive_answer_units(evidence)
    if not key_points and documents:
        key_points = _related_answer_units(documents)
    limitations = _limitations(
        payload,
        status=status,
        evidence=evidence,
        documents=documents,
        warnings=warnings,
    )
    japanese = _contains_japanese(str(payload.get("query") or ""))
    draft = _initial_answer_draft(
        status=status,
        key_points=key_points,
        limitations=limitations,
        evidence=evidence,
        documents=documents,
        japanese=japanese,
    )
    defaults = _default_detail_ids(evidence, documents)
    available = [
        *(item["id"] for item in evidence),
        *(item["id"] for item in documents),
    ][:MAX_ITEMS_PER_RESULT]
    summary = {
        "schema_version": SUMMARY_SCHEMA,
        "status": status,
        "answerability": answerability,
        "selected_db": selected_db,
        "result_set_id": result_set_id,
        "initial_response": {
            "answer_draft_markdown": draft,
            "key_points": key_points,
            "limitations": limitations,
            "response_rules": {
                "use_only_this_summary": False,
                "cached_detail_lookup_allowed": True,
                "do_not_add_unsupported_claims": True,
                "do_not_infer_missing_table_headers": True,
                "citation_style": "[E1]",
            },
        },
        "evidence": evidence,
        "background_context": background,
        "document_results": documents,
        "warnings": warnings,
        "follow_up": {
            "detail_available": bool(available),
            "default_item_ids": defaults,
            "available_item_ids": available,
            "expires_at": _iso_z(expires_at),
        },
        "coverage": dict(payload.get("coverage") or {}),
    }
    details = [*evidence_details, *document_details]
    return summary, details[:MAX_ITEMS_PER_RESULT]


def load_expanded_result(
    result_set_id: str,
    item_ids: Iterable[str] | None,
    *,
    detail_level: str,
    spool_root: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], datetime | None]:
    root = _managed_root(spool_root)
    cleanup_result_spool(spool_root=root, now=now)
    current = _utc_now(now)
    try:
        result_dir = _result_dir(result_set_id, root)
    except ValueError:
        return _expired_packet(result_set_id), None
    meta = _read_ready_meta(result_dir)
    if meta is None or _parse_iso(meta.get("expires_at")) <= current:
        return _expired_packet(result_set_id), None

    manifest = _read_json(result_dir / "manifest.json")
    summary = _read_json(result_dir / "summary.json")
    manifest_items = manifest.get("items")
    if not isinstance(manifest_items, dict):
        return _expired_packet(result_set_id), None
    requested = [str(value) for value in (item_ids or []) if value]
    if not requested:
        requested = [
            str(value)
            for value in (
                (summary.get("follow_up") or {}).get("default_item_ids")
                or []
            )
        ]
    maximum = 1 if detail_level == "deep" else 3
    requested = requested[:maximum]
    expanded: list[dict[str, Any]] = []
    warnings: list[str] = []
    for item_id in requested:
        if not _ITEM_ID_RE.fullmatch(item_id):
            warnings.append(f"item_not_available:{item_id[:20]}")
            continue
        entry = manifest_items.get(item_id)
        if not isinstance(entry, dict):
            warnings.append(f"item_not_available:{item_id}")
            continue
        item_file = str(entry.get("file") or "")
        item_path = result_dir / item_file
        if not _safe_manifest_item_path(item_path, result_dir):
            warnings.append(f"item_not_available:{item_id}")
            continue
        try:
            detail = _read_json(item_path)
        except (OSError, ValueError, json.JSONDecodeError):
            warnings.append(f"item_not_available:{item_id}")
            continue
        if (
            detail.get("result_set_id") != result_set_id
            or detail.get("item_id") != item_id
        ):
            warnings.append(f"item_not_available:{item_id}")
            continue
        expanded.append(_expanded_item(detail, detail_level=detail_level))

    hard_expires = _parse_iso(meta.get("hard_expires_at"))
    new_expires = min(
        current + timedelta(seconds=SLIDING_EXTENSION_SECONDS),
        hard_expires,
    )
    meta["last_access_at"] = _iso_z(current)
    meta["expires_at"] = _iso_z(new_expires)
    _atomic_write_json(result_dir / "meta.json", meta)
    status = "ok" if expanded else "error"
    packet = {
        "schema_version": EXPANDED_SCHEMA,
        "status": status,
        "result_set_id": result_set_id,
        "expanded_items": expanded,
        "answer_draft_markdown": _expanded_answer_draft(expanded),
        "warnings": warnings,
    }
    return packet, new_expires


def publish_expanded_packet(
    packet: dict[str, Any],
    *,
    result_set_id: str,
    expires_at: datetime,
    spool_root: Path | None = None,
) -> dict[str, Any]:
    root = _managed_root(spool_root)
    result_dir = _result_dir(result_set_id, root)
    if _read_ready_meta(result_dir) is None:
        return _expired_packet(result_set_id)
    responses = result_dir / "responses"
    responses.mkdir(mode=0o700, exist_ok=True)
    storage_id = str(uuid.uuid4())
    output_path = responses / f"{storage_id}.json"
    _atomic_write_json(output_path, packet)
    _prune_expanded_responses(responses, keep=output_path)
    return {
        "status": "written",
        "schema_version": DETAIL_POINTER_SCHEMA,
        "result_set_id": result_set_id,
        "detail_file": str(output_path),
        "expires_at": _iso_z(expires_at),
        "bytes": output_path.stat().st_size,
    }


def _prune_expanded_responses(
    responses: Path,
    *,
    keep: Path,
) -> None:
    """Bound cached response packets without modifying packet contents."""
    try:
        candidates = sorted(
            (
                path
                for path in responses.glob("*.json")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
    except OSError:
        return
    retained_other = 0
    for path in candidates:
        if path == keep:
            continue
        if retained_other < MAX_EXPANDED_RESPONSES_PER_RESULT - 1:
            retained_other += 1
            continue
        try:
            path.unlink()
        except OSError:
            continue


def cleanup_result_spool(
    *,
    spool_root: Path | None = None,
    now: datetime | None = None,
) -> None:
    root = _managed_root(spool_root)
    if not root.exists():
        return
    current = _utc_now(now)
    lock = root / ".cleanup.lock"
    descriptor: int | None = None
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while descriptor is None:
        try:
            descriptor = os.open(
                lock,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS
            except OSError:
                return
            if stale:
                try:
                    lock.unlink()
                except OSError:
                    return
                continue
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)
        except OSError:
            return
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        _cleanup_locked(root, current)
    except Exception:
        # Cleanup is maintenance only and must never fail a search.
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            lock.unlink()
        except OSError:
            pass


def _cleanup_locked(root: Path, current: datetime) -> None:
    filesystem_now = time.time()
    stale_cutoff = filesystem_now - STALE_TMP_SECONDS
    for directory, child_directories, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        child_directories[:] = [
            name
            for name in child_directories
            if not (Path(directory) / name).is_symlink()
        ]
        for filename in filenames:
            if not filename.endswith(".tmp"):
                continue
            candidate = Path(directory) / filename
            try:
                if candidate.stat().st_mtime < stale_cutoff:
                    _ensure_beneath(candidate, root)
                    candidate.unlink()
            except OSError:
                continue

    ready_sets: list[tuple[datetime, Path, int]] = []
    for child in list(root.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            try:
                if filesystem_now - child.lstat().st_mtime > ORPHAN_GRACE_SECONDS:
                    child.unlink()
            except OSError:
                pass
            continue
        if not child.is_dir():
            continue
        try:
            uuid.UUID(child.name)
        except ValueError:
            try:
                age = filesystem_now - child.stat().st_mtime
            except OSError:
                continue
            if age > ORPHAN_GRACE_SECONDS:
                _remove_result_dir(child, root)
            continue
        meta = _read_ready_meta(child)
        if meta is None:
            try:
                age = filesystem_now - child.stat().st_mtime
            except OSError:
                continue
            if age > ORPHAN_GRACE_SECONDS:
                _remove_result_dir(child, root)
            continue
        expires = _parse_iso(meta.get("expires_at"))
        if expires <= current:
            _remove_result_dir(child, root)
            continue
        last_access = _parse_iso(
            meta.get("last_access_at") or meta.get("created_at")
        )
        ready_sets.append((last_access, child, _tree_size(child)))

    total = sum(item[2] for item in ready_sets)
    ready_sets.sort(key=lambda item: item[0])
    while (
        len(ready_sets) > MAX_RESULT_SETS
        or (
            total > MAX_SPOOL_BYTES
            and len(ready_sets) > 1
        )
    ):
        _last_access, candidate, size = ready_sets.pop(0)
        _remove_result_dir(candidate, root)
        total -= size


def _summary_evidence(
    contexts: Iterable[dict[str, Any]],
    *,
    result_set_id: str,
) -> tuple[
    list[dict[str, Any]],
    list[tuple[str, str, dict[str, Any]]],
]:
    summary: list[dict[str, Any]] = []
    details: list[tuple[str, str, dict[str, Any]]] = []
    eligible = [
        context
        for context in contexts
        if _authoritative_evidence_context(context)
    ]
    for index, context in enumerate(eligible[:4], start=1):
        item_id = f"E{index}"
        source = context.get("source") or {}
        location = context.get("location") or {}
        excerpt = _shorten(
            str(
                context.get("matched_excerpt")
                or context.get("text")
                or ""
            ),
            450,
        )
        warnings = _unique_strings(
            [
                *(context.get("warnings") or []),
                *(context.get("context_warnings") or []),
            ],
            limit=8,
        )
        entry = {
            "id": item_id,
            "path": _shorten(str(source.get("path") or ""), 400),
            "title": _shorten(str(source.get("title") or ""), 160),
            "section": _shorten(
                str(
                    location.get("section")
                    or context.get("heading")
                    or ""
                ),
                160,
            ),
            "excerpt": excerpt,
            "support_level": "direct",
            "detail_available": True,
            "warnings": warnings,
        }
        _copy_source_link_fields(context, entry)
        summary.append(entry)
        source_ranges = [
            dict(value)
            for value in context.get("source_ranges") or []
            if isinstance(value, dict)
        ][:12]
        matched_range = next(
            (
                value
                for value in source_ranges
                if value.get("kind") == "matched"
            ),
            {},
        )
        detail = {
            "schema_version": DETAIL_ITEM_SCHEMA,
            "result_set_id": result_set_id,
            "item_id": item_id,
            "document_id": str(
                source.get("revision")
                or _stable_document_id(entry["path"])
            ),
            "chunk_uid": str(
                matched_range.get("chunk_uid")
                or context.get("anchor_chunk_uid")
                or ""
            ),
            "path": entry["path"],
            "title": entry["title"],
            "heading_path": [entry["section"]]
            if entry["section"]
            else [],
            "matched_excerpt": str(
                context.get("matched_excerpt")
                or context.get("text")
                or ""
            ),
            "context_before": str(context.get("context_before") or ""),
            "context_after": str(context.get("context_after") or ""),
            "additional_sections": [
                dict(value)
                for value in context.get("additional_sections") or []
                if isinstance(value, dict)
            ][:2],
            "table_context": context.get("table_context"),
            "source_ranges": source_ranges,
            "support_level": "direct",
            "context_reason": str(
                context.get("context_reason") or ""
            ),
            "warnings": warnings,
        }
        _copy_source_link_fields(context, detail)
        details.append((item_id, "evidence", detail))
    return summary, details


def _authoritative_evidence_context(context: dict[str, Any]) -> bool:
    if context.get("authoritative") is False:
        return False
    support = str(context.get("support_level") or "").casefold()
    if support in {"background", "related", "moderate", "weak"}:
        return False
    signals = {
        str(value).casefold()
        for value in context.get("signals") or []
        if value
    }
    if "neighbor" not in signals:
        return True
    if signals - {"neighbor"} or context.get("independent_signals"):
        return True
    return bool(
        context.get("context_reason")
        or context.get("support_kind") == "anchored_neighbor"
    )


def _summary_background(
    contexts: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, context in enumerate(list(contexts)[:2], start=1):
        source = context.get("source") or {}
        entry = {
            "id": f"B{index}",
            "path": _shorten(str(source.get("path") or ""), 300),
            "title": _shorten(str(source.get("title") or ""), 120),
            "excerpt": _shorten(
                str(context.get("text") or ""),
                260,
            ),
            "support_level": "background",
        }
        _copy_source_link_fields(context, entry)
        output.append(entry)
    return output


def _summary_documents(
    cards: Iterable[dict[str, Any]],
    *,
    internal_details: Iterable[dict[str, Any]],
    result_set_id: str,
) -> tuple[
    list[dict[str, Any]],
    list[tuple[str, str, dict[str, Any]]],
]:
    detail_by_path = {
        str(item.get("path") or ""): item
        for item in internal_details
        if isinstance(item, dict) and item.get("path")
    }
    summary: list[dict[str, Any]] = []
    details: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for card in cards:
        path = str(card.get("path") or "")
        if not path or path in seen or len(summary) >= 10:
            continue
        seen.add(path)
        item_id = f"D{len(summary) + 1}"
        support_level = str(card.get("support_level") or "weak")
        entry = {
            "id": item_id,
            "path": _shorten(path, 400),
            "title": _shorten(str(card.get("title") or ""), 160),
            "section": _shorten(str(card.get("section") or ""), 160),
            "preview": _shorten(str(card.get("preview") or ""), 220),
            "support_level": support_level
            if support_level in {"direct", "strong", "moderate", "weak"}
            else "weak",
            "authoritative": bool(card.get("authoritative")),
            "relationship": _shorten(
                str(card.get("relationship") or ""),
                220,
            ),
            "detail_available": True,
        }
        _copy_source_link_fields(card, entry)
        summary.append(entry)
        cached = detail_by_path.get(path) or {}
        detail = {
            "schema_version": DETAIL_ITEM_SCHEMA,
            "result_set_id": result_set_id,
            "item_id": item_id,
            "document_id": str(
                cached.get("document_id")
                or _stable_document_id(path)
            ),
            "chunk_uid": str(cached.get("chunk_uid") or ""),
            "path": entry["path"],
            "title": entry["title"],
            "heading_path": list(
                cached.get("heading_path") or [entry["section"]]
            )[:6]
            if entry["section"] or cached.get("heading_path")
            else [],
            "matched_excerpt": str(
                cached.get("matched_excerpt")
                or card.get("preview")
                or ""
            ),
            "context_before": str(cached.get("context_before") or ""),
            "context_after": str(cached.get("context_after") or ""),
            "additional_sections": [
                dict(value)
                for value in cached.get("additional_sections") or []
                if isinstance(value, dict)
            ][:2],
            "table_context": cached.get("table_context"),
            "source_ranges": [
                dict(value)
                for value in cached.get("source_ranges") or []
                if isinstance(value, dict)
            ][:12],
            "support_level": entry["support_level"],
            "context_reason": str(
                cached.get("context_reason")
                or card.get("relationship")
                or ""
            ),
            "warnings": _unique_strings(
                cached.get("warnings") or [],
                limit=8,
            ),
        }
        _copy_source_link_fields(card, detail)
        _copy_source_link_fields(cached, detail)
        details.append((item_id, "document", detail))
    return summary, details


def _copy_source_link_fields(
    source: dict[str, Any],
    target: dict[str, Any],
) -> None:
    for key in (
        "source_provider",
        "source_url",
        "source_permalink",
        "source_link_status",
    ):
        value = str(source.get(key) or "")
        if value:
            target[key] = value


def _extractive_answer_units(
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    normalized_seen: list[str] = []
    for item in evidence:
        if item.get("support_level") != "direct":
            continue
        if _unknown_table_headers(item):
            continue
        text = _complete_extract(
            str(item.get("excerpt") or ""),
            heading=str(item.get("section") or ""),
        )
        if not text:
            continue
        normalized = re.sub(r"\W+", "", text).casefold()
        if any(
            normalized in existing or existing in normalized
            for existing in normalized_seen
        ):
            continue
        normalized_seen.append(normalized)
        output.append(
            {
                "id": f"P{len(output) + 1}",
                "text": text,
                "support": "direct",
                "source_ids": [str(item["id"])],
            }
        )
        if len(output) >= 4:
            break
    return output


def _related_answer_units(
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    normalized_seen: set[str] = set()
    for item in documents:
        text = _complete_extract(
            str(
                item.get("preview")
                or item.get("relationship")
                or item.get("title")
                or ""
            ),
            heading=str(item.get("section") or item.get("title") or ""),
        )
        if not text:
            continue
        normalized = re.sub(r"\W+", "", text).casefold()
        if not normalized or normalized in normalized_seen:
            continue
        normalized_seen.add(normalized)
        output.append(
            {
                "id": f"P{len(output) + 1}",
                "text": text,
                "support": "related",
                "source_ids": [str(item["id"])],
            }
        )
        if len(output) >= 4:
            break
    return output


def _limitations(
    payload: dict[str, Any],
    *,
    status: str,
    evidence: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    warnings: list[str],
) -> list[str]:
    output = list(warnings)
    unmatched = [
        str(value)
        for value in payload.get("unmatched_identifiers") or []
        if value
    ]
    if unmatched:
        output.append(
            "No verified literal occurrence was found for: "
            + ", ".join(unmatched[:3])
            + "."
        )
    if not evidence and documents:
        output.append(
            "Direct supporting evidence was not found; document results are "
            "related research leads and do not prove the requested claim."
        )
    elif not evidence and status in {"no_hit", "partial"}:
        output.append("Direct supporting evidence was not found.")
    if status == "error":
        output.append("The lookup did not complete successfully.")
    return _unique_strings(output, limit=20)


def _initial_answer_draft(
    *,
    status: str,
    key_points: list[dict[str, Any]],
    limitations: list[str],
    evidence: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    japanese: bool,
) -> str:
    lines: list[str] = []
    references = {
        str(item.get("id") or ""): item
        for item in [*evidence, *documents]
        if item.get("id")
    }
    if key_points:
        lines.append("## 回答" if japanese else "## Answer")
        related_only = all(
            point.get("support") == "related" for point in key_points
        )
        if related_only:
            lines.append(
                "検証済みの直接根拠は見つかりませんでした。以下は関連資料から"
                "組み立てた暫定回答です。内容の適合性は利用者が判断してください。"
                if japanese
                else (
                    "Verified direct evidence was not found. The following is "
                    "a provisional answer assembled from related documents; "
                    "the user should judge its relevance."
                )
            )
        for point in key_points:
            sources = " ".join(
                _markdown_source_reference(
                    str(value),
                    references.get(str(value)),
                )
                for value in point.get("source_ids") or []
            )
            lines.append(f"- {point['text']} {sources}".rstrip())
    elif documents:
        lines.append("## 回答" if japanese else "## Answer")
        lines.append(
            "検証済みの直接根拠は見つかりませんでした。以下は関連資料の候補であり、"
            "要求された用語や主張の意味・存在を証明するものではありません。"
            if japanese
            else (
                "Verified direct evidence was not found. The documents below "
                "are related research leads and do not prove the requested "
                "term or claim."
            )
        )
    else:
        lines.append("## 回答" if japanese else "## Answer")
        lines.append(
            "ローカル資料から回答を支持する根拠は見つかりませんでした。"
            if japanese
            else "No supporting evidence was found in the local material."
        )
    if limitations:
        lines.append("")
        lines.append("## 制約" if japanese else "## Limitations")
        lines.extend(f"- {value}" for value in limitations)
    if documents:
        lines.append("")
        lines.append("## 関連資料" if japanese else "## Related documents")
        for item in documents[:8]:
            label = item.get("title") or item.get("path") or item["id"]
            reference = _markdown_source_reference(
                str(item["id"]),
                item,
            )
            relationship = _shorten(
                str(item.get("relationship") or ""),
                100,
            )
            suffix = f" — {relationship}" if relationship else ""
            lines.append(
                f"- {reference} {label} "
                f"({item['support_level']}){suffix}"
            )
    return "\n".join(lines)


def _default_detail_ids(
    evidence: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> list[str]:
    defaults = [item["id"] for item in evidence[:2]]
    evidence_paths = {str(item.get("path") or "") for item in evidence}
    candidate = next(
        (
            item
            for item in documents
            if item.get("support_level") == "strong"
            and str(item.get("path") or "") not in evidence_paths
        ),
        None,
    )
    if candidate is None:
        candidate = next(
            (
                item
                for item in documents
                if str(item.get("path") or "") not in evidence_paths
            ),
            None,
        )
    if candidate is not None:
        defaults.append(str(candidate["id"]))
    return defaults[:3]


def _expanded_item(
    detail: dict[str, Any],
    *,
    detail_level: str,
) -> dict[str, Any]:
    expanded = {
        key: detail.get(key)
        for key in (
            "item_id",
            "path",
            "title",
            "heading_path",
            "matched_excerpt",
            "context_before",
            "context_after",
            "additional_sections",
            "table_context",
            "source_ranges",
            "support_level",
            "context_reason",
            "warnings",
            "source_provider",
            "source_url",
            "source_permalink",
            "source_link_status",
        )
    }
    if detail_level == "expanded":
        expanded["matched_excerpt"] = _shorten(
            str(expanded.get("matched_excerpt") or ""),
            1_200,
        )
        expanded["context_before"] = _shorten(
            str(expanded.get("context_before") or ""),
            600,
        )
        expanded["context_after"] = _shorten(
            str(expanded.get("context_after") or ""),
            600,
        )
        expanded["additional_sections"] = list(
            expanded.get("additional_sections") or []
        )[:1]
    else:
        expanded["matched_excerpt"] = _shorten(
            str(expanded.get("matched_excerpt") or ""),
            4_000,
        )
        expanded["context_before"] = _shorten(
            str(expanded.get("context_before") or ""),
            1_500,
        )
        expanded["context_after"] = _shorten(
            str(expanded.get("context_after") or ""),
            1_500,
        )
        expanded["additional_sections"] = list(
            expanded.get("additional_sections") or []
        )[:2]
    return expanded


def _expanded_answer_draft(
    expanded_items: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    for item in expanded_items:
        item_id = str(item.get("item_id") or "")
        title = str(item.get("title") or item.get("path") or item_id)
        support = str(item.get("support_level") or "weak")
        reference = _markdown_source_reference(item_id, item)
        lines.append(f"## {reference} {title}")
        if support != "direct":
            lines.append(
                "Related cached material; this is not authoritative proof."
            )
        if item.get("context_before"):
            lines.append(str(item["context_before"]))
        if item.get("matched_excerpt"):
            lines.append(str(item["matched_excerpt"]))
        if item.get("context_after"):
            lines.append(str(item["context_after"]))
        for section in item.get("additional_sections") or []:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "")
            text = str(section.get("text") or "")
            if heading:
                lines.append(f"### {heading}")
            if text:
                lines.append(text)
        if item.get("warnings"):
            lines.append(
                "Warnings: "
                + "; ".join(str(value) for value in item["warnings"])
            )
        lines.append("")
    return "\n".join(lines).strip()


def _markdown_source_reference(
    item_id: str,
    item: dict[str, Any] | None,
) -> str:
    label = item_id.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    if not item:
        return f"[{label}]"
    url = str(item.get("source_permalink") or item.get("source_url") or "")
    if not url:
        return f"[{label}]"
    safe_url = (
        url.replace("\\", "%5C")
        .replace(" ", "%20")
        .replace("(", "%28")
        .replace(")", "%29")
    )
    return f"[{label}]({safe_url})"


def _complete_extract(text: str, *, heading: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return ""
    candidates = [
        value.strip()
        for value in re.split(r"(?<=[。！？.!?])\s*", normalized)
        if value.strip()
    ]
    complete = next(
        (
            value
            for value in candidates
            if len(value) >= 24 and value[-1:] in "。！？.!?"
        ),
        "",
    )
    selected = complete or normalized
    selected = _shorten(selected, 220)
    begins_mid_sentence = bool(
        selected
        and (
            selected[0].islower()
            or re.match(
                r"^(?:this|that|these|those|it|they|これ|それ|この|その)\b",
                selected,
                re.IGNORECASE,
            )
        )
    )
    if heading and (begins_mid_sentence or not complete):
        selected = _shorten(f"{heading}: {selected}", 220)
    return selected


def _unknown_table_headers(item: dict[str, Any]) -> bool:
    warnings = " ".join(str(value) for value in item.get("warnings") or [])
    if "table_headers_incomplete" in warnings:
        return True
    excerpt = str(item.get("excerpt") or "")
    table_like = any(
        line.count("|") >= 2 or line.count("\t") >= 2
        for line in excerpt.splitlines()
    )
    return table_like and "header" in warnings.casefold()


def _expired_packet(result_set_id: str) -> dict[str, Any]:
    return {
        "schema_version": EXPANDED_SCHEMA,
        "status": "result_expired",
        "result_set_id": str(result_set_id),
        "expanded_items": [],
        "answer_draft_markdown": "",
        "warnings": [],
    }


def _read_ready_meta(result_dir: Path) -> dict[str, Any] | None:
    try:
        meta = _read_json(result_dir / "meta.json")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (
        meta.get("ready") is not True
        or meta.get("result_set_id") != result_dir.name
    ):
        return None
    return meta


def _safe_manifest_item_path(path: Path, result_dir: Path) -> bool:
    try:
        relative = path.relative_to(result_dir)
    except ValueError:
        return False
    return (
        len(relative.parts) == 2
        and relative.parts[0] == "items"
        and re.fullmatch(
            r"[0-9a-fA-F-]{36}\.json",
            relative.parts[1],
        )
        is not None
    )


def _result_dir(result_set_id: str, root: Path) -> Path:
    try:
        parsed = uuid.UUID(str(result_set_id))
    except ValueError as exc:
        raise ValueError("invalid result set ID") from exc
    if str(parsed) != str(result_set_id).lower():
        raise ValueError("invalid result set ID")
    result_dir = root / str(parsed)
    _ensure_managed_child(result_dir, root)
    return result_dir


def _managed_root(root: Path | None) -> Path:
    selected = root if root is not None else result_spool_root()
    return selected.expanduser().absolute()


def _ensure_managed_child(path: Path, root: Path) -> None:
    if path.parent != root or path == root:
        raise ValueError("result path is outside the managed spool")


def _ensure_beneath(path: Path, root: Path) -> None:
    root_resolved = root.resolve()
    resolved = path.resolve(strict=False)
    if resolved == root_resolved:
        raise ValueError("refusing to remove the spool root")
    resolved.relative_to(root_resolved)


def _remove_result_dir(path: Path, root: Path) -> None:
    _ensure_managed_child(path, root)
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        shutil.rmtree(path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    try:
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_descriptor)
    except OSError:
        pass
    finally:
        os.close(directory_descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object")
    return payload


def _tree_size(path: Path) -> int:
    total = 0
    for directory, _children, filenames in os.walk(
        path,
        followlinks=False,
    ):
        for filename in filenames:
            candidate = Path(directory) / filename
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def _stable_document_id(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _shorten(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 1:
        return normalized[:limit]
    return normalized[: limit - 1].rstrip() + "…"


def _unique_strings(
    values: Iterable[Any],
    *,
    limit: int,
) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _shorten(str(value), 240)
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _contains_japanese(value: str) -> bool:
    return re.search(r"[\u3040-\u30ff\u3400-\u9fff]", value) is not None


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return (
        _utc_now(value)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(value: Any) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return _utc_now(parsed)
