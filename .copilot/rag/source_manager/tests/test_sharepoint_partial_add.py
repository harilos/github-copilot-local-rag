from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from source_manager import SourceManagerError, SourceStore
from source_manager import execution, runner
from source_manager.diagnostics import exception_diagnostic
from source_manager.subprocess_stream import RESULT_FRAME


def _synthetic_sharepoint_root() -> Path:
    return Path("C:" + "\\Users") / "Person Name" / "SharePoint" / "Shared Documents"


def _synthetic_runtime_root() -> Path:
    return Path("C:" + "\\Users") / "Runtime User" / "Local RAG"


def _framed(value: dict) -> str:
    return RESULT_FRAME + json.dumps(value, ensure_ascii=False)


def _summary(source_id: str, *, status: str = "partial") -> dict:
    if status == "partial":
        return {
            "operation": "add",
            "source_id": source_id,
            "file_count": 2,
            "indexed_files": 1,
            "skipped_files": 0,
            "error_files": 1,
            "input_error_files": 1,
            "extract_error_files": 0,
            "upserted_records": 1,
            "deleted_records": 0,
            "result_status": "partial",
            "warning_ja": "1件を読み取れませんでした。",
            "error_details": [
                {
                    "path": "Shared Documents/locked.docx",
                    "stage": "hash-read",
                    "error_type": "PermissionError",
                    "errno": 13,
                    "winerror": 32,
                    "retryable": True,
                }
            ],
        }
    return {
        "operation": "add",
        "source_id": source_id,
        "file_count": 2,
        "indexed_files": 0,
        "skipped_files": 0,
        "error_files": 2,
        "input_error_files": 2,
        "extract_error_files": 0,
        "upserted_records": 0,
        "deleted_records": 0,
        "result_status": "failure",
        "error_details": [
            {
                "path": "Shared Documents/locked.docx",
                "stage": "hash-read",
                "error_type": "PermissionError",
                "errno": 13,
                "retryable": True,
            }
        ],
    }


