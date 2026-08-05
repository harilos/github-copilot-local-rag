from __future__ import annotations

import json
import re
import sqlite3
import stat
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable, Sequence

LOCK_SCHEMA = "local-rag.windows-runtime-lock.v1"
VERSION_SCHEMA = "local-rag.db-version.v1"
MAX_JSON_BYTES = 1024 * 1024


def normalize_distribution_name(value: str) -> str:
    """Return the PEP 503 normalized distribution name."""

    return re.sub(r"[-_.]+", "-", str(value).strip()).casefold()


@dataclass(frozen=True)
class TokenizerContract:
    implementation_distribution: str
    implementation_version: str
    dictionary_distribution: str
    dictionary_version: str
    split_mode: str
    occurrences: str
    fingerprint_schema: str
    fingerprint: str
    catalog_schema_version: int

    @property
    def descriptor(self) -> dict[str, str]:
        return {
            "mode": "sudachi",
            "implementation": normalize_distribution_name(
                self.implementation_distribution
            ),
            "implementation_version": self.implementation_version,
            "dictionary": normalize_distribution_name(
                self.dictionary_distribution
            ),
            "dictionary_version": self.dictionary_version,
            "split_mode": self.split_mode,
            "occurrences": self.occurrences,
        }


class WindowsTokenizerContractError(RuntimeError):
    """A bounded tokenizer contract failure usable in the search-only runtime."""


class DatabaseTokenizerCompatibilityError(WindowsTokenizerContractError):
    """A bounded, non-sensitive package compatibility failure."""

    def __init__(
        self,
        database: str,
        expected: str,
        actual: str,
        *,
        reason: str,
    ) -> None:
        self.database = _bounded_database_name(database)
        self.expected = _bounded_fingerprint(expected)
        self.actual = _bounded_fingerprint(actual)
        self.reason = _bounded_reason(reason)
        super().__init__(str(self))

    def __str__(self) -> str:
        return "\n".join(
            (
                "windows_offline_database_tokenizer_mismatch",
                f"database={self.database}",
                f"expected={self.expected}",
                f"actual={self.actual}",
                "action=rebuild_database_with_distribution_tokenizer",
            )
        )


def load_tokenizer_contract(lock_path: Path) -> TokenizerContract:
    payload = _read_json_object(lock_path, label="runtime_lock")
    tokenizer = payload.get("tokenizer")
    database = payload.get("database_contract")
    if (
        payload.get("schema") != LOCK_SCHEMA
        or not isinstance(tokenizer, dict)
        or not isinstance(database, dict)
    ):
        raise WindowsTokenizerContractError("windows_runtime_tokenizer_lock_invalid")
    implementation = tokenizer.get("implementation")
    dictionary = tokenizer.get("dictionary")
    if not isinstance(implementation, dict) or not isinstance(dictionary, dict):
        raise WindowsTokenizerContractError("windows_runtime_tokenizer_lock_invalid")
    contract = TokenizerContract(
        implementation_distribution=str(
            implementation.get("distribution") or ""
        ),
        implementation_version=str(implementation.get("version") or ""),
        dictionary_distribution=str(dictionary.get("distribution") or ""),
        dictionary_version=str(dictionary.get("version") or ""),
        split_mode=str(tokenizer.get("split_mode") or ""),
        occurrences=str(tokenizer.get("occurrences") or ""),
        fingerprint_schema=str(tokenizer.get("fingerprint_schema") or ""),
        fingerprint=str(tokenizer.get("fingerprint") or ""),
        catalog_schema_version=_positive_integer(
            database.get("catalog_schema_version")
        ),
    )
    expected = (
        f"{contract.fingerprint_schema}"
        f":{normalize_distribution_name(contract.implementation_distribution)}-"
        f"{contract.implementation_version}"
        f":{normalize_distribution_name(contract.dictionary_distribution)}-"
        f"{contract.dictionary_version}"
    )
    if (
        not contract.implementation_distribution
        or not contract.implementation_version
        or not contract.dictionary_distribution
        or not contract.dictionary_version
        or contract.split_mode != "A"
        or contract.occurrences != "preserved"
        or contract.fingerprint_schema != "sudachi-a-v3-tf"
        or contract.fingerprint != expected
        or database.get("version_schema") != VERSION_SCHEMA
    ):
        raise WindowsTokenizerContractError("windows_runtime_tokenizer_lock_invalid")
    return contract


def validate_runtime_tokenizer_packages(
    contract: TokenizerContract,
    *,
    version_provider: Callable[[str], str] = importlib_metadata.version,
) -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution, expected in (
        (
            contract.implementation_distribution,
            contract.implementation_version,
        ),
        (contract.dictionary_distribution, contract.dictionary_version),
    ):
        normalized = normalize_distribution_name(distribution)
        try:
            actual = str(version_provider(distribution))
        except importlib_metadata.PackageNotFoundError as exc:
            raise WindowsTokenizerContractError(
                "windows_runtime_tokenizer_dependency_missing:"
                + normalized
            ) from exc
        if actual != expected:
            raise WindowsTokenizerContractError(
                "windows_runtime_tokenizer_dependency_mismatch:"
                f"{normalized}:expected={expected}:actual={_bounded_fingerprint(actual)}"
            )
        versions[normalized] = actual
    return versions


