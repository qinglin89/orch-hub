"""Task-evaluation foundation: resolve a target task, aggregate its runs, and
lay out where its evidence artifacts are stored.

This module is the boundary for the optional task-evaluation subsystem. It is
deliberately kept **separate from run orchestration** (:mod:`orch_hub.runs`) so a
customer deployment can omit or disable it: :class:`EvaluationService` fails
closed when ``Config.evaluation_enabled`` is off, and the later evaluation
API/dashboard surfaces gate on the same flag.

Everything here is strictly **read-only** over existing state. Target task files,
run records, run history, session snapshots, and startup captures are only read,
never written; the only writes the subsystem ever performs are its own evidence
artifacts under the hub data dir (session-2), scoped outside every target repo:

.. code-block:: text

    <data_dir>/evaluations/<repo-slug>/<task-id>/<evaluation-id>/
        evidence.json    the bounded evidence pack

The subsystem is built from four composable primitives:

- **target resolution** — resolve a ``(repo, task_id)`` to its task file, whether
  active (``.ai-tasks/<id>.md``) or archived (``.ai-tasks/archive/<id>.md``),
  reusing the contained/​safe :mod:`orch_hub.task_store` readers.
- **related-run aggregation** — find every hub run for that exact
  ``(repo_path, task_id)`` from the persisted run records, read-only (via
  :func:`orch_hub.runs.load_records`, which never refreshes/mutates run state).
- **artifact path layout** — compute the contained, per-``(repo, task,
  evaluation)`` directory an evidence pack is written to, rejecting unsafe path
  segments so a crafted id cannot escape the evaluations tree. Touching what is
  stored there is a second, stronger primitive: a pathname is re-walked by every
  operation that uses it, so :func:`opened_evaluations_dir` walks the tree's own
  hops following no link and :func:`opened_artifact` /
  :func:`write_contained_artifact` open each artifact relative to that descriptor.
  The tree may be *reached* through a link (a relocated data dir); nothing inside
  it may be. Every consumer that turns a stored artifact into bytes — this
  module's own readers, the global report feed's exports, and the digests
  publication records — goes through them, and so does every path that creates a
  directory or writes an artifact *into* the tree: the hub must not be the thing
  that populates a tree its own reads would then refuse as damaged.
- **evidence pack build + persist** — assemble a bounded :class:`EvidencePack`
  from the target task, its related runs (each run's metadata-only history
  events, per-session context snapshot, and *bounded* log/``spawn.out`` tails),
  best-effort git-window commit metadata, and a bounded view of the repo's
  ``.ai/`` memory snapshot plus its patches over that same window
  (:mod:`orch_hub.ai_snapshot`), then persist it as ``evidence.json``. Every
  per-run and ``.ai/`` read is isolated so a corrupt artifact degrades to a
  warning rather than failing the pack, and the whole build stays read-only over
  target repo/run state — the only writes are the pack's own artifacts.

Raw *code* diffs stay out by design (:class:`EvidenceGit`): they are unbounded,
and the bounded-evidence rule keeps them and raw transcripts out of the pack.
``.ai/`` is the deliberate exception, and only because the unboundedness
objection does not apply to it — the memory protocol caps its size — so it is
admitted with its own explicit caps rather than on trust.
"""

from __future__ import annotations

import datetime as dt
import errno
import itertools
import json
import logging
import os
import re
import shutil
import stat
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from .ai_snapshot import (
    PREFETCH_SOURCE_SESSION_ENTRIES,
    PREFETCH_SOURCE_TASK_FILE,
    AiDiff,
    AiSnapshot,
    read_ai_snapshot,
)
from .config import Config
from .git_meta import read_commits_between
from .history import KIND_HISTORY_ERROR, read_events
from .logs import LogSnapshot, read_recent, read_spawn_out
from .models import (
    CommitInfo,
    EvaluationTarget,
    ManagedRepo,
    RelatedRun,
    RunEvent,
    RunRecord,
    SessionsSnapshot,
    StoredEvaluation,
)
from .protocol_provenance import (
    EffectiveProtocol,
    ProtocolSnapshot,
    effective_protocol,
    read_protocol,
)
from .runs import load_records
from .session_reads import ReadBehavior, SessionRef, read_session_behavior
from .sessions import read_snapshot
from .subprocess_utils import Runner, run_command
from .task_snapshots import (
    SESSION_KIND,
    SessionTaskSnapshot,
    TaskSnapshots,
    read_snapshots,
    utf8_head,
)
from .task_store import parse_prefetch, read_archived_task_content, read_task_content

_log = logging.getLogger(__name__)

EVALUATIONS_SUBDIR = "evaluations"
"""Top-level hub-data-dir subtree holding evaluation artifacts, parallel to the
run manager's ``runs/`` and the intake manager's ``intake/`` trees."""

EVIDENCE_NAME = "evidence.json"
"""The persisted evidence pack, written in each evaluation's artifact dir."""

EVIDENCE_SCHEMA_VERSION = "17"
"""Version of the serialized ``evidence.json`` shape, stamped into every pack.

Bumped when the shape gains or changes inputs the downstream evaluators reason
over, so a batch of reports spanning the change can be split into coherent
cohorts instead of being compared across a moved baseline. ``2`` added the
``ai`` section (:class:`orch_hub.ai_snapshot.AiSnapshot`); ``3`` added the
``reads`` section (:class:`orch_hub.session_reads.ReadBehavior`), which is the
first evidence about how sessions *read* memory rather than what they wrote to
it; ``4`` split that section's reads at an observable entry/work boundary, so a
declared doc opened after the code was already read stopped counting as a
pre-load; ``5`` through ``10`` successively moved that boundary and the shell
reading it rested on, each one changing which sessions are ``complete``,
``late``, or unassessable rather than only adding fields. ``11`` removes the
partition entirely, on a human ruling: there is no entry/work phase, no boundary
and no shell classification left in the section. A read now counts wherever it
happened, provided its content entered context before the close-out marker with
no earlier write to the same path, and every qualifying read — declared prefetch
or not — is one event in a single ordered trace carrying the timing evidence
(order, per-path first write, surrounding activity counts) for the semantic layer
to judge. A v10 session with a ``late`` doc is therefore ``complete`` in v11,
while a doc this session wrote before reading back stops counting at all, so the
two versions are not comparable in either direction. ``12`` settles that write
clause against the edit's *result* rather than its request: an edit that failed or
was never answered wrote nothing, so in v11 it could report a genuinely read
declared doc missing, and an in-flight edit now withholds the session's verdict
instead of excluding the read. The per-read ``first_write`` also moves from the
call row to the row that confirmed the write. ``13`` carries that unsettled
ordering on the read itself (``trace[].write_uncertain``): in v12 it reached only
the declared-set verdict, so an unresolved edit of an *undeclared* ``.ai/`` doc
left its trace event looking exactly like a clean read. A v12 event says nothing
about its own ordering; a v13 one does. ``14`` makes the section account for what
it could not carry: the sessions its own cap dropped (``dropped_sessions`` /
``dropped_candidates``), and — as a warning per session — evidence a *measured*
session lost to a hole or a cap. In v13 both were invisible to a consumer, so a
partly-read task and a wholly-read one presented identical coverage. ``15``
finishes that accounting for the sessions nothing was read from at all
(``unobserved_sessions``, and one warning for the whole class): v14 recorded only
the ones a prefetch obligation reached, so a task whose review sessions ran on a
backend that leaves no transcript reported the same read evidence as one observed
in every session — while the trace the semantic layer judges was short by all of
them. ``16`` carries the per-session **task file at session entry**
(:mod:`orch_hub.task_snapshots`, per run under ``runs[].task_snapshots``), which
is the first evidence in the pack that is not recoverable from the archived task
file at all: ``session-est`` ``total``, ``prefetch``, and the body's
Goal/Scope/Acceptance are each rewritten in flight, so every pack up to v15
described a task by the version of it that came out the far end. The pack's own
``.ai/`` prefetch slice moves onto that evidence in the same version, and the
read section's expected sets with it, so a v15 pack and a v16 one can disagree
about which docs a session was measured against. ``17`` carries the **deployed
protocol revision** each run ran under (``runs[].protocol``) and the one the
task's runs agree on (``protocol``, from
:func:`orch_hub.protocol_provenance.effective_protocol`) — the ai-native-development
commit whose protocol text the sessions were actually working under, which no
earlier pack could state at all. It is the first evidence here that a report is
*cohortable* by: up to v16 two reports written months and several protocol
revisions apart were indistinguishable to a consumer grouping them, so the
question "did this change to the protocol help?" had no answer in the artifacts.
A persisted pack with no ``schema_version`` key is version 1 and predates all of
it.

Only *additive* so far, deliberately: every consumer of a persisted pack reads
named keys and tolerates absent ones (:mod:`orch_hub.report_catalog`, the
evaluation detail route), so packs written before a bump stay readable rather
than needing migration.
"""

EVIDENCE_LOG_MAX_LINES = 200
"""Bounded tail size for a run's orchestrator log and ``spawn.out`` in the pack.
Evidence carries an excerpt plus ``size``/``truncated`` metadata, never the raw
transcript (which grows large); the semantic evaluator reads bounded text."""

EVIDENCE_COMMIT_LIMIT = 50
"""Hard cap on git-window commits in the pack, so a wide lifecycle window cannot
pull unbounded history (matches :data:`orch_hub.git_meta.MAX_COMMIT_LIMIT`)."""

EVIDENCE_TASK_FILE_MAX_BYTES = 64 * 1024
"""Per-entry cap on a session-entry task file carried in the pack, below the run
dir's own capture cap (:data:`orch_hub.task_snapshots.TASK_FILE_MAX_BYTES`): the
run dir holds one run's captures, while a pack holds every run's. The *head* is
kept, for the same reason the capture keeps it — the frontmatter leads the file
and carries the fields nothing else recovers. A head is bytes, not the file: it
is marked ``truncated`` and therefore not exact entry evidence, and it stops
being a declared-prefetch source too once the cut reaches the frontmatter (see
:attr:`orch_hub.task_snapshots.SessionTaskSnapshot.complete` and
``frontmatter_complete``). At measured task-file sizes (~16KB, largest observed
170KB) only the frontmatter case is hypothetical."""

