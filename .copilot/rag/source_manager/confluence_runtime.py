from __future__ import annotations

import contextvars
import copy
import functools
import hashlib
import json
import re
import sys
import unicodedata
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError, exception_summary
from .store import SourceStore


_PROVIDER_MARKER = "_local_rag_confluence_provider_installed"
_EXECUTION_MARKER = "_local_rag_confluence_execution_installed"
_RUNNER_MARKER = "_local_rag_confluence_runner_installed"
_MANAGER_HOOK_MARKER = "_local_rag_confluence_manager_hook_installed"
_MANAGER_CLASS_MARKER = "_local_rag_confluence_manager_installed"
_MAX_PAGE_ID = 9_223_372_036_854_775_807
_PAGE_IDS_STATE_KEY = "confluence_page_ids"
_INVENTORY_ETAG_STATE_KEY = "confluence_inventory_etag"
_INVENTORY_FROZEN_STATE_KEY = "confluence_inventory_frozen"
_INVENTORY_RECONCILED_STATE_KEY = "confluence_inventory_reconciled"
_ACTIVE_CREDENTIALS: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "local_rag_confluence_credentials",
    default=None,
)


def install_confluence_runtime() -> None:
    """Install the Confluence Source without changing existing providers."""

    from . import execution, manager_connections, progress, providers, runner
    from . import store as store_module

    _install_provider_contract(providers, runner, store_module)
    _install_execution(execution, runner)
    _install_runner(runner, providers)
    progress._PROVIDER_LABELS["confluence"] = "Confluence"
    _install_manager_hook(manager_connections)


def _validate_fetch(settings: Mapping[str, Any]) -> dict[str, Any]:
    supplied = dict(settings)
    allowed = {
        "connection_id",
        "space_key",
        "scope",
        "root_page_id",
        "attachments",
    }
    if set(supplied) - allowed:
        raise SourceManagerError(
            "Confluence settings contain unsupported fields"
        )
    raw_connection_id = str(supplied.get("connection_id") or "")
    if raw_connection_id != raw_connection_id.strip():
        raise SourceManagerError("Confluence connection_id is invalid")
    try:
        parsed_connection_id = uuid.UUID(raw_connection_id)
        connection_id = str(parsed_connection_id)
    except (ValueError, AttributeError) as exc:
        raise SourceManagerError(
            "Confluence connection_id is invalid"
        ) from exc
    if connection_id != raw_connection_id or parsed_connection_id.version != 4:
        raise SourceManagerError("Confluence connection_id is invalid")
    space_key = unicodedata.normalize(
        "NFC", str(supplied.get("space_key") or "")
    )
    if (
        space_key != space_key.strip()
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", space_key)
    ):
        raise SourceManagerError("Confluence space_key is invalid")
    scope = str(supplied.get("scope") or "space").strip().lower()
    if scope not in {"space", "subtree"}:
        raise SourceManagerError("Confluence scope is invalid")
    raw_root = supplied.get("root_page_id")
    root_page_id: str | None = None
    if raw_root is not None:
        root_text = str(raw_root)
        if (
            root_text != root_text.strip()
            or not re.fullmatch(r"[1-9][0-9]*", root_text)
            or int(root_text) > _MAX_PAGE_ID
        ):
            raise SourceManagerError(
                "Confluence root_page_id must be a finite decimal page ID"
            )
        root_page_id = root_text
    if scope == "subtree" and root_page_id is None:
        raise SourceManagerError(
            "Confluence subtree scope requires root_page_id"
        )
    if scope == "space" and root_page_id is not None:
        raise SourceManagerError(
            "Confluence space scope cannot contain root_page_id"
        )
    attachments = str(
        supplied.get("attachments") or "none"
    ).strip().lower()
    if attachments not in {"none", "metadata"}:
        raise SourceManagerError("Confluence attachments mode is invalid")
    return {
        "connection_id": connection_id,
        "space_key": space_key,
        "scope": scope,
        "root_page_id": root_page_id,
        "attachments": attachments,
    }


def _install_provider_contract(providers: Any, runner: Any, store_module: Any) -> None:
    if bool(getattr(providers, _PROVIDER_MARKER, False)):
        return
    original_validate = providers.validate_provider_config
    original_build = providers.build_fetch_plan

    @functools.wraps(original_validate)
    def validate_provider_config(
        provider: str,
        settings: Mapping[str, Any],
    ) -> dict[str, Any]:
        kind = str(provider or "").strip().lower()
        if kind == "confluence":
            return _validate_fetch(settings)
        return original_validate(kind, settings)

    @functools.wraps(original_build)
    def build_fetch_plan(
        *,
        source_key: str,
        provider: str,
        settings: Mapping[str, Any],
        logical_root: str,
        work_path: str,
    ) -> Any:
        kind = str(provider or "").strip().lower()
        if kind != "confluence":
            return original_build(
                source_key=source_key,
                provider=provider,
                settings=settings,
                logical_root=logical_root,
                work_path=work_path,
            )
        normalized = validate_provider_config(kind, settings)
        normalized_root = providers.validate_relative_path(
            logical_root,
            field="logical_root",
            allow_empty=False,
        )
        normalized_work = providers.validate_relative_path(
            work_path,
            field="work_path",
            allow_empty=False,
        )
        if normalized_root != normalized_work:
            raise SourceManagerError(
                "logical_root and work_path must use the fixed Source work path"
            )
        step = providers.FetchStep(
            "pages",
            "confluence_fetch_pages",
            True,
            normalized_work,
            normalized,
            "confluence_visible_inventory_refresh",
        )
        body = {
            "schema_version": "local-rag.fetch-plan.v1",
            "source_key": str(source_key),
            "provider": kind,
            "logical_root": normalized_root,
            "work_path": normalized_work,
            "steps": [step.to_dict()],
        }
        providers.validate_persistable(body, field="fetch_plan")
        digest = hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return providers.FetchPlan(
            schema_version=body["schema_version"],
            source_key=str(source_key),
            provider=kind,
            logical_root=normalized_root,
            work_path=normalized_work,
            steps=(step,),
            plan_etag=digest,
        )

    providers.SUPPORTED_PROVIDERS = frozenset(
        set(providers.SUPPORTED_PROVIDERS) | {"confluence"}
    )
    providers.validate_provider_config = validate_provider_config
    providers.build_fetch_plan = build_fetch_plan
    runner.validate_provider_config = validate_provider_config
    store_module.validate_provider_config = validate_provider_config
    store_module.build_fetch_plan = build_fetch_plan
    setattr(providers, _PROVIDER_MARKER, True)


