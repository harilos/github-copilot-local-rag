from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import PurePosixPath

SCHEMA = "local-rag.windows-package.v2"

def safe(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and bool(path.parts) and all(part not in {"", ".", ".."} for part in path.parts) and ":" not in path.parts[0] and "\\" not in name

def fingerprint(records: list[dict[str, object]]) -> str:
    values = [{"path": str(item["path"]), "sha256": str(item["sha256"]), "size": int(item["size"])} for item in sorted(records, key=lambda item: str(item["path"]))]
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package")
    args = parser.parse_args()
    with zipfile.ZipFile(args.package) as archive:
        names = archive.namelist()
        if not names or not all(safe(name) for name in names):
            raise SystemExit("unsafe archive path")
        if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
            raise SystemExit("duplicate or case-colliding archive path")
        manifests = [name for name in names if name.endswith("/PACKAGE-MANIFEST.json")]
        if len(manifests) != 1:
            raise SystemExit("exactly one package manifest is required")
        root = manifests[0].removesuffix("PACKAGE-MANIFEST.json")
        payload = json.loads(archive.read(manifests[0]).decode("utf-8"))
        if payload.get("schema") != SCHEMA:
            raise SystemExit("unsupported package manifest schema")
        files = list(payload.get("files") or [])
        paths = [str(item.get("path") or "") for item in files]
        expected = {root + path for path in paths} | {manifests[0]}
        if set(names) != expected:
            raise SystemExit("archive is not the manifest closed set")
        if len(paths) != len(set(paths)) or len(paths) != len({path.casefold() for path in paths}):
            raise SystemExit("duplicate or case-colliding manifest path")
        for entry in files:
            path = str(entry["path"])
            if not safe(path):
                raise SystemExit(f"unsafe manifest path: {path}")
            data = archive.read(root + path)
            if len(data) != int(entry["size"]) or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise SystemExit(f"file mismatch: {path}")
        databases = list(payload.get("databases") or [])
        db_names = [str(item.get("name") or "") for item in databases]
        if len(db_names) != len(set(db_names)) or len(db_names) != len({name.casefold() for name in db_names}):
            raise SystemExit("duplicate database declaration")
        for database in databases:
            name = str(database.get("name") or "")
            prefix = f".copilot/rag/dbs/{name}"
            subset = [item for item in files if str(item["path"]).startswith(prefix + "/")]
            if database.get("prefix") != prefix or database.get("coverage") != "closed-set" or not subset:
                raise SystemExit(f"invalid database coverage: {name}")
            if int(database.get("file_count") or -1) != len(subset) or int(database.get("bytes") or -1) != sum(int(item["size"]) for item in subset) or database.get("fingerprint") != fingerprint(subset):
                raise SystemExit(f"invalid database aggregate: {name}")
        for path in (path for path in paths if path.startswith(".copilot/rag/dbs/")):
            if not any(path.startswith(f".copilot/rag/dbs/{name}/") for name in db_names):
                raise SystemExit(f"undeclared database payload: {path}")
    print(json.dumps({"status":"verified","profile":payload["profile"],"product_version":payload["product_version"],"file_count":len(files),"databases":db_names}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
