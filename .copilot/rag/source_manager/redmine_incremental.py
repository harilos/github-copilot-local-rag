from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .errors import SourceManagerError
from .redmine import parse_redmine_project_url

_STRUCTURED_METADATA_HEADING = "## Structured issue metadata"
_INSTALLED = False


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local_issue_timestamp(path: Path) -> datetime | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        if not path.stem.isdecimal():
            return None
        issue_id = int(path.stem)
    except ValueError:
        return None
    payload = _structured_issue_payload(text)
    if payload is None:
        return None
    stored_id = payload.get("id")
    if (
        isinstance(stored_id, bool)
        or not isinstance(stored_id, int)
        or stored_id != issue_id
    ):
        return None
    return _parse_timestamp(payload.get("updated_on"))


def _structured_issue_payload(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return _json_object(stripped)

    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if line.strip() != _STRUCTURED_METADATA_HEADING:
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor >= len(lines) or lines[cursor].strip() != "```json":
            return None
        cursor += 1
        encoded: list[str] = []
        while cursor < len(lines) and lines[cursor].strip() != "```":
            encoded.append(lines[cursor])
            cursor += 1
        if cursor >= len(lines):
            return None
        return _json_object("\n".join(encoded))
    return None


def _json_object(value: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _inventory(
    *,
    settings: Mapping[str, Any],
    getter: Any,
    environment: Mapping[str, str],
    updated_on_cutoff: str | None,
    execution: Any,
    progress_callback: Any,
) -> list[tuple[int, datetime | None]]:
    api_key = environment.get(str(settings["api_key_env"]))
    if not api_key:
        raise SourceManagerError(
            "Redmine API credential environment is unavailable"
        )
    project = parse_redmine_project_url(settings.get("project_url"))
    offset = 0
    result: list[tuple[int, datetime | None]] = []
    seen_issue_ids: set[int] = set()
    expected_total: int | None = None
    while True:
        parameters: dict[str, Any] = {
            "project_id": project.project_id,
            "status_id": "*",
            "limit": 100,
            "offset": offset,
            "sort": "updated_on:asc,id:asc",
        }
        if updated_on_cutoff is not None:
            parameters["updated_on"] = f">={updated_on_cutoff}"
        url = f"{project.issues_api_url}?{urllib.parse.urlencode(parameters)}"
        diagnostic: dict[str, Any] = {}
        status, body = execution._get_with_retry(
            getter,
            url,
            {"X-Redmine-API-Key": api_key},
            progress_callback=progress_callback,
            response_diagnostic=diagnostic,
        )
        if status != 200:
            raise SourceManagerError(
                "Redmine issue inventory request failed",
                stage="fetch.redmine",
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceManagerError(
                "Redmine issue inventory response is invalid",
                stage="fetch.redmine",
            ) from exc
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if not isinstance(issues, list):
            raise SourceManagerError(
                "Redmine issue inventory response has no issues",
                stage="fetch.redmine",
            )
        for issue in issues:
            issue_id = issue.get("id") if isinstance(issue, dict) else None
            if (
                isinstance(issue_id, bool)
                or not isinstance(issue_id, int)
                or issue_id in seen_issue_ids
            ):
                raise SourceManagerError(
                    "redmine_inventory_changed",
                    stage="fetch.redmine",
                )
            seen_issue_ids.add(issue_id)
            result.append(
                (issue_id, _parse_timestamp(issue.get("updated_on")))
            )
        offset += len(issues)
        total = int(payload.get("total_count") or offset)
        if expected_total is None:
            expected_total = total
        elif expected_total != total:
            raise SourceManagerError(
                "redmine_inventory_changed",
                stage="fetch.redmine",
            )
        execution._emit_http_progress(
            progress_callback,
            {
                "event": "provider.page",
                "provider": "redmine",
                "phase": "redmine.inventory",
                "label_ja": "Redmine Issue一覧取得",
                "completed": len(result),
                "total": total,
                "unit": "件",
                "total_kind": "exact",
                "current_item": f"offset={offset}",
                "status": "running",
            },
        )
        if not issues or offset >= total:
            break
    if expected_total is None or len(result) != expected_total:
        raise SourceManagerError(
            "redmine_inventory_changed",
            stage="fetch.redmine",
        )
    return result


def _changed_issue_ids(
    inventory: list[tuple[int, datetime | None]],
    issues_directory: Path,
) -> list[int]:
    changed: list[int] = []
    for issue_id, remote_updated_on in inventory:
        local_path = issues_directory / f"{issue_id}.md"
        if not local_path.is_file():
            changed.append(issue_id)
            continue
        local_updated_on = _local_issue_timestamp(local_path)
        if remote_updated_on is None or local_updated_on is None:
            changed.append(issue_id)
            continue
        if local_updated_on < remote_updated_on:
            changed.append(issue_id)
    return changed


def install_redmine_incremental_refresh() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from . import execution

    original = execution._redmine

    def incremental_redmine(
        settings: dict[str, Any],
        work: Path,
        getter: Any,
        environment: Mapping[str, str],
        *,
        item_callback: Any,
        batch_callback: Any,
        resume_count: int,
        stable_issue_ids: list[int] | None,
        inventory_callback: Any,
        updated_on_cutoff: str | None,
        progress_callback: Any,
    ) -> dict[str, Any]:
        # Interrupted runs must resume against their frozen inventory unchanged.
        if stable_issue_ids is not None or resume_count:
            return original(
                settings,
                work,
                getter,
                environment,
                item_callback=item_callback,
                batch_callback=batch_callback,
                resume_count=resume_count,
                stable_issue_ids=stable_issue_ids,
                inventory_callback=inventory_callback,
                updated_on_cutoff=updated_on_cutoff,
                progress_callback=progress_callback,
            )

        issues_directory = Path(work) / "issues"
        if not issues_directory.is_dir() or not any(
            issues_directory.glob("*.md")
        ):
            return original(
                settings,
                work,
                getter,
                environment,
                item_callback=item_callback,
                batch_callback=batch_callback,
                resume_count=resume_count,
                stable_issue_ids=stable_issue_ids,
                inventory_callback=inventory_callback,
                updated_on_cutoff=updated_on_cutoff,
                progress_callback=progress_callback,
            )

        current = _inventory(
            settings=settings,
            getter=getter,
            environment=environment,
            updated_on_cutoff=updated_on_cutoff,
            execution=execution,
            progress_callback=progress_callback,
        )
        changed = _changed_issue_ids(current, issues_directory)
        if inventory_callback is not None:
            inventory_callback(list(changed))
        outcome = original(
            settings,
            work,
            getter,
            environment,
            item_callback=item_callback,
            batch_callback=batch_callback,
            resume_count=0,
            stable_issue_ids=changed,
            inventory_callback=None,
            updated_on_cutoff=updated_on_cutoff,
            progress_callback=progress_callback,
        )
        outcome["inventory_documents"] = len(current)
        outcome["unchanged_documents"] = len(current) - len(changed)
        outcome["incremental"] = True
        return outcome

    execution._redmine = incremental_redmine
    _INSTALLED = True
