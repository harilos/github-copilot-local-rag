from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from source_manager import execution, networking, providers
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

    def test_provider_accepts_legacy_joined_include_paths(self) -> None:
        value = providers.validate_provider_config(
            "github",
            {
                "repository_url": "https://git.example/group/project.git",
                "include_paths": "docs / specifications/api / src",
                "updated_within_days": None,
            },
        )

        self.assertEqual(
            ["docs", "specifications/api", "src"],
            value["include_paths"],
        )

    def test_source_command_timeout_defaults_and_honors_environment(self) -> None:
        self.assertEqual(1800.0, networking.source_command_timeout_seconds({}))
        self.assertEqual(
            2400.0,
            networking.source_command_timeout_seconds(
                {"LOCAL_RAG_SOURCE_CMD_TIMEOUT_SECONDS": "2400"}
            ),
        )

        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.dict(
                execution.os.environ,
                {"LOCAL_RAG_SOURCE_CMD_TIMEOUT_SECONDS": "2400"},
                clear=False,
            ),
            mock.patch.object(
                execution.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            execution._run_command(["git", "--version"])

        self.assertEqual(2400.0, run.call_args.kwargs["timeout"])

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

    def test_fetch_removes_legacy_pointer_and_enables_long_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
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
            control = work.parent.parent / "provider" / ".git"
            control.mkdir(parents=True)
            (work / ".git").write_text(
                f"gitdir: {control}\n",
                encoding="utf-8",
            )
            commands: list[list[str]] = []

            def runner(arguments, **_kwargs):
                command = list(arguments)
                commands.append(command)
                stdout = "origin/main\n" if "symbolic-ref" in command else ""
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

            result = _git_fetch(
                {
                    "repository_url": "https://git.example/group/project.git",
                    "include_paths": [],
                    "updated_within_days": 30,
                },
                work,
                runner,
                updated_on_cutoff=datetime(
                    2026,
                    7,
                    1,
                    tzinfo=timezone.utc,
                ),
                execution=execution,
            )

            self.assertEqual(0, result["documents"])
            self.assertFalse((work / ".git").exists())
            self.assertGreater(len(commands), 0)
            for command in commands:
                self.assertEqual(
                    ["git", "-c", "core.longpaths=true"],
                    command[:3],
                )

    @unittest.skipUnless(shutil.which("git"), "git command is required")
    def test_no_change_skips_but_change_and_delete_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self.run_git("init", "-q", str(repository))
            self.run_git("-C", str(repository), "config", "user.email", "test@example.com")
            self.run_git("-C", str(repository), "config", "user.name", "Test")
            tracked = repository / "tracked.md"
            tracked.write_text("v1", encoding="utf-8")
            self.run_git("-C", str(repository), "add", ".")
            self.run_git("-C", str(repository), "commit", "-qm", "initial")
            remote = root / "remote.git"
            self.run_git("clone", "-q", "--bare", str(repository), str(remote))
            self.run_git("-C", str(repository), "remote", "add", "origin", str(remote))

            work = (
                root / "db" / "sources" / "src_git-000000000000"
                / "work" / "ingest" / "src_git-000000000000"
            )
            work.mkdir(parents=True)
            settings = {
                "repository_url": str(remote),
                "include_paths": [],
                "updated_within_days": None,
            }
            first = _git_fetch(
                settings,
                work,
                execution._run_command,
                updated_on_cutoff=None,
                execution=execution,
            )
            unchanged = _git_fetch(
                settings,
                work,
                execution._run_command,
                updated_on_cutoff=None,
                execution=execution,
                previous_run_complete=True,
            )
            self.assertNotIn("no_change", first)
            self.assertTrue(unchanged["no_change"])

            tracked.write_text("v2", encoding="utf-8")
            self.run_git("-C", str(repository), "add", ".")
            self.run_git("-C", str(repository), "commit", "-qm", "change")
            self.run_git("-C", str(repository), "push", "-q", "origin", "HEAD")
            changed = _git_fetch(
                settings,
                work,
                execution._run_command,
                updated_on_cutoff=None,
                execution=execution,
                previous_run_complete=True,
            )
            self.assertNotIn("no_change", changed)
            self.assertEqual("v2", (work / "tracked.md").read_text(encoding="utf-8"))

            tracked.unlink()
            self.run_git("-C", str(repository), "add", "-A")
            self.run_git("-C", str(repository), "commit", "-qm", "delete")
            self.run_git("-C", str(repository), "push", "-q", "origin", "HEAD")
            deleted = _git_fetch(
                settings,
                work,
                execution._run_command,
                updated_on_cutoff=None,
                execution=execution,
                previous_run_complete=True,
            )
            self.assertNotIn("no_change", deleted)
            self.assertFalse((work / "tracked.md").exists())

    @unittest.skipUnless(shutil.which("git"), "git command is required")
    def test_cutoff_disables_revision_only_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            self.run_git("init", "-q", str(repository))
            self.run_git("-C", str(repository), "config", "user.email", "test@example.com")
            self.run_git("-C", str(repository), "config", "user.name", "Test")
            (repository / "tracked.md").write_text("v1", encoding="utf-8")
            self.run_git("-C", str(repository), "add", ".")
            self.run_git("-C", str(repository), "commit", "-qm", "initial")
            remote = root / "remote.git"
            self.run_git("clone", "-q", "--bare", str(repository), str(remote))
            work = root / "db" / "sources" / "src" / "work" / "ingest" / "src"
            work.mkdir(parents=True)
            settings = {"repository_url": str(remote), "include_paths": []}
            _git_fetch(
                settings, work, execution._run_command,
                updated_on_cutoff=None, execution=execution,
            )
            result = _git_fetch(
                settings, work, execution._run_command,
                updated_on_cutoff=datetime(2000, 1, 1, tzinfo=timezone.utc),
                execution=execution, previous_run_complete=True,
            )
            self.assertNotIn("no_change", result)

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
