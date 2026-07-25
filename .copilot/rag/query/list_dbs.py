from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
DBS_ROOT = Path(os.getenv("RAG_DBS_ROOT", str(RAG_ROOT / "dbs"))).expanduser().resolve()
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.dbs import list_db_names, read_db_config, read_profile_hint


def main() -> None:
    dbs = []
    for name in list_db_names(DBS_ROOT):
        root = DBS_ROOT / name
        dbs.append({"db": name, "config": read_db_config(root), "hint": read_profile_hint(root, max_chars=240)})
    print(json.dumps({"dbs": dbs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
