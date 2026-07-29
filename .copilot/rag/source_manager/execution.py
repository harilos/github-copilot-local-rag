from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import (
    SourceManagerError,
    exception_summary,
    sanitize_diagnostic,
)
from .diagnostics import process_diagnostic
from .redmine import parse_redmine_project_url, redmine_updated_on_cutoff
from .security import validate_environment_name


CommandRunner = Callable[..., Any]
HttpGet = Callable[
    [str, Mapping[str, str], float],
    tuple[int, bytes] | tuple[int, bytes, Mapping[str, str]],
]
ProgressCallback = Callable[[int, int], None]
InventoryCallback = Callable[[list[int]], None]
HttpProgressCallback = Callable[[Mapping[str, Any]], None]


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
    clock: Callable[[], datetime] | None = None,
    progress_callback: HttpProgressCallback | None = None,
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
    _emit_provider_progress(progress_callback, provider, "started")
    try:
        if provider == "github":
            result = _github(
                parameters,
                work,
                runner,
                progress_callback=progress_callback,
            )
        elif provider == "svn":
            result = _svn(
                parameters,
                work,
                runner,
                progress_callback=progress_callback,
            )
        elif provider == "redmine":
            cutoff = redmine_updated_on_cutoff(
                parameters.get("updated_within_days"),
                state,
                clock=clock,
            )
            result = _redmine(
                parameters,
                work,
                getter,
                env,
                item_callback=item_callback,
                batch_callback=batch_callback,
                resume_count=resume_count,
                stable_issue_ids=stable_issue_ids,
                inventory_callback=inventory_callback,
                updated_on_cutoff=cutoff,
                progress_callback=progress_callback,
            )
        elif provider == "sharepoint":
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
                raise SourceManagerError(
                    "SharePoint environment root is unavailable"
                )
            source = Path(root)
            relative = str(parameters.get("relative_path") or "")
            if relative:
                source = source.joinpath(*relative.split("/"))
            validate_external_add_root(source)
            result = {
                "status": "ok",
                "documents": _regular_file_count(
                    source,
                    progress_callback=progress_callback,
                    provider="sharepoint",
                ),
                "external_add_root": str(source),
            }
        elif provider == "other":
            runtime = state.get("runtime")
            value = (
                runtime.get("input_path")
                if isinstance(runtime, dict)
                else None
            )
            if not value or str(value).startswith("<"):
                raise SourceManagerError("Other runtime input is unavailable")
            result = _materialize_snapshot(
                Path(str(value)),
                work,
                progress_callback=progress_callback,
            )
        else:
            raise SourceManagerError("unsupported fetch provider")
    except Exception as exc:
        _emit_provider_progress(
            progress_callback,
            provider,
            "failed",
            error=exc,
        )
        raise
    _emit_provider_progress(
        progress_callback,
        provider,
        "completed",
        documents=int(result.get("documents") or 0),
    )
    return result


