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
  and best-effort git-window commit metadata, then persist it as ``evidence.json``.
  Every per-run read is isolated so a corrupt run artifact degrades to a warning
  rather than failing the pack, and the whole build stays read-only over target
  repo/run state — the only writes are the pack's own artifacts.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

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
from .sessions import read_snapshot
from .subprocess_utils import Runner, run_command
from .task_store import read_archived_task_content, read_task_content

EVALUATIONS_SUBDIR = "evaluations"
"""Top-level hub-data-dir subtree holding evaluation artifacts, parallel to the
run manager's ``runs/`` and the intake manager's ``intake/`` trees."""

EVIDENCE_NAME = "evidence.json"
"""The persisted evidence pack, written in each evaluation's artifact dir."""

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
    repo_seg = _safe_segment(_slug(repo_name), label="repo")
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


def _slug(value: str) -> str:
    """Make a repo name safe as a path component (matches ``runs._slug``)."""
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
    ``truncated`` marks a window that hit :data:`EVIDENCE_COMMIT_LIMIT` (more
    commits exist than returned). Diffs are intentionally omitted: they are
    unbounded, and the bounded-evidence rule keeps raw diffs/transcripts out of
    the pack.
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
    ``runs`` are its related runs' aggregated evidence (oldest-first), and ``git``
    is the best-effort commit window. ``warnings`` collects pack-level degradation
    (per-run warnings live on each :class:`RunEvidence`). ``summary`` (computed in
    :meth:`to_dict`) is the validating metadata — run/ended counts, whether task
    content was found, git availability, and the total warning count — enough to
    sanity-check a pack without walking it. ``evaluation_dir`` is the persisted
    location, set only by :meth:`EvaluationService.generate_evidence`; it is
    internal filesystem detail and is excluded from :meth:`to_dict`, matching
    ``RunRecord``/``LogSnapshot``.
    """

    evaluation_id: str
    generated_ts: str | None
    target: EvaluationTarget
    runs: tuple[RunEvidence, ...] = ()
    git: EvidenceGit | None = None
    warnings: tuple[str, ...] = ()
    evaluation_dir: Path | None = None

    def to_dict(self) -> dict:
        git = self.git if self.git is not None else EvidenceGit(available=False)
        return {
            "evaluation_id": self.evaluation_id,
            "generated_ts": self.generated_ts,
            "target": self.target.to_dict(),
            "runs": [run.to_dict() for run in self.runs],
            "git": git.to_dict(),
            "warnings": list(self.warnings),
            "summary": self._summary(git),
        }

    def _summary(self, git: EvidenceGit) -> dict:
        return {
            "run_count": len(self.runs),
            "runs_ended": sum(1 for run in self.runs if run.ended),
            "has_task_content": self.target.present,
            "target_source": self.target.source,
            "git_available": git.available,
            "warning_count": (
                len(self.warnings) + sum(len(run.warnings) for run in self.runs)
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


def _write_evidence(path: Path, data: dict) -> None:
    """Persist ``evidence.json`` atomically (same-dir tmp + rename), like run.json."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


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
    ):
        self._config = config
        self._record_loader = record_loader
        self._git_runner = git_runner
        self._clock = clock

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
        (the id is a UTC stamp, so a reverse lexical sort is chronological,
        matching the run manager's run ordering). An absent tree (no evaluation
        ever generated) is an empty list, not an error; only the gate fails
        closed. Read-only — it creates nothing.
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
        stored.sort(key=lambda e: e.evaluation_id, reverse=True)
        return stored

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

    def build_evidence(
        self, repo: ManagedRepo, task_id: str, *, evaluation_id: str
    ) -> EvidencePack:
        """Assemble a bounded, read-only :class:`EvidencePack` (no writes).

        Composes the three foundation primitives: the resolved target task
        (active/archived/absent), every related run's aggregated evidence
        (:meth:`_run_evidence` — bounded logs, history events, session snapshot,
        each read isolated), and a best-effort git-window
        (:meth:`_git_evidence`). Stays entirely read-only over target repo and run
        state; ``evaluation_id`` is caller-supplied so a build is deterministic
        for tests (:meth:`generate_evidence` is the path that mints an id and
        persists). ``resolve_target`` may still propagate a genuine target-repo IO
        error (its session-1 contract), so callers that must never fail wrap it.
        """
        self._require_enabled()
        target = self.resolve_target(repo, task_id)
        related = self.related_runs(repo, task_id)
        runs = tuple(self._run_evidence(run) for run in related)
        git = self._git_evidence(repo, related)
        return EvidencePack(
            evaluation_id=evaluation_id,
            generated_ts=self._clock().isoformat(),
            target=target,
            runs=runs,
            git=git,
        )

    def generate_evidence(self, repo: ManagedRepo, task_id: str) -> EvidencePack:
        """Build and persist an evidence pack; return it with its artifact dir set.

        Mints a fresh, collision-free evaluation id, reserves its contained
        artifact dir, assembles the pack (:meth:`build_evidence`), and writes
        ``evidence.json`` atomically. The id must be settled before the build (it
        is embedded in the pack and names the dir), so the dir is reserved first
        and removed on any failure — mirroring the run manager's spawn-failure
        cleanup — leaving no empty evaluation dir behind. The only writes are the
        pack's own artifacts under ``<data_dir>/evaluations``; target repo, task
        file, run records, ``.ai/``, and ``.ai-tasks/`` are never touched.
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
        self, repo: ManagedRepo, related: Sequence[RelatedRun]
    ) -> EvidenceGit:
        """Best-effort git commit metadata for the task's run-lifecycle window.

        The window spans the earliest run ``started_ts`` to the latest run
        ``ended_ts``, and stays open-ended (``until=None``) whenever any run is
        still live (no parseable ``ended_ts``) — a live run's activity runs to now,
        so the window must not close at an earlier run's end.
        With no parseable run start there is no window, so git evidence is
        ``available=False`` with that reason. The query is bounded by
        :data:`EVIDENCE_COMMIT_LIMIT` and fails closed to unavailable on any git
        problem (:func:`orch_hub.git_meta.read_commits_between`); it is read-only
        ``git log``. ``truncated`` marks a window with more commits than the cap.
        """
        starts = [ts for ts in (_parse_ts(r.record.started_ts) for r in related) if ts]
        if not starts:
            return EvidenceGit(
                available=False,
                detail="no run start timestamps to form a git window",
            )
        run_ends = [_parse_ts(r.record.ended_ts) for r in related]
        any_live = any(end is None for end in run_ends)
        since = min(starts).isoformat()
        # The window's upper bound is the union of every run's activity: an ended
        # run reaches its ``ended_ts``, a still-live run (no parseable ``ended_ts``)
        # runs to now. So any live run leaves the lifecycle open and the window
        # unbounded (``until=None``) — bounding it at an earlier run's end would drop
        # commits made during that live run. Only an all-ended set closes, at the
        # latest end.
        until = None if any_live else max(run_ends).isoformat()
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
            truncated=len(log.commits) >= EVIDENCE_COMMIT_LIMIT,
            detail=None if log.available else "git history unavailable",
        )
