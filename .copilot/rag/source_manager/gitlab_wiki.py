from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode

from .errors import SourceManagerError
from .gitlab_issues import (
    GitLabProject,
    _validate_gitlab_api_project_identity,
    gitlab_token_env,
    parse_gitlab_project,
)

HttpRequest = Callable[[str, Mapping[str, str]], tuple[int, bytes, Mapping[str, str]]]
ProgressCallback = Callable[[Mapping[str, Any]], None]
_LOCAL_METADATA = re.compile(r"(?m)^<!-- local-rag-gitlab-wiki: (\{[^\r\n]+\}) -->$")
_CHUNK = re.compile(r"^s-([A-Za-z0-9_-]{1,120})$")
_MAX_PAGES = 100_000


@dataclass(frozen=True)
class GitLabWikiInventoryItem:
    slug: str
    title: str


InventoryCallback = Callable[[list[GitLabWikiInventoryItem]], None]


def generated_gitlab_wiki_link(project_url: Any, gitlab_url: Any) -> dict[str, Any]:
    project = parse_gitlab_project(project_url, gitlab_url)
    return {
        "enabled": True,
        "strategy": "gitlab-wiki",
        "settings": {"project_url": project.project_url},
    }


def repair_generated_gitlab_wiki_link(
    project_url: Any,
    gitlab_url: Any,
    link: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(link, Mapping):
        return None
    value = dict(link)
    if (
        value.get("enabled") is True
        and value.get("strategy") == "gitlab-wiki"
        and set(value) == {"enabled", "strategy", "settings"}
        and isinstance(value.get("settings"), Mapping)
        and set(value["settings"]) == {"project_url"}
    ):
        try:
            parse_gitlab_project(value["settings"]["project_url"], gitlab_url)
        except SourceManagerError:
            return value
        return generated_gitlab_wiki_link(project_url, gitlab_url)
    return value


def gitlab_wiki_page_relative_path(slug: Any) -> str:
    value = _slug(slug)
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
    chunks = [encoded[i : i + 120] for i in range(0, len(encoded), 120)]
    relative = "/".join(["wikis", "v1", *(f"s-{part}" for part in chunks), "page.md"])
    if len(relative.encode("utf-8")) > 2048:
        raise SourceManagerError("GitLab Wiki slug is too long for a stored path")
    return relative


def decode_gitlab_wiki_page_relative_path(value: Any) -> str:
    text = str(value or "")
    if "\\" in text or len(text.encode("utf-8")) > 2048:
        raise SourceManagerError("GitLab Wiki page path is invalid")
    parts = text.split("/")
    if len(parts) < 4 or parts[:2] != ["wikis", "v1"] or parts[-1] != "page.md":
        raise SourceManagerError("GitLab Wiki page path is invalid")
    chunks: list[str] = []
    for index, component in enumerate(parts[2:-1]):
        match = _CHUNK.fullmatch(component)
        if match is None:
            raise SourceManagerError("GitLab Wiki page path is invalid")
        chunk = match.group(1)
        if index < len(parts[2:-1]) - 1 and len(chunk) != 120:
            raise SourceManagerError("GitLab Wiki page path is invalid")
        chunks.append(chunk)
    encoded = "".join(chunks)
    try:
        raw = base64.b64decode(encoded + "=" * ((4 - len(encoded) % 4) % 4), altchars=b"-_", validate=True)
        slug = raw.decode("utf-8")
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise SourceManagerError("GitLab Wiki page path is invalid") from exc
    slug = _slug(slug)
    if gitlab_wiki_page_relative_path(slug) != text:
        raise SourceManagerError("GitLab Wiki page path is not canonical")
    return slug


def gitlab_wiki_page_url(project: GitLabProject, slug: Any) -> str:
    encoded = "/".join(quote(part, safe="-._~") for part in _slug(slug).split("/"))
    return f"{project.project_url}/-/wikis/{encoded}"


def validate_gitlab_wiki_work_tree(
    settings: Mapping[str, Any],
    work: Path,
    *,
    expected_documents: int | None = None,
) -> int:
    project = parse_gitlab_project(settings.get("project_url"), settings.get("gitlab_url"))
    pages = _owned_pages(Path(work), _fingerprint(project))
    if expected_documents is not None and len(pages) != int(expected_documents):
        raise SourceManagerError(
            "GitLab Wiki work tree document count does not match fetch state",
            stage="reflect.gitlab_wiki",
        )
    return len(pages)


def fetch_gitlab_wiki(
    settings: Mapping[str, Any],
    work: Path,
    request: HttpRequest,
    environment: Mapping[str, str],
    *,
    inventory_callback: InventoryCallback | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    project = parse_gitlab_project(settings.get("project_url"), settings.get("gitlab_url"))
    expected_env = gitlab_token_env(project.gitlab_url)
    if str(settings.get("token_env") or "") != expected_env:
        raise SourceManagerError(
            "GitLab token environment does not match the GitLab URL",
            stage="fetch.gitlab_wiki",
        )
    token = str(environment.get(expected_env) or "").strip()
    if not token:
        raise SourceManagerError(
            "GitLab access token environment is unavailable",
            stage="fetch.gitlab_wiki",
        )
    headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
    project = _project_identity(project, request, headers)
    inventory = _inventory(project, request, headers, progress_callback)
    if inventory_callback is not None:
        inventory_callback(list(inventory))
    root = _wiki_root(Path(work), create=True)
    fingerprint = _fingerprint(project)
    existing = _owned_pages(Path(work), fingerprint)
    active: set[str] = set()
    written = unchanged = 0
    for index, item in enumerate(inventory, start=1):
        page = _page(project, item.slug, request, headers)
        markdown = _markdown(project, page, fingerprint)
        target = Path(work).joinpath(*gitlab_wiki_page_relative_path(item.slug).split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise SourceManagerError("GitLab Wiki file is unsafe", stage="fetch.gitlab_wiki")
        changed = _atomic_if_changed(target, markdown)
        written += int(changed)
        unchanged += int(not changed)
        active.add(item.slug)
        _emit(progress_callback, {
            "event": "provider.item",
            "provider": "gitlab_wiki",
            "phase": "gitlab_wiki.pages",
            "label_ja": "GitLab Wikiページ取得",
            "completed": index,
            "total": len(inventory),
            "unit": "ページ",
            "total_kind": "exact",
            "current_item": item.slug,
            "status": "running" if index < len(inventory) else "completed",
        })
    deleted = 0
    for slug, path in existing.items():
        if slug not in active:
            path.unlink()
            deleted += 1
    _remove_empty(root)
    local = _owned_pages(Path(work), fingerprint)
    return {
        "status": "ok",
        "documents": len(local),
        "inventory_documents": len(inventory),
        "local_documents": len(local),
        "written_this_run": written,
        "unchanged_this_run": unchanged,
        "deleted_this_run": deleted,
        "project_url": project.project_url,
        "last_completed_item": inventory[-1].slug if inventory else None,
    }


def _project_identity(project: GitLabProject, request: HttpRequest, headers: Mapping[str, str]) -> GitLabProject:
    status, body, _ = request(project.project_api_url, headers)
    if status != 200:
        raise _http("GitLab project request failed", status)
    payload = _json(body, "GitLab project")
    if not isinstance(payload, Mapping):
        raise SourceManagerError("GitLab project response must be an object", stage="fetch.gitlab_wiki")
    project_id = _positive(payload.get("id"), "GitLab project ID")
    _validate_gitlab_api_project_identity(
        payload,
        project.project_path,
        error_message="GitLab project response has the wrong path identity",
        stage="fetch.gitlab_wiki",
    )
    return GitLabProject(project.gitlab_url, project.api_base_url, project.project_url, project.project_path, project_id)


def _inventory(project: GitLabProject, request: HttpRequest, headers: Mapping[str, str], progress: ProgressCallback | None) -> list[GitLabWikiInventoryItem]:
    page = 1
    expected: int | None = None
    output: list[GitLabWikiInventoryItem] = []
    seen: set[str] = set()
    while page <= _MAX_PAGES:
        query = urlencode({"with_content": "0", "per_page": 100, "page": page})
        status, body, response_headers = request(f"{project.api_base_url}/projects/{project.project_id}/wikis?{query}", headers)
        if status != 200:
            raise _http("GitLab Wiki inventory request failed", status)
        payload = _json(body, "GitLab Wiki inventory")
        if not isinstance(payload, list):
            raise SourceManagerError("GitLab Wiki inventory response must be an array", stage="fetch.gitlab_wiki")
        for raw in payload:
            if not isinstance(raw, Mapping):
                raise SourceManagerError("GitLab Wiki inventory contains an invalid item", stage="fetch.gitlab_wiki")
            slug = _slug(raw.get("slug"))
            if slug in seen:
                raise SourceManagerError("gitlab_wiki_inventory_changed", stage="fetch.gitlab_wiki")
            seen.add(slug)
            output.append(GitLabWikiInventoryItem(slug, str(raw.get("title") or slug)))
        total_text = _header(response_headers, "X-Total")
        if total_text:
            if not total_text.isdigit():
                raise SourceManagerError("GitLab Wiki pagination total is invalid", stage="fetch.gitlab_wiki")
            total = int(total_text)
            if expected is None:
                expected = total
            elif expected != total:
                raise SourceManagerError("gitlab_wiki_inventory_changed", stage="fetch.gitlab_wiki")
        _emit(progress, {
            "event": "provider.page", "provider": "gitlab_wiki",
            "phase": "gitlab_wiki.inventory", "label_ja": "GitLab Wiki一覧取得",
            "completed": len(output), "total": expected, "unit": "ページ",
            "total_kind": "exact" if expected is not None else "unknown",
            "current_item": f"page={page}", "status": "running",
        })
        next_page = _header(response_headers, "X-Next-Page")
        if next_page:
            if not next_page.isdigit() or int(next_page) <= page or int(next_page) > _MAX_PAGES:
                raise SourceManagerError("GitLab Wiki pagination next page is invalid", stage="fetch.gitlab_wiki")
            page = int(next_page)
            continue
        if expected is not None:
            if len(output) == expected:
                break
            if len(output) > expected or not payload:
                raise SourceManagerError("gitlab_wiki_inventory_changed", stage="fetch.gitlab_wiki")
            page += 1
            continue
        if len(payload) < 100:
            break
        page += 1
    else:
        raise SourceManagerError("GitLab Wiki pagination exceeded the safety limit", stage="fetch.gitlab_wiki")
    return output


def _page(project: GitLabProject, slug: str, request: HttpRequest, headers: Mapping[str, str]) -> dict[str, Any]:
    encoded = quote(slug, safe="")
    status, body, _ = request(f"{project.api_base_url}/projects/{project.project_id}/wikis/{encoded}", headers)
    if status != 200:
        raise _http("GitLab Wiki detail request failed", status)
    payload = _json(body, "GitLab Wiki detail")
    if not isinstance(payload, dict) or _slug(payload.get("slug")) != slug:
        raise SourceManagerError("GitLab Wiki detail response has the wrong page identity", stage="fetch.gitlab_wiki")
    return payload


def _markdown(project: GitLabProject, page: Mapping[str, Any], fingerprint: str) -> str:
    slug = _slug(page.get("slug"))
    title = str(page.get("title") or slug).strip()
    content = str(page.get("content") or "").strip()
    metadata = json.dumps(
        {"schema_version": "local-rag-gitlab-wiki-v1", "slug": slug, "project_fingerprint": fingerprint},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    structured = json.dumps(dict(page), ensure_ascii=False, sort_keys=True, indent=2)
    return "\n".join([
        f"# GitLab Wiki: {title}", "", f"<!-- local-rag-gitlab-wiki: {metadata} -->", "",
        f"- Slug: {slug}", f"- URL: {gitlab_wiki_page_url(project, slug)}", "",
        "## 本文", "", content or "（本文なし）", "",
        "## Structured GitLab metadata", "", "```json", structured, "```", "",
    ])


def _owned_pages(work: Path, fingerprint: str) -> dict[str, Path]:
    root = _wiki_root(work, create=False)
    if root is None:
        return {}
    output: dict[str, Path] = {}
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SourceManagerError("GitLab Wiki managed tree is unsafe", stage="fetch.gitlab_wiki")
        if path.is_dir():
            continue
        if not path.is_file() or path.name != "page.md":
            raise SourceManagerError("GitLab Wiki managed tree contains an unknown file", stage="fetch.gitlab_wiki")
        files.append(path)
    for path in files:
        relative = path.relative_to(work).as_posix()
        slug = decode_gitlab_wiki_page_relative_path(relative)
        match = _LOCAL_METADATA.search(path.read_text(encoding="utf-8"))
        if not match:
            raise SourceManagerError("GitLab Wiki managed file metadata is invalid", stage="fetch.gitlab_wiki")
        try:
            metadata = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise SourceManagerError("GitLab Wiki managed file metadata is invalid", stage="fetch.gitlab_wiki") from exc
        if metadata.get("slug") != slug or metadata.get("project_fingerprint") != fingerprint or slug in output:
            raise SourceManagerError("GitLab Wiki managed file identity is invalid", stage="fetch.gitlab_wiki")
        output[slug] = path
    return output


def _wiki_root(work: Path, *, create: bool) -> Path | None:
    root = Path(work)
    if root.is_symlink() or not root.is_dir():
        raise SourceManagerError("GitLab Wiki work directory is unsafe", stage="fetch.gitlab_wiki")
    for entry in root.iterdir():
        if entry.name != "wikis" or entry.is_symlink() or (entry.exists() and not entry.is_dir()):
            raise SourceManagerError("GitLab Wiki work directory contains an unknown entry", stage="fetch.gitlab_wiki")
    directory = root / "wikis"
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise SourceManagerError("GitLab Wiki work directory is unsafe", stage="fetch.gitlab_wiki")
    elif create:
        directory.mkdir()
    else:
        return None
    return directory


def _atomic_if_changed(path: Path, text: str) -> bool:
    encoded = text.encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded:
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try: Path(temporary).unlink(missing_ok=True)
        except OSError: pass
    return True


def _remove_empty(root: Path) -> None:
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try: path.rmdir()
        except OSError: pass


def _fingerprint(project: GitLabProject) -> str:
    return hashlib.sha256(f"local-rag-gitlab-wiki-project-v1\0{project.gitlab_url}\0{project.project_path}".encode()).hexdigest()


def _slug(value: Any) -> str:
    text = str(value or "")
    if not text or len(text) > 4096 or text.startswith("/") or text.endswith("/") or "\\" in text or any(part in {"", ".", ".."} for part in text.split("/")) or any(ord(c) < 0x20 for c in text):
        raise SourceManagerError("GitLab Wiki slug is invalid")
    return text


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
        raise SourceManagerError(f"{field} must be a positive integer")
    return int(value)


def _json(body: bytes, field: str) -> Any:
    try: return json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceManagerError(f"{field} response is invalid JSON", stage="fetch.gitlab_wiki") from exc


def _http(message: str, status: Any) -> SourceManagerError:
    error = SourceManagerError(message, stage="fetch.gitlab_wiki")
    error.diagnostic = {"event": "gitlab_wiki.http_response", "status": int(status), "retry": False}
    return error


def _header(headers: Mapping[str, Any], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected: return str(value or "").strip()
    return ""


def _emit(callback: ProgressCallback | None, event: Mapping[str, Any]) -> None:
    if callback is not None:
        try: callback(dict(event))
        except Exception: pass
