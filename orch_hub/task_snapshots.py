"""Per-session task-file snapshots for one run (``task_snapshots.json``).

Replaying a past session needs the task file *as it stood when that session
started*, and nothing else keeps it: ``.ai-tasks/`` is gitignored, so no commit
carries it, and the archived file has lost exactly the fields a reconstruction
would have to guess — ``session-est`` ``total`` (preReEst raises it mid-flight,
only the final value survives), ``prefetch`` (mutable, backfilled with what was
actually consulted), and the body's Goal/Scope/Acceptance (the dev contract
permits adjusting them as understanding sharpens). All three skew the same way:
a reconstruction hands the replayed session more context and a better-calibrated
plan than the original had, which makes a replay look systematically better than
the run it replays. This module captures the real bytes instead.

Capture is hub-side, by the settled 2026-07-04 ruling (:mod:`orch_hub.sessions`):
the hub reads what the orchestrator already logs rather than the orchestrator
growing a new contract. The trigger is the same ``--- {role} session start`` line
:mod:`orch_hub.sessions` brackets on, observed by whatever drives
:meth:`orch_hub.runs.RunManager.list_runs` — the resident 30s monitor, plus
ordinary dashboard traffic. Reading a log line rather than being called at the
boundary has four consequences, each of them recorded rather than papered over:

- **A capture is not the pre-entry file.** The orchestrator logs the start line
  *before* it sends the first prompt, but the hub sees it seconds later, by which
  point the session has usually claimed the task (``claimed-by``, the
  ``session-est`` increment). Every entry says so
  (:data:`CAPTURE_POINT_POST_CLAIM`) instead of presenting itself as the file the
  session found. Post-claim is the state the session actually worked from and the
  claim is mechanical, so this is the right thing to capture; widening it into an
  orchestrator-side boundary contract would mean reopening that ruling.
- **The window is bounded** (:data:`ENTRY_LAG_MAX_SECONDS`). A hub that starts
  mid-session would otherwise capture a file the session has already been
  editing for an hour and label it "entry" — the very skew this exists to
  remove. Past the window the session is recorded as *not captured*, and every
  entry carries its own :attr:`SessionTaskSnapshot.entry_lag_seconds` so a
  consumer can weigh how close to entry the bytes really are.
- **Only ``session`` brackets are captured**, not the dev ``plan-gate``
  pre-session. A plan-gate does not claim the task, and its sibling dev session
  — whose own snapshot follows — is the unit that writes; marking a plan-gate
  capture post-claim would be false, and the two files differ only by the
  mechanical claim.
- **A pending sid is backfilled.** Codex names its session id on the first turn,
  so the start line can log ``sid=(pending)`` and only the end line reveals it.
  Entries are therefore keyed by the bracket's position in the run's parsed
  session list (stable: the log is append-only, and ``sessions.json`` is the same
  parse), with the sid filled in once the log carries it — see
  :func:`reconcile_sids`.

The artifact sits beside ``sessions.json`` in the run dir and is rewritten whole
on each change, which is what lets a later scan fill in a sid. Concurrent scans
are harmless: both compute the same entry for the same bracket, writes are atomic
and per-writer, and a backfill lost to a racing capture is redone by the next
scan.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .models import ManagedRepo, RunRecord, SessionsSnapshot
from .task_store import read_task_content

_log = logging.getLogger(__name__)

ARTIFACT_NAME = "task_snapshots.json"
"""Persisted per-session task-file snapshots, written beside ``sessions.json``."""

SESSION_KIND = "session"
"""The :class:`~orch_hub.models.SessionSummary` kind that claims the task. The
dev ``plan-gate`` pre-session is deliberately not captured (see the module doc)."""

CAPTURE_POINT_POST_CLAIM = "post_claim"
"""What every entry's :attr:`SessionTaskSnapshot.capture_point` states: the bytes
were read after the session-start line was observed, so they are *not* the
pre-entry file and normally already carry the session's mechanical claim. It is a
provenance label, not a measurement — a scan that beats a slow agent to the claim
captures the file just before it, and ``entry_lag_seconds`` plus the ``claimed-by``
line in the captured content say exactly where a given entry fell."""

ENTRY_LAG_MAX_SECONDS = 180
"""How long after the session-start line a capture still counts as entry state.
Six times the monitor's 30s interval, so ordinary jitter, a slow scan, or a hub
restarted moments into a session never loses a capture — while a hub that comes
up well inside a running session records a miss instead of passing that session's
own mid-flight edits off as its entry state."""

TASK_FILE_MAX_BYTES = 256 * 1024
"""Per-snapshot byte cap. Measured task files run ~16KB (largest observed 170KB),
so a real one is never truncated and a pathological one is bounded rather than
refused. The *head* is kept: the frontmatter leads the file, and it holds exactly
the fields (``session-est``, ``prefetch``, ``status``) that nothing else
recovers."""

MAX_CONTENT_SNAPSHOTS = 24
"""Cap on how many entries in one run may carry content. Runs host a handful of
sessions, so this is pathology insurance on the artifact's size; a session beyond
it is still recorded, as an entry with no content, so the cap is never silent."""


@dataclass(frozen=True)
class SessionTaskSnapshot:
    """The target task file as one protocol session entered it (or why it is not).

    ``index`` is the session's position in the run's parsed session list
    (:class:`~orch_hub.models.SessionsSnapshot`), which is the join key while
    ``sid`` may still be unknown; ``role``/``kind``/``status_before``/
    ``session_start_hms`` restate the bracket so the entry is self-describing when
    read back beside a ``sessions.json`` written later.

    ``present`` follows :class:`orch_hub.ai_snapshot.AiDoc`: True only when the
    file was found *and* read into ``content``. A False entry is a recorded miss
    — the entry window closed, or the content cap was reached — with ``detail``
    saying which; the run and every downstream read still complete. ``size`` is
    the file's true byte size and ``truncated`` marks a kept head shorter than it.

    ``captured_at`` is the UTC instant the bytes were read and
    ``entry_lag_seconds`` how long after the session-start log line that was
    (``None`` when the log carried no usable stamp). ``capture_point`` is always
    :data:`CAPTURE_POINT_POST_CLAIM`.
    """

    index: int
    sid: str | None = None
    role: str | None = None
    kind: str = SESSION_KIND
    status_before: str | None = None
    session_start_hms: str | None = None
    captured_at: str | None = None
    capture_point: str = CAPTURE_POINT_POST_CLAIM
    entry_lag_seconds: int | None = None
    present: bool = False
    content: str | None = None
    size: int = 0
    truncated: bool = False
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "sid": self.sid,
            "role": self.role,
            "kind": self.kind,
            "status_before": self.status_before,
            "session_start_hms": self.session_start_hms,
            "captured_at": self.captured_at,
            "capture_point": self.capture_point,
            "entry_lag_seconds": self.entry_lag_seconds,
            "present": self.present,
            "content": self.content,
            "size": self.size,
            "truncated": self.truncated,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TaskSnapshots:
    """One run's session-entry task-file snapshots, in session order.

    An empty set is a valid artifact (a run whose sessions were all observed too
    late, or that never reached one). Absence of an entry for a session is not an
    error either: it means no capture was recorded for it, and a consumer treats
    that session's task file as reconstructed rather than exact.
    """

    snapshots: tuple[SessionTaskSnapshot, ...] = ()

    def for_index(self, index: int) -> SessionTaskSnapshot | None:
        """The entry recorded for a session's bracket position, if any."""
        for entry in self.snapshots:
            if entry.index == index:
                return entry
        return None

    def for_sid(self, sid: str) -> SessionTaskSnapshot | None:
        """The entry for a backend session id, if one was captured and reconciled.

        The join a consumer holding a :class:`~orch_hub.models.SessionSummary`
        uses. ``None`` covers both "no capture for that session" and "the sid was
        never revealed", which are the same thing downstream: no exact task file.
        """
        for entry in self.snapshots:
            if entry.sid is not None and entry.sid == sid:
                return entry
        return None

    def to_dict(self) -> dict:
        return {"snapshots": [entry.to_dict() for entry in self.snapshots]}


