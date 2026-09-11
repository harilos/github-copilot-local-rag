from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from source_manager import (
    SourceStore,
    list_sources,
    providers,
    register_source,
    update_source,
    update_source_configuration,
)
from source_manager import runner, source_preflight
from source_manager.errors import SourceManagerError
from source_manager.git_host_urls import GIT_SOURCE_TYPES
from source_manager.runner import _file_preview_add_resume_required
from source_manager.source_exclusion import (
    exclusion_signature,
    is_excluded,
    normalize_exclusion_paths,
    preview_and_prepare_work,
)


class SourceExclusionTests(unittest.TestCase):
    def test_normalizes_posix_paths_and_empty_is_backward_compatible(self) -> None:
        self.assertEqual([], normalize_exclusion_paths(None))
        self.assertEqual([], normalize_exclusion_paths([]))
        self.assertEqual(
            ["build/generated", "**/*.tmp"],
            normalize_exclusion_paths(
                ["build\\generated", "./**/*.tmp", "build/generated"]
            ),
        )

    def test_rejects_absolute_drive_and_parent_escape(self) -> None:
        for value in (
            ["/etc/passwd"],
            [r"C:\\work\\private"],
            ["docs/../private"],
            [r"\\server\\share\\private"],
        ):
            with self.subTest(value=value):
                with self.assertRaises(SourceManagerError):
                    normalize_exclusion_paths(value)

    def test_root_relative_path_and_recursive_glob_semantics(self) -> None:
        patterns = ["build", "**/*.tmp", "docs/*/draft.md"]
        self.assertTrue(is_excluded("build/output/a.txt", patterns))
        self.assertTrue(is_excluded("cache/a.tmp", patterns))
        self.assertTrue(is_excluded("docs/v2/draft.md", patterns))
        self.assertFalse(is_excluded("src/build/a.txt", patterns))
        self.assertFalse(is_excluded("docs/v2/final.md", patterns))

    def test_preview_stats_and_filter_use_stat_without_reading_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / "work"
            root = work_root / "ingest" / "source-key"
            root.mkdir(parents=True)
            (root / "keep").mkdir()
            (root / "build" / "nested").mkdir(parents=True)
            (root / "keep" / "guide.md").write_bytes(b"keep")
            (root / "keep" / "trace.tmp").write_bytes(b"tmp")
            (root / "build" / "nested" / "generated.txt").write_bytes(
                b"generated"
            )

            filtered = work_root / "filtered" / root.name
            prepared = preview_and_prepare_work(
                root,
                ["build", "**/*.tmp"],
                filtered_root=filtered,
            )
            preview = prepared.preview

            self.assertEqual(1, preview.included_count)
            self.assertEqual(4, preview.included_bytes)
            self.assertEqual(2, preview.excluded_count)
            self.assertEqual(12, preview.excluded_bytes)
            self.assertTrue((root / "keep" / "guide.md").is_file())
            self.assertTrue((root / "keep" / "trace.tmp").is_file())
            self.assertTrue((root / "build").is_dir())
            self.assertTrue((filtered / "keep" / "guide.md").is_file())
            self.assertTrue(
                os.path.samefile(
                    root / "keep" / "guide.md",
                    filtered / "keep" / "guide.md",
                )
            )
            self.assertFalse((filtered / "keep" / "trace.tmp").exists())
            self.assertFalse((filtered / "build").exists())

    def test_git_svn_and_local_schema_accept_exclusions_only(self) -> None:
        for source_type in sorted(GIT_SOURCE_TYPES):
            with self.subTest(source_type=source_type):
                git = providers.validate_provider_config(
                    source_type,
                    {
                        "repository_url": "https://git.example/group/project.git",
                        "include_paths": [],
                        "updated_within_days": None,
                        "exclude_paths": ["build", "**/*.tmp"],
                    },
                )
                self.assertEqual(["build", "**/*.tmp"], git["exclude_paths"])
        svn = providers.validate_provider_config(
            "svn",
            {
                "repository_url": "https://svn.example/project/trunk",
                "recursive": True,
                "updated_within_days": None,
                "exclude_paths": ["generated"],
            },
        )
        local = providers.validate_provider_config(
            "other",
            {"one_shot": True, "exclude_paths": ["private"]},
        )
        self.assertEqual(["generated"], svn["exclude_paths"])
        self.assertEqual(["private"], local["exclude_paths"])

        for provider, settings in (
            ("redmine", {"project_url": "https://redmine.example/projects/x"}),
            (
                "gitlab_issues",
                {
                    "gitlab_url": "https://gitlab.example",
                    "project_url": "https://gitlab.example/group/project",
                },
            ),
        ):
            with self.subTest(provider=provider):
                with self.assertRaises(SourceManagerError):
                    providers.validate_provider_config(
                        provider,
                        {**settings, "exclude_paths": ["private"]},
                    )

    def test_issue_sources_never_use_file_preview_add_only_resume(self) -> None:
        state = {
            "status": "interrupted",
            "phase": "reflect",
            "preflight_filter_applied": True,
            "preflight_included_count": 0,
            "preflight_excluded_count": 1,
        }
        for source_type in ("redmine", "gitlab_issues"):
            with self.subTest(source_type=source_type):
                self.assertFalse(
                    _file_preview_add_resume_required(
                        {"source_type": source_type},
                        state,
                    )
                )

    def test_all_excluded_add_failure_resumes_without_provider_fetch(self) -> None:
        self._assert_all_excluded_resume(RuntimeError)

    def test_all_git_providers_apply_exclusions_on_registration_and_changes(self) -> None:
        self._assert_git_exclusion_updates(legacy=False)

    def test_legacy_unfiltered_git_run_is_repaired_even_without_remote_changes(self) -> None:
        self._assert_git_exclusion_updates(legacy=True)

    def _assert_git_exclusion_updates(self, *, legacy: bool) -> None:
        files = {
            "keep.md", "src/build/keep.md", "build/generated.md",
            "scratch.tmp", "docs/cache.tmp",
        }
        included = {"keep.md", "src/build/keep.md"}
        patterns = ["build", "**/*.tmp"]
        for source_type in sorted(GIT_SOURCE_TYPES):
            with (
                self.subTest(source_type=source_type),
                tempfile.TemporaryDirectory() as temporary,
            ):
                db_root = Path(temporary) / "fixture-rag"
                db_root.mkdir()
                fetched_as_complete: list[bool] = []
                reflected: list[set[str]] = []
                identities: list[Path] = []
                plans: list[str] = []

                def fetch(_plan, work, _state, **kwargs):
                    completed = kwargs["previous_run_complete"]
                    fetched_as_complete.append(completed)
                    for relative in files:
                        path = work / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("fixture", encoding="utf-8")
                    return {
                        "status": "ok", "documents": len(files),
                        "revision": "unchanged-revision", "no_change": completed,
                    }

                def add(arguments):
                    root = Path(arguments[arguments.index("--root") + 1])
                    identity = (
                        Path(arguments[
                            arguments.index("--persistent-root-identity") + 1
                        ])
                        if "--persistent-root-identity" in arguments else root
                    )
                    identities.append(identity)
                    reflected.append({
                        path.relative_to(root).as_posix()
                        for path in root.rglob("*") if path.is_file()
                    })
                    self.assertEqual(files, {
                        path.relative_to(identity).as_posix()
                        for path in identity.rglob("*") if path.is_file()
                    })
                    count = len(reflected[-1])
                    summary = {
                        "operation": "add",
                        "source_id": list_sources(db_root)[0]["local_source_key"],
                        "file_count": count, "indexed_files": count,
                        "skipped_files": 0, "error_files": 0,
                        "input_error_files": 0, "extract_error_files": 0,
                        "error_details": [], "upserted_records": count,
                        "deleted_records": 0, "result_status": "success",
                    }
                    return SimpleNamespace(
                        returncode=0,
                        stdout="@@LOCAL_RAG_RESULT_V1@@" + json.dumps(summary),
                        stderr="",
                    )

                runtime = {
                    "python_executable": db_root / "venv-python",
                    "rag_root": db_root / "rag-runtime",
                    "command_runner": add,
                    "metadata_publisher": lambda *_: None,
                }
                settings = {
                    "repository_url": "https://git.example/group/project.git",
                    "exclude_paths": patterns,
                }
                with (
                    mock.patch.object(runner, "execute_fetch_plan", side_effect=fetch),
                    mock.patch.object(
                        runner, "_search_artifacts_match_completed_source",
                        return_value=True,
                    ),
                ):
                    # Recreate a completed run from before file preview supported
                    # hosted Git types, with the configured exclusions ignored.
                    with mock.patch.object(
                        source_preflight, "FILE_BASED_SOURCE_TYPES",
                        (
                            frozenset() if legacy
                            else source_preflight.FILE_BASED_SOURCE_TYPES
                        ),
                    ):
                        registered = register_source(
                            db_root, source_type=source_type,
                            display_name=f"{source_type} fixture", fetch=settings,
                            start=True, **runtime,
                        )
                    self.assertEqual("updated", registered["status"])
                    self.assertEqual(files if legacy else included, reflected[-1])
                    key = registered["local_source_key"]
                    store = SourceStore(db_root)
                    if legacy:
                        self.assertNotIn(
                            "preflight_filter_applied", store.read_state(key).payload,
                        )
                        repaired = update_source(db_root, key, **runtime)
                        self.assertEqual("updated", repaired["status"])
                        self.assertEqual([False, False], fetched_as_complete)
                        self.assertEqual(included, reflected[-1])

                    preview = store.read_state(key).payload
                    self.assertEqual(5, preview["preflight_acquired_count"])
                    self.assertEqual(2, preview["preflight_included_count"])
                    self.assertEqual(3, preview["preflight_excluded_count"])
                    self.assertEqual(
                        exclusion_signature(patterns), preview["preflight_exclusion_hash"],
                    )
                    plans.append(preview["plan_etag"])

                    # Repeated unchanged updates retain the evidence that this
                    # exact exclusion policy was reflected, without another ADD.
                    add_count = len(reflected)
                    for _ in range(2):
                        skipped = update_source(db_root, key, **runtime)
                        self.assertEqual("skipped", skipped["status"])
                        self.assertEqual(add_count, len(reflected))
                        self.assertEqual(
                            {
                                name: value for name, value in preview.items()
                                if name.startswith("preflight_")
                            },
                            {
                                name: value
                                for name, value in store.read_state(key).payload.items()
                                if name.startswith("preflight_")
                            },
                        )

                    for changed, expected in (
                        (["src/build"], files - {"src/build/keep.md"}), ([], files),
                    ):
                        update_source_configuration(
                            db_root, key, fetch={**settings, "exclude_paths": changed},
                        )
                        updated = update_source(db_root, key, **runtime)
                        self.assertEqual("updated", updated["status"])
                        self.assertFalse(fetched_as_complete[-1])
                        self.assertEqual(expected, reflected[-1])
                        source = store.read_source(key).payload
                        self.assertEqual(source_type, source["source_type"])
                        self.assertEqual(changed, source["fetch"]["exclude_paths"])
                        self.assertEqual(registered["source_id"], source["source_id"])
                        state = store.read_state(key).payload
                        self.assertEqual(
                            len(expected), state["preflight_included_count"],
                        )
                        self.assertEqual(
                            len(files - expected), state["preflight_excluded_count"],
                        )
                        plans.append(state["plan_etag"])
                    self.assertEqual(3, len(set(plans)))
                    self.assertEqual(1, len(set(identities)))

    def test_all_excluded_keyboard_interrupt_resumes_without_provider_fetch(
        self,
    ) -> None:
        self._assert_all_excluded_resume(KeyboardInterrupt)

    def _assert_all_excluded_resume(
        self,
        interruption_type: type[BaseException],
    ) -> None:
        provider_settings = {
            **{
                source_type: {
                    "repository_url": "https://git.example/group/project.git",
                    "exclude_paths": ["drop"],
                }
                for source_type in sorted(GIT_SOURCE_TYPES)
            },
            "svn": {
                "repository_url": "https://svn.example/project/trunk",
                "recursive": True,
                "exclude_paths": ["drop"],
            },
            "other": {"one_shot": True, "exclude_paths": ["drop"]},
        }
        for source_type, settings in provider_settings.items():
            with self.subTest(source_type=source_type):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = Path(temporary)
                    db_root = fixture / "fixture-rag"
                    db_root.mkdir()
                    incoming = fixture / "incoming"
                    incoming.mkdir()
                    calls = {"fetch": 0, "add": 0}
                    add_roots: list[Path] = []
                    persistent_roots: list[Path] = []

                    def fetch(_plan, work, _state):
                        calls["fetch"] += 1
                        dropped = Path(work) / "drop"
                        dropped.mkdir(parents=True, exist_ok=True)
                        (dropped / "old.md").write_text(
                            "excluded",
                            encoding="utf-8",
                        )
                        return {"status": "ok", "documents": 1}

                    def add(arguments):
                        calls["add"] += 1
                        root = Path(arguments[arguments.index("--root") + 1])
                        identity = Path(
                            arguments[
                                arguments.index("--persistent-root-identity")
                                + 1
                            ]
                        )
                        add_roots.append(root)
                        persistent_roots.append(identity)
                        self.assertEqual([], list(root.rglob("*")))
                        self.assertNotEqual(root.resolve(), identity.resolve())
                        self.assertTrue((identity / "drop" / "old.md").is_file())
                        if calls["add"] == 1:
                            raise interruption_type(
                                "synthetic ADD interruption"
                            )
                        key = list_sources(db_root)[0]["local_source_key"]
                        summary = {
                            "operation": "add",
                            "source_id": key,
                            "file_count": 0,
                            "indexed_files": 0,
                            "skipped_files": 0,
                            "error_files": 0,
                            "input_error_files": 0,
                            "extract_error_files": 0,
                            "error_details": [],
                            "upserted_records": 0,
                            "deleted_records": 1,
                            "result_status": "success",
                        }
                        return SimpleNamespace(
                            returncode=0,
                            stdout=(
                                "@@LOCAL_RAG_RESULT_V1@@"
                                + json.dumps(summary)
                            ),
                            stderr="",
                        )

                    arguments = {
                        "source_type": source_type,
                        "display_name": f"{source_type} fixture",
                        "fetch": settings,
                        "start": True,
                        "python_executable": fixture / "venv-python",
                        "rag_root": fixture / "rag-runtime",
                        "executor": fetch,
                        "command_runner": add,
                    }
                    if source_type == "other":
                        arguments["runtime_input"] = incoming

                    with self.assertRaisesRegex(
                        interruption_type,
                        "synthetic ADD interruption",
                    ):
                        register_source(db_root, **arguments)

                    registered = list_sources(db_root)[0]
                    state = SourceStore(db_root).read_state(
                        registered["local_source_key"]
                    ).payload
                    self.assertEqual("interrupted", state["status"])
                    self.assertEqual("reflect", state["phase"])
                    self.assertEqual(0, state["fetched_count"])
                    self.assertEqual(0, state["indexed_confirmed_count"])
                    self.assertEqual(0, state["pending_count"])
                    self.assertTrue(state["preflight_filter_applied"])

                    resumed = update_source(
                        db_root,
                        registered["local_source_key"],
                        python_executable=fixture / "venv-python",
                        rag_root=fixture / "rag-runtime",
                        executor=fetch,
                        command_runner=add,
                        metadata_publisher=lambda *_: None,
                    )

                    self.assertEqual("updated", resumed["status"])
                    self.assertEqual("add", resumed["resumed_operation"])
                    self.assertEqual({"fetch": 1, "add": 2}, calls)
                    self.assertEqual(2, len(add_roots))
                    self.assertEqual(persistent_roots[0], persistent_roots[1])
                    final_state = SourceStore(db_root).read_state(
                        registered["local_source_key"]
                    ).payload
                    self.assertEqual("complete", final_state["status"])
                    self.assertEqual("complete", final_state["phase"])
                    self.assertFalse(final_state["can_resume"])


if __name__ == "__main__":
    unittest.main()
