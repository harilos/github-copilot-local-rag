from __future__ import annotations

import copy
import functools
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .errors import SourceManagerError


_PROVIDER_MARKER = "_local_rag_generic_git_provider_installed"
_EXECUTION_MARKER = "_local_rag_generic_git_execution_installed"
_MANAGER_HOOK_MARKER = "_local_rag_generic_git_manager_hook_installed"
_MANAGER_CLASS_MARKER = "_local_rag_generic_git_manager_installed"
_MAX_INCLUDE_PATHS = 100
_GIT_SOURCE_TYPE = "github"  # Persistent compatibility identifier.


def install_git_source_runtime() -> None:
    """Extend the legacy ``github`` Source into a generic Git Source.

    ``source_type=github`` remains the persistent identifier so existing DBs,
    catalog rows, Source metadata, and links do not require migration. Only the
    acquisition contract and human-facing labels become provider-neutral.
    """

    from . import execution, manager_connections, progress, providers, runner
    from . import store as store_module

    _install_provider_contract(providers, runner, store_module)
    _install_execution_contract(execution, runner)
    progress._PROVIDER_LABELS[_GIT_SOURCE_TYPE] = "Git"
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
        if kind != _GIT_SOURCE_TYPE:
            return original(kind, settings)
        if not isinstance(settings, Mapping):
            raise SourceManagerError("provider settings must be an object")
        supplied = dict(settings)
        providers.validate_persistable(supplied, field="provider_settings")
        providers._only_keys(
            supplied,
            {"repository_url", "include_paths", "updated_within_days"},
        )
        days = supplied.get("updated_within_days")
        if days is not None:
            if (
                isinstance(days, bool)
                or not str(days).isdigit()
                or not 1 <= int(days) <= 3650
            ):
                raise SourceManagerError(
                    "updated_within_days must be null or between 1 and 3650"
                )
            days = int(days)
        return {
            "repository_url": providers._validate_git_fetch_url(
                supplied.get("repository_url")
            ),
            "include_paths": _normalize_include_paths(
                supplied.get("include_paths"),
                providers=providers,
            ),
            "updated_within_days": days,
        }

    providers.validate_provider_config = validate_provider_config
    runner.validate_provider_config = validate_provider_config
    store_module.validate_provider_config = validate_provider_config
    setattr(providers, _PROVIDER_MARKER, True)


