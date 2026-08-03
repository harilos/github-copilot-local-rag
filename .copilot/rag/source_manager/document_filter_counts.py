from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, Mapping


FILE_SELECTION_KEY = "file_selection"
FILE_SELECTION_DOCUMENTS = "documents_only"
DOCUMENT_ONLY_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".log",
        ".pdf",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xlsx",
        ".asta",
        ".pu",
        ".puml",
        ".plantuml",
    }
)
_MARKER = "_local_rag_document_filter_count_runtime_installed"


def is_office_temporary_file(path: Path | str) -> bool:
    return Path(path).name.startswith("~$")


def install_document_filter_count_runtime() -> None:
    """Align the approximate preflight count with the selected file set."""

    from . import execution, runner

    if bool(getattr(execution, _MARKER, False)):
        return
    original = execution.execute_fetch_plan

    @functools.wraps(original)
    def execute_fetch_plan(
        plan: Mapping[str, Any],
        work_directory: Path,
        state: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = dict(original(plan, work_directory, state, **kwargs))
        if _plan_selection(plan) != FILE_SELECTION_DOCUMENTS:
            return result
        root_value = result.get("external_add_root") or work_directory
        root = Path(str(root_value))
        result["documents"] = count_document_files(root)
        return result

    execution.execute_fetch_plan = execute_fetch_plan
    runner.execute_fetch_plan = execute_fetch_plan
    setattr(execution, _MARKER, True)


def count_document_files(root: Path) -> int:
    value = Path(root)
    if value.is_symlink():
        raise OSError("document count root must not be a symlink")
    if value.is_file():
        return int(
            not is_office_temporary_file(value)
            and value.suffix.lower() in DOCUMENT_ONLY_EXTENSIONS
        )
    if not value.is_dir():
        return 0
    count = 0

    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, child_directories, filenames in os.walk(
        value,
        topdown=True,
        onerror=raise_walk_error,
        followlinks=False,
    ):
        current = Path(directory)
        child_directories[:] = sorted(
            name
            for name in child_directories
            if not (current / name).is_symlink()
        )
        for filename in sorted(filenames):
            if is_office_temporary_file(filename):
                continue
            path = current / filename
            if (
                not path.is_symlink()
                and path.is_file()
                and path.suffix.lower() in DOCUMENT_ONLY_EXTENSIONS
            ):
                count += 1
    return count


def _plan_selection(plan: Mapping[str, Any]) -> str:
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return ""
    step = steps[0]
    if not isinstance(step, Mapping):
        return ""
    parameters = step.get("parameters")
    if not isinstance(parameters, Mapping):
        return ""
    return str(parameters.get(FILE_SELECTION_KEY) or "").strip().lower()


__all__ = [
    "DOCUMENT_ONLY_EXTENSIONS",
    "count_document_files",
    "is_office_temporary_file",
    "install_document_filter_count_runtime",
]
