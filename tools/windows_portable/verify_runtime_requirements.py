from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
from pathlib import Path


_PIN = re.compile(
    r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^;\s]+)(?:\s*;.*)?$"
)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_lock(path: Path, *, seen: set[Path] | None = None) -> dict[str, tuple[str, str]]:
    resolved = path.expanduser().resolve(strict=True)
    visited = set() if seen is None else seen
    if resolved in visited:
        raise ValueError(f"recursive requirement include: {resolved.name}")
    visited.add(resolved)
    pins: dict[str, tuple[str, str]] = {}
    for raw in resolved.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            included = _load_lock(resolved.parent / line[3:].strip(), seen=visited)
            for name, value in included.items():
                if name in pins and pins[name] != value:
                    raise ValueError(f"conflicting requirement pin: {value[0]}")
                pins[name] = value
            continue
        match = _PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"requirement is not an exact pin: {line}")
        normalized = _normalize(match.group(1))
        value = (match.group(1), match.group(2))
        if normalized in pins and pins[normalized] != value:
            raise ValueError(f"conflicting requirement pin: {value[0]}")
        pins[normalized] = value
    visited.remove(resolved)
    if not pins:
        raise ValueError("runtime lock is empty")
    return pins


def verify(
    lock: Path,
    *,
    python_version: str,
    site_packages: Path | None = None,
) -> dict[str, object]:
    pins = _load_lock(lock)
    distributions = (
        importlib.metadata.distributions(
            path=[str(site_packages.resolve(strict=True))]
        )
        if site_packages
        else importlib.metadata.distributions()
    )
    installed = {
        _normalize(str(distribution.metadata.get("Name") or "")): distribution.version
        for distribution in distributions
        if distribution.metadata.get("Name")
    }
    mismatches = [
        {
            "name": display,
            "expected": expected,
            "actual": installed.get(name),
        }
        for name, (display, expected) in sorted(pins.items())
        if installed.get(name) != expected
    ]
    actual_python = platform.python_version()
    return {
        "schema": "local-rag-portable-runtime-requirements-v1",
        "status": "pass" if actual_python == python_version and not mismatches else "fail",
        "python_expected": python_version,
        "python_actual": actual_python,
        "checked_requirements": len(pins),
        "mismatches": mismatches,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify an offline runtime against exact pins")
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--site-packages", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = verify(
            args.lock,
            python_version=args.python_version,
            site_packages=args.site_packages,
        )
    except (OSError, ValueError) as exc:
        result = {
            "schema": "local-rag-portable-runtime-requirements-v1",
            "status": "error",
            "error": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