def _github(
    settings: dict[str, Any],
    work: Path,
    runner: CommandRunner,
    *,
    progress_callback: HttpProgressCallback | None = None,
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
    documents = _regular_file_count(
        work,
        progress_callback=progress_callback,
        provider="github",
    )
    return {
        "status": "ok",
        "default_branch": branch,
        "documents": documents,
    }


def _svn(
    settings: dict[str, Any],
    work: Path,
    runner: CommandRunner,
    *,
    progress_callback: HttpProgressCallback | None = None,
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
        _replace_materialized_tree(
            checkout,
            work,
            progress_callback=progress_callback,
            provider="svn",
        )
    else:
        _materialize_direct_files(
            checkout,
            work,
            progress_callback=progress_callback,
        )
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
    updated_on_cutoff: str | None,
    progress_callback: HttpProgressCallback | None,
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
    project = parse_redmine_project_url(settings.get("project_url"))
    inventory_changed = False
    while stable_issue_ids is None:
        query_parameters: dict[str, Any] = {
            "project_id": project.project_id,
            "status_id": "*",
            "limit": 5,
            "offset": offset,
            "sort": "updated_on:asc,id:asc",
        }
        if updated_on_cutoff is not None:
            query_parameters["updated_on"] = f">={updated_on_cutoff}"
        query = urllib.parse.urlencode(query_parameters)
        url = f"{project.issues_api_url}?{query}"
        response_diagnostic: dict[str, Any] = {}
        status, body = _get_with_retry(
            getter,
            url,
            {"X-Redmine-API-Key": api_key},
            progress_callback=progress_callback,
            response_diagnostic=response_diagnostic,
        )
        if status != 200:
            raise _redmine_http_error(
                _http_diagnostic(
                    url=url,
                    request_headers={},
                    timeout=10.0,
                    attempt=1,
                    max_attempts=1,
                    elapsed=0.0,
                    status=status,
                    reason=_http_reason(status),
                    retry=False,
                    wait=0.0,
                    body=body,
                    include_body=True,
                )
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _redmine_response_parse_error(
                url,
                body,
                exc,
                response_diagnostic=response_diagnostic,
            ) from exc
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if not isinstance(issues, list):
            raise _redmine_response_schema_error(
                url,
                body,
                "Redmine response has no issues",
                response_diagnostic=response_diagnostic,
            )
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
        _emit_http_progress(
            progress_callback,
            {
                "event": "provider.page",
                "provider": "redmine",
                "phase": "redmine.inventory",
                "label_ja": "Redmine Issue一覧取得",
                "completed": len(ordered_issue_ids),
                "total": total,
                "unit": "件",
                "total_kind": "exact",
                "current_item": f"offset={offset}",
                "status": "running",
            },
        )
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
        detail_url = f"{project.issue_api_url(issue_id)}?{detail_query}"
        response_diagnostic = {}
        detail_status, detail_body = _get_with_retry(
            getter,
            detail_url,
            {"X-Redmine-API-Key": api_key},
            progress_callback=progress_callback,
            response_diagnostic=response_diagnostic,
        )
        if detail_status != 200:
            raise SourceManagerError("Redmine issue detail request failed")
        try:
            detail_payload = json.loads(detail_body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _redmine_response_parse_error(
                detail_url,
                detail_body,
                exc,
                response_diagnostic=response_diagnostic,
            ) from exc
        issue = (
            detail_payload.get("issue")
            if isinstance(detail_payload, dict)
            else None
        )
        if not isinstance(issue, dict) or int(issue.get("id") or 0) != issue_id:
            raise _redmine_response_schema_error(
                detail_url,
                detail_body,
                "Redmine issue detail response has the wrong identity",
                response_diagnostic=response_diagnostic,
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
    *,
    progress_callback: HttpProgressCallback | None = None,
    response_diagnostic: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    retryable = {429, 502, 503, 504}
    max_attempts = 3
    timeout = 10.0
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        try:
            response = _call_http_with_heartbeat(
                getter,
                url,
                headers,
                timeout,
                progress_callback=progress_callback,
                attempt=attempt,
                max_attempts=max_attempts,
                started=started,
            )
            status, body = int(response[0]), response[1]
            response_headers = (
                response[2]
                if len(response) > 2 and isinstance(response[2], Mapping)
                else {}
            )
        except Exception as exc:
            error_kind = _network_error_kind(exc)
            retry = (
                error_kind in {
                    "connection_error",
                    "connection_timeout",
                    "dns_resolution_failed",
                }
                and attempt < max_attempts
            )
            wait = 0.1 * attempt if retry else 0.0
            diagnostic = _http_diagnostic(
                url=url,
                request_headers=headers,
                timeout=timeout,
                attempt=attempt,
                max_attempts=max_attempts,
                elapsed=time.monotonic() - started,
                error_kind=error_kind,
                reason=exception_summary(exc),
                retry=retry,
                wait=wait,
            )
            _emit_http_progress(progress_callback, diagnostic)
            if not retry:
                raise _redmine_http_error(diagnostic) from exc
            time.sleep(wait)
            continue
        reason = _http_reason(status)
        if status == 200:
            diagnostic = _http_diagnostic(
                url=url,
                request_headers=headers,
                timeout=timeout,
                attempt=attempt,
                max_attempts=max_attempts,
                elapsed=time.monotonic() - started,
                status=status,
                reason=reason,
                retry=False,
                wait=0.0,
                response_headers=response_headers,
                body=body,
                include_body=False,
            )
            if response_diagnostic is not None:
                response_diagnostic.clear()
                response_diagnostic.update(diagnostic)
            _emit_http_progress(
                progress_callback,
                diagnostic,
            )
            return status, body
        retry_after = _header_value(response_headers, "Retry-After")
        retry = status in retryable and attempt < max_attempts
        retry_after_delay = _retry_after_seconds(retry_after)
        delay = (
            retry_after_delay
            if retry_after_delay is not None
            else 0.1 * attempt
        )
        wait = max(0.0, delay) if retry else 0.0
        diagnostic = _http_diagnostic(
            url=url,
            request_headers=headers,
            timeout=timeout,
            attempt=attempt,
            max_attempts=max_attempts,
            elapsed=time.monotonic() - started,
            status=status,
            reason=reason,
            retry=retry,
            retry_after=retry_after or None,
            wait=wait,
            response_headers=response_headers,
            body=body,
            include_body=True,
        )
        _emit_http_progress(progress_callback, diagnostic)
        if not retry:
            raise _redmine_http_error(diagnostic)
        time.sleep(wait)
    raise SourceManagerError(
        "Redmine HTTP request failed",
        stage="fetch.redmine",
    )


def _http_diagnostic(
    *,
    url: str,
    request_headers: Mapping[str, Any],
    timeout: float,
    attempt: int,
    max_attempts: int,
    elapsed: float,
    status: int | None = None,
    reason: str | None = None,
    error_kind: str | None = None,
    retry: bool,
    retry_after: str | None = None,
    wait: float,
    response_headers: Mapping[str, Any] | None = None,
    body: bytes | None = None,
    include_body: bool = False,
) -> dict[str, Any]:
    response = response_headers or {}
    payload: dict[str, Any] = {
        "event": "redmine.http_attempt",
        "method": "GET",
        "url": sanitize_diagnostic(url, max_chars=4_096),
        "timeout_seconds": float(timeout),
        "attempt": int(attempt),
        "max_attempts": int(max_attempts),
        "elapsed_seconds": round(max(0.0, float(elapsed)), 6),
        "status": int(status) if status is not None else None,
        "reason": sanitize_diagnostic(reason or "", max_chars=2_000) or None,
        "error_kind": str(error_kind or "") or None,
        "retry": bool(retry),
        "retry_after": sanitize_diagnostic(
            retry_after or "",
            max_chars=200,
        )
        or None,
        "wait_seconds": round(max(0.0, float(wait)), 6),
        "content_type": sanitize_diagnostic(
            _header_value(response, "Content-Type"),
            max_chars=500,
        )
        or None,
        "request_headers": _sanitized_headers(request_headers),
        "response_headers": _sanitized_headers(response),
        "body_bytes": len(body or b""),
    }
    if include_body and body:
        preview = bytes(body[:65_536]).decode("utf-8", errors="replace")
        payload["body_preview"] = sanitize_diagnostic(
            preview,
            max_chars=65_536,
        )
        payload["body_truncated"] = len(body) > 65_536
    return payload


def _call_http_with_heartbeat(
    getter: HttpGet,
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    *,
    progress_callback: HttpProgressCallback | None,
    attempt: int,
    max_attempts: int,
    started: float,
) -> Any:
    if progress_callback is None:
        return getter(url, headers, timeout)
    result: list[Any] = []
    failure: list[BaseException] = []
    finished = threading.Event()

    def invoke() -> None:
        try:
            result.append(getter(url, headers, timeout))
        except BaseException as exc:
            failure.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    while not finished.wait(5.0):
        _emit_http_progress(
            progress_callback,
            {
                "event": "heartbeat",
                "phase": "redmine.http",
                "label_ja": "Redmine応答待ち",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "total_kind": "unknown",
            },
        )
    worker.join()
    if failure:
        raise failure[0]
    return result[0]


def _retry_after_seconds(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            return None
        return max(0.0, parsed.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _redmine_response_parse_error(
    url: str,
    body: bytes,
    cause: BaseException,
    *,
    response_diagnostic: Mapping[str, Any] | None = None,
) -> SourceManagerError:
    return _redmine_response_error(
        url,
        body,
        reason=f"response_parse_failed: {type(cause).__name__}: {cause}",
        error_kind="response_parse_failed",
        response_diagnostic=response_diagnostic,
    )


def _redmine_response_schema_error(
    url: str,
    body: bytes,
    reason: str,
    *,
    response_diagnostic: Mapping[str, Any] | None = None,
) -> SourceManagerError:
    return _redmine_response_error(
        url,
        body,
        reason=reason,
        error_kind="response_schema_invalid",
        response_diagnostic=response_diagnostic,
    )


def _redmine_response_error(
    url: str,
    body: bytes,
    *,
    reason: str,
    error_kind: str,
    response_diagnostic: Mapping[str, Any] | None,
) -> SourceManagerError:
    diagnostic = dict(response_diagnostic or {})
    if not diagnostic:
        diagnostic = _http_diagnostic(
            url=url,
            request_headers={},
            timeout=10.0,
            attempt=1,
            max_attempts=1,
            elapsed=0.0,
            status=200,
            reason=reason,
            error_kind=error_kind,
            retry=False,
            wait=0.0,
            response_headers={},
            body=body,
            include_body=True,
        )
    else:
        diagnostic.update(
            {
                "reason": sanitize_diagnostic(reason, max_chars=2_000),
                "error_kind": error_kind,
                "retry": False,
                "wait_seconds": 0.0,
                "body_bytes": len(body),
            }
        )
        if body:
            preview = bytes(body[:65_536]).decode(
                "utf-8",
                errors="replace",
            )
            diagnostic["body_preview"] = sanitize_diagnostic(
                preview,
                max_chars=65_536,
            )
            diagnostic["body_truncated"] = len(body) > 65_536
    return _redmine_http_error(diagnostic)


def _redmine_http_error(diagnostic: Mapping[str, Any]) -> SourceManagerError:
    encoded = json.dumps(
        dict(diagnostic),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    error = SourceManagerError(
        f"Redmine HTTP request failed: {encoded}",
        stage="fetch.redmine",
    )
    error.diagnostic = dict(diagnostic)
    error.error_kind = str(
        diagnostic.get("error_kind") or "http_status_error"
    )
    return error


def _network_error_kind(exc: BaseException) -> str:
    reason: BaseException = exc
    if isinstance(exc, urllib.error.URLError):
        nested = getattr(exc, "reason", None)
        if isinstance(nested, BaseException):
            reason = nested
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "connection_timeout"
    if isinstance(reason, socket.gaierror):
        return "dns_resolution_failed"
    if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
        return "tls_verification_failed"
    if isinstance(reason, OSError):
        return "connection_error"
    return "network_error"


def _http_reason(status: int) -> str:
    try:
        return HTTPStatus(int(status)).phrase
    except ValueError:
        return f"HTTP {int(status)}"


def _header_value(headers: Mapping[str, Any], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value or "").strip()
    return ""


def _sanitized_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in sorted(
        headers.items(),
        key=lambda item: str(item[0]).casefold(),
    )[:50]:
        name = sanitize_diagnostic(str(key), max_chars=200)
        if _is_secret_header_name(str(key)):
            result[name] = "<REDACTED>"
        else:
            result[name] = sanitize_diagnostic(
                value,
                max_chars=1_000,
            )
    return result


def _is_secret_header_name(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")
    parts = set(normalized.split("-"))
    if parts.intersection(
        {
            "auth",
            "authorization",
            "cookie",
            "credential",
            "credentials",
            "password",
            "passwd",
            "secret",
            "token",
        }
    ):
        return True
    return "api-key" in normalized or normalized.endswith("apikey")


def _emit_http_progress(
    callback: HttpProgressCallback | None,
    diagnostic: Mapping[str, Any],
) -> None:
    if callback is None:
        return
    try:
        callback(dict(diagnostic))
    except Exception:
        # Diagnostics must never change request/retry behavior.
        return


def _emit_provider_progress(
    callback: HttpProgressCallback | None,
    provider: str,
    status: str,
    *,
    documents: int | None = None,
    error: BaseException | None = None,
) -> None:
    payload: dict[str, Any] = {
        "event": "provider.fetch",
        "provider": str(provider),
        "phase": "fetch",
        "status": str(status),
    }
    if documents is not None:
        payload["documents"] = int(documents)
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["error"] = exception_summary(error)
    _emit_http_progress(callback, payload)


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    progress_callback: HttpProgressCallback | None = None,
    provider: str = "other",
) -> dict[str, Any]:
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
        _copy_regular_file(
            source,
            target,
            progress_callback=progress_callback,
            provider=provider,
        )
        _emit_file_progress(progress_callback, provider, 1, source.name)
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
            _copy_regular_file(
                candidate,
                target,
                progress_callback=progress_callback,
                provider=provider,
            )
            copied += 1
            _emit_file_progress(
                progress_callback,
                provider,
                copied,
                candidate.name,
            )
    return {"status": "ok", "documents": copied}


def _materialize_snapshot(
    source: Path,
    destination: Path,
    *,
    progress_callback: HttpProgressCallback | None = None,
) -> dict[str, Any]:
    """Publish a fully validated local snapshot without merge-only leftovers."""
    parent = destination.parent
    _ensure_real_directory(parent)
    staging = parent / f".incoming-{uuid.uuid4().hex}"
    backup = parent / f".previous-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        outcome = _copy_tree(
            source,
            staging,
            progress_callback=progress_callback,
            provider="other",
        )
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


def _replace_materialized_tree(
    source: Path,
    destination: Path,
    *,
    progress_callback: HttpProgressCallback | None = None,
    provider: str = "other",
) -> None:
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
    _copy_tree(
        source,
        destination,
        progress_callback=progress_callback,
        provider=provider,
    )


def _materialize_direct_files(
    source: Path,
    destination: Path,
    *,
    progress_callback: HttpProgressCallback | None = None,
) -> None:
    """Refresh only Source-root files; preserve prior child-tree documents."""
    validate_managed_work_tree(destination)
    direct_names: set[str] = set()
    copied = 0
    for candidate in sorted(source.iterdir()):
        if candidate.name.casefold() == ".svn":
            continue
        metadata = os.lstat(candidate)
        if _is_link_or_reparse(candidate, metadata):
            raise SourceManagerError("SVN checkout contains an unsafe link")
        if stat.S_ISREG(metadata.st_mode):
            direct_names.add(candidate.name)
            _copy_regular_file(
                candidate,
                destination / candidate.name,
                progress_callback=progress_callback,
                provider="svn",
            )
            copied += 1
            _emit_file_progress(
                progress_callback,
                "svn",
                copied,
                candidate.name,
            )
        elif not stat.S_ISDIR(metadata.st_mode):
            raise SourceManagerError("SVN checkout contains a special file")
    for candidate in list(destination.iterdir()):
        metadata = os.lstat(candidate)
        if stat.S_ISREG(metadata.st_mode) and candidate.name not in direct_names:
            candidate.unlink()


def _regular_file_count(
    root: Path,
    *,
    progress_callback: HttpProgressCallback | None = None,
    provider: str = "",
) -> int:
    count = 0
    for directory, _children, files in os.walk(root, followlinks=False):
        for name in files:
            if stat.S_ISREG(os.lstat(Path(directory) / name).st_mode):
                count += 1
                _emit_file_progress(
                    progress_callback,
                    provider,
                    count,
                    name,
                )
    return count


def _emit_file_progress(
    callback: HttpProgressCallback | None,
    provider: str,
    completed: int,
    current_item: str,
) -> None:
    _emit_http_progress(
        callback,
        {
            "event": "provider.file",
            "provider": provider,
            "phase": f"{provider}.files",
            "label_ja": "対象ファイル処理",
            "completed": completed,
            "unit": "件",
            "total_kind": "unknown",
            "current_item": current_item,
            "status": "running",
        },
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


def _copy_regular_file(
    source: Path,
    target: Path,
    *,
    progress_callback: HttpProgressCallback | None = None,
    provider: str = "",
) -> None:
    metadata = os.lstat(source)
    if (
        _is_link_or_reparse(source, metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise SourceManagerError(
            "runtime input must contain only regular files"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if progress_callback is None:
        shutil.copy2(source, target, follow_symlinks=False)
        return
    copied = 0
    total = int(metadata.st_size)
    with source.open("rb") as input_stream, target.open("wb") as output_stream:
        while True:
            chunk = input_stream.read(1024 * 1024)
            if not chunk:
                break
            output_stream.write(chunk)
            copied += len(chunk)
            _emit_http_progress(
                progress_callback,
                {
                    "event": "provider.bytes",
                    "provider": provider,
                    "phase": f"{provider}.copy",
                    "label_ja": "ファイルコピー",
                    "completed": copied,
                    "total": total,
                    "unit": "bytes",
                    "total_kind": "exact",
                    "current_item": source.name,
                    "status": "running",
                },
            )
    shutil.copystat(source, target, follow_symlinks=False)


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
            max_chars=65_536,
        )
        stdout = sanitize_diagnostic(
            getattr(result, "stdout", ""),
            max_chars=65_536,
        )
        details: list[str] = []
        if stderr:
            details.append(f"標準エラー:\n{stderr}")
        if stdout:
            details.append(f"標準出力:\n{stdout}")
        suffix = "\n" + "\n".join(details) if details else ""
        error = SourceManagerError(
            f"{operation}に失敗しました（終了コード: {returncode}）。"
            f"{suffix}",
            stage=stage,
        )
        error.process_diagnostic = process_diagnostic(
            arguments=getattr(result, "args", (operation,)),
            cwd=os.getcwd(),
            returncode=returncode,
            elapsed_seconds=float(
                getattr(result, "elapsed_seconds", 0.0) or 0.0
            ),
            stdout=getattr(result, "stdout", ""),
            stderr=getattr(result, "stderr", ""),
        )
        raise error
    return result


def _run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    result = subprocess.run(
        arguments,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    result.elapsed_seconds = time.monotonic() - started
    return result


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
