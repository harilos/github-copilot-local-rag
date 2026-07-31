from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


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
    while index < len(text) and text[index] not in ",}\n\r":
        index += 1
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
    while True:
        index = _skip_ws_comments(text, index)
        if index >= object_end:
            return None
        if text[index] == ",":
            index += 1
            continue
        if text[index] not in {'\"', "'"}:
            raise ValueError("object key must be quoted")
        key_start = index
        key_end = _scan_string(text, index)
        raw_key = text[key_start + 1 : key_end - 1]
        index = _skip_ws_comments(text, key_end)
        if index >= object_end or text[index] != ":":
            raise ValueError("missing property colon")
        value_start = _skip_ws_comments(text, index + 1)
        value_end = _scan_value(text, value_start)
        if raw_key == key:
            return value_start, value_end
        index = value_end


def _insert_property(text: str, object_end: int, rendered: str) -> str:
    before = text[:object_end]
    tail = text[object_end:]
    stripped = before.rstrip()
    indent = "  "
    needs_comma = not stripped.endswith("{") and not stripped.endswith(",")
    separator = "," if needs_comma else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    return stripped + separator + newline + indent + rendered + newline + tail


def _upsert_object_entry(text: str, object_start: int, object_end: int, key: str, value: str) -> str:
    found = _find_property(text, object_start, object_end, key)
    if found is not None:
        start, end = found
        return text[:start] + value + text[end:]
    escaped_key = key.replace("\\", "\\\\").replace('"', '\\"')
    return _insert_property(text, object_end, f'"{escaped_key}": {value}')


def patch_settings(text: str, python_path: str) -> str:
    if not text.strip():
        text = "{}\n"
    root_start, root_end = _object_bounds(text)
    text = _upsert_object_entry(
        text,
        root_start,
        root_end,
        "chat.tools.terminal.enableAutoApprove",
        "true",
    )
    root_start, root_end = _object_bounds(text)
    found = _find_property(text, root_start, root_end, "chat.tools.terminal.autoApprove")
    escaped_path = python_path.replace("\\", "\\\\").replace('"', '\\"')
    if found is None:
        return _insert_property(
            text,
            root_end,
            f'"chat.tools.terminal.autoApprove": {{\n    "{escaped_path}": true\n  }}',
        )
    value_start, value_end = found
    container_start = _skip_ws_comments(text, value_start)
    if text[container_start] != "{":
        raise ValueError("chat.tools.terminal.autoApprove exists but is not an object")
    container_end = _scan_value(text, container_start) - 1
    return _upsert_object_entry(text, container_start, container_end, python_path, "true")


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
    values = [appdata / "Code" / "User" / "settings.json"]
    insiders = appdata / "Code - Insiders" / "User"
    if insiders.exists():
        values.append(insiders / "settings.json")
    for base in (appdata / "Code" / "User", insiders):
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copilot-home", required=True)
    args = parser.parse_args()
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("APPDATA is unavailable")
    python_path = str(
        Path(args.copilot_home)
        / "rag"
        / "query"
        / ".venv"
        / "Scripts"
        / "python.exe"
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path in candidate_settings(Path(appdata)):
        original = path.read_text(encoding="utf-8-sig") if path.is_file() else "{}\n"
        patched = patch_settings(original, python_path)
        if patched == original:
            continue
        if path.is_file():
            backup = path.with_name(path.name + f".local-rag-backup-{timestamp}")
            shutil.copy2(path, backup)
        _atomic_write(path, patched)
        print(f"Updated VS Code settings: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
