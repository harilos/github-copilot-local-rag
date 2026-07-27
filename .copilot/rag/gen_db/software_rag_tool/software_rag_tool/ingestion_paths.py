from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


WHOLE_ROOT_SCAN = "."


@dataclass(frozen=True)
class StoredFilePath:
    resolved_path: Path
    relative_to_root: str
    stored_path: str


@dataclass(frozen=True)
class IngestionScope:
    logical_root: Path
    resolved_root: Path
    root_display_name: str
    scan_subdir: str
    scan_root: Path
    stored_path_prefix: str
    include_root_name_in_path: bool = True

    def file(self, path: Path) -> StoredFilePath:
        resolved_path = path.expanduser().resolve(strict=True)
        try:
            relative = resolved_path.relative_to(self.resolved_root)
        except ValueError as exc:
            raise ValueError(
                f"input file is outside the logical root: {path}"
            ) from exc
        relative_posix = PurePosixPath(*relative.parts).as_posix()
        stored_path = PurePosixPath(
            self.root_display_name,
            *relative.parts,
        ).as_posix()
        if (
            not relative_posix
            or relative_posix == "."
            or "\\" in stored_path
            or PureWindowsPath(stored_path).drive
            or PurePosixPath(stored_path).is_absolute()
        ):
            raise ValueError(f"cannot create a portable stored path: {path}")
        return StoredFilePath(
            resolved_path=resolved_path,
            relative_to_root=relative_posix,
            stored_path=stored_path,
        )

    def contains_stored_path(self, stored_path: str) -> bool:
        candidate = PurePosixPath(stored_path)
        prefix = PurePosixPath(self.stored_path_prefix.rstrip("/"))
        try:
            candidate.relative_to(prefix)
        except ValueError:
            return False
        return True

    def state_fields(self, *, source_id: str) -> dict[str, Any]:
        return {
            "root": str(self.logical_root),
            "resolved_root": str(self.resolved_root),
            "root_display_name": self.root_display_name,
            "scan_subdir": self.scan_subdir,
            "scan_root": str(self.scan_root),
            "stored_path_prefix": self.stored_path_prefix,
            "include_root_name_in_path": True,
            "source_id": source_id,
        }


def resolve_ingestion_scope(
    root: Path,
    scan_subdir: str | None = None,
) -> IngestionScope:
    logical_root = root.expanduser()
    if not logical_root.is_absolute():
        logical_root = Path.cwd() / logical_root
    logical_root = Path(os.path.abspath(logical_root))
    if not logical_root.exists():
        raise FileNotFoundError(f"logical root does not exist: {logical_root}")
    if not logical_root.is_dir():
        raise NotADirectoryError(f"logical root is not a directory: {logical_root}")

    resolved_root = logical_root.resolve(strict=True)
    root_display_name = logical_root.name
    if not root_display_name or root_display_name in {".", ".."}:
        raise ValueError(
            "logical root must have a directory name for stored paths"
        )
    if "\\" in root_display_name:
        raise ValueError(
            "logical root directory name must not contain a backslash"
        )

    normalized_input = _normalize_scan_subdir_input(scan_subdir)
    relative_parts = (
        ()
        if normalized_input == WHOLE_ROOT_SCAN
        else PurePosixPath(normalized_input).parts
    )
    requested_scan_root = logical_root.joinpath(*relative_parts)
    if not requested_scan_root.exists():
        raise FileNotFoundError(
            f"scan subdirectory does not exist: {normalized_input}"
        )
    if not requested_scan_root.is_dir():
        raise NotADirectoryError(
            f"scan subdirectory is not a directory: {normalized_input}"
        )
    scan_root = requested_scan_root.resolve(strict=True)
    try:
        resolved_scan_relative = scan_root.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            "--scan-subdir must resolve inside --root"
        ) from exc
    normalized_scan = (
        PurePosixPath(*resolved_scan_relative.parts).as_posix()
        if resolved_scan_relative.parts
        else WHOLE_ROOT_SCAN
    )
    stored_prefix = root_display_name
    if normalized_scan != WHOLE_ROOT_SCAN:
        stored_prefix = PurePosixPath(
            root_display_name,
            *PurePosixPath(normalized_scan).parts,
        ).as_posix()
    return IngestionScope(
        logical_root=logical_root,
        resolved_root=resolved_root,
        root_display_name=root_display_name,
        scan_subdir=normalized_scan,
        scan_root=scan_root,
        stored_path_prefix=stored_prefix.rstrip("/") + "/",
    )


def _normalize_scan_subdir_input(scan_subdir: str | None) -> str:
    if scan_subdir is None:
        return WHOLE_ROOT_SCAN
    value = str(scan_subdir)
    if not value:
        raise ValueError("--scan-subdir must not be empty")
    if "\x00" in value:
        raise ValueError("--scan-subdir contains an invalid character")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
    if windows.is_absolute() or windows.drive or posix.is_absolute():
        raise ValueError("--scan-subdir must be relative to --root")
    parts = [part for part in posix.parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError("--scan-subdir must not contain parent traversal")
    if not parts:
        return WHOLE_ROOT_SCAN
    return PurePosixPath(*parts).as_posix()
