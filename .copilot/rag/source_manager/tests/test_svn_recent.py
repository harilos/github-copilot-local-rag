from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.sax.saxutils import quoteattr

from source_manager import (
    SourceManagerError,
    build_fetch_plan,
    execute_fetch_plan,
    validate_provider_config,
)
from source_manager import execution as execution_module
from source_manager import networking as networking_module


def _svn_info_xml(
    checkout: Path,
    files: list[tuple[str, str]],
    *,
    padding: int = 0,
) -> bytes:
    entries = [
        f"<entry kind=\"dir\" path={quoteattr(str(checkout))}/>"
    ]
    entries.extend(
        (
            f"<entry kind=\"file\" "
            f"path={quoteattr(str(checkout / relative))}>"
            f"<commit revision=\"1\"><date>{changed_at}</date></commit>"
            "</entry>"
        )
        for relative, changed_at in files
    )
    filler = f"<!--{'x' * padding}-->" if padding else ""
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        f"<info>{filler}{''.join(entries)}</info>"
    ).encode("utf-8")


class SvnRecentValidationTests(unittest.TestCase):
    def test_svn_transport_url_is_preserved_and_planned(self) -> None:
        repository_url = "svn://127.0.0.1:3690/hogehoge-republic"
        normalized = validate_provider_config(
            "svn",
            {"repository_url": repository_url},
        )
        self.assertEqual(repository_url, normalized["repository_url"])
        plan = build_fetch_plan(
            source_key="src_svn-0123456789ab",
            provider="svn",
            settings=normalized,
            logical_root="sources/src_svn-0123456789ab/work/ingest/src_svn-0123456789ab",
            work_path="sources/src_svn-0123456789ab/work/ingest/src_svn-0123456789ab",
        )
        self.assertEqual("svn_checkout_or_update", plan.steps[0].operation)
        self.assertEqual(
            repository_url,
            plan.steps[0].parameters["repository_url"],
        )

    def test_svn_fetch_url_rejects_unsafe_or_unsupported_values(self) -> None:
        invalid = (
            "svn://user:password@example.com/repository",
            "svn://example.com/repository?token=value",
            "svn://example.com/repository#fragment",
            "svn:///repository",
            "svn://example.com:99999/repository",
            "svn://example.com:/repository",
            "svn://example.com:0/repository",
            "svn://[invalid/repository",
            "svn://exa mple.com/repository",
            "svn://example.com/repository name",
            "svn://example.com/repository%ZZ",
            "svn://example.com/%0Arepository",
            "svn://user%3Apassword%40example.com/repository",
            "svn://example.com%3A0/repository",
            "svn://example.com/repository/%74oken=secret",
            "svn://example.com/repository/%70assword=secret",
            "svn://example.com/../repository",
            "svn://example.com/%2e%2e/repository",
            "svn://example.com/repository%5Coutside",
            "svn://example.com",
            "file:///repository",
            "svn+ssh://example.com/repository",
            "svn://example.com/repository\nnext",
            "svn://example.com/repository?",
            "svn://example.com/repository#",
        )
        for repository_url in invalid:
            with self.subTest(repository_url=repository_url):
                with self.assertRaises(SourceManagerError):
                    validate_provider_config(
                        "svn",
                        {"repository_url": repository_url},
                    )

    def test_recent_window_is_normalized_and_optional(self) -> None:
        base = {
            "repository_url": "https://svn.example.invalid/project",
        }
        self.assertIsNone(
            validate_provider_config("svn", base)["updated_within_days"]
        )
        for value in (1, "30", 3650):
            with self.subTest(value=value):
                normalized = validate_provider_config(
                    "svn",
                    {**base, "updated_within_days": value},
                )
                self.assertEqual(int(value), normalized["updated_within_days"])

    def test_recent_window_rejects_boolean_and_out_of_range_values(self) -> None:
        base = {
            "repository_url": "https://svn.example.invalid/project",
        }
        for value in (True, False, 0, -1, 3651, "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaises(SourceManagerError):
                    validate_provider_config(
                        "svn",
                        {**base, "updated_within_days": value},
                    )

    def test_cutoff_uses_run_start_in_utc_and_is_inclusive(self) -> None:
        cutoff = execution_module._svn_updated_on_cutoff(
            30,
            {"started_at": "2026-07-30T21:00:00+09:00"},
            clock=lambda: datetime(
                2000,
                1,
                1,
                tzinfo=timezone.utc,
            ),
        )
        self.assertEqual(
            datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
            cutoff,
        )

    def test_network_route_forwards_complete_stdout_sink(self) -> None:
        sink = io.BytesIO()
        resolution = SimpleNamespace(
            environment={"ROUTE": "selected"},
            build_url_opener=lambda: object(),
        )
        network = SimpleNamespace(
            resolve_network_configuration=lambda **_kwargs: resolution,
        )
        completed = SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        )
        with (
            mock.patch.object(
                networking_module,
                "_network_module",
                return_value=network,
            ),
            mock.patch.object(
                networking_module,
                "run_streaming_process",
                return_value=completed,
            ) as run,
        ):
            route = networking_module.resolve_source_network_route(
                Path("/synthetic/rag"),
                environment={},
            )
            result = route.command_runner(
                ["svn", "info"],
                stdout_sink=sink,
            )

        self.assertIs(completed, result)
        self.assertTrue(route.command_runner.supports_stdout_sink)
        run.assert_called_once_with(
            ["svn", "info"],
            timeout=1800.0,
            env={"ROUTE": "selected"},
            progress_callback=None,
            stdout_sink=sink,
        )


class SvnRecentFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="svn-recent-")
        self.root = Path(self.temporary.name).resolve()
        self.work = self.root / "managed" / "ingest" / "source"
        self.work.mkdir(parents=True)
        self.checkout = (
            self.work.parent.parent / "provider" / ".svn-worktree"
        )
        self.source_key = "src_fixture-0123456789ab"
        self.relative_work = (
            f"sources/{self.source_key}/work/ingest/{self.source_key}"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _plan(
        self,
        *,
        recursive: bool,
        updated_within_days: int | None = 30,
        repository_url: str = "https://svn.example.invalid/project",
    ) -> dict:
        return build_fetch_plan(
            source_key=self.source_key,
            provider="svn",
            settings={
                "repository_url": repository_url,
                "recursive": recursive,
                "updated_within_days": updated_within_days,
            },
            logical_root=self.relative_work,
            work_path=self.relative_work,
        ).to_dict()

    def _runner(
        self,
        files: list[tuple[str, str, str]],
        *,
        commands: list[list[str]] | None = None,
    ):
        xml = _svn_info_xml(
            self.checkout,
            [
                (relative, changed_at)
                for relative, _contents, changed_at in files
            ],
        )

        def runner(arguments):
            values = list(arguments)
            if commands is not None:
                commands.append(values)
            if "checkout" in values:
                (self.checkout / ".svn").mkdir(parents=True)
                for relative, contents, _changed_at in files:
                    target = self.checkout / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(contents, encoding="utf-8")
            if "propget" in values:
                return SimpleNamespace(
                    returncode=0,
                    stdout="<?xml version='1.0'?><properties/>",
                    stderr="",
                )
            if "--xml" in values:
                return SimpleNamespace(
                    returncode=0,
                    stdout=xml,
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout="42\n" if "--show-item" in values else "",
                stderr="",
            )

        return runner

    def test_checkout_receives_complete_svn_transport_url(self) -> None:
        repository_url = "svn://127.0.0.1:3690/hogehoge-republic"
        commands: list[list[str]] = []
        execute_fetch_plan(
            self._plan(
                recursive=True,
                updated_within_days=None,
                repository_url=repository_url,
            ),
            self.work,
            {"started_at": "2026-07-30T12:00:00Z"},
            command_runner=self._runner(
                [("README.md", "fixture", "2026-07-30T12:00:00Z")],
                commands=commands,
            ),
        )
        checkout = next(
            command for command in commands if "checkout" in command
        )
        self.assertIn(repository_url, checkout)

    def test_recursive_filter_copies_boundary_and_recent_files(self) -> None:
        (self.work / "old.md").write_text(
            "previous old contents",
            encoding="utf-8",
        )
        (self.work / "deleted.md").write_text(
            "no longer versioned",
            encoding="utf-8",
        )
        files = [
            ("boundary.md", "boundary", "2026-06-30T12:00:00Z"),
            ("old.md", "checkout old", "2026-06-30T11:59:59Z"),
            ("nested/recent.md", "recent", "2026-07-30T11:59:59Z"),
        ]

        outcome = execute_fetch_plan(
            self._plan(recursive=True),
            self.work,
            {"started_at": "2026-07-30T12:00:00Z"},
            command_runner=self._runner(files),
        )

        self.assertEqual(
            "boundary",
            (self.work / "boundary.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "recent",
            (self.work / "nested" / "recent.md").read_text(
                encoding="utf-8"
            ),
        )
        self.assertEqual(
            "previous old contents",
            (self.work / "old.md").read_text(encoding="utf-8"),
        )
        self.assertFalse((self.work / "deleted.md").exists())
        self.assertEqual(3, outcome["inventory_documents"])
        self.assertEqual(2, outcome["eligible_documents"])

    def test_direct_filter_preserves_child_tree_and_aged_versioned_file(
        self,
    ) -> None:
        (self.work / "child").mkdir()
        (self.work / "child" / "keep.md").write_text(
            "child",
            encoding="utf-8",
        )
        (self.work / "old.md").write_text(
            "previous old contents",
            encoding="utf-8",
        )
        (self.work / "deleted.md").write_text(
            "no longer versioned",
            encoding="utf-8",
        )
        commands: list[list[str]] = []
        files = [
            ("recent.md", "recent", "2026-07-01T00:00:00Z"),
            ("old.md", "checkout old", "2026-01-01T00:00:00Z"),
        ]

        execute_fetch_plan(
            self._plan(recursive=False),
            self.work,
            {"started_at": "2026-07-30T12:00:00Z"},
            command_runner=self._runner(files, commands=commands),
        )

        self.assertEqual(
            "recent",
            (self.work / "recent.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "previous old contents",
            (self.work / "old.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "child",
            (self.work / "child" / "keep.md").read_text(encoding="utf-8"),
        )
        self.assertFalse((self.work / "deleted.md").exists())
        xml_commands = [command for command in commands if "--xml" in command]
        self.assertEqual(1, len(xml_commands))
        self.assertEqual(
            "files",
            xml_commands[0][xml_commands[0].index("--depth") + 1],
        )

    def test_no_filter_keeps_legacy_commands_and_materialization(self) -> None:
        commands: list[list[str]] = []
        files = [
            ("all.md", "all", "2000-01-01T00:00:00Z"),
        ]

        outcome = execute_fetch_plan(
            self._plan(recursive=True, updated_within_days=None),
            self.work,
            {},
            command_runner=self._runner(files, commands=commands),
        )

        self.assertEqual(
            "all",
            (self.work / "all.md").read_text(encoding="utf-8"),
        )
        self.assertFalse(any("--xml" in command for command in commands))
        self.assertNotIn("eligible_documents", outcome)

    def test_same_revision_skips_only_without_cutoff(self) -> None:
        files = [("all.md", "all", "2026-07-30T00:00:00Z")]
        plan = self._plan(recursive=True, updated_within_days=None)
        execute_fetch_plan(
            plan,
            self.work,
            {},
            command_runner=self._runner(files),
        )
        unchanged = execute_fetch_plan(
            plan,
            self.work,
            {},
            command_runner=self._runner(files),
            previous_run_complete=True,
        )
        self.assertTrue(unchanged["no_change"])

        cutoff_result = execute_fetch_plan(
            self._plan(recursive=True, updated_within_days=30),
            self.work,
            {"started_at": "2026-07-30T12:00:00Z"},
            command_runner=self._runner(files),
            previous_run_complete=True,
        )
        self.assertNotIn("no_change", cutoff_result)

    def test_same_primary_revision_with_external_refreshes_work_tree(self) -> None:
        plan = self._plan(recursive=True, updated_within_days=None)

        def runner(arguments):
            values = list(arguments)
            if "checkout" in values:
                (self.checkout / ".svn").mkdir(parents=True)
                (self.checkout / "external.md").write_text(
                    "external-v1",
                    encoding="utf-8",
                )
            elif "update" in values:
                (self.checkout / "external.md").write_text(
                    "external-v2",
                    encoding="utf-8",
                )
            if "propget" in values:
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "<?xml version='1.0'?><properties><target path='.'>"
                        "<property name='svn:externals'>"
                        "^/external external.md"
                        "</property></target></properties>"
                    ),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout="42\n" if "--show-item" in values else "",
                stderr="",
            )

        execute_fetch_plan(plan, self.work, {}, command_runner=runner)
        unchanged_primary = execute_fetch_plan(
            plan,
            self.work,
            {},
            command_runner=runner,
            previous_run_complete=True,
        )

        self.assertNotIn("no_change", unchanged_primary)
        self.assertEqual(
            "external-v2",
            (self.work / "external.md").read_text(encoding="utf-8"),
        )

    def test_large_xml_uses_complete_stdout_sink(self) -> None:
        files = [
            ("recent.md", "recent", "2026-07-30T00:00:00Z"),
        ]
        xml = _svn_info_xml(self.checkout, [
            (files[0][0], files[0][2]),
        ], padding=200_000)

        def runner(arguments, *, stdout_sink=None):
            values = list(arguments)
            if "checkout" in values:
                (self.checkout / ".svn").mkdir(parents=True)
                (self.checkout / "recent.md").write_text(
                    "recent",
                    encoding="utf-8",
                )
            if "--xml" in values:
                self.assertIsNotNone(stdout_sink)
                stdout_sink.write(xml)
                return SimpleNamespace(
                    returncode=0,
                    stdout="<bounded-output>",
                    stdout_truncated=True,
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout="42\n" if "--show-item" in values else "",
                stderr="",
            )

        runner.supports_stdout_sink = True

        execute_fetch_plan(
            self._plan(recursive=True),
            self.work,
            {"started_at": "2026-07-30T12:00:00Z"},
            command_runner=runner,
        )

        self.assertEqual(
            "recent",
            (self.work / "recent.md").read_text(encoding="utf-8"),
        )

    def test_unsafe_xml_path_fails_before_materialization(self) -> None:
        sentinel = self.work / "sentinel.md"
        sentinel.write_text("unchanged", encoding="utf-8")
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        invalid = (
            "<?xml version=\"1.0\"?>"
            f"<info><entry kind=\"dir\" path={quoteattr(str(self.checkout))}/>"
            f"<entry kind=\"file\" path={quoteattr(str(outside))}>"
            "<commit><date>2026-07-30T00:00:00Z</date></commit>"
            "</entry></info>"
        ).encode("utf-8")

        with self.assertRaises(SourceManagerError) as captured:
            execution_module._parse_svn_info_xml(
                io.BytesIO(invalid),
                self.checkout,
            )

        self.assertEqual("fetch.svn", captured.exception.stage)
        self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))

    def test_windows_style_xml_paths_map_to_checkout_files(self) -> None:
        target = self.checkout / "nested" / "document.md"
        target.parent.mkdir(parents=True)
        target.write_text("document", encoding="utf-8")
        xml = (
            "<?xml version=\"1.0\"?>"
            r'<info><entry kind="dir" path="C:\work\svn"/>'
            r'<entry kind="file" '
            r'path="C:\work\svn\nested\document.md">'
            "<commit><date>2026-07-30T00:00:00Z</date></commit>"
            "</entry></info>"
        ).encode("utf-8")

        inventory = execution_module._parse_svn_info_xml(
            io.BytesIO(xml),
            self.checkout,
        )

        self.assertEqual(
            ["nested/document.md"],
            [path.as_posix() for path in inventory],
        )


if __name__ == "__main__":
    unittest.main()
