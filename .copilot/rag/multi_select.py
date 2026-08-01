from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|$))")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

@dataclass(frozen=True)
class SelectionRow:
    key: str
    label: str

@dataclass(frozen=True)
class SelectionResult:
    mode: str
    keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"all", "explicit", "none", "cancelled"}:
            raise ValueError("selection mode is invalid")
        if self.mode in {"none", "cancelled"} and self.keys:
            raise ValueError("empty selection mode cannot contain keys")

def safe_label(value: object) -> str:
    text = _ANSI_ESCAPE.sub("", str(value or ""))
    return " ".join(_CONTROL.sub(" ", text).split())

def toggle_selection(
    rows: Sequence[SelectionRow], *, ask: Callable[[str], str | None],
    output: Callable[[str], None], invalid: Callable[[str], None], title: str,
    selected_text: str = "selected", excluded_text: str = "excluded",
) -> SelectionResult:
    normalized = tuple(SelectionRow(str(row.key), safe_label(row.label)) for row in rows)
    seen: set[str] = set()
    for row in normalized:
        key = row.key.casefold()
        if not row.key or key in seen:
            raise ValueError("selection stable key is empty or duplicated")
        seen.add(key)
    selected = {row.key for row in normalized}
    while True:
        output(f"\n{safe_label(title)}")
        for index, row in enumerate(normalized, start=1):
            state = selected_text if row.key in selected else excluded_text
            output(f"{index}. [{safe_label(state)}] {row.label}")
        output("\nnumber/comma list: toggle  A: select all  X: exclude all"
               "\nC: confirm  0: cancel")
        action = ask("Action: ")
        if action is None or str(action).strip() == "0":
            return SelectionResult("cancelled")
        value = str(action).strip().casefold()
        if value == "a":
            selected = {row.key for row in normalized}; continue
        if value == "x":
            selected.clear(); continue
        if value == "c":
            keys = tuple(row.key for row in normalized if row.key in selected)
            if not keys: return SelectionResult("none")
            if len(keys) == len(normalized): return SelectionResult("all", keys)
            return SelectionResult("explicit", keys)
        tokens = value.split(",")
        if not tokens or any(not token.strip() for token in tokens):
            invalid("Enter a number, A, X, C, or 0"); continue
        try:
            indexes = {int(token.strip()) for token in tokens}
        except ValueError:
            invalid("Enter a number, A, X, C, or 0"); continue
        if not indexes or any(index < 1 or index > len(normalized) for index in indexes):
            invalid(f"Enter 1-{len(normalized)}, A, X, C, or 0"); continue
        for index in indexes:
            key = normalized[index - 1].key
            if key in selected: selected.remove(key)
            else: selected.add(key)

def database_selection_rows(
    summaries: Iterable[Mapping[str, object]], dbs_root: Path,
) -> tuple[SelectionRow, ...]:
    rows: list[SelectionRow] = []
    root = _trusted_database_root(Path(dbs_root))
    for summary in summaries:
        name = str(summary.get("name") or "")
        if not name: continue
        candidate = root / name
        metadata = os.lstat(candidate)
        if _is_link_or_reparse(candidate, metadata):
            raise ValueError("database link or reparse point is forbidden")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir() or resolved.parent != root:
            raise ValueError("database path escapes trusted root")
        display = safe_label(summary.get("display_name") or summary.get("label") or
                             summary.get("title") or name)
        rows.append(SelectionRow(name, f"{safe_label(name)}  display: {display}  "
                                 f"{_format_size(_tree_size(resolved))}"))
    rows.sort(key=lambda row: row.key.casefold())
    return tuple(rows)

def discover_database_summaries(dbs_root: Path) -> list[dict[str, str]]:
    root = _trusted_database_root(Path(dbs_root))
    values: list[dict[str, str]] = []
    for candidate in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not candidate.is_dir() or candidate.is_symlink(): continue
        metadata = os.lstat(candidate)
        if _is_link_or_reparse(candidate, metadata): continue
        db_json = candidate / "db.json"
        if not db_json.is_file(): continue
        display = candidate.name
        try:
            payload = json.loads(db_json.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                display = str(payload.get("display_name") or payload.get("name") or candidate.name)
        except (OSError, UnicodeError, ValueError):
            pass
        values.append({"name": candidate.name, "display_name": display})
    return values

def _tree_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        metadata = os.lstat(path)
        if _is_link_or_reparse(path, metadata):
            raise ValueError("database link or reparse point is forbidden")
        if stat.S_ISREG(metadata.st_mode): total += int(metadata.st_size)
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("database special file is forbidden")
    return total

def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

def _trusted_database_root(value: Path) -> Path:
    original = value.expanduser().absolute()
    current = original
    while True:
        if current.exists() or current.is_symlink():
            metadata = os.lstat(current)
            if _is_link_or_reparse(current, metadata):
                raise ValueError("database root contains a link or reparse point")
        if current.parent == current:
            break
        current = current.parent
    resolved = original.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("database root is not a directory")
    return resolved

def _format_size(value: int) -> str:
    units = ("bytes", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "bytes": return f"{int(amount):,} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} bytes"
