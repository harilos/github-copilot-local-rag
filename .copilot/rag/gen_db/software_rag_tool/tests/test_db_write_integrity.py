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
     ëMô¶‰Ëkºwµçy}µ…­•}‘ˆ ‰…¹…ÉäµÉ…œˆ°€‰…¹…Éå}ÕÍÑ½´ˆ¤4(€€€€€€€€¡…¹…Éä€¼€‰Í•¹Ñ¥¹•°¹‰¥¸ˆ¤¹İÉ¥Ñ•}‰åÑ•Ì¡ˆ‰Õ¹¡…¹•ˆ¤4(€€€€€€€‰•™½É”€ôÍ•±˜¹}Í¹…ÁÍ¡½Ğ¡…¹…Éä¤4(€€€€€€€½‰Í•ÉÙ•è±¥ÍÑmÑÕÁ±•mÍÑÈğ9½¹”°ÍÑÈğ9½¹•ut€ômt4(€€€€€€€Á½¥Í½¸€ôì4(€€€€€€€€€€€€‰I}	M}I==PˆèÍÑÈ¡Í•±˜¹‘‰Í}É½½Ğ¤°4(€€€€€€€€€€€€‰I}	}95ˆè€‰…¹…ÉäµÉ…œˆ°4(€€€€€€€€€€€€‰I}=UQAUQ}I==PˆèÍÑÈ¡…¹…Éä¤°4(€€€€€€€€€€€€‰1=1I}=UQAUQ}I==PˆèÍÑÈ¡…¹…Éä¤°4(€€€€€€€€€€€€‰!I=5}%I}XÈˆèÍÑÈ¡…¹…Éä€¼€‰¥¹‘•à½¡É½µ„ˆ¤°4(€€€€€€€€€€€€‰!I=5}=11Q%=8ˆè€‰İÉ½¹}½±±•Ñ¥½¸ˆ°4(€€€€€€€ô4(€€€€€€€µ½‘Õ±”€ô]É¥Ñ•É¹ÑÉåÁ½¥¹ÑQ•ÍÑÌ¹}±½… 4(€€€€€€€€€€€€‰…ÑÕ…±}¥¹Ñ•É¥Ñå}…‘ˆ°I}I==P€¼€‰•¹}‘ˆ½…‘‘}‘…Ñ„¹Áäˆ4(€€€€€€€€¤4(€€€€€€€É•‰Õ¥±€ô]É¥Ñ•É¹ÑÉåÁ½¥¹ÑQ•ÍÑÌ¹}±½… 4(€€€€€€€€€€€€‰…ÑÕ…±}¥¹Ñ•É¥Ñå}É•‰Õ¥±ˆ°I}I==P€¼€‰•¹}‘ˆ½É•‰Õ¥±‘}½µÁ½¹•¹Ğ¹Áäˆ4(€€€€€€€€¤4(€€€€€€€‰Õ¥±€ô]É¥Ñ•É¹ÑÉåÁ½¥¹ÑQ•ÍÑÌ¹}±½… 4(€€€€€€€€€€€€‰…ÑÕ…±}¥¹Ñ•É¥Ñå}‰Õ¥±ˆ°I}I==P€¼€‰•¹}‘ˆ½‰Õ¥±‘}‘ˆ¹Áäˆ4(€€€€€€€€¤4(€€€€€€€µ…¹…”€ô]É¥Ñ•É¹ÑÉåÁ½¥¹ÑQ•ÍÑÌ¹}±½… 4(€€€€€€€€€€€€‰…ÑÕ…±}¥¹Ñ•É¥Ñå}µ…¹…•Èˆ°I}I==P€¼€‰µ…¹…”¹Áäˆ4(€€€€€€€€¤4(4(€€€€€€€‘•˜‘¥ÍÁ…Ñ ¡…ÉÕµ•¹ÑÌ°€¨©­İ…ÉÌ¤è4(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡I}I==P°A…Ñ ¡­İ…ÉÍl‰İ‰t¤¹É•Í½±Ù” ¤¤4(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡ÍÑÈ¡Í•±˜¹‘‰Í}É½½Ğ¤°­İ…ÉÍl‰•¹Ø‰ul‰I}	M}I==P‰t¤4(€€€€€€€€€€€ÍÑ‘½ÕĞ°ÍÑ‘•ÉÈ€ô¥¼¹MÑÉ¥¹%< ¤°¥¼¹MÑÉ¥¹%< ¤4(€€€€€€€€€€€ÁÉ•Ù¥½ÕÍ}İ€ôA…Ñ ¹İ ¤4(€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€½Ì¹¡‘¥È¡­İ…ÉÍl‰İ‰t¤4(€€€€€€€€€€€€€€€İ¥Ñ µ½¬¹Á…Ñ ¹‘¥Ğ 4(€€€€€€€€€€€€€€€€€€€½Ì¹•¹Ù¥É½¸°­İ…ÉÍl‰•¹Ø‰t°±•…ÈõQÉÕ”4(€€€€€€€€€€€€€€€€¤°µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€ÍåÌ°€‰…ÉØˆ°±¥ÍĞ¡…ÉÕµ•¹ÑÍlÄét¤4(€€€€€€€€€€€€€€€€¤°É•‘¥É•Ñ}ÍÑ‘½ÕĞ¡ÍÑ‘½ÕĞ¤°É•‘¥É•Ñ}ÍÑ‘•ÉÈ¡ÍÑ‘•ÉÈ¤è4(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¹½‘”€ôµ½‘Õ±”¹µ…¥¸ ¤4(€€€€€€€€€€€™¥¹…±±äè4(€€€€€€€€€€€€€€€½Ì¹¡‘¥È¡ÁÉ•Ù¥½ÕÍ}İ¤4(€€€€€€€€€€€É•ÑÕÉ¸M¥µÁ±•9…µ•ÍÁ…” 4(€€€€€€€€€€€€€€€É•ÑÕÉ¹½‘”õÉ•ÑÕÉ¹½‘”°4(€€€€€€€€€€€€€€€ÍÑ‘½ÕĞõÍÑ‘½ÕĞ¹•ÑÙ…±Õ” ¤°4(€€€€€€€€€€€€€€€ÍÑ‘•ÉÈõÍÑ‘•ÉÈ¹•ÑÙ…±Õ” ¤°4(€€€€€€€€€€€€¤4(€€€€€€€±¥•¹ÑÌè‘¥Ñm¥¹Ğ°½‰©•Ñt€ôíô4(€€€€€€€ÑÉäè4(€€€€€€€€€€€İ¥Ñ á¥ÑMÑ…¬ ¤…ÌÍÑ…¬è4(€€€€€€€€€€€€€€€ÍÑ…¬¹•¹Ñ•É}½¹Ñ•áĞ¡µ½¬¹Á…Ñ ¹‘¥Ğ¡½Ì¹•¹Ù¥É½¸°Á½¥Í½¸°±•…Èõ…±Í”¤¤4(€€€€€€€€€€€€€€€±¥•¹ÑÌ€ôÍ•±˜¹}¥¹ÍÑ…±±}…ÑÕ…±}ÉÕ¹Ñ¥µ”¡ÍÑ…¬¤4(€€€€€€€€€€€€€€€ÍÑ…¬¹•¹Ñ•É}½¹Ñ•áĞ¡µ½¬¹Á…Ñ ¹½‰©•Ğ¡µ½‘Õ±”°€‰±½…‘}•¹Øˆ¤¤4(€€€€€€€€€€€€€€€ÍÑ…¬¹•¹Ñ•É}½¹Ñ•áĞ¡µ½¬¹Á…Ñ ¹½‰©•Ğ¡É•‰Õ¥±°€‰±½…‘}•¹Øˆ¤¤4(€€€€€€€€€€€€€€€ÍÑ…¬¹•¹Ñ•É}½¹Ñ•áĞ 4(€€€€€€€€€€€€€€€€€€€µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€€€€€‰Õ¥±°€‰}ÉÕ¹Ñ¥µ•}ÁåÑ¡½¹}½É}•á¥Ğˆ°É•ÑÕÉ¹}Ù…±Õ”õÍåÌ¹•á•ÕÑ…‰±”4(€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€ÍÑ…¬¹•¹Ñ•É}½¹Ñ•áĞ 4(€€€€€€€€€€€€€€€€€€€µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€€€€€‰Õ¥±°€‰ÍÕ‰ÁÉ½•ÍÌˆ°M¥µÁ±•9…µ•ÍÁ…”¡ÉÕ¸õ‘¥ÍÁ…Ñ ¤4(€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€ÍÑ…¬¹•¹Ñ•É}½¹Ñ•áĞ¡É•‘¥É•Ñ}ÍÑ‘½ÕĞ¡¥¼¹MÑÉ¥¹%< ¤¤¤4(€€€€€€€€€€€€€€€•Ñ}½±±•Ñ¥½¸€ôÍÑ½É”¹}•Ñ}½É}É•…Ñ•}½±±•Ñ¥½¸4(4(€€€€€€€€€€€€€€€‘•˜½‰Í•ÉÙ•‘}½±±•Ñ¥½¸ ¤è4(€€€€€€€€€€€€€€€€€€€½‰Í•ÉÙ•¹…ÁÁ•¹ 4(€€€€€€€€€€€€€€€€€€€€€€€€ 4(€€€€€€€€€€€€€€€€€€€€€€€€€€€½Ì¹•¹Ù¥É½¸¹•Ğ ‰!I=5}%I}XÈˆ¤°4(€€€€€€€€€€€€€€€€€€€€€€€€€€€½Ì¹•¹Ù¥É½¸¹•Ğ ‰!I=5}=11Q%=8ˆ¤°4(€€€€€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸•Ñ}½±±•Ñ¥½¸ ¤4(4(€€€€€€€€€€€€€€€ÍÑ…¬¹•¹Ñ•É}½¹Ñ•áĞ 4(€€€€€€€€€€€€€€€€€€€µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ½É”°4(€€€€€€€€€€€€€€€€€€€€€€€€‰}•Ñ}½É}É•…Ñ•}½±±•Ñ¥½¸ˆ°4(€€€€€€€€€€€€€€€€€€€€€€€Í¥‘•}•™™•Ğõ½‰Í•ÉÙ•‘}½±±•Ñ¥½¸°4(€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€‰Õ¥±‘}…ÉÌ€ôl4(€€€€€€€€€€€€€€€€€€€€ˆ´µÉ•Í•Ğµ‘ˆˆ°4(€€€€€€€€€€€€€€€€€€€€ˆ´µÉ•Í•Ğµ±•…¸ˆ°4(€€€€€€€€€€€€€€€€€€€€ˆ´µ¥¹¥Ñ¥…°µ‘…Ñ…‰…Í”µÉ•™±•Ñ¥½¸ˆ°4(€€€€€€€€€€€€€€€t4(€€€€€€€€€€€€€€€İ¥Ñ µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€ÍåÌ°4(€€€€€€€€€€€€€€€€€€€€‰…ÉØˆ°4(€€€€€€€€€€€€€€€€€€€l4(€€€€€€€€€€€€€€€€€€€€€€€€‰…‘‘}‘…Ñ„¹Áäˆ°€ˆ´µ‘ˆˆ°€‰Ñ…É•ĞµÉ…œˆ°€ˆ´µÉ½½Ğˆ°4(€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡Í•±˜¹¥¹ÁÕÑ}É½½Ğ¤°€ˆ´µÍ½ÕÉ”µ¥ˆ°€‰ÍÉŒµ„ˆ°€©‰Õ¥±‘}…ÉÌ°4(€€€€€€€€€€€€€€€€€€€t°4(€€€€€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° À°µ½‘Õ±”¹µ…¥¸ ¤¤4(€€€€€€€€€€€€€€€İ¥Ñ µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€ÍåÌ°4(€€€€€€€€€€€€€€€€€€€€‰…ÉØˆ°4(€€€€€€€€€€€€€€€€€€€l4(€€€€€€€€€€€€€€€€€€€€€€€€‰‰Õ¥±‘}‘ˆ¹Áäˆ°€ˆ´µ‘ˆˆ°€‰Ñ…É•ĞµÉ…œˆ°€ˆ´µÉ½½Ğˆ°4(€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡Í•±˜¹¥¹ÁÕÑ}É½½Ğ¤°€ˆ´µÍ½ÕÉ”µ¥ˆ°€‰ÍÉŒµ„ˆ°4(€€€€€€€€€€€€€€€€€€€t°4(€€€€€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° À°‰Õ¥±¹µ…¥¸ ¤¤4(€€€€€€€€€€€€€€€€¡Í•±˜¹¥¹ÁÕÑ}É½½Ğ€¼€‰Í•½¹¹ÑáĞˆ¤¹İÉ¥Ñ•}Ñ•áĞ 4(€€€€€€€€€€€€€€€€€€€€‰Í•½¹™¥áÑÕÉ”ˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€µ…¹…•È€ôµ…¹…”¹1½…±I…5…¹…•È 4(€€€€€€€€€€€€€€€€€€€É…}É½½ĞõI}I==P°4(€€€€€€€€€€€€€€€€€€€‘‰Í}É½½ĞõÍ•±˜¹‘‰Í}É½½Ğ°4(€€€€€€€€€€€€€€€€€€€ÉÕ¹Ñ¥µ•}ÁåÑ¡½¸õA…Ñ ¡ÍåÌ¹•á•ÕÑ…‰±”¤°4(€€€€€€€€€€€€€€€€€€€ÉÕ¹¹•Èõ‘¥ÍÁ…Ñ °4(€€€€€€€€€€€€€€€€€€€½ÕÑÁÕÑ}™¸õ±…µ‰‘„}µ•ÍÍ…”è9½¹”°4(€€€€€€€€€€€€€€€€€€€½±½Èõ…±Í”°4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€½µÁ±•Ñ•€ôµ…¹…•È¹}¥¹Ù½­” 4(€€€€€€€€€€€€€€€€€€€€‰•¹}‘ˆ½…‘‘}‘…Ñ„¹Áäˆ°4(€€€€€€€€€€€€€€€€€€€l4(€€€€€€€€€€€€€€€€€€€€€€€€ˆ´µ‘ˆˆ°€‰Ñ…É•ĞµÉ…œˆ°€ˆ´µÉ½½Ğˆ°ÍÑÈ¡Í•±˜¹¥¹ÁÕÑ}É½½Ğ¤°4(€€€€€€€€€€€€€€€€€€€€€€€€ˆ´µÍ½ÕÉ”µ¥ˆ°€‰ÍÉŒµ„ˆ°4(€€€€€€€€€€€€€€€€€€€t°4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° À°½µÁ±•Ñ•¹É•ÑÕÉ¹½‘”¤4(€€€€€€€€€€€€€€€€¡Í•±˜¹¥¹ÁÕÑ}É½½Ğ€¼€‰Ñ¡¥É¹ÑáĞˆ¤¹İÉ¥Ñ•}Ñ•áĞ 4(€€€€€€€€€€€€€€€€€€€€‰Ñ¡¥É™¥áÑÕÉ”ˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€İ¥Ñ µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€Í½ÕÉ•}ÉÕ¹¹•È°€‰ÉÕ¹}ÍÑÉ•…µ¥¹}ÁÉ½•ÍÌˆ°Í¥‘•}•™™•Ğõ‘¥ÍÁ…Ñ 4(€€€€€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€€€€€ÍÕµµ…Éä€ôÍ½ÕÉ•}ÉÕ¹¹•È¹}•á•ÕÑ•}…‘ 4(€€€€€€€€€€€€€€€€€€€€€€€‘‰}É½½ĞõÑ…É•Ğ°4(€€€€€€€€€€€€€€€€€€€€€€€Í½ÕÉ”õì‰±½…±}Í½ÕÉ•}­•äˆè€‰ÍÉŒµ„ˆ°€‰Í½ÕÉ•}ÑåÁ”ˆè€‰±½…°‰ô°4(€€€€€€€€€€€€€€€€€€€€€€€İ½É¬õÍ•±˜¹¥¹ÁÕÑ}É½½Ğ°4(€€€€€€€€€€€€€€€€€€€€€€€ÁåÑ¡½¹}•á•ÕÑ…‰±”õA…Ñ ¡ÍåÌ¹•á•ÕÑ…‰±”¤°4(€€€€€€€€€€€€€€€€€€€€€€€É…}É½½ĞõI}I==P°4(€€€€€€€€€€€€€€€€€€€€€€€½µµ…¹‘}ÉÕ¹¹•Èõ9½¹”°4(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ½É•ÍÍ}…±±‰…¬õ9½¹”°4(€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° ‰ÍÕ•ÍÌˆ°ÍÕµµ…Éål‰ÍÑ…ÑÕÌ‰t¤4(€€€€€€€€€€€€€€€İ¥Ñ µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€ÍåÌ°4(€€€€€€€€€€€€€€€€€€€€‰…ÉØˆ°4(€€€€€€€€€€€€€€€€€€€l4(€€€€€€€€€€€€€€€€€€€€€€€€‰…‘‘}‘…Ñ„¹Áäˆ°€ˆ´µ‘ˆˆ°€‰Ñ…É•ĞµÉ…œˆ°€ˆ´µÉ½½Ğˆ°4(€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡Í•±˜¹¥¹ÁÕÑ}É½½Ğ¤°€ˆ´µÍ½ÕÉ”µ¥ˆ°€‰ÍÉŒµ„ˆ°€ˆ´µÉ•ÍÕµ”ˆ°4(€€€€€€€€€€€€€€€€€€€t°4(€€€€€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° À°µ½‘Õ±”¹µ…¥¸ ¤¤4(€€€€€€€€€€€€€€€™½È½µÁ½¹•¹Ğ¥¸€ ‰Ù•Ñ½Èˆ°€‰…±°ˆ¤è4(€€€€€€€€€€€€€€€€€€€İ¥Ñ µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€€€€€ÍåÌ°4(€€€€€€€€€€€€€€€€€€€€€€€€‰…ÉØˆ°4(€€€€€€€€€€€€€€€€€€€€€€€l4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•‰Õ¥±‘}½µÁ½¹•¹Ğ¹Áäˆ°€ˆ´µ‘ˆˆ°€‰Ñ…É•ĞµÉ…œˆ°4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€ˆ´µ½µÁ½¹•¹Ğˆ°½µÁ½¹•¹Ğ°4(€€€€€€€€€€€€€€€€€€€€€€€t°4(€€€€€€€€€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° À°É•‰Õ¥±¹µ…¥¸ ¤¤4(€€€€€€€€€€€€€€€¥‘}Í•ÑÌ€ôÍ•±˜¹}…ÑÕ…±}¥‘}Í•ÑÌ¡Ñ…É•Ğ¤4(€€€€€€€€€€€€€€€É•ÍÑ½É•€ôí­•äè½Ì¹•¹Ù¥É½¸¹•Ğ¡­•ä¤™½È­•ä¥¸Á½¥Í½¹ô4(€€€€€€€™¥¹…±±äè4(€€€€€€€€€€€Í•±˜¹}±½Í•}…ÑÕ…±}±¥•¹ÑÌ¡±¥•¹ÑÌ¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡¥‘}Í•ÑÍlÁt¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° Ì°±•¸¡¥‘}Í•ÑÍlÁt¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡…±°¡Ù…±Õ•Ì€ôô¥‘}Í•ÑÍlÁt™½ÈÙ…±Õ•Ì¥¸¥‘}Í•ÑÍlÄét¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ” ¡Ñ…É•Ğ€¼€‰¥¹‘•à½¡É½µ„½¡É½µ„¹ÍÅ±¥Ñ”Ìˆ¤¹¥Í}™¥±” ¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ” ¡Ñ…É•Ğ€¼€‰…Ñ…±½œ¹ÍÅ±¥Ñ”ˆ¤¹¥Í}™¥±” ¤¤4(€€€€€€€µ…¹¥™•ÍÑ}Á…å±½…€ô©Í½¸¹±½…‘Ì ¡Ñ…É•Ğ€¼€‰¥¹‘•à½µ…¹¥™•ÍĞ¹©Í½¸ˆ¤¹É•…‘}Ñ•áĞ ¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° ‰Ñ…É•Ñ}ÕÍÑ½´ˆ°µ…¹¥™•ÍÑ}Á…å±½…‘l‰½±±•Ñ¥½¸‰t¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° Ì°µ…¹¥™•ÍÑ}Á…å±½…‘l‰É•½É‘}½Õ¹Ğ‰t¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡½‰Í•ÉÙ•¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° 4(€€€€€€€€€€€ì¡ÍÑÈ¡Ñ…É•Ğ€¼€‰¥¹‘•à½¡É½µ„ˆ¤°€‰Ñ…É•Ñ}ÕÍÑ½´ˆ¥ô°4(€€€€€€€€€€€Í•Ğ¡½‰Í•ÉÙ•¤°4(€€€€€€€€¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡‰•™½É”°Í•±˜¹}Í¹…ÁÍ¡½Ğ¡…¹…Éä¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡Á½¥Í½¸°É•ÍÑ½É•¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡mt°±¥ÍĞ¡Ñ…É•Ğ¹É±½ˆ ˆ¸¨¹ÑµÀˆ¤¤¤4(€€€€€€€µ…É­•È€ô©Í½¸¹±½…‘Ì ¡Ñ…É•Ğ€¼€‰É…œµİÉ…ÁÁ•È¹©Í½¸ˆ¤¹É•…‘}Ñ•áĞ ¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° ‰¥¹¥Ñ¥…±}‘…Ñ…‰…Í•}É•™±•Ñ¥½¸ˆ°µ…É­•Él‰É•…Í½¸‰t¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡µ…É­•Él‰½¹Ñ•¹Ñ}Í¹…ÁÍ¡½Ñ}…Ğ‰t¹•¹‘Íİ¥Ñ  ‰hˆ¤¤4(4(€€€‘•˜Ñ•ÍÑ}•…¡}•á¥ÍÑ¥¹}ÍÑ½É•}‰½Õ¹‘…Éå}É•ÍÕµ•Í}…¹‘}¹½}¡…¹•}½¹Ù•É•Ì 4(€€€€€€€Í•±˜°4(€€€€¤€´ø9½¹”è4(€€€€€€€™½ÈÍÑ…”¥¸€ ‰Ù•Ñ½Èˆ°€‰…Ñ…±½œˆ°€‰±•…¸ˆ°€‰ÍÑ…Ñ•}‰•™½É”ˆ°€‰ÍÑ…Ñ•}…™Ñ•Èˆ¤è4(€€€€€€€€€€€İ¥Ñ Í•±˜¹ÍÕ‰Q•ÍĞ¡ÍÑ…”õÍÑ…”¤è4(€€€€€€€€€€€€€€€‘‰}É½½Ğ€ôÍ•±˜¹}µ…­•}‘ˆ ‰Ñ…É•ĞµÉ…œˆ°€‰Ñ…É•Ñ}ÕÍÑ½´ˆ¤4(€€€€€€€€€€€€€€€±¥•¹ÑÌè‘¥Ñm¥¹Ğ°½‰©•Ñt€ôíô4(€€€€€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€€€€€İ¥Ñ á¥ÑMÑ…¬ ¤…ÌÍÑ…¬è4(€€€€€€€€€€€€€€€€€€€€€€€±¥•¹ÑÌ€ôÍ•±˜¹}¥¹ÍÑ…±±}…ÑÕ…±}ÉÕ¹Ñ¥µ”¡ÍÑ…¬¤4(€€€€€€€€€€€€€€€€€€€€€€€İ¥Ñ ‘…Ñ…‰…Í•}İÉ¥Ñ•É}Í•ÍÍ¥½¸¡Í•±˜¹‘‰Í}É½½Ğ°€‰Ñ…É•ĞµÉ…œˆ¤è4(€€€€€€€€€€€€€€€€€€€€€€€€€€€µ…¹¥™•ÍĞ¹İÉ¥Ñ•}µ…¹¥™•ÍĞ À¤4(€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹}¥¹ÍÑ…±±}…ÑÕ…±}É…Í¡}‰½Õ¹‘…Éä¡ÍÑ…¬°ÍÑ…”¤4(€€€€€€€€€€€€€€€€€€€€€€€İ¥Ñ Í•±˜¹…ÍÍ•ÉÑI…¥Í•ÍI••à¡IÕ¹Ñ¥µ•ÉÉ½È°€‰¥¹©•Ñ•ˆ¤è4(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹}ÉÕ¹}¥¹É•µ•¹Ñ…° ¤4(€€€€€€€€€€€€€€€€€€€€€€€™½ÈÁ…Ñ ¥¸‘‰}É½½Ğ¹É±½ˆ ˆ¨¹©Í½¸ˆ¤è4(€€€€€€€€€€€€€€€€€€€€€€€€€€€©Í½¸¹±½…‘Ì¡Á…Ñ ¹É•…‘}Ñ•áĞ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¤4(€€€€€€€€€€€€€€€€€€€€€€€É•½Ù•É•€ôÍ•±˜¹}ÉÕ¹}¥¹É•µ•¹Ñ…°¡É•ÍÕµ”õQÉÕ”¤4(€€€€€€€€€€€€€€€€€€€€€€€±•…¹}‰•™½É”€ôì4(€€€€€€€€€€€€€€€€€€€€€€€€€€€Á…Ñ ¹É•±…Ñ¥Ù•}Ñ¼¡‘‰}É½½Ğ¤¹…Í}Á½Í¥à ¤èÁ…Ñ ¹É•…‘}‰åÑ•Ì ¤4(€€€€€€€€€€€€€€€€€€€€€€€€€€€™½ÈÁ…Ñ ¥¸€¡‘‰}É½½Ğ€¼€‰‘…Ñ„½±•…¸ˆ¤¹É±½ˆ ˆ¨¹©Í½¹°ˆ¤4(€€€€€€€€€€€€€€€€€€€€€€€ô4(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ½É•Í}‰•™½É”€ôÍ•±˜¹}…ÑÕ…±}¥‘}Í•ÑÌ¡‘‰}É½½Ğ¤4(€€€€€€€€€€€€€€€€€€€€€€€Õ¹¡…¹•€ôÍ•±˜¹}ÉÕ¹}¥¹É•µ•¹Ñ…° ¤4(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ½É•Í}…™Ñ•È€ôÍ•±˜¹}…ÑÕ…±}¥‘}Í•ÑÌ¡‘‰}É½½Ğ¤4(€€€€€€€€€€€€€€€™¥¹…±±äè4(€€€€€€€€€€€€€€€€€€€Í•±˜¹}±½Í•}…ÑÕ…±}±¥•¹ÑÌ¡±¥•¹ÑÌ¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° ‰ÍÕ•ÍÌˆ°É•½Ù•É•‘l‰É•ÍÕ±Ñ}ÍÑ…ÑÕÌ‰t¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡ÍÑ½É•Í}‰•™½É•lÁt¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ” 4(€€€€€€€€€€€€€€€€€€€…±°¡Ù…±Õ•Ì€ôôÍÑ½É•Í}‰•™½É•lÁt™½ÈÙ…±Õ•Ì¥¸ÍÑ½É•Í}‰•™½É•lÄét¤4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡ÍÑ½É•Í}‰•™½É”°ÍÑ½É•Í}…™Ñ•È¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° À°Õ¹¡…¹•‘l‰ÕÁÍ•ÉÑ•‘}É•½É‘Ì‰t¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° À°Õ¹¡…¹•‘l‰‘•±•Ñ•‘}É•½É‘Ì‰t¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° 4(€€€€€€€€€€€€€€€€€€€±•…¹}‰•™½É”°4(€€€€€€€€€€€€€€€€€€€ì4(€€€€€€€€€€€€€€€€€€€€€€€Á…Ñ ¹É•±…Ñ¥Ù•}Ñ¼¡‘‰}É½½Ğ¤¹…Í}Á½Í¥à ¤èÁ…Ñ ¹É•…‘}‰åÑ•Ì ¤4(€€€€€€€€€€€€€€€€€€€€€€€™½ÈÁ…Ñ ¥¸€¡‘‰}É½½Ğ€¼€‰‘…Ñ„½±•…¸ˆ¤¹É±½ˆ ˆ¨¹©Í½¹°ˆ¤4(€€€€€€€€€€€€€€€€€€€ô°4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡mt°±¥ÍĞ¡‘‰}É½½Ğ¹É±½ˆ ˆ¸¨¹ÑµÀˆ¤¤¤4(€€€€€€€€€€€€€€€Í¡ÕÑ¥°¹ÉµÑÉ•”¡‘‰}É½½Ğ¤4(4(€€€‘•˜Ñ•ÍÑ}ÍÑ…Ñ•}É•…‘}‰…ÉÉ¥•É}‰±½­Í}Ñİ•¹Ñå}±½Í•ÉÍ}Ñ¡•¹}Ñİ½}Í½ÕÉ•Í}½¹Ù•É” 4(€€€€€€€Í•±˜°4(€€€€¤€´ø9½¹”è4(€€€€€€€‘‰}É½½Ğ€ôÍ•±˜¹}µ…­•}‘ˆ ‰Ñ…É•ĞµÉ…œˆ°€‰Ñ…É•Ñ}ÕÍÑ½´ˆ¤4(€€€€€€€Í•½¹‘}¥¹ÁÕĞ€ôÍ•±˜¹É½½Ğ€¼€‰¥¹ÁÕĞµˆˆ4(€€€€€€€Í•½¹‘}¥¹ÁÕĞ¹µ­‘¥È ¤4(€€€€€€€€¡Í•½¹‘}¥¹ÁÕĞ€¼€‰Í•½¹¹ÑáĞˆ¤¹İÉ¥Ñ•}Ñ•áĞ ‰Í•½¹ˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤4(€€€€€€€•¹Ñ•É•€ôÑ¡É•…‘¥¹œ¹Ù•¹Ğ ¤4(€€€€€€€É•±•…Í”€ôÑ¡É•…‘¥¹œ¹Ù•¹Ğ ¤4(€€€€€€€•ÉÉ½ÉÌè±¥ÍÑm	…Í•á•ÁÑ¥½¹t€ômt4(€€€€€€€±¥•¹ÑÌè‘¥Ñm¥¹Ğ°½‰©•Ñt€ôíô4(€€€€€€€ÑÉäè4(€€€€€€€€€€€İ¥Ñ á¥ÑMÑ…¬ ¤…ÌÍÑ…¬è4(€€€€€€€€€€€€€€€±¥•¹ÑÌ€ôÍ•±˜¹}¥¹ÍÑ…±±}…ÑÕ…±}ÉÕ¹Ñ¥µ”¡ÍÑ…¬¤4(€€€€€€€€€€€€€€€İ¥Ñ ‘…Ñ…‰…Í•}İÉ¥Ñ•É}Í•ÍÍ¥½¸¡Í•±˜¹‘‰Í}É½½Ğ°€‰Ñ…É•ĞµÉ…œˆ¤è4(€€€€€€€€€€€€€€€€€€€µ…¹¥™•ÍĞ¹İÉ¥Ñ•}µ…¹¥™•ÍĞ À¤4(€€€€€€€€€€€€€€€½É¥¥¹…±}±½…€ô¥¹É•µ•¹Ñ…°¹}±½…‘}ÍÑ…Ñ”4(4(€€€€€€€€€€€€€€€‘•˜‰…ÉÉ¥•É}±½… ¤è4(€€€€€€€€€€€€€€€€€€€ÍÑ…Ñ”€ô½É¥¥¹…±}±½… ¤4(€€€€€€€€€€€€€€€€€€€¥˜¹½Ğ•¹Ñ•É•¹¥Í}Í•Ğ ¤è4(€€€€€€€€€€€€€€€€€€€€€€€•¹Ñ•É•¹Í•Ğ ¤4(€€€€€€€€€€€€€€€€€€€€€€€¥˜¹½ĞÉ•±•…Í”¹İ…¥Ğ¡Ñ¥µ•½ÕĞôÈÀ¤è4(€€€€€€€€€€€€€€€€€€€€€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È ‰Ñ•ÍĞ‰…ÉÉ¥•ÈÑ¥µ•½ÕĞˆ¤4(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸ÍÑ…Ñ”4(4(€€€€€€€€€€€€€€€ÍÑ…¬¹•¹Ñ•É}½¹Ñ•áĞ 4(€€€€€€€€€€€€€€€€€€€µ½¬¹Á…Ñ ¹½‰©•Ğ 4(€€€€€€€€€€€€€€€€€€€€€€€¥¹É•µ•¹Ñ…°°€‰}±½…‘}ÍÑ…Ñ”ˆ°Í¥‘•}•™™•Ğõ‰…ÉÉ¥•É}±½…4(€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€€¤4(4(€€€€€€€€€€€€€€€‘•˜™¥ÉÍÑ}İÉ¥Ñ•È ¤è4(€€€€€€€€€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€€€€€€€€€Í•±˜¹}ÉÕ¹}¥¹É•µ•¹Ñ…° ¤4(€€€€€€€€€€€€€€€€€€€•á•ÁĞ	…Í•á•ÁÑ¥½¸…Ì•áŒè€€ŒÁÉ…µ„è¹¼½Ù•È€´…ÍÍ•ÉÑ•‰•±½Ü4(€€€€€€€€€€€€€€€€€€€€€€€•ÉÉ½ÉÌ¹…ÁÁ•¹¡•áŒ¤4(4(€€€€€€€€€€€€€€€Ñ¡É•…€ôÑ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•Ğõ™¥ÉÍÑ}İÉ¥Ñ•È¤4(€€€€€€€€€€€€€€€Ñ¡É•…¹ÍÑ…ÉĞ ¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡•¹Ñ•É•¹İ…¥Ğ¡Ñ¥µ•½ÕĞôÈÀ¤¤4(€€€€€€€€€€€€€€€‰•™½É”€ôÍ•±˜¹}Í¹…ÁÍ¡½Ğ¡‘‰}É½½Ğ¤4(€€€€€€€€€€€€€€€İ¥Ñ Q¡É•…‘A½½±á•ÕÑ½È¡µ…á}İ½É­•ÉÌôÈÀ¤…Ì•á•ÕÑ½Èè4(€€€€€€€€€€€€€€€€€€€…ÑÑ•µÁÑÌ€ô±¥ÍĞ 4(€€€€€€€€€€€€€€€€€€€€€€€•á•ÕÑ½È¹µ…À 4(€€€€€€€€€€€€€€€€€€€€€€€€€€€±…µ‰‘„}¥¹‘•àè…Ñ…‰…Í•]É¥Ñ•IÕ¹Ñ¥µ•Q•ÍÑÌ¹}ÁÉ½‰” 4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•±˜°Í•±˜¹‘‰Í}É½½Ğ°€‰Ñ…É•ĞµÉ…œˆ4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤°4(€€€€€€€€€€€€€€€€€€€€€€€€€€€É…¹” ÈÀ¤°4(€€€€€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ” 4(€€€€€€€€€€€€€€€€€€€…±°¡¥Ñ•´¹É•ÑÕÉ¹½‘”€ôô	}	UMe}a%Q}=™½È¥Ñ•´¥¸…ÑÑ•µÁÑÌ¤4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ” 4(€€€€€€€€€€€€€€€€€€€…±°¡©Í½¸¹±½…‘Ì¡¥Ñ•´¹ÍÑ‘½ÕĞ¥l‰É•ÑÉå…‰±”‰t™½È¥Ñ•´¥¸…ÑÑ•µÁÑÌ¤4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡‰•™½É”°Í•±˜¹}Í¹…ÁÍ¡½Ğ¡‘‰}É½½Ğ¤¤4(€€€€€€€€€€€€€€€É•±•…Í”¹Í•Ğ ¤4(€€€€€€€€€€€€€€€Ñ¡É•…¹©½¥¸¡Ñ¥µ•½ÕĞôÈÀ¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑ…±Í”¡Ñ¡É•…¹¥Í}…±¥Ù” ¤¤4(€€€€€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡mt°•ÉÉ½ÉÌ¤4(€€€€€€€€€€€€€€€İ¥Ñ ‘…Ñ…‰…Í•}İÉ¥Ñ•É}Í•ÍÍ¥½¸¡Í•±˜¹‘‰Í}É½½Ğ°€‰Ñ…É•ĞµÉ…œˆ¤è4(€€€€€€€€€€€€€€€€€€€¥¹É•µ•¹Ñ…°¹…‘‘}½É}ÕÁ‘…Ñ•}É½½Ğ 4(€€€€€€€€€€€€€€€€€€€€€€€Í•½¹‘}¥¹ÁÕĞ°4(€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÉŒµˆˆ°4(€€€€€€€€€€€€€€€€€€€€€€€‰…Ñ¡}Í¥é•}™¥±•ÌôÄ°4(€€€€€€€€€€€€€€€€€€€€€€€‘½Õµ•¹Ñ}Ñ½­•¹}‰Õ‘•ĞõÍ•±˜¹Ñ½­•¹}‰Õ‘•Ğ°4(€€€€€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€€€€€¥‘}Í•ÑÌ€ôÍ•±˜¹}…ÑÕ…±}¥‘}Í•ÑÌ¡‘‰}É½½Ğ¤4(€€€€€€€™¥¹…±±äè4(€€€€€€€€€€€Í•±˜¹}±½Í•}…ÑÕ…±}±¥•¹ÑÌ¡±¥•¹ÑÌ¤4(€€€€€€€ÍÑ…Ñ”€ô©Í½¸¹±½…‘Ì ¡‘‰}É½½Ğ€¼€‰±½Ì½¥¹‘•á}ÍÑ…Ñ”¹©Í½¸ˆ¤¹É•…‘}Ñ•áĞ ¤¤4(€€€€€€€Í½ÕÉ•Ì€ôíÍÑÈ¡¥Ñ•´¹•Ğ ‰Í½ÕÉ•}¥ˆ¤¤™½È¥Ñ•´¥¸ÍÑ…Ñ•l‰™¥±•Ì‰t¹Ù…±Õ•Ì ¥ô4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡ì‰ÍÉŒµ„ˆ°€‰ÍÉŒµˆ‰ô°Í½ÕÉ•Ì¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑQÉÕ”¡…±°¡Ù…±Õ•Ì€ôô¥‘}Í•ÑÍlÁt™½ÈÙ…±Õ•Ì¥¸¥‘}Í•ÑÍlÄét¤¤4(€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…° È°±•¸¡¥‘}Í•ÑÍlÁt¤¤4(4(4)±…ÍÌÑ½µ¥¡•­Á½¥¹ÑQ•ÍÑÌ¡Õ¹¥ÑÑ•ÍĞ¹Q•ÍÑ…Í”¤è4(€€€‘•˜Ñ•ÍÑ}É•Á±…•}™…¥±ÕÉ•}ÁÉ•Í•ÉÙ•Í}½µÁ±•Ñ•}½±‘}©Í½¹}…¹‘}É•µ½Ù•Í}Ñ•µÀ¡Í•±˜¤€´ø9½¹”è4(€€€€€€€İ¥Ñ Ñ•µÁ™¥±”¹Q•µÁ½É…Éå¥É•Ñ½Éä ¤…ÌÑ•µÁ½É…Éäè4(€€€€€€€€€€€Á…Ñ €ôA…Ñ ¡Ñ•µÁ½É…Éä¤€¼€‰ÁÉ½É•ÍÌ¹©Í½¸ˆ4(€€€€€€€€€€€…Ñ½µ¥}İÉ¥Ñ•}©Í½¸¡Á…Ñ °ì‰•¹•É…Ñ¥½¸ˆè€‰½±‰ô¤4(€€€€€€€€€€€İ¥Ñ µ½¬¹Á…Ñ  4(€€€€€€€€€€€€€€€€‰Í½™Ñİ…É•}É…}Ñ½½°¹…Ñ½µ¥}¥¼¹½Ì¹É•Á±…”ˆ°4(€€€€€€€€€€€€€€€Í¥‘•}•™™•Ğõ=MÉÉ½È ‰¥¹©•Ñ•É•Á±…”™…¥±ÕÉ”ˆ¤°4(€€€€€€€€€€€€¤°Í•±˜¹…ÍÍ•ÉÑI…¥Í•ÍI••à¡=MÉÉ½È°€‰¥¹©•Ñ•ˆ¤è4(€€€€€€€€€€€€€€€…Ñ½µ¥}İÉ¥Ñ•}©Í½¸¡Á…Ñ °ì‰•¹•É…Ñ¥½¸ˆè€‰¹•Ü‰ô¤4(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡ì‰•¹•É…Ñ¥½¸ˆè€‰½±‰ô°©Í½¸¹±½…‘Ì¡Á…Ñ ¹É•…‘}Ñ•áĞ ¤¤¤4(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡mt°±¥ÍĞ¡Á…Ñ ¹Á…É•¹Ğ¹±½ˆ¡˜ˆ¹íÁ…Ñ ¹¹…µ•ô¸¨¹ÑµÀˆ¤¤¤4(€€€€€€€€€€€…Ñ½µ¥}İÉ¥Ñ•}©Í½¸¡Á…Ñ °ì‰•¹•É…Ñ¥½¸ˆè€‰¹•Ü‰ô¤4(€€€€€€€€€€€Í•±˜¹…ÍÍ•ÉÑÅÕ…°¡ì‰•¹•É…Ñ¥½¸ˆè€‰¹•Ü‰ô°©Í½¸¹±½…‘Ì¡Á…Ñ ¹É•…‘}Ñ•áĞ ¤¤¤4(4(4)¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè4(€€€Õ¹¥ÑÑ•ÍĞ¹µ…¥¸ ¤4(