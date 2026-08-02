#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parent
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from wrapper.teams_database_list import main


if __name__ == "__main__":
    raise SystemExit(main())
