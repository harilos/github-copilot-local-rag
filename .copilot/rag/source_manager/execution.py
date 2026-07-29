from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import SourceManagerError, sanitize_diagnostic
from .security import validate_environment_name


CommandRunner = Callable[..., Any]
HttpGet = Callable[
    [str, Mapping[str, str], float],
    tuple[int, bytes] | tuple[int, bytes, Mapping[str, str]],
]
ProgressCallback = Callable[[int, int], None]
InventoryCallback = Callable[[list[int]], None]


def execute_fetch_plan(
    plan: Mapping[str, Any],
    work_directory: Path,
    state: Mapping[str, Any],
    *,
    command_runner: CommandRunner | None = None,
    http_get: HttpGet | None = None,
    environment: Mapping[str, str] | None = None,
    item_callback: ProgressCallback | None = None,
    batch_callback: ProgressCallback | None = None,
    resume_count: int = 0,
    stable_issue_ids: list[int] | None = None,
    inventory_callback: InventoryCallback | None = None,
) -> dict[str, Any]:
    runner = command_runner or _run_command
    getter = http_get or _http_get
    env = os.environ if environment is None else environment
    provider = str(plan.get("provider") or "")
    step = dict((plan.get("steps") or [{}])[0])
    parameters = dict(step.get("parameters") or {})
    work = Path(work_directory)
    if not work.is_dir() or work.is_symlink():
        raise SourceManagerError("work directory is unsafe")
    if provider == "github":
        return _github(parameters, work, runner)
    if provider == "svn":
        return _svn(parameters, work, runner)
    if provider == "redmine":
        return _redmine(
            parameters,
            work,
            getter,
            env,
            item_callback=item_callback,
            batch_callback=batch_callback,
            resume_count=resume_count,
            stable_issue_ids=stable_issue_ids,
            inventory_callback=inventory_callback,
        )
    if provider == "sharepoint":
        if not _is_windows():
            raise SourceManagerError(
                "SharePoint Source updates require Windows"
            )
        name = validate_environment_name(
            parameters.get("root_env"),
            field="root_env",
        )
        root = env.get(name)
        if not root:
            raise SourceManagerError("SharePoint environment root is unavailable")
        source = Path(root)
        relative = str(parameters.get("relative_path") or "")
        if relative:
            source = source.joinpath(*relative.split("/"))
        validate_external_add_root(source)
        return {
            "status": "ok",
            "documents": _regular_file_count(source),
            "external_add_root": str(source),
        }
    if provider == "other":
        runtime = state.get("runtime")
        value = runtime.get("input_path") if isinstance(runtime, dict) else None
        if not value or str(value).startswith("<"):
            raise SourceManagerError("Other runtime input is unavailable")
        return _materialize_snapshot(Path(str(value)), work)
    raise SourceManagerError("unsupported fetch provider")


