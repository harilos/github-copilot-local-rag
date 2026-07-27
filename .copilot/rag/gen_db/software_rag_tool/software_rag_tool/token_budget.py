from __future__ import annotations

import math


def conservative_token_count(text: str) -> int:
    """Return a tokenizer-free upper-biased token estimate for mixed text."""
    count = 0
    ascii_buffer = ""
    for char in text:
        if (
            "\u3040" <= char <= "\u30ff"
            or "\u3400" <= char <= "\u9fff"
            or "\uf900" <= char <= "\ufaff"
        ):
            if ascii_buffer:
                count += max(
                    len(ascii_buffer.split()),
                    math.ceil(len(ascii_buffer) / 3),
                )
                ascii_buffer = ""
            count += 1
        elif char.isspace():
            if ascii_buffer:
                count += max(
                    len(ascii_buffer.split()),
                    math.ceil(len(ascii_buffer) / 3),
                )
                ascii_buffer = ""
        elif char.isalnum() or char in "_-":
            ascii_buffer += char
        else:
            if ascii_buffer:
                count += max(
                    len(ascii_buffer.split()),
                    math.ceil(len(ascii_buffer) / 3),
                )
                ascii_buffer = ""
            count += 1
    if ascii_buffer:
        count += max(
            len(ascii_buffer.split()),
            math.ceil(len(ascii_buffer) / 3),
        )
    return count


def truncate_to_token_limit(text: str, limit: int) -> str:
    if limit <= 8:
        return ""
    low, high = 0, len(text)
    suffix = "...[truncated]"
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle].rstrip() + suffix
        if conservative_token_count(candidate) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + suffix if low else ""
