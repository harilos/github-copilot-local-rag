#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence


RAG_ROOT = Path(__file__).resolve().parents[1]
MAX_QUESTION_CHARS = 16_000
MAX_NORMALIZED_REQUEST_BYTES = 3_072
_DATABASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*-rag$")
_RESULT_SET_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ITEM_ID_RE = re.compile(r"^[ED][1-9]\d?$")
_ANSWER_GOALS = (
    "comparison",
    "definition",
    "evidence",
    "history",
    "procedure",
    "survey",
)
_REPEATABLE_SEARCH_OPTIONS = (
    ("literal_identifier", "--literal-identifier", 3),
    ("entity", "--entity", 5),
    ("facet", "--facet", 4),
    ("semantic_hypothesis", "--semantic-hypothesis", 3),
)
_VALUE_OPTIONS = frozenset(
    {
        "--answer-goal",
        "--db",
        "--detail-level",
        "--entity",
        "--facet",
        "--item-id",
        "--literal-identifier",
        "--question",
        "--result-set-id",
        "--semantic-hypothesis",
    }
)


def _database_name(value: str) -> str:
    if not _DATABASE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "database must match '<name>-rag'"
        )
    return value


def _nonempty_text(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise argparse.ArgumentTypeError("value must be non-empty and contain no NUL")
    return value


def _question(value: str) -> str:
    question = _nonempty_text(value)
    if len(question) > MAX_QUESTION_CHARS:
        raise argparse.ArgumentTypeError(
            f"question must contain at most {MAX_QUESTION_CHARS} characters"
        )
    return question


def _result_set_id(value: str) -> str:
    if not _RESULT_SET_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "result-set-id must be a canonical UUID"
        )
    return value.lower()


def _item_id(value: str) -> str:
    if not _ITEM_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("item-id must match E1..E99 or D1..D99")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Strict command surface used by the Local RAG prompt/Skill."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "list",
        help="List installed databases as JSON.",
        allow_abbrev=False,
    )

    search = commands.add_parser(
        "search",
        help="Run one search through the public Local RAG entry point.",
        allow_abbrev=False,
    )
    search.add_argument("--db", required=True, type=_database_name)
    search.add_argument("--question", required=True, type=_question)
    search.add_argument("--answer-goal", choices=_ANSWER_GOALS)
    search.add_argument(
        "--literal-identifier",
        action="append",
        default=[],
        type=_nonempty_text,
    )
    search.add_argument(
        "--entity",
        action="append",
        default=[],
        type=_nonempty_text,
    )
    search.add_argument(
        "--facet",
        action="append",
        default=[],
        type=_nonempty_text,
    )
    search.add_argument(
        "--semantic-hypothesis",
        action="append",
        default=[],
        type=_nonempty_text,
    )

    detail = commands.add_parser(
        "detail",
        help="Read cached detail without running retrieval again.",
        allow_abbrev=False,
    )
    detail.add_argument(
        "--result-set-id",
        required=True,
        type=_result_set_id,
    )
    detail.add_argument(
        "--item-id",
        action="append",
        required=True,
        type=_item_id,
    )
    detail.add_argument(
        "--detail-level",
        choices=("expanded", "deep"),
        default="expanded",
    )

    commands.add_parser(
        "setup",
        help="Run the public initial-setup entry point in JSON mode.",
        allow_abbrev=False,
    )
    return parser


def _protect_option_values(arguments: Sequence[str]) -> list[str]:
    """Keep a declared value as data even when it starts with a dash."""

    protected: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in _VALUE_OPTIONS and index + 1 < len(arguments):
            protected.append(f"{value}={arguments[index + 1]}")
            index += 2
            continue
        protected.append(value)
        index += 1
    return protected


