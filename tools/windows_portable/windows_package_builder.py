from __future__ import annotations

import json
import importlib.util
import os
import shutil
import re
import stat
import struct
import sys
import tempfile
import types
import zipfile
from dataclasses import dataclass
from pathlib import Path


SOURCE_MANAGER_ROOT = (
    Path(__file__).resolve().parents[2] / ".copilot" / "rag" / "source_manager"
)
ZIP_COPY_BUFFER_SIZE = 1024 * 1024


def _load_source_manager_module(name: str):
    package_name = "_windows_portable_source_manager"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(SOURCE_MANAGER_ROOT)]
        sys.modules[package_name] = package
    module_name = f"{package_name}.{name}"
    spec = importlib.util.spec_from_file_location(
        module_name, SOURCE_MANAGER_ROOT / f"{name}.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"source manager module is unavailable: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_SNAPSHOT_MODULE = _load_source_manager_module("packages")
_WINDOWS_BANNER_MODULE = _load_source_manager_module("windows_banner")
PackageError = _SNAPSHOT_MODULE.PackageError
stage_search_database_snapshots = (
    _SNAPSHOT_MODULE.stage_search_database_snapshots
)


_DB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
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
        ".packaged-runtime.json",
        "portable_runtime.py",
        "portable_db_install.py",
        "portable_db_smoke.py",
    }
)
FORBIDDEN_PARTS = frozenset({"__pycache__", "run"})
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".pem", ".key")
PUBLIC_CA_BUNDLE_PATHS = frozenset(
    {
        ".copilot/rag/query/.venv/lib/site-packages/certifi/cacert.pem",
        ".copilot/rag/query/.venv/lib/site-packages/grpc/_cython/_credentials/roots.pem",
    }
)


@dataclass(frozen=True)
class BuildRequest:
    payload_root: Path
    runtime_root: Path
    model_root: Path
    output_dir: Path
    version: str
    profile: str
    python_version: str
    databases_root: Path | None = None
    database_names: tuple[str, ...] = ()
    no_database: bool = False
    database_root: Path | None = None


@dataclass(frozen=True)
class BuildResult:
    zip_path: Path
    database_names: tuple[str, ...]


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
        _ensure_query_root_on_runtime_path(runtime_target)
        _copy_tree_exact(request.model_root, model_target)
        database_names, databases_root = _normalized_databases(request)
        if databases_root is not None:
            target_dbs = copilot_root / "rag" / "dbs"
            try:
                stage_search_database_snapshots(
                    databases_root,
                    target_dbs,
                    db_names=database_names,
                )
            except PackageError as exc:
                raise ValueError(str(exc)) from exc

        _prune_foreign_arch_setuptools_launchers(runtime_target)
        _assert_amd64_runtime(runtime_target)
        _write_text(
            package_root / "install.cmd",
            _install_cmd(),
            newline="\r\n",
        )
        _write_text(package_root / "internal" / "install.ps1", _install_bootstrap())
        _write_text(package_root / "README-WINDOWS.md", _windows_readme())
        _write_text(
            package_root / "THIRD_PARTY_NOTICES.md",
            _third_party_notices(request),
        )
        _write_json(package_root / "sbom.spdx.json", _sbom(request))

        _assert_no_forbidden_payload(package_root)
        _write_deterministic_zip(package_root, zip_path)

    return BuildResult(
        zip_path=zip_path,
        database_names=database_names,
    )


def _normalized_databases(request: BuildRequest) -> tuple[tuple[str, ...], Path | None]:
    if request.no_database:
        if request.database_root is not None or request.databases_root is not None or request.database_names:
            raise ValueError("no_database cannot be combined with database arguments")
        return (), None
    if request.database_root is not None:
        if request.databases_root is not None or request.database_names:
            raise ValueError("legacy database_root cannot be combined with canonical DB arguments")
        legacy_input = request.database_root.expanduser().absolute()
        _reject_reparse_ancestors(legacy_input)
        legacy = legacy_input.resolve(strict=True)
        root = legacy.parent.resolve(strict=True)
        names = (legacy.name,)
    else:
        if request.databases_root is None:
            raise ValueError("database selection is required; use no_database explicitly")
        root = (
            _trusted_root(request.databases_root)
            if request.databases_root is not None
            else None
        )
        names = tuple(str(value) for value in request.database_names)
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        raise ValueError("database names collide after casefold")
    if any(not _DB_NAME.fullmatch(name) for name in names):
        raise ValueError("database name is invalid")
    if names and root is None:
        raise ValueError("databases_root is required when databases are selected")
    if root is not None:
        if not root.is_dir():
            raise ValueError("databases_root is missing")
        metadata = root.lstat()
        if root.is_symlink() or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("databases_root link or reparse point is forbidden")
        for name in names:
            candidate = root / name
            metadata = candidate.lstat()
            if candidate.is_symlink() or (
                getattr(metadata, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("database link or reparse point is forbidden")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir() or resolved.parent != root:
                raise ValueError("database is outside the trusted root")
            _reject_links(resolved)
    return tuple(sorted(names, key=str.casefold)), root


def _validate_request(request: BuildRequest) -> None:
    if request.profile not in SUPPORTED_PROFILES:
        raise ValueError(f"unsupported package profile: {request.profile}")
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
    _normalized_databases(request)


def _reject_links(root: Path) -> None:
    attributes = getattr(root.lstat(), "st_file_attributes", 0)
    if root.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"symlink or reparse point is forbidden: {root}")
    for path in root.rglob("*"):
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if path.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"symlink or reparse point is forbidden: {path}")


