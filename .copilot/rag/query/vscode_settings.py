from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

class PolicyManualAction(ValueError):
    """A target policy is valid JSONC but must not be changed automatically."""


def _skip_ws_comments(text: str, index: int) -> int:
    size = len(text)
    while index < size:
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index + 2)
            return size if end < 0 else _skip_ws_comments(text, end + 1)
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated block comment")
            index = end + 2
            continue
        break
    return index


def _scan_string(text: str, index: int) -> int:
    quote = text[index]
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    raise ValueError("unterminated string")


def _scan_value(text: str, index: int) -> int:
    index = _skip_ws_comments(text, index)
    if index >= len(text):
        raise ValueError("missing value")
    if text[index] in {'\"', "'"}:
        return _scan_string(text, index)
    if text[index] in "[{":
        opening = text[index]
        closing = "]" if opening == "[" else "}"
        depth = 1
        index += 1
        while index < len(text):
            if text[index] in {'\"', "'"}:
                index = _scan_string(text, index)
                continue
            if text.startswith("//", index):
                end = text.find("\n", index + 2)
                index = len(text) if end < 0 else end + 1
                continue
            if text.startswith("/*", index):
                end = text.find("*/", index + 2)
                if end < 0:
                    raise ValueError("unterminated block comment")
                index = end + 2
                continue
            if text[index] == opening:
                depth += 1
            elif text[index] == closing:
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        raise ValueError("unterminated container")
    primitive_start = index
    while index < len(text) and text[index] not in ",}\n\r":
        if text[index].isspace():
            break
        if text.startswith("//", index) or text.startswith("/*", index):
            break
        index += 1
    while index > primitive_start and text[index - 1].isspace():
        index -= 1
    return index


def _object_bounds(text: str, start: int = 0) -> tuple[int, int]:
    start = _skip_ws_comments(text, start)
    if start >= len(text) or text[start] != "{":
        raise ValueError("settings root must be an object")
    end = _scan_value(text, start)
    if _skip_ws_comments(text, end) != len(text):
        raise ValueError("unexpected trailing content")
    return start, end - 1


def _find_property(text: str, object_start: int, object_end: int, key: str):
    index = object_start + 1
    found = None
    seen: set[str] = set()
    while True:
        index = _skip_ws_comments(text, index)
        if index >= object_end:
            return found
        if text[index] == ",":
            index += 1
            continue
        if text[index] not in {'\"', "'"}:
            raise ValueError("object key must be quoted")
        key_start = index
        key_end = _scan_string(text, index)
        quoted_key = text[key_start:key_end]
        raw_key = (
            json.loads(quoted_key)
            if quoted_key.startswith('"')
            else quoted_key[1:-1].replace("\\'", "'").replace("\\\\", "\\")
        )
        if raw_key in seen:
            raise ValueError(f"duplicate object key: {raw_key}")
        seen.add(raw_key)
        index = _skip_ws_comments(text, key_end)
        if index >= object_end or text[index] != ":":
            raise ValueError("missing property colon")
        value_start = _skip_ws_comments(text, index + 1)
        value_end = _scan_value(text, value_start)
        if raw_key == key:
            if found is not None:
                raise ValueError(f"duplicate target key: {key}")
            found = (value_start, value_end)
        following = _skip_ws_comments(text, value_end)
        if following < object_end and text[following] != ",":
            raise ValueError("missing property comma")
        index = following + 1 if following < object_end else following


def _object_property_keys(
    text: str, object_start: int, object_end: int
) -> set[str]:
    index = object_start + 1
    keys: set[str] = set()
    while True:
        index = _skip_ws_comments(text, index)
        if index >= object_end:
            return keys
        if text[index] == ",":
            index += 1
            continue
        if text[index] not in {'"', "'"}:
            raise ValueError("object key must be quoted")
        key_start = index
        key_end = _scan_string(text, index)
        quoted_key = text[key_start:key_end]
        raw_key = (
            json.loads(quoted_key)
            if quoted_key.startswith('"')
            else quoted_key[1:-1].replace("\\'", "'").replace("\\\\", "\\")
        )
        if raw_key in keys:
            raise ValueError(f"duplicate object key: {raw_key}")
        keys.add(raw_key)
        index = _skip_ws_comments(text, key_end)
        if index >= object_end or text[index] != ":":
            raise ValueError("missing property colon")
        value_end = _scan_value(text, _skip_ws_comments(text, index + 1))
        following = _skip_ws_comments(text, value_end)
        if following < object_end and text[following] != ",":
            raise ValueError("missing property comma")
        index = following + 1 if following < object_end else following


