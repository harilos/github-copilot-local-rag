from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOL_ROOT = Path(__file__).resolve().parents[1]
RAG_ROOT = TOOL_ROOT.parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from software_rag_tool import incremental, manifest, store
from software_rag_tool.atomic_io import atomic_write_json
from software_rag_tool.embeddings import DocumentTokenBudget
from software_rag_tool.writer_runtime import (
    DB_BUSY_EXIT_CODE,
    DatabaseBusyError,
    DatabaseWriteError,
    database_writer_session,
)

SUBPROCESS_BOOTSTRAP = (
    "import sys\n"
    f"sys.path.insert(0, {str(TOOL_ROOT)!r})\n"
)
PROBE = r"""
import json, sys
from pathlib import Path
from software_rag_tool.writer_runtime import (
    DB_BUSY_EXIT_CODE, DatabaseBusyError, database_writer_session,
)
try:
    with database_writer_session(Path(sys.argv[1]), sys.argv[2]) as target:
        print(json.dumps({"status": "ok", "collection": target.collection}))
except DatabaseBusyError as exc:
    print(json.dumps({"code": exc.code, "retryable": exc.retryable}))
    raise SystemExit(DB_BUSY_EXIT_CODE)
"""
HOLDER = r"""
import sys
from pathlib import Path
from software_rag_tool.writer_runtime import database_writer_session
with database_writer_session(Path(sys.argv[1]), sys.argv[2]):
    print("READY", flush=True)
    sys.stdin.buffer.read()
"""


class CharacterTokenizer:
    def __call__(self, text, **_kwargs):
        values = [text] if isinstance(text, str) else list(text)
        encoded = [[1, *range(2, len(value) + 2), 2] for value in values]
        return {"input_ids": encoded[0] if isinstance(text, str) else encoded}


class DatabaseWriteRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dbs_root = self.root / "dbs"
        self.dbs_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_db(self, name: str, collection: str) -> Path:
        root = self.dbs_root / name
        for relative in ("data/raw", "data/clean", "index/chroma", "logs"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "db.json").write_text(
            json.dumps({"db_name": name, "collection": collection}),
            encoding="utf-8",
        )
        (root / "VERSION.json").write_text(
            json.dumps({"db_name": name, "collection": collection}),
            encoding="utf-8",
        )
        (root / "index/manifest.json").write_text(
            json.dumps({"collection": collection, "record_count": 0}),
            encoding="utf-8",
        )
        return root

    @staticmethod
    def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
        return {
            path.relative_to(root).as_posix(): (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
            for path in root.rglob("*")
            if path.is_file()
        }

    def _probe(self, root: Path, name: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(TOOL_ROOT)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                SUBPROCESS_BOOTSTRAP + PROBE,
                str(root),
                name,
            ],
            cwd=str(TOOL_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )

    def _make_directory_alias(self, alias: Path, target: Path) -> None:
        try:
            alias.symlink_to(target, target_is_directory=True)
            return
        except (NotImplementedError, OSError):
            if os.name != "nt":
                self.fail("directory aliases are unavailable")
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            self.fail("directory junctions are unavailable")

    @staticmethod
    def _remove_directory_alias(alias: Path) -> None:
        if alias.is_symlink():
            alias.unlink()
        elif alias.exists():
            alias.rmdir()

    def test_poison_environment_is_bound_and_restored_for_custom_db(self) -> None:
        target = self._make_db("target-rag", "custom_target_collection")
        canary = self._make_db("canary-rag", "canary_collection")
        (canary / "sentinel.bin").write_bytes(b"unchanged")
        canary_before = self._snapshot(canary)
        poison = {
            "RAG_DBS_ROOT": str(self.dbs_root),
            "RAG_DB_NAME": "canary-rag",
            "RAG_OUTPUT_ROOT": str(canary),
            "LOCALRAG_OUTPUT_ROOT": str(canary),
            "CHROMA_DIR_V2": str(canary / "index/chroma"),
            "CHROMA_COLLECTION": "wrong_collection",
        }
        with mock.patch.dict(os.environ, poison, clear=False):
            with database_writer_session(self.dbs_root, "target-rag") as bound:
                self.assertEqual(target, bound.db_root)
                self.assertEqual("custom_target_collection", bound.collection)
                self.assertEqual(str(self.dbs_root), os.environ["RAG_DBS_ROOT"])
                self.assertEqual("target-rag", os.environ["RAG_DB_NAME"])
                self.assertEqual(str(target), os.environ["RAG_OUTPUT_ROOT"])
                self.assertEqual(
                    str(target / "index/chroma"), os.environ["CHROMA_DIR_V2"]
                )
                self.assertEqual(
                    "custom_target_collection", os.environ["CHROMA_COLLECTION"]
                )
                self.assertNotIn("LOCALRAG_OUTPUT_ROOT", os.environ)
            self.assertEqual(
                poison,
                {key: os.environ.get(key) for key in poison},
            )
        self.assertEqual(canary_before, self._snapshot(canary))

    def test_exception_and_sequential_database_switch_restore_environment(self) -> None:
        alpha = self._make_db("alpha-rag", "alpha_custom")
        beta = self._make_db("beta-rag", "beta_custom")
        previous = {
            "RAG_DBS_ROOT": "previous-root",
            "RAG_DB_NAME": "previous-rag",
            "RAG_OUTPUT_ROOT": "previous-output",
            "LOCALRAG_OUTPUT_ROOT": "previous-local",
            "CHROMA_DIR_V2": "previous-chroma",
            "CHROMA_COLLECTION": "previous-collection",
        }
        with mock.patch.dict(os.environ, previous, clear=False):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with database_writer_session(self.dbs_root, "alpha-rag"):
                    self.assertEqual(str(alpha), os.environ["RAG_OUTPUT_ROOT"])
                    raise RuntimeError("boom")
            self.assertEqual(previous, {key: os.environ.get(key) for key in previous})
            with database_writer_session(self.dbs_root, "alpha-rag") as target:
                self.assertEqual(alpha, target.db_root)
            with database_writer_session(self.dbs_root, "beta-rag") as target:
                self.assertEqual(beta, target.db_root)
                self.assertEqual("beta_custom", target.collection)
            self.assertEqual(previous, {key: os.environ.get(key) for key in previous})

    def test_metadata_mismatch_fails_before_database_artifact_write(self) -> None:
        target = self._make_db("target-rag", "collection_a")
        (target / "VERSION.json").write_text(
            json.dumps({"db_name": "target-rag", "collection": "collection_b"}),
            encoding="utf-8",
        )
        before = self._snapshot(target)
        with self.assertRaisesRegex(DatabaseWriteError, "collection mismatch"):
            with database_writer_session(self.dbs_root, "target-rag"):
                self.fail("mismatched metadata must not enter the writer body")
        self.assertEqual(before, self._snapshot(target))

    def test_artifact_reset_layout_can_be_recreated_under_lock(self) -> None:
        target = self._make_db("target-rag", "target_collection")
        shutil.rmtree(target / "data/clean")
        shutil.rmtree(target / "index")
        shutil.rmtree(target / "logs")
        with database_writer_session(self.dbs_root, "target-rag"):
            self.assertTrue((target / "data/clean").is_dir())
            self.assertTrue((target / "index").is_dir())
            self.assertTrue((target / "logs").is_dir())

    def test_different_database_locks_are_independent(self) -> None:
        self._make_db("alpha-rag", "alpha_collection")
        self._make_db("beta-rag", "beta_collection")
        with database_writer_session(self.dbs_root, "alpha-rag"):
            completed = self._probe(self.dbs_root, "beta-rag")
            self.assertEqual(0, completed.returncode, completed.stderr)

    def test_directory_alias_cannot_bypass_same_database_lock(self) -> None:
        self._make_db("target-rag", "target_collection")
        alias = self.root / "dbs-alias"
        self._make_directory_alias(alias, self.dbs_root)
        try:
            with database_writer_session(self.dbs_root, "target-rag"):
                completed = self._probe(alias, "target-rag")
            self.assertEqual(DB_BUSY_EXIT_CODE, completed.returncode)
        finally:
            self._remove_directory_alias(alias)

    @unittest.skipUnless(os.name == "nt", "Windows case-folding contract")
    def test_case_alias_cannot_bypass_same_database_lock(self) -> None:
        self._make_db("target-rag", "target_collection")
        with database_writer_session(self.dbs_root, "target-rag"):
            completed = self._probe(self.dbs_root, "TARGET-rag")
        self.assertEqual(
            DB_BUSY_EXIT_CODE,
            completed.returncode,
            (completed.stdout, completed.stderr),
        )

    def test_linked_internal_storage_is_rejected(self) -> None:
        target = self._make_db("target-rag", "target_collection")
        shutil.rmtree(target / "data/clean")
        outside = self.root / "outside"
        outside.mkdir()
        linked = target / "data/clean"
        self._make_directory_alias(linked, outside)
        try:
            with self.assertRaisesRegex(DatabaseWriteError, "linked storage"):
                with database_writer_session(self.dbs_root, "target-rag"):
                    self.fail("linked storage must fail before writer execution")
        finally:
            self._remove_directory_alias(linked)

    def test_nested_clean_or_chroma_reparse_is_rejected_before_artifact_write(
        self,
    ) -> None:
        target = self._make_db("target-rag", "target_collection")
        outside = self.root / "outside"
        outside.mkdir()
        for parent in (target / "data/clean", target / "index/chroma"):
            alias = parent / "linked"
            self._make_directory_alias(alias, outside)
            try:
                before = self._snapshot(target)
                with self.assertRaisesRegex(DatabaseWriteError, "linked storage"):
                    with database_writer_session(self.dbs_root, "target-rag"):
                        self.fail("nested reparse must fail before writer execution")
                self.assertEqual(before, self._snapshot(target))
                self.assertEqual([], list(outside.iterdir()))
            finally:
                self._remove_directory_alias(alias)

    def test_artifact_reset_rejects_nested_reparse_before_removal(self) -> None:
        from source_manager.artifact_reset import reset_derived_artifacts
        from source_manager.errors import SourceManagerError

        target = self._make_db("target-rag", "target_collection")
        (target / "catalog.sqlite").write_bytes(b"catalog")
        outside = self.root / "reset-outside"
        outside.mkdir()
        sentinel = outside / "sentinel.bin"
        sentinel.write_bytes(b"unchanged")
        alias = target / "index" / "linked"
        self._make_directory_alias(alias, outside)
        try:
            with self.assertRaisesRegex(SourceManagerError, "link"):
                reset_derived_artifacts(target, daemon_status="stopped")
            self.assertEqual(b"catalog", (target / "catalog.sqlite").read_bytes())
            self.assertEqual(b"unchanged", sentinel.read_bytes())
        finally:
            self._remove_directory_alias(alias)

    def test_source_manager_busy_preflight_does_not_mutate_source_store(self) -> None:
        from source_manager import runner
        from source_manager.checkpoints import new_run_state
        from source_manager.store import SourceStore

        target = self._make_db("target-rag", "target_collection")
        store = SourceStore(target)
        source = store.create_source(
            source_type="other",
            display_name="Concurrent Source",
            fetch={},
        )
        missing = store.read_state(source.payload["local_source_key"])
        state = store.save_state(
            source.payload["local_source_key"],
            new_run_state(store.plan(source.payload)),
            expected_revision=missing.revision,
            expected_etag=missing.etag,
        )
        command = mock.Mock()
        with database_writer_session(self.dbs_root, "target-rag"):
            before = self._snapshot(target)
            with self.assertRaises(Exception) as raised:
                runner._reflect_and_sync(
                    store,
                    source,
                    state,
                    add_root=store.ensure_work_directory(
                        source.payload["local_source_key"]
                    ),
                    python_executable=Path(sys.executable),
                    rag_root=RAG_ROOT,
                    command_runner=command,
                    metadata_publisher=lambda *_args: None,
                    progress_callback=None,
                )
            self.assertEqual(
                "DB_BUSY",
                getattr(raised.exception, "code", None),
                repr(raised.exception),
            )
            self.assertIs(True, getattr(raised.exception, "retryable", None))
            self.assertEqual(before, self._snapshot(target))
        command.assert_not_called()

    def test_native_process_kill_releases_lock_without_deleting_lock_file(self) -> None:
        self._make_db("target-rag", "target_collection")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(TOOL_ROOT)
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                SUBPROCESS_BOOTSTRAP + HOLDER,
                str(self.dbs_root),
                "target-rag",
            ],
            cwd=str(TOOL_ROOT),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            self.assertEqual("READY", process.stdout.readline().strip())
            process.kill()
            process.wait(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=20)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        lock_file = self.dbs_root / ".writer-locks/target-rag.lock"
        self.assertTrue(lock_file.is_file())
        completed = self._probe(self.dbs_root, "target-rag")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(lock_file.is_file())