def artifact_path(run_dir: Path) -> Path:
    return run_dir / ARTIFACT_NAME


def write_snapshots(run_dir: Path, snapshots: TaskSnapshots) -> None:
    """Persist the whole set atomically (per-writer tmp + rename).

    The tmp name carries pid and thread id — unlike ``sessions.json``, which is
    written once per run at the end transition, this file is rewritten by any
    scan, so two writers must not share a tmp path.
    """
    path = artifact_path(run_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(snapshots.to_dict(), indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_snapshots(run_dir: Path) -> TaskSnapshots | None:
    """Load a run's persisted snapshots, or ``None`` when absent/unreadable.

    ``None`` distinguishes "nothing was ever captured for this run" (a run that
    predates this artifact, or one no scan reached) from an empty set. Corrupt
    JSON is treated as absent rather than raising, like
    :func:`orch_hub.sessions.read_snapshot`.
    """
    try:
        data = json.loads(artifact_path(run_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return _from_dict(data)


def capture_entry(
    run_dir: Path,
    record: RunRecord,
    sessions: SessionsSnapshot,
    *,
    now: dt.datetime,
    read_task: Callable[[ManagedRepo, str], str | None] = read_task_content,
) -> SessionTaskSnapshot | None:
    """Capture the task file for the run's currently-open protocol session.

    ``sessions`` is the caller's fresh parse of the run's log
    (:func:`orch_hub.sessions.read_live`) and ``now`` a UTC instant; the module
    reads logs through neither, so the caller owns log access and clock exactly as
    it does for ``sessions.json``.

    Only the *last* bracket is a candidate: an earlier one is either already
    recorded or long past its entry window, and a bracket that has closed took its
    window with it. Returns the entry just persisted, or ``None`` when nothing was
    written — no open ``session`` bracket, one already recorded, or a read failure
    inside the window, which is left unrecorded on purpose so the next scan retries
    (a mid-write file is over in milliseconds; a genuinely absent one is recorded
    when the window closes). Failures are warned about here; the file is never a
    reason for a run to fail.
    """
    index = len(sessions.sessions) - 1
    if index < 0:
        return None
    summary = sessions.sessions[index]
    if not summary.open or summary.kind != SESSION_KIND:
        return None
    existing = read_snapshots(run_dir) or TaskSnapshots()
    if existing.for_index(index) is not None:
        return None
    lag = _entry_lag_seconds(summary.start_hms, now)
    entry = SessionTaskSnapshot(
        index=index,
        sid=summary.sid,
        role=summary.role,
        kind=summary.kind,
        status_before=summary.status_before,
        session_start_hms=summary.start_hms,
        captured_at=now.isoformat(),
        entry_lag_seconds=lag,
    )
    if lag is not None and lag > ENTRY_LAG_MAX_SECONDS:
        return _persist(
            run_dir,
            existing,
            replace(
                entry,
                detail=(
                    f"session start observed {lag}s late, past the "
                    f"{ENTRY_LAG_MAX_SECONDS}s entry window"
                ),
            ),
        )
    if sum(1 for kept in existing.snapshots if kept.present) >= MAX_CONTENT_SNAPSHOTS:
        return _persist(
            run_dir,
            existing,
            replace(
                entry,
                detail=f"run already holds {MAX_CONTENT_SNAPSHOTS} captured task files",
            ),
        )
    content, failure = _read_task_file(record, read_task)
    if content is None:
        _log.warning(
            "task-file snapshot for run %s session %s: %s",
            record.run_id,
            summary.sid or f"#{index}",
            failure,
        )
        return None
    data = content.encode("utf-8")
    kept = data[:TASK_FILE_MAX_BYTES]
    return _persist(
        run_dir,
        existing,
        replace(
            entry,
            present=True,
            # Lossy-tolerant decode, matching the log/doc readers: it absorbs the
            # partial multi-byte character a cut at a byte offset can leave.
            content=kept.decode("utf-8", errors="replace"),
            size=len(data),
            truncated=len(kept) < len(data),
        ),
    )


def reconcile_sids(run_dir: Path, sessions: SessionsSnapshot) -> TaskSnapshots | None:
    """Fill in session ids that were still ``(pending)`` when captured.

    Codex names its session id on the first turn, so a bracket can log
    ``sid=(pending)`` at start and reveal the real id only at its end line — which
    a live scan may never see (the run can exit right after it). Re-run this
    against the run's final parse to settle those entries; a scan that happens to
    catch the end line settles them earlier, and both are idempotent.

    An entry adopts a sid only when the bracket at its position still matches the
    role, kind, and start stamp it was captured with, so a shifted index (a
    rotated or truncated log) leaves the entry unreconciled instead of stamping it
    with another session's id. Returns the rewritten set, or ``None`` when there
    was nothing to change.
    """
    existing = read_snapshots(run_dir)
    if existing is None:
        return None
    changed = False
    settled: list[SessionTaskSnapshot] = []
    for entry in existing.snapshots:
        sid = _revealed_sid(sessions, entry)
        if sid is not None:
            entry = replace(entry, sid=sid)
            changed = True
        settled.append(entry)
    if not changed:
        return None
    snapshots = TaskSnapshots(snapshots=tuple(settled))
    write_snapshots(run_dir, snapshots)
    return snapshots


def _persist(
    run_dir: Path, existing: TaskSnapshots, entry: SessionTaskSnapshot
) -> SessionTaskSnapshot:
    """Append one entry and rewrite the artifact (brackets arrive in order)."""
    write_snapshots(run_dir, TaskSnapshots(snapshots=(*existing.snapshots, entry)))
    return entry


def _read_task_file(
    record: RunRecord, read_task: Callable[[ManagedRepo, str], str | None]
) -> tuple[str | None, str | None]:
    """Read the run's target task file; ``(content, None)`` or ``(None, reason)``.

    Goes through :func:`orch_hub.task_store.read_task_content` so the run's own
    ``task_id`` is subject to the same single-segment and containment discipline
    every other task-file read is — a run record is hub-written, but this read
    should not be the one place that trusts it. Only the *active* file is read: a
    session entering a task is by definition working an active one, and a
    fallback to ``archive/`` would silently capture a post-close-out file.
    """
    repo = record.managed_repo()
    if repo is None:
        return None, "run record carries no repo path"
    try:
        content = read_task(repo, record.task_id)
    except OSError as err:
        return None, f"unreadable ({err})"
    if content is None:
        return None, f".ai-tasks/{record.task_id}.md not present"
    if not content.strip():
        return None, "task file is empty"
    if not _frontmatter_closed(content):
        return None, "task file frontmatter is unterminated (write in progress?)"
    return content, None


def _frontmatter_closed(content: str) -> bool:
    """True unless the file opens a ``---`` frontmatter fence it never closes.

    A cheap partial-write guard, not a validator: a task file is written head
    first, so a read that caught the write in progress usually shows an open
    fence. A cut further down the body is indistinguishable from a short file and
    is accepted as-is; a file with no fence at all (not a conforming task file,
    but not evidence of a partial write either) is captured rather than dropped.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return True
    return any(line.strip() == "---" for line in lines[1:])


def _entry_lag_seconds(start_hms: str | None, now: dt.datetime) -> int | None:
    """Seconds between a ``HH:MM:SS`` session-start stamp and ``now`` (UTC).

    The orchestrator logs UTC time-of-day with no date, so the difference is taken
    modulo a day. A session observed within a few minutes of an exact 24h multiple
    would alias to a small lag — a session that long is not a real shape, and the
    cost would be one mislabeled entry, not a wrong run state. An unparseable
    stamp yields ``None``: unknown lag, which :func:`capture_entry` treats as no
    reason to refuse the capture.

    An aware ``now`` is converted rather than trusted: comparing a local-time
    clock against a UTC log stamp would produce an hours-wide lag and silently
    refuse every capture in a non-UTC timezone.
    """
    if not start_hms:
        return None
    parts = start_hms.split(":")
    if len(parts) != 3 or not all(p.isascii() and p.isdigit() for p in parts):
        return None
    hours, minutes, seconds = (int(part) for part in parts)
    if hours > 23 or minutes > 59 or seconds > 59:
        return None
    moment = now.astimezone(dt.timezone.utc) if now.tzinfo is not None else now
    started = hours * 3600 + minutes * 60 + seconds
    current = moment.hour * 3600 + moment.minute * 60 + moment.second
    return (current - started) % 86400


def _revealed_sid(
    sessions: SessionsSnapshot, entry: SessionTaskSnapshot
) -> str | None:
    """A sid now known for ``entry``'s bracket, or ``None`` if nothing to settle."""
    if entry.sid is not None or not 0 <= entry.index < len(sessions.sessions):
        return None
    summary = sessions.sessions[entry.index]
    if (
        summary.role != entry.role
        or summary.kind != entry.kind
        or summary.start_hms != entry.session_start_hms
    ):
        return None
    return summary.sid


def _from_dict(data: object) -> TaskSnapshots | None:
    """Rebuild a set from persisted JSON, tolerating a corrupt/partial file."""
    if not isinstance(data, dict):
        return None
    raw = data.get("snapshots")
    entries: list[SessionTaskSnapshot] = []
    if isinstance(raw, list):
        for item in raw:
            entry = _entry_from_dict(item)
            if entry is not None:
                entries.append(entry)
    return TaskSnapshots(snapshots=tuple(entries))


def _entry_from_dict(item: object) -> SessionTaskSnapshot | None:
    if not isinstance(item, dict):
        return None
    index = _opt_int(item.get("index"))
    if index is None:
        return None  # the join key: an entry without one carries no evidence
    return SessionTaskSnapshot(
        index=index,
        sid=_opt_str(item.get("sid")),
        role=_opt_str(item.get("role")),
        kind=_opt_str(item.get("kind")) or SESSION_KIND,
        status_before=_opt_str(item.get("status_before")),
        session_start_hms=_opt_str(item.get("session_start_hms")),
        captured_at=_opt_str(item.get("captured_at")),
        capture_point=_opt_str(item.get("capture_point")) or CAPTURE_POINT_POST_CLAIM,
        entry_lag_seconds=_opt_int(item.get("entry_lag_seconds")),
        present=bool(item.get("present")),
        content=_opt_str(item.get("content")),
        size=_opt_int(item.get("size")) or 0,
        truncated=bool(item.get("truncated")),
        detail=_opt_str(item.get("detail")),
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
