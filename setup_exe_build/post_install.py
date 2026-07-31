from __future__ import annotations

import argparse
import os
from pathlib import Path


INSTRUCTION = (
    "For requests to use RAG, local documents, internal or company information, "
    "or information installed in or provided to Copilot, read "
    "~/.copilot/instructions/rag.instructions.md."
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copilot-home", required=True)
    return parser.parse_args()


def _append_instruction(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""

    if (
        INSTRUCTION.encode("ascii") in raw
        or INSTRUCTION.encode("utf-16-le") in raw
        or INSTRUCTION.encode("utf-16-be") in raw
    ):
        return

    if raw.startswith(b"\xff\xfe"):
        encoding = "utf-16-le"
        newline = "\r\n".encode(encoding)
        addition = INSTRUCTION.encode(encoding)
    elif raw.startswith(b"\xfe\xff"):
        encoding = "utf-16-be"
        newline = "\r\n".encode(encoding)
        addition = INSTRUCTION.encode(encoding)
    else:
        newline = b"\r\n"
        addition = INSTRUCTION.encode("utf-8")

    prefix = b""
    if raw and not raw.endswith((b"\n", b"\r\n", b"\x0a\x00", b"\x00\x0a")):
        prefix = newline
    elif raw:
        prefix = b""

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(raw)
            handle.write(prefix)
            handle.write(addition)
            handle.write(newline)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    args = _parse_args()
    copilot_home = Path(args.copilot_home).expanduser()
    if copilot_home.is_symlink():
        raise RuntimeError("Copilot home must not be a symbolic link")
    copilot_home.mkdir(parents=True, exist_ok=True)
    copilot_home = copilot_home.resolve(strict=True)
    _append_instruction(copilot_home / "copilot-instructions.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
