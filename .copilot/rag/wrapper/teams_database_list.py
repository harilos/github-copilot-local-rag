from __future__ import annotations

from . import database_list as _database_list


_database_list.TYPE_LABELS["teams"] = "Microsoft Teams"
main = _database_list.main


__all__ = ["main"]
