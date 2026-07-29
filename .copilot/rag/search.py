#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


_QUERY_ROOT = Path(__file__).resolve().parent / "query"
if str(_QUERY_ROOT) not in sys.path:
    sys.path.insert(0, str(_QUERY_ROOT))

from reference_contract import (  # noqa: E402
    install_result_bundle_reference_contract,
    install_search_command_reference_contract,
)
from wrapper import search_command  # noqa: E402


install_result_bundle_reference_contract()
install_search_command_reference_contract(search_command)


if __name__ == "__main__":
    raise SystemExit(search_command.main())
