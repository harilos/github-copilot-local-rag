from __future__ import annotations

import types
import unittest
from pathlib import Path
from unittest import mock

from source_manager import providers
from source_manager.machine_connections import SHAREPOINT_ROOT_ENV
from source_manager.teams_source import (
    _prepare_external_root_resume,
    normalize_update_all_result,
    prompt_new_teams_source,
)


class TeamsSourceTests(unittest.TestCase):
    def test_provider_uses_sharepoint_root_contract_but_keeps_teams_identity(self) -> None:
        normalized = providers.validate_provider_config(
            "teams",
            {
                "root_env": SHAREPOINT_ROOT_ENV,
                "relative_path": "Contoso Team/General/Documents",
            },
        )
        self.assertEqual(SHAREPOINT_ROOT_ENV, normalized["root_env"])
        self.assertEqual(
            "Contoso Team/General/Documents",
            normalized["relative_path"],
        )
        plan = providers.build_fetch_plan(
            source_key="src_teams-0123456789ab",
            provider="teams",
            settings=normalized,
            logical_root=(
                "sources/src_teams-0123456789ab/work/ingest/"
                "src_teams-0123456789ab"
            ),
            work_path=(
                "sources/src_teams-0123456789ab/work/ingest/"
                "src_teams-0123456789ab"
            ),
        )
        self.assertEqual("teams", plan.provider)
        self.assertFalse(plan.requires_network)

    def test_source_metadata_accepts_teams_without_a_link(self) -> None:
        from software_rag_tool import source_links

        value = source_links.validate_source_links(
            {
                "schema_version": source_links.SCHEMA_VERSION,
                "revision": 1,
                "sources": [
                    {
                        "source_id": "teams-general",
                        "display_name": "General",
                        "source_type": "teams",
                    }
                ],
            },
            allow_unmatched_sources=True,
        )
        self.assertEqual("teams", value["sources"][0]["source_type"])
        self.assertNotIn("link", value["sources"][0])

    def test_prompt_creates_teams_source_without_url_configuration(self) -> None:
        class Manager:
            rag_root = Path("/tmp/rag")

            def __init__(self) -> None:
                self.prompts: list[str] = []

            def _prompt_preserving_value(
                self,
                label: str,
                *_args: object,
                **_kwargs: object,
            ) -> str:
                self.prompts.append(label)
                return "Team A/General" if "相対パス" in label else "Team A General"

            def _examples(self, _key: str) -> tuple[str, ...]:
                return ()

            def _print_warning(self, _value: str) -> None:
                raise AssertionError("unexpected warning")

            def _print_info(self, _value: str) -> None:
                pass

            def _source_connection_settings_screen(self, **_kwargs: object) -> bool:
                return True

        status = types.SimpleNamespace(configured=True)
        with (
            mock.patch("source_manager.teams_source.os.name", "nt"),
            mock.patch(
                "source_manager.teams_source.sharepoint_root_status",
                return_value=status,
            ),
        ):
            result = prompt_new_teams_source(Manager())
        assert result is not None
        self.assertEqual("teams", result["source_type"])
        self.assertEqual(SHAREPOINT_ROOT_ENV, result["fetch"]["root_env"])
        self.assertEqual("Team A/General", result["fetch"]["relative_path"])
        self.assertNotIn("link", result)

    def test_missing_root_opens_shared_connection_screen(self) -> None:
        class Manager:
            rag_root = Path("/tmp/rag")

            def __init__(self) -> None:
                self.required: list[str] = []

            def _print_warning(self, _value: str) -> None:
                raise AssertionError("unexpected warning")

            def _print_info(self, _value: str) -> None:
                pass

            def _source_connection_settings_screen(self, **kwargs: object) -> bool:
                self.required.append(str(kwargs.get("required") or ""))
                return False

            def _prompt_preserving_value(
                self,
                *_args: object,
                **_kwargs: object,
            ) -> str:
                raise AssertionError("prompt must not continue")

            def _examples(self, _key: str) -> tuple[str, ...]:
                return ()

        manager = Manager()
        with (
            mock.patch("source_manager.teams_source.os.name", "nt"),
            mock.patch(
                "source_manager.teams_source.sharepoint_root_status",
                return_value=types.SimpleNamespace(configured=False),
            ),
        ):
            self.assertIsNone(prompt_new_teams_source(manager))
        self.assertEqual(["teams"], manager.required)

    def test_reflect_resume_is_rebased_to_external_fetch(self) -> None:
        class Stored:
            def __init__(self, payload: dict[str, object], revision: int = 1) -> None:
                self.payload = payload
                self.revision = revision
                self.etag = "etag"

        class Store:
            def __init__(self) -> None:
                self.state = Stored(
                    {
                        "schema_version": "local-rag-source-state-v1",
                        "local_source_key": "src_teams-0123456789ab",
                        "status": "interrupted",
                        "phase": "reflect",
                        "pending_count": 4,
                        "can_resume": True,
                    }
                )
                self.saved: dict[str, object] | None = None

            def read_state(self, _key: str) -> Stored:
                return self.state

            def save_state(
                self,
                _key: str,
                payload: dict[str, object],
                **_kwargs: object,
            ) -> Stored:
                self.saved = dict(payload)
                return Stored(dict(payload), revision=2)

        store = Store()
        _prepare_external_root_resume(store, "src_teams-0123456789ab")
        assert store.saved is not None
        self.assertEqual("fetch", store.saved["phase"])
        self.assertEqual(4, store.saved["pending_count"])
        self.assertTrue(store.saved["can_resume"])

    def test_non_windows_update_all_reports_teams_as_blocking_skip(self) -> None:
        result = normalize_update_all_result(
            {
                "status": "partial",
                "results": [
                    {
                        "source_type": "teams",
                        "display_name": "Team A",
                        "status": "failed",
                        "error_type": "SourceManagerError",
                        "error": "Teams Source updates require Windows",
                    },
                    {
                        "source_type": "github",
                        "display_name": "Repo",
                        "status": "updated",
                    },
                ],
            }
        )
        teams = result["results"][0]
        self.assertEqual("skipped", teams["status"])
        self.assertEqual("teams_update_requires_windows", teams["skip_reason"])
        self.assertEqual("ok", result["status"])
        self.assertFalse(result["snapshot_marker_eligible"])


if __name__ == "__main__":
    unittest.main()