EVIDENCE_TASK_ENTRIES_MAX_BYTES = 192 * 1024
"""Cap on the summed session-entry task files across the whole pack, the bound
that makes this section producer-bounded like every other one (``.ai/`` docs at
:data:`orch_hub.ai_snapshot.TOTAL_MAX_BYTES`, its patch stream, the log tails).
Sessions are filled oldest-first, so what an exhausted budget leaves without
content is normally the newest — whose entry file is the one closest to
``target.content``, which the pack already carries whole, so that end is the
cheapest to lose. At measured task-file sizes (~16KB) this holds a dozen
sessions."""

FIDELITY_EXACT = "exact"
"""Every session the pack knows of carries its entry task file verbatim."""

FIDELITY_PARTIAL = "partial"
"""Some sessions carry an exact entry task file and some do not."""

FIDELITY_RECONSTRUCTED = "reconstructed"
"""No session carries an exact entry task file, so anything said about how a
session entered the task is inferred from the archived file — which lost the
estimate total, the session-time ``prefetch:``, and the pre-adjustment
Goal/Scope/Acceptance. The three losses all skew the same way (a reconstruction
hands a replayed session more context and a better-calibrated plan than the
original had), which is why the signal exists: a downstream case built from a
reconstructed pack has to be weighted, not read as equivalent evidence."""

_EVAL_ID_TS = "%Y%m%dT%H%M%SZ"
"""Evaluation-id timestamp format (matches the run manager's run-id stamp); the
id is already scoped under ``<repo>/<task>/`` so it needs no repo/task prefix."""

SOURCE_ACTIVE = "active"
"""Target task file resolved from the active ``.ai-tasks/<id>.md``."""

SOURCE_ARCHIVED = "archived"
"""Target task file resolved from ``.ai-tasks/archive/<id>.md`` (post close-out)."""

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
"""Same repo/task slug rule the run manager uses for path components: collapse
any run of unsafe chars to ``-`` (a local copy, not an import of a private, so
the evaluation module stays independent of run-manager internals)."""

_TREE_BASE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK
"""Flags for opening the evaluations base. Links are followed here on purpose —
the base *is* the tree, and a hub may keep it on a relocated volume — so
``O_DIRECTORY`` is enough to make anything else standing there an ``ENOTDIR``."""

_TREE_HOP_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
"""Flags for opening one hop *inside* the tree: no link at that hop, and no
blocking open. Deliberately without ``O_DIRECTORY``, which on macOS makes a
refused link indistinguishable from a plain non-directory (it reports ``ENOTDIR``
where Linux reports ``ELOOP``) and would collapse tree damage into ordinary
absence. The descriptor is checked with ``fstat`` instead, which answers about the
object that was opened rather than about a name — the same reason nothing else
here trusts a pathname."""

_ARTIFACT_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
"""Flags for opening one stored artifact: no link at its own hop, and no blocking
open (see :func:`opened_artifact`)."""

_ARTIFACT_WRITE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK
)
"""Flags for opening one artifact's temp file for writing: the write-side mirror of
:data:`_ARTIFACT_FLAGS`. ``O_NOFOLLOW`` refuses a link planted at the temp name —
otherwise ``O_CREAT|O_TRUNC`` through one truncates and overwrites whatever it
points at — and ``O_NONBLOCK`` keeps a planted FIFO from parking a request thread
until a reader shows up. ``O_TRUNC`` rather than ``O_EXCL`` so a temp file left by
an interrupted write is reused, exactly as the previous ``write_text`` did."""

_ARTIFACT_WRITE_MODE = 0o666
"""Creation mode for a written artifact, matching what ``open(..., "w")`` requests
so the umask keeps deciding the result: containment is about *which object* is
written, not about tightening permissions the operator already chose."""


class EvaluationError(Exception):
    """Base class for task-evaluation errors."""


class EvaluationDisabledError(EvaluationError):
    """A task-evaluation operation was attempted while the subsystem is disabled.

    Raised by every :class:`EvaluationService` operation when
    ``Config.evaluation_enabled`` is off, so a deployment that disables
    evaluation gets a fail-closed backend boundary (the later API layer maps this
    to a removed/404 surface).
    """


class EvaluationDamagedTreeError(EvaluationError):
    """A hop inside the evaluations tree is a symbolic link, and was refused.

    The hub writes only real directories and regular files under
    ``<data_dir>/evaluations``, so a link standing at one of its hops is something
    the subsystem did not put there. Following it would serve or publish another
    file's bytes as this evaluation's artifact, so every path that turns the tree
    into bytes refuses it (:func:`opened_evaluations_dir`,
    :func:`opened_artifact`) and raises this instead.

    Deliberately distinct from an *absent* artifact, which reads as ``None``
    everywhere: absence is retention's designed end state, while this is damage an
    operator can repair, and a caller that could not tell them apart would report
    tampering as the normal end of an artifact's life.
    """


def task_evaluations_dir(data_dir: Path, *, repo_name: str, task_id: str) -> Path:
    """Return the contained parent dir holding every evaluation for one task.

    Layout is ``<data_dir>/evaluations/<repo-slug>/<task-id>/``, the directory
    :func:`evaluation_artifact_dir` places each ``<evaluation-id>/`` under and
    :meth:`EvaluationService.list_evaluations` enumerates. Segments come from
    :func:`tree_segments` (slugged repo, validated task id), so a crafted id
    fails closed with ``ValueError`` rather than building a path that could escape
    the tree. As a final defense the resolved target is asserted to stay under the
    resolved evaluations base. Pure path math: it creates nothing, and — unlike
    :func:`opened_evaluations_dir` — it *follows* links, so it is for the write and
    delete paths that name a directory, never for reading artifacts out of one.
    """
    base = data_dir / EVALUATIONS_SUBDIR
    target = base.joinpath(*tree_segments(repo_name=repo_name, task_id=task_id))
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"evaluations path escapes {base}: {target}")
    return target


def evaluation_artifact_dir(
    data_dir: Path,
    *,
    repo_name: str,
    task_id: str,
    evaluation_id: str,
) -> Path:
    """Return the contained directory for one evaluation's artifacts.

    Layout is ``<data_dir>/evaluations/<repo-slug>/<task-id>/<evaluation-id>/``,
    validated and contained exactly as :func:`task_evaluations_dir` is, with the
    same pure-path-math and link-following caveats: this is the dir a generation
    creates and writes into and a prune removes, while every *read* of what stands
    inside it goes through :func:`opened_evaluations_dir`.
    """
    base = data_dir / EVALUATIONS_SUBDIR
    target = base.joinpath(
        *tree_segments(
            repo_name=repo_name, task_id=task_id, evaluation_id=evaluation_id
        )
    )
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"evaluation artifact path escapes {base}: {target}")
    return target


def tree_segments(
    *,
    repo_name: str,
    task_id: str | None = None,
    evaluation_id: str | None = None,
) -> tuple[str, ...]:
    """The validated path components addressing one level of the evaluations tree.

    One rule for the two ways the tree is addressed — as a path
    (:func:`task_evaluations_dir`, :func:`evaluation_artifact_dir`) and as a
    descriptor walk (:func:`opened_evaluations_dir`) — so a segment one of them
    rejects can never be a segment the other accepts. The repo component is
    slugged (:data:`_SLUG`) and every component must be a single safe segment
    (:func:`_safe_segment`).

    Public because a caller holding an identity from elsewhere — the report
    catalog, resolving a stored catalog line — needs to ask *whether it addresses
    the tree at all* before deciding anything else about it, without opening
    anything.

    Levels cascade: naming a deeper one without its parent is a caller bug, not a
    tree state, so it raises rather than silently addressing the shallower dir.
    """
    if evaluation_id is not None and task_id is None:
        raise ValueError("evaluation id given without a task id")
    segments = [_safe_segment(repo_slug(repo_name), label="repo")]
    if task_id is not None:
        segments.append(_safe_segment(task_id, label="task id"))
    if evaluation_id is not None:
        segments.append(_safe_segment(evaluation_id, label="evaluation id"))
    return tuple(segments)


