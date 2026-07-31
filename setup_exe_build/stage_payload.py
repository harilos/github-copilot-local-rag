from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--dictionary", required=True)
    parser.add_argument("--app-stage", required=True)
    parser.add_argument("--dictionary-stage", required=True)
    return parser.parse_args()


def _clean_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"unsafe staging path: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True)


def main() -> int:
    args = _parse_args()
    repo_root = Path(args.repo_root).expanduser().resolve(strict=True)
    dictionary = Path(args.dictionary).expanduser().resolve(strict=True)
    app_stage = Path(args.app_stage).expanduser().resolve(strict=False)
    dictionary_stage = Path(args.dictionary_stage).expanduser().resolve(strict=False)

    rag_root = repo_root / ".copilot" / "rag"
    copilot_home = repo_root / ".copilot"
    if not rag_root.is_dir() or rag_root.is_symlink():
        raise RuntimeError("repository .copilot/rag directory is unavailable")
    if not dictionary.is_dir() or dictionary.is_symlink():
        raise RuntimeError("selected dictionary is not a real directory")
    if dictionary.parent.name.casefold() != "dbs":
        raise RuntimeError("select a database folder directly below a rag/dbs directory")

    # Import only source_manager.packages. Importing source_manager itself also
    # initializes every administrator provider and would force the build Python
    # to have unrelated credential and ingestion dependencies installed.
    source_manager_root = rag_root / "source_manager"
    package = types.ModuleType("source_manager")
    package.__file__ = str(source_manager_root / "__init__.py")
    package.__package__ = "source_manager"
    package.__path__ = [str(source_manager_root)]  # type: ignore[attr-defined]
    sys.modules["source_manager"] = package
    packages = importlib.import_module("source_manager.packages")

    _clean_directory(app_stage)
    _clean_directory(dictionary_stage)
    created = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    version = (rag_root / "VERSION").read_text(encoding="utf-8").strip()

    product_entries = packages._product_entries(copilot_home, admin=False)
    packages._stage_package(
        app_stage,
        product_entries,
        kind="distribution",
        databases=[],
        created=created,
        tool_version=version,
    )

    dictionary_entries, databases = packages._database_entries(
        dictionary.parent,
        db_names=[dictionary.name],
        distribution=True,
    )
    packages._stage_package(
        dictionary_stage,
        dictionary_entries,
        kind="distribution",
        databases=databases,
        created=created,
        tool_version=version,
    )

    print(
        json.dumps(
            {
                "status": "staged",
                "version": version,
                "dictionary": dictionary.name,
                "app_root": str(app_stage / ".copilot"),
                "dictionary_root": str(
                    dictionary_stage
                    / ".copilot"
                    / "rag"
                    / "dbs"
                    / dictionary.name
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
