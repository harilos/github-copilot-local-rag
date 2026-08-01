from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import PurePosixPath


def _safe(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
        and ":" not in path.parts[0]
        and "\\" not in name
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package")
    args = parser.parse_args()
    with zipfile.ZipFile(args.package) as archive:
        names = archive.namelist()
        if not names or not all(_safe(name) for name in names):
            raise SystemExit("unsafe archive path")
        manifests = [
            name for name in names if name.endswith("/PACKAGE-MANIFEST.json")
        ]
        if len(manifests) != 1:
            raise SystemExit("exactly one package manifest is required")
        root = manifests[0].removesuffix("PACKAGE-MANIFEST.json")
        payload = json.loads(archive.read(manifests[0]).decode("utf-8"))
        if payload.get("schema") != "local-rag.windows-package.v1":
            raise SystemExit("unsupported package manifest schema")
        for entry in payload.get("files") or []:
            name = root + entry["path"]
            data = archive.read(name)
            if len(data) != entry["size"]:
                raise SystemExit(f"size mismatch: {entry['path']}")
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise SystemExit(f"SHA-256 mismatch: {entry['path']}")
    print(
        json.dumps(
            {
                "status": "verified",
                "profile": payload["profile"],
                "product_version": payload["product_version"],
                "file_count": len(payload["files"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
