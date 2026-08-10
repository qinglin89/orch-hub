"""Bounded, read-only rollup of how a task's sessions *read* the ``.ai/`` memory.

The evidence pack already carries the **write** side of project memory
(:mod:`orch_hub.ai_snapshot`: the snapshot as it stands plus the patches the run
window produced). This module carries the **read** side, which nothing else
observes: whether each session actually loaded the lazy docs its task declared,
and — ordered — which further lazy docs it went and fetched while working.

Two distinct mechanisms, kept distinct here because they are scored differently
downstream:

- **entry prefetch completeness** — the taskfile schema's ``prefetch:`` list is
  a *pre-load at start* instruction, and the orchestrator instantiates it in the
  dev entry checklist. Loading one of three declared docs is not compliance, so
  this records the whole expected set, what was loaded, and what was missed.
- **mid-flight retrieval** — an ordered trace of the lazy ``.ai/`` docs a
  session consulted *outside* that declared set. It is deliberately **not**
  scored here: zero mid-flight reads is the correct behavior for a session whose
  prefetch was sufficient, and more reads is not better. The trace exists so a
  semantic judge can ask whether a new knowledge need arose and whether memory
  was consulted before the knowledge was re-derived from code.

Evidence source and boundary
----------------------------

The only observation surface is the agent CLI's own conversation transcript.
Claude Code writes one JSONL file per session at
``<claude_home>/projects/<slugged-cwd>/<session-id>.jsonl``, and the run
manager's session snapshots (:mod:`orch_hub.sessions`) already carry each
session's ``sid``, so a transcript is reachable from persisted run state with no
new orchestrator contract — the same "read what is already written" ruling that
governs the session parser.

What crosses into the pack is a **rollup**, never the transcript: paths,
booleans, counts, and an ordered event list. That is the existing
bounded-evidence rule, and the precedent is ``has_diagnostic``-style
summarization of logs the pack never ships. The transcript is also the one
place a session's *own* conversation content lives — prompts, human answers,
tool output — so shipping it would be both unbounded and a disclosure change.

Session-time truth
------------------

``prefetch:`` is mutable: the schema explicitly invites a session to backfill it
with what it actually consulted. Comparing a 2026-05 session against the list a
2026-08 session left behind would therefore invent both false misses and false
credit. The expected set is consequently taken from the *session's own* first
full read of the task file inside its transcript (the frontmatter exactly as it
stood at entry), and when that is not recoverable the session is reported
**unassessable** rather than scored against today's list.

Degradation
-----------

Every gap is a first-class recorded state, never a penalty: a session with no
transcript (a Codex-backed review session has none in this store), an
unparseable one, a backend whose events do not expose file reads, a session that
never re-read its own task file — each yields ``assessable=False`` with a
``detail``, and the downstream layers are expected to drop it from the quality
denominator and lower confidence instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from .ai_snapshot import AI_DIRNAME, is_eager_doc, prefetch_doc_path
from .models import ManagedRepo
from .task_store import parse_prefetch

TRANSCRIPTS_SUBDIR = "projects"
"""Per-project transcript tree inside the Claude home (``Config.claude_home``);
:attr:`orch_hub.config.Config.claude_transcripts_dir` is the wired form."""

PREFETCH_ROLES = ("dev",)
"""Session roles the ``prefetch:`` pre-load instruction is actually delivered to.

Read off the deployed orchestrator's entry-prompt assembly, not assumed: the
prefetch checklist line is appended for ``role == "dev"`` only, while a review
session gets estimate/pending-review lines instead. A plan-gate turn composes on
top of the same dev prompt, so it carries the instruction too — hence the role,
not the kind, is the predicate.