def _reject_reparse_ancestors(path: Path) -> None:
    current = path.absolute()
    while True:
        if current.exists() or current.is_symlink():
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
            if current.is_symlink() or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"symlink or reparse point is forbidden: {current}")
        if current.parent == current:
            return
        current = current.parent


def _trusted_root(path: Path) -> Path:
    original = path.expanduser().absolute()
    _reject_reparse_ancestors(original)
    resolved = original.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("databases_root is missing")
    return resolved


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
        return True
    return False


def _copy_tree_exact(source: Path, target: Path) -> None:
    _reject_links(source)
    if target.exists():
        raise ValueError(f"staging target already exists: {target}")
    shutil.copytree(source, target, symlinks=False)


def _ensure_query_root_on_runtime_path(runtime_root: Path) -> None:
    scripts_root = runtime_root / "Scripts"
    path_files = sorted(
        scripts_root.glob("python*._pth"),
        key=lambda path: path.name.casefold(),
    )
    if len(path_files) != 1:
        raise ValueError("runtime must contain exactly one Python ._pth")
    path_file = path_files[0]
    try:
        text = path_file.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Python ._pth must be UTF-8 text") from exc
    lines = text.splitlines()
    query_root_entry = r"..\.."
    lines = [
        line
        for line in lines
        if line.strip().replace("/", "\\").casefold()
        != query_root_entry.casefold()
    ]
    import_site_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().casefold() == "import site"
        ),
        len(lines),
    )
    lines.insert(import_site_index, query_root_entry)
    with path_file.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _assert_amd64_runtime(runtime_root: Path) -> None:
    python = runtime_root / "Scripts" / "python.exe"
    if not python.is_file():
        raise ValueError("runtime does not contain Scripts/python.exe")
    binaries = sorted(
        path
        for path in runtime_root.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".exe", ".dll", ".pyd"}
    )
    for binary in binaries:
        if _pe_machine(binary) != 0x8664:
            relative = binary.relative_to(runtime_root).as_posix()
            raise ValueError(f"runtime PE is not AMD64: {relative}")


def _prune_foreign_arch_setuptools_launchers(runtime_root: Path) -> None:
    """Remove setuptools' inert cross-architecture launcher templates.

    Setuptools redistributes these files as package data for creating future
    entry points.  Local RAG never installs packages after packaging, and the
    Windows x64 artifact must not retain executable PE files for x86/ARM64.
    Only these exact upstream resource names and locations are eligible.
    """
    names = {
        "cli-32.exe",
        "cli-arm64.exe",
        "cli.exe",
        "gui-32.exe",
        "gui-arm64.exe",
        "gui.exe",
    }
    roots = (
        runtime_root / "Lib" / "site-packages" / "setuptools",
        runtime_root / "Scripts" / "Lib" / "site-packages" / "setuptools",
    )
    for root in roots:
        for name in names:
            path = root / name
            if not path.exists():
                continue
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"setuptools launcher resource is unsafe: {path}")
            if _pe_machine(path) != 0x8664:
                path.unlink()


