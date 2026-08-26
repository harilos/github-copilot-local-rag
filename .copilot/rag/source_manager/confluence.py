from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from .errors import SourceManagerError

try:
    from lxml import etree
except ImportError:  # pragma: no cover - the Windows admin runtime pins lxml.
    etree = None


HttpGet = Callable[
    [str, Mapping[str, str], float],
    tuple[int, bytes] | tuple[int, bytes, Mapping[str, str]],
]
InventoryCallback = Callable[[list[str]], None]
InventoryEtagCallback = Callable[[list[str], str], None]
ItemCallback = Callable[[int, str], None]
BatchCallback = Callable[[int, str | None], None]
ProgressCallback = Callable[[Mapping[str, Any]], None]

_PAGE_ID = re.compile(r"^[1-9][0-9]{0,18}$")
_MAX_PAGE_ID = 9_223_372_036_854_775_807
_SPACE_KEY = re.compile(r"^[A-Za-z0-9_.~-]{1,255}$")
_CLOUD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,199}$")
_LOCAL_METADATA = re.compile(
    r"(?m)^<!-- local-rag-confluence: (\{[^\r\n]+\}) -->$"
)
_MAX_PAGES = 10_000
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_STORAGE_BYTES = 4 * 1024 * 1024
_MAX_MARKDOWN_CHARS = 8 * 1024 * 1024
_MAX_HTML_NODES = 100_000
_MAX_HTML_DEPTH = 128
_BATCH_SIZE = 5
_MAX_PAGE_LABELS = 10_000
_MAX_PAGE_ANCESTORS = 10_000
_MAX_METADATA_TEXT = 4_096
_WINDOWS_FILE_RETRY_SECONDS = 2.0
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_SAFE_LINK_SCHEMES = frozenset({"", "http", "https", "mailto"})
_FORBIDDEN_HTML_TAGS = frozenset(
    {
        "applet",
        "audio",
        "embed",
        "form",
        "iframe",
        "math",
        "object",
        "script",
        "style",
        "svg",
        "template",
        "video",
    }
)


@dataclass(frozen=True)
class ConfluenceEndpoint:
    deployment: str
    cloud_scope: str | None
    site_url: str
    context_path: str
    api_root: str
    web_root: str
    cloud_id: str | None = None


@dataclass(frozen=True)
class ConfluenceInventoryItem:
    page_id: str
    title: str
    version: int | None


@dataclass(frozen=True)
class _OwnedPage:
    path: Path
    metadata: dict[str, Any]


def resolve_confluence_endpoint(
    settings: Mapping[str, Any],
) -> ConfluenceEndpoint:
    """Resolve one explicit, canonical Cloud or Data Center endpoint."""

    if not isinstance(settings, Mapping):
        raise SourceManagerError("Confluence settings must be an object")
    deployment = str(settings.get("deployment") or "").strip().lower()
    if deployment not in {"cloud", "data_center"}:
        raise SourceManagerError(
            "Confluence deployment must be explicitly cloud or data_center"
        )
    site_url = _site_origin(settings.get("site_url"))
    context_path = _context_path(settings.get("context_path"))
    web_root = site_url + context_path

    cloud_scope: str | None = None
    cloud_id: str | None = None
    if deployment == "cloud":
        cloud_scope = str(settings.get("cloud_scope") or "").strip().lower()
        if cloud_scope not in {"unscoped", "scoped"}:
            raise SourceManagerError(
                "Confluence Cloud scope must be explicitly unscoped or scoped"
            )
        if context_path != "/wiki":
            raise SourceManagerError(
                "Confluence Cloud context_path must be /wiki"
            )
        if cloud_scope == "scoped":
            cloud_id = _cloud_id(settings.get("cloud_id"))
            expected_api_root = (
                "https://api.atlassian.com/ex/confluence/"
                f"{quote(cloud_id, safe='-')}{context_path}/api/v2"
            )
        else:
            if settings.get("cloud_id") not in {None, ""}:
                raise SourceManagerError(
                    "Unscoped Confluence Cloud must not set cloud_id"
                )
            expected_api_root = web_root + "/api/v2"
    else:
        if settings.get("cloud_scope") not in {None, ""}:
            raise SourceManagerError(
                "Confluence Data Center must not set cloud_scope"
            )
        if settings.get("cloud_id") not in {None, ""}:
            raise SourceManagerError(
                "Confluence Data Center must not set cloud_id"
            )
        expected_api_root = web_root + "/rest/api"

    supplied_api_root = _canonical_url_root(
        settings.get("api_root"),
        field="Confluence api_root",
    )
    if supplied_api_root != expected_api_root:
        raise SourceManagerError(
            "Confluence api_root does not match deployment/site/context"
        )
    endpoint = ConfluenceEndpoint(
        deployment=deployment,
        cloud_scope=cloud_scope,
        site_url=site_url,
        context_path=context_path,
        api_root=supplied_api_root,
        web_root=web_root,
        cloud_id=cloud_id,
    )
    _validate_api_url(endpoint, endpoint.api_root)
    return endpoint


def confluence_page_relative_path(page_id: Any) -> str:
    return f"pages/{_page_id(page_id)}.md"


def validate_confluence_work_tree(
    work: Path,
    *,
    expected_documents: int,
) -> None:
    """Validate the canonical materialized tree before a reflected ADD batch."""

    if isinstance(expected_documents, bool) or int(expected_documents) < 0:
        raise SourceManagerError("Confluence expected document count is invalid")
    expected = int(expected_documents)
    root = Path(work)
    if root.is_symlink() or not root.is_dir():
        raise SourceManagerError("Confluence work directory is unsafe")
    for entry in root.iterdir():
        if entry.name != "pages" or entry.is_symlink() or not entry.is_dir():
            raise SourceManagerError("Confluence work directory contains an unknown entry")
    pages = root / "pages"
    if not pages.exists():
        if expected != 0:
            raise SourceManagerError("Confluence work tree document count is inconsistent")
        return
    if pages.is_symlink() or not pages.is_dir():
        raise SourceManagerError("Confluence pages directory is unsafe")
    count = 0
    fingerprint: str | None = None
    inventory_etag: str | None = None
    for path in sorted(pages.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise SourceManagerError("Confluence pages directory contains an unsafe entry")
        match = re.fullmatch(r"([1-9][0-9]{0,18})\.md", path.name)
        if match is None:
            raise SourceManagerError("Confluence pages directory contains an unknown file")
        page_id = _page_id(match.group(1))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SourceManagerError("Confluence managed page cannot be read") from exc
        marker = _LOCAL_METADATA.search(text)
        if marker is None:
            raise SourceManagerError("Confluence managed page metadata is missing")
        try:
            metadata = json.loads(marker.group(1))
        except json.JSONDecodeError as exc:
            raise SourceManagerError("Confluence managed page metadata is invalid") from exc
        current_fingerprint = str(
            metadata.get("source_fingerprint") if isinstance(metadata, Mapping) else ""
        )
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("schema_version") != "local-rag-confluence-v1"
            or metadata.get("page_id") != page_id
            or metadata.get("path") != confluence_page_relative_path(page_id)
            or re.fullmatch(r"[0-9a-f]{64}", current_fingerprint) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("inventory_etag") or ""))
            is None
        ):
            raise SourceManagerError("Confluence managed page identity is invalid")
        if fingerprint is None:
            fingerprint = current_fingerprint
        elif fingerprint != current_fingerprint:
            raise SourceManagerError("Confluence work tree mixes source identities")
        current_inventory_etag = str(metadata.get("inventory_etag") or "")
        if inventory_etag is None:
            inventory_etag = current_inventory_etag
        elif inventory_etag != current_inventory_etag:
            raise SourceManagerError("Confluence work tree mixes inventory checkpoints")
        count += 1
    if count != expected:
        raise SourceManagerError("Confluence work tree document count is inconsistent")


