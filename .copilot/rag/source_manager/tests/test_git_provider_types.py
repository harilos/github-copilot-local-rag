from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from source_manager import providers
from source_manager.git_host_runtime import _ProgressProxy
from source_manager.git_host_urls import (
    GIT_SOURCE_TYPES,
    derive_repository_web_url,
    make_repository_link,
)
from source_manager.runner import register_source
from source_manager.store import SourceStore


class GitProviderTypeTests(unittest.TestCase):
    def test_requested_source_type_names_are_canonical(self) -> None:
        self.assertEqual(
            {
                "github",
                "gitlab",
                "azure-devops",
                "other-git",
            },
            set(GIT_SOURCE_TYPES),
        )

    def test_distinct_git_source_types_share_fetch_contract(self) -> None:
        urls = {
            "github": "https://github.com/example/project.git",
            "gitlab": "https://gitlab.example/group/project.git",
            "azure-devops": (
                "https://dev.azure.com/example/project/_git/repository"
            ),
            "other-git": "https://git.example/group/project.git",
        }
        for source_type, repository_url in urls.items():
            with self.subTest(source_type=source_type):
                normalized = providers.validate_provider_config(
                    source_type,
                    {
                        "repository_url": repository_url,
                        "include_paths": ["docs", "docs/api"],
                        "updated_within_days": "90",
                        "file_selection": "documents_only",
                    },
                )
                self.assertEqual(["docs"], normalized["include_paths"])
                self.assertEqual(90, normalized["updated_within_days"])
                self.assertEqual(
                    "documents_only",
                    normalized["file_selection"],
                )
                key = f"src_{source_type}-000000000000"
                work = f"sources/{key}/work/ingest/{key}"
                plan = providers.build_fetch_plan(
                    source_key=key,
                    provider=source_type,
                    settings=normalized,
                    logical_root=work,
                    work_path=work,
                )
                self.assertEqual(source_type, plan.provider)
                self.assertEqual("git_fetch", plan.steps[0].operation)

    def test_registration_persists_exact_source_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_root = Path(temporary)
            for source_type in sorted(GIT_SOURCE_TYPES):
                with self.subTest(source_type=source_type):
                    result = register_source(
                        db_root,
                        source_type=source_type,
                        display_name=f"{source_type} source",
                        fetch={
                            "repository_url": (
                                "https://git.example/group/project.git"
                            ),
                            "include_paths": [],
                            "updated_within_days": None,
                            "file_selection": "all_supported",
                        },
                        start=False,
                    )
                    stored = SourceStore(db_root).read_source(
                        result["local_source_key"]
                    )
                    self.assertEqual(
                        source_type,
                        stored.payload["source_type"],
                    )

    def test_progress_proxy_preserves_provider_and_callback_state(self) -> None:
        class Callback:
            def __init__(self) -> None:
                self.events: list[dict[str, object]] = []

            def __call__(self, event: dict[str, object]) -> None:
                self.events.append(event)

        callback = Callback()
        proxy = _ProgressProxy(callback, "azure-devops")
        proxy.preflight_confirmed = True
        proxy({"provider": "github", "phase": "github.fetch"})
        self.assertTrue(callback.preflight_confirmed)
        self.assertEqual(
            "azure-devops",
            callback.events[0]["provider"],
        )
        self.assertEqual(
            "azure-devops.fetch",
            callback.events[0]["phase"],
        )

    def test_provider_specific_link_contracts(self) -> None:
        self.assertEqual(
            "https://dev.azure.com/example/project/_git/repository",
            derive_repository_web_url(
                "azure-devops",
                "git@ssh.dev.azure.com:v3/example/project/repository",
            ),
        )
        self.assertIsNone(
            make_repository_link(
                "other-git",
                "https://git.example/group/project",
                ref="main",
            )
        )

    def test_source_metadata_accepts_exact_type_names(self) -> None:
        rag_root = Path(__file__).resolve().parents[2]
        tool_root = rag_root / "gen_db" / "software_rag_tool"
        if str(tool_root) not in sys.path:
            sys.path.insert(0, str(tool_root))
        from software_rag_tool import source_links

        azure = source_links.validate_source_links(
            {
                "schema_version": source_links.SCHEMA_VERSION,
                "revision": 1,
                "sources": [
                    {
                        "source_id": "azure-source",
                        "display_name": "Azure",
                        "source_type": "azure-devops",
                        "link": {
                            "enabled": True,
                            "strategy": "azure-devops-item",
                            "settings": {
                                "repository_url": (
                                    "https://dev.azure.com/example/"
                                    "project/_git/repository"
                                ),
                                "ref": "main",
                                "permalink_enabled": False,
                            },
                        },
                    }
                ],
            }
        )
        self.assertEqual(
            "azure-devops",
            azure["sources"][0]["source_type"],
        )

        other = source_links.validate_source_links(
            {
                "schema_version": source_links.SCHEMA_VERSION,
                "revision": 1,
                "sources": [
                    {
                        "source_id": "other-source",
                        "display_name": "Other",
                        "source_type": "other-git",
                    }
                ],
            }
        )
        self.assertEqual(
            "other-git",
            other["sources"][0]["source_type"],
        )


if __name__ == "__main__":
    unittest.main()
