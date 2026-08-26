"""Parse orchestrator run logs into per-session context summaries.

The deployed orchestrator does not persist structured per-session state — by the
settled 2026-07-04 ruling the hub *reads* what the orchestrator already logs
rather than the orchestrator growing a new contract. Every genuine log line is
emitted through ``o.log()``/``flog()`` with a ``[HH:MM:SS] `` UTC time-of-day
prefix (``orchestrator.py``), and a handful of those lines bracket each protocol
session:

.. code-block:: text

    [HH:MM:SS] --- {role} plan-gate start: sid={sid} backend={desc} status_before={s}
    [HH:MM:SS] run finished: ... context≈{N} tokens          # one per turn
    [HH:MM:SS] --- {role} plan-gate end: sid={sid}
    [HH:MM:SS] --- {role} session start: sid={sid} backend={desc} status_before={s}
    [HH:MM:SS] run finished: ... context≈{N} tokens          # one per turn
    [HH:MM:SS] --- {role} session end: sid={sid} status={end_status}

``role`` is ``dev``/``review``; the plan-gate bracket is dev-only and is the
planning pre-session before the formal dev session (each is a distinct backend
conversation, so context is per-session, not cumulative). ``run finished`` lines
sit positionally inside the currently-open bracket and belong to it; a bracket
can have several (a plan-gate that looped, a multi-turn session), and the latest
one's ``context≈`` is the peak (context is monotonic within a conversation).
``sid`` can be the literal ``(pending)`` at start and carry the real value only
at end (a review session), so the two are reconciled. Context can exceed the 200k
budget (an observed dev session peaked at ~278k); the budget is a soft display
line, not a cap.

The parser is deliberately tolerant and never raises:

- Every marker must be anchored on ``^[HH:MM:SS] `` first. ``transcript()`` and
  ``ask_human()`` write raw agent text and ``--- ... ---`` banners into the same
  log file *without* that prefix, so anchoring excludes interleaved transcript
  content that would otherwise look like a marker. (A transcript that emits a
  perfectly-formed timestamped marker at column 0 is an unpreventable residual;
  it degrades to slightly-wrong partial data, never a crash or run-state impact.)
- A truncated / mid-write tail line simply fails to match and is skipped; a
  trailing session with no end line is a valid *open* summary; a session with
  zero turns is valid.

Read model: a *live* run is parsed from its current repo-side log on demand
(:func:`read_live`, located via :func:`orch_hub.logs.locate_log`, never a client
path); an *ended* run is served from the snapshot persisted into its run dir
(:func:`write_snapshot`/:func:`read_snapshot`), so the summary survives later
cleanup of the repo-side log. The run manager owns both — it writes the snapshot
once at the live->ended transition and chooses parse-vs-snapshot per read.

:func:`read_run_owned` is the third read, and the strict one: it stays inside a
single run's own dir (its snapshot, else its ``spawn.out`` stdout capture) and
never touches the repo-side log, whose filename a fast re-run of the same task
can collide with. That containment is what makes these markers usable as
*per-run* evidence — the run manager classifies a finished run from them.
:func:`read_run_closeout` is the same read for the one completion the brackets
cannot express (a blocked session resumed in place, archiving the task without
opening a bracket): the orchestrator's ``close-out done`` line, anchored the
same way, out of the same capture.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

from . import logs
from .models import (
    CONTEXT_BUDGET_TOKENS,
    RunRecord,
    SessionSummary,
    SessionsSnapshot,
)

SNAPSHOT_NAME = "sessions.json"
"""Persisted per-session summary, written beside ``run.json`` in the run dir."""

_PENDING_SID = "(pending)"
"""Literal the orchestrator logs when a backend has not yet revealed a sid."""

_ANCHOR = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] (.*)$")
"""Every genuine log line: ``[HH:MM:SS] <content>``. Anchoring on it first is
what excludes unprefixed transcript/banner text from marker matching."""

_START = re.compile(
    r"^--- (dev|review) (plan-gate|session) start: sid=(\S+) .*status_before=(\S+)$"
)
"""``--- {role} {kind} start: sid={sid} backend={desc} status_before={status}``.
The greedy ``.*`` spans the backend descriptor to the final ``status_before=``."""

_END = re.compile(
    r"^--- (dev|review) (plan-gate|session) end: sid=(\S+)(?: status=(\S+))?$"
)
"""``--- {role} {kind} end: sid={sid}[ status={end_status}]``. A session end
carries ``status=``; a plan-gate end does not (optional group)."""

_RUN_FINISHED = re.compile(r"^run finished:.*context≈(\d+) tokens$")
"""Both backends' per-turn line ends ``context≈{N} tokens``; match on that."""

_CLOSE_OUT_DONE = re.compile(r"^close-out done\b")
"""``close-out done``, and its ``close-out done (performed in-session by the
native hook chain; ...)`` variant. Not a bracket marker and not part of a
session summary — the run's own terminal statement that its task's close-out
finished, read by :func:`read_run_closeout`."""


