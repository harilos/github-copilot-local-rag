from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from .errors import SourceManagerError
from .git_host_urls import GIT_SOURCE_TYPES

_TOOL_ROOT = Path(__file__).resolve().parents[1] / "gen_db" / "software_rag_tool"
if str(_TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOL_ROOT))
from software_rag_tool.file_selection import (
    is_excluded, walk_selected,
    normalize_exclusion_paths as _normalize_exclusions,
    normalize_include_paths as _normalize_includes,
)


FILE_BASED_SOURCE_TYPES = GIT_SOURCE_TYPES | {"svn", "other"}
MAX_EXCLUSION_PATHS = 100
_GLOB_MAGIC = re.compile(r"[*?[]")
_SPLIT_INPUT = re.compile(r"[,、;；\r\n]+")


@dataclass(frozen=True)
class SourcePreview:
    included_count: int
    included_bytes: int
    excluded_count: int
    excluded_bytes: int

    @property
    def acquired_count(self) -> int:
        return self.included_count + self.excluded_count

    @property
    def acquired_bytes(self) -> int:
        return self.included_bytes + self.excluded_bytes

    def to_dict(self) -> dict[str, int]:
        return {
            "included_count": self.included_count,
            "included_bytes": self.included_bytes,
            "excluded_count": self.excluded_count,
            "excluded_bytes": self.excluded_bytes,
        }


@dataclass(frozen=True)
class PreparedSourceWork:
    preview: SourcePreview
    add_root: Path


def parse_exclusion_input(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, str):
        raise SourceManagerError("exclude_paths input must be text")
    return normalize_exclusion_paths(
        [item.strip() for item in _SPLIT_INPUT.split(value) if item.strip()]
    )


def normalize_exclusion_paths(value: Any) -> list[str]:
    try:
        return _normalize_exclusions(value)
    except ValueError as exc:
        raise SourceManagerError(str(exc)) from exc


def normalize_include_paths(value: Any) -> list[str]:
    try:
        return _normalize_includes(value)
    except ValueError as exc:
        raise SourceManagerError(str(exc)) from exc


def parse_include_input(value: str) -> list[str]:
    return normalize_include_paths([part.strip() for part in _SPLIT_INPUT.split(value) if part.strip()])


def exclusion_signature(paths: Iterable[str]) -> str:
    body = json.dumps(
        list(paths),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()



def preview_and_prepare_work(
    root: Path,
    exclusion_paths: Iterable[str],
    *,
    filtered_root: Path,
) -> PreparedSourceWork:
    """Stat acquired work and hard-link the included ADD view."""

    work = Path(root)
    try:
        root_metadata = os.lstat(work)
    except OSError as exc:
        raise SourceManagerError("Source work directory is unavailable") from exc
    if _unsafe_link(work, root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise SourceManagerError("Source work directory is unsafe")

    patterns = normalize_exclusion_paths(list(exclusion_paths))
    included_files: list[tuple[Path, Path]] = []
    included_count = included_bytes = excluded_count = excluded_bytes = 0
    for directory, child_names, file_names in os.walk(
        work,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        for name in sorted(child_names):
            candidate = directory_path / name
            metadata = os.lstat(candidate)
            if _unsafe_link(candidate, metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise SourceManagerError(
                    "Source work must not contain links or special files"
                )
        for name in sorted(file_names):
            candidate = directory_path / name
            metadata = os.lstat(candidate)
            if _unsafe_link(candidate, metadata) or not stat.S_ISREG(metadata.st_mode):
                raise SourceManagerError(
                    "Source work must not contain links or special files"
                )
            relative = candidate.relative_to(work).as_posix()
            size = max(0, int(metadata.st_size))
            if is_excluded(relative, patterns):
                excluded_count += 1
                excluded_bytes += size
            else:
                included_count += 1
                included_bytes += size
                included_files.append(
                    (candidate, Path(*PurePosixPath(relative).parts))
                )

    preview = SourcePreview(
        included_count=included_count,
        included_bytes=included_bytes,
        excluded_count=excluded_count,
        excluded_bytes=excluded_bytes,
    )
    if not patterns:
        return PreparedSourceWork(preview=preview, add_root=work)

    destination = Path(filtered_root)
    if (
        destination.name != work.name
        or destination == work
        or destination.parent.name != "filtered"
        or destination.parent.parent != work.parent.parent
    ):
        raise SourceManagerError("filtered Source work root is unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent_metadata = os.lstat(destination.parent)
    if _unsafe_link(destination.parent, parent_metadata):
        raise SourceManagerError("filtered Source work parent is unsafe")
    staging = destination.parent / f".incoming-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for source, relative in included_files:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, target)
        if destination.exists() or destination.is_symlink():
            metadata = os.lstat(destination)
            if _unsafe_link(destination, metadata) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                raise SourceManagerError("filtered Source work root is unsafe")
            shutil.rmtree(destination)
        os.replace(staging, destination)
    except OSError as exc:
        raise SourceManagerError(
            "filtered Source work could not be prepared without reading bodies"
        ) from exc
    finally:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
    return PreparedSourceWork(preview=preview, add_root=destination)


def discard_prepared_work(prepared_root: Path, acquired_root: Path) -> None:
    candidate = Path(prepared_root)
    original = Path(acquired_root)
    if (
        candidate == original
        or candidate.name != original.name
        or candidate.parent.name != "filtered"
        or candidate.parent.parent != original.parent.parent
    ):
        return
    try:
        metadata = os.lstat(candidate)
    except OSError:
        return
    if _unsafe_link(candidate, metadata) or not stat.S_ISDIR(metadata.st_mode):
        return
    try:
        shutil.rmtree(candidate)
    except OSError:
        return



def _unsafe_link(path: Path, metadata: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or path.is_symlink()
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )


__all__ = [
    "FILE_BASED_SOURCE_TYPES",
    "PreparedSourceWork",
    "SourcePreview",
    "exclusion_signature",
    "discard_prepared_work",
    "is_excluded",
    "normalize_exclusion_paths",
    "parse_exclusion_input",
    "preview_and_prepare_work",
]
