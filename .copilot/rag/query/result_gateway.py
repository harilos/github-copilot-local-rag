from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


POINTER_SCHEMA = "rag-result-pointer-v1"
BINDING_SCHEMA = "local-rag-result-binding-v1"
TOKEN_PREFIX = "lrt_"
TOKEN_TTL_SECONDS = 15 * 60
TOKEN_REGISTRY_SIZE = 64
MAX_INSPECTABLE_EVIDENCE_IDS = 6
MAX_BUNDLE_FILE_BYTES = 2 * 1024 * 1024
MAX_REGISTRY_RECORD_BYTES = 32 * 1024
REGISTRY_TEMP_MAX_AGE_SECONDS = 60

_DATABASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
_TOKEN_RE = re.compile(r"^lrt_[A-Za-z0-9_-]{32}$")
_ITEM_ID_RE = re.compile(r"^[ED][1-9]\d?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")
_TEMP_RECORD_NAME_RE = re.compile(r"^\.[0-9a-f]{64}\.[0-9a-f]{32}\.tmp$")
_POINTER_FIELDS = {
    "status",
    "schema_version",
    "result_set_id",
    "summary_file",
    "expires_at",
    "bytes",
}
_POINTER_OPTIONAL_FIELDS = {"database_freshness"}
_FRESHNESS_FIELDS = {"status", "content_snapshot_at", "age_days"}
_FRESHNESS_NOTICE_FIELDS = {"code", "scope", "dedupe_key", "message_ja"}
_BINDING_FIELDS = {
    "schema_version",
    "token_digest",
    "result_set_id",
    "selected_db",
    "evidence_ids",
    "created_at",
    "expires_at",
    "manifest_integrity",
    "summary_integrity",
    "bundle_size",
}


class GatewayError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FileIntegrity:
    sha256: str
    size: int


@dataclass(frozen=True)
class ResultBinding:
    result_set_id: str
    selected_db: str
    evidence_ids: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    manifest_integrity: FileIntegrity
    summary_integrity: FileIntegrity
    bundle_size: int


def default_registry_root() -> Path:
    return Path(tempfile.gettempdir()) / "GitHubCopilotLocalRAG" / "skill-bindings"


