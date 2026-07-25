from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-model", action="store_true", help="Deprecated: model preparation is now the default")
    parser.add_argument("--no-prepare-model", action="store_true", help="Only create the virtual environment and install dependencies")
    parser.add_argument("--force-model", action="store_true", help="Recreate the ONNX INT8 model even if it exists")
    parser.add_argument("--proxy", help="Proxy URL passed to pip/Hugging Face capable tools, e.g. http://proxy.example:8080")
    args = parser.parse_args()
    if args.prepare_model and args.no_prepare_model:
        parser.error("--prepare-model and --no-prepare-model cannot be used together")

    here = Path(__file__).resolve().parent
    venv = here / ".venv"
    env = os.environ.copy()
    if args.proxy:
        env["HTTP_PROXY"] = args.proxy
        env["HTTPS_PROXY"] = args.proxy
        env["PIP_PROXY"] = args.proxy
    try:
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)], env=env)
    except subprocess.CalledProcessError as exc:
        _explain_setup_failure(exc)
        raise
    python = venv / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    try:
        proxy_args = ["--proxy", args.proxy] if args.proxy else []
        subprocess.check_call([str(python), "-m", "pip", "install", *proxy_args, "--upgrade", "pip"], env=env)
        subprocess.check_call([str(python), "-m", "pip", "install", *proxy_args, "-r", str(here / "requirements.txt")], env=env)
        if not args.no_prepare_model:
            cmd = [str(python), str(here / "prepare_onnx_model.py")]
            if args.force_model:
                cmd.append("--force")
            subprocess.check_call(cmd, env=env)
    except subprocess.CalledProcessError as exc:
        _explain_setup_failure(exc)
        raise
    (venv / ".rag-deps-installed").write_text("ok\n", encoding="utf-8")
    print(f"Ready: {python}")


def _explain_setup_failure(exc: subprocess.CalledProcessError) -> None:
    print("", file=sys.stderr)
    print("RAG初期設定に失敗しました。社内プロキシや証明書が原因のことが多いです。", file=sys.stderr)
    print("", file=sys.stderr)
    print("確認してください:", file=sys.stderr)
    print("- プロキシが必要なら: python ~/.copilot/rag/query/setup.py --proxy http://proxy.example:8080", file=sys.stderr)
    print("- 会社のCA証明書が必要なら、REQUESTS_CA_BUNDLE / SSL_CERT_FILE / PIP_CERT に証明書ファイルを設定してください。", file=sys.stderr)
    print("- pipの証明書エラーなら: python -m pip config list で global.cert / global.proxy を確認してください。", file=sys.stderr)
    print("- Hugging Faceからモデル取得できない場合は、HTTPS_PROXY と REQUESTS_CA_BUNDLE がPythonプロセスにも渡っているか確認してください。", file=sys.stderr)
    print("- 完全にローカルで動かせない端末では、別PCのRAGサービスを proxy_client.py で呼ぶ運用に切り替えてください。", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"失敗したコマンドの終了コード: {exc.returncode}", file=sys.stderr)


if __name__ == "__main__":
    main()
