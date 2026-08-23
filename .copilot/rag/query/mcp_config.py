from __future__ import annotations
import argparse, contextlib, json, os, shutil, tempfile, uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone; from pathlib import Path
SERVER_NAME = "localragagent003"; MCP_CONFIG_NAME = "mcp-config.json"
CLI_ROOT_KEY = "mcpServers"; VSCODE_ROOT_KEY = "servers"
CLI_TOOLS = ("local_rag_search", "local_rag_get_evidence")
CLI_TOOL_TIMEOUT_MS = 180_000
RUNTIME_COMMAND = "${userHome}\\.copilot\\rag\\query\\.venv\\Scripts\\python.exe"; SERVER_SCRIPT = "${userHome}\\.copilot\\rag\\query\\mcp_server.py"
RAG_ROOT = "${userHome}\\.copilot\\rag"
_LEGACY_SERVER_NAME = "localRagAgent003"
_LEGACY_SERVER_SCRIPT = "${userHome}\\.copilot\\rag\\query\\agent003_thin_mcp_server.py"
class McpConfigCollisionError(ValueError): pass
@dataclass(frozen=True)
class _Property:
    key: str; key_start: int; key_end: int; value_start: int; value_end: int; value: object
@dataclass(frozen=True)
class _ObjectView:
    start: int; end: int; properties: tuple[_Property, ...]; trailing_comma: bool
    def property(self, key: str) -> _Property | None:
        return next((item for item in self.properties if item.key == key), None)
def _reject_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")
def _reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in items:
        if key in output:
            raise ValueError(f"duplicate object key: {key}")
        output[key] = value
    return output
def _jsonc_text(text: str) -> tuple[str, str]:
    """Return comment-masked and trailing-comma-masked equal-length text."""
    characters = list(text)
    index, quoted, escaped = 0, False, False
    while index < len(text):
        character = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            index += 1
            continue
        if character == '"':
            quoted = True
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index + 2)
            end = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated JSONC block comment")
            end += 2
        else:
            index += 1
            continue
        for offset in range(index, end):
            if characters[offset] not in "\r\n":
                characters[offset] = " "
        index = end
    comments_masked = "".join(characters)
    sanitized = characters.copy()
    quoted, escaped = False, False
    for index, character in enumerate(comments_masked):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
            continue
        if character != ",":
            continue
        following = index + 1
        while following < len(comments_masked) and comments_masked[following].isspace(): following += 1
        if following < len(comments_masked) and comments_masked[following] in "}]":
            sanitized[index] = " "
    return comments_masked, "".join(sanitized)
class _JsoncLexer:
    """Parse JSONC and retain the direct property spans used for patching."""
    def __init__(self, text: str) -> None:
        self.text = text
        self.masked, self.clean = _jsonc_text(text)
        self.decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    def skip(self, index: int) -> int:
        while index < len(self.clean) and self.clean[index].isspace():
            index += 1
        return index
    def decode(self, index: int) -> tuple[object, int]:
        try:
            return self.decoder.raw_decode(self.clean, index)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid JSONC") from exc
    def document(self) -> tuple[dict[str, object], _ObjectView]:
        start = self.skip(0)
        value, end = self.decode(start)
        if self.skip(end) != len(self.clean):
            raise ValueError("unexpected trailing JSONC content")
        if not isinstance(value, dict):
            raise ValueError("MCP configuration root must be an object")
        return value, self.object_view(start, value)
    def object_view(self, start: int, value: dict[str, object] | None = None) -> _ObjectView:
        if value is None:
            value, _ = self.decode(start)
        if not isinstance(value, dict) or self.clean[start] != "{":
            raise ValueError("MCP servers must be an object")
        properties: list[_Property] = []
        index = self.skip(start + 1)
        if index < len(self.clean) and self.clean[index] == "}":
            return _ObjectView(start, index, (), False)
        while True:
            key_start = index
            key, key_end = self.decode(index)
            if not isinstance(key, str):
                raise ValueError("JSONC object keys must be strings")
            index = self.skip(key_end)
            if index >= len(self.clean) or self.clean[index] != ":":
                raise ValueError("missing JSONC property colon")
            value_start = self.skip(index + 1)
            item, value_end = self.decode(value_start)
            properties.append(_Property(key, key_start, key_end, value_start, value_end, item))
            index = self.skip(value_end)
            if index >= len(self.clean):
                raise ValueError("unterminated JSONC object")
            if self.clean[index] == "}":
                trailing = "," in self.masked[value_end:index]
                return _ObjectView(start, index, tuple(properties), trailing)
            if self.clean[index] != ",":
                raise ValueError("missing JSONC property comma")
            index = self.skip(index + 1)
