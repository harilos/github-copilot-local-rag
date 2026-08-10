from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from help_links import MANAGER_HELP_URL


SCHEMA_VERSION = "local-rag.database-list.v2"
MAX_PUBLIC_SOURCES = 8
TYPE_LABELS = {
    "folder": "Folder",
    "git": "Git repository",
    "github": "GitHub",
    "gitlab": "GitLab",
    "azure_devops": "Azure DevOps",
    "svn": "Subversion",
    "sharepoint": "SharePoint",
    "redmine": "Redmine",
    "gitlab_issues": "GitLab Issue",
    "github_issues": "GitHub Issues",
    "github_wiki": "GitHub Wiki",
    "other": "Other",
}


def main(argv: Sequence[str] | None = None) -> int:
    _configure_output_streams()
    arguments = list(sys.argv[1:] if argv is None else argv)
    parsed = _parse_arguments(arguments)
    rag_root = Path(__file__).resolve().parents[1]
    lower = rag_root / "query" / "list_dbs.py"
    # The public view always joins the lower JSON contract with DB-local
    # catalog/metadata information.  Human-readable text is rendered only
    # after that single lower invocation.
    child_arguments = ["--format", "json"]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-B", str(lower), *child_arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    _write_bytes(sys.stderr, completed.stderr)
    if completed.returncode != 0:
        _write_bytes(sys.stdout, completed.stdout)
        return int(completed.returncode)
    try:
        lower_payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _write_bytes(sys.stdout, completed.stdout)
        return int(completed.returncode)
    databases = lower_payload.get("databases")
    if not isinstance(databases, list):
        _write_bytes(sys.stdout, completed.stdout)
        return int(completed.returncode)
    dbs_root = Path(
        os.getenv("RAG_DBS_ROOT", str(rag_root / "dbs"))
    ).expanduser()
    public_databases = [
        _public_database(item, dbs_root)
        for item in databases
        if isinstance(item, dict)
    ]
    output = {
        "schema": SCHEMA_VERSION,
        "status": "ok",
        "databases": public_databases,
    }
    if parsed == "text":
        _print_text(public_databases)
        return int(completed.returncode)
    print(
        json.dumps(
            output,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return int(completed.returncode)


def _print_text(databases: list[dict[str, Any]]) -> None:
    if not databases:
        print("利用可能なDBはありません。")
        return
    for index, database in enumerate(databases):
        if index:
            print()
        name = str(database.get("name") or "")
        title = str(database.get("title") or name)
        print(f"{name} — {title}")
        summary = str(database.get("content_summary") or "").strip()
        status = str(
            database.get("content_summary_status") or "unavailable"
        )
        print(
            f"  内容: {summary or '確認できません'}"
            + ("" if status == "complete" else f"（{status}）")
        )
        hint = str(database.get("query_hint") or "").strip()
        if hint:
            print(f"  検索向け: {hint}")


def _parse_arguments(arguments: list[str]) -> str:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "利用可能なLocal RAGデータベースと資料の種類を表示します。"
        ),
        epilog=(
            "Local RAG Managerと日本語ガイド:\n"
            f"{MANAGER_HELP_URL}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
    )
    return str(parser.parse_args(arguments).format)


def _public_database(item: dict[str, Any], dbs_root: Path) -> dict[str, Any]:
    name = str(item.get("name") or "")
    root = _safe_database_root(dbs_root, name)
    summary = (
        _content_summary(root, name)
        if root is not None
        else _unavailable_summary()
    )
    output = dict(item)
    output.update(summary)
    output["name"] = name
    output["title"] = str(item.get("title") or name)
    output["query_hint"] = str(item.get("query_hint") or "")
    return output


def _safe_database_root(dbs_root: Path, name: str) -> Path | None:
    if not name:
        return None
    candidate = dbs_root / name
    if candidate.is_symlink():
        return None
    try:
        root = dbs_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_dir() or resolved.parent != root:
        return None
    return resolved


def _content_summary(db_root: Path, db_name: str) -> dict[str, Any]:
    try:
        rows, unattributed = _catalog_source_counts(db_root)
    except (OSError, sqlite3.Error, ValueError):
        return _unavailable_summary()
    metadata, metadata_complete = _current_source_metadata(db_root, db_name)
    named_cards: list[dict[str, Any]] = []
    anonymous_documents: Counter[str] = Counter()
    anonymous_sources: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    for source_id, document_count in sorted(
        rows,
        key=lambda value: (value[0].casefold(), value[0]),
    ):
        configured = metadata.get(source_id) or {}
        source_type = _public_type(configured.get("source_type"))
        type_counts[source_type] += 1
        display_name = str(configured.get("display_name") or "").strip()
        if (
            source_id.casefold() in display_name.casefold()
            if display_name
            else False
        ):
            display_name = ""
        if not display_name:
            anonymous_sources[source_type] += 1
            anonymous_documents[source_type] += int(document_count)
            continue
        named_cards.append(
            {
                "name": display_name,
                "type": source_type,
                "label": TYPE_LABELS[source_type],
                "document_count": int(document_count),
            }
        )
    if unattributed:
        type_counts["other"] += 1
        anonymous_sources["other"] += 1
        anonymous_documents["other"] += int(unattributed)
    anonymous_cards = [
        {
            "name": (
                f"{TYPE_LABELS[source_type]}"
                f"（{anonymous_sources[source_type]} Source）"
            ),
            "type": source_type,
            "label": TYPE_LABELS[source_type],
            "document_count": int(anonymous_documents[source_type]),
        }
        for source_type in sorted(
            anonymous_sources,
            key=lambda value: (TYPE_LABELS[value].casefold(), value),
        )
    ]
    cards = [*named_cards, *anonymous_cards]
    cards.sort(
        key=lambda value: (
            -int(value["document_count"]),
            str(value["name"]).casefold(),
            str(value["type"]),
        )
    )
    source_count = len(rows) + (1 if unattributed else 0)
    shown = [
        {
            key: value[key]
            for key in ("name", "type", "label", "document_count")
        }
        for value in cards[:MAX_PUBLIC_SOURCES]
    ]
    source_types = [
        {
            "type": source_type,
            "label": TYPE_LABELS[source_type],
            "count": count,
        }
        for source_type, count in sorted(
            type_counts.items(),
            key=lambda value: (
                -value[1],
                TYPE_LABELS[value[0]].casefold(),
                value[0],
            ),
        )
    ]
    total_documents = (
        sum(int(document_count) for _source_id, document_count in rows)
        + int(unattributed)
    )
    if source_count:
        summary_cards = "、".join(
            _summary_card(value) for value in shown
        )
        content_summary = (
            f"全{total_documents}文書、{source_count} Source"
            + (f": {summary_cards}" if summary_cards else "")
        )
    else:
        content_summary = "索引済み文書はありません。"
    return {
        "content_summary": content_summary,
        "source_count": source_count,
        "unattributed_document_count": int(unattributed),
        "source_types": source_types,
        "sources": shown,
        "additional_source_count": max(0, source_count - len(shown)),
        "content_summary_status": (
            "complete" if metadata_complete else "partial"
        ),
    }


def _summary_card(value: dict[str, Any]) -> str:
    label = str(value["label"])
    name = str(value["name"])
    document_count = int(value["document_count"])
    if name.startswith(f"{label}（") and name.endswith(" Source）"):
        descriptor = name
    else:
        descriptor = f"{label}「{name}」"
    return f"{descriptor}{document_count}件"


def _catalog_source_counts(
    db_root: Path,
) -> tuple[list[tuple[str, int]], int]:
    catalog = db_root / "catalog.sqlite"
    if not _is_regular_nonsymlink(catalog):
        raise FileNotFoundError(catalog)
    for companion in (
        Path(str(catalog) + "-wal"),
        Path(str(catalog) + "-shm"),
    ):
        if companion.exists() or companion.is_symlink():
            if not _is_regular_nonsymlink(companion):
                raise OSError(
                    f"{companion.name} is not a regular local file"
                )
    with _connect_readonly(catalog) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(document)")
        }
        if not {"source_id", "doc_pk"}.issubset(columns):
            raise sqlite3.OperationalError("catalog document fields unavailable")
        visibility = (
            "visible_until IS NULL"
            if "visible_until" in columns
            else "1=1"
        )
        grouped = connection.execute(
            f"""
            SELECT
              CASE
                WHEN source_id IS NULL OR TRIM(source_id) = '' THEN ''
                ELSE source_id
              END AS grouped_source_id,
              COUNT(DISTINCT doc_pk) AS document_count
            FROM document
            WHERE {visibility}
            GROUP BY grouped_source_id
            ORDER BY grouped_source_id
            """
        ).fetchall()
    rows: list[tuple[str, int]] = []
    unattributed = 0
    for source_id, count in grouped:
        value = str(source_id or "")
        if value:
            rows.append((value, int(count or 0)))
        else:
            unattributed = int(count or 0)
    return rows, unattributed


