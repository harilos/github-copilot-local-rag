from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    here = Path(__file__).resolve().parent
    venv = here / ".venv"
    subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    python = venv / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    subprocess.check_call([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([str(python), "-m", "pip", "install", "-r", str(here / "requirements.txt")])
    print(f"Ready: {python}")


if __name__ == "__main__":
    main()
