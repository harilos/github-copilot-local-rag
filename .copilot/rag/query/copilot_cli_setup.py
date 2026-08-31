from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import mcp_config


SCHEMA_VERSION = 1
BUNDLE_NAME = "copilot-cli"
MANIFEST_NAME = "owned-manifest.json"
PINNED_CONFIG_NAME = "local-rag-agent003.pinned-mcp.json"
LAUNCHER_NAME = "local-rag-agent003.ps1"
TEMPORARY_RELATIVE = Path("rag") / "query" / "run" / "tmp"
AGENT_NAMES = (
    "local-rag-agent003-savings.agent.md",
    "local-rag-agent003-standard.agent.md",
    "local-rag-agent003-thorough.agent.md",
)
LEGACY_AGENT_HASHES = {
    "internal-doc-search.agent.md": frozenset(
        {
            "93c395b28ca84c3cd328fae8b3a9b5702b4089ef49703b7322527502a5520cf8",
            "486dddb48dd394c131932511a97a80938bee4a8eec02b26f17fb32931ede4fca",
            "72a299323fcd9ae112fef3dd5ddc482815bb4290a5b5e033c876890928354262",
            "babcd820d4d2b6970dda51e419807c1d9504f58c410a2057b2eff8ca08470142",
            "129fab090776441bfbaae48b152f941a8f3c100fe63c1062d08ca948738e7098",
            "5142023323d7e7c1e20666100b1cb02f2176a8044061716409f7ab7222eb9110",
            "8633860ae1ed2c823658ff3544c2eaf2ef0852f9fb44fe4e519c3679dc48fa3d",
            "be29b7c674ecb48da8b4aa243e1925ed0d03a9b4edfc32d35548c153aed430d9",
        }
    ),
    "agent003-readonly-local-rag.agent.md": frozenset(
        {
            "e9c3591c7ae5a0b17ec9759c67f580eb080b02a8a8b834a3834d32779ea87836",
            "98b092c5f1d0731d8b58f64440ea8d9983d475649bef7eb02fffff08b7bedceb",
            "37da776945f38682d785a28fa50d1f3665d78b3035d37778bd2eeb6b88a4289f",
        }
    ),
    "internal-doc-deep-research.agent.md": frozenset(
        {
            "5bc8ba97a9d51ebca3f441724cfdd392d258a1d6e551802220b6c01b7768ef39",
            "bae16f42a6fdba678d8cf3ae0ab6facecbe97b3e8f5be8589db8e4c4312fc2a9",
            "e9cce412e5cec4a14c6d62d657fced68df01ef0c1fda1edaa2912dbf26e4e146",
            "5baec62979950f74c66264c8fbdd5a45fde5790ec1fc5be9b925d21c43ffe175",
            "1479f17ba50170c9d248de540a8dc4f1f74407a55d6f17f6d8c69f521b86bdd3",
            "2c71cd3f27e58d7bb51a686fde24f0bf0f82100dc7366b9470d950bc43f3b35c",
            "4d209adc281064579d83bae9d830d15af5950c273dca718ebb38762691e4cc40",
            "a441ecef3b450bc627948097b7087c4d4a9a0abaa732750e2766889feef5d886",
        }
    ),
}
TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "copilot-cli"
COPILOT_ROOT = "copilot_home"
INSTALL_ROOT = "install_root"
PROFILE_START = "# >>> Local RAG Agent003 CLI (owned) >>>"
PROFILE_END = "# <<< Local RAG Agent003 CLI (owned) <<<"


class CopilotCliSetupError(RuntimeError):
    """Base class for controlled Agent003 CLI setup failures."""