This is what makes ``applicable`` decidable *without* a transcript: a review
session was never asked to pre-load anything, so it is not a missing measurement
that should lower confidence — it is outside the measurement.
"""

CLOSEOUT_SKILL = "ai-sync-v2"
"""The task-completion closeout skill. Reads after it are absorption (the write
path) walking ``.ai/`` to update it, not consultation, so the scan stops there —
the retired session-audit skill drew the same divider for the same reason."""

MAX_SESSIONS = 24
"""Cap on sessions rolled up per pack. A long task's runs can carry many
sessions; the rollup is per session, so the count is the unbounded dimension."""

MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
"""Byte cap on one transcript scan. Measured transcripts run ~1MB median and
~18MB at the tail, so a conforming session is read whole and a runaway one costs
a bounded read. A cut is recorded (``truncated``): prefetch facts live at the
head and survive it, while the mid-flight tail may not."""

MAX_LINE_BYTES = 1024 * 1024
"""Per-line cap inside a transcript. One JSONL row holds one message, and a
tool result can be arbitrarily large — a whole-file cap alone would still let a
single row be read into memory unbounded. An over-long row is skipped, not
parsed."""

MAX_MIDFLIGHT_EVENTS = 40
"""Cap on the ordered mid-flight trace per session. Only the *first* consult of
each path is recorded, so this bounds distinct docs, not repeat reads."""

MAX_EXPECTED_DOCS = 32
"""Cap on the declared prefetch set a session is measured against. A larger
declared set is not truncated into a coverage fraction — that would score a
session against a partial expectation — the session is reported unassessable."""

ACTOR_ROOT = "root"
"""A read performed by the session's own turn."""

ACTOR_SUBAGENT = "subagent"
"""A read performed by a delegated subagent. Consultation by delegation is still
consultation, so these count — the transcript splices them in as sidechain rows."""

METHOD_READ_TOOL = "read_tool"
"""Content reached the session through an explicit file-read tool call."""

METHOD_INJECTED = "injected"
"""Content was assembled into the context by the caller/harness, with no tool
call of the session's own. Credited identically: the operational definition of
"read" is that the content was in context, not that the session asked for it."""

SOURCE_TRANSCRIPT_TASK_READ = "transcript_task_read"
"""The expected prefetch set came from the session's own first full read of the
task file, i.e. the frontmatter exactly as it stood at that session's entry."""

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
"""Session ids reach us from persisted run state and are used as a filename, so
they are validated as a single benign segment before any path is built."""

_SLUG = re.compile(r"[^A-Za-z0-9]")
"""Claude Code's project-dir rule: every non-alphanumeric character in the
absolute cwd becomes ``-`` (``/Users/x/y.z`` -> ``-Users-x-y-z``)."""

_COMMAND_NAME = re.compile(r"<command-name>\s*/?([A-Za-z0-9._-]+)\s*</command-name>")
"""Slash-command invocations as the transcript records them; the other spelling
of a skill invocation beside a ``Skill`` tool call."""

_READ_TOOLS = ("Read",)
"""Tool names whose successful result puts a named file's content into context.
Deliberately narrow: a shell command that happens to mention a path is not
evidence that its content was read, and crediting one would turn ``ls .ai/``
into a consultation."""


@dataclass(frozen=True)
class SessionRef:
    """One session to roll up, as the evidence builder knows it.

    ``session_id`` is the backend conversation id the orchestrator logged and the
    run manager persisted; ``role``/``kind`` come from the same snapshot and
    decide applicability (:data:`PREFETCH_ROLES`) without needing a transcript.
    """

    session_id: str
    run_id: str | None = None
    role: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class PrefetchLoad:
    """One declared prefetch doc and whether the session actually loaded it.

    ``order``/``method`` are present only when ``loaded`` — they describe the
    event that delivered it, so a reader can tell caller-injected delivery from
    an explicit read without a second lookup.
    """

    path: str
    loaded: bool
    method: str | None = None
    order: int | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "loaded": self.loaded,
            "method": self.method,
            "order": self.order,
        }


