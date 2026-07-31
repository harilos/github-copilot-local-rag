from __future__ import annotations

import copy
import functools
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError
from .git_host_urls import (
    GIT_SOURCE_TYPES,
    SOURCE_LABELS,
    make_repository_link,
    normalize_clone_url,
    propose_repository_web_url,
)

_PROVIDER_MARKER = "_local_rag_git_host_provider_installed"
_EXECUTION_MARKER = "_local_rag_git_host_execution_installed"
_RUNNER_MARKER = "_local_rag_git_host_runner_installed"


def install_git_host_runtime(
    providers: Any,
    runner: Any,
    store: Any,
    execution: Any,
    progress: Any,
) -> None:
    _install_provider_contract(providers, runner, store)
    _install_execution_contract(execution, runner)
    _install_runner_contract(runner)
    for source_type, label in SOURCE_LABELS.items():
        progress._PROVIDER_LABELS[source_type] = label


def _install_provider_contract(
    providers: Any,
    runner: Any,
    store: Any,
) -> None:
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
        if kind not in GIT_SOURCE_TYPES:
            return original_validate(kind, settings)
        if not isinstance(settings, Mapping):
            raise SourceManagerError("provider settings must be an object")
        supplied = dict(settings)
        supplied["repository_url"] = normalize_clone_url(
            kind,
            supplied.get("repository_url"),
        )
        return dict(original_validate("github", supplied))

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
        if kind not in GIT_SOURCE_TYPES:
            return original_build(
                source_key=source_key,
                provider=provider,
                settings=settings,
                logical_root=logical_root,
                work_path=work_path,
            )
        normalized = validate_provider_config(kind, settings)
        root = providers.validate_relative_path(
            logical_root,
            field="logical_root",
            allow_empty=False,
        )
        work = providers.validate_relative_path(
            work_path,
            field="work_path",
            allow_empty=False,
        )
        if root != work:
            raise SourceManagerError(
                "logical_root and work_path must use the fixed Source work path"
            )
        step = providers.FetchStep(
            "repository",
            "git_fetch",
            True,
            work,
            normalized,
            "repository_revision",
        )
        body = {
            "schema_version": "local-rag.fetch-plan.v1",
            "source_key": str(source_key),
            "provider": kind,
            "logical_root": root,
            "work_path": work,
            "steps": [step.to_dict()],
        }
        providers.validate_persistable(body, field="fetch_plan")
        etag = hashlib.sha256(
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
            logical_root=root,
            work_path=work,
            steps=(step,),
            plan_etag=etag,
        )

    providers.SUPPORTED_PROVIDERS = frozenset(
        set(providers.SUPPORTED_PROVIDERS) | set(GIT_SOURCE_TYPES)
    )
    providers.validate_provider_config = validate_provider_config
    providers.build_fetch_plan = build_fetch_plan
    runner.validate_provider_config = validate_provider_config
    store.validate_provider_config = validate_provider_config
    store.build_fetch_plan = build_fetch_plan
    setattr(providers, _PROVIDER_MARKER, True)


def _install_execution_contract(execution: Any, runner: Any) -> None:
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
        kind = str(plan.get("provider") or "").strip().lower()
        if kind not in GIT_SOURCE_TYPES:
            return original(plan, work_directory, state, **kwargs)
        delegated = copy.deepcopy(dict(plan))
        delegated["provider"] = "github"
        options = dict(kwargs)
        options["progress_callback"] = _ProgressProxy(
            kwargs.get("progress_callback"),
            kind,
        )
        return dict(
            original(
                delegated,
                work_directory,
                state,
                **options,
            )
        )

    execution.execute_fetch_plan = execute_fetch_plan
    runner.execute_fetch_plan = execute_fetch_plan
    setattr(execution, _EXECUTION_MARKER, True)


