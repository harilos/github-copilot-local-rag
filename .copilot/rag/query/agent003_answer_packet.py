from __future__ import annotations
import copy
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit
SEARCH_SCHEMA_VERSION = "local-rag-answer-packet-v2"
DETAIL_SCHEMA_VERSION = "local-rag-evidence-detail-v1"
MAX_TOOL_RESULT_BYTES = 1_048_576
MAX_DETAIL_EVIDENCE_ITEMS = 3
MAX_INSPECTABLE_IDS = 6
MAX_ROUTING_CANDIDATES = 5
_STATUS_CONTRACTS = {"ok": ("answer_now", "full"), "partial": ("answer_partial", "partial"),
                     "no_hit": ("report_no_hit", "none"), "error": ("report_error", "none"),
                     "response_too_large": ("report_response_too_large", "none"),
                     "stale_result": ("report_stale_result", "none"),
                     "database_required": ("choose_database", "none")}
_DATABASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}-rag$")
_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")
_TOKEN_RE = re.compile(r"^lrt_[A-Za-z0-9_-]{20,92}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_POINTER_RE = re.compile(r"(?i)\b(?:summary_file|detail_file|result_set_id|resource(?:_uri)?|"
                         r"file_uri|source_path|path)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_URI_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:file|https?|ftp|ssh|data|urn|vscode|resource|mcp):"
                     r"(?://)?[^\s<>{}\[\]\"'`]+")
_PATH_RES = (
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/][^\s<>{}\[\]\"']*"),
    re.compile(r"(?<![A-Za-z0-9])(?:\\\\|//)[A-Za-z0-9._$-]+(?:[\\/][^\s]*)?"),
    re.compile(r"(?<![A-Za-z0-9])(?:~[\\/]|\.\.?[\\/])[^\s<>{}\[\]\"']+"),
    re.compile(r"(?<![\w:])/(?:[\w._-]+/)*[\w._-]+"),
    re.compile(r"(?i)(?<![\w:/])(?:[\w._-]+[\\/])+[\w._-]+"),
    re.compile(r"(?i)(?<![A-Za-z0-9])(?:\.copilot|\.github|tools|src|query|docs?|"
               r"results?|spool|items)(?:[\\/][\w._-]+)+"),
    re.compile(r"(?i)(?<![\w._-])[\w][\w._-]{0,120}\."
               r"(?:md|txt|json|ya?ml|toml|log|csv|py|ps1|sh|js|sql|db|sqlite|pdf)(?![A-Za-z0-9])"),
)
_LOCATOR_OMITTED = "[locator omitted]"
_LOSS_NOTICES = {"requested_evidence_unavailable"}
class PacketContractError(ValueError): pass
def packet_output_schema(schema: str) -> dict[str, Any]:
    """Return the closed-world MCP output schema for one packet kind."""
    if schema not in {SEARCH_SCHEMA_VERSION, DETAIL_SCHEMA_VERSION}:
        raise PacketContractError("unknown packet schema")
    evidence = {
        "type": "object", "additionalProperties": False,
        "required": ["id", "text", "source_title", "citation_label"],
        "properties": {
            "id": {"type": "string"}, "text": {"type": "string"},
            "source_title": {"type": "string"},
            "citation_label": {"type": "string"},
            "url": {"type": "string", "pattern": "^https://"},
        },
    }
    properties: dict[str, Any] = {
        "schema_version": {"type": "string", "const": schema},
        "status": {"type": "string", "enum": list(_STATUS_CONTRACTS)},
        "next_action": {"type": "string", "enum": sorted({
            action for action, _ in _STATUS_CONTRACTS.values()
        } | {"inspect_evidence"})},
        "answerability": {"type": "string", "enum": ["full", "partial", "none"]},
        "database": {"type": "string"}, "payload_complete": {"const": True},
        "result_token": {"type": "string"},
        "evidence": {"type": "array", "items": evidence},
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "notices": {"type": "array", "items": {"type": "string"}},
    }
    extra = "inspectable_evidence_ids" if schema == SEARCH_SCHEMA_VERSION else "requested_evidence_ids"
    properties[extra] = {"type": "array", "items": {"type": "string"}}
    if schema == SEARCH_SCHEMA_VERSION:
        properties["candidates"] = {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "title", "query_hint", "content_summary"],
            "properties": {key: {"type": "string"} for key in
                           ("name", "title", "query_hint", "content_summary")},
        }}
    return {"type": "object", "additionalProperties": False,
            "required": list(properties), "properties": properties}
