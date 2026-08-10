"""Bounded, read-only rollup of how a task's sessions *read* the ``.ai/`` memory.

The evidence pack already carries the **write** side of project memory
(:mod:`orch_hub.ai_snapshot`: the snapshot as it stands plus the patches the run
window produced). This module carries the **read** side, which nothing else
observes: which of the lazy docs a session's task declared actually reached its
context, and — ordered — every other ``.ai/`` doc it consulted along the way.

The measurement model
---------------------

**One ordered trace, not two mechanisms.** Every qualifying ``.ai/`` read is one
event in a single sequence, and ``declared_prefetch`` is an *attribute* of the
event rather than a phase it belongs to. The task's ``prefetch:`` list supplies
only the session-time expected set; the deterministic question asked of it is
whether a qualifying read exists **anywhere** in the session, never where it
happened.

A read qualifies exactly when::

    content successfully entered context
    AND before the close-out marker
    AND no write to the same path preceded it

Each clause is here because dropping it would credit a read that never informed
any work:

- *entered context* — a successful read result or caller injection. A failed or
  unanswered call put nothing in the context to be used.
- *before close-out* — reads after the close-out marker are absorption walking
  ``.ai/`` in order to update it (the write path), not knowledge used while
  working.
- *no earlier write* — a read of a doc this session wrote first is at most a
  read-back of its own text, not use of existing project memory. Read-then-write
  is the opposite case and does count: knowledge used, then the memory updated.

**When a read happened is evidence, not a verdict.** This module records each
read's order, the first-write position for the same path, and bounded counts of
the work the session had already done, then leaves "early enough", "relevant",
and "actually useful" to the semantic layer, which can weigh them against the
work the session was doing. Nothing here infers a phase, an entry/work boundary,
or the meaning of a shell command: the extractor reports order and lets the judge
judge.

Evidence source
---------------

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
summarization of logs the pack never ships. The transcript is also the one place
a session's *own* conversation content lives — prompts, human answers, tool
output — so shipping it would be both unbounded and a disclosure change.

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
transcript (a Codex-backed review session leaves none in this store), an
unparseable one, a backend whose events do not expose file reads, a session that
never re-read its own task file — each yields ``assessable=False`` with a
``detail``, and the downstream layers are expected to drop it from the quality
denominator and lower confidence instead.

Partial evidence is the subtler case, and it turns on one asymmetry: a read is
*positive* evidence, so a row this reader could not use can only ever hide a
read, never invent one. That gives two rules and no bookkeeping about where the
damage sits relative to anything (:func:`_verdict_refusal`):

- a declared doc reported **missing** cannot be claimed once any evidence was
  lost, because the lost part may hold exactly the read that would have found it;
- a declared doc reported **read** cannot be claimed when evidence was lost
  before that read, because the lost part may hold an earlier write to the same
  path or the close-out marker — the two order-dependent clauses of the success
  condition above.

Either way the observed facts stay in the rollup and only the verdict is
withheld.

Known limits, recorded rather than approximated
-----------------------------------------------

Writes are observed from structured edit tool calls only. A ``.ai/`` doc rewritten
through a shell redirection is therefore invisible as a write, because telling a
shell write from a mention of the same path requires interpreting a shell — the
approximation this reader is deliberately without. The protocol makes that
narrow: sessions do not edit ``.ai/`` mid-task, and close-out absorption is
already excluded by its marker.

Close-out is observable only as an invocation of the close-out skill
(:data:`CLOSEOUT_SKILL`). An absorption performed without it is not visible, and
reads after it would be credited; a session that never closes out is the benign
case, since then every read is genuinely pre-close-out.
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
session was never asked to load anything, so it is not a missing measurement
that should lower confidence — it is outside the measurement.
"""

CLOSEOUT_SKILL = "ai-sync-v2"
"""The task-completion closeout skill, and the one observable close-out marker.

Reads after it are absorption walking ``.ai/`` to update it — the write path —
rather than knowledge used while working, so the scan stops there. The retired
session-audit skill drew the same divider for the same reason.
"""

MAX_SESSIONS = 24
"""Cap on sessions rolled up per pack. A long task's runs can carry many
sessions; the rollup is per session, so the count is the unbounded dimension."""

MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
"""Byte cap on one transcript scan. Measured transcripts run ~1MB median and
~18MB at the tail, so a conforming session is read whole and a runaway one costs
a bounded read. A cut is recorded (``truncated``) and counts as lost evidence
like any other hole."""

MAX_LINE_BYTES = 1024 * 1024
"""Per-line cap inside a transcript. One JSONL row holds one message, and a
tool result can be arbitrarily large — a whole-file cap alone would still let a
single row be read into memory unbounded. An over-long row is skipped, not
parsed."""

MAX_READ_EVENTS = 64
"""Cap on the ordered read trace per session. Only the *first* qualifying read of
each path is recorded, so this bounds distinct docs rather than repeat reads. It
is comfortably above :data:`MAX_EXPECTED_DOCS` because the trace carries declared
and undeclared reads alike — a session with a full declared set has room left for
what it went and fetched besides."""

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

INSTRUCTION_DIRS = (AI_DIRNAME, ".ai-tasks", ".ai-protocol", ".claude")
"""Repo subtrees holding a session's *instructions* rather than its work material.

Memory, task files, the protocol contracts, and the deployed skills are what a
session reads to know what it is doing; everything else in the repo — source,
tests, docs — is the material the task acts on.

This decides only what the :class:`Activity` anchors count as work, so that "the
session had already read code / searched the tree" is legible next to a read.
It is **not** a phase boundary: no verdict in this module turns on it, and a
misplaced path costs an anchor count, not a score.
"""

INSTRUCTION_NAMES = ("CLAUDE.md",)
"""Filenames that are instructions wherever they sit, matched by basename so a
subtree's own ``CLAUDE.md`` (which the harness injects when a session reads
inside that subtree) is not counted as work material."""

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

_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
"""Tools that mutate a file. A mutation of a ``.ai/`` doc is also the write that
the write-before-read clause is about, so these are the one place this reader
learns that a session wrote its own memory before reading it back."""

_SEARCH_TOOLS = ("Grep", "Glob")
"""Tools that search the tree. Rediscovering knowledge from the code is the
behavior a retrieval is judged against, so a search is an activity anchor."""

_SHELL_TOOLS = ("Bash",)
"""Tools that run a shell command. Counted, never interpreted: a raw count is a
fact about the session, while any statement about what a command *did* would be
the shell approximation this reader is deliberately without."""


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
class Activity:
    """Content-free counts of the work a session had done at some point in it.

    The anchor each read is read against. Order alone cannot answer the question
    the judgment needs — *did this read come before the work it informs, or after
    that work had already derived the same knowledge?* — because event 40 means
    nothing without knowing what happened in the first 39. Once the transcript is
    discarded these counts are the only surviving answer, so they travel with each
    read; the work done *after* it is the session totals minus them.

    Counts, never content: no paths, no commands, no text. ``task_edits`` is
    separate from ``edits`` because writing the session's own task file is
    protocol bookkeeping (the claim, the plan, the session-log entry) rather than
    work on the repo. ``shell`` counts shell commands as commands — this reader
    does not claim to know what any of them did.
    """

    code_reads: int = 0
    searches: int = 0
    edits: int = 0
    task_edits: int = 0
    shell: int = 0

    def bump(self, field_name: str) -> "Activity":
        return replace(self, **{field_name: getattr(self, field_name) + 1})

    def to_dict(self) -> dict:
        return {
            "code_reads": self.code_reads,
            "searches": self.searches,
            "edits": self.edits,
            "task_edits": self.task_edits,
            "shell": self.shell,
        }


@dataclass(frozen=True)
class PrefetchDoc:
    """One declared prefetch doc and whether a qualifying read of it exists.

    ``read`` is the ruling's success condition applied to this path anywhere in
    the session: content entered context, before close-out, with no earlier write
    to it. Where in the session it happened is deliberately not encoded here —
    ``order``/``method`` describe the delivering event so the judgment layer can
    ask that question against the trace, and so a reader can tell caller-injected
    delivery from an explicit read without a second lookup.
    """

    path: str
    read: bool
    method: str | None = None
    order: int | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "read": self.read,
            "method": self.method,
            "order": self.order,
        }


