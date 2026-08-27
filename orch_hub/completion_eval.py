"""Completion-triggered task evaluation: L1 now, L2 enqueued, never a gate.

Evaluation used to start only when an operator asked for it, so the evaluated
corpus was a product of judgment — you evaluate the tasks that felt problematic
— and a batch drawn from it cannot support recurrence or base-rate claims no
matter how correctly it is imported. This module removes that selection: a run
whose end classifies as ``completed`` evaluates its task mechanically, with
nobody deciding which completions deserve a report.

The two layers are dispatched differently because they cost differently:

- **Layer 1 (static metrics) runs immediately.** It is deterministic and makes
  no model call, so there is nothing to schedule and nothing to contend for.
- **Layer 2 (semantic) is enqueued** through the existing
  :class:`~orch_hub.semantic_jobs.SemanticJobManager` — a persisted job record,
  a single in-flight turn per task, a failure recorded rather than retried
  forever. Cost is not the constraint here; contention is: one L2 turn can land
  inside the same subscription rate window a dev session needs.

Two gates and one guard:

- ``Config.evaluation_enabled`` is the subsystem gate (shared with every manual
  path) and ``Config.eval_auto`` is the trigger gate, so automatic evaluation
  can be switched off while manual generation stays available.
- The task-level once-guard is
  :meth:`~orch_hub.semantic_jobs.SemanticJobManager.start_job_if_absent`. The
  completion signal is per *run*, not per task: a task that reaches ``completed``
  without being archived is still launchable, and the close-out-only re-run that
  follows classifies ``completed`` again. The durable one-record-per-``(repo,
  task)`` job store is what makes the corpus count a task once.

Isolation is the invariant, exactly like notification delivery and the usage
refresh hook: this runs after the ending run is already persisted, off the run
manager's lock and off its thread, and every failure is logged and dropped. A
missing repo, a broken evidence pack, a dead evaluator — none of them may touch
run state, readiness, attention, or a launch/intake gate.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .config import Config
from .models import ManagedRepo, RunRecord
from .semantic_jobs import SemanticJobManager
from .static_metrics import StaticEvaluator

_log = logging.getLogger(__name__)

CompletionExecutor = Callable[[Callable[[], None]], None]
"""Runs one completed task's evaluation work off the caller's thread.

