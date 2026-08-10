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
  segments so a crafted id cannot escape the evaluations tree.
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
import itertools
import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .ai_snapshot import AiDiff, AiSnapshot, read_ai_snapshot
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
from .runs import load_records
from .session_reads import ReadBehavior, SessionRef, read_session_behavior
from .sessions import read_snapshot
from .subprocess_utils import Runner, run_command
from .task_store import parse_prefetch, read_archived_task_content, read_task_content

_log = logging.getLogger(__name__)

EVALUATIONS_SUBDIR = "evaluations"
"""Top-level hub-data-dir subtree holding evaluation artifacts, parallel to the
run manager's ``runs/`` and the intake manager's ``intake/`` trees."""

EVIDENCE_NAME = "evidence.json"
"""The persisted evidence pack, written in each evaluation's artifact dir."""

EVIDENCE_SCHEMA_VERSION = "13"
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
about its own ordering; a v13 one does.
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


class EvaluationError(Exception):
    """Base class for task-evaluation errors."""


class EvaluationDisabledError(EvaluationError):
    """A task-evaluation operation was attempted while the subsystem is disabled.

    Raised by every :class:`EvaluationService` operation when
    ``Config.evaluation_enabled`` is off, so a deployment that disables
    evaluation gets a fail-closed backend boundary (the later API layer maps this
    to a removed/404 surface).
    """


