from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


_PORTABLE_DB_PATH = "__local_rag_db_relative_path__"
_PORTABLE_EXTERNAL_SOURCE = "__local_rag_sharepoint_source_key__"
_PORTABLE_EXTERNAL_SUFFIX = "source_relative_suffix"
_CONNECTION_SCHEMA = "local-rag.source-connections.v1"
_DB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
_MAX_STATE_BYTES = 16 * 1024 * 1024


def restore_copied_installation(rag_root: str | Path | None = None) -> dict[str, Any]:
    """Rebind portable admin-transfer state after the .copilot tree is copied.

    Search-only distribution packages normally contain no portable ADD state, so
    this is a no-op for ordinary users.  Administrator transfer packages may
    contain DB-relative placeholders.  They are restored here, during setup,
    rather than by a separate package-level installer.
    """

    root = (
        Path(rag_root).expanduser()
        if rag_root is not None
        else Path(__file__).resolve().parent
    )
    root = root.resolve(strict=False)
    dbs_root = root / "dbs"
    result: dict[str, Any] = {
        "status": "ok",
        "databases_checked": 0,
        "files_rewritten": 0,
        "values_restored": 0,
        "unresolved_external_roots": 0,
        "warnings": [],
    }
    if dbs_root.is_symlink() or not dbs_root.is_dir():
        return result

    for candidate in sorted(dbs_root.iterdir(), key=lambda value: value.name.casefold()):
        if (
            candidate.is_symlink()
            or not candidate.is_dir()
            or not _DB_NAME.fullmatch(candidate.name)
        ):
            continue
        database = restore_portable_database(
            candidate,
            portable_root=candidate,
            rag_root=root,
        )
        result["databases_checked"] += 1
        result["files_rewritten"] += int(database["files_rewritten"])
        result["values_restored"] += int(database["values_restored"])
        result["unresolved_external_roots"] += int(
            database["unresolved_external_roots"]
        )
        result["warnings"].extend(database["warnings"])
    return result