def _current_source_metadata(
    db_root: Path,
    db_name: str,
) -> tuple[dict[str, dict[str, Any]], bool]:
    sidecar = db_root / "source-links.json"
    if sidecar.is_symlink():
        return {}, False
    if not sidecar.exists():
        return {}, True
    try:
        raw = _read_regular_nonsymlink(sidecar)
        if len(raw) > 1_048_576:
            return {}, False
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, False
    if not isinstance(payload, dict):
        return {}, False
    tool_root = (
        Path(__file__).resolve().parents[1]
        / "gen_db"
        / "software_rag_tool"
    )
    sys.path.insert(0, str(tool_root))
    try:
        from software_rag_tool.source_links import (
            LEGACY_SCHEMA_VERSION,
            LEGACY_V2_SCHEMA_VERSION,
            SCHEMA_VERSION,
            load_source_links,
            validate_source_links,
        )

        schema_version = payload.get("schema_version")
        if schema_version == SCHEMA_VERSION:
            payload = validate_source_links(
                payload,
                allow_unmatched_sources=True,
            )
        elif schema_version in {
            LEGACY_SCHEMA_VERSION,
            LEGACY_V2_SCHEMA_VERSION,
        }:
            loaded = load_source_links(db_root, db_name)
            if loaded.status != "configured" or loaded.payload is None:
                return {}, False
            # The compatibility reader accepts only a safely normalizable
            # legacy SharePoint link.  Other legacy providers stay anonymous.
            payload = loaded.payload
        else:
            return {}, False
    except (ImportError, OSError, ValueError):
        return {}, False
    finally:
        try:
            sys.path.remove(str(tool_root))
        except ValueError:
            pass
    sources: dict[str, dict[str, Any]] = {}
    try:
        for source in payload["sources"]:
            if not isinstance(source, dict):
                return {}, False
            source_id = str(source.get("source_id") or "").strip()
            if not source_id or source_id in sources:
                return {}, False
            source_type = _public_type(source.get("source_type"))
            value: dict[str, Any] = {"source_type": source_type}
            display_name = str(source.get("display_name") or "").strip()
            if display_name:
                value["display_name"] = display_name
            sources[source_id] = value
    except (TypeError, ValueError):
        return {}, False
    return sources, True


