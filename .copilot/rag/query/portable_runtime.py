from __future__ import annotations

import hashlib
import json
import email.parser
import os
import platform
import re
from dataclasses import dataclass
import struct
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_SCHEMA = "local-rag.packaged-runtime.v1"
SUPPORTED_PROFILES = frozenset({"search-only", "admin-full"})
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class PortableRuntimeError(RuntimeError):
    """A packaged runtime failed a closed, offline validation."""


@dataclass(frozen=True)
class VerifiedPortableRuntime:
    manifest_path: Path
    manifest_sha256: str
    profile: str
    product_version: str
    python_version: str
    dependency_lock_sha256: str
    model_fingerprint: str


def manifest_path_for(query_root: Path) -> Path:
    return query_root / ".packaged-runtime.json"


def packaged_runtime_present(query_root: Path) -> bool:
    return manifest_path_for(query_root).is_file()


def load_and_verify_runtime(
    manifest_path: Path,
    *,
    check_platform: bool = True,
) -> VerifiedPortableRuntime:
    """Verify the immutable packaged runtime without network or writes."""

    manifest_path = manifest_path.resolve(strict=True)
    raw = manifest_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableRuntimeError("runtime manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise PortableRuntimeError("runtime manifest must be an object")
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise PortableRuntimeError("unsupported runtime manifest schema")

    profile = _required_text(payload, "profile")
    if profile not in SUPPORTED_PROFILES:
        raise PortableRuntimeError(f"unsupported runtime profile: {profile}")
    product_version = _required_text(payload, "product_version")
    dependency_lock = _required_sha256(payload, "dependency_lock_sha256")
    model_fingerprint = _required_sha256(payload, "model_fingerprint")

    platform_payload = _required_object(payload, "platform")
    if platform_payload.get("os") != "windows" or platform_payload.get("arch") != "amd64":
        raise PortableRuntimeError("runtime platform must be windows/amd64")
    if check_platform:
        actual_os = platform.system().casefold()
        actual_arch = platform.machine().casefold()
        if actual_os != "windows" or actual_arch not in {"amd64", "x86_64"}:
            raise PortableRuntimeError(
                f"unsupported host platform: {actual_os}/{actual_arch}"
            )
        if os.environ.get("PROCESSOR_ARCHITEW6432") is None and actual_arch in {
            "x86",
            "i386",
            "i686",
        }:
            raise PortableRuntimeError("32-bit process is unsupported")

    python_payload = _required_object(payload, "python")
    python_version = _required_text(python_payload, "version")
    executable = _safe_relative_path(_required_text(python_payload, "executable"))

    runtime_root = manifest_path.parent / ".venv"
    if not runtime_root.is_dir() or _is_reparse(runtime_root):
        raise PortableRuntimeError("packaged .venv is missing or is a reparse point")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise PortableRuntimeError("runtime manifest files must be a non-empty array")

    declared: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise PortableRuntimeError("runtime manifest file entry must be an object")
        relative = _safe_relative_path(_required_text(entry, "path"))
        key = relative.as_posix().casefold()
        if key in declared:
            raise PortableRuntimeError(f"duplicate runtime path: {relative.as_posix()}")
        declared.add(key)
        expected_size = entry.get("size")
        if not isinstance(expected_size, int) or expected_size < 0:
            raise PortableRuntimeError(f"invalid size for {relative.as_posix()}")
        expected_hash = _required_sha256(entry, "sha256")
        target = runtime_root.joinpath(*relative.parts)
        _verify_regular_file(runtime_root, target, relative)
        if target.stat().st_size != expected_size:
            raise PortableRuntimeError(f"size mismatch for {relative.as_posix()}")
        actual_hash = _sha256_file(target)
        if actual_hash != expected_hash:
            raise PortableRuntimeError(f"SHA-256 mismatch for {relative.as_posix()}")
        if target.suffix.casefold() in {".exe", ".dll", ".pyd"}:
            _verify_pe_amd64(target, relative)

    actual = {
        path.relative_to(runtime_root).as_posix().casefold()
        for path in runtime_root.rglob("*")
        if path.is_file()
    }
    unexpected = sorted(actual - declared)
    missing = sorted(declared - actual)
    if unexpected:
        raise PortableRuntimeError(f"unexpected runtime file: {unexpected[0]}")
    if missing:
        raise PortableRuntimeError(f"missing runtime file: {missing[0]}")
    distributions = payload.get("distributions")
    if not isinstance(distributions, list) or not distributions:
        raise PortableRuntimeError("runtime distributions must be non-empty")
    expected_distributions = sorted(
        (
            _required_text(item, "name").casefold(),
            _required_text(item, "version"),
        )
        for item in distributions
        if isinstance(item, dict)
    )
    if expected_distributions != _distribution_inventory(runtime_root):
        raise PortableRuntimeError("runtime distribution inventory mismatch")

    if executable.as_posix().casefold() not in declared:
        raise PortableRuntimeError("python executable is absent from runtime manifest")

    _require_declared_glob(declared, "scripts/python*.dll", "Python DLL")
    _require_declared_glob(declared, "scripts/python*._pth", "Python ._pth")
    _require_declared_glob(declared, "scripts/python*.zip", "Python standard library")

    return VerifiedPortableRuntime(
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        profile=profile,
        product_version=product_version,
        python_version=python_version,
        dependency_lock_sha256=dependency_lock,
        model_fingerprint=model_fingerprint,
    )


def _safe_relative_path(value: str) -> PurePosixPath:
    if "\\" in value:
        raise PortableRuntimeError(f"unsafe runtime path: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PortableRuntimeError(f"unsafe runtime path: {value}")
    if ":" in path.parts[0]:
        raise PortableRuntimeError(f"unsafe runtime path: {value}")
    return path


def _verify_regular_file(root: Path, target: Path, relative: PurePosixPath) -> None:
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PortableRuntimeError(f"unsafe or missing runtime file: {relative}") from exc
    if not target.is_file() or target.is_symlink() or _is_reparse(target):
        raise PortableRuntimeError(f"runtime path is not a regular file: {relative}")


def _is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)


def _required_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise PortableRuntimeError(f"runtime manifest {key} must be an object")
    return value


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PortableRuntimeError(f"runtime manifest {key} must be non-empty text")
    return value


def _verify_pe_amd64(path: Path, relative: PurePosixPath) -> None:
    if is_amd64_pe(path):
        return
    raise PortableRuntimeError(
        f"runtime PE is not AMD64: {relative.as_posix()}"
    )


def is_amd64_pe(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(4096)
        if len(header) < 70 or header[:2] != b"MZ":
            raise ValueError
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if pe_offset + 6 > len(header):
            raise ValueError
        if header[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise ValueError
        machine = struct.unpack_from("<H", header, pe_offset + 4)[0]
        return machine == 0x8664
    except (OSError, struct.error, ValueError):
        return False


def _distribution_inventory(runtime_root: Path) -> list[tuple[str, str]]:
    inventory: list[tuple[str, str]] = []
    site_packages = runtime_root / "Lib" / "site-packages"
    for metadata in sorted(site_packages.glob("*.dist-info/METADATA")):
        try:
            message = email.parser.Parser().parsestr(
                metadata.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError) as exc:
            raise PortableRuntimeError("runtime distribution metadata is unreadable") from exc
        name = str(message.get("Name") or "").strip().casefold()
        version = str(message.get("Version") or "").strip()
        if not name or not version:
            raise PortableRuntimeError("runtime distribution metadata is incomplete")
        inventory.append((name, version))
    if len(inventory) != len({name for name, _ in inventory}):
        raise PortableRuntimeError("duplicate runtime distribution metadata")
    return sorted(inventory)


def _required_sha256(payload: dict[str, Any], key: str) -> str:
    value = _required_text(payload, key).casefold()
    if SHA256_PATTERN.fullmatch(value) is None:
        raise PortableRuntimeError(f"runtime manifest {key} must be SHA-256")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_declared_glob(declared: set[str], pattern: str, label: str) -> None:
    import fnmatch

    if not any(fnmatch.fnmatchcase(path, pattern) for path in declared):
        raise PortableRuntimeError(f"required {label} is absent from runtime manifest")
