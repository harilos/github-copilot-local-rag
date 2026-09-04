from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from source_manager import SourceManagerError
from source_manager import runner
from source_manager import confluence_runtime
from source_manager.store import StoredJson
from source_manager.subprocess_stream import RESULT_FRAME


RAG_ROOT = Path(__file__).resolve().parents[2]
SOURCE_KEY = "src_sharepoint-0123456789ab"


def _summary(**extra):
    return {
        "operation": "add",
        "source_id": SOURCE_KEY,
        "file_count": 1,
        "indexed_files": 1,
        "skipped_files": 0,
        "error_files": 0,
        "input_error_files": 0,
        "extract_error_files": 0,
        "upserted_records": 1,
        "deleted_records": 0,
        "error_details": [],
        "result_status": "success",
        **extra,
    }


def _observation(*sinks):
    return {
        "observability_degraded": bool(sinks),
        "observability_failed_sinks": list(sinks),
    }


def _execute(summary, *, provider="sharepoint", returncode=0):
    return runner._execute_add(
        db_root=Path("fixture-rag"),
        source={"local_source_key": SOURCE_KEY, "source_type": provider},
        work=Path("private-fixture-input"),
        python_executable=Path("python.exe"),
        rag_root=RAG_ROOT,
        command_runner=lambda _: SimpleNamespace(
            returncode=returncode,
            stdout=RESULT_FRAME + json.dumps(summary),
            stderr="",
        ),
        progress_callback=None,
    )


class ObservationResultValidationTests(unittest.TestCase):
    def test_privacy_safe_result_preserves_closed_observation_schema(self):
        result = _execute(_summary(
            **_observation("progress", "events"),
            observability_path="C:/private/observation.json",
        ))
        self.assertEqual("success", result["status"])
        self.assertEqual("success", result["summary"]["result_status"])
        self.assertTrue(result["observability_degraded"])
        self.assertEqual(["events", "progress"], result["observability_failed_sinks"])
        self.assertEqual(
            result["observability_failed_sinks"],
            result["summary"]["observability_failed_sinks"],
        )
        self.assertNotIn("C:/private", json.dumps(result))
        self.assertNotIn("observability_path", result["summary"])

    def test_ordinary_source_also_keeps_success_and_observation_separate(self):
        result = _execute(_summary(**_observation("events")), provider="other")
        self.assertEqual("success", result["status"])
        self.assertTrue(result["observability_degraded"])
        self.assertEqual(["events"], result["summary"]["observability_failed_sinks"])

    def test_absent_and_explicit_healthy_observation_are_compatible(self):
        result = _execute(_summary())
        self.assertNotIn("observability_degraded", result)
        result = _execute(_summary(**_observation()))
        self.assertIs(False, result["observability_degraded"])
        self.assertEqual([], result["observability_failed_sinks"])

    def test_untrusted_observation_fields_are_rejected_without_path_leak(self):
        invalid = (
            {"observability_degraded": True},
            {"observability_failed_sinks": ["progress"]},
            {"observability_degraded": 1, "observability_failed_sinks": ["progress"]},
            {"observability_degraded": "true", "observability_failed_sinks": ["progress"]},
            {"observability_degraded": True, "observability_failed_sinks": "progress"},
            _observation("C:/private/observation.json"),
            _observation("state"),
            _observation("events", "events"),
            _observation({"path": "C:/private/observation.json"}),
            {"observability_degraded": False, "observability_failed_sinks": ["progress"]},
            {"observability_degraded": True, "observability_failed_sinks": []},
        )
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(SourceManagerError) as error:
                _execute(_summary(**fields))
            self.assertNotIn("C:/private", str(error.exception))

    def test_degraded_observation_cannot_override_failed_database_or_process(self):
        failed = _summary(
            **_observation("progress"),
            indexed_files=0,
            upserted_records=0,
            error_files=1,
            input_error_files=1,
            result_status="failure",
            error_details=[{
                "path": "document.txt", "stage": "hash-read",
                "error_type": "PermissionError", "retryable": True,
            }],
        )
        with self.assertRaises(SourceManagerError):
            _execute(failed)
        with self.assertRaises(SourceManagerError):
            _execute(_summary(**_observation("progress")), returncode=1)