def _deduplicated(values: Sequence[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value not in output:
            output.append(value)
    return output


def _normalized_request_size(args: argparse.Namespace) -> int:
    identifiers = _deduplicated(args.literal_identifier)
    entities = _deduplicated(args.entity)
    facets: list[dict[str, str]] = []
    for value in _deduplicated(args.facet):
        facets.append(
            {
                "kind": "literal" if value in identifiers else "semantic",
                "query": value,
                "purpose": (
                    "Find literal occurrences and identifier evidence."
                    if value in identifiers
                    else "Find related local documents."
                ),
            }
        )
    normalized = {
        "schema_version": "rag-search-request-v1",
        "original_question": args.question,
        "answer_goal": args.answer_goal or "evidence",
        "literal_identifiers": identifiers,
        "entities": entities,
        "facets": facets,
        "inferred_concepts": [
            {
                "term": value,
                "confidence": "medium",
                "semantic_only": True,
            }
            for value in _deduplicated(args.semantic_hypothesis)
        ],
        "coverage": {
            "policy": "wide",
            "target_distinct_documents": 8,
            "minimum_desired_documents": 6,
            "maximum_distinct_documents": 10,
            "max_chunks_per_document": 2,
            "allow_weak_related": True,
        },
    }
    return len(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command == "search":
        for attribute, option, maximum in _REPEATABLE_SEARCH_OPTIONS:
            values = getattr(args, attribute)
            if len(values) > maximum:
                parser.error(f"{option} accepts at most {maximum} values")
        if _normalized_request_size(args) > MAX_NORMALIZED_REQUEST_BYTES:
            parser.error(
                "normalized search request exceeds "
                f"{MAX_NORMALIZED_REQUEST_BYTES} UTF-8 bytes"
            )
        return

    if args.command != "detail":
        return
    maximum = 1 if args.detail_level == "deep" else 3
    if len(args.item_id) > maximum:
        parser.error(
            f"--detail-level {args.detail_level} accepts at most "
            f"{maximum} --item-id value(s)"
        )
    if len(set(args.item_id)) != len(args.item_id):
        parser.error("--item-id values must be unique")


def _search_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-I",
        "-B",
        str(RAG_ROOT / "search.py"),
        "--db",
        args.db,
        "--include-db-hint",
        "--compact-json",
        "--result-delivery",
        "file",
        "--format",
        "json",
        "--stdin",
    ]
    if args.answer_goal:
        command.extend(["--answer-goal", args.answer_goal])
    for attribute, option, _maximum in _REPEATABLE_SEARCH_OPTIONS:
        for value in getattr(args, attribute):
            command.append(f"{option}={value}")
    return command


def _detail_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-I",
        "-B",
        str(RAG_ROOT / "search.py"),
        "--result-set-id",
        args.result_set_id,
    ]
    for item_id in args.item_id:
        command.extend(["--item-id", item_id])
    command.extend(
        [
            "--detail-level",
            args.detail_level,
            "--result-delivery",
            "file",
        ]
    )
    return command


def _invocation(args: argparse.Namespace) -> tuple[list[str], bytes | None]:
    if args.command == "list":
        return (
            [
                sys.executable,
                "-I",
                "-B",
                str(RAG_ROOT / "list_dbs.py"),
                "--format",
                "json",
            ],
            None,
        )
    if args.command == "search":
        # Passing the semantic question over stdin keeps it data even if it
        # begins with a dash.  This is a direct child-process pipe, not a shell
        # pipeline; the public search entry point still owns validation.
        return _search_command(args), args.question.encode("utf-8")
    if args.command == "detail":
        return _detail_command(args), None
    return (
        [
            sys.executable,
            "-I",
            "-B",
            str(RAG_ROOT / "setup.py"),
            "--format",
            "json",
        ],
        None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_protect_option_values(arguments))
    _validate(parser, args)
    command, input_bytes = _invocation(args)
    kwargs: dict[str, object] = {
        "check": False,
        "cwd": str(RAG_ROOT),
        "env": {
            **os.environ,
            "RAG_DBS_ROOT": str(RAG_ROOT / "dbs"),
        },
        "shell": False,
    }
    if input_bytes is not None:
        kwargs["input"] = input_bytes
    completed = subprocess.run(command, **kwargs)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
