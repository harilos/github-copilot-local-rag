"""User-selected Windows VS Code settings edits, not an authorization boundary."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

from mcp_config import _JsoncLexer, _insert, _atomic_write_bytes, _path_has_reparse

GLOBAL = "chat.tools.global.autoApprove"
TERMINAL = "chat.tools.terminal.autoApprove"
ENABLE = "chat.tools.terminal.enableAutoApprove"


def runner_rule(install_root: Path) -> str:
    root = str(install_root.absolute()).replace("/", "\\")
    if any(character in root for character in "'\"`$\r\n"):
        raise ValueError("install path cannot be represented by a safe command rule")
    # Full-command matching, not a Python prefix allowlist. Only single-quoted
    # data or conservative bare arguments; no expansion, chaining or redirects.
    def quoted(path: str) -> str:
        escaped = re.escape(path).replace("/", r"\/")
        return '(?:"' + escaped + '"|\x27' + escaped + '\x27)'

    python = quoted(root + r"\rag\query\.venv\Scripts\python.exe")
    runner = quoted(root + r"\rag\query\skill_runner.py")
    # The shipped Skill uses this exact environment reference. This is a
    # best-effort convenience rule, not protection against a modified shell.
    default = str(Path(os.environ.get("USERPROFILE", "")) / ".copilot")
    if os.path.normcase(str(install_root.absolute())) == os.path.normcase(default):
        python = '(?:' + python + '|' + re.escape('"$env:USERPROFILE\\.copilot\\rag\\query\\.venv\\Scripts\\python.exe"') + ')'
        runner = '(?:' + runner + '|' + re.escape('"$env:USERPROFILE\\.copilot\\rag\\query\\skill_runner.py"') + ')'
    argument = r"(?:'[^'\r\n]*(?:''[^'\r\n]*)*'|[A-Za-z0-9_.,:%/@=+\-]+)"
    return '/^&[ \\t]+' + python + r'[ \t]+-I[ \t]+-X[ \t]+utf8[ \t]+-B[ \t]+' + runner + r'[ \t]+(?:list|search|detail|setup)(?:[ \t]+' + argument.replace('/', r'\/') + r')*[ \t]*$(?![\s\S])/'


def patch(text: str, root: Path, mode: str) -> str:
    if mode not in ("global", "runner"):
        raise ValueError("an explicit approval mode is required")
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text.removeprefix(bom) if bom else text
    body = body if body.strip() else "{}\n"
    lexer = _JsoncLexer(body)
    values, view = lexer.document()
    if mode == "global":
        item = view.property(GLOBAL)
        if item is not None:
            if not isinstance(item.value, bool):
                raise ValueError("global approval setting is not boolean")
            body = body[:item.value_start] + "true" + body[item.value_end:]
        else:
            body = _insert(body, view, GLOBAL, "true")
    else:
        if values.get(GLOBAL) is True:
            raise ValueError("global approval is already enabled; runner-only scope cannot be claimed")
        if values.get(ENABLE) is False:
            raise ValueError("terminal auto approval is disabled; existing choice preserved")
        item = view.property(TERMINAL)
        key = runner_rule(root)
        value = {"approve": True, "matchCommandLine": True}
        if item is None:
            body = _insert(body, view, TERMINAL, json.dumps({key: value}))
        else:
            rules = lexer.object_view(item.value_start)
            existing = rules.property(key)
            if existing is not None and existing.value != value:
                raise ValueError("existing runner rule differs; preserved")
            if existing is None:
                body = _insert(body, rules, key, json.dumps(value))
    _JsoncLexer(body).document()
    return bom + body


def policy_allows(mode: str) -> bool:
    """Only read local policies. Absence is NOT proof of organization consent."""
    import winreg
    name = "ChatToolsAutoApprove" if mode == "global" else "ChatToolsTerminalEnableAutoApprove"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Policies\Microsoft\VSCode", 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    if value not in (1, "true"):
                        return False
            except FileNotFoundError:
                pass
    return True


def configure(settings: Path, root: Path, mode: str) -> str:
    boundary = Path(settings.anchor)
    if _path_has_reparse(settings, boundary):
        raise ValueError("settings path crosses a reparse point")
    for file in (root / "rag/query/skill_runner.py", root / "rag/query/.venv/Scripts/python.exe"):
        if not file.is_file() or _path_has_reparse(file, Path(file.anchor)):
            raise ValueError("fixed installed runner/runtime unavailable")
    existed = settings.exists()
    before = settings.read_bytes() if existed else b""
    if len(before) > 2 * 1024 * 1024:
        raise ValueError("settings file too large")
    after = patch(before.decode("utf-8", errors="strict"), root, mode).encode("utf-8")
    if before == after:
        return "unchanged"
    # Detect ordinary concurrent edits; never silently replace malformed JSONC.
    if settings.exists() != existed or (existed and settings.read_bytes() != before):
        raise ValueError("settings changed concurrently")
    _atomic_write_bytes(settings, after, boundary=boundary)
    return "settings_written_not_effective_permission_verified"


def choose_mode() -> str | None:
    """Enter selects runner only in a real terminal; EOF/pipes never consent."""
    print("1. Individual runner approval [default]: VS Code only; NOT Copilot CLI.")
    print("2. Leave approval settings unchanged.")
    print("3. Global approval [DANGER / NOT RECOMMENDED]: VS Code ALL tools/workspaces.")
    print("   This VS Code setting does NOT affect standalone Copilot CLI; CLI requires its own permission options.")
    if not sys.stdin.isatty():
        print("No interactive input: approval settings unchanged. Use an explicit installer approval option to configure them.")
        return None
    try:
        selection = input("Approval choice [1]: ").strip()
    except (EOFError, OSError):
        print("No selection received: approval settings unchanged.")
        return None
    if selection in ("", "1"):
        return "runner"
    if selection == "3":
        return "global"
    if selection != "2":
        print("Unrecognized selection: approval settings unchanged.")
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("global", "runner", "choose"), required=True)
    args = parser.parse_args()
    print("WARNING: Organization permission is required. Policy may disable this option. Native VS Code consent is not bypassed.")
    mode = choose_mode() if args.mode == "choose" else args.mode
    if mode is None:
        return 0
    if mode == "global":
        print("DANGER / NOT RECOMMENDED: global auto approval covers ALL VS Code tools and workspaces, including destructive commands, files and MCP. This setting does NOT affect standalone Copilot CLI.")
    else:
        print("Runner approval is best-effort, Windows PowerShell only; it is not a security boundary and does not affect Copilot CLI.")
    try:
        if os.name != "nt" or not os.environ.get("APPDATA"):
            raise ValueError("Windows APPDATA is required")
        if not policy_allows(mode):
            raise ValueError("organization policy disables auto approval")
        settings = Path(os.environ["APPDATA"]) / "Code/User/settings.json"
        print(configure(settings, args.install_root.absolute(), mode))
        return 0
    except (ValueError, OSError):
        # Do not print user settings, commands or document content.
        print("WARNING: approval option NOT APPLIED; check policy, existing settings and fixed runtime. Local RAG remains usable with manual approval.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