@dataclass(frozen=True)
class MemoryRead:
    """One mid-flight consultation of a lazy ``.ai/`` doc.

    ``order`` is the event's position in the session's own transcript, which is
    the whole point of the trace: a doc opened before the code that would have
    re-derived it, and the same doc opened after a decision was already made, are
    different behaviors that a bare set of paths cannot distinguish.
    """

    path: str
    order: int
    actor: str
    method: str
    ts: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "order": self.order,
            "actor": self.actor,
            "method": self.method,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class SessionReads:
    """One session's bounded read-behavior rollup.

    Three independent availability facts, kept separate on purpose:

    - ``available`` — a transcript was located and parsed at all.
    - ``assessable`` — *and* the session-time expected set was recovered, so
      prefetch completeness is a measurement rather than a guess.
    - ``applicable`` — the session was one the prefetch instruction was
      delivered to *and* it declared a non-empty set. ``False`` for a role that
      never receives it (nothing to comply with); ``None`` when the declared set
      could not be recovered, so applicability itself is unknown.

    The split is what keeps a missing transcript from being read as bad behavior:
    a downstream score takes ``applicable and assessable`` as its denominator and
    lets the unassessable remainder lower *confidence* instead.

    ``coverage``/``complete`` are ``None`` unless the session is assessable and
    applicable; there is no default-to-zero, because a zero is a claim.
    """

    session_id: str
    run_id: str | None = None
    role: str | None = None
    kind: str | None = None
    available: bool = False
    assessable: bool = False
    applicable: bool | None = None
    expected: tuple[str, ...] = ()
    expected_source: str | None = None
    loads: tuple[PrefetchLoad, ...] = ()
    coverage: float | None = None
    complete: bool | None = None
    midflight: tuple[MemoryRead, ...] = ()
    midflight_truncated: bool = False
    closeout_seen: bool = False
    events_scanned: int = 0
    truncated: bool = False
    detail: str | None = None

    @property
    def loaded(self) -> tuple[str, ...]:
        return tuple(load.path for load in self.loads if load.loaded)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(load.path for load in self.loads if not load.loaded)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "role": self.role,
            "kind": self.kind,
            "available": self.available,
            "assessable": self.assessable,
            "applicable": self.applicable,
            "expected": list(self.expected),
            "expected_source": self.expected_source,
            "loaded": list(self.loaded),
            "missing": list(self.missing),
            "loads": [load.to_dict() for load in self.loads],
            "coverage": self.coverage,
            "complete": self.complete,
            "midflight": [read.to_dict() for read in self.midflight],
            "midflight_truncated": self.midflight_truncated,
            "closeout_seen": self.closeout_seen,
            "events_scanned": self.events_scanned,
            "truncated": self.truncated,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReadBehavior:
    """The evidence pack's read-behavior section: one rollup per known session.

    ``available`` follows :class:`orch_hub.ai_snapshot.AiSnapshot`'s rule — it
    reports what the section actually *carries*, not whether a read was
    attempted. It is True only when at least one session's transcript was
    located and parsed, so a pack built where no transcript store exists reads
    the same as one built for sessions that left none: no read evidence. An
    evaluator deciding whether it may judge read behavior needs that answer, not
    the reason.

    ``warnings`` are this section's degradation notes, aggregated into the pack's
    warning count like the per-run and ``.ai/`` ones. Only sessions that *could*
    have been measured warn — a review session with no transcript is expected,
    not degraded.
    """

    available: bool
    sessions: tuple[SessionReads, ...] = ()
    warnings: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def assessable_count(self) -> int:
        return sum(1 for session in self.sessions if session.assessable)

    @property
    def applicable_count(self) -> int:
        return sum(1 for session in self.sessions if session.applicable)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "sessions": [session.to_dict() for session in self.sessions],
            "warnings": list(self.warnings),
            "detail": self.detail,
        }


def project_slug(path: Path) -> str:
    """Claude Code's project-dir name for an absolute working directory."""
    return _SLUG.sub("-", str(path))


