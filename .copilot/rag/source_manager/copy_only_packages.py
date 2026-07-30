from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .machine_connections import configured_sharepoint_root
from .setup_copy_bridge import restore_portable_database


_PATCH_MARKER = "_local_rag_copy_only_packages_installed"
_BUFFER_SIZE = 1024 * 1024


def install_copy_only_package_runtime() -> None:
    """Remove the generated installer and make package import copy-native."""

    from . import packages

    if bool(getattr(packages, _PATCH_MARKER, False)):
        return

    packages._RAG_DISTRIBUTION_FILES = frozenset(
        set(packages._RAG_DISTRIBUTION_FILES) | {"setup_copy.py"}
    )

    original_distribution_entries = packages._distribution_entries
    original_admin_entries = packages._admin_entries

    @functools.wraps(original_distribution_entries)
    def distribution_entries(*args: Any, **kwargs: Any):
        entries, databases = original_distribution_entries(*args, **kwargs)
        return _without_bootstrap(entries), databases

    @functools.wraps(original_admin_entries)
    def admin_entries(*args: Any, **kwargs: Any):
        entries, databases = original_admin_entries(*args, **kwargs)
        return _without_bootstrap(entries), databases

    packages._distribution_entries = distribution_entries
    packages._admin_entries = admin_entries
    packages._sharepoint_external_identities = _external_source_identities
    packages.import_package = _import_package
    setattr(packages, _PATCH_MARKER, True)


def _without_bootstrap(entries: Sequence[Any]) -> list[Any]:
    return [
        entry
        for entry in entries
        if str(getattr(entry, "destination", "")) != "bootstrap.py"
        and str(getattr(entry, "mode", "")) != "bootstrap"
    ]


