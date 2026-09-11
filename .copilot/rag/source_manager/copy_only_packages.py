from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .machine_connections import configured_sharepoint_root
from .setup_copy_bridge import restore_portable_database
from .persistent_paths import create_persistent_directory


_PATCH_MARKER = "_local_rag_copy_only_packages_installed"
_BUFFER_SIZE = 1024 * 1024
_PACKAGE_INSTALLERS = frozenset({"install.ps1", "install.sh"})


def install_copy_only_package_runtime() -> None:
    """Remove the generated installer and make package import copy-native."""

    from . import packages

    if bool(getattr(packages, _PATCH_MARKER, False)):
        return

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
    and copy that directory to the home directory. Manager import replaces each
    included DB directly, without retaining its old contents or a staged DB copy.
    """

    from . import packages

    package = Path(package_path).expanduser()
    target = Path(copilot_home).expanduser()
    _check_target_path(target, directory=True, packages=packages)
    _reject_overlap(package, target, packages)
    if not target.exists():
        create_persistent_directory(
            target,
            trusted_root=target.parent,
        )
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
    _reject_overlap(package_root, target, packages)
    database_parent = target / "rag" / "dbs"
    files: list[tuple[Path, PurePosixPath, str | None]] = []
    for record in manifest.get("files", []):
        relative = packages._safe_relative(str(record.get("path") or ""))
        if any(":" in part for part in relative.parts):
            raise packages.PackageError("package_path_invalid")
        if relative.as_posix() in {"bootstrap.py", *_PACKAGE_INSTALLERS}:
            continue
        if not relative.parts or relative.parts[0] != ".copilot":
            raise packages.PackageError("package_copy_root_invalid")
        source = package_root.joinpath(*relative.parts)
        for path in (source, *source.parents):
            if path == package_root.parent:
                break
            if _is_link(path):
                raise packages.PackageError("package_symlink_forbidden")
        if not source.is_file():
            raise packages.PackageError("package_source_missing")
        name, database_relative = _database_path(relative, database_names)
        destination_relative = (
            database_relative if name is not None else PurePosixPath(*relative.parts[1:])
        )
        if name is None:
            _check_target_path(
                target.joinpath(*destination_relative.parts),
                directory=False, packages=packages,
            )
        files.append((source, destination_relative, name))

    # Check every DB and target before removing any old database.
    for name in database_names:
        _check_target_path(database_parent / name, directory=True, packages=packages)
        _validate_database(
            package_root / ".copilot" / "rag" / "dbs" / name, name, manifest, packages,
        )

    if database_names:
        create_persistent_directory(
            database_parent, trusted_root=target, parents=True, exist_ok=True,
        )
    for source, relative, name in files:
        if name is None:
            _copy_atomic(source, _safe_destination(target, relative, packages))
    for name in database_names:
        destination = database_parent / name
        _check_target_path(destination, directory=True, packages=packages)
        try:
            if destination.exists():
                shutil.rmtree(destination)
            for source, relative, database_name in files:
                if database_name == name:
                    _copy_atomic(source, _safe_destination(destination, relative, packages))
            _validate_database(destination, name, manifest, packages)
            if manifest.get("kind") == packages._ADMIN_KIND:
                restore_portable_database(
                    destination,
                    portable_root=destination,
                    rag_root=target / "rag",
                )
        except BaseException as exc:
            # The old DB is intentionally gone; remove only this incomplete DB.
            try:
                _check_target_path(destination, directory=True, packages=packages)
                if destination.exists():
                    shutil.rmtree(destination)
            except (OSError, packages.PackageError):
                raise packages.PackageError(
                    "install_database_cleanup_failed_reinstall_required"
                ) from exc
            raise packages.PackageError("install_database_failed_reinstall_required") from exc


def _is_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _check_target_path(path: Path, *, directory: bool, packages: Any) -> None:
    absolute = path.absolute()
    for current in (*reversed(absolute.parents), absolute):
        if _is_link(current):
            raise packages.PackageError("install_target_symlink_forbidden")
        if current.exists():
            expected_directory = directory or current != absolute
            if (expected_directory and not current.is_dir()) or (
                not expected_directory and not current.is_file()
            ):
                raise packages.PackageError("install_target_path_invalid")


def _reject_overlap(package: Path, target: Path, packages: Any) -> None:
    source = package.resolve(strict=False)
    destination = target.resolve(strict=False)
    if source == destination or source in destination.parents or destination in source.parents:
        raise packages.PackageError("install_source_target_overlap")


def _database_names(manifest: Mapping[str, Any], packages: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in manifest.get("dbs", []):
        name = str(item.get("name") or "").strip() if isinstance(item, Mapping) else ""
        if not packages._DB_NAME.fullmatch(name) or name.casefold() in seen:
            raise packages.PackageError("package_database_invalid")
        seen.add(name.casefold())
        output.append(name)
    return output


def _database_path(
    relative: PurePosixPath,
    database_names: Iterable[str],
) -> tuple[str | None, PurePosixPath]:
    parts = relative.parts
    if parts[:3] != (".copilot", "rag", "dbs"):
        return None, PurePosixPath()
    if len(parts) < 5:
        raise ValueError("package_database_not_declared")
    name = parts[3]
    if name not in set(database_names):
        raise ValueError("package_database_not_declared")
    return name, PurePosixPath(*parts[4:])


def _safe_destination(target: Path, relative: PurePosixPath, packages: Any) -> Path:
    if not relative.parts:
        raise packages.PackageError("install_target_path_invalid")
    if not target.exists():
        create_persistent_directory(
            target,
            trusted_root=target.parent,
        )
    current = target
    resolved_target = target.resolve(strict=True)
    for part in relative.parts[:-1]:
        current = current / part
        if _is_link(current):
            raise packages.PackageError("install_target_symlink_forbidden")
        if current.exists() and not current.is_dir():
            raise packages.PackageError("install_target_path_invalid")
        create_persistent_directory(
            current,
            trusted_root=target,
            exist_ok=True,
        )
        resolved = current.resolve(strict=True)
        if resolved != resolved_target and resolved_target not in resolved.parents:
            raise packages.PackageError("install_target_escape")
    destination = current / relative.parts[-1]
    if _is_link(destination) or (destination.exists() and not destination.is_file()):
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


def _validate_database(
    root: Path,
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
    if _is_link(root) or not root.is_dir() or not expected:
        raise packages.PackageError("package_database_invalid")
    actual: dict[str, Path] = {}
    for path in root.rglob("*"):
        if _is_link(path):
            raise packages.PackageError("package_symlink_forbidden")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        packages._safe_relative(relative)
        actual[relative] = path
    if set(actual) != set(expected):
        raise packages.PackageError("package_database_manifest_mismatch")
    for relative, path in actual.items():
        record = expected[relative]
        if (
            path.stat().st_size != int(record.get("size", -1))
            or _sha256(path) != record.get("sha256")
        ):
            raise packages.PackageError("package_database_checksum_mismatch")


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
