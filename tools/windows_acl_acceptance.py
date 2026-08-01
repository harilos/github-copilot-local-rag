from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import stat
import sys
import types
import uuid
from pathlib import Path

_SCRIPT_ROOT = Path(__file__).resolve().parents[1]
_MODULE_ROOT = _SCRIPT_ROOT / ".copilot" / "rag" / "source_manager"
_PACKAGE_NAME = "_acl_acceptance_source_manager"
package = types.ModuleType(_PACKAGE_NAME)
package.__path__ = [str(_MODULE_ROOT)]
sys.modules.setdefault(_PACKAGE_NAME, package)


def _load_module(name: str):
    qualified = f"{_PACKAGE_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(
        qualified,
        _MODULE_ROOT / f"{name}.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


_load_module("errors")
create_persistent_directory = _load_module(
    "persistent_paths"
).create_persistent_directory


_SCHEMA = "local-rag-windows-acl-acceptance-v1"
_LEAF_PREFIX = "local-rag-acl-test-"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("create", "consume", "cleanup"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--forbidden-root", action="append", default=[])
    args = parser.parse_args()

    if os.name != "nt" or sys.version_info < (3, 13):
        raise SystemExit("Windows Python 3.13 or newer is required")
    test_id = str(uuid.UUID(args.test_id))
    root = _validate_root(Path(args.root), args.forbidden_root)
    if args.phase == "create":
        result = _create(root, test_id)
    elif args.phase == "consume":
        result = _consume(root, test_id)
    else:
        result = _cleanup(root, test_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _validate_root(root: Path, extra_forbidden: list[str]) -> Path:
    if not root.is_absolute():
        raise SystemExit("--root must be absolute")
    normalized = Path(os.path.abspath(os.fspath(root)))
    leaf = normalized.name
    suffix = leaf[len(_LEAF_PREFIX) :] if leaf.startswith(_LEAF_PREFIX) else ""
    if (
        not suffix
        or not 12 <= len(suffix) <= 32
        or any(character not in "0123456789abcdefABCDEF" for character in suffix)
    ):
        raise SystemExit("root leaf must use local-rag-acl-test- plus 12-32 hex characters")
    default_forbidden = Path.home() / ".copilot" / "rag" / "dbs"
    forbidden = [default_forbidden, *(Path(value) for value in extra_forbidden)]
    for candidate in forbidden:
        blocked = Path(os.path.abspath(os.fspath(candidate.expanduser())))
        if normalized == blocked or normalized in blocked.parents or blocked in normalized.parents:
            raise SystemExit("test root must not equal, contain, or descend from a DB root")
    if normalized.exists() and _is_link_or_reparse(normalized):
        raise SystemExit("test root must not be a link or reparse point")
    parent = normalized.parent
    if not parent.is_dir() or _is_link_or_reparse(parent):
        raise SystemExit("test root parent must be an existing real directory")
    return normalized


def _create(root: Path, test_id: str) -> dict[str, object]:
    if root.exists():
        raise SystemExit("create requires an absent root")
    create_persistent_directory(root, trusted_root=root.parent)
    shared = create_persistent_directory(root / "shared", trusted_root=root)
    create_persistent_directory(shared / "nested", trusted_root=root, parents=True)
    (shared / "seed.txt").write_text("created\n", encoding="utf-8")
    _sentinel(root).write_text(
        json.dumps({"schema_version": _SCHEMA, "test_id": test_id}) + "\n",
        encoding="utf-8",
    )
    return {"phase": "create", "status": "ok", "test_id": test_id}


def _consume(root: Path, test_id: str) -> dict[str, object]:
    _require_sentinel(root, test_id)
    create_persistent_directory(
        root / "shared",
        trusted_root=root,
        exist_ok=True,
    )
    shared = root / "shared"
    entries = sorted(path.name for path in shared.iterdir())
    if "seed.txt" not in entries:
        raise SystemExit("seed file is missing")
    if (shared / "seed.txt").read_text(encoding="utf-8") != "created\n":
        raise SystemExit("seed file content changed")
    written = shared / "consumer-write.txt"
    written.write_text("consumer\n", encoding="utf-8")
    temporary = shared / ".replace.tmp"
    temporary.write_text("replaced\n", encoding="utf-8")
    os.replace(temporary, written)
    renamed = shared / "consumer-renamed.txt"
    written.rename(renamed)
    renamed.unlink()
    disposable = shared / "consumer-delete"
    create_persistent_directory(disposable, trusted_root=root)
    disposable.rmdir()
    return {
        "phase": "consume",
        "status": "ok",
        "test_id": test_id,
        "operations": ["list", "read", "write", "atomic-replace", "rename", "delete"],
        "winerror_5": 0,
    }


def _cleanup(root: Path, test_id: str) -> dict[str, object]:
    _require_sentinel(root, test_id)
    if _is_link_or_reparse(root):
        raise SystemExit("cleanup refuses a link or reparse point")
    shutil.rmtree(root)
    return {"phase": "cleanup", "status": "ok", "test_id": test_id}


def _require_sentinel(root: Path, test_id: str) -> None:
    if not root.is_dir() or _is_link_or_reparse(root):
        raise SystemExit("acceptance root is missing or unsafe")
    sentinel = _sentinel(root)
    try:
        payload = json.loads(sentinel.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("acceptance sentinel is missing or invalid") from exc
    if payload != {"schema_version": _SCHEMA, "test_id": test_id}:
        raise SystemExit("acceptance sentinel does not match this test")


def _sentinel(root: Path) -> Path:
    return root / ".local-rag-acl-acceptance.json"


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        path.is_symlink()
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )


if __name__ == "__main__":
    raise SystemExit(main())
