"""Streaming subprocess boundary for Source operations.

The low-level CLIs remain usable without framing.  Newer CLIs may emit:

* ``@@LOCAL_RAG_PROGRESS_V1@@<json>`` on stderr
* ``@@LOCAL_RAG_RESULT_V1@@<json>`` on stdout

Both pipes are drained concurrently.  Human-readable output is retained as a
bounded head/tail diagnostic only; framed result payloads are captured while
streaming so a valid result is not lost when a verbose command is truncated.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Iterable, Mapping, Sequence

from .errors import sanitize_diagnostic
from .diagnostics import process_diagnostic


PROGRESS_FRAME = "@@LOCAL_RAG_PROGRESS_V1@@"
RESULT_FRAME = "@@LOCAL_RAG_RESULT_V1@@"
# Each stream keeps roughly 64 KiB total while preserving both ends.
CAPTURE_HEAD_BYTES = 32 * 1024
CAPTURE_TAIL_BYTES = 32 * 1024
_MAX_LINE_BYTES = 256 * 1024
_POST_EXIT_DRAIN_IDLE_SECONDS = 0.25
_POST_EXIT_TERMINATE_SECONDS = 0.5

ProgressCallback = Callable[[Mapping[str, Any]], None]
ResultValidator = Callable[[Any], bool | None]


@dataclass(frozen=True)
class StreamingProcessResult:
    """Completed-process compatible result with bounded diagnostics."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    result_frames: tuple[str, ...]
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_total_bytes: int
    stderr_total_bytes: int


