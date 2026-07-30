from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def stop_search_daemon(
    rag_root: str | Path,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Stop the authenticated search daemon without terminating a stale PID."""
    search = _load_query_search(Path(rag_root))
    result = search.stop_persistent_daemon(
        timeout_seconds=timeout_seconds,
    )
    return dict(result)


def _load_query_search(rag_root: Path) -> ModuleType:
    query_root = rag_root.expanduser().resolve() / "query"
    search_path = query_root / "search.py"
    if not search_path.is_file():
        raise ImportError("query/search.py is missing")
    module_name = "_local_rag_query_search_daemon_control"
    specification = importlib.util.spec_from_file_location(
        module_name,
        search_path,
    )
    if specification is None or specification.loader is None:
        raise ImportError("query/search.py cannot be loaded")
    module = importlib.util.module_from_spec(specification)
    inserted = str(query_root) not in sys.path
    if inserted:
        sys.path.insert(0, str(query_root))
    try:
        specification.loader.exec_module(module)
    finally:
        if inserted:
            try:
                sys.path.remove(str(query_root))
            except ValueError:
                pass
    return module