def request_confluence_json(
    endpoint: ConfluenceEndpoint,
    url: str,
    http_get: HttpGet,
    connection: Mapping[str, Any] | object,
    *,
    timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    progress_callback: ProgressCallback | None = None,
) -> Any:
    """Perform one GET-only request with bounded transient retries."""

    request_url = _validate_api_url(endpoint, url)
    headers = _authorization_headers(connection, deployment=endpoint.deployment)
    if not callable(http_get):
        raise SourceManagerError("Confluence HTTP GET callback is unavailable")
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise SourceManagerError("Confluence timeout must be positive") from exc
    if timeout_value <= 0 or timeout_value > 300:
        raise SourceManagerError("Confluence timeout must be positive and bounded")

    for attempt in range(1, 4):
        try:
            raw_response = http_get(request_url, headers, timeout_value)
        except Exception as exc:
            retry = _retryable_network_exception(exc) and attempt < 3
            _emit_http_progress(
                progress_callback,
                request_url,
                attempt,
                status=None,
                retry=retry,
            )
            if not retry:
                raise SourceManagerError(
                    "Confluence GET request failed",
                    stage="fetch.confluence",
                ) from exc
            sleep(0.1 * attempt)
            continue
        if not isinstance(raw_response, tuple) or len(raw_response) not in {2, 3}:
            raise SourceManagerError(
                "Confluence HTTP response contract is invalid",
                stage="fetch.confluence",
            )
        status = int(raw_response[0])
        body = raw_response[1]
        response_headers = (
            raw_response[2]
            if len(raw_response) == 3 and isinstance(raw_response[2], Mapping)
            else {}
        )
        if not isinstance(body, (bytes, bytearray)):
            raise SourceManagerError(
                "Confluence HTTP response body must be bytes",
                stage="fetch.confluence",
            )
        if 300 <= status <= 399:
            _emit_http_progress(
                progress_callback,
                request_url,
                attempt,
                status=status,
                retry=False,
            )
            raise SourceManagerError(
                "Confluence redirect response is rejected",
                stage="fetch.confluence",
            )
        retry = status in _RETRYABLE_STATUSES and attempt < 3
        _emit_http_progress(
            progress_callback,
            request_url,
            attempt,
            status=status,
            retry=retry,
        )
        if status != 200:
            if retry:
                delay = _retry_after_seconds(_header(response_headers, "Retry-After"))
                sleep(delay if delay is not None else 0.1 * attempt)
                continue
            raise SourceManagerError(
                f"Confluence GET request returned HTTP {status}",
                stage="fetch.confluence",
            )
        if len(body) > _MAX_JSON_BYTES:
            raise SourceManagerError(
                "Confluence JSON response is too large",
                stage="fetch.confluence",
            )
        try:
            return json.loads(bytes(body).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceManagerError(
                "Confluence response is invalid JSON",
                stage="fetch.confluence",
            ) from exc
    raise SourceManagerError(
        "Confluence GET retry limit was exhausted",
        stage="fetch.confluence",
    )


def fetch_confluence(
    settings: Mapping[str, Any],
    work: Path,
    *,
    credentials: Mapping[str, Any] | object,
    http_get: HttpGet,
    inventory_callback: InventoryCallback | None = None,
    inventory_etag_callback: InventoryEtagCallback | None = None,
    item_callback: ItemCallback | None = None,
    batch_callback: BatchCallback | None = None,
    resume_count: int = 0,
    stable_page_ids: Sequence[Any] | None = None,
    resume_inventory_etag: str | None = None,
    progress_callback: ProgressCallback | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch one Confluence space/subtree into deterministic page Markdown.

    Secrets are accepted only through ``connection`` and are used to construct
    request headers. They are never copied into the returned outcome or files.
    """

    if not isinstance(settings, Mapping):
        raise SourceManagerError("Confluence Source settings must be an object")
    allowed_settings = {
        "connection_id",
        "space_key",
        "scope",
        "root_page_id",
        "attachments",
    }
    if set(settings) - allowed_settings:
        raise SourceManagerError("Confluence Source settings contain unknown fields")
    connection_id = str(settings.get("connection_id") or "").strip()
    if not connection_id:
        raise SourceManagerError("Confluence connection_id is required")
    credential_connection_id = _connection_text(credentials, "connection_id")
    if credential_connection_id and credential_connection_id != connection_id:
        raise SourceManagerError("Confluence credentials have the wrong connection identity")
    endpoint = resolve_confluence_endpoint(_credential_mapping(credentials))
    root = Path(work)
    if root.is_symlink() or not root.is_dir():
        raise SourceManagerError(
            "Confluence work directory is unsafe",
            stage="fetch.confluence",
        )
    space_key = _space_key(settings.get("space_key"))
    content_scope = str(settings.get("scope") or "").strip().lower()
    if content_scope not in {"space", "subtree"}:
        raise SourceManagerError(
            "Confluence scope must be space or subtree"
        )
    root_page_id = (
        _page_id(settings.get("root_page_id"))
        if content_scope == "subtree"
        else None
    )
    if content_scope == "space" and settings.get("root_page_id") not in {
        None,
        "",
    }:
        raise SourceManagerError(
            "space Confluence source must not set root_page_id"
        )
    attachments_mode = str(settings.get("attachments") or "").strip().lower()
    if attachments_mode not in {"none", "metadata"}:
        raise SourceManagerError(
            "Confluence attachments must be none or metadata"
    )
    page_limit = 100
    cloud_space_id = (
        _cloud_space_id(
            endpoint,
            space_key,
            page_limit,
            http_get,
            credentials,
            progress_callback=progress_callback,
            sleep=sleep,
        )
        if endpoint.deployment == "cloud"
        else None
    )
    fingerprint = _source_fingerprint(
        endpoint,
        space_key,
        content_scope,
        root_page_id,
    )
    existing = _owned_pages(root, fingerprint)

    if stable_page_ids is None:
        inventory = _fetch_page_inventory(
            endpoint,
            settings,
            space_key,
            content_scope,
            root_page_id,
            page_limit,
            http_get,
            credentials,
            cloud_space_id=cloud_space_id,
            progress_callback=progress_callback,
            sleep=sleep,
        )
        stable_ids = [item.page_id for item in inventory]
        inventory_etag = _inventory_etag(fingerprint, stable_ids)
        if inventory_callback is not None:
            inventory_callback(list(stable_ids))
        if inventory_etag_callback is not None:
            inventory_etag_callback(list(stable_ids), inventory_etag)
    else:
        stable_ids = _validated_page_ids(stable_page_ids)
        inventory_etag = _inventory_etag(fingerprint, stable_ids)
        supplied_inventory_etag = str(resume_inventory_etag or "").strip().lower()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", supplied_inventory_etag)
            or not hmac.compare_digest(supplied_inventory_etag, inventory_etag)
        ):
            raise SourceManagerError(
                "Confluence resume inventory checkpoint etag is missing or invalid",
                stage="fetch.confluence",
            )
        if content_scope == "subtree" and (
            not stable_ids or stable_ids[0] != root_page_id
        ):
            raise SourceManagerError(
                "Confluence resume inventory has the wrong subtree root",
                stage="fetch.confluence",
            )
        inventory = [
            ConfluenceInventoryItem(page_id=value, title="", version=None)
            for value in stable_ids
        ]

    if isinstance(resume_count, bool):
        raise SourceManagerError("Confluence resume_count is invalid")
    completed_before = int(resume_count)
    if completed_before < 0 or completed_before > len(stable_ids):
        raise SourceManagerError("Confluence resume_count is invalid")
    if stable_page_ids is not None and completed_before < 1:
        raise SourceManagerError(
            "Confluence resume requires a completed checkpoint batch",
            stage="fetch.confluence",
        )
    for page_id in stable_ids[:completed_before]:
        owned = existing.get(page_id)
        if owned is None:
            raise SourceManagerError(
                "Confluence resume checkpoint is missing a published page",
                stage="fetch.confluence",
            )

    page_links_by_id: dict[str, dict[str, str]] = {}
    for page_id in stable_ids[:completed_before]:
        metadata = existing[page_id].metadata
        api_url = str(metadata.get("api_url") or "")
        web_url = str(metadata.get("web_url") or "")
        if not api_url or not web_url:
            raise SourceManagerError(
                "Confluence resume checkpoint page metadata is incomplete",
                stage="fetch.confluence",
            )
        _validate_api_url(endpoint, api_url)
        _validate_web_url(endpoint, web_url)
        if not hmac.compare_digest(
            str(metadata.get("inventory_etag") or ""), inventory_etag
        ):
            raise SourceManagerError(
                "Confluence resume checkpoint page has the wrong inventory etag",
                stage="fetch.confluence",
            )
        page_links_by_id[page_id] = _page_link(page_id, api_url, web_url)

    written = unchanged = 0
    last_completed: str | None = (
        stable_ids[completed_before - 1] if completed_before else None
    )
    failed_page_ids: list[str] = []
    checkpoint_contiguous = True
    for index in range(completed_before, len(inventory)):
        item = inventory[index]
        page_id = item.page_id
        try:
            detail_url = _page_detail_request_url(endpoint, page_id)
            detail = request_confluence_json(
                endpoint,
                detail_url,
                http_get,
                credentials,
                sleep=sleep,
                progress_callback=progress_callback,
            )
            normalized = _validated_page_detail(
                endpoint,
                detail,
                page_id,
                space_key,
                cloud_space_id=cloud_space_id,
            )
            if endpoint.deployment == "cloud":
                normalized["labels"] = _fetch_cloud_page_labels(
                    endpoint,
                    page_id,
                    page_limit,
                    http_get,
                    credentials,
                    progress_callback=progress_callback,
                    sleep=sleep,
                )
                normalized["ancestors"] = _fetch_cloud_page_ancestors(
                    endpoint,
                    page_id,
                    page_limit,
                    http_get,
                    credentials,
                    progress_callback=progress_callback,
                    sleep=sleep,
                )
            body_markdown = storage_xhtml_to_markdown(normalized["storage"])
            attachments = (
                _fetch_attachments(
                    endpoint,
                    page_id,
                    page_limit,
                    http_get,
                    credentials,
                    progress_callback=progress_callback,
                    sleep=sleep,
                )
                if attachments_mode == "metadata"
                else []
            )
            api_url, web_url = _page_urls(endpoint, normalized, page_id)
            relative_path = confluence_page_relative_path(page_id)
            markdown = _page_markdown(
                normalized,
                body_markdown,
                attachments,
                fingerprint=fingerprint,
                api_url=api_url,
                web_url=web_url,
                relative_path=relative_path,
                inventory_etag=inventory_etag,
            )
        except SourceManagerError:
            failed_page_ids.append(page_id)
            checkpoint_contiguous = False
            _emit(
                progress_callback,
                {
                    "event": "provider.item",
                    "provider": "confluence",
                    "phase": "confluence.pages",
                    "completed": index,
                    "total": len(inventory),
                    "unit": "pages",
                    "current_item": page_id,
                    "status": "failed",
                },
            )
            continue
        relative_path = confluence_page_relative_path(page_id)
        target = root.joinpath(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise SourceManagerError(
                "Confluence page target is unsafe",
                stage="fetch.confluence",
            )
        changed = _atomic_if_changed(target, markdown)
        written += int(changed)
        unchanged += int(not changed)
        page_links_by_id[page_id] = _page_link(page_id, api_url, web_url)
        completed = index + 1
        if checkpoint_contiguous:
            last_completed = page_id
        if checkpoint_contiguous and item_callback is not None:
            item_callback(completed, page_id)
        # Interior batches may be reflected immediately.  The final batch is
        # delayed until after stale-page reconciliation so the ADD observes
        # the complete, authoritative work tree (including deletions).
        if (
            checkpoint_contiguous
            and batch_callback is not None
            and completed % _BATCH_SIZE == 0
            and completed < len(inventory)
        ):
            batch_callback(completed, page_id)
        _emit(
            progress_callback,
            {
                "event": "provider.item",
                "provider": "confluence",
                "phase": "confluence.pages",
                "completed": completed,
                "total": len(inventory),
                "unit": "pages",
                "current_item": page_id,
            },
        )

    if failed_page_ids:
        error = SourceManagerError(
            f"Confluence page acquisition failed for {len(failed_page_ids)} page(s)",
            stage="fetch.confluence",
        )
        error.failed_page_ids = tuple(failed_page_ids)
        raise error

    # Deletion is deliberately the final operation: an inventory, detail,
    # attachment, XHTML parse, callback, or atomic-publish failure cannot make
    # a remotely absent page disappear locally.
    active = set(stable_ids)
    deleted = 0
    for page_id, owned in existing.items():
        if page_id not in active:
            owned.path.unlink()
            deleted += 1
    _remove_empty_pages_directory(root)
    local = _owned_pages(root, fingerprint)
    if set(local) != active:
        raise SourceManagerError(
            "Confluence work tree does not match the completed inventory",
            stage="fetch.confluence",
        )

    if batch_callback is not None:
        batch_callback(len(stable_ids), stable_ids[-1] if stable_ids else None)

    page_links = [page_links_by_id[value] for value in stable_ids]
    return {
        "status": "ok",
        "deployment": endpoint.deployment,
        "cloud_scope": endpoint.cloud_scope,
        "site_url": endpoint.site_url,
        "context_path": endpoint.context_path,
        "api_root": endpoint.api_root,
        "space_key": space_key,
        "content_scope": content_scope,
        "root_page_id": root_page_id,
        "documents": len(local),
        "inventory_documents": len(stable_ids),
        "local_documents": len(local),
        "written_this_run": written,
        "unchanged_this_run": unchanged,
        "deleted_this_run": deleted,
        "fetched_this_run": len(stable_ids) - completed_before,
        "last_completed_item": last_completed,
        "stable_page_ids": list(stable_ids),
        "inventory_etag": inventory_etag,
        "page_links": page_links,
        "api_webui_map": {
            item["api_url"]: item["web_url"] for item in page_links
        },
        "page_urls": {
            item["page_id"]: item["web_url"] for item in page_links
        },
    }


def storage_xhtml_to_markdown(value: Any) -> str:
    """Convert bounded Confluence storage XHTML without active-content output."""

    if not isinstance(value, str):
        raise SourceManagerError(
            "Confluence storage body must be text",
            stage="fetch.confluence",
        )
    if len(value.encode("utf-8")) > _MAX_STORAGE_BYTES:
        raise SourceManagerError(
            "Confluence storage body is too large",
            stage="fetch.confluence",
        )
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", value, flags=re.IGNORECASE):
        raise SourceManagerError(
            "Confluence storage XHTML declarations are forbidden",
            stage="fetch.confluence",
        )
    if etree is None:
        raise SourceManagerError(
            "Confluence XHTML conversion requires lxml",
            stage="fetch.confluence",
        )
    try:
        parser = etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            recover=False,
            remove_comments=True,
            huge_tree=False,
        )
        synthetic = (
            '<local-rag-root xmlns:ac="urn:atlassian:confluence" '
            'xmlns:ri="urn:atlassian:resource">'
            + value
            + "</local-rag-root>"
        )
        root = etree.fromstring(synthetic.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError, TypeError, UnicodeError) as exc:
        raise SourceManagerError(
            "Confluence storage XHTML is invalid",
            stage="fetch.confluence",
        ) from exc
    nodes = 0
    for element in root.iter():
        nodes += 1
        if nodes > _MAX_HTML_NODES:
            raise SourceManagerError(
                "Confluence storage XHTML has too many nodes",
                stage="fetch.confluence",
            )
        depth = 0
        parent = element.getparent()
        while parent is not None:
            depth += 1
            if depth > _MAX_HTML_DEPTH:
                raise SourceManagerError(
                    "Confluence storage XHTML is too deeply nested",
                    stage="fetch.confluence",
                )
            parent = parent.getparent()
    rendered = _render_children(root, list_depth=0)
    normalized = _normalize_markdown(rendered)
    if len(normalized) > _MAX_MARKDOWN_CHARS:
        raise SourceManagerError(
            "Confluence converted Markdown is too large",
            stage="fetch.confluence",
        )
    return normalized


def _fetch_page_inventory(
    endpoint: ConfluenceEndpoint,
    settings: Mapping[str, Any],
    space_key: str,
    content_scope: str,
    root_page_id: str | None,
    page_limit: int,
    http_get: HttpGet,
    connection: Mapping[str, Any] | object,
    *,
    cloud_space_id: str | None,
    progress_callback: ProgressCallback | None,
    sleep: Callable[[float], None],
) -> list[ConfluenceInventoryItem]:
    if endpoint.deployment == "cloud":
        return _fetch_cloud_page_inventory(
            endpoint,
            space_key,
            content_scope,
            root_page_id,
            page_limit,
            http_get,
            connection,
            cloud_space_id=cloud_space_id,
            progress_callback=progress_callback,
            sleep=sleep,
        )
    if content_scope == "space":
        path = f"{endpoint.api_root}/content"
        query = {
            "type": "page",
            "spaceKey": space_key,
            "status": "current",
            "limit": page_limit,
            "start": 0,
            "expand": "version",
        }
        output: list[ConfluenceInventoryItem] = []
    else:
        assert root_page_id is not None
        path = (
            f"{endpoint.api_root}/content/"
            f"{quote(root_page_id, safe='')}/descendant/page"
        )
        query = {
            "limit": page_limit,
            "start": 0,
            "expand": "version",
        }
        output = [ConfluenceInventoryItem(root_page_id, "", None)]
    initial_url = path + "?" + urlencode(query)
    seen = {item.page_id for item in output}
    expected_total: int | None = None
    remote_count = 0
    current_url = initial_url
    visited: set[str] = set()
    for page_number in range(1, _MAX_PAGES + 1):
        current_url = _validate_api_url(endpoint, current_url)
        if current_url in visited:
            raise SourceManagerError(
                "Confluence pagination loop was detected",
                stage="fetch.confluence",
            )
        visited.add(current_url)
        current_start = _query_start(current_url)
        payload = request_confluence_json(
            endpoint,
            current_url,
            http_get,
            connection,
            sleep=sleep,
            progress_callback=progress_callback,
        )
        results, size, payload_limit = _page_envelope(
            payload,
            current_start=current_start,
            field="Confluence page inventory",
        )
        remote_count += size
        for raw in results:
            if not isinstance(raw, Mapping):
                raise SourceManagerError(
                    "Confluence page inventory contains an invalid item",
                    stage="fetch.confluence",
                )
            page_id = _page_id(raw.get("id"))
            if str(raw.get("type") or "page") != "page":
                raise SourceManagerError(
                    "Confluence page inventory contains a non-page item",
                    stage="fetch.confluence",
                )
            _validate_inventory_space(raw, space_key)
            if page_id in seen:
                raise SourceManagerError(
                    "Confluence page inventory changed during pagination",
                    stage="fetch.confluence",
                )
            seen.add(page_id)
            version = _optional_positive_int(
                (raw.get("version") or {}).get("number")
                if isinstance(raw.get("version"), Mapping)
                else None,
                field="Confluence page version",
            )
            output.append(
                ConfluenceInventoryItem(
                    page_id,
                    str(raw.get("title") or "").strip(),
                    version,
                )
            )
        supplied_total = _payload_total(payload)
        if supplied_total is not None:
            if expected_total is None:
                expected_total = supplied_total
            elif expected_total != supplied_total:
                raise SourceManagerError(
                    "Confluence inventory total changed during pagination",
                    stage="fetch.confluence",
                )
        _emit(
            progress_callback,
            {
                "event": "provider.page",
                "provider": "confluence",
                "phase": "confluence.inventory",
                "completed": remote_count,
                "total": expected_total,
                "unit": "pages",
                "current_item": f"start={current_start}",
            },
        )
        next_link = _next_link(payload)
        if next_link:
            next_url = _validated_next_url(
                endpoint,
                current_url,
                next_link,
                current_start=current_start,
            )
            if urlsplit(next_url).path != urlsplit(initial_url).path:
                raise SourceManagerError(
                    "Confluence pagination changed the inventory endpoint",
                    stage="fetch.confluence",
                )
            current_url = next_url
            continue
        if expected_total is not None and remote_count != expected_total:
            raise SourceManagerError(
                "Confluence inventory ended before its declared total",
                stage="fetch.confluence",
            )
        if expected_total is None and size == payload_limit and size > 0:
            raise SourceManagerError(
                "Confluence inventory omitted the next pagination link",
                stage="fetch.confluence",
            )
        return output
    raise SourceManagerError(
        "Confluence pagination exceeded the safety limit",
        stage="fetch.confluence",
    )


def _fetch_cloud_page_inventory(
    endpoint: ConfluenceEndpoint,
    space_key: str,
    content_scope: str,
    root_page_id: str | None,
    page_limit: int,
    http_get: HttpGet,
    connection: Mapping[str, Any] | object,
    *,
    cloud_space_id: str | None,
    progress_callback: ProgressCallback | None,
    sleep: Callable[[float], None],
) -> list[ConfluenceInventoryItem]:
    if cloud_space_id is None:
        raise SourceManagerError(
            "Confluence Cloud space identity is unavailable",
            stage="fetch.confluence",
        )
    space_id = cloud_space_id
    if content_scope == "space":
        path = f"{endpoint.api_root}/pages"
        query = {
            "space-id": space_id,
            "status": "current",
            "limit": page_limit,
        }
        output: list[ConfluenceInventoryItem] = []
    else:
        assert root_page_id is not None
        path = (
            f"{endpoint.api_root}/pages/{quote(root_page_id, safe='')}"
            "/descendants"
        )
        query = {"limit": page_limit}
        output = [ConfluenceInventoryItem(root_page_id, "", None)]
    initial_url = path + "?" + urlencode(query)
    current_url = initial_url
    seen = {item.page_id for item in output}
    visited: set[str] = set()
    remote_count = 0
    for _page_number in range(1, _MAX_PAGES + 1):
        current_url = _validate_api_url(endpoint, current_url)
        if current_url in visited:
            raise SourceManagerError(
                "Confluence Cloud pagination loop was detected",
                stage="fetch.confluence",
            )
        visited.add(current_url)
        payload = request_confluence_json(
            endpoint,
            current_url,
            http_get,
            connection,
            sleep=sleep,
            progress_callback=progress_callback,
        )
        results = _cloud_results(payload, field="Confluence Cloud page inventory")
        if len(results) > page_limit:
            raise SourceManagerError(
                "Confluence Cloud page inventory exceeded its page limit",
                stage="fetch.confluence",
            )
        for raw in results:
            if not isinstance(raw, Mapping):
                raise SourceManagerError(
                    "Confluence Cloud inventory contains an invalid item",
                    stage="fetch.confluence",
                )
            page_id = _page_id(raw.get("id"))
            if str(raw.get("type") or "page") != "page":
                raise SourceManagerError(
                    "Confluence Cloud inventory contains a non-page item",
                    stage="fetch.confluence",
                )
            if str(raw.get("spaceId") or "") != space_id:
                raise SourceManagerError(
                    "Confluence Cloud inventory has the wrong space identity",
                    stage="fetch.confluence",
                )
            if page_id in seen:
                raise SourceManagerError(
                    "Confluence Cloud inventory contains duplicate page IDs",
                    stage="fetch.confluence",
                )
            seen.add(page_id)
            version_value = raw.get("version")
            output.append(
                ConfluenceInventoryItem(
                    page_id,
                    str(raw.get("title") or "").strip(),
                    _optional_positive_int(
                        version_value.get("number")
                        if isinstance(version_value, Mapping)
                        else None,
                        field="Confluence Cloud page version",
                    ),
                )
            )
        remote_count += len(results)
        _emit(
            progress_callback,
            {
                "event": "provider.page",
                "provider": "confluence",
                "phase": "confluence.inventory",
                "completed": remote_count,
                "total": None,
                "unit": "pages",
                "current_item": _query_cursor(current_url) or "first",
            },
        )
        next_link = _next_link(payload)
        if not next_link:
            return output
        next_url = _validated_cloud_next_url(
            endpoint,
            current_url,
            next_link,
        )
        if urlsplit(next_url).path != urlsplit(initial_url).path:
            raise SourceManagerError(
                "Confluence Cloud pagination changed the inventory endpoint",
                stage="fetch.confluence",
            )
        current_url = next_url
    raise SourceManagerError(
        "Confluence Cloud pagination exceeded the safety limit",
        stage="fetch.confluence",
    )


def _cloud_space_id(
    endpoint: ConfluenceEndpoint,
    space_key: str,
    page_limit: int,
    http_get: HttpGet,
    connection: Mapping[str, Any] | object,
    *,
    progress_callback: ProgressCallback | None,
    sleep: Callable[[float], None],
) -> str:
    initial_url = f"{endpoint.api_root}/spaces?" + urlencode(
        {"keys": space_key, "limit": page_limit}
    )
    current_url = initial_url
    visited: set[str] = set()
    matches: list[str] = []
    for _page_number in range(1, _MAX_PAGES + 1):
        if current_url in visited:
            raise SourceManagerError(
                "Confluence Cloud space pagination loop was detected",
                stage="fetch.confluence",
            )
        visited.add(current_url)
        payload = request_confluence_json(
            endpoint,
            current_url,
            http_get,
            connection,
            sleep=sleep,
            progress_callback=progress_callback,
        )
        results = _cloud_results(payload, field="Confluence Cloud space lookup")
        for raw in results:
            if not isinstance(raw, Mapping):
                raise SourceManagerError(
                    "Confluence Cloud space lookup contains an invalid item",
                    stage="fetch.confluence",
                )
            if str(raw.get("key") or "") == space_key:
                matches.append(_page_id(raw.get("id")))
        next_link = _next_link(payload)
        if not next_link:
            break
        next_url = _validated_cloud_next_url(endpoint, current_url, next_link)
        if urlsplit(next_url).path != urlsplit(initial_url).path:
            raise SourceManagerError(
                "Confluence Cloud space pagination changed endpoint",
                stage="fetch.confluence",
            )
        current_url = next_url
    else:
        raise SourceManagerError(
            "Confluence Cloud space pagination exceeded the safety limit",
            stage="fetch.confluence",
        )
    if len(matches) != 1 or len(set(matches)) != 1:
        raise SourceManagerError(
            "Confluence Cloud space key did not resolve to exactly one space",
            stage="fetch.confluence",
        )
    return matches[0]


def _fetch_attachments(
    endpoint: ConfluenceEndpoint,
    page_id: str,
    page_limit: int,
    http_get: HttpGet,
    connection: Mapping[str, Any] | object,
    *,
    progress_callback: ProgressCallback | None,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    if endpoint.deployment == "cloud":
        return _fetch_cloud_attachments(
            endpoint,
            page_id,
            page_limit,
            http_get,
            connection,
            progress_callback=progress_callback,
            sleep=sleep,
        )
    path = (
        f"{endpoint.api_root}/content/{quote(page_id, safe='')}"
        "/child/attachment"
    )
    initial_url = path + "?" + urlencode({"limit": page_limit, "start": 0})
    current_url = initial_url
    visited: set[str] = set()
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    expected_total: int | None = None
    for _page_number in range(1, _MAX_PAGES + 1):
        if current_url in visited:
            raise SourceManagerError(
                "Confluence attachment pagination loop was detected",
                stage="fetch.confluence",
            )
        visited.add(current_url)
        current_start = _query_start(current_url)
        payload = request_confluence_json(
            endpoint,
            current_url,
            http_get,
            connection,
            sleep=sleep,
            progress_callback=progress_callback,
        )
        results, size, payload_limit = _page_envelope(
            payload,
            current_start=current_start,
            field="Confluence attachment inventory",
        )
        for raw in results:
            if not isinstance(raw, Mapping):
                raise SourceManagerError(
                    "Confluence attachment inventory contains an invalid item",
                    stage="fetch.confluence",
                )
            attachment_id = _page_id(raw.get("id"))
            if attachment_id in seen or str(raw.get("type") or "") != "attachment":
                raise SourceManagerError(
                    "Confluence attachment inventory identity is invalid",
                    stage="fetch.confluence",
                )
            seen.add(attachment_id)
            extensions = raw.get("extensions")
            if not isinstance(extensions, Mapping):
                extensions = {}
            version = raw.get("version")
            if not isinstance(version, Mapping):
                version = {}
            output.append(
                {
                    "id": attachment_id,
                    "title": str(raw.get("title") or "").strip(),
                    "media_type": str(extensions.get("mediaType") or "").strip(),
                    "file_size": _nonnegative_int(
                        extensions.get("fileSize"),
                        field="Confluence attachment file size",
                    ),
                    "version": _optional_positive_int(
                        version.get("number"),
                        field="Confluence attachment version",
                    ),
                }
            )
        supplied_total = _payload_total(payload)
        if supplied_total is not None:
            if expected_total is None:
                expected_total = supplied_total
            elif expected_total != supplied_total:
                raise SourceManagerError(
                    "Confluence attachment total changed during pagination",
                    stage="fetch.confluence",
                )
        next_link = _next_link(payload)
        if next_link:
            next_url = _validated_next_url(
                endpoint,
                current_url,
                next_link,
                current_start=current_start,
            )
            if urlsplit(next_url).path != urlsplit(initial_url).path:
                raise SourceManagerError(
                    "Confluence attachment pagination changed endpoint",
                    stage="fetch.confluence",
                )
            current_url = next_url
            continue
        if expected_total is not None and len(output) != expected_total:
            raise SourceManagerError(
                "Confluence attachment inventory ended before its total",
                stage="fetch.confluence",
            )
        if expected_total is None and size == payload_limit and size > 0:
            raise SourceManagerError(
                "Confluence attachment inventory omitted its next link",
                stage="fetch.confluence",
            )
        return output
    raise SourceManagerError(
        "Confluence attachment pagination exceeded the safety limit",
        stage="fetch.confluence",
    )


def _fetch_cloud_attachments(
    endpoint: ConfluenceEndpoint,
    page_id: str,
    page_limit: int,
    http_get: HttpGet,
    connection: Mapping[str, Any] | object,
    *,
    progress_callback: ProgressCallback | None,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    initial_url = (
        f"{endpoint.api_root}/pages/{quote(page_id, safe='')}/attachments?"
        + urlencode({"limit": page_limit})
    )
    current_url = initial_url
    visited: set[str] = set()
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for _page_number in range(1, _MAX_PAGES + 1):
        if current_url in visited:
            raise SourceManagerError(
                "Confluence Cloud attachment pagination loop was detected",
                stage="fetch.confluence",
            )
        visited.add(current_url)
        payload = request_confluence_json(
            endpoint,
            current_url,
            http_get,
            connection,
            sleep=sleep,
            progress_callback=progress_callback,
        )
        results = _cloud_results(
            payload,
            field="Confluence Cloud attachment inventory",
        )
        if len(results) > page_limit:
            raise SourceManagerError(
                "Confluence Cloud attachment inventory exceeded its page limit",
                stage="fetch.confluence",
            )
        for raw in results:
            if not isinstance(raw, Mapping):
                raise SourceManagerError(
                    "Confluence Cloud attachment inventory contains an invalid item",
                    stage="fetch.confluence",
                )
            attachment_id = _page_id(raw.get("id"))
            if attachment_id in seen:
                raise SourceManagerError(
                    "Confluence Cloud attachment inventory has duplicate IDs",
                    stage="fetch.confluence",
                )
            raw_type = str(raw.get("type") or "attachment")
            if raw_type != "attachment":
                raise SourceManagerError(
                    "Confluence Cloud attachment identity is invalid",
                    stage="fetch.confluence",
                )
            owner = raw.get("pageId")
            if owner not in {None, ""} and _page_id(owner) != page_id:
                raise SourceManagerError(
                    "Confluence Cloud attachment has the wrong page identity",
                    stage="fetch.confluence",
                )
            seen.add(attachment_id)
            extensions = raw.get("extensions")
            if not isinstance(extensions, Mapping):
                extensions = {}
            version = raw.get("version")
            if not isinstance(version, Mapping):
                version = {}
            output.append(
                {
                    "id": attachment_id,
                    "title": str(raw.get("title") or "").strip(),
                    "media_type": str(
                        raw.get("mediaType") or extensions.get("mediaType") or ""
                    ).strip(),
                    "file_size": _nonnegative_int(
                        raw.get("fileSize", extensions.get("fileSize", 0)),
                        field="Confluence Cloud attachment file size",
                    ),
                    "version": _optional_positive_int(
                        version.get("number"),
                        field="Confluence Cloud attachment version",
                    ),
                }
            )
        next_link = _next_link(payload)
        if not next_link:
            return output
        next_url = _validated_cloud_next_url(endpoint, current_url, next_link)
        if urlsplit(next_url).path != urlsplit(initial_url).path:
            raise SourceManagerError(
                "Confluence Cloud attachment pagination changed endpoint",
                stage="fetch.confluence",
            )
        current_url = next_url
    raise SourceManagerError(
        "Confluence Cloud attachment pagination exceeded the safety limit",
        stage="fetch.confluence",
    )


def _fetch_cloud_page_labels(
    endpoint: ConfluenceEndpoint,
    page_id: str,
    page_limit: int,
    http_get: HttpGet,
    connection: Mapping[str, Any] | object,
    *,
    progress_callback: ProgressCallback | None,
    sleep: Callable[[float], None],
) -> list[dict[str, str]]:
    initial_url = (
        f"{endpoint.api_root}/pages/{quote(page_id, safe='')}/labels?"
        + urlencode({"limit": page_limit})
    )
    current_url = initial_url
    visited: set[str] = set()
    output: list[dict[str, str]] = []
    for _page_number in range(1, _MAX_PAGES + 1):
        if current_url in visited:
            raise SourceManagerError(
                "Confluence Cloud label pagination loop was detected",
                stage="fetch.confluence",
            )
        visited.add(current_url)
        payload = request_confluence_json(
            endpoint,
            current_url,
            http_get,
            connection,
            sleep=sleep,
            progress_callback=progress_callback,
        )
        results = _cloud_results(payload, field="Confluence Cloud page labels")
        if len(results) > page_limit or len(output) + len(results) > _MAX_PAGE_LABELS:
            raise SourceManagerError(
                "Confluence Cloud page labels exceeded their safety limit",
                stage="fetch.confluence",
            )
        output.extend(_normalized_label(item) for item in results)
        next_link = _next_link(payload)
        if not next_link:
            return output
        next_url = _validated_cloud_next_url(endpoint, current_url, next_link)
        if urlsplit(next_url).path != urlsplit(initial_url).path:
            raise SourceManagerError(
                "Confluence Cloud label pagination changed endpoint",
                stage="fetch.confluence",
            )
        current_url = next_url
    raise SourceManagerError(
        "Confluence Cloud label pagination exceeded the safety limit",
        stage="fetch.confluence",
    )


def _fetch_cloud_page_ancestors(
    endpoint: ConfluenceEndpoint,
    page_id: str,
    page_limit: int,
    http_get: HttpGet,
    connection: Mapping[str, Any] | object,
    *,
    progress_callback: ProgressCallback | None,
    sleep: Callable[[float], None],
) -> list[dict[str, str]]:
    initial_url = (
        f"{endpoint.api_root}/pages/{quote(page_id, safe='')}/ancestors?"
        + urlencode({"limit": page_limit})
    )
    current_url = initial_url
    visited: set[str] = set()
    batches: list[list[dict[str, str]]] = []
    pivot_pagination = False
    total = 0
    root_path = urlsplit(endpoint.api_root).path.rstrip("/")
    expected_path = re.compile(
        rf"^{re.escape(root_path)}/pages/([1-9][0-9]{{0,18}})/ancestors$"
    )
    for _page_number in range(1, _MAX_PAGES + 1):
        if current_url in visited:
            raise SourceManagerError(
                "Confluence Cloud ancestor pagination loop was detected",
                stage="fetch.confluence",
            )
        visited.add(current_url)
        payload = request_confluence_json(
            endpoint,
            current_url,
            http_get,
            connection,
            sleep=sleep,
            progress_callback=progress_callback,
        )
        results = _cloud_results(payload, field="Confluence Cloud page ancestors")
        if len(results) > page_limit or total + len(results) > _MAX_PAGE_ANCESTORS:
            raise SourceManagerError(
                "Confluence Cloud page ancestors exceeded their safety limit",
                stage="fetch.confluence",
            )
        normalized = [_normalized_ancestor(item) for item in results]
        total += len(normalized)
        batches.append(normalized)
        next_link = _next_link(payload)
        if not next_link:
            ordered_batches = list(reversed(batches)) if pivot_pagination else batches
            flattened = [item for batch in ordered_batches for item in batch]
            ids = [item["id"] for item in flattened]
            if len(ids) != len(set(ids)):
                raise SourceManagerError(
                    "Confluence Cloud page ancestors contain duplicate IDs",
                    stage="fetch.confluence",
                )
            return flattened
        text = str(next_link).strip()
        if not text or text.startswith("//"):
            raise SourceManagerError(
                "Confluence Cloud ancestor pagination link is unsafe",
                stage="fetch.confluence",
            )
        next_url = _validate_api_url(endpoint, urljoin(current_url, text))
        next_path = urlsplit(next_url).path
        if next_path == urlsplit(current_url).path:
            next_url = _validated_cloud_next_url(endpoint, current_url, next_link)
        else:
            match = expected_path.fullmatch(next_path)
            if match is None or not normalized or match.group(1) != normalized[0]["id"]:
                raise SourceManagerError(
                    "Confluence Cloud ancestor pagination changed endpoint",
                    stage="fetch.confluence",
                )
            pivot_pagination = True
        current_url = next_url
    raise SourceManagerError(
        "Confluence Cloud ancestor pagination exceeded the safety limit",
        stage="fetch.confluence",
    )


def _bounded_metadata_text(value: Any, *, field: str, optional: bool = False) -> str:
    text = str(value or "").strip()
    if not text and optional:
        return ""
    if not text or len(text) > _MAX_METADATA_TEXT or any(
        ord(character) < 0x20 and character not in "\t" for character in text
    ):
        raise SourceManagerError(f"{field} is invalid", stage="fetch.confluence")
    return text


def _normalized_label(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise SourceManagerError(
            "Confluence page label is invalid", stage="fetch.confluence"
        )
    return {
        "name": _bounded_metadata_text(value.get("name"), field="Confluence label name"),
        "prefix": _bounded_metadata_text(
            value.get("prefix"), field="Confluence label prefix", optional=True
        ),
    }


def _normalized_ancestor(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or str(value.get("type") or "page") != "page":
        raise SourceManagerError(
            "Confluence page ancestor is invalid", stage="fetch.confluence"
        )
    return {
        "id": _page_id(value.get("id")),
        "title": _bounded_metadata_text(
            value.get("title"), field="Confluence ancestor title", optional=True
        ),
    }


def _validated_page_detail(
    endpoint: ConfluenceEndpoint,
    value: Any,
    page_id: str,
    space_key: str,
    *,
    cloud_space_id: str | None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceManagerError(
            "Confluence page detail must be an object",
            stage="fetch.confluence",
        )
    if _page_id(value.get("id")) != page_id or str(value.get("type") or "") != "page":
        raise SourceManagerError(
            "Confluence page detail has the wrong identity",
            stage="fetch.confluence",
        )
    status = str(value.get("status") or "").strip().lower()
    if status != "current":
        raise SourceManagerError(
            "Confluence page detail is not current",
            stage="fetch.confluence",
        )
    if endpoint.deployment == "cloud":
        if cloud_space_id is None or str(value.get("spaceId") or "") != cloud_space_id:
            raise SourceManagerError(
                "Confluence Cloud page detail has the wrong space identity",
                stage="fetch.confluence",
            )
    else:
        space = value.get("space")
        if not isinstance(space, Mapping) or str(space.get("key") or "") != space_key:
            raise SourceManagerError(
                "Confluence page detail has the wrong space identity",
                stage="fetch.confluence",
            )
    body = value.get("body")
    storage = body.get("storage") if isinstance(body, Mapping) else None
    if (
        not isinstance(storage, Mapping)
        or str(storage.get("representation") or "") != "storage"
        or not isinstance(storage.get("value"), str)
    ):
        raise SourceManagerError(
            "Confluence page detail is missing storage XHTML",
            stage="fetch.confluence",
        )
    version = value.get("version")
    if not isinstance(version, Mapping):
        raise SourceManagerError(
            "Confluence page detail version is invalid",
            stage="fetch.confluence",
        )
    version_number = _bounded_positive_int(
        version.get("number"),
        field="Confluence page version",
        maximum=2_147_483_647,
    )
    links = value.get("_links")
    if not isinstance(links, Mapping):
        raise SourceManagerError(
            "Confluence page detail links are invalid",
            stage="fetch.confluence",
        )
    labels: list[dict[str, str]] = []
    ancestors: list[dict[str, str]] = []
    if endpoint.deployment == "data_center":
        raw_ancestors = value.get("ancestors")
        if not isinstance(raw_ancestors, list) or len(raw_ancestors) > _MAX_PAGE_ANCESTORS:
            raise SourceManagerError(
                "Confluence page ancestors are invalid",
                stage="fetch.confluence",
            )
        ancestors = [_normalized_ancestor(item) for item in raw_ancestors]
        metadata = value.get("metadata")
        raw_labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
        label_results = raw_labels.get("results") if isinstance(raw_labels, Mapping) else None
        if not isinstance(label_results, list) or len(label_results) > _MAX_PAGE_LABELS:
            raise SourceManagerError(
                "Confluence page labels are invalid",
                stage="fetch.confluence",
            )
        labels = [_normalized_label(item) for item in label_results]
    updated_at_key = "createdAt" if endpoint.deployment == "cloud" else "when"
    if endpoint.deployment == "cloud":
        author = _bounded_metadata_text(
            version.get("authorId"), field="Confluence page author ID", optional=True
        )
    else:
        author = (
            _bounded_metadata_text(
                version.get("by", {}).get("displayName"),
                field="Confluence page author",
                optional=True,
            )
            if isinstance(version.get("by"), Mapping)
            else ""
        )
    return {
        "page_id": page_id,
        "title": str(value.get("title") or "").strip() or f"Page {page_id}",
        "space_key": space_key,
        "status": status,
        "version": version_number,
        "updated_at": _bounded_metadata_text(
            version.get(updated_at_key),
            field="Confluence page update timestamp",
            optional=True,
        ) or None,
        "author": author,
        "labels": labels,
        "ancestors": ancestors,
        "storage": storage["value"],
        "links": dict(links),
    }


def _page_urls(
    endpoint: ConfluenceEndpoint,
    detail: Mapping[str, Any],
    page_id: str,
) -> tuple[str, str]:
    links = detail["links"]
    collection = "pages" if endpoint.deployment == "cloud" else "content"
    expected_api = f"{endpoint.api_root}/{collection}/{quote(page_id, safe='')}"
    supplied_self = str(links.get("self") or "").strip()
    if supplied_self:
        api_url = _validate_api_url(endpoint, supplied_self)
        if urlsplit(api_url).path != urlsplit(expected_api).path:
            raise SourceManagerError(
                "Confluence page self link has the wrong identity",
                stage="fetch.confluence",
            )
    else:
        api_url = expected_api
    webui = str(links.get("webui") or "").strip()
    if not webui:
        raise SourceManagerError(
            "Confluence page detail is missing its webui link",
            stage="fetch.confluence",
        )
    base = str(links.get("base") or endpoint.web_root).strip()
    if base:
        canonical_base = _canonical_url_root(base, field="Confluence web base")
        if canonical_base != endpoint.web_root:
            raise SourceManagerError(
                "Confluence page web base does not match the configured site",
                stage="fetch.confluence",
            )
    if urlsplit(webui).scheme:
        web_url = webui
    else:
        if webui.startswith("//"):
            raise SourceManagerError(
                "Confluence webui link is unsafe",
                stage="fetch.confluence",
            )
        web_url = endpoint.web_root.rstrip("/") + "/" + webui.lstrip("/")
    return api_url, _validate_web_url(endpoint, web_url)


def _page_markdown(
    detail: Mapping[str, Any],
    body_markdown: str,
    attachments: Sequence[Mapping[str, Any]],
    *,
    fingerprint: str,
    api_url: str,
    web_url: str,
    relative_path: str,
    inventory_etag: str,
) -> str:
    metadata = {
        "api_url": api_url,
        "page_id": detail["page_id"],
        "path": relative_path,
        "schema_version": "local-rag-confluence-v1",
        "source_fingerprint": fingerprint,
        "inventory_etag": inventory_etag,
        "space_key": detail["space_key"],
        "version": detail["version"],
        "web_url": web_url,
    }
    marker = json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    lines = [
        f"# {detail['title']}",
        "",
        f"<!-- local-rag-confluence: {marker} -->",
        "",
        f"- Page ID: {detail['page_id']}",
        f"- Space: {detail['space_key']}",
        f"- Status: {detail['status']}",
        f"- Version: {detail['version']}",
        f"- API URL: {api_url}",
        f"- Web URL: {web_url}",
    ]
    if detail.get("updated_at"):
        lines.append(f"- Updated: {detail['updated_at']}")
    if detail.get("author"):
        lines.append(f"- Author: {detail['author']}")
    labels = list(detail.get("labels") or [])
    ancestors = list(detail.get("ancestors") or [])
    lines.append(
        "- Labels: "
        + (
            ", ".join(
                f"{item['prefix']}:{item['name']}" if item.get("prefix") else item["name"]
                for item in labels
            )
            if labels
            else "(none)"
        )
    )
    lines.append(
        "- Ancestors: "
        + (
            " > ".join(
                f"{item['title']} (id={item['id']})"
                if item.get("title")
                else f"id={item['id']}"
                for item in ancestors
            )
            if ancestors
            else "(none)"
        )
    )
    lines.extend(["", "## Body", "", body_markdown or "(empty)", ""])
    lines.extend(["## Attachments", ""])
    if attachments:
        for attachment in attachments:
            title = str(attachment.get("title") or "(untitled)")
            media_type = str(attachment.get("media_type") or "unknown")
            file_size = int(attachment.get("file_size") or 0)
            version = attachment.get("version")
            lines.append(
                f"- {title} (id={attachment['id']}, type={media_type}, "
                f"bytes={file_size}, version={version or 'unknown'})"
            )
    else:
        lines.append("- (none)")
    lines.extend(["", "## Structured attachment metadata", "", "```json"])
    lines.extend(
        json.dumps(
            list(attachments),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).splitlines()
    )
    lines.extend(["```", ""])
    return "\n".join(lines)


def _owned_pages(work: Path, fingerprint: str) -> dict[str, _OwnedPage]:
    root = Path(work)
    for entry in root.iterdir():
        if entry.name != "pages" or entry.is_symlink() or not entry.is_dir():
            raise SourceManagerError(
                "Confluence work directory contains an unknown entry",
                stage="fetch.confluence",
            )
    pages = root / "pages"
    if not pages.exists():
        return {}
    if pages.is_symlink() or not pages.is_dir():
        raise SourceManagerError(
            "Confluence pages directory is unsafe",
            stage="fetch.confluence",
        )
    output: dict[str, _OwnedPage] = {}
    for path in sorted(pages.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise SourceManagerError(
                "Confluence pages directory contains an unsafe entry",
                stage="fetch.confluence",
            )
        match_name = re.fullmatch(r"([1-9][0-9]{0,18})\.md", path.name)
        if match_name is None:
            raise SourceManagerError(
                "Confluence pages directory contains an unknown file",
                stage="fetch.confluence",
            )
        page_id = _page_id(match_name.group(1))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SourceManagerError(
                "Confluence managed page cannot be read",
                stage="fetch.confluence",
            ) from exc
        marker = _LOCAL_METADATA.search(text)
        if marker is None:
            raise SourceManagerError(
                "Confluence managed page metadata is missing",
                stage="fetch.confluence",
            )
        try:
            metadata = json.loads(marker.group(1))
        except json.JSONDecodeError as exc:
            raise SourceManagerError(
                "Confluence managed page metadata is invalid",
                stage="fetch.confluence",
            ) from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema_version") != "local-rag-confluence-v1"
            or metadata.get("page_id") != page_id
            or metadata.get("path") != confluence_page_relative_path(page_id)
            or metadata.get("source_fingerprint") != fingerprint
            or (
                metadata.get("inventory_etag") not in {None, ""}
                and re.fullmatch(
                    r"[0-9a-f]{64}", str(metadata.get("inventory_etag") or "")
                )
                is None
            )
            or page_id in output
        ):
            raise SourceManagerError(
                "Confluence managed page identity is invalid",
                stage="fetch.confluence",
            )
        output[page_id] = _OwnedPage(path=path, metadata=metadata)
    return output


def _atomic_if_changed(path: Path, text: str) -> bool:
    encoded = text.encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded:
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.{os.getpid()}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_windows_retry(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


def _replace_with_windows_retry(source: Path, target: Path) -> None:
    deadline = time.monotonic() + _WINDOWS_FILE_RETRY_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            retryable = (
                os.name == "nt"
                and (
                    getattr(exc, "winerror", None) in {5, 32, 33}
                    or exc.errno in {errno.EACCES, errno.EPERM}
                )
                and time.monotonic() < deadline
            )
            if not retryable:
                raise
            time.sleep(0.05)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _render_children(element: Any, *, list_depth: int) -> str:
    parts = [_escape_text(element.text or "")]
    for child in element:
        parts.append(_render_element(child, list_depth=list_depth))
        parts.append(_escape_text(child.tail or ""))
    return "".join(parts)


def _render_element(element: Any, *, list_depth: int) -> str:
    tag = _tag(element)
    if tag in _FORBIDDEN_HTML_TAGS:
        return ""
    if tag == "br":
        return "\n"
    if tag in {"strong", "b"}:
        return f"**{_render_children(element, list_depth=list_depth).strip()}**"
    if tag in {"em", "i"}:
        return f"*{_render_children(element, list_depth=list_depth).strip()}*"
    if tag == "code" and _tag(element.getparent()) != "pre":
        return f"`{''.join(element.itertext()).strip()}`"
    if tag == "pre":
        code = "".join(element.itertext()).strip("\n")
        return f"\n```\n{code}\n```\n\n"
    if tag == "link" and str(getattr(element, "tag", "")).startswith(
        "{urn:atlassian:confluence}"
    ):
        return _render_confluence_link(element, list_depth=list_depth)
    if tag == "a":
        label = _render_children(element, list_depth=list_depth).strip()
        href = str(element.get("href") or "").strip()
        if href and _safe_link(href):
            return f"[{label or href}]({href})"
        return label
    if tag == "img":
        return _escape_text(str(element.get("alt") or "").strip())
    if tag in {"ul", "ol"}:
        ordered = tag == "ol"
        lines: list[str] = []
        ordinal = 1
        for child in element:
            if _tag(child) != "li":
                continue
            content = _render_children(child, list_depth=list_depth + 1).strip()
            prefix = f"{ordinal}. " if ordered else "- "
            indent = "  " * list_depth
            content = content.replace("\n", "\n" + indent + "  ")
            lines.append(indent + prefix + content)
            ordinal += 1
        return "\n" + "\n".join(lines) + "\n\n"
    if tag == "table":
        return _render_table(element)
    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(tag[1])
        body = _render_children(element, list_depth=list_depth).strip()
        return f"\n{'#' * level} {body}\n\n"
    if tag in {"p", "div", "section", "article", "blockquote"}:
        body = _render_children(element, list_depth=list_depth).strip()
        if not body:
            return ""
        if tag == "blockquote":
            body = "\n".join(f"> {line}" for line in body.splitlines())
        return f"\n{body}\n\n"
    if tag.endswith("structured-macro"):
        name = _attribute_suffix(element, "name") or "unknown"
        body = _render_children(element, list_depth=list_depth).strip()
        suffix = f"\n\n{body}" if body else ""
        return f"\n[Confluence macro: {name}]{suffix}\n\n"
    if tag.endswith("attachment"):
        filename = _attribute_suffix(element, "filename")
        return f"[Attachment: {filename}]" if filename else "[Attachment]"
    return _render_children(element, list_depth=list_depth)


def _render_confluence_link(element: Any, *, list_depth: int) -> str:
    label = ""
    target: Any | None = None
    for child in element.iter():
        tag = _tag(child)
        if tag in {"plain-text-link-body", "link-body"}:
            label = _render_children(child, list_depth=list_depth).strip()
        if child is not element and tag in {"page", "url", "attachment"}:
            if target is not None:
                raise SourceManagerError(
                    "Confluence storage link has multiple targets",
                    stage="fetch.confluence",
                )
            target = child
    if target is None:
        return label
    target_tag = _tag(target)
    if target_tag == "url":
        href = _attribute_suffix(target, "value")
        if href and _safe_link(href):
            return f"[{label or href}]({href})"
        return label
    if target_tag == "page":
        title = _bounded_metadata_text(
            _attribute_suffix(target, "content-title"),
            field="Confluence linked page title",
        )
        space_key = _bounded_metadata_text(
            _attribute_suffix(target, "space-key"),
            field="Confluence linked page space",
            optional=True,
        )
        identity = f"{space_key}:{title}" if space_key else title
        return f"{label or title} [Confluence page: {identity}]"
    filename = _bounded_metadata_text(
        _attribute_suffix(target, "filename"),
        field="Confluence linked attachment filename",
    )
    return f"{label or filename} [Confluence attachment: {filename}]"


def _render_table(element: Any) -> str:
    rows: list[list[str]] = []
    for row in element.iter():
        if _tag(row) != "tr":
            continue
        cells: list[str] = []
        for cell in row:
            if _tag(cell) not in {"th", "td"}:
                continue
            value = _render_children(cell, list_depth=0)
            value = re.sub(r"\s+", " ", value).strip().replace("|", r"\|")
            cells.append(value)
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(row) + " |" for row in padded]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n" + "\n".join(lines) + "\n\n"


def _normalize_markdown(value: str) -> str:
    lines: list[str] = []
    blank = False
    in_fence = False
    for raw in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not in_fence:
            line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            if not blank and lines:
                lines.append("")
            blank = True
            continue
        lines.append(line)
        blank = False
    return "\n".join(lines).strip()


def _escape_text(value: str) -> str:
    return value.replace("\x00", "")


def _safe_link(value: str) -> bool:
    split = urlsplit(value)
    return (
        split.scheme.lower() in _SAFE_LINK_SCHEMES
        and not split.username
        and not split.password
        and not value.startswith("//")
    )


def _tag(element: Any) -> str:
    value = str(getattr(element, "tag", "")).lower()
    if "}" in value:
        value = value.rsplit("}", 1)[-1]
    return value


def _attribute_suffix(element: Any, suffix: str) -> str:
    expected = suffix.casefold()
    for name, value in element.attrib.items():
        key = str(name).casefold().rsplit("}", 1)[-1].rsplit(":", 1)[-1]
        if key == expected:
            return str(value or "").strip()
    return ""


def _authorization_headers(
    connection: Mapping[str, Any] | object,
    *,
    deployment: str,
) -> dict[str, str]:
    auth_type = _connection_text(connection, "auth_type").lower()
    expected_auth_type = "basic" if deployment == "cloud" else "bearer"
    if auth_type != expected_auth_type:
        raise SourceManagerError(
            f"Confluence {deployment} connection requires {expected_auth_type} auth",
            stage="fetch.confluence",
        )
    if auth_type == "basic":
        username = _connection_text(connection, "username") or _connection_text(
            connection, "email"
        )
        secret = _connection_text(connection, "api_token") or _connection_text(
            connection, "password"
        )
        if not username or not secret:
            raise SourceManagerError(
                "Confluence Basic connection is incomplete",
                stage="fetch.confluence",
            )
        encoded = base64.b64encode(
            f"{username}:{secret}".encode("utf-8")
        ).decode("ascii")
        authorization = f"Basic {encoded}"
    elif auth_type == "bearer":
        secret = _connection_text(connection, "token") or _connection_text(
            connection, "access_token"
        )
        if not secret:
            raise SourceManagerError(
                "Confluence Bearer connection is incomplete",
                stage="fetch.confluence",
            )
        authorization = f"Bearer {secret}"
    else:
        raise SourceManagerError(
            "Confluence auth_type must be basic or bearer",
            stage="fetch.confluence",
        )
    return {
        "Accept": "application/json",
        "Authorization": authorization,
    }


def _connection_text(connection: Mapping[str, Any] | object, name: str) -> str:
    if isinstance(connection, Mapping):
        value = connection.get(name)
    else:
        value = getattr(connection, name, None)
    return str(value or "").strip()


def _credential_mapping(
    credentials: Mapping[str, Any] | object,
) -> dict[str, Any]:
    fields = (
        "connection_id",
        "deployment",
        "cloud_scope",
        "site_url",
        "context_path",
        "api_root",
        "cloud_id",
    )
    output: dict[str, Any] = {}
    for field in fields:
        if isinstance(credentials, Mapping):
            output[field] = credentials.get(field)
        else:
            output[field] = getattr(credentials, field, None)
    return output


def _site_origin(value: Any) -> str:
    text = str(value or "").strip()
    split = urlsplit(text)
    if (
        split.scheme.lower() != "https"
        or not split.hostname
        or split.username
        or split.password
        or split.query
        or split.fragment
        or split.path not in {"", "/"}
    ):
        raise SourceManagerError(
            "Confluence site_url must be an HTTPS origin without a path"
        )
    host = split.hostname.encode("idna").decode("ascii").lower()
    port = f":{split.port}" if split.port is not None else ""
    return f"https://{host}{port}"


def _validate_url_path(value: str, *, field: str) -> None:
    """Reject traversal and separator smuggling before an authenticated GET."""

    current = str(value or "")
    for _depth in range(4):
        if re.search(r"%(?![0-9a-fA-F]{2})", current):
            raise SourceManagerError(f"{field} contains invalid percent encoding")
        if "\\" in current or re.search(r"%(?:2f|5c)", current, re.IGNORECASE):
            raise SourceManagerError(f"{field} contains an encoded path separator")
        if any(part in {".", ".."} for part in current.split("/")):
            raise SourceManagerError(f"{field} contains path traversal")
        try:
            decoded = unquote(current, encoding="utf-8", errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise SourceManagerError(f"{field} contains invalid percent encoding") from exc
        if decoded == current:
            return
        current = decoded
    if current != unquote(current, encoding="utf-8", errors="strict"):
        raise SourceManagerError(f"{field} is excessively percent encoded")
    if "\\" in current or any(part in {".", ".."} for part in current.split("/")):
        raise SourceManagerError(f"{field} contains path traversal")


def _context_path(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if text in {"", "/"}:
        return ""
    if (
        not text.startswith("/")
        or text.endswith("/")
        or "?" in text
        or "#" in text
        or any(part == "" for part in text.split("/")[1:])
    ):
        raise SourceManagerError("Confluence context_path is invalid")
    _validate_url_path(text, field="Confluence context_path")
    return text


def _canonical_url_root(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    split = urlsplit(text)
    if (
        split.scheme.lower() != "https"
        or not split.hostname
        or split.username
        or split.password
        or split.query
        or split.fragment
        or not split.path.startswith("/")
        or split.path.endswith("/")
    ):
        raise SourceManagerError(f"{field} is invalid")
    _validate_url_path(split.path, field=field)
    host = split.hostname.encode("idna").decode("ascii").lower()
    port = f":{split.port}" if split.port is not None else ""
    return urlunsplit(("https", host + port, split.path, "", ""))


def _validate_api_url(endpoint: ConfluenceEndpoint, value: Any) -> str:
    text = str(value or "").strip()
    split = urlsplit(text)
    root = urlsplit(endpoint.api_root)
    if (
        split.scheme.lower() != "https"
        or split.hostname is None
        or split.username
        or split.password
        or split.fragment
        or split.hostname.encode("idna").decode("ascii").lower()
        != root.hostname
        or split.port != root.port
        or not (
            split.path == root.path
            or split.path.startswith(root.path.rstrip("/") + "/")
        )
    ):
        raise SourceManagerError(
            "Confluence API URL escaped its configured origin/context",
            stage="fetch.confluence",
        )
    _validate_url_path(split.path, field="Confluence API URL")
    return urlunsplit(("https", split.netloc.lower(), split.path, split.query, ""))


def _validate_web_url(endpoint: ConfluenceEndpoint, value: Any) -> str:
    text = str(value or "").strip()
    split = urlsplit(text)
    root = urlsplit(endpoint.web_root)
    context = root.path.rstrip("/")
    if (
        split.scheme.lower() != "https"
        or split.hostname is None
        or split.username
        or split.password
        or split.fragment
        or split.hostname.encode("idna").decode("ascii").lower()
        != root.hostname
        or split.port != root.port
        or (context and not (split.path == context or split.path.startswith(context + "/")))
    ):
        raise SourceManagerError(
            "Confluence web URL escaped its configured site/context",
            stage="fetch.confluence",
        )
    _validate_url_path(split.path, field="Confluence web URL")
    return urlunsplit(("https", split.netloc.lower(), split.path, split.query, ""))


def _validated_next_url(
    endpoint: ConfluenceEndpoint,
    current_url: str,
    next_link: str,
    *,
    current_start: int,
) -> str:
    text = str(next_link or "").strip()
    if not text or text.startswith("//"):
        raise SourceManagerError(
            "Confluence pagination next link is unsafe",
            stage="fetch.confluence",
        )
    if endpoint.deployment == "data_center" and (
        text == "/rest/api"
        or text.startswith("/rest/api/")
        or text.startswith("/rest/api?")
    ):
        candidate = endpoint.site_url + endpoint.context_path + text
    else:
        candidate = urljoin(current_url, text)
    next_url = _validate_api_url(endpoint, candidate)
    if _query_start(next_url) <= current_start:
        raise SourceManagerError(
            "Confluence pagination did not move forward",
            stage="fetch.confluence",
        )
    return next_url


def _validated_cloud_next_url(
    endpoint: ConfluenceEndpoint,
    current_url: str,
    next_link: str,
) -> str:
    text = str(next_link or "").strip()
    if not text or text.startswith("//"):
        raise SourceManagerError(
            "Confluence Cloud pagination next link is unsafe",
            stage="fetch.confluence",
        )
    next_url = _validate_api_url(endpoint, urljoin(current_url, text))
    current_cursor = _query_cursor(current_url)
    next_cursor = _query_cursor(next_url)
    if not next_cursor or next_cursor == current_cursor or next_url == current_url:
        raise SourceManagerError(
            "Confluence Cloud pagination cursor did not move forward",
            stage="fetch.confluence",
        )
    return next_url


def _query_cursor(url: str) -> str:
    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get("cursor")
    if values is None:
        return ""
    if len(values) != 1 or not values[0] or len(values[0]) > 4_096:
        raise SourceManagerError(
            "Confluence Cloud pagination cursor is invalid",
            stage="fetch.confluence",
        )
    return values[0]


def _cloud_results(payload: Any, *, field: str) -> list[Any]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
        raise SourceManagerError(
            f"{field} response must contain a results array",
            stage="fetch.confluence",
        )
    links = payload.get("_links")
    if links is not None and not isinstance(links, Mapping):
        raise SourceManagerError(
            f"{field} links are invalid",
            stage="fetch.confluence",
        )
    return list(payload["results"])


def _page_envelope(
    payload: Any,
    *,
    current_start: int,
    field: str,
) -> tuple[list[Any], int, int]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
        raise SourceManagerError(
            f"{field} response must contain a results array",
            stage="fetch.confluence",
        )
    results = list(payload["results"])
    start = _nonnegative_int(payload.get("start"), field=f"{field} start")
    size = _nonnegative_int(payload.get("size"), field=f"{field} size")
    limit = _bounded_positive_int(
        payload.get("limit"),
        field=f"{field} limit",
        maximum=10_000,
    )
    if start != current_start or size != len(results) or size > limit:
        raise SourceManagerError(
            f"{field} pagination envelope is inconsistent",
            stage="fetch.confluence",
        )
    return results, size, limit


def _payload_total(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("totalSize", payload.get("total"))
    if raw is None:
        return None
    return _nonnegative_int(raw, field="Confluence pagination total")


def _next_link(payload: Mapping[str, Any]) -> str:
    links = payload.get("_links")
    if links is None:
        return ""
    if not isinstance(links, Mapping):
        raise SourceManagerError(
            "Confluence pagination links are invalid",
            stage="fetch.confluence",
        )
    return str(links.get("next") or "").strip()


def _query_start(url: str) -> int:
    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get("start")
    if values is None or len(values) != 1:
        raise SourceManagerError(
            "Confluence pagination start is missing",
            stage="fetch.confluence",
        )
    return _nonnegative_int(values[0], field="Confluence pagination start")


def _validate_inventory_space(value: Mapping[str, Any], space_key: str) -> None:
    space = value.get("space")
    if space is None:
        return
    if not isinstance(space, Mapping) or str(space.get("key") or "") != space_key:
        raise SourceManagerError(
            "Confluence inventory has the wrong space identity",
            stage="fetch.confluence",
        )


def _page_detail_request_url(endpoint: ConfluenceEndpoint, page_id: str) -> str:
    if endpoint.deployment == "cloud":
        return (
            f"{endpoint.api_root}/pages/{quote(page_id, safe='')}?"
            + urlencode({"body-format": "storage"})
        )
    return (
        f"{endpoint.api_root}/content/{quote(page_id, safe='')}?"
        + urlencode(
            {"expand": "body.storage,version,space,ancestors,metadata.labels"}
        )
    )


def _page_link(page_id: str, api_url: str, web_url: str) -> dict[str, str]:
    return {
        "page_id": page_id,
        "path": confluence_page_relative_path(page_id),
        "api_url": api_url,
        "web_url": web_url,
    }


def _source_fingerprint(
    endpoint: ConfluenceEndpoint,
    space_key: str,
    content_scope: str,
    root_page_id: str | None,
) -> str:
    identity = "\0".join(
        [
            "local-rag-confluence-source-v1",
            endpoint.deployment,
            endpoint.cloud_scope or "",
            endpoint.site_url,
            endpoint.context_path,
            endpoint.api_root,
            endpoint.cloud_id or "",
            space_key,
            content_scope,
            root_page_id or "",
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _inventory_etag(fingerprint: str, page_ids: Sequence[str]) -> str:
    payload = json.dumps(
        {
            "page_ids": list(page_ids),
            "schema": "local-rag-confluence-inventory-v1",
            "source_fingerprint": fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validated_page_ids(values: Sequence[Any]) -> list[str]:
    if isinstance(values, (str, bytes, bytearray)):
        raise SourceManagerError("Confluence stable page inventory is invalid")
    output = [_page_id(value) for value in values]
    if len(output) != len(set(output)):
        raise SourceManagerError("Confluence stable page inventory has duplicates")
    return output


def _page_id(value: Any) -> str:
    if isinstance(value, bool):
        raise SourceManagerError("Confluence page ID must be a positive integer")
    text = str(value or "").strip()
    if _PAGE_ID.fullmatch(text) is None or int(text) > _MAX_PAGE_ID:
        raise SourceManagerError("Confluence page ID must be a positive integer")
    return text


def _space_key(value: Any) -> str:
    text = str(value or "").strip()
    if _SPACE_KEY.fullmatch(text) is None:
        raise SourceManagerError("Confluence space_key is invalid")
    return text


def _cloud_id(value: Any) -> str:
    text = str(value or "").strip()
    if _CLOUD_ID.fullmatch(text) is None:
        raise SourceManagerError("Confluence cloud_id is invalid")
    return text


def _bounded_positive_int(value: Any, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not str(value).isdigit():
        raise SourceManagerError(f"{field} must be a positive integer")
    result = int(value)
    if result < 1 or result > maximum:
        raise SourceManagerError(f"{field} must be a positive bounded integer")
    return result


def _optional_positive_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _bounded_positive_int(value, field=field, maximum=2_147_483_647)


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not str(value).isdigit():
        raise SourceManagerError(f"{field} must be a non-negative integer")
    result = int(value)
    if result < 0 or result > 2_147_483_647:
        raise SourceManagerError(f"{field} must be a bounded integer")
    return result


def _retryable_network_exception(exc: BaseException) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError))


def _retry_after_seconds(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return min(300.0, max(0.0, float(text)))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            return None
        return min(300.0, max(0.0, parsed.timestamp() - time.time()))
    except (TypeError, ValueError, OverflowError):
        return None


def _header(headers: Mapping[str, Any], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value or "").strip()
    return ""


def _emit_http_progress(
    callback: ProgressCallback | None,
    url: str,
    attempt: int,
    *,
    status: int | None,
    retry: bool,
) -> None:
    _emit(
        callback,
        {
            "event": "confluence.http_attempt",
            "provider": "confluence",
            "method": "GET",
            "url": url,
            "attempt": attempt,
            "max_attempts": 3,
            "status": status,
            "retry": retry,
            "request_headers_redacted_count": 1,
        },
    )


def _emit(callback: ProgressCallback | None, event: Mapping[str, Any]) -> None:
    if callback is not None:
        callback(dict(event))


def _remove_empty_pages_directory(work: Path) -> None:
    pages = Path(work) / "pages"
    if not pages.exists():
        return
    try:
        pages.rmdir()
    except OSError:
        pass