class ResultExtractionError(ValueError):
    """Raised when a unique schema-valid result cannot be selected."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: Iterable[Mapping[str, Any]],
    ) -> None:
        super().__init__(message)
        self.diagnostics = tuple(dict(item) for item in diagnostics)


class _BoundedCapture:
    def __init__(
        self,
        head_limit: int = CAPTURE_HEAD_BYTES,
        tail_limit: int = CAPTURE_TAIL_BYTES,
    ) -> None:
        self._head_limit = int(head_limit)
        self._tail_limit = int(tail_limit)
        self._head = bytearray()
        self._tail = bytearray()
        self.total_bytes = 0

    def append(self, value: bytes) -> None:
        if not value:
            return
        self.total_bytes += len(value)
        remaining = value
        if len(self._head) < self._head_limit:
            count = min(self._head_limit - len(self._head), len(remaining))
            self._head.extend(remaining[:count])
            remaining = remaining[count:]
        if remaining and self._tail_limit:
            self._tail.extend(remaining)
            if len(self._tail) > self._tail_limit:
                del self._tail[: len(self._tail) - self._tail_limit]

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self._head_limit + self._tail_limit

    def text(self) -> str:
        if self.truncated:
            value = (
                bytes(self._head)
                + b"\n...[bounded diagnostic omitted]...\n"
                + bytes(self._tail)
            )
        else:
            value = bytes(self._head) + bytes(self._tail)
        return value.decode("utf-8", errors="replace")


class _LineDecoder:
    """Split byte streams on CR, LF, or CRLF without unbounded buffering."""

    def __init__(self, callback: Callable[[bytes], None]) -> None:
        self._callback = callback
        self._buffer = bytearray()
        self._discarding = False

    def feed(self, value: bytes) -> None:
        for byte in value:
            if byte in (10, 13):
                if self._buffer or not self._discarding:
                    self._callback(bytes(self._buffer))
                self._buffer.clear()
                self._discarding = False
                continue
            if self._discarding:
                continue
            if len(self._buffer) >= _MAX_LINE_BYTES:
                self._discarding = True
                continue
            self._buffer.append(byte)

    def finish(self) -> None:
        if self._buffer:
            self._callback(bytes(self._buffer))
        self._buffer.clear()


def _safe_progress(
    callback: ProgressCallback | None,
    callback_lock: threading.Lock,
    event: Mapping[str, Any],
) -> None:
    if callback is None:
        return
    try:
        with callback_lock:
            callback(dict(event))
    except Exception:
        # UI callbacks are observational and must never fail Source processing.
        return


def _progress_event(payload: str) -> dict[str, Any]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return {
            "event": "progress",
            "protocol": "local-rag.progress.v1",
            "status": "invalid",
        }
    if isinstance(decoded, dict):
        return {
            "event": "progress",
            "protocol": "local-rag.progress.v1",
            "payload": decoded,
        }
    return {
        "event": "progress",
        "protocol": "local-rag.progress.v1",
        "payload": {"value": decoded},
    }


def _wait_for_output_readers(
    readers: Sequence[threading.Thread],
    captures: Sequence[_BoundedCapture],
    *,
    idle_timeout: float,
) -> bool:
    """Wait while output is still flowing, without trusting inherited pipes."""
    totals = tuple(capture.total_bytes for capture in captures)
    last_progress = time.monotonic()
    while any(reader.is_alive() for reader in readers):
        for reader in readers:
            reader.join(timeout=0.02)
        current = tuple(capture.total_bytes for capture in captures)
        now = time.monotonic()
        if current != totals:
            totals = current
            last_progress = now
        elif now - last_progress >= idle_timeout:
            return False
    return True


def _signal_process_tree(
    process: subprocess.Popen[bytes],
    *,
    force: bool,
) -> None:
    """Best-effort termination of the isolated POSIX process group."""
    if os.name != "nt":
        try:
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    if process.poll() is None:
        try:
            if force:
                process.kill()
            else:
                process.terminate()
        except OSError:
            return


def run_streaming_process(
    arguments: Sequence[str],
    *,
    progress_callback: ProgressCallback | None = None,
    timeout: float | None = 3600,
    heartbeat_interval: float = 5.0,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdout_sink: BinaryIO | None = None,
) -> StreamingProcessResult:
    """Run one command with concurrent, bounded diagnostics.

    When ``stdout_sink`` is supplied, the caller-owned binary stream receives
    the complete stdout byte sequence in addition to the bounded diagnostic
    capture.  The sink is never closed here.
    """

    argv = tuple(str(item) for item in arguments)
    if not argv:
        raise ValueError("arguments must not be empty")
    child_environment = (
        dict(env) if env is not None else dict(os.environ)
    )
    child_environment["PYTHONIOENCODING"] = "utf-8"
    child_environment["PYTHONUTF8"] = "1"
    process = subprocess.Popen(
        list(argv),
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        env=child_environment,
        start_new_session=(os.name != "nt"),
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("subprocess pipes were not created")

    stdout_capture = _BoundedCapture()
    stderr_capture = _BoundedCapture()
    result_frames: list[str] = []
    result_lock = threading.Lock()
    callback_lock = threading.Lock()
    sink_failures: list[BaseException] = []

    def handle_stdout(line: bytes) -> None:
        text = line.decode("utf-8", errors="replace")
        if text.startswith(RESULT_FRAME):
            with result_lock:
                result_frames.append(text[len(RESULT_FRAME) :].strip())

    def handle_stderr(line: bytes) -> None:
        text = line.decode("utf-8", errors="replace")
        if text.startswith(PROGRESS_FRAME):
            _safe_progress(
                progress_callback,
                callback_lock,
                _progress_event(text[len(PROGRESS_FRAME) :].strip()),
            )
        elif text.strip():
            safe_line = sanitize_diagnostic(text.strip(), max_chars=2_000)
            percentage_match = re.search(r"(?<![0-9])([0-9]{1,3})%", safe_line)
            event: dict[str, Any] = {
                "event": "subprocess.log",
                "phase": "provider.internal",
                "label_ja": "Provider内部進捗",
                "message": safe_line,
                "total_kind": "unknown",
            }
            if percentage_match is not None:
                event["provider_percentage"] = min(
                    100,
                    int(percentage_match.group(1)),
                )
            _safe_progress(
                progress_callback,
                callback_lock,
                event,
            )

    def drain(
        stream: Any,
        capture: _BoundedCapture,
        line_callback: Callable[[bytes], None],
        full_sink: BinaryIO | None = None,
    ) -> None:
        decoder = _LineDecoder(line_callback)
        read_chunk = getattr(stream, "read1", stream.read)
        active_sink = full_sink
        try:
            while True:
                chunk = read_chunk(8192)
                if not chunk:
                    break
                capture.append(chunk)
                if active_sink is not None:
                    try:
                        written = active_sink.write(chunk)
                        if written is not None and int(written) != len(chunk):
                            raise OSError("stdout sink accepted a partial write")
                    except BaseException as exc:
                        sink_failures.append(exc)
                        active_sink = None
                decoder.feed(chunk)
        finally:
            if active_sink is not None:
                try:
                    active_sink.flush()
                except BaseException as exc:
                    sink_failures.append(exc)
            decoder.finish()
            stream.close()

    readers = [
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout_capture, handle_stdout, stdout_sink),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr_capture, handle_stderr),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    started = time.monotonic()
    next_heartbeat = (
        started + heartbeat_interval
        if heartbeat_interval > 0
        else float("inf")
    )
    timed_out = False
    try:
        while process.poll() is None:
            now = time.monotonic()
            if timeout is not None and now - started >= timeout:
                timed_out = True
                _signal_process_tree(process, force=False)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _signal_process_tree(process, force=True)
                break
            if now >= next_heartbeat:
                _safe_progress(
                    progress_callback,
                    callback_lock,
                    {
                        "event": "heartbeat",
                        "protocol": "local-rag.progress.v1",
                        "elapsed_seconds": round(now - started, 3),
                    },
                )
                next_heartbeat = now + heartbeat_interval
            wait_for = min(
                0.05,
                max(0.001, next_heartbeat - now),
            )
            if timeout is not None:
                wait_for = min(
                    wait_for,
                    max(0.001, timeout - (now - started)),
                )
            try:
                process.wait(timeout=wait_for)
            except subprocess.TimeoutExpired:
                pass
        process.wait()
    except BaseException:
        _signal_process_tree(process, force=False)
        try:
            process.wait(timeout=_POST_EXIT_TERMINATE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_process_tree(process, force=True)
        raise
    drained = _wait_for_output_readers(
        readers,
        (stdout_capture, stderr_capture),
        idle_timeout=_POST_EXIT_DRAIN_IDLE_SECONDS,
    )
    if not drained:
        # A descendant may have inherited stdout/stderr after the direct child
        # exited.  Never wait forever or report a complete sink in that state.
        _signal_process_tree(process, force=False)
        if not _wait_for_output_readers(
            readers,
            (stdout_capture, stderr_capture),
            idle_timeout=_POST_EXIT_TERMINATE_SECONDS,
        ):
            _signal_process_tree(process, force=True)
            _wait_for_output_readers(
                readers,
                (stdout_capture, stderr_capture),
                idle_timeout=_POST_EXIT_TERMINATE_SECONDS,
            )
    if sink_failures:
        raise OSError("complete stdout sink write failed") from sink_failures[0]
    result = StreamingProcessResult(
        args=argv,
        returncode=int(process.returncode),
        stdout=stdout_capture.text(),
        stderr=stderr_capture.text(),
        result_frames=tuple(result_frames),
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
        stdout_total_bytes=stdout_capture.total_bytes,
        stderr_total_bytes=stderr_capture.total_bytes,
    )
    if timed_out:
        error = subprocess.TimeoutExpired(
            list(argv),
            timeout,
            output=result.stdout,
            stderr=result.stderr,
        )
        error.process_diagnostic = process_diagnostic(
            arguments=argv,
            cwd=cwd or os.getcwd(),
            returncode=result.returncode,
            elapsed_seconds=time.monotonic() - started,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        raise error
    if not drained:
        raise OSError(
            "subprocess output pipes remained open after the process exited"
        )
    return result


def _validate_result(
    value: Any,
    validator: ResultValidator | None,
    *,
    stage: str,
    candidate: int,
    diagnostics: list[dict[str, Any]],
) -> bool:
    if not isinstance(value, dict):
        diagnostics.append(
            {
                "stage": stage,
                "candidate": candidate,
                "error": "result_not_object",
                "actual_type": type(value).__name__,
            }
        )
        return False
    if validator is None:
        return True
    try:
        accepted = validator(value)
    except Exception as exc:
        diagnostics.append(
            {
                "stage": stage,
                "candidate": candidate,
                "error": "schema_rejected",
                "exception_type": type(exc).__name__,
                "message": str(exc)[:300],
            }
        )
        return False
    if accepted is False:
        diagnostics.append(
            {
                "stage": stage,
                "candidate": candidate,
                "error": "schema_rejected",
            }
        )
        return False
    return True


def _decode_candidate(
    payload: str,
    *,
    stage: str,
    candidate: int,
    diagnostics: list[dict[str, Any]],
) -> Any | None:
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        excerpt, caret = _decode_error_excerpt(payload, exc.pos)
        diagnostics.append(
            {
                "stage": stage,
                "candidate": candidate,
                "error": "json_decode_error",
                "line": exc.lineno,
                "column": exc.colno,
                "offset": exc.pos,
                "excerpt": excerpt,
                "caret": caret,
            }
        )
        return None


def extract_json_result(
    completed: Any,
    *,
    validator: ResultValidator | None = None,
    require_frame: bool = False,
) -> dict[str, Any]:
    """Extract exactly one schema-valid result.

    Selection order is framed results, the complete stdout JSON document, then
    every top-level JSON candidate found by ``raw_decode``.  A tier with more
    than one valid candidate is rejected as ambiguous.
    """

    diagnostics: list[dict[str, Any]] = []
    stdout = str(getattr(completed, "stdout", "") or "")
    supplied_frames = getattr(completed, "result_frames", ())
    frames = (
        [str(item) for item in supplied_frames]
        if isinstance(supplied_frames, (list, tuple))
        else []
    )
    if not frames:
        for line in stdout.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if line.startswith(RESULT_FRAME):
                frames.append(line[len(RESULT_FRAME) :].strip())

    valid_frames: list[dict[str, Any]] = []
    for index, payload in enumerate(frames, start=1):
        decoded = _decode_candidate(
            payload,
            stage="frame",
            candidate=index,
            diagnostics=diagnostics,
        )
        if decoded is not None and _validate_result(
            decoded,
            validator,
            stage="frame",
            candidate=index,
            diagnostics=diagnostics,
        ):
            valid_frames.append(decoded)
    if len(valid_frames) == 1:
        return valid_frames[0]
    if len(valid_frames) > 1:
        diagnostics.append(
            {
                "stage": "frame",
                "error": "ambiguous_results",
                "valid_count": len(valid_frames),
            }
        )
        raise ResultExtractionError(
            "multiple schema-valid framed results",
            diagnostics=diagnostics,
        )
    if require_frame:
        diagnostics.append(
            {
                "stage": "selection",
                "error": "no_valid_framed_result",
                "frame_count": len(frames),
            }
        )
        raise ResultExtractionError(
            "no schema-valid framed result",
            diagnostics=diagnostics,
        )

    stripped = stdout.strip()
    if stripped:
        decoded = _decode_candidate(
            stripped,
            stage="whole",
            candidate=1,
            diagnostics=diagnostics,
        )
        if decoded is not None and _validate_result(
            decoded,
            validator,
            stage="whole",
            candidate=1,
            diagnostics=diagnostics,
        ):
            return decoded

    decoder = json.JSONDecoder()
    valid_raw: list[dict[str, Any]] = []
    index = 0
    candidate = 0
    while index < len(stdout):
        brace = min(
            (
                offset
                for offset in (
                    stdout.find("{", index),
                    stdout.find("[", index),
                )
                if offset >= 0
            ),
            default=-1,
        )
        if brace < 0:
            break
        candidate += 1
        try:
            decoded, end = decoder.raw_decode(stdout, brace)
        except json.JSONDecodeError as exc:
            excerpt, caret = _decode_error_excerpt(stdout, exc.pos)
            diagnostics.append(
                {
                    "stage": "raw_decode",
                    "candidate": candidate,
                    "start_offset": brace,
                    "error": "json_decode_error",
                    "line": exc.lineno,
                    "column": exc.colno,
                    "offset": exc.pos,
                    "excerpt": excerpt,
                    "caret": caret,
                }
            )
            index = brace + 1
            continue
        if _validate_result(
            decoded,
            validator,
            stage="raw_decode",
            candidate=candidate,
            diagnostics=diagnostics,
        ):
            valid_raw.append(decoded)
        index = max(end, brace + 1)
    if len(valid_raw) == 1:
        return valid_raw[0]
    if len(valid_raw) > 1:
        diagnostics.append(
            {
                "stage": "raw_decode",
                "error": "ambiguous_results",
                "valid_count": len(valid_raw),
            }
        )
        raise ResultExtractionError(
            "multiple schema-valid JSON results",
            diagnostics=diagnostics,
        )
    diagnostics.append(
        {
            "stage": "selection",
            "error": "no_valid_result",
            "frame_count": len(frames),
        }
    )
    raise ResultExtractionError(
        "no schema-valid JSON result",
        diagnostics=diagnostics,
    )


def _decode_error_excerpt(value: str, offset: int) -> tuple[str, str]:
    start = max(0, int(offset) - 160)
    end = min(len(value), int(offset) + 160)
    excerpt = sanitize_diagnostic(value[start:end], max_chars=400)
    relative = max(0, min(len(excerpt), int(offset) - start))
    return excerpt, " " * relative + "^"
