from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def lifecycle_api() -> Any:
    tool_root = Path(__file__).resolve().parents[1] / "gen_db" / "software_rag_tool"
    if str(tool_root) not in sys.path:
        sys.path.insert(0, str(tool_root))
    from software_rag_tool import data_lifecycle

    return data_lifecycle


def require_ready_database(db_root: Path) -> str:
    """Fail closed for reset generations while preserving markerless DBs."""
    api = lifecycle_api()
    epoch = api.capture_ready_epoch(Path(db_root))
    marker = api.read_lifecycle(Path(db_root))
    if marker is not None and marker.status != api.READY:
        raise api.DataLifecycleError("database refresh is incomplete for export or copy")
    return str(epoch)
