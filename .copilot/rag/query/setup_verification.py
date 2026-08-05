from __future__ import annotations

import importlib
import importlib.metadata
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any


RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
DBS_ROOT = Path(
    os.getenv("RAG_DBS_ROOT", str(RAG_ROOT / "dbs"))
).expanduser().resolve()
sys.path.insert(0, str(TOOL_ROOT))

REQUIRED_IMPORTS = (
    "packaging",
    "chromadb",
    "numpy",
    "onnxruntime",
    "transformers",
    "optimum",
    "optimum.onnxruntime",
    "sentencepiece",
    "sudachipy",
    "sudachidict_core",
    "pypdf",
    "docx",
    "pptx",
    "openpyxl",
)
REQUIRED_MODEL_FILES = (
    "model.onnx",
    "config.json",
    "tokenizer_config.json",
    "MODEL_MANIFEST.json",
)
def main() -> int:
    result = verify_installation()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("setup_complete") else 1


def verify_installation() -> dict[str, Any]:
    runtime: dict[str, Any] = {
        "venv": "pass",
        "dependencies": "pending",
        "lexical_tokenizer": "pending",
        "requirements": "pending",
        "pip_check": "pending",
        "model_files": "pending",
        "model_manifest": "pending",
        "model_load": "pending",
        "embedding_dimension": None,
        "list_dbs": "pending",
    }
    warnings: list[str] = []
    failed_check: str | None = None
    error_kind: str | None = None

    try:
        for module_name in REQUIRED_IMPORTS:
            importlib.import_module(module_name)
        from software_rag_tool.tokenize import tokenizer_runtime_descriptor

        runtime["lexical_tokenizer_config"] = tokenizer_runtime_descriptor()
        runtime["lexical_tokenizer"] = "pass"
        runtime["dependencies"] = "pass"
    except Exception as exc:
        runtime["dependencies"] = "fail"
        runtime["lexical_tokenizer"] = "fail"
        failed_check = "dependencies"
        error_kind = type(exc).__name__
        return _result(runtime, [], [], warnings, failed_check, error_kind)

    try:
        checked_requirements = _verify_declared_requirements(
            (
                RAG_ROOT / "query" / "requirements.txt",
                RAG_ROOT
                / "gen_db"
                / "software_rag_tool"
                / "requirements.txt",
            )
        )
        runtime["requirements"] = "pass"
        runtime["requirements_checked"] = checked_requirements
    except Exception as exc:
        runtime["requirements"] = "fail"
        failed_check = "requirements"
        error_kind = type(exc).__name__
        return _result(runtime, [], [], warnings, failed_check, error_kind)

    try:
        pip_check = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_offline_environment(),
            timeout=30,
        )
        if pip_check.returncode != 0:
            raise RuntimeError(
                _safe_message(
                    pip_check.stdout or pip_check.stderr or "pip check failed"
                )
            )
        runtime["pip_check"] = "pass"
    except Exception as exc:
        runtime["pip_check"] = "fail"
        failed_check = "pip_check"
        error_kind = type(exc).__name__
        return _result(runtime, [], [], warnings, failed_check, error_kind)

    from software_rag_tool.config import (
        DEFAULT_EMBEDDING_DIMENSION,
        default_onnx_model_dir,
    )
    from software_rag_tool.embeddings import embedding_fingerprint

    model_dir = default_onnx_model_dir()
    missing_files = [
        name
        for name in REQUIRED_MODEL_FILES
        if not _readable_nonempty_file(model_dir / name)
    ]
    tokenizer_files = [
        model_dir / "tokenizer.json",
        model_dir / "tokenizer.model",
    ]
    if not any(_readable_nonempty_file(path) for path in tokenizer_files):
        missing_files.append("tokenizer.json|tokenizer.model")
    if missing_files:
        runtime["model_files"] = "fail"
        runtime["missing_model_files"] = missing_files
        failed_check = "model_files"
        error_kind = "model_files_missing"
        return _result(runtime, [], [], warnings, failed_check, error_kind)
    runtime["model_files"] = "pass"

    try:
        model_manifest = _read_json_object(
            model_dir / "MODEL_MANIFEST.json"
        )
        if model_manifest.get("schema") != "local-rag.onnx-model.v1":
            raise ValueError("unsupported model manifest schema")
        expected_fingerprint = embedding_fingerprint()
        for key, expected in expected_fingerprint.items():
            if model_manifest.get(key) != expected:
                raise ValueError(
                    f"model manifest fingerprint mismatch: {key}"
                )
        if model_manifest.get("source_model") != expected_fingerprint.get(
            "embedding_model"
        ):
            raise ValueError("model manifest source model mismatch")
        runtime["model_manifest"] = "pass"
    except Exception as exc:
        runtime["model_manifest"] = "fail"
        failed_check = "model_manifest"
        error_kind = type(exc).__name__
        return _result(runtime, [], [], warnings, failed_check, error_kind)

    try:
        from software_rag_tool.embeddings import get_embedder

        vector = get_embedder().encode(
            ["Local RAG setup verification"],
            mode="query",
        )[0]
        dimension = len(vector)
        runtime["embedding_dimension"] = dimension
        if (
            dimension != DEFAULT_EMBEDDING_DIMENSION
            or not vector
            or not all(math.isfinite(float(value)) for value in vector)
        ):
            raise ValueError(
                f"expected {DEFAULT_EMBEDDING_DIMENSION} finite dimensions, "
                f"received {dimension}"
            )
        runtime["model_load"] = "pass"
    except Exception as exc:
        runtime["model_load"] = "fail"
        failed_check = "model_load"
        error_kind = type(exc).__name__
        return _result(runtime, [], [], warnings, failed_check, error_kind)

    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(RAG_ROOT / "query" / "list_dbs.py"),
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_offline_environment(),
            timeout=30,
        )
        payload = json.loads(completed.stdout)
        if completed.returncode != 0 or not isinstance(
            payload.get("databases"), list
        ):
            raise ValueError("list_dbs.py did not return the JSON contract")
        runtime["list_dbs"] = "pass"
        discovered = [
            str(item.get("name") or "")
            for item in payload["databases"]
            if item.get("name")
        ]
    except Exception as exc:
        runtime["list_dbs"] = "fail"
        failed_check = "list_dbs"
        error_kind = type(exc).__name__
        return _result(runtime, [], [], warnings, failed_check, error_kind)

    healthy: list[str] = []
    unhealthy: list[dict[str, Any]] = []
    for name in discovered:
        try:
            _verify_database(name)
            healthy.append(name)
        except Exception as exc:
            unhealthy.append(
                {
                    "name": name,
                    "error_kind": type(exc).__name__,
                    "message": _safe_message(str(exc)),
                }
            )
    if unhealthy:
        warnings.append(
            "One or more installed databases failed the read-only health check."
        )
    return _result(
        runtime,
        healthy,
        unhealthy,
        warnings,
        failed_check,
        error_kind,
    )


