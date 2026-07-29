#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parent
QUERY_ROOT = RAG_ROOT / "query"
VENV_PYTHON = QUERY_ROOT / ".venv" / (
    "Scripts/python.exe" if sys.platform.startswith("win") else "bin/python"
)


def main() -> int:
    if not VENV_PYTHON.is_file():
        print(
            "[エラー] Local RAGの仮想環境が見つかりません。\n"
            f"確認先: {VENV_PYTHON}\n"
            "この場合は一時修復ではなく、初期設定を実行してください。",
            file=sys.stderr,
        )
        return 2
    print(
        "検索利用判定を修復します（一時的）。\n"
        "完了マーカーだけをオフライン検証して置き換えます。\n"
        "モデル、DB、検索索引は再構築しません。",
        file=sys.stderr,
    )
    completed = subprocess.run(
        [
            str(VENV_PYTHON),
            str(QUERY_ROOT / "setup.py"),
            "--repair-completion-marker",
            "--format",
            "human",
        ],
        check=False,
        cwd=str(RAG_ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