class _MemoryStore:
    """Only exercise result/state orchestration; never open a database."""

    db_root = Path("fixture-rag")

    def __init__(self, provider):
        self.source = StoredJson({
            "local_source_key": SOURCE_KEY,
            "source_id": SOURCE_KEY,
            "source_type": provider,
            "fetch": {"updated_within_days": None},
        }, 1, "source-etag", Path("source.json"))
        self.state = StoredJson({
            "status": "running", "phase": "fetch", "fetched_count": 0,
            "indexed_confirmed_count": 0, "pending_count": 0,
            "initial_database_reflection": False,
        }, 1, "state-etag", Path("state.json"))
        self.saved = []

    def save_state(self, key, payload, **kwargs):
        self.saved.append(copy.deepcopy(payload))
        self.state = StoredJson(
            copy.deepcopy(payload), self.state.revision + 1,
            "state-etag", Path("state.json"),
        )
        return self.state

    def read_source(self, key):
        return self.source

    def ensure_work_directory(self, key):
        return Path("fixture-work")

    def plan(self, source):
        return SimpleNamespace(plan_etag="plan", to_dict=lambda: {})


class SourceObservationPropagationTests(unittest.TestCase):
    def test_reflection_result_exposes_degradation_without_mutating_state_schema(self):
        store = _MemoryStore("sharepoint")
        summary = _summary(**_observation("progress"))
        with (
            mock.patch.object(runner, "_database_writer_session", return_value=nullcontext()),
            mock.patch("source_manager.source_preflight._prepare_file_source_preview", return_value=(store.state, Path("fixture-input"))),
            mock.patch.object(runner, "validate_external_add_root"),
            mock.patch.object(runner, "_record_initial_snapshot_candidate", return_value=(store.state, False)),
            mock.patch.object(runner, "_execute_add", return_value={
                "source_id": SOURCE_KEY, "status": "success", "summary": summary,
            }),
            mock.patch.object(runner, "confirm_add_success", return_value={"status": "success"}),
            mock.patch.object(runner, "_synchronize_metadata", return_value={"metadata_sync_pending": False}),
        ):
            result = runner._reflect_and_sync(
                store, store.source, store.state,
                add_root=Path("fixture-input"), python_executable=Path("python.exe"),
                rag_root=RAG_ROOT, command_runner=None, metadata_publisher=None,
                progress_callback=None,
            )
        self.assertEqual("success", result["status"])
        self.assertTrue(result["observability_degraded"])
        self.assertEqual(["progress"], result["observability_failed_sinks"])
        self.assertEqual("complete", store.state.payload["status"])
        self.assertFalse(store.state.payload["can_resume"])
        self.assertTrue(all("observability_degraded" not in value for value in store.saved))

    def test_provider_batches_union_degradation_and_do_not_latch_into_next_run(self):
        for provider, function, reflect_name in (
            ("redmine", runner._update_redmine_source, "_redmine_reflect_batch"),
            ("gitlab_issues", runner._update_gitlab_issues_source, "_gitlab_issues_reflect_batch"),
        ):
            for degraded in (True, False):
                with self.subTest(provider=provider, degraded=degraded):
                    store = _MemoryStore(provider)
                    summaries = iter((
                        _observation("progress") if degraded else {},
                        _observation("events") if degraded else {},
                        {},
                    ))

                    def reflect(_store, source, state, **kwargs):
                        return source, state, next(summaries)

                    def fetch(*args, **kwargs):
                        for count in range(1, 4):
                            kwargs["batch_callback"](count, count)
                        return {"documents": 3}

                    with ExitStack() as stack:
                        stack.enter_context(mock.patch.object(runner, reflect_name, side_effect=reflect))
                        stack.enter_context(mock.patch.object(runner, "execute_fetch_plan", side_effect=fetch))
                        stack.enter_context(mock.patch.object(runner, "_synchronize_metadata", return_value={"metadata_sync_pending": False}))
                        stack.enter_context(mock.patch.object(runner, "_source_dto", return_value={"status": "success"}))
                        result = function(
                            store, store.source, store.state,
                            python_executable=Path("python.exe"), rag_root=RAG_ROOT,
                            command_runner=None, http_get=None, environment=None,
                            metadata_publisher=None, clock=None, progress_callback=None,
                            force_full_materialization=False,
                        )
                    self.assertEqual("updated", result["status"])
                    self.assertEqual("complete", store.state.payload["status"])
                    if degraded:
                        self.assertTrue(result["observability_degraded"])
                        self.assertEqual(["events", "progress"], result["observability_failed_sinks"])
                    else:
                        self.assertNotIn("observability_degraded", result)
                    self.assertTrue(all("observability_degraded" not in value for value in store.saved))

    def test_confluence_batches_and_pending_resume_retain_observation_union(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                store = _MemoryStore("confluence")
                store.state.payload["plan_etag"] = "plan"
                page_ids = ["1", "2", "3"]
                etag = "a" * 64
                if resume:
                    store.state.payload.update({
                        "phase": "reflect", "fetched_count": 1, "pending_count": 1,
                        confluence_runtime._PAGE_IDS_STATE_KEY: page_ids,
                        confluence_runtime._INVENTORY_FROZEN_STATE_KEY: True,
                        confluence_runtime._INVENTORY_ETAG_STATE_KEY: etag,
                    })
                summaries = iter((
                    _observation("progress"), _observation("events"), {}, {},
                ))

                def reflect(_runner, _store, source, state, **kwargs):
                    reflected = copy.deepcopy(state.payload)
                    reflected.update({
                        "indexed_confirmed_count": reflected["fetched_count"],
                        "pending_count": 0,
                        confluence_runtime._INVENTORY_RECONCILED_STATE_KEY: kwargs["final_batch"],
                    })
                    return source, store.save_state(SOURCE_KEY, reflected), next(summaries)

                def fetch(*args, **kwargs):
                    kwargs["inventory_etag_callback"](page_ids, etag)
                    for count, page_id in enumerate(page_ids, 1):
                        kwargs["item_callback"](count, page_id)
                        kwargs["batch_callback"](count, page_id)
                    return {
                        "status": "ok", "documents": 3,
                        "stable_page_ids": page_ids, "inventory_etag": etag,
                    }

                with (
                    mock.patch.object(confluence_runtime, "_confluence_reflect_batch", side_effect=reflect),
                    mock.patch.object(runner, "execute_fetch_plan", side_effect=fetch),
                    mock.patch.object(runner, "_apply_fetch_metadata", return_value=(store.source, False)),
                    mock.patch.object(runner, "_synchronize_metadata", return_value={"metadata_sync_pending": False}),
                    mock.patch.object(runner, "_source_dto", return_value={"status": "success"}),
                ):
                    result = confluence_runtime._update_confluence_source(
                        runner, store, store.source, store.state,
                        python_executable=Path("python.exe"), rag_root=RAG_ROOT,
                        command_runner=None, http_get=None, environment=None,
                        metadata_publisher=None, clock=None, progress_callback=None,
                    )
                self.assertEqual("updated", result["status"])
                self.assertTrue(result["observability_degraded"])
                self.assertEqual(["events", "progress"], result["observability_failed_sinks"])
                self.assertEqual("complete", store.state.payload["status"])
                self.assertTrue(all("observability_degraded" not in value for value in store.saved))


class ManagerObservationWarningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "local_rag_manage_observability", RAG_ROOT / "manage.py"
        )
        cls.manage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.manage)

    def manager(self):
        outputs = []
        # Bypass configuration loading: these are display-only contracts.
        manager = self.manage.LocalRagManager.__new__(self.manage.LocalRagManager)
        manager.output = outputs.append
        manager.use_color = False
        return manager, outputs

    def test_successful_single_summary_has_fixed_warning_without_sink_path(self):
        manager, outputs = self.manager()
        manager._print_ingestion_diagnostics(_summary(
            **_observation("progress"), observability_path="C:/private/trace.json",
        ))
        self.assertEqual(1, len(outputs))
        self.assertIn("DBへの反映結果とは別の警告", outputs[0])
        self.assertNotIn("C:/private", outputs[0])

    def test_bulk_result_keeps_success_count_and_displays_degradation(self):
        manager, outputs = self.manager()
        result = {"results": [{
            "status": "updated", "display_name": "fixture",
            **_observation("progress", "events"),
        }]}
        manager._show_source_update_result(result)
        text = "\n".join(outputs)
        self.assertIn("成功: 1 Source", text)
        self.assertIn("失敗: 0 Source", text)
        self.assertEqual(1, text.count("DBへの反映結果とは別の警告"))

    def test_healthy_and_unknown_sinks_never_render_arbitrary_content(self):
        manager, outputs = self.manager()
        manager._print_ingestion_diagnostics(_summary())
        manager._print_ingestion_diagnostics(_summary(**_observation()))
        manager._print_ingestion_diagnostics(_summary(**_observation("C:/private/trace.json")))
        self.assertEqual([], outputs)


if __name__ == "__main__":
    unittest.main()