def _verify_declared_requirements(paths: tuple[Path, ...]) -> int:
    requirements: list[Any] = []
    visited: set[Path] = set()
    for path in paths:
        requirements.extend(_read_requirements(path, visited))

    checked = 0
    for requirement in requirements:
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            installed_version = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"required distribution is not installed: {requirement.name}"
            ) from exc
        if requirement.specifier and not requirement.specifier.contains(
            installed_version,
            prereleases=True,
        ):
            raise RuntimeError(
                f"installed {requirement.name} {installed_version} does not "
                f"satisfy {requirement.specifier}"
            )
        checked += 1
    return checked


def _read_requirements(
    path: Path,
    visited: set[Path],
) -> list[Any]:
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ModuleNotFoundError:
        # Unit/bootstrap environments may expose packaging only through pip.
        # The installed RAG venv still verifies standalone ``packaging`` in
        # REQUIRED_IMPORTS before this parser is reached.
        from pip._vendor.packaging.requirements import (  # type: ignore
            InvalidRequirement,
            Requirement,
        )

    resolved = path.expanduser().resolve()
    if resolved in visited:
        return []
    visited.add(resolved)
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(
            f"requirements file is unreadable: {resolved}"
        ) from exc

    parsed: list[Any] = []
    for line_number, raw_line in enumerate(lines, start=1):
        line = re.split(r"\s+#", raw_line, maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        include = _requirement_include_path(line)
        if include is not None:
            parsed.extend(
                _read_requirements(resolved.parent / include, visited)
            )
            continue
        if line.startswith("-"):
            raise RuntimeError(
                f"unsupported requirements option at "
                f"{resolved}:{line_number}"
            )
        try:
            parsed.append(Requirement(line))
        except InvalidRequirement as exc:
            raise RuntimeError(
                f"invalid requirement at {resolved}:{line_number}"
            ) from exc
    return parsed


def _requirement_include_path(line: str) -> str | None:
    if line.startswith("-r") and line != "-r":
        return line[2:].strip()
    if line.startswith("--requirement="):
        return line.split("=", 1)[1].strip()
    parts = line.split(maxsplit=1)
    if len(parts) == 2 and parts[0] in {"-r", "--requirement"}:
        return parts[1].strip()
    return None


def _verify_database(name: str) -> None:
    from software_rag_tool.catalog import SCHEMA_VERSION
    from software_rag_tool.dbs import collection_name_for_db
    from software_rag_tool.embeddings import embedding_fingerprint
    from software_rag_tool.manifest import validate_embedding_manifest
    from software_rag_tool.tokenize import tokenizer_fingerprint

    database_root = DBS_ROOT.resolve()
    root = (database_root / name).resolve()
    try:
        root.relative_to(database_root)
    except ValueError:
        raise ValueError("database path escapes the database root") from None
    config = _read_json_object(root / "db.json")
    version = _read_json_object(root / "VERSION.json")
    manifest = _read_json_object(root / "index" / "manifest.json")
    if version.get("schema") != "local-rag.db-version.v1":
        raise ValueError("unsupported VERSION.json schema")
    collection_name = collection_name_for_db(name)
    for label, payload in (
        ("db.json", config),
        ("VERSION.json", version),
        ("manifest", manifest),
    ):
        configured_collection = payload.get("collection")
        if (
            configured_collection is not None
            and str(configured_collection) != collection_name
        ):
            raise ValueError(f"collection mismatch in {label}")
    if manifest.get("catalog_schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported catalog schema version in manifest")
    if manifest.get("tokenizer") != tokenizer_fingerprint():
        raise ValueError("unsupported tokenizer version in manifest")
    validate_embedding_manifest(
        manifest,
        collection=collection_name,
    )
    version_embedding = version.get("embedding")
    if not isinstance(version_embedding, dict):
        raise ValueError("VERSION.json embedding fingerprint is missing")
    for key, expected in embedding_fingerprint().items():
        if version_embedding.get(key) != expected:
            raise ValueError(
                f"VERSION.json embedding fingerprint mismatch: {key}"
            )

    catalog_path = root / "catalog.sqlite"
    if not _readable_nonempty_file(catalog_path):
        raise FileNotFoundError("catalog.sqlite is missing or unreadable")
    _reject_nonempty_wal(catalog_path)
    uri = f"{catalog_path.resolve().as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        metadata = {
            str(key): str(value)
            for key, value in connection.execute(
                "SELECT key, value FROM database_meta"
            )
        }
        catalog_count = int(
            connection.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
        )
    finally:
        connection.close()
    if metadata.get("schema_version") != str(SCHEMA_VERSION):
        raise ValueError("unsupported catalog schema version")
    if metadata.get("tokenizer") != tokenizer_fingerprint():
        raise ValueError("unsupported catalog tokenizer version")

    chroma_dir = root / "index" / "chroma"
    if not chroma_dir.is_dir():
        raise FileNotFoundError("Chroma directory is missing")
    chroma_db = chroma_dir / "chroma.sqlite3"
    if not _readable_nonempty_file(chroma_db):
        raise FileNotFoundError("Chroma metadata database is missing")
    _reject_nonempty_wal(chroma_db)
    chroma_uri = f"{chroma_db.resolve().as_uri()}?mode=ro&immutable=1"
    chroma_connection = sqlite3.connect(chroma_uri, uri=True)
    try:
        collection_row = chroma_connection.execute(
            "SELECT id, dimension FROM collections WHERE name = ?",
            (collection_name,),
        ).fetchone()
        if collection_row is None:
            raise ValueError("Chroma collection is not readable")
        collection_id, collection_dimension = collection_row
        if int(collection_dimension or 0) != 256:
            raise ValueError("Chroma collection dimension is unsupported")
        chroma_count = int(
            chroma_connection.execute(
                """
                SELECT COUNT(*)
                FROM embeddings AS e
                JOIN segments AS s ON s.id = e.segment_id
                WHERE s.collection = ?
                """,
                (collection_id,),
            ).fetchone()[0]
        )
    finally:
        chroma_connection.close()
    manifest_count = int(manifest.get("record_count") or 0)
    if not (
        catalog_count == chroma_count
        and (manifest_count == 0 or manifest_count == chroma_count)
    ):
        raise ValueError(
            "critical catalog/collection count inconsistency "
            f"(catalog={catalog_count}, chroma={chroma_count}, "
            f"manifest={manifest_count})"
        )