def _pe_machine(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"MZ":
                raise ValueError
            handle.seek(0x3C)
            pe_offset = struct.unpack("<I", handle.read(4))[0]
            handle.seek(pe_offset)
            if handle.read(4) != b"PE\0\0":
                raise ValueError
            return struct.unpack("<H", handle.read(2))[0]
    except (OSError, struct.error, ValueError) as exc:
        raise ValueError(f"runtime PE header is invalid: {path}") from exc


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
        is_public_ca_bundle = (
            relative.as_posix().casefold() in PUBLIC_CA_BUNDLE_PATHS
        )
        if name.endswith(".key") or (
            name.endswith(".pem") and not is_public_ca_bundle
        ):
            raise ValueError(f"possible credential must not be packaged: {relative}")


def _write_deterministic_zip(package_root: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing package: {destination}")
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in sorted(package_root.rglob("*")):
                if not path.is_file():
                    continue
                relative = Path(package_root.name) / path.relative_to(package_root)
                info = zipfile.ZipInfo(relative.as_posix(), (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                with path.open("rb") as source:
                    info.file_size = os.fstat(source.fileno()).st_size
                    with archive.open(info, "w") as target:
                        shutil.copyfileobj(
                            source,
                            target,
                            length=ZIP_COPY_BUFFER_SIZE,
                        )
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("generated ZIP failed CRC verification")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, payload: object) -> None:
    _write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_text(path: Path, content: str, *, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline=newline)


def _install_bootstrap() -> str:
    template = Path(__file__).with_name("install-template.ps1")
    if not template.is_file():
        raise ValueError("Windows portable installer template is missing")
    return template.read_text(encoding="utf-8")


def _install_cmd() -> str:
    return (
        "@echo off\n"
        "setlocal EnableExtensions DisableDelayedExpansion\n"
        + _WINDOWS_BANNER_MODULE.install_cmd_banner(newline="\n")
        + 'set "local_rag_no_pause=0"\n'
        'set "local_rag_replace="\n'
        'set "local_rag_argument_error="\n'
        ":local_rag_parse\n"
        'if "%~1"=="" goto local_rag_run\n'
        'if /I "%~1"=="-NoPause" goto local_rag_mark_no_pause\n'
        'if /I "%~1"=="-ConfigureVSCodeAutoApprove" goto local_rag_ignore\n'
        'if /I "%~1"=="-SkipVSCodeAutoApprove" goto local_rag_ignore\n'
        'if /I "%~1"=="-RetryVSCodeApprovals" goto local_rag_ignore\n'
        'if /I "%~1"=="-ReplaceExistingDatabases" goto local_rag_mark_replace\n'
        'set "local_rag_argument_error=-LauncherArgumentError"\n'
        "shift\n"
        "goto local_rag_parse\n"
        ":local_rag_ignore\n"
        "shift\n"
        "goto local_rag_parse\n"
        ":local_rag_mark_no_pause\n"
        'set "local_rag_no_pause=1"\n'
        "shift\n"
        "goto local_rag_parse\n"
        ":local_rag_mark_replace\n"
        'set "local_rag_replace=-ReplaceExistingDatabases"\n'
        "shift\n"
        "goto local_rag_parse\n"
        ":local_rag_run\n"
        'if not exist "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" goto local_rag_powershell_unavailable\n'
        "\"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe\" "
        "-NoLogo -NoProfile -ExecutionPolicy Bypass -File "
        "\"%~dp0internal\\install.ps1\" "
        "%local_rag_replace% "
        "%local_rag_argument_error%\n"
        "set \"local_rag_rc=%ERRORLEVEL%\"\n"
        'if not "%local_rag_rc%"=="0" if not "%local_rag_rc%"=="1" goto local_rag_powershell_unavailable\n'
        "goto local_rag_finish\n"
        + _WINDOWS_BANNER_MODULE.install_cmd_powershell_failure(newline="\n")
        + ":local_rag_finish\n"
        'if "%local_rag_no_pause%"=="1" goto local_rag_exit\n'
        "echo.\n"
        "echo Press any key to close this window . . .\n"
        "pause >nul\n"
        ":local_rag_exit\n"
        "exit /b %local_rag_rc%\n"
    )


def _windows_readme() -> str:
    return """# Local RAG for Windows x64

This package contains a fixed, offline Python runtime. System Python, py.exe,
PATH changes, administrator rights, registry changes, pip, and model downloads
are not required.

Extract the ZIP to a local folder and double-click `install.cmd`. The launcher
uses Windows PowerShell 5.1 with a process-scoped execution-policy bypass and
returns the installer's exit code. Normal mode waits exactly once after either
success or failure. Add `-NoPause` anywhere in the `install.cmd` arguments for
automation; the launcher consumes it, does not forward it to PowerShell, and
does not print the wait prompt.

If Windows PowerShell cannot start, the cmd launcher itself prints Japanese
failure guidance and writes an actual run log before returning a nonzero code.

PowerShell writes exactly one log per run under
`%LOCALAPPDATA%\\LocalRAG\\logs` (falling back to TEMP), prints a Japanese result
summary, and shows the absolute log path. PowerShell never waits for input.

The package contains only the databases selected by the builder. Existing
unrelated databases are preserved. Replacing a same-name database requires
`install.cmd -ReplaceExistingDatabases`.

In VS Code Copilot Chat, select one of the three installed LOCAL-RAG Agents.
They expose only the two read-only Local RAG MCP tools. The installer registers
the fixed user-level `localragagent003` MCP server in both the portable Copilot
configuration and the normal VS Code Default Profile.
In PowerShell 7, run `local-rag-copilot` for the standard CLI profile, or add
`-Tier savings|standard|thorough`. That launcher pins the owned MCP definition
and auto-approves only those two read-only tools in any working directory.
It does not replace the normal `copilot` command or write persistent global
permissions.
The installer does not change VS Code approval settings.
Copilot acceptance is never run by the installer or product tests.
"""


def _third_party_notices(request: BuildRequest) -> str:
    return f"""# Third-party notices

The packaged runtime profile is `{request.profile}` and embeds CPython
{request.python_version}. The package includes `sbom.spdx.json` and this notice.
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
                "versionInfo": request.profile,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
            },
        ],
    }