@dataclass(frozen=True)
class MemoryRead:
    """One qualifying read of a lazy ``.ai/`` doc, with the timing evidence for it.

    ``order`` is the event's position in the session's own transcript, ``before``
    is the work the session had already done when it happened, and ``first_write``
    is where this session first wrote the same path (always *after* ``order``,
    since a read preceded by a write does not qualify at all). Together they are
    what lets a judge separate read-then-do from do-then-read from
    read-then-update without the transcript.

    ``declared_prefetch`` marks the read as delivery of a declared doc; it is
    ``None`` when the session-time expected set could not be recovered, since
    membership is then unknown rather than false.
    """

    path: str
    order: int
    actor: str
    method: str
    ts: str | None = None
    declared_prefetch: bool | None = None
    before: Activity = Activity()
    first_write: int | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "order": self.order,
            "actor": self.actor,
            "method": self.method,
            "ts": self.ts,
            "declared_prefetch": self.declared_prefetch,
            "before": self.before.to_dict(),
            "first_write": self.first_write,
        }


@dataclass(frozen=True)
class SessionReads:
    """One session's bounded read-behavior rollup.

    Three independent availability facts, kept separate on purpose:

    - ``available`` — a transcript was located and parsed at all.
    - ``assessable`` — *and* the session-time expected set was recovered, with no
      lost evidence able to change the per-doc verdict, so prefetch completeness
      is a measurement rather than a guess.
    - ``applicable`` — the session was one the prefetch instruction is delivered
      to *and* it declared a non-empty set. ``False`` for a role that never
      receives it (nothing to comply with); ``None`` when the declared set could
      not be recovered, so applicability itself is unknown.

    The split is what keeps a missing transcript from being read as bad behavior:
    a downstream score takes ``applicable and assessable`` as its denominator and
    lets the unassessable remainder lower *confidence* instead.

    ``coverage``/``complete`` are ``None`` unless the session is assessable and
    applicable; there is no default-to-zero, because a zero is a claim.

    ``trace`` is every qualifying lazy-``.ai/`` read in order, declared and
    undeclared alike. ``writeback_excluded`` names the docs whose only reads
    followed this session's own write to them: excluded from the trace by the
    success condition, but recorded rather than silently dropped, since "read
    back what it just wrote" is itself a fact about the session.
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
    docs: tuple[PrefetchDoc, ...] = ()
    coverage: float | None = None
    complete: bool | None = None
    trace: tuple[MemoryRead, ...] = ()
    trace_truncated: bool = False
    writeback_excluded: tuple[str, ...] = ()
    activity: Activity = Activity()
    closeout_seen: bool = False
    events_scanned: int = 0
    skipped_rows: int = 0
    truncated: bool = False
    detail: str | None = None

    @property
    def read(self) -> tuple[str, ...]:
        """Declared docs a qualifying read was found for."""
        return tuple(doc.path for doc in self.docs if doc.read)

    @property
    def missing(self) -> tuple[str, ...]:
        """Declared docs no qualifying read was found for."""
        return tuple(doc.path for doc in self.docs if not doc.read)

    @property
    def undeclared_reads(self) -> tuple[str, ...]:
        """Traced docs the task never declared, in read order.

        Not a defect and not a merit on its own: a sufficient prefetch set with no
        new need arising yields none, and count is not quality. It is the evidence
        a judge needs to ask whether a knowledge need arose and was met.
        """
        return tuple(read.path for read in self.trace if read.declared_prefetch is False)

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
            "read": list(self.read),
            "missing": list(self.missing),
            "docs": [doc.to_dict() for doc in self.docs],
            "coverage": self.coverage,
            "complete": self.complete,
            "trace": [read.to_dict() for read in self.trace],
            "trace_truncated": self.trace_truncated,
            "undeclared_reads": list(self.undeclared_reads),
            "writeback_excluded": list(self.writeback_excluded),
            "activity": self.activity.to_dict(),
            "closeout_seen": self.closeout_seen,
            "events_scanned": self.events_scanned,
            "skipped_rows": self.skipped_rows,
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
    max_reads: int = MAX_READ_EVENTS,
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
            max_reads=max_reads,
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
    max_reads: int,
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
        skipped_rows=scan.skipped,
        truncated=scan.truncated,
        closeout_seen=scan.closeout_seen,
        activity=scan.activity,
        writeback_excluded=tuple(scan.writeback[:max_reads]),
    )
    if scan.rows == 0:
        return _degraded(base, "session transcript exposed no readable events", warnings)

    expected, expected_detail = _expected_set(scan.task_content)
    # The trace and the prefetch verdict degrade independently, like the ``.ai/``
    # section's docs and patch stream: an unrecoverable expected set only costs
    # the trace its ``declared_prefetch`` attribution, since which docs were read
    # and in what order stands on its own evidence.
    trace, trace_truncated = _trace(scan.reads, expected=expected, limit=max_reads)
    base = _replace(base, trace=trace, trace_truncated=trace_truncated)
    if expected is None:
        return _degraded(
            base,
            expected_detail or "session-time prefetch set not recoverable",
            warnings,
        )

    docs = _prefetch_docs(expected, scan.reads)
    applicable = bool(expected) and _prefetch_role(ref.role)
    base = _replace(
        base,
        applicable=applicable,
        expected=expected,
        expected_source=SOURCE_TRANSCRIPT_TASK_READ,
        docs=docs,
        detail=expected_detail,
    )
    # Facts stay, the verdict does not: lost evidence could hold the very read
    # that would flip a missing doc, or the write that would disqualify one we
    # counted, so what was observed is reported as diagnosis while coverage stays
    # unclaimed.
    refusal = _verdict_refusal(scan, docs) if expected else None
    if refusal is not None:
        return _degraded(base, refusal, warnings)

    found = sum(1 for doc in docs if doc.read)
    return _replace(
        base,
        assessable=True,
        coverage=(found / len(expected)) if applicable else None,
        complete=(found == len(expected)) if applicable else None,
    )


def _prefetch_role(role: str | None) -> bool:
    """Whether a session in ``role`` is delivered the prefetch pre-load instruction."""
    return role in PREFETCH_ROLES


def _verdict_refusal(scan: "_Scan", docs: Sequence[PrefetchDoc]) -> str | None:
    """Why this session's prefetch verdict cannot be claimed, or ``None``.

    Reads are positive evidence, so a row this reader could not use can only hide
    a read. That makes the two questions asymmetric, and neither of them needs to
    know *where* the damage sits relative to anything but the read itself:

    - a **missing** doc rests on the absence of a read, which any lost evidence
      anywhere could be concealing;
    - a **found** doc rests on the two order-dependent clauses of the success
      condition, so only evidence lost *before* that read can undermine it: a
      lost row there could carry an earlier write to the same path, or the
      close-out marker that would have ended the scan before it.
    """
    holes = scan.skipped > 0 or scan.truncated
    if holes and any(not doc.read for doc in docs):
        return (
            f"evidence incomplete ({_gap_detail(scan)}); a declared doc reported"
            " missing could have been read in what was lost, so prefetch reading"
            " is not measured"
        )
    if scan.first_gap is not None and any(
        doc.read and doc.order is not None and doc.order >= scan.first_gap
        for doc in docs
    ):
        return (
            f"evidence incomplete ({_gap_detail(scan)}) before a declared doc's"
            " read; an earlier write to it or the close-out marker could be in"
            " what was lost, so prefetch reading is not measured"
        )
    return None


def _gap_detail(scan: "_Scan") -> str:
    """What was lost from this scan, in the reader's own terms."""
    parts = []
    if scan.skipped:
        parts.append(f"{scan.skipped} unreadable row(s)")
    if scan.truncated:
        parts.append("a truncated tail")
    return " and ".join(parts) or "incomplete evidence"


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
    be counted as a doc that could have been read; it is noted in the detail
    instead, because a reader comparing ``expected`` against the raw frontmatter
    would otherwise see an unexplained gap.

    An **eager** doc is dropped for the same reason, and it is the one that bites
    in practice. ``prefetch:`` is lazy-only by the taskfile schema, but tasks do
    declare eager docs, and the eager set reaches a session through the system
    prompt — which the transcript does not record. Such an entry is therefore
    unobservable by construction: keeping it in the denominator would score a
    session down for a doc the loading contract had already put in its context,
    which is precisely the measurement artifact this reader must not produce.

    A recovered-but-empty set is ``()``, not ``None``: a task that declared no
    lazy doc has nothing to comply with, which is a measurement, whereas an
    unrecovered set is the absence of one.
    """
    if content is None:
        return None, "session did not read its task file; expected set unrecoverable"
    entries = parse_prefetch(content)
    expected: list[str] = []
    malformed = 0
    eager = 0
    for entry in entries:
        rel = prefetch_doc_path(entry)
        if rel is None:
            malformed += 1
            continue
        if is_eager_doc(rel):
            eager += 1
            continue
        if rel not in expected:
            expected.append(rel)
    if len(expected) > MAX_EXPECTED_DOCS:
        return None, (
            f"declared prefetch set of {len(expected)} docs exceeds the"
            f" {MAX_EXPECTED_DOCS}-doc cap; not measured"
        )
    notes = []
    if malformed:
        notes.append(
            f"{malformed} malformed prefetch entr{'y' if malformed == 1 else 'ies'} ignored"
        )
    if eager:
        notes.append(
            f"{eager} eager doc(s) declared in prefetch ignored; the eager set"
            " reaches a session outside the transcript"
        )
    return tuple(expected), "; ".join(notes) or None


def _prefetch_docs(
    expected: Sequence[str], reads: Sequence["_Read"]
) -> tuple[PrefetchDoc, ...]:
    """Pair each declared doc with the qualifying read that delivered it, if any.

    Position in the session is deliberately not a criterion: the ruling asks
    whether a qualifying read exists *anywhere*, and a doc opened late is complete
    here and left for the semantic layer to call late from the trace. Only reads
    that survived the success condition reach this function at all, so a doc read
    back after this session wrote it, or read after close-out, is not paired.
    """
    first: dict[str, _Read] = {}
    for read in reads:
        first.setdefault(read.path, read)
    docs = []
    for path in expected:
        read = first.get(path)
        docs.append(
            PrefetchDoc(
                path=path,
                read=read is not None,
                method=read.method if read else None,
                order=read.order if read else None,
            )
        )
    return tuple(docs)


def _trace(
    reads: Sequence["_Read"], *, expected: Sequence[str] | None, limit: int
) -> tuple[tuple[MemoryRead, ...], bool]:
    """The ordered trace of qualifying lazy-``.ai/`` reads, declared and undeclared.

    One sequence, because the ruling made attribution an attribute rather than a
    partition: a declared doc and one the session went and found are the same kind
    of event, and only ``declared_prefetch`` tells them apart.

    The eager set is excluded: it is in every session's context by the loading
    contract, so reading it is not retrieval and not reading it is not a missed
    consultation.
    """
    declared = None if expected is None else set(expected)
    trace: list[MemoryRead] = []
    truncated = False
    for read in reads:
        if not _lazy_memory_doc(read.path):
            continue
        if len(trace) >= limit:
            truncated = True
            break
        trace.append(
            MemoryRead(
                path=read.path,
                order=read.order,
                actor=read.actor,
                method=read.method,
                ts=read.ts,
                declared_prefetch=None if declared is None else read.path in declared,
                before=read.before,
                first_write=read.first_write,
            )
        )
    return tuple(trace), truncated


def _lazy_memory_doc(rel: str) -> bool:
    """Whether a repo-relative path is a ``.ai/`` doc that has to be fetched."""
    return rel.startswith(f"{AI_DIRNAME}/") and not is_eager_doc(rel)


@dataclass(frozen=True)
class _Read:
    """One qualifying delivery of a ``.ai/`` doc's content into a session's context.

    ``order`` is the row the content landed in, ``before`` the work already done
    by then, and ``first_write`` where this session first wrote the same path —
    filled at the end of the scan, and necessarily after ``order``, since a read
    a write preceded never becomes a ``_Read`` at all.
    """

    path: str
    order: int
    actor: str
    method: str
    ts: str | None = None
    before: Activity = Activity()
    first_write: int | None = None


@dataclass
class _Scan:
    """Raw facts extracted from one transcript, before any judgment.

    ``pending`` is scanning scaffolding rather than an extracted fact: it holds a
    read tool call's repo-relative path only until its result arrives, so a call
    that never returned is dropped rather than credited.

    ``first_gap`` is the row position of the earliest hole — a malformed row, a
    row past the per-row cap. A position rather than a flag because that is what
    decides which credited reads a hole can undermine
    (:func:`_verdict_refusal`); a truncated tail is recorded separately since it
    is always after everything.
    """

    reads: list[_Read] = field(default_factory=list)
    writes: dict[str, int] = field(default_factory=dict)
    writeback: list[str] = field(default_factory=list)
    task_content: str | None = None
    closeout_seen: bool = False
    rows: int = 0
    truncated: bool = False
    skipped: int = 0
    first_gap: int | None = None
    activity: Activity = Activity()
    error: str | None = None
    pending: dict[str, str | None] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)

    def note_gap(self) -> None:
        """Record a row this reader could not use, at the position it would hold."""
        self.skipped += 1
        if self.first_gap is None:
            self.first_gap = self.rows

    def note_activity(self, kind: str) -> None:
        """Count one observed activity, as the anchor later reads are read against."""
        self.activity = self.activity.bump(kind)

    def note_write(self, path: str, order: int) -> None:
        """Record where this session first wrote a ``.ai/`` doc.

        Only the first matters: the success condition asks whether a write
        *preceded* the read, and if the first one did not, no later one did.
        """
        self.writes.setdefault(path, order)

    def add_read(
        self,
        *,
        path: str,
        order: int,
        actor: str,
        method: str,
        ts: str | None,
    ) -> None:
        """Keep the first qualifying read of each ``.ai/`` path; drop the rest.

        Only ``.ai/`` docs are retained: a read is evidence here only insofar as
        it is memory the session loaded, and every other read has already been
        folded into the activity anchors by the time it gets here. Repeat reads of
        the same doc add nothing the first does not and would grow with the
        session — which is also why the dedup is asked *first*: a doc read, then
        updated, then re-read already has its qualifying read, and calling that
        re-read a read-back would contradict the trace it is standing in.
        """
        if not path.startswith(f"{AI_DIRNAME}/"):
            return
        if path in self.seen:
            return
        if path in self.writes:
            # Nothing qualifying was read for this path, and this session wrote it
            # first: at most a read-back of its own text. Recorded rather than
            # dropped, since that is itself a fact about the session.
            if path not in self.writeback:
                self.writeback.append(path)
            return
        self.seen.add(path)
        self.reads.append(
            _Read(
                path=path,
                order=order,
                actor=actor,
                method=method,
                ts=ts,
                before=self.activity,
            )
        )

    def finish(self) -> None:
        """Attach each read's later same-path write, then drop scanning scaffolding."""
        self.reads = [
            replace(read, first_write=self.writes.get(read.path))
            for read in self.reads
        ]
        self.pending.clear()
        self.seen.clear()


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

    The scan stops at the close-out marker rather than filtering afterwards, so a
    long absorption phase costs nothing and no post-close-out read can leak into
    the measurement.

    Rows this reader cannot use — malformed, non-object, or past the per-row cap
    — are counted and positioned rather than silently dropped: what is missing
    from the evidence decides what may be concluded from it. A blank line is not
    such a row; it carries nothing either way.
    """
    scan = _Scan()
    try:
        with path.open("rb") as handle:
            for raw, complete in _iter_rows(
                handle, scan, max_bytes=max_bytes, max_line_bytes=max_line_bytes
            ):
                if not raw.strip():
                    continue
                if not complete:
                    scan.note_gap()
                    continue
                row = _parse_row(raw)
                if row is None:
                    scan.note_gap()
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
    scan.finish()
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

    Reads are credited from the **result** side, never the request: a read call
    that failed put nothing into context, and crediting the call would let a
    typo'd path count as a declared doc that was read. The pending map holds the
    requested path only until its result arrives, and an unanswered request (a
    session cut off mid-turn) is simply never credited.

    Mutations and searches are read from the **call** side instead, because that
    is where the session's intent is expressed and their results carry nothing
    this reader needs.
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
                scan.pending[block_id] = _repo_rel(args.get("file_path"), repo_root)
            elif name in _EDIT_TOOLS:
                _absorb_edit(
                    args,
                    scan,
                    order=order,
                    repo_root=repo_root,
                    task_paths=task_paths,
                )
            elif name in _SEARCH_TOOLS:
                _absorb_search(args, scan, repo_root=repo_root)
            elif name in _SHELL_TOOLS:
                scan.note_activity("shell")
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


def _absorb_edit(
    args: dict,
    scan: _Scan,
    *,
    order: int,
    repo_root: Path,
    task_paths: tuple[str, ...],
) -> None:
    """Count one file mutation, and record it when it writes project memory.

    Writing the session's own task file is protocol bookkeeping — the claim, the
    plan, the session-log entry — so it is counted apart from work on the repo.
    Writing a ``.ai/`` doc is additionally the write the success condition asks
    about: a later read of that path is this session reading back its own text.
    """
    rel = _repo_rel(args.get("file_path") or args.get("notebook_path"), repo_root)
    if rel is None:
        return
    scan.note_activity("task_edits" if rel in task_paths else "edits")
    if rel.startswith(f"{AI_DIRNAME}/"):
        scan.note_write(rel, order)


def _absorb_search(args: dict, scan: _Scan, *, repo_root: Path) -> None:
    """Count one tree search, unless its scope says it is not searching this code.

    An unscoped search runs from the session's cwd — the repo — so it is a search
    of the code. One scoped to the instruction paths is routed memory or task
    lookup, and one scoped elsewhere on the machine is not this repo's work at
    all; neither is work a later read could be late relative to.
    """
    path = args.get("path")
    if isinstance(path, str) and path:
        rel = _repo_rel(path, repo_root)
        if rel is None or _instruction_path(rel):
            return
    scan.note_activity("searches")


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
    ``filePath`` to a second block would credit a file that block never read. The
    body is only consulted when the request itself is unavailable (a transcript
    whose head was cut) and, for the task file, only when it agrees about which
    file it holds.
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
        rel = requested or result_rel
        if rel is None:
            continue
        if not _instruction_path(rel):
            scan.note_activity("code_reads")
        scan.add_read(
            path=rel,
            order=order,
            actor=actor,
            method=METHOD_READ_TOOL,
            ts=ts,
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
    if not _instruction_path(rel):
        scan.note_activity("code_reads")
    scan.add_read(
        path=rel,
        order=order,
        actor=actor,
        method=METHOD_INJECTED,
        ts=ts,
    )
    if rel in task_paths and scan.task_content is None and _complete_frontmatter(content):
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
    """The file body from a read result, only when it carries whole frontmatter.

    An offset/limited read can return a slice with no frontmatter at all, and a
    line-capped one can return a frontmatter block cut open in the middle — both
    would parse as "declared fewer docs than it did", a false measurement rather
    than a missing one, and the false direction here invents misses. Only a body
    that starts at line 1 *and* closes its frontmatter fence is accepted; a later
    full read in the same session then supplies the content instead.
    """
    start = result_file.get("startLine")
    if start is not None and start != 1:
        return None
    content = result_file.get("content")
    if not isinstance(content, str) or not _complete_frontmatter(content):
        return None
    return content


def _complete_frontmatter(content: str) -> bool:
    """Whether ``content`` opens a ``---`` frontmatter fence and closes it.

    The frontmatter parser tolerates a missing closing fence (it simply consumes
    what is there), which is right for reading a file but wrong for deciding
    whether a *body* is complete: a task file cut mid-frontmatter would silently
    yield a shorter ``prefetch:`` set, or none.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    return any(line.strip() == "---" for line in lines[1:])


def _instruction_path(rel: str) -> bool:
    """Whether a repo-relative path is instructions rather than work material.

    Matches the directories themselves as well as what is inside them, since a
    search is scoped by naming the directory.
    """
    if rel.rsplit("/", 1)[-1] in INSTRUCTION_NAMES:
        return True
    return any(rel == d or rel.startswith(f"{d}/") for d in INSTRUCTION_DIRS)


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
