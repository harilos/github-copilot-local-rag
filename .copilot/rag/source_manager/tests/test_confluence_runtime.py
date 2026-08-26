from __future__ import annotations

import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from source_manager import providers
from source_manager.errors import SourceManagerError
from source_manager.store import MISSING_ETAG, SourceStore


RAG_ROOT = Path(__file__).resolve().parents[2]
MANAGER_PATH = RAG_ROOT / "manage.py"
INVENTORY_ETAG = "a" * 64


def _connection_id() -> str:
    return str(uuid.UUID("d35a0a8b-4953-4f71-8ab4-62db403c7771"))


def _fetch(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "connection_id": _connection_id(),
        "space_key": "ENG",
        "scope": "space",
        "root_page_id": None,
        "attachments": "none",
    }
    value.update(overrides)
    return value


class ConfluenceProviderContractTests(unittest.TestCase):
    def test_in_memory_connection_adapter_uses_basic_for_cloud_and_bearer_for_dc(self) -> None:
        from source_manager.confluence_runtime import _credential_mapping

        cloud = _credential_mapping(
            {
                "connection_id": _connection_id(),
                "deployment": "cloud",
                "base_url": "https://docs.example.invalid",
                "token_kind": "scoped",
                "cloud_id": "da575a8b-cdce-4c9d-89fc-c49c288c22df",
                "api_root": (
                    "https://api.atlassian.com/ex/confluence/"
                    "da575a8b-cdce-4c9d-89fc-c49c288c22df/wiki/api/v2"
                ),
                "account_email": "reader@example.invalid",
                "token": "cloud-secret",
                "principal": "account-1",
            }
        )
        self.assertEqual("basic", cloud["auth_type"])
        self.assertEqual("cloud-secret", cloud["api_token"])
        self.assertEqual("", cloud["token"])

        data_center = _credential_mapping(
            {
                "connection_id": _connection_id(),
                "deployment": "data_center",
                "base_url": "https://kb.example.invalid/confluence",
                "token_kind": "pat",
                "api_root": "https://kb.example.invalid/confluence/rest/api",
                "account_email": "",
                "token": "dc-secret",
                "principal": "user-key-1",
            }
        )
        self.assertEqual("bearer", data_center["auth_type"])
        self.assertEqual("dc-secret", data_center["token"])
        self.assertEqual("/confluence", data_center["context_path"])

    def test_provider_normalizes_portable_non_secret_settings(self) -> None:
        normalized = providers.validate_provider_config("confluence", _fetch())

        self.assertEqual(_fetch(), normalized)
        rendered = repr(normalized).casefold()
        self.assertNotIn("token", rendered)
        self.assertNotIn("email", rendered)
        self.assertNotIn("password", rendered)

    def test_subtree_requires_strict_decimal_root_page_id(self) -> None:
        normalized = providers.validate_provider_config(
            "confluence",
            _fetch(scope="subtree", root_page_id="9007199254740991"),
        )
        self.assertEqual("9007199254740991", normalized["root_page_id"])

        for invalid in (None, "", "0", "-1", "1.0", "../1", " 1 "):
            with self.subTest(value=invalid):
                with self.assertRaises(SourceManagerError):
                    providers.validate_provider_config(
                        "confluence",
                        _fetch(scope="subtree", root_page_id=invalid),
                    )

    def test_space_scope_rejects_root_and_unknown_fields(self) -> None:
        with self.assertRaises(SourceManagerError):
            providers.validate_provider_config(
                "confluence",
                _fetch(root_page_id="123"),
            )
        with self.assertRaises(SourceManagerError):
            providers.validate_provider_config(
                "confluence",
                {**_fetch(), "api_token": "must-not-persist"},
            )

    def test_fetch_plan_uses_fixed_managed_work_path(self) -> None:
        plan = providers.build_fetch_plan(
            source_key="src-confluence-0123456789ab",
            provider="confluence",
            settings=_fetch(),
            logical_root=(
                "sources/src-confluence-0123456789ab/work/ingest/"
                "src-confluence-0123456789ab"
            ),
            work_path=(
                "sources/src-confluence-0123456789ab/work/ingest/"
                "src-confluence-0123456789ab"
            ),
        )

        self.assertEqual("confluence", plan.provider)
        self.assertEqual(1, len(plan.steps))
        self.assertEqual("confluence_fetch_pages", plan.steps[0].operation)
        self.assertEqual(_connection_id(), plan.steps[0].parameters["connection_id"])


class ConfluenceRunnerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-confluence-runtime-")
        self.db_root = Path(self.temporary.name) / "example-rag"
        self.db_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registration_creates_empty_exact_page_link_without_credentials(self) -> None:
        from source_manager.runner import register_source

        result = register_source(
            self.db_root,
            source_type="confluence",
            display_name="Engineering Confluence",
            fetch=_fetch(),
            start=False,
        )

        source = SourceStore(self.db_root).read_source(result["local_source_key"])
        self.assertEqual(_fetch(), source.payload["fetch"])
        self.assertEqual(
            {
                "enabled": True,
                "strategy": "confluence-page-map",
                "settings": {"page_urls": {}},
            },
            source.payload["pending_metadata"]["link"],
        )
        persisted = source.path.read_text(encoding="utf-8").casefold()
        self.assertNotIn("token", persisted)
        self.assertNotIn("email", persisted)

    def test_indexed_source_security_identity_is_immutable(self) -> None:
        from source_manager.runner import update_source_configuration

        store = SourceStore(self.db_root)
        created = store.create_source(
            source_type="confluence",
            display_name="Engineering Confluence",
            fetch=_fetch(),
        )
        indexed = store.confirm_source_id(
            created.payload["local_source_key"],
            "confluence-eng",
            expected_revision=created.revision,
            expected_etag=created.etag,
        )

        immutable_variants = (
            _fetch(connection_id=str(uuid.uuid4())),
            _fetch(space_key="OPS"),
            _fetch(scope="subtree", root_page_id="12"),
        )
        for value in immutable_variants:
            with self.subTest(value=value):
                with self.assertRaises(SourceManagerError):
                    update_source_configuration(
                        self.db_root,
                        indexed.payload["local_source_key"],
                        fetch=value,
                    )

        update_source_configuration(
            self.db_root,
            indexed.payload["local_source_key"],
            fetch=_fetch(attachments="metadata"),
        )
        reloaded = store.read_source(indexed.payload["local_source_key"])
        self.assertEqual("metadata", reloaded.payload["fetch"]["attachments"])

    def test_fetch_exact_page_map_becomes_pending_canonical_metadata(self) -> None:
        from source_manager import runner

        store = SourceStore(self.db_root)
        created = store.create_source(
            source_type="confluence",
            display_name="Engineering Confluence",
            fetch=_fetch(),
            link={
                "enabled": True,
                "strategy": "confluence-page-map",
                "settings": {"page_urls": {}},
            },
        )
        updated, pending = runner._apply_fetch_metadata(
            store,
            created,
            {
                "status": "ok",
                "documents": 2,
                "page_urls": {
                    "123": "https://docs.example.invalid/wiki/spaces/ENG/pages/123/One",
                    "456": "https://docs.example.invalid/wiki/spaces/ENG/pages/456/Two",
                },
            },
        )

        self.assertFalse(pending)
        link = updated.payload["pending_metadata"]["link"]
        self.assertEqual("confluence-page-map", link["strategy"])
        self.assertEqual(
            "https://docs.example.invalid/wiki/spaces/ENG/pages/456/Two",
            link["settings"]["page_urls"]["456"],
        )

    def test_real_update_dispatches_to_confluence_batch_runtime(self) -> None:
        from source_manager import confluence_runtime, runner

        store = SourceStore(self.db_root)
        created = store.create_source(
            source_type="confluence",
            display_name="Engineering Confluence",
            fetch=_fetch(),
            link={
                "enabled": True,
                "strategy": "confluence-page-map",
                "settings": {"page_urls": {}},
            },
        )
        with mock.patch(
            "source_manager.machine_connections.resolve_confluence_credentials",
            return_value=object(),
        ), mock.patch.object(
            confluence_runtime,
            "_update_confluence_source",
            return_value={"status": "updated"},
        ) as specialized:
            result = runner.update_source(
                self.db_root,
                created.payload["local_source_key"],
                python_executable=Path("python.exe"),
                rag_root=self.db_root,
                http_get=lambda *_args: None,
            )

        self.assertEqual("updated", result["status"])
        specialized.assert_called_once()

    def test_injected_executor_does_not_resolve_machine_credentials(self) -> None:
        from source_manager import runner

        store, source = self._new_confluence_source()
        with mock.patch(
            "source_manager.machine_connections.resolve_confluence_credentials"
        ) as resolve:
            result = runner.update_source(
                self.db_root,
                source.payload["local_source_key"],
                executor=lambda _plan, _work, _state: {
                    "status": "ok",
                    "documents": 0,
                    "page_urls": {},
                },
            )

        resolve.assert_not_called()
        self.assertEqual("fetched", result["status"])

    def test_metadata_only_resume_precedes_machine_credentials(self) -> None:
        from source_manager import runner

        store, source = self._new_confluence_source()
        confirmed = store.confirm_source_id(
            source.payload["local_source_key"],
            source.payload["local_source_key"],
            expected_revision=source.revision,
            expected_etag=source.etag,
        )
        self.assertTrue(confirmed.payload["metadata_sync_pending"])
        published: list[str] = []
        with mock.patch(
            "source_manager.machine_connections.resolve_confluence_credentials"
        ) as resolve:
            result = runner.update_source(
                self.db_root,
                source.payload["local_source_key"],
                rag_root=self.db_root,
                metadata_publisher=lambda *_args: published.append("ok"),
            )

        resolve.assert_not_called()
        self.assertEqual(["ok"], published)
        self.assertEqual("metadata_sync", result["resumed_operation"])

    def test_active_checkpoint_plan_change_fails_closed(self) -> None:
        from source_manager import confluence_runtime, runner

        store, source = self._new_confluence_source()
        value = runner.new_run_state(store.plan(source.payload))
        value["plan_etag"] = "0" * 64
        state = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=0,
            expected_etag=MISSING_ETAG,
        )
        with mock.patch.object(runner, "execute_fetch_plan") as execute:
            with self.assertRaisesRegex(
                SourceManagerError,
                "does not match the current fetch plan",
            ):
                confluence_runtime._update_confluence_source(
                    runner,
                    store,
                    source,
                    state,
                    python_executable=Path("python.exe"),
                    rag_root=self.db_root,
                    command_runner=None,
                    http_get=None,
                    environment=None,
                    metadata_publisher=lambda *_args: None,
                    clock=None,
                    progress_callback=None,
                )
        execute.assert_not_called()

    def test_twelve_pages_are_reflected_as_five_five_two_then_metadata_once(
        self,
    ) -> None:
        from source_manager import confluence_runtime, runner

        store, source = self._new_confluence_source()
        page_ids = [str(value) for value in range(1, 13)]
        add_counts: list[int] = []
        metadata_counts: list[int] = []

        def fake_fetch(_plan, work, _state, **kwargs):
            kwargs["inventory_callback"](page_ids)
            staged = store.read_state(
                source.payload["local_source_key"]
            ).payload
            self.assertFalse(staged["confluence_inventory_frozen"])
            self.assertNotIn("confluence_inventory_etag", staged)
            kwargs["inventory_etag_callback"](
                page_ids,
                INVENTORY_ETAG,
            )
            pages = Path(work) / "pages"
            pages.mkdir(parents=True, exist_ok=True)
            for completed, page_id in enumerate(page_ids, start=1):
                (pages / f"{page_id}.md").write_text(
                    f"page {page_id}\n",
                    encoding="utf-8",
                )
                kwargs["item_callback"](completed, page_id)
                if completed % 5 == 0 and completed < len(page_ids):
                    kwargs["batch_callback"](completed, page_id)
            # The provider's final callback is after stale deletion and local
            # work-tree validation; confluence.py owns that ordering.
            kwargs["batch_callback"](len(page_ids), page_ids[-1])
            return self._fetch_outcome(page_ids)

        def fake_add(**kwargs):
            count = len(list((Path(kwargs["work"]) / "pages").glob("*.md")))
            add_counts.append(count)
            return {
                "status": "success",
                "source_id": source.payload["local_source_key"],
                "summary": {},
            }

        def publish(_db_root, payload, _rag_root):
            metadata_counts.append(len(payload["pending_metadata"]["link"]["settings"]["page_urls"]))
            self.assertEqual([5, 10, 12], add_counts)

        with mock.patch.object(
            runner,
            "execute_fetch_plan",
            side_effect=fake_fetch,
        ), mock.patch.object(
            runner,
            "_execute_add",
            side_effect=fake_add,
        ), mock.patch.object(
            runner,
            "_is_initial_database_reflection",
            return_value=False,
        ):
            result = confluence_runtime._update_confluence_source(
                runner,
                store,
                source,
                store.read_state(source.payload["local_source_key"]),
                python_executable=Path("python.exe"),
                rag_root=self.db_root,
                command_runner=None,
                http_get=None,
                environment=None,
                metadata_publisher=publish,
                clock=None,
                progress_callback=None,
            )

        self.assertEqual("updated", result["status"])
        self.assertEqual([5, 10, 12], add_counts)
        self.assertEqual([12], metadata_counts)
        final = store.read_state(source.payload["local_source_key"]).payload
        self.assertEqual("complete", final["status"])
        self.assertEqual(12, final["fetched_count"])
        self.assertEqual(12, final["indexed_confirmed_count"])
        self.assertEqual(INVENTORY_ETAG, final["confluence_inventory_etag"])
        self.assertTrue(final["confluence_inventory_reconciled"])

    def test_second_add_interrupt_resumes_frozen_inventory_before_tail(
        self,
    ) -> None:
        from source_manager import confluence_runtime, runner

        store, source = self._new_confluence_source()
        page_ids = [str(value) for value in range(1, 13)]
        attempted_add_counts: list[int] = []
        metadata_calls: list[int] = []
        inventory_calls = 0
        detail_calls: list[str] = []
        fail_second_add = True

        def fake_fetch(_plan, work, _state, **kwargs):
            nonlocal inventory_calls
            stable = kwargs.get("stable_page_ids")
            if stable is None:
                inventory_calls += 1
                kwargs["inventory_callback"](page_ids)
                kwargs["inventory_etag_callback"](
                    page_ids,
                    INVENTORY_ETAG,
                )
                stable = page_ids
            else:
                self.assertEqual(page_ids, list(stable))
                self.assertEqual(
                    INVENTORY_ETAG,
                    kwargs.get("resume_inventory_etag"),
                )
            pages = Path(work) / "pages"
            pages.mkdir(parents=True, exist_ok=True)
            start = int(kwargs.get("resume_count") or 0)
            for index in range(start, len(page_ids)):
                page_id = page_ids[index]
                detail_calls.append(page_id)
                (pages / f"{page_id}.md").write_text(
                    f"page {page_id}\n",
                    encoding="utf-8",
                )
                completed = index + 1
                kwargs["item_callback"](completed, page_id)
                if completed % 5 == 0 and completed < len(page_ids):
                    kwargs["batch_callback"](completed, page_id)
            kwargs["batch_callback"](len(page_ids), page_ids[-1])
            return self._fetch_outcome(page_ids)

        def fake_add(**kwargs):
            nonlocal fail_second_add
            count = len(list((Path(kwargs["work"]) / "pages").glob("*.md")))
            attempted_add_counts.append(count)
            if fail_second_add and len(attempted_add_counts) == 2:
                raise RuntimeError("synthetic second ADD interruption")
            return {
                "status": "success",
                "source_id": source.payload["local_source_key"],
                "summary": {},
            }

        def publish(_db_root, payload, _rag_root):
            metadata_calls.append(len(payload["pending_metadata"]["link"]["settings"]["page_urls"]))

        patches = (
            mock.patch.object(
                runner,
                "execute_fetch_plan",
                side_effect=fake_fetch,
            ),
            mock.patch.object(
                runner,
                "_execute_add",
                side_effect=fake_add,
            ),
            mock.patch.object(
                runner,
                "_is_initial_database_reflection",
                return_value=False,
            ),
        )
        with patches[0], patches[1], patches[2]:
            with self.assertRaisesRegex(
                RuntimeError,
                "synthetic second ADD interruption",
            ):
                confluence_runtime._update_confluence_source(
                    runner,
                    store,
                    source,
                    store.read_state(source.payload["local_source_key"]),
                    python_executable=Path("python.exe"),
                    rag_root=self.db_root,
                    command_runner=None,
                    http_get=None,
                    environment=None,
                    metadata_publisher=publish,
                    clock=None,
                    progress_callback=None,
                )

        interrupted = store.read_state(source.payload["local_source_key"]).payload
        self.assertEqual("reflect", interrupted["phase"])
        self.assertEqual(10, interrupted["fetched_count"])
        self.assertEqual(5, interrupted["indexed_confirmed_count"])
        self.assertEqual(5, interrupted["pending_count"])
        self.assertEqual(page_ids, interrupted["confluence_page_ids"])
        self.assertEqual([], metadata_calls)

        fail_second_add = False
        source = store.read_source(source.payload["local_source_key"])
        with mock.patch.object(
            runner,
            "execute_fetch_plan",
            side_effect=fake_fetch,
        ), mock.patch.object(
            runner,
            "_execute_add",
            side_effect=fake_add,
        ), mock.patch.object(
            runner,
            "_is_initial_database_reflection",
            return_value=False,
        ):
            result = confluence_runtime._update_confluence_source(
                runner,
                store,
                source,
                store.read_state(source.payload["local_source_key"]),
                python_executable=Path("python.exe"),
                rag_root=self.db_root,
                command_runner=None,
                http_get=None,
                environment=None,
                metadata_publisher=publish,
                clock=None,
                progress_callback=None,
            )

        self.assertEqual("updated", result["status"])
        self.assertEqual([5, 10, 10, 12], attempted_add_counts)
        self.assertEqual(1, inventory_calls)
        self.assertEqual(page_ids, detail_calls)
        self.assertEqual([12], metadata_calls)

    def test_frozen_inventory_with_zero_progress_is_refetched(self) -> None:
        from source_manager import confluence_runtime, runner

        store, source = self._new_confluence_source()
        plan = store.plan(source.payload)
        value = runner.new_run_state(plan)
        value.update(
            {
                "status": "interrupted",
                "phase": "fetch",
                "confluence_page_ids": ["1", "2"],
                "confluence_inventory_etag": INVENTORY_ETAG,
                "confluence_inventory_frozen": True,
                "confluence_inventory_reconciled": False,
                "fetched_count": 0,
                "indexed_confirmed_count": 0,
                "pending_count": 0,
            }
        )
        state = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=0,
            expected_etag=MISSING_ETAG,
        )
        inventory_calls = 0

        def fake_fetch(_plan, work, _state, **kwargs):
            nonlocal inventory_calls
            self.assertIsNone(kwargs.get("stable_page_ids"))
            self.assertIsNone(kwargs.get("resume_inventory_etag"))
            self.assertEqual(0, kwargs.get("resume_count"))
            inventory_calls += 1
            page_ids = ["1", "2"]
            kwargs["inventory_callback"](page_ids)
            kwargs["inventory_etag_callback"](
                page_ids,
                INVENTORY_ETAG,
            )
            pages = Path(work) / "pages"
            pages.mkdir(parents=True, exist_ok=True)
            for completed, page_id in enumerate(page_ids, start=1):
                (pages / f"{page_id}.md").write_text(
                    page_id,
                    encoding="utf-8",
                )
                kwargs["item_callback"](completed, page_id)
            kwargs["batch_callback"](2, "2")
            return self._fetch_outcome(page_ids)

        with mock.patch.object(
            runner,
            "execute_fetch_plan",
            side_effect=fake_fetch,
        ), mock.patch.object(
            runner,
            "_execute_add",
            return_value={
                "status": "success",
                "source_id": source.payload["local_source_key"],
                "summary": {},
            },
        ):
            result = confluence_runtime._update_confluence_source(
                runner,
                store,
                source,
                state,
                python_executable=Path("python.exe"),
                rag_root=self.db_root,
                command_runner=None,
                http_get=None,
                environment=None,
                metadata_publisher=lambda *_args: None,
                clock=None,
                progress_callback=None,
            )

        self.assertEqual("updated", result["status"])
        self.assertEqual(1, inventory_calls)

    def test_empty_final_add_interrupt_resumes_before_fresh_inventory(self) -> None:
        from source_manager import confluence_runtime, runner

        store, source = self._new_confluence_source()
        add_attempts: list[int] = []
        provider_calls = 0
        metadata_calls: list[str] = []

        def fake_fetch(_plan, _work, _state, **kwargs):
            nonlocal provider_calls
            provider_calls += 1
            self.assertIsNone(kwargs.get("stable_page_ids"))
            self.assertIsNone(kwargs.get("resume_inventory_etag"))
            self.assertEqual(0, kwargs.get("resume_count"))
            kwargs["inventory_callback"]([])
            kwargs["inventory_etag_callback"]([], INVENTORY_ETAG)
            kwargs["batch_callback"](0, None)
            return self._fetch_outcome([])

        def fake_add(**_kwargs):
            add_attempts.append(0)
            if len(add_attempts) == 1:
                raise RuntimeError("synthetic empty ADD interruption")
            return {
                "status": "success",
                "source_id": source.payload["local_source_key"],
                "summary": {},
            }

        with mock.patch.object(
            runner,
            "execute_fetch_plan",
            side_effect=fake_fetch,
        ), mock.patch.object(
            runner,
            "_execute_add",
            side_effect=fake_add,
        ), mock.patch.object(
            runner,
            "_is_initial_database_reflection",
            return_value=False,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "synthetic empty ADD interruption",
            ):
                confluence_runtime._update_confluence_source(
                    runner,
                    store,
                    source,
                    store.read_state(source.payload["local_source_key"]),
                    python_executable=Path("python.exe"),
                    rag_root=self.db_root,
                    command_runner=None,
                    http_get=None,
                    environment=None,
                    metadata_publisher=lambda *_args: metadata_calls.append("bad"),
                    clock=None,
                    progress_callback=None,
                )

        interrupted = store.read_state(source.payload["local_source_key"]).payload
        self.assertEqual("reflect", interrupted["phase"])
        self.assertEqual(0, interrupted["pending_count"])
        self.assertEqual([], interrupted["confluence_page_ids"])
        self.assertEqual(INVENTORY_ETAG, interrupted["confluence_inventory_etag"])
        source = store.read_source(source.payload["local_source_key"])

        with mock.patch.object(
            runner,
            "execute_fetch_plan",
            side_effect=fake_fetch,
        ), mock.patch.object(
            runner,
            "_execute_add",
            side_effect=fake_add,
        ):
            result = confluence_runtime._update_confluence_source(
                runner,
                store,
                source,
                store.read_state(source.payload["local_source_key"]),
                python_executable=Path("python.exe"),
                rag_root=self.db_root,
                command_runner=None,
                http_get=None,
                environment=None,
                metadata_publisher=lambda *_args: metadata_calls.append("ok"),
                clock=None,
                progress_callback=None,
            )

        self.assertEqual("updated", result["status"])
        self.assertEqual([0, 0], add_attempts)
        self.assertEqual(2, provider_calls)
        self.assertEqual(["ok"], metadata_calls)

    def _new_confluence_source(self):
        store = SourceStore(self.db_root)
        source = store.create_source(
            source_type="confluence",
            display_name="Engineering Confluence",
            fetch=_fetch(),
            link={
                "enabled": True,
                "strategy": "confluence-page-map",
                "settings": {"page_urls": {}},
            },
        )
        return store, source

    @staticmethod
    def _fetch_outcome(page_ids: list[str]) -> dict[str, object]:
        return {
            "status": "ok",
            "documents": len(page_ids),
            "stable_page_ids": list(page_ids),
            "inventory_etag": INVENTORY_ETAG,
            "page_urls": {
                page_id: (
                    "https://docs.example.invalid/wiki/spaces/ENG/pages/"
                    f"{page_id}/Page-{page_id}"
                )
                for page_id in page_ids
            },
        }


class ConfluenceManagerContractTests(unittest.TestCase):
    def _manager_module(self):
        name = f"local_rag_manage_confluence_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, MANAGER_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_runtime_menu_exposes_one_confluence_entry_and_routes_it(self) -> None:
        module = self._manager_module()
        with tempfile.TemporaryDirectory(prefix="rag-confluence-menu-") as temporary:
            root = Path(temporary) / "rag"
            db = root / "dbs" / "example-rag"
            runtime = root / "query" / ".venv" / "Scripts" / "python.exe"
            db.mkdir(parents=True)
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b"")
            output: list[str] = []
            manager = module.LocalRagManager(
                rag_root=root,
                dbs_root=root / "dbs",
                runtime_python=runtime,
                input_fn=lambda _prompt: "11",
                output_fn=output.append,
            )
            with mock.patch.object(
                manager,
                "_prompt_new_confluence_source",
                return_value=None,
            ) as form:
                manager._add_source_screen("example-rag")

            form.assert_called_once_with()
            rendered = "\n".join(output)
            self.assertEqual(1, rendered.count("Confluence"))


if __name__ == "__main__":
    unittest.main()
