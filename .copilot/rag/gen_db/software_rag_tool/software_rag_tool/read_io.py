from __future__ import annotations

import errno
import os
import time
from pathlib import Path


def read_text_with_windows_retry(path: Path, **kwargs) -> str:
    """Bound transient CRT read-open contention; never retry a write here.

    Python's Windows CRT open can drop winerror and retain only EACCES.
    This reader-only exception must not broaden atomic replacement policy.
    """
    if os.name != "nt":
        return path.read_text(**kwargs)
    deadline = time.monotonic() + 2.0
    delay = 0.01
    while True:
        try:
            return path.read_text(**kwargs)
        except OSError as exc:
            code = getattr(exc, "winerror", None)
            retryable = code in {5, 32, 33} or (
                code is None and isinstance(exc, PermissionError)
                and exc.errno == errno.EACCES
            )
            remaining = deadline - time.monotonic()
            if not retryable or remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(0.1, delay * 2)