class SharePointPartialAddRunnerTests(unittest.TestCase):
    def test_execute_add_accepts_typed_partial_and_adds_privacy_flag(self) -> None:
        observed: list[str] = []

        def command(arguments: list[str]) -> SimpleNamespace:
            observed.extend(arguments)
            source_id = arguments[arguments.index("--source-id") + 1]
            return SimpleNamespace(
                returncode=0,
                stdout=_framed(_summary(source_id)),
                stderr="",
            )

        result = runner._execute_add(
            db_root=Path("fixture-rag"),
            source={
                "local_source_key": "src_sharepoint-0123456789ab",
                "source_type": "sharepoint",
            },
            work=_synthetic_sharepoint_root(),
            python_executable=Path("python.exe"),
            rag_root=Path("rag"),
            command_runner=command,
            progress_callback=None,
        )
        self.assertEqual("partial", result["status"])
        self.assertIn("--privacy-safe-root", observed)

    def test_all_files_unreadable_is_failure_without_absolute_process_data(self) -> None:
        absolute = _synthetic_sharepoint_root()

        def command(arguments: list[str]) -> SimpleNamespace:
            source_id = arguments[arguments.index("--source-id") + 1]
            return SimpleNamespace(
                returncode=0,
                stdout=_framed(_summary(source_id, status="failure")),
                stderr="",
            )

        with self.assertRaises(SourceManagerError) as captured:
            runner._execute_add(
                db_root=Path("fixture-rag"),
                source={
                    "local_source_key": "src_sharepoint-0123456789ab",
                    "source_type": "sharepoint",
                },
                work=absolute,
                python_executable=Path("python.exe"),
                rag_root=Path("rag"),
                command_runner=command,
                progress_callback=None,
            )
        self.assertFalse(hasattr(captured.exception, "process_diagnostic"))
        persisted = json.dumps(
            captured.exception.diagnostic,
            ensure_ascii=False,
        )
        self.assertNotIn(str(absolute), persisted)
        self.assertNotIn("Person Name", persisted)

    def test_malformed_partial_without_read_error_is_rejected(self) -> None:
        malformed = {
            **_summary("src_sharepoint-0123456789ab"),
            "error_files": 0,
            "input_error_files": 0,
            "error_details": [],
        }
        completed = SimpleNamespace(
            returncode=0,
            stdout=_framed(malformed),
            stderr="",
        )
        with self.assertRaises(SourceManagerError):
            runner._execute_add(
                db_root=Path("fixture-rag"),
                source={
                    "local_source_key": "src_sharepoint-0123456789ab",
                    "source_type": "sharepoint",
                },
                work=Path("Shared Documents"),
                python_executable=Path("python.exe"),
                rag_root=Path("rag"),
                command_runner=lambda _arguments: completed,
                progress_callback=None,
            )

    def test_unframed_add_summary_is_rejected(self) -> None:
        source_id = "src_sharepoint-0123456789ab"
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_summary(source_id), ensure_ascii=False),
            stderr="",
        )
        with self.assertRaisesRegex(
            SourceManagerError,
            "trusted JSON result",
        ):
            runner._execute_add(
                db_root=Path("fixture-rag"),
                source={
                    "local_source_key": source_id,
                    "source_type": "sharepoint",
                },
                work=Path("Shared Documents"),
                python_executable=Path("python.exe"),
                rag_root=Path("rag"),
                command_runner=lambda _arguments: completed,
                progress_callback=None,
            )

    def test_multiple_frames_are_rejected_even_if_one_is_valid(self) -> None:
        source_id = "src_sharepoint-0123456789ab"
        completed = SimpleNamespace(
            returncode=0,
            stdout=_framed({"status": "noise"})
            + "\n"
            + _framed(_summary(source_id)),
            stderr="",
        )
        with self.assertRaisesRegex(SourceManagerError, "trusted JSON result"):
            runner._execute_add(
                db_root=Path("fixture-rag"),
                source={
                    "local_source_key": source_id,
                    "source_type": "sharepoint",
                },
                work=Path("Shared Documents"),
                python_executable=Path("python.exe"),
                rag_root=Path("rag"),
                command_runner=lambda _arguments: completed,
                progress_callback=None,
            )

    def test_absolute_error_detail_path_is_rejected(self) -> None:
        source_id = "src_sharepoint-0123456789ab"
        unsafe = _summary(source_id)
        unsafe["error_details"][0]["path"] = str(
            _synthetic_sharepoint_root().parent / "locked.docx"
        )
        completed = SimpleNamespace(
            returncode=0,
            stdout=_framed(unsafe),
            stderr="",
        )
        with self.assertRaisesRegex(SourceManagerError, "trusted JSON result"):
            runner._execute_add(
                db_root=Path("fixture-rag"),
                source={
                    "local_source_key": source_id,
                    "source_type": "sharepoint",
                },
                work=Path("Shared Documents"),
                python_executable=Path("python.exe"),
                rag_root=Path("rag"),
                command_runner=lambda _arguments: completed,
                progress_callback=None,
            )

    def test_nonzero_sharepoint_process_redacts_root_in_every_field(self) -> None:
        absolute = _synthetic_sharepoint_root()
        runtime = _synthetic_runtime_root()
        completed = SimpleNamespace(
            returncode=1,
            stdout=f"cannot read {absolute / 'locked.docx'}",
            stderr=(
                "PermissionError: "
                + str(absolute / "locked.docx").upper()
            ),
        )
        with self.assertRaises(SourceManagerError) as captured:
            runner._execute_add(
                db_root=Path("fixture-rag"),
                source={
                    "local_source_key": "src_sharepoint-0123456789ab",
                    "source_type": "sharepoint",
                },
                work=absolute,
                python_executable=runtime / "python.exe",
                rag_root=runtime / "rag",
                command_runner=lambda _arguments: completed,
                progress_callback=None,
            )
        serialized = json.dumps(
            captured.exception.process_diagnostic,
            ensure_ascii=False,
        ) + json.dumps(
            exception_diagnostic(
                captured.exception,
                operation="SharePoint test",
                stage="reflect.add",
            ),
            ensure_ascii=False,
        ) + str(captured.exception)
        self.assertNotIn(str(absolute), serialized)
        self.assertNotIn("Person Name", serialized)
        self.assertNotIn("PERSON NAME", serialized)
        self.assertNotIn("Runtime User", serialized)
        self.assertIn("<EXTERNAL_SOURCE_ROOT>", serialized)
        self.assertIn("<PYTHON_EXECUTABLE>", serialized)
        self.assertIn("<RAG_RUNTIME>", serialized)
        self.assertIn("Traceback suppressed", serialized)

    def test_sharepoint_process_invocation_failures_hide_all_arguments(self) -> None:
        absolute = _synthetic_sharepoint_root()
        runtime = _synthetic_runtime_root()

        def launch_error(arguments: list[str]) -> SimpleNamespace:
            error = OSError(2, "launch failed", str(absolute))
            error.winerror = 2
            raise error

        def timeout_error(arguments: list[str]) -> SimpleNamespace:
            raise subprocess.TimeoutExpired(
                cmd=arguments,
                timeout=30,
                output=str(absolute),
                stderr=str(runtime),
            )

        for command in (launch_error, timeout_error):
            with self.subTest(command=command.__name__):
                with self.assertRaises(SourceManagerError) as captured:
                    runner._execute_add(
                        db_root=Path("fixture-rag"),
                        source={
                            "local_source_key": "src_sharepoint-0123456789ab",
                            "source_type": "sharepoint",
                        },
                        work=absolute,
                        python_executable=runtime / "python.exe",
                        rag_root=runtime / "rag",
                        command_runner=command,
                        progress_callback=None,
                    )
                diagnostic = exception_diagnostic(
                    captured.exception,
                    operation="SharePoint test",
                    stage="reflect.add",
                )
                serialized = json.dumps(diagnostic, ensure_ascii=False)
                self.assertNotIn("Person Name", serialized)
                self.assertNotIn("Runtime User", serialized)
                self.assertEqual(1, len(diagnostic["exception_chain"]))
                self.assertIsNone(captured.exception.__cause__)
                self.assertTrue(captured.exception.__suppress_context__)

    def test_validation_oserror_hides_suppressed_exception_context(self) -> None:
        absolute = _synthetic_sharepoint_root()
        inner = OSError(5, "access denied", str(absolute))
        inner.winerror = 5
        with (
            mock.patch.object(
                execution,
                "_reject_unsafe_external_path_components",
            ),
            mock.patch.object(
                execution,
                "_validate_external_sharepoint_tree",
                side_effect=inner,
            ),
        ):
            with self.assertRaises(SourceManagerError) as captured:
                execution.validate_external_add_root(absolute)
        diagnostic = exception_diagnostic(
            captured.exception,
            operation="SharePoint test",
            stage="fetch.sharepoint.validate",
        )
        serialized = json.dumps(diagnostic, ensure_ascii=False)
        self.assertNotIn("Person Name", serialized)
        self.assertNotIn("access denied", serialized)
        self.assertEqual(1, len(diagnostic["exception_chain"]))
        self.assertEqual(
            getattr(inner, "errno", None),
            diagnostic["operation_detail"]["errno"],
        )
        self.assertEqual(5, diagnostic["operation_detail"]["winerror"])

    def test_reflection_rejects_vcs_metadata_before_add(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_root = root / "fixture-rag"
            db_root.mkdir()
            external = root / "SharePoint"
            external.mkdir()
            (external / ".git").mkdir()
            source_key = "src_sharepoint-0123456789ab"
            store = SourceStore(db_root)
            source = store.create_source(
                source_type="sharepoint",
                display_name="Fixture SharePoint",
                fetch={"root_env": "RAG_SHAREPOINT_ROOT"},
                local_source_key=source_key,
            )
            state_value = runner.new_run_state(store.plan(source.payload))
            state_value.update(
                {
                    "status": "fetched",
                    "phase": "reflect",
                    "fetched_count": 0,
                    "pending_count": 0,
                }
            )
            state = store.save_state(
                source_key,
                state_value,
                expected_revision=0,
                expected_etag=runner.MISSING_ETAG,
            )
            command = mock.Mock()
            with self.assertRaisesRegex(
                SourceManagerError,
                "VCS metadata",
            ):
                runner._reflect_and_sync(
                    store,
                    source,
                    state,
                    add_root=external,
                    command_runner=command,
                    python_executable=Path("python.exe"),
                    rag_root=root / "rag",
                    metadata_publisher=lambda *_args: None,
                    progress_callback=None,
                )
        command.assert_not_called()

    def test_streaming_progress_redacts_external_root_before_callback(self) -> None:
        absolute = _synthetic_sharepoint_root()
        observed: list[dict] = []

        def run(arguments, *, progress_callback, cwd, env):
            self.assertEqual("rag", cwd)
            self.assertEqual(Path.cwd().resolve(), Path(env["RAG_DBS_ROOT"]))
            self.assertNotIn("RAG_OUTPUT_ROOT", env)
            progress_callback(
                {
                    "event": "subprocess.log",
                    "message": (
                        "PermissionError: "
                        + str(absolute / "locked.docx").upper()
                    ),
                }
            )
            progress_callback(
                {
                    "event": "progress",
                    "payload": {
                        "current_file": str(
                            absolute / "locked.docx"
                        ).upper().replace("\\", "\\\\")
                    },
                }
            )
            source_id = arguments[arguments.index("--source-id") + 1]
            return SimpleNamespace(
                returncode=0,
                stdout=_framed(
                    {
                        **_summary(source_id),
                        "error_files": 0,
                        "input_error_files": 0,
                        "indexed_files": 2,
                        "result_status": "success",
                        "error_details": [],
                    },
                ),
                stderr="",
            )

        with mock.patch.object(runner, "run_streaming_process", side_effect=run):
            result = runner._execute_add(
                db_root=Path("fixture-rag"),
                source={
                    "local_source_key": "src_sharepoint-0123456789ab",
                    "source_type": "sharepoint",
                },
                work=absolute,
                python_executable=Path("python.exe"),
                rag_root=Path("rag"),
                command_runner=None,
                progress_callback=lambda event: observed.append(dict(event)),
            )
        serialized = json.dumps(observed, ensure_ascii=False)
        self.assertEqual("success", result["status"])
        self.assertNotIn("PERSON NAME", serialized)
        self.assertIn("<EXTERNAL_SOURCE_ROOT>", serialized)
        self.assertIn("詳細は秘匿", serialized)

    def test_partial_source_state_is_retryable_and_event_is_relative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_root = root / "fixture-rag"
            db_root.mkdir()
            external = root / "Person Name" / "SharePoint" / "Shared Documents"
            external.mkdir(parents=True)
            (external / "ok.docx").write_bytes(b"ok")
            (external / "locked.docx").write_bytes(b"locked")
            source_key = "src_sharepoint-0123456789ab"

            def command(arguments: list[str]) -> SimpleNamespace:
                source_id = arguments[arguments.index("--source-id") + 1]
                return SimpleNamespace(
                    returncode=0,
                    stdout=_framed(_summary(source_id)),
                    stderr="",
                )

            store = SourceStore(db_root)
            source = store.create_source(
                source_type="sharepoint",
                display_name="Fixture SharePoint",
                fetch={"root_env": "RAG_SHAREPOINT_ROOT"},
                local_source_key=source_key,
            )
            state_value = runner.new_run_state(store.plan(source.payload))
            state_value.update(
                {
                    "status": "fetched",
                    "phase": "reflect",
                    "fetched_count": 2,
                    "pending_count": 2,
                }
            )
            state = store.save_state(
                source_key,
                state_value,
                expected_revision=0,
                expected_etag=runner.MISSING_ETAG,
            )
            result = runner._reflect_and_sync(
                store,
                source,
                state,
                add_root=external,
                command_runner=command,
                python_executable=Path("python.exe"),
                rag_root=root / "rag",
                metadata_publisher=lambda *_args: None,
                progress_callback=None,
            )

            state = store.read_state(source_key).payload
            paths = store.paths(source_key)
            events_path = paths.absolute(db_root, paths.events_jsonl)
            persisted = events_path.read_text(encoding="utf-8") + json.dumps(
                state,
                ensure_ascii=False,
            )

        self.assertEqual("partial", result["status"])
        self.assertEqual("partial", state["status"])
        self.assertEqual("reflect", state["phase"])
        self.assertEqual(1, state["pending_count"])
        self.assertTrue(state["can_resume"])
        self.assertIn("add.partial", persisted)
        self.assertIn("Shared Documents/locked.docx", persisted)
        self.assertNotIn(str(external), persisted)
        self.assertNotIn("Person Name", persisted)

    def test_update_all_reports_partial_and_blocks_snapshot_marker(self) -> None:
        item = {
            "local_source_key": "src_sharepoint-0123456789ab",
            "source_type": "sharepoint",
            "display_name": "Fixture",
        }
        with (
            mock.patch.object(runner, "list_sources", return_value=[item]),
            mock.patch.object(runner, "_is_windows", return_value=True),
            mock.patch.object(
                runner,
                "update_source",
                return_value={**item, "status": "partial"},
            ),
        ):
            result = runner.update_all_sources(Path("fixture-rag"))
        self.assertEqual("partial", result["status"])
        self.assertEqual(0, result["completed_source_count"])
        self.assertFalse(result["snapshot_marker_eligible"])


if __name__ == "__main__":
    unittest.main()
