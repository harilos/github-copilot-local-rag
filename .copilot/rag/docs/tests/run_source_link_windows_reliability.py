from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import multiprocessing
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from unittest import mock
from urllib.parse import quote


RESULT_SCHEMA = "rag-source-link-windows-reliability-v2"
FIXTURE_DB_NAME = "fixture-rag"
SOURCE_A = "source-a"
SOURCE_B = "source-b"
SOURCE_MULTI = "source-multi"
OBSERVED_ROOT_A = "Observed Root"
OBSERVED_ROOT_B = "Second Root"
FORBIDDEN_V2_KEYS = {
    "mappings",
    "mapping_id",
    "path_prefix",
}
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
WINDOWS_PATH_PATTERN = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\"'<>]*"
)


class EnvironmentUnavailable(RuntimeError):
    pass


class CaseNotRun(RuntimeError):
    pass


def _tool_module(rag_root: Path) -> Any:
    tool_root = rag_root / "gen_db" / "software_rag_tool"
    if str(tool_root) not in sys.path:
        sys.path.insert(0, str(tool_root))
    from software_rag_tool import source_links

    return source_links


def _manager_module(rag_root: Path) -> Any:
    path = rag_root / "manage.py"
    spec = importlib.util.spec_from_file_location(
        "local_rag_windows_manager",
        path,
    )
    if spec is None or spec.loader is None:
        raise EnvironmentUnavailable("manager module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _link(
    source_web_root: str,
    *,
    source_id: str = SOURCE_A,
    revision: int = 1,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "schema_version": "rag-source-links-v2",
        "revision": revision,
        "sources": [
            {
                "source_id": source_id,
                "provider": "other",
                "enabled": enabled,
                "strategy": "append-relative-path",
                "settings": {"source_web_root": source_web_root},
            }
        ],
    }


def _legacy(prefix: str) -> dict[str, Any]:
    return {
        "schema_version": "rag-source-links-v1",
        "database": FIXTURE_DB_NAME,
        "revision": 1,
        "sources": [
            {
                "source_id": SOURCE_A,
                "mappings": [
                    {
                        "mapping_id": (
                            "00000000-0000-0000-0000-000000000001"
                        ),
                        "enabled": True,
                        "path_prefix": prefix,
                        "provider": "other",
                        "strategy": "append-relative-path",
                        "settings": {
                            "source_web_root": (
                                "https://fixture.example.invalid/root"
                            )
                        },
                    }
                ],
            }
        ],
    }


