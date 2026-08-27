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
  inside the same subscription rate window a dev session needs. So enqueuing is
  *acceptance*, not admission: the record lands ``deferred`` and the dispatch
  policy (:mod:`orch_hub.eval_dispatch`) decides when the window can afford the
  turn. This module asks for the first sweep immediately, off the lock, so an
  uncontended completion still becomes a turn within milliseconds; the resident
  monitor's sweep is what retries a queue the policy is holding.

Accepting a completion is durable **before the hook returns**. The run manager
settles an end permanently — ``ended_ts`` is persisted and ``_refresh``
early-returns for that run forever after — so a completion this module has only
*promised* to evaluate (a closure handed to a daemon thread) is lost outright if
the hub stops before that closure reaches the job store: nothing on disk records
that the task ever reached the sidecar, and no reconciliation can find what was
never written. The Layer 2 job record is therefore written inline, before the
hook returns; only the slow part — Layer 1's evidence build — is detached. A hub
that dies after that record exists leaves an observable orphan the startup sweep
settles to ``failed``, which is a recorded outcome. A hub that dies before it
would have left a silent gap in the one corpus whose entire point is that it has
none.

Two gates and one guard:

- ``Config.evaluation_enabled`` is the subsystem gate (shared with every manual
  path) and ``Config.eval_auto`` is the trigger gate, so automatic evaluation
  can be switched off while manual generation stays available. They gate both
  halves of the automatic path, not just this one: the job records accepted here
  are durable, so :class:`~orch_hub.eval_dispatch.DeferredDispatcher` re-checks
  the same pair before promoting anything an earlier process left queued.
- The task-level once-guard is
  :meth:`~orch_hub.semantic_jobs.SemanticJobManager.enqueue_if_absent`. The
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
from .eval_dispatch import DeferredDispatcher
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
    is Layer 1, ``job_manager`` owns Layer 2's persisted job lifecycle,
    ``dispatcher`` owns when a queued Layer 2 turn may actually spend, and
    ``executor`` decides where the work runs.
    """

    def __init__(
        self,
        config: Config,
        *,
        static_evaluator: StaticEvaluator,
        job_manager: SemanticJobManager,
        dispatcher: DeferredDispatcher,
        executor: CompletionExecutor = _thread_executor,
    ):
        self._config = config
        self._static = static_evaluator
        self._jobs = job_manager
        self._dispatcher = dispatcher
        self._executor = executor

    @property
    def enabled(self) -> bool:
        """Whether automatic evaluation is on: subsystem gate AND trigger gate.

        Subordinate by construction — a disabled subsystem has nothing to trigger,
        so ``ORCH_HUB_EVAL_AUTO`` only ever narrows ``ORCH_HUB_EVAL_ENABLED``.

        Derived once in :attr:`orch_hub.config.Config.auto_evaluation_enabled` and
        read from there by both halves of the automatic path (acceptance here,
        dispatch in :meth:`orch_hub.eval_dispatch.DeferredDispatcher.sweep`): the
        queue is durable, so a gate honored at acceptance but not at dispatch is
        still a model turn in a hub that switched automatic evaluation off.
        """
        return self._config.auto_evaluation_enabled

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

        Then the ordering that matters: **Layer 2 is enqueued inline, Layer 1 is
        dispatched.** The completion signal is one-shot and already settled when
        this runs, so the automatic path has to accept it durably before returning
        (module docstring). Enqueuing is that acceptance and it is O(1) — one small
        record read plus one atomic write, under the job manager's briefly-held
        lock — the same class of work ``_refresh`` already does under its own.
        Layer 1 is not O(1): it walks the task file, every related run's
        records/events/sessions, a bounded git window and the ``.ai/`` snapshot, so
        it stays on the executor.

        The two steps are independent and independently isolated: they are separate
        artifacts over separately generated evidence, so an enqueue failure must
        not also cost the task its deterministic layer, and a Layer 1 failure — a
        git hiccup, an unreadable log — cannot reach an already-durable job. The
        consequence of enqueuing first is that the semantic turn (dispatched by the
        job manager onto its own thread) may now overlap this Layer 1 build rather
        than following it. Both are read-only and unrelated; a redundant concurrent
        evidence build costs less than a completion that is never recorded.

        The enqueued job lands ``deferred``, so a third step follows on the
        executor: ask the dispatcher to sweep. Whether this task's turn may spend
        now is a policy question over subscription usage — a file read that can
        trigger a background refresh — and it has no business on the run manager's
        lock. Asking here rather than waiting for the resident sweep is what keeps
        the uncontended case immediate; the sweep is global, so it may well start a
        different, older job than this one, which is the queue behaving correctly.
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
        self._enqueue_semantic(repo, task_id)
        self._executor(lambda: self._dispatch_and_measure(repo, task_id))

    def _dispatch_and_measure(self, repo: ManagedRepo, task_id: str) -> None:
        """The off-lock half: admit a Layer 2 turn if allowed, then build Layer 1.

        One executor submission rather than two, because the two steps share a
        thread happily and neither blocks: the sweep only *starts* a turn (the job
        manager detaches it onto its own executor), so putting it first costs
        Layer 1 nothing and lets the turn begin while the evidence build runs.

        Each half is isolated from the other. A dispatch policy that cannot read
        usage, or a job store that cannot be walked, must not cost the task its
        free deterministic layer; a failing Layer 1 must not stop the queue.
        """
        try:
            self._dispatcher.sweep()
        except Exception as exc:  # noqa: BLE001 - isolation from run state
            _log.warning(
                "automatic semantic dispatch sweep failed (%s): %s",
                type(exc).__name__,
                exc,
            )
        self._run_static(repo, task_id)

    def _run_static(self, repo: ManagedRepo, task_id: str) -> None:
        """Generate the deterministic Layer 1 report off the lock; never raise.

        A fresh evidence pack plus ``static_metrics.json``, exactly what the manual
        ``/metrics`` route produces — the trigger adds no second generation path,
        it only removes the click. Read-only over target state by construction: the
        only writes are the two hub-owned artifacts, and a failure has already
        removed its own reserved dir.

        Interrupting this loses only a free, reproducible artifact: the Layer 2 job
        is already durable when this runs, and its own turn regenerates a complete
        evidence + static pair.
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
        """Durably accept the completion as a Layer 2 job; never raise.

        ``enqueue_if_absent`` is both the single-flight guard and the
        once-per-task guard (see its docstring): a second completion signal for the
        same task — the close-out-only re-run, a rescan, a hub restart — finds the
        record and accepts nothing. The job then waits for the dispatch policy,
        runs asynchronously, and settles to ``completed``/``failed`` on its own; a
        failure there is a recorded job outcome, not an exception here.

        That same record is the only durable trace that the automatic path accepted
        this completion, which is why there is no startup replay to fall back on:
        a scan for "completed runs with no job record" cannot distinguish a
        completion this hub accepted from every task that completed before the
        trigger existed, so it would enqueue a model turn for the entire archived
        backlog on the first restart after deployment — the backfill this task
        deliberately leaves to its own resumable, capacity-aware work. Writing the
        record inline instead needs no second mechanism: it is already the guard.

        Called on the run manager's thread, under its lock, so it must stay O(1);
        the slow layer is dispatched, not this one. Isolated all the same — a
        broken job store leaves the run untouched and costs this task its automatic
        Layer 2, not the hub's run control.
        """
        try:
            job = self._jobs.enqueue_if_absent(repo, task_id)
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
            "automatic semantic job %s accepted for %s/%s (awaiting dispatch)",
            job.job_id,
            repo.name,
            task_id,
        )