def build_search_packet(payload: Mapping[str, Any], *, result_token: str = "",
                        inspectable_evidence_ids: Iterable[str] = ()) -> dict[str, Any]:
    """Project a daemon summary without exposing its result file or identifier."""
    if _normalized_status(payload.get("status")) == "database_required":
        return _build_routing_packet(payload)
    source = payload.get("summary")
    source = source if isinstance(source, Mapping) else payload
    token = _validated_token(result_token, allow_empty=True)
    inspectable = _validated_ids(inspectable_evidence_ids, MAX_INSPECTABLE_IDS)
    if inspectable and not token:
        raise PacketContractError("inspectable evidence requires an opaque token")
    evidence, projection_notices = _project_evidence(source.get("evidence") or [])
    raw_status = _normalized_status(source.get("status") or payload.get("status"))
    raw_answerability = str(source.get("answerability") or "").casefold()
    status = _answer_status(raw_status, raw_answerability, evidence, False,
                            bool(token and inspectable))
    missing = _visible_list(source.get("missing_information") or
                            payload.get("missing_information") or [])
    notices = _unique([*projection_notices, *_visible_list(
        source.get("warnings") or source.get("notices") or [])])
    missing = _complete_missing(status, missing, False)
    if status not in {"ok", "partial"}:
        evidence = []
    packet = _base_packet(SEARCH_SCHEMA_VERSION, status, _database(payload, source),
                          evidence, missing, notices, token)
    packet["inspectable_evidence_ids"] = inspectable if token else []
    packet["candidates"] = []
    if status == "partial" and token and inspectable:
        packet["next_action"] = "inspect_evidence"
    return validate_packet(packet)
def build_evidence_detail(payload: Mapping[str, Any], *, result_token: str,
                          evidence_ids: Iterable[str]) -> dict[str, Any]:
    """Project up to three internally loaded evidence items for the model."""
    if _normalized_status(payload.get("status")) in {"stale_result", "expired"}:
        return build_stale_evidence_detail()
    token = _validated_token(result_token)
    requested = _validated_ids(evidence_ids, MAX_DETAIL_EVIDENCE_ITEMS)
    if not requested:
        raise PacketContractError("at least one evidence id is required")
    raw_items = payload.get("expanded_items") or payload.get("evidence") or []
    by_id = {str(item.get("item_id") or item.get("id")): item for item in raw_items
             if isinstance(item, Mapping)} if isinstance(raw_items, list) else {}
    selected = [by_id[item_id] for item_id in requested if item_id in by_id]
    evidence, notices = _project_evidence(selected)
    lossy = False
    unavailable = [item_id for item_id in requested if item_id not in by_id]
    if unavailable and evidence:
        lossy = True
        notices = _unique([*notices, "requested_evidence_unavailable"])
    raw_status = _normalized_status(payload.get("status"))
    status = ("error" if raw_status == "error" else "partial" if evidence and lossy
              else "ok" if evidence else "no_hit")
    missing = _complete_missing(status, [], lossy or bool(unavailable))
    packet = _base_packet(DETAIL_SCHEMA_VERSION, status, _database(payload, payload),
                          evidence, missing, notices, token)
    packet["requested_evidence_ids"] = requested
    return validate_packet(packet)
def build_stale_evidence_detail() -> dict[str, Any]:
    """Return a small non-leaking response for invalid or expired tokens."""
    packet = _base_packet(DETAIL_SCHEMA_VERSION, "stale_result", "", [],
                          ["The requested Local RAG evidence is no longer available."],
                          ["stale_result"], "")
    packet["requested_evidence_ids"] = []
    return validate_packet(packet)
def build_error_packet(schema: str, code: str) -> dict[str, Any]:
    """Return a closed-world error packet for the tool that raised it."""
    if schema not in {SEARCH_SCHEMA_VERSION, DETAIL_SCHEMA_VERSION}:
        raise PacketContractError("unknown packet schema")
    safe_code, _, _ = sanitize_visible_text(code)
    notice = safe_code or "local_rag_error"
    message = (
        "The Local RAG response exceeded the configured payload limit."
        if notice == "response_too_large"
        else "Local RAG could not return usable evidence."
    )
    status = "response_too_large" if notice == "response_too_large" else "error"
    packet = _base_packet(schema, status, "", [], [message], [notice], "")
    if schema == SEARCH_SCHEMA_VERSION:
        packet.update({"inspectable_evidence_ids": [], "candidates": []})
    else:
        packet["requested_evidence_ids"] = []
    return validate_packet(packet)