@contextmanager
def opened_evaluations_dir(
    data_dir: Path,
    *,
    repo_name: str | None = None,
    task_id: str | None = None,
    evaluation_id: str | None = None,
    create: bool = False,
) -> Iterator[int | None]:
    """Yield a descriptor for one level of the evaluations tree, following no link.

    The subsystem's containment primitive, and the thing every read of a stored
    artifact starts from — the per-task detail route, the global feed's bundle and
    raw-artifact reads, and publication's own completeness/hashing pass — as well
    as every write into the tree (``create=True``; see
    :meth:`EvaluationService._reserve_evaluation_dir` and
    :func:`write_contained_artifact`). It exists
    because a *pathname* cannot carry containment: whatever a check proves about
    ``<repo>/<task>/<eval>/evidence.json``, the read walks those names a second
    time and may get different objects, and a no-follow open of the file alone
    proves nothing when a *directory* above it became a link in between (the
    ``O_NOFOLLOW`` on the last component says nothing about the earlier ones). So
    the walk is the check: each hop below the base is opened ``O_NOFOLLOW``, and
    everything the caller does afterwards is relative to the descriptor it gets.

    The rule this establishes, stated once for every caller: **the evaluations
    tree may be reached through links; nothing inside it may be.** Its base is
    opened normally, so a hub whose data dir is a relocated volume or sits under
    macOS's own ``/var`` → ``/private/var`` reads and publishes exactly as one on a
    plain path does. Below the base the hub writes only real directories, so a link
    at ``<repo-slug>``, ``<task-id>``, or ``<evaluation-id>`` is not tree state to
    interpret — it is damage, and following it would pull an outside artifact set
    into the feed with the hub's own hashes on it.

    Yields ``None`` when the level is simply absent — nothing evaluated yet, or a
    prune took it — including when a non-directory stands at a hop, since that is
    no more the tree than an absent name is. Raises
    :class:`EvaluationDamagedTreeError` for a refused link (a loop included: it is
    never followed far enough to be one) and ``ValueError`` for an unsafe segment;
    every other ``OSError`` propagates as the IO fault it is.

    ``create=True`` makes each named level on the way down, so a generation reaches
    its dir through the same walk instead of a ``mkdir(parents=True)`` on a
    pathname the kernel would resolve through any link standing in it. Creation
    changes nothing about the rule — the hop is still opened ``O_NOFOLLOW`` after
    the ``mkdir``, so an existing link is refused as damage rather than followed
    and written through. It also removes the *absence* case (the level exists
    by the time the descriptor is yielded, or the attempt raised), so this never
    yields ``None`` under ``create=True``: an absent or non-directory hop is a
    failed write to report, not a state to interpret.

    The descriptors are closed on the way out, so a caller must do its reading
    inside the ``with``; nothing it obtains from them outlives it.
    """
    if repo_name is None:
        if task_id is not None or evaluation_id is not None:
            raise ValueError("task id given without a repo name")
        segments: tuple[str, ...] = ()
    else:
        segments = tree_segments(
            repo_name=repo_name, task_id=task_id, evaluation_id=evaluation_id
        )
    # The base is the tree, not something inside it: opened (and, for a write,
    # created) by path, links and all, which is what keeps a relocated data dir
    # readable and writable.
    base = data_dir / EVALUATIONS_SUBDIR
    if create:
        base.mkdir(parents=True, exist_ok=True)
    try:
        fds = [os.open(base, _TREE_BASE_FLAGS)]
    except (FileNotFoundError, NotADirectoryError):
        yield None
        return
    try:
        for segment in segments:
            if create:
                try:
                    os.mkdir(segment, dir_fd=fds[-1])
                except FileExistsError:
                    # Already there — which says nothing about *what* is there.
                    # The no-follow open below is what decides that, so a link
                    # standing at this hop is still refused rather than made the
                    # parent of a fresh evaluation.
                    pass
            try:
                fds.append(os.open(segment, _TREE_HOP_FLAGS, dir_fd=fds[-1]))
            except FileNotFoundError:
                if create:
                    raise
                yield None
                return
            except OSError as err:
                if err.errno == errno.ELOOP:
                    raise EvaluationDamagedTreeError(
                        f"{segment!r} is reached through a symbolic link and is"
                        " refused rather than followed"
                    ) from err
                raise
            if not stat.S_ISDIR(os.fstat(fds[-1]).st_mode):
                if create:
                    # Nothing to interpret on a write path: the tree cannot be
                    # built through a file or a FIFO, so this is an IO fault.
                    raise NotADirectoryError(
                        errno.ENOTDIR, os.strerror(errno.ENOTDIR), segment
                    )
                # A file, a FIFO, anything else: not a level of the tree, and as
                # absent as a name that is not there at all.
                yield None
                return
        yield fds[-1]
    finally:
        for fd in reversed(fds):
            os.close(fd)


