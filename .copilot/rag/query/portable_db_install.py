from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath

SCHEMA = "local-rag.windows-package.v2"


def remove_tree(path: Path, *, ignore_errors: bool = False) -> None:
    def clear_readonly(function: object, value: str, error_info: tuple[type[BaseException], BaseException, object]) -> None:
        error = error_info[1]
        if not isinstance(error, PermissionError):
            raise error
        os.chmod(value, stat.S_IWRITE)
        function(value)  # type: ignore[operator]

    try:
        shutil.rmtree(path, onerror=clear_readonly)
    except FileNotFoundError:
        return
    except Exception:
        if not ignore_errors:
            raise

def reject_reparse_ancestors(path: Path) -> None:
    current = path.absolute()
    while True:
        if current.exists() or current.is_symlink():
            metadata = current.lstat()
            if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"target path contains a link or reparse point: {current}")
        if current.parent == current:
            return
        current = current.parent

def records_fingerprint(records: list[dict[str, object]]) -> str:
    canonical = [{"path": str(item["path"]), "sha256": str(item["sha256"]), "size": int(item["size"])} for item in sorted(records, key=lambda value: str(value["path"]))]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

def file_records(root: Path, prefix: str) -> list[dict[str, object]]:
    root_metadata = root.lstat()
    if root.is_symlink() or getattr(root_metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"database link or reparse point is forbidden: {root}")
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"database link or reparse point is forbidden: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            records.append({"path": f"{prefix}/{relative}", "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return records

def validate_package(package_root: Path, manifest: dict[str, object]) -> list[dict[str, object]]:
    if manifest.get("schema") != SCHEMA:
        raise ValueError("unsupported package manifest")
    files = list(manifest.get("files") or [])
    paths = [str(item.get("path") or "") for item in files]
    if len(paths) != len(set(paths)) or len(paths) != len({value.casefold() for value in paths}):
        raise ValueError("duplicate or case-colliding manifest path")
    for value in paths:
        pure = PurePosixPath(value)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError("unsafe manifest path")
    actual = {path.relative_to(package_root).as_posix() for path in package_root.rglob("*") if path.is_file()}
    if actual != set(paths) | {"PACKAGE-MANIFEST.json"}:
        raise ValueError("package tree is not a closed set")
    declared = list(manifest.get("databases") or [])
    names = [str(item.get("name") or "") for item in declared]
    if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
        raise ValueError("duplicate database declaration")
    for database in declared:
        name = str(database.get("name") or "")
        prefix = f".copilot/rag/dbs/{name}"
        if database.get("prefix") != prefix or database.get("coverage") != "closed-set":
            raise ValueError("database manifest coverage is invalid")
        subset = [item for item in files if str(item["path"]).startswith(prefix + "/")]
        if not subset or int(database.get("file_count") or -1) != len(subset):
            raise ValueError("database manifest file count is invalid")
        if int(database.get("bytes") or -1) != sum(int(item["size"]) for item in subset):
            raise ValueError("database manifest byte count is invalid")
        if database.get("fingerprint") != records_fingerprint(subset):
            raise ValueError("database manifest fingerprint is invalid")
    for path in (value for value in paths if value.startswith(".copilot/rag/dbs/")):
        if not any(path.startswith(f".copilot/rag/dbs/{name}/") for name in names):
            raise ValueError("undeclared database payload")
    return declared

def preflight(package_root: Path, target_root: Path, *, replace_existing: bool = False) -> dict[str, object]:
    package_root = package_root.resolve(strict=True)
    manifest = json.loads((package_root / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8"))
    databases = validate_package(package_root, manifest)
    reject_reparse_ancestors(target_root)
    if target_root.exists():
        metadata = target_root.lstat()
        if not target_root.is_dir() or target_root.is_symlink() or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("target database root is unsafe")
    statuses: dict[str, str] = {}
    for database in databases:
        name = str(database["name"])
        target = target_root / name
        prefix = str(database["prefix"])
        if not target.exists():
            statuses[name] = "install"
        elif records_fingerprint(file_records(target, prefix)) == database["fingerprint"]:
            statuses[name] = "already_installed"
        elif replace_existing:
            statuses[name] = "replace"
        else:
            raise ValueError(f"database differs and replacement was not approved: {name}")
    return {"status": "ready", "databases": statuses}

def install_databases(package_root: Path, target_root: Path, *, replace_existing: bool = False) -> dict[str, object]:
    package_root = package_root.resolve(strict=True)
    manifest = json.loads((package_root / "PACKAGE-MANIFEST.json").read_text(encoding="utf-8"))
    databases = validate_package(package_root, manifest)
    reject_reparse_ancestors(target_root)
    if target_root.exists():
        metadata = target_root.lstat()
        if not target_root.is_dir() or target_root.is_symlink() or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("target database root is unsafe")
    target_root.mkdir(parents=True, exist_ok=True)
    transaction = uuid.uuid4().hex
    plans: list[tuple[str, Path, Path, Path | None]] = []
    statuses: dict[str, str] = {}
    try:
        for database in databases:
            name = str(database["name"])
            source = package_root / ".copilot" / "rag" / "dbs" / name
            target = target_root / name
            prefix = str(database["prefix"])
            if target.exists():
                if records_fingerprint(file_records(target, prefix)) == database["fingerprint"]:
                    statuses[name] = "already_installed"
                    continue
                if not replace_existing:
                    raise ValueError(f"database differs and replacement was not approved: {name}")
            stage = target_root / f".{name}.stage-{transaction}"
            backup = target_root / f".{name}.backup-{transaction}" if target.exists() else None
            plans.append((name, target, stage, backup))
            shutil.copytree(source, stage)
            if records_fingerprint(file_records(stage, prefix)) != database["fingerprint"]:
                raise ValueError(f"staged database fingerprint mismatch: {name}")
        published: list[tuple[str, Path, Path | None]] = []
        try:
            for name, target, stage, backup in plans:
                if backup is not None:
                    os.replace(target, backup)
                try:
                    os.replace(stage, target)
                except Exception:
                    if backup is not None and backup.exists():
                        os.replace(backup, target)
                    raise
                published.append((name, target, backup))
                statuses[name] = "replaced" if backup is not None else "installed"
        except Exception:
            for _name, target, backup in reversed(published):
                remove_tree(target, ignore_errors=True)
                if backup is not None and backup.exists():
                    os.replace(backup, target)
            raise
        for _name, _target, backup in published:
            if backup is not None:
                remove_tree(backup)
    finally:
        for _name, _target, stage, _backup in plans:
            remove_tree(stage, ignore_errors=True)
    return {"status": "ok", "databases": statuses}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    arguments = parser.parse_args()
    action = preflight if arguments.preflight else install_databases
    print(json.dumps(action(arguments.package_root, arguments.target_root, replace_existing=arguments.replace_existing), sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
