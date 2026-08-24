from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
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
WINDOWS_REPLACE_RETRY_SECONDS = 2.0
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
        entries: dict[str, dict[str, Any]] = {}
        for logical_id, kind, detail in detail_items[:MAX_ITEMS_PER_RESULT]:
            storage_id = str(uuid.uuid4())
            relative_file = f"items/{storage_id}.json"
            detail_path = result_dir / relative_file
            _atomic_write_json(detail_path, detail)
            integrity = _file_integrity(detail_path)
            entries[logical_id] = {
                "kind": kind,
                "storage_id": storage_id,
                "file": relative_file,
                **integrity,
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

        summary_path = result_dir / "summary.json"
        _atomic_write_json(summary_path, summary)
        immutable_files = {
            "summary.json": _file_integrity(summary_path),
            **{
                str(entry["file"]): {
                    "size": int(entry["size"]),
                    "sha256": str(entry["sha256"]),
                }
                for entry in entries.values()
            },
        }
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "result_set_id": result_set_id,
            "created_at": _iso_z(current),
            "expires_at": _iso_z(expires),
            "items": entries,
            # meta.json is a mutable ready/access marker and manifest.json
            # cannot hash itself.  Every immutable payload file is recorded.
            "files": dict(sorted(immutable_files.items())),
        }
        _atomic_write_json(result_dir / "manifest.json", manifest)
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
        content_bytes = _tree_size(result_dir)
        bundle_bytes = content_bytes
        while True:
            meta["bundle_bytes"] = bundle_bytes
            total = content_bytes + len(
                json.dumps(
                    meta,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if total == bundle_bytes:
                break
            bundle_bytes = total
        # meta.json is the ready marker. Publish it exactly once, after every
        # other bundle file is complete, so concurrent cleanup never observes
        # and opens a marker that this publisher still needs to replace.
        _atomic_write_json(result_dir / "meta.json", meta)
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
        query=str(payload.get("query") or ""),
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


def load_initial_summary(
    result_set_id: str,
    expected_db: str,
    *,
    spool_root: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, datetime | None]:
    """Load one ready initial summary without exposing its spool identity.

    The result-set identifier is an internal lookup capability.  Callers get
    only an independent summary projection with that identifier removed, plus
    the validated expiry.  Every validation failure is deliberately collapsed
    to ``(None, None)`` so a model-facing adapter cannot use this API to probe
    managed paths or distinguish malformed, tampered, and expired bundles.
    """

    root = _managed_root(spool_root)
    if not _safe_spool_root(root):
        return None, None
    cleanup_result_spool(spool_root=root, now=now)
    current = _utc_now(now)
    database = str(expected_db or "")
    if not database:
        return None, None
    try:
        result_dir = _result_dir(result_set_id, root)
        if not _safe_ready_directory(root, result_dir):
            return None, None

        meta_path = result_dir / "meta.json"
        manifest_path = result_dir / "manifest.json"
        summary_path = result_dir / "summary.json"
        if not all(
            _safe_regular_bundle_file(path, result_dir)
            for path in (meta_path, manifest_path, summary_path)
        ):
            return None, None

        meta = _read_ready_meta(result_dir)
        if (
            meta is None
            or meta.get("schema_version") != "rag-result-meta-v1"
        ):
            return None, None
        expires_at = _parse_iso(meta.get("expires_at"))
        if expires_at <= current:
            return None, None
        if str(meta.get("selected_db") or "") != database:
            return None, None

        manifest = _read_json(manifest_path)
        if (
            manifest.get("schema_version") != MANIFEST_SCHEMA
            or manifest.get("result_set_id") != result_set_id
        ):
            return None, None
        manifest_files = manifest.get("files")
        summary_integrity = (
            manifest_files.get("summary.json")
            if isinstance(manifest_files, dict)
            else None
        )
        if (
            not isinstance(summary_integrity, dict)
            or "size" not in summary_integrity
            or "sha256" not in summary_integrity
            or not _manifest_file_matches(summary_path, summary_integrity)
        ):
            return None, None

        summary = _read_json(summary_path)
        if (
            summary.get("schema_version") != SUMMARY_SCHEMA
            or summary.get("result_set_id") != result_set_id
            or str(summary.get("selected_db") or "") != database
        ):
            return None, None
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None

    public_summary = dict(summary)
    public_summary.pop("result_set_id", None)
    return public_summary, expires_at


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
    summary_path = result_dir / "summary.json"
    manifest_files = manifest.get("files")
    if (
        isinstance(manifest_files, dict)
        and not _manifest_file_matches(
            summary_path,
            manifest_files.get("summary.json"),
        )
    ):
        return _expired_packet(result_set_id), None
    summary = _read_json(summary_path)
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
        integrity_record = (
            manifest_files.get(item_file)
            if isinstance(manifest_files, dict)
            else entry
        )
        if not _manifest_file_matches(item_path, integrity_record):
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
    query: str = "",
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
        excerpt = _evidence_excerpt(
            context,
            query=query,
            limit=450,
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
        "source_link_error",
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
            "source_link_error",
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
    return f"[{label}]"


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


def _safe_ready_directory(root: Path, result_dir: Path) -> bool:
    """Reject managed-root or result-directory reparse escapes."""

    try:
        if not _safe_spool_root(root) or _is_reparse_point(result_dir):
            return False
        result_metadata = result_dir.lstat()
        if not stat.S_ISDIR(result_metadata.st_mode):
            return False
        resolved_root = root.resolve(strict=True)
        resolved_result = result_dir.resolve(strict=True)
    except OSError:
        return False
    return resolved_result.parent == resolved_root


def _safe_spool_root(root: Path) -> bool:
    try:
        if _is_reparse_point(root):
            return False
        metadata = root.lstat()
        return stat.S_ISDIR(metadata.st_mode)
    except OSError:
        return False


def _safe_regular_bundle_file(path: Path, result_dir: Path) -> bool:
    """Accept only a direct, non-reparse regular file in one result set."""

    try:
        if path.parent != result_dir or _is_reparse_point(path):
            return False
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return False
        resolved_result = result_dir.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except OSError:
        return False
    return resolved_path.parent == resolved_result


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    reparse_attribute = int(
        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )
    return bool(attributes & reparse_attribute)


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
    _atomic_replace(temporary, path)
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


def _atomic_replace(source: Path, target: Path) -> None:
    deadline = time.monotonic() + WINDOWS_REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if (
                not _is_windows()
                or getattr(exc, "winerror", None) not in {5, 32, 33}
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.01)


def _is_windows() -> bool:
    return os.name == "nt"


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be an object")
    return payload


def _file_integrity(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {
        "size": size,
        "sha256": digest.hexdigest(),
    }


def _manifest_file_matches(path: Path, record: object) -> bool:
    if not isinstance(record, dict):
        return False
    expected_size = record.get("size")
    expected_digest = record.get("sha256")
    # Result sets created immediately before this additive manifest contract
    # may still be alive in the short-lived spool.  Their item records contain
    # neither field and remain readable until normal expiry.
    if expected_size is None and expected_digest is None:
        return True
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
        or not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        return False
    try:
        actual_size = path.stat().st_size
    except OSError:
        return False
    if actual_size != expected_size:
        return False
    return _file_integrity(path)["sha256"] == expected_digest


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


def _evidence_excerpt(
    context: dict[str, Any],
    *,
    query: str,
    limit: int,
) -> str:
    """Return a bounded excerpt without systematically discarding late facts."""
    matched = str(context.get("matched_excerpt") or "")
    full_text = str(context.get("text") or "")
    raw = matched or full_text
    normalized = raw.strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 1:
        return normalized[:limit]

    leading_trim = len(raw) - len(raw.lstrip())
    same_as_full_text = not matched or matched.strip() == full_text.strip()
    focus = _evidence_offset_focus(
        context,
        text_length=len(normalized),
        leading_trim=leading_trim,
        full_text_coordinates=same_as_full_text,
    )
    if focus is None:
        focus = _evidence_anchor_focus(context, normalized)
    if focus is None:
        focus = _query_focus(normalized, query)
    if focus is not None:
        return _focused_excerpt(normalized, focus[0], focus[1], limit)
    return _head_tail_excerpt(normalized, limit)


def _evidence_offset_focus(
    context: dict[str, Any],
    *,
    text_length: int,
    leading_trim: int,
    full_text_coordinates: bool,
) -> tuple[int, int] | None:
    ranges = [
        value
        for value in context.get("source_ranges") or []
        if isinstance(value, dict)
    ]
    ranges.sort(key=lambda value: value.get("kind") != "matched")
    candidates = [context, *ranges]
    for value in candidates:
        span = _bounded_offset_span(
            value.get("anchor_excerpt_start"),
            value.get("anchor_excerpt_end"),
            text_length=text_length,
            leading_trim=leading_trim,
        )
        if span is not None:
            return span
    if not full_text_coordinates:
        return None
    for start_key, end_key in (
        ("anchor_char_start", "anchor_char_end"),
        ("char_start", "char_end"),
    ):
        for value in candidates:
            span = _bounded_offset_span(
                value.get(start_key),
                value.get(end_key),
                text_length=text_length,
                leading_trim=leading_trim,
            )
            if span is not None:
                return span
    return None


def _bounded_offset_span(
    start: Any,
    end: Any,
    *,
    text_length: int,
    leading_trim: int,
) -> tuple[int, int] | None:
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
    ):
        return None
    adjusted_start = start - leading_trim
    adjusted_end = end - leading_trim
    if adjusted_start < 0 or adjusted_end <= adjusted_start:
        return None
    if adjusted_start >= text_length or adjusted_end > text_length:
        return None
    return adjusted_start, adjusted_end


def _evidence_anchor_focus(
    context: dict[str, Any],
    text: str,
) -> tuple[int, int] | None:
    candidates: list[str] = []
    for value in (
        context.get("anchor_term"),
        context.get("matched_term"),
        context.get("matched_text"),
    ):
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    for source_range in context.get("source_ranges") or []:
        if not isinstance(source_range, dict):
            continue
        for key in ("anchor_term", "matched_term", "matched_text"):
            value = source_range.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    debug = context.get("debug")
    if isinstance(debug, dict):
        exact_match = debug.get("exact_match")
        if isinstance(exact_match, dict):
            candidates.extend(
                str(value).strip()
                for value in exact_match.get("matched_terms") or []
                if str(value).strip()
            )
    for candidate in sorted(set(candidates), key=len, reverse=True):
        span = _literal_span(text, candidate)
        if span is not None:
            return span
    return None


def _query_focus(text: str, query: str) -> tuple[int, int] | None:
    tokens = re.findall(
        r"[0-9A-Za-z_\-\u3040-\u30ff\u3400-\u9fff]+",
        query,
    )
    candidates: set[str] = set()
    for token in tokens:
        if len(token) < 4:
            continue
        candidates.add(token)
        maximum = min(len(token), 32)
        for width in range(maximum, 3, -1):
            found = False
            for start in range(0, len(token) - width + 1):
                fragment = token[start : start + width]
                if _literal_span(text, fragment) is not None:
                    candidates.add(fragment)
                    found = True
            if found:
                break
    for candidate in sorted(candidates, key=len, reverse=True):
        span = _literal_span(text, candidate)
        if span is not None:
            return span
    return None


def _literal_span(text: str, value: str) -> tuple[int, int] | None:
    match = re.search(re.escape(value), text, flags=re.IGNORECASE)
    if match is None:
        return None
    return match.start(), match.end()


def _focused_excerpt(
    text: str,
    focus_start: int,
    focus_end: int,
    limit: int,
) -> str:
    content_budget = max(1, limit - 2)
    span_length = max(1, focus_end - focus_start)
    if span_length >= content_budget:
        window_start = max(0, min(focus_start, len(text) - content_budget))
    else:
        left_context = (content_budget - span_length) // 2
        window_start = max(0, focus_start - left_context)
        window_start = min(window_start, len(text) - content_budget)
        if focus_end > window_start + content_budget:
            window_start = focus_end - content_budget
    window_end = min(len(text), window_start + content_budget)
    prefix = "…" if window_start > 0 else ""
    suffix = "…" if window_end < len(text) else ""
    return prefix + text[window_start:window_end].strip() + suffix


def _head_tail_excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    head_length = (limit - 1) // 2
    tail_length = limit - 1 - head_length
    return (
        text[:head_length].rstrip()
        + "…"
        + text[-tail_length:].lstrip()
    )


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
