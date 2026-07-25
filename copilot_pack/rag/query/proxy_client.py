from __future__ import annotations

import argparse
import json
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description="Fallback client for a remote/proxy RAG service.")
    parser.add_argument("question", nargs="+")
    parser.add_argument("--url", required=True, help="Proxy endpoint URL")
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    payload = json.dumps({"db": args.db, "question": " ".join(args.question)}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(args.url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as res:
        print(res.read().decode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