def locate_transcript(
    transcript_root: Path, repo: ManagedRepo, session_id: str
) -> Path | None:
    """Path of one session's transcript under ``transcript_root``, or ``None``.

    The orchestrator launches every backend session with ``cwd=<repo>``, so the
    project dir is derived from the repo path rather than searched for. A session
    stored under some other project slug — one resumed from a different cwd —
    reads as *not located* instead of being hunted across the store: widening the
    lookup would make the reader enumerate an operator's unrelated projects to
    answer a question about this repo.

    The id is validated as a benign single segment and the resolved file is
    required to stay under the resolved root, so a crafted id recorded in run
    state cannot steer the read out of the transcript store.
    """
    if not _SESSION_ID.match(session_id or ""):
        return None
    root = _resolve(transcript_root)
    if root is None:
        return None
    candidate = _resolve(
        transcript_root / project_slug(repo.path) / f"{session_id}.jsonl"
    )
    if candidate is None or not candidate.is_relative_to(root):
        return None
    return candidate if candidate.is_file() else None


def read_session_behavior(
    repo: ManagedRepo,
    *,
    task_id: str,
    sessions: Sequence[SessionRef],
    transcript_root: Path,
    max_sessions: int = MAX_SESSIONS,
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
    max_line_bytes: int = MAX_LINE_BYTES,
    max_midflight: int = MAX_MIDFLIGHT_EVENTS,
) -> ReadBehavior:
    """Roll up read behavior for ``sessions``, bounded and total.

    Never raises for transcript-store state: a missing store, a missing or
    unparseable transcript, an unreadable file, or a session that never re-read
    its own task file each degrade to a recorded state with a ``detail``.

    With no session ids at all — the pack's ordinary shape for a live run, whose
    session snapshot is written only at the live-to-ended transition — nothing is
    read and the section reports why. That early exit is also what keeps a pack
    built for a task with no sessions from touching the transcript store at all.
    """
    if not sessions:
        return ReadBehavior(
            available=False,
            detail="no persisted session ids to attribute read behavior to",
        )
    repo_root = _resolve(repo.path)
    if repo_root is None:
        return ReadBehavior(
            available=False,
            detail="managed repo path does not resolve",
            warnings=("read behavior: managed repo path does not resolve",),
        )
    task_paths = _task_paths(task_id)

    unique = _unique_sessions(sessions)
    warnings: list[str] = []
    rollups: list[SessionReads] = []
    for ref in unique[:max_sessions]:
        rollup = _session_rollup(
            ref,
            repo=repo,
            repo_root=repo_root,
            task_paths=task_paths,
            transcript_root=transcript_root,
            max_bytes=max_bytes,
            max_line_bytes=max_line_bytes,
            max_midflight=max_midflight,
            warnings=warnings,
        )
        rollups.append(rollup)
    dropped = max(0, len(unique) - max_sessions)
    if dropped:
        warnings.append(
            f"read behavior: {dropped} session(s) dropped past the"
            f" {max_sessions}-session cap"
        )
    available = any(rollup.available for rollup in rollups)
    return ReadBehavior(
        available=available,
        sessions=tuple(rollups),
        warnings=tuple(warnings),
        detail=None if available else "no session transcript could be read",
    )


def _unique_sessions(sessions: Sequence[SessionRef]) -> list[SessionRef]:
    """Sessions in given order, first mention of each id winning.

    A resumed or reattached run can carry the same conversation id in two run
    snapshots; the rollup is per *session*, so the second mention is the same
    subject, not a second measurement.
    """
    seen: set[str] = set()
    unique: list[SessionRef] = []
    for ref in sessions:
        if not ref.session_id or ref.session_id in seen:
            continue
        seen.add(ref.session_id)
        unique.append(ref)
    return unique


def _task_paths(task_id: str) -> tuple[str, ...]:
    """Repo-relative spellings of the target task file, active and archived.

    Both are accepted because a session that ran before close-out read the active
    path while a later reader of the same task may see the archived one; either
    read carries the same frontmatter.
    """
    return (
        f".ai-tasks/{task_id}.md",
        f".ai-tasks/archive/{task_id}.md",
    )