def _vscode_server(script: str) -> dict[str, object]:
    return {"type": "stdio", "command": RUNTIME_COMMAND, "args": ["-B", script, "--rag-root", RAG_ROOT], "cwd": RAG_ROOT}
def owned_vscode_server_config() -> dict[str, object]:
    return _vscode_server(SERVER_SCRIPT)
def owned_server_config() -> dict[str, object]:
    """Compatibility alias for the VS Code MCP server definition."""
    return owned_vscode_server_config()
def owned_cli_server_config(install_root: Path) -> dict[str, object]:
    root = Path(os.path.abspath(install_root))
    rag_root = root / "rag"
    python = rag_root / "query" / ".venv" / "Scripts" / "python.exe"
    temporary = rag_root / "query" / "run" / "tmp"
    spool_root = temporary / "GitHubCopilotLocalRAG" / "results"
    return {
        "type": "local",
        "command": str(python),
        "args": [
            "-B",
            str(rag_root / "query" / "mcp_server.py"),
            "--rag-root",
            str(rag_root),
            "--python",
            str(python),
            "--spool-root",
            str(spool_root),
        ],
        "env": {"TEMP": str(temporary), "TMP": str(temporary)},
        "tools": list(CLI_TOOLS),
        "timeout": CLI_TOOL_TIMEOUT_MS,
    }
def _legacy_owned_server_configs() -> tuple[dict[str, object], ...]:
    return (_vscode_server(_LEGACY_SERVER_SCRIPT),)
