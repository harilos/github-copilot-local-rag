from __future__ import annotations

from . import database_list as _database_list


_database_list.TYPE_LABELS["github"] = "GitHub"
_database_list.TYPE_LABELS["gitlab"] = "GitLab"
_database_list.TYPE_LABELS["azure_devops"] = "Azure DevOps"
_database_list.TYPE_LABELS["git"] = "その他のGit"
_database_list.TYPE_LABELS["teams"] = "Microsoft Teams"
_database_list.TYPE_LABELS["gitlab_wiki"] = "GitLab Wiki"
main = _database_list.main


__all__ = ["main"]
