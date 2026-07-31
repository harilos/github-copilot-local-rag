from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from source_manager import execution, providers
from source_manager.errors import SourceManagerError
from source_manager.git_source import _git_fetch, git_updated_on_cutoff


class GenericGitSourceTests(unittest.TestCase):
    def test_provider_accepts_gitlab_and_normalizes_scope_and_days(self) -> None:
        value = providers.validate_provider_config(
            "github",
            {
                "repository_url": "https://gitlab.example/group/project.git",
                "include_paths": ["docs", "docs/api", "specifications"],
                "updated_within_days": "90",
            },
        )
        self.assertEqual(
            "https://gitlab.example/group/project.git",
            value["repository_url"],
        )
        self.assertEqual(["docs", "specifications"], value["include_paths"])
        self.assertEqual(90, value["updated_within_days"])

    def test_provider_rejects_vcs_metadata_and_invalid_period(self) -> None:
        with self.assertRaises(SourceManagerError):
            providers.validate_provider_config(
                "github",
                {
                    "repository_url": "https://gitlab.example/group/project.git",
                    "include_paths": ["docs/.git/private"],
                    "updated_within_days": None,
                },
            )
        with self.assertRaises(SourceManagerError):
            providers.validate_provider_config(
                "github",
                {
                    "repository_url": "https://gitlab.example/group/project.git",
                    "include_paths": [],
                    "updated_within_days": 0,
                },
            )

    def test_cutoff_uses_saved_run_start_time(self) -> None:
        cutoff = git_updated_on_cutoff(
            30,
            {"started_at": "2026-07-31T12:00:00Z"},
            clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
            cutoff,
        )

    @unittest.skipUnless(shutil.which("git"), "git command is required")
    def test_sparse_checkout_and_last_commit_filter_materialize_only_eligible_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self.run_git("init", "-q", str(repository))
            self.run_git("-C", str(repository), "config", "user.email", "test@example.com")
            self.run_git("-C", str(repository), "config", "user.name", "Test")
            (repository / "docs").mkdir()
            (repository / "other").mkdir()
            (repository / "README.md").write_text("root", encoding="utf-8")
            (repository / "docs" / "old.md").write_text("old", encoding="utf-8")
            (repository / "other" / "skip.md").write_text("skip", encoding="utf-8")
            old_environment = {
                **os.environ,
                "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
            }
            self.run_git("-C", str(repository), "add", ".", environment=old_environment)
            self.run_git(
                "-C",
                str(repository),
                "commit",
                "-qm",
                "old",
                environment=old_environment,
            )
            (repository / "docs" / "new.md").write_text("new", encoding="utf-8")
            recent_environment = {
                **os.environ,
                "GIT_AUTHOR_DATE": "2026-07-30T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-07-30T00:00:00Z",
            }
            self.run_git("-C", str(repository), "add", ".", environment=recent_environment)
            self.run_git(
                "-C",
                str(repository),
                "commit",
                "-qm",
                "recent",
                environment=recent_environment,
            )
            remote = root / "remote.git"
            self.run_git("clone", "-q", "--bare", str(repository), str(remote))

            work = (
                root
                / "db"
                / "sources"
                / "src_git-000000000000"
                / "work"
                / "ingest"
                / "src_git-000000000000"
            )
            work.mkdir(parents=True)
            result = _git_fetch(
                {
                    "repository_url": str(remote),
                    "include_paths": ["docs"],
                    "updated_within_days": 30,
                },
                work,
                execution._run_command,
                updated_on_cutoff=datetime(
                    2026,
                    7,
                    1,
                    tzinfo=timezone.utc,
                ),
                execution=execution,
            )
            files = sorted(
                path.relative_to(work).as_posix()
                for path in work.rglob("*")
                if path.is_file()
            )
            self.assertEqual(["docs/new.md"], files)
            self.assertEqual(2, result["inventory_documents"])
            self.assertEqual(1, result["eligible_documents"])

    def run_git(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> None:
        subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )


if __name__ == "__main__":
    unittest.main()