def validate_distribution_databases(
    dbs_root: Path,
    database_names: Sequence[str],
    *,
    lock_path: Path,
) -> list[dict[str, Any]]:
    contract = load_tokenizer_contract(lock_path)
    results: list[dict[str, Any]] = []
    for database_name in database_names:
        results.append(
            validate_distribution_database(
                dbs_root / database_name,
                database_name,
                contract=contract,
            )
        )
    return results


def validate_distribution_database(
    database_root: Path,
    database_name: str,
    *,
    contract: TokenizerContract,
) -> dict[str, Any]:
    name = _bounded_database_name(database_name)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*-rag", name)
        or not database_root.is_dir()
        or database_root.is_symlink()
        or _is_reparse_point(database_root)
    ):
        _failure(name, contract, "invalid", "database_layout")

    version = _database_json(
        database_root / "VERSION.json",
        name,
        contract,
        "version",
    )
    manifest = _database_json(
        database_root / "index" / "manifest.json",
        name,
        contract,
        "manifest",
    )
    if version.get("schema") != VERSION_SCHEMA:
        _failure(name, contract, "unsupported", "version_schema")
    if manifest.get("catalog_schema_version") != contract.catalog_schema_version:
        _failure(name, contract, "unsupported", "manifest_schema")

    expected_descriptor = contract.descriptor
    for label, payload in (("version", version), ("manifest", manifest)):
        actual = payload.get("tokenizer")
        if actual != contract.fingerprint:
            _failure(name, contract, actual, f"{label}_tokenizer")
        if payload.get("tokenizer_config") != expected_descriptor:
            _failure(name, contract, "invalid", f"{label}_tokenizer_config")

    catalog = database_root / "catalog.sqlite"
    if not _is_regular_file(catalog) or catalog.stat().st_size <= 0:
        _failure(name, contract, "missing", "catalog")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(catalog) + suffix)
        try:
            if sidecar.exists() and sidecar.stat().st_size > 0:
                _failure(name, contract, "uncheckpointed", "catalog_wal")
        except OSError:
            _failure(name, contract, "unreadable", "catalog_wal")

    connection: sqlite3.Connection | None = None
    try:
        uri = catalog.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=0)
        connection.execute("PRAGMA query_only = ON")
        integrity = connection.execute("PRAGMA quick_check").fetchall()
        if integrity != [("ok",)]:
            _failure(name, contract, "corrupt", "catalog_integrity")
        rows = connection.execute(
            "SELECT key, value FROM database_meta "
            "WHERE key IN ('schema_version', 'tokenizer')"
        ).fetchall()
    except DatabaseTokenizerCompatibilityError:
        raise
    except (OSError, sqlite3.Error, ValueError):
        _failure(name, contract, "unreadable", "catalog_read")
    finally:
        if connection is not None:
            connection.close()

    metadata: dict[str, str] = {}
    for key, value in rows:
        normalized_key = str(key)
        if normalized_key in metadata:
            _failure(name, contract, "duplicate", "catalog_metadata")
        metadata[normalized_key] = str(value)
    if set(metadata) != {"schema_version", "tokenizer"}:
        _failure(name, contract, "missing", "catalog_metadata")
    if metadata["schema_version"] != str(contract.catalog_schema_version):
        _failure(name, contract, "unsupported", "catalog_schema")
    if metadata["tokenizer"] != contract.fingerprint:
        _failure(name, contract, metadata["tokenizer"], "catalog_tokenizer")
    if not (
        version["tokenizer"]
        == manifest["tokenizer"]
        == metadata["tokenizer"]
        == contract.fingerprint
    ):
        _failure(name, contract, "inconsistent", "tokenizer_consistency")
    return {
        "database": name,
        "tokenizer": contract.fingerprint,
        "schema_version": contract.catalog_schema_version,
        "status": "pass",
    }


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        if not _is_regular_file(path):
            raise OSError
        raw = path.read_bytes()
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise WindowsTokenizerContractError(f"windows_{label}_invalid") from exc
    if not isinstance(payload, dict):
        raise WindowsTokenizerContractError(f"windows_{label}_invalid")
    return payload


def _database_json(
    path: Path,
    database: str,
    contract: TokenizerContract,
    label: str,
) -> dict[str, Any]:
    try:
        return _read_json_object(path, label=f"database_{label}")
    except WindowsTokenizerContractError:
        _failure(database, contract, "missing_or_invalid", label)


def _failure(
    database: str,
    contract: TokenizerContract,
    actual: object,
    reason: str,
) -> None:
    raise DatabaseTokenizerCompatibilityError(
        database,
        contract.fingerprint,
        str(actual or "missing"),
        reason=reason,
    )


def _positive_integer(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return result if result > 0 and str(result) == str(value) else 0


def _is_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and not bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _bounded_database_name(value: object) -> str:
    text = str(value or "unknown")
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", text) else "invalid"


def _bounded_fingerprint(value: object) -> str:
    text = str(value or "missing")
    if len(text) > 180 or not re.fullmatch(r"[A-Za-z0-9_.:+-]+", text):
        return "invalid"
    return text


def _bounded_reason(value: object) -> str:
    text = str(value or "unknown")
    return text if re.fullmatch(r"[a-z0-9_]{1,80}", text) else "unknown"


__all__ = [
    "DatabaseTokenizerCompatibilityError",
    "TokenizerContract",
    "WindowsTokenizerContractError",
    "load_tokenizer_contract",
    "normalize_distribution_name",
    "validate_distribution_database",
    "validate_distribution_databases",
    "validate_runtime_tokenizer_packages",
]
