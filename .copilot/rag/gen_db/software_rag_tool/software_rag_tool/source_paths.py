from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

from .catalog import connect_readonly


class SourcePathError(ValueError):
    pass


def canonical_stored_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if (
        not text
        or PurePosixPath(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or bool(PureWindowsPath(text).drive)
    ):
        raise SourcePathError("stored path must be relative")
    parts = PurePosixPath(text.strip("/")).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise SourcePathError("stored path contains traversal")
    return PurePosixPath(*parts).as_posix()


def observed_root_from_paths(paths: Iterable[object]) -> tuple[str, ...]:
    roots: set[str] = set()
    for value in paths:
        path = canonical_stored_path(value)
        roots.add(path.split("/", 1)[0] + "/")
    return tuple(sorted(roots))


def source_relative_path(stored_path: object, observed_root: object) -> str:
    path = canonical_stored_path(stored_path)
    root = canonical_stored_path(str(observed_root or "").rstrip("/"))
    root_parts = PurePosixPath(root).parts
    if len(root_parts) != 1:
        raise SourcePathError("observed root must contain one path component")
    path_parts = PurePosixPath(path).parts
    if len(path_parts) < 2 or path_parts[0] != root_parts[0]:
        raise SourcePathError("stored path is outside the observed root")
    return PurePosixPath(*path_parts[1:]).as_posix()


def read_visible_source_paths(db_root: Path) -> dict[str, tuple[str, ...]]:
    catalog_path = Path(db_root).expanduser().resolve() / "catalog.sqlite"
    if not catalog_path.is_file():
        return {}
    with connect_readonly(catalog_path) as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(document)")
        }
        if not {"source_id", "path"}.issubset(columns):
            raise sqlite3.OperationalError(
                "catalog document table lacks Source path fields"
            )
        visibility = (
            "visible_until IS NULL"
            if "visible_until" in columns
            else "1=1"
        )
        rows = connection.execute(
            f"""
            SELECT source_id, path
            FROM document
            WHERE {visibility}
              AND source_id IS NOT NULL
              AND TRIM(source_id) <> ''
            ORDER BY source_id, path
            """
        ).fetchall()
    values: dict[str, list[str]] = {}
    for row in rows:
        source_id = str(row["source_id"])
        try:
            path = canonical_stored_path(row["path"])
        except SourcePathError:
            continue
        values.setdefault(source_id, []).append(path)
    return {
        source_id: tuple(paths)
        for source_id, paths in values.items()
    }


def read_visible_observed_roots(
    db_root: Path,
) -> dict[str, tuple[str, ...]]:
    return {
        source_id: observed_root_from_paths(paths)
        for source_id, paths in read_visible_source_paths(db_root).items()
    }