class _ProgressProxy:
    def __init__(self, target: Any, source_type: str) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_source_type", source_type)

    def __call__(self, event: Mapping[str, Any]) -> Any:
        target = object.__getattribute__(self, "_target")
        if target is None:
            return None
        value = dict(event)
        kind = object.__getattribute__(self, "_source_type")
        if str(value.get("provider") or "").strip().lower() == "github":
            value["provider"] = kind
        phase = str(value.get("phase") or "")
        if phase == "github" or phase.startswith("github."):
            value["phase"] = kind + phase[len("github") :]
        return target(value)

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_target")
        if target is None:
            raise AttributeError(name)
        return getattr(target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_target", "_source_type"}:
            object.__setattr__(self, name, value)
            return
        target = object.__getattribute__(self, "_target")
        if target is None:
            object.__setattr__(self, name, value)
        else:
            setattr(target, name, value)

    def __delattr__(self, name: str) -> None:
        target = object.__getattribute__(self, "_target")
        if target is None:
            raise AttributeError(name)
        delattr(target, name)


def _install_runner_contract(runner: Any) -> None:
    if bool(getattr(runner, _RUNNER_MARKER, False)):
        return
    original_apply = runner._apply_fetch_metadata
    original_update = runner.update_source

    @functools.wraps(original_apply)
    def apply_fetch_metadata(
        store: Any,
        source: Any,
        outcome: Mapping[str, Any],
    ) -> tuple[Any, bool]:
        kind = str(
            source.payload.get("source_type") or ""
        ).strip().lower()
        if kind not in GIT_SOURCE_TYPES:
            return original_apply(store, source, outcome)
        if source.payload.get("source_id"):
            return source, False
        if kind == "other-git":
            if "pending_metadata" not in source.payload:
                return source, False
            payload = copy.deepcopy(source.payload)
            payload.pop("pending_metadata", None)
            return (
                store.save_source(
                    payload,
                    expected_revision=source.revision,
                    expected_etag=source.etag,
                ),
                False,
            )
        branch = str(outcome.get("default_branch") or "").strip()
        pending = source.payload.get("pending_metadata")
        link = (
            copy.deepcopy(dict(pending.get("link") or {}))
            if isinstance(pending, Mapping)
            else {}
        )
        settings = link.get("settings")
        settings = dict(settings) if isinstance(settings, Mapping) else {}
        web_url = str(settings.get("repository_url") or "").strip()
        if not web_url:
            web_url = propose_repository_web_url(
                kind,
                (source.payload.get("fetch") or {}).get("repository_url"),
            )
        if not branch or not web_url:
            return source, True
        try:
            link = make_repository_link(kind, web_url, ref=branch)
        except SourceManagerError:
            return source, True
        payload = copy.deepcopy(source.payload)
        payload["pending_metadata"] = {
            "source_type": kind,
            "link": link,
        }
        return (
            store.save_source(
                payload,
                expected_revision=source.revision,
                expected_etag=source.etag,
            ),
            False,
        )

    @functools.wraps(original_update)
    def update_source(
        db_root: Path,
        local_source_key: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        store = runner.SourceStore(Path(db_root))
        source = store.read_source(local_source_key)
        kind = str(
            source.payload.get("source_type") or ""
        ).strip().lower()
        if (
            kind in GIT_SOURCE_TYPES - {"github"}
            and kwargs.get("executor") is None
            and kwargs.get("command_runner") is None
            and kwargs.get("http_get") is None
            and kwargs.get("rag_root") is not None
        ):
            route = runner.resolve_source_network_route(
                Path(kwargs["rag_root"]),
                environment=kwargs.get("environment"),
                progress_callback=kwargs.get("progress_callback"),
            )
            kwargs["command_runner"] = route.command_runner
            kwargs["http_get"] = route.http_get
            kwargs["environment"] = route.environment
        return dict(original_update(db_root, local_source_key, **kwargs))

    runner._apply_fetch_metadata = apply_fetch_metadata
    runner.update_source = update_source
    package = sys.modules.get(__package__)
    if package is not None:
        setattr(package, "update_source", update_source)
    setattr(runner, _RUNNER_MARKER, True)


__all__ = ["install_git_host_runtime"]
