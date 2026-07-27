from __future__ import annotations

import argparse
import json
from typing import Any


SCHEMA_VERSION = "rag-search-request-v1"
ANSWER_GOALS = {
    "definition",
    "evidence",
    "comparison",
    "procedure",
    "history",
    "survey",
}
MAX_STRUCTURED_REQUEST_BYTES = 3_072


class SearchRequestError(ValueError):
    pass


def add_search_request_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--request-json",
        action="store_true",
        help="Read a rag-search-request-v1 object from --stdin",
    )
    parser.add_argument("--answer-goal", choices=sorted(ANSWER_GOALS))
    parser.add_argument(
        "--literal-identifier",
        action="append",
        default=[],
        help="Repeatable exact identifier; maximum three",
    )
    parser.add_argument(
        "--entity",
        action="append",
        default=[],
        help="Repeatable named entity; maximum five",
    )
    parser.add_argument(
        "--facet",
        action="append",
        default=[],
        help="Repeatable retrieval facet; maximum four",
    )
    parser.add_argument(
        "--semantic-hypothesis",
        action="append",
        default=[],
        help="Repeatable semantic-only hypothesis; maximum three",
    )


def request_from_cli(
    args: argparse.Namespace,
    *,
    positional_question: str,
    stdin_text: str = "",
) -> dict[str, Any]:
    if bool(getattr(args, "request_json", False)):
        if not bool(getattr(args, "stdin", False)):
            raise SearchRequestError("--request-json requires --stdin")
        if positional_question:
            raise SearchRequestError(
                "--request-json does not accept a positional question"
            )
        if any(
            [
                getattr(args, "answer_goal", None),
                getattr(args, "literal_identifier", None),
                getattr(args, "entity", None),
                getattr(args, "facet", None),
                getattr(args, "semantic_hypothesis", None),
            ]
        ):
            raise SearchRequestError(
                "--request-json cannot be combined with repeated planning arguments"
            )
        raw = stdin_text
        if len(raw.encode("utf-8")) > MAX_STRUCTURED_REQUEST_BYTES:
            raise SearchRequestError("structured request exceeds 3072 bytes")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SearchRequestError(f"invalid request JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise SearchRequestError("request JSON must be an object")
        return normalize_search_request(payload)

    question = stdin_text.strip() if bool(getattr(args, "stdin", False)) else positional_question
    payload = {
        "schema_version": SCHEMA_VERSION,
        "original_question": question,
        "answer_goal": getattr(args, "answer_goal", None) or "evidence",
        "literal_identifiers": list(
            getattr(args, "literal_identifier", []) or []
        ),
        "entities": list(getattr(args, "entity", []) or []),
        "facets": list(getattr(args, "facet", []) or []),
        "inferred_concepts": list(
            getattr(args, "semantic_hypothesis", []) or []
        ),
    }
    return normalize_search_request(payload)


def normalize_search_request(payload: dict[str, Any]) -> dict[str, Any]:
    schema = str(payload.get("schema_version") or SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise SearchRequestError(f"unsupported request schema: {schema}")
    question = payload.get("original_question")
    if not isinstance(question, str) or not question.strip():
        raise SearchRequestError("original_question is required")
    answer_goal = str(payload.get("answer_goal") or "evidence").strip().lower()
    if answer_goal not in ANSWER_GOALS:
        raise SearchRequestError(
            f"answer_goal must be one of {sorted(ANSWER_GOALS)}"
        )
    identifiers = _strings(
        payload.get("literal_identifiers"),
        "literal_identifiers",
        3,
    )
    entities = _strings(payload.get("entities"), "entities", 5)
    inferred = _inferred_concepts(payload.get("inferred_concepts"), 3)
    facets = _facets(payload.get("facets"), identifiers, 4)
    coverage = _coverage(payload.get("coverage"))
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "original_question": question,
        "answer_goal": answer_goal,
        "literal_identifiers": identifiers,
        "entities": entities,
        "facets": facets,
        "inferred_concepts": inferred,
        "coverage": coverage,
    }
    if len(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ) > MAX_STRUCTURED_REQUEST_BYTES:
        raise SearchRequestError("normalized structured request exceeds 3072 bytes")
    return normalized


def request_to_cli_arguments(request: dict[str, Any]) -> list[str]:
    normalized = normalize_search_request(request)
    arguments = ["--answer-goal", normalized["answer_goal"]]
    for value in normalized["literal_identifiers"]:
        arguments.extend(["--literal-identifier", value])
    for value in normalized["entities"]:
        arguments.extend(["--entity", value])
    for facet in normalized["facets"]:
        arguments.extend(["--facet", facet["query"]])
    for concept in normalized["inferred_concepts"]:
        arguments.extend(["--semantic-hypothesis", concept["term"]])
    return arguments


def _strings(value: Any, name: str, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SearchRequestError(f"{name} must be an array")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SearchRequestError(f"{name} entries must be non-empty strings")
        if item not in output:
            output.append(item)
    if len(output) > limit:
        raise SearchRequestError(f"{name} accepts at most {limit} entries")
    return output


def _facets(
    value: Any,
    identifiers: list[str],
    limit: int,
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SearchRequestError("facets must be an array")
    output: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            query = item
            kind = "literal" if item in identifiers else "semantic"
        elif isinstance(item, dict):
            query = item.get("query")
            kind = str(item.get("kind") or "semantic").strip().lower()
        else:
            raise SearchRequestError("facet entries must be strings or objects")
        if not isinstance(query, str) or not query.strip():
            raise SearchRequestError("facet query must be a non-empty string")
        if kind not in {"literal", "semantic"}:
            raise SearchRequestError("facet kind must be literal or semantic")
        canonical = {
            "kind": kind,
            "query": query,
            "purpose": (
                "Find literal occurrences and identifier evidence."
                if kind == "literal"
                else "Find related local documents."
            ),
        }
        if canonical not in output:
            output.append(canonical)
    if len(output) > limit:
        raise SearchRequestError(f"facets accepts at most {limit} entries")
    return output


def _inferred_concepts(value: Any, limit: int) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SearchRequestError("inferred_concepts must be an array")
    output: list[dict[str, Any]] = []
    for item in value:
        term = item if isinstance(item, str) else item.get("term") if isinstance(item, dict) else None
        if not isinstance(term, str) or not term.strip():
            raise SearchRequestError(
                "inferred concepts must contain a non-empty term"
            )
        canonical = {
            "term": term,
            "confidence": "medium",
            "semantic_only": True,
        }
        if canonical not in output:
            output.append(canonical)
    if len(output) > limit:
        raise SearchRequestError(
            f"inferred_concepts accepts at most {limit} entries"
        )
    return output


def _coverage(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    policy = str(payload.get("policy") or "wide").strip().lower()
    if policy not in {"wide", "narrow"}:
        raise SearchRequestError("coverage.policy must be wide or narrow")
    if policy == "narrow":
        return {
            "policy": "narrow",
            "target_distinct_documents": 1,
            "minimum_desired_documents": 1,
            "maximum_distinct_documents": 1,
            "max_chunks_per_document": 2,
            "allow_weak_related": False,
        }
    maximum = _bounded_int(
        payload.get("maximum_distinct_documents"),
        default=10,
        minimum=1,
        maximum=10,
    )
    target = min(
        maximum,
        _bounded_int(
            payload.get("target_distinct_documents"),
            default=8,
            minimum=1,
            maximum=10,
        ),
    )
    minimum_desired = min(
        target,
        _bounded_int(
            payload.get("minimum_desired_documents"),
            default=6,
            minimum=1,
            maximum=10,
        ),
    )
    return {
        "policy": "wide",
        "target_distinct_documents": target,
        "minimum_desired_documents": minimum_desired,
        "maximum_distinct_documents": maximum,
        "max_chunks_per_document": min(
            2,
            _bounded_int(
                payload.get("max_chunks_per_document"),
                default=2,
                minimum=1,
                maximum=2,
            ),
        ),
        "allow_weak_related": bool(payload.get("allow_weak_related", True)),
    }


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise SearchRequestError("coverage counts must be integers") from None
    if not minimum <= parsed <= maximum:
        raise SearchRequestError(
            f"coverage count must be between {minimum} and {maximum}"
        )
    return parsed
