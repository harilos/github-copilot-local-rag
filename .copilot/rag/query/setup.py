from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-model", action="store_true", help="Deprecated: model preparation is now the default")
    parser.add_argument("--no-prepare-model", action="store_true", help="Only create the virtual environment and install dependencies")
    parser.add_argument("--force-model", action="store_true", help="Recreate the ONNX INT8 model even if it exists")
    args = parser.parse_args()
    if args.prepare_model and args.no_prepare_model:
        parser.error("--prepare-model and --no-prepare-model cannot be used together")

    here = Path(__file__).resolve().parent
    venv = here / ".venv"
    subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    python = venv / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    subprocess.check_call([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([str(python), "-m", "pip", "install", "-r", str(here / "requirements.txt")])
    if not args.no_prepare_model:
        cmd = [str(python), str(here / "prepare_onnx_model.py")]
        if args.force_model:
            cmd.append("--force")
        subprocess.check_call(cmd)
    (venv / ".rag-deps-installed").write_text("ok\n", encoding="utf-8")
    print(f"Ready: {python}")


if __name__ == "__main__":
    main()
