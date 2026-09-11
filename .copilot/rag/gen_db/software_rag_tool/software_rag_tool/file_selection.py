from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

MAX_EXCLUSION_PATHS = 100
_GLOB_MAGIC = re.compile(r"[*?[]")

def normalize_exclusion_paths(value: Any) -> list[str]:
    """Normalize root-relative paths/globs to a portable POSIX list."""

    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("exclude_paths must be an array")
    if len(value) > MAX_EXCLUSION_PATHS:
        raise ValueError(
            f"exclude_paths cannot contain more than {MAX_EXCLUSION_PATHS} entries"
        )

    normalized: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("exclude_paths entries must be text")
        text = raw.strip()
        if not text:
            continue
        windows = PureWindowsPath(text)
        if windows.is_absolute() or windows.drive or text.startswith(("/", "\\")):
            raise ValueError("exclude_paths must be root-relative")
        text = text.replace("\\", "/")
        parts: list[str] = []
        for part in text.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise ValueError("exclude_paths must not escape the Source root")
            if "\x00" in part or any(ord(character) < 32 for character in part):
                raise ValueError("exclude_paths contains control characters")
            parts.append(part)
        if not parts:
            raise ValueError("exclude_paths entries must not be empty")
        path = PurePosixPath(*parts).as_posix()
        if path not in normalized:
            normalized.append(path)
    return normalized

def is_excluded(relative_path: str, patterns: Iterable[str]) -> bool:
    path_parts = tuple(PurePosixPath(relative_path).parts)
    if not path_parts:
        return False
    for pattern in patterns:
        pattern_parts = tuple(PurePosixPath(pattern).parts)
        if not _GLOB_MAGIC.search(pattern):
            if path_parts[: len(pattern_parts)] == pattern_parts:
                return True
            continue
        # A glob selecting a directory excludes everything below it as well.
        for end in range(1, len(path_parts) + 1):
            if _match_segments(path_parts[:end], pattern_parts):
                return True
    return False

def _match_segments(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        return _match_segments(path, pattern[1:]) or (
            bool(path) and _match_segments(path[1:], pattern)
        )
    return bool(path) and fnmatch.fnmatchcase(path[0], head) and _match_segments(
        path[1:], pattern[1:]
    )


def normalize_include_paths(value: Any) -> list[str]:
    paths = normalize_exclusion_paths(value)
    selected: list[str] = []
    for path in paths:
        if _GLOB_MAGIC.search(path) or any(p.casefold() in {".git", ".svn"} for p in PurePosixPath(path).parts):
            raise ValueError("include_paths must contain relative folders, without globs or VCS metadata")
        if any(path == p or path.startswith(p + "/") for p in selected):
            continue
        selected = [p for p in selected if not p.startswith(path + "/")]
        selected.append(path)
    return selected


def path_selected(relative: str, includes: Iterable[str], excludes: Iterable[str], *, directory: bool = False) -> bool:
    if os.name == "nt":
        relative = relative.casefold()
        includes = tuple(p.casefold() for p in includes)
        excludes = tuple(p.casefold() for p in excludes)
    if is_excluded(relative, excludes):
        return False
    return not includes or any(
        relative == p or relative.startswith(p + "/")
        or (directory and p.startswith(relative + "/"))
        for p in includes
    )


def validate_selected_folders(root: Path, includes: Iterable[str], excludes: Iterable[str]) -> None:
    for relative in includes:
        if path_selected(relative, (), excludes, directory=True) and not root.joinpath(*relative.split("/")).is_dir():
            raise ValueError(f"selected folder is unavailable: {relative}")


def walk_selected(root: Path, includes: Iterable[str] = (), excludes: Iterable[str] = ()):
    """Prune unselected directories before entering them; never read file bodies."""
    root = Path(root)
    includes, excludes = tuple(includes), tuple(excludes)
    validate_selected_folders(root, includes, excludes)

    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, children, files in os.walk(root, topdown=True, onerror=raise_walk_error, followlinks=False):
        current = Path(directory)
        children[:] = sorted(name for name in children if path_selected(
            (current / name).relative_to(root).as_posix(), includes, excludes, directory=True
        ))
        files[:] = sorted(name for name in files if path_selected(
            (current / name).relative_to(root).as_posix(), includes, excludes
        ))
        yield directory, children, files
