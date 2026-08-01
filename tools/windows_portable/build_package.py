from __future__ import annotations

import argparse
import json
from pathlib import Path

from windows_package_builder import BuildRequest, build_package


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database-root", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--profile", choices=("search-only", "admin-full"), required=True
    )
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--dependency-lock-sha256", required=True)
    parser.add_argument("--model-fingerprint", required=True)
    args = parser.parse_args()
    result = build_package(
        BuildRequest(
            payload_root=args.payload_root,
            runtime_root=args.runtime_root,
            model_root=args.model_root,
            output_dir=args.output_dir,
            database_root=args.database_root,
            version=args.version,
            profile=args.profile,
            python_version=args.python_version,
            dependency_lock_sha256=args.dependency_lock_sha256,
            model_fingerprint=args.model_fingerprint,
        )
    )
    print(
        json.dumps(
            {
                "zip_path": str(result.zip_path),
                "zip_sha256": result.zip_sha256,
                "package_manifest_sha256": result.package_manifest_sha256,
                "expanded_size": result.expanded_size,
                "file_count": result.file_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