def _find_forbidden(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_V2_KEYS:
                found.add(key)
            found.update(_find_forbidden(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_find_forbidden(child))
    return found


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(
            payload,
            stream,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".restore.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_text(value: object, redactions: tuple[tuple[str, str], ...]) -> str:
    text = str(value)
    for original, replacement in redactions:
        if original:
            text = text.replace(original, replacement)
            text = text.replace(original.replace("\\", "/"), replacement)
    text = URL_PATTERN.sub("<URL>", text)
    text = WINDOWS_PATH_PATTERN.sub("<WINDOWS_PATH>", text)
    return text[:500]


def _create_catalog(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE document (
                doc_pk INTEGER PRIMARY KEY,
                doc_id TEXT NOT NULL UNIQUE,
                source_id TEXT,
                path TEXT NOT NULL,
                title TEXT,
                updated_at TEXT,
                visible_until INTEGER
            );
            CREATE TABLE chunk (
                chunk_pk INTEGER PRIMARY KEY,
                chunk_uid TEXT NOT NULL UNIQUE,
                doc_pk INTEGER NOT NULL,
                visible_until INTEGER
            );
            """
        )
        documents = [
            (
                1,
                "doc-a-1",
                SOURCE_A,
                f"{OBSERVED_ROOT_A}/docs/設計 仕様 #1 (最終).md",
                None,
            ),
            (
                2,
                "doc-a-2",
                SOURCE_A,
                f"{OBSERVED_ROOT_A}/space dir/file name.txt",
                None,
            ),
            (
                3,
                "doc-a-3",
                SOURCE_A,
                f"{OBSERVED_ROOT_A}/emoji/🚀📚-😀.md",
                None,
            ),
            (
                4,
                "doc-a-4",
                SOURCE_A,
                rf"{OBSERVED_ROOT_A}\mixed/separator\child.txt",
                None,
            ),
            (
                5,
                "doc-a-hidden",
                SOURCE_A,
                "Hidden Root/must-not-affect-root.txt",
                2,
            ),
            (
                6,
                "doc-b-1",
                SOURCE_B,
                f"{OBSERVED_ROOT_B}/shared.txt",
                None,
            ),
            (
                7,
                "doc-multi-1",
                SOURCE_MULTI,
                "First Root/a.txt",
                None,
            ),
            (
                8,
                "doc-multi-2",
                SOURCE_MULTI,
                "Another Root/b.txt",
                None,
            ),
        ]
        for doc_pk, doc_id, source_id, stored_path, visible_until in documents:
            connection.execute(
                """
                INSERT INTO document(
                    doc_pk, doc_id, source_id, path, title,
                    updated_at, visible_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_pk,
                    doc_id,
                    source_id,
                    stored_path,
                    doc_id,
                    "2026-01-01T00:00:00+00:00",
                    visible_until,
                ),
            )
            connection.execute(
                "INSERT INTO chunk VALUES (?, ?, ?, ?)",
                (doc_pk, f"chunk-{doc_pk}", doc_pk, visible_until),
            )
        connection.commit()
    finally:
        connection.close()


def _search_payload(path: str, source_id: str = SOURCE_A) -> dict[str, Any]:
    return {
        "status": "ok",
        "answerability": "full",
        "evidence": [
            {
                "id": "E1",
                "_source_id": source_id,
                "source": {"path": path},
                "text": "Synthetic evidence.",
            }
        ],
        "background_context": [],
        "related_context": [],
        "document_results": [
            {
                "id": "D1",
                "_source_id": source_id,
                "path": path,
            }
        ],
    }


def _require_windows(args: argparse.Namespace) -> None:
    if os.name == "nt":
        return
    if args.synthetic_smoke:
        raise CaseNotRun("Windows-only case omitted by synthetic smoke")
    raise EnvironmentUnavailable("Windows is required")


def _concurrent_save_worker(
    rag_root: str,
    db_root: str,
    barrier: Any,
    output: Any,
    index: int,
) -> None:
    try:
        module = _tool_module(Path(rag_root))
        loaded = module.load_source_links(Path(db_root), FIXTURE_DB_NAME)
        if loaded.payload is None:
            raise RuntimeError("missing sidecar")
        payload = loaded.payload
        revision = int(payload["revision"])
        payload["revision"] = revision + 1
        payload["sources"][0]["settings"]["source_web_root"] = (
            f"https://writer-{index}.example.invalid/root"
        )
        barrier.wait(timeout=10)
        module.save_source_links(
            Path(db_root),
            payload,
            db_name=FIXTURE_DB_NAME,
            existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
            expected_revision=revision,
            expected_etag=loaded.etag,
        )
    except Exception as exc:
        output.put(("error", type(exc).__name__))
    else:
        output.put(("success", index))


def _dead_lock_worker(
    rag_root: str,
    lock_path: str,
    ready: Any,
) -> None:
    module = _tool_module(Path(rag_root))
    handle = module._acquire_lock(Path(lock_path))
    ready.set()
    try:
        time.sleep(300)
    finally:
        module._release_lock(handle)


def _interrupted_save_worker(
    rag_root: str,
    db_root: str,
    ready: Any,
) -> None:
    module = _tool_module(Path(rag_root))
    root = Path(db_root)
    loaded = module.load_source_links(root, FIXTURE_DB_NAME)
    if loaded.payload is None:
        raise RuntimeError("missing sidecar")
    payload = loaded.payload
    payload["revision"] = int(payload["revision"]) + 1
    original_write = module._write_bytes
    paused = False

    def write_then_pause(path: Path, value: bytes) -> None:
        nonlocal paused
        original_write(path, value)
        name = path.name
        if (
            not paused
            and name.startswith(f".{module.SIDECAR_NAME}.")
            and name.endswith(".tmp")
            and module.BACKUP_NAME not in name
        ):
            paused = True
            ready.set()
            time.sleep(300)

    module._write_bytes = write_then_pause
    module.save_source_links(
        root,
        payload,
        db_name=FIXTURE_DB_NAME,
        existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
        expected_revision=loaded.revision,
        expected_etag=loaded.etag,
    )


class Results:
    def __init__(
        self,
        output_root: Path,
        *,
        redactions: tuple[tuple[str, str], ...],
    ) -> None:
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.case_path = self.output_root / "cases.jsonl"
        self.case_path.unlink(missing_ok=True)
        self.records: list[dict[str, Any]] = []
        self.redactions = redactions

    def case(
        self,
        case_id: str,
        action: Callable[[], dict[str, Any] | None],
        *,
        incomplete_on: tuple[type[BaseException], ...] = (),
        required: bool = True,
    ) -> bool:
        started = time.monotonic()
        record: dict[str, Any] = {
            "case_id": case_id,
            "required": required,
        }
        try:
            details = action() or {}
        except CaseNotRun as exc:
            record.update(
                {
                    "result": "NOT_RUN",
                    "error_kind": type(exc).__name__,
                }
            )
        except EnvironmentUnavailable as exc:
            record.update(
                {
                    "result": "INCOMPLETE_ENV",
                    "error_kind": type(exc).__name__,
                    "error_detail": _safe_text(exc, self.redactions),
                }
            )
        except incomplete_on as exc:
            record.update(
                {
                    "result": "INCOMPLETE_ENV",
                    "error_kind": type(exc).__name__,
                    "error_detail": _safe_text(exc, self.redactions),
                }
            )
        except Exception as exc:
            record.update(
                {
                    "result": "FAIL",
                    "error_kind": type(exc).__name__,
                    "error_detail": _safe_text(exc, self.redactions),
                }
            )
        else:
            record.update({"result": "PASS", **details})
        record["elapsed_seconds"] = round(
            time.monotonic() - started,
            6,
        )
        self.records.append(record)
        with self.case_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
        return record["result"] == "PASS"

    def finish(
        self,
        preflight: dict[str, Any],
        *,
        synthetic_smoke: bool,
    ) -> dict[str, Any]:
        counts = {
            key: sum(record["result"] == key for record in self.records)
            for key in ("PASS", "FAIL", "INCOMPLETE_ENV", "NOT_RUN")
        }
        required_incomplete = any(
            record["required"]
            and record["result"] in {"INCOMPLETE_ENV", "NOT_RUN"}
            for record in self.records
        )
        if counts["FAIL"]:
            status = "FAIL"
        elif required_incomplete and not synthetic_smoke:
            status = "INCOMPLETE_ENV"
        else:
            status = "PASS"
        summary = {
            "schema_version": RESULT_SCHEMA,
            "status": status,
            "counts": counts,
            "preflight": preflight,
            "cases": len(self.records),
            "run_mode": (
                "synthetic-smoke" if synthetic_smoke else "windows-p0"
            ),
        }
        _atomic_json(self.output_root / "summary.json", summary)
        return summary


def _preflight(rag_root: Path, runtime: Path) -> dict[str, Any]:
    return {
        "platform": sys.platform,
        "python": (
            f"{sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        ),
        "installed_layout": (
            (rag_root / "manage.py").is_file()
            and (
                rag_root
                / "gen_db/software_rag_tool/software_rag_tool/source_links.py"
            ).is_file()
        ),
        "runtime_exists": runtime.is_file(),
        "windows": os.name == "nt",
    }


def run(args: argparse.Namespace) -> int:
    rag_root = args.installed_rag.expanduser().resolve()
    runtime = (
        rag_root / "query/.venv/Scripts/python.exe"
        if os.name == "nt"
        else rag_root / "query/.venv/bin/python"
    )
    module = _tool_module(rag_root)
    output_root = args.output.expanduser().resolve()
    fixture_parent = Path(
        tempfile.mkdtemp(prefix="RAG Source Link v2 試験 🚀 ")
    )
    fixture = fixture_parent / FIXTURE_DB_NAME
    fixture.mkdir()
    catalog = fixture / "catalog.sqlite"
    _create_catalog(catalog)
    catalog_before = _hash(catalog)
    results = Results(
        output_root,
        redactions=(
            (str(Path.home()), "<HOME>"),
            (str(rag_root), "<RAG_ROOT>"),
            (str(output_root), "<OUTPUT>"),
            (str(fixture_parent), "<FIXTURE>"),
        ),
    )
    preflight = _preflight(rag_root, runtime)
    _atomic_json(output_root / "preflight.json", preflight)

    def v2_shape() -> dict[str, Any]:
        payload = _link("https://fixture.example.invalid/root")
        normalized = module.validate_source_links(
            payload,
            existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
        )
        if set(normalized["sources"][0]) != {
            "source_id",
            "provider",
            "enabled",
            "strategy",
            "settings",
        }:
            raise AssertionError("v2 Source shape is not one complete link")
        if "database" in normalized or _find_forbidden(normalized):
            raise AssertionError("v2 normalization retained a legacy field")
        for field, value in (
            ("mappings", []),
            ("mapping_id", "legacy"),
            ("path_prefix", "Legacy Root/"),
        ):
            invalid = _link("https://fixture.example.invalid/root")
            invalid["sources"][0][field] = value
            try:
                module.validate_source_links(invalid)
            except module.SourceLinkError:
                pass
            else:
                raise AssertionError(f"v2 accepted forbidden field {field}")
        invalid_database = _link("https://fixture.example.invalid/root")
        invalid_database["database"] = FIXTURE_DB_NAME
        try:
            module.validate_source_links(invalid_database)
        except module.SourceLinkError:
            pass
        else:
            raise AssertionError("v2 accepted database")
        missing_strategy = _link("https://fixture.example.invalid/root")
        del missing_strategy["sources"][0]["strategy"]
        try:
            module.validate_source_links(missing_strategy)
        except module.SourceLinkError:
            pass
        else:
            raise AssertionError("v2 accepted a link without strategy")
        create_root = fixture_parent / "new-sidecar-rag"
        create_root.mkdir()
        _create_catalog(create_root / "catalog.sqlite")
        module.save_source_links(
            create_root,
            payload,
            existing_sources={SOURCE_A},
            expected_revision=0,
            expected_etag="missing",
        )
        created = module.load_source_links(create_root)
        if created.payload is None or created.revision != 1:
            raise AssertionError("0/missing compare-and-swap create failed")
        attempted_update = created.payload
        attempted_update["revision"] = 2
        try:
            module.save_source_links(
                create_root,
                attempted_update,
                existing_sources={SOURCE_A},
            )
        except module.SourceLinkError:
            pass
        else:
            raise AssertionError("save accepted missing compare-and-swap inputs")
        return {
            "compare_and_swap_required": True,
            "database_absent": True,
            "forbidden_keys": 0,
            "strategy_required": True,
        }

    results.case("V2-001-exact-source-shape", v2_shape)

    def v2_migration() -> dict[str, Any]:
        current_path = fixture / module.SIDECAR_NAME
        matching_legacy = _legacy(f"{OBSERVED_ROOT_A}/")
        _atomic_json(current_path, matching_legacy)
        matched = module.load_source_links(
            fixture,
            FIXTURE_DB_NAME,
        )
        if matched.status != "configured" or matched.payload is None:
            raise AssertionError("matching legacy Source was not configured")
        if not matched.migration_required:
            raise AssertionError("legacy sidecar did not require migration")
        if (
            dict(matched.source_statuses).get(SOURCE_A)
            != "legacy_migration_available"
        ):
            raise AssertionError("matching legacy root status was not exposed")
        relative = module.enrich_search_payload(
            _search_payload(
                f"{OBSERVED_ROOT_A}/docs/設計 仕様 #1 (最終).md"
            ),
            fixture,
            FIXTURE_DB_NAME,
        )
        url = relative["evidence"][0].get("source_url", "")
        if not url or "Observed%20Root" in url:
            raise AssertionError("observed root was not removed exactly once")

        _atomic_json(current_path, _legacy("Different Root/"))
        mismatched = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if (
            dict(mismatched.source_statuses).get(SOURCE_A)
            != "legacy_root_mismatch"
        ):
            raise AssertionError("legacy root mismatch was not reported")
        if any(
            source.get("provider")
            for source in (mismatched.payload or {}).get("sources", [])
        ):
            raise AssertionError("mismatched legacy root remained configured")

        wrong_database = json.loads(json.dumps(matching_legacy))
        wrong_database["database"] = "other-fixture-rag"
        _atomic_json(current_path, wrong_database)
        wrong_database_loaded = module.load_source_links(
            fixture,
            FIXTURE_DB_NAME,
        )
        if (
            wrong_database_loaded.status != "invalid"
            or wrong_database_loaded.payload is not None
        ):
            raise AssertionError(
                "legacy sidecar from another database was accepted"
            )

        _atomic_json(current_path, matching_legacy)
        matched = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if matched.payload is None:
            raise AssertionError("matching legacy payload disappeared")
        matched.payload["revision"] = 2
        module.save_source_links(
            fixture,
            matched.payload,
            db_name=FIXTURE_DB_NAME,
            existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
            expected_revision=1,
            expected_etag=matched.etag,
        )
        saved = json.loads(current_path.read_text(encoding="utf-8"))
        backup = json.loads(
            (fixture / module.BACKUP_NAME).read_text(encoding="utf-8")
        )
        if _find_forbidden(saved) or "database" in saved:
            raise AssertionError("forbidden v2 keys were persisted")
        if saved.get("schema_version") != module.SCHEMA_VERSION:
            raise AssertionError("legacy save did not publish v2")
        if backup.get("schema_version") != module.LEGACY_SCHEMA_VERSION:
            raise AssertionError("raw legacy sidecar was not retained in backup")
        return {
            "forbidden_keys": 0,
            "legacy_backup_retained": True,
            "matching_root_only": True,
        }

    results.case("V2-002-legacy-root-gated-migration", v2_migration)

    def full_paths() -> dict[str, Any]:
        paths = [
            f"{OBSERVED_ROOT_A}/docs/設計 仕様 #1 (最終).md",
            f"{OBSERVED_ROOT_A}/space dir/file name.txt",
            f"{OBSERVED_ROOT_A}/emoji/🚀📚-😀.md",
            rf"{OBSERVED_ROOT_A}\mixed/separator\child.txt",
        ]
        loaded = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if loaded.payload is None:
            raise AssertionError("v2 sidecar missing")
        source = loaded.payload["sources"][0]
        previews = module.resolve_mapping_preview(source, paths)
        if len(previews) != len(paths):
            raise AssertionError("preview count mismatch")
        if not all(value.get("source_url") for value in previews):
            raise AssertionError("a valid path did not resolve")
        if any(
            "Observed%20Root" in str(value.get("source_url"))
            for value in previews
        ):
            raise AssertionError("preview included the observed root")
        observed = module.read_visible_observed_roots(fixture)
        if observed.get(SOURCE_A) != (f"{OBSERVED_ROOT_A}/",):
            raise AssertionError("visible observed root derivation was incorrect")
        if "Hidden Root/" in observed.get(SOURCE_A, ()):
            raise AssertionError("hidden document affected the observed root")
        if len(observed.get(SOURCE_MULTI, ())) != 2:
            raise AssertionError("multiple observed roots were not detected")
        for invalid in ("../outside", "C:/absolute/file.txt", "//host/share"):
            try:
                module.resolve_mapping_preview(source, [invalid])
            except module.SourceLinkError:
                continue
            raise AssertionError("unsafe stored path was accepted")
        for stored_path in paths:
            enriched = module.enrich_search_payload(
                _search_payload(stored_path),
                fixture,
                FIXTURE_DB_NAME,
            )
            if not enriched["evidence"][0].get("source_url"):
                raise AssertionError("catalog-derived root did not resolve")
        return {
            "hidden_documents_ignored": True,
            "paths": len(paths),
            "source_relative": True,
            "utf8": True,
        }

    results.case("V2-003-catalog-root-and-source-relative-path", full_paths)

    def source_identity_and_fail_open() -> dict[str, Any]:
        loaded = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if loaded.payload is None:
            raise AssertionError("v2 sidecar missing")
        payload = loaded.payload
        payload["revision"] = loaded.revision + 1
        payload["sources"].extend(
            [
                {
                    "source_id": SOURCE_B,
                    "provider": "other",
                    "enabled": True,
                    "strategy": "append-relative-path",
                    "settings": {
                        "source_web_root": (
                            "https://second.example.invalid/base"
                        )
                    },
                },
            ]
        )
        module.save_source_links(
            fixture,
            payload,
            db_name=FIXTURE_DB_NAME,
            existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
            expected_revision=loaded.revision,
            expected_etag=loaded.etag,
        )
        first = module.enrich_search_payload(
            _search_payload(f"{OBSERVED_ROOT_A}/space dir/file name.txt"),
            fixture,
            FIXTURE_DB_NAME,
        )
        second = module.enrich_search_payload(
            _search_payload(f"{OBSERVED_ROOT_B}/shared.txt", SOURCE_B),
            fixture,
            FIXTURE_DB_NAME,
        )
        if (
            "fixture.example.invalid"
            not in first["evidence"][0].get("source_url", "")
            or "second.example.invalid"
            not in second["evidence"][0].get("source_url", "")
        ):
            raise AssertionError("source_id did not select one configuration")

        before_multi = module.load_source_links(fixture, FIXTURE_DB_NAME)
        assert before_multi.payload is not None
        attempted = json.loads(json.dumps(before_multi.payload))
        attempted["revision"] = before_multi.revision + 1
        attempted["sources"].append(
            {
                "source_id": SOURCE_MULTI,
                "provider": "other",
                "enabled": True,
                "strategy": "append-relative-path",
                "settings": {
                    "source_web_root": (
                        "https://multiple.example.invalid/base"
                    )
                },
            }
        )
        try:
            module.save_source_links(
                fixture,
                attempted,
                db_name=FIXTURE_DB_NAME,
                existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
                expected_revision=before_multi.revision,
                expected_etag=before_multi.etag,
            )
        except module.SourceLinkError:
            pass
        else:
            raise AssertionError("multiple-root Source configuration was saved")
        after_reject = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if after_reject.revision != before_multi.revision:
            raise AssertionError("rejected multiple-root save changed current")

        raw_before_multi = (fixture / module.SIDECAR_NAME).read_bytes()
        hand_edited = json.loads(json.dumps(before_multi.payload))
        hand_edited["sources"].append(
            {
                "source_id": SOURCE_MULTI,
                "provider": "other",
                "enabled": True,
                "strategy": "append-relative-path",
                "settings": {
                    "source_web_root": (
                        "https://multiple.example.invalid/base"
                    )
                },
            }
        )
        _atomic_json(fixture / module.SIDECAR_NAME, hand_edited)
        ambiguous = module.enrich_search_payload(
            _search_payload("First Root/a.txt", SOURCE_MULTI),
            fixture,
            FIXTURE_DB_NAME,
            explain=True,
        )
        if ambiguous["evidence"][0].get("source_url"):
            raise AssertionError("multiple roots generated a URL")
        if (
            ambiguous["evidence"][0].get("source_link_status")
            != "multiple_observed_roots"
        ):
            raise AssertionError("multiple roots did not fail open explicitly")
        _atomic_bytes(fixture / module.SIDECAR_NAME, raw_before_multi)
        unknown = module.enrich_search_payload(
            _search_payload(
                f"{OBSERVED_ROOT_A}/space dir/file name.txt",
                "unknown-source",
            ),
            fixture,
            FIXTURE_DB_NAME,
        )
        if unknown["evidence"][0].get("source_url"):
            raise AssertionError("unknown Source generated a URL")

        loaded = module.load_source_links(fixture, FIXTURE_DB_NAME)
        assert loaded.payload is not None
        loaded.payload["revision"] = loaded.revision + 1
        loaded.payload["sources"][0]["enabled"] = False
        module.save_source_links(
            fixture,
            loaded.payload,
            db_name=FIXTURE_DB_NAME,
            existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
            expected_revision=loaded.revision,
            expected_etag=loaded.etag,
        )
        disabled = module.enrich_search_payload(
            _search_payload(f"{OBSERVED_ROOT_A}/space dir/file name.txt"),
            fixture,
            FIXTURE_DB_NAME,
        )
        if disabled["evidence"][0].get("source_url"):
            raise AssertionError("disabled Source generated a URL")
        loaded = module.load_source_links(fixture, FIXTURE_DB_NAME)
        assert loaded.payload is not None
        loaded.payload["revision"] = loaded.revision + 1
        loaded.payload["sources"][0]["enabled"] = True
        module.save_source_links(
            fixture,
            loaded.payload,
            db_name=FIXTURE_DB_NAME,
            existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
            expected_revision=loaded.revision,
            expected_etag=loaded.etag,
        )
        return {
            "disabled_fail_open": True,
            "multiple_roots_fail_open": True,
            "source_identity": True,
            "unknown_fail_open": True,
        }

    results.case(
        "V2-004-source-identity-and-fail-open",
        source_identity_and_fail_open,
    )

    def sequential_saves() -> dict[str, Any]:
        current = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if current.payload is None:
            raise AssertionError("sidecar missing")
        revision = current.revision
        iterations = int(args.save_iterations)
        for index in range(iterations):
            payload = current.payload
            payload["revision"] = revision + 1
            payload["sources"][0]["settings"]["source_web_root"] = (
                "https://a.example.invalid/root"
                if index % 2
                else "https://b.example.invalid/root"
            )
            module.save_source_links(
                fixture,
                payload,
                db_name=FIXTURE_DB_NAME,
                existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
                expected_revision=revision,
                expected_etag=current.etag,
            )
            revision += 1
            current = module.load_source_links(fixture, FIXTURE_DB_NAME)
            if current.payload is None or current.revision != revision:
                raise AssertionError("saved sidecar could not be read")
            if "database" in current.payload or _find_forbidden(current.payload):
                raise AssertionError("save emitted a legacy v1 field")
            if any(
                source.get("provider") and not source.get("strategy")
                for source in current.payload["sources"]
            ):
                raise AssertionError("save removed a required strategy")
        saved = json.loads(
            (fixture / module.SIDECAR_NAME).read_text(encoding="utf-8")
        )
        backup = json.loads(
            (fixture / module.BACKUP_NAME).read_text(encoding="utf-8")
        )
        if saved["revision"] != revision:
            raise AssertionError("revision mismatch")
        if backup["revision"] != revision - 1:
            raise AssertionError("backup generation mismatch")
        if _find_forbidden(saved) or "database" in saved:
            raise AssertionError("forbidden v2 keys were persisted")
        return {"iterations": iterations, "final_revision": revision}

    results.case("SC-001-003-sequential-atomic-saves", sequential_saves)

    def backup_restore() -> dict[str, Any]:
        current_path = fixture / module.SIDECAR_NAME
        backup_path = fixture / module.BACKUP_NAME
        newest = current_path.read_bytes()
        newest_payload = json.loads(newest.decode("utf-8"))
        previous = backup_path.read_bytes()
        previous_payload = json.loads(previous.decode("utf-8"))
        if previous_payload["revision"] != newest_payload["revision"] - 1:
            raise AssertionError("backup is not the immediately prior revision")
        current_path.write_bytes(b"{invalid")
        invalid = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if invalid.status != "invalid" or invalid.payload is not None:
            raise AssertionError("invalid current sidecar did not fail open")
        _atomic_bytes(current_path, previous)
        restored = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if restored.payload is None:
            raise AssertionError("backup restore was not readable")
        if restored.revision != previous_payload["revision"]:
            raise AssertionError("backup restore revision mismatch")
        _atomic_bytes(current_path, newest)
        recovered = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if recovered.revision != newest_payload["revision"]:
            raise AssertionError("newest sidecar restore failed")
        return {
            "backup_revision": previous_payload["revision"],
            "manual_atomic_restore": True,
        }

    results.case("SC-004-backup-restore", backup_restore)

    def concurrent_saves() -> dict[str, Any]:
        worker_count = int(args.concurrent_writers)
        if worker_count < 2:
            raise AssertionError("concurrent-writers must be at least 2")
        before = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if before.payload is None:
            raise AssertionError("sidecar missing")
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(worker_count)
        output = context.Queue()
        workers = [
            context.Process(
                target=_concurrent_save_worker,
                args=(
                    str(rag_root),
                    str(fixture),
                    barrier,
                    output,
                    index,
                ),
            )
            for index in range(worker_count)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.kill()
                worker.join(5)
                raise AssertionError("concurrent writer did not exit")
        values: list[tuple[str, Any]] = []
        for _index in range(worker_count):
            try:
                values.append(output.get(timeout=2))
            except queue.Empty:
                break
        successes = sum(value[0] == "success" for value in values)
        if successes != 1:
            raise AssertionError(
                f"expected one successful writer, observed {successes}"
            )
        lock = fixture / ".source-links.lock"
        if not lock.is_file():
            raise AssertionError("persistent writer lock file is missing")
        after = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if after.revision != before.revision + 1:
            raise AssertionError("concurrent save revision was not incremented once")
        return {
            "conflicts": worker_count - successes,
            "successes": successes,
            "writers": worker_count,
        }

    results.case("SC-005-concurrent-save-CAS", concurrent_saves)

    def killed_writer_lock() -> dict[str, Any]:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        worker = context.Process(
            target=_dead_lock_worker,
            args=(
                str(rag_root),
                str(fixture / ".source-links.lock"),
                ready,
            ),
        )
        worker.start()
        if not ready.wait(5):
            worker.kill()
            worker.join(5)
            raise AssertionError("lock worker did not become ready")
        worker.kill()
        worker.join(5)
        loaded = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if loaded.payload is None:
            raise AssertionError("sidecar missing after killed writer")
        revision = loaded.revision
        loaded.payload["revision"] = revision + 1
        started = time.monotonic()
        module.save_source_links(
            fixture,
            loaded.payload,
            db_name=FIXTURE_DB_NAME,
            existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
            expected_revision=revision,
            expected_etag=loaded.etag,
        )
        elapsed = time.monotonic() - started
        if elapsed > 5:
            raise AssertionError("dead writer lock recovery exceeded 5 seconds")
        return {"recovery_seconds": round(elapsed, 6)}

    results.case("SC-006-dead-owner-lock-recovery", killed_writer_lock)

    def opaque_lock_file_uses_kernel_ownership() -> dict[str, Any]:
        lock = fixture / ".source-links.lock"
        raw = b"{opaque legacy bytes"
        lock.write_bytes(raw)
        identity = lock.stat().st_ino
        original_wait = module.LOCK_WAIT_SECONDS
        holder = module._acquire_lock(lock)
        try:
            module.LOCK_WAIT_SECONDS = 0.2
            started = time.monotonic()
            try:
                module._acquire_lock(lock)
            except module.SourceLinkError:
                pass
            else:
                raise AssertionError("active kernel lock was bypassed")
            if time.monotonic() - started > 3:
                raise AssertionError("kernel lock wait was unbounded")
            if lock.stat().st_ino != identity:
                raise AssertionError("persistent lock file identity changed")
        finally:
            module.LOCK_WAIT_SECONDS = original_wait
            module._release_lock(holder)
        if lock.read_bytes() != raw or lock.stat().st_ino != identity:
            raise AssertionError("persistent lock file was modified")
        replacement = module._acquire_lock(lock)
        module._release_lock(replacement)
        return {
            "opaque_contents_preserved": True,
            "stable_inode_preserved": True,
        }

    results.case(
        "SC-006B-persistent-kernel-lock",
        opaque_lock_file_uses_kernel_ownership,
    )

    def windows_byte_range_lock_interoperability() -> dict[str, Any]:
        _require_windows(args)
        import msvcrt

        lock = fixture / ".source-links.lock"
        descriptor = os.open(
            lock,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os.set_inheritable(descriptor, False)
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        original_wait = module.LOCK_WAIT_SECONDS
        try:
            module.LOCK_WAIT_SECONDS = 0.2
            try:
                module._acquire_lock(lock)
            except module.SourceLinkError:
                pass
            else:
                raise AssertionError("msvcrt byte-range lock was bypassed")
        finally:
            module.LOCK_WAIT_SECONDS = original_wait
            os.close(descriptor)
        replacement = module._acquire_lock(lock)
        module._release_lock(replacement)
        return {
            "msvcrt_contention_detected": True,
            "release_by_close_confirmed": True,
        }

    results.case(
        "SC-006C-windows-byte-range-lock",
        windows_byte_range_lock_interoperability,
    )

    def credential_save_rejection() -> dict[str, Any]:
        credential_root = fixture_parent / "credential-fixture-rag"
        credential_root.mkdir()
        _create_catalog(credential_root / "catalog.sqlite")
        rejected = 0
        deeply_encoded = "access_token=value"
        for _index in range(12):
            deeply_encoded = quote(deeply_encoded, safe="")
        values = (
            "https://fixture.example.invalid/bearer=value/root",
            "https://fixture.example.invalid/jwt=value/root",
            "https://fixture.example.invalid/session_token=value/root",
            "https://fixture.example.invalid/root?bearer=value",
            "https://fixture.example.invalid/root#jwt=value",
            f"https://fixture.example.invalid/{deeply_encoded}/root",
            "https://fixture.example.invalid/base/%252e%252e/wrong",
            "https://fixture.example.invalid/base/%5c..%5cwrong",
            "https://fixture.example.invalid/foo.refresh_token=value/root",
            "https://fixture.example.invalid/foo+access_token=value/root",
            "https://fixture.example.invalid/foo%20session_token%3Dvalue/root",
            "https://fixture.example.invalid/x(refresh_token=value)/root",
        )
        for value in values:
            payload = _link(value)
            try:
                module.save_source_links(
                    credential_root,
                    payload,
                    db_name="credential-fixture-rag",
                    existing_sources={SOURCE_A},
                    expected_revision=0,
                    expected_etag="missing",
                )
            except module.SourceLinkError:
                rejected += 1
            else:
                raise AssertionError("credential-bearing URL was saved")
        sensitive_setting = _link(
            "https://fixture.example.invalid/root"
        )
        sensitive_setting["sources"][0]["settings"][
            "session_token"
        ] = "value"
        try:
            module.save_source_links(
                credential_root,
                sensitive_setting,
                db_name="credential-fixture-rag",
                existing_sources={SOURCE_A},
                expected_revision=0,
                expected_etag="missing",
            )
        except module.SourceLinkError:
            rejected += 1
        else:
            raise AssertionError("credential setting was saved")
        for value in (
            "refresh_token%253Dvalue",
            quote("credentials=value", safe=""),
            quote("signature=value", safe=""),
            quote(
                "https://user:value@nested.example.invalid/",
                safe="",
            ),
            quote(
                "https://synthetic-token@nested.example.invalid/",
                safe="",
            ),
            quote(
                "//user:value@nested.example.invalid/",
                safe="",
            ),
            quote(
                "ftp://user:value@nested.example.invalid/",
                safe="",
            ),
            quote("Bearer value", safe=""),
            quote("foo+Basic value", safe=""),
            quote("apiKeys=value", safe=""),
            quote(quote("APIKeys=value", safe=""), safe=""),
            quote("pwd=value", safe=""),
            quote(quote("passPhrase=value", safe=""), safe=""),
            quote(quote("APIKEYS=value", safe=""), safe=""),
            quote(quote("apikeys=value", safe=""), safe=""),
            quote(quote("ACCESSKEYS=value", safe=""), safe=""),
            quote(quote("accesskeys=value", safe=""), safe=""),
            quote(quote("SSHKEY=value", safe=""), safe=""),
            quote(quote("sshkey=value", safe=""), safe=""),
            quote(quote("SUBSCRIPTIONKEY=value", safe=""), safe=""),
            quote(quote("subscriptionkey=value", safe=""), safe=""),
            quote(quote("ACCESSKEYID=value", safe=""), safe=""),
            quote(quote("accesskeyid=value", safe=""), safe=""),
            quote(quote("AWSACCESSKEYID=value", safe=""), safe=""),
            quote(quote("awsaccesskeyid=value", safe=""), safe=""),
            quote(quote("PASSPHRASES=value", safe=""), safe=""),
            quote(quote("passphrases=value", safe=""), safe=""),
            quote(quote("XAMZSIGNATURE=value", safe=""), safe=""),
            quote(quote("xamzsignature=value", safe=""), safe=""),
            quote(quote("XGOOGSIGNATURE=value", safe=""), safe=""),
            quote(quote("xgoogsignature=value", safe=""), safe=""),
            quote(quote("PROXYAUTHORIZATION=value", safe=""), safe=""),
            quote(quote("proxyauthorization=value", safe=""), safe=""),
            quote(quote("PROXYAUTH=value", safe=""), safe=""),
            quote(quote("proxyauth=value", safe=""), safe=""),
            quote(quote("SECRETACCESSKEY=value", safe=""), safe=""),
            quote(quote("secretaccesskey=value", safe=""), safe=""),
        ):
            sensitive_query = {
                "schema_version": module.SCHEMA_VERSION,
                "revision": 1,
                "sources": [
                    {
                        "source_id": SOURCE_A,
                        "enabled": True,
                        "provider": "other",
                        "strategy": "home-only",
                        "settings": {
                            "source_home_url": (
                                "https://fixture.example.invalid/"
                                f"?next={value}"
                            )
                        },
                    }
                ],
            }
            try:
                module.save_source_links(
                    credential_root,
                    sensitive_query,
                    db_name="credential-fixture-rag",
                    existing_sources={SOURCE_A},
                    expected_revision=0,
                    expected_etag="missing",
                )
            except module.SourceLinkError:
                rejected += 1
            else:
                raise AssertionError(
                    "encoded credential query value was saved"
                )
        for query_key in ("pwd", "passphrase", "sas"):
            direct_query = {
                "schema_version": module.SCHEMA_VERSION,
                "revision": 1,
                "sources": [
                    {
                        "source_id": SOURCE_A,
                        "enabled": True,
                        "provider": "other",
                        "strategy": "home-only",
                        "settings": {
                            "source_home_url": (
                                "https://fixture.example.invalid/"
                                f"?{query_key}=value"
                            )
                        },
                    }
                ],
            }
            try:
                module.save_source_links(
                    credential_root,
                    direct_query,
                    db_name="credential-fixture-rag",
                    existing_sources={SOURCE_A},
                    expected_revision=0,
                    expected_etag="missing",
                )
            except module.SourceLinkError:
                rejected += 1
            else:
                raise AssertionError(
                    "top-level credential query key was saved"
                )
        if any(
            (credential_root / name).exists()
            for name in (
                module.SIDECAR_NAME,
                module.BACKUP_NAME,
                ".source-links.lock",
            )
        ):
            raise AssertionError(
                "credential rejection published Source-Link output"
            )
        return {
            "credential_inputs_rejected": rejected,
            "published_files": 0,
        }

    results.case(
        "SEC-001-credential-save-rejection",
        credential_save_rejection,
    )

    def interrupted_save() -> dict[str, Any]:
        _require_windows(args)
        before = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if before.payload is None:
            raise AssertionError("sidecar missing")
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        worker = context.Process(
            target=_interrupted_save_worker,
            args=(str(rag_root), str(fixture), ready),
        )
        worker.start()
        if not ready.wait(10):
            worker.kill()
            worker.join(5)
            raise AssertionError("save worker did not reach the atomic pause")
        worker.kill()
        worker.join(5)
        unchanged = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if unchanged.payload is None or unchanged.revision != before.revision:
            raise AssertionError("interrupted save changed or corrupted current")
        unchanged.payload["revision"] = unchanged.revision + 1
        started = time.monotonic()
        module.save_source_links(
            fixture,
            unchanged.payload,
            db_name=FIXTURE_DB_NAME,
            existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
            expected_revision=unchanged.revision,
            expected_etag=unchanged.etag,
        )
        elapsed = time.monotonic() - started
        if elapsed > 5:
            raise AssertionError("interrupted save recovery exceeded 5 seconds")
        orphaned = list(fixture.glob(f".{module.SIDECAR_NAME}.*.tmp"))
        for candidate in orphaned:
            candidate.unlink(missing_ok=True)
        if not (fixture / ".source-links.lock").is_file():
            raise AssertionError("persistent lock file disappeared")
        return {
            "orphan_temporary_files": len(orphaned),
            "recovery_seconds": round(elapsed, 6),
        }

    results.case("SC-007-interrupted-save-recovery", interrupted_save)

    def manager_cycles() -> dict[str, Any]:
        if not runtime.is_file():
            raise EnvironmentUnavailable("installed runtime is missing")
        manage = rag_root / "manage.py"
        if not manage.is_file():
            raise EnvironmentUnavailable("manager entrypoint is missing")
        iterations = int(args.manager_iterations)
        environment = os.environ.copy()
        environment["RAG_DBS_ROOT"] = str(fixture_parent)
        for _index in range(iterations):
            completed = subprocess.run(
                [str(runtime), str(manage)],
                input="0\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=5,
                shell=False,
                check=False,
                cwd=str(rag_root),
                env=environment,
            )
            if completed.returncode != 0 or "Traceback" in completed.stderr:
                raise AssertionError("manager did not exit cleanly")
        return {"iterations": iterations, "utf8": True}

    results.case("MGR-001-start-exit-100", manager_cycles)

    def manager_nested_eof_and_double_launch() -> dict[str, Any]:
        if not runtime.is_file():
            raise EnvironmentUnavailable("installed runtime is missing")
        manage = rag_root / "manage.py"
        environment = os.environ.copy()
        environment["RAG_DBS_ROOT"] = str(fixture_parent)
        nested = subprocess.run(
            [str(runtime), str(manage)],
            input="3\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=5,
            shell=False,
            check=False,
            cwd=str(rag_root),
            env=environment,
        )
        if nested.returncode != 0 or "Traceback" in nested.stderr:
            raise AssertionError("manager did not exit cleanly from nested EOF")
        managers = [
            subprocess.Popen(
                [str(runtime), str(manage)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                shell=False,
                cwd=str(rag_root),
                env=environment,
            )
            for _index in range(2)
        ]
        for process in managers:
            stdout, stderr = process.communicate("0\n", timeout=5)
            del stdout
            if process.returncode != 0 or "Traceback" in stderr:
                raise AssertionError("double-launched manager failed")
        return {"double_launch": True, "nested_eof": True}

    results.case(
        "MGR-002-nested-exit-and-double-launch",
        manager_nested_eof_and_double_launch,
    )

    def ctrl_break() -> dict[str, Any]:
        _require_windows(args)
        if not runtime.is_file():
            raise EnvironmentUnavailable("installed runtime is missing")
        creationflags = int(subprocess.CREATE_NEW_PROCESS_GROUP)
        process = subprocess.Popen(
            [str(runtime), str(rag_root / "manage.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            shell=False,
            creationflags=creationflags,
        )
        try:
            time.sleep(0.3)
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
            process.wait(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        stderr = process.stderr.read() if process.stderr else ""
        if "Traceback" in stderr:
            raise AssertionError("Ctrl+Break emitted a traceback")
        return {"returncode": process.returncode}

    results.case("MGR-003-console-interrupt", ctrl_break)

    def manager_legacy_migration_confirmation() -> dict[str, Any]:
        manager_code = _manager_module(rag_root)
        db_name = "manager-legacy-fixture-rag"
        db_root = fixture_parent / db_name
        db_root.mkdir()
        _create_catalog(db_root / "catalog.sqlite")
        legacy = _legacy(f"{OBSERVED_ROOT_A}/")
        legacy["database"] = db_name
        current = db_root / module.SIDECAR_NAME
        _atomic_json(current, legacy)
        original = current.read_bytes()

        class Inventory:
            @staticmethod
            def to_dict() -> dict[str, Any]:
                return {
                    "sources": [
                        {"source_id": SOURCE_A},
                        {"source_id": SOURCE_B},
                        {"source_id": SOURCE_MULTI},
                    ]
                }

        denied_prompts: list[str] = []
        denied = manager_code.LocalRagManager(
            rag_root=rag_root,
            dbs_root=fixture_parent,
            runtime_python=runtime,
            input_fn=lambda prompt: (
                denied_prompts.append(prompt) or "n"
            ),
            output_fn=lambda _text: None,
        )
        loaded = denied._load_sidecar_payload(db_name)
        if loaded is None:
            raise AssertionError("manager could not load legacy sidecar")
        if denied._save_sidecar(
            db_name,
            Inventory(),
            loaded[0],
            loaded[1],
        ):
            raise AssertionError(
                "manager migrated legacy sidecar after rejection"
            )
        if current.read_bytes() != original:
            raise AssertionError(
                "cancelled manager migration changed the sidecar"
            )
        if not any("Migrate Source-Link" in value for value in denied_prompts):
            raise AssertionError(
                "manager did not ask a separate migration confirmation"
            )

        accepted_prompts: list[str] = []
        accepted = manager_code.LocalRagManager(
            rag_root=rag_root,
            dbs_root=fixture_parent,
            runtime_python=runtime,
            input_fn=lambda prompt: (
                accepted_prompts.append(prompt) or "y"
            ),
            output_fn=lambda _text: None,
        )
        loaded = accepted._load_sidecar_payload(db_name)
        if loaded is None or not accepted._save_sidecar(
            db_name,
            Inventory(),
            loaded[0],
            loaded[1],
        ):
            raise AssertionError(
                "confirmed manager migration did not publish v2"
            )
        saved = json.loads(current.read_text(encoding="utf-8"))
        backup = json.loads(
            (db_root / module.BACKUP_NAME).read_text(encoding="utf-8")
        )
        if saved.get("schema_version") != module.SCHEMA_VERSION:
            raise AssertionError("manager migration did not publish v2")
        if backup.get("schema_version") != module.LEGACY_SCHEMA_VERSION:
            raise AssertionError("manager migration did not retain v1 backup")
        return {
            "cancel_preserved_v1": True,
            "confirmation_prompts": len(
                denied_prompts + accepted_prompts
            ),
            "explicit_migration_published_v2": True,
        }

    results.case(
        "MGR-004-explicit-legacy-migration-confirmation",
        manager_legacy_migration_confirmation,
    )

    def windows_share_delete_lock() -> dict[str, Any]:
        _require_windows(args)
        import ctypes
        from ctypes import wintypes

        current_path = str(fixture / module.SIDECAR_NAME)
        generic_read = 0x80000000
        generic_write = 0x40000000
        open_existing = 3
        file_share_read = 1
        file_share_write = 2
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            current_path,
            generic_read | generic_write,
            file_share_read | file_share_write,
            None,
            open_existing,
            0,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        loaded = module.load_source_links(fixture, FIXTURE_DB_NAME)
        assert loaded.payload is not None
        revision = loaded.revision
        current_hash = _hash(fixture / module.SIDECAR_NAME)
        loaded.payload["revision"] = revision + 1
        rejected = False
        try:
            try:
                module.save_source_links(
                    fixture,
                    loaded.payload,
                    db_name=FIXTURE_DB_NAME,
                    existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
                    expected_revision=revision,
                    expected_etag=loaded.etag,
                )
            except OSError:
                rejected = True
        finally:
            kernel32.CloseHandle(handle)
        if not rejected:
            raise AssertionError("replace succeeded while delete sharing denied")
        if _hash(fixture / module.SIDECAR_NAME) != current_hash:
            raise AssertionError("sharing violation changed current sidecar")
        valid = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if valid.payload is None:
            raise AssertionError("sharing failure corrupted current sidecar")
        valid.payload["revision"] = valid.revision + 1
        module.save_source_links(
            fixture,
            valid.payload,
            db_name=FIXTURE_DB_NAME,
            existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
            expected_revision=valid.revision,
            expected_etag=valid.etag,
        )
        return {"controlled_failure": True, "retry": "pass"}

    results.case("WIN-001-sharing-violation", windows_share_delete_lock)

    def near_max_path() -> dict[str, Any]:
        _require_windows(args)
        lengths: list[int] = []
        for target in (248, 259):
            root = fixture_parent / f"path-{target}"
            while len(str(root / module.SIDECAR_NAME)) < target - 41:
                root /= "x" * 40
            remaining = target - len(str(root / module.SIDECAR_NAME))
            if remaining > 1:
                root /= "y" * (remaining - 1)
            root.mkdir(parents=True)
            actual = len(str(root / module.SIDECAR_NAME))
            if abs(actual - target) > 1:
                raise AssertionError("could not construct boundary path")
            _create_catalog(root / "catalog.sqlite")
            module.save_source_links(
                root,
                _link("https://boundary.example.invalid/root"),
                existing_sources={SOURCE_A},
                expected_revision=0,
                expected_etag="missing",
            )
            loaded = module.load_source_links(root)
            if loaded.payload is None:
                raise AssertionError("boundary path sidecar was unreadable")
            lengths.append(actual)
        return {"path_lengths": lengths}

    results.case("WIN-002-near-max-path", near_max_path)

    def alternate_drive() -> dict[str, Any]:
        _require_windows(args)
        drive = next(
            (
                f"{letter}:"
                for letter in "ZYXWVUT"
                if not Path(f"{letter}:/").exists()
            ),
            None,
        )
        if drive is None:
            raise EnvironmentUnavailable("no free drive letter for subst")
        created = subprocess.run(
            ["subst", drive, str(fixture_parent)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            shell=False,
        )
        if created.returncode != 0:
            raise EnvironmentUnavailable("subst could not create a test drive")
        try:
            alias = Path(f"{drive}/{FIXTURE_DB_NAME}")
            loaded = module.load_source_links(alias, FIXTURE_DB_NAME)
            if loaded.payload is None:
                raise AssertionError("drive alias could not read the sidecar")
            loaded.payload["revision"] = loaded.revision + 1
            module.save_source_links(
                alias,
                loaded.payload,
                db_name=FIXTURE_DB_NAME,
                existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
                expected_revision=loaded.revision,
                expected_etag=loaded.etag,
            )
        finally:
            subprocess.run(
                ["subst", drive, "/D"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
            )
        return {"subst_drive": True}

    results.case("WIN-003-alternate-drive", alternate_drive)

    def unc_root() -> dict[str, Any]:
        _require_windows(args)
        if args.unc_root is None:
            raise EnvironmentUnavailable("a disposable UNC root is required")
        parent = args.unc_root.expanduser()
        if not str(parent).startswith("\\\\"):
            raise EnvironmentUnavailable("the supplied test root is not UNC")
        test_root = parent / f"source-link-v2-{uuid.uuid4().hex}" / "unc-rag"
        if test_root.exists():
            raise AssertionError("UNC fixture unexpectedly exists")
        try:
            test_root.mkdir(parents=True)
            _create_catalog(test_root / "catalog.sqlite")
            module.save_source_links(
                test_root,
                _link("https://unc.example.invalid/root"),
                existing_sources={SOURCE_A},
                expected_revision=0,
                expected_etag="missing",
            )
            loaded = module.load_source_links(test_root)
            if loaded.payload is None:
                raise AssertionError("UNC sidecar was unreadable")
        finally:
            shutil.rmtree(test_root.parent, ignore_errors=True)
        return {"unc_round_trip": True}

    results.case("WIN-004-unc-round-trip", unc_root)

    def sqlite_concurrency() -> dict[str, Any]:
        failures: list[str] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                try:
                    connection = sqlite3.connect(
                        f"{catalog.resolve().as_uri()}?mode=ro",
                        uri=True,
                        timeout=1,
                    )
                    try:
                        result = connection.execute(
                            "PRAGMA quick_check"
                        ).fetchone()[0]
                        if result != "ok":
                            raise RuntimeError("quick_check")
                    finally:
                        connection.close()
                    enriched = module.enrich_search_payload(
                        _search_payload(
                            f"{OBSERVED_ROOT_A}/space dir/file name.txt"
                        ),
                        fixture,
                        FIXTURE_DB_NAME,
                    )
                    if enriched.get("status") != "ok":
                        raise RuntimeError("search_status")
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    stop.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        writes = min(max(int(args.save_iterations), 10), 25)
        try:
            for index in range(writes):
                loaded = module.load_source_links(fixture, FIXTURE_DB_NAME)
                if loaded.payload is None:
                    raise AssertionError("sidecar disappeared during SQLite read")
                loaded.payload["revision"] = loaded.revision + 1
                loaded.payload["sources"][0]["enabled"] = bool(index % 2)
                module.save_source_links(
                    fixture,
                    loaded.payload,
                    db_name=FIXTURE_DB_NAME,
                    existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
                    expected_revision=loaded.revision,
                    expected_etag=loaded.etag,
                )
        finally:
            stop.set()
            thread.join(5)
        if thread.is_alive():
            raise AssertionError("SQLite reader did not stop")
        if failures:
            raise AssertionError("SQLite/search reader failed during sidecar save")
        if _hash(catalog) != catalog_before:
            raise AssertionError("sidecar concurrency modified catalog.sqlite")
        return {"concurrent_reads": True, "sidecar_writes": writes}

    results.case("SQL-001-concurrent-read-and-sidecar-save", sqlite_concurrency)

    def stress() -> dict[str, Any]:
        iterations = int(args.stress_iterations)
        if iterations <= 0:
            return {"iterations": 0}
        loaded = module.load_source_links(fixture, FIXTURE_DB_NAME)
        if loaded.payload is None:
            raise AssertionError("sidecar missing")
        revision = loaded.revision
        for index in range(iterations):
            payload = loaded.payload
            payload["revision"] = revision + 1
            payload["sources"][0]["enabled"] = bool(index % 2)
            module.save_source_links(
                fixture,
                payload,
                db_name=FIXTURE_DB_NAME,
                existing_sources={SOURCE_A, SOURCE_B, SOURCE_MULTI},
                expected_revision=revision,
                expected_etag=loaded.etag,
            )
            revision += 1
            loaded = module.load_source_links(fixture, FIXTURE_DB_NAME)
            if loaded.payload is None:
                raise AssertionError("stress load failed")
        leftovers = list(fixture.glob(".*.tmp"))
        if leftovers:
            raise AssertionError("stress left temporary files")
        if not (fixture / ".source-links.lock").is_file():
            raise AssertionError("persistent kernel lock file is missing")
        return {"iterations": iterations, "final_revision": revision}

    results.case("P3-save-load-stress", stress, required=False)

    def catalog_unchanged() -> dict[str, Any]:
        if _hash(catalog) != catalog_before:
            raise AssertionError("sidecar operations modified catalog.sqlite")
        connection = sqlite3.connect(
            f"{catalog.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        try:
            value = connection.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            connection.close()
        if value != "ok":
            raise AssertionError("SQLite quick_check failed")
        return {"catalog_unchanged": True, "sqlite_quick_check": value}

    results.case("SQL-002-catalog-integrity", catalog_unchanged)
    summary = results.finish(
        preflight,
        synthetic_smoke=bool(args.synthetic_smoke),
    )
    if args.keep_fixture:
        shutil.copytree(
            fixture_parent,
            output_root / "fixture",
            dirs_exist_ok=True,
        )
    shutil.rmtree(fixture_parent, ignore_errors=True)
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    if summary["status"] == "PASS":
        return 0
    if summary["status"] == "INCOMPLETE_ENV":
        return 2
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Windows reliability checks for the Source Link v2 sidecar."
        )
    )
    parser.add_argument(
        "--installed-rag",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            Path(tempfile.gettempdir())
            / "GitHubCopilotLocalRAG"
            / "source-link-windows-reliability"
        ),
    )
    parser.add_argument("--manager-iterations", type=int, default=100)
    parser.add_argument("--save-iterations", type=int, default=100)
    parser.add_argument("--concurrent-writers", type=int, default=8)
    parser.add_argument("--stress-iterations", type=int, default=0)
    parser.add_argument(
        "--unc-root",
        type=Path,
        help=(
            "Disposable UNC directory supplied by the test environment. "
            "Its value is never written to result artifacts."
        ),
    )
    parser.add_argument(
        "--synthetic-smoke",
        action="store_true",
        help=(
            "Allow a non-Windows local smoke; Windows-only cases become "
            "NOT_RUN and do not establish release evidence."
        ),
    )
    parser.add_argument("--keep-fixture", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
