from __future__ import annotations

import copy
import functools
import sys
from pathlib import Path
from typing import Any, Mapping


FILE_SELECTION_KEY = "file_selection"
FILE_SELECTION_ALL = "all_supported"
FILE_SELECTION_DOCUMENTS = "documents_only"
FILE_SOURCE_TYPES = frozenset({"github", "svn", "sharepoint", "teams", "other"})
_EXTERNAL_FOLDER_TYPES = frozenset({"sharepoint", "teams"})
_PROVIDER_MARKER = "_local_rag_document_filter_provider_installed"
_RUNNER_MARKER = "_local_rag_document_filter_runner_installed"
_MANAGER_HOOK_MARKER = "_local_rag_document_filter_manager_hook_installed"
_MANAGER_CLASS_MARKER = "_local_rag_document_filter_manager_installed"


def install_document_filter_runtime() -> None:
    """Install a portable per-Source document-only ingestion selection."""

    from . import manager_connections, providers, runner, store as store_module

    _install_provider_contract(providers, runner, store_module)
    _install_runner_contract(runner, providers)
    _install_manager_hook(manager_connections)


def _install_provider_contract(providers: Any, runner: Any, store_module: Any) -> None:
    if bool(getattr(providers, _PROVIDER_MARKER, False)):
        return
    original = providers.validate_provider_config

    @functools.wraps(original)
    def validate_provider_config(
        provider: str,
        settings: Mapping[str, Any],
    ) -> dict[str, Any]:
        kind = str(provider or "").strip().lower()
        supplied = dict(settings)
        if kind not in FILE_SOURCE_TYPES:
            return original(kind, supplied)
        selection = _normalize_selection(supplied.pop(FILE_SELECTION_KEY, None))
        normalized = dict(original(kind, supplied))
        normalized[FILE_SELECTION_KEY] = selection
        return normalized

    providers.validate_provider_config = validate_provider_config
    runner.validate_provider_config = validate_provider_config
    store_module.validate_provider_config = validate_provider_config
    setattr(providers, _PROVIDER_MARKER, True)


