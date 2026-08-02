from __future__ import annotations

import stat
import struct
from pathlib import Path


def is_amd64_pe(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(4096)
        if len(header) < 70 or header[:2] != b"MZ":
            return False
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if pe_offset + 6 > len(header):
            return False
        if header[pe_offset : pe_offset + 4] != b"PE\0\0":
            return False
        machine = struct.unpack_from("<H", header, pe_offset + 4)[0]
        return machine == 0x8664
    except (OSError, struct.error, ValueError):
        return False


def is_fixed_windows_runtime(query_root: Path) -> bool:
    """Identify the embedded portable layout without a validation artifact."""

    scripts = query_root / ".venv" / "Scripts"
    python = scripts / "python.exe"
    if not is_amd64_pe(python) or _is_reparse(scripts) or _is_reparse(python):
        return False
    try:
        path_files = sorted(scripts.glob("python*._pth"))
    except OSError:
        return False
    if len(path_files) != 1 or _is_reparse(path_files[0]):
        return False
    try:
        lines = path_files[0].read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return False
    return any(
        line.strip().replace("/", "\\").casefold() == r"..\..".casefold()
        for line in lines
    )


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse
    )