@contextmanager
def opened_artifact(name: str, *, dir_fd: int) -> Iterator[BinaryIO | None]:
    """Open one artifact by name inside an already-contained dir descriptor.

    The file-hop half of the rule :func:`opened_evaluations_dir` states for the
    directory hops, and the last step of every path that turns a stored artifact
    into bytes — a detail-route read, a bundle export, a raw verification read, and
    the manifest digest publication records. ``O_NOFOLLOW`` refuses a link at the
    artifact's own hop, and ``fstat`` and the read then describe exactly the object
    that was opened, whatever the name comes to mean afterwards; opening relative
    to ``dir_fd`` is what keeps that guarantee from being undone one level up.
    ``O_NONBLOCK`` keeps the open from being turned into a hang — a FIFO planted
    where the artifact was would otherwise wait for a writer forever, on a request
    thread of the resident service (a regular file ignores the flag).

    Yields ``None`` when what stands there is not a regular file — a directory, a
    FIFO — since none of them is the published artifact. Raises
    :class:`EvaluationDamagedTreeError` for a refused link and ``OSError``
    (``FileNotFoundError`` for an absent artifact) otherwise.
    """
    try:
        fd = os.open(name, _ARTIFACT_FLAGS, dir_fd=dir_fd)
    except OSError as err:
        if err.errno == errno.ELOOP:
            raise EvaluationDamagedTreeError(
                f"artifact {name!r} is reached through a symbolic link and is"
                " refused rather than followed"
            ) from err
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            yield None
            return
        with open(fd, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(fd)


def read_contained_artifact(
    data_dir: Path,
    *,
    repo_name: str,
    task_id: str,
    evaluation_id: str,
    name: str,
) -> bytes | None:
    """One stored artifact's bytes, or ``None`` when it is not there to read.

    The whole contained read in one call, for callers that want the bytes and the
    ordinary absent/damaged split rather than the descriptors: absent dir, absent
    file, and a non-regular file all read as ``None``, while a link at any hop
    raises :class:`EvaluationDamagedTreeError`. ``name`` is validated as a single
    safe segment first, so a traversal attempt opens nothing at all.
    """
    artifact = _safe_segment(name, label="artifact name")
    with opened_evaluations_dir(
        data_dir,
        repo_name=repo_name,
        task_id=task_id,
        evaluation_id=evaluation_id,
    ) as dir_fd:
        if dir_fd is None:
            return None
        try:
            with opened_artifact(artifact, dir_fd=dir_fd) as handle:
                return handle.read() if handle is not None else None
        except FileNotFoundError:
            return None


def write_contained_artifact(
    data_dir: Path,
    *,
    repo_name: str,
    task_id: str,
    evaluation_id: str,
    name: str,
    text: str,
) -> None:
    """Write one artifact into an already-reserved evaluation dir, atomically.

    The write mirror of :func:`read_contained_artifact`, and the only way the
    subsystem's layers put bytes in the tree: the evidence pack here, the static
    report, and the semantic report plus its Markdown render. Same containment, by
    the same walk — ``name`` is validated as a single safe segment, the
    repo/task/evaluation hops are opened ``O_NOFOLLOW``, and both the temp file and
    the rename are relative to that descriptor, so no hop can be redirected by a
    link planted after a check.

    Atomicity is unchanged from the ``write_text`` + :func:`os.replace` it
    replaces (same-dir temp file, then a rename over the final name); ``renameat``
    replaces a link standing at the destination rather than writing through it, so
    the final hop needs no separate check. The temp file's own open is where the
    write side was previously weakest: ``O_CREAT|O_TRUNC`` follows a link, so a
    link planted at ``<name>.tmp`` was a truncate-and-overwrite of whatever it
    named (see :data:`_ARTIFACT_WRITE_FLAGS`).

    ``text`` is already-serialized content: each layer owns the shape of its own
    artifact (JSON here and in the two report layers, Markdown for ``report.md``),
    so this owns only how bytes reach the tree. Raises ``FileNotFoundError`` when
    the evaluation dir is not there — a generation reserves it first, so its
    absence means it was removed mid-generation, which fails the generation rather
    than silently writing nothing — and :class:`EvaluationDamagedTreeError` for a
    link at any hop, including the artifact's own.
    """
    artifact = _safe_segment(name, label="artifact name")
    payload = text.encode("utf-8")
    with opened_evaluations_dir(
        data_dir,
        repo_name=repo_name,
        task_id=task_id,
        evaluation_id=evaluation_id,
    ) as dir_fd:
        if dir_fd is None:
            raise FileNotFoundError(
                errno.ENOENT,
                "evaluation directory is absent",
                str(
                    evaluation_artifact_dir(
                        data_dir,
                        repo_name=repo_name,
                        task_id=task_id,
                        evaluation_id=evaluation_id,
                    )
                ),
            )
        tmp = f"{artifact}.tmp"
        try:
            fd = os.open(
                tmp, _ARTIFACT_WRITE_FLAGS, _ARTIFACT_WRITE_MODE, dir_fd=dir_fd
            )
        except OSError as err:
            if err.errno == errno.ELOOP:
                raise EvaluationDamagedTreeError(
                    f"artifact temp file {tmp!r} is reached through a symbolic link"
                    " and is refused rather than written through"
                ) from err
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                # A FIFO with a reader already attached, a device node: the open
                # succeeded, but renaming it over the artifact name would leave the
                # tree holding something no read path will ever serve.
                raise OSError(
                    errno.EINVAL, "artifact temp file is not a regular file", tmp
                )
            with open(fd, "wb", closefd=False) as handle:
                handle.write(payload)
        finally:
            os.close(fd)
        os.replace(tmp, artifact, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def contained_dir_names(dir_fd: int) -> list[str]:
    """Sorted names of the real subdirectories of an opened tree dir.

    The census counterpart of :func:`opened_evaluations_dir`: it says what the
    level holds, without opening any of it. ``follow_symlinks=False`` keeps it
    answering the same question the walk enforces, so a listing can never offer a
    name the read would then refuse. Sorted so every enumeration of the tree — the
    per-task listing, the feed's sweep — is deterministic.
    """
    return sorted(
        entry.name
        for entry in os.scandir(dir_fd)
        if entry.is_dir(follow_symlinks=False)
    )


def contained_file_names(dir_fd: int) -> list[str]:
    """Sorted names of the regular files in an opened tree dir (links excluded).

    What :meth:`EvaluationService.list_evaluations` reports as an evaluation's
    artifacts, under the census rule :func:`contained_dir_names` states: a linked
    name is not one of the hub's artifacts, so a listing must not advertise bytes
    that every read path refuses to serve.
    """
    return sorted(
        entry.name
        for entry in os.scandir(dir_fd)
        if entry.is_file(follow_symlinks=False)
    )


def repo_slug(value: str) -> str:
    """Make a repo name safe as a path component (matches ``runs._slug``).

    Idempotent: a value this function already produced passes through unchanged,
    so a caller holding a slug rather than a repo name — the report catalog
    resolving an entry's artifact dir from its stored ``repo_slug`` — can route
    through the same contained path helpers without a second slug rule.

    Public (unlike the run manager's copy) because the report catalog must apply
    exactly this rule to decide whether an artifact's recorded ``repo_name``
    agrees with the ``<repo-slug>/`` directory it was found under; a second copy
    of the rule there could drift and start rejecting coherent bundles.
    """
    return _SLUG.sub("-", value).strip("-") or "x"


def _safe_segment(value: str, *, label: str) -> str:
    """Return ``value`` iff it is a single safe path segment, else raise.

    Rejects the same shapes :mod:`orch_hub.task_store` refuses for raw task-file
    reads — empty, separators, null byte, or a ``.``/``..`` dot reference — but
    raises (fail closed) rather than returning ``None``, since this guards a
    write path about to create a directory.
    """
    if (
        not value
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or value in (".", "..")
    ):
        raise ValueError(f"unsafe {label} path segment: {value!r}")
    return value


@dataclass(frozen=True)
class EvidenceGit:
    """Best-effort git commit metadata for a task's run-lifecycle window.

    The builder queries the target repo for commits between the earliest run
    start and the latest run end (:func:`orch_hub.git_meta.read_commits_between`).
    ``available`` is False when git could not be read (missing git, not a work
    tree, a failed command) *or* when there were no run timestamps to form a
    window; ``detail`` then says which. Windowing is by git commit date over
    history reachable from HEAD, so ``commits`` is ambient repo history in that
    window — not a task-scoped filter — a deliberately best-effort signal.
    ``truncated`` marks a window whose commit list is a prefix — more commits
    exist than returned — whether the count hit :data:`EVIDENCE_COMMIT_LIMIT` or
    the reader's byte cap cut the tail
    (:data:`orch_hub.git_meta.COMMIT_LOG_MAX_BYTES`); a consumer weighing the
    evidence needs the incompleteness, not which bound produced it. Diffs are
    intentionally omitted: they are unbounded, and the bounded-evidence rule
    keeps raw diffs/transcripts out of the pack.
    """

    available: bool
    since: str | None = None
    until: str | None = None
    commits: tuple[CommitInfo, ...] = ()
    truncated: bool = False
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "since": self.since,
            "until": self.until,
            "commits": [commit.to_dict() for commit in self.commits],
            "truncated": self.truncated,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RunEvidence:
    """One related run's aggregated, read-only evidence for an evidence pack.

    Composes the run's persisted artifacts, each read best-effort and bounded:
    the :class:`~orch_hub.models.RunRecord` (which already carries terminal
    state — ``ended_ts``/``exit_code``/``error``), its metadata-only history
    events, its per-session context snapshot (``None`` when none was persisted —
    a live or pre-field run), and *bounded* tails of its orchestrator log and
    ``spawn.out`` startup diagnostic (excerpt + ``size`` + ``truncated``, never
    the raw transcript). ``warnings`` records per-run degradation — a corrupt
    ``events.jsonl``, or an unexpected read failure — so one broken run yields
    partial evidence instead of failing the whole pack. ``ended`` is the plain
    presence of ``ended_ts`` (persisted state, *not* a re-classification: the
    live/end-state verdict stays the run manager's).

    ``task_snapshots`` is the run's per-session task file *as each session
    entered it* (:class:`orch_hub.task_snapshots.TaskSnapshots`), carried beside
    ``sessions`` because that is what it joins to: one entry per session bracket,
    keyed by the same ``index``/``sid``. ``None`` means the run wrote no such
    artifact — it predates the capture, or its accounting never closed — which
    downstream reads as no exact entry state, exactly like a recorded miss.

    ``protocol`` is the deployed protocol revision this run ran under, as its
    launch capture and end check recorded it
    (:class:`orch_hub.protocol_provenance.ProtocolSnapshot`). ``None`` is again a
    run that wrote no artifact — one launched before the capture existed — and is
    deliberately not the same as a capture that found no usable lock: the pack
    carries the distinction so a consumer cannot read a hub that never looked as a
    repo that had nothing to look at. It is read from the run dir rather than
    re-derived from the repo, which is the whole point of capturing it: by
    evaluation time the tree usually holds a later deployment.
    """

    record: RunRecord
    events: tuple[RunEvent, ...] = ()
    sessions: SessionsSnapshot | None = None
    log: LogSnapshot | None = None
    spawn_out: LogSnapshot | None = None
    task_snapshots: TaskSnapshots | None = None
    protocol: ProtocolSnapshot | None = None
    warnings: tuple[str, ...] = ()

    @property
    def ended(self) -> bool:
        return self.record.ended_ts is not None

    def to_dict(self) -> dict:
        return {
            "run": self.record.to_dict(),
            "ended": self.ended,
            "events": [event.to_dict() for event in self.events],
            "sessions": self.sessions.to_dict() if self.sessions is not None else None,
            "log": self.log.to_dict() if self.log is not None else None,
            "spawn_out": (
                self.spawn_out.to_dict() if self.spawn_out is not None else None
            ),
            "task_snapshots": (
                self.task_snapshots.to_dict()
                if self.task_snapshots is not None
                else None
            ),
            "protocol": (
                self.protocol.to_dict() if self.protocol is not None else None
            ),
            "warnings": list(self.warnings),
        }

    @property
    def task_entries(self) -> tuple[SessionTaskSnapshot, ...]:
        """This run's session-entry snapshots, or ``()`` when it wrote none."""
        return () if self.task_snapshots is None else self.task_snapshots.snapshots

    @property
    def protocol_sessions(self) -> int:
        """How many task-claiming sessions this run is known to have had.

        The denominator the entry-fidelity rollup counts against, taken from the
        persisted session snapshot when there is one and from the entry
        accounting otherwise. A live run has the second and not the first (its
        ``sessions.json`` is written at the live-to-ended transition, while
        entries are recorded during the run), so neither source alone answers for
        every run; the larger is the honest count, since each is complete for the
        sessions it saw. Only ``session`` brackets count — a ``plan-gate`` never
        claims the task and is deliberately outside this accounting.
        """
        summaries = 0 if self.sessions is None else sum(
            1 for summary in self.sessions.sessions if summary.kind == SESSION_KIND
        )
        return max(summaries, len(self.task_entries))


@dataclass(frozen=True)
class EvidencePack:
    """A bounded, read-only evidence pack for one task, serialized to ``evidence.json``.

    The whole subject of static/semantic evaluation, assembled once and self-
    contained: ``target`` is the resolved task file (active/archived/absent),
    ``runs`` are its related runs' aggregated evidence (oldest-first), ``git``
    is the best-effort commit window, and ``ai`` is the bounded view of the
    repo's ``.ai/`` memory snapshot plus the ``.ai/``-scoped patches for that same
    window (:class:`orch_hub.ai_snapshot.AiSnapshot`) — the evidence that lets a
    downstream evaluator judge knowledge maintenance from what the task actually
    left behind rather than from commit subjects. ``reads`` is its counterpart on
    the read side (:class:`orch_hub.session_reads.ReadBehavior`): a per-session
    rollup of which declared prefetch docs were read and one ordered trace of
    every qualifying ``.ai/`` read, derived from each session's own transcript and
    carrying none of it.
    ``warnings`` collects
    pack-level degradation (per-run warnings live on each :class:`RunEvidence`,
    ``.ai/`` warnings on the snapshot). ``summary`` (computed in
    :meth:`to_dict`) is the validating metadata — run/ended counts, whether task
    content was found, git/``.ai/``/read availability, the session-entry
    fidelity rollup, and the total warning count
    across every section — enough to sanity-check a pack without walking it.
    ``evaluation_dir`` is the persisted location, set only by
    :meth:`EvaluationService.generate_evidence`; it is internal filesystem detail
    and is excluded from :meth:`to_dict`, matching ``RunRecord``/``LogSnapshot``.
    """

    evaluation_id: str
    generated_ts: str | None
    target: EvaluationTarget
    runs: tuple[RunEvidence, ...] = ()
    git: EvidenceGit | None = None
    ai: AiSnapshot | None = None
    reads: ReadBehavior | None = None
    warnings: tuple[str, ...] = ()
    evaluation_dir: Path | None = None

    def to_dict(self) -> dict:
        git = self.git if self.git is not None else EvidenceGit(available=False)
        ai = self.ai if self.ai is not None else AiSnapshot(present=False)
        reads = self.reads if self.reads is not None else ReadBehavior(available=False)
        protocol = self.protocol
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "evaluation_id": self.evaluation_id,
            "generated_ts": self.generated_ts,
            "target": self.target.to_dict(),
            "runs": [run.to_dict() for run in self.runs],
            "protocol": protocol.to_dict(),
            "git": git.to_dict(),
            "ai": ai.to_dict(),
            "reads": reads.to_dict(),
            "warnings": list(self.warnings),
            "summary": self._summary(git, ai, reads, protocol),
        }

    @property
    def protocol(self) -> EffectiveProtocol:
        """The deployed protocol revision this task's runs are publishable under.

        Computed from the runs rather than stored beside them
        (:func:`orch_hub.protocol_provenance.effective_protocol`), for the same
        reason ``entry_fidelity`` is: an aggregate that can be set independently of
        the parts it summarizes can contradict them, and this one decides what a
        published report claims about protocol cohort membership. A pack with no
        runs, with a run that recorded no verified revision, or whose runs verified
        different revisions carries the reason instead of a revision.
        """
        return effective_protocol([run.protocol for run in self.runs])

    @property
    def entry_counts(self) -> tuple[int, int]:
        """``(sessions this pack knows of, those carrying their entry file whole)``.

        Counted over what the pack actually carries: an entry whose content the
        section budget dropped is not exact evidence *here*, whatever the run dir
        still holds, because the pack is the published artifact a downstream case
        is built from.

        Exactness needs the *whole* file
        (:attr:`orch_hub.task_snapshots.SessionTaskSnapshot.complete`), not merely
        bytes. A kept head is missing an unknown amount of what the session
        entered on — possibly the acceptance criteria, possibly the frontmatter
        itself — so counting it as exact would tell a downstream case builder that
        a replay can be reproduced from a file it cannot be reproduced from. The
        head is still carried, and its ``truncated`` flag is what a consumer that
        wants partial bytes reads.
        """
        total = 0
        exact = 0
        for run in self.runs:
            total += run.protocol_sessions
            exact += sum(1 for entry in run.task_entries if entry.complete)
        return total, exact

    @property
    def entry_fidelity(self) -> str:
        """Whether this pack's sessions carry exact entry task files.

        The signal a downstream case format weights by
        (:data:`FIDELITY_EXACT` / :data:`FIDELITY_PARTIAL` /
        :data:`FIDELITY_RECONSTRUCTED`). Three values rather than the two the
        mixed case forces a choice between: a task with three exact sessions and
        one missed is neither, and collapsing it either way misprices it —
        strictly, by discarding three exact cases; loosely, by presenting a
        reconstructed one as exact. ``summary`` carries the two counts beside it,
        so the ratio is readable without walking the runs.

        A pack with no sessions at all is :data:`FIDELITY_RECONSTRUCTED`, which is
        the honest answer to the question the signal asks — nothing here can be
        replayed exactly — and its zero counts say why.
        """
        total, exact = self.entry_counts
        if total and exact == total:
            return FIDELITY_EXACT
        return FIDELITY_PARTIAL if exact else FIDELITY_RECONSTRUCTED

    def _summary(
        self,
        git: EvidenceGit,
        ai: AiSnapshot,
        reads: ReadBehavior,
        protocol: EffectiveProtocol,
    ) -> dict:
        entry_total, entry_exact = self.entry_counts
        return {
            "run_count": len(self.runs),
            "runs_ended": sum(1 for run in self.runs if run.ended),
            "protocol_available": protocol.available,
            "protocol_verified_runs": protocol.verified_count,
            "has_task_content": self.target.present,
            "target_source": self.target.source,
            "task_entry_fidelity": self.entry_fidelity,
            "task_entry_sessions": entry_total,
            "task_entries_exact": entry_exact,
            "git_available": git.available,
            "ai_snapshot_present": ai.present,
            "ai_doc_count": sum(1 for doc in ai.docs if doc.present),
            "ai_snippets_available": ai.snippets_available,
            "ai_diffs_available": ai.diffs_available,
            "reads_available": reads.available,
            "reads_session_count": len(reads.sessions),
            "reads_applicable_count": reads.applicable_count,
            "reads_assessable_count": reads.assessable_count,
            "warning_count": (
                len(self.warnings)
                + sum(len(run.warnings) for run in self.runs)
                + len(ai.warnings)
                + len(reads.warnings)
            ),
        }


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(value: str | None) -> dt.datetime | None:
    """Parse an ISO run timestamp to aware UTC, or ``None`` when absent/malformed.

    A naive timestamp is assumed UTC so every parsed value is aware and thus
    mutually comparable for the git-window min/max (mirrors
    :func:`orch_hub.logs._floor_started`, without the second flooring)."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _git_window(
    related: Sequence[RelatedRun],
) -> tuple[str | None, str | None, str | None]:
    """The task's run-lifecycle window as ``(since, until, unavailable-detail)``.

    Computed once and shared by every git-backed evidence section (the commit
    list and the ``.ai/`` patch stream), so they cannot drift into describing
    different spans.

    The window spans the earliest run ``started_ts`` to the latest run
    ``ended_ts``, and stays open-ended (``until=None``) whenever any run is still
    live (no parseable ``ended_ts``) — a live run's activity runs to now, so the
    window must not close at an earlier run's end and drop the commits made
    during it. Only an all-ended set closes, at the latest end. With no parseable
    run start there is no window at all: ``since`` is ``None`` and the third
    element carries the reason for the sections to report.
    """
    starts = [ts for ts in (_parse_ts(r.record.started_ts) for r in related) if ts]
    if not starts:
        return None, None, "no run start timestamps to form a git window"
    run_ends = [_parse_ts(r.record.ended_ts) for r in related]
    any_live = any(end is None for end in run_ends)
    until = None if any_live else max(run_ends).isoformat()
    return min(starts).isoformat(), until, None


def _session_refs(runs: Sequence[RunEvidence]) -> list[SessionRef]:
    """Every identified session across a pack's runs, oldest run first.

    Sessions with no reconciled ``sid`` are skipped: the read rollup is keyed by
    conversation id, and a session the orchestrator never learned the id of has
    no transcript to attribute. Ordering follows the runs, which are already
    oldest-first, so a task's sessions appear in the order they happened.

    Each ref carries its session-entry task file when the pack has one, so the
    read reader can take the session's declared prefetch set from the bytes the
    session actually entered on rather than from its own first task-file read —
    a source that needs no transcript and no re-read to survive.
    """
    return [
        SessionRef(
            session_id=summary.sid,
            run_id=run.record.run_id,
            role=summary.role,
            kind=summary.kind,
            entry_task_content=_entry_content(run, summary.sid),
        )
        for run in runs
        if run.sessions is not None
        for summary in run.sessions.sessions
        if summary.sid
    ]


def _entry_content(run: RunEvidence, sid: str) -> str | None:
    """One session's captured entry task file from ``run``, if the pack carries it.

    Gated on the frontmatter being whole rather than on bytes existing: the only
    thing read off this content is the session's declared ``prefetch:`` set
    (:func:`orch_hub.session_reads._expected_set`), and a head cut inside the
    frontmatter yields a *wrong* set rather than no set — a dropped ``prefetch:``
    line reads as a session that declared nothing, which is a claim, and one the
    read rollup would carry into its own applicability and confidence.
    """
    if run.task_snapshots is None:
        return None
    entry = run.task_snapshots.for_sid(sid)
    return entry.content if entry is not None and entry.frontmatter_complete else None


def _bounded_task_entries(
    runs: Sequence[RunEvidence],
) -> tuple[tuple[RunEvidence, ...], tuple[str, ...]]:
    """Fit every run's session-entry task files into the pack's own byte budget.

    The capture bounds one *run* (:mod:`orch_hub.task_snapshots`); a pack holds
    every run of a task, so it re-bounds at its own producer like every other
    section. Entries are filled oldest run first, oldest bracket first, up to
    :data:`EVIDENCE_TASK_ENTRIES_MAX_BYTES`, each capped at
    :data:`EVIDENCE_TASK_FILE_MAX_BYTES`.

    An entry the remaining budget cannot hold *whole* is carried without content
    rather than as a stub head — a partial head would be indistinguishable from a
    genuinely short task file — and says so in its ``detail``, keeping the pack's
    own limit visibly distinct from a capture that was never made. A later,
    smaller entry that still fits is then carried: skipping it too would drop
    evidence the budget can afford, so the carried set is the ones that fit in
    order, not a prefix. Recorded misses cost nothing — they carry no bytes to
    begin with.

    A per-entry cut keeps the head (:data:`EVIDENCE_TASK_FILE_MAX_BYTES`) and is
    taken on a character boundary (:func:`orch_hub.task_snapshots.utf8_head`), so
    the carried text costs exactly the bytes debited from the budget rather than
    growing back past the cap through a replaced partial character. Such a head is
    marked ``truncated``, which is what keeps it out of the exact count and, when
    the cut reached into the frontmatter, out of the declared-prefetch sources
    too; that case is warned about, since it silently narrows what the pack's
    derived sections could be built from.

    Returns the rebuilt runs and any pack-level warnings, one line per limit event
    rather than one per affected entry.
    """
    budget = EVIDENCE_TASK_ENTRIES_MAX_BYTES
    bounded: list[RunEvidence] = []
    dropped = 0
    cut_frontmatter = 0
    for run in runs:
        if run.task_snapshots is None:
            bounded.append(run)
            continue
        entries: list[SessionTaskSnapshot] = []
        for entry in run.task_snapshots.snapshots:
            if not entry.present or entry.content is None:
                entries.append(entry)
                continue
            head, head_bytes = utf8_head(entry.content, EVIDENCE_TASK_FILE_MAX_BYTES)
            if head_bytes > budget:
                dropped += 1
                entries.append(
                    replace(
                        entry,
                        present=False,
                        content=None,
                        truncated=False,
                        detail=(
                            "carried without content: the pack's"
                            f" {EVIDENCE_TASK_ENTRIES_MAX_BYTES // 1024}KB"
                            " session-entry budget was exhausted by earlier"
                            " sessions"
                        ),
                    )
                )
                continue
            budget -= head_bytes
            kept = replace(
                entry,
                content=head,
                truncated=entry.truncated or head_bytes < entry.size,
            )
            if not kept.frontmatter_complete:
                cut_frontmatter += 1
                kept = replace(
                    kept,
                    detail=(
                        "kept head stops inside the frontmatter, so this entry is"
                        " not a session-time prefetch source"
                    ),
                )
            entries.append(kept)
        bounded.append(
            replace(run, task_snapshots=TaskSnapshots(snapshots=tuple(entries)))
        )
    warnings: list[str] = []
    if dropped:
        warnings.append(
            f"task entries: {dropped} session-entry task file(s) carried without"
            f" content past the pack's"
            f" {EVIDENCE_TASK_ENTRIES_MAX_BYTES // 1024}KB budget"
        )
    if cut_frontmatter:
        warnings.append(
            f"task entries: {cut_frontmatter} session-entry task file(s) cut"
            f" inside the frontmatter by the pack's"
            f" {EVIDENCE_TASK_FILE_MAX_BYTES // 1024}KB per-entry cap; their"
            " declared prefetch sets are unrecoverable from this pack"
        )
    return tuple(bounded), tuple(warnings)


def _prefetch_slice(
    runs: Sequence[RunEvidence], target: EvaluationTarget
) -> tuple[tuple[str, ...], str]:
    """The lazy ``.ai/`` docs to carry, and which declaration they came from.

    The target task file's ``prefetch:`` is *accumulated*: the schema calls it
    mutable and backfilled with what was actually consulted, so the archived
    value is the union of every session's needs as they were discovered — not
    what any session declared when it ran. Reading the slice off it made the pack
    describe the sessions by knowledge they finished with.

    So the session-entry snapshots decide it when the pack carries any: the union
    of what the sessions themselves declared, in first-seen order. The union
    rather than a per-session slice because this section is per *pack* — one
    ``.ai/`` view for the whole task — and a doc any session declared is one this
    task's memory evidence should show. Per-session sets stay per-session, in the
    read rollups.

    Falling back needs the *carried bytes* as its test, not a non-empty union: a
    task whose sessions declared nothing has an empty slice as a fact, and
    reaching for the accumulated value there would reintroduce exactly the skew
    this replaces.

    Those bytes have to reach the end of the frontmatter to say anything, though.
    A head cut short of it parses as a smaller declaration or none at all, which
    is not "declared nothing" but "not known" — the one state the fact-versus-skew
    argument above does not cover — so such an entry is no source here. A pack
    carrying only those falls back to the accumulated value and says so through
    ``prefetch_source``, the honest answer when the session-time sets are simply
    not recoverable from what the pack holds.
    """
    carried = [
        entry.content
        for run in runs
        for entry in run.task_entries
        if entry.frontmatter_complete and entry.content is not None
    ]
    if not carried:
        return parse_prefetch(target.content), PREFETCH_SOURCE_TASK_FILE
    declared: list[str] = []
    for content in carried:
        for entry in parse_prefetch(content):
            if entry not in declared:
                declared.append(entry)
    return tuple(declared), PREFETCH_SOURCE_SESSION_ENTRIES


def artifact_json_text(data: dict) -> str:
    """Serialize one artifact's JSON exactly as every layer persists it.

    One rule for the three layers that write JSON into an evaluation dir
    (``evidence.json``, ``static_metrics.json``, ``semantic_report.json``), so the
    stored shape cannot drift between them — the ``indent=1`` is load-bearing only
    in that the report catalog hashes these bytes and a re-render must reproduce
    them.
    """
    return json.dumps(data, indent=1)


def evaluation_order_key(evaluation_id: str) -> tuple[str, int]:
    """Chronological sort key for an evaluation id: ``(stamp, suffix)``.

    Public because the report catalog's backfill
    (:meth:`orch_hub.report_catalog.ReportCatalog.reconcile`) must order existing
    evaluations by the same rule this module's listing uses, so a backfilled
    global sequence follows generation order instead of directory-walk order.

    An id is a fixed-width UTC stamp (:data:`_EVAL_ID_TS`, hyphen-free, so it
    sorts chronologically as a string) with an optional ``-N`` same-second
    collision suffix (``N`` >= 2; :meth:`EvaluationService._reserve_evaluation_dir`
    mints the first id in a second with no suffix). The suffix is *unpadded*, so
    it must sort numerically — a plain lexical sort ranks ``-10`` before ``-9``,
    which would surface a stale newest evaluation. The base (no suffix) is the
    first generation in its second, so it keys as suffix 1 (one below the first
    real ``-2``), ordering ahead of every suffix at the same stamp.
    """
    stamp, sep, suffix = evaluation_id.partition("-")
    return (stamp, int(suffix) if sep and suffix.isdigit() else 1)


class EvaluationService:
    """Read-only task-evaluation foundation, gated by ``evaluation_enabled``.

    Every operation fails closed with :class:`EvaluationDisabledError` when the
    subsystem is disabled, so a deployment that omits evaluation has a hard
    backend boundary independent of the (later) API gate. Collaborators are
    injectable for tests: ``record_loader`` defaults to
    :func:`orch_hub.runs.load_records` (the read-only run-record reader that never
    mutates run state), ``git_runner`` to the shared blocking command runner (so
    a test drives the git window with a fake runner instead of a real
    subprocess), and ``clock`` must return timezone-aware UTC datetimes (it stamps
    the evaluation id and the pack's ``generated_ts``).

    ``retention_guard`` is the optional hook that keeps retention from deleting a
    completed report the global feed has not published yet (the app wires
    :meth:`orch_hub.report_catalog.ReportCatalog.unpublished_ids`). Pruning is
    otherwise free to remove a durably complete report whose live catalog append
    failed, which would erase it from the feed permanently — reconciliation heals
    only from artifacts that still exist. Injected as a plain callable so this
    module stays independent of the catalog (the dependency runs the other way).
    """

    def __init__(
        self,
        config: Config,
        *,
        record_loader: Callable[
            [Config], Sequence[tuple[Path, RunRecord]]
        ] = load_records,
        git_runner: Runner = run_command,
        clock: Callable[[], dt.datetime] = _utc_now,
        retention_guard: Callable[[ManagedRepo, str], Collection[str]] | None = None,
    ):
        self._config = config
        self._record_loader = record_loader
        self._git_runner = git_runner
        self._clock = clock
        self._retention_guard = retention_guard

    @property
    def enabled(self) -> bool:
        return self._config.evaluation_enabled

    def _require_enabled(self) -> None:
        if not self._config.evaluation_enabled:
            raise EvaluationDisabledError(
                "task evaluation is disabled (ORCH_HUB_EVAL_ENABLED)"
            )

    def resolve_target(self, repo: ManagedRepo, task_id: str) -> EvaluationTarget:
        """Resolve ``task_id`` in ``repo`` to its task file, active or archived.

        Tries the active ``.ai-tasks/<id>.md`` first, then the archived copy, so a
        completed task still evaluates after close-out moved its file. Mirrors the
        :mod:`orch_hub.task_store` readers: genuine *absence* yields
        ``present=False`` / ``source=None``, while a real IO error propagates —
        :meth:`build_evidence` does *not* wrap this (unlike the per-run reads):
        the task file is the pack's primary subject, so an unreadable one fails
        the build loudly rather than yielding a targetless pack. ``task_id`` is
        not index-gated here — an evaluation may target a specific id (including an
        archived one) the caller already holds, like the run-scoped task route.
        """
        self._require_enabled()
        content = read_task_content(repo, task_id)
        source: str | None = None
        if content is not None:
            source = SOURCE_ACTIVE
        else:
            content = read_archived_task_content(repo, task_id)
            if content is not None:
                source = SOURCE_ARCHIVED
        return EvaluationTarget(
            repo_name=repo.name,
            repo_path=repo.path,
            task_id=task_id,
            source=source,
            present=content is not None,
            content=content,
        )

    def related_runs(self, repo: ManagedRepo, task_id: str) -> list[RelatedRun]:
        """Return every hub run for exactly ``(repo.path, task_id)``, oldest-first.

        Read-only: run records are loaded through :func:`orch_hub.runs.load_records`
        with no refresh, pid probe, or write. Runs for other repos or other tasks
        are excluded; an unreadable ``run.json`` (an ``error`` stub with no
        ``repo_path`` attribution) cannot match a concrete repo path and is thus
        naturally left out. Repo matching is exact-path equality, the same rule
        the run manager's per-repo serialization uses.
        """
        self._require_enabled()
        return [
            RelatedRun(record=record, run_dir=run_dir)
            for run_dir, record in self._record_loader(self._config)
            if record.repo_path is not None
            and record.repo_path == repo.path
            and record.task_id == task_id
        ]

    def artifact_dir(
        self, repo: ManagedRepo, task_id: str, evaluation_id: str
    ) -> Path:
        """Contained artifact directory for one evaluation (see
        :func:`evaluation_artifact_dir`). Gated; wires the hub data dir and repo
        name. Pure path math — :meth:`generate_evidence` is what creates it."""
        self._require_enabled()
        return evaluation_artifact_dir(
            self._config.data_dir,
            repo_name=repo.name,
            task_id=task_id,
            evaluation_id=evaluation_id,
        )

    def list_evaluations(
        self, repo: ManagedRepo, task_id: str
    ) -> list[StoredEvaluation]:
        """List every persisted evaluation for ``(repo, task_id)``, newest-first.

        A structural directory read, gated and contained: it lists the
        ``<evaluation-id>/`` dirs under the per-task level of the evaluations tree
        and, for each, the artifact filenames it holds (``evidence.json`` /
        ``static_metrics.json``), so the caller learns which evaluations exist and
        what each carries without reading any artifact body. Newest-first by id
        via :func:`evaluation_order_key`: the id is a UTC stamp with an optional
        unpadded ``-N`` same-second collision suffix, so it is ordered by
        ``(stamp, numeric suffix)`` (a plain reverse lexical sort would rank a
        same-second ``-10`` before ``-9`` and report a stale latest). An absent
        tree (no evaluation ever generated) is an empty list, not an error; only
        the gate fails closed. Read-only — it creates nothing.

        Enumerated through the contained walk and its census helpers
        (:func:`opened_evaluations_dir`, :func:`contained_dir_names`,
        :func:`contained_file_names`), so this manifest promises exactly what the
        read paths will serve: a linked evaluation dir or artifact is not listed,
        and a link at the repo/task hop raises
        :class:`EvaluationDamagedTreeError` rather than quietly listing another
        tree's evaluations as this task's.
        """
        self._require_enabled()
        stored: list[StoredEvaluation] = []
        with opened_evaluations_dir(
            self._config.data_dir, repo_name=repo.name, task_id=task_id
        ) as parent_fd:
            if parent_fd is None:
                return []
            names = contained_dir_names(parent_fd)
        for name in names:
            with opened_evaluations_dir(
                self._config.data_dir,
                repo_name=repo.name,
                task_id=task_id,
                evaluation_id=name,
            ) as child_fd:
                if child_fd is None:
                    continue
                stored.append(
                    StoredEvaluation(
                        evaluation_id=name,
                        artifacts=tuple(contained_file_names(child_fd)),
                    )
                )
        stored.sort(key=lambda e: evaluation_order_key(e.evaluation_id), reverse=True)
        return stored

    def prune_evaluations(
        self,
        repo: ManagedRepo,
        task_id: str,
        *,
        keep: int | None = None,
        protect: str | None = None,
    ) -> list[str]:
        """Remove all but the ``keep`` newest evaluations for ``(repo, task_id)``.

        The retention primitive: enumerate the task's evaluations newest-first
        (:meth:`list_evaluations`) and delete every one past the ``keep`` cap, so
        ``<data_dir>/evaluations`` stays bounded while the freshest history is kept
        for trend analysis. ``keep`` defaults to ``Config.eval_retention``; ``keep
        <= 0`` disables retention (a no-op, returning ``[]``), so a deployment can
        opt out. ``protect`` is an evaluation id that is never removed even if it
        sorts past the cap: the generate path passes the just-created id, because a
        same-second collision can reuse a *pruned* base id (which then sorts oldest
        by ``(stamp, suffix)``), and deleting the dir a generation just wrote would
        discard the evidence/metrics/report it just persisted — the guard keeps the
        fresh evaluation at a transient ``keep + 1`` that self-corrects next generation.
        Scoped strictly to this ``(repo, task)`` subtree — other tasks' evaluations
        are never touched — and removals go through the contained
        :meth:`artifact_dir` (rejecting unsafe segments). Best-effort per dir
        (``rmtree(ignore_errors=True)``, matching the generate-failure cleanup), so
        one un-removable dir cannot fail the prune. Returns the ids it removed,
        newest-first among the removed. Gated and hub-owned: it only ever deletes
        the subsystem's own artifacts, never target repo/task/run state.

        Deletion is the one write path that stays *path*-based, because
        ``shutil.rmtree`` takes a ``dir_fd`` only on Python 3.13+, above this
        package's floor. Three existing properties contain it, relied on
        deliberately and pinned by tests rather than inherited by accident:

        - What it deletes comes from :meth:`list_evaluations`, the contained walk,
          so a linked evaluation dir is never a candidate and a link at the
          ``<repo-slug>``/``<task-id>`` hop refuses the whole prune as damage
          instead of enumerating another tree's ids into a deletion list.
        - Should one reach the deletion list anyway (a dir swapped for a link after
          the listing), :meth:`artifact_dir`'s ``resolve()`` assertion refuses a
          link pointing *out* of the tree before ``rmtree`` runs.
        - A link pointing *inside* the tree passes that assertion and is then
          refused by ``rmtree`` itself, which ``lstat``s its own argument and will
          not delete through a symbolic link. ``ignore_errors`` turns that into
          "left alone", so the cost is a retained evaluation, never another
          evaluation's artifacts. Its interior walk is descriptor-based on every
          platform this runs on.

        An injected ``retention_guard`` extends ``protect`` with every evaluation
        the global report feed still owes a publication (see the class docstring):
        deleting one of those is unrecoverable, because reconciliation re-derives
        entries from the artifacts themselves. The guard is consulted **fail
        closed** — if it cannot answer, this prune is skipped entirely rather than
        risking the one deletion that loses a report — and retention self-corrects
        on the next generation once the feed has caught up.
        """
        self._require_enabled()
        limit = self._config.eval_retention if keep is None else keep
        if limit <= 0:
            return []
        protected = {protect} if protect is not None else set()
        if self._retention_guard is not None:
            try:
                protected |= set(self._retention_guard(repo, task_id))
            except Exception as err:  # noqa: BLE001 - unknown guard state = no prune
                _log.warning(
                    "retention skipped for %s/%s: guard failed (%s): %s",
                    repo.name,
                    task_id,
                    type(err).__name__,
                    err,
                )
                return []
        stored = self.list_evaluations(repo, task_id)
        removed: list[str] = []
        for entry in stored[limit:]:
            if entry.evaluation_id in protected:
                continue
            eval_dir = self.artifact_dir(repo, task_id, entry.evaluation_id)
            shutil.rmtree(eval_dir, ignore_errors=True)
            removed.append(entry.evaluation_id)
        return removed

    def auto_prune(
        self, repo: ManagedRepo, task_id: str, *, protect: str | None = None
    ) -> None:
        """Best-effort retention pass, run only after a generation is fully durable.

        Wraps :meth:`prune_evaluations` so retention never fails or rolls back an
        otherwise-successful generation — a broken directory read/removal is
        swallowed rather than propagated. The **timing** is the load-bearing part:
        a generation path must call this only once its whole artifact set is on
        disk, never before a higher layer's write could still fail and roll the
        just-minted dir back. :meth:`generate_evidence` calls it for the standalone
        evidence pack (complete once ``evidence.json`` is written); the metrics and
        semantic layers call it *after* their own higher-layer artifacts are
        written (:meth:`orch_hub.static_metrics.StaticEvaluator.generate_metrics`,
        :meth:`orch_hub.semantic.SemanticEvaluator.evaluate`). Pruning any earlier
        would let a failed static/semantic write — whose cleanup removes only the
        new dir — leave the task with *fewer* evaluations than before it ran, by
        having already deleted the older retained ones. ``protect`` is forwarded to
        keep the just-minted id alive even when a reused same-second base id sorts
        it oldest (see :meth:`prune_evaluations`).
        """
        try:
            self.prune_evaluations(repo, task_id, protect=protect)
        except Exception:
            pass

    def read_artifact(
        self, repo: ManagedRepo, task_id: str, evaluation_id: str, name: str
    ) -> dict | None:
        """Read and parse one persisted artifact JSON, or ``None`` when absent.

        ``name`` is an artifact filename (:data:`EVIDENCE_NAME` or the static
        layer's ``STATIC_METRICS_NAME`` — a fixed constant supplied by the caller,
        so the subsystem's own filenames stay in their home modules rather than
        being duplicated here) and is re-validated as a single safe segment.
        Resolution goes through :func:`read_contained_artifact`, so a crafted
        repo/task/evaluation id is rejected before any read and every hop of the
        tree is walked without following a link — the bytes this hands back came
        from the object that open accepted, never from whatever the name resolves
        to now. A missing evaluation dir or a missing artifact file both read as
        ``None`` (a not-yet-generated or evidence-only evaluation), kept distinct
        from a present-but-empty artifact; a link standing where an artifact or one
        of its parent dirs should be raises
        :class:`EvaluationDamagedTreeError` rather than serving the outside file it
        points at. Gated and read-only; a corrupt artifact surfaces its ``json``
        decode error rather than being silently masked.
        """
        self._require_enabled()
        data = read_contained_artifact(
            self._config.data_dir,
            repo_name=repo.name,
            task_id=task_id,
            evaluation_id=evaluation_id,
            name=name,
        )
        if data is None:
            return None
        return json.loads(data.decode("utf-8"))

    def read_text_artifact(
        self, repo: ManagedRepo, task_id: str, evaluation_id: str, name: str
    ) -> str | None:
        """Read one persisted *text* artifact verbatim, or ``None`` when absent.

        The text parallel of :meth:`read_artifact` for a non-JSON sibling
        artifact (the semantic layer's ``report.md`` — a human-readable export
        rendered beside ``semantic_report.json``). Same containment, by the same
        helper: ``name`` is re-validated as a single safe segment, a crafted
        repo/task/evaluation id is rejected before any read, no hop of the tree is
        followed through a link, and a missing dir or file both read as ``None``.
        Gated and read-only. Kept distinct from :meth:`read_artifact` because a
        Markdown artifact must not be run through ``json.loads``.
        """
        self._require_enabled()
        data = read_contained_artifact(
            self._config.data_dir,
            repo_name=repo.name,
            task_id=task_id,
            evaluation_id=evaluation_id,
            name=name,
        )
        if data is None:
            return None
        return data.decode("utf-8")

    def write_artifact(
        self, repo: ManagedRepo, task_id: str, evaluation_id: str, name: str, data: dict
    ) -> None:
        """Persist one JSON artifact into an already-reserved evaluation dir.

        The write counterpart of :meth:`read_artifact`, and the single seam the
        upper layers use to put an artifact in the tree — the static and semantic
        evaluators call this rather than opening ``pack.evaluation_dir / <name>``
        themselves, so containment is decided once here instead of once per layer.
        Serialization is :func:`artifact_json_text` and the write is
        :func:`write_contained_artifact`: atomic, and no hop of the tree followed
        through a link. Gated; the dir must already have been reserved by
        :meth:`generate_evidence`.
        """
        self._require_enabled()
        write_contained_artifact(
            self._config.data_dir,
            repo_name=repo.name,
            task_id=task_id,
            evaluation_id=evaluation_id,
            name=name,
            text=artifact_json_text(data),
        )

    def write_text_artifact(
        self, repo: ManagedRepo, task_id: str, evaluation_id: str, name: str, text: str
    ) -> None:
        """Persist one *text* artifact verbatim (the semantic layer's ``report.md``).

        The write counterpart of :meth:`read_text_artifact`, same containment by
        the same helper; kept distinct from :meth:`write_artifact` for the same
        reason the readers are — a Markdown render must not be routed through
        ``json.dumps``.
        """
        self._require_enabled()
        write_contained_artifact(
            self._config.data_dir,
            repo_name=repo.name,
            task_id=task_id,
            evaluation_id=evaluation_id,
            name=name,
            text=text,
        )

    def build_evidence(
        self, repo: ManagedRepo, task_id: str, *, evaluation_id: str
    ) -> EvidencePack:
        """Assemble a bounded, read-only :class:`EvidencePack` (no writes).

        Composes the foundation primitives: the resolved target task
        (active/archived/absent), every related run's aggregated evidence
        (:meth:`_run_evidence` — bounded logs, history events, session snapshot,
        each read isolated), a best-effort git-window (:meth:`_git_evidence`),
        the bounded ``.ai/`` snapshot view (:meth:`_ai_evidence`), and the
        per-session read-behavior rollup (:meth:`_reads_evidence`, which reads
        the session ids the run evidence just collected). The two
        git-backed sections are handed the *same* window (:func:`_git_window`),
        computed once: a commit list and a ``.ai/`` patch stream that disagreed
        about which window they covered would be worse than either alone.

        Stays entirely read-only over target repo and run state;
        ``evaluation_id`` is caller-supplied so a build is deterministic for
        tests (:meth:`generate_evidence` is the path that mints an id and
        persists). ``resolve_target`` may still propagate a genuine target-repo IO
        error (its session-1 contract), so callers that must never fail wrap it.
        """
        self._require_enabled()
        target = self.resolve_target(repo, task_id)
        related = self.related_runs(repo, task_id)
        # Bound the session-entry files before anything reads them, so every
        # section downstream — the ``.ai/`` slice, the read rollups — is derived
        # from exactly the bytes the persisted pack carries and stays
        # reproducible from it.
        runs, warnings = _bounded_task_entries(
            tuple(self._run_evidence(run) for run in related)
        )
        since, until, window_detail = _git_window(related)
        git = self._git_evidence(repo, since, until, window_detail)
        prefetch, prefetch_source = _prefetch_slice(runs, target)
        ai = self._ai_evidence(
            repo, prefetch, prefetch_source, since, until, window_detail
        )
        reads = self._reads_evidence(repo, task_id, runs)
        return EvidencePack(
            evaluation_id=evaluation_id,
            generated_ts=self._clock().isoformat(),
            target=target,
            runs=runs,
            git=git,
            ai=ai,
            reads=reads,
            warnings=warnings,
        )

    def generate_evidence(
        self, repo: ManagedRepo, task_id: str, *, prune: bool = True
    ) -> EvidencePack:
        """Build and persist an evidence pack; return it with its artifact dir set.

        Mints a fresh, collision-free evaluation id, reserves its contained
        artifact dir, assembles the pack (:meth:`build_evidence`), and writes
        ``evidence.json`` atomically. The id must be settled before the build (it
        is embedded in the pack and names the dir), so the dir is reserved first
        and removed on any failure — mirroring the run manager's spawn-failure
        cleanup — leaving no empty evaluation dir behind. The only writes are the
        pack's own artifacts under ``<data_dir>/evaluations``; target repo, task
        file, run records, ``.ai/``, and ``.ai-tasks/`` are never touched.

        ``prune`` controls the post-write retention pass. It defaults True for the
        **standalone** evidence path (this method's own artifact set is complete
        once ``evidence.json`` is written, so retention is safe here). The metrics
        and semantic layers, which call this to *mint the dir* and then write more
        artifacts into it, pass ``prune=False`` and run retention themselves only
        after their higher-layer writes are durable — see :meth:`auto_prune` for
        why pruning here would otherwise let a failed static/semantic write destroy
        older retained evaluations.
        """
        self._require_enabled()
        evaluation_id, evaluation_dir = self._reserve_evaluation_dir(repo, task_id)
        try:
            pack = self.build_evidence(repo, task_id, evaluation_id=evaluation_id)
            self.write_artifact(
                repo, task_id, evaluation_id, EVIDENCE_NAME, pack.to_dict()
            )
        except Exception:
            # This call created the dir; remove it so a failed generate leaves no
            # empty evaluation dir (the run manager cleans a failed spawn the same
            # way). ``ignore_errors`` keeps cleanup itself from masking the cause.
            shutil.rmtree(evaluation_dir, ignore_errors=True)
            raise
        # Retention for the standalone evidence path only: this method's artifacts
        # are complete once evidence.json is written above, so it is safe to prune
        # now. The metrics/semantic paths pass ``prune=False`` and prune themselves
        # AFTER their higher-layer writes land, because those writes can still fail
        # and roll this dir back — pruning here first would already have deleted the
        # older retained evaluations (see :meth:`auto_prune`). ``protect`` the id
        # just written: a same-second collision can reuse a pruned base id that now
        # sorts oldest, so an unprotected prune could delete the evidence just
        # persisted here.
        if prune:
            self.auto_prune(repo, task_id, protect=evaluation_id)
        return replace(pack, evaluation_dir=evaluation_dir)

    def _reserve_evaluation_dir(
        self, repo: ManagedRepo, task_id: str
    ) -> tuple[str, Path]:
        """Mint a collision-free evaluation id and create its contained dir.

        The id is a UTC stamp; a same-second collision bumps a ``-N`` suffix
        (mirrors the run manager's run-dir creation). Segment legality still comes
        from :meth:`artifact_dir`, which is also what hands back the path the
        generation's failure cleanup and :class:`EvidencePack` carry.

        The directories are made through the tree's own walk
        (:func:`opened_evaluations_dir` with ``create=True``) rather than by
        ``mkdir(parents=True)`` on that path. The path helper's ``resolve()``
        assertion already refuses a hop link pointing *outside* the tree, but it
        proves that about the names at check time and the kernel then resolves them
        again at ``mkdir`` time — so a link swapped in between, or one pointing at
        another dir *inside* the tree, was followed. Creating hop by hop and
        re-opening each ``O_NOFOLLOW`` closes both: this is the same rule the reads
        hold, applied at the one place that can put a directory in the tree at all.

        The collision bump is a plain ``mkdir`` relative to the ``<repo>/<task>``
        descriptor, so ``FileExistsError`` keeps meaning exactly what it did — this
        second is taken — without a second round trip through the pathname.
        """
        base_id = self._clock().strftime(_EVAL_ID_TS)
        with opened_evaluations_dir(
            self._config.data_dir,
            repo_name=repo.name,
            task_id=task_id,
            create=True,
        ) as parent_fd:
            assert parent_fd is not None  # create=True never yields absence
            for attempt in itertools.count():
                evaluation_id = base_id if attempt == 0 else f"{base_id}-{attempt + 1}"
                evaluation_dir = self.artifact_dir(repo, task_id, evaluation_id)
                try:
                    os.mkdir(evaluation_id, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                return evaluation_id, evaluation_dir
        raise AssertionError("unreachable")

    def _run_evidence(self, related: RelatedRun) -> RunEvidence:
        """Aggregate one related run's bounded, read-only evidence.

        Each per-run read is isolated: the module functions already degrade
        rather than raise (corrupt ``events.jsonl`` yields inline ``history_error``
        events; a bad ``sessions.json`` reads as ``None``; a missing/rotated log
        is an absent snapshot), and an unexpected failure is caught and recorded
        as a warning so one broken run never fails the whole pack. A present
        ``history_error`` event is also surfaced as a warning so a corrupt history
        is visible in the pack summary, not just buried in the event list."""
        record = related.record
        run_dir = related.run_dir
        warnings: list[str] = []
        events: tuple[RunEvent, ...] = ()
        sessions: SessionsSnapshot | None = None
        log: LogSnapshot | None = None
        spawn_out: LogSnapshot | None = None
        task_snapshots: TaskSnapshots | None = None
        protocol: ProtocolSnapshot | None = None
        try:
            events = read_events(run_dir)
            if any(event.kind == KIND_HISTORY_ERROR for event in events):
                warnings.append(f"run {record.run_id}: corrupt history events present")
        except Exception:
            warnings.append(f"run {record.run_id}: history events unreadable")
        try:
            sessions = read_snapshot(run_dir)
        except Exception:
            warnings.append(f"run {record.run_id}: session snapshot unreadable")
        try:
            log = read_recent(record, max_lines=EVIDENCE_LOG_MAX_LINES)
        except Exception:
            warnings.append(f"run {record.run_id}: orchestrator log unreadable")
        try:
            spawn_out = read_spawn_out(record, max_lines=EVIDENCE_LOG_MAX_LINES)
        except Exception:
            warnings.append(f"run {record.run_id}: startup diagnostic unreadable")
        try:
            task_snapshots = read_snapshots(run_dir)
        except Exception:
            warnings.append(f"run {record.run_id}: session-entry snapshots unreadable")
        try:
            protocol = read_protocol(run_dir)
        except Exception:
            warnings.append(f"run {record.run_id}: protocol provenance unreadable")
        return RunEvidence(
            record=record,
            events=events,
            sessions=sessions,
            log=log,
            spawn_out=spawn_out,
            task_snapshots=task_snapshots,
            protocol=protocol,
            warnings=tuple(warnings),
        )

    def _git_evidence(
        self,
        repo: ManagedRepo,
        since: str | None,
        until: str | None,
        window_detail: str | None,
    ) -> EvidenceGit:
        """Best-effort git commit metadata for the task's run-lifecycle window.

        With no window (:func:`_git_window` found no parseable run start) git
        evidence is ``available=False`` carrying that reason and git is never
        invoked. The query is bounded by :data:`EVIDENCE_COMMIT_LIMIT` and fails
        closed to unavailable on any git problem
        (:func:`orch_hub.git_meta.read_commits_between`); it is read-only
        ``git log``. ``truncated`` folds both incompleteness bounds together:
        the count cap here, and the reader's own byte cap on the capture — a
        byte-cut list is short of the count cap, so the count test alone would
        report a partial window as whole.
        """
        if since is None:
            return EvidenceGit(available=False, detail=window_detail)
        log = read_commits_between(
            repo,
            since=since,
            until=until,
            limit=EVIDENCE_COMMIT_LIMIT,
            runner=self._git_runner,
        )
        return EvidenceGit(
            available=log.available,
            since=since,
            until=until,
            commits=log.commits,
            truncated=log.truncated or len(log.commits) >= EVIDENCE_COMMIT_LIMIT,
            detail=None if log.available else "git history unavailable",
        )

    def _ai_evidence(
        self,
        repo: ManagedRepo,
        prefetch: Sequence[str],
        prefetch_source: str,
        since: str | None,
        until: str | None,
        window_detail: str | None,
    ) -> AiSnapshot:
        """Bounded, read-only view of the repo's ``.ai/`` memory for this task.

        The lazy slice to carry alongside the eager set is chosen by
        :func:`_prefetch_slice` — the sessions' own entry declarations where the
        pack has them, the task file's accumulated list otherwise — and the
        snapshot records which, so the section says whose declaration it shows
        rather than leaving a reader to assume. The window
        is the pack's own (shared with :meth:`_git_evidence`), so the ``.ai/``
        patches and the commit list describe the same span.

        Isolated like the per-run reads and for the same reason: ``.ai/`` is
        *secondary* evidence — unlike the target task file, whose absence changes
        what is being evaluated — so an unexpected failure degrades this section
        to unavailable instead of failing a pack whose runs and task file are
        perfectly intact. :func:`orch_hub.ai_snapshot.read_ai_snapshot` already
        handles every expected failure mode itself; this catches the rest.
        """
        try:
            return read_ai_snapshot(
                repo,
                prefetch=prefetch,
                prefetch_source=prefetch_source,
                since=since,
                until=until,
                window_detail=window_detail,
                runner=self._git_runner,
            )
        except Exception as err:  # noqa: BLE001 - secondary evidence never fails a pack
            detail = f".ai/ snapshot unreadable ({type(err).__name__}: {err})"
            return AiSnapshot(
                present=False,
                diff=AiDiff(available=False, since=since, until=until, detail=detail),
                warnings=(f".ai/: {detail}",),
                detail=detail,
                prefetch_source=prefetch_source,
            )

    def _reads_evidence(
        self, repo: ManagedRepo, task_id: str, runs: Sequence[RunEvidence]
    ) -> ReadBehavior:
        """Per-session read-behavior rollup for the sessions this pack knows about.

        The session ids come from the run evidence already assembled above — the
        persisted per-run session snapshots — so no new source of run state is
        introduced and a live run (whose snapshot is written only at the
        live-to-ended transition) simply contributes no sessions.

        Isolated like the ``.ai/`` section and for the same reason: read behavior
        is *secondary* evidence about how a task was worked, not what it is, so an
        unexpected failure degrades this section alone rather than failing a pack
        whose runs, task file, and memory snapshot are intact.
        :func:`orch_hub.session_reads.read_session_behavior` already handles every
        expected failure mode itself; this catches the rest.
        """
        try:
            return read_session_behavior(
                repo,
                task_id=task_id,
                sessions=_session_refs(runs),
                transcript_root=self._config.claude_transcripts_dir,
            )
        except Exception as err:  # noqa: BLE001 - secondary evidence never fails a pack
            detail = f"read behavior unreadable ({type(err).__name__}: {err})"
            return ReadBehavior(
                available=False, warnings=(f"read behavior: {detail}",), detail=detail
            )
