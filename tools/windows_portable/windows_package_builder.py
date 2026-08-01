from __future__ import annotations

import email.parser
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


MANIFEST_SCHEMA = "local-rag.packaged-runtime.v1"
PACKAGE_SCHEMA = "local-rag.windows-package.v1"
SUPPORTED_PROFILES = frozenset({"search-only", "admin-full"})
FORBIDDEN_NAMES = frozenset(
    {
        ".rag-deps-installed",
        "network.json",
        "manage-custom.json",
        "sensitive-terms.local",
        "source-connections.json",
        "source-connections.secrets.json",
        ".source-connections.key",
        "windows-test-connection.local.json",
    }
)
FORBIDDEN_PARTS = frozenset({"__pycache__", "run"})
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".pem", ".key")


@dataclass(frozen=True)
class BuildRequest:
    payload_root: Path
    runtime_root: Path
    model_root: Path
    output_dir: Path
    version: str
    profile: str
    python_version: str
    dependency_lock_sha256: str
    model_fingerprint: str
    database_root: Path | None = None


@dataclass(frozen=True)
class BuildResult:
    zip_path: Path
    zip_sha256: str
    package_manifest_sha256: str
    expanded_size: int
    file_count: int


def build_package(request: BuildRequest) -> BuildResult:
    _validate_request(request)
    output_dir = request.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package_name = f"local-rag-windows-x64-{request.version}"
    zip_path = output_dir / f"{package_name}.zip"

    with tempfile.TemporaryDirectory(
        prefix=".local-rag-windows-package-", dir=str(output_dir)
    ) as temporary:
        package_root = Path(temporary) / package_name
        copilot_root = package_root / ".copilot"
        _copy_payload(request.payload_root, copilot_root)
        query_root = copilot_root / "rag" / "query"
        runtime_target = query_root / ".venv"
        model_target = copilot_root / "rag" / "models" / "ruri-v3-30m-onnx-int8"
        _copy_tree_exact(request.runtime_root, runtime_target)
        _copy_tree_exact(request.model_root, model_target)
        if request.database_root is not None:
            _copy_tree_exact(
                request.database_root,
                copilot_root / "rag" / "dbs" / request.database_root.name,
            )

        runtime_manifest = _runtime_manifest(request, runtime_target)
        runtime_manifest_path = query_root / ".packaged-runtime.json"
        _write_json(runtime_manifest_path, runtime_manifest)
        _write_text(package_root / "install.ps1", _install_bootstrap())
        _write_text(package_root / "README-WINDOWS.md", _windows_readme())
        _write_text(
            package_root / "THIRD_PARTY_NOTICES.md",
            _third_party_notices(request),
        )
        _write_json(package_root / "sbom.spdx.json", _sbom(request))

        payload_entries = _manifest_entries(
            package_root,
            excluded={"PACKAGE-MANIFEST.json", "SHA256SUMS"},
        )
        sums = "".join(
            f"{entry['sha256']}  {entry['path']}\n" for entry in payload_entries
        )
        _write_text(package_root / "SHA256SUMS", sums)
        package_entries = _manifest_entries(
            package_root, excluded={"PACKAGE-MANIFEST.json"}
        )
        package_manifest = {
            "schema": PACKAGE_SCHEMA,
            "product_version": request.version,
            "profile": request.profile,
            "platform": {"os": "windows", "arch": "amd64"},
            "python_version": request.python_version,
            "dependency_lock_sha256": request.dependency_lock_sha256,
            "model_fingerprint": request.model_fingerprint,
            "files": package_entries,
        }
        package_manifest_path = package_root / "PACKAGE-MANIFEST.json"
        _write_json(package_manifest_path, package_manifest)
        package_manifest_sha256 = _sha256_file(package_manifest_path)

        _assert_no_forbidden_payload(package_root)
        _write_deterministic_zip(package_root, zip_path)
        expanded_size = sum(
            path.stat().st_size
            for path in package_root.rglob("*")
            if path.is_file()
        )
        file_count = sum(1 for path in package_root.rglob("*") if path.is_file())

    return BuildResult(
        zip_path=zip_path,
        zip_sha256=_sha256_file(zip_path),
        package_manifest_sha256=package_manifest_sha256,
        expanded_size=expanded_size,
        file_count=file_count,
    )


def _validate_request(request: BuildRequest) -> None:
    if request.profile not in SUPPORTED_PROFILES:
        raise ValueError(f"unsupported package profile: {request.profile}")
    for label, value in (
        ("dependency lock", request.dependency_lock_sha256),
        ("model fingerprint", request.model_fingerprint),
    ):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.casefold()):
            raise ValueError(f"{label} must be a SHA-256 hex digest")
    if not request.version.strip() or not request.python_version.strip():
        raise ValueError("version fields must be non-empty")
    for label, root in (
        ("payload", request.payload_root),
        ("runtime", request.runtime_root),
        ("model", request.model_root),
    ):
        if not root.is_dir():
            raise ValueError(f"{label} root is missing: {root}")
        _reject_links(root)


def _reject_links(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"symlink or reparse point is forbidden: {path}")


def _copy_payload(source: Path, target: Path) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if _payload_excluded(relative, path.is_dir()):
            continue
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def _payload_excluded(relative: Path, is_dir: bool) -> bool:
    parts = tuple(part.casefold() for part in relative.parts)
    if any(part in FORBIDDEN_PARTS for part in parts):
        return True
    if "__pycache__" in parts or ".venv" in parts:
        return True
    name = relative.name.casefold()
    if (
        name in FORBIDDEN_NAMES
        or name.startswith(".source-connections.")
        or name.startswith(".rag-deps-installed.")
    ):
        return True
    if name.endswith(FORBIDDEN_SUFFIXES):
        return True
    if len(parts) >= 2 and parts[0] == "rag" and parts[1] in {"dbs", "models"}:
        return len(parts) != 3 or is_dir or name not in {"readme.md", ".gitkeep"}
    return False