def _is_regular_nonsymlink(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(
        metadata.st_mode
    )


def _read_regular_nonsymlink(path: Path) -> bytes:
    if not _is_regular_nonsymlink(path):
        raise OSError(f"{path.name} is not a regular local file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"{path.name} is not a regular local file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _public_type(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in TYPE_LABELS else "other"


def _unavailable_summary() -> dict[str, Any]:
    return {
        "content_summary": "",
        "source_count": 0,
        "unattributed_document_count": 0,
        "source_types": [],
        "sources": [],
        "additional_source_count": 0,
        "content_summary_status": "unavailable",
    }


def _connect_readonly(path: Path) -> Any:
    """Use the catalog package's canonical no-WAL read-only connection."""
    tool_root = (
        Path(__file__).resolve().parents[1]
        / "gen_db"
        / "software_rag_tool"
    )
    sys.path.insert(0, str(tool_root))
    try:
        from software_rag_tool.catalog import connect_readonly
    finally:
        try:
            sys.path.remove(str(tool_root))
        except ValueError:
            pass
    return connect_readonly(path)


def _write_bytes(stream: Any, value: bytes) -> None:
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(value)
        binary.flush()
    else:
        stream.write(value.decode("utf-8", errors="replace"))
        stream.flush()


def _configure_output_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