class _Open:
    """Mutable accumulator for the currently-open session bracket."""

    __slots__ = (
        "role",
        "kind",
        "start_sid",
        "status_before",
        "start_hms",
        "turns",
        "context_tokens",
        "ended",
        "end_sid",
        "end_status",
    )

    def __init__(
        self,
        role: str,
        kind: str,
        start_sid: str,
        status_before: str,
        start_hms: str,
    ):
        self.role = role
        self.kind = kind
        self.start_sid = start_sid
        self.status_before = status_before
        self.start_hms = start_hms
        self.turns = 0
        self.context_tokens: int | None = None
        self.ended = False
        self.end_sid: str | None = None
        self.end_status: str | None = None


def parse_sessions(lines: Iterable[str]) -> SessionsSnapshot:
    """Scan log lines into an ordered :class:`SessionsSnapshot`; never raises.

    A single-pass, bounded-memory scan: it retains only the small open-bracket
    accumulator and the finished summaries, never the log lines themselves, so a
    large log (transcripts get big) does not load whole. Each line is anchored on
    ``[HH:MM:SS] `` before any marker match, so transcript/banner text without
    that prefix is ignored. A ``start`` while a bracket is still open finalizes
    the prior as *open* (its end line was lost or it is still live); an ``end``
    matching the open bracket's role+kind closes it; an orphan or role/kind
    mismatched end is ignored so a stray line cannot truncate a live bracket. A
    ``run finished`` line increments the open bracket's turn count and updates its
    latest context.
    """
    sessions: list[SessionSummary] = []
    current: _Open | None = None
    for raw in lines:
        anchored = _ANCHOR.match(raw.rstrip("\r\n"))
        if anchored is None:
            continue  # transcript/banner/partial line: not an anchored marker
        hms, rest = anchored.group(1), anchored.group(2)
        start = _START.match(rest)
        if start is not None:
            if current is not None:
                sessions.append(_finalize(current))  # prior never ended -> open
            current = _Open(
                start.group(1), start.group(2), start.group(3), start.group(4), hms
            )
            continue
        end = _END.match(rest)
        if end is not None:
            if (
                current is not None
                and current.role == end.group(1)
                and current.kind == end.group(2)
            ):
                current.ended = True
                current.end_sid = end.group(3)
                current.end_status = end.group(4)
                sessions.append(_finalize(current))
                current = None
            # An orphan end (nothing open) or a role/kind mismatch is ignored: it
            # must never truncate a legitimately open bracket.
            continue
        finished = _RUN_FINISHED.match(rest)
        if finished is not None and current is not None:
            current.turns += 1
            current.context_tokens = int(finished.group(1))  # latest wins (peak)
    if current is not None:
        sessions.append(_finalize(current))  # trailing open bracket (live/truncated)
    return SessionsSnapshot(sessions=tuple(sessions))


def _finalize(open_session: _Open) -> SessionSummary:
    return SessionSummary(
        role=open_session.role,
        kind=open_session.kind,
        sid=_reconcile_sid(open_session.start_sid, open_session.end_sid),
        turns=open_session.turns,
        context_tokens=open_session.context_tokens,
        status_before=open_session.status_before,
        end_status=open_session.end_status,
        start_hms=open_session.start_hms,
        open=not open_session.ended,
    )


def _reconcile_sid(start_sid: str, end_sid: str | None) -> str | None:
    """Prefer a real sid over ``(pending)``: start when known, else end, else None."""
    if _real_sid(start_sid):
        return start_sid
    if end_sid is not None and _real_sid(end_sid):
        return end_sid
    return None


def _real_sid(sid: str) -> bool:
    return bool(sid) and sid != _PENDING_SID


def read_live(record: RunRecord) -> SessionsSnapshot:
    """Parse a run's current orchestrator log; empty snapshot if none/unreadable.

    Locates the log from the run record alone (:func:`orch_hub.logs.locate_log`
    — ``log_dir``/``task_id``/``started_ts``, never a client path) and streams it
    through :func:`parse_sessions`. A run with no log yet, or one whose file
    rotated/was removed, yields an empty snapshot, never an error — the same
    graceful-absence contract the logs module uses.
    """
    return _parse_file(logs.locate_log(record))


def read_run_owned(run_dir: Path, record: RunRecord) -> SessionsSnapshot:
    """Parse the sessions of *this* run from artifacts inside its own run dir.

    The first non-empty of two run-local sources: the ``sessions.json`` persisted
    at the run's end, then the ``spawn.out`` capture of its stdout. The fallback
    works because ``o.log()`` prints every line it writes to the log file, so the
    capture holds the whole marker stream — and only that: ``flog()`` and
    ``transcript()`` are file-only, which leaves the capture a strict, smaller
    subset with none of the transcript text the anchor rule exists to skip. The
    snapshot still goes first (already parsed, and the same artifact the sessions
    view serves, so a verdict cannot contradict the session list beside it); the
    capture covers a run predating the snapshot field and a run whose end found
    no locatable log, whose snapshot is therefore an empty one it can fill.

    Deliberately *not* :func:`read_live`: the repo-side log is located by a
    ``<utc-ts>-<task-id>.log`` convention two runs of one task started in the
    same second share, so it is not evidence about one run. Everything read here
    was written by this run into its own dir and never changes again — which is
    what lets a caller classify a finished run from it. Absent or unreadable
    sources yield an empty snapshot, never an error.
    """
    snapshot = read_snapshot(run_dir)
    if snapshot is not None and snapshot.sessions:
        return snapshot
    return _parse_file(record.spawn_out)


