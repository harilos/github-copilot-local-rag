from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Callable

from .errors import SourceManagerError


_MAX_STAGE_ATTEMPTS = 16


def _is_windows() -> bool:
    return os.name == "nt"


def create_persistent_directory(
    path: str | Path,
    *,
    trusted_root: str | Path,
    parents: bool = False,
    exist_ok: bool = False,
) -> Path:
    """Create a persistent directory without private Windows ACL synthesis.

    Windows Python 3.13 gives mkdir(mode=0o700) a protected ACL. Persistent
    paths must instead inherit the already-approved ACL of their trusted parent
    tree. POSIX retains the existing private 0700 contract.
    """

    root = _required_real_directory(Path(trusted_root), "trusted persistent root")
    target = _normalized_absolute(Path(path))
    if target == root or root not in target.parents:
        raise SourceManagerError("persistent path escaped its trusted root")

    relative = target.relative_to(root)
    current = root
    for index, component in enumerate(relative.parts):
        current = current / component
        final = index == len(relative.parts) - 1
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if not parents and not final:
                raise
            _mkdir_for_platform(current)
            _required_real_directory(current, "persistent directory")
            continue
        if _is_link_or_reparse(current, metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise SourceManagerError("persistent path must contain only real directories")
        if final and not exist_ok:
            raise FileExistsError(str(current))
    return target


def create_persistent_staging_directory(
    parent: str | Path,
    *,
    prefix: str,
    attempts: int = _MAX_STAGE_ATTEMPTS,
    token_factory: Callable[[], str] | None = None,
) -> Path:
    """Atomically reserve a persistent staging directory under a trusted parent."""

    trusted_parent = _required_real_directory(
        Path(parent), "persistent staging parent"
    )
    if (
        not prefix
        or "/" in prefix
        or "\\" in prefix
        or prefix in {".", ".."}
        or attempts < 1
    ):
        raise SourceManagerError("persistent staging parameters are invalid")
    make_token = token_factory or (lambda: secrets.token_hex(16))
    for _ in range(attempts):
        nonce = str(make_token())
        if (
            not nonce
            or len(nonce) > 128
            or any(character not in "0123456789abcdefABCDEF-" for character in nonce)
        ):
            raise SourceManagerError("persistent staging nonce is invalid")
        candidate = trusted_parent / f"{prefix}{nonce}"
        try:
            return create_persistent_directory(
                candidate,
                trusted_root=trusted_parent,
                exist_ok=False,
            )
        except FileExistsError:
            continue
    raise SourceManagerError("persistent staging name collision limit exceeded")


def persistent_access_error(
    managed_root: str | Path,
    path: str | Path,
    *,
    database_identifier: str,
) -> SourceManagerError:
    root = _normalized_absolute(Path(managed_root))
    candidate = _normalized_absolute(Path(path))
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        relative = "<outside-managed-root>"
    identifier = str(database_identifier or "<unknown>").replace("\r", " ").replace(
        "\n", " "
    )[:200]
    return SourceManagerError(
        "database access denied "
        f"(database={identifier}, path={relative}); "
        "the ACL owner may differ or inheritance may be disabled. "
        "An administrator must inspect only this managed path; "
        "do not run Manager permanently elevated."
    )


def _mkdir_for_platform(path: Path) -> None:
    if _is_windows():
        path.mkdir()
    else:
        path.mkdir(mode=0o700)


def _required_real_directory(path: Path, label: str) -> Path:
    absolute = _normalized_absolute(path)
    try:
        metadata = os.lstat(absolute)
    except FileNotFoundError as exc:
        raise SourceManagerError(f"{label} is missing") from exc
    if _is_link_or_reparse(absolute, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise SourceManagerError(f"{label} must be a real directory")
    return absolute


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )


__all__ = [
    "create_persistent_directory",
    "create_persistent_staging_directory",
    "persistent_access_error",
]
