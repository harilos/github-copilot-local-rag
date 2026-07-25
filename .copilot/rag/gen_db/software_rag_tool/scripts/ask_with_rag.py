from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    script = Path(__file__).with_name("query.py")
    args = sys.argv[1:]
    if "--db" not in args:
        raise SystemExit("usage: ask_with_rag.py --db <name-rag> <question>")
    question_parts = [arg for arg in args if arg != "--format" and arg != "prompt"]
    question = " ".join(arg for arg in question_parts if arg != "--db").strip()
    if not question:
        raise SystemExit("usage: ask_with_rag.py --db <name-rag> <question>")
    raise SystemExit(subprocess.call([sys.executable, str(script), *args, "--format", "prompt"]))


if __name__ == "__main__":
    main()