def _install_runner_contract(runner: Any, providers: Any) -> None:
    if bool(getattr(runner, _RUNNER_MARKER, False)):
        return
    original_execute_add = runner._execute_add
    original_update_configuration = runner.update_source_configuration

    @functools.wraps(original_execute_add)
    def execute_add(
        *,
        db_root: Path,
        source: Mapping[str, Any],
        work: Path,
        python_executable: Path,
        rag_root: Path,
        command_runner: Any,
        progress_callback: Any,
    ) -> dict[str, Any]:
        if _source_selection(source) != FILE_SELECTION_DOCUMENTS:
            return original_execute_add(
                db_root=db_root,
                source=source,
                work=work,
                python_executable=python_executable,
                rag_root=rag_root,
                command_runner=command_runner,
                progress_callback=progress_callback,
            )

        def document_runner(arguments: list[str]) -> Any:
            command = list(arguments)
            for index, value in enumerate(command):
                if Path(str(value)).name == "add_data.py":
                    replacement = Path(str(value)).with_name(
                        "add_data_documents_only.py"
                    )
                    command[index] = str(replacement)
                    break
            else:
                raise runner.SourceManagerError(
                    "ADD command does not contain add_data.py",
                    stage="reflect.add",
                )
            if command_runner is not None:
                return command_runner(command)
            return runner.run_streaming_process(
                command,
                progress_callback=progress_callback,
            )

        return original_execute_add(
            db_root=db_root,
            source=source,
            work=work,
            python_executable=python_executable,
            rag_root=rag_root,
            command_runner=document_runner,
            progress_callback=progress_callback,
        )

    @functools.wraps(original_update_configuration)
    def update_source_configuration(
        db_root: Path,
        local_source_key: str,
        *,
        fetch: Mapping[str, Any],
        display_name: str | None = None,
        pending_link: Any = runner._UNSET,
    ) -> dict[str, Any]:
        store = runner.SourceStore(Path(db_root))
        source = store.read_source(local_source_key)
        source_type = str(source.payload.get("source_type") or "").strip().lower()
        if source.payload.get("source_id") and source_type in _EXTERNAL_FOLDER_TYPES:
            normalized = providers.validate_provider_config(source_type, fetch)
            current = dict(source.payload.get("fetch") or {})
            if _without_selection(normalized) == _without_selection(current):
                payload = copy.deepcopy(source.payload)
                payload["fetch"] = normalized
                if display_name is not None:
                    payload["display_name"] = str(display_name)
                if pending_link is not runner._UNSET:
                    if pending_link is None:
                        payload.pop("pending_metadata", None)
                    else:
                        payload["pending_metadata"] = {
                            "source_type": payload["source_type"],
                            "link": copy.deepcopy(dict(pending_link)),
                        }
                saved = store.save_source(
                    payload,
                    expected_revision=source.revision,
                    expected_etag=source.etag,
                )
                store.append_event(
                    local_source_key,
                    "source.file_selection_updated",
                    {FILE_SELECTION_KEY: normalized[FILE_SELECTION_KEY]},
                )
                return runner._source_dto(store, saved)
        arguments: dict[str, Any] = {
            "fetch": fetch,
            "display_name": display_name,
        }
        if pending_link is not runner._UNSET:
            arguments["pending_link"] = pending_link
        return original_update_configuration(
            db_root,
            local_source_key,
            **arguments,
        )

    runner._execute_add = execute_add
    runner.update_source_configuration = update_source_configuration
    setattr(runner, _RUNNER_MARKER, True)
    package = sys.modules.get(__package__)
    if package is not None:
        setattr(package, "update_source_configuration", update_source_configuration)


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

    for method_name in (
        "_prompt_new_github_source",
        "_prompt_new_svn_source",
        "_prompt_new_sharepoint_source",
        "_prompt_new_teams_source",
        "_prompt_new_other_source",
    ):
        original = getattr(manager_class, method_name, None)
        if not callable(original):
            continue
        setattr(manager_class, method_name, _wrap_registration_form(original))

    original_show = manager_class._show_source_fetch_settings
    original_edit = manager_class._edit_source_fetch_settings

    @functools.wraps(original_show)
    def show_source_fetch_settings(self: Any, source: dict[str, Any]) -> dict[str, Any]:
        fetch = original_show(self, source)
        source_type = str(self._ui_source_type(source.get("source_type"))).lower()
        if source_type in FILE_SOURCE_TYPES:
            self.output(
                "ファイル種類: "
                + _selection_label(_selection_from_fetch(fetch))
            )
        return fetch

    @functools.wraps(original_edit)
    def edit_source_fetch_settings(
        self: Any,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        source_type = str(self._ui_source_type(source.get("source_type"))).lower()
        if source_type not in FILE_SOURCE_TYPES:
            return original_edit(self, db_name, source)
        fetch = source.get("fetch")
        if not isinstance(fetch, dict):
            fetch = source.get("provider_settings")
        current_fetch = dict(fetch) if isinstance(fetch, dict) else {}
        current_selection = _selection_from_fetch(current_fetch)
        selection = _prompt_selection(self, current=current_selection)
        if selection is None:
            self._print_info("取得設定は変更されていません。")
            return
        updated_source = copy.deepcopy(source)
        updated_fetch = dict(current_fetch)
        updated_fetch[FILE_SELECTION_KEY] = selection
        updated_source["fetch"] = updated_fetch

        if source_type in _EXTERNAL_FOLDER_TYPES and source.get("source_id"):
            if selection == current_selection:
                return original_edit(self, db_name, updated_source)
            return _save_selection_only(
                self,
                db_name,
                updated_source,
                updated_fetch,
            )
        if source_type == "other":
            if selection == current_selection:
                return original_edit(self, db_name, updated_source)
            return _save_selection_only(
                self,
                db_name,
                updated_source,
                updated_fetch,
            )
        return original_edit(self, db_name, updated_source)

    manager_class._show_source_fetch_settings = show_source_fetch_settings
    manager_class._edit_source_fetch_settings = edit_source_fetch_settings
    setattr(manager_class, _MANAGER_CLASS_MARKER, True)


def _wrap_registration_form(original: Any) -> Any:
    @functools.wraps(original)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        specification = original(self, *args, **kwargs)
        if specification is None:
            return None
        selection = _prompt_selection(self, current=FILE_SELECTION_ALL)
        if selection is None:
            return None
        value = copy.deepcopy(dict(specification))
        fetch = dict(value.get("fetch") or {})
        fetch[FILE_SELECTION_KEY] = selection
        value["fetch"] = fetch
        value["summary"] = tuple(value.get("summary") or ()) + (
            ("ファイル種類", _selection_label(selection)),
        )
        return value

    return wrapped


def _prompt_selection(self: Any, *, current: str) -> str | None:
    selected = self._select_value(
        "取り込むファイル種類",
        (
            ("1", "対応する全ファイル【既定】"),
            (
                "2",
                "文書のみ取得（Office・PDF・テキスト・Astah・PlantUML）",
            ),
        ),
        default="2" if current == FILE_SELECTION_DOCUMENTS else "1",
    )
    if selected is None:
        return None
    return FILE_SELECTION_DOCUMENTS if str(selected) == "2" else FILE_SELECTION_ALL


def _save_selection_only(
    self: Any,
    db_name: str,
    source: Mapping[str, Any],
    fetch: Mapping[str, Any],
) -> None:
    local_key = str(source.get("_local_source_key") or "")
    if not local_key:
        self._print_info("このSourceには変更できる取得設定がありません。")
        return
    self.output("\n変更後の取得設定")
    self.output("ファイル種類: " + _selection_label(_selection_from_fetch(fetch)))
    if not self._confirm("この内容で取得設定を保存しますか？"):
        self._print_info("取得設定は変更されていません。")
        return
    try:
        from source_manager.runner import update_source_configuration

        update_source_configuration(
            self._database_root(db_name),
            local_key,
            fetch=dict(fetch),
        )
    except Exception as exc:
        self._print_internal_diagnostic(
            exc,
            operation="Source取得設定の保存",
            stage="source_config.file_selection.save",
            db_name=db_name,
            source_name=str(source.get("display_name") or ""),
            source_key=local_key,
            provider=str(source.get("source_type") or ""),
            can_resume=True,
        )
        return
    self._print_success("取り込むファイル種類を保存しました。")
    self.output(
        "次回の更新で対象外ファイルはコピー先DBのベクトル・catalogから削除されます。"
    )


def _normalize_selection(value: Any) -> str:
    text = str(value or FILE_SELECTION_ALL).strip().lower()
    if text not in {FILE_SELECTION_ALL, FILE_SELECTION_DOCUMENTS}:
        raise ValueError("file_selection must be all_supported or documents_only")
    return text


def _selection_from_fetch(fetch: Mapping[str, Any]) -> str:
    try:
        return _normalize_selection(fetch.get(FILE_SELECTION_KEY))
    except ValueError:
        return FILE_SELECTION_ALL


def _source_selection(source: Mapping[str, Any]) -> str:
    fetch = source.get("fetch")
    return _selection_from_fetch(fetch if isinstance(fetch, Mapping) else {})


def _without_selection(value: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(value)
    output.pop(FILE_SELECTION_KEY, None)
    return output


def _selection_label(selection: str) -> str:
    if selection == FILE_SELECTION_DOCUMENTS:
        return "文書のみ取得（.asta・.puを含む）"
    return "対応する全ファイル"


__all__ = [
    "FILE_SELECTION_ALL",
    "FILE_SELECTION_DOCUMENTS",
    "FILE_SELECTION_KEY",
    "FILE_SOURCE_TYPES",
    "install_document_filter_runtime",
]