class WriterEntrypointTests(unittest.TestCase):
    setUp = DatabaseWriteRuntimeTests.setUp
    tearDown = DatabaseWriteRuntimeTests.tearDown
    _make_db = DatabaseWriteRuntimeTests._make_db
    _snapshot = staticmethod(DatabaseWriteRuntimeTests._snapshot)

    @staticmethod
    def _load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"unable to load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _observed_environment() -> tuple[str | None, ...]:
        return tuple(
            os.environ.get(key)
            for key in (
                "RAG_DBS_ROOT", "RAG_DB_NAME", "RAG_OUTPUT_ROOT",
                "LOCALRAG_OUTPUT_ROOT", "CHROMA_DIR_V2", "CHROMA_COLLECTION",
            )
        )

    def test_all_writer_entrypoints_use_the_same_authoritative_context(self) -> None:
        target = self._make_db("target-rag", "target_custom")
        canary = self._make_db("canary-rag", "canary_custom")
        (canary / "sentinel.bin").write_bytes(b"unchanged")
        before = self._snapshot(canary)
        expected = (
            str(self.dbs_root), "target-rag", str(target), None,
            str(target / "index/chroma"), "target_custom",
        )
        poison = {
            "RAG_DBS_ROOT": str(self.dbs_root),
            "RAG_DB_NAME": "canary-rag",
            "RAG_OUTPUT_ROOT": str(canary),
            "LOCALRAG_OUTPUT_ROOT": str(canary),
            "CHROMA_DIR_V2": str(canary / "index/chroma"),
            "CHROMA_COLLECTION": "wrong_collection",
        }
        observed: list[tuple[str | None, ...]] = []
        input_root = self.root / "input"
        input_root.mkdir()

        add_data = self._load("integrity_add_data", RAG_ROOT / "gen_db/add_data.py")
        rebuild = self._load(
            "integrity_rebuild", RAG_ROOT / "gen_db/rebuild_component.py"
        )
        index_build = self._load(
            "integrity_index_build", TOOL_ROOT / "scripts/index_build.py"
        )
        prepare = self._load("integrity_prepare", TOOL_ROOT / "scripts/prepare.py")
        delete = self._load(
            "integrity_delete", RAG_ROOT / "gen_db/delete_source.py"
        )

        def capture(value):
            observed.append(self._observed_environment())
            return value

        with mock.patch.dict(os.environ, poison, clear=False), redirect_stdout(io.StringIO()):
            with mock.patch.object(add_data, "load_env"), mock.patch.object(
                add_data, "_install_exact_file_index_progress"
            ), mock.patch.object(
                add_data.incremental_module,
                "add_or_update_root",
                side_effect=lambda **_kwargs: capture({"operation": "add"}),
            ), mock.patch.object(
                sys,
                "argv",
                [
                    "add_data.py", "--db", "target-rag", "--root",
                    str(input_root),
                ],
            ):
                self.assertEqual(0, add_data.main())

            with mock.patch.object(rebuild, "load_env"), mock.patch.object(
                rebuild,
                "_rebuild",
                side_effect=lambda _args, _name: capture(None),
            ), mock.patch.object(
                sys,
                "argv",
                [
                    "rebuild_component.py", "--db", "target-rag",
                    "--component", "all",
                ],
            ):
                self.assertEqual(0, rebuild.main())

            with mock.patch.object(index_build, "load_env"), mock.patch.object(
                index_build, "build_index", side_effect=lambda **_kwargs: capture(0)
            ), mock.patch.object(
                sys, "argv", ["index_build.py", "--db", "target-rag"]
            ):
                self.assertEqual(0, index_build.main())

            with mock.patch.object(prepare, "load_env"), mock.patch.object(
                prepare, "build_records", return_value=([], [])
            ), mock.patch.object(
                prepare,
                "write_jsonl",
                side_effect=lambda *_args, **_kwargs: capture(0),
            ), mock.patch.object(
                sys,
                "argv",
                [
                    "prepare.py", "--db", "target-rag", "--root",
                    str(input_root),
                ],
            ):
                self.assertEqual(0, prepare.main())

            with mock.patch.object(delete, "load_env"), mock.patch.object(
                delete,
                "delete_source_data",
                side_effect=lambda *_args, **_kwargs: capture({"status": "deleted"}),
            ), mock.patch.object(
                sys,
                "argv",
                [
                    "delete_source.py", "--db", "target-rag",
                    "--source-id", "source-a",
                ],
            ):
                self.assertEqual(0, delete.main())

            self.assertEqual(poison, {key: os.environ.get(key) for key in poison})
        self.assertEqual([expected] * 5, observed)
        self.assertEqual(before, self._snapshot(canary))

    def test_all_direct_writer_entrypoints_share_db_busy_contract(self) -> None:
        target = self._make_db("target-rag", "target_collection")
        input_root = self.root / "input"
        input_root.mkdir()
        cases = (
            (
                self._load("busy_add", RAG_ROOT / "gen_db/add_data.py"),
                ["add_data.py", "--db", "target-rag", "--root", str(input_root)],
            ),
            (
                self._load("busy_rebuild", RAG_ROOT / "gen_db/rebuild_component.py"),
                ["rebuild_component.py", "--db", "target-rag", "--component", "all"],
            ),
            (
                self._load("busy_index", TOOL_ROOT / "scripts/index_build.py"),
                ["index_build.py", "--db", "target-rag"],
            ),
            (
                self._load("busy_prepare", TOOL_ROOT / "scripts/prepare.py"),
                ["prepare.py", "--db", "target-rag", "--root", str(input_root)],
            ),
            (
                self._load("busy_delete", RAG_ROOT / "gen_db/delete_source.py"),
                ["delete_source.py", "--db", "target-rag", "--source-id", "src-a"],
            ),
        )
        before = self._snapshot(target)
        with database_writer_session(self.dbs_root, "target-rag"):
            for module, argv in cases:
                stderr = io.StringIO()
                with mock.patch.object(module, "load_env"), mock.patch.object(
                    sys, "argv", argv
                ), redirect_stderr(stderr):
                    self.assertEqual(DB_BUSY_EXIT_CODE, module.main())
                payload = json.loads(stderr.getvalue())
                self.assertEqual("DB_BUSY", payload["code"])
                self.assertIs(True, payload["retryable"])
        self.assertEqual(before, self._snapshot(target))

    def test_artifact_reset_is_busy_before_deleting_any_target(self) -> None:
        from source_manager.artifact_reset import reset_derived_artifacts

        target = self._make_db("target-rag", "target_collection")
        (target / "catalog.sqlite").write_bytes(b"sentinel")
        before = self._snapshot(target)
        with database_writer_session(self.dbs_root, "target-rag"):
            with self.assertRaises(DatabaseBusyError):
                reset_derived_artifacts(target, daemon_status="stopped")
        self.assertEqual(before, self._snapshot(target))

    def test_build_wrapper_sanitizes_child_routing_and_propagates_busy_code(self) -> None:
        self._make_db("target-rag", "target_collection")
        module = self._load("integrity_build_db", RAG_ROOT / "gen_db/build_db.py")
        poison = {
            "RAG_DBS_ROOT": str(self.dbs_root),
            "RAG_DB_NAME": "wrong-rag",
            "RAG_OUTPUT_ROOT": "wrong-output",
            "LOCALRAG_OUTPUT_ROOT": "wrong-local",
            "CHROMA_DIR_V2": "wrong-chroma",
            "CHROMA_COLLECTION": "wrong-collection",
        }
        with mock.patch.dict(os.environ, poison, clear=False), mock.patch.object(
            module, "_runtime_python_or_exit", return_value=sys.executable
        ), mock.patch.object(
            module.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=DB_BUSY_EXIT_CODE),
        ) as run, mock.patch.object(
            sys,
            "argv",
            ["build_db.py", "--db", "target-rag", "--root", str(self.root)],
        ):
            self.assertEqual(DB_BUSY_EXIT_CODE, module.main())
        kwargs = run.call_args.kwargs
        self.assertEqual(str(RAG_ROOT), kwargs["cwd"])
        self.assertEqual(str(self.dbs_root), kwargs["env"]["RAG_DBS_ROOT"])
        for key in (
            "RAG_DB_NAME", "RAG_OUTPUT_ROOT", "LOCALRAG_OUTPUT_ROOT",
            "CHROMA_DIR_V2", "CHROMA_COLLECTION",
        ):
            self.assertNotIn(key, kwargs["env"])


class IncrementalIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dbs_root = self.root / "dbs"
        self.dbs_root.mkdir()
        self.input_root = self.root / "input"
        self.input_root.mkdir()
        (self.input_root / "document.txt").write_text("fixture", encoding="utf-8")
        self.token_budget = DocumentTokenBudget(
            tokenizer=CharacterTokenizer(),
            target_tokens=320,
            max_tokens=384,
            tokenizer_name="character-integrity-test",
            document_prefix="document: ",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_db(self, name: str, collection: str) -> Path:
        root = self.dbs_root / name
        for relative in ("data/raw", "data/clean", "index/chroma", "logs"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        for filename in ("db.json", "VERSION.json"):
            (root / filename).write_text(
                json.dumps({"db_name": name, "collection": collection}),
                encoding="utf-8",
            )
        (root / "index/manifest.json").write_text(
            json.dumps({"collection": collection, "record_count": 0}),
            encoding="utf-8",
        )
        return root

    @staticmethod
    def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
        return DatabaseWriteRuntimeTests._snapshot(root)

    def _install_actual_runtime(self, stack: ExitStack) -> dict[int, object]:
        stack.enter_context(
            mock.patch.dict(
                os.environ,
                {
                    "EMBEDDING_MODEL": "__hash__",
                    "EMBEDDING_BACKEND": "hash",
                    "EMBEDDING_DIMENSION": "32",
                    "LOCAL_RAG_LEXICAL_TOKENIZER": "fallback",
                },
                clear=False,
            )
        )
        stack.enter_context(
            mock.patch.object(
                incremental,
                "get_document_token_budget",
                return_value=self.token_budget,
            )
        )
        clients: dict[int, object] = {}
        persistent_client = store._persistent_client

        def tracked_client(path):
            client = persistent_client(path)
            clients[id(client)] = client
            return client

        stack.enter_context(
            mock.patch.object(
                store, "_persistent_client", side_effect=tracked_client
            )
        )
        return clients

    @staticmethod
    def _close_actual_clients(clients: dict[int, object]) -> None:
        for client in reversed(list(clients.values())):
            client.close()

    def _actual_id_sets(self, db_root: Path) -> tuple[set[str], ...]:
        with database_writer_session(self.dbs_root, db_root.name):
            collection = store._get_existing_collection()
            vector_ids = set(collection.get()["ids"]) if collection is not None else set()
            connection = sqlite3.connect(db_root / "catalog.sqlite")
            try:
                catalog_ids = {
                    str(row[0])
                    for row in connection.execute("SELECT chunk_uid FROM chunk")
                }
            finally:
                connection.close()
            state = json.loads((db_root / "logs/index_state.json").read_text())
            state_ids = {
                value
                for item in state["files"].values()
                for value in item.get("record_ids", [])
            }
            clean_ids = {
                json.loads(line)["id"]
                for path in (db_root / "data/clean").rglob("*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()
                if line
            }
        return vector_ids, catalog_ids, state_ids, clean_ids

    @staticmethod
    def _install_actual_crash_boundary(stack: ExitStack, stage: str) -> None:
        control = {"crashed": False, "state_writes": 0}
        upsert_vectors = incremental.upsert_records
        upsert_catalog = incremental.upsert_catalog_records
        write_clean = incremental.write_jsonl
        write_state = incremental.atomic_write_json

        def crash_after(label: str, result):
            if stage == label and not control["crashed"]:
                control["crashed"] = True
                raise RuntimeError(f"injected {label} boundary crash")
            return result

        def vector(records, progress_callback=None):
            return crash_after(
                "vector",
                upsert_vectors(records, progress_callback=progress_callback),
            )

        def catalog(records):
            return crash_after("catalog", upsert_catalog(records))

        def clean(path, records):
            return crash_after("clean", write_clean(path, records))

        def state(path, value, **kwargs):
            if Path(path).name == "index_state.json":
                control["state_writes"] += 1
                boundary = control["state_writes"] == 2
                if stage == "state_before" and boundary and not control["crashed"]:
                    control["crashed"] = True
                    raise RuntimeError("injected state pre-replace crash")
                write_state(path, value, **kwargs)
                if stage == "state_after" and boundary and not control["crashed"]:
                    control["crashed"] = True
                    raise RuntimeError("injected state post-replace crash")
                return
            write_state(path, value, **kwargs)

        stack.enter_context(
            mock.patch.object(incremental, "upsert_records", side_effect=vector)
        )
        stack.enter_context(
            mock.patch.object(
                incremental, "upsert_catalog_records", side_effect=catalog
            )
        )
        stack.enter_context(
            mock.patch.object(incremental, "write_jsonl", side_effect=clean)
        )
        stack.enter_context(
            mock.patch.object(incremental, "atomic_write_json", side_effect=state)
        )

    def _run_incremental(self, *, resume: bool = False) -> dict:
        with database_writer_session(self.dbs_root, "target-rag"):
            return incremental.add_or_update_root(
                self.input_root,
                "src-a",
                batch_size_files=1,
                resume=resume,
                document_token_budget=self.token_budget,
            )

    def test_actual_add_and_resume_ignore_poison_environment_and_preserve_canary(
        self,
    ) -> None:
        from source_manager import runner as source_runner

        target = self._make_db("target-rag", "target_custom")
        canary = self._make_db("canary-rag", "canary_custom")
        (canary / "sentinel.bin").write_bytes(b"unchanged")
        before = self._snapshot(canary)
        observed: list[tuple[str | None, str | None]] = []
        poison = {
            "RAG_DBS_ROOT": str(self.dbs_root),
            "RAG_DB_NAME": "canary-rag",
            "RAG_OUTPUT_ROOT": str(canary),
            "LOCALRAG_OUTPUT_ROOT": str(canary),
            "CHROMA_DIR_V2": str(canary / "index/chroma"),
            "CHROMA_COLLECTION": "wrong_collection",
        }
        module = WriterEntrypointTests._load(
            "actual_integrity_add", RAG_ROOT / "gen_db/add_data.py"
        )
        rebuild = WriterEntrypointTests._load(
            "actual_integrity_rebuild", RAG_ROOT / "gen_db/rebuild_component.py"
        )
        build = WriterEntrypointTests._load(
            "actual_integrity_build", RAG_ROOT / "gen_db/build_db.py"
        )
        manage = WriterEntrypointTests._load(
            "actual_integrity_manager", RAG_ROOT / "manage.py"
        )

        def dispatch(arguments, **kwargs):
            self.assertEqual(RAG_ROOT, Path(kwargs["cwd"]).resolve())
            self.assertEqual(str(self.dbs_root), kwargs["env"]["RAG_DBS_ROOT"])
            stdout, stderr = io.StringIO(), io.StringIO()
            previous_cwd = Path.cwd()
            try:
                os.chdir(kwargs["cwd"])
                with mock.patch.dict(
                    os.environ, kwargs["env"], clear=True
                ), mock.patch.object(
                    sys, "argv", list(arguments[1:])
                ), redirect_stdout(stdout), redirect_stderr(stderr):
                    returncode = module.main()
            finally:
                os.chdir(previous_cwd)
            return SimpleNamespace(
                returncode=returncode,
                stdout=stdout.getvalue(),
                stderr=stderr.getvalue(),
            )
        clients: dict[int, object] = {}
        try:
            with ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, poison, clear=False))
                clients = self._install_actual_runtime(stack)
                stack.enter_context(mock.patch.object(module, "load_env"))
                stack.enter_context(mock.patch.object(rebuild, "load_env"))
                stack.enter_context(
                    mock.patch.object(
                        build, "_runtime_python_or_exit", return_value=sys.executable
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        build, "subprocess", SimpleNamespace(run=dispatch)
                    )
                )
                stack.enter_context(redirect_stdout(io.StringIO()))
                get_collection = store._get_or_create_collection

                def observed_collection():
                    observed.append(
                        (
                            os.environ.get("CHROMA_DIR_V2"),
                            os.environ.get("CHROMA_COLLECTION"),
                        )
                    )
                    return get_collection()

                stack.enter_context(
                    mock.patch.object(
                        store,
                        "_get_or_create_collection",
                        side_effect=observed_collection,
                    )
                )
                build_args = [
                    "--reset-db",
                    "--reset-clean",
                    "--initial-database-reflection",
                ]
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "add_data.py", "--db", "target-rag", "--root",
                        str(self.input_root), "--source-id", "src-a", *build_args,
                    ],
                ):
                    self.assertEqual(0, module.main())
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "build_db.py", "--db", "target-rag", "--root",
                        str(self.input_root), "--source-id", "src-a",
                    ],
                ):
                    self.assertEqual(0, build.main())
                (self.input_root / "second.txt").write_text(
                    "second fixture", encoding="utf-8"
                )
                manager = manage.LocalRagManager(
                    rag_root=RAG_ROOT,
                    dbs_root=self.dbs_root,
                    runtime_python=Path(sys.executable),
                    runner=dispatch,
                    output_fn=lambda _message: None,
                    color=False,
                )
                completed = manager._invoke(
                    "gen_db/add_data.py",
                    [
                        "--db", "target-rag", "--root", str(self.input_root),
                        "--source-id", "src-a",
                    ],
                )
                self.assertEqual(0, completed.returncode)
                (self.input_root / "third.txt").write_text(
                    "third fixture", encoding="utf-8"
                )
                with mock.patch.object(
                    source_runner, "run_streaming_process", side_effect=dispatch
                ):
                    summary = source_runner._execute_add(
                        db_root=target,
                        source={"local_source_key": "src-a", "source_type": "local"},
                        work=self.input_root,
                        python_executable=Path(sys.executable),
                        rag_root=RAG_ROOT,
                        command_runner=None,
                        progress_callback=None,
                    )
                self.assertEqual("success", summary["status"])
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "add_data.py", "--db", "target-rag", "--root",
                        str(self.input_root), "--source-id", "src-a", "--resume",
                    ],
                ):
                    self.assertEqual(0, module.main())
                for component in ("vector", "all"):
                    with mock.patch.object(
                        sys,
                        "argv",
                        [
                            "rebuild_component.py", "--db", "target-rag",
                            "--component", component,
                        ],
                    ):
                        self.assertEqual(0, rebuild.main())
                id_sets = self._actual_id_sets(target)
                restored = {key: os.environ.get(key) for key in poison}
        finally:
            self._close_actual_clients(clients)
        self.assertTrue(id_sets[0])
        self.assertEqual(3, len(id_sets[0]))
        self.assertTrue(all(values == id_sets[0] for values in id_sets[1:]))
        self.assertTrue((target / "index/chroma/chroma.sqlite3").is_file())
        self.assertTrue((target / "catalog.sqlite").is_file())
        manifest_payload = json.loads((target / "index/manifest.json").read_text())
        self.assertEqual("target_custom", manifest_payload["collection"])
        self.assertEqual(3, manifest_payload["record_count"])
        self.assertTrue(observed)
        self.assertEqual(
            {(str(target / "index/chroma"), "target_custom")},
            set(observed),
        )
        self.assertEqual(before, self._snapshot(canary))
        self.assertEqual(poison, restored)
        self.assertEqual([], list(target.rglob(".*.tmp")))
        marker = json.loads((target / "rag-wrapper.json").read_text())
        self.assertEqual("initial_database_reflection", marker["reason"])
        self.assertTrue(marker["content_snapshot_at"].endswith("Z"))

    def test_each_existing_store_boundary_resumes_and_no_change_converges(
        self,
    ) -> None:
        for stage in ("vector", "catalog", "clean", "state_before", "state_after"):
            with self.subTest(stage=stage):
                db_root = self._make_db("target-rag", "target_custom")
                clients: dict[int, object] = {}
                try:
                    with ExitStack() as stack:
                        clients = self._install_actual_runtime(stack)
                        with database_writer_session(self.dbs_root, "target-rag"):
                            manifest.write_manifest(0)
                        self._install_actual_crash_boundary(stack, stage)
                        with self.assertRaisesRegex(RuntimeError, "injected"):
                            self._run_incremental()
                        for path in db_root.rglob("*.json"):
                            json.loads(path.read_text(encoding="utf-8"))
                        recovered = self._run_incremental(resume=True)
                        clean_before = {
                            path.relative_to(db_root).as_posix(): path.read_bytes()
                            for path in (db_root / "data/clean").rglob("*.jsonl")
                        }
                        stores_before = self._actual_id_sets(db_root)
                        unchanged = self._run_incremental()
                        stores_after = self._actual_id_sets(db_root)
                finally:
                    self._close_actual_clients(clients)
                self.assertEqual("success", recovered["result_status"])
                self.assertTrue(stores_before[0])
                self.assertTrue(
                    all(values == stores_before[0] for values in stores_before[1:])
                )
                self.assertEqual(stores_before, stores_after)
                self.assertEqual(0, unchanged["upserted_records"])
                self.assertEqual(0, unchanged["deleted_records"])
                self.assertEqual(
                    clean_before,
                    {
                        path.relative_to(db_root).as_posix(): path.read_bytes()
                        for path in (db_root / "data/clean").rglob("*.jsonl")
                    },
                )
                self.assertEqual([], list(db_root.rglob(".*.tmp")))
                shutil.rmtree(db_root)

    def test_state_read_barrier_blocks_twenty_losers_then_two_sources_converge(
        self,
    ) -> None:
        db_root = self._make_db("target-rag", "target_custom")
        second_input = self.root / "input-b"
        second_input.mkdir()
        (second_input / "second.txt").write_text("second", encoding="utf-8")
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        clients: dict[int, object] = {}
        try:
            with ExitStack() as stack:
                clients = self._install_actual_runtime(stack)
                with database_writer_session(self.dbs_root, "target-rag"):
                    manifest.write_manifest(0)
                original_load = incremental._load_state

                def barrier_load():
                    state = original_load()
                    if not entered.is_set():
                        entered.set()
                        if not release.wait(timeout=20):
                            raise RuntimeError("test barrier timed out")
                    return state

                stack.enter_context(
                    mock.patch.object(
                        incremental, "_load_state", side_effect=barrier_load
                    )
                )

                def first_writer():
                    try:
                        self._run_incremental()
                    except BaseException as exc:  # pragma: no cover - asserted below
                        errors.append(exc)

                thread = threading.Thread(target=first_writer)
                thread.start()
                self.assertTrue(entered.wait(timeout=20))
                before = self._snapshot(db_root)
                with ThreadPoolExecutor(max_workers=20) as executor:
                    attempts = list(
                        executor.map(
                            lambda _index: DatabaseWriteRuntimeTests._probe(
                                self, self.dbs_root, "target-rag"
                            ),
                            range(20),
                        )
                    )
                self.assertTrue(
                    all(item.returncode == DB_BUSY_EXIT_CODE for item in attempts)
                )
                self.assertTrue(
                    all(json.loads(item.stdout)["retryable"] for item in attempts)
                )
                self.assertEqual(before, self._snapshot(db_root))
                release.set()
                thread.join(timeout=20)
                self.assertFalse(thread.is_alive())
                self.assertEqual([], errors)
                with database_writer_session(self.dbs_root, "target-rag"):
                    incremental.add_or_update_root(
                        second_input,
                        "src-b",
                        batch_size_files=1,
                        document_token_budget=self.token_budget,
                    )
                id_sets = self._actual_id_sets(db_root)
        finally:
            self._close_actual_clients(clients)
        state = json.loads((db_root / "logs/index_state.json").read_text())
        sources = {str(item.get("source_id")) for item in state["files"].values()}
        self.assertEqual({"src-a", "src-b"}, sources)
        self.assertTrue(all(values == id_sets[0] for values in id_sets[1:]))
        self.assertEqual(2, len(id_sets[0]))


class AtomicCheckpointTests(unittest.TestCase):
    def test_replace_failure_preserves_complete_old_json_and_removes_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress.json"
            atomic_write_json(path, {"generation": "old"})
            with mock.patch(
                "software_rag_tool.atomic_io.os.replace",
                side_effect=OSError("injected replace failure"),
            ), self.assertRaisesRegex(OSError, "injected"):
                atomic_write_json(path, {"generation": "new"})
            self.assertEqual({"generation": "old"}, json.loads(path.read_text()))
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))
            atomic_write_json(path, {"generation": "new"})
            self.assertEqual({"generation": "new"}, json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
