from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import Any


_ABSOLUTE_USER_PATH_PATTERNS = (
    re.compile(r"(?<![<\w])/(?:Users|home)/(?!<)[^/\s\"']+"),
    re.compile(
        r"(?i)(?<![<\w])[A-Z]:(?:\\\\|\\)Users"
        r"(?:\\\\|\\)(?!<)[^\\/\s\"']+"
    ),
    re.compile(
        r"(?i)(?<![<\w])[A-Z]:/Users/"
        r"(?!<)[^\\/\s\"']+"
    ),
)


def scan_tracked_hygiene(
    repo_root: Path,
    *,
    sensitive_terms_file: Path | None = None,
) -> list[dict[str, Any]]:
    root = repo_root.expanduser().resolve()
    tracked = _tracked_files(root)
    indexed = _indexed_files(root)
    denylist_path = sensitive_terms_file
    if denylist_path is None:
        configured = os.getenv("RAG_SENSITIVE_TERMS_FILE", "").strip()
        denylist_path = Path(configured).expanduser() if configured else None
    findings: list[dict[str, Any]] = []
    literals: list[str] = []
    denylist_relative = ""
    if denylist_path is not None:
        resolved_denylist = denylist_path.resolve(strict=True)
        try:
            denylist_relative = resolved_denylist.relative_to(root).as_posix()
        except ValueError:
            denylist_relative = ""
        if denylist_relative and denylist_relative in indexed:
            findings.append(
                {
                    "kind": "tracked_sensitive_terms_file",
                    "path": denylist_relative,
                    "line": 1,
                }
            )
        literals = [
            line.strip()
            for line in resolved_denylist.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    for relative in sorted(tracked):
        if relative == denylist_relative:
            continue
        path = root / relative
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in _ABSOLUTE_USER_PATH_PATTERNS):
                findings.append(
                    {
                        "kind": "absolute_user_path",
                        "path": relative,
                        "line": line_number,
                    }
                )
            if literals and any(literal in line for literal in literals):
                findings.append(
                    {
                        "kind": "sensitive_term",
                        "path": relative,
                        "line": line_number,
                    }
                )
    return findings


def _tracked_files(repo_root: Path) -> set[str]:
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return {
        value.decode("utf-8")
        for value in completed.stdout.split(b"\0")
        if value
    }


def _indexed_files(repo_root: Path) -> set[str]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "-z"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return {
        value.decode("utf-8")
        for value in completed.stdout.split(b"\0")
        if value
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan tracked text without disclosing sensitive literals."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
    )
    parser.add_argument("--sensitive-terms-file", type=Path)
    args = parser.parse_args()
    findings = scan_tracked_hygiene(
        args.repo_root,
        sensitive_terms_file=args.sensitive_terms_file,
    )
    if findings:
        for finding in findings:
            print("sensitive_term_match_detected")
            print(finding["path"])
            print(finding["line"])
        return 1
    print("source_hygiene_pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
