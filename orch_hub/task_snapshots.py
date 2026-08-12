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
  :func:`settle_snapshots`.

Because the trigger is a poll and not a boundary callback, an observation can
arrive after a session is already over — a session that starts and ends between
two scans is never seen at entry at all. Such a session is *recorded as a miss*
rather than skipped: :func:`capture_entries` accounts for **every** ``session``
bracket the parse holds, not only the one that can still be captured, and
:func:`settle_snapshots` does the same final pass at the run's end, where no
further scan can follow. One entry per session either way — the difference is
whether it carries bytes, and a consumer reads ``present``/``detail`` to know
which. The alternative, letting an unobserved session leave no trace, is
indistinguishable downstream from a run that never had that session.

The artifact sits beside ``sessions.json`` in the run dir and is rewritten whole
on each change, which is what lets a later scan fill in a sid. Every rewrite is
therefore a read-modify-write, and the writers genuinely overlap (any scan
thread, plus the end transition), so each one runs inside a per-run
:func:`_locked` transaction: atomic rename alone stops a *torn* file, not a
stale writer overwriting a fresh one — a capture that read before an end
transition backfilled a sid would otherwise erase it, with no later scan to redo
it. Reads stay lock-free: the rename makes every reader see one whole version.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .models import ManagedRepo, RunRecord, SessionsSnapshot, SessionSummary
from .task_store import read_task_content

_log = logging.getLogger(__name__)

ARTIFACT_NAME = "task_snapshots.json"
"""Persisted per-session task-file snapshots, written beside ``sessions.json``."""

LOCK_NAME = "task_snapshots.lock"
"""Stable lock file for the artifact's read-modify-write transaction. Separate
from the artifact because the artifact is replaced by rename: a lock held on the
file it replaces guards an inode nobody writes to any more."""

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

_MISS_ENDED = "session had already ended when first observed"
_MISS_SUPERSEDED = "the run had already moved on when this session was observed"
_MISS_UNOBSERVED = "session was never observed while the run was live"
_MISS_CAP = f"run already holds {MAX_CONTENT_SNAPSHOTS} captured task files"
"""Why an accounted-for session carries no bytes. Each names *what the observer
missed*, never a fault of the session: the entry exists so a consumer can tell a
session with no exact task file from one that was never there at all."""


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
    — the session was already over when a scan first saw it, its entry window had
    closed, or the content cap was reached — with ``detail`` saying which; the run
    and every downstream read still complete. ``size`` is the file's true byte
    size and ``truncated`` marks a kept head shorter than it.

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
    """One run's session-entry task-file snapshots, in bracket order.

    An empty set is a valid artifact (a run that never reached a session). Once a
    run has ended, every ``session`` bracket its log holds has an entry here, so a
    missing one means the run's accounting never closed — it predates this
    artifact, or the hub died before its end transition. Downstream that reads the
    same as a miss: no exact task file, treat that session as reconstructed.
    """

    snapshots: tuple[SessionTaskSnapshot, ...] = ()

    def for_index(self, index: int) -> SessionTaskSnapshot | None:
        """The entry recorded for a session's bracket position, if any."""
        for entry in self.snapshots:
            if entry.index == index:
                return entry
        return None

    def for_sid(self, sid: str) -> SessionTaskSnapshot | None:
        """The entry recorded for a backend session id, if its sid is known.

        The join a consumer holding a :class:`~orch_hub.models.SessionSummary`
        uses. A returned entry may still be a recorded miss (``present`` False) —
        the session was accounted for but its bytes were not captured. ``None``
        means the sid was never revealed in the log, which is downstream the same
        thing as a miss: no exact task file for that session.
        """
        for entry in self.snapshots:
            if entry.sid is not None and entry.sid == sid:
                return entry
        return None

    def to_dict(self) -> dict:
        return {"snapshots": [entry.to_dict() for entry in self.snapshots]}


def artifact_path(run_dir: Path) -> Path:
    return run_dir / ARTIFACT_NAME


def lock_path(run_dir: Path) -> Path:
    return run_dir / LOCK_NAME


