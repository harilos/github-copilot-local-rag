from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.network import (
    NetworkConfigError,
    add_network_arguments,
    redact_text,
    resolve_network_configuration,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fallback client for a remote/proxy RAG service."
    )
    parser.add_argument("question", nargs="+")
    parser.add_argument("--url", required=True, help="Remote RAG endpoint URL")
    parser.add_argument("--db", required=True)
    add_network_arguments(parser)
    args = parser.parse_args()

    try:
        network = resolve_network_configuration(
            cli_proxy=args.proxy,
            cli_ca_bundle=args.ca_bundle,
            cli_no_proxy=args.no_proxy,
            network_config=args.network_config,
            ignore_network_config=args.ignore_network_config,
            external_operation=True,
        )
        payload = json.dumps(
            {"db": args.db, "question": " ".join(args.question)},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            args.url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        opener = network.build_url_opener()
        with opener.open(request, timeout=60) as response:
            print(
                redact_text(
                    response.read().decode("utf-8", errors="replace")
                )
            )
        return 0
    except NetworkConfigError as exc:
        return _emit_error(exc.kind, str(exc))
    except urllib.error.HTTPError as exc:
        return _emit_error(f"http_{exc.code}", str(exc))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _emit_error(type(exc).__name__, str(exc))


def _emit_error(kind: str, message: str) -> int:
    print(
        json.dumps(
            {
                "status": "error",
                "error_kind": kind,
                "error": redact_text(message),
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
