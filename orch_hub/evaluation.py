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
        evidence.json    the bounded evidence pack (built in session-2)

This session (foundation) provides three primitives the evidence builder composes:

- **target resolution** — resolve a ``(repo, task_id)`` to its task file, whether
  active (``.ai-tasks/<id>.md``) or archived (``.ai-tasks/archive/<id>.md``),
  reusing the contained/​safe :mod:`orch_hub.task_store` readers.
- **related-run aggregation** — find every hub run for that exact
  ``(repo_path, task_id)`` from the persisted run records, read-only (via
  :func:`orch_hub.runs.load_records`, which never refreshes/mutates run state).
- **artifact path layout** — compute the contained, per-``(repo, task,
  evaluation)`` directory an evidence pack is written to, rejecting unsafe path
  segments so a crafted id cannot escape the evaluations tree.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

from .config import Config
from .models import EvaluationTarget, ManagedRepo, RelatedRun, RunRecord
from .runs import load_records
from .task_store import read_archived_task_content, read_task_content

EVALUATIONS_SUBDIR = "evaluations"
"""Top-level hub-data-dir subtree holding evaluation artifacts, parallel to the
run manager's ``runs/`` and the intake manager's ``intake/`` trees."""

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


def evaluation_artifact_dir(
    data_dir: Path,
    *,
    repo_name: str,
    task_id: str,
    evaluation_id: str,
) -> Path:
    """Return the contained directory for one evaluation's artifacts.

    Layout is ``<data_dir>/evaluations/<repo-slug>/<task-id>/<evaluation-id>/``.
    The repo component is slugged (:data:`_SLUG`); the task and evaluation
    components must each be a single safe path segment. A segment that is empty,
    contains a separator or null byte, or is a ``.``/``..`` dot reference raises
    ``ValueError`` — fail closed rather than build a path that could escape the
    evaluations tree. As a final defense the resolved target is asserted to stay
    under the resolved evaluations base (catching, e.g., an all-dots repo slug),
    so neither a crafted task id, evaluation id, nor repo name can steer a write
    outside ``<data_dir>/evaluations``. Pure path math: it creates nothing.
    """
    base = data_dir / EVALUATIONS_SUBDIR
    repo_seg = _safe_segment(_slug(repo_name), label="repo")
    task_seg = _safe_segment(task_id, label="task id")
    eval_seg = _safe_segment(evaluation_id, label="evaluation id")
    target = base / repo_seg / task_seg / eval_seg
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


class EvaluationService:
    """Read-only task-evaluation foundation, gated by ``evaluation_enabled``.

    Every operation fails closed with :class:`EvaluationDisabledError` when the
    subsystem is disabled, so a deployment that omits evaluation has a hard
    backend boundary independent of the (later) API gate. ``record_loader`` is
    injectable for tests — it defaults to :func:`orch_hub.runs.load_records`, the
    read-only run-record reader that never mutates run state.
    """

    def __init__(
        self,
        config: Config,
        *,
        record_loader: Callable[
            [Config], Sequence[tuple[Path, RunRecord]]
        ] = load_records,
    ):
        self._config = config
        self._record_loader = record_loader

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
        ``present=False`` / ``source=None``, while a real IO error propagates
        (the evidence builder isolates that in session-2). ``task_id`` is not
        index-gated here — an evaluation may target a specific id (including an
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
        name. Pure path math — the evidence builder creates it in session-2."""
        self._require_enabled()
        return evaluation_artifact_dir(
            self._config.data_dir,
            repo_name=repo.name,
            task_id=task_id,
            evaluation_id=evaluation_id,
        )
