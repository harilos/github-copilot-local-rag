from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


INNER_PAYLOAD_NAME = "payload.zip"
COPY_BUFFER_SIZE = 1024 * 1024


class CompactPayloadError(RuntimeError):
    pass


def is_inner_payload_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.name != INNER_PAYLOAD_NAME:
        return False
    parts = path.parts
    return (
        parts == (".copilot", "rag", "query", ".venv", INNER_PAYLOAD_NAME)
        or parts
        == (
            ".copilot",
            "rag",
            "models",
            "ruri-v3-30m-onnx-int8",
            INNER_PAYLOAD_NAME,
        )
        or (
            len(parts) == 5
            and parts[:3] == (".copilot", "rag", "dbs")
            and parts[-1] == INNER_PAYLOAD_NAME
        )
    )


def compact_heavy_payloads(
    package_root: Path,
    database_names: Sequence[str],
    *,
    manifest_name: str | None = None,
) -> tuple[str, ...]:
    roots = [
        package_root / ".copilot" / "rag" / "query" / ".venv",
        package_root
        / ".copilot"
        / "rag"
        / "models"
        / "ruri-v3-30m-onnx-int8",
    ]
    roots.extend(
        package_root / ".copilot" / "rag" / "dbs" / name
        for name in database_names
    )
    archived: list[str] = []
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            raise CompactPayloadError("inner_payload_root_invalid")
        _archive_tree_in_place(root)
        archived.append(
            (root.relative_to(package_root) / INNER_PAYLOAD_NAME).as_posix()
        )
    if manifest_name is not None:
        _rewrite_manifest(package_root, archived, manifest_name)
    return tuple(archived)


def inner_archive_names(path: Path) -> tuple[str, ...]:
    with zipfile.ZipFile(path) as archive:
        names = tuple(info.filename for info in archive.infolist() if not info.is_dir())
        if archive.testzip() is not None:
            raise CompactPayloadError("inner_payload_crc_invalid")
    _validate_names(names)
    return names


def _archive_tree_in_place(root: Path) -> None:
    parent = root.parent
    temporary = parent / f".{root.name}.{uuid.uuid4().hex}.payload.tmp"
    try:
        with zipfile.ZipFile(
            temporary,
            "x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for source in _regular_files(root):
                relative = source.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                with source.open("rb") as reader:
                    info.file_size = os.fstat(reader.fileno()).st_size
                    with archive.open(info, "w") as writer:
                        shutil.copyfileobj(reader, writer, length=COPY_BUFFER_SIZE)
        inner_archive_names(temporary)
        for child in tuple(root.iterdir()):
            if child.is_symlink():
                raise CompactPayloadError("inner_payload_link_forbidden")
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        os.replace(temporary, root / INNER_PAYLOAD_NAME)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _regular_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        metadata = path.lstat()
        if path.is_symlink() or bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise CompactPayloadError("inner_payload_link_forbidden")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CompactPayloadError("inner_payload_special_file_forbidden")
        yield path


def _validate_names(names: Sequence[str]) -> None:
    seen: set[str] = set()
    for raw in names:
        value = raw.replace("\\", "/")
        path = PurePosixPath(value)
        if (
            not value
            or value.startswith("/")
            or len(value) >= 2
            and value[1] == ":"
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise CompactPayloadError("inner_payload_path_invalid")
        folded = value.casefold()
        if folded in seen:
            raise CompactPayloadError("inner_payload_duplicate_path")
        seen.add(folded)


def _rewrite_manifest(
    package_root: Path,
    archived: Sequence[str],
    manifest_name: str,
) -> None:
    path = package_root / manifest_name
    manifest = json.loads(path.read_text(encoding="utf-8"))
    prefixes = tuple(value[: -len(INNER_PAYLOAD_NAME)] for value in archived)
    records = [
        record
        for record in manifest["files"]
        if not any(str(record["path"]).startswith(prefix) for prefix in prefixes)
    ]
    for relative in archived:
        source = package_root.joinpath(*PurePosixPath(relative).parts)
        records.append(
            {
                "path": relative,
                "size": source.stat().st_size,
                "sha256": _sha256(source),
            }
        )
    records.sort(key=lambda item: str(item["path"]))
    manifest["files"] = records
    manifest["total"] = {
        "files": len(records),
        "bytes": sum(int(record["size"]) for record in records),
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(COPY_BUFFER_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()
