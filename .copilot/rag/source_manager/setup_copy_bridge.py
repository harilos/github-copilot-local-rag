from __future__ import annotations

import sys
from pathlib import Path


_RAG_ROOT = Path(__file__).resolve().parents[1]
if str(_RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(_RAG_ROOT))

from setup_copy import restore_portable_database  # noqa: E402


__all__ = ["restore_portable_database"]