def parse_search_pointer(value: object) -> tuple[str, int]:
    """Parse the closed pointer schema without trusting its file locator."""

    if (
        not isinstance(value, dict)
        or not _POINTER_FIELDS.issubset(value)
        or set(value) - _POINTER_FIELDS - _POINTER_OPTIONAL_FIELDS
        or not _valid_database_freshness(value.get("database_freshness"))
    ):
        raise GatewayError("invalid_search_pointer")
    result_id, size = value.get("result_set_id"), value.get("bytes")
    if (
        value.get("status") != "written"
        or value.get("schema_version") != POINTER_SCHEMA
        or not isinstance(result_id, str)
        or not isinstance(value.get("summary_file"), str)
        or not isinstance(value.get("expires_at"), str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 2 <= size <= MAX_BUNDLE_FILE_BYTES
    ):
        raise GatewayError("invalid_search_pointer")
    try:
        canonical = str(uuid.UUID(result_id))
    except (ValueError, AttributeError) as exc:
        raise GatewayError("invalid_search_pointer") from exc
    if canonical != result_id:
        raise GatewayError("invalid_search_pointer")
    return result_id, size


def _valid_database_freshness(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    fields = set(value)
    if fields not in (_FRESHNESS_FIELDS, _FRESHNESS_FIELDS | {"chat_notice"}):
        return False
    status = value.get("status")
    snapshot = value.get("content_snapshot_at")
    age_days = value.get("age_days")
    if status not in {"current", "stale", "unknown"}:
        return False
    if status == "unknown":
        if snapshot is not None or age_days is not None:
            return False
    elif (
        not isinstance(snapshot, str)
        or not _valid_utc_timestamp(snapshot)
        or not isinstance(age_days, int)
        or isinstance(age_days, bool)
        or age_days < 0
    ):
        return False
    notice = value.get("chat_notice")
    if notice is None:
        return status != "stale"
    return (
        status == "stale"
        and isinstance(notice, dict)
        and set(notice) == _FRESHNESS_NOTICE_FIELDS
        and all(isinstance(notice.get(field), str) and notice[field]
                for field in _FRESHNESS_NOTICE_FIELDS)
    )


def _valid_utc_timestamp(value: object) -> bool:
    try:
        _parse_utc(value)
    except GatewayError:
        return False
    return True


def create_result_binding(
    result_set_id: str,
    selected_db: str,
    summary: dict[str, Any],
    bundle_expires_at: datetime,
    pointer_size: int,
    *,
    spool_root: Path,
    now: datetime | None = None,
) -> ResultBinding:
    current = _utc_now(now)
    if not _DATABASE_RE.fullmatch(selected_db):
        raise GatewayError("invalid_result_bundle")
    result_dir = _safe_result_directory(spool_root, result_set_id)
    manifest = _digest_regular(result_dir / "manifest.json", result_dir)
    summary_integrity = _digest_regular(result_dir / "summary.json", result_dir)
    meta = _read_object(result_dir / "meta.json", result_dir)
    bundle_size = meta.get("bundle_bytes")
    if (
        summary_integrity.size != pointer_size
        or meta.get("schema_version") != "rag-result-meta-v1"
        or meta.get("result_set_id") != result_set_id
        or meta.get("selected_db") != selected_db
        or not isinstance(bundle_size, int)
        or isinstance(bundle_size, bool)
        or bundle_size <= 0
    ):
        raise GatewayError("invalid_result_bundle")
    expires_at = min(
        _utc_datetime(bundle_expires_at),
        current + timedelta(seconds=TOKEN_TTL_SECONDS),
    )
    if expires_at <= current:
        raise GatewayError("stale_result")
    follow_up = summary.get("follow_up")
    if not isinstance(follow_up, dict):
        raise GatewayError("invalid_result_bundle")
    available = follow_up.get("available_item_ids")
    defaults = follow_up.get("default_item_ids")
    if (
        not isinstance(available, list)
        or not isinstance(defaults, list)
        or len(available) != len(set(available))
        or len(defaults) != len(set(defaults))
        or any(not isinstance(item, str) or not _ITEM_ID_RE.fullmatch(item)
               for item in available)
        or any(item not in available for item in defaults)
        or len(defaults) > MAX_INSPECTABLE_EVIDENCE_IDS
    ):
        raise GatewayError("invalid_result_bundle")
    # Bind only IDs that the bounded answer packet advertises.  The bundle may
    # contain more internal items, but unadvertised IDs are never authorized by
    # this token.
    evidence_ids = tuple(defaults or available[:MAX_INSPECTABLE_EVIDENCE_IDS])
    return ResultBinding(
        result_set_id=result_set_id,
        selected_db=selected_db,
        evidence_ids=evidence_ids,
        created_at=current,
        expires_at=expires_at,
        manifest_integrity=manifest,
        summary_integrity=summary_integrity,
        bundle_size=bundle_size,
    )


def revalidate_result_binding(
    binding: ResultBinding,
    *,
    spool_root: Path,
    now: datetime | None = None,
) -> None:
    current = _utc_now(now)
    if binding.expires_at <= current:
        raise GatewayError("stale_result")
    result_dir = _safe_result_directory(spool_root, binding.result_set_id)
    meta = _read_object(result_dir / "meta.json", result_dir)
    if (
        _digest_regular(result_dir / "manifest.json", result_dir)
        != binding.manifest_integrity
        or _digest_regular(result_dir / "summary.json", result_dir)
        != binding.summary_integrity
        or meta.get("schema_version") != "rag-result-meta-v1"
        or meta.get("result_set_id") != binding.result_set_id
        or meta.get("selected_db") != binding.selected_db
        or meta.get("bundle_bytes") != binding.bundle_size
        or _parse_utc(meta.get("expires_at")) <= current
    ):
        raise GatewayError("stale_result")


class DiskTokenRegistry:
    """Short-lived, path-free capability records shared by runner processes."""

    def __init__(self, root: Path | None = None, *, maximum: int = TOKEN_REGISTRY_SIZE) -> None:
        self.root = (root or default_registry_root()).expanduser().absolute()
        self.maximum = min(TOKEN_REGISTRY_SIZE, max(1, int(maximum)))

    def add(self, binding: ResultBinding, *, now: datetime | None = None) -> str:
        current = _utc_now(now)
        self._prepare_root()
        self.cleanup(now=current)
        self._prune_to(self.maximum - 1)
        for _attempt in range(16):
            token = TOKEN_PREFIX + secrets.token_urlsafe(24)
            digest = _token_digest(token)
            path = self.root / f"{digest}.json"
            temporary = self.root / f".{digest}.{uuid.uuid4().hex}.tmp"
            record = _binding_record(binding, digest)
            raw = _compact_json(record).encode("utf-8")
            if len(raw) > MAX_REGISTRY_RECORD_BYTES:
                raise GatewayError("binding_registry_write_failed")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            published = False
            try:
                descriptor = os.open(temporary, flags, 0o600)
            except FileExistsError:
                continue
            except OSError as exc:
                raise GatewayError("binding_registry_write_failed") from exc
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(raw)
                    stream.flush()
                    try:
                        os.fsync(stream.fileno())
                    except OSError:
                        pass
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                if path.exists():
                    temporary.unlink(missing_ok=True)
                    continue
                os.replace(temporary, path)
                published = True
                if not _safe_regular(path, self.root):
                    raise GatewayError("binding_registry_write_failed")
                return token
            except Exception:
                try:
                    temporary.unlink(missing_ok=True)
                    if published:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        raise GatewayError("binding_registry_write_failed")

    def get(self, token: str, *, now: datetime | None = None) -> ResultBinding | None:
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            return None
        current = _utc_now(now)
        self._prepare_root()
        self.cleanup(now=current)
        digest = _token_digest(token)
        path = self.root / f"{digest}.json"
        try:
            record = _read_registry_record(path, self.root)
            binding = _record_binding(record, digest)
        except (GatewayError, OSError, ValueError, json.JSONDecodeError):
            return None
        if binding.expires_at <= current:
            self.discard(token)
            return None
        return binding

    def discard(self, token: str) -> None:
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            return
        try:
            self._prepare_root()
            path = self.root / f"{_token_digest(token)}.json"
            if _safe_regular(path, self.root):
                path.unlink(missing_ok=True)
        except (GatewayError, OSError):
            return

    def cleanup(self, *, now: datetime | None = None) -> None:
        current = _utc_now(now)
        self._prepare_root()
        retained: list[tuple[datetime, Path]] = []
        try:
            entries = list(self.root.iterdir())
        except OSError as exc:
            raise GatewayError("binding_registry_unavailable") from exc
        for path in entries:
            if _TEMP_RECORD_NAME_RE.fullmatch(path.name):
                if not _safe_regular(path, self.root):
                    raise GatewayError("binding_registry_unavailable")
                try:
                    age = current.timestamp() - path.stat().st_mtime
                    if age > REGISTRY_TEMP_MAX_AGE_SECONDS:
                        path.unlink(missing_ok=True)
                except OSError as exc:
                    raise GatewayError("binding_registry_unavailable") from exc
                continue
            if not _RECORD_NAME_RE.fullmatch(path.name) or not _safe_regular(path, self.root):
                raise GatewayError("binding_registry_unavailable")
            try:
                record = _read_registry_record(path, self.root)
                binding = _record_binding(record, path.stem)
            except (GatewayError, OSError, ValueError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                continue
            if binding.expires_at <= current:
                path.unlink(missing_ok=True)
            else:
                retained.append((binding.created_at, path))
        if len(retained) > self.maximum:
            for _created, path in sorted(retained)[: len(retained) - self.maximum]:
                path.unlink(missing_ok=True)

    def _prune_to(self, count: int) -> None:
        records: list[tuple[int, Path]] = []
        for path in self.root.iterdir():
            if _RECORD_NAME_RE.fullmatch(path.name) and _safe_regular(path, self.root):
                records.append((path.stat().st_mtime_ns, path))
        for _stamp, path in sorted(records)[: max(0, len(records) - count)]:
            path.unlink(missing_ok=True)

    def _prepare_root(self) -> None:
        parent = self.root.parent
        if parent.exists() and not _safe_directory(parent):
            raise GatewayError("binding_registry_unavailable")
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.root, 0o700)
            except OSError:
                pass
        except OSError as exc:
            raise GatewayError("binding_registry_unavailable") from exc
        if not _safe_directory(self.root):
            raise GatewayError("binding_registry_unavailable")


def _binding_record(binding: ResultBinding, token_digest: str) -> dict[str, Any]:
    return {
        "schema_version": BINDING_SCHEMA,
        "token_digest": token_digest,
        "result_set_id": binding.result_set_id,
        "selected_db": binding.selected_db,
        "evidence_ids": list(binding.evidence_ids),
        "created_at": _iso_utc(binding.created_at),
        "expires_at": _iso_utc(binding.expires_at),
        "manifest_integrity": {
            "sha256": binding.manifest_integrity.sha256,
            "size": binding.manifest_integrity.size,
        },
        "summary_integrity": {
            "sha256": binding.summary_integrity.sha256,
            "size": binding.summary_integrity.size,
        },
        "bundle_size": binding.bundle_size,
    }


def _record_binding(record: object, token_digest: str) -> ResultBinding:
    if not isinstance(record, dict) or set(record) != _BINDING_FIELDS:
        raise GatewayError("invalid_binding_record")
    if record.get("schema_version") != BINDING_SCHEMA or record.get("token_digest") != token_digest:
        raise GatewayError("invalid_binding_record")
    result_id = record.get("result_set_id")
    database = record.get("selected_db")
    evidence = record.get("evidence_ids")
    if (
        not isinstance(result_id, str)
        or str(uuid.UUID(result_id)) != result_id
        or not isinstance(database, str)
        or not _DATABASE_RE.fullmatch(database)
        or not isinstance(evidence, list)
        or len(evidence) > 20
        or len(evidence) != len(set(evidence))
        or any(not isinstance(item, str) or not _ITEM_ID_RE.fullmatch(item) for item in evidence)
    ):
        raise GatewayError("invalid_binding_record")
    manifest = _record_integrity(record.get("manifest_integrity"))
    summary = _record_integrity(record.get("summary_integrity"))
    bundle_size = record.get("bundle_size")
    if not isinstance(bundle_size, int) or isinstance(bundle_size, bool) or bundle_size <= 0:
        raise GatewayError("invalid_binding_record")
    created_at = _parse_utc(record.get("created_at"))
    expires_at = _parse_utc(record.get("expires_at"))
    if expires_at <= created_at or expires_at - created_at > timedelta(seconds=TOKEN_TTL_SECONDS):
        raise GatewayError("invalid_binding_record")
    return ResultBinding(
        result_id,
        database,
        tuple(evidence),
        created_at,
        expires_at,
        manifest,
        summary,
        bundle_size,
    )


def _record_integrity(value: object) -> FileIntegrity:
    if not isinstance(value, dict) or set(value) != {"sha256", "size"}:
        raise GatewayError("invalid_binding_record")
    digest, size = value.get("sha256"), value.get("size")
    if (
        not isinstance(digest, str)
        or not _SHA256_RE.fullmatch(digest)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 2 <= size <= MAX_BUNDLE_FILE_BYTES
    ):
        raise GatewayError("invalid_binding_record")
    return FileIntegrity(digest, size)


def _read_registry_record(path: Path, root: Path) -> dict[str, Any]:
    if not _safe_regular(path, root):
        raise GatewayError("invalid_binding_record")
    raw = path.read_bytes()
    if not 2 <= len(raw) <= MAX_REGISTRY_RECORD_BYTES:
        raise GatewayError("invalid_binding_record")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayError("invalid_binding_record") from exc
    if not isinstance(value, dict):
        raise GatewayError("invalid_binding_record")
    return value


def _safe_result_directory(spool_root: Path, result_set_id: str) -> Path:
    root = spool_root.expanduser().absolute()
    try:
        canonical = str(uuid.UUID(result_set_id))
    except (ValueError, AttributeError) as exc:
        raise GatewayError("invalid_result_bundle") from exc
    if canonical != result_set_id or not _safe_directory(root):
        raise GatewayError("invalid_result_bundle")
    result_dir = root / canonical
    if result_dir.parent != root or not _safe_directory(result_dir):
        raise GatewayError("invalid_result_bundle")
    return result_dir


def _digest_regular(path: Path, parent: Path) -> FileIntegrity:
    if not _safe_regular(path, parent):
        raise GatewayError("invalid_result_bundle")
    try:
        before = path.lstat()
        if not 2 <= before.st_size <= MAX_BUNDLE_FILE_BYTES:
            raise GatewayError("invalid_result_bundle")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(64 * 1024):
                digest.update(block)
        after = path.lstat()
    except OSError as exc:
        raise GatewayError("invalid_result_bundle") from exc
    if _identity(before) != _identity(after) or not _safe_regular(path, parent):
        raise GatewayError("invalid_result_bundle")
    return FileIntegrity(digest.hexdigest(), before.st_size)


def _read_object(path: Path, parent: Path) -> dict[str, Any]:
    if not _safe_regular(path, parent):
        raise GatewayError("invalid_result_bundle")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayError("invalid_result_bundle") from exc
    if not 2 <= len(raw) <= MAX_BUNDLE_FILE_BYTES or not isinstance(value, dict):
        raise GatewayError("invalid_result_bundle")
    return value


def _safe_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not _is_link_or_reparse(info)


def _safe_regular(path: Path, parent: Path) -> bool:
    if path.parent != parent:
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not _is_link_or_reparse(info)


def _is_link_or_reparse(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _utc_now(value: datetime | None = None) -> datetime:
    return _utc_datetime(value or datetime.now(timezone.utc))


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise GatewayError("invalid_result_bundle")
    return value.astimezone(timezone.utc)


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GatewayError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GatewayError("invalid_timestamp") from exc
    return _utc_datetime(parsed)


def _iso_utc(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


def validated_item_ids(values: Iterable[str], *, maximum: int = 3) -> tuple[str, ...]:
    result = tuple(values)
    if (
        not 1 <= len(result) <= maximum
        or len(result) != len(set(result))
        or any(not isinstance(item, str) or not _ITEM_ID_RE.fullmatch(item) for item in result)
    ):
        raise GatewayError("invalid_evidence_ids")
    return result
