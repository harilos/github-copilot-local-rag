#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from source_manager.packages import (
    PackageError,
)
from source_manager.windows_distribution import (
    create_windows_distribution_package,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a validated Local RAG lookup distribution ZIP."
    )
    parser.add_argument(
        "--copilot-home",
        type=Path,
        default=Path.home() / ".copilot",
        help="Installed .copilot directory (default: ~/.copilot).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New ZIP path. An existing path is never overwritten.",
    )
    parser.add_argument(
        "--db",
        action="append",
        dest="databases",
        help="Database to include. Repeat to select multiple databases.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if not sys.platform.startswith("win"):
            raise PackageError(
                "windows_offline_package_requires_windows"
            )
        result = create_windows_distribution_package(
            arguments.copilot_home,
            arguments.output,
            db_names=arguments.databases,
            progress=lambda message: print(message, file=sys.stderr),
        )
    except PackageError as exc:
        print("=== Package creation: FAILED ===", file=sys.stderr)
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 2
    print("=== Package creation: SUCCESS ===", file=sys.stderr)
    print(
        json.dumps(
            result,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
