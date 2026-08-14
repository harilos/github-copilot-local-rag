from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
for root in (RAG_ROOT, TOOL_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from software_rag_tool import dbs


SPEC = importlib.util.spec_from_file_location(
    "integrity_explicit_public_search",
    RAG_ROOT / "query" / "search.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load public search implementation")
SEARCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SEARCH)


class ExplicitDatabasePublicSearchTests(unittest.TestCase):
    def _fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name) / "dbs"
        target = root / "target-rag"
        canary = root / "unreadable-canary-rag"
        target.mkdir(parents=True)
        canary.mkdir()
        sentinel = canary / "sentinel.bin"
        sentinel.write_bytes(b"unchanged")
        return temporary, root, sentinel, (sentinel.read_bytes(), sentinel.stat().st_mtime_ns)

    def _base_patches(self, stack: ExitStack, root: Path, argv: list[str]):
        stack.enter_context(mock.patch.object(SEARCH, "DBS_ROOT", root))
        stack.enter_context(mock.patch.object(SEARCH, "cleanup_result_spool"))
        stack.enter_context(mock.patch.object(SEARCH, "_configure_standard_streams"))
        stack.enter_context(mock.patch.object(SEARCH, "is_fixed_windows_runtime", return_value=True))
        stack.enter_context(mock.patch.object(Path, "exists", return_value=True))
        stack.enter_context(
            mock.patch.object(
                dbs,
                "list_db_names",
                side_effect=PermissionError("unreadable sibling boundary"),
            )
        )
        stack.enter_context(
            mock.patch.object(Path, "iterdir", side_effect=AssertionError("iterdir called"))
        )
        stack.enter_context(
            mock.patch.object(Path, "glob", side_effect=AssertionError("glob called"))
        )
        stack.enter_context(
            mock.patch.object(os, "scandir", side_effect=AssertionError("scandir called"))
        )
        stack.enter_context(mock.patch.object(sys, "argv", argv))

    def test_explicit_and_embedded_public_search_never_enumerates_siblings(
        self,
    ) -> None:
        selectors = (
            ("explicit", ["evidence", "--db", "target-rag"]),
            ("embedded", ["target-rag evidence"]),
        )
        state = {"generation": "fixture", "pid": 1, "transport": "tcp"}
        for no_daemon in (False, True):
            for selector, request in selectors:
                with self.subTest(selector=selector, no_daemon=no_daemon):
                    temporary, root, sentinel, before = self._fixture()
                    try:
                        argv = ["search.py", *request]
                        if no_daemon:
                            argv.append("--no-daemon")
                        argv.extend(["--format", "json"])
                        with ExitStack() as stack:
                            self._base_patches(stack, root, argv)
                            stack.enter_context(
                                mock.patch.object(SEARCH, "_read_state", return_value=None)
                            )
                            stack.enter_context(
                                mock.patch.object(
                                    SEARCH,
                                    "_inspect_daemon_state",
                                    return_value=(
                                        "READY", state, {"dense_ready": True}
                                    ),
                                )
                            )
                            query = stack.enter_context(
                                mock.patch.object(
                                    SEARCH,
                                    "_query_daemon",
                                    return_value={"status": "ok", "db": "target-rag"},
                                )
                            )
                            stack.enter_context(
                                mock.patch.object(SEARCH, "_print_search_payload")
                            )
                            sync = stack.enter_context(
                                mock.patch.object(SEARCH, "_run_sync_script")
                            )
                            if no_daemon:
                                SEARCH.main()
                                selected = sync.call_args.kwargs["db_name"]
                                query.assert_not_called()
                            else:
                                with self.assertRaises(SystemExit) as raised:
                                    SEARCH.main()
                                self.assertEqual(0, raised.exception.code)
                                selected = query.call_args.args[1]["db"]
                                sync.assert_not_called()
                        self.assertEqual("target-rag", selected)
                        self.assertEqual(
                            before,
                            (sentinel.read_bytes(), sentinel.stat().st_mtime_ns),
                        )
                    finally:
                        temporary.cleanup()

    def test_public_auto_and_unspecified_paths_keep_candidate_enumeration(
        self,
    ) -> None:
        cases = (
            ("auto-single", ["RAG evidence", "--auto"], ["target-rag"], None),
            (
                "auto-multiple",
                ["RAG evidence", "--auto"],
                ["alpha-rag", "beta-rag"],
                "needs_db",
            ),
            (
                "unspecified",
                ["ordinary question"],
                ["alpha-rag", "beta-rag"],
                "skipped",
            ),
        )
        for no_daemon in (False, True):
            for label, request, candidates, early_status in cases:
                with self.subTest(label=label, no_daemon=no_daemon):
                    temporary, root, sentinel, before = self._fixture()
                    try:
                        argv = ["search.py", *request]
                        if no_daemon:
                            argv.append("--no-daemon")
                        argv.extend(["--format", "json"])
                        output = io.StringIO()
                        state = {"generation": "fixture", "pid": 1, "transport": "tcp"}
                        with ExitStack() as stack, redirect_stdout(output):
                            stack.enter_context(mock.patch.object(SEARCH, "DBS_ROOT", root))
                            stack.enter_context(mock.patch.object(SEARCH, "cleanup_result_spool"))
                            stack.enter_context(mock.patch.object(SEARCH, "_configure_standard_streams"))
                            stack.enter_context(
                                mock.patch.object(
                                    SEARCH, "is_fixed_windows_runtime", return_value=True
                                )
                            )
                            stack.enter_context(mock.patch.object(Path, "exists", return_value=True))
                            listing = stack.enter_context(
                                mock.patch.object(
                                    dbs, "list_db_names", return_value=candidates
                                )
                            )
                            stack.enter_context(mock.patch.object(sys, "argv", argv))
                            stack.enter_context(
                                mock.patch.object(SEARCH, "_read_state", return_value=None)
                            )
                            inspect = stack.enter_context(
                                mock.patch.object(
                                    SEARCH,
                                    "_inspect_daemon_state",
                                    return_value=(
                                        "READY",
                                        state,
                                        {"dense_ready": True},
                                    ),
                                )
                            )
                            query = stack.enter_context(
                                mock.patch.object(
                                    SEARCH,
                                    "_query_daemon",
                                    return_value={"status": "ok", "db": "target-rag"},
                                )
                            )
                            stack.enter_context(
                                mock.patch.object(SEARCH, "_print_search_payload")
                            )
                            sync = stack.enter_context(
                                mock.patch.object(SEARCH, "_run_sync_script")
                            )
                            if early_status is None and not no_daemon:
                                with self.assertRaises(SystemExit) as raised:
                                    SEARCH.main()
                                self.assertEqual(0, raised.exception.code)
                            else:
                                SEARCH.main()
                        listing.assert_called_once_with(root)
                        if early_status is not None:
                            payload = json.loads(output.getvalue())
                            self.assertEqual(early_status, payload["status"])
                            self.assertEqual(candidates, payload["available_dbs"])
                            inspect.assert_not_called()
                            query.assert_not_called()
                            sync.assert_not_called()
                        elif no_daemon:
                            self.assertEqual(
                                "target-rag", sync.call_args.kwargs["db_name"]
                            )
                            query.assert_not_called()
                        else:
                            self.assertEqual("target-rag", query.call_args.args[1]["db"])
                            sync.assert_not_called()
                        self.assertEqual(
                            before,
                            (sentinel.read_bytes(), sentinel.stat().st_mtime_ns),
                        )
                    finally:
                        temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