def _session_rollup(
    ref: SessionRef,
    *,
    repo: ManagedRepo,
    repo_root: Path,
    task_paths: tuple[str, ...],
    transcript_root: Path,
    max_bytes: int,
    max_line_bytes: int,
    max_midflight: int,
    warnings: list[str],
) -> SessionReads:
    """Locate, scan, and summarize one session's transcript."""
    base = SessionReads(
        session_id=ref.session_id,
        run_id=ref.run_id,
        role=ref.role,
        kind=ref.kind,
        applicable=None if _prefetch_role(ref.role) else False,
    )
    path = locate_transcript(transcript_root, repo, ref.session_id)
    if path is None:
        return _degraded(base, "no session transcript found", warnings)

    scan = _scan_transcript(
        path,
        repo_root=repo_root,
        task_paths=task_paths,
        max_bytes=max_bytes,
        max_line_bytes=max_line_bytes,
    )
    if scan.error is not None:
        return _degraded(base, scan.error, warnings)

    base = _replace(
        base,
        available=True,
        events_scanned=scan.rows,
        truncated=scan.truncated,
        closeout_seen=scan.closeout_seen,
    )
    if scan.rows == 0:
        return _degraded(base, "session transcript exposed no readable events", warnings)

    expected, expected_detail = _expected_set(scan.task_content)
    # The two mechanisms degrade independently, like the ``.ai/`` section's docs
    # and patch stream: an unrecoverable expected set makes prefetch completeness
    # unassessable, but the retrieval evidence the same scan collected is still
    # real and is not thrown away with it.
    midflight, midflight_truncated = _midflight_trace(
        scan.reads, expected=expected or (), limit=max_midflight
    )
    base = _replace(
        base, midflight=midflight, midflight_truncated=midflight_truncated
    )
    if expected is None:
        return _degraded(
            base,
            expected_detail or "session-time prefetch set not recoverable",
            warnings,
        )

    loads = _prefetch_loads(expected, scan.reads)
    applicable = bool(expected) and _prefetch_role(ref.role)
    loaded = sum(1 for load in loads if load.loaded)
    return _replace(
        base,
        assessable=True,
        applicable=applicable,
        expected=expected,
        expected_source=SOURCE_TRANSCRIPT_TASK_READ,
        loads=loads,
        coverage=(loaded / len(expected)) if applicable else None,
        complete=(loaded == len(expected)) if applicable else None,
        detail=expected_detail,
    )


def _prefetch_role(role: str | None) -> bool:
    """Whether a session in ``role`` is delivered the prefetch pre-load instruction."""
    return role in PREFETCH_ROLES


def _degraded(base: SessionReads, detail: str, warnings: list[str]) -> SessionReads:
    """Record an unassessable session, warning only when it was measurable.

    A role that never receives the prefetch instruction (a review session, whose
    Codex-backed conversation leaves nothing in this store) is *expected* to have
    no read evidence, so it is not degradation. A dev session with no usable
    transcript is a real gap in the measurement and says so.
    """
    if _prefetch_role(base.role):
        warnings.append(f"read behavior: session {base.session_id}: {detail}")
    return _replace(base, assessable=False, detail=detail)


def _replace(base: SessionReads, **changes: object) -> SessionReads:
    """``dataclasses.replace`` for the frozen rollup, typed loosely for brevity."""
    return replace(base, **changes)  # type: ignore[arg-type]


def _expected_set(content: str | None) -> tuple[tuple[str, ...] | None, str | None]:
    """The session-time expected prefetch set, or ``None`` when unrecoverable.

    ``content`` is the task file exactly as the session read it at entry. Entries
    are normalized through the one prefetch-path rule
    (:func:`orch_hub.ai_snapshot.prefetch_doc_path`), so a malformed entry cannot
    be counted as a doc that could have been loaded; it is noted in the detail
    instead, because a reader comparing ``expected`` against the raw frontmatter
    would otherwise see an unexplained gap.

    A recovered-but-empty set is ``()``, not ``None``: a task that declared no
    prefetch has nothing to comply with, which is a measurement, whereas an
    unrecovered set is the absence of one.
    """
    if content is None:
        return None, "session did not read its task file; expected set unrecoverable"
    entries = parse_prefetch(content)
    expected: list[str] = []
    malformed = 0
    for entry in entries:
        rel = prefetch_doc_path(entry)
        if rel is None:
            malformed += 1
            continue
        if rel not in expected:
            expected.append(rel)
    if len(expected) > MAX_EXPECTED_DOCS:
        return None, (
            f"declared prefetch set of {len(expected)} docs exceeds the"
            f" {MAX_EXPECTED_DOCS}-doc cap; not measured"
        )
    detail = (
        f"{malformed} malformed prefetch entr{'y' if malformed == 1 else 'ies'} ignored"
        if malformed
        else None
    )
    return tuple(expected), detail