def _external_source_identities(database_root: Path) -> list[tuple[str, str, Path]]:
    """Return SharePoint and Teams roots for portable admin-state rewriting."""

    identities: list[tuple[str, str, Path]] = []
    sources = Path(database_root) / "sources"
    if not sources.is_dir() or sources.is_symlink():
        return identities
    rag_root = Path(database_root).resolve(strict=False).parent.parent
    common_root = configured_sharepoint_root(rag_root)
    for source_json in sorted(sources.glob("*/source.json")):
        if source_json.is_symlink() or not source_json.is_file():
            continue
        try:
            if source_json.stat().st_size > 1024 * 1024:
                continue
            payload = json.loads(source_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or str(payload.get("source_type") or "") not in {
            "sharepoint",
            "teams",
        }:
            continue
        source_id = str(payload.get("source_id") or "").strip()
        local_source_key = str(
            payload.get("local_source_key") or source_json.parent.name
        ).strip()
        fetch = payload.get("fetch")
        if not source_id or not local_source_key or not isinstance(fetch, dict):
            continue
        root = common_root
        if root is None:
            environment_name = str(fetch.get("root_env") or "").strip()
            inherited = str(os.environ.get(environment_name) or "").strip()
            if not inherited:
                continue
            root = Path(inherited).expanduser()
        if not root.is_absolute():
            continue
        relative_text = str(fetch.get("relative_path") or "").strip()
        if relative_text:
            try:
                relative = _safe_relative(relative_text)
            except ValueError:
                continue
            root = root.joinpath(*relative.parts)
        try:
            root = root.resolve(strict=True)
        except OSError:
            continue
        identities.append((source_id, local_source_key, root))
    return identities


def _import_package(package_path: Path, copilot_home: Path) -> dict[str, Any]:
    """Import through the same copy layout users can perform manually.

    Package files live only below ``.copilot``.  A human may extract the package
    and copy that directory to the home directory.  Manager import adds staged DB
    replacement and rollback, but does not depend on a generated installer.
    """

    from . import packages

    package = Path(package_path).expanduser()
    target = Path(copilot_home).expanduser()
    if target.is_symlink():
        raise packages.PackageError("install_target_symlink_forbidden")
    target.mkdir(parents=True, exist_ok=True)
    target = target.resolve(strict=True)

    with tempfile.TemporaryDirectory(prefix="local-rag-package-import.") as temp:
        temporary = Path(temp)
        if package.is_file() and not package.is_symlink():
            package_root = temporary / "package"
            package_root.mkdir()
            manifest = packages._extract_distribution_zip(
                package,
                package_root,
                expected_kind=packages._DISTRIBUTION_KIND,
            )
        else:
            package_root = packages._real_directory(package, "package")
            manifest = packages.validate_package_tree(package_root)
        _publish_copy_tree(package_root, manifest, target, packages)
    return {
        "status": "imported",
        "kind": manifest["kind"],
        "databases": [str(item["name"]) for item in manifest.get("dbs", [])],
    }


def _publish_copy_tree(
    package_root: Path,
    manifest: Mapping[str, Any],
    target: Path,
    packages: Any,
) -> None:
    database_names = _database_names(manifest, packages)
    database_parent = _safe_directory(target, PurePosixPath("rag/dbs"), packages)
    staging_parent = Path(
        tempfile.mkdtemp(prefix=".local-rag-import.", dir=str(database_parent))
    )
    database_stages = {
        name: staging_parent / name
        for name in database_names
    }
    try:
        for record in manifest.get("files", []):
            relative = packages._safe_relative(str(record.get("path") or ""))
            if relative.as_posix() == "bootstrap.py":
                # Backward-compatible import of an older package; never execute it.
                continue
            if not relative.parts or relative.parts[0] != ".copilot":
                raise packages.PackageError("package_copy_root_invalid")
            source = package_root.joinpath(*relative.parts)
            if source.is_symlink() or not source.is_file():
                raise packages.PackageError("package_source_missing")
            database_name, database_relative = _database_path(relative, database_names)
            if database_name is not None:
                destination = _safe_destination(
                    database_stages[database_name],
                    database_relative,
                    packages,
                )
            else:
                destination = _safe_destination(
                    target,
                    PurePosixPath(*relative.parts[1:]),
                    packages,
                )
            _copy_atomic(source, destination)

        for name, stage in database_stages.items():
            _validate_staged_database(stage, name, manifest, packages)
            if manifest.get("kind") == packages._ADMIN_KIND:
                restore_portable_database(
                    stage,
                    portable_root=database_parent / name,
                    rag_root=target / "rag",
                )
        _publish_databases(database_parent, database_stages, packages)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def _database_names(manifest: Mapping[str, Any], packages: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in manifest.get("dbs", []):
        name = str(item.get("name") or "").strip() if isinstance(item, Mapping) else ""
        if not packages._DB_NAME.fullmatch(name) or name in seen:
            raise packages.PackageError("package_database_invalid")
        seen.add(name)
        output.append(name)
    return output


def _database_path(
    relative: PurePosixPath,
    database_names: Iterable[str],
) -> tuple[str | None, PurePosixPath]:
    parts = relative.parts
    if len(parts) < 4 or parts[:3] != (".copilot", "rag", "dbs"):
        return None, PurePosixPath()
    name = parts[3]
    if name not in set(database_names) or len(parts) < 5:
        raise ValueError("package_database_not_declared")
    return name, PurePosixPath(*parts[4:])


def _safe_directory(target: Path, relative: PurePosixPath, packages: Any) -> Path:
    current = target
    resolved_target = target.resolve(strict=True)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise packages.PackageError("install_target_symlink_forbidden")
        if current.exists() and not current.is_dir():
            raise packages.PackageError("install_target_path_invalid")
        current.mkdir(exist_ok=True)
        resolved = current.resolve(strict=True)
        if resolved != resolved_target and resolved_target not in resolved.parents:
            raise packages.PackageError("install_target_escape")
    return current


def _safe_destination(target: Path, relative: PurePosixPath, packages: Any) -> Path:
    if not relative.parts:
        raise packages.PackageError("install_target_path_invalid")
    target.mkdir(parents=True, exist_ok=True)
    current = target
    resolved_target = target.resolve(strict=True)
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise packages.PackageError("install_target_symlink_forbidden")
        if current.exists() and not current.is_dir():
            raise packages.PackageError("install_target_path_invalid")
        current.mkdir(exist_ok=True)
        resolved = current.resolve(strict=True)
        if resolved != resolved_target and resolved_target not in resolved.parents:
            raise packages.PackageError("install_target_escape")
    destination = current / relative.parts[-1]
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise packages.PackageError("install_target_path_invalid")
    return destination


def _copy_atomic(source: Path, destination: Path) -> None:
    temporary = destination.parent / (
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, _BUFFER_SIZE)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_staged_database(
    stage: Path,
    db_name: str,
    manifest: Mapping[str, Any],
    packages: Any,
) -> None:
    prefix = f".copilot/rag/dbs/{db_name}/"
    expected = {
        str(item["path"])[len(prefix):]: item
        for item in manifest.get("files", [])
        if isinstance(item, Mapping) and str(item.get("path") or "").startswith(prefix)
    }
    if stage.is_symlink() or not stage.is_dir() or not expected:
        raise packages.PackageError("staged_database_invalid")
    actual: dict[str, Path] = {}
    for path in stage.rglob("*"):
        if path.is_symlink():
            raise packages.PackageError("package_symlink_forbidden")
        if not path.is_file():
            continue
        relative = path.relative_to(stage).as_posix()
        packages._safe_relative(relative)
        actual[relative] = path
    if set(actual) != set(expected):
        raise packages.PackageError("staged_database_manifest_mismatch")
    for relative, path in actual.items():
        record = expected[relative]
        if (
            path.stat().st_size != int(record.get("size", -1))
            or _sha256(path) != record.get("sha256")
        ):
            raise packages.PackageError("staged_database_checksum_mismatch")


def _publish_databases(
    database_parent: Path,
    database_stages: Mapping[str, Path],
    packages: Any,
) -> None:
    backups: dict[str, Path] = {}
    published: list[str] = []
    try:
        for name in sorted(database_stages):
            destination = database_parent / name
            if destination.is_symlink():
                raise packages.PackageError("install_database_symlink_forbidden")
            if destination.exists():
                if not destination.is_dir():
                    raise packages.PackageError("install_database_path_invalid")
                backup = database_parent / f".{name}.{uuid.uuid4().hex}.previous"
                os.replace(destination, backup)
                backups[name] = backup
        for name in sorted(database_stages):
            os.replace(database_stages[name], database_parent / name)
            published.append(name)
    except BaseException:
        restore_failed = False
        for name in reversed(published):
            destination = database_parent / name
            try:
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                elif destination.exists() or destination.is_symlink():
                    destination.unlink()
            except OSError:
                restore_failed = True
        for name in reversed(sorted(backups)):
            backup = backups[name]
            destination = database_parent / name
            try:
                if backup.exists() and not destination.exists():
                    os.replace(backup, destination)
            except OSError:
                restore_failed = True
        if restore_failed:
            raise packages.PackageError("install_database_restore_failed")
        raise
    else:
        for backup in backups.values():
            shutil.rmtree(backup, ignore_errors=True)


def _safe_relative(value: str) -> PurePosixPath:
    text = str(value or "").replace("\\", "/").strip("/")
    relative = PurePosixPath(text)
    if not text or relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("invalid_relative_path")
    return relative


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["install_copy_only_package_runtime"]
