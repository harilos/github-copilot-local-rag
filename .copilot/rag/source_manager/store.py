from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .errors import SourceManagerError
from .providers import build_fetch_plan, validate_provider_config
from .security import redact_runtime_paths, validate_persistable


SOURCE_SCHEMA_VERSION = "local-rag-source-manager-v1"
STATE_SCHEMA_VERSION = "local-rag-source-state-v1"
EVENT_SCHEMA_VERSION = "local-rag-source-event-v1"
CLASSIFICATION_SCHEMA_VERSION = "local-rag-source-classifications-v1"
CLASSIFICATION_FILE_NAME = "source-classifications.json"
SECRET_CLASSIFICATION = "secret"
MISSING_ETAG = "missing"
MAX_JSON_BYTES = 1_048_576
WINDOWS_FILE_RETRY_SECONDS = 2.0
_SOURCE_ID = re.compile(r"^[^\x00-\x1f/\\]{1,200}$")
_LOCAL_KEY = re.compile(r"^src_[a-z0-9][a-z0-9-]{0,39}-[0-9a-f]{12}$")
_SLUG = re.compile(r"[^a-z0-9]+")
_WINDOWS_TRANSIENT_FILE_ERRORS = frozenset({5, 32, 33})


@dataclass(frozen=True)
class SourcePaths:
    local_source_key: str
    source_dir: str
    source_json: str
    state_json: str
    events_jsonl: str
    work_directory: str
    logical_root_name: str

    def absolute(self, db_root: Path, relative: str) -> Path:
        root = _real_directory(Path(db_root))
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise SourceManagerError("Source path must be DB-relative")
        candidate = root.joinpath(*path.parts)
        _reject_linked_components(root, candidate)
        return candidate


@dataclass(frozen=True)
class StoredJson:
    payload: dict[str, Any]
    revision: int
    etag: str
    path: Path