def restore_portable_database(
    db_root: str | Path,
    *,
    portable_root: str | Path | None = None,
    rag_root: str | Path | None = None,
) -> dict[str, Any]:
    """Restore one copied DB without following links outside the DB tree."""

    database = Path(db_root).expanduser()
    if database.is_symlink() or not database.is_dir():
        return _empty_result("database_missing_or_unsafe")
    database = database.resolve(strict=True)
    target_root = (
        Path(portable_root).expanduser().resolve(strict=False)
        if portable_root is not None
        else database
    )
    installation_root = (
        Path(rag_root).expanduser().resolve(strict=False)
        if rag_root is not None
        else database.parent.parent
    )
    logs = database / "logs"
    if logs.is_symlink() or not logs.is_dir():
        return _empty_result()

    files_rewritten = 0
    values_restored = 0
    unresolved = 0
    warnings: list[str] = []
    for path in sorted(logs.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        try:
            if path.stat().st_size > _MAX_STATE_BYTES:
                warnings.append(f"state_too_large:{path.name}")
                continue
            if path.suffix == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                restored, changed, count, missing = _restore_value(
                    payload,
                    database=database,
                    target_root=target_root,
                    rag_root=installation_root,
                )
                if changed:
                    _atomic_json(path, restored)
                    files_rewritten += 1
            else:
                values = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                restored_values: list[Any] = []
                changed = False
                count = 0
                missing = 0
                for value in values:
                    restored, item_changed, item_count, item_missing = _restore_value(
                        value,
                        database=database,
                        target_root=target_root,
                        rag_root=installation_root,
                    )
                    restored_values.append(restored)
                    changed = changed or item_changed
                    count += item_count
                    missing += item_missing
                if changed:
                    _atomic_jsonl(path, restored_values)
                    files_rewritten += 1
            values_restored += count
            unresolved += missing
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            warnings.append(f"state_restore_skipped:{path.name}:{type(exc).__name__}")
    return {
        "status": "ok",
        "files_rewritten": files_rewritten,
        "values_restored": values_restored,
        "unresolved_external_roots": unresolved,
        "warnings": warnings,
    }


def _restore_value(
    value: Any,
    *,
    database: Path,
    target_root: Path,
    rag_root: Path,
) -> tuple[Any, bool, int, int]:
    if isinstance(value, list):
        output: list[Any] = []
        changed = False
        restored_count = 0
        unresolved = 0
        for item in value:
            restored, item_changed, item_count, item_unresolved = _restore_value(
                item,
                database=database,
                target_root=target_root,
                rag_root=rag_root,
            )
            output.append(restored)
            changed = changed or item_changed
            restored_count += item_count
            unresolved += item_unresolved
        return output, changed, restored_count, unresolved
    if not isinstance(value, dict):
        return value, False, 0, 0

    if set(value) == {_PORTABLE_DB_PATH}:
        relative = _safe_relative(value[_PORTABLE_DB_PATH])
        candidate = target_root.joinpath(*relative.parts)
        _require_inside(candidate.parent, target_root)
        return str(candidate), True, 1, 0

    if set(value) == {_PORTABLE_EXTERNAL_SOURCE, _PORTABLE_EXTERNAL_SUFFIX}:
        key = str(value[_PORTABLE_EXTERNAL_SOURCE] or "").strip()
        source = _read_source(database, key)
        external_root = _configured_external_root(rag_root, source)
        if external_root is None:
            return value, False, 0, 1
        fetch = source["fetch"]
        candidate = external_root
        relative_text = str(fetch.get("relative_path") or "").strip()
        if relative_text:
            relative = _safe_relative(relative_text)
            candidate = candidate.joinpath(*relative.parts)
        suffix_text = str(value[_PORTABLE_EXTERNAL_SUFFIX] or "").strip()
        if suffix_text:
            suffix = _safe_relative(suffix_text)
            candidate = candidate.joinpath(*suffix.parts)
        _require_inside(candidate.resolve(strict=False), external_root.resolve(strict=False))
        return str(candidate), True, 1, 0

    output: dict[str, Any] = {}
    changed = False
    restored_count = 0
    unresolved = 0
    for key, item in value.items():
        restored, item_changed, item_count, item_unresolved = _restore_value(
            item,
            database=database,
            target_root=target_root,
            rag_root=rag_root,
        )
        output[str(key)] = restored
        changed = changed or item_changed
        restored_count += item_count
        unresolved += item_unresolved
    return output, changed, restored_count, unresolved


def _read_source(database: Path, key: str) -> dict[str, Any]:
    if not re.fullmatch(r"src_[a-z0-9][a-z0-9-]{0,39}-[0-9a-f]{12}", key):
        raise ValueError("invalid_source_key")
    path = database / "sources" / key / "source.json"
    _require_inside(path.parent, database)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("source_configuration_invalid")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("source_configuration_invalid")
    if str(payload.get("source_type") or "") not in {"sharepoint", "teams"}:
        raise ValueError("external_source_configuration_invalid")
    fetch = payload.get("fetch")
    if not isinstance(fetch, dict):
        raise ValueError("external_source_configuration_invalid")
    return payload


def _configured_external_root(rag_root: Path, source: Mapping[str, Any]) -> Path | None:
    config_path = rag_root / "config" / "source-connections.json"
    if not config_path.is_symlink() and config_path.is_file():
        try:
            if config_path.stat().st_size <= 1024 * 1024:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if (
                    isinstance(config, dict)
                    and config.get("schema_version") == _CONNECTION_SCHEMA
                ):
                    stored = str(config.get("sharepoint_root") or "").strip()
                    if stored:
                        candidate = Path(stored).expanduser()
                        if candidate.is_absolute():
                            return candidate
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    fetch = source.get("fetch")
    settings = fetch if isinstance(fetch, Mapping) else {}
    env_name = str(settings.get("root_env") or "LOCAL_RAG_SHAREPOINT_ROOT").strip()
    inherited = str(os.environ.get(env_name) or "").strip()
    if inherited:
        candidate = Path(inherited).expanduser()
        if candidate.is_absolute():
            return candidate
    return None


def _safe_relative(value: Any) -> PurePosixPath:
    text = str(value or "").replace("\\", "/").strip("/")
    relative = PurePosixPath(text)
    if (
        not text
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("invalid_portable_path")
    return relative


def _require_inside(candidate: Path, root: Path) -> None:
    resolved_root = root.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("portable_path_escape")


def _atomic_json(path: Path, payload: Any) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    _atomic_write(path, encoded)


def _atomic_jsonl(path: Path, values: list[Any]) -> None:
    encoded = (
        "\n".join(json.dumps(value, ensure_ascii=False, separators=(",", ":")) for value in values)
        + ("\n" if values else "")
    ).encode("utf-8")
    _atomic_write(path, encoded)


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _empty_result(warning: str | None = None) -> dict[str, Any]:
    return {
        "status": "ok",
        "files_rewritten": 0,
        "values_restored": 0,
        "unresolved_external_roots": 0,
        "warnings": [warning] if warning else [],
    }


__all__ = ["restore_copied_installation", "restore_portable_database"]