def _prefetch_loads(
    expected: Sequence[str], reads: Sequence["_Read"]
) -> tuple[PrefetchLoad, ...]:
    """Pair each expected doc with the first event that put it in context.

    Order is not required: the checklist's pre-load step competes with the claim
    edit and other entry bookkeeping, so a prefetch doc read a few events in is
    still a pre-load. What is required is that the delivery happened before
    close-out, which the scan already enforces by stopping there.
    """
    first: dict[str, _Read] = {}
    for read in reads:
        first.setdefault(read.path, read)
    return tuple(
        PrefetchLoad(
            path=path,
            loaded=path in first,
            method=first[path].method if path in first else None,
            order=first[path].order if path in first else None,
        )
        for path in expected
    )


def _midflight_trace(
    reads: Sequence["_Read"], *, expected: Sequence[str], limit: int
) -> tuple[tuple[MemoryRead, ...], bool]:
    """The ordered first-consult trace of lazy ``.ai/`` docs outside ``expected``.

    Excludes the eager set (always in context, so reading it is not retrieval)
    and the declared prefetch slice (entry delivery, measured separately). When
    the expected set was unrecoverable the caller passes ``()``, so entry
    pre-loads then appear in the trace — an over-count that is visible from the
    same rollup's ``assessable=False``, and preferable to silently dropping real
    retrievals against a set we do not have.
    """
    declared = set(expected)
    seen: set[str] = set()
    trace: list[MemoryRead] = []
    truncated = False
    for read in reads:
        if not read.path.startswith(f"{AI_DIRNAME}/"):
            continue
        if read.path in declared or is_eager_doc(read.path) or read.path in seen:
            continue
        seen.add(read.path)
        if len(trace) >= limit:
            truncated = True
            continue
        trace.append(
            MemoryRead(
                path=read.path,
                order=read.order,
                actor=read.actor,
                method=read.method,
                ts=read.ts,
            )
        )
    return tuple(trace), truncated


@dataclass(frozen=True)
class _Read:
    """One observed delivery of a repo file's content into a session's context."""

    path: str
    order: int
    actor: str
    method: str
    ts: str | None = None


@dataclass
class _Scan:
    """Raw facts extracted from one transcript, before any judgment.

    ``pending`` is scanning scaffolding, not an extracted fact: it holds each
    read tool call's requested path only until its result arrives, so a call that
    never returned is dropped rather than credited.
    """

    reads: list[_Read] = field(default_factory=list)
    task_content: str | None = None
    closeout_seen: bool = False
    rows: int = 0
    truncated: bool = False
    long_rows: int = 0
    error: str | None = None
    pending: dict[str, object] = field(default_factory=dict)


def _scan_transcript(
    path: Path,
    *,
    repo_root: Path,
    task_paths: tuple[str, ...],
    max_bytes: int,
    max_line_bytes: int,
) -> _Scan:
    """Stream one transcript into a :class:`_Scan`, bounded at the read itself.

    Bounded at the producer, like every other evidence reader here: the file cap
    and the per-row cap are applied while reading, not to a buffer already in
    memory, and only the small extracted facts are retained — never a row, never
    the file.

    The scan stops at the close-out marker rather than filtering afterwards, so
    a long absorption phase costs nothing and no post-close-out read can leak
    into either measurement.
    """
    scan = _Scan()
    try:
        with path.open("rb") as handle:
            for raw, complete in _iter_rows(
                handle, scan, max_bytes=max_bytes, max_line_bytes=max_line_bytes
            ):
                if not complete:
                    scan.long_rows += 1
                    continue
                row = _parse_row(raw)
                if row is None:
                    continue
                order = scan.rows
                scan.rows += 1
                closeout = _absorb_row(
                    row,
                    scan,
                    order=order,
                    repo_root=repo_root,
                    task_paths=task_paths,
                )
                if closeout:
                    scan.closeout_seen = True
                    break
    except OSError as err:
        return _Scan(error=f"session transcript unreadable ({err})")
    scan.pending.clear()
    return scan


