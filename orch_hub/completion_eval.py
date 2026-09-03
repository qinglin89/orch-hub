"""Completion-triggered task evaluation: Layer 1 automatically, Layer 2 never.

A run whose end classifies as ``completed`` gets its task measured mechanically,
with nobody deciding which completions deserve a report — but only as far as the
deterministic layer reaches. This module builds the evidence pack and the static
metrics; the semantic layer is started by a person asking for it
(:meth:`~orch_hub.semantic_jobs.SemanticJobManager.start_job`) and by nothing
else.

The split is not about cost. One Layer 2 turn is a bounded ``claude -p`` call,
and the corpus argument for evaluating every completion is a real one: evaluate
only the tasks that felt problematic and the resulting sample cannot support
recurrence or base-rate claims. What broke the automatic half is what it takes to
run a *model* turn on a schedule nobody is watching:

- **It contends with the work it measures.** The turn draws on the same
  subscription rate window the next dev session needs, and it fires whenever a
  run happens to finish — which is exactly when the operator is most likely to be
  starting the next one. Deferral moves the spend around inside the window
  without removing it from the window.
- **It needs a network the hub does not have.** Layer 1 is filesystem work and
  completes on a train; a semantic turn on an unreliable link fails, records a
  terminal ``failed``, and consumes the task's one automatic attempt. Automation
  that turns a flaky connection into a permanently unmeasured task is worse than
  no automation.
- **Its consumer never needed the population.** Evolution reads evaluated
  *samples* to find actionable improvements; it does not compute rates over the
  full corpus, so mechanical completeness bought nothing that a person asking for
  the reports that matter does not also buy. The machinery that existed to make
  the denominator honest — the durable once-per-task acceptance record, the
  capacity/spacing dispatch policy, the launch-time opt-out, the operator pause —
  was all in service of a guarantee nobody consumes.

Layer 1 keeps its automatic trigger for the mirror-image reasons: it makes no
model call and opens no socket, so there is nothing to schedule, nothing to
contend for, and nothing a dead network can turn into a lost attempt. Its
failure mode is a missing free artifact that the next completion regenerates.

Layer 1 is deliberately per completion signal rather than task-once. The signal
is per *run* — a task that reaches ``completed`` without being archived is still
launchable, and the close-out-only re-run that follows classifies ``completed``
again — and a repeat simply mints a fresher evaluation dir. There is no durable
guard here and none is needed: a free deterministic artifact regenerated twice
costs a directory, whereas the guard's only purpose was to stop a *model turn*
from running twice.

Isolation is the invariant, exactly like notification delivery and the usage
refresh hook: this runs after the ending run is already persisted, off the run
manager's lock and off its thread, and every failure is logged and dropped. A
missing repo, a broken evidence pack, an unreadable log — none of them may touch
run state, readiness, attention, or a launch/intake gate. Nothing here is durably
promised before the hook returns either: the whole work item is the artifact
itself, so a hub that stops mid-build loses a report the next completion (or one
click) rebuilds, not a record of something that was supposed to happen.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .config import Config
from .models import ManagedRepo, RunRecord
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
    evaluation is safe — its own all-or-nothing cleanup removes any partial
    artifact dir, and nothing else is pending on it.
    """
    threading.Thread(target=run, daemon=True, name="orch-hub-completion-eval").start()


class CompletionEvaluationTrigger:
    """Generates Layer 1 for a task when one of its runs ends ``completed``.

    Wired into the app's single :class:`~orch_hub.runs.RunManager` as a plain
    ``Callable[[RunRecord], None]`` hook, so the run manager keeps no evaluation
    import and no evaluation policy — the same seam shape as the display-only
    usage-refresh hook. Collaborators are injectable for tests:
    ``static_evaluator`` is Layer 1 and ``executor`` decides where it runs. It
    holds no reference to the semantic layer at all, which is the structural half
    of "no completion can start a model turn": there is nothing here to call.
    """

    def __init__(
        self,
        config: Config,
        *,
        static_evaluator: StaticEvaluator,
        executor: CompletionExecutor = _thread_executor,
    ):
        self._config = config
        self._static = static_evaluator
        self._executor = executor

    @property
    def enabled(self) -> bool:
        """Whether automatic evaluation is on: subsystem gate AND trigger gate.

        Subordinate by construction — a disabled subsystem has nothing to trigger,
        so ``ORCH_HUB_EVAL_AUTO`` only ever narrows ``ORCH_HUB_EVAL_ENABLED``.
        Derived once in :attr:`orch_hub.config.Config.auto_evaluation_enabled`.

        What it now gates is the deterministic layer alone. It is the switch for
        "a completed run measures its own task without being asked", not for any
        model spend: with automatic Layer 2 gone, no configuration value anywhere
        can cause a semantic turn.
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

        Everything past that goes to the executor. Nothing is written inline
        because nothing needs to be: the only output is the artifact the executor
        produces, so a hub that stops before it runs leaves no half-kept promise
        behind — just an unmeasured completion that the next run of the same task,
        or one click, measures.
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
        self._executor(lambda: self._run_static(repo, task_id))

    def _run_static(self, repo: ManagedRepo, task_id: str) -> None:
        """Generate the deterministic Layer 1 report off the lock; never raise.

        A fresh evidence pack plus ``static_metrics.json``, exactly what the manual
        ``/metrics`` route produces — the trigger adds no second generation path,
        it only removes the click. Read-only over target state by construction: the
        only writes are the two hub-owned artifacts, and a failure has already
        removed its own reserved dir.

        Interrupting this loses only a free, reproducible artifact.
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