def _copy_tree_exact(source: Path, target: Path) -> None:
    _reject_links(source)
    if target.exists():
        raise ValueError(f"staging target already exists: {target}")
    shutil.copytree(source, target, symlinks=False)


def _runtime_manifest(request: BuildRequest, runtime_root: Path) -> dict[str, object]:
    entries = _manifest_entries(runtime_root)
    executable = "Scripts/python.exe"
    declared = {str(entry["path"]).casefold() for entry in entries}
    if executable.casefold() not in declared:
        raise ValueError("runtime does not contain Scripts/python.exe")
    for pattern, label in (
        ("scripts/python*.dll", "Python DLL"),
        ("scripts/python*._pth", "Python ._pth"),
        ("scripts/python*.zip", "Python standard library"),
    ):
        import fnmatch

        if not any(fnmatch.fnmatchcase(path, pattern) for path in declared):
            raise ValueError(f"runtime does not contain {label}")
    return {
        "schema": MANIFEST_SCHEMA,
        "product_version": request.version,
        "profile": request.profile,
        "platform": {"os": "windows", "arch": "amd64"},
        "python": {
            "version": request.python_version,
            "executable": executable,
        },
        "dependency_lock_sha256": request.dependency_lock_sha256,
        "model_fingerprint": request.model_fingerprint,
        "distributions": _distribution_inventory(runtime_root),
        "files": entries,
    }


def _distribution_inventory(runtime_root: Path) -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []
    seen: set[str] = set()
    site_packages = runtime_root / "Lib" / "site-packages"
    for metadata in sorted(site_packages.glob("*.dist-info/METADATA")):
        message = email.parser.Parser().parsestr(
            metadata.read_text(encoding="utf-8")
        )
        name = str(message.get("Name") or "").strip()
        version = str(message.get("Version") or "").strip()
        key = name.casefold()
        if not name or not version:
            raise ValueError("runtime distribution metadata is incomplete")
        if key in seen:
            raise ValueError("duplicate runtime distribution metadata")
        seen.add(key)
        inventory.append({"name": name, "version": version})
    if not inventory:
        raise ValueError("runtime distribution inventory is empty")
    return sorted(
        inventory,
        key=lambda item: (item["name"].casefold(), item["version"]),
    )

def _manifest_entries(
    root: Path, *, excluded: set[str] | None = None
) -> list[dict[str, object]]:
    excluded = excluded or set()
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        _safe_archive_path(relative)
        if relative in excluded:
            continue
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return entries


def _safe_archive_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise ValueError(f"unsafe package path: {value}")


def _assert_no_forbidden_payload(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        name = path.name.casefold()
        if name == ".rag-deps-installed" or name.startswith(
            ".rag-deps-installed."
        ):
            raise ValueError("completion marker must not be packaged")
        if name in FORBIDDEN_NAMES or name.startswith(".source-connections."):
            raise ValueError(f"machine-local configuration must not be packaged: {relative}")
        if name.endswith((".pem", ".key")):
            raise ValueError(f"possible credential must not be packaged: {relative}")


def _write_deterministic_zip(package_root: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing package: {destination}")
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in sorted(package_root.rglob("*")):
            if not path.is_file():
                continue
            relative = Path(package_root.name) / path.relative_to(package_root)
            info = zipfile.ZipInfo(relative.as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def _write_json(path: Path, payload: object) -> None:
    _write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _install_bootstrap() -> str:
    template = Path(__file__).with_name("install-template.ps1")
    if not template.is_file():
        raise ValueError("Windows portable installer template is missing")
    return template.read_text(encoding="utf-8")


def _windows_readme() -> str:
    return """# Local RAG for Windows x64

This package contains a fixed, offline Python runtime. System Python, py.exe,
PATH changes, administrator rights, registry changes, pip, and model downloads
are not required.

After copying the package's .copilot tree into your profile, run:

    & "$env:USERPROFILE\\.copilot\\rag\\query\\.venv\\Scripts\\python.exe" `
      "$env:USERPROFILE\\.copilot\\rag\\setup.py" --format human

In VS Code Copilot Chat, select Agent and enable runInTerminal in Configure
Tools. Enable readFile when using file result delivery. Global auto-approve,
Bypass Approvals, and Autopilot are not Local RAG requirements.
"""


def _third_party_notices(request: BuildRequest) -> str:
    return f"""# Third-party notices

The packaged runtime profile is `{request.profile}` and embeds CPython
{request.python_version}. Exact dependency and model identities are recorded in
`PACKAGE-MANIFEST.json`, `.packaged-runtime.json`, and `sbom.spdx.json`.
Release production must replace or extend this generated notice with every
license text required by the locked dependency set before distribution.
"""


def _sbom(request: BuildRequest) -> dict[str, object]:
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"local-rag-windows-x64-{request.version}",
        "documentNamespace": (
            "https://github.com/harilos/github-copilot-local-rag/"
            f"sbom/windows/{request.version}/{request.profile}"
        ),
        "creationInfo": {
            "created": "2026-08-01T00:00:00Z",
            "creators": ["Tool: Local-RAG-Windows-Portable-Builder"],
        },
        "packages": [
            {
                "name": "CPython",
                "SPDXID": "SPDXRef-Package-CPython",
                "versionInfo": request.python_version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "PSF-2.0",
                "licenseDeclared": "PSF-2.0",
            },
            {
                "name": "Local-RAG-runtime-dependencies",
                "SPDXID": "SPDXRef-Package-Dependencies",
                "versionInfo": request.dependency_lock_sha256,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
            },
        ],
    }