def _github(
    settings: dict[str, Any],
    work: Path,
    runner: CommandRunner,
) -> dict[str, Any]:
    repository = str(settings["repository_url"])
    control = work.parent.parent / "provider" / ".git"
    _ensure_real_directory(control.parent)
    if control.is_dir() and not control.is_symlink():
        _checked(
            runner(
                [
                    "git", f"--git-dir={control}",
                    f"--work-tree={work}", "fetch", "--prune", "origin",
                ]
            ),
            operation="Gitの更新",
            stage="fetch.github",
        )
    else:
        if any(work.iterdir()):
            raise SourceManagerError("Git work directory is not empty")
        if control.exists() or control.is_symlink():
            raise SourceManagerError("Git control directory is unsafe")
        _checked(
            runner(
                [
                    "git", "clone", "--no-checkout",
                    f"--separate-git-dir={control}",
                    "--", repository, str(work),
                ]
            ),
            operation="Gitリポジトリの取得",
            stage="fetch.github",
        )
        pointer = work / ".git"
        if pointer.exists() or pointer.is_symlink():
            pointer_metadata = os.lstat(pointer)
            if (
                _is_link_or_reparse(pointer, pointer_metadata)
                or not stat.S_ISREG(pointer_metadata.st_mode)
            ):
                raise SourceManagerError("Git metadata pointer is unsafe")
            pointer.unlink()
    branch_result = _checked(
        runner(
            [
                "git", f"--git-dir={control}", "symbolic-ref", "--short",
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
        raise SourceManagerError("remote default branch is unavailable")
    remote_branch = f"refs/remotes/origin/{branch}"
    _checked(
        runner(
            [
                "git", f"--git-dir={control}", f"--work-tree={work}",
                "reset", "--hard", remote_branch,
            ]
        ),
        operation="Git作業ファイルの反映",
        stage="fetch.github",
    )
    _checked(
        runner(
            [
                "git", f"--git-dir={control}", f"--work-tree={work}",
                "clean", "-ffdqx",
            ]
        ),
        operation="Git作業ファイルの整理",
        stage="fetch.github",
    )
    return {"status": "ok", "default_branch": branch, "documents": 0}


def _svn(
    settings: dict[str, Any],
    work: Path,
    runner: CommandRunner,
) -> dict[str, Any]:
    depth = "infinity" if settings.get("recursive", True) else "files"
    checkout = work.parent.parent / "provider" / ".svn-worktree"
    _ensure_real_directory(checkout.parent)
    metadata = checkout / ".svn"
    if metadata.is_dir() and not metadata.is_symlink():
        _checked(
            runner(
                [
                    "svn", "update", "--set-depth", depth, str(checkout),
                ]
            ),
            operation="SVNの更新",
            stage="fetch.svn",
        )
    else:
        if checkout.exists() and any(checkout.iterdir()):
            raise SourceManagerError("SVN control directory is unsafe")
        _checked(
            runner(
                [
                    "svn", "checkout", "--depth", depth,
                    str(settings["repository_url"]), str(checkout),
                ]
            ),
            operation="SVNリポジトリの取得",
            stage="fetch.svn",
        )
    if settings.get("recursive", True):
        _replace_materialized_tree(checkout, work)
    else:
        _materialize_direct_files(checkout, work)
    revision_result = _checked(
        runner(
            [
                "svn", "info", "--show-item", "revision", str(checkout),
            ]
        ),
        operation="SVNリビジョンの確認",
        stage="fetch.svn",
    )
    revision = str(getattr(revision_result, "stdout", "") or "").strip()
    return {
        "status": "ok",
        "documents": _regular_file_count(work),
        "revision": revision,
    }


def _redmine(
    settings: dict[str, Any],
    work: Path,
    getter: HttpGet,
    environment: Mapping[str, str],
    *,
    item_callback: ProgressCallback | None,
    batch_callback: ProgressCallback | None,
    resume_count: int,
    stable_issue_ids: list[int] | None,
    inventory_callback: InventoryCallback | None,
) -> dict[str, Any]:
    api_key = environment.get(str(settings["api_key_env"]))
    if not api_key:
        raise SourceManagerError("Redmine API credential environment is unavailable")
    offset = 0
    written = 0
    issues_directory = work / "issues"
    issues_directory.mkdir(parents=True, exist_ok=True)
    ordered_issue_ids: list[int] = list(stable_issue_ids or [])
    inventory_seen: set[int] = set(ordered_issue_ids)
    expected_total: int | None = (
        len(ordered_issue_ids) if stable_issue_ids is not None else None
    )
    inventory_changed = False
    while stable_issue_ids is None:
        query_parameters: dict[str, Any] = {
            "project_id": settings["project_id"],
            "status_id": "*",
            "limit": 5,
            "offset": offset,
            "sort": "updated_on:asc,id:asc",
        }
        updated_within_days = settings.get("updated_within_days")
        if updated_within_days is not None:
            query_parameters["updated_on"] = (
                f">=-{int(updated_within_days)}d"
            )
        query = urllib.parse.urlencode(query_parameters)
        url = f"{settings['base_url']}/issues.json?{query}"
        status, body = _get_with_retry(
            getter,
            url,
            {"X-Redmine-API-Key": api_key},
        )
        if status != 200:
            raise SourceManagerError("Redmine request failed")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceManagerError("Redmine response is invalid") from exc
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if not isinstance(issues, list):
            raise SourceManagerError("Redmine response has no issues")
        issue_ids = [
            int(issue["id"])
            for issue in issues
            if isinstance(issue, dict)
            and isinstance(issue.get("id"), int)
        ]
        for issue_id in issue_ids:
            if issue_id in inventory_seen:
                inventory_changed = True
                continue
            inventory_seen.add(issue_id)
            ordered_issue_ids.append(issue_id)
        offset += len(issues)
        total = int(payload.get("total_count") or offset)
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            inventory_changed = True
        if not issues or offset >= total:
            break
    if (
        stable_issue_ids is None
        and (
            inventory_changed
            or expected_total is None
            or len(ordered_issue_ids) != expected_total
        )
    ):
        raise SourceManagerError("redmine_inventory_changed")
    if stable_issue_ids is None and inventory_callback is not None:
        inventory_callback(list(ordered_issue_ids))

    if resume_count < 0 or resume_count > len(ordered_issue_ids):
        raise SourceManagerError("Redmine resume checkpoint is invalid")
    last_reflected_count = int(resume_count)
    for position, issue_id in enumerate(ordered_issue_ids, start=1):
        if position <= resume_count:
            continue
        detail_query = urllib.parse.urlencode(
            {"include": "journals,relations,attachments"}
        )
        detail_url = (
            f"{settings['base_url']}/issues/{issue_id}.json?"
            f"{detail_query}"
        )
        detail_status, detail_body = _get_with_retry(
            getter,
            detail_url,
            {"X-Redmine-API-Key": api_key},
        )
        if detail_status != 200:
            raise SourceManagerError("Redmine issue detail request failed")
        try:
            detail_payload = json.loads(detail_body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceManagerError(
                "Redmine issue detail response is invalid"
            ) from exc
        issue = (
            detail_payload.get("issue")
            if isinstance(detail_payload, dict)
            else None
        )
        if not isinstance(issue, dict) or int(issue.get("id") or 0) != issue_id:
            raise SourceManagerError(
                "Redmine issue detail response has the wrong identity"
            )
        target = issues_directory / f"{issue_id}.md"
        target.write_text(
            _redmine_issue_markdown(issue),
            encoding="utf-8",
        )
        written += 1
        if item_callback is not None:
            item_callback(position, issue_id)
        if (
            batch_callback is not None
            and position - last_reflected_count >= 5
            and position < len(ordered_issue_ids)
        ):
            batch_callback(position, issue_id)
            last_reflected_count = position
    # Previously fetched Issue files are intentionally retained when a later
    # run uses a shorter updated-on window. The stable tree is additive/update
    # only; Source Manager never interprets an unseen Issue as deleted.
    if (
        batch_callback is not None
        and len(ordered_issue_ids) > last_reflected_count
    ):
        last_issue_id = ordered_issue_ids[-1]
        batch_callback(len(ordered_issue_ids), last_issue_id)
        last_reflected_count = len(ordered_issue_ids)
    return {
        "status": "ok",
        "documents": len(ordered_issue_ids),
        "fetched_this_run": written,
        "last_completed_item": (
            ordered_issue_ids[-1] if ordered_issue_ids else None
        ),
    }


def _get_with_retry(
    getter: HttpGet,
    url: str,
    headers: Mapping[str, str],
) -> tuple[int, bytes]:
    retryable = {429, 502, 503, 504}
    for attempt in range(1, 4):
        try:
            response = getter(url, headers, 10.0)
            status, body = int(response[0]), response[1]
            response_headers = (
                response[2]
                if len(response) > 2 and isinstance(response[2], Mapping)
                else {}
            )
        except (TimeoutError, OSError):
            if attempt >= 3:
                raise SourceManagerError("Redmine request failed")
            continue
        if status not in retryable or attempt >= 3:
            return status, body
        retry_after = str(response_headers.get("Retry-After") or "").strip()
        delay = (
            min(float(retry_after), 5.0)
            if retry_after.replace(".", "", 1).isdigit()
            else 0.1 * attempt
        )
        time.sleep(max(0.0, delay))
    raise SourceManagerError("Redmine request failed")


def _copy_tree(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_absolute():
        raise SourceManagerError("runtime input root is unsafe")
    _reject_linked_path_components(source)
    try:
        source_metadata = os.lstat(source)
    except OSError as exc:
        raise SourceManagerError("runtime input root is unsafe") from exc
    if _is_link_or_reparse(source, source_metadata):
        raise SourceManagerError("runtime input root is unsafe")
    if stat.S_ISREG(source_metadata.st_mode):
        target = destination / source.name
        _copy_regular_file(source, target)
        return {"status": "ok", "documents": 1}
    if not stat.S_ISDIR(source_metadata.st_mode):
        raise SourceManagerError("runtime input root is unsafe")
    copied = 0
    for directory, child_names, file_names in os.walk(
        source,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        retained: list[str] = []
        for name in sorted(child_names):
            candidate = directory_path / name
            candidate_metadata = os.lstat(candidate)
            if _is_link_or_reparse(candidate, candidate_metadata):
                raise SourceManagerError("runtime input must not contain links")
            if not stat.S_ISDIR(candidate_metadata.st_mode):
                raise SourceManagerError(
                    "runtime input must contain only regular files"
                )
            if name.casefold() not in {".git", ".svn"}:
                retained.append(name)
        child_names[:] = retained
        relative_directory = directory_path.relative_to(source)
        target_directory = destination / relative_directory
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in sorted(file_names):
            candidate = directory_path / name
            target = target_directory / name
            _copy_regular_file(candidate, target)
            copied += 1
    return {"status": "ok", "documents": copied}


def _materialize_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    """Publish a fully validated local snapshot without merge-only leftovers."""
    parent = destination.parent
    _ensure_real_directory(parent)
    staging = parent / f".incoming-{uuid.uuid4().hex}"
    backup = parent / f".previous-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        outcome = _copy_tree(source, staging)
        validate_managed_work_tree(staging)
        validate_managed_work_tree(destination)
        os.replace(destination, backup)
        try:
            os.replace(staging, destination)
        except Exception:
            os.replace(backup, destination)
            raise
        shutil.rmtree(backup)
        return outcome
    finally:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)


def _replace_materialized_tree(source: Path, destination: Path) -> None:
    validate_managed_work_tree(destination)
    for candidate in list(destination.iterdir()):
        metadata = os.lstat(candidate)
        if _is_link_or_reparse(candidate, metadata):
            raise SourceManagerError("managed work contains an unsafe link")
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(candidate)
        elif stat.S_ISREG(metadata.st_mode):
            candidate.unlink()
        else:
            raise SourceManagerError("managed work contains a special file")
    _copy_tree(source, destination)


def _materialize_direct_files(source: Path, destination: Path) -> None:
    """Refresh only Source-root files; preserve prior child-tree documents."""
    validate_managed_work_tree(destination)
    direct_names: set[str] = set()
    for candidate in sorted(source.iterdir()):
        if candidate.name.casefold() == ".svn":
            continue
        metadata = os.lstat(candidate)
        if _is_link_or_reparse(candidate, metadata):
            raise SourceManagerError("SVN checkout contains an unsafe link")
        if stat.S_ISREG(metadata.st_mode):
            direct_names.add(candidate.name)
            _copy_regular_file(candidate, destination / candidate.name)
        elif not stat.S_ISDIR(metadata.st_mode):
            raise SourceManagerError("SVN checkout contains a special file")
    for candidate in list(destination.iterdir()):
        metadata = os.lstat(candidate)
        if stat.S_ISREG(metadata.st_mode) and candidate.name not in direct_names:
            candidate.unlink()


def _regular_file_count(root: Path) -> int:
    return sum(
        1
        for directory, _children, files in os.walk(root, followlinks=False)
        for name in files
        if stat.S_ISREG(os.lstat(Path(directory) / name).st_mode)
    )


def validate_managed_work_tree(work: Path) -> None:
    """Reject links, reparse points, special files, and VCS metadata."""
    root = Path(work)
    metadata = os.lstat(root)
    if _is_link_or_reparse(root, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise SourceManagerError("managed work directory is unsafe")
    for directory, child_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        for name in sorted(child_names):
            candidate = directory_path / name
            child_metadata = os.lstat(candidate)
            if (
                _is_link_or_reparse(candidate, child_metadata)
                or not stat.S_ISDIR(child_metadata.st_mode)
            ):
                raise SourceManagerError(
                    "managed work must not contain links or special files"
                )
            if name.casefold() in {".git", ".svn"}:
                raise SourceManagerError(
                    "managed work must not contain VCS metadata"
                )
        for name in sorted(file_names):
            candidate = directory_path / name
            file_metadata = os.lstat(candidate)
            if (
                _is_link_or_reparse(candidate, file_metadata)
                or not stat.S_ISREG(file_metadata.st_mode)
            ):
                raise SourceManagerError(
                    "managed work must not contain links or special files"
                )


def validate_external_add_root(root: Path) -> None:
    """Validate a synchronized external tree without copying or mutating it."""
    candidate = Path(root)
    if not candidate.is_absolute():
        raise SourceManagerError("external ADD root must be absolute")
    _reject_linked_path_components(candidate)
    validate_managed_work_tree(candidate)


def _copy_regular_file(source: Path, target: Path) -> None:
    metadata = os.lstat(source)
    if (
        _is_link_or_reparse(source, metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise SourceManagerError(
            "runtime input must contain only regular files"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)


def _ensure_real_directory(path: Path) -> None:
    current = Path(path)
    if current.exists() or current.is_symlink():
        metadata = os.lstat(current)
        if _is_link_or_reparse(current, metadata) or not stat.S_ISDIR(
            metadata.st_mode
        ):
            raise SourceManagerError("provider control path is unsafe")
        return
    parent = current.parent
    if parent != current:
        _ensure_real_directory(parent)
    current.mkdir(mode=0o700)


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
        or (hasattr(path, "is_junction") and path.is_junction())
    )


def _reject_linked_path_components(path: Path) -> None:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise SourceManagerError("runtime path must be absolute")
    parts = candidate.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        metadata = os.lstat(current)
        if _is_link_or_reparse(current, metadata):
            raise SourceManagerError(
                "runtime path components must not be links"
            )


def _is_windows() -> bool:
    return os.name == "nt"


def _checked(
    result: Any,
    *,
    operation: str,
    stage: str,
) -> Any:
    returncode = int(getattr(result, "returncode", 1))
    if returncode != 0:
        stderr = sanitize_diagnostic(
            getattr(result, "stderr", ""),
            max_chars=4_000,
        )
        stdout = sanitize_diagnostic(
            getattr(result, "stdout", ""),
            max_chars=2_000,
        )
        details: list[str] = []
        if stderr:
            details.append(f"標準エラー:\n{stderr}")
        if stdout:
            details.append(f"標準出力:\n{stdout}")
        suffix = "\n" + "\n".join(details) if details else ""
        raise SourceManagerError(
            f"{operation}に失敗しました（終了コード: {returncode}）。"
            f"{suffix}",
            stage=stage,
        )
    return result


def _run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def _http_get(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[int, bytes, Mapping[str, str]]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(), dict(exc.headers or {})


def _redmine_issue_markdown(issue: Mapping[str, Any]) -> str:
    issue_id = int(issue["id"])
    subject = str(issue.get("subject") or "").strip()
    description = str(issue.get("description") or "").strip()
    structured = json.dumps(
        dict(issue),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return (
        f"# Issue {issue_id}: {subject}\n\n"
        f"{description}\n\n"
        "## Structured issue metadata\n\n"
        "```json\n"
        f"{structured}\n"
        "```\n"
    )