def _normalize_include_paths(value: Any, *, providers: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise SourceManagerError("include_paths must be an array of Git folders")
    if len(value) > _MAX_INCLUDE_PATHS:
        raise SourceManagerError(
            f"include_paths cannot contain more than {_MAX_INCLUDE_PATHS} folders"
        )
    normalized: list[str] = []
    for item in value:
        path = providers.validate_relative_path(
            item,
            field="include_paths",
            allow_empty=False,
        )
        parts = PurePosixPath(path).parts
        if any(part.casefold() == ".git" for part in parts):
            raise SourceManagerError("include_paths cannot contain .git")
        if any(
            path == existing or path.startswith(existing + "/")
            for existing in normalized
        ):
            continue
        normalized = [
            existing
            for existing in normalized
            if not existing.startswith(path + "/")
        ]
        normalized.append(path)
    return normalized


def _install_execution_contract(execution: Any, runner_module: Any) -> None:
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
        provider = str(plan.get("provider") or "").strip().lower()
        if provider != _GIT_SOURCE_TYPE:
            return original(plan, work_directory, state, **kwargs)
        command_runner = kwargs.get("command_runner") or execution._run_command
        progress_callback = kwargs.get("progress_callback")
        clock = kwargs.get("clock")
        step = dict((plan.get("steps") or [{}])[0])
        settings = dict(step.get("parameters") or {})
        work = Path(work_directory)
        if not work.is_dir() or work.is_symlink():
            raise SourceManagerError("work directory is unsafe")
        execution._emit_provider_progress(
            progress_callback,
            _GIT_SOURCE_TYPE,
            "started",
        )
        try:
            cutoff = git_updated_on_cutoff(
                settings.get("updated_within_days"),
                state,
                clock=clock,
            )
            result = _git_fetch(
                settings,
                work,
                command_runner,
                updated_on_cutoff=cutoff,
                execution=execution,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            execution._emit_provider_progress(
                progress_callback,
                _GIT_SOURCE_TYPE,
                "failed",
                error=exc,
            )
            raise
        execution._emit_provider_progress(
            progress_callback,
            _GIT_SOURCE_TYPE,
            "completed",
            documents=int(result.get("documents") or 0),
        )
        return result

    execution.execute_fetch_plan = execute_fetch_plan
    runner_module.execute_fetch_plan = execute_fetch_plan
    setattr(execution, _EXECUTION_MARKER, True)


def git_updated_on_cutoff(
    updated_within_days: Any,
    state: Mapping[str, Any] | None,
    *,
    clock: Any = None,
) -> datetime | None:
    """Return a resume-stable cutoff for a Git file's last commit time."""

    if updated_within_days is None:
        return None
    if (
        isinstance(updated_within_days, bool)
        or not str(updated_within_days).isdigit()
        or not 1 <= int(updated_within_days) <= 3650
    ):
        raise SourceManagerError(
            "updated_within_days must be null or between 1 and 3650",
            stage="fetch.github",
        )
    payload = state if isinstance(state, Mapping) else {}
    started_at = payload.get("started_at")
    if started_at is not None:
        if not isinstance(started_at, str) or not started_at.strip():
            raise SourceManagerError(
                "Git run start time is invalid",
                stage="fetch.github",
            )
        try:
            anchor = datetime.fromisoformat(
                started_at.strip().replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise SourceManagerError(
                "Git run start time is invalid",
                stage="fetch.github",
            ) from exc
    else:
        anchor = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(anchor, datetime):
        raise SourceManagerError(
            "Git clock must return a datetime",
            stage="fetch.github",
        )
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return anchor.astimezone(timezone.utc) - timedelta(
        days=int(updated_within_days)
    )


def _git_fetch(
    settings: Mapping[str, Any],
    work: Path,
    command_runner: Any,
    *,
    updated_on_cutoff: datetime | None,
    execution: Any,
    progress_callback: Any = None,
) -> dict[str, Any]:
    repository = str(settings["repository_url"])
    include_paths = [
        str(value) for value in settings.get("include_paths") or []
    ]
    control = work.parent.parent / "provider" / ".git"
    execution._ensure_real_directory(control.parent)
    if control.exists() or control.is_symlink():
        metadata = os.lstat(control)
        if (
            execution._is_link_or_reparse(control, metadata)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise SourceManagerError(
                "Git control directory is unsafe",
                stage="fetch.github",
            )
        execution._checked(
            command_runner(
                [
                    "git",
                    f"--git-dir={control}",
                    "remote",
                    "set-url",
                    "origin",
                    repository,
                ]
            ),
            operation="Git取得URLの更新",
            stage="fetch.github",
        )
        execution._checked(
            command_runner(
                [
                    "git",
                    f"--git-dir={control}",
                    f"--work-tree={work}",
                    "fetch",
                    "--prune",
                    "origin",
                ]
            ),
            operation="Gitの更新",
            stage="fetch.github",
        )
    else:
        if any(work.iterdir()):
            raise SourceManagerError(
                "Git work directory is not empty",
                stage="fetch.github",
            )
        execution._checked(
            command_runner(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    f"--separate-git-dir={control}",
                    "--",
                    repository,
                    str(work),
                ]
            ),
            operation="Gitリポジトリの取得",
            stage="fetch.github",
        )
        _remove_git_pointer(work, execution=execution)

    branch_result = execution._checked(
        command_runner(
            [
                "git",
                f"--git-dir={control}",
                "symbolic-ref",
                "--short",
                "refs/remotes/origin/HEAD",
            ]
        ),
        operation="Gitの既定ブランチ確認",
        stage="fetch.github",
    )
    branch = str(getattr(branch_result, "stdout", "") or "").strip()
    if branch.startswith("origin/"):
        branch = branch[len("origin/") :]
    if not branch:
        raise SourceManagerError(
            "remote default branch is unavailable",
            stage="fetch.github",
        )
    remote_branch = f"refs/remotes/origin/{branch}"

    if include_paths:
        execution._checked(
            command_runner(
                [
                    "git",
                    f"--git-dir={control}",
                    f"--work-tree={work}",
                    "sparse-checkout",
                    "init",
                    "--cone",
                ]
            ),
            operation="Git部分取得の準備",
            stage="fetch.github",
        )
        execution._checked(
            command_runner(
                [
                    "git",
                    f"--git-dir={control}",
                    f"--work-tree={work}",
                    "sparse-checkout",
                    "set",
                    "--cone",
                    "--",
                    *include_paths,
                ]
            ),
            operation="Git取得フォルダの設定",
            stage="fetch.github",
        )
    else:
        execution._checked(
            command_runner(
                [
                    "git",
                    f"--git-dir={control}",
                    f"--work-tree={work}",
                    "sparse-checkout",
                    "disable",
                ]
            ),
            operation="Git全体取得への切り替え",
            stage="fetch.github",
        )

    execution._checked(
        command_runner(
            [
                "git",
                f"--git-dir={control}",
                f"--work-tree={work}",
                "reset",
                "--hard",
                remote_branch,
            ]
        ),
        operation="Git作業ファイルの反映",
        stage="fetch.github",
    )
    execution._checked(
        command_runner(
            [
                "git",
                f"--git-dir={control}",
                f"--work-tree={work}",
                "clean",
                "-ffdqx",
            ]
        ),
        operation="Git作業ファイルの整理",
        stage="fetch.github",
    )

    selected = [PurePosixPath(value) for value in include_paths]
    if selected:
        for relative in selected:
            target = work.joinpath(*relative.parts)
            try:
                metadata = os.lstat(target)
            except OSError as exc:
                raise SourceManagerError(
                    f"Gitの指定フォルダが見つかりません: {relative.as_posix()}",
                    stage="fetch.github",
                ) from exc
            if (
                execution._is_link_or_reparse(target, metadata)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise SourceManagerError(
                    f"Gitの取得対象はフォルダで指定してください: {relative.as_posix()}",
                    stage="fetch.github",
                )

    all_files = _git_work_files(work, execution=execution)
    scoped_files: list[tuple[PurePosixPath, Path]] = []
    for index, (relative, path) in enumerate(all_files, start=1):
        if selected and not _inside_selected_folder(relative, selected):
            path.unlink()
        else:
            scoped_files.append((relative, path))
        execution._emit_file_progress(
            progress_callback,
            _GIT_SOURCE_TYPE,
            index,
            relative.as_posix(),
        )

    inventory_documents = len(scoped_files)
    if updated_on_cutoff is not None:
        eligible_paths = _git_changed_paths_since(
            control,
            remote_branch,
            updated_on_cutoff,
            include_paths=include_paths,
            command_runner=command_runner,
            execution=execution,
        )
        for index, (relative, path) in enumerate(scoped_files, start=1):
            if relative not in eligible_paths:
                path.unlink()
            execution._emit_file_progress(
                progress_callback,
                _GIT_SOURCE_TYPE,
                index,
                relative.as_posix(),
            )

    _prune_empty_directories(work, execution=execution)
    execution.validate_managed_work_tree(work)
    final_files = _git_work_files(work, execution=execution)
    result: dict[str, Any] = {
        "status": "ok",
        "default_branch": branch,
        "documents": len(final_files),
        "inventory_documents": inventory_documents,
        "eligible_documents": len(final_files),
        "include_paths": include_paths,
    }
    if updated_on_cutoff is not None:
        result["updated_on_cutoff"] = updated_on_cutoff.isoformat().replace(
            "+00:00", "Z"
        )
    return result


def _remove_git_pointer(work: Path, *, execution: Any) -> None:
    pointer = work / ".git"
    if not (pointer.exists() or pointer.is_symlink()):
        return
    metadata = os.lstat(pointer)
    if (
        execution._is_link_or_reparse(pointer, metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise SourceManagerError(
            "Git metadata pointer is unsafe",
            stage="fetch.github",
        )
    pointer.unlink()


def _git_work_files(
    work: Path,
    *,
    execution: Any,
) -> list[tuple[PurePosixPath, Path]]:
    values: list[tuple[PurePosixPath, Path]] = []
    for directory, child_names, file_names in os.walk(
        work,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        for name in list(child_names):
            candidate = directory_path / name
            metadata = os.lstat(candidate)
            if (
                execution._is_link_or_reparse(candidate, metadata)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise SourceManagerError(
                    "Git checkout contains a link or special directory",
                    stage="fetch.github",
                )
            if name.casefold() == ".git":
                raise SourceManagerError(
                    "Git checkout contains visible VCS metadata",
                    stage="fetch.github",
                )
        for name in sorted(file_names):
            candidate = directory_path / name
            metadata = os.lstat(candidate)
            if (
                execution._is_link_or_reparse(candidate, metadata)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise SourceManagerError(
                    "Git checkout contains a link or special file",
                    stage="fetch.github",
                )
            relative = PurePosixPath(candidate.relative_to(work).as_posix())
            _validate_git_relative_path(relative)
            values.append((relative, candidate))
    values.sort(key=lambda item: item[0].as_posix())
    return values


def _inside_selected_folder(
    path: PurePosixPath,
    selected: list[PurePosixPath],
) -> bool:
    return any(
        len(path.parts) > len(folder.parts)
        and path.parts[: len(folder.parts)] == folder.parts
        for folder in selected
    )


def _git_changed_paths_since(
    control: Path,
    remote_branch: str,
    cutoff: datetime,
    *,
    include_paths: list[str],
    command_runner: Any,
    execution: Any,
) -> set[PurePosixPath]:
    arguments = [
        "git",
        f"--git-dir={control}",
        "-c",
        "core.quotepath=false",
        "log",
        f"--since=@{max(1, int(cutoff.timestamp()))}",
        "--format=",
        "--name-only",
        "-z",
        "--diff-filter=ACMRT",
        "--no-renames",
        remote_branch,
        "--",
        *include_paths,
    ]
    with tempfile.TemporaryFile(mode="w+b") as stdout_sink:
        if bool(getattr(command_runner, "supports_stdout_sink", False)):
            completed = command_runner(arguments, stdout_sink=stdout_sink)
            execution._checked(
                completed,
                operation="Git最終コミット日時の確認",
                stage="fetch.github",
            )
            stdout_sink.flush()
            stdout_sink.seek(0)
            raw = stdout_sink.read()
        else:
            completed = execution._checked(
                command_runner(arguments),
                operation="Git最終コミット日時の確認",
                stage="fetch.github",
            )
            if bool(getattr(completed, "stdout_truncated", False)):
                raise SourceManagerError(
                    "Git最終コミット日時の出力が途中で切れました",
                    stage="fetch.github",
                )
            stdout = getattr(completed, "stdout", "")
            raw = stdout if isinstance(stdout, bytes) else str(stdout or "").encode(
                "utf-8"
            )
    return _parse_git_nul_paths(raw)


def _parse_git_nul_paths(raw: bytes) -> set[PurePosixPath]:
    values: set[PurePosixPath] = set()
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        if encoded.startswith(b"\n"):
            encoded = encoded[1:]
        try:
            text = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise SourceManagerError(
                "Git path output is not valid UTF-8",
                stage="fetch.github",
            ) from exc
        relative = PurePosixPath(text)
        _validate_git_relative_path(relative)
        values.add(relative)
    return values


def _validate_git_relative_path(path: PurePosixPath) -> None:
    text = path.as_posix()
    if (
        not text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.casefold() == ".git" for part in path.parts)
        or "\\" in text
        or any(ord(character) < 0x20 for character in text)
    ):
        raise SourceManagerError(
            "Git returned an unsafe repository path",
            stage="fetch.github",
        )


def _prune_empty_directories(work: Path, *, execution: Any) -> None:
    for directory, _children, _files in os.walk(
        work,
        topdown=False,
        followlinks=False,
    ):
        candidate = Path(directory)
        if candidate == work:
            continue
        metadata = os.lstat(candidate)
        if (
            execution._is_link_or_reparse(candidate, metadata)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise SourceManagerError(
                "Git checkout contains an unsafe directory",
                stage="fetch.github",
            )
        try:
            candidate.rmdir()
        except OSError:
            pass


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
        module._PROVIDER_JA[_GIT_SOURCE_TYPE] = "Gitリポジトリ"

    original_print_menu = manager_class._print_menu
    original_show = manager_class._show_source_fetch_settings
    original_edit = manager_class._edit_source_fetch_settings
    original_failure_label = manager_class._source_failure_stage_label

    @functools.wraps(original_print_menu)
    def print_menu(self: Any, title: Any, options: Any, *args: Any, **kwargs: Any):
        updated = []
        for item in options:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                values = list(item)
                if str(values[1]) == "GitHubリポジトリ":
                    values[1] = "Gitリポジトリ（GitHub・GitLab等）"
                item = tuple(values) if isinstance(item, tuple) else values
            updated.append(item)
        return original_print_menu(self, title, tuple(updated), *args, **kwargs)

    @functools.wraps(original_show)
    def show_source_fetch_settings(
        self: Any,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        if self._ui_source_type(source.get("source_type")) != _GIT_SOURCE_TYPE:
            return original_show(self, source)
        fetch = source.get("fetch")
        if not isinstance(fetch, dict):
            fetch = source.get("provider_settings")
        normalized = dict(fetch) if isinstance(fetch, dict) else {}
        normalized.setdefault("include_paths", [])
        normalized.setdefault("updated_within_days", None)
        display = copy.deepcopy(source)
        display["fetch"] = normalized
        original_show(self, display)
        include_paths = [str(value) for value in normalized.get("include_paths") or []]
        if include_paths:
            self.output("Git取得範囲: 指定フォルダのみ（sparse checkout）")
            self.output("Git取得フォルダ: " + ", ".join(include_paths))
        else:
            self.output("Git取得範囲: リポジトリ全体")
        self.output(
            "Git日時基準: 既定ブランチ上の各ファイルの最終コミット日時"
        )
        return normalized

    @functools.wraps(original_edit)
    def edit_source_fetch_settings(
        self: Any,
        db_name: str,
        source: dict[str, Any],
    ) -> None:
        if self._ui_source_type(source.get("source_type")) != _GIT_SOURCE_TYPE:
            return original_edit(self, db_name, source)
        fetch = self._show_source_fetch_settings(source)
        local_key = str(source.get("_local_source_key") or "")
        if not local_key:
            self._print_info("このSourceには変更できる取得設定がありません。")
            return
        repository_url = self._prompt_preserving_value(
            "Gitリポジトリの取得URL",
            str(fetch.get("repository_url") or ""),
            required=True,
            description=(
                "GitHub、GitLab、GitHub Enterpriseなどのclone URLです。"
                "HTTP(S)、SSH、git@host:path形式を利用できます。"
            ),
            examples=self._examples("github_repository_clone_url"),
        )
        if repository_url is None:
            return
        current_paths = [str(value) for value in fetch.get("include_paths") or []]
        scope = self._select_value(
            "取り込む範囲",
            (
                ("all", "リポジトリ全体"),
                ("partial", "指定フォルダのみ（sparse checkout）"),
            ),
            default="partial" if current_paths else "all",
        )
        if scope is None:
            return
        include_paths: list[str] = []
        if scope == "partial":
            raw_paths = self._prompt_preserving_value(
                "リポジトリ内フォルダ（カンマ区切り）",
                ", ".join(current_paths),
                required=True,
                description=(
                    "リポジトリrootからの相対フォルダです。"
                    "例: docs, specifications/api"
                ),
                examples=self._examples("git_repository_path_prefix"),
            )
            if raw_paths is None:
                return
            include_paths = _parse_include_path_input(raw_paths)
            if not include_paths:
                self._print_error("少なくとも1つのフォルダを入力してください。")
                return
        days = _prompt_edit_days(self, fetch.get("updated_within_days"))
        if days is _CANCELLED:
            return
        updated = dict(fetch)
        updated.update(
            {
                "repository_url": repository_url,
                "include_paths": include_paths,
                "updated_within_days": days,
            }
        )
        self.output("\n変更後の取得設定")
        self.output(f"Gitリポジトリの取得URL: {repository_url}")
        self.output(
            "取得範囲: "
            + (
                ", ".join(include_paths)
                if include_paths
                else "リポジトリ全体"
            )
        )
        self.output(
            "取得期間: "
            + ("制限なし" if days is None else f"過去{days}日")
        )
        if not self._confirm("この内容で取得設定を保存しますか？"):
            self._print_info("取得設定は変更されていません。")
            return
        try:
            from source_manager.runner import update_source_configuration

            update_source_configuration(
                self._database_root(db_name),
                local_key,
                fetch=updated,
            )
        except Exception as exc:
            self._print_internal_diagnostic(
                exc,
                operation="Git取得設定の保存",
                stage="source_config.git.save",
                db_name=db_name,
                source_name=str(source.get("display_name") or ""),
                source_key=local_key,
                provider=_GIT_SOURCE_TYPE,
                can_resume=True,
            )
            return
        self._print_success("Git取得設定を保存しました。")

    @staticmethod
    def source_failure_stage_label(value: Any) -> str:
        stage = str(value or "").strip()
        if stage.startswith("fetch.github"):
            return "Gitリポジトリの取得"
        return original_failure_label(value)

    manager_class._print_menu = print_menu
    manager_class._show_source_fetch_settings = show_source_fetch_settings
    manager_class._edit_source_fetch_settings = edit_source_fetch_settings
    manager_class._source_failure_stage_label = source_failure_stage_label
    manager_class._prompt_new_github_source = prompt_new_git_source
    setattr(manager_class, _MANAGER_CLASS_MARKER, True)


def prompt_new_git_source(self: Any) -> dict[str, Any] | None:
    self.output(
        "\n[1/4] Gitリポジトリの取得URL【必須】\n"
        "GitHub、GitLab、GitHub Enterpriseなどのclone URLを指定します。"
    )
    url = self._prompt_preserving_value(
        "URL",
        "",
        required=True,
        description="HTTP(S)、SSH、git@host:path形式を利用できます。",
        examples=self._examples("github_repository_clone_url"),
    )
    if url is None:
        return None
    scope = self._select_value(
        "取り込む範囲",
        (
            ("all", "リポジトリ全体【既定】"),
            ("partial", "指定フォルダのみ（sparse checkout）"),
        ),
        default="all",
    )
    if scope is None:
        return None
    include_paths: list[str] = []
    if scope == "partial":
        raw_paths = self._prompt_preserving_value(
            "リポジトリ内フォルダ（カンマ区切り）",
            "",
            required=True,
            description=(
                "リポジトリrootからの相対フォルダです。"
                "複数指定例: docs, specifications/api"
            ),
            examples=self._examples("git_repository_path_prefix"),
        )
        if raw_paths is None:
            return None
        include_paths = _parse_include_path_input(raw_paths)
        if not include_paths:
            self._print_error("少なくとも1つのフォルダを入力してください。")
            return None
    days = _prompt_new_days(self)
    if days is _CANCELLED:
        return None
    proposed = _repository_name_from_url(url)
    self.output(
        "\n[4/4] Sourceの名前【必須】\n"
        f"リポジトリ名から「{proposed}」を提案しました。"
    )
    name = self._prompt_preserving_value(
        "Sourceの名前",
        proposed,
        required=True,
        examples=self._examples("github_source_display_name"),
    )
    if name is None:
        return None
    return {
        "source_type": _GIT_SOURCE_TYPE,
        "label": "Gitリポジトリ",
        "display_name": name,
        "fetch": {
            "repository_url": url,
            "include_paths": include_paths,
            "updated_within_days": days,
        },
        "summary": (
            (
                "取得範囲",
                ", ".join(include_paths)
                if include_paths
                else "リポジトリ全体",
            ),
            ("Branch", "remoteの既定branch"),
            (
                "更新日時",
                "制限なし"
                if days is None
                else f"各ファイルの最終コミットが過去{days}日以内",
            ),
            ("作業場所", "DB内でLocal RAGが管理"),
        ),
    }


def _repository_name_from_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if text.casefold().endswith(".git"):
        text = text[:-4]
    candidate = re.split(r"[/\\:]", text)[-1].strip()
    return candidate or "git-source"


def _parse_include_path_input(value: Any) -> list[str]:
    text = str(value or "")
    return [
        item.strip()
        for item in re.split(r"[,、;；\r\n]+", text)
        if item.strip()
    ]


_CANCELLED = object()


def _prompt_new_days(self: Any) -> int | None | object:
    period = self._select_value(
        "どこまでさかのぼって取得しますか？（ファイルの最終コミット日時）",
        (
            ("1", "過去1年"),
            ("2", "過去90日"),
            ("3", "過去30日"),
            ("4", "期間を指定"),
            ("5", "制限しない【既定】"),
        ),
        default="5",
    )
    if period is None:
        return _CANCELLED
    if period in {"1", "2", "3"}:
        return {"1": 365, "2": 90, "3": 30}[period]
    if period == "5":
        return None
    raw = self._prompt_preserving_value(
        "日数",
        "",
        required=True,
        description="1～3650の日数を入力します。",
        examples=self._examples("svn_days"),
    )
    if raw is None:
        return _CANCELLED
    try:
        days = int(raw)
    except ValueError:
        self._print_error("日数は1～3650の整数で入力してください。")
        return _CANCELLED
    if not 1 <= days <= 3650:
        self._print_error("日数は1～3650の整数で入力してください。")
        return _CANCELLED
    return days


def _prompt_edit_days(self: Any, current: Any) -> int | None | object:
    raw = self._prompt_preserving_value(
        "取得期間（日）",
        "" if current is None else str(current),
        required=False,
        description=(
            "既定ブランチ上の各ファイルの最終コミット日時を基準にします。"
            "空欄は現在値を維持し、- は制限なしです。"
        ),
        examples=self._examples("svn_days"),
        empty_help="制限なし",
    )
    if raw is None:
        return _CANCELLED
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        self._print_error("取得期間は1～3650の整数で入力してください。")
        return _CANCELLED
    if not 1 <= days <= 3650:
        self._print_error("取得期間は1～3650の整数で入力してください。")
        return _CANCELLED
    return days


__all__ = [
    "git_updated_on_cutoff",
    "install_git_source_runtime",
]
