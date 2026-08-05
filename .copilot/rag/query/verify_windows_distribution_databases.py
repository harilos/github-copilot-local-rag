from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
if str(QUERY_ROOT) not in sys.path:
    sys.path.insert(0, str(QUERY_ROOT))
SOURCE_MANAGER_ROOT = RAG_ROOT / "source_manager"
if not (QUERY_ROOT / "windows_tokenizer_contract.py").is_file():
    sys.path.insert(0, str(SOURCE_MANAGER_ROOT))

from windows_tokenizer_contract import (  # noqa: E402
    DatabaseTokenizerCompatibilityError,
    load_tokenizer_contract,
    validate_distribution_databases,
    validate_runtime_tokenizer_packages,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only verification for packaged Windows databases."
    )
    parser.add_argument("--rag-root", type=Path, default=RAG_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    rag_root = arguments.rag_root.resolve()
    lock_path = rag_root / "query" / "windows-runtime-lock.json"
    if not lock_path.is_file():
        lock_path = rag_root / "source_manager" / "windows-runtime-lock.json"
    dbs_root = rag_root / "dbs"
    names = (
        sorted(
            (path.name for path in dbs_root.iterdir() if path.is_dir()),
            key=str.casefold,
        )
        if dbs_root.is_dir()
        else []
    )
    try:
        contract = load_tokenizer_contract(lock_path)
        runtime_versions = validate_runtime_tokenizer_packages(contract)
        results = validate_distribution_databases(
            dbs_root,
            names,
            lock_path=lock_path,
        )
    except DatabaseTokenizerCompatibilityError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "windows_offline_database_tokenizer_mismatch",
                    "database": exc.database,
                    "expected": exc.expected,
                    "actual": exc.actual,
                    "action": "rebuild_database_with_distribution_tokenizer",
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc).splitlines()[0][:160],
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": "pass",
                "database_tokenizer_compatibility": "pass",
                "databases": [result["database"] for result in results],
                "runtime_tokenizer_versions": runtime_versions,
                "list_dbs_executed": False,
                "real_search_executed": False,
                "dense_inference_executed": False,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
