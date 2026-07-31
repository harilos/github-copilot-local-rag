from __future__ import annotations

import contextvars
import functools
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

_CONTEXT_DB_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "local_rag_gitlab_wiki_db_root",
    default=None,
)
_MARKER = "_local_rag_gitlab_wiki_path_fix_installed"


def install_gitlab_wiki_path_fix() -> None:
    """Resolve DB-relative Wiki work paths before provider-owned-tree checks."""

    from . import gitlab_wiki_runtime, runner

    if bool(getattr(runner, _MARKER, False)):
        return
    original_validate = gitlab_wiki_runtime.validate_gitlab_wiki_work_tree
    original_update = runner.update_source

    @functools.wraps(original_validate)
    def validate_gitlab_wiki_work_tree(
        settings: Mapping[str, Any],
        work: Path | str,
        *,
        expected_documents: int | None = None,
    ) -> int:
        candidate = Path(work)
        if not candidate.is_absolute():
            db_root = _CONTEXT_DB_ROOT.get()
            if db_root is None:
                raise gitlab_wiki_runtime.SourceManagerError(
                    "GitLab Wiki DB root is unavailable for work-tree validation",
                    stage="reflect.gitlab_wiki",
                )
            relative = PurePosixPath(str(work).replace("\\", "/"))
            if relative.is_absolute() or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                raise gitlab_wiki_runtime.SourceManagerError(
                    "GitLab Wiki work path is invalid",
                    stage="reflect.gitlab_wiki",
                )
            candidate = db_root.joinpath(*relative.parts)
        return original_validate(
            settings,
            candidate,
            expected_documents=expected_documents,
        )

    @functools.wraps(original_update)
    def update_source(
        db_root: Path,
        local_source_key: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        token = _CONTEXT_DB_ROOT.set(Path(db_root).expanduser().resolve())
        try:
            return original_update(db_root, local_source_key, **kwargs)
        finally:
            _CONTEXT_DB_ROOT.reset(token)

    gitlab_wiki_runtime.validate_gitlab_wiki_work_tree = (
        validate_gitlab_wiki_work_tree
    )
    runner.update_source = update_source
    setattr(runner, _MARKER, True)
    package = sys.modules.get(__package__)
    if package is not None:
        setattr(package, "update_source", update_source)


__all__ = ["install_gitlab_wiki_path_fix"]
