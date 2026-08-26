from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from . import packages, windows_banner


RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
LOCK_PATH = Path(__file__).with_name("windows-runtime-lock.json")
INSTALL_TEMPLATE = Path(__file__).with_name("windows-install-template.ps1")
SEARCH_REQUIREMENTS = (
    RAG_ROOT / "query" / "requirements-windows-search.lock"
)
MODEL_NAME = "ruri-v3-30m-onnx-int8"
MODEL_REQUIRED = (
    "model.onnx",
    "config.json",
    "tokenizer_config.json",
    "MODEL_MANIFEST.json",
)
PUBLIC_CA_BUNDLES = frozenset(
    {
        "scripts/lib/site-packages/certifi/cacert.pem",
        "scripts/lib/site-packages/grpc/_cython/_credentials/roots.pem",
        "scripts/lib/site-packages/pip/_vendor/certifi/cacert.pem",
    }
)
PIP_DISTLIB_LAUNCHERS = {
    "t32.exe": 0x014C,
    "t64-arm.exe": 0xAA64,
    "t64.exe": 0x8664,
    "w32.exe": 0x014C,
    "w64-arm.exe": 0xAA64,
    "w64.exe": 0x8664,
}


def create_windows_distribution_package(
    copilot_home: Path,
    output_zip: Path,
    *,
    db_names: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Build a Windows x64 package that needs no recipient Python or network."""

    if not sys.platform.startswith("win"):
        raise packages.PackageError("windows_offline_package_requires_windows")
    emit = progress or (lambda _message: None)
    home = packages._real_directory(copilot_home, "copilot_home")
    output = packages._new_output_path(output_zip, directory=False)
    _validate_model(home)
    created = packages._created_at(None)
    version = packages._tool_version(home / "rag")
    work = Path(
        tempfile.mkdtemp(
            prefix=".local-rag-windows-offline.",
            dir=str(output.parent),
        )
    )
    archive_tmp = output.parent / (
        f".{output.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        emit("[1/5] 固定Pythonを準備しています。")
        runtime = work / "runtime"
        _prepare_runtime(home, runtime, emit=emit)

        emit("[2/5] 検索コード、モデル、選択DBを収集しています。")
        entries = packages._product_entries(home, admin=False)
        database_entries, databases = packages._database_entries(
            home / "rag" / "dbs",
            db_names=db_names,
            distribution=True,
        )
        entries.extend(database_entries)
        entries.extend(_runtime_entries(runtime))
        entries.extend(_generated_installer_entries(work))
        entries = packages._dedupe_entries(entries)

        emit("[3/5] package manifestとZIPを作成しています。")
        stage = work / "package"
        manifest = packages._stage_package(
            stage,
            entries,
            kind=packages._DISTRIBUTION_KIND,
            databases=databases,
            created=created,
            tool_version=version,
        )
        packages.validate_package_tree(
            stage,
            expected_kind=packages._DISTRIBUTION_KIND,
        )

        emit("[4/5] runtime・model・DBの同梱構造を確認しています。")
        verification = _verify_staged_structure(stage, databases)

        packages._write_zip(stage, archive_tmp)
        emit("[5/5] 完成ZIPのchecksumと内容を検証しています。")
        packages.validate_distribution_zip(
            archive_tmp,
            expected_kind=packages._DISTRIBUTION_KIND,
        )
        packages._fsync_file(archive_tmp)
        os.replace(archive_tmp, output)
        packages._fsync_directory(output.parent)
        return {
            "status": "written",
            "kind": packages._DISTRIBUTION_KIND,
            "platform": "windows-amd64-offline",
            "recipient_python_required": False,
            "recipient_network_required": False,
            "output": str(output),
            "manifest": manifest,
            "verification": verification,
        }
    except packages.PackageError:
        raise
    except Exception as exc:
        raise packages.PackageError(
            f"windows_offline_package_failed:{type(exc).__name__}"
        ) from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            archive_tmp.unlink()
        except OSError:
            pass


def _prepare_runtime(
    copilot_home: Path,
    runtime: Path,
    *,
    emit: Callable[[str], None],
) -> None:
    lock = _load_lock()
    python_lock = lock["python"]
    version = str(python_lock["version"])
    version_parts = version.split(".")
    if len(version_parts) < 2 or not all(
        part.isdigit() for part in version_parts[:2]
    ):
        raise packages.PackageError("windows_runtime_lock_invalid")
    major, minor = version_parts[:2]
    tag = f"{major}{minor}"
    cache = copilot_home / "rag" / "cache" / "windows-portable"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"python-{version}-embed-amd64.zip"
    _obtain_archive(
        archive,
        url=str(python_lock["url"]),
        expected_sha256=str(python_lock["sha256"]),
        copilot_home=copilot_home,
        emit=emit,
    )

    scripts = runtime / "Scripts"
    scripts.mkdir(parents=True)
    _extract_archive(archive, scripts)
    path_files = sorted(scripts.glob("python*._pth"))
    python_zips = sorted(scripts.glob("python*.zip"))
    if len(path_files) != 1 or len(python_zips) != 1:
        raise packages.PackageError("windows_embedded_python_layout_invalid")
    path_files[0].write_text(
        "\n".join(
            (
                python_zips[0].name,
                ".",
                r"..\..",
                r"Lib\site-packages",
                "import site",
                "",
            )
        ),
        encoding="ascii",
        newline="\n",
    )

    site_packages = scripts / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    pip_cache = cache / "pip"
    pip_cache.mkdir(parents=True, exist_ok=True)
    environment = _network_environment(copilot_home)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--only-binary=:all:",
        "--no-compile",
        "--disable-pip-version-check",
        "--platform",
        "win_amd64",
        "--implementation",
        "cp",
        "--python-version",
        f"{major}.{minor}",
        "--abi",
        f"cp{tag}",
        "--cache-dir",
        str(pip_cache),
        "--target",
        str(site_packages),
        "--requirement",
        str(SEARCH_REQUIREMENTS),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=1800,
    )
    if completed.returncode != 0:
        message = _redact_output(
            "\n".join(
                value
                for value in (completed.stdout, completed.stderr)
                if value
            )[-1800:]
        )
        raise packages.PackageError(
            "windows_runtime_dependencies_failed" + (
                f":{message}" if message else ""
            )
        )
    for path in sorted(runtime.rglob("*"), reverse=True):
        if path.is_dir() and path.name.casefold() == "__pycache__":
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and path.suffix.casefold() in {".pyc", ".pyo"}:
            path.unlink(missing_ok=True)
    _prune_pip_distlib_launchers(runtime)
    _validate_runtime(runtime)


def _load_lock() -> dict[str, Any]:
    try:
        payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise packages.PackageError("windows_runtime_lock_invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "local-rag.windows-runtime-lock.v1"
        or not isinstance(payload.get("python"), dict)
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(payload["python"].get("sha256") or ""),
        )
        or not str(payload["python"].get("url") or "").startswith(
            "https://www.python.org/"
        )
    ):
        raise packages.PackageError("windows_runtime_lock_invalid")
    return payload


def _obtain_archive(
    destination: Path,
    *,
    url: str,
    expected_sha256: str,
    copilot_home: Path,
    emit: Callable[[str], None],
) -> None:
    if destination.is_file() and _sha256(destination) == expected_sha256:
        emit("固定Pythonは検証済みcacheを再利用します。")
        return
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise packages.PackageError("windows_runtime_cache_invalid")
        destination.unlink()
    resolution = _resolve_network(copilot_home)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.partial"
    )
    try:
        with resolution.build_url_opener().open(url, timeout=120) as source:
            with temporary.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        if _sha256(temporary) != expected_sha256:
            raise packages.PackageError("windows_runtime_archive_hash_mismatch")
        os.replace(temporary, destination)
    except packages.PackageError:
        raise
    except Exception as exc:
        raise packages.PackageError(
            f"windows_runtime_download_failed:{type(exc).__name__}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _extract_archive(archive_path: Path, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise packages.PackageError("windows_runtime_archive_invalid") from exc
    with archive:
        for info in archive.infolist():
            name = info.filename.rstrip("/")
            if not name:
                continue
            relative = PurePosixPath(name)
            if relative.is_absolute() or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                raise packages.PackageError("windows_runtime_archive_invalid")
            target = destination.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)


def _runtime_entries(runtime: Path) -> list[packages._Entry]:
    _validate_runtime(runtime)
    entries: list[packages._Entry] = []
    for path in sorted(runtime.rglob("*")):
        metadata = path.lstat()
        if path.is_symlink() or bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise packages.PackageError("windows_runtime_link_forbidden")
        if path.is_dir():
            continue
        if not path.is_file():
            raise packages.PackageError("windows_runtime_special_file_forbidden")
        relative = path.relative_to(runtime)
        lowered = relative.as_posix().casefold()
        name = path.name.casefold()
        if (
            "__pycache__" in {part.casefold() for part in relative.parts}
            or path.suffix.casefold() in {".pyc", ".pyo", ".key", ".p12", ".pfx"}
            or name == ".rag-deps-installed"
            or name.startswith(".rag-deps-installed.")
        ):
            raise packages.PackageError("windows_runtime_forbidden_file")
        if path.suffix.casefold() == ".pem" and lowered not in PUBLIC_CA_BUNDLES:
            raise packages.PackageError("windows_runtime_private_material_forbidden")
        packages._reject_private_key_material(path)
        if path.suffix.casefold() == ".pth":
            _validate_pth(path)
        entries.append(
            packages._Entry(
                path,
                (
                    PurePosixPath(".copilot/rag/query/.venv")
                    / PurePosixPath(relative.as_posix())
                ).as_posix(),
                source_root=runtime,
            )
        )
    return entries


def _prune_pip_distlib_launchers(runtime: Path) -> None:
    site_packages = runtime / "Scripts" / "Lib" / "site-packages"
    pip_root = site_packages / "pip"
    metadata = sorted(site_packages.glob("pip-*.dist-info"))
    if (
        not pip_root.is_dir()
        or pip_root.is_symlink()
        or len(metadata) != 1
        or not metadata[0].is_dir()
        or metadata[0].is_symlink()
        or not (pip_root / "__init__.py").is_file()
    ):
        raise packages.PackageError("windows_runtime_pip_invalid")
    launcher_root = pip_root / "_vendor" / "distlib"
    actual = {
        path.name
        for path in launcher_root.glob("*.exe")
        if path.is_file() and not path.is_symlink()
    }
    if actual != set(PIP_DISTLIB_LAUNCHERS):
        raise packages.PackageError("windows_runtime_pip_launcher_set_invalid")
    for name, expected_machine in PIP_DISTLIB_LAUNCHERS.items():
        path = launcher_root / name
        try:
            machine = _pe_machine(path)
        except packages.PackageError as exc:
            raise packages.PackageError(
                "windows_runtime_pip_launcher_invalid"
            ) from exc
        if machine != expected_machine:
            raise packages.PackageError("windows_runtime_pip_launcher_invalid")
    for name in PIP_DISTLIB_LAUNCHERS:
        (launcher_root / name).unlink()


def _validate_runtime(runtime: Path) -> None:
    scripts = runtime / "Scripts"
    python = scripts / "python.exe"
    path_files = list(scripts.glob("python*._pth"))
    if not python.is_file() or len(path_files) != 1:
        raise packages.PackageError("windows_embedded_python_layout_invalid")
    _assert_amd64_pe(python)
    for path in runtime.rglob("*"):
        if path.is_file() and path.suffix.casefold() in {".exe", ".dll", ".pyd"}:
            _assert_amd64_pe(path)


def _assert_amd64_pe(path: Path) -> None:
    if _pe_machine(path) != 0x8664:
        raise packages.PackageError("windows_runtime_not_amd64")


def _pe_machine(path: Path) -> int:
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError
            stream.seek(0x3C)
            pe_offset = int.from_bytes(stream.read(4), "little")
            stream.seek(pe_offset)
            if stream.read(4) != b"PE\0\0":
                raise ValueError
            machine = int.from_bytes(stream.read(2), "little")
    except (OSError, ValueError) as exc:
        raise packages.PackageError("windows_runtime_not_amd64") from exc
    return machine


def _validate_pth(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise packages.PackageError("windows_runtime_pth_invalid") from exc
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#") or value.startswith("import "):
            continue
        if (
            Path(value).is_absolute()
            or re.match(r"^[A-Za-z]:[\\/]", value)
            or value.startswith("\\\\")
        ):
            raise packages.PackageError("windows_runtime_pth_not_portable")


def _generated_installer_entries(work: Path) -> list[packages._Entry]:
    generated = work / "generated"
    generated.mkdir()
    install_cmd = generated / "install.cmd"
    readme = generated / "README-WINDOWS.md"
    install_cmd.write_text(
        "@echo off\r\n"
        "setlocal EnableExtensions DisableDelayedExpansion\r\n"
        + windows_banner.install_cmd_banner()
        + 'set "local_rag_no_pause=0"\r\n'
        'set "local_rag_replace="\r\n'
        'set "local_rag_argument_error="\r\n'
        ":local_rag_parse\r\n"
        'if "%~1"=="" goto local_rag_run\r\n'
        'if /I "%~1"=="-NoPause" goto local_rag_mark_no_pause\r\n'
        'if /I "%~1"=="-ConfigureVSCodeAutoApprove" goto local_rag_ignore\r\n'
        'if /I "%~1"=="-SkipVSCodeAutoApprove" goto local_rag_ignore\r\n'
        'if /I "%~1"=="-RetryVSCodeApprovals" goto local_rag_ignore\r\n'
        'if /I "%~1"=="-ReplaceExistingDatabases" goto local_rag_mark_replace\r\n'
        'set "local_rag_argument_error=-LauncherArgumentError"\r\n'
        "shift\r\n"
        "goto local_rag_parse\r\n"
        ":local_rag_ignore\r\n"
        "shift\r\n"
        "goto local_rag_parse\r\n"
        ":local_rag_mark_no_pause\r\n"
        'set "local_rag_no_pause=1"\r\n'
        "shift\r\n"
        "goto local_rag_parse\r\n"
        ":local_rag_mark_replace\r\n"
        'set "local_rag_replace=-ReplaceExistingDatabases"\r\n'
        "shift\r\n"
        "goto local_rag_parse\r\n"
        ":local_rag_run\r\n"
        'if not exist "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" goto local_rag_powershell_unavailable\r\n'
        '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        "-NoLogo -NoProfile -ExecutionPolicy Bypass -File "
        '"%~dp0internal\\install.ps1" '
        "%local_rag_replace% "
        "%local_rag_argument_error%\r\n"
        'set "local_rag_rc=%ERRORLEVEL%"\r\n'
        'if not "%local_rag_rc%"=="0" if not "%local_rag_rc%"=="1" goto local_rag_powershell_unavailable\r\n'
        'goto local_rag_finish\r\n'
        + windows_banner.install_cmd_powershell_failure()
        + ':local_rag_finish\r\n'
        'if "%local_rag_no_pause%"=="1" goto local_rag_exit\r\n'
        "echo.\r\n"
        "echo Press any key to close this window . . .\r\n"
        "pause >nul\r\n"
        ":local_rag_exit\r\n"
        "exit /b %local_rag_rc%\r\n",
        encoding="utf-8",
        newline="",
    )
    readme.write_text(
        "# Local RAG Windows x64 offline package\n\n"
        "Extract this ZIP and double-click `install.cmd`. The recipient does "
        "not need Python, pip, PATH changes, administrator rights, or a "
        "network connection. The PowerShell installer writes one run log, "
        "prints a Japanese SUCCESS or FAILED summary, and shows the absolute "
        "log path. Normal mode waits exactly once after either result. Add "
        "`-NoPause` anywhere in the `install.cmd` arguments for automation; "
        "the launcher removes it before PowerShell and does not print a wait "
        "prompt. Logs are written under "
        "`%LOCALAPPDATA%\\LocalRAG\\logs` with a TEMP fallback. If Windows "
        "PowerShell cannot start, the cmd launcher itself prints Japanese "
        "failure guidance and writes the same class of run log.\n\n"
        "The package registers the fixed user-level `localragagent003` MCP "
        "server in both the portable Copilot configuration and the normal "
        "VS Code Default Profile. PowerShell 7 users can run "
        "`local-rag-copilot` and select savings, standard, or thorough; that "
        "launcher pins the owned MCP definition and auto-approves only the "
        "two read-only Local RAG tools in any working directory. It does not "
        "replace the normal `copilot` command or write persistent global "
        "permissions. It does not change VS Code approval settings. Copilot "
        "acceptance is not run by the installer or product tests.\n",
        encoding="utf-8",
    )
    return [
        packages._Entry(install_cmd, "install.cmd", source_root=generated),
        packages._Entry(
            INSTALL_TEMPLATE,
            "internal/install.ps1",
            source_root=INSTALL_TEMPLATE.parent,
        ),
        packages._Entry(readme, "README-WINDOWS.md", source_root=generated),
    ]


def _verify_staged_structure(
    stage: Path,
    databases: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    runtime = stage / ".copilot" / "rag" / "query" / ".venv"
    _validate_runtime(runtime)
    _validate_model(stage / ".copilot")
    expected = {str(item["name"]) for item in databases}
    dbs_root = stage / ".copilot" / "rag" / "dbs"
    packaged = (
        {
            path.name
            for path in dbs_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        }
        if dbs_root.is_dir()
        else set()
    )
    if packaged != expected:
        raise packages.PackageError("windows_offline_database_layout_mismatch")
    return {
        "runtime_layout": "pass",
        "model_required_files": "pass",
        "packaged_databases": sorted(packaged, key=str.casefold),
        "manifest": "pass",
        "list_dbs_executed": False,
        "real_search_executed": False,
    }


def _validate_model(copilot_home: Path) -> None:
    model = copilot_home / "rag" / "models" / MODEL_NAME
    missing = [
        name
        for name in MODEL_REQUIRED
        if not (model / name).is_file() or (model / name).stat().st_size <= 0
    ]
    if not any(
        (model / name).is_file() and (model / name).stat().st_size > 0
        for name in ("tokenizer.json", "tokenizer.model")
    ):
        missing.append("tokenizer.json|tokenizer.model")
    if missing:
        raise packages.PackageError(
            "windows_offline_model_missing:" + ",".join(missing)
        )


def _resolve_network(copilot_home: Path) -> Any:
    if str(TOOL_ROOT) not in sys.path:
        sys.path.insert(0, str(TOOL_ROOT))
    from software_rag_tool.network import resolve_network_configuration

    return resolve_network_configuration(
        external_operation=True,
        default_config_path=copilot_home / "rag" / "config" / "network.json",
    )


def _network_environment(copilot_home: Path) -> dict[str, str]:
    environment = dict(_resolve_network(copilot_home).environment)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _redact_output(value: str) -> str:
    if str(TOOL_ROOT) not in sys.path:
        sys.path.insert(0, str(TOOL_ROOT))
    from software_rag_tool.network import redact_text

    return redact_text(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["create_windows_distribution_package"]
