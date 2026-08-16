from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "lrr-agent-002-vscode-normalized-v1"
INTERESTING_KEYS = {
    "agent",
    "agentname",
    "agent_name",
    "argv",
    "command",
    "input",
    "model",
    "modelid",
    "model_id",
    "name",
    "prompt",
    "query",
    "request",
    "response",
    "sessiontarget",
    "session_target",
    "tool",
    "toolname",
    "tool_name",
    "userprompt",
    "user_prompt",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _walk(value: Any, path: tuple[str, ...] = ()) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        lowered = {str(key).lower().replace("-", "_"): key for key in value}
        for normalized, original in lowered.items():
            compact = normalized.replace("_", "")
            if normalized in INTERESTING_KEYS or compact in INTERESTING_KEYS:
                candidate = value[original]
                if isinstance(candidate, (str, int, float, bool)) or candidate is None:
                    yield {
                        "path": "/" + "/".join((*path, str(original))),
                        "key": str(original),
                        "value": candidate,
                    }
                elif normalized in {"argv", "input", "request"}:
                    yield {
                        "path": "/" + "/".join((*path, str(original))),
                        "key": str(original),
                        "value": candidate,
                    }
        for key, child in value.items():
            yield from _walk(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, str(index)))


def normalize(chat_path: Path, otlp_path: Path, case_path: Path) -> dict[str, Any]:
    inputs: dict[str, dict[str, str]] = {}
    events: list[dict[str, Any]] = []
    for label, path in (("chat", chat_path), ("otlp", otlp_path), ("case", case_path)):
        document = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        inputs[label] = {"path": str(path.resolve()), "sha256": _sha256(path)}
        if label != "case":
            events.extend({"source": label, **event} for event in _walk(document))
    if not events:
        raise ValueError("chat and OTLP inputs contain no recognizable grading fields")
    return {
        "schema_version": SCHEMA_VERSION,
        "inputs": inputs,
        "events": events,
    }


def self_test() -> None:
    exact_query = "Q'$()\n\u96ea\u2603"
    payload = {
        "sessionTarget": "Local",
        "agentName": "AGENT002-D",
        "modelId": "GPT-5 mini",
        "messages": [{"userPrompt": exact_query}],
        "toolCalls": [
            {
                "toolName": "execute",
                "input": {
                    "command": "search.py",
                    "argv": ["--db", "agent002-decoy-rag", exact_query],
                },
            }
        ],
    }
    events = list(_walk(payload))
    values = [event["value"] for event in events]
    required = ["Local", "AGENT002-D", "GPT-5 mini", exact_query, "execute"]
    missing = [value for value in required if value not in values]
    if missing:
        raise AssertionError(f"parser self-test missing fields: {missing!r}")
    argv_events = [event for event in events if event["key"].lower() == "argv"]
    if len(argv_events) != 1 or argv_events[0]["value"][-1] != exact_query:
        raise AssertionError("parser self-test did not preserve exact argv")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize VS Code Chat and OTLP exports without network access."
    )
    parser.add_argument("--chat", type=Path)
    parser.add_argument("--otlp", type=Path)
    parser.add_argument("--case", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("SELF_TEST_PASS")
        return 0
    if not all((args.chat, args.otlp, args.case, args.output)):
        parser.error(
            "--chat, --otlp, --case, and --output are required unless --self-test is used"
        )
    result = normalize(args.chat, args.otlp, args.case)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(str(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
