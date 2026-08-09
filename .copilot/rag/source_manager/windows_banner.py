from __future__ import annotations

import base64
from collections.abc import Mapping


WINDOWS_BANNER = {
    "classification": "xxxx",
    "developer": "harlos",
    "dictionary_maintenance": "yyyy",
}

README_URL = "https://github.com/harilos/github-copilot-local-rag"


def install_cmd_banner(
    values: Mapping[str, str] | None = None,
    *,
    newline: str = "\r\n",
) -> str:
    configured = WINDOWS_BANNER if values is None else values
    required = ("classification", "developer", "dictionary_maintenance")
    if any(not isinstance(configured.get(key), str) for key in required):
        raise ValueError("Windows banner values must be strings")

    localized = newline.join(
        (
            f"秘密等級: {configured['classification']}",
            f"開発者: {configured['developer']}",
            f"辞書メンテナンス: {configured['dictionary_maintenance']}",
            "配布用: 受領済みDBを検索するためのパッケージです。",
            "管理用: DBやSourceの追加・更新と配布package作成に使います。",
            "自分で資料を追加・更新する場合は管理用を利用してください。",
            f"詳細はREADMEの管理者向け説明を参照してください: {README_URL}",
        )
    ) + newline
    payload = base64.b64encode(localized.encode("utf-8")).decode("ascii")
    return (
        "echo ========================================\n"
        "echo  Local-RAG\n"
        "echo ========================================\n"
        '"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
        "-NoLogo -NoProfile -Command "
        '"[Console]::OutputEncoding=[Text.Encoding]::UTF8;'
        "[Console]::Write([Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{payload}')))\"\n"
        "echo.\n"
    ).replace("\n", newline)


def install_cmd_powershell_failure(*, newline: str = "\r\n") -> str:
    """Return the cmd-only failure path used when PowerShell cannot start."""

    return (
        ':local_rag_powershell_unavailable\n'
        'if not defined local_rag_rc set "local_rag_rc=9009"\n'
        'chcp 65001 >nul\n'
        'set "local_rag_log_dir=%LOCALAPPDATA%\\LocalRAG\\logs"\n'
        'if not defined LOCALAPPDATA set "local_rag_log_dir=%TEMP%\\LocalRAG\\logs"\n'
        '2>nul mkdir "%local_rag_log_dir%"\n'
        'set "local_rag_log=%local_rag_log_dir%\\portable-install-launcher-%RANDOM%-%RANDOM%.log"\n'
        '>"%local_rag_log%" echo Local RAG インストール結果: 失敗 ^(FAILED^)\n'
        'if exist "%local_rag_log%" goto local_rag_launcher_log_ready\n'
        'set "local_rag_log_dir=%TEMP%\\LocalRAG\\logs"\n'
        '2>nul mkdir "%local_rag_log_dir%"\n'
        'set "local_rag_log=%local_rag_log_dir%\\portable-install-launcher-%RANDOM%-%RANDOM%.log"\n'
        '>"%local_rag_log%" echo Local RAG インストール結果: 失敗 ^(FAILED^)\n'
        ':local_rag_launcher_log_ready\n'
        '>>"%local_rag_log%" echo PowerShellを起動できませんでした。Windows PowerShell 5.1 が利用可能か確認してください。\n'
        'for %%I in ("%local_rag_log%") do set "local_rag_log=%%~fI"\n'
        'echo Local RAG インストール結果: 失敗 ^(FAILED^)\n'
        'echo PowerShellを起動できませんでした。Windows PowerShell 5.1 が利用可能か確認してください。\n'
        'echo ログ: %local_rag_log%\n'
        'goto local_rag_finish\n'
    ).replace("\n", newline)