def stable_source_key(seed: str | None = None) -> str:
    """Allocate a registration-time key; it never depends on later source_id."""
    label = unicodedata.normalize("NFC", str(seed or "source").strip())
    ascii_text = (
        unicodedata.normalize("NFKD", label)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    slug = _SLUG.sub("-", ascii_text).strip("-")[:32] or "source"
    random_part = uuid.uuid4().hex[:12]
    return f"src_{slug}-{random_part}"


def validate_local_source_key(value: Any) -> str:
    key = str(value or "").strip()
    if not _LOCAL_KEY.fullmatch(key):
        raise SourceManagerError("local_source_key is invalid")
    return key


def normalize_source_id(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def remove_source_classification(
    db_root: Path,
    source_id: str,
) -> bool:
    store = SourceStore(db_root)
    loaded = store.read_source_classifications()
    sources = (
        loaded.payload.get("sources")
        if isinstance(loaded.payload, dict)
        else []
    )
    source_value = _validate_source_id(source_id)
    if (
        source_value is None
        or not isinstance(sources, list)
        or not any(
            isinstance(item, dict)
            and item.get("source_id") == source_value
            for item in sources
        )
    ):
        return False
    store.save_source_classification(
        source_value,
        "",
        expected_revision=loaded.revision,
        expected_etag=loaded.etag,
    )
    return True


class SourceStore:
    def __init__(self, db_root: Path):
        self.db_root = _real_directory(Path(db_root))

    def paths(self, local_source_key: str) -> SourcePaths:
        key = validate_local_source_key(local_source_key)
        directory = f"sources/{key}"
        work = f"{directory}/work/ingest/{key}"
        return SourcePaths(
            local_source_key=key,
            source_dir=directory,
            source_json=f"{directory}/source.json",
            state_json=f"{directory}/state.json",
            events_jsonl=f"{directory}/events.jsonl",
            work_directory=work,
            logical_root_name=key,
        )

    def read_source_classifications(self) -> StoredJson:
        path = self.db_root / "sources" / CLASSIFICATION_FILE_NAME
        loaded = self._read_json(
            path,
            expected_schema=CLASSIFICATION_SCHEMA_VERSION,
        )
        if loaded.payload:
            _validate_classifications(loaded.payload)
        return loaded

    def save_source_classification(
        self,
        source_id: str,
        classification: str,
        *,
        expected_revision: int,
        expected_etag: str,
    ) -> StoredJson:
        source_value = _validate_source_id(source_id)
        if source_value is None:
            raise SourceManagerError("source_id is required")
        classification_value = str(classification or "").strip().lower()
        if classification_value not in {"", SECRET_CLASSIFICATION}:
            raise SourceManagerError("unsupported Source classification")
        loaded = self.read_source_classifications()
        if (
            loaded.revision != int(expected_revision)
            or loaded.etag != str(expected_etag)
        ):
            raise SourceManagerError("source_configuration_changed")
        current = loaded.payload.get("sources") if loaded.payload else []
        values: dict[str, str] = {}
        if isinstance(current, list):
            values = {
                str(item["source_id"]): str(item["classification"])
                for item in current
                if isinstance(item, dict)
                and item.get("source_id")
                and item.get("classification")
            }
        if classification_value:
            values[source_value] = classification_value
        else:
            values.pop(source_value, None)
        payload = {
            "schema_version": CLASSIFICATION_SCHEMA_VERSION,
            "sources": [
                {
                    "source_id": key,
                    "classification": values[key],
                }
                for key in sorted(values)
            ],
        }
        _validate_classifications(payload)
        stored = self._atomic_json_write(
            loaded.path,
            payload,
            expected_revision=expected_revision,
            expected_etag=expected_etag,
            expected_schema=CLASSIFICATION_SCHEMA_VERSION,
        )
        _validate_classifications(stored.payload)
        return stored

    def create_source(
        self,
        *,
        source_type: str,
        display_name: str,
        fetch: Mapping[str, Any],
        local_source_key: str | None = None,
        source_id: str | None = None,
        link: Mapping[str, Any] | None = None,
    ) -> StoredJson:
        key = (
            validate_local_source_key(local_source_key)
            if local_source_key
            else stable_source_key(display_name or source_type)
        )
        paths = self.paths(key)
        kind = str(source_type or "").strip().lower()
        normalized_fetch = validate_provider_config(kind, fetch)
        payload: dict[str, Any] = {
            "schema_version": SOURCE_SCHEMA_VERSION,
            "local_source_key": key,
            "source_id": _validate_source_id(source_id),
            "source_type": kind,
            "display_name": _display_name(display_name),
            "fetch": normalized_fetch,
            "ingest": {
                "work_directory": paths.work_directory,
                "logical_root_name": paths.logical_root_name,
            },
            "metadata_sync_pending": False,
        }
        if link is not None:
            payload["pending_metadata"] = {
                "source_type": kind,
                "link": _validate_link(kind, link),
            }
        self._validate_source(payload, paths)
        self.ensure_work_directory(key)
        stored = self._atomic_json_write(
            paths.absolute(self.db_root, paths.source_json),
            payload,
            expected_revision=0,
            expected_etag=MISSING_ETAG,
            expected_schema=SOURCE_SCHEMA_VERSION,
        )
        return stored

    def list_keys(self) -> list[str]:
        sources = self.db_root / "sources"
        if not sources.exists():
            return []
        _reject_linked_components(self.db_root, sources)
        keys: list[str] = []
        for entry in sources.iterdir():
            try:
                key = validate_local_source_key(entry.name)
                _reject_linked_components(self.db_root, entry)
                if entry.is_dir() and (entry / "source.json").is_file():
                    keys.append(key)
            except (OSError, SourceManagerError):
                continue
        return sorted(keys)

    def ensure_work_directory(self, local_source_key: str) -> Path:
        paths = self.paths(local_source_key)
        target = paths.absolute(self.db_root, paths.work_directory)
        current = self.db_root
        for component in PurePosixPath(paths.work_directory).parts:
            current = current / component
            if current.exists():
                _reject_linked_components(self.db_root, current)
                if not current.is_dir():
                    raise SourceManagerError("Source work path is not a directory")
            else:
                current.mkdir(mode=0o700)
                _reject_linked_components(self.db_root, current)
        return target

    def read_source(self, local_source_key: str) -> StoredJson:
        paths = self.paths(local_source_key)
        loaded = self._read_json(
            paths.absolute(self.db_root, paths.source_json),
            expected_schema=SOURCE_SCHEMA_VERSION,
        )
        if loaded.payload:
            self._validate_source(loaded.payload, paths)
        return loaded

    def save_source(
        self,
        payload: Mapping[str, Any],
        *,
        expected_revision: int,
        expected_etag: str,
    ) -> StoredJson:
        source = copy.deepcopy(dict(payload))
        key = validate_local_source_key(source.get("local_source_key"))
        paths = self.paths(key)
        self._validate_source(source, paths)
        path = paths.absolute(self.db_root, paths.source_json)
        current = self._read_json(path, expected_schema=SOURCE_SCHEMA_VERSION)
        if source.get("source_id") and (
            _metadata_content(current.payload) != _metadata_content(source)
        ):
            source["metadata_sync_pending"] = True
        stored = self._atomic_json_write(
            path,
            source,
            expected_revision=expected_revision,
            expected_etag=expected_etag,
            expected_schema=SOURCE_SCHEMA_VERSION,
        )
        self.ensure_work_directory(key)
        return stored

    def mark_metadata_synced(
        self,
        local_source_key: str,
        *,
        expected_revision: int,
        expected_etag: str,
    ) -> StoredJson:
        loaded = self.read_source(local_source_key)
        if loaded.revision != expected_revision or loaded.etag != expected_etag:
            raise SourceManagerError("source_configuration_changed")
        payload = copy.deepcopy(loaded.payload)
        payload["metadata_sync_pending"] = False
        payload.pop("pending_metadata", None)
        paths = self.paths(local_source_key)
        self._validate_source(payload, paths)
        return self._atomic_json_write(
            paths.absolute(self.db_root, paths.source_json),
            payload,
            expected_revision=expected_revision,
            expected_etag=expected_etag,
            expected_schema=SOURCE_SCHEMA_VERSION,
        )

    def confirm_source_id(
        self,
        local_source_key: str,
        source_id: str,
        *,
        expected_revision: int,
        expected_etag: str,
    ) -> StoredJson:
        loaded = self.read_source(local_source_key)
        if loaded.revision != expected_revision or loaded.etag != expected_etag:
            raise SourceManagerError("source_configuration_changed")
        payload = copy.deepcopy(loaded.payload)
        payload["source_id"] = _validate_source_id(source_id)
        return self.save_source(
            payload,
            expected_revision=expected_revision,
            expected_etag=expected_etag,
        )

    def delete_source(
        self,
        local_source_key: str,
        *,
        expected_revision: int,
        expected_etag: str,
    ) -> None:
        """Delete one management directory after an optimistic recheck."""
        paths = self.paths(local_source_key)
        loaded = self.read_source(local_source_key)
        if (
            loaded.revision != int(expected_revision)
            or loaded.etag != str(expected_etag)
        ):
            raise SourceManagerError("source_configuration_changed")
        directory = paths.absolute(self.db_root, paths.source_dir)
        sources_root = self.db_root / "sources"
        if (
            directory.parent != sources_root
            or directory.name != paths.local_source_key
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            raise SourceManagerError("Source directory is unsafe")
        # Re-read immediately before the destructive step.
        latest = self.read_source(local_source_key)
        if (
            latest.revision != int(expected_revision)
            or latest.etag != str(expected_etag)
        ):
            raise SourceManagerError("source_configuration_changed")
        shutil.rmtree(directory, onerror=_remove_readonly_path)

    def plan(self, source: Mapping[str, Any]):
        payload = dict(source)
        key = validate_local_source_key(payload.get("local_source_key"))
        paths = self.paths(key)
        self._validate_source(payload, paths)
        return build_fetch_plan(
            source_key=key,
            provider=str(payload["source_type"]),
            settings=dict(payload["fetch"]),
            logical_root=paths.work_directory,
            work_path=paths.work_directory,
        )

    def read_state(self, local_source_key: str) -> StoredJson:
        paths = self.paths(local_source_key)
        return self._read_json(
            paths.absolute(self.db_root, paths.state_json),
            expected_schema=STATE_SCHEMA_VERSION,
            allow_transient_runtime_path=True,
        )

    def save_state(
        self,
        local_source_key: str,
        payload: Mapping[str, Any],
        *,
        expected_revision: int,
        expected_etag: str,
    ) -> StoredJson:
        state = copy.deepcopy(dict(payload))
        paths = self.paths(local_source_key)
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise SourceManagerError("unsupported Source state schema")
        if state.get("local_source_key") != paths.local_source_key:
            raise SourceManagerError("state local_source_key does not match")
        _validate_state(state)
        return self._atomic_json_write(
            paths.absolute(self.db_root, paths.state_json),
            state,
            expected_revision=expected_revision,
            expected_etag=expected_etag,
            expected_schema=STATE_SCHEMA_VERSION,
            allow_transient_runtime_path=True,
        )

    def append_event(
        self,
        local_source_key: str,
        event_type: str,
        details: Mapping[str, Any] | None = None,
        *,
        runtime_roots: Iterable[tuple[str | Path, str]] | None = None,
    ) -> dict[str, Any]:
        paths = self.paths(local_source_key)
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "local_source_key": paths.local_source_key,
            "event": _bounded_event_type(event_type),
            "timestamp": _now(),
            "details": redact_runtime_paths(dict(details or {}), roots=runtime_roots),
        }
        validate_persistable(event, field="event")
        encoded = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        path = paths.absolute(self.db_root, paths.events_jsonl)
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_linked_components(self.db_root, path)
        descriptor = _safe_append_open(path)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return event

    def _validate_source(self, payload: dict[str, Any], paths: SourcePaths) -> None:
        allowed = {
            "schema_version", "local_source_key", "source_id", "source_type",
            "display_name", "fetch", "ingest", "pending_metadata",
            "metadata_sync_pending", "revision", "updated_at",
        }
        if set(payload) - allowed:
            raise SourceManagerError("Source contains unsupported fields")
        if payload.get("schema_version") != SOURCE_SCHEMA_VERSION:
            raise SourceManagerError("unsupported Source schema")
        if payload.get("local_source_key") != paths.local_source_key:
            raise SourceManagerError("local_source_key mismatch")
        _validate_source_id(payload.get("source_id"))
        source_type = str(payload.get("source_type") or "").strip().lower()
        validate_provider_config(source_type, payload.get("fetch") or {})
        ingest = payload.get("ingest")
        if not isinstance(ingest, dict) or ingest != {
            "work_directory": paths.work_directory,
            "logical_root_name": paths.logical_root_name,
        }:
            raise SourceManagerError("Source ingest paths are immutable")
        if "pending_metadata" in payload:
            pending = payload["pending_metadata"]
            if not isinstance(pending, dict) or set(pending) != {
                "source_type", "link"
            }:
                raise SourceManagerError("pending_metadata is invalid")
            if pending["source_type"] != source_type:
                raise SourceManagerError("pending source_type mismatch")
            pending["link"] = _validate_link(source_type, pending["link"])
        if not isinstance(payload.get("metadata_sync_pending"), bool):
            raise SourceManagerError("metadata_sync_pending must be boolean")
        validate_persistable(payload, field="source")

    def _read_json(
        self,
        path: Path,
        *,
        expected_schema: str,
        allow_transient_runtime_path: bool = False,
    ) -> StoredJson:
        _reject_linked_components(self.db_root, path)
        try:
            raw = _read_bytes_with_windows_retry(path)
        except FileNotFoundError:
            return StoredJson({}, 0, MISSING_ETAG, path)
        if len(raw) > MAX_JSON_BYTES:
            raise SourceManagerError("Source JSON is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceManagerError("Source JSON is invalid") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != expected_schema:
            raise SourceManagerError("Source JSON schema is invalid")
        revision = payload.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise SourceManagerError("Source JSON revision is invalid")
        if allow_transient_runtime_path:
            _validate_state(payload)
        else:
            validate_persistable(payload, field="stored_json")
        return StoredJson(payload, revision, hashlib.sha256(raw).hexdigest(), path)

    def _atomic_json_write(
        self,
        path: Path,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        expected_etag: str,
        expected_schema: str,
        allow_transient_runtime_path: bool = False,
    ) -> StoredJson:
        current = self._read_json(
            path,
            expected_schema=expected_schema,
            allow_transient_runtime_path=allow_transient_runtime_path,
        )
        if current.revision != int(expected_revision) or current.etag != str(expected_etag):
            raise SourceManagerError("source_configuration_changed")
        value = copy.deepcopy(payload)
        value["revision"] = int(expected_revision) + 1
        value["updated_at"] = _now()
        if allow_transient_runtime_path:
            _validate_state(value)
        else:
            validate_persistable(value, field="stored_json")
        encoded = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_JSON_BYTES:
            raise SourceManagerError("Source JSON is too large")
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_linked_components(self.db_root, path)
        temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            _write_bytes(temporary, encoded)
            latest = self._read_json(
                path,
                expected_schema=expected_schema,
                allow_transient_runtime_path=allow_transient_runtime_path,
            )
            if latest.revision != int(expected_revision) or latest.etag != str(expected_etag):
                raise SourceManagerError("source_configuration_changed")

            def verify_current() -> None:
                current_value = self._read_json(
                    path,
                    expected_schema=expected_schema,
                    allow_transient_runtime_path=allow_transient_runtime_path,
                )
                if (
                    current_value.revision != int(expected_revision)
                    or current_value.etag != str(expected_etag)
                ):
                    raise SourceManagerError(
                        "source_configuration_changed"
                    )

            _replace_with_windows_retry(
                temporary,
                path,
                before_retry=verify_current,
            )
            _reject_linked_components(self.db_root, path)
            _fsync_directory(path.parent)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
        return self._read_json(
            path,
            expected_schema=expected_schema,
            allow_transient_runtime_path=allow_transient_runtime_path,
        )


def _validate_source_id(value: Any) -> str | None:
    if value is None:
        return None
    text = normalize_source_id(value)
    if not _SOURCE_ID.fullmatch(text) or text in {".", ".."}:
        raise SourceManagerError("source_id is invalid")
    return text


def _validate_link(source_type: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if source_type == "other":
        raise SourceManagerError("Other Sources do not publish a Source URI")
    if not isinstance(value, Mapping):
        raise SourceManagerError("Source link must be an object")
    link = dict(value)
    if set(link) - {"enabled", "strategy", "settings"}:
        raise SourceManagerError("Source link contains unsupported fields")
    if not isinstance(link.get("enabled", True), bool):
        raise SourceManagerError("Source link enabled must be boolean")
    strategy = str(link.get("strategy") or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", strategy):
        raise SourceManagerError("Source link strategy is invalid")
    settings = link.get("settings")
    if not isinstance(settings, Mapping):
        raise SourceManagerError("Source link settings must be an object")
    settings_value = copy.deepcopy(dict(settings))
    validate_persistable(settings_value, field="source_link.settings")
    return {"enabled": link.get("enabled", True), "strategy": strategy, "settings": settings_value}


def _validate_state(payload: Mapping[str, Any]) -> None:
    runtime = payload.get("runtime")
    sanitized = copy.deepcopy(dict(payload))
    if isinstance(runtime, dict) and runtime.get("input_path"):
        sanitized_runtime = dict(runtime)
        sanitized_runtime["input_path"] = "<TRANSIENT_RUNTIME_PATH>"
        sanitized["runtime"] = sanitized_runtime
    validate_persistable(sanitized, field="state")


def _validate_classifications(payload: Mapping[str, Any]) -> None:
    allowed = {
        "schema_version",
        "sources",
        "revision",
        "updated_at",
    }
    if set(payload) - allowed:
        raise SourceManagerError(
            "Source classifications contain unsupported fields"
        )
    if payload.get("schema_version") != CLASSIFICATION_SCHEMA_VERSION:
        raise SourceManagerError("unsupported Source classification schema")
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) > 100_000:
        raise SourceManagerError(
            "Source classifications must be a bounded array"
        )
    seen: set[str] = set()
    for item in sources:
        if not isinstance(item, dict) or set(item) != {
            "source_id",
            "classification",
        }:
            raise SourceManagerError("Source classification is invalid")
        source_id = _validate_source_id(item.get("source_id"))
        classification = item.get("classification")
        if source_id is None:
            raise SourceManagerError("Source classification requires source_id")
        if source_id in seen:
            raise SourceManagerError("duplicate Source classification")
        seen.add(source_id)
        if classification != SECRET_CLASSIFICATION:
            raise SourceManagerError("unsupported Source classification")
    validate_persistable(payload, field="source_classifications")


def _real_directory(path: Path) -> Path:
    expanded = path.expanduser()
    metadata = os.lstat(expanded)
    if _is_link(metadata, expanded) or not stat.S_ISDIR(metadata.st_mode):
        raise SourceManagerError("database root must be a real directory")
    return expanded.resolve(strict=True)


def _reject_linked_components(root: Path, candidate: Path) -> None:
    root = root.resolve(strict=True)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise SourceManagerError("Source path escaped the database root") from exc
    current = root
    for component in relative.parts:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if _is_link(metadata, current):
            raise SourceManagerError("Source paths must not contain links")


def _is_link(metadata: os.stat_result, path: Path) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )


def _remove_readonly_path(function: Any, path: str, _error: Any) -> None:
    """Allow cleanup of read-only Git/SVN work files on Windows."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _safe_append_open(path: Path) -> int:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    opened = os.fstat(descriptor)
    current = os.lstat(path)
    if not stat.S_ISREG(opened.st_mode) or _is_link(current, path):
        os.close(descriptor)
        raise SourceManagerError("events target must be a regular file")
    if (
        getattr(opened, "st_ino", 0)
        and getattr(current, "st_ino", 0)
        and (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        os.close(descriptor)
        raise SourceManagerError("events target changed during open")
    return descriptor


def _display_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 300:
        raise SourceManagerError("display_name is required and bounded")
    return text


def _bounded_event_type(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", text):
        raise SourceManagerError("event type is invalid")
    return text


def _write_bytes(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _read_bytes_with_windows_retry(path: Path) -> bytes:
    deadline = (
        time.monotonic() + WINDOWS_FILE_RETRY_SECONDS
        if _is_windows()
        else 0.0
    )
    delay = 0.01
    while True:
        try:
            return path.read_bytes()
        except OSError as exc:
            if not _should_retry_windows_file_error(exc, deadline):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def _replace_with_windows_retry(
    source: Path,
    target: Path,
    *,
    before_retry: Any,
) -> None:
    deadline = (
        time.monotonic() + WINDOWS_FILE_RETRY_SECONDS
        if _is_windows()
        else 0.0
    )
    delay = 0.01
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not _should_retry_windows_file_error(exc, deadline):
                raise
            time.sleep(delay)
            before_retry()
            delay = min(delay * 2, 0.1)


def _should_retry_windows_file_error(
    exc: OSError,
    deadline: float,
) -> bool:
    return (
        _is_windows()
        and getattr(exc, "winerror", None)
        in _WINDOWS_TRANSIENT_FILE_ERRORS
        and time.monotonic() < deadline
    )


def _is_windows() -> bool:
    return os.name == "nt"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _metadata_content(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key in {
            "source_id",
            "source_type",
            "display_name",
            "pending_metadata",
        }
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
