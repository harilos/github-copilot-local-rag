from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Sequence
from urllib.parse import quote

from help_links import MANAGER_HELP_URL

from .freshness import add_freshness


_POINTER_SCHEMAS = {
    "rag-result-pointer-v1",
    "rag-detail-pointer-v1",
}
_RESULT_LISTS = (
    "evidence",
    "contexts",
    "background_context",
    "related_context",
    "document_results",
    "_result_detail_items",
    "expanded_items",
)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_output_streams()
    started = time.monotonic()
    arguments = list(sys.argv[1:] if argv is None else argv)
    mode, parsed = _parse_arguments(arguments)
    public_timeout = (
        float(parsed.timeout)
        if mode == "search"
        else 15.0
    )
    deadline = (
        started + public_timeout
        if public_timeout > 0
        else None
    )
    rag_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["LOCAL_RAG_WRAPPER_INTERNAL"] = "1"
    if mode == "detail":
        lower = rag_root / "query" / "result_detail.py"
        child_arguments = arguments
        catalog_before: str | None = None
    else:
        lower = rag_root / "query" / "search.py"
        child_arguments = _internal_search_arguments(arguments)
        explicit_root = _database_root(rag_root, str(parsed.db or ""))
        catalog_before = (
            _catalog_path_sources(explicit_root)[1]
            if explicit_root is not None
            else None
        )
    if deadline is None:
        process_timeout: float | None = None
    else:
        serialization_reserve = min(
            1.0,
            max(0.01, public_timeout * 0.05),
        )
        process_timeout = max(
            0.01,
            deadline - time.monotonic() - serialization_reserve,
        )
    try:
        completed = subprocess.run(
            [sys.executable, str(lower), *child_arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
            timeout=process_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _write_bytes(sys.stderr, exc.stderr or b"")
        _print_json(
            _wrapper_timeout_payload(mode, parsed, public_timeout),
            ascii_safe=False,
        )
        return 124
    _write_bytes(sys.stderr, completed.stderr)
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _write_bytes(sys.stdout, completed.stdout)
        return int(completed.returncode)
    if not isinstance(payload, dict):
        _write_bytes(sys.stdout, completed.stdout)
        return int(completed.returncode)
    if deadline is not None and time.monotonic() >= deadline:
        _print_json(
            _wrapper_timeout_payload(mode, parsed, public_timeout),
            ascii_safe=False,
        )
        return 124

    if mode == "detail":
        db_name = _detail_database_name(payload)
        output = copy.deepcopy(payload)
        _collapse_links_to_uri(output)
        _strip_private_fields(output)
        output = add_freshness(
            output,
            _database_root(rag_root, db_name),
        )
        _print_json(output, ascii_safe=_is_pointer(output))
        return int(completed.returncode)

    resolution_payload = (
        _compact_payload(payload, rag_root, explain=bool(parsed.explain))
        if parsed.compact_json and parsed.result_delivery == "stdout"
        else payload
    )
    enriched = _resolve_source_uris(
        resolution_payload,
        rag_root=rag_root,
        db_name=_payload_database_name(payload, parsed.db),
        explain=bool(parsed.explain),
        expected_catalog_fingerprint=catalog_before,
    )
    _discard_uris_if_catalog_changed(
        enriched,
        rag_root=rag_root,
        db_name=_payload_database_name(enriched, parsed.db),
        expected_fingerprint=catalog_before,
    )
    _strip_private_fields(enriched)
    if deadline is not None and time.monotonic() >= deadline:
        _print_json(
            _wrapper_timeout_payload(mode, parsed, public_timeout),
            ascii_safe=False,
        )
        return 124
    if parsed.result_delivery == "file" and enriched.get("status") != "busy":
        # The public result set is published exactly once.  Recheck immediately
        # before publication and fail open to path-only results if the catalog
        # changed while URI resolution was in progress.
        _discard_uris_if_catalog_changed(
            enriched,
            rag_root=rag_root,
            db_name=_payload_database_name(enriched, parsed.db),
            expected_fingerprint=catalog_before,
        )
        pointer = _publish_bundle(enriched, rag_root)
        output = add_freshness(
            pointer,
            _database_root(
                rag_root,
                _payload_database_name(enriched, parsed.db),
            ),
        )
        _print_json(output, ascii_safe=True)
        return int(completed.returncode)
    if parsed.format == "prompt" and not parsed.compact_json:
        _print_prompt(enriched, rag_root, explain=bool(parsed.explain))
        return int(completed.returncode)
    output = copy.deepcopy(enriched)
    output.pop("_result_detail_items", None)
    output = add_freshness(
        output,
        _database_root(
            rag_root,
            _payload_database_name(output, parsed.db),
        ),
    )
    _print_json(output, ascii_safe=_is_pointer(output))
    return int(completed.returncode)


def _parse_arguments(
    arguments: list[str],
) -> tuple[str, argparse.Namespace]:
    detail_options = {
        "--result-set-id",
        "--item-id",
        "--detail-level",
    }
    detail = any(
        argument.split("=", 1)[0] in detail_options
        for argument in arguments
    )
    if detail:
        parser = argparse.ArgumentParser(
            description=(
                "再検索せず、既存のLocal RAG検索結果から詳細を取得します。"
            ),
            epilog=(
                "Local RAG Managerと日本語ガイド:\n"
                f"{MANAGER_HELP_URL}"
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
            allow_abbrev=False,
        )
        parser.add_argument("--result-set-id", required=True)
        parser.add_argument("--item-id", action="append", default=[])
        parser.add_argument(
            "--detail-level",
            choices=["expanded", "deep"],
            default="expanded",
        )
        parser.add_argument(
            "--result-delivery",
            choices=["file", "stdout"],
            default="file" if sys.platform.startswith("win") else "stdout",
        )
        parsed = parser.parse_args(arguments)
        maximum = 1 if parsed.detail_level == "deep" else 3
        if len(parsed.item_id) > maximum:
            parser.error(
                f"--detail-level {parsed.detail_level} accepts at most "
                f"{maximum} --item-id value(s)"
            )
        return "detail", parsed
    parser = _search_parser()
    parsed = parser.parse_args(arguments)
    _validate_search_arguments(parser, parsed)
    return "search", parsed


def _wrapper_timeout_payload(
    mode: str,
    parsed: argparse.Namespace,
    timeout: float,
) -> dict[str, Any]:
    if mode == "detail":
        return {
            "schema_version": "rag-detail-pointer-v1",
            "status": "error",
            "error": "detail command timed out",
            "error_kind": "timeout",
            "result_set_id": str(parsed.result_set_id),
        }
    question = " ".join(str(value) for value in parsed.question)
    return {
        "schema": "local-rag.search.v1",
        "status": "error",
        "error": (
            "search process did not terminate within "
            f"{timeout:g} seconds"
        ),
        "error_kind": "timeout",
        "db": str(parsed.db or ""),
        "query": question,
    }


def _search_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="選択したLocal RAGデータベースを1回検索します。",
        epilog=(
            "Local RAG Managerと日本語ガイド:\n"
            f"{MANAGER_HELP_URL}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("question", nargs="*")
    parser.add_argument("--db")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--budget-tokens", type=int, default=0)
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("RAG_QUERY_TIMEOUT_SECONDS", "15")),
    )
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument(
        "--format",
        choices=["json", "prompt"],
        default="prompt",
    )
    parser.add_argument("--compact-json", action="store_true")
    parser.add_argument(
        "--result-delivery",
        choices=["stdout", "file"],
        default="stdout",
    )
    parser.add_argument("--include-db-hint", action="store_true")
    parser.add_argument(
        "--retrieval-mode",
        choices=["hybrid", "lexical", "dense"],
        default="hybrid",
    )
    parser.add_argument(
        "--disable-identifier-diagnostics",
        action="store_true",
    )
    parser.add_argument("--no-daemon", action="store_true")
    parser.add_argument("--daemon-idle-timeout", type=int)
    parser.add_argument("--daemon-attempt-timeout", type=float)
    parser.add_argument(
        "--daemon-fallback",
        choices=["on", "off"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--require-daemon", action="store_true")
    parser.add_argument("--request-json", action="store_true")
    parser.add_argument(
        "--answer-goal",
        choices=[
            "comparison",
            "definition",
            "evidence",
            "history",
            "procedure",
            "survey",
        ],
    )
    parser.add_argument("--literal-identifier", action="append", default=[])
    parser.add_argument("--entity", action="append", default=[])
    parser.add_argument("--facet", action="append", default=[])
    parser.add_argument(
        "--semantic-hypothesis",
        action="append",
        default=[],
    )
    return parser


def _validate_search_arguments(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    bounded = (
        ("--literal-identifier", args.literal_identifier, 3),
        ("--entity", args.entity, 5),
        ("--facet", args.facet, 4),
        ("--semantic-hypothesis", args.semantic_hypothesis, 3),
    )
    for option, values, maximum in bounded:
        if len(values) > maximum:
            parser.error(f"{option} accepts at most {maximum} values")
    if args.request_json:
        if not args.stdin:
            parser.error("--request-json requires --stdin")
        if args.question:
            parser.error(
                "--request-json does not accept a positional question"
            )
        if any(
            (
                args.answer_goal,
                args.literal_identifier,
                args.entity,
                args.facet,
                args.semantic_hypothesis,
            )
        ):
            parser.error(
                "--request-json cannot be combined with planning arguments"
            )


def _internal_search_arguments(arguments: list[str]) -> list[str]:
    output: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        option = value.split("=", 1)[0]
        if option == "--compact-json":
            index += 1
            continue
        if option in {"--format", "--result-delivery"}:
            index += 1 if "=" in value else 2
            continue
        output.append(value)
        index += 1
    output.extend(["--format", "json", "--result-delivery", "stdout"])
    return output


def _resolve_source_uris(
    payload: dict[str, Any],
    *,
    rag_root: Path,
    db_name: str,
    explain: bool,
    expected_catalog_fingerprint: str | None = "",
) -> dict[str, Any]:
    if not db_name:
        output = copy.deepcopy(payload)
        _strip_private_fields(output)
        return output
    db_root = _database_root(rag_root, db_name)
    if db_root is None:
        output = copy.deepcopy(payload)
        _strip_private_fields(output)
        return output
    output = copy.deepcopy(payload)
    _remove_uris(output)
    source_ids, catalog_fingerprint = _catalog_path_sources(db_root)
    if expected_catalog_fingerprint is None:
        return output
    if (
        expected_catalog_fingerprint
        and catalog_fingerprint != expected_catalog_fingerprint
    ):
        return output
    _inject_source_ids(output, source_ids)
    tool_root = rag_root / "gen_db" / "software_rag_tool"
    sys.path.insert(0, str(tool_root))
    try:
        from software_rag_tool.source_links import enrich_search_payload

        output = enrich_search_payload(
            output,
            db_root,
            db_name,
            explain=explain,
        )
    except Exception:
        output = copy.deepcopy(payload)
        _remove_uris(output)
    finally:
        try:
            sys.path.remove(str(tool_root))
        except ValueError:
            pass
    _collapse_links_to_uri(output)
    _current_sources, current_fingerprint = _catalog_path_sources(db_root)
    if catalog_fingerprint != current_fingerprint:
        _remove_uris(output)
    return output


def _discard_uris_if_catalog_changed(
    payload: dict[str, Any],
    *,
    rag_root: Path,
    db_name: str,
    expected_fingerprint: str | None,
) -> bool:
    db_root = _database_root(rag_root, db_name)
    if db_root is None or expected_fingerprint is None:
        _remove_uris(payload)
        return True
    _sources, current = _catalog_path_sources(db_root)
    if current != expected_fingerprint:
        _remove_uris(payload)
        return True
    return False


def _catalog_path_sources(
    db_root: Path,
) -> tuple[dict[str, dict[str, Any]], str]:
    catalog = db_root / "catalog.sqlite"
    try:
        catalog_stat = catalog.lstat()
    except OSError:
        return {}, "unavailable"
    if (
        stat.S_ISLNK(catalog_stat.st_mode)
        or not stat.S_ISREG(catalog_stat.st_mode)
    ):
        return {}, "unavailable"
    for companion in (
        Path(str(catalog) + "-wal"),
        Path(str(catalog) + "-shm"),
    ):
        try:
            companion_stat = companion.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return {}, "unavailable"
        if (
            stat.S_ISLNK(companion_stat.st_mode)
            or not stat.S_ISREG(companion_stat.st_mode)
        ):
            return {}, "unavailable"
    try:
        with _connect_readonly(catalog) as connection:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(document)"
                )
            }
            if not {"source_id", "path"}.issubset(columns):
                return {}, "unavailable"
            content_hash = (
                "content_hash" if "content_hash" in columns else "NULL"
            )
            visibility = (
                "visible_until IS NULL"
                if "visible_until" in columns
                else "1=1"
            )
            rows = connection.execute(
                f"""
                SELECT source_id, path, {content_hash} AS content_hash
                FROM document
                WHERE {visibility}
                  AND source_id IS NOT NULL
                  AND TRIM(source_id) <> ''
                ORDER BY path, source_id
                """
            ).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        return {}, "unavailable"
    values: dict[str, dict[str, set[str]]] = {}
    fingerprint = hashlib.sha256()
    for source_id, path, content_hash_value in rows:
        fingerprint.update(
            json.dumps(
                [
                    str(source_id or ""),
                    str(path or ""),
                    str(content_hash_value or ""),
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        fingerprint.update(b"\n")
        normalized = _canonical_relative_path(path)
        if normalized:
            entry = values.setdefault(
                normalized,
                {"source_ids": set(), "content_hashes": set()},
            )
            entry["source_ids"].add(str(source_id))
            if content_hash_value:
                entry["content_hashes"].add(str(content_hash_value))
    stat_parts: list[object] = []
    for candidate in (catalog, Path(str(catalog) + "-wal")):
        try:
            metadata = candidate.stat()
            stat_parts.append(
                (
                    candidate.name,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                )
            )
        except OSError:
            stat_parts.append((candidate.name, "missing"))
    fingerprint.update(repr(stat_parts).encode("utf-8"))
    public = {
        path: {
            "source_id": next(iter(value["source_ids"])),
            "content_hashes": frozenset(value["content_hashes"]),
        }
        for path, value in values.items()
        if len(value["source_ids"]) == 1
    }
    return public, fingerprint.hexdigest()


def _inject_source_ids(
    payload: dict[str, Any],
    source_ids: dict[str, dict[str, Any]],
) -> None:
    for key in _RESULT_LISTS:
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            source_value = source if isinstance(source, dict) else None
            path = _canonical_relative_path(
                source_value.get("path")
                if source_value is not None
                else item.get("path")
            )
            candidate = source_ids.get(path)
            if not candidate:
                continue
            if source_value is not None:
                revision = str(source_value.get("revision") or "")
                hashes = candidate["content_hashes"]
                if (
                    revision.startswith("sha256:")
                    and hashes
                    and revision.removeprefix("sha256:") not in hashes
                ):
                    continue
                source_value["_source_id"] = candidate["source_id"]
            else:
                item["_source_id"] = candidate["source_id"]


def _canonical_relative_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if (
        not text
        or PurePosixPath(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or bool(PureWindowsPath(text).drive)
    ):
        return ""
    parts = PurePosixPath(text.strip("/")).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return ""
    return PurePosixPath(*parts).as_posix()


def _collapse_links_to_uri(payload: dict[str, Any]) -> None:
    for key in _RESULT_LISTS:
        for item in payload.get(key) or []:
            if not isinstance(item, dict):
                continue
            existing = str(item.pop("uri", "") or "")
            final = str(
                item.pop("source_permalink", "")
                or item.pop("source_url", "")
                or existing
                or ""
            )
            item.pop("source_provider", None)
            item.pop("source_link_status", None)
            item.pop("source_link_error", None)
            if final:
                item["uri"] = final


def _remove_uris(payload: dict[str, Any]) -> None:
    for key in _RESULT_LISTS:
        for item in payload.get(key) or []:
            if isinstance(item, dict):
                for field in (
                    "uri",
                    "source_provider",
                    "source_url",
                    "source_permalink",
                    "source_link_status",
                    "source_link_error",
                ):
                    item.pop(field, None)
                source = item.get("source")
                if isinstance(source, dict):
                    for field in (
                        "uri",
                        "source_url",
                        "source_permalink",
                    ):
                        source.pop(field, None)


def _strip_private_fields(payload: dict[str, Any]) -> None:
    for key in _RESULT_LISTS:
        for item in payload.get(key) or []:
            if isinstance(item, dict):
                item.pop("_source_id", None)
                source = item.get("source")
                if isinstance(source, dict):
                    source.pop("_source_id", None)


def _publish_bundle(payload: dict[str, Any], rag_root: Path) -> dict[str, Any]:
    query_root = rag_root / "query"
    sys.path.insert(0, str(query_root))
    try:
        from result_bundle import publish_result_bundle

        return publish_result_bundle(payload)
    finally:
        try:
            sys.path.remove(str(query_root))
        except ValueError:
            pass


def _compact_payload(
    payload: dict[str, Any],
    rag_root: Path,
    *,
    explain: bool,
) -> dict[str, Any]:
    tool_root = rag_root / "gen_db" / "software_rag_tool"
    sys.path.insert(0, str(tool_root))
    try:
        from software_rag_tool.search_api import compact_search_contract

        return compact_search_contract(
            copy.deepcopy(payload),
            explain=explain,
        )
    except Exception:
        return copy.deepcopy(payload)
    finally:
        try:
            sys.path.remove(str(tool_root))
        except ValueError:
            pass


def _print_prompt(
    payload: dict[str, Any],
    rag_root: Path,
    *,
    explain: bool,
) -> None:
    query_root = rag_root / "query"
    sys.path.insert(0, str(query_root))
    try:
        from search_output import payload_to_text

        print(payload_to_text(payload, "prompt", explain=explain))
    finally:
        try:
            sys.path.remove(str(query_root))
        except ValueError:
            pass


def _payload_database_name(payload: dict[str, Any], fallback: object) -> str:
    return str(
        payload.get("selected_db")
        or payload.get("db")
        or fallback
        or ""
    )


def _detail_database_name(payload: dict[str, Any]) -> str:
    result_set_id = str(payload.get("result_set_id") or "")
    if not result_set_id:
        return ""
    if any(character not in "0123456789abcdefABCDEF-" for character in result_set_id):
        return ""
    meta = (
        Path(tempfile.gettempdir())
        / "GitHubCopilotLocalRAG"
        / "results"
        / result_set_id
        / "meta.json"
    )
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    return str(value.get("selected_db") or "") if isinstance(value, dict) else ""


def _database_root(rag_root: Path, db_name: str) -> Path | None:
    if not db_name:
        return None
    dbs_root = Path(
        os.getenv("RAG_DBS_ROOT", str(rag_root / "dbs"))
    ).expanduser()
    candidate = dbs_root / db_name
    if candidate.is_symlink():
        return None
    try:
        resolved_root = dbs_root.resolve()
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved.parent != resolved_root:
        return None
    return resolved


def _is_pointer(payload: dict[str, Any]) -> bool:
    return str(payload.get("schema_version") or "") in _POINTER_SCHEMAS


def _print_json(payload: dict[str, Any], *, ascii_safe: bool) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=ascii_safe,
            separators=(",", ":"),
        )
    )


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


class _ReadonlyConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        self.connection.close()


def _connect_readonly(path: Path) -> _ReadonlyConnection:
    resolved = path.resolve()
    text = str(resolved)
    if text.startswith("\\\\"):
        unc = text.lstrip("\\").replace("\\", "/")
        uri = "file:////" + quote(unc, safe="/:") + "?mode=ro"
    else:
        uri = resolved.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only=ON")
    return _ReadonlyConnection(connection)