The caller is :meth:`orch_hub.runs.RunManager._refresh`, holding the run-manager
lock: every run read in the process (the dashboard, ``/api/escalations``, the
resident monitor, an unrelated repo's launch) queues behind it. Layer 1 is
deterministic but not instant — it walks the task file, every related run's
records/events/sessions, a bounded git window, and the ``.ai/`` snapshot — so it
must not run there. The default detaches onto a daemon thread; tests inject a
synchronous ``lambda run: run()`` so the work settles before the trigger returns.
Mirrors :data:`orch_hub.semantic_jobs.SemanticJobExecutor` and the run manager's
own notify dispatch."""


def _thread_executor(run: Callable[[], None]) -> None:
    """Default executor: evaluate a completed task on a short-lived daemon thread.

    Daemon so it never blocks interpreter shutdown; an abandoned read-only
    evaluation is safe (its own all-or-nothing cleanup removes any partial
    artifact dir, and an abandoned semantic job is reconciled to ``failed`` at the
    next startup).
    """
    threading.Thread(target=run, daemon=True, name="orch-hub-completion-eval").start()


class CompletionEvaluationTrigger:
    """Evaluates a task when one of its runs ends ``completed``.

    Wired into the app's single :class:`~orch_hub.runs.RunManager` as a plain
    ``Callable[[RunRecord], None]`` hook, so the run manager keeps no evaluation
    import and no evaluation policy — the same seam shape as the display-only
    usage-refresh hook. Collaborators are injectable for tests: ``static_evaluator``
    is Layer 1, ``job_manager`` owns Layer 2's persisted job lifecycle, and
    ``executor`` decides where the work runs.
    """

    def __init__(
        self,
        config: Config,
        *,
        static_evaluator: StaticEvaluator,
        job_manager: SemanticJobManager,
        executor: CompletionExecutor = _thread_executor,
    ):
        self._config = config
        self._static = static_evaluator
        self._jobs = job_manager
        self._executor = executor

    @property
    def enabled(self) -> bool:
        """Whether automatic evaluation is on: subsystem gate AND trigger gate.

        Subordinate by construction — a disabled subsystem has nothing to trigger,
        so ``ORCH_HUB_EVAL_AUTO`` only ever narrows ``ORCH_HUB_EVAL_ENABLED``.
        """
        return self._config.evaluation_enabled and self._config.eval_auto

    def on_task_completed(self, record: RunRecord) -> None:
        """Hook entry: this run just ended and classified ``completed``.

        Called from the run manager's live→ended transition, once per run, under
        its lock — so everything here is O(1) and the real work is handed to the
        executor. The gate is checked first so a hub with automatic evaluation off
        (or the subsystem off) does no work at all, not even a thread.

        The caller decides *what* completed, not this: it passes only runs whose
        own frozen run-dir evidence says the task finished. A run that merely
        exited 0 is not a completion.

        Refuses a record it cannot attribute — a stub from an unreadable
        ``run.json``, or one with no task id — rather than guessing a target;
        there is nothing to evaluate and an evaluation keyed by a wrong
        ``(repo, task)`` would be worse than none.
        """
        if not self.enabled:
            return
        repo = record.managed_repo()
        if repo is None or not record.task_id:
            _log.warning(
                "completed run %s carries no repo/task attribution;"
                " skipping automatic evaluation",
                record.run_id,
            )
            return
        task_id = record.task_id
        self._executor(lambda: self._evaluate(repo, task_id))

    def _evaluate(self, repo: ManagedRepo, task_id: str) -> None:
        """Run Layer 1 and enqueue Layer 2 for one completed task, off the lock.

        The two steps are independent and independently isolated. Layer 1 runs
        first because it is the layer that always happens (it makes no model call,
        so there is nothing to schedule and nothing to skip), but its outcome does
        **not** gate Layer 2: they are separate artifacts over separately generated
        evidence, so a transient Layer 1 failure — a git hiccup, an unreadable log
        — must not also cost the corpus its semantic report.

        Ordering note: Layer 2 is enqueued *after* Layer 1 returns rather than
        concurrently, so the two evidence builds do not run the same repo's git
        window and ``.ai/`` reads at once for no benefit. The semantic turn is the
        slow part and it is asynchronous either way.
        """
        self._run_static(repo, task_id)
        self._enqueue_semantic(repo, task_id)

    def _run_static(self, repo: ManagedRepo, task_id: str) -> None:
        """Generate the deterministic Layer 1 report; never raise.

        A fresh evidence pack plus ``static_metrics.json``, exactly what the manual
        ``/metrics`` route produces — the trigger adds no second generation path,
        it only removes the click. Read-only over target state by construction: the
        only writes are the two hub-owned artifacts, and a failure has already
        removed its own reserved dir.
        """
        try:
            report = self._static.generate_metrics(repo, task_id)
        except Exception as exc:  # noqa: BLE001 - isolation from run state
            _log.warning(
                "automatic static metrics failed for %s/%s (%s): %s",
                repo.name,
                task_id,
                type(exc).__name__,
                exc,
            )
            return
        _log.info(
            "automatic static metrics for %s/%s -> %s",
            repo.name,
            task_id,
            report.evaluation_id,
        )

    def _enqueue_semantic(self, repo: ManagedRepo, task_id: str) -> None:
        """Enqueue the Layer 2 job unless this task already has one; never raise.

        ``start_job_if_absent`` is both the single-flight guard and the
        once-per-task guard (see its docstring): a second completion signal for the
        same task — the close-out-only re-run, a rescan, a hub restart — finds the
        record and starts nothing. The job itself then runs asynchronously and
        settles to ``completed``/``failed`` on its own; a failure there is a
        recorded job outcome, not an exception here.
        """
        try:
            job = self._jobs.start_job_if_absent(repo, task_id)
        except Exception as exc:  # noqa: BLE001 - isolation from run state
            _log.warning(
                "automatic semantic job enqueue failed for %s/%s (%s): %s",
                repo.name,
                task_id,
                type(exc).__name__,
                exc,
            )
            return
        if job is None:
            _log.info(
                "automatic semantic evaluation skipped for %s/%s:"
                " the task already has a job record",
                repo.name,
                task_id,
            )
            return
        _log.info(
            "automatic semantic job %s queued for %s/%s",
            job.job_id,
            repo.name,
            task_id,
        )