def _render(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
def _insert(text: str, view: _ObjectView, key: str, value: str) -> str:
    rendered = f"{json.dumps(key, ensure_ascii=False)}: {value}"
    newline = "\r\n" if "\r\n" in text else "\n"
    line = text.rfind("\n", view.start, view.end) + 1
    close_indent = text[line:view.end]
    own_line = "\n" in text[view.start:view.end] and not close_indent.strip()
    if own_line:
        insert_at = line
        indent = close_indent + "  "
        if view.properties:
            first = view.properties[0].key_start
            candidate = text[text.rfind("\n", view.start, first) + 1:first]
            indent = candidate if candidate and not candidate.strip() else indent
        insertion = indent + rendered + ("," if view.trailing_comma else "") + newline
    else:
        insert_at = view.end
        while insert_at > view.start + 1 and text[insert_at - 1] in " \t":
            insert_at -= 1
        insertion = (" " if insert_at > view.start + 1 else "") + rendered
        insertion += "," if view.trailing_comma else ""
    if view.properties and not view.trailing_comma:
        comma = view.properties[-1].value_end
        text = text[:comma] + "," + text[comma:]
        insert_at += comma <= insert_at
    return text[:insert_at] + insertion + text[insert_at:]
def _replace_owned(
    text: str, item: _Property, value: dict[str, object], *, rename: bool
) -> str:
    text = text[:item.value_start] + _render(value) + text[item.value_end:]
    if rename:
        text = text[:item.key_start] + json.dumps(SERVER_NAME) + text[item.key_end:]
    return text


def _patch_server_config(
    text: str,
    *,
    root_key: str,
    owned: dict[str, object],
    legacy_configs: tuple[dict[str, object], ...] = (),
    accept_legacy_name: bool = False,
) -> str:
    bom, body = ("\ufeff", text[1:]) if text.startswith("\ufeff") else ("", text)
    if not body.strip():
        ending = "\r\n" if body.endswith("\r\n") else "\n" if body.endswith("\n") else ""
        body = "{}" + ending
    lexer = _JsoncLexer(body)
    _, root = lexer.document()
    servers = root.property(root_key)
    if servers is None:
        return bom + _insert(body, root, root_key, _render({SERVER_NAME: owned}))
    view = lexer.object_view(servers.value_start)
    current = view.property(SERVER_NAME)
    legacy = view.property(_LEGACY_SERVER_NAME) if accept_legacy_name else None
    accepted = (owned,) + legacy_configs
    if current is not None and current.value not in accepted:
        raise McpConfigCollisionError(f"MCP server name is already owned: {SERVER_NAME}")
    if legacy is not None and legacy.value not in accepted:
        raise McpConfigCollisionError(f"MCP server name is already owned: {_LEGACY_SERVER_NAME}")
    if current is not None and legacy is not None:
        raise McpConfigCollisionError("both Local RAG MCP server names are present")
    if legacy is not None:
        return bom + _replace_owned(body, legacy, owned, rename=True)
    if current is None:
        return bom + _insert(body, view, SERVER_NAME, _render(owned))
    return text if current.value == owned else bom + _replace_owned(body, current, owned, rename=False)


def patch_cli_mcp_config(text: str, install_root: Path) -> str:
    patched = _patch_server_config(
        text,
        root_key=CLI_ROOT_KEY,
        owned=owned_cli_server_config(install_root),
    )
    return _remove_known_vscode_entries(patched)


def patch_vscode_mcp_config(text: str) -> str:
    return _patch_server_config(
        text,
        root_key=VSCODE_ROOT_KEY,
        owned=owned_vscode_server_config(),
        legacy_configs=_legacy_owned_server_configs(),
        accept_legacy_name=True,
    )


def patch_mcp_config(text: str) -> str:
    """Compatibility alias for the VS Code `servers` configuration."""
    return patch_vscode_mcp_config(text)


def _remove_known_vscode_entries(text: str) -> str:
    bom, body = ("\ufeff", text[1:]) if text.startswith("\ufeff") else ("", text)
    accepted = (owned_vscode_server_config(),) + _legacy_owned_server_configs()
    for name in (SERVER_NAME, _LEGACY_SERVER_NAME):
        lexer = _JsoncLexer(body)
        _, root = lexer.document()
        servers = root.property(VSCODE_ROOT_KEY)
        if servers is None:
            break
        view = lexer.object_view(servers.value_start)
        current = view.property(name)
        if current is None or current.value not in accepted:
            continue
        body = _remove_property(body, view, current)
    return bom + body


def _remove_property(text: str, view: _ObjectView, item: _Property) -> str:
    properties = list(view.properties)
    index = properties.index(item)
    masked, _ = _jsonc_text(text)
    line_start = text.rfind("\n", view.start, item.key_start) + 1
    own_line = not text[line_start:item.key_start].strip()

    comma_after = masked.find(",", item.value_end, view.end)
    has_comma_after = comma_after >= 0 and (
        index < len(properties) - 1 or view.trailing_comma
    )
    if has_comma_after:
        start = line_start if own_line else item.key_start
        end = comma_after + 1
        if own_line:
            line_end = text.find("\n", end, view.end + 1)
            if line_end >= 0 and not text[end:line_end].strip():
                end = line_end + 1
        else:
            while end < view.end and text[end] in " \t":
                end += 1
        output = text[:start] + text[end:]
    elif len(properties) == 1:
        start = line_start if own_line else item.key_start
        end = item.value_end
        if own_line:
            line_end = text.find("\n", end, view.end + 1)
            if line_end >= 0 and not text[end:line_end].strip():
                end = line_end + 1
        output = text[:start] + text[end:]
    else:
        previous = properties[index - 1]
        separator = masked.find(",", previous.value_end, item.key_start)
        if separator < 0:
            raise ValueError("missing JSONC property comma")
        start = line_start if own_line else item.key_start
        end = item.value_end
        if own_line:
            line_end = text.find("\n", end, view.end + 1)
            if line_end >= 0 and not text[end:line_end].strip():
                end = line_end + 1
        output = text[:start] + text[end:]
        output = output[:separator] + output[separator + 1:]
    _JsoncLexer(output).document()
    return output


def remove_cli_mcp_config(text: str, install_root: Path) -> str:
    bom, body = ("\ufeff", text[1:]) if text.startswith("\ufeff") else ("", text)
    if not body.strip():
        return text
    lexer = _JsoncLexer(body)
    _, root = lexer.document()
    servers = root.property(CLI_ROOT_KEY)
    if servers is None:
        return text
    view = lexer.object_view(servers.value_start)
    current = view.property(SERVER_NAME)
    if current is None:
        return text
    if current.value != owned_cli_server_config(install_root):
        raise McpConfigCollisionError(
            f"MCP server name is no longer owned: {SERVER_NAME}"
        )
    return bom + _remove_property(body, view, current)


def remove_vscode_mcp_config(text: str) -> str:
    bom, body = ("\ufeff", text[1:]) if text.startswith("\ufeff") else ("", text)
    if not body.strip():
        return text
    accepted = (owned_vscode_server_config(),) + _legacy_owned_server_configs()
    for name in (SERVER_NAME, _LEGACY_SERVER_NAME):
        lexer = _JsoncLexer(body)
        _, root = lexer.document()
        servers = root.property(VSCODE_ROOT_KEY)
        if servers is None:
            break
        view = lexer.object_view(servers.value_start)
        current = view.property(name)
        if current is None:
            continue
        if current.value not in accepted:
            raise McpConfigCollisionError(
                f"MCP server name is no longer owned: {name}"
            )
        body = _remove_property(body, view, current)
    return bom + body
def _lexists(path: Path) -> bool:
    return os.path.lexists(path)
def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return path.is_symlink() or bool(int(getattr(metadata, "st_file_attributes", 0) or 0) & 0x0400)
def _path_has_reparse(path: Path, boundary: Path) -> bool:
    boundary = Path(os.path.abspath(boundary))
    current = path if _lexists(path) else path.parent
    while True:
        if _lexists(current) and _is_reparse(current):
            return True
        if current == boundary:
            return False
        if current.parent == current or not current.is_relative_to(boundary):
            return True
        current = current.parent
def _atomic_write_bytes(path: Path, content: bytes, *, boundary: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _path_has_reparse(path, boundary):
        raise ValueError("MCP configuration target crosses a reparse point")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        readback = temporary.read_bytes()
        if readback != content:
            raise OSError("MCP configuration temporary readback mismatch")
        if readback.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise ValueError("MCP configuration must use UTF-8")
        try:
            decoded = readback.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("MCP configuration must use UTF-8") from exc
        _JsoncLexer(decoded).document()
        if path.is_file():
            shutil.copymode(path, temporary)
        if _path_has_reparse(path, boundary):
            raise ValueError("MCP configuration target crosses a reparse point")
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


@dataclass
class _ConfigPlan:
    kind: str
    path: Path
    boundary: Path
    existed: bool
    original_bytes: bytes
    patched_bytes: bytes
    status: str
    backup: Path | None = None


def _plan_mcp_config(
    kind: str,
    target: Path,
    *,
    boundary: Path,
    patcher: Callable[[str], str],
) -> _ConfigPlan:
    target = Path(os.path.abspath(target))
    boundary = Path(os.path.abspath(boundary))
    if _path_has_reparse(target, boundary):
        raise ValueError("MCP configuration target crosses a reparse point")
    if target.exists() and not target.is_file():
        raise ValueError("MCP configuration target is not a regular file")
    existed = target.is_file()
    original_bytes = target.read_bytes() if existed else b"{}\n"
    if original_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("MCP configuration must use UTF-8")
    try:
        original = original_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("MCP configuration must use UTF-8") from exc
    patched = patcher(original)
    if patched == original:
        patched_bytes = original_bytes
        status = "already_configured"
    else:
        patched_bytes = patched.encode("utf-8")
        if original_bytes.startswith(b"\xef\xbb\xbf"):
            patched_bytes = b"\xef\xbb\xbf" + patched_bytes
        status = "configured_on_disk"
    return _ConfigPlan(
        kind=kind,
        path=target,
        boundary=boundary,
        existed=existed,
        original_bytes=original_bytes,
        patched_bytes=patched_bytes,
        status=status,
    )


def _plan_result(plan: _ConfigPlan) -> dict[str, object]:
    return {
        "kind": plan.kind,
        "status": plan.status,
        "path": str(plan.path),
        "backup": str(plan.backup) if plan.backup else None,
    }


def _apply_plans(
    plans: tuple[_ConfigPlan, ...], *, create_backup: bool
) -> dict[str, object]:
    changed = tuple(
        plan for plan in plans if plan.status == "configured_on_disk"
    )
    created_backups: list[Path] = []
    written: list[_ConfigPlan] = []
    try:
        if create_backup:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            for plan in changed:
                if not plan.existed:
                    continue
                plan.backup = plan.path.with_name(
                    f"{plan.path.name}.local-rag-backup-{stamp}-{uuid.uuid4().hex[:8]}"
                )
                shutil.copy2(plan.path, plan.backup)
                created_backups.append(plan.backup)
        for plan in changed:
            _atomic_write_bytes(
                plan.path, plan.patched_bytes, boundary=plan.boundary
            )
            written.append(plan)
    except BaseException as exc:
        rollback_errors: list[str] = []
        for plan in reversed(written):
            try:
                if plan.existed:
                    _atomic_write_bytes(
                        plan.path, plan.original_bytes, boundary=plan.boundary
                    )
                elif plan.path.is_file():
                    if _path_has_reparse(plan.path, plan.boundary):
                        raise ValueError(
                            "MCP configuration target crosses a reparse point"
                        )
                    plan.path.unlink()
            except (OSError, ValueError) as rollback_exc:
                rollback_errors.append(
                    f"{plan.kind}:{type(rollback_exc).__name__}"
                )
        for backup in created_backups:
            with contextlib.suppress(OSError):
                backup.unlink(missing_ok=True)
        if rollback_errors:
            raise OSError(
                "MCP configuration transaction rollback failed: "
                + ",".join(rollback_errors)
            ) from exc
        raise
    status = (
        "configured_on_disk" if changed else "already_configured"
    )
    targets = [_plan_result(plan) for plan in plans]
    return {
        "status": status,
        "server": SERVER_NAME,
        "path": targets[0]["path"],
        "backup": targets[0]["backup"],
        "targets": targets,
    }


def configure_mcp(
    copilot_home: Path,
    *,
    install_root: Path | None = None,
    create_backup: bool = True,
) -> dict[str, object]:
    home = Path(os.path.abspath(copilot_home))
    product_root = Path(os.path.abspath(install_root or home))
    return _apply_plans(
        (
            _plan_mcp_config(
                "copilot_cli",
                home / MCP_CONFIG_NAME,
                boundary=home,
                patcher=lambda text: patch_cli_mcp_config(text, product_root),
            ),
        ),
        create_backup=create_backup,
    )


def configure_mcp_targets(
    copilot_home: Path,
    vscode_mcp_config: Path,
    *,
    install_root: Path | None = None,
    create_backup: bool = True,
) -> dict[str, object]:
    home = Path(os.path.abspath(copilot_home))
    product_root = Path(os.path.abspath(install_root or home))
    vscode_target = Path(os.path.abspath(vscode_mcp_config))
    copilot_target = home / MCP_CONFIG_NAME
    if os.path.normcase(copilot_target) == os.path.normcase(vscode_target):
        raise ValueError("MCP configuration targets must be distinct")
    plans = (
        _plan_mcp_config(
            "copilot_cli",
            copilot_target,
            boundary=home,
            patcher=lambda text: patch_cli_mcp_config(text, product_root),
        ),
        _plan_mcp_config(
            "vscode_default_profile",
            vscode_target,
            boundary=vscode_target.parent,
            patcher=patch_vscode_mcp_config,
        ),
    )
    return _apply_plans(plans, create_backup=create_backup)


def unconfigure_mcp_targets(
    copilot_home: Path,
    vscode_mcp_config: Path,
    *,
    install_root: Path | None = None,
    create_backup: bool = True,
) -> dict[str, object]:
    home = Path(os.path.abspath(copilot_home))
    product_root = Path(os.path.abspath(install_root or home))
    vscode_target = Path(os.path.abspath(vscode_mcp_config))
    copilot_target = home / MCP_CONFIG_NAME
    if os.path.normcase(copilot_target) == os.path.normcase(vscode_target):
        raise ValueError("MCP configuration targets must be distinct")
    plans = (
        _plan_mcp_config(
            "copilot_cli",
            copilot_target,
            boundary=home,
            patcher=lambda text: remove_cli_mcp_config(
                text, product_root
            ),
        ),
        _plan_mcp_config(
            "vscode_default_profile",
            vscode_target,
            boundary=vscode_target.parent,
            patcher=remove_vscode_mcp_config,
        ),
    )
    return _apply_plans(plans, create_backup=create_backup)


def unconfigure_mcp(
    copilot_home: Path,
    *,
    install_root: Path | None = None,
    create_backup: bool = True,
) -> dict[str, object]:
    home = Path(os.path.abspath(copilot_home))
    product_root = Path(os.path.abspath(install_root or home))
    return _apply_plans(
        (
            _plan_mcp_config(
                "copilot_cli",
                home / MCP_CONFIG_NAME,
                boundary=home,
                patcher=lambda text: remove_cli_mcp_config(
                    text, product_root
                ),
            ),
        ),
        create_backup=create_backup,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copilot-home", type=Path, default=Path.home() / ".copilot")
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--vscode-mcp-config", type=Path)
    parser.add_argument("--no-backup", action="store_true"); args = parser.parse_args(argv)
    try:
        if args.vscode_mcp_config is None:
            result = configure_mcp(
                args.copilot_home,
                install_root=args.install_root,
                create_backup=not args.no_backup,
            )
        else:
            result = configure_mcp_targets(
                args.copilot_home,
                args.vscode_mcp_config,
                install_root=args.install_root,
                create_backup=not args.no_backup,
            )
        code = 0
    except McpConfigCollisionError:
        path = Path(os.path.abspath(args.copilot_home)) / MCP_CONFIG_NAME
        result = {"status": "collision", "server": SERVER_NAME, "path": str(path), "backup": None}; code = 2
    except (OSError, UnicodeError, ValueError) as exc:
        result = {"status": "error", "server": SERVER_NAME, "error_kind": type(exc).__name__, "backup": None}; code = 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return code
if __name__ == "__main__":
    raise SystemExit(main())
