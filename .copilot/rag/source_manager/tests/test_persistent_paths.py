from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock


_MODULE_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "_acl_source_manager"
package = types.ModuleType(_PACKAGE_NAME)
package.__path__ = [str(_MODULE_ROOT)]
sys.modules.setdefault(_PACKAGE_NAME, package)


def _load(name: str):
    qualified = f"{_PACKAGE_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(
        qualified,
        _MODULE_ROOT / f"{name}.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


errors = _load("errors")
persistent_paths = _load("persistent_paths")
github_content = _load("github_content")


def _stub_module(name: str, **attributes: object):
    qualified = f"{_PACKAGE_NAME}.{name}"
    module = types.ModuleType(qualified)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[qualified] = module
    return module


_stub_module("diagnostics", process_diagnostic=lambda *args, **kwargs: None)
_stub_module(
    "gitlab_issues",
    fetch_gitlab_issues=lambda *args, **kwargs: {},
    gitlab_issues_updated_after=lambda *args, **kwargs: None,
)
_stub_module(
    "networking",
    is_gitlab_token_request=lambda *args, **kwargs: False,
    reject_http_redirects=lambda *args, **kwargs: None,
)
_stub_module(
    "redmine",
    parse_redmine_project_url=lambda value: value,
    redmine_updated_on_cutoff=lambda *args, **kwargs: None,
)
_stub_module("security", validate_environment_name=lambda value, **kwargs: str(value))
execution = _load("execution")

_stub_module(
    "database_copy_storage",
    copy_catalog_snapshot=lambda *args, **kwargs: None,
    copy_chroma_snapshot=lambda *args, **kwargs: 0,
    delete_excluded_sources=lambda *args, **kwargs: [],
    validate_excluded_vectors=lambda *args, **kwargs: None,
)
database_copy_core = _load("database_copy_core")

_stub_module(
    "machine_connections",
    configured_sharepoint_root=lambda *args, **kwargs: None,
)
_stub_module(
    "setup_copy_bridge",
    restore_portable_database=lambda *args, **kwargs: None,
)
copy_only_packages = _load("copy_only_packages")
SourceManagerError = errors.SourceManagerError


