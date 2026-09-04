"""Focused Issue20 matrix; synthetic fixtures only, sanitized console evidence."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import platform
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
RAG = REPOSITORY / ".copilot" / "rag"
TOOL = RAG / "gen_db" / "software_rag_tool"
sys.path[:0] = [str(TOOL), str(RAG), str(TOOL / "tests"), str(RAG / "query")]
os.environ["PYTHONUTF8"] = "1"

GROUPS = {
    "core": [
        "test_atomic_io_windows_retry",
        "test_progress_observability",
        "test_windows_read_retry",
        "test_rebuild_scope_authority",
        "test_progress_actual_store_resilience",
    ],
    "regression": [
        "test_db_write_integrity",
        "test_index_integrity_hotfix",
        "test_ingestion_observability",
        "test_ingestion_layer_invariants",
        "test_catalog_write_productization_r2",
        "test_source_paths_contracts",
        "test_source_inventory_contracts",
        "test_add_progress_contracts",
    ],
    "source": [
        "source_manager.tests.test_observability_results",
        "source_manager.tests.test_add_progress_rendering",
        "source_manager.tests.test_confluence_runtime",
        "source_manager.tests.test_redmine_add_batching",
        "source_manager.tests.test_redmine_progress_cadence",
        "source_manager.tests.test_sharepoint_partial_add",
        "source_manager.tests.test_windows_store_retry",
        "source_manager.tests.test_document_filter",
        "source_manager.tests.test_progress",
    ],
    "scope": [
        "test_ingestion_scope_contracts",
        "test_sharepoint_read_error_hotfix",
    ],
}


def redacted(text):
    roots = [(str(REPOSITORY), "<REPOSITORY>"), (tempfile.gettempdir(), "<TEMP>")]
    roots += [(os.environ[key], "<USER>") for key in ("USERPROFILE", "HOME") if os.environ.get(key)]
    for root, label in roots:
        text = text.replace(root, label).replace(root.replace("\\", "/"), label)
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("group", choices=GROUPS)
    args = parser.parse_args()
    capture = io.StringIO()
    started = time.monotonic()
    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
        suite = unittest.defaultTestLoader.loadTestsFromNames(GROUPS[args.group])
        result = unittest.TextTestRunner(stream=capture, verbosity=1).run(suite)
    record = {
        "group": args.group,
        "python": platform.python_version(),
        "windows_build": platform.version(),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skips": [{"test": test.id(), "reason": redacted(reason)} for test, reason in result.skipped],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "status": "PASS" if result.wasSuccessful() else "FAIL",
    }
    # Preserve failure evidence without rendering workspace or user paths.
    if not result.wasSuccessful():
        record["diagnostics"] = redacted(capture.getvalue())
    print(json.dumps(record, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