def validate_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(packet, Mapping):
        raise PacketContractError("packet must be an object")
    schema = packet.get("schema_version")
    common = {
        "schema_version", "status", "next_action", "answerability", "database",
        "payload_complete", "evidence", "missing_information", "notices",
        "result_token",
    }
    if schema not in {SEARCH_SCHEMA_VERSION, DETAIL_SCHEMA_VERSION}:
        raise PacketContractError("unknown packet schema")
    expected = common | (
        {"inspectable_evidence_ids", "candidates"} if schema == SEARCH_SCHEMA_VERSION
        else {"requested_evidence_ids"}
        if schema == DETAIL_SCHEMA_VERSION
        else set()
    )
    if not expected or set(packet) != expected:
        raise PacketContractError("packet fields do not match its schema")
    status = packet.get("status")
    if status not in _STATUS_CONTRACTS or (
        status == "stale_result" and schema != DETAIL_SCHEMA_VERSION
    ) or (
        status == "database_required" and schema != SEARCH_SCHEMA_VERSION
    ):
        raise PacketContractError("invalid packet status")
    action = packet.get("next_action")
    expected_action, expected_answerability = _STATUS_CONTRACTS[status]
    valid_action = action == expected_action or (
        schema == SEARCH_SCHEMA_VERSION and status == "partial" and
        action == "inspect_evidence"
    )
    if not valid_action or packet.get("answerability") != expected_answerability:
        raise PacketContractError("status and next action disagree")
    if packet.get("payload_complete") is not True:
        raise PacketContractError("packet is incomplete")
    database = packet.get("database")
    if not isinstance(database, str) or (database and not _DATABASE_RE.fullmatch(database)):
        raise PacketContractError("invalid database")
    token = packet.get("result_token")
    if not isinstance(token, str) or (token and not _TOKEN_RE.fullmatch(token)):
        raise PacketContractError("invalid opaque token")
    evidence = packet.get("evidence")
    if not isinstance(evidence, list) or (
        schema == DETAIL_SCHEMA_VERSION and
        len(evidence) > MAX_DETAIL_EVIDENCE_ITEMS
    ):
        raise PacketContractError("invalid evidence list")
    _validate_evidence(evidence)
    missing = _validate_visible_list(packet.get("missing_information"), "missing")
    notices = _validate_visible_list(packet.get("notices"), "notices")
    if status in {"ok", "partial"} and not evidence and action != "inspect_evidence":
        raise PacketContractError("answerable packet has no evidence")
    if status not in {"ok", "partial"} and evidence:
        raise PacketContractError("non-answer packet contains evidence")
    if status == "ok" and missing:
        raise PacketContractError("full answer reports missing information")
    if status in {
        "partial", "no_hit", "error", "response_too_large", "stale_result"
    } and not missing:
        raise PacketContractError("non-full result requires missing information")
    if _LOSS_NOTICES.intersection(notices) and status != "partial":
        raise PacketContractError("lossy projection must be partial")
    if schema == SEARCH_SCHEMA_VERSION:
        ids = _validate_id_list(packet.get("inspectable_evidence_ids"), MAX_INSPECTABLE_IDS)
        candidates = packet.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > MAX_ROUTING_CANDIDATES:
            raise PacketContractError("invalid routing candidates")
        for candidate in candidates:
            if not isinstance(candidate, Mapping) or set(candidate) != {"name", "title", "query_hint", "content_summary"} or not _DATABASE_RE.fullmatch(str(candidate.get("name") or "")):
                raise PacketContractError("invalid routing candidate")
            for key in ("title", "query_hint", "content_summary"):
                _assert_visible(candidate.get(key))
        if status == "database_required" and (not candidates or token or ids or database):
            raise PacketContractError("database routing packet is inconsistent")
        if status != "database_required" and candidates:
            raise PacketContractError("answer packet contains routing candidates")
        if action == "inspect_evidence" and not (token and ids):
            raise PacketContractError("inspect_evidence requires an opaque token and ids")
    else:
        requested = _validate_id_list(
            packet.get("requested_evidence_ids"), MAX_DETAIL_EVIDENCE_ITEMS
        )
        if status == "stale_result" and (token or requested or database):
            raise PacketContractError("stale result exposes internal identity")
    return copy.deepcopy(dict(packet))
def serialize_packet(packet: Mapping[str, Any]) -> str: return _compact_json(validate_packet(packet))
def build_tool_result(packet: Mapping[str, Any], *, is_error: bool | None = None) -> dict[str, Any]:
    safe = validate_packet(packet)
    result = _unchecked_tool_result(safe, is_error=is_error)
    if tool_result_size(result) <= MAX_TOOL_RESULT_BYTES:
        return result
    oversized = build_error_packet(str(safe["schema_version"]),
                                   "response_too_large")
    return _unchecked_tool_result(oversized, is_error=True)
