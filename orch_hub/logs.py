"""Locate and read orchestrator run logs.

A run's log path is *not* stored in its ``run.json``: the deployed orchestrator
stamps its own filename ``<log_dir>/<utc-ts>-<task-id>.log`` at startup
(``orchestrator.py``: ``ts = strftime("%Y%m%dT%H%M%SZ")``, then
``LOG_DIR / f"{ts}-{task_id}.log"``; ``LOG_DIR`` resolves from the script's
own dir to ``<repo>/.cursor/orchestrator/logs`` — exactly the ``log_dir`` the
run record carries). So the log is *located*, not looked up, from the record's
``log_dir``, ``task_id``, and ``started_ts``.

Attribution matters because one task is re-run across many dev/review sessions,
each leaving its own ``<utc-ts>-<task-id>.log`` in the same dir. A run's own
log is the earliest matching file stamped at or after the run started — a later
re-run stamps a strictly later ts (:func:`locate_log`). The stamp format is the
same ``%Y%m%dT%H%M%SZ`` the run manager uses for run ids. The run manager
records ``started_ts`` just *before* spawning (:mod:`orch_hub.runs`), and the
orchestrator picks its own filename later during its startup, so on one machine
that stamp is wall-clock >= ``started_ts`` and a second-floored ``started_ts``
is a sound lower bound.

Log access is deliberately scoped to the run record: callers pass a
:class:`~orch_hub.models.RunRecord` (obtained from the run manager for a known
run id), never a client-supplied path, so no path outside a managed repo's log
dir is ever read. Missing or rotated logs are reported as an absent snapshot,
never an error — a run may be listed before its orchestrator writes the first
line, and old logs get cleaned up out from under us.

Live tailing (:func:`tail_events`) follows that same located file, emitting each
newly completed line as a Server-Sent Events ``data:`` frame and a terminal
``event: end`` frame when the run ends or the file rotates away. It rides the
same bearer-header auth as every other route, so browser clients tail it with
``fetch`` plus a stream reader rather than ``EventSource`` (which cannot set an
``Authorization`` header): the hub keeps one header-based credential path and
never accepts the token in a URL.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .models import RunRecord

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
"""Orchestrator log-filename timestamp format (matches ``runs._RUN_ID_TS``)."""

DEFAULT_TAIL_LINES = 200
"""Recent-log lines returned when the caller does not ask for a specific tail."""

MAX_TAIL_LINES = 10_000
"""Hard cap on requested tail lines, so one request cannot ask for everything."""

_TAIL_BYTES = 1 << 20
"""Bytes read from the file end for a tail: bounds memory on a huge log
(transcripts get large) while comfortably holding a normal line tail. When it
binds before the line count, the snapshot is marked ``truncated``."""


@dataclass(frozen=True)
class LogSnapshot:
    """A bounded recent tail of one run's log.

    ``present`` is True only when a log file was found and read; a run with no
    log yet, or one whose file rotated away, yields ``present=False`` with a
    human ``detail`` (never an error). ``lines`` are the tail lines with line
    endings stripped, oldest first. ``truncated`` means earlier content was
    dropped to honor the line/byte bounds. ``size`` is the current file size in
    bytes (0 when absent). ``path`` is kept internal (filesystem detail the UI
    does not need) and is excluded from :meth:`to_dict`, matching
    ``Question``/``RunRecord``.
    """

    present: bool
    lines: tuple[str, ...] = ()
    truncated: bool = False
    size: int = 0
    detail: str | None = None
    path: Path | None = None

    def to_dict(self) -> dict:
        return {
            "present": self.present,
            "lines": list(self.lines),
            "truncated": self.truncated,
            "size": self.size,
            "detail": self.detail,
        }


def locate_log(record: RunRecord) -> Path | None:
    """Return the log file this run wrote, or ``None`` if none is found.

    Matches ``<utc-ts>-<task_id>.log`` files in the record's ``log_dir`` whose
    ts prefix parses and is at or after the run's floored start, then returns
    the earliest such file — the run's own log (a later re-run of the same task
    stamps a strictly later ts). The lower bound is exactly
    ``floor(started_ts)``, with no pre-start slack: the run manager stamps
    ``started_ts`` just *before* spawning (:mod:`orch_hub.runs`), and the
    orchestrator picks its own filename later during its startup, so on one
    machine that stamp is at or after ``floor(started_ts)``. A file stamped
    before the floored start therefore belongs to an earlier run, not this one;
    any pre-start tolerance would re-admit that earlier run's file and, as the
    earliest match, mis-attribute it. With no usable ``started_ts`` (broken
    record) the lower bound is dropped and the newest matching file is returned
    as a best-effort "most recent log for this task".

    Residual limit: two runs of one task that start within the same wall-clock
    second cannot be told apart by these second-precision stamps; the earlier
    file is returned for both. Per-repo serialization plus real orchestrator
    startup cost makes a genuine same-second re-run of the same task unlikely.
    """
    log_dir = record.log_dir
    task_id = record.task_id
    if log_dir is None or not task_id or not log_dir.is_dir():
        return None

    suffix = f"-{task_id}.log"
    lower_bound = _floor_started(record.started_ts)
    matches: list[tuple[dt.datetime, Path]] = []
    for entry in log_dir.iterdir():
        name = entry.name
        if not name.endswith(suffix) or not entry.is_file():
            continue
        stamp = _parse_stamp(name[: -len(suffix)])
        if stamp is None:
            continue
        if lower_bound is not None and stamp < lower_bound:
            continue  # stamped before this run started: an earlier run's log
        matches.append((stamp, entry))
    if not matches:
        return None
    # Earliest at/after the run start is this run's own file; only the
    # unattributable no-start fallback wants the newest instead.
    matches.sort(key=lambda pair: (pair[0], pair[1].name))
    return matches[0][1] if lower_bound is not None else matches[-1][1]


def read_recent(
    record: RunRecord,
    *,
    max_lines: int = DEFAULT_TAIL_LINES,
    max_bytes: int = _TAIL_BYTES,
) -> LogSnapshot:
    """Read a bounded tail of this run's log into a :class:`LogSnapshot`.

    Reads at most ``max_bytes`` from the file end (so a large log never loads
    whole) and keeps the last ``max_lines`` lines. Marks ``truncated`` when
    either bound dropped earlier content. A missing or since-rotated file is a
    graceful absent snapshot, not an exception.
    """
    max_lines = max(1, min(max_lines, MAX_TAIL_LINES))
    path = locate_log(record)
    if path is None:
        return LogSnapshot(present=False, detail="no log file found for this run yet")

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read()
    except FileNotFoundError:
        # Located, then rotated/removed before we opened it.
        return LogSnapshot(
            present=False, detail="log file rotated or removed", path=path
        )
    except OSError as err:
        return LogSnapshot(present=False, detail=f"log unreadable ({err})", path=path)

    byte_truncated = start > 0
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if byte_truncated and lines:
        # The byte window almost surely began mid-line; drop that partial head.
        lines = lines[1:]
    if lines and lines[-1] == "":
        # A trailing newline leaves an empty final element; it is not a line.
        lines.pop()

    line_truncated = len(lines) > max_lines
    tail = tuple(lines[-max_lines:])
    return LogSnapshot(
        present=True,
        lines=tail,
        truncated=byte_truncated or line_truncated,
        size=size,
        path=path,
    )


# -- live tailing (SSE) -------------------------------------------------------

DEFAULT_POLL_INTERVAL = 1.0
"""Seconds between file polls while following a live log."""

HEARTBEAT_SECONDS = 15.0
"""Idle gap after which the tailer emits an SSE comment heartbeat, so a client
(or an intermediary) can see the connection is alive during quiet stretches."""


def _sse_data(line: str) -> str:
    """Frame one log line as an SSE ``data:`` event.

    The line carries no embedded newline (the tailer splits on ``\\n``), so a
    single ``data:`` field holds it verbatim and the blank line ends the event.
    """
    return f"data: {line}\n\n"


def _sse_end(reason: str) -> str:
    """Frame the terminal ``event: end`` event that closes a stream cleanly."""
    return f"event: end\ndata: {reason}\n\n"


_SSE_HEARTBEAT = ": keep-alive\n\n"
"""An SSE comment line: clients ignore it; it keeps an idle connection warm."""


async def tail_events(
    record: RunRecord,
    *,
    is_live: Callable[[], bool],
    locator: Callable[[RunRecord], Path | None] = locate_log,
    initial_lines: int = DEFAULT_TAIL_LINES,
    max_bytes: int = _TAIL_BYTES,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[str]:
    """Follow this run's log, yielding Server-Sent Events until the run ends.

    Emits a bounded initial tail (the last ``initial_lines`` lines, same clamp
    as :func:`read_recent`) as ``data:`` events, then follows appended lines —
    one ``data:`` event per newly completed line. ``is_live()`` is re-checked
    each poll: once it returns False the tailer does one final draining read,
    emits a terminal ``event: end``, and stops, so an already-ended run yields a
    finite stream. A log that never appears (the run ended before its first
    write) or one that rotates away also ends cleanly with an ``event: end``
    carrying the reason. Only complete (newline-terminated) lines are emitted; a
    half-written trailing line waits for its newline.

    ``is_live`` and ``sleep`` are injected so the caller owns liveness (the run
    manager) and tests can drive the loop without real time; the route passes
    the defaults (:func:`asyncio.sleep` and a run-manager liveness probe). Each
    poll reads at most ``max_bytes`` so a burst of appended output cannot load
    unboundedly. The reads run inline on the event loop, acceptable for the v1
    localhost/tailnet deployment's low connection count.

    Client disconnects need no handling here: Starlette's ``StreamingResponse``
    races this generator against a disconnect listener and closes it when the
    client goes away.
    """
    initial_lines = max(1, min(initial_lines, MAX_TAIL_LINES))

    # Phase 1 — wait for the log file to appear. A run is listed the instant it
    # starts, before its orchestrator opens the log; poll until the file exists
    # or the run ends without ever writing one.
    path = locator(record)
    while path is None:
        if not is_live():
            yield _sse_end("no log file was written for this run")
            return
        yield _SSE_HEARTBEAT
        await sleep(poll_interval)
        path = locator(record)

    # Phase 2 — bounded initial tail. ``_initial_window`` returns the exact end
    # offset, so the follow phase never re-emits a line across the seam.
    try:
        offset, pending, initial = _initial_window(path, initial_lines, max_bytes)
    except OSError:
        yield _sse_end("log file rotated or removed")
        return
    for line in initial:
        yield _sse_data(line)

    # Phase 3 — follow. Read new bytes, emit each completed line, and once the
    # run is no longer live do one final draining read before ending.
    idle = 0.0
    ended = False
    while True:
        try:
            data, offset = _read_from(path, offset, max_bytes)
        except OSError:
            yield _sse_end("log file rotated or removed")
            return
        pending += data
        parts = pending.split(b"\n")
        pending = parts.pop()  # trailing partial line (empty after a newline)
        for chunk in parts:
            yield _sse_data(chunk.decode("utf-8", errors="replace"))
        if ended and not data:
            # The run is over and the final drain read nothing new: done.
            yield _sse_end("run ended")
            return
        if parts:
            idle = 0.0
        elif not is_live():
            # Keep reading (no sleep) until a drain read comes back empty, so a
            # final burst larger than one ``max_bytes`` window is not truncated.
            ended = True
            continue
        else:
            idle += poll_interval
            if idle >= heartbeat_seconds:
                yield _SSE_HEARTBEAT
                idle = 0.0
        await sleep(poll_interval)


def _initial_window(
    path: Path, max_lines: int, max_bytes: int
) -> tuple[int, bytes, list[str]]:
    """Read the last ``max_lines`` lines of the ``max_bytes`` tail window.

    Returns the exact end offset (where following resumes), the trailing partial
    bytes not yet a complete line, and the decoded initial lines. A window that
    starts mid-file drops its partial head line, mirroring :func:`read_recent`.
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        data = handle.read()
        offset = handle.tell()
    parts = data.split(b"\n")
    pending = parts.pop()  # trailing partial (empty when data ended on a newline)
    if start > 0 and parts:
        parts = parts[1:]  # byte window began mid-line: drop the partial head
    if len(parts) > max_lines:
        parts = parts[-max_lines:]
    return offset, pending, [chunk.decode("utf-8", errors="replace") for chunk in parts]


def _read_from(path: Path, offset: int, max_bytes: int) -> tuple[bytes, int]:
    """Read up to ``max_bytes`` new bytes from ``offset``; return (data, offset).

    A file now shorter than ``offset`` was truncated or rotated in place, so the
    read restarts from the top. A missing file raises ``OSError`` for the caller
    to treat as rotation.
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size < offset:
            offset = 0
        handle.seek(offset)
        data = handle.read(max_bytes)
        return data, handle.tell()


def _parse_stamp(stamp: str) -> dt.datetime | None:
    """Parse a ``%Y%m%dT%H%M%SZ`` filename stamp as aware UTC, or ``None``."""
    try:
        return dt.datetime.strptime(stamp, STAMP_FORMAT).replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return None


def _floor_started(started_ts: str | None) -> dt.datetime | None:
    """Parse an ISO ``started_ts`` to aware UTC floored to the second.

    Flooring matches the second-precision log stamp: a hub spawn at
    ``03:00:00.5`` and a log stamped ``03:00:00`` (same second) must compare
    equal, not later. A naive timestamp is assumed UTC; an unparseable one
    drops the lower bound.
    """
    if not started_ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(started_ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.replace(microsecond=0)