def _iter_rows(
    handle,
    scan: _Scan,
    *,
    max_bytes: int,
    max_line_bytes: int,
) -> Iterator[tuple[bytes, bool]]:
    """Yield ``(row bytes, complete)`` under both caps, recording a budget cut.

    ``readline`` is given an explicit size so a single pathological row cannot be
    pulled into memory whole; an over-long row's tail is then drained (still
    bounded) so the next yield resumes on a real row boundary rather than on the
    middle of one, which would otherwise turn one huge row into a stream of
    unparseable fragments.

    ``truncated`` is set only when content is actually left behind — a file that
    ends exactly on the budget was read whole, and reporting it as cut would put
    a false gap in the evidence.
    """
    remaining = max_bytes
    while remaining > 0:
        chunk = handle.readline(min(max_line_bytes, remaining) + 1)
        if not chunk:
            return
        remaining -= len(chunk)
        if chunk.endswith(b"\n"):
            yield chunk, True
            continue
        # No terminator: either the file's last row, the per-row cap, or the byte
        # budget. Drain to the next boundary to tell them apart.
        drained = 0
        while remaining > 0:
            more = handle.readline(min(max_line_bytes, remaining) + 1)
            if not more:
                break
            remaining -= len(more)
            drained += len(more)
            if more.endswith(b"\n"):
                break
        yield chunk, drained == 0
    scan.truncated = bool(handle.read(1))


def _parse_row(raw: bytes) -> dict | None:
    """One JSONL row as a dict, or ``None`` for anything else.

    A mid-write tail line, a non-object row, or a row this reader's version of
    the format does not understand is skipped rather than failing the scan — the
    same tolerance the orchestrator-log parser applies for the same reason.
    """
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _absorb_row(
    row: dict,
    scan: _Scan,
    *,
    order: int,
    repo_root: Path,
    task_paths: tuple[str, ...],
) -> bool:
    """Fold one row into ``scan``; return True when it is the close-out marker.

    Reads are credited from the **result** side, never the request: a ``Read``
    call that failed put nothing into context, and crediting the call would let a
    typo'd path count as a loaded prefetch doc. The pending map holds the
    requested path only until its result arrives, and an unanswered request (a
    session cut off mid-turn) is simply never credited.
    """
    kind = row.get("type")
    actor = ACTOR_SUBAGENT if row.get("isSidechain") else ACTOR_ROOT
    ts = row.get("timestamp") if isinstance(row.get("timestamp"), str) else None

    if kind == "assistant":
        for block in _content_blocks(row):
            if block.get("type") != "tool_use":
                continue
            name, block_id = block.get("name"), block.get("id")
            args = block.get("input")
            args = args if isinstance(args, dict) else {}
            if name == "Skill" and args.get("skill") == CLOSEOUT_SKILL:
                return True
            if name in _READ_TOOLS and isinstance(block_id, str):
                scan.pending[block_id] = args.get("file_path")
        return False

    if kind == "user":
        if _closeout_command(row):
            return True
        _absorb_tool_results(
            row,
            scan,
            order=order,
            actor=actor,
            ts=ts,
            repo_root=repo_root,
            task_paths=task_paths,
        )
        return False

    if kind == "attachment":
        _absorb_attachment(
            row,
            scan,
            order=order,
            actor=actor,
            ts=ts,
            repo_root=repo_root,
            task_paths=task_paths,
        )
    return False