def tool_result_size(result: Mapping[str, Any]) -> int: return len(_compact_json(result).encode("utf-8"))
def sanitize_visible_text(value: object, byte_limit: int | None = None
                          ) -> tuple[str, bool, bool]:
    if not isinstance(value, str):
        return "", False, False
    text = " ".join(unicodedata.normalize("NFC", value).split())
    changed = False
    for pattern in (_POINTER_RE, _URI_RE, *_PATH_RES):
        text, count = pattern.subn(_LOCATOR_OMITTED, text)
        changed = changed or bool(count)
    if "<<<" in text or ">>>" in text:
        text = text.replace("<<<", "‹‹‹").replace(">>>", "›››")
        changed = True
    text = " ".join(text.split()).strip(" ,;:")
    meaningful = text.replace(_LOCATOR_OMITTED, "").strip(" .,:;()[]{}-")
    if not meaningful:
        return "", changed, False
    truncated = byte_limit is not None and len(text.encode("utf-8")) > byte_limit
    return (_truncate_utf8(text, byte_limit) if truncated and byte_limit is not None
            else text), changed, truncated
def _build_routing_packet(payload: Mapping[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, str]] = []
    for raw in payload.get("candidates") or []:
        if not isinstance(raw, Mapping) or len(candidates) >= MAX_ROUTING_CANDIDATES:
            continue
        name = str(raw.get("name") or "")
        if not _DATABASE_RE.fullmatch(name):
            continue
        item = {"name": name}
        for key in ("title", "query_hint", "content_summary"):
            text, _, _ = sanitize_visible_text(raw.get(key))
            item[key] = text or (
                "Local RAG database" if key == "title" else "Not specified"
            )
        candidates.append(item)
    if not candidates: return build_search_packet({"status": "error"})
    packet = _base_packet(
        SEARCH_SCHEMA_VERSION,
        "database_required",
        "",
        [],
        [
            "Routing only; retrieval has not run. When one candidate clearly matches the routing metadata, the Agent must call local_rag_search again in the same turn with the unchanged question and exact database name without asking the user."
        ],
        [],
        "",
    )
    packet.update({"inspectable_evidence_ids": [], "candidates": candidates})
    return validate_packet(packet)
def _project_evidence(raw_items: object) -> tuple[list[dict[str, str]], list[str]]:
    if not isinstance(raw_items, list):
        return [], []
    output: list[dict[str, str]] = []
    notices: list[str] = []
    seen_ids: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        raw_id = str(raw.get("item_id") or raw.get("id") or "")
        item_id = raw_id if _EVIDENCE_ID_RE.fullmatch(raw_id) and raw_id not in seen_ids else ""
        if not item_id:
            index = 1
            while f"E{index}" in seen_ids:
                index += 1
            item_id = f"E{index}"
        seen_ids.add(item_id)
        text_source = raw.get("text") or raw.get("excerpt")
        if not text_source:
            text_source = " ".join(
                str(raw.get(key) or "")
                for key in ("context_before", "matched_excerpt", "context_after")
            )
        text, changed, _ = sanitize_visible_text(text_source)
        if not text:
            continue
        if changed:
            notices.append("locator_hints_omitted")
        source = raw.get("source")
        title_value = raw.get("source_title") or raw.get("title")
        if not title_value and isinstance(source, Mapping):
            title_value = source.get("title")
        title, changed_title, _ = sanitize_visible_text(title_value)
        if changed_title:
            notices.append("locator_hints_omitted")
        if not title:
            title = "Local RAG evidence"
            title, _, _ = sanitize_visible_text(title)
            title = title or "Local RAG evidence"
        item = {"id": item_id, "text": text, "source_title": title, "citation_label": f"[{item_id}]"}
        url = _safe_url(raw)
        if url:
            item["url"] = url
        output.append(item)
    return output, _unique(notices)
def _base_packet(schema: str, status: str, database: str, evidence: list[dict[str, str]], missing: list[str], notices: list[str], token: str) -> dict[str, Any]:
    action, answerability = _STATUS_CONTRACTS[status]
    return {
        "schema_version": schema, "status": status, "next_action": action,
        "answerability": answerability, "database": database,
        "payload_complete": True, "evidence": evidence,
        "missing_information": missing, "notices": notices,
        "result_token": token,
    }