def _last_property_value_end(
    text: str, object_start: int, object_end: int
) -> int | None:
    index = object_start + 1
    last = None
    while True:
        index = _skip_ws_comments(text, index)
        if index >= object_end:
            return last
        if text[index] == ",":
            index += 1
            continue
        if text[index] not in {'"', "'"}:
            raise ValueError("object key must be quoted")
        key_end = _scan_string(text, index)
        index = _skip_ws_comments(text, key_end)
        if index >= object_end or text[index] != ":":
            raise ValueError("missing property colon")
        value_start = _skip_ws_comments(text, index + 1)
        last = _scan_value(text, value_start)
        following = _skip_ws_comments(text, last)
        if following < object_end and text[following] != ",":
            raise ValueError("missing property comma")
        index = following + 1 if following < object_end else following


def _insert_property(
    text: str,
    object_start: int,
    object_end: int,
    rendered: str,
) -> str:
    value_end = _last_property_value_end(text, object_start, object_end)
    if value_end is not None:
        following = _skip_ws_comments(text, value_end)
        if following >= object_end or text[following] != ",":
            text = text[:value_end] + "," + text[value_end:]
            object_end += 1
    before = text[:object_end]
    tail = text[object_end:]
    stripped = before.rstrip()
    indent = "  "
    newline = "\r\n" if "\r\n" in text else "\n"
    return stripped + newline + indent + rendered + newline + tail


def _upsert_object_entry(text: str, object_start: int, object_end: int, key: str, value: str) -> str:
    found = _find_property(text, object_start, object_end, key)
    if found is not None:
        start, end = found
        return text[:start] + value + text[end:]
    escaped_key = key.replace("\\", "\\\\").replace('"', '\\"')
    return _insert_property(
        text, object_start, object_end, f'"{escaped_key}": {value}'
    )


def scoped_command_rules(copilot_home: Path) -> tuple[str, str]:
    query = copilot_home / "rag" / "query"
    python = query / ".venv" / "Scripts" / "python.exe"
    list_script = copilot_home / "rag" / "list_dbs.py"
    search_script = copilot_home / "rag" / "search.py"
    safe_argument = r'(?!(?:-c|-m)(?=$|[ \t]|\x60))(?:"[^"\r\n;&|<>\x60$()]*"|[^\s;&|<>\x60$()]+)'
    separator = r'(?:[ \t]+|[ \t]*\x60\r?\n[ \t]*)'
    formal_python = (
        r'& "$env:USERPROFILE\.copilot\rag\query\.venv\Scripts\python.exe"'
    )

    def command(script: Path) -> str:
        absolute = re.escape(f'"{python}"') + separator + re.escape(f'"{script}"')
        formal_script = (
            '"$env:USERPROFILE\\.copilot\\rag\\' + script.name + '"'
        )
        formal = re.escape(formal_python) + separator + re.escape(formal_script)
        prefix = f"(?:{absolute}|{formal})"
        return f"/^{prefix}(?:{separator}{safe_argument})*$/"

    return (
        command(list_script),
        command(search_script),
    )


def _is_true(text: str, bounds: tuple[int, int] | None) -> bool:
    return (
        bounds is not None
        and text[bounds[0] : bounds[1]].strip() == "true"
    )


def _rule_is_explicit_allow(text: str, bounds: tuple[int, int]) -> bool:
    start = _skip_ws_comments(text, bounds[0])
    if start >= bounds[1] or text[start] != "{":
        return False
    end = _scan_value(text, start) - 1
    if _object_property_keys(text, start, end) != {
        "approve",
        "matchCommandLine",
    }:
        return False
    return _is_true(text, _find_property(text, start, end, "approve")) and _is_true(
        text, _find_property(text, start, end, "matchCommandLine")
    )