def _absorb_tool_results(
    row: dict,
    scan: _Scan,
    *,
    order: int,
    actor: str,
    ts: str | None,
    repo_root: Path,
    task_paths: tuple[str, ...],
) -> None:
    """Credit the reads whose results landed in this row.

    The path comes from the call that was answered, not from the row-level result
    body: a row carries at most one result in practice, but attributing a body's
    ``filePath`` to a second block would credit a file that block never read.
    The body is only consulted when the request itself is unavailable (a
    transcript whose head was cut) and, for the task file, only when it agrees
    about which file it holds.
    """
    pending = scan.pending
    result = row.get("toolUseResult")
    result_file = result.get("file") if isinstance(result, dict) else None
    result_file = result_file if isinstance(result_file, dict) else {}
    result_rel = _repo_rel(result_file.get("filePath"), repo_root)
    for block in _content_blocks(row):
        if block.get("type") != "tool_result":
            continue
        requested = pending.pop(block.get("tool_use_id"), None)
        if block.get("is_error"):
            continue
        rel = _repo_rel(requested, repo_root) or result_rel
        if rel is None:
            continue
        scan.reads.append(
            _Read(path=rel, order=order, actor=actor, method=METHOD_READ_TOOL, ts=ts)
        )
        if rel in task_paths and rel == result_rel and scan.task_content is None:
            scan.task_content = _whole_file_content(result_file)


def _absorb_attachment(
    row: dict,
    scan: _Scan,
    *,
    order: int,
    actor: str,
    ts: str | None,
    repo_root: Path,
    task_paths: tuple[str, ...],
) -> None:
    """Credit caller-injected content that names the file it carries.

    Matched by *shape* — a path plus content — rather than by attachment type, so
    a harness that starts pre-loading a session's prefetch set is credited without
    this reader needing to learn a new name first. That is the operational
    definition the measurement rests on: content in context counts, whether or not
    the session asked for it.
    """
    attachment = row.get("attachment")
    if not isinstance(attachment, dict):
        return
    content = attachment.get("content")
    if not isinstance(content, str) or not content:
        return
    rel = _repo_rel(attachment.get("path"), repo_root)
    if rel is None:
        return
    scan.reads.append(
        _Read(path=rel, order=order, actor=actor, method=METHOD_INJECTED, ts=ts)
    )
    if rel in task_paths and scan.task_content is None and content.startswith("---"):
        scan.task_content = content


def _closeout_command(row: dict) -> bool:
    """Whether a user row invokes the close-out skill as a slash command."""
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    text = content if isinstance(content, str) else None
    if text is None:
        text = "".join(
            block.get("text", "")
            for block in _content_blocks(row)
            if block.get("type") == "text"
        )
    return any(name == CLOSEOUT_SKILL for name in _COMMAND_NAME.findall(text))


def _content_blocks(row: dict) -> Iterable[dict]:
    """The row's message content blocks, or nothing for a non-block message."""
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ()
    return [block for block in content if isinstance(block, dict)]


def _whole_file_content(result_file: dict) -> str | None:
    """The file body from a read result, only when the read covered the head.

    An offset/limited read can return a slice with no frontmatter at all, which
    would parse as "declared no prefetch" — a false measurement rather than a
    missing one. Only a read starting at line 1 is accepted; a later full read in
    the same session then supplies the content instead.
    """
    start = result_file.get("startLine")
    if start is not None and start != 1:
        return None
    content = result_file.get("content")
    return content if isinstance(content, str) and content else None


def _repo_rel(value: object, repo_root: Path) -> str | None:
    """A read path as a repo-relative POSIX path, or ``None`` when outside the repo.

    Paths are resolved before comparison so a repo reached through a symlinked
    parent is not mistaken for an outside path; a file elsewhere on the machine
    is simply not this repo's memory and is dropped.
    """
    if not isinstance(value, str) or not value:
        return None
    resolved = _resolve(Path(value))
    if resolved is None:
        return None
    try:
        return resolved.relative_to(repo_root).as_posix()
    except ValueError:
        return None


def _resolve(path: Path) -> Path | None:
    """Fully resolve ``path``, or ``None`` when the filesystem refuses."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None
