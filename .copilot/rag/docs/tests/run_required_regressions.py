from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


RAG_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = RAG_ROOT.parents[1]
SOFTWARE_RAG_TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
_RAN_TESTS = re.compile(r"Ran\s+(\d+)\s+tests?\s+in")
_SKIPPED_TESTS = re.compile(r"skipped=(\d+)")


def discover_test_files(root: Path = RAG_ROOT) -> list[Path]:
    return sorted(
        (
            path.resolve()
            for path in Path(root).rglob("test_*.py")
            if "__pycache__" not in path.parts
        ),
        key=lambda path: path.as_posix().casefold(),
    )


def test_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    roots = [str(RAG_ROOT), str(SOFTWARE_RAG_TOOL_ROOT)]
    current = environment.get("PYTHONPATH")
    if current:
        roots.append(current)
    environment["PYTHONPATH"] = os.pathsep.join(roots)
    environment.setdefault("PYTHONUTF8", "1")
    return environment


def parse_test_counts(output: str) -> tuple[int, int]:
    ran = _RAN_TESTS.search(output)
    skipped = _SKIPPED_TESTS.search(output)
    return (
        int(ran.group(1)) if ran else 0,
        int(skipped.group(1)) if skipped else 0,
    )


def run_files(files: Iterable[Path]) -> dict[str, Any]:
    requested = [Path(path).resolve() for path in files]
    results: list[dict[str, Any]] = []
    for path in requested:
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=REPOSITORY_ROOT,
            env=test_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        combined = "\n".join(
            value for value in (completed.stdout, completed.stderr) if value
        )
        tests, skipped = parse_test_counts(combined)
        result = {
            "file": path.relative_to(REPOSITORY_ROOT).as_posix(),
            "returncode": completed.returncode,
            "tests": tests,
            "skipped": skipped,
        }
        results.append(result)
        print(
            f"[{('PASS' if completed.returncode == 0 else 'FAIL')}] "
            f"{result['file']} tests={tests} skipped={skipped}",
            flush=True,
        )
        if completed.returncode != 0:
            print(combined, flush=True)
    executed = {Path(result["file"]).as_posix() for result in results}
    expected = {
        path.relative_to(REPOSITORY_ROOT).as_posix() for path in requested
    }
    missing = sorted(expected - executed)
    summary = {
        "test_files": len(requested),
        "executed_files": len(results),
        "tests": sum(int(result["tests"]) for result in results),
        "skipped": sum(int(result["skipped"]) for result in results),
        "failed_files": [
            result["file"]
            for result in results
            if int(result["returncode"]) != 0
        ],
        "missing_files": missing,
    }
    print("LOCAL_RAG_REGRESSION_SUMMARY=" + json.dumps(summary, sort_keys=True))
    return summary


def main() -> int:
    files = discover_test_files()
    summary = run_files(files)
    return 1 if summary["failed_files"] or summary["missing_files"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