def _result(
    runtime: dict[str, Any],
    healthy: list[str],
    unhealthy: list[dict[str, Any]],
    warnings: list[str],
    failed_check: str | None,
    error_kind: str | None,
) -> dict[str, Any]:
    setup_complete = failed_check is None
    lookup_ready = setup_complete and bool(healthy)
    if not setup_complete:
        status = "error"
        next_action = _next_action(failed_check)
    elif healthy:
        status = "ready"
        next_action = None
    elif unhealthy:
        status = "runtime_ready_db_unhealthy"
        next_action = "Repair, replace, or rebuild the unhealthy database."
    else:
        status = "runtime_ready_no_db"
        warnings = [
            *warnings,
            "The RAG runtime is ready, but no searchable database is installed.",
        ]
        next_action = "Copy an existing database or build a new database."
    payload: dict[str, Any] = {
        "status": status,
        "setup_complete": setup_complete,
        "lookup_ready": lookup_ready,
        "runtime": runtime,
        "databases": {
            "healthy": healthy,
            "unhealthy": unhealthy,
        },
        "warnings": warnings,
        "next_action": next_action,
    }
    if failed_check:
        payload["failed_check"] = failed_check
    if error_kind:
        payload["error_kind"] = error_kind
    return payload


def _next_action(failed_check: str | None) -> str:
    if failed_check in {"model_files", "model_load"}:
        return (
            "Run normal setup again. If TLS fails, confirm the company CA "
            "certificate and proxy configuration."
        )
    if failed_check in {"dependencies", "requirements"}:
        return "Run normal setup again to install the required dependencies."
    return "Run normal setup again and inspect the sanitized stderr diagnostics."


def _read_json_object(path: Path) -> dict[str, Any]:
    if not _readable_nonempty_file(path):
        raise FileNotFoundError(f"{path.name} is missing or unreadable")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _readable_nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0 and os.access(path, os.R_OK)
    except OSError:
        return False


def _reject_nonempty_wal(database: Path) -> None:
    wal = Path(f"{database}-wal")
    try:
        if wal.is_file() and wal.stat().st_size > 0:
            raise ValueError(
                f"{database.name} has an uncheckpointed write-ahead log"
            )
    except OSError as exc:
        raise ValueError(
            f"could not inspect the write-ahead log for {database.name}"
        ) from exc


def _offline_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
    environment["ANONYMIZED_TELEMETRY"] = "False"
    return environment


def _safe_message(message: str) -> str:
    return " ".join(message.replace("\r", " ").replace("\n", " ").split())[:500]


if __name__ == "__main__":
    raise SystemExit(main())