def patch_settings_with_status(
    text: str, command_rules: tuple[str, ...]
) -> tuple[str, bool]:
    if not text.strip():
        text = "{}\n"
    root_start, root_end = _object_bounds(text)
    enable = _find_property(
        text, root_start, root_end, "chat.tools.terminal.enableAutoApprove"
    )
    if enable is not None:
        enabled = text[enable[0] : enable[1]].strip()
        if enabled != "true":
            return text, True
    root_start, root_end = _object_bounds(text)
    found = _find_property(text, root_start, root_end, "chat.tools.terminal.autoApprove")
    rule_value = json.dumps(
        {"approve": True, "matchCommandLine": True}, separators=(",", ":")
    )
    rendered_rules = ",\n".join(
        "    \"" + rule.replace("\\", "\\\\").replace('"', '\\"') + "\": " + rule_value
        for rule in command_rules
    )
    if found is None:
        return _insert_property(
            text,
            root_start,
            root_end,
            f'"chat.tools.terminal.autoApprove": {{\n{rendered_rules}\n  }}',
        ), False
    value_start, value_end = found
    container_start = _skip_ws_comments(text, value_start)
    if text[container_start] != "{":
        raise PolicyManualAction(
            "chat.tools.terminal.autoApprove exists but is not an object"
        )
    container_end = _scan_value(text, container_start) - 1
    manual = False
    for command in command_rules:
        root_start, root_end = _object_bounds(text)
        found = _find_property(text, root_start, root_end, "chat.tools.terminal.autoApprove")
        value_start, value_end = found
        container_start = _skip_ws_comments(text, value_start)
        container_end = _scan_value(text, container_start) - 1
        try:
            existing = _find_property(
                text, container_start, container_end, command
            )
            explicit_allow = (
                existing is not None
                and _rule_is_explicit_allow(text, existing)
            )
        except ValueError as exc:
            raise PolicyManualAction(str(exc)) from exc
        if existing is not None:
            if not explicit_allow:
                manual = True
            continue
        text = _upsert_object_entry(
            text, container_start, container_end, command, rule_value
        )
    return text, manual


def patch_settings(text: str, command_rules: tuple[str, ...]) -> str:
    return patch_settings_with_status(text, command_rules)[0]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def candidate_settings(appdata: Path) -> list[Path]:
    values: list[Path] = []
    stable = appdata / "Code" / "User"
    insiders = appdata / "Code - Insiders" / "User"
    for base in (stable, insiders):
        if not base.is_dir() or _is_reparse(base):
            continue
        values.append(base / "settings.json")
        profiles = base / "profiles"
        if profiles.is_dir():
            values.extend(sorted(profiles.glob("*/settings.json")))
    output: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = str(value).casefold()
        if key not in seen:
            seen.add(key)
            output.append(value)
    return output

def configure_vscode(copilot_home: Path, appdata: Path) -> dict[str, object]:
    command_rules = scoped_command_rules(copilot_home)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    checked = 0
    changed = 0
    manual = False
    errors: list[str] = []
    for path in candidate_settings(appdata):
        checked += 1
        try:
            if path.exists() and _is_reparse(path):
                raise ValueError("settings target is a symlink or reparse point")
            original_bytes = path.read_bytes() if path.is_file() else b"{}\n"
            if original_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
                raise ValueError("unsupported settings encoding")
            had_bom = original_bytes.startswith(b"\xef\xbb\xbf")
            original = original_bytes.decode("utf-8-sig")
            try:
                patched, target_manual = patch_settings_with_status(
                    original, command_rules
                )
            except PolicyManualAction:
                manual = True
                continue
            manual = manual or target_manual
            if patched == original:
                continue
            if path.is_file():
                backup = path.with_name(
                    path.name
                    + f".local-rag-backup-{timestamp}-{secrets.token_hex(4)}"
                )
                shutil.copy2(path, backup)
            encoded = patched.encode("utf-8")
            if had_bom:
                encoded = b"\xef\xbb\xbf" + encoded
            _atomic_write_bytes(path, encoded)
            changed += 1
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, ValueError) and "duplicate " in str(exc):
                manual = True
            else:
                errors.append(type(exc).__name__)
    if checked == 0:
        status = "not_detected"
    elif errors and changed:
        status = "partial_failure"
    elif errors:
        status = "error"
    elif manual:
        status = "manual_action_required"
    elif changed:
        status = "configured_on_disk"
    else:
        status = "already_configured"
    return {
        "status": status,
        "targets_checked": checked,
        "targets_changed": changed,
        "policy_effectiveness": "unknown",
        "error_kinds": sorted(set(errors)),
    }


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & 0x400)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copilot-home", required=True)
    args = parser.parse_args()
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("APPDATA is unavailable")
    result = configure_vscode(Path(args.copilot_home), Path(appdata))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