class PersistentPathPolicyTests(unittest.TestCase):
    def test_windows_omits_mode_and_posix_keeps_0700(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Path.mkdir
            calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

            def recording(path: Path, *args: object, **kwargs: object) -> None:
                calls.append((args, dict(kwargs)))
                original(path, *args, **kwargs)

            with mock.patch.object(Path, "mkdir", recording), mock.patch.object(
                persistent_paths, "_is_windows", return_value=True
            ):
                persistent_paths.create_persistent_directory(
                    root / "windows-child",
                    trusted_root=root,
                )
            self.assertEqual(calls[-1], ((), {}))

            calls.clear()
            with mock.patch.object(Path, "mkdir", recording), mock.patch.object(
                persistent_paths, "_is_windows", return_value=False
            ):
                persistent_paths.create_persistent_directory(
                    root / "posix-child",
                    trusted_root=root,
                )
            self.assertEqual(calls[-1], ((), {"mode": 0o700}))
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE((root / "posix-child").stat().st_mode),
                    0o700,
                )

    def test_parents_exist_ok_and_existing_file_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = persistent_paths.create_persistent_directory(
                root / "one" / "two",
                trusted_root=root,
                parents=True,
            )
            self.assertTrue(nested.is_dir())
            self.assertEqual(
                persistent_paths.create_persistent_directory(
                    nested,
                    trusted_root=root,
                    exist_ok=True,
                ),
                nested,
            )
            with self.assertRaises(FileExistsError):
                persistent_paths.create_persistent_directory(
                    nested,
                    trusted_root=root,
                )
            file_path = root / "file"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(SourceManagerError):
                persistent_paths.create_persistent_directory(
                    file_path,
                    trusted_root=root,
                    exist_ok=True,
                )

    def test_escape_and_link_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(SourceManagerError, "escaped"):
                persistent_paths.create_persistent_directory(
                    root.parent / "outside",
                    trusted_root=root,
                )
            link = root / "linked"
            try:
                link.symlink_to(root.parent, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")
            with self.assertRaisesRegex(SourceManagerError, "real directories"):
                persistent_paths.create_persistent_directory(
                    link / "child",
                    trusted_root=root,
                    parents=True,
                )

    def test_staging_collision_retries_and_is_finite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persistent_paths.create_persistent_directory(
                root / ".stage-dead",
                trusted_root=root,
            )
            stage_nonces = iter(("dead", "beef"))
            stage = persistent_paths.create_persistent_staging_directory(
                root,
                prefix=".stage-",
                token_factory=lambda: next(stage_nonces),
            )
            self.assertEqual(stage.name, ".stage-beef")
            with self.assertRaisesRegex(SourceManagerError, "collision limit"):
                persistent_paths.create_persistent_staging_directory(
                    root,
                    prefix=".stage-",
                    attempts=2,
                    token_factory=lambda: "dead",
                )

    def test_permission_error_is_not_converted_to_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                persistent_paths,
                "_mkdir_for_platform",
                side_effect=PermissionError(5, "denied"),
            ):
                with self.assertRaises(PermissionError):
                    persistent_paths.create_persistent_directory(
                        root / "blocked",
                        trusted_root=root,
                    )
            error = persistent_paths.persistent_access_error(
                root,
                root / "sources" / "source-a",
                database_identifier="fixture-rag",
            )
            message = str(error)
            self.assertIn("database=fixture-rag", message)
            self.assertIn("sources/source-a", message)
            self.assertIn("inheritance may be disabled", message)
            self.assertNotIn(str(root), message)

        store_source = (_MODULE_ROOT / "store.py").read_text(encoding="utf-8")
        self.assertIn("metadata = os.lstat(sources)", store_source)
        self.assertIn("except PermissionError as exc:", store_source)

    def test_reparse_attribute_is_rejected(self) -> None:
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        metadata = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=reparse,
        )
        self.assertTrue(
            persistent_paths._is_link_or_reparse(Path("fixture"), metadata)
        )

    def test_other_snapshot_rolls_back_when_publication_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source"
            source.mkdir()
            (source / "new.txt").write_text("new", encoding="utf-8")
            destination = root / "work"
            destination.mkdir()
            (destination / "old.txt").write_text("old", encoding="utf-8")
            original_replace = execution.os.replace

            def failing_replace(source_path: object, target_path: object) -> None:
                source_value = Path(source_path)
                target_value = Path(target_path)
                if (
                    source_value.name.startswith(".incoming-")
                    and target_value == destination
                ):
                    raise OSError("fixture publication failure")
                original_replace(source_path, target_path)

            with mock.patch.object(
                execution.os,
                "replace",
                side_effect=failing_replace,
            ):
                with self.assertRaisesRegex(OSError, "fixture publication failure"):
                    execution._materialize_snapshot(source, destination)

            self.assertEqual(
                (destination / "old.txt").read_text(encoding="utf-8"),
                "old",
            )
            self.assertFalse((destination / "new.txt").exists())
            self.assertEqual(
                [path.name for path in root.iterdir() if path.name != "fixture-source"],
                ["work"],
            )


    def test_other_snapshot_and_provider_control_paths_are_transactional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "new.txt").write_text("new", encoding="utf-8")
            destination = root / "work"
            destination.mkdir()
            (destination / "old.txt").write_text("old", encoding="utf-8")

            result = execution._materialize_snapshot(source, destination)

            self.assertEqual(result["documents"], 1)
            self.assertFalse((destination / "old.txt").exists())
            self.assertEqual(
                (destination / "nested" / "new.txt").read_text(encoding="utf-8"),
                "new",
            )
            control = root / "providers" / "git" / "wiki"
            execution._ensure_real_directory(control)
            self.assertTrue(control.is_dir())
            self.assertFalse(
                any(path.name.startswith(".incoming-") for path in root.iterdir())
            )
            self.assertFalse(
                any(path.name.startswith(".previous-") for path in root.iterdir())
            )

    def test_database_copy_publishes_directly_from_persistent_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-rag"
            source.mkdir()
            (source / "payload.txt").write_text("payload", encoding="utf-8")
            destination = root / "copy-rag"

            with mock.patch.multiple(
                database_copy_core,
                configured_collection=lambda *args, **kwargs: "old",
                collection_name_for_db=lambda *args, **kwargs: "new",
                rewrite_database_identity=lambda *args, **kwargs: None,
                rewrite_db_local_paths=lambda *args, **kwargs: None,
                delete_excluded_sources=lambda *args, **kwargs: [],
                write_copy_marker=lambda *args, **kwargs: None,
                validate_copied_database=lambda *args, **kwargs: None,
                fsync_directory=lambda *args, **kwargs: None,
            ):
                result = database_copy_core.copy_database(
                    source,
                    destination,
                    destination_name="copy-rag",
                    title="Fixture",
                    query_hint="fixture",
                    rag_root=root,
                )

            self.assertEqual(result["status"], "copied")
            self.assertEqual(
                (destination / "payload.txt").read_text(encoding="utf-8"),
                "payload",
            )
            self.assertFalse(
                any(path.name.startswith(".copy-copy-rag-") for path in root.iterdir())
            )

    def test_copy_only_import_replaces_database_via_persistent_stage(self) -> None:
        class PackageError(ValueError):
            pass

        packages = SimpleNamespace(
            _ADMIN_KIND="admin",
            _DB_NAME=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$"),
            PackageError=PackageError,
            _safe_relative=lambda value: PurePosixPath(value),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "package"
            source = (
                package_root
                / ".copilot"
                / "rag"
                / "dbs"
                / "fixture-rag"
                / "data.txt"
            )
            source.parent.mkdir(parents=True)
            target = root / "target"
            target.mkdir()

            for content in ("first", "second"):
                source.write_text(content, encoding="utf-8")
                encoded = content.encode("utf-8")
                manifest = {
                    "kind": "distribution",
                    "dbs": [{"name": "fixture-rag"}],
                    "files": [
                        {
                            "path": ".copilot/rag/dbs/fixture-rag/data.txt",
                            "size": len(encoded),
                            "sha256": hashlib.sha256(encoded).hexdigest(),
                        }
                    ],
                }
                copy_only_packages._publish_copy_tree(
                    package_root,
                    manifest,
                    target,
                    packages,
                )

            installed = target / "rag" / "dbs" / "fixture-rag" / "data.txt"
            self.assertEqual(installed.read_text(encoding="utf-8"), "second")
            database_parent = target / "rag" / "dbs"
            self.assertFalse(
                any(
                    path.name.startswith(".local-rag-import-")
                    or path.name.endswith(".previous")
                    for path in database_parent.iterdir()
                )
            )


    def test_github_issue_publication_and_repeat_use_persistent_stage(self) -> None:
        issue = {
            "number": 1,
            "title": "Fixture",
            "state": "open",
            "comments": 0,
            "body": "Body",
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T00:00:00Z",
            "labels": [],
            "user": {"login": "fixture"},
        }

        def runner(_arguments: list[str]) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([[issue]]),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            work = parent / "work"
            work.mkdir()
            settings = {
                "repository_url": "https://github.com/example/project",
                "state": "all",
                "include_comments": False,
            }
            first = github_content.fetch_github_issues(settings, work, runner)
            second = github_content.fetch_github_issues(settings, work, runner)
            self.assertEqual(first["documents"], 1)
            self.assertEqual(second["documents"], 1)
            self.assertIn("# GitHub Issue #1", (work / "issues" / "1.md").read_text())
            leftovers = [
                path.name
                for path in parent.iterdir()
                if path.name != "work"
            ]
            self.assertEqual(leftovers, [])


    def test_persistent_callers_do_not_reintroduce_private_staging(self) -> None:
        module_root = Path(__file__).resolve().parents[1]
        names = (
            "store.py",
            "execution.py",
            "github_content.py",
            "database_copy_core.py",
            "copy_only_packages.py",
        )
        required_helpers = {
            "store.py": ("create_persistent_directory(",),
            "execution.py": (
                "create_persistent_directory(",
                "create_persistent_staging_directory(",
            ),
            "github_content.py": ("create_persistent_staging_directory(",),
            "database_copy_core.py": ("create_persistent_staging_directory(",),
            "copy_only_packages.py": ("create_persistent_staging_directory(",),
        }
        violations: list[str] = []
        for name in names:
            source = (module_root / name).read_text(encoding="utf-8")
            for marker in required_helpers[name]:
                if marker not in source:
                    violations.append(f"{name}:missing:{marker}")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute) and node.func.attr == "mkdir":
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "mode"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value == 0o700
                        ):
                            violations.append(f"{name}:{node.lineno}:mkdir")
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "tempfile"
                    and node.func.attr == "mkdtemp"
                    and any(keyword.arg == "dir" for keyword in node.keywords)
                ):
                    violations.append(f"{name}:{node.lineno}:mkdtemp")
        self.assertEqual(violations, [])

    def test_result_bundle_remains_private_spool(self) -> None:
        query = Path(__file__).resolve().parents[2] / "query" / "result_bundle.py"
        text = query.read_text(encoding="utf-8")
        self.assertIn("mode=0o700", text)
        self.assertNotIn("persistent_paths", text)


if __name__ == "__main__":
    unittest.main()