class OwnedArtifactCollisionError(CopilotCliSetupError):
    """An owned path cannot safely be claimed or removed."""


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    boundary: Path
    existed: bool
    content: bytes


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def default_copilot_home(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    explicit = values.get("COPILOT_HOME", "").strip()
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            raise CopilotCliSetupError("COPILOT_HOME must be an absolute path")
        return _absolute(candidate)
    user_profile = values.get("USERPROFILE", "").strip()
    if not user_profile:
        raise CopilotCliSetupError(
            "USERPROFILE is required when COPILOT_HOME is unset"
        )
    candidate = Path(user_profile)
    if not candidate.is_absolute():
        raise CopilotCliSetupError("USERPROFILE must be an absolute path")
    return _absolute(candidate / ".copilot")


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _normalized_utf8_sha256(content: bytes) -> str:
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise OwnedArtifactCollisionError(
            "legacy Local RAG Agent is not strict UTF-8"
        ) from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return _sha256(normalized.encode("utf-8"))


def _legacy_agent_retirements(
    copilot_home: Path, install_root: Path
) -> tuple[_Snapshot, ...]:
    roots: list[Path] = []
    seen_roots: set[str] = set()
    for candidate in (copilot_home, install_root):
        root = _absolute(candidate)
        identity = os.path.normcase(str(root))
        if identity not in seen_roots:
            roots.append(root)
            seen_roots.add(identity)
    retirements: list[_Snapshot] = []
    for root in roots:
        for name, known_hashes in LEGACY_AGENT_HASHES.items():
            path = root / "agents" / name
            _assert_safe_file(path, boundary=root, allow_missing=True)
            if not _lexists(path):
                continue
            content = path.read_bytes()
            digest = _normalized_utf8_sha256(content)
            if digest not in known_hashes:
                raise OwnedArtifactCollisionError(
                    "legacy Local RAG Agent is not an exact known product "
                    f"revision: {path}"
                )
            retirements.append(_Snapshot(path, root, True, content))
    return tuple(retirements)


def _root_path(kind: str, copilot_home: Path, install_root: Path) -> Path:
    if kind == COPILOT_ROOT:
        return _absolute(copilot_home)
    if kind == INSTALL_ROOT:
        return _absolute(install_root)
    raise OwnedArtifactCollisionError(f"unknown owned root: {kind}")


def _artifact_paths(
    copilot_home: Path, install_root: Path
) -> dict[tuple[str, str], Path]:
    home = _absolute(copilot_home)
    root = _absolute(install_root)
    result = {
        (COPILOT_ROOT, f"agents/{name}"): home / "agents" / name
        for name in AGENT_NAMES
    }
    result[(INSTALL_ROOT, f"{BUNDLE_NAME}/{LAUNCHER_NAME}")] = (
        root / BUNDLE_NAME / LAUNCHER_NAME
    )
    result[(INSTALL_ROOT, f"{BUNDLE_NAME}/{PINNED_CONFIG_NAME}")] = (
        root / BUNDLE_NAME / PINNED_CONFIG_NAME
    )
    return result


def _manifest_path(install_root: Path) -> Path:
    return _absolute(install_root) / BUNDLE_NAME / MANIFEST_NAME


def _launcher_path(install_root: Path) -> Path:
    return _absolute(install_root) / BUNDLE_NAME / LAUNCHER_NAME


def _assert_safe_file(path: Path, *, boundary: Path, allow_missing: bool) -> None:
    path = _absolute(path)
    boundary = _absolute(boundary)
    if not path.is_relative_to(boundary):
        raise OwnedArtifactCollisionError(f"path escapes boundary: {path}")
    if mcp_config._path_has_reparse(path, boundary):
        raise OwnedArtifactCollisionError(f"path crosses a reparse point: {path}")
    if _lexists(path):
        if not path.is_file() or mcp_config._is_reparse(path):
            raise OwnedArtifactCollisionError(
                f"owned path is not a regular file: {path}"
            )
    elif not allow_missing:
        raise OwnedArtifactCollisionError(f"owned artifact is missing: {path}")


def _validate_runtime(install_root: Path) -> Path:
    root = _absolute(install_root)
    if not root.is_dir() or mcp_config._is_reparse(root):
        raise CopilotCliSetupError("install root must be a regular directory")
    for path in (
        root / "rag" / "query" / ".venv" / "Scripts" / "python.exe",
        root / "rag" / "query" / "mcp_server.py",
    ):
        _assert_safe_file(path, boundary=root, allow_missing=False)
    return root


def _temporary_path(install_root: Path) -> Path:
    root = _absolute(install_root)
    temporary = _absolute(root / TEMPORARY_RELATIVE)
    if not temporary.is_relative_to(root):
        raise OwnedArtifactCollisionError(
            "expected temporary directory escapes install root"
        )
    return temporary


def _validate_temporary_directory(
    install_root: Path, *, allow_missing: bool
) -> Path:
    root = _absolute(install_root)
    temporary = _temporary_path(root)
    if mcp_config._path_has_reparse(temporary, root):
        raise OwnedArtifactCollisionError(
            "expected temporary directory crosses a reparse point"
        )
    current = root
    for part in temporary.relative_to(root).parts:
        current = current / part
        if not _lexists(current):
            if not allow_missing:
                raise OwnedArtifactCollisionError(
                    f"expected temporary directory is missing: {current}"
                )
            continue
        if not current.is_dir() or mcp_config._is_reparse(current):
            raise OwnedArtifactCollisionError(
                f"expected temporary path is not a regular directory: {current}"
            )
    return temporary


def _remove_created_temporary_directories(paths: tuple[Path, ...]) -> None:
    for path in reversed(paths):
        try:
            if (
                _lexists(path)
                and path.is_dir()
                and not mcp_config._is_reparse(path)
            ):
                path.rmdir()
        except OSError:
            # A non-empty or concurrently replaced directory is foreign state.
            # Never recurse into it during rollback.
            pass


def _ensure_temporary_directory(install_root: Path) -> tuple[Path, ...]:
    root = _absolute(install_root)
    temporary = _validate_temporary_directory(root, allow_missing=True)
    created: list[Path] = []
    current = root
    try:
        for part in temporary.relative_to(root).parts:
            current = current / part
            if not _lexists(current):
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                else:
                    created.append(current)
            if (
                not _lexists(current)
                or not current.is_dir()
                or mcp_config._is_reparse(current)
                or mcp_config._path_has_reparse(current, root)
            ):
                raise OwnedArtifactCollisionError(
                    f"expected temporary path is not a regular directory: {current}"
                )
        _validate_temporary_directory(root, allow_missing=False)
    except BaseException:
        _remove_created_temporary_directories(tuple(created))
        raise
    return tuple(created)


def _strict_json(content: bytes, *, label: str) -> object:
    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(f"unsupported JSON constant: {value}")

    try:
        return json.loads(
            content.decode("utf-8-sig", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise OwnedArtifactCollisionError(f"invalid {label}") from exc


def _template_bytes(name: str) -> bytes:
    path = TEMPLATE_ROOT / name
    if not path.is_file() or mcp_config._is_reparse(path):
        raise CopilotCliSetupError(f"CLI template is unavailable: {name}")
    return path.read_bytes()


def _pinned_config_bytes(install_root: Path) -> bytes:
    document = {
        mcp_config.CLI_ROOT_KEY: {
            mcp_config.SERVER_NAME: mcp_config.owned_cli_server_config(
                install_root
            )
        }
    }
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )


def _desired_artifacts(
    copilot_home: Path, install_root: Path
) -> dict[tuple[str, str], tuple[Path, bytes]]:
    paths = _artifact_paths(copilot_home, install_root)
    result: dict[tuple[str, str], tuple[Path, bytes]] = {}
    for name in AGENT_NAMES:
        key = (COPILOT_ROOT, f"agents/{name}")
        result[key] = (paths[key], _template_bytes(name))
    launcher_key = (INSTALL_ROOT, f"{BUNDLE_NAME}/{LAUNCHER_NAME}")
    result[launcher_key] = (paths[launcher_key], _template_bytes(LAUNCHER_NAME))
    pinned_key = (INSTALL_ROOT, f"{BUNDLE_NAME}/{PINNED_CONFIG_NAME}")
    result[pinned_key] = (
        paths[pinned_key],
        _pinned_config_bytes(install_root),
    )
    return result


def _decode_profile(content: bytes) -> tuple[bytes, str]:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise OwnedArtifactCollisionError("PowerShell profile must use UTF-8")
    bom = b"\xef\xbb\xbf" if content.startswith(b"\xef\xbb\xbf") else b""
    try:
        return bom, content[len(bom) :].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OwnedArtifactCollisionError("PowerShell profile must use UTF-8") from exc


def _profile_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _profile_block(launcher: Path, newline: str) -> str:
    quoted = str(_absolute(launcher)).replace("'", "''")
    return newline.join(
        (
            PROFILE_START,
            "function global:local-rag-copilot {",
            "    [CmdletBinding(PositionalBinding = $false)]",
            "    param(",
            "        [ValidateSet('savings', 'standard', 'thorough')]",
            "        [string]$Tier = 'standard',",
            "        [Parameter(ValueFromRemainingArguments = $true)]",
            "        [string[]]$CopilotArguments",
            "    )",
            f"    & '{quoted}' -Tier $Tier @CopilotArguments",
            "}",
            PROFILE_END,
        )
    )


def _profile_marker_span(text: str) -> tuple[int, int] | None:
    starts = text.count(PROFILE_START)
    ends = text.count(PROFILE_END)
    if starts == 0 and ends == 0:
        return None
    if starts != 1 or ends != 1:
        raise OwnedArtifactCollisionError("foreign or malformed profile marker collision")
    start = text.index(PROFILE_START)
    end = text.index(PROFILE_END)
    if end < start:
        raise OwnedArtifactCollisionError("foreign or malformed profile marker collision")
    return start, end + len(PROFILE_END)


def _profile_meta(
    path: Path,
    *,
    existed_before_install: bool,
    leading_separator_added: bool,
    newline: str,
    block: str,
) -> dict[str, object]:
    return {
        "path": str(_absolute(path)),
        "existed_before_install": existed_before_install,
        "leading_separator_added": leading_separator_added,
        "newline": newline,
        "block_sha256": _sha256(block.encode("utf-8")),
    }


def _verify_profile_owned(
    profile_path: Path, metadata: dict[str, object]
) -> None:
    boundary = _absolute(profile_path).parent
    _assert_safe_file(profile_path, boundary=boundary, allow_missing=False)
    _, text = _decode_profile(_absolute(profile_path).read_bytes())
    span = _profile_marker_span(text)
    if span is None:
        raise OwnedArtifactCollisionError("owned PowerShell profile block is missing")
    block = text[span[0] : span[1]]
    if _sha256(block.encode("utf-8")) != metadata["block_sha256"]:
        raise OwnedArtifactCollisionError("owned PowerShell profile block was modified")


def _render_profile(
    original: bytes,
    *,
    profile_path: Path,
    launcher_path: Path,
    action: str,
    existed_before_install: bool,
    existing_metadata: dict[str, object] | None,
) -> tuple[bytes, dict[str, object]]:
    bom, text = _decode_profile(original)
    span = _profile_marker_span(text)
    if existing_metadata is None and span is not None:
        raise OwnedArtifactCollisionError("foreign PowerShell profile marker collision")
    if existing_metadata is not None:
        newline = str(existing_metadata["newline"])
        existed_before = bool(existing_metadata["existed_before_install"])
        leading_added = bool(existing_metadata["leading_separator_added"])
    else:
        newline = _profile_newline(text)
        existed_before = existed_before_install
        leading_added = bool(text) and not text.endswith(("\n", "\r"))
    block = _profile_block(launcher_path, newline)
    if span is None:
        leading_added = bool(text) and not text.endswith(("\n", "\r"))
        separator = newline if leading_added else ""
        rendered = text + separator + block + newline
    else:
        current = text[span[0] : span[1]]
        if action == "install" and existing_metadata is not None:
            if _sha256(current.encode("utf-8")) != existing_metadata["block_sha256"]:
                raise OwnedArtifactCollisionError("owned PowerShell profile block was modified")
        rendered = text[: span[0]] + block + text[span[1] :]
    metadata = _profile_meta(
        profile_path,
        existed_before_install=existed_before,
        leading_separator_added=leading_added,
        newline=newline,
        block=block,
    )
    return bom + rendered.encode("utf-8"), metadata


def _remove_profile_block(original: bytes, metadata: dict[str, object]) -> bytes:
    bom, text = _decode_profile(original)
    span = _profile_marker_span(text)
    if span is None:
        raise OwnedArtifactCollisionError("owned PowerShell profile block is missing")
    block = text[span[0] : span[1]]
    if _sha256(block.encode("utf-8")) != metadata["block_sha256"]:
        raise OwnedArtifactCollisionError("owned PowerShell profile block was modified")
    start, end = span
    newline = str(metadata["newline"])
    if bool(metadata["leading_separator_added"]):
        if start < len(newline) or text[start - len(newline) : start] != newline:
            raise OwnedArtifactCollisionError("owned profile separator was modified")
        start -= len(newline)
    if text[end : end + len(newline)] != newline:
        raise OwnedArtifactCollisionError("owned profile terminator was modified")
    end += len(newline)
    return bom + (text[:start] + text[end:]).encode("utf-8")


def _vscode_identity(
    path: Path | None, *, existed_before_install: bool = False
) -> dict[str, object] | None:
    if path is None:
        return None
    return {
        "path": str(_absolute(path)),
        "existed_before_install": existed_before_install,
    }


def _manifest_document(
    artifacts: dict[tuple[str, str], tuple[Path, bytes]],
    *,
    copilot_home: Path,
    install_root: Path,
    config_existed_before_install: bool,
    profile: dict[str, object],
    vscode_mcp_config: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "schema": SCHEMA_VERSION,
        "server": mcp_config.SERVER_NAME,
        "copilot_home": str(_absolute(copilot_home)),
        "install_root": str(_absolute(install_root)),
        "config_existed_before_install": config_existed_before_install,
        "profile": profile,
        "vscode_mcp_config": vscode_mcp_config,
        "artifacts": [
            {
                "root": kind,
                "path": relative,
                "bytes": len(content),
                "sha256": _sha256(content),
            }
            for (kind, relative), (_, content) in sorted(artifacts.items())
        ],
    }


def _manifest_bytes(document: dict[str, object]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _load_manifest(
    copilot_home: Path,
    install_root: Path,
    profile_path: Path,
    vscode_mcp_config: Path | None,
) -> dict[str, object]:
    home = _absolute(copilot_home)
    root = _absolute(install_root)
    path = _manifest_path(root)
    _assert_safe_file(path, boundary=root, allow_missing=False)
    parsed = _strict_json(path.read_bytes(), label="owned manifest")
    if not isinstance(parsed, dict):
        raise OwnedArtifactCollisionError("owned manifest must be an object")
    if set(parsed) != {
        "schema",
        "server",
        "copilot_home",
        "install_root",
        "config_existed_before_install",
        "profile",
        "vscode_mcp_config",
        "artifacts",
    }:
        raise OwnedArtifactCollisionError("owned manifest fields are not exact")
    if (
        parsed["schema"] != SCHEMA_VERSION
        or parsed["server"] != mcp_config.SERVER_NAME
        or type(parsed["config_existed_before_install"]) is not bool
        or os.path.normcase(str(parsed["copilot_home"]))
        != os.path.normcase(str(home))
        or os.path.normcase(str(parsed["install_root"]))
        != os.path.normcase(str(root))
    ):
        raise OwnedArtifactCollisionError("owned manifest identity mismatch")
    profile = parsed["profile"]
    if (
        not isinstance(profile, dict)
        or set(profile)
        != {
            "path",
            "existed_before_install",
            "leading_separator_added",
            "newline",
            "block_sha256",
        }
        or os.path.normcase(str(profile["path"]))
        != os.path.normcase(str(_absolute(profile_path)))
        or type(profile["existed_before_install"]) is not bool
        or type(profile["leading_separator_added"]) is not bool
        or profile["newline"] not in {"\n", "\r\n"}
        or not isinstance(profile["block_sha256"], str)
        or len(profile["block_sha256"]) != 64
    ):
        raise OwnedArtifactCollisionError("owned manifest profile identity mismatch")
    vscode = parsed["vscode_mcp_config"]
    if vscode_mcp_config is None:
        if vscode is not None:
            raise OwnedArtifactCollisionError("owned manifest VS Code target mismatch")
    elif (
        not isinstance(vscode, dict)
        or set(vscode) != {"path", "existed_before_install"}
        or os.path.normcase(str(vscode["path"]))
        != os.path.normcase(str(_absolute(vscode_mcp_config)))
        or type(vscode["existed_before_install"]) is not bool
    ):
        raise OwnedArtifactCollisionError("owned manifest VS Code target mismatch")
    entries = parsed["artifacts"]
    expected_paths = set(_artifact_paths(home, root))
    if not isinstance(entries, list) or len(entries) != len(expected_paths):
        raise OwnedArtifactCollisionError("owned manifest artifact set is not exact")
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "root",
            "path",
            "bytes",
            "sha256",
        }:
            raise OwnedArtifactCollisionError("owned manifest artifact is invalid")
        key = (entry["root"], entry["path"])
        if key not in expected_paths or key in seen:
            raise OwnedArtifactCollisionError("owned manifest path is invalid")
        seen.add(key)
        if (
            type(entry["bytes"]) is not int
            or entry["bytes"] < 0
            or not isinstance(entry["sha256"], str)
            or len(entry["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in entry["sha256"])
        ):
            raise OwnedArtifactCollisionError("owned manifest hash is invalid")
    return parsed


def _manifest_entries(
    document: dict[str, object]
) -> dict[tuple[str, str], dict[str, object]]:
    return {
        (str(entry["root"]), str(entry["path"])): entry
        for entry in document["artifacts"]  # type: ignore[union-attr]
    }


def _verify_owned_artifacts(
    copilot_home: Path, install_root: Path, manifest: dict[str, object]
) -> None:
    home = _absolute(copilot_home)
    root = _absolute(install_root)
    entries = _manifest_entries(manifest)
    for key, path in _artifact_paths(home, root).items():
        boundary = _root_path(key[0], home, root)
        _assert_safe_file(path, boundary=boundary, allow_missing=False)
        content = path.read_bytes()
        entry = entries[key]
        if len(content) != entry["bytes"] or _sha256(content) != entry["sha256"]:
            raise OwnedArtifactCollisionError(
                f"owned artifact hash mismatch: {key[0]}:{key[1]}"
            )


def _snapshot(path: Path, *, boundary: Path) -> _Snapshot:
    _assert_safe_file(path, boundary=boundary, allow_missing=True)
    existed = path.is_file()
    return _Snapshot(path, boundary, existed, path.read_bytes() if existed else b"")


def _atomic_write_owned_bytes(path: Path, content: bytes, *, boundary: Path) -> None:
    path = _absolute(path)
    boundary = _absolute(boundary)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mcp_config._path_has_reparse(path, boundary):
        raise OwnedArtifactCollisionError(f"owned path crosses a reparse point: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != content:
            raise OSError("owned artifact temporary readback mismatch")
        if path.is_file():
            shutil.copymode(path, temporary)
        if mcp_config._path_has_reparse(path, boundary):
            raise OwnedArtifactCollisionError(f"owned path crosses a reparse point: {path}")
        os.replace(temporary, path)
        if path.read_bytes() != content:
            raise OSError("owned artifact final readback mismatch")
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def _restore(snapshots: tuple[_Snapshot, ...]) -> None:
    errors: list[str] = []
    for snapshot in reversed(snapshots):
        try:
            _assert_safe_file(snapshot.path, boundary=snapshot.boundary, allow_missing=True)
            if snapshot.existed:
                _atomic_write_owned_bytes(
                    snapshot.path, snapshot.content, boundary=snapshot.boundary
                )
            elif snapshot.path.is_file():
                snapshot.path.unlink()
        except (OSError, ValueError, CopilotCliSetupError) as exc:
            errors.append(f"{snapshot.path.name}:{type(exc).__name__}")
    if errors:
        raise CopilotCliSetupError(
            "Agent003 CLI transaction rollback failed: " + ",".join(errors)
        )


def _write_and_readback(path: Path, content: bytes, *, boundary: Path) -> None:
    _assert_safe_file(path, boundary=boundary, allow_missing=True)
    if path.is_file() and path.read_bytes() == content:
        return
    _atomic_write_owned_bytes(path, content, boundary=boundary)
    _assert_safe_file(path, boundary=boundary, allow_missing=False)
    if path.read_bytes() != content:
        raise CopilotCliSetupError(f"atomic readback mismatch: {path}")


def _read_config(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise CopilotCliSetupError("MCP configuration is not UTF-8") from exc


def _verify_configured_cli(copilot_home: Path, install_root: Path) -> None:
    path = _absolute(copilot_home) / mcp_config.MCP_CONFIG_NAME
    text = _read_config(path)
    if mcp_config.patch_cli_mcp_config(text, install_root) != text:
        raise CopilotCliSetupError("CLI MCP configuration readback is not pinned")


def _verify_configured_vscode(path: Path) -> None:
    text = _read_config(_absolute(path))
    if mcp_config.patch_vscode_mcp_config(text) != text:
        raise CopilotCliSetupError("VS Code MCP configuration readback is not pinned")


def _config_is_semantically_empty(text: str, root_key: str) -> bool:
    document, _ = mcp_config._JsoncLexer(text.lstrip("\ufeff")).document()
    remaining = dict(document)
    servers = remaining.get(root_key)
    if isinstance(servers, dict) and not servers:
        remaining.pop(root_key)
    return not remaining


def _cleanup_empty_directories(copilot_home: Path, install_root: Path) -> None:
    for path in (
        _absolute(install_root) / BUNDLE_NAME,
        _absolute(copilot_home) / "agents",
        _absolute(copilot_home),
    ):
        with contextlib.suppress(OSError):
            path.rmdir()


def _transaction_snapshots(
    copilot_home: Path,
    install_root: Path,
    profile_path: Path,
    vscode_mcp_config: Path | None,
) -> tuple[_Snapshot, ...]:
    home = _absolute(copilot_home)
    root = _absolute(install_root)
    profile = _absolute(profile_path)
    snapshots = [_snapshot(home / mcp_config.MCP_CONFIG_NAME, boundary=home)]
    if vscode_mcp_config is not None:
        vscode = _absolute(vscode_mcp_config)
        snapshots.append(_snapshot(vscode, boundary=vscode.parent))
    for key, path in _artifact_paths(home, root).items():
        snapshots.append(_snapshot(path, boundary=_root_path(key[0], home, root)))
    snapshots.append(_snapshot(_manifest_path(root), boundary=root))
    snapshots.append(_snapshot(profile, boundary=profile.parent))
    return tuple(snapshots)


def _validate_distinct_targets(
    copilot_home: Path,
    install_root: Path,
    profile_path: Path,
    vscode_mcp_config: Path | None,
) -> None:
    home = _absolute(copilot_home)
    root = _absolute(install_root)
    paths = [
        home / mcp_config.MCP_CONFIG_NAME,
        *_artifact_paths(home, root).values(),
        _manifest_path(root),
        _absolute(profile_path),
    ]
    if vscode_mcp_config is not None:
        paths.append(_absolute(vscode_mcp_config))
    normalized = [os.path.normcase(str(_absolute(path))) for path in paths]
    if len(set(normalized)) != len(normalized):
        raise CopilotCliSetupError("Agent003 CLI managed targets must be distinct")


def install_or_repair(
    action: str,
    copilot_home: Path,
    *,
    install_root: Path | None = None,
    profile_path: Path,
    vscode_mcp_config: Path | None = None,
) -> dict[str, object]:
    if action not in {"install", "repair"}:
        raise ValueError("action must be install or repair")
    home = _absolute(copilot_home)
    root = _validate_runtime(install_root or home)
    profile = _absolute(profile_path)
    vscode = _absolute(vscode_mcp_config) if vscode_mcp_config is not None else None
    _validate_distinct_targets(home, root, profile, vscode)
    legacy_agent_retirements = _legacy_agent_retirements(home, root)
    temporary = _validate_temporary_directory(root, allow_missing=True)
    temporary_missing = not temporary.is_dir()
    artifacts = _desired_artifacts(home, root)
    manifest_path = _manifest_path(root)
    existing_manifest: dict[str, object] | None = None
    if manifest_path.is_file():
        existing_manifest = _load_manifest(home, root, profile, vscode)
        if action == "install":
            _verify_owned_artifacts(home, root, existing_manifest)
            _verify_profile_owned(profile, existing_manifest["profile"])  # type: ignore[arg-type]
    else:
        if action == "repair":
            raise OwnedArtifactCollisionError("repair requires an owned manifest")
        for key, (path, _) in artifacts.items():
            boundary = _root_path(key[0], home, root)
            _assert_safe_file(path, boundary=boundary, allow_missing=True)
            if _lexists(path):
                raise OwnedArtifactCollisionError(
                    f"unowned artifact already exists: {key[0]}:{key[1]}"
                )
    _assert_safe_file(profile, boundary=profile.parent, allow_missing=True)
    profile_existed = profile.is_file()
    original_profile = profile.read_bytes() if profile_existed else b""
    profile_bytes, profile_metadata = _render_profile(
        original_profile,
        profile_path=profile,
        launcher_path=_launcher_path(root),
        action=action,
        existed_before_install=profile_existed,
        existing_metadata=(
            existing_manifest["profile"] if existing_manifest is not None else None
        ),  # type: ignore[arg-type]
    )
    config_path = home / mcp_config.MCP_CONFIG_NAME
    config_existed = (
        bool(existing_manifest["config_existed_before_install"])
        if existing_manifest is not None
        else config_path.is_file()
    )
    vscode_existed = (
        bool(existing_manifest["vscode_mcp_config"]["existed_before_install"])  # type: ignore[index]
        if existing_manifest is not None and vscode is not None
        else bool(vscode is not None and vscode.is_file())
    )
    manifest = _manifest_document(
        artifacts,
        copilot_home=home,
        install_root=root,
        config_existed_before_install=config_existed,
        profile=profile_metadata,
        vscode_mcp_config=_vscode_identity(
            vscode, existed_before_install=vscode_existed
        ),
    )
    desired_manifest_bytes = _manifest_bytes(manifest)
    changed = (
        existing_manifest is None
        or original_profile != profile_bytes
        or temporary_missing
        or bool(legacy_agent_retirements)
    )
    if existing_manifest is not None:
        changed = changed or manifest_path.read_bytes() != desired_manifest_bytes
        changed = changed or any(
            not path.is_file() or path.read_bytes() != content
            for path, content in artifacts.values()
        )
    snapshots = _transaction_snapshots(
        home,
        root,
        profile,
        vscode,
    )
    created_temporary_directories: tuple[Path, ...] = ()
    retired_legacy_agents: list[_Snapshot] = []
    try:
        home.mkdir(parents=True, exist_ok=True)
        if vscode is None:
            config_result = mcp_config.configure_mcp(
                home, install_root=root, create_backup=False
            )
        else:
            config_result = mcp_config.configure_mcp_targets(
                home, vscode, install_root=root, create_backup=False
            )
        changed = changed or config_result["status"] != "already_configured"
        _verify_configured_cli(home, root)
        if vscode is not None:
            _verify_configured_vscode(vscode)
        for snapshot in legacy_agent_retirements:
            path = snapshot.path
            _assert_safe_file(
                path, boundary=snapshot.boundary, allow_missing=False
            )
            if path.read_bytes() != snapshot.content:
                raise OwnedArtifactCollisionError(
                    "legacy Local RAG Agent changed during setup: "
                    f"install_root:agents/{path.name}"
                )
            path.unlink()
            if _lexists(path):
                raise CopilotCliSetupError(
                    f"legacy Local RAG Agent retirement failed: {path.name}"
                )
            retired_legacy_agents.append(snapshot)
        for key, (path, content) in sorted(artifacts.items()):
            _write_and_readback(
                path, content, boundary=_root_path(key[0], home, root)
            )
        _write_and_readback(profile, profile_bytes, boundary=profile.parent)
        created_temporary_directories = _ensure_temporary_directory(root)
        _write_and_readback(manifest_path, desired_manifest_bytes, boundary=root)
        readback = _load_manifest(home, root, profile, vscode)
        if readback != manifest:
            raise CopilotCliSetupError("owned manifest readback mismatch")
        _verify_owned_artifacts(home, root, readback)
        _verify_profile_owned(profile, readback["profile"])  # type: ignore[arg-type]
    except BaseException as exc:
        try:
            _remove_created_temporary_directories(created_temporary_directories)
            _restore(snapshots + tuple(retired_legacy_agents))
            _cleanup_empty_directories(home, root)
        except BaseException as rollback_exc:
            raise CopilotCliSetupError(
                f"setup failed and rollback failed: {type(rollback_exc).__name__}"
            ) from exc
        raise
    status = (
        "installed"
        if existing_manifest is None
        else "repaired"
        if action == "repair"
        else "updated"
        if changed
        else "already_installed"
    )
    return {
        "status": status,
        "copilot_home": str(home),
        "install_root": str(root),
        "profile": str(profile),
        "vscode_mcp_config": str(vscode) if vscode is not None else None,
        "manifest": str(manifest_path),
        "config": config_result,
        "artifacts": [f"{key[0]}:{key[1]}" for key in sorted(artifacts)],
        "retired_legacy_agents": [
            snapshot.path.name for snapshot in legacy_agent_retirements
        ],
    }


def uninstall(
    copilot_home: Path,
    *,
    install_root: Path | None = None,
    profile_path: Path,
    vscode_mcp_config: Path | None = None,
) -> dict[str, object]:
    home = _absolute(copilot_home)
    root = _absolute(install_root or home)
    profile = _absolute(profile_path)
    vscode = _absolute(vscode_mcp_config) if vscode_mcp_config is not None else None
    _validate_distinct_targets(home, root, profile, vscode)
    manifest = _load_manifest(home, root, profile, vscode)
    _verify_owned_artifacts(home, root, manifest)
    _verify_profile_owned(profile, manifest["profile"])  # type: ignore[arg-type]
    snapshots = _transaction_snapshots(home, root, profile, vscode)
    config_path = home / mcp_config.MCP_CONFIG_NAME
    try:
        if vscode is None:
            config_result = mcp_config.unconfigure_mcp(
                home, install_root=root, create_backup=False
            )
        else:
            config_result = mcp_config.unconfigure_mcp_targets(
                home, vscode, install_root=root, create_backup=False
            )
        for key, path in _artifact_paths(home, root).items():
            _assert_safe_file(
                path, boundary=_root_path(key[0], home, root), allow_missing=False
            )
            path.unlink()
        _assert_safe_file(_manifest_path(root), boundary=root, allow_missing=False)
        _manifest_path(root).unlink()
        profile_result = _remove_profile_block(
            profile.read_bytes(), manifest["profile"]  # type: ignore[arg-type]
        )
        if manifest["profile"]["existed_before_install"] or profile_result:  # type: ignore[index]
            _write_and_readback(profile, profile_result, boundary=profile.parent)
        else:
            profile.unlink()
        if (
            not manifest["config_existed_before_install"]
            and config_path.is_file()
            and _config_is_semantically_empty(
                _read_config(config_path), mcp_config.CLI_ROOT_KEY
            )
        ):
            _assert_safe_file(config_path, boundary=home, allow_missing=False)
            config_path.unlink()
        elif config_path.is_file():
            mcp_config._JsoncLexer(_read_config(config_path)).document()
        if vscode is not None:
            vscode_meta = manifest["vscode_mcp_config"]
            if (
                not vscode_meta["existed_before_install"]  # type: ignore[index]
                and vscode.is_file()
                and _config_is_semantically_empty(
                    _read_config(vscode), mcp_config.VSCODE_ROOT_KEY
                )
            ):
                _assert_safe_file(vscode, boundary=vscode.parent, allow_missing=False)
                vscode.unlink()
            elif vscode.is_file():
                mcp_config._JsoncLexer(_read_config(vscode)).document()
    except BaseException as exc:
        try:
            _restore(snapshots)
        except BaseException as rollback_exc:
            raise CopilotCliSetupError(
                f"uninstall failed and rollback failed: {type(rollback_exc).__name__}"
            ) from exc
        raise
    _cleanup_empty_directories(home, root)
    return {
        "status": "uninstalled",
        "copilot_home": str(home),
        "install_root": str(root),
        "profile": str(profile),
        "vscode_mcp_config": str(vscode) if vscode is not None else None,
        "config": config_result,
    }


def retire(
    copilot_home: Path,
    *,
    install_root: Path | None = None,
    profile_path: Path,
    vscode_mcp_config: Path | None = None,
) -> dict[str, object]:
    """Remove a manifest-owned Agent003 integration, if one is installed.

    Retirement is intentionally manifest-gated.  A fresh installation must not
    infer ownership from filenames or edit MCP configuration, Agent files, or a
    PowerShell profile left by another owner.
    """
    home = _absolute(copilot_home)
    root = _absolute(install_root or home)
    manifest_path = _manifest_path(root)
    _assert_safe_file(manifest_path, boundary=root, allow_missing=True)
    if not _lexists(manifest_path):
        return {
            "status": "absent",
            "copilot_home": str(home),
            "install_root": str(root),
            "manifest": str(manifest_path),
        }

    result = uninstall(
        home,
        install_root=root,
        profile_path=profile_path,
        vscode_mcp_config=vscode_mcp_config,
    )
    result["status"] = "retired"
    result["manifest"] = str(manifest_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install, repair, uninstall, or retire Agent003 Copilot CLI files."
        )
    )
    parser.add_argument(
        "action", choices=("install", "repair", "uninstall", "retire")
    )
    parser.add_argument("--copilot-home", type=Path)
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--profile-path", type=Path, required=True)
    parser.add_argument("--vscode-mcp-config", type=Path)
    args = parser.parse_args(argv)
    copilot_home = args.copilot_home or default_copilot_home()
    try:
        if args.action == "retire":
            result = retire(
                copilot_home,
                install_root=args.install_root,
                profile_path=args.profile_path,
                vscode_mcp_config=args.vscode_mcp_config,
            )
        elif args.action == "uninstall":
            result = uninstall(
                copilot_home,
                install_root=args.install_root,
                profile_path=args.profile_path,
                vscode_mcp_config=args.vscode_mcp_config,
            )
        else:
            result = install_or_repair(
                args.action,
                copilot_home,
                install_root=args.install_root,
                profile_path=args.profile_path,
                vscode_mcp_config=args.vscode_mcp_config,
            )
    except (
        CopilotCliSetupError,
        mcp_config.McpConfigCollisionError,
        ValueError,
        OSError,
    ) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