def _answer_status(raw: str, answerability: str, evidence: list[dict[str, str]],
                   lossy: bool, can_inspect: bool) -> str:
    if raw == "error":
        return "error"
    if raw in {"no_hit", "no_evidence"}:
        return "no_hit"
    if not evidence:
        return "partial" if can_inspect else "no_hit"
    if lossy or raw == "partial" or answerability == "partial":
        return "partial"
    return "ok"
def _complete_missing(status: str, values: list[str], lossy: bool) -> list[str]:
    if status == "ok":
        return []
    if values:
        return values
    if status == "partial" or lossy:
        return ["The Local RAG result is partial."]
    if status == "no_hit":
        return ["No supported answer was found in the selected Local RAG database."]
    return ["Local RAG could not return usable evidence."]
def _validated_token(value: str, allow_empty: bool = False) -> str:
    if value == "" and allow_empty:
        return ""
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise PacketContractError("result token must be an opaque lrt_ token")
    return value
def _validated_ids(values: Iterable[str], limit: int) -> list[str]:
    output = list(values)
    if len(output) > limit or len(output) != len(set(output)) or any(not isinstance(v, str) or not _EVIDENCE_ID_RE.fullmatch(v) for v in output):
        raise PacketContractError("invalid evidence ids")
    return output
def _validate_id_list(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        raise PacketContractError("evidence ids must be a list")
    return _validated_ids(value, limit)
def _validate_evidence(items: list[object]) -> None:
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or set(item) not in ({"id", "text", "source_title", "citation_label"}, {"id", "text", "source_title", "citation_label", "url"}):
            raise PacketContractError("invalid evidence item")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not _EVIDENCE_ID_RE.fullmatch(item_id) or item_id in seen or item.get("citation_label") != f"[{item_id}]":
            raise PacketContractError("invalid evidence identity")
        seen.add(item_id)
        _assert_visible(item.get("text"))
        _assert_visible(item.get("source_title"))
        if "url" in item and not _is_safe_https(item.get("url")):
            raise PacketContractError("unsafe evidence URL")
def _validate_visible_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise PacketContractError(f"invalid {field}")
    for item in value:
        _assert_visible(item)
    return value
def _assert_visible(value: object, limit: int | None = None) -> None:
    if (not isinstance(value, str) or not value or
            (limit is not None and len(value.encode("utf-8")) > limit) or
            _CONTROL_RE.search(value) or "<<<" in value or ">>>" in value):
        raise PacketContractError("unsafe visible text")
    sanitized, changed, truncated = sanitize_visible_text(value, limit)
    if changed or truncated or sanitized != value:
        raise PacketContractError("visible text contains a locator or unsafe form")
def _visible_list(value: object) -> list[str]:
    raw = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else []
    output: list[str] = []
    for item in raw:
        text, _, _ = sanitize_visible_text(item)
        if text and text not in output:
            output.append(text)
    return output
def _database(first: Mapping[str, Any], second: Mapping[str, Any]) -> str:
    value = str(first.get("database") or first.get("selected_db") or second.get("database") or second.get("selected_db") or "").strip()
    return value if _DATABASE_RE.fullmatch(value) else ""
def _normalized_status(value: object) -> str:
    return str(value or "").strip().casefold().replace("-", "_")
def _safe_url(item: Mapping[str, Any]) -> str:
    for key in ("url", "source_permalink", "source_url"):
        value = item.get(key)
        if _is_safe_https(value):
            return str(value).strip()
    return ""
def _is_safe_https(value: object) -> bool:
    if (not isinstance(value, str) or _CONTROL_RE.search(value) or
            any(c.isspace() for c in value) or "\\" in value):
        return False
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return False
    return bool(parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None and (port is None or 1 <= port <= 65_535))
def _truncate_utf8(value: str, limit: int) -> str:
    marker = "…".encode("utf-8")
    if limit <= len(marker):
        return value.encode("utf-8")[:limit].decode("utf-8", "ignore")
    prefix = value.encode("utf-8")[: limit - len(marker)].decode("utf-8", "ignore")
    return prefix.rstrip() + "…"
def _unique(values: Iterable[str], limit: int | None = None) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
        if limit is not None and len(output) >= limit:
            break
    return output
def _unchecked_tool_result(packet: Mapping[str, Any], *,
                           is_error: bool | None = None) -> dict[str, Any]:
    text = (
        f"Local RAG {packet['schema_version']} status={packet['status']}; "
        f"next_action={packet['next_action']}; "
        "use structuredContent for the complete packet."
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": copy.deepcopy(dict(packet)),
        "isError": packet["status"] == "error" if is_error is None else bool(is_error),
    }
def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
