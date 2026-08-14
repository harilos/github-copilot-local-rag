"""Disposable Redmine manager benchmark used by the PERF-007 acceptance."""

from __future__ import annotations

import argparse
import ctypes
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from source_manager import SourceStore, register_source, update_source


def _peak_rss_bytes() -> int:
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(
        handle,
        ctypes.byref(counters),
        counters.cb,
    ):
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)


def run(count: int) -> dict[str, object]:
    timings = {
        "inventory_seconds": 0.0,
        "detail_seconds": 0.0,
        "state_seconds": 0.0,
        "add_seconds": 0.0,
    }
    add_calls = 0
    detail_calls = 0
    original_save = SourceStore.save_state

    def timed_save(store, key, payload, **kwargs):
        started = time.perf_counter()
        try:
            return original_save(store, key, payload, **kwargs)
        finally:
            timings["state_seconds"] += time.perf_counter() - started

    def getter(url, _headers, _timeout):
        nonlocal detail_calls
        started = time.perf_counter()
        split = urlsplit(url)
        if split.path == "/issues.json":
            query = parse_qs(split.query)
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            ids = list(range(1, count + 1))[offset : offset + limit]
            payload = {
                "issues": [
                    {"id": value, "updated_on": "2026-08-14T00:00:00Z"}
                    for value in ids
                ],
                "total_count": count,
            }
            timings["inventory_seconds"] += time.perf_counter() - started
            return 200, json.dumps(payload).encode()
        detail_calls += 1
        issue_id = int(split.path.rsplit("/", 1)[-1].split(".")[0])
        payload = {
            "issue": {
                "id": issue_id,
                "subject": f"Issue {issue_id}",
                "description": "fixture",
                "updated_on": "2026-08-14T00:00:00Z",
                "journals": [],
            }
        }
        timings["detail_seconds"] += time.perf_counter() - started
        return 200, json.dumps(payload).encode()

    def add(arguments):
        nonlocal add_calls
        started = time.perf_counter()
        add_calls += 1
        source_id = arguments[arguments.index("--source-id") + 1]
        payload = {
            "operation": "add",
            "source_id": source_id,
            "file_count": count,
            "indexed_files": count,
            "skipped_files": 0,
            "error_files": 0,
            "input_error_files": 0,
            "extract_error_files": 0,
            "error_details": [],
            "upserted_records": count,
            "deleted_records": 0,
            "result_status": "success",
        }
        result = SimpleNamespace(
            returncode=0,
            stdout="@@LOCAL_RAG_RESULT_V1@@" + json.dumps(payload),
            stderr="",
        )
        timings["add_seconds"] += time.perf_counter() - started
        return result

    with tempfile.TemporaryDirectory(prefix="redmine-perf007-") as temporary:
        db_root = Path(temporary) / "rag"
        db_root.mkdir()
        started = time.perf_counter()
        with mock.patch.object(SourceStore, "save_state", new=timed_save):
            result = register_source(
                db_root,
                source_type="redmine",
                display_name="PERF-007 fixture",
                fetch={
                    "project_url": (
                        "https://issues.example.invalid/projects/fixture"
                    ),
                    "updated_within_days": None,
                    "api_key_env": "REDMINE_TEST_KEY",
                },
                start=True,
                python_executable=Path(temporary) / "python.exe",
                rag_root=Path(temporary) / "runtime",
                command_runner=add,
                http_get=getter,
                environment={"REDMINE_TEST_KEY": "fixture"},
                metadata_publisher=lambda *_args: None,
            )
            first_seconds = time.perf_counter() - started
            first_add_calls = add_calls
            source_key = str(result["local_source_key"])
            started = time.perf_counter()
            update_source(
                db_root,
                source_key,
                python_executable=Path(temporary) / "python.exe",
                rag_root=Path(temporary) / "runtime",
                command_runner=add,
                http_get=getter,
                environment={"REDMINE_TEST_KEY": "fixture"},
                metadata_publisher=lambda *_args: None,
            )
            no_change_seconds = time.perf_counter() - started
        issue_files = list(
            (db_root / "sources" / source_key / "work" / "ingest" / source_key / "issues")
            .glob("*.md")
        )
        issue_ids = [path.stem for path in issue_files]
        return {
            "count": count,
            "first_seconds": first_seconds,
            "no_change_seconds": no_change_seconds,
            "first_add_calls": first_add_calls,
            "total_add_calls_after_no_change": add_calls,
            "detail_calls": detail_calls,
            "missing": count - len(issue_files),
            "persistent_duplicates": len(issue_ids) - len(set(issue_ids)),
            "peak_rss_bytes": _peak_rss_bytes(),
            **timings,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("count", type=int, choices=(0, 5, 50, 51, 400))
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.count), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
