from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit

from .package_installers import INSTALL_PS1_TEXT, INSTALL_SH_TEXT


PACKAGE_SCHEMA = "local-rag.package.v1"
MANIFEST_NAME = "manifest.json"
PACKAGE_TOOL_NAME = "github-copilot-local-rag"
_DISTRIBUTION_KIND = "distribution"
_ADMIN_KIND = "admin-transfer"
_BUFFER_SIZE = 1024 * 1024
_MAX_TEXT_CONFIG_BYTES = 1024 * 1024
_DB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?:authorization|cookie|credential|password|passwd|secret|"
    r"token|private[_-]?key|proxy)\s*[:=]"
)
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "auth",
        "authorization",
        "code",
        "cookie",
        "credential",
        "key",
        "oauth",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)
_SECRET_FILENAMES = frozenset(
    {
        ".env",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "secret",
        "secret.json",
        "secrets",
        "secrets.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
_SECRET_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
_TRANSIENT_NAMES = frozenset(
    {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "cache",
        "run",
        "temp",
        "tmp",
    }
)
_TRANSIENT_SUFFIXES = (
    ".bak",
    ".lock",
    ".pyc",
    ".pyo",
    ".swp",
    ".tmp",
    "-journal",
    "-shm",
    "-wal",
)
_PORTABLE_DB_PATH = "__local_rag_db_relative_path__"
_PORTABLE_SHAREPOINT_SOURCE = "__local_rag_sharepoint_source_key__"
_PORTABLE_SHAREPOINT_SUFFIX = "source_relative_suffix"
_PRODUCT_TEST_DIRECTORIES = frozenset({"test", "tests"})
_PRODUCT_ROOT_DIRECTORIES = frozenset(
    {
        "config",
        "docs",
        "gen_db",
        "models",
        "query",
        "wrapper",
    }
)
_DISTRIBUTION_ADMIN_ROOT_FILES = frozenset(
    {
        "manage.py",
        "make_admin_transfer_package.py",
        "make_distribution_package.py",
    }
)
_PORTABLE_CONFIG_EXAMPLES = frozenset(
    {
        "manage-custom.example.json",
        "network.example.json",
    }
)
_PROJECT_SKILLS = ("local-rag", "local-rag-setup")
_DB_SEARCH_FILES = frozenset(
    {
        "DB_PROFILE.md",
        "VERSION.json",
        "catalog.sqlite",
        "db.json",
        "source-links.json",
    }
)
_DB_ADMIN_DIRECTORIES = frozenset({"data", "index", "logs", "sources"})
_RAG_WRAPPER_NAME = "rag-wrapper.json"
_RAG_WRAPPER_SCHEMA = "local-rag.wrapper.v1"
_BOOTSTRAP_TEXT = """#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath


DB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")


def safe_relative(value):
    text = str(value or "")
    relative = PurePosixPath(text)
    if (
        not text
        or "\\\\" in text
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise SystemExit("invalid_package_path")
    return relative


def safe_database_name(value):
    relative = safe_relative(value)
    if len(relative.parts) != 1 or not DB_NAME.fullmatch(relative.parts[0]):
        raise SystemExit("invalid_database_name")
    return relative.parts[0]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate(package, manifest):
    expected = {}
    for item in manifest.get("files", []):
        relative = safe_relative(item.get("path"))
        value = relative.as_posix()
        if value == "manifest.json" or value in expected:
            raise SystemExit("invalid_package_manifest")
        expected[value] = item
    actual = {}
    for path in package.rglob("*"):
        if path.is_symlink():
            raise SystemExit("package_symlink_forbidden")
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        if relative == "manifest.json":
            continue
        safe_relative(relative)
        actual[relative] = path
    if set(actual) != set(expected):
        raise SystemExit("package_manifest_coverage_mismatch")
    for relative, path in actual.items():
        item = expected[relative]
        if (
            path.stat().st_size != int(item.get("size", -1))
            or sha256(path) != item.get("sha256")
        ):
            raise SystemExit("package_checksum_mismatch")


def validate_staged_database(stage, db_name, manifest):
    prefix = ".copilot/rag/dbs/" + db_name + "/"
    expected = {
        item["path"][len(prefix):]: item
        for item in manifest.get("files", [])
        if str(item.get("path") or "").startswith(prefix)
    }
    actual = {}
    if stage.is_symlink() or not stage.is_dir():
        raise SystemExit("staged_database_invalid")
    for path in stage.rglob("*"):
        if path.is_symlink():
            raise SystemExit("package_symlink_forbidden")
        if not path.is_file():
            continue
        relative = path.relative_to(stage).as_posix()
        safe_relative(relative)
        actual[relative] = path
    if not expected or set(actual) != set(expected):
        raise SystemExit("staged_database_manifest_mismatch")
    for relative, path in actual.items():
        item = expected[relative]
        if (
            path.stat().st_size != int(item.get("size", -1))
            or sha256(path) != item.get("sha256")
        ):
            raise SystemExit("staged_database_checksum_mismatch")


def safe_destination(target, relative):
    if target.is_symlink():
        raise SystemExit("install_target_symlink_forbidden")
    target.mkdir(parents=True, exist_ok=True)
    resolved_target = target.resolve(strict=True)
    current = target
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise SystemExit("install_target_symlink_forbidden")
        if current.exists() and not current.is_dir():
            raise SystemExit("install_target_path_invalid")
        current.mkdir(exist_ok=True)
        resolved = current.resolve(strict=True)
        if resolved != resolved_target and resolved_target not in resolved.parents:
            raise SystemExit("install_target_escape")
    destination = current / relative.parts[-1]
    if destination.is_symlink():
        raise SystemExit("install_target_symlink_forbidden")
    if destination.exists() and not destination.is_file():
        raise SystemExit("install_target_path_invalid")
    resolved_parent = destination.parent.resolve(strict=True)
    if (
        resolved_parent != resolved_target
        and resolved_target not in resolved_parent.parents
    ):
        raise SystemExit("install_target_escape")
    return destination


def copy_atomic(source, destination):
    temporary = destination.parent / (
        "." + destination.name + "." + uuid.uuid4().hex + ".tmp"
    )
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def publish_databases(database_parent, database_stages):
    backups = {}
    published = []
    try:
        for name in sorted(database_stages):
            destination = database_parent / name
            if destination.is_symlink():
                raise SystemExit("install_database_symlink_forbidden")
            if destination.exists():
                if not destination.is_dir():
                    raise SystemExit("install_database_path_invalid")
                backup = database_parent / (
                    "." + name + "." + uuid.uuid4().hex + ".previous"
                )
                os.replace(destination, backup)
                backups[name] = backup
        for name in sorted(database_stages):
            destination = database_parent / name
            os.replace(database_stages[name], destination)
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
            raise SystemExit("install_database_restore_failed")
        raise
    else:
        for backup in backups.values():
            shutil.rmtree(backup, ignore_errors=True)


def main() -> int:
    package = Path(__file__).resolve().parent
    manifest = json.loads(
        (package / "manifest.json").read_text(encoding="utf-8")
    )
    validate(package, manifest)
    arguments = list(sys.argv[1:])
    skip_dependencies = "--skip-dependencies" in arguments
    skip_runtime_setup = "--skip-runtime-setup" in arguments
    arguments = [
        value
        for value in arguments
        if value not in {"--skip-dependencies", "--skip-runtime-setup"}
    ]
    if len(arguments) > 1:
        raise SystemExit(
            "usage: bootstrap.py [COPILOT_HOME] "
            "[--skip-dependencies|--skip-runtime-setup]"
        )
    target = (
        Path(arguments[0]).expanduser()
        if arguments
        else Path.home() / ".copilot"
    )
    if target.is_symlink():
        raise SystemExit("install_target_symlink_forbidden")
    database_names = [
        safe_database_name(item.get("name"))
        for item in manifest.get("dbs", [])
    ]
    database_parent = target / "rag" / "dbs"
    database_stages = {
        name: database_parent / (
            "." + name + "." + uuid.uuid4().hex + ".incoming"
        )
        for name in database_names
    }
    try:
        for item in manifest.get("files", []):
            relative = safe_relative(item.get("path"))
            if relative.parts[0] != ".copilot":
                continue
            source = package.joinpath(*relative.parts)
            is_database_path = (
                len(relative.parts) >= 3
                and relative.parts[1:3] == ("rag", "dbs")
            )
            if is_database_path and (
                len(relative.parts) < 5
                or relative.parts[3] not in database_stages
            ):
                raise SystemExit("package_database_not_declared")
            if is_database_path:
                stage = database_stages[relative.parts[3]]
                destination = safe_destination(
                    stage,
                    PurePosixPath(*relative.parts[4:]),
                )
            else:
                destination = safe_destination(
                    target,
                    PurePosixPath(*relative.parts[1:]),
                )
            copy_atomic(source, destination)
        for name, stage in database_stages.items():
            validate_staged_database(stage, name, manifest)
        if manifest.get("kind") == "admin-transfer":
            for name, stage in database_stages.items():
                restore_portable_database(
                    stage,
                    portable_root=database_parent / name,
                )
        publish_databases(database_parent, database_stages)
    finally:
        for stage in database_stages.values():
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    if skip_runtime_setup:
        return 0
    runtime = target / "rag" / "query" / ".venv"
    if skip_dependencies:
        subprocess.run(
            [sys.executable, "-m", "venv", str(runtime)],
            check=True,
        )
    else:
        subprocess.run(
            [
                sys.executable,
                str(target / "rag" / "query" / "setup.py"),
                "--format",
                "json",
            ],
            check=True,
        )
    return 0


def restore_portable_add_state(dbs_root):
    if not dbs_root.is_dir() or dbs_root.is_symlink():
        return
    for db_root in sorted(dbs_root.iterdir()):
        if db_root.is_dir() and not db_root.is_symlink():
            restore_portable_database(db_root)


def restore_portable_database(db_root, portable_root=None):
    marker = "__local_rag_db_relative_path__"
    sharepoint_marker = "__local_rag_sharepoint_source_key__"
    sharepoint_suffix = "source_relative_suffix"
    path_root = portable_root or db_root

    def restore(value):
        if isinstance(value, list):
            return [restore(item) for item in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {marker}:
            relative = safe_relative(value[marker])
            candidate = path_root.joinpath(*relative.parts)
            resolved_root = path_root.resolve(strict=False)
            resolved_parent = candidate.parent.resolve(strict=False)
            if (
                resolved_parent != resolved_root
                and resolved_root not in resolved_parent.parents
            ):
                raise SystemExit("portable_add_state_escape")
            return str(candidate)
        if set(value) == {sharepoint_marker, sharepoint_suffix}:
            key = str(value[sharepoint_marker])
            source_config = safe_destination(
                db_root,
                safe_relative("sources/" + key + "/source.json"),
            )
            try:
                source = json.loads(source_config.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise SystemExit("sharepoint_source_configuration_invalid")
            fetch = source.get("fetch") if isinstance(source, dict) else None
            if (
                not isinstance(fetch, dict)
                or source.get("source_type") != "sharepoint"
            ):
                raise SystemExit("sharepoint_source_configuration_invalid")
            environment_name = str(fetch.get("root_env") or "")
            environment_root = os.environ.get(environment_name)
            if not environment_name or not environment_root:
                raise SystemExit("sharepoint_root_reconfiguration_required")
            root = Path(environment_root).expanduser()
            if not root.is_absolute():
                raise SystemExit("sharepoint_root_reconfiguration_required")
            relative_text = str(fetch.get("relative_path") or "")
            source_root = root
            if relative_text:
                relative = safe_relative(relative_text)
                source_root = root.joinpath(*relative.parts)
            suffix_text = str(value[sharepoint_suffix] or "")
            candidate = source_root
            if suffix_text:
                suffix = safe_relative(suffix_text)
                candidate = source_root.joinpath(*suffix.parts)
            resolved_root = root.resolve(strict=False)
            resolved_candidate = candidate.resolve(strict=False)
            if (
                resolved_candidate != resolved_root
                and resolved_root not in resolved_candidate.parents
            ):
                raise SystemExit("portable_add_state_escape")
            return str(candidate)
        return {
            str(key): restore(item)
            for key, item in value.items()
        }

    def publish(path, payload, *, jsonl=False):
        temporary = path.parent / (
            "." + path.name + "." + uuid.uuid4().hex + ".tmp"
        )
        if jsonl:
            encoded = (
                "\\n".join(
                    json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    for item in payload
                )
                + ("\\n" if payload else "")
            ).encode("utf-8")
        else:
            encoded = (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\\n"
            ).encode("utf-8")
        try:
            with temporary.open("xb") as writer:
                writer.write(encoded)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    logs = db_root / "logs"
    if not logs.is_dir() or logs.is_symlink():
        return
    for path in sorted(logs.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            restored = restore(payload)
            if restored != payload:
                publish(path, restored)
        elif path.suffix == ".jsonl":
            values = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            restored = [restore(item) for item in values]
            if restored != values:
                publish(path, restored, jsonl=True)


if __name__ == "__main__":
    raise SystemExit(main())
"""


class PackageError(RuntimeError):
    """A non-sensitive package contract failure."""


@dataclass(frozen=True)
class _Entry:
    source: Path | None
    destination: str
    mode: str = "copy"
    database_root: Path | None = None
    content_snapshot_at: str | None = None
    source_root: Path | None = None


def create_distribution_package(
    copilot_home: Path,
    output_zip: Path,
    *,
    db_names: Sequence[str] | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Create and atomically publish one validated distribution ZIP."""
    home = _real_directory(copilot_home, "copilot_home")
    output = _new_output_path(output_zip, directory=False)
    created = _created_at(created_at)
    entries, databases = _distribution_entries(
        home,
        db_names=db_names,
    )
    stage = Path(
        tempfile.mkdtemp(
            prefix=".local-rag-distribution.",
            dir=str(output.parent),
        )
    )
    archive_tmp = output.parent / (
        f".{output.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        manifest = _stage_package(
            stage,
            entries,
            kind=_DISTRIBUTION_KIND,
            databases=databases,
            created=created,
            tool_version=_tool_version(home / "rag"),
        )
        validate_package_tree(stage, expected_kind=_DISTRIBUTION_KIND)
        _write_zip(stage, archive_tmp)
        validate_distribution_zip(
            archive_tmp,
            expected_kind=_DISTRIBUTION_KIND,
        )
        _fsync_file(archive_tmp)
        os.replace(archive_tmp, output)
        _fsync_directory(output.parent)
        return {
            "status": "written",
            "kind": _DISTRIBUTION_KIND,
            "output": str(output),
            "manifest": manifest,
        }
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            archive_tmp.unlink()
        except OSError:
            pass


def create_admin_transfer_package(
    copilot_home: Path,
    output_directory: Path,
    *,
    db_names: Sequence[str] | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Create or resume one validated admin-transfer folder."""
    home = _real_directory(copilot_home, "copilot_home")
    output = Path(output_directory).expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise PackageError("package_output_exists")
    if output.exists():
        try:
            manifest = validate_package_tree(
                output,
                expected_kind=_ADMIN_KIND,
            )
        except PackageError as exc:
            raise PackageError("package_output_exists") from exc
        return {
            "status": "already_complete",
            "kind": _ADMIN_KIND,
            "output": str(output),
            "manifest": manifest,
        }
    created = _created_at(created_at)
    entries, databases = _admin_entries(home, db_names=db_names)
    stage = output.parent / f".{output.name}.partial"
    if stage.is_symlink() or (stage.exists() and not stage.is_dir()):
        raise PackageError("package_resume_path_invalid")
    stage.mkdir(parents=True, exist_ok=True)
    _validate_resume_stage(stage)
    try:
        (stage / MANIFEST_NAME).unlink()
    except FileNotFoundError:
        pass
    manifest = _stage_package(
        stage,
        entries,
        kind=_ADMIN_KIND,
        databases=databases,
        created=created,
        tool_version=_tool_version(home / "rag"),
    )
    validate_package_tree(stage, expected_kind=_ADMIN_KIND)
    os.replace(stage, output)
    _fsync_directory(output.parent)
    return {
        "status": "written",
        "kind": _ADMIN_KIND,
        "output": str(output),
        "manifest": manifest,
    }


def validate_package_tree(
    root: Path,
    *,
    expected_kind: str | None = None,
) -> dict[str, Any]:
    """Validate exact manifest coverage and every file checksum."""
    package_root = _real_directory(root, "package")
    manifest_path = package_root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PackageError("package_manifest_missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("package_manifest_invalid") from exc
    _validate_manifest_shape(manifest, expected_kind=expected_kind)
    expected: dict[str, Mapping[str, Any]] = {}
    for record in manifest["files"]:
        relative = _safe_relative(str(record["path"]))
        value = relative.as_posix()
        if value == MANIFEST_NAME or value in expected:
            raise PackageError("package_manifest_path_invalid")
        expected[value] = record
    actual: dict[str, Path] = {}
    for path in package_root.rglob("*"):
        if path.is_symlink():
            raise PackageError("package_symlink_forbidden")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PackageError("package_special_file_forbidden")
        relative = path.relative_to(package_root).as_posix()
        if relative == MANIFEST_NAME:
            continue
        _safe_relative(relative)
        actual[relative] = path
    if set(actual) != set(expected):
        raise PackageError("package_manifest_coverage_mismatch")
    total_size = 0
    for relative, record in expected.items():
        path = actual[relative]
        size = path.stat().st_size
        digest = _sha256(path)
        if (
            size != int(record["size"])
            or digest != str(record["sha256"])
        ):
            raise PackageError("package_checksum_mismatch")
        total_size += size
    total = manifest["total"]
    if (
        int(total["files"]) != len(actual)
        or int(total["bytes"]) != total_size
    ):
        raise PackageError("package_total_mismatch")
    return manifest


def _validate_resume_stage(stage: Path) -> None:
    root = stage.resolve(strict=True)
    for path in stage.rglob("*"):
        if path.is_symlink():
            raise PackageError("package_resume_symlink_forbidden")
        if not path.is_dir() and not path.is_file():
            raise PackageError("package_resume_special_file_forbidden")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise PackageError("package_resume_path_invalid") from exc
        if resolved != root and root not in resolved.parents:
            raise PackageError("package_resume_path_invalid")


def validate_distribution_zip(
    archive_path: Path,
    *,
    expected_kind: str | None = _DISTRIBUTION_KIND,
) -> dict[str, Any]:
    """Validate a ZIP without trusting archive paths or external attributes."""
    archive = Path(archive_path)
    if archive.is_symlink() or not archive.is_file():
        raise PackageError("package_archive_missing")
    with tempfile.TemporaryDirectory(prefix="local-rag-package-verify.") as temp:
        root = Path(temp)
        return _extract_distribution_zip(
            archive,
            root,
            expected_kind=expected_kind,
        )


def import_package(
    package_path: Path,
    copilot_home: Path,
) -> dict[str, Any]:
    """Import a validated package and safely replace same-name databases.

    The generated standalone bootstrap owns publication so manager imports and
    manual bootstrap runs share the exact same staged validation and rollback
    behavior. Runtime setup is deliberately skipped because this entry point
    is used by an already-running Local RAG manager.
    """
    package = Path(package_path).expanduser()
    target = Path(copilot_home).expanduser()
    if target.is_symlink():
        raise PackageError("install_target_symlink_forbidden")
    target.mkdir(parents=True, exist_ok=True)
    target = target.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="local-rag-package-import.") as temp:
        if package.is_file() and not package.is_symlink():
            package_root = Path(temp) / "package"
            package_root.mkdir()
            manifest = _extract_distribution_zip(
                package,
                package_root,
                expected_kind=_DISTRIBUTION_KIND,
            )
        else:
            package_root = _real_directory(package, "package")
            manifest = validate_package_tree(package_root)
        bootstrap = package_root / "bootstrap.py"
        if bootstrap.is_symlink() or not bootstrap.is_file():
            raise PackageError("package_bootstrap_missing")
        completed = subprocess.run(
            [
                sys.executable,
                str(bootstrap),
                str(target),
                "--skip-runtime-setup",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        if completed.returncode != 0:
            raise PackageError("package_import_failed")
    return {
        "status": "imported",
        "kind": manifest["kind"],
        "databases": [
            str(item["name"]) for item in manifest.get("dbs", [])
        ],
    }


def _extract_distribution_zip(
    archive: Path,
    root: Path,
    *,
    expected_kind: str | None,
) -> dict[str, Any]:
    try:
        package = zipfile.ZipFile(archive, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageError("package_archive_invalid") from exc
    with package:
        seen: set[str] = set()
        for info in package.infolist():
            name = info.filename.rstrip("/")
            if not name:
                continue
            relative = _safe_relative(name)
            normalized = relative.as_posix()
            if normalized in seen:
                raise PackageError("package_archive_duplicate_path")
            seen.add(normalized)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(unix_mode):
                raise PackageError("package_symlink_forbidden")
            destination = root.joinpath(*relative.parts)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with package.open(info, "r") as source, destination.open(
                "xb"
            ) as target:
                shutil.copyfileobj(source, target, _BUFFER_SIZE)
    return validate_package_tree(root, expected_kind=expected_kind)


def _distribution_entries(
    copilot_home: Path,
    *,
    db_names: Sequence[str] | None,
) -> tuple[list[_Entry], list[dict[str, Any]]]:
    rag_root = _real_directory(copilot_home / "rag", "rag_root")
    entries = _product_entries(copilot_home, admin=False)
    database_entries, databases = _database_entries(
        rag_root / "dbs",
        db_names=db_names,
        distribution=True,
    )
    entries.extend(database_entries)
    entries.extend(_package_installer_entries())
    entries.append(_Entry(None, "bootstrap.py", mode="bootstrap"))
    return _dedupe_entries(entries), databases


def _admin_entries(
    copilot_home: Path,
    *,
    db_names: Sequence[str] | None,
) -> tuple[list[_Entry], list[dict[str, Any]]]:
    entries = _product_entries(copilot_home, admin=True)
    rag_root = _real_directory(copilot_home / "rag", "rag_root")
    database_entries, databases = _database_entries(
        rag_root / "dbs",
        db_names=db_names,
        distribution=False,
    )
    entries = [
        entry
        for entry in entries
        if not entry.destination.startswith(".copilot/rag/dbs/")
    ]
    entries.extend(database_entries)
    entries.append(_Entry(None, "bootstrap.py", mode="bootstrap"))
    return _dedupe_entries(entries), databases


def _package_installer_entries() -> list[_Entry]:
    return [
        _Entry(None, "install.sh", mode="install_sh"),
        _Entry(None, "install.ps1", mode="install_ps1"),
    ]


def _product_entries(
    copilot_home: Path,
    *,
    admin: bool,
) -> list[_Entry]:
    """Collect product files by denylist, outside security-sensitive DBs.

    Product runtime grows frequently. New runtime modules and documentation
    therefore ship automatically. Machine-local configuration and database
    contents remain on their separate, restrictive package contracts.
    """

    rag_root = _real_directory(copilot_home / "rag", "rag_root")
    entries: list[_Entry] = []
    required = [
        "README.md",
        "VERSION",
        "list_dbs.py",
        "search.py",
        "setup.py",
        "query/list_dbs.py",
        "query/reference_contract.py",
        "query/result_detail.py",
        "query/search.py",
        "query/setup.py",
        "gen_db/software_rag_tool/pyproject.toml",
        "gen_db/software_rag_tool/software_rag_tool/__init__.py",
    ]
    if admin:
        required.extend(
            [
                "manage.py",
                "gen_db/add_data.py",
                "source_manager/__init__.py",
            ]
        )
    for relative in required:
        _add_file(
            entries,
            rag_root.joinpath(*PurePosixPath(relative).parts),
            f".copilot/rag/{relative}",
            required=True,
            source_root=rag_root,
        )
    _add_tree(
        entries,
        rag_root,
        ".copilot/rag",
        include=lambda path: _product_payload_file(
            path,
            rag_root=rag_root,
            admin=admin,
        ),
        descend=lambda path: _product_payload_directory(
            path,
            rag_root=rag_root,
            admin=admin,
        ),
    )
    instructions_root = _real_directory(
        copilot_home / "instructions",
        "instructions_root",
    )
    _add_file(
        entries,
        instructions_root / "rag.instructions.md",
        ".copilot/instructions/rag.instructions.md",
        required=True,
        source_root=instructions_root,
    )
    for skill_name in _PROJECT_SKILLS:
        skill_root = _real_directory(
            copilot_home / "skills" / skill_name,
            f"{skill_name}_skill_root",
        )
        _add_file(
            entries,
            skill_root / "SKILL.md",
            f".copilot/skills/{skill_name}/SKILL.md",
            required=True,
            source_root=skill_root,
        )
        _add_tree(
            entries,
            skill_root,
            f".copilot/skills/{skill_name}",
            include=_ordinary_payload_file,
        )
    return _dedupe_entries(entries)


def _database_entries(
    dbs_root: Path,
    *,
    db_names: Sequence[str] | None,
    distribution: bool,
) -> tuple[list[_Entry], list[dict[str, Any]]]:
    if not dbs_root.exists():
        if db_names:
            raise PackageError("database_missing")
        return [], []
    root = _real_directory(dbs_root, "dbs_root")
    names = _selected_database_names(root, db_names)
    entries: list[_Entry] = []
    databases: list[dict[str, Any]] = []
    for name in names:
        db_root = _safe_database_root(root, name)
        content_snapshot_at, content_snapshot_reason = (
            _database_content_snapshot(db_root)
        )
        databases.append(
            {
                "name": name,
                "content_snapshot_at": (
                    content_snapshot_at or None
                ),
                "content_snapshot_reason": content_snapshot_reason,
            }
        )
        prefix = f".copilot/rag/dbs/{name}"
        for filename in sorted(_DB_SEARCH_FILES):
            source = db_root / filename
            if filename == "catalog.sqlite":
                mode = "sqlite"
            elif filename == "source-links.json":
                mode = "source_links"
            else:
                mode = "copy"
            _add_file(
                entries,
                source,
                f"{prefix}/{filename}",
                required=filename in {
                    "VERSION.json",
                    "catalog.sqlite",
                    "db.json",
                },
                mode=mode,
                database_root=db_root,
                source_root=db_root,
            )
        if distribution:
            entries.append(
                _Entry(
                    None,
                    f"{prefix}/{_RAG_WRAPPER_NAME}",
                    mode="distribution_wrapper",
                    database_root=db_root,
                    content_snapshot_at=content_snapshot_at or None,
                )
            )
        else:
            _add_file(
                entries,
                db_root / _RAG_WRAPPER_NAME,
                f"{prefix}/{_RAG_WRAPPER_NAME}",
                required=False,
                database_root=db_root,
                source_root=db_root,
            )
        _add_tree(
            entries,
            db_root / "index",
            f"{prefix}/index",
            include=_ordinary_payload_file,
            database_root=db_root,
        )
        if distribution:
            continue
        for directory in sorted(_DB_ADMIN_DIRECTORIES):
            _add_tree(
                entries,
                db_root / directory,
                f"{prefix}/{directory}",
                include=_admin_payload_file,
                mode_for=lambda path, directory=directory: (
                    _admin_state_mode(
                        path,
                        db_root=db_root,
                        top_level=directory,
                    )
                ),
                database_root=db_root,
            )
    return _dedupe_entries(entries), databases


def _database_content_snapshot(db_root: Path) -> tuple[str, str]:
    path = db_root / _RAG_WRAPPER_NAME
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 65_536
        ):
            return "", "unknown"
    except OSError:
        return "", "unknown"
    wrapper = _read_optional_json(path)
    if wrapper.get("schema_version") == _RAG_WRAPPER_SCHEMA:
        value = _normalized_timestamp(wrapper.get("content_snapshot_at"))
        if value is not None:
            return value, "rag_wrapper"
    return "", "unknown"


def _normalized_timestamp(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stage_package(
    stage: Path,
    entries: Sequence[_Entry],
    *,
    kind: str,
    databases: Sequence[dict[str, Any]],
    created: str,
    tool_version: str,
) -> dict[str, Any]:
    stage.mkdir(parents=True, exist_ok=True)
    observations: list[tuple[_Entry, str]] = []
    records: list[dict[str, Any]] = []
    for entry in entries:
        destination = stage.joinpath(*_safe_relative(entry.destination).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if entry.mode == "bootstrap":
            _atomic_bytes(destination, _BOOTSTRAP_TEXT.encode("utf-8"))
            source_fingerprint = "generated"
        elif entry.mode == "install_sh":
            _atomic_bytes(destination, INSTALL_SH_TEXT.encode("utf-8"))
            source_fingerprint = "generated"
        elif entry.mode == "install_ps1":
            _atomic_bytes(destination, INSTALL_PS1_TEXT.encode("utf-8"))
            source_fingerprint = "generated"
        elif entry.mode == "distribution_wrapper":
            if entry.database_root is None:
                raise PackageError("package_database_root_missing")
            _atomic_bytes(
                destination,
                _distribution_wrapper_bytes(
                    entry.content_snapshot_at,
                    packaged_at=created,
                ),
            )
            source_fingerprint = "generated"
        elif entry.source is None:
            raise PackageError("package_source_missing")
        elif entry.mode == "sqlite":
            _backup_sqlite(
                entry.source,
                destination,
                source_root=entry.source_root,
            )
            source_fingerprint = _sha256(destination)
        else:
            source_fingerprint, raw = _stable_read(
                entry.source,
                source_root=entry.source_root,
            )
            if entry.mode == "admin_json":
                raw = _portable_admin_json(
                    raw,
                    source=entry.source,
                    database_root=entry.database_root,
                )
            elif entry.mode == "admin_jsonl":
                raw = _portable_admin_jsonl(
                    raw,
                    source=entry.source,
                    database_root=entry.database_root,
                )
            elif entry.mode == "source_links":
                _validate_source_links_payload(raw)
            _atomic_bytes(destination, raw)
            if entry.mode == "copy" and _sha256(destination) != source_fingerprint:
                raise PackageError("package_source_changed")
        observations.append((entry, source_fingerprint))
        records.append(
            {
                "path": entry.destination,
                "size": destination.stat().st_size,
                "sha256": _sha256(destination),
            }
        )
    _verify_source_fingerprints(observations)
    records.sort(key=lambda item: str(item["path"]))
    database_records = [dict(value) for value in databases]
    manifest = {
        "schema": PACKAGE_SCHEMA,
        "kind": kind,
        "created": created,
        "tool": {
            "name": PACKAGE_TOOL_NAME,
            "version": tool_version,
        },
        "dbs": database_records,
        "files": records,
        "total": {
            "files": len(records),
            "bytes": sum(int(value["size"]) for value in records),
        },
    }
    _atomic_bytes(
        stage / MANIFEST_NAME,
        (
            json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8"),
    )
    return manifest


def _verify_source_fingerprints(
    observations: Sequence[tuple[_Entry, str]],
) -> None:
    with tempfile.TemporaryDirectory(prefix="local-rag-sqlite-check.") as temp:
        for index, (entry, before) in enumerate(observations):
            if entry.mode in {
                "bootstrap",
                "distribution_wrapper",
                "install_ps1",
                "install_sh",
            }:
                continue
            if entry.source is None:
                raise PackageError("package_source_missing")
            if entry.mode == "sqlite":
                snapshot = Path(temp) / f"{index}.sqlite"
                _backup_sqlite(
                    entry.source,
                    snapshot,
                    source_root=entry.source_root,
                )
                after = _sha256(snapshot)
            else:
                after, _raw = _stable_read(
                    entry.source,
                    source_root=entry.source_root,
                )
            if after != before:
                raise PackageError("package_source_changed")


def _portable_admin_json(
    raw: bytes,
    *,
    source: Path,
    database_root: Path | None,
) -> bytes:
    del source
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("admin_state_json_invalid") from exc
    if database_root is not None:
        payload = _portable_add_state(payload, database_root)
    payload = _redact_temporary_paths(payload)
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _portable_admin_jsonl(
    raw: bytes,
    *,
    source: Path,
    database_root: Path | None,
) -> bytes:
    del source
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise PackageError("admin_state_json_invalid") from exc
    output: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PackageError("admin_state_json_invalid") from exc
        if database_root is not None:
            payload = _portable_add_state(payload, database_root)
        payload = _redact_temporary_paths(payload)
        output.append(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return (("\n".join(output) + "\n") if output else "").encode("utf-8")


def _portable_add_state(
    value: Any,
    database_root: Path,
    *,
    inherited_source_id: str = "",
) -> Any:
    if isinstance(value, list):
        return [
            _portable_add_state(
                item,
                database_root,
                inherited_source_id=inherited_source_id,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    source_id = str(value.get("source_id") or inherited_source_id)
    output = {
        str(key): _portable_add_state(
            item,
            database_root,
            inherited_source_id=source_id,
        )
        for key, item in value.items()
    }
    identities = _source_work_identities(database_root)
    sharepoint_identities = _sharepoint_external_identities(database_root)
    for key in ("root", "scan_root", "resolved_root"):
        path = str(output.get(key) or "")
        if not _looks_absolute(path):
            continue
        sharepoint_matches = _matching_sharepoint_identities(
            path,
            sharepoint_identities,
            source_id=source_id,
        )
        if len(sharepoint_matches) > 1:
            raise PackageError("portable_add_state_ambiguous")
        if len(sharepoint_matches) == 1:
            local_source_key, suffix = sharepoint_matches[0]
            output[key] = {
                _PORTABLE_SHAREPOINT_SOURCE: local_source_key,
                _PORTABLE_SHAREPOINT_SUFFIX: suffix,
            }
            continue
        matches = _matching_work_identities(
            path,
            identities,
            source_id=source_id,
        )
        if len(matches) > 1:
            raise PackageError("portable_add_state_ambiguous")
        if len(matches) == 1:
            relative_root, suffix = matches[0]
            relative = (
                f"{relative_root}/{suffix}" if suffix else relative_root
            )
            output[key] = {_PORTABLE_DB_PATH: relative}
            continue
        if source_id and any(
            identity == source_id
            for identity, _relative, _root in identities
        ):
            raise PackageError("portable_add_state_ambiguous")
        try:
            relative = (
                Path(path)
                .expanduser()
                .resolve(strict=False)
                .relative_to(database_root.resolve(strict=True))
                .as_posix()
            )
        except (OSError, ValueError):
            continue
        output[key] = {_PORTABLE_DB_PATH: relative}
    return output


def _source_work_identities(
    database_root: Path,
) -> list[tuple[str, str, Path]]:
    identities: list[tuple[str, str, Path]] = []
    sources = database_root / "sources"
    if not sources.is_dir() or sources.is_symlink():
        return identities
    for source_json in sorted(sources.glob("*/source.json")):
        if source_json.is_symlink() or not source_json.is_file():
            continue
        payload = _read_optional_json(source_json)
        source_id = str(payload.get("source_id") or "")
        ingest = payload.get("ingest")
        relative = _safe_relative_string(
            (
                ingest.get("work_directory")
                if isinstance(ingest, dict)
                else None
            )
            or payload.get("work_path")
        )
        if not source_id or not relative:
            continue
        actual = database_root.joinpath(*PurePosixPath(relative).parts)
        try:
            actual = actual.resolve(strict=True)
        except OSError:
            continue
        identities.append((source_id, relative, actual))
    return identities


def _sharepoint_external_identities(
    database_root: Path,
) -> list[tuple[str, str, Path]]:
    identities: list[tuple[str, str, Path]] = []
    sources = database_root / "sources"
    if not sources.is_dir() or sources.is_symlink():
        return identities
    for source_json in sorted(sources.glob("*/source.json")):
        if source_json.is_symlink() or not source_json.is_file():
            continue
        payload = _read_optional_json(source_json)
        if str(payload.get("source_type") or "") != "sharepoint":
            continue
        source_id = str(payload.get("source_id") or "")
        local_source_key = str(payload.get("local_source_key") or source_json.parent.name)
        fetch = payload.get("fetch")
        if not source_id or not isinstance(fetch, dict):
            continue
        environment_name = str(fetch.get("root_env") or "")
        environment_root = os.environ.get(environment_name)
        if not environment_name or not environment_root:
            continue
        root = Path(environment_root).expanduser()
        if not root.is_absolute():
            continue
        relative = _safe_relative_string(fetch.get("relative_path"))
        if relative:
            root = root.joinpath(*PurePosixPath(relative).parts)
        try:
            root = root.resolve(strict=True)
        except OSError:
            continue
        identities.append((source_id, local_source_key, root))
    return identities


def _matching_work_identities(
    value: str,
    identities: Sequence[tuple[str, str, Path]],
    *,
    source_id: str,
) -> list[tuple[str, str]]:
    try:
        candidate = Path(value).expanduser().resolve(strict=False)
    except OSError:
        return []
    output: list[tuple[str, str]] = []
    for identity, relative, root in identities:
        if source_id and identity != source_id:
            continue
        try:
            suffix = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        output.append((relative, "" if suffix == "." else suffix))
    return output


def _matching_sharepoint_identities(
    value: str,
    identities: Sequence[tuple[str, str, Path]],
    *,
    source_id: str,
) -> list[tuple[str, str]]:
    try:
        candidate = Path(value).expanduser().resolve(strict=False)
    except OSError:
        return []
    output: list[tuple[str, str]] = []
    for identity, local_source_key, root in identities:
        if source_id and identity != source_id:
            continue
        try:
            suffix = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        output.append(
            (local_source_key, "" if suffix == "." else suffix)
        )
    return output


def _redact_temporary_paths(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if (
                name == "input_path"
                and isinstance(item, str)
                and _looks_absolute(item)
            ):
                output[name] = "<INPUT_RESELECT_REQUIRED>"
            else:
                output[name] = _redact_temporary_paths(item)
        return output
    if isinstance(value, list):
        return [_redact_temporary_paths(item) for item in value]
    if not isinstance(value, str) or not _looks_absolute(value):
        return value
    temporary = Path(tempfile.gettempdir()).resolve()
    try:
        path = Path(value).expanduser().resolve(strict=False)
        suffix = path.relative_to(temporary).as_posix()
    except (OSError, ValueError):
        return "<ABSOLUTE_PATH_REDACTED>"
    return "<TEMP_ROOT>" + (f"/{suffix}" if suffix != "." else "")


def _distribution_wrapper_bytes(
    content_snapshot_at: str | None,
    *,
    packaged_at: str,
) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": _RAG_WRAPPER_SCHEMA,
                "content_snapshot_at": content_snapshot_at or None,
                "packaged_at": packaged_at,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _validate_source_links_payload(raw: bytes) -> None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("source_links_invalid") from exc
    if not isinstance(payload, dict):
        raise PackageError("source_links_invalid")

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if (
                    normalized in _SENSITIVE_QUERY_NAMES
                    or any(
                        marker in normalized
                        for marker in (
                            "access_key",
                            "credential",
                            "password",
                            "private_key",
                            "proxy",
                            "secret",
                            "token",
                        )
                    )
                ):
                    raise PackageError("credential_configuration_detected")
                inspect(item)
            return
        if isinstance(value, list):
            for item in value:
                inspect(item)
            return
        if isinstance(value, str) and _url_has_credentials(value):
            raise PackageError("credential_configuration_detected")

    inspect(payload)
    tool_root = (
        Path(__file__).resolve().parents[1]
        / "gen_db"
        / "software_rag_tool"
    )
    sys.path.insert(0, str(tool_root))
    try:
        from software_rag_tool.source_links import (
            SCHEMA_VERSION,
            validate_source_links,
        )

        if payload.get("schema_version") != SCHEMA_VERSION:
            raise PackageError("source_links_not_current")
        validate_source_links(
            payload,
            allow_unmatched_sources=True,
        )
    except PackageError:
        raise
    except (ImportError, OSError, ValueError) as exc:
        raise PackageError("source_links_invalid") from exc
    finally:
        try:
            sys.path.remove(str(tool_root))
        except ValueError:
            pass


def _backup_sqlite(
    source: Path,
    destination: Path,
    *,
    source_root: Path | None = None,
) -> None:
    _assert_regular_source(source, source_root=source_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    uri = source.resolve().as_uri() + "?mode=ro"
    reader: sqlite3.Connection | None = None
    writer: sqlite3.Connection | None = None
    try:
        reader = sqlite3.connect(uri, uri=True, timeout=5)
        writer = sqlite3.connect(destination)
        reader.backup(writer)
        row = writer.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).casefold() != "ok":
            raise PackageError("catalog_backup_invalid")
        writer.commit()
    except PackageError:
        raise
    except (OSError, sqlite3.Error) as exc:
        try:
            destination.unlink()
        except OSError:
            pass
        raise PackageError("catalog_backup_failed") from exc
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
    _assert_regular_source(source, source_root=source_root)
    _fsync_file(destination)


def _write_zip(root: Path, output: Path) -> None:
    with zipfile.ZipFile(
        output,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as package:
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(root).as_posix(),
        ):
            if path.is_symlink():
                raise PackageError("package_symlink_forbidden")
            relative = path.relative_to(root).as_posix()
            _safe_relative(relative)
            package.write(path, relative)


def _validate_manifest_shape(
    manifest: Any,
    *,
    expected_kind: str | None,
) -> None:
    if not isinstance(manifest, dict):
        raise PackageError("package_manifest_invalid")
    if set(manifest) != {
        "schema",
        "kind",
        "created",
        "tool",
        "dbs",
        "files",
        "total",
    }:
        raise PackageError("package_manifest_invalid")
    if manifest.get("schema") != PACKAGE_SCHEMA:
        raise PackageError("package_manifest_schema_invalid")
    kind = str(manifest.get("kind") or "")
    if kind not in {_DISTRIBUTION_KIND, _ADMIN_KIND}:
        raise PackageError("package_manifest_kind_invalid")
    if expected_kind is not None and kind != expected_kind:
        raise PackageError("package_manifest_kind_mismatch")
    if not isinstance(manifest.get("tool"), dict):
        raise PackageError("package_manifest_tool_invalid")
    tool = manifest["tool"]
    if (
        set(tool) != {"name", "version"}
        or tool.get("name") != PACKAGE_TOOL_NAME
        or not isinstance(tool.get("version"), str)
    ):
        raise PackageError("package_manifest_tool_invalid")
    if not isinstance(manifest.get("created"), str):
        raise PackageError("package_manifest_created_invalid")
    databases = manifest.get("dbs")
    if not isinstance(databases, list):
        raise PackageError("package_manifest_dbs_invalid")
    seen_databases: set[str] = set()
    for database in databases:
        if (
            not isinstance(database, dict)
            or set(database)
            != {
                "name",
                "content_snapshot_at",
                "content_snapshot_reason",
            }
            or not _DB_NAME.fullmatch(str(database.get("name") or ""))
            or (
                database.get("content_snapshot_at") is not None
                and (
                    not isinstance(
                        database.get("content_snapshot_at"),
                        str,
                    )
                    or _normalized_timestamp(
                        database.get("content_snapshot_at")
                    )
                    is None
                )
            )
            or database.get("content_snapshot_reason")
            not in {"rag_wrapper", "unknown"}
            or database["name"] in seen_databases
        ):
            raise PackageError("package_manifest_dbs_invalid")
        seen_databases.add(database["name"])
    files = manifest.get("files")
    if not isinstance(files, list):
        raise PackageError("package_manifest_files_invalid")
    file_databases: set[str] = set()
    for record in files:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "size", "sha256"}
            or not isinstance(record["size"], int)
            or record["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"]))
        ):
            raise PackageError("package_manifest_file_invalid")
        relative = _safe_relative(str(record["path"]))
        if (
            len(relative.parts) >= 3
            and relative.parts[:3] == (".copilot", "rag", "dbs")
        ):
            if (
                len(relative.parts) < 5
                or relative.parts[3] not in seen_databases
            ):
                raise PackageError("package_manifest_dbs_invalid")
            file_databases.add(relative.parts[3])
    if file_databases != seen_databases:
        raise PackageError("package_manifest_dbs_invalid")
    total = manifest.get("total")
    if (
        not isinstance(total, dict)
        or set(total) != {"files", "bytes"}
        or not all(
            isinstance(total.get(key), int) and total[key] >= 0
            for key in ("files", "bytes")
        )
    ):
        raise PackageError("package_manifest_total_invalid")


def _selected_database_names(
    root: Path,
    values: Sequence[str] | None,
) -> list[str]:
    if values:
        names = sorted(set(str(value) for value in values))
    else:
        names = sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and _DB_NAME.fullmatch(path.name)
            and (path / "db.json").is_file()
        )
    for name in names:
        if not _DB_NAME.fullmatch(name):
            raise PackageError("database_name_invalid")
    return names


def _safe_database_root(root: Path, name: str) -> Path:
    candidate = root / name
    try:
        metadata = os.lstat(candidate)
    except OSError as exc:
        raise PackageError("database_missing") from exc
    if _is_link_or_reparse(candidate, metadata):
        raise PackageError("database_symlink_forbidden")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PackageError("database_missing") from exc
    if not resolved.is_dir() or resolved.parent != root:
        raise PackageError("database_path_invalid")
    return resolved


def _add_file(
    entries: list[_Entry],
    source: Path,
    destination: str,
    *,
    required: bool,
    mode: str = "copy",
    database_root: Path | None = None,
    source_root: Path | None = None,
) -> None:
    if not source.exists():
        if required:
            raise PackageError("required_package_source_missing")
        return
    _assert_regular_source(source, source_root=source_root)
    if _is_secret_path(source) or _is_transient_path(Path(source.name)):
        raise PackageError("forbidden_package_source")
    _reject_private_key_material(source)
    entries.append(
        _Entry(
            source,
            _safe_relative(destination).as_posix(),
            mode,
            database_root,
            source_root=source_root,
        )
    )


def _add_tree(
    entries: list[_Entry],
    source_root: Path,
    destination_root: str,
    *,
    include: Any,
    descend: Any | None = None,
    mode_for: Any | None = None,
    database_root: Path | None = None,
) -> None:
    if not source_root.exists():
        return
    try:
        root_metadata = os.lstat(source_root)
    except OSError as exc:
        raise PackageError("package_source_unreadable") from exc
    if (
        _is_link_or_reparse(source_root, root_metadata)
        or not stat.S_ISDIR(root_metadata.st_mode)
    ):
        raise PackageError("package_source_tree_invalid")
    try:
        source_root = source_root.resolve(strict=True)
    except OSError as exc:
        raise PackageError("package_source_unreadable") from exc

    def visit(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda value: value.name)
        except OSError as exc:
            raise PackageError("package_source_unreadable") from exc
        for path in children:
            try:
                metadata = os.lstat(path)
            except OSError as exc:
                raise PackageError("package_source_unreadable") from exc
            if _is_link_or_reparse(path, metadata):
                raise PackageError("package_symlink_forbidden")
            relative = path.relative_to(source_root)
            if stat.S_ISDIR(metadata.st_mode):
                if _is_transient_path(relative):
                    continue
                if descend is not None and not descend(path):
                    continue
                visit(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PackageError("package_special_file_forbidden")
            if not include(path):
                continue
            if _is_secret_path(path):
                raise PackageError("forbidden_package_source")
            if _is_transient_path(relative):
                continue
            _reject_private_key_material(path)
            if _git_security_file(path):
                _validate_git_configuration(path)
            destination = (
                PurePosixPath(destination_root)
                / PurePosixPath(relative.as_posix())
            ).as_posix()
            entries.append(
                _Entry(
                    path,
                    _safe_relative(destination).as_posix(),
                    mode_for(path) if mode_for else "copy",
                    database_root,
                    source_root=source_root,
                )
            )
    visit(source_root)


def _product_payload_directory(
    path: Path,
    *,
    rag_root: Path,
    admin: bool,
) -> bool:
    """Return whether a product directory may be traversed."""

    relative = path.relative_to(rag_root)
    lowered = tuple(part.casefold() for part in relative.parts)
    if not lowered:
        return True
    if lowered[0] == "dbs":
        return False
    allowed_roots = set(_PRODUCT_ROOT_DIRECTORIES)
    if admin:
        allowed_roots.add("source_manager")
    if lowered[0] not in allowed_roots:
        return False
    if any(part in _PRODUCT_TEST_DIRECTORIES for part in lowered):
        return False
    if any(part in {".git", ".github"} for part in lowered):
        return False
    if (
        not admin
        and lowered[:3] == ("gen_db", "software_rag_tool", "scripts")
    ):
        return False
    return True


def _product_payload_file(
    path: Path,
    *,
    rag_root: Path,
    admin: bool,
) -> bool:
    """Select product payload by explicit exclusions, not runtime allowlists."""

    relative = path.relative_to(rag_root)
    lowered = tuple(part.casefold() for part in relative.parts)
    if not lowered or lowered[0] == "dbs":
        return False
    if any(part in _PRODUCT_TEST_DIRECTORIES for part in lowered[:-1]):
        return False
    name = lowered[-1]
    if (
        name == "conftest.py"
        or name.startswith("test_")
        or name.endswith("_test.py")
    ):
        return False
    if lowered[0] == "config":
        return (
            len(lowered) == 2
            and name in _PORTABLE_CONFIG_EXAMPLES
        )
    if len(lowered) == 1:
        if not admin and name in _DISTRIBUTION_ADMIN_ROOT_FILES:
            return False
        return (
            path.suffix.casefold() in {".md", ".py"}
            or name == "version"
        )
    if not admin:
        if (
            len(lowered) == 2
            and lowered[0] == "gen_db"
            and path.suffix.casefold() == ".py"
        ):
            return False
        if (
            lowered[:3] == ("gen_db", "software_rag_tool", "scripts")
        ):
            return False
    return True


def _ordinary_payload_file(path: Path) -> bool:
    return path.is_file()


def _admin_payload_file(path: Path) -> bool:
    return path.is_file()


def _admin_state_mode(
    path: Path,
    *,
    db_root: Path,
    top_level: str,
) -> str:
    """Transform only administrative state, never indexed/source content."""
    suffix = path.suffix.casefold()
    if top_level == "logs":
        if suffix == ".json":
            return "admin_json"
        if suffix == ".jsonl":
            return "admin_jsonl"
        return "copy"
    if top_level != "sources":
        return "copy"
    try:
        relative = path.relative_to(db_root / "sources")
    except ValueError:
        return "copy"
    if len(relative.parts) != 2:
        return "copy"
    if path.name in {"source.json", "state.json"}:
        return "admin_json"
    if path.name == "events.jsonl":
        return "admin_jsonl"
    return "copy"


def _git_security_file(path: Path) -> bool:
    lowered = [part.casefold() for part in path.parts]
    name = path.name.casefold()
    return (
        name in {".gitmodules", ".lfsconfig"}
        or (
            ".git" in lowered
            and name in {"config", "config.worktree"}
        )
        or (
            ".git" in lowered
            and "lfs" in lowered
            and name == "config"
        )
    )


def _validate_git_configuration(path: Path) -> None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PackageError("credential_configuration_detected") from exc
    _validate_git_configuration_payload(raw)


def _validate_git_configuration_payload(raw: bytes) -> None:
    if len(raw) > _MAX_TEXT_CONFIG_BYTES:
        raise PackageError("credential_configuration_detected")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise PackageError("credential_configuration_detected") from exc
    if _SENSITIVE_ASSIGNMENT.search(text):
        raise PackageError("credential_configuration_detected")
    parser = configparser.RawConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        # .gitmodules and .lfsconfig are INI. Refuse an unreviewable file.
        raise PackageError("credential_configuration_detected")
    for section in parser.sections():
        for key, value in parser.items(section, raw=True):
            normalized = key.casefold().replace("-", "_")
            if (
                any(
                    marker in normalized
                    for marker in (
                        "credential",
                        "extraheader",
                        "password",
                        "secret",
                        "token",
                    )
                )
                or _url_has_credentials(value)
            ):
                raise PackageError("credential_configuration_detected")


def _url_has_credentials(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    split = urlsplit(text)
    if split.username is not None or split.password is not None:
        return True
    for key, _value in parse_qsl(split.query, keep_blank_values=True):
        normalized = key.casefold().replace("-", "_")
        if normalized in _SENSITIVE_QUERY_NAMES or any(
            part in normalized
            for part in ("credential", "password", "secret", "token")
        ):
            return True
    return bool(_SENSITIVE_ASSIGNMENT.search(text))


def _dedupe_entries(entries: Sequence[_Entry]) -> list[_Entry]:
    values: dict[str, _Entry] = {}
    for entry in entries:
        existing = values.get(entry.destination)
        if existing is not None and existing != entry:
            raise PackageError("package_destination_collision")
        values[entry.destination] = entry
    return [values[key] for key in sorted(values)]


def _stable_read(
    path: Path,
    *,
    source_root: Path | None = None,
) -> tuple[str, bytes]:
    before = _assert_regular_source(path, source_root=source_root)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PackageError("package_source_unreadable") from exc
    after = _assert_regular_source(path, source_root=source_root)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(raw) != after.st_size
    ):
        raise PackageError("package_source_changed")
    _reject_private_key_payload(raw)
    if _git_security_file(path):
        _validate_git_configuration_payload(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return digest, raw


def _assert_regular_source(
    path: Path,
    *,
    source_root: Path | None = None,
) -> os.stat_result:
    if source_root is not None:
        root = Path(source_root)
        try:
            relative = Path(path).relative_to(root)
        except ValueError as exc:
            raise PackageError("package_source_outside_root") from exc
        try:
            root_metadata = os.lstat(root)
        except OSError as exc:
            raise PackageError("package_source_unreadable") from exc
        if (
            _is_link_or_reparse(root, root_metadata)
            or not stat.S_ISDIR(root_metadata.st_mode)
        ):
            raise PackageError("package_symlink_forbidden")
        current = root
        for component in relative.parts[:-1]:
            current = current / component
            try:
                parent_metadata = os.lstat(current)
            except OSError as exc:
                raise PackageError("package_source_unreadable") from exc
            if (
                _is_link_or_reparse(current, parent_metadata)
                or not stat.S_ISDIR(parent_metadata.st_mode)
            ):
                raise PackageError("package_symlink_forbidden")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PackageError("package_source_unreadable") from exc
    if _is_link_or_reparse(path, metadata):
        raise PackageError("package_symlink_forbidden")
    if not stat.S_ISREG(metadata.st_mode):
        raise PackageError("package_source_not_regular")
    return metadata


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )


def _is_secret_path(path: Path) -> bool:
    name = path.name.casefold()
    stem = path.stem.casefold()
    return (
        name in _SECRET_FILENAMES
        or (
            name.startswith(".env.")
            and name != ".env.example"
        )
        or path.suffix.casefold() in _SECRET_SUFFIXES
        or stem
        in {
            "access-key",
            "access_key",
            "credential",
            "credentials",
            "password",
            "passwd",
            "private-key",
            "private_key",
            "secret",
            "secrets",
            "token",
            "tokens",
        }
    )


def _reject_private_key_material(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(16 * 1024)
    except OSError as exc:
        raise PackageError("package_source_unreadable") from exc
    _reject_private_key_payload(prefix)


def _reject_private_key_payload(payload: bytes) -> None:
    prefix = payload[: 16 * 1024]
    if (
        b"-----BEGIN OPENSSH PRIVATE KEY-----" in prefix
        or re.search(
            rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
            prefix,
        )
    ):
        raise PackageError("forbidden_package_source")


def _is_transient_path(path: Path) -> bool:
    lowered = [part.casefold() for part in path.parts]
    name = path.name.casefold()
    return (
        any(part in _TRANSIENT_NAMES for part in lowered)
        or name.startswith("._")
        or name == ".ds_store"
        or name.endswith(_TRANSIENT_SUFFIXES)
    )


def _safe_relative(value: str) -> PurePosixPath:
    text = str(value or "")
    if (
        not text
        or "\\" in text
        or _WINDOWS_DRIVE.match(text)
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise PackageError("package_path_invalid")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackageError("package_path_invalid")
    return path


def _safe_relative_string(value: Any) -> str:
    text = str(value or "")
    try:
        return _safe_relative(text).as_posix()
    except PackageError:
        return ""


def _looks_absolute(value: str) -> bool:
    text = str(value or "").strip()
    return (
        text.startswith("/")
        or bool(_WINDOWS_DRIVE.match(text))
        or PureWindowsPath(text).is_absolute()
    )


def _read_optional_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _real_directory(path: Path, field: str) -> Path:
    candidate = Path(path).expanduser()
    try:
        metadata = os.lstat(candidate)
    except OSError as exc:
        raise PackageError(f"{field}_invalid") from exc
    if (
        _is_link_or_reparse(candidate, metadata)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise PackageError(f"{field}_invalid")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PackageError(f"{field}_invalid") from exc
    return resolved


def _new_output_path(path: Path, *, directory: bool) -> Path:
    output = Path(path).expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise PackageError("package_output_exists")
    if directory:
        return output
    if output.suffix.casefold() != ".zip":
        raise PackageError("distribution_output_must_be_zip")
    return output


def _created_at(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise PackageError("created_at_requires_timezone")
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _tool_version(rag_root: Path) -> str:
    try:
        return rag_root.joinpath("VERSION").read_text(
            encoding="utf-8"
        ).splitlines()[0].strip()
    except (OSError, IndexError):
        return "unknown"


def _atomic_bytes(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise PackageError("package_resume_path_invalid")
        expected = hashlib.sha256(payload).hexdigest()
        if path.stat().st_size == len(payload) and _sha256(path) == expected:
            return
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    # Windows rejects FlushFileBuffers/fsync for a read-only handle.  Open the
    # already-created file without truncation but with write access so the same
    # durability step works on every supported OS.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