def read_run_closeout(record: RunRecord) -> bool:
    """Did *this* run log its task's close-out as done?

    The second run-owned completion carrier, in the same anchored-marker
    vocabulary as the brackets and read out of the same ``spawn.out`` capture:
    the orchestrator logs ``close-out done`` when it drives the close-out from
    its loop head, and ``close-out done (performed in-session ...)`` when it
    verifies one a session's native hook chain already ran. Either line is
    written during this run into this run's own dir and never changes again.

    It is needed because one real completion path leaves no bracket at all. A
    *blocked* task is resumed by re-entering the blocked session directly
    (``handle_blocked()``) rather than through the bracketed session runner, so
    a final gate concluded in that resumed turn archives the task without any
    ``--- {role} session start/end`` pair being logged. Such a run's last
    bracket reads ``status=blocked``, or it has none at all when it launched
    against the already-blocked task — brackets alone would call a run that
    finished its task merely ended.

    Deliberately read from the capture only: ``sessions.json`` holds parsed
    brackets, so it cannot carry this line, and reading the repo-side log would
    give up the containment (:func:`read_run_owned`) the verdict depends on. An
    absent or unreadable capture is ``False``, never an error.
    """
    return _scan_closeout(record.spawn_out)


def _scan_closeout(path: Path | None) -> bool:
    """Stream one file for the close-out marker; absent/unreadable -> ``False``.

    Same anchor rule as :func:`parse_sessions` — a marker only counts on a
    genuine ``[HH:MM:SS] `` line, so agent text quoting the phrase cannot fake
    it — and the same single-pass, bounded-memory read.
    """
    if path is None:
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                anchored = _ANCHOR.match(raw.rstrip("\r\n"))
                if anchored is not None and _CLOSE_OUT_DONE.match(anchored.group(2)):
                    return True
    except OSError:
        return False
    return False


def _parse_file(path: Path | None) -> SessionsSnapshot:
    """Stream one file through :func:`parse_sessions`; absent/unreadable -> empty."""
    if path is None:
        return SessionsSnapshot()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return parse_sessions(handle)
    except OSError:
        return SessionsSnapshot()


def snapshot_path(run_dir: Path) -> Path:
    return run_dir / SNAPSHOT_NAME


def write_snapshot(run_dir: Path, snapshot: SessionsSnapshot) -> None:
    """Persist ``sessions.json`` atomically (same-dir tmp + rename), like run.json."""
    path = snapshot_path(run_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(snapshot.to_dict(), indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_snapshot(run_dir: Path) -> SessionsSnapshot | None:
    """Load a persisted ``sessions.json``, or ``None`` when absent/unreadable.

    ``None`` distinguishes "no snapshot was ever written" (a run predating this
    field, so the caller may fall back to a live parse) from an *empty* snapshot
    written deliberately (the run ended with no locatable log). Corrupt JSON is
    treated as absent rather than raising.
    """
    try:
        data = json.loads(snapshot_path(run_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return _snapshot_from_dict(data)


def _snapshot_from_dict(data: object) -> SessionsSnapshot | None:
    """Rebuild a snapshot from persisted JSON, tolerating a corrupt/partial file."""
    if not isinstance(data, dict):
        return None
    raw_sessions = data.get("sessions")
    summaries: list[SessionSummary] = []
    if isinstance(raw_sessions, list):
        for item in raw_sessions:
            summary = _summary_from_dict(item)
            if summary is not None:
                summaries.append(summary)
    budget = data.get("context_budget")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        budget = CONTEXT_BUDGET_TOKENS
    return SessionsSnapshot(sessions=tuple(summaries), context_budget=budget)


def _summary_from_dict(item: object) -> SessionSummary | None:
    if not isinstance(item, dict):
        return None
    role = item.get("role")
    kind = item.get("kind")
    if not isinstance(role, str) or not isinstance(kind, str):
        return None
    return SessionSummary(
        role=role,
        kind=kind,
        sid=_opt_str(item.get("sid")),
        turns=_opt_int(item.get("turns")) or 0,
        context_tokens=_opt_int(item.get("context_tokens")),
        status_before=_opt_str(item.get("status_before")),
        end_status=_opt_str(item.get("end_status")),
        start_hms=_opt_str(item.get("start_hms")),
        open=bool(item.get("open")),
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