@contextmanager
def _locked(run_dir: Path) -> Iterator[None]:
    """Hold one run's read-modify-write transaction against threads *and* processes.

    Every write here recomputes the whole artifact from what it just read, so any
    interleaving of two writers drops one of them: a capture that read before an
    end transition backfilled a sid writes that sid back to ``None``, and two
    scans that observed different brackets each write a set missing the other's
    entry. Neither loss is self-healing — the end transition runs once per run,
    and a session already accounted for is never revisited.

    A POSIX advisory lock on :attr:`lock_path`, the same mechanism
    :mod:`orch_hub.report_catalog` uses for its append transaction: ``flock`` is
    held per open file description, so a fresh handle per entry makes it
    exclusive between this process's own threads as well as against a second hub
    process over the same data dir. The OS releases it if the holder dies, so a
    crashed scan cannot wedge a run's artifact.

    Lock order is one-way: a caller may hold the run manager's lock and take
    this one (the end transition does), but nothing holding this one reaches back
    for a manager lock — the only work inside is the artifact plus one bounded
    task-file read.
    """
    with lock_path(run_dir).open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_snapshots(run_dir: Path, snapshots: TaskSnapshots) -> None:
    """Persist the whole set atomically (per-writer tmp + rename).

    Serialization between writers is :func:`_locked`'s job, not this function's;
    the rename is what keeps a *reader* from ever seeing a partial file. The tmp
    name still carries pid and thread id so that a caller writing outside the
    transaction (tests, a future tool) cannot collide on the tmp path.
    """
    path = artifact_path(run_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(snapshots.to_dict(), indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_snapshots(run_dir: Path) -> TaskSnapshots | None:
    """Load a run's persisted snapshots, or ``None`` when absent/unreadable.

    ``None`` distinguishes "no artifact was ever written for this run" (one that
    predates it, or whose accounting never closed) from an empty set. Corrupt
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


def capture_entries(
    run_dir: Path,
    record: RunRecord,
    sessions: SessionsSnapshot,
    *,
    now: dt.datetime,
    read_task: Callable[[ManagedRepo, str], str | None] = read_task_content,
) -> tuple[SessionTaskSnapshot, ...]:
    """Account for every ``session`` bracket of a live run; capture the live one.

    ``sessions`` is the caller's fresh parse of the run's log
    (:func:`orch_hub.sessions.read_live`) and ``now`` a UTC instant; the module
    reads logs through neither, so the caller owns log access and clock exactly as
    it does for ``sessions.json``.

    Exactly one bracket can still be at entry — the last one, while it is open —
    and that is the only one whose bytes are read. Every *other* unrecorded
    ``session`` bracket is past: this scan is the first to see it and its entry
    state is already gone, so it is recorded as a miss naming why (it closed
    before it was observed, or a later session had already started). Recording it
    is the point: the poll that drives this can straddle a whole short session,
    and a session that leaves no entry at all is indistinguishable downstream from
    one that never ran.

    Returns the entries added by this call, newest bracket last; ``()`` when there
    was nothing to add. A read failure *inside* the entry window adds nothing on
    purpose, so the next scan retries (a mid-write file is over in milliseconds; a
    genuinely absent one is recorded when the window closes). Failures are warned
    about here; the task file is never a reason for a run to fail.
    """
    with _locked(run_dir):
        kept, changed = _settled_sids(read_snapshots(run_dir), sessions)
        last = len(sessions.sessions) - 1
        added: list[SessionTaskSnapshot] = []
        for index, summary in _unaccounted(sessions, kept):
            if index == last and summary.open:
                entry = _capture(
                    record,
                    index,
                    summary,
                    (*kept, *added),
                    now=now,
                    read_task=read_task,
                )
            else:
                entry = _missed(
                    index,
                    summary,
                    now,
                    _MISS_ENDED if index == last else _MISS_SUPERSEDED,
                )
            if entry is not None:
                added.append(entry)
        if changed or added:
            write_snapshots(run_dir, TaskSnapshots(snapshots=_ordered(kept, added)))
        return tuple(added)


def settle_snapshots(
    run_dir: Path, sessions: SessionsSnapshot, *, now: dt.datetime
) -> TaskSnapshots | None:
    """Close a run's accounting: backfill revealed sids, record what was missed.

    Run once against the run's final parse, at the live->ended transition, for the
    two things only that moment can settle:

    - **Session ids the log revealed late.** Codex names its session id on the
      first turn, so a bracket can log ``sid=(pending)`` at start and carry the
      real id only on its end line — which a live scan may never see, since the
      run can exit right after it. An entry adopts a sid only when the bracket at
      its position still matches the role, kind, and start stamp it was captured
      with, so a shifted index (a rotated or truncated log) leaves the entry
      unsettled rather than stamped with another session's id.
    - **Sessions no scan ever observed.** A run whose sessions all fell between
      polls — or that ended before any scan reached it — would otherwise leave an
      artifact that simply omits them. Each is recorded here as a miss, because
      after this no scan follows.

    Returns the rewritten set, or ``None`` when there was nothing to settle.
    """
    with _locked(run_dir):
        kept, changed = _settled_sids(read_snapshots(run_dir), sessions)
        added = [
            _missed(index, summary, now, _MISS_UNOBSERVED)
            for index, summary in _unaccounted(sessions, kept)
        ]
        if not changed and not added:
            return None
        snapshots = TaskSnapshots(snapshots=_ordered(kept, added))
        write_snapshots(run_dir, snapshots)
        return snapshots


def _unaccounted(
    sessions: SessionsSnapshot, kept: tuple[SessionTaskSnapshot, ...]
) -> Iterator[tuple[int, SessionSummary]]:
    """Session brackets with no entry yet, in log order.

    ``plan-gate`` brackets are not among them at all: a plan-gate does not claim
    the task, and its sibling dev session — the unit that writes — gets its own
    entry moments later (see the module doc), so it is neither captured nor
    counted as a miss.
    """
    recorded = {entry.index for entry in kept}
    for index, summary in enumerate(sessions.sessions):
        if summary.kind == SESSION_KIND and index not in recorded:
            yield index, summary


def _settled_sids(
    existing: TaskSnapshots | None, sessions: SessionsSnapshot
) -> tuple[tuple[SessionTaskSnapshot, ...], bool]:
    """Existing entries with any newly-revealed sid filled in, plus whether any was."""
    if existing is None:
        return (), False
    changed = False
    settled: list[SessionTaskSnapshot] = []
    for entry in existing.snapshots:
        sid = _revealed_sid(sessions, entry)
        if sid is not None:
            entry = replace(entry, sid=sid)
            changed = True
        settled.append(entry)
    return tuple(settled), changed


def _ordered(
    kept: tuple[SessionTaskSnapshot, ...], added: list[SessionTaskSnapshot]
) -> tuple[SessionTaskSnapshot, ...]:
    """The whole set in bracket order. Indexes are unique (``_unaccounted`` skips
    recorded ones), so this is a total order and a late-observed earlier session
    lands where it belongs rather than at the end."""
    return tuple(sorted((*kept, *added), key=lambda entry: entry.index))


def _base(
    index: int, summary: SessionSummary, now: dt.datetime
) -> SessionTaskSnapshot:
    """The self-describing entry for one bracket, before bytes or a miss reason."""
    return SessionTaskSnapshot(
        index=index,
        sid=summary.sid,
        role=summary.role,
        kind=summary.kind,
        status_before=summary.status_before,
        session_start_hms=summary.start_hms,
        captured_at=now.isoformat(),
        entry_lag_seconds=_entry_lag_seconds(summary.start_hms, now),
    )


def _missed(
    index: int, summary: SessionSummary, now: dt.datetime, detail: str
) -> SessionTaskSnapshot:
    """A session accounted for without bytes; ``detail`` says what the observer missed."""
    return replace(_base(index, summary, now), detail=detail)


def _capture(
    record: RunRecord,
    index: int,
    summary: SessionSummary,
    kept: tuple[SessionTaskSnapshot, ...],
    *,
    now: dt.datetime,
    read_task: Callable[[ManagedRepo, str], str | None],
) -> SessionTaskSnapshot | None:
    """The entry for the one bracket still at entry: its bytes, or why not.

    ``None`` is the retry case and the only outcome that records nothing — see
    :func:`capture_entries`.
    """
    entry = _base(index, summary, now)
    lag = entry.entry_lag_seconds
    if lag is not None and lag > ENTRY_LAG_MAX_SECONDS:
        return replace(
            entry,
            detail=(
                f"session start observed {lag}s late, past the "
                f"{ENTRY_LAG_MAX_SECONDS}s entry window"
            ),
        )
    if sum(1 for entry_kept in kept if entry_kept.present) >= MAX_CONTENT_SNAPSHOTS:
        return replace(entry, detail=_MISS_CAP)
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
    head = data[:TASK_FILE_MAX_BYTES]
    return replace(
        entry,
        present=True,
        # Lossy-tolerant decode, matching the log/doc readers: it absorbs the
        # partial multi-byte character a cut at a byte offset can leave.
        content=head.decode("utf-8", errors="replace"),
        size=len(data),
        truncated=len(head) < len(data),
    )


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
    stamp yields ``None``: unknown lag, which :func:`_capture` treats as no
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
