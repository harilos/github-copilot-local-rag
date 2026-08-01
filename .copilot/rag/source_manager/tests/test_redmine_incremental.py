from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from source_manager import build_fetch_plan, execute_fetch_plan
from source_manager.errors import SourceManagerError
from source_manager.redmine_incremental import (
    _changed_issue_ids,
    _inventory,
    _local_issue_timestamp,
)


class RedmineIncrementalRefreshTests(unittest.TestCase):
    @staticmethod
    def _write_issue(
        path: Path,
        *,
        issue_id: int,
        updated_on: str,
        description: str = "",
        journals: list[dict[str, object]] | None = None,
    ) -> None:
        payload = {
            "description": description,
            "id": issue_id,
            "journals": list(journals or []),
            "updated_on": updated_on,
        }
        path.write_text(
            f"# Issue {issue_id}\n\n"
            "## Structured issue metadata\n\n"
            "```json\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n```\n",
            encoding="utf-8",
        )

    def test_existing_markdown_updated_on_skips_unchanged_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            issues = Path(temporary)
            self._write_issue(
                issues / "10.md",
                issue_id=10,
                updated_on="2026-07-29T01:02:03Z",
            )
            changed = _changed_issue_ids(
                [
                    (
                        10,
                        datetime(
                            2026, 7, 29, 1, 2, 3, tzinfo=timezone.utc
                        ),
                    )
                ],
                issues,
            )
            self.assertEqual([], changed)

    def test_newer_remote_issue_is_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            issues = Path(temporary)
            (issues / "11.md").write_text(
                '{"id":11,"updated_on":"2026-07-29T01:00:00Z"}\n',
                encoding="utf-8",
            )
            changed = _changed_issue_ids(
                [
                    (
                        11,
                        datetime(
                            2026, 7, 29, 1, 0, 1, tzinfo=timezone.utc
                        ),
                    )
                ],
                issues,
            )
            self.assertEqual([11], changed)

    def test_top_level_timestamp_wins_over_description_and_journals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "14.md"
            self._write_issue(
                path,
                issue_id=14,
                updated_on="2026-07-29T04:00:00+09:00",
                description='example "updated_on": "1999-01-01T00:00:00Z"',
                journals=[
                    {"id": 1, "updated_on": "2020-01-01T00:00:00Z"},
                    {"id": 2, "updated_on": "2021-01-01T00:00:00Z"},
                ],
            )
            self.assertEqual(
                datetime(2026, 7, 28, 19, 0, tzinfo=timezone.utc),
                _local_issue_timestamp(path),
            )

    def test_untrusted_local_metadata_fetches_only_that_issue(self) -> None:
        cases = {
            "id-mismatch": (
                '{"id":999,"updated_on":"2026-07-29T01:00:00Z"}',
                20,
            ),
            "malformed": ("{not-json", 21),
            "invalid-timestamp": (
                '{"id":22,"updated_on":"not-a-time"}',
                22,
            ),
        }
        for label, (contents, issue_id) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                issues = Path(temporary)
                (issues / f"{issue_id}.md").write_text(
                    contents,
                    encoding="utf-8",
                )
                changed = _changed_issue_ids(
                    [
                        (
                            issue_id,
                            datetime(2026, 7, 29, tzinfo=timezone.utc),
                        )
                    ],
                    issues,
                )
                self.assertEqual([issue_id], changed)

    def test_missing_local_issue_is_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            issues = Path(temporary)
            changed = _changed_issue_ids(
                [
                    (
                        12,
                        datetime(
                            2026, 7, 29, 1, 0, 0, tzinfo=timezone.utc
                        ),
                    )
                ],
                issues,
            )
            self.assertEqual([12], changed)

    def test_legacy_markdown_without_structured_metadata_is_refetched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "13.md"
            path.write_text("# legacy issue\n", encoding="utf-8")
            self.assertIsNone(_local_issue_timestamp(path))
            self.assertEqual(
                [13],
                _changed_issue_ids(
                    [
                        (
                            13,
                            datetime(
                                2026,
                                7,
                                29,
                                1,
                                59,
                                59,
                                tzinfo=timezone.utc,
                            ),
                        )
                    ],
                    path.parent,
                ),
            )

    def test_duplicate_inventory_ids_fail_closed_before_details(self) -> None:
        payload = {
            "issues": [
                {"id": 1, "updated_on": "2026-07-29T01:00:00Z"},
                {"id": 1, "updated_on": "2026-07-29T01:00:00Z"},
            ],
            "total_count": 2,
        }
        execution = SimpleNamespace(
            _get_with_retry=lambda *_args, **_kwargs: (
                200,
                json.dumps(payload).encode("utf-8"),
            ),
            _emit_http_progress=lambda *_args, **_kwargs: None,
        )
        with self.assertRaisesRegex(
            SourceManagerError,
            "redmine_inventory_changed",
        ):
            _inventory(
                settings={
                    "project_url": "http://localhost:3000/projects/project",
                    "api_key_env": "REDMINE_TEST_KEY",
                },
                getter=object(),
                environment={"REDMINE_TEST_KEY": "[REDACTED]"},
                updated_on_cutoff=None,
                execution=execution,
                progress_callback=None,
            )

    def test_four_hundred_issue_fixture_is_stable_after_first_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="redmine 日本語 ") as temporary:
            root = Path(temporary)
            source_key = "src_redmine-0123456789ab"
            work_path = (
                f"sources/{source_key}/work/ingest/{source_key}"
            )
            work = root / work_path
            work.mkdir(parents=True)
            plan = build_fetch_plan(
                source_key=source_key,
                provider="redmine",
                settings={
                    "project_url": "http://localhost:3000/projects/project",
                    "updated_within_days": None,
                    "api_key_env": "REDMINE_TEST_KEY",
                },
                logical_root=work_path,
                work_path=work_path,
            ).to_dict()
            updated = {
                issue_id: "2026-07-29T01:00:00Z"
                for issue_id in range(1, 401)
            }
            counts = {"detail": 0, "batch": 0}

            def getter(url, headers, _timeout):
                self.assertEqual("[REDACTED]", headers["X-Redmine-API-Key"])
                parsed = urlsplit(url)
                tail = parsed.path.rsplit("/", 1)[-1]
                if tail != "issues.json":
                    counts["detail"] += 1
                    issue_id = int(tail.removesuffix(".json"))
                    journals = (
                        []
                        if issue_id <= 100
                        else [
                            {
                                "id": issue_id,
                                "updated_on": "2020-01-01T00:00:00Z",
                            }
                        ]
                    )
                    return 200, json.dumps(
                        {
                            "issue": {
                                "description": "fixture",
                                "id": issue_id,
                                "journals": journals,
                                "subject": f"Issue {issue_id}",
                                "updated_on": updated[issue_id],
                            }
                        }
                    ).encode("utf-8")
                query = parse_qs(parsed.query)
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
                page_ids = list(range(1, 401))[offset : offset + limit]
                return 200, json.dumps(
                    {
                        "issues": [
                            {"id": issue_id, "updated_on": updated[issue_id]}
                            for issue_id in page_ids
                        ],
                        "total_count": 400,
                    }
                ).encode("utf-8")

            def batch(*_args):
                counts["batch"] += 1

            def run() -> dict[str, object]:
                return execute_fetch_plan(
                    plan,
                    work,
                    {"started_at": "2026-07-29T01:00:00Z"},
                    http_get=getter,
                    environment={"REDMINE_TEST_KEY": "[REDACTED]"},
                    batch_callback=batch,
                )

            first = run()
            self.assertEqual(400, first["documents"])
            self.assertEqual(400, counts["detail"])
            self.assertEqual(80, counts["batch"])
            self.assertEqual(400, len(list((work / "issues").glob("*.md"))))

            second = run()
            self.assertEqual(400, second["inventory_documents"])
            self.assertEqual(400, second["unchanged_documents"])
            self.assertEqual(400, counts["detail"])
            self.assertEqual(80, counts["batch"])

            third = run()
            self.assertEqual(400, third["inventory_documents"])
            self.assertEqual(400, counts["detail"])
            self.assertEqual(80, counts["batch"])

            updated[301] = "2026-07-29T01:00:01Z"
            changed = run()
            self.assertEqual(1, changed["fetched_this_run"])
            self.assertEqual(401, counts["detail"])
            self.assertEqual(81, counts["batch"])
            run()
            self.assertEqual(401, counts["detail"])
            self.assertEqual(81, counts["batch"])

    def test_localhost_http_refresh_keeps_credentials_out_of_output(self) -> None:
        state = {
            "details": 0,
            "headers": [],
            "updated": {
                1: "2026-07-29T01:00:00Z",
                2: "2026-07-29T01:00:00Z",
            },
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                state["headers"].append(
                    self.headers.get("X-Redmine-API-Key")
                )
                parsed = urlsplit(self.path)
                tail = parsed.path.rsplit("/", 1)[-1]
                if tail == "issues.json":
                    payload = {
                        "issues": [
                            {"id": issue_id, "updated_on": updated_on}
                            for issue_id, updated_on in state["updated"].items()
                        ],
                        "total_count": 2,
                    }
                else:
                    state["details"] += 1
                    issue_id = int(tail.removesuffix(".json"))
                    payload = {
                        "issue": {
                            "description": "fixture",
                            "id": issue_id,
                            "journals": [],
                            "subject": f"Issue {issue_id}",
                            "updated_on": state["updated"][issue_id],
                        }
                    }
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                source_key = "src_redmine-http-0123456789ab"
                work_path = (
                    f"sources/{source_key}/work/ingest/{source_key}"
                )
                work = Path(temporary) / work_path
                work.mkdir(parents=True)
                project_url = (
                    f"http://127.0.0.1:{server.server_port}/projects/project"
                )
                plan = build_fetch_plan(
                    source_key=source_key,
                    provider="redmine",
                    settings={
                        "project_url": project_url,
                        "updated_within_days": None,
                        "api_key_env": "REDMINE_TEST_KEY",
                    },
                    logical_root=work_path,
                    work_path=work_path,
                ).to_dict()
                arguments = {
                    "plan": plan,
                    "work_directory": work,
                    "state": {"started_at": "2026-07-29T01:00:00Z"},
                    "environment": {"REDMINE_TEST_KEY": "[REDACTED]"},
                }
                execute_fetch_plan(**arguments)
                self.assertEqual(2, state["details"])
                execute_fetch_plan(**arguments)
                self.assertEqual(2, state["details"])
                self.assertTrue(state["headers"])
                self.assertEqual(
                    {"[REDACTED]"},
                    set(state["headers"]),
                )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