def task_evaluations_dir(data_dir: Path, *, repo_name: str, task_id: str) -> Path:
    """Return the contained parent dir holding every evaluation for one task.

    Layout is ``<data_dir>/evaluations/<repo-slug>/<task-id>/``, the directory
    :func:`evaluation_artifact_dir` places each ``<evaluation-id>/`` under and
    :meth:`EvaluationService.list_evaluations` enumerates. The repo component is
    slugged (:data:`_SLUG`); the task component must be a single safe path
    segment (empty, a separator/null byte, or a ``.``/``..`` dot reference raises
    ``ValueError``, failing closed). As a final defense the resolved target is
    asserted to stay under the resolved evaluations base, so neither a crafted
    task id nor repo name can steer a read/write outside ``<data_dir>/evaluations``.
    Pure path math: it creates nothing.
    """
    base = data_dir / EVALUATIONS_SUBDIR
    repo_seg = _safe_segment(repo_slug(repo_name), label="repo")
    task_seg = _safe_segment(task_id, label="task id")
    target = base / repo_seg / task_seg
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

    Layout is ``<data_dir>/evaluations/<repo-slug>/<task-id>/<evaluation-id>/`` —
    the per-task parent (:func:`task_evaluations_dir`, which slugs the repo and
    validates the task segment) plus a validated ``<evaluation-id>`` segment. A
    segment that is empty, contains a separator or null byte, or is a ``.``/``..``
    dot reference raises ``ValueError`` — fail closed rather than build a path
    that could escape the evaluations tree. As a final defense the resolved target
    is asserted to stay under the resolved evaluations base (catching, e.g., an
    all-dots repo slug), so neither a crafted task id, evaluation id, nor repo
    name can steer a write outside ``<data_dir>/evaluations``. Pure path math: it
    creates nothing.
    """
    base = data_dir / EVALUATIONS_SUBDIR
    parent = task_evaluations_dir(data_dir, repo_name=repo_name, task_id=task_id)
    eval_seg = _safe_segment(evaluation_id, label="evaluation id")
    target = parent / eval_seg
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"evaluation artifact path escapes {base}: {target}")
    return target


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
    """

    record: RunRecord
    events: tuple[RunEvent, ...] = ()
    sessions: SessionsSnapshot | None = None
    log: LogSnapshot | None = None
    spawn_out: LogSnapshot | None = None
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
            "warnings": list(self.warnings),
        }


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
    content was found, git/``.ai/``/read availability, and the total warning count
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
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "evaluation_id": self.evaluation_id,
            "generated_ts": self.generated_ts,
            "target": self.target.to_dict(),
            "runs": [run.to_dict() for run in self.runs],
            "git": git.to_dict(),
            "ai": ai.to_dict(),
            "reads": reads.to_dict(),
            "warnings": list(self.warnings),
            "summary": self._summary(git, ai, reads),
        }

    def _summary(
        self, git: EvidenceGit, ai: AiSnapshot, reads: ReadBehavior
    ) -> dict:
        return {
            "run_count": len(self.runs),
            "runs_ended": sum(1 for run in self.runs if run.ended),
            "has_task_content": self.target.present,
            "target_source": self.target.source,
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
    """
    return [
        SessionRef(
            session_id=summary.sid,
            run_id=run.record.run_id,
            role=summary.role,
            kind=summary.kind,
        )
        for run in runs
        if run.sessions is not None
        for summary in run.sessions.sessions
        if summary.sid
    ]


def _write_evidence(path: Path, data: dict) -> None:
    """Persist ``evidence.json`` atomically (same-dir tmp + rename), like run.json."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


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
        ``<evaluation-id>/`` dirs under the per-task :func:`task_evaluations_dir`
        and, for each, the artifact filenames it holds (``evidence.json`` /
        ``static_metrics.json``), so the caller learns which evaluations exist and
        what each carries without reading any artifact body. Newest-first by id
        via :func:`evaluation_order_key`: the id is a UTC stamp with an optional
        unpadded ``-N`` same-second collision suffix, so it is ordered by
        ``(stamp, numeric suffix)`` (a plain reverse lexical sort would rank a
        same-second ``-10`` before ``-9`` and report a stale latest). An absent
        tree (no evaluation ever generated) is an empty list, not an error; only
        the gate fails closed. Read-only — it creates nothing.
        """
        self._require_enabled()
        parent = task_evaluations_dir(
            self._config.data_dir, repo_name=repo.name, task_id=task_id
        )
        if not parent.is_dir():
            return []
        stored = [
            StoredEvaluation(
                evaluation_id=child.name,
                artifacts=tuple(
                    sorted(p.name for p in child.iterdir() if p.is_file())
                ),
            )
            for child in parent.iterdir()
            if child.is_dir()
        ]
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
        Resolution goes through :meth:`artifact_dir`, so a crafted
        repo/task/evaluation id is rejected before any read. A missing evaluation
        dir or a missing artifact file both read as ``None`` (a not-yet-generated
        or evidence-only evaluation), kept distinct from a present-but-empty
        artifact. Gated and read-only; a corrupt artifact surfaces its ``json``
        decode error rather than being silently masked.
        """
        self._require_enabled()
        eval_dir = self.artifact_dir(repo, task_id, evaluation_id)
        path = eval_dir / _safe_segment(name, label="artifact name")
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def read_text_artifact(
        self, repo: ManagedRepo, task_id: str, evaluation_id: str, name: str
    ) -> str | None:
        """Read one persisted *text* artifact verbatim, or ``None`` when absent.

        The text parallel of :meth:`read_artifact` for a non-JSON sibling
        artifact (the semantic layer's ``report.md`` — a human-readable export
        rendered beside ``semantic_report.json``). Same containment: ``name`` is
        re-validated as a single safe segment and resolution goes through
        :meth:`artifact_dir`, so a crafted repo/task/evaluation id is rejected
        before any read; a missing dir or file both read as ``None``. Gated and
        read-only. Kept distinct from :meth:`read_artifact` because a Markdown
        artifact must not be run through ``json.loads``.
        """
        self._require_enabled()
        eval_dir = self.artifact_dir(repo, task_id, evaluation_id)
        path = eval_dir / _safe_segment(name, label="artifact name")
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

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
        runs = tuple(self._run_evidence(run) for run in related)
        since, until, window_detail = _git_window(related)
        git = self._git_evidence(repo, since, until, window_detail)
        ai = self._ai_evidence(repo, target, since, until, window_detail)
        reads = self._reads_evidence(repo, task_id, runs)
        return EvidencePack(
            evaluation_id=evaluation_id,
            generated_ts=self._clock().isoformat(),
            target=target,
            runs=runs,
            git=git,
            ai=ai,
            reads=reads,
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
            _write_evidence(evaluation_dir / EVIDENCE_NAME, pack.to_dict())
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
        (mirrors the run manager's run-dir creation). The dir is the contained
        :meth:`artifact_dir` path (which rejects unsafe repo/task/eval segments),
        created with parents, so persistence has a validated target."""
        base_id = self._clock().strftime(_EVAL_ID_TS)
        for attempt in itertools.count():
            evaluation_id = base_id if attempt == 0 else f"{base_id}-{attempt + 1}"
            evaluation_dir = self.artifact_dir(repo, task_id, evaluation_id)
            try:
                evaluation_dir.mkdir(parents=True)
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
        return RunEvidence(
            record=record,
            events=events,
            sessions=sessions,
            log=log,
            spawn_out=spawn_out,
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
        target: EvaluationTarget,
        since: str | None,
        until: str | None,
        window_detail: str | None,
    ) -> AiSnapshot:
        """Bounded, read-only view of the repo's ``.ai/`` memory for this task.

        The task's own frontmatter ``prefetch:`` list selects the lazy slice to
        carry alongside the eager set, so the pack records the part of the
        snapshot the sessions declared they used rather than a guess. The window
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
                prefetch=parse_prefetch(target.content),
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