def _credential_mapping(value: Any) -> dict[str, Any]:
    def field(name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    deployment = str(field("deployment") or "").strip().lower()
    base_url = str(field("base_url") or "").strip()
    token_kind = str(field("token_kind") or "").strip().lower()
    account_email = str(field("account_email") or "")
    token = str(field("token") or "")
    split = urllib.parse.urlsplit(base_url)
    site_url = urllib.parse.urlunsplit(
        (split.scheme, split.netloc, "", "", "")
    )
    context_path = "/wiki" if deployment == "cloud" else split.path.rstrip("/")
    return {
        "connection_id": str(field("connection_id") or ""),
        "deployment": deployment,
        "base_url": base_url,
        "site_url": site_url,
        "context_path": context_path,
        "token_kind": token_kind,
        "cloud_scope": token_kind if deployment == "cloud" else None,
        "cloud_id": field("cloud_id"),
        "api_root": str(field("api_root") or ""),
        "auth_type": "basic" if deployment == "cloud" else "bearer",
        "account_email": account_email,
        "email": account_email,
        "username": account_email,
        "api_token": token if deployment == "cloud" else "",
        "token": token if deployment == "data_center" else "",
        "principal": str(field("principal") or ""),
    }


def _install_execution(execution: Any, runner: Any) -> None:
    if bool(getattr(execution, _EXECUTION_MARKER, False)):
        return
    original = execution.execute_fetch_plan

    @functools.wraps(original)
    def execute_fetch_plan(
        plan: Mapping[str, Any],
        work_directory: Path,
        state: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if str(plan.get("provider") or "").strip().lower() != "confluence":
            return original(plan, work_directory, state, **kwargs)
        credentials = _ACTIVE_CREDENTIALS.get()
        if credentials is None:
            raise SourceManagerError(
                "Confluence connection is not registered on this computer"
            )
        from .confluence import fetch_confluence

        work = Path(work_directory)
        if not work.is_dir() or work.is_symlink():
            raise SourceManagerError("work directory is unsafe")
        parameters = dict(
            dict((plan.get("steps") or [{}])[0]).get("parameters") or {}
        )
        getter = kwargs.get("http_get") or execution._http_get

        def request(
            url: str,
            headers: Mapping[str, str],
            timeout: float = 10.0,
        ) -> tuple[int, bytes, Mapping[str, str]]:
            response = getter(url, headers, timeout)
            if len(response) == 2:
                return int(response[0]), bytes(response[1]), {}
            return int(response[0]), bytes(response[1]), dict(response[2])

        return dict(
            fetch_confluence(
                parameters,
                work,
                credentials=_credential_mapping(credentials),
                http_get=request,
                inventory_callback=kwargs.get("inventory_callback"),
                inventory_etag_callback=kwargs.get(
                    "inventory_etag_callback"
                ),
                item_callback=kwargs.get("item_callback"),
                batch_callback=kwargs.get("batch_callback"),
                resume_count=int(kwargs.get("resume_count") or 0),
                stable_page_ids=kwargs.get("stable_page_ids"),
                resume_inventory_etag=kwargs.get(
                    "resume_inventory_etag"
                ),
                progress_callback=kwargs.get("progress_callback"),
            )
        )

    execution.execute_fetch_plan = execute_fetch_plan
    runner.execute_fetch_plan = execute_fetch_plan
    setattr(execution, _EXECUTION_MARKER, True)


def _empty_link() -> dict[str, Any]:
    return {
        "enabled": True,
        "strategy": "confluence-page-map",
        "settings": {"page_urls": {}},
    }


def _valid_inventory_etag(value: Any) -> bool:
    text = str(value or "")
    return text == text.strip().lower() and re.fullmatch(
        r"[0-9a-f]{64}", text
    ) is not None


def _install_runner(runner: Any, providers: Any) -> None:
    if bool(getattr(runner, _RUNNER_MARKER, False)):
        return
    original_register = runner.register_source
    original_update = runner.update_source
    original_update_configuration = runner.update_source_configuration
    original_apply_metadata = runner._apply_fetch_metadata
    original_reflect = runner._reflect_and_sync

    @functools.wraps(original_register)
    def register_source(
        db_root: Path,
        *,
        source_type: str,
        display_name: str,
        fetch: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if str(source_type or "").strip().lower() == "confluence":
            fetch = providers.validate_provider_config("confluence", fetch)
            kwargs["link"] = _empty_link()
        return original_register(
            db_root,
            source_type=source_type,
            display_name=display_name,
            fetch=fetch,
            **kwargs,
        )

    @functools.wraps(original_update)
    def update_source(
        db_root: Path,
        local_source_key: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        store = SourceStore(Path(db_root))
        loaded = store.read_source(local_source_key)
        if str(loaded.payload.get("source_type") or "").strip().lower() != "confluence":
            return original_update(db_root, local_source_key, **kwargs)
        existing_state = store.read_state(local_source_key)
        if (
            loaded.payload.get("source_id")
            and loaded.payload.get("metadata_sync_pending")
            and (
                not existing_state.payload
                or existing_state.payload.get("phase")
                in {"metadata", "complete"}
            )
        ):
            # Metadata publication uses only the already-persisted, non-secret
            # page map.  It must remain resumable even when the machine-local
            # Confluence registration is temporarily absent.
            return original_update(db_root, local_source_key, **kwargs)
        if (
            kwargs.get("executor") is not None
            or kwargs.get("python_executable") is None
            or kwargs.get("rag_root") is None
            or kwargs.get("runtime_input") is not None
        ):
            # An injected executor is an explicit in-process test/automation
            # boundary and does not require machine-local credentials.  The
            # generic runner also owns planning-only and invalid runtime-input
            # behavior.
            return original_update(db_root, local_source_key, **kwargs)
        rag_root = kwargs.get("rag_root")
        assert rag_root is not None
        from .machine_connections import resolve_confluence_credentials

        settings = providers.validate_provider_config(
            "confluence", loaded.payload.get("fetch") or {}
        )
        credentials = resolve_confluence_credentials(
            Path(rag_root), settings["connection_id"]
        )
        if credentials is None:
            raise SourceManagerError(
                "Confluence connection is not registered on this computer"
            )
        if (
            kwargs.get("executor") is None
            and kwargs.get("command_runner") is None
            and kwargs.get("http_get") is None
        ):
            route = runner.resolve_source_network_route(
                Path(rag_root),
                environment=kwargs.get("environment"),
                progress_callback=kwargs.get("progress_callback"),
            )
            kwargs["command_runner"] = route.command_runner
            kwargs["http_get"] = route.http_get
            kwargs["environment"] = route.environment
        token = _ACTIVE_CREDENTIALS.set(credentials)
        try:
            if (
                kwargs.get("executor") is None
                and kwargs.get("python_executable") is not None
                and kwargs.get("rag_root") is not None
                and kwargs.get("runtime_input") is None
            ):
                return _update_confluence_source(
                    runner,
                    store,
                    loaded,
                    existing_state,
                    python_executable=Path(kwargs["python_executable"]),
                    rag_root=Path(kwargs["rag_root"]),
                    command_runner=kwargs.get("command_runner"),
                    http_get=kwargs.get("http_get"),
                    environment=kwargs.get("environment"),
                    metadata_publisher=kwargs.get("metadata_publisher"),
                    clock=kwargs.get("clock"),
                    progress_callback=kwargs.get("progress_callback"),
                )
            return original_update(db_root, local_source_key, **kwargs)
        finally:
            _ACTIVE_CREDENTIALS.reset(token)
            credentials = None

    @functools.wraps(original_update_configuration)
    def update_source_configuration(
        db_root: Path,
        local_source_key: str,
        *,
        fetch: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        store = SourceStore(Path(db_root))
        source = store.read_source(local_source_key)
        if (
            source.payload.get("source_id")
            and str(source.payload.get("source_type") or "").strip().lower()
            == "confluence"
        ):
            current = providers.validate_provider_config(
                "confluence", source.payload.get("fetch") or {}
            )
            normalized = providers.validate_provider_config("confluence", fetch)
            immutable = (
                "connection_id",
                "space_key",
                "scope",
                "root_page_id",
            )
            if any(normalized[key] != current[key] for key in immutable):
                raise SourceManagerError(
                    "confluence_identity_is_immutable_add_new_source"
                )
        return original_update_configuration(
            db_root,
            local_source_key,
            fetch=fetch,
            **kwargs,
        )

    @functools.wraps(original_apply_metadata)
    def apply_fetch_metadata(
        store: Any,
        source: Any,
        outcome: Mapping[str, Any],
    ) -> tuple[Any, bool]:
        if str(source.payload.get("source_type") or "").strip().lower() != "confluence":
            return original_apply_metadata(store, source, outcome)
        raw_urls = outcome.get("page_urls")
        if not isinstance(raw_urls, Mapping):
            raise SourceManagerError(
                "Confluence fetch did not return exact page links"
            )
        page_urls = {
            str(page_id): str(url)
            for page_id, url in raw_urls.items()
        }
        payload = copy.deepcopy(source.payload)
        payload["pending_metadata"] = {
            "source_type": "confluence",
            "link": {
                "enabled": True,
                "strategy": "confluence-page-map",
                "settings": {"page_urls": page_urls},
            },
        }
        saved = store.save_source(
            payload,
            expected_revision=source.revision,
            expected_etag=source.etag,
        )
        return saved, False

    @functools.wraps(original_reflect)
    def reflect_and_sync(
        store: Any,
        source: Any,
        state: Any,
        *,
        add_root: Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if str(source.payload.get("source_type") or "").strip().lower() == "confluence":
            from .confluence import validate_confluence_work_tree

            validate_confluence_work_tree(
                Path(add_root),
                expected_documents=int(state.payload.get("fetched_count") or 0),
            )
        return original_reflect(
            store,
            source,
            state,
            add_root=add_root,
            **kwargs,
        )

    runner.register_source = register_source
    runner.update_source = update_source
    runner.update_source_configuration = update_source_configuration
    runner._apply_fetch_metadata = apply_fetch_metadata
    runner._reflect_and_sync = reflect_and_sync
    setattr(runner, _RUNNER_MARKER, True)
    package = sys.modules.get(__package__)
    if package is not None:
        package.register_source = register_source
        package.update_source = update_source
        package.update_source_configuration = update_source_configuration


def _update_confluence_source(
    runner: Any,
    store: SourceStore,
    source: Any,
    state: Any,
    *,
    python_executable: Path,
    rag_root: Path,
    command_runner: Any,
    http_get: Any,
    environment: Mapping[str, str] | None,
    metadata_publisher: Any,
    clock: Any,
    progress_callback: Any,
) -> dict[str, Any]:
    """Fetch and reflect one frozen Confluence inventory in batches of five.

    ``fetch_confluence`` freezes the complete visible inventory before it
    invokes any item callback.  Its final batch callback is required to run
    only after stale-page deletion and work-tree validation; this runtime
    therefore publishes canonical metadata only after that callback succeeds.
    """

    if (
        source.payload.get("source_id")
        and source.payload.get("metadata_sync_pending")
        and (
            not state.payload
            or state.payload.get("phase") in {"metadata", "complete"}
        )
    ):
        return runner._resume_metadata_sync(
            store,
            source,
            rag_root=rag_root,
            metadata_publisher=metadata_publisher,
        )

    plan = store.plan(source.payload)
    if not state.payload or state.payload.get("status") == "complete":
        initial = runner.new_run_state(plan)
        initial[_INVENTORY_FROZEN_STATE_KEY] = False
        initial[_INVENTORY_RECONCILED_STATE_KEY] = False
        with runner._database_writer_session(
            store.db_root,
            stage="reflect.preflight",
        ):
            initial["initial_database_reflection"] = (
                runner._is_initial_database_reflection(store, source)
            )
        current_state = store.save_state(
            source.payload["local_source_key"],
            initial,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )
    else:
        if str(state.payload.get("plan_etag") or "") != plan.plan_etag:
            raise SourceManagerError(
                "Confluence active checkpoint does not match the current fetch plan",
                stage="fetch.confluence",
            )
        resumed = copy.deepcopy(state.payload)
        current_state = store.save_state(
            source.payload["local_source_key"],
            resumed,
            expected_revision=state.revision,
            expected_etag=state.etag,
        )

    current_source = source
    pending_ids = current_state.payload.get(_PAGE_IDS_STATE_KEY)
    pending_etag = current_state.payload.get(_INVENTORY_ETAG_STATE_KEY)
    pending_frozen = (
        current_state.payload.get(_INVENTORY_FROZEN_STATE_KEY) is True
    )
    if pending_frozen and (
        not isinstance(pending_ids, list)
        or not _valid_inventory_etag(pending_etag)
    ):
        raise SourceManagerError(
            "Confluence frozen inventory checkpoint is incomplete",
            stage="fetch.confluence",
        )
    empty_final_add_pending = (
        pending_frozen
        and pending_ids == []
        and current_state.payload.get(_INVENTORY_RECONCILED_STATE_KEY)
        is not True
    )
    if current_state.payload.get("phase") == "reflect" and (
        int(current_state.payload.get("pending_count") or 0) > 0
        or empty_final_add_pending
    ):
        pending_is_final = (
            isinstance(pending_ids, list)
            and pending_frozen
            and int(current_state.payload.get("fetched_count") or 0)
            == len(pending_ids)
        )
        current_source, current_state, _summary = _confluence_reflect_batch(
            runner,
            store,
            current_source,
            current_state,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            progress_callback=progress_callback,
            final_batch=pending_is_final,
        )

    state_holder = [current_state]
    source_holder = [current_source]
    stable_values = current_state.payload.get(_PAGE_IDS_STATE_KEY)
    stable_etag_value = current_state.payload.get(
        _INVENTORY_ETAG_STATE_KEY
    )
    completed_before = int(
        current_state.payload.get("fetched_count") or 0
    )
    stable_page_ids = (
        [str(value) for value in stable_values]
        if current_state.payload.get(_INVENTORY_FROZEN_STATE_KEY) is True
        and isinstance(stable_values, list)
        and _valid_inventory_etag(stable_etag_value)
        and completed_before >= 1
        else None
    )
    resume_inventory_etag = (
        str(stable_etag_value) if stable_page_ids is not None else None
    )
    total_holder: list[int | None] = [
        len(stable_page_ids) if stable_page_ids is not None else None
    ]
    staged_inventory: list[list[str] | None] = [None]

    def save_state(value: Mapping[str, Any]) -> None:
        stored = state_holder[0]
        state_holder[0] = store.save_state(
            source.payload["local_source_key"],
            value,
            expected_revision=stored.revision,
            expected_etag=stored.etag,
        )

    def inventory_checkpoint(page_ids: list[str]) -> None:
        normalized = [str(value) for value in page_ids]
        if len(normalized) != len(set(normalized)):
            raise SourceManagerError(
                "Confluence frozen inventory contains duplicates",
                stage="fetch.confluence",
            )
        # The legacy callback is deliberately non-durable.  The core invokes
        # inventory_etag_callback next, allowing IDs and their cryptographic
        # inventory identity to be persisted in one atomic state revision.
        staged_inventory[0] = normalized

    def inventory_etag_checkpoint(
        page_ids: list[str],
        inventory_etag: str,
    ) -> None:
        normalized = [str(value) for value in page_ids]
        if (
            staged_inventory[0] is not None
            and staged_inventory[0] != normalized
        ):
            raise SourceManagerError(
                "Confluence inventory callbacks disagree",
                stage="fetch.confluence",
            )
        if (
            len(normalized) != len(set(normalized))
            or not _valid_inventory_etag(inventory_etag)
        ):
            raise SourceManagerError(
                "Confluence inventory checkpoint etag is invalid",
                stage="fetch.confluence",
            )
        value = copy.deepcopy(state_holder[0].payload)
        preserve_reconciled = (
            value.get(_INVENTORY_FROZEN_STATE_KEY) is True
            and value.get(_INVENTORY_RECONCILED_STATE_KEY) is True
            and value.get(_PAGE_IDS_STATE_KEY) == normalized
            and value.get(_INVENTORY_ETAG_STATE_KEY) == inventory_etag
            and int(value.get("fetched_count") or 0) == len(normalized)
            and int(value.get("indexed_confirmed_count") or 0)
            == len(normalized)
        )
        value[_PAGE_IDS_STATE_KEY] = normalized
        value[_INVENTORY_ETAG_STATE_KEY] = inventory_etag
        value[_INVENTORY_FROZEN_STATE_KEY] = True
        value[_INVENTORY_RECONCILED_STATE_KEY] = preserve_reconciled
        value.update(
            {
                "status": "running",
                "phase": "fetch",
                "fetched_count": 0,
                "indexed_confirmed_count": 0,
                "pending_count": 0,
                "last_completed_item": None,
                "can_resume": True,
                "last_error": None,
            }
        )
        if preserve_reconciled:
            value["fetched_count"] = len(normalized)
            value["indexed_confirmed_count"] = len(normalized)
        save_state(value)
        total_holder[0] = len(normalized)
        staged_inventory[0] = None
        runner._emit_progress(
            progress_callback,
            {
                "phase": "confluence.inventory",
                "label_ja": "Confluenceページ一覧取得",
                "provider": "confluence",
                "completed": len(normalized),
                "total": len(normalized),
                "unit": "件",
                "total_kind": "exact",
                "status": "completed",
                "checkpoint_saved": True,
            },
        )

    def item_checkpoint(completed_count: int, page_id: str) -> None:
        completed = int(completed_count)
        value = copy.deepcopy(state_holder[0].payload)
        frozen = value.get(_PAGE_IDS_STATE_KEY)
        if (
            value.get(_INVENTORY_FROZEN_STATE_KEY) is not True
            or not isinstance(frozen, list)
            or completed < 1
            or completed > len(frozen)
            or str(frozen[completed - 1]) != str(page_id)
        ):
            raise SourceManagerError(
                "Confluence detail escaped its frozen inventory",
                stage="fetch.confluence",
            )
        confirmed = int(value.get("indexed_confirmed_count") or 0)
        value.update(
            {
                "status": "running",
                "phase": "fetch",
                "fetched_count": completed,
                "pending_count": completed - confirmed,
                "last_completed_item": str(page_id),
                "can_resume": True,
                "last_error": None,
            }
        )
        save_state(value)
        runner._emit_progress(
            progress_callback,
            {
                "phase": "confluence.detail",
                "label_ja": "Confluenceページ詳細取得",
                "provider": "confluence",
                "completed": completed,
                "total": total_holder[0],
                "unit": "件",
                "total_kind": "exact",
                "current_item": str(page_id),
                "status": "running",
                "checkpoint_saved": True,
            },
        )

    def reflect_batch(completed_count: int, page_id: str | None) -> None:
        completed = int(completed_count)
        value = copy.deepcopy(state_holder[0].payload)
        frozen = value.get(_PAGE_IDS_STATE_KEY)
        if (
            value.get(_INVENTORY_FROZEN_STATE_KEY) is not True
            or not isinstance(frozen, list)
            or completed != int(value.get("fetched_count") or 0)
            or completed > len(frozen)
        ):
            raise SourceManagerError(
                "Confluence ADD batch escaped its frozen inventory",
                stage="reflect.confluence_batch",
            )
        is_final = completed == len(frozen)
        if (
            is_final
            and value.get(_INVENTORY_RECONCILED_STATE_KEY) is True
            and int(value.get("indexed_confirmed_count") or 0) == completed
        ):
            return
        value.update(
            {
                "status": "running",
                "phase": "reflect",
                "fetched_count": completed,
                "pending_count": max(
                    0,
                    completed
                    - int(value.get("indexed_confirmed_count") or 0),
                ),
                "last_completed_item": (
                    str(page_id) if page_id is not None else None
                ),
                "can_resume": True,
            }
        )
        save_state(value)
        source_holder[0], state_holder[0], _summary = (
            _confluence_reflect_batch(
                runner,
                store,
                source_holder[0],
                state_holder[0],
                python_executable=python_executable,
                rag_root=rag_root,
                command_runner=command_runner,
                progress_callback=progress_callback,
                final_batch=is_final,
            )
        )

    def confluence_progress(event: Mapping[str, Any]) -> None:
        if event.get("event") == "confluence.http_attempt":
            try:
                store.append_event(
                    source.payload["local_source_key"],
                    "confluence.http_attempt",
                    runner._persistable_http_diagnostic(event),
                )
            except Exception:
                pass
        runner._emit_progress(progress_callback, event)

    try:
        outcome = dict(
            runner.execute_fetch_plan(
                plan.to_dict(),
                store.ensure_work_directory(
                    source.payload["local_source_key"]
                ),
                current_state.payload,
                command_runner=command_runner,
                http_get=http_get,
                environment=environment,
                clock=clock,
                inventory_callback=inventory_checkpoint,
                inventory_etag_callback=inventory_etag_checkpoint,
                item_callback=item_checkpoint,
                batch_callback=reflect_batch,
                resume_count=(
                    completed_before if stable_page_ids is not None else 0
                ),
                stable_page_ids=stable_page_ids,
                resume_inventory_etag=resume_inventory_etag,
                progress_callback=confluence_progress,
            )
        )
    except (Exception, KeyboardInterrupt) as exc:
        stored = state_holder[0]
        value = copy.deepcopy(stored.payload)
        value.update(
            {
                "status": "interrupted",
                "can_resume": True,
                "last_error": exception_summary(exc),
            }
        )
        try:
            state_holder[0] = store.save_state(
                source.payload["local_source_key"],
                value,
                expected_revision=stored.revision,
                expected_etag=stored.etag,
            )
        except Exception:
            pass
        try:
            store.append_event(
                source.payload["local_source_key"],
                "confluence.fetch.interrupted",
                {"error": exception_summary(exc)},
            )
        except Exception:
            pass
        if getattr(exc, "stage", None) is None:
            setattr(exc, "stage", "fetch.confluence")
        raise

    if outcome.get("status") not in {"ok", "complete"}:
        raise SourceManagerError(
            "Confluence fetch did not complete",
            stage="fetch.confluence",
        )
    frozen = state_holder[0].payload.get(_PAGE_IDS_STATE_KEY)
    frozen_etag = state_holder[0].payload.get(
        _INVENTORY_ETAG_STATE_KEY
    )
    if (
        state_holder[0].payload.get(_INVENTORY_FROZEN_STATE_KEY) is not True
        or not isinstance(frozen, list)
        or not _valid_inventory_etag(frozen_etag)
        or [str(value) for value in outcome.get("stable_page_ids") or []]
        != [str(value) for value in frozen]
        or str(outcome.get("inventory_etag") or "") != frozen_etag
        or int(outcome.get("documents") or 0) != len(frozen)
        or state_holder[0].payload.get(_INVENTORY_RECONCILED_STATE_KEY)
        is not True
        or int(state_holder[0].payload.get("indexed_confirmed_count") or 0)
        != len(frozen)
    ):
        raise SourceManagerError(
            "Confluence completed inventory was not fully reflected",
            stage="reflect.confluence_batch",
        )

    source_holder[0], link_pending = runner._apply_fetch_metadata(
        store,
        source_holder[0],
        outcome,
    )
    if link_pending:
        raise SourceManagerError(
            "Confluence exact page links are incomplete",
            stage="metadata.confluence",
        )
    if bool(state_holder[0].payload.get("initial_database_reflection")):
        with runner._database_writer_session(
            store.db_root,
            stage="reflect.snapshot",
        ):
            runner._write_initial_snapshot_marker(store.db_root)
    sync_result = runner._synchronize_metadata(
        store,
        source_holder[0],
        rag_root=rag_root,
        metadata_publisher=metadata_publisher,
    )
    stored = state_holder[0]
    final = copy.deepcopy(stored.payload)
    final.update(
        {
            "fetched_count": len(frozen),
            "indexed_confirmed_count": len(frozen),
            "pending_count": 0,
            "metadata_sync_pending": bool(
                sync_result.get("metadata_sync_pending")
            ),
            "last_error": sync_result.get("metadata_error"),
        }
    )
    if sync_result.get("metadata_sync_pending"):
        final.update(
            {
                "status": "interrupted",
                "phase": "metadata",
                "can_resume": True,
            }
        )
    else:
        final = runner.complete_run(final)
    final_state = store.save_state(
        source.payload["local_source_key"],
        final,
        expected_revision=stored.revision,
        expected_etag=stored.etag,
    )
    return {
        **runner._source_dto(
            store,
            store.read_source(source.payload["local_source_key"]),
        ),
        **sync_result,
        "status": (
            "metadata_sync_pending"
            if sync_result.get("metadata_sync_pending")
            else "updated"
        ),
        "fetched_count": len(frozen),
        "indexed_confirmed_count": int(
            final_state.payload.get("indexed_confirmed_count") or 0
        ),
        "state_revision": final_state.revision,
    }


def _confluence_reflect_batch(
    runner: Any,
    store: SourceStore,
    source: Any,
    state: Any,
    *,
    python_executable: Path,
    rag_root: Path,
    command_runner: Any,
    progress_callback: Any,
    final_batch: bool,
) -> tuple[Any, Any, dict[str, Any]]:
    work = store.ensure_work_directory(source.payload["local_source_key"])
    runner.validate_managed_work_tree(work)
    fetched = int(state.payload.get("fetched_count") or 0)
    indexed = int(state.payload.get("indexed_confirmed_count") or 0)
    batch_count = fetched - indexed
    if batch_count <= 0 and not (final_batch and fetched == 0):
        raise SourceManagerError(
            "Confluence ADD batch has no pending pages",
            stage="reflect.confluence_batch",
        )
    runner._emit_progress(
        progress_callback,
        {
            "event": "confluence.add_batch",
            "phase": "confluence.reflect",
            "label_ja": "検索DB反映",
            "provider": "confluence",
            "completed": indexed,
            "current_index": fetched,
            "total": len(state.payload.get(_PAGE_IDS_STATE_KEY) or []),
            "unit": "件",
            "total_kind": "exact",
            "documents": max(0, batch_count),
            "status": "started",
        },
    )
    try:
        add_result = runner._execute_add(
            db_root=store.db_root,
            source=source.payload,
            work=work,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=command_runner,
            progress_callback=progress_callback,
            initial_database_reflection=bool(
                state.payload.get("initial_database_reflection")
            ),
        )
    except (Exception, KeyboardInterrupt) as exc:
        if getattr(exc, "code", None) == "DB_BUSY":
            raise
        interrupted = copy.deepcopy(state.payload)
        interrupted.update(
            {
                "status": "interrupted",
                "phase": "reflect",
                "can_resume": True,
                "last_error": exception_summary(exc),
            }
        )
        try:
            store.save_state(
                source.payload["local_source_key"],
                interrupted,
                expected_revision=state.revision,
                expected_etag=state.etag,
            )
        except Exception:
            pass
        if getattr(exc, "stage", None) is None:
            setattr(exc, "stage", "reflect.confluence_batch")
        raise
    if source.payload.get("source_id"):
        current_source = source
    else:
        runner.confirm_add_success(
            store.db_root,
            source.payload["local_source_key"],
            source_id=str(add_result["source_id"]),
        )
        current_source = store.read_source(
            source.payload["local_source_key"]
        )
    reflected = copy.deepcopy(state.payload)
    reflected.update(
        {
            "status": "running",
            "phase": "fetch",
            "indexed_confirmed_count": fetched,
            "pending_count": 0,
            "can_resume": True,
            "last_error": None,
            _INVENTORY_RECONCILED_STATE_KEY: bool(final_batch),
        }
    )
    current_state = store.save_state(
        source.payload["local_source_key"],
        reflected,
        expected_revision=state.revision,
        expected_etag=state.etag,
    )
    runner._emit_progress(
        progress_callback,
        {
            "event": "confluence.add_batch",
            "phase": "confluence.reflect",
            "label_ja": "検索DB反映",
            "provider": "confluence",
            "completed": fetched,
            "current_index": fetched,
            "total": len(reflected.get(_PAGE_IDS_STATE_KEY) or []),
            "unit": "件",
            "total_kind": "exact",
            "documents": max(0, batch_count),
            "status": "success",
            "checkpoint_saved": True,
        },
    )
    return current_source, current_state, dict(add_result["summary"])


def _install_manager_hook(manager_connections: Any) -> None:
    if bool(getattr(manager_connections, _MANAGER_HOOK_MARKER, False)):
        return
    original = manager_connections.install_manager_connection_ui

    @functools.wraps(original)
    def install_manager_connection_ui(manager_class: type[Any]) -> None:
        original(manager_class)
        _install_manager_ui(manager_class)

    manager_connections.install_manager_connection_ui = install_manager_connection_ui
    setattr(manager_connections, _MANAGER_HOOK_MARKER, True)


def _install_manager_ui(manager_class: type[Any]) -> None:
    if bool(getattr(manager_class, _MANAGER_CLASS_MARKER, False)):
        return
    module = sys.modules.get(manager_class.__module__)
    if module is not None and isinstance(getattr(module, "_PROVIDER_JA", None), dict):
        module._PROVIDER_JA["confluence"] = "Confluence"
    original_ui_type = manager_class._ui_source_type
    original_edit = manager_class._edit_source_fetch_settings
    original_failure_label = manager_class._source_failure_stage_label

    @staticmethod
    def ui_source_type(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        return "confluence" if normalized == "confluence" else original_ui_type(value)

    @functools.wraps(original_edit)
    def edit_source_fetch_settings(
        self: Any,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        if self._ui_source_type(source.get("source_type")) != "confluence":
            return original_edit(self, db_name, source)
        self._show_source_fetch_settings(source)
        self._print_warning(
            "接続・space・取得範囲は検索反映後に変更できません。"
        )
        self._print_info(
            "別範囲を取り込む場合は新しいConfluence Sourceを追加してください。"
        )

    @staticmethod
    def source_failure_stage_label(value: Any) -> str:
        stage = str(value or "").strip()
        if stage.startswith("fetch.confluence"):
            return "Confluenceの取得"
        if stage.startswith("reflect.confluence"):
            return "Confluenceの検索反映"
        return original_failure_label(value)

    manager_class._ui_source_type = ui_source_type
    manager_class._edit_source_fetch_settings = edit_source_fetch_settings
    manager_class._source_failure_stage_label = source_failure_stage_label
    manager_class._prompt_new_confluence_source = prompt_new_confluence_source
    manager_class._add_source_screen = add_source_screen
    setattr(manager_class, _MANAGER_CLASS_MARKER, True)


def _space_key_from_input(value: Any) -> str:
    text = str(value or "")
    if text != text.strip() or not text:
        raise SourceManagerError("Confluence space URL/key is invalid")
    if "://" not in text:
        candidate = text
    else:
        split = urllib.parse.urlsplit(text)
        query = urllib.parse.parse_qs(split.query)
        match = re.search(r"/spaces/([^/?#]+)", split.path, re.IGNORECASE)
        candidate = (
            urllib.parse.unquote(match.group(1))
            if match
            else str((query.get("spaceKey") or [""])[0])
        )
    normalized = _validate_fetch(
        {
            "connection_id": "d35a0a8b-4953-4f71-8ab4-62db403c7771",
            "space_key": candidate,
            "scope": "space",
            "root_page_id": None,
            "attachments": "none",
        }
    )
    return str(normalized["space_key"])


def _page_id_from_input(value: Any) -> str:
    text = str(value or "")
    if text != text.strip() or not text:
        raise SourceManagerError("Confluence root page URL/id is invalid")
    candidate = text
    if "://" in text:
        split = urllib.parse.urlsplit(text)
        query = urllib.parse.parse_qs(split.query)
        match = re.search(r"/pages/([1-9][0-9]*)(?:/|$)", split.path)
        candidate = (
            match.group(1)
            if match
            else str((query.get("pageId") or [""])[0])
        )
    if (
        not re.fullmatch(r"[1-9][0-9]*", candidate)
        or int(candidate) > _MAX_PAGE_ID
    ):
        raise SourceManagerError("Confluence root page ID is invalid")
    return candidate


def prompt_new_confluence_source(self: Any) -> dict[str, Any] | None:
    from .machine_connections import list_confluence_registrations

    registrations = [
        item
        for item in list_confluence_registrations(self.rag_root)
        if item.registered
    ]
    if not registrations:
        self._print_info(
            "Confluence接続が未登録のため、Source接続設定を開きます。"
        )
        if not self._source_connection_settings_screen(required="confluence"):
            return None
        registrations = [
            item
            for item in list_confluence_registrations(self.rag_root)
            if item.registered
        ]
    if not registrations:
        return None
    options = tuple(
        (
            str(index),
            f"{item.display_name} — {item.deployment} — {item.base_url}",
        )
        for index, item in enumerate(registrations, start=1)
    )
    selected = self._select_value("登録済みConfluence接続", options)
    if selected is None:
        return None
    connection = registrations[int(selected) - 1]
    raw_space = self._prompt_preserving_value(
        "Confluence spaceのURLまたはkey",
        "",
        required=True,
        description="例: ENG または https://example.atlassian.net/wiki/spaces/ENG",
    )
    if raw_space is None:
        return None
    try:
        space_key = _space_key_from_input(raw_space)
    except Exception as exc:
        self._print_internal_diagnostic(
            exc,
            operation="Confluence spaceの確認",
            stage="machine_connections.confluence.space",
        )
        return None
    selected_scope = self._select_value(
        "取得範囲",
        (("1", "space全体"), ("2", "特定ページ以下のsubtree")),
        default="1",
    )
    if selected_scope is None:
        return None
    scope = "subtree" if selected_scope == "2" else "space"
    root_page_id: str | None = None
    if scope == "subtree":
        raw_root = self._prompt_preserving_value(
            "root pageのURLまたはpage ID",
            "",
            required=True,
        )
        if raw_root is None:
            return None
        try:
            root_page_id = _page_id_from_input(raw_root)
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="Confluence root pageの確認",
                stage="machine_connections.confluence.root_page",
            )
            return None
    attachment_choice = self._select_value(
        "添付ファイル",
        (("1", "取得しない"), ("2", "metadataだけ取得する")),
        default="1",
    )
    if attachment_choice is None:
        return None
    attachments = "metadata" if attachment_choice == "2" else "none"
    if not self._confirm(
        "登録した資格情報の閲覧権限と同じ範囲だけが取得されることを確認しましたか？"
    ):
        self._print_info("Confluence Source設定は保存されていません。")
        return None
    name = self._prompt_preserving_value(
        "Sourceの名前", "", required=True
    )
    if name is None:
        return None
    return {
        "source_type": "confluence",
        "label": "Confluence",
        "display_name": name,
        "fetch": {
            "connection_id": connection.connection_id,
            "space_key": space_key,
            "scope": scope,
            "root_page_id": root_page_id,
            "attachments": attachments,
        },
        "link": _empty_link(),
        "summary": (
            ("接続", connection.display_name),
            ("space", space_key),
            ("範囲", "space全体" if scope == "space" else f"page {root_page_id} 以下"),
            ("添付", "metadataのみ" if attachments == "metadata" else "取得しない"),
            ("検索への反映", "5件ずつ"),
            ("途中再開", "可能"),
        ),
    }


def add_source_screen(self: Any, db_name: str) -> None:
    if not self._guard_valid_database_target(db_name):
        return
    self._print_screen_header("新しいSourceを追加する", db_name=db_name)
    self._print_menu(
        "種類を選択してください",
        (
            ("1", "GitHubリポジトリ"),
            ("2", "SVN"),
            ("3", "Redmineプロジェクト"),
            ("4", "SharePoint同期フォルダ【追加・更新はWindowsのみ】"),
            ("5", "Teams共有フォルダ【OneDrive同期・Windowsのみ】"),
            ("6", "GitLab Issue"),
            ("7", "GitLab Wiki"),
            ("8", "手元の資料を一度だけ取り込む（Other）"),
            ("9", "GitHub Issues"),
            ("10", "GitHub Wiki"),
            ("11", "Confluence"),
            ("0", "戻る"),
        ),
    )
    choice = self._ask("番号を入力してください: ")
    if choice in (None, "0"):
        return
    forms = {
        "1": self._prompt_new_github_source,
        "2": self._prompt_new_svn_source,
        "3": self._prompt_new_redmine_source,
        "4": self._prompt_new_sharepoint_source,
        "5": self._prompt_new_teams_source,
        "6": self._prompt_new_gitlab_issues_source,
        "7": self._prompt_new_gitlab_wiki_source,
        "8": self._prompt_new_other_source,
        "9": self._prompt_new_github_issues_source,
        "10": self._prompt_new_github_wiki_source,
        "11": self._prompt_new_confluence_source,
    }
    form = forms.get(choice)
    if form is None:
        self._invalid_selection("0～11")
        return
    specification = form()
    if specification is None:
        self._print_info("Source設定は保存されていません。")
        return
    self.output("\n登録内容")
    self.output(f"取得元          : {specification['label']}")
    self.output(f"Sourceの名前    : {specification['display_name']}")
    for label, value in specification.get("summary") or []:
        self.output(f"{label:<16}: {value}")
    if specification["source_type"] == "other":
        self._print_menu("確認", (("1", "保存して取り込みを開始"), ("0", "中止")))
    else:
        self._print_menu(
            "確認",
            (("1", "保存して取得を開始"), ("2", "設定だけ保存"), ("0", "中止")),
        )
    action = self._ask("番号を入力してください: ")
    if action in (None, "0"):
        self._print_info("Source設定は保存されていません。")
        return
    if action not in {"1", "2"} or (
        specification["source_type"] == "other" and action == "2"
    ):
        self._invalid_selection(
            "1、または0" if specification["source_type"] == "other" else "0～2"
        )
        return
    try:
        from source_manager.runner import register_source

        result = register_source(
            self._database_root(db_name),
            source_type=str(specification["source_type"]),
            display_name=str(specification["display_name"]),
            fetch=dict(specification["fetch"]),
            link=specification.get("link"),
            runtime_input=specification.get("runtime_input"),
            start=action == "1",
            python_executable=self._runtime_python(),
            rag_root=self.rag_root,
            progress_callback=self._progress_callback(
                "Source追加",
                provider=str(specification.get("source_type") or ""),
            ),
        )
    except Exception as exc:
        self._print_source_exception(
            exc,
            operation="Source登録",
            db_name=db_name,
            source_name=str(specification.get("display_name") or ""),
            provider=str(specification.get("source_type") or ""),
        )
        return
    if action == "1":
        status = str(result.get("status") or "")
        if status == "updated":
            self._print_success("Sourceを保存し、検索へ反映しました。")
        elif status in {"failed", "error"}:
            self._print_source_result_failure(result, operation="Source登録後の初回処理")
        else:
            self._print_warning(
                "Sourceを保存しましたが、処理は再開可能な位置で"
                f"停止しています（状態: {status or '不明'}）。"
            )
    else:
        self._print_success("Sourceの取得設定を保存しました。")
        self.output(
            "検索へ反映されるまでは、Copilot向けDB内容一覧には表示されません。"
        )
