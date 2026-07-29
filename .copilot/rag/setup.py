#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parent
LOWER_SETUP = RAG_ROOT / "query" / "setup.py"


def main() -> int:
    if not LOWER_SETUP.is_file():
        print(
            "Local RAG setup implementation is missing. Reinstall the Local RAG package.",
            file=sys.stderr,
        )
        return 2
    completed = subprocess.run(
        [sys.executable, str(LOWER_SETUP), *sys.argv[1:]],
        check=False,
        cwd=str(RAG_ROOT),
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
