"""Tests for the semantic-job queue projection (:mod:`orch_hub.eval_dispatch`).

This module used to own the automatic Layer 2 dispatch policy — a capacity gate,
spacing between turns, a sweep that promoted held records — and this file used to
test it. All of that is gone (:mod:`orch_hub.completion_eval` says why), and what
is left is the read-only fold ``queue_snapshot`` takes over the persisted job
store: a complete census the row cap never shrinks, the two-group order, the
overdue flag over legacy held records, a read that moves nothing — and what the
census is *not*, pinned from a real completed-then-failed retry.

Nothing here can start a turn, which is now a property of the module rather than
of the tests: there is no dispatcher left to drive.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.evaluation import EvaluationService
from orch_hub.semantic import (
    SEMANTIC_END,
    SEMANTIC_START,
    SemanticEvaluationError,
    SemanticEvaluator,
)
from orch_hub.subprocess_utils import CommandResult
from orch_hub.eval_dispatch import queue_snapshot
from orch_hub.eval_control import PAUSE_OPERATOR, EvaluationControlStore
from orch_hub.models import (
    EvaluationControl,
    ManagedRepo,
    SemanticJob,
    SemanticEvaluatorInfo,
    SemanticReport,
)
from orch_hub.semantic_jobs import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DEFERRED,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    SemanticJobManager,
)

TASK = "2026-08-09-completion-triggered-evaluation"
REPO_PATH = Path("/repos/alpha")

NOW = dt.datetime(2026, 8, 9, 12, 0, 0, tzinfo=dt.timezone.utc)


# -- helpers ------------------------------------------------------------------


def repo(name: str = "alpha", path: Path = REPO_PATH) -> ManagedRepo:
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


class Clock:
    def __init__(self, now: dt.datetime = NOW):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)


def config(tmp_path: Path, **overrides) -> Config:
    return Config(
        mandrel_path=tmp_path / "mandrel",
        data_dir=tmp_path / "hub-data",
        **overrides,
    )


def mk_report(evaluation_id: str) -> SemanticReport:
    return SemanticReport(
        evaluation_id=evaluation_id,
        generated_ts=NOW.isoformat(),
        repo_name="alpha",
        task_id=TASK,
        target_present=True,
        target_source="archive",
        evaluator=SemanticEvaluatorInfo(backend="fake", model="fake-model", schema_version="1"),
        summary="ok",
        overall_confidence=0.7,
    )


class FlippingBackend:
    """A real semantic backend seam that succeeds, then fails when told to.

    The two-attempt shape one test needs: a first turn that produces a genuine
    persisted report, and a retry that fails the way a broken CLI does. Only the
    ``(info, evaluate, doctor)`` seam, so the evaluation/static/persistence chain
    above it is the real one.
    """

    def __init__(self):
        self.fail = False

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return SemanticEvaluatorInfo(
            backend="claude-cli", model="fake-model", schema_version="1"
        )

    def evaluate(self, prompt: str, r: ManagedRepo, *, cancel=None) -> str:
        if self.fail:
            raise SemanticEvaluationError("claude backend exited 1")
        body = json.dumps(
            {
                "summary": "clean handoff",
                "confidence": 0.8,
                "scores": [
                    {
                        "dimension": "handoff_completeness",
                        "score": 0.9,
                        "confidence": 0.8,
                        "rationale": "log complete",
                    }
                ],
                "findings": [],
                "improvements": [],
            }
        )
        return f"{SEMANTIC_START}\n{body}\n{SEMANTIC_END}"

    def doctor(self):  # pragma: no cover - not exercised here
        raise NotImplementedError


def _git_unavailable(args, **kwargs) -> CommandResult:
    """No git in the fixture tree; the pack degrades to best-effort, as designed."""
    return CommandResult(tuple(str(a) for a in args), 128, "", "")


def sync_executor(run) -> None:
    run()


# -- the operator view --------------------------------------------------------
#
# `queue_snapshot`'s projection. Most of it runs on hand-built records, since the
# fold is pure; the last test drives the real manager and evaluator instead,
# because what it pins is a divergence only the real overwrite produces — the
# census counts each task's latest *attempt*, which is not the set of tasks
# carrying a report.


def job(
    task_id: str,
    status: str,
    *,
    created: dt.datetime | None = None,
    updated: dt.datetime | None = None,
    repo_name: str = "alpha",
    **fields,
) -> SemanticJob:
    """One persisted-shaped record, without going through the manager."""
    return SemanticJob(
        job_id="J-" + task_id,
        repo_name=repo_name,
        repo_path=REPO_PATH,
        task_id=task_id,
        status=status,
        created_ts=(created or NOW).isoformat(),
        updated_ts=(updated or created or NOW).isoformat(),
        **fields,
    )


def test_the_census_counts_every_status_including_the_zeroes(tmp_path):
    """A count keyed only on observed statuses hides the figures worth stating.

    "Nothing is held" and "nothing failed" are the reassuring readings; if they
    are simply absent from the payload a reader cannot tell them from a surface
    that forgot to look.
    """
    snap = queue_snapshot(
        config(tmp_path),
        [job("a", STATUS_COMPLETED), job("b", STATUS_COMPLETED), job("c", STATUS_DEFERRED)],
        now=NOW,
    )

    assert snap.counts == {
        STATUS_DEFERRED: 1,
        STATUS_QUEUED: 0,
        STATUS_RUNNING: 0,
        STATUS_COMPLETED: 2,
        STATUS_FAILED: 0,
        STATUS_CANCELLED: 0,
        STATUS_SKIPPED: 0,
        STATUS_ERROR: 0,
    }


def test_a_skipped_task_is_counted_and_broken_down_per_repo(tmp_path):
    """The count is what makes the launch-time opt-out admissible at all.

    An opt-out whose entries left no countable trace would hand back, at
    production time, exactly the selection automatic evaluation exists to remove.
    Per repo because that is the shape a thinning corpus has — skips cluster on the
    expensive, remediation-heavy tail rather than spreading evenly.
    """
    snap = queue_snapshot(
        config(tmp_path),
        [
            job("a", STATUS_SKIPPED, repo_name="alpha"),
            job("b", STATUS_SKIPPED, repo_name="beta"),
            job("c", STATUS_SKIPPED, repo_name="beta"),
            job("d", STATUS_COMPLETED, repo_name="beta"),
        ],
        now=NOW,
    )

    assert snap.counts[STATUS_SKIPPED] == 3
    # Most-skipped first, so the repo losing the most reports leads.
    assert list(snap.skipped_by_repo.items()) == [("beta", 2), ("alpha", 1)]


def test_skipped_records_stay_out_of_the_actionable_rows(tmp_path):
    """A deliberate resting outcome has nothing to act on, and there can be many.

    One row per opted-out task would crowd out the jobs that are genuinely stuck —
    the question this list exists to answer.
    """
    snap = queue_snapshot(
        config(tmp_path),
        [job(f"s{i}", STATUS_SKIPPED) for i in range(30)]
        + [job("held", STATUS_DEFERRED)],
        now=NOW,
    )

    assert [e.job.task_id for e in snap.entries] == ["held"]
    assert snap.truncated is False  # the skips are not a truncated page, they are counted
    assert snap.counts[STATUS_SKIPPED] == 30


def test_a_store_with_no_skips_states_nothing_per_repo(tmp_path):
    """Only repos holding a skipped record appear: a zero row would be an invention.

    The job store knows nothing about repos it has no record for, so it cannot
    honestly render "0 skipped" for them.
    """
    snap = queue_snapshot(
        config(tmp_path), [job("a", STATUS_COMPLETED), job("b", STATUS_DEFERRED)], now=NOW
    )

    assert snap.skipped_by_repo == {}
    assert snap.counts[STATUS_SKIPPED] == 0  # the census still states the zero


def test_a_skip_with_no_repo_attribution_is_still_counted(tmp_path):
    """An unreadable record must not shrink the tally silently."""
    snap = queue_snapshot(config(tmp_path), [job("a", STATUS_SKIPPED, repo_name="")], now=NOW)

    assert snap.skipped_by_repo == {"(unattributed)": 1}


def test_an_unknown_status_is_counted_rather_than_dropped(tmp_path):
    """A hand-edited record must not shrink the census silently."""
    snap = queue_snapshot(config(tmp_path), [job("a", "improvised")], now=NOW)

    assert snap.counts["improvised"] == 1
    assert len(snap.entries) == 1


def test_completed_jobs_are_counted_but_not_listed(tmp_path):
    """The healthy outcome is a figure, not a row; rows are what may need action."""
    snap = queue_snapshot(
        config(tmp_path), [job("a", STATUS_COMPLETED), job("b", STATUS_FAILED)], now=NOW
    )

    assert snap.counts[STATUS_COMPLETED] == 1
    assert [e.job.task_id for e in snap.entries] == ["b"]


def test_waiting_jobs_lead_oldest_first_then_failures_newest_first(tmp_path):
    """Two groups with opposite senses: a queue, then a log.

    The waiting group is shown in the dispatcher's own promotion order, so the
    top row is what actually runs next; the settled group leads with the newest
    failure, which is the one still worth reading.
    """
    older = NOW - dt.timedelta(hours=3)
    newer = NOW - dt.timedelta(hours=1)
    snap = queue_snapshot(
        config(tmp_path),
        [
            job("fail-old", STATUS_FAILED, created=older, updated=older),
            job("held-new", STATUS_DEFERRED, created=newer),
            job("running", STATUS_RUNNING, created=newer),
            job("fail-new", STATUS_FAILED, created=older, updated=newer),
            job("held-old", STATUS_DEFERRED, created=older),
        ],
        now=NOW,
    )

    assert [e.job.task_id for e in snap.entries] == [
        "held-old",
        "held-new",
        "running",
        "fail-new",
        "fail-old",
    ]


def test_the_age_is_measured_from_acceptance_not_from_a_turn(tmp_path):
    """How long the task has gone unmeasured, which is the corpus's question."""
    snap = queue_snapshot(
        config(tmp_path),
        [job("a", STATUS_DEFERRED, created=NOW - dt.timedelta(hours=2))],
        now=NOW,
    )

    assert snap.entries[0].age_seconds == 7200


def test_an_unparseable_stamp_reads_as_unknown_age_not_zero(tmp_path):
    record = SemanticJob(
        job_id="J",
        repo_name="alpha",
        repo_path=REPO_PATH,
        task_id="a",
        status=STATUS_DEFERRED,
        created_ts="not-a-timestamp",
    )
    snap = queue_snapshot(config(tmp_path), [record], now=NOW)

    assert snap.entries[0].age_seconds is None
    assert snap.entries[0].overdue is False
    assert snap.overdue == 0


def test_the_overdue_flag_comes_from_the_configured_bound(tmp_path):
    """The threshold is configuration, and the flag is measured against it."""
    cfg = config(tmp_path, eval_queue_max_age=3600)
    records = [
        job("young", STATUS_DEFERRED, created=NOW - dt.timedelta(seconds=3599)),
        job("old", STATUS_DEFERRED, created=NOW - dt.timedelta(seconds=3600)),
    ]

    snap = queue_snapshot(cfg, records, now=NOW)

    assert snap.queue_max_age_seconds == 3600
    assert snap.overdue == 1
    assert {e.job.task_id: e.overdue for e in snap.entries} == {
        "young": False,
        "old": True,
    }


def test_a_disabled_bound_flags_nothing(tmp_path):
    """``0`` disables the bound, per the config contract — not "everything is overdue"."""
    cfg = config(tmp_path, eval_queue_max_age=0)
    snap = queue_snapshot(
        cfg, [job("a", STATUS_DEFERRED, created=NOW - dt.timedelta(days=30))], now=NOW
    )

    assert snap.overdue == 0
    assert snap.entries[0].overdue is False


def test_only_held_jobs_can_be_overdue(tmp_path):
    """The bound has exactly one meaning, shared with the dispatcher's own warning.

    A hung ``queued``/``running`` turn is a different failure with its own
    recovery (restart reconciliation settles it), so it renders its age without
    borrowing the stalled-queue flag.
    """
    cfg = config(tmp_path, eval_queue_max_age=60)
    stale = NOW - dt.timedelta(days=1)
    snap = queue_snapshot(
        cfg,
        [
            job("q", STATUS_QUEUED, created=stale),
            job("r", STATUS_RUNNING, created=stale),
            job("f", STATUS_FAILED, created=stale),
            job("d", STATUS_DEFERRED, created=stale),
        ],
        now=NOW,
    )

    assert snap.overdue == 1
    assert {e.job.task_id for e in snap.entries if e.overdue} == {"d"}


def test_the_persisted_pause_reaches_a_reader_through_the_snapshot(tmp_path):
    """The pause store outlived the drain it held; the projection still reports it.

    Nothing consults it to decide whether a turn may start — nothing decides that
    any more — but it is persisted operator-set state, and a reader that showed the
    queue without it would be describing a store it only half looked at.
    """
    control = EvaluationControl(
        paused=True, paused_ts="2026-08-09T11:00:00+00:00", reason=PAUSE_OPERATOR
    )

    snap = queue_snapshot(
        config(tmp_path), [job("a", STATUS_DEFERRED)], control=control, now=NOW
    )

    assert snap.control == control
    assert snap.to_dict()["control"] == {
        "paused": True,
        "paused_ts": "2026-08-09T11:00:00+00:00",
        "reason": PAUSE_OPERATOR,
    }
    # The held record is still reported: hiding it would answer "why is this
    # sitting here?" with the same silence that made the question necessary.
    assert len(snap.entries) == 1


def test_a_stored_pause_is_read_as_a_value_the_caller_supplies(tmp_path):
    """The projection stays pure: the store is the caller's to open, not this fold's."""
    store = EvaluationControlStore(config(tmp_path).data_dir)
    store.pause()

    snap = queue_snapshot(config(tmp_path), [], control=store.state(), now=NOW)

    assert snap.control.paused is True and snap.control.reason == PAUSE_OPERATOR


def test_both_holds_are_reported_independently(tmp_path):
    """The automatic switch and the pause are separate facts and travel separately."""
    snap = queue_snapshot(
        config(tmp_path, eval_auto=False),
        [job("a", STATUS_DEFERRED)],
        control=EvaluationControl(paused=True, reason=PAUSE_OPERATOR),
        now=NOW,
    )

    assert snap.auto_enabled is False
    assert snap.control.paused is True


def test_a_snapshot_with_no_control_supplied_reads_as_running(tmp_path):
    snap = queue_snapshot(config(tmp_path), [], now=NOW)

    assert snap.control == EvaluationControl()
    assert snap.to_dict()["control"]["paused"] is False


def test_a_cancelled_record_is_counted_but_never_listed_as_actionable(tmp_path):
    """A resting, deliberate outcome — the same treatment ``skipped`` gets. The
    count is what keeps a deliberate gap in the corpus answerable; a row would only
    crowd out the jobs that are genuinely stuck."""
    snap = queue_snapshot(
        config(tmp_path),
        [job("a", STATUS_CANCELLED), job("b", STATUS_DEFERRED)],
        now=NOW,
    )

    assert snap.counts[STATUS_CANCELLED] == 1
    assert [e.job.task_id for e in snap.entries] == ["b"]
    # And it is not folded into the skip tally: a task nobody declined at launch.
    assert snap.skipped_by_repo == {}


def test_the_entry_cap_never_shrinks_the_counts(tmp_path):
    """The cap bounds the page, never the census — a count that quietly meant "the
    first twenty" would report a twenty-deep queue as calm."""
    records = [
        job(f"t{i:02d}", STATUS_DEFERRED, created=NOW - dt.timedelta(days=1, seconds=i))
        for i in range(5)
    ]

    snap = queue_snapshot(config(tmp_path), records, now=NOW, limit=2)

    assert len(snap.entries) == 2
    assert snap.truncated is True
    assert snap.counts[STATUS_DEFERRED] == 5
    assert snap.overdue == 5  # counted over every held job, not just the shown ones
    assert [e.job.task_id for e in snap.entries] == ["t04", "t03"]  # oldest first


def test_an_uncapped_page_is_not_marked_truncated(tmp_path):
    snap = queue_snapshot(config(tmp_path), [job("a", STATUS_DEFERRED)], now=NOW)

    assert snap.truncated is False
    assert len(snap.entries) == 1


def test_the_view_reports_a_closed_gate_rather_than_hiding_the_queue(tmp_path):
    """A reader asking why nothing drains must see the held records AND the switch.

    A closed gate leaves no trace on the records by design (it is global config,
    not a per-job fact), so it can only be explained from the config echo — and
    hiding the queue while the gate is closed would answer the question with the
    same silence that prompted it.
    """
    records = [job("a", STATUS_DEFERRED)]
    for cfg in (
        config(tmp_path, eval_auto=False),
        config(tmp_path, evaluation_enabled=False),
    ):
        snap = queue_snapshot(cfg, records, now=NOW)
        assert snap.auto_enabled is False
        assert [e.job.task_id for e in snap.entries] == ["a"]

    assert queue_snapshot(config(tmp_path), records, now=NOW).auto_enabled is True


def test_an_empty_store_is_a_calm_snapshot_not_an_error(tmp_path):
    snap = queue_snapshot(config(tmp_path), [], now=NOW)

    assert snap.entries == ()
    assert snap.overdue == 0
    assert snap.truncated is False
    assert set(snap.counts.values()) == {0}


def test_the_snapshot_reads_the_store_without_moving_it(tmp_path):
    """Asking about the queue must never start, settle, or rewrite a record."""
    cfg = config(tmp_path)
    clock = Clock()
    held: list = []
    backend = FlippingBackend()
    service = EvaluationService(cfg, git_runner=_git_unavailable, clock=clock)
    jobs = SemanticJobManager(
        cfg,
        evaluator=SemanticEvaluator(
            cfg, evaluation_service=service, backend=backend, clock=clock
        ),
        clock=clock,
        executor=held.append,  # the turn is never run, so the record rests queued
    )
    jobs.start_job(repo(), TASK)
    path = jobs.jobs_dir / "alpha" / TASK / "job.json"
    before = path.read_bytes()

    snap = queue_snapshot(cfg, jobs.list_jobs(), now=clock.now)

    assert [e.job.status for e in snap.entries] == [STATUS_QUEUED]
    assert path.read_bytes() == before
    assert len(held) == 1  # still pending; reading the queue did not run it


def test_a_report_outlives_the_record_that_produced_it(tmp_path):
    """The census counts attempts, not reports, and the two really do diverge.

    One real evaluator, two attempts: the first completes and persists a report,
    then a manual retry hits a failing backend. The task's single job record is
    overwritten (``start_job`` does that deliberately — an operator asking again
    is a fresh attempt), so the census reads ``0 completed · 1 failed`` while the
    first attempt's ``semantic_report.json`` is still sitting in that task's
    retained evaluation history, reachable from its evaluation view and, once the
    task is archived, from the report feed.

    Both facts are correct and they are about different things: the queue's latest
    attempt failed, and the corpus holds a report for this task. Reading the
    ``completed`` figure as "tasks carrying a report" conflates them and makes the
    displayed number shrink and regrow with retry state, which is why nothing in
    the payload, the docstrings, or the dashboard label claims it — see
    :func:`orch_hub.eval_dispatch.queue_snapshot`. This test is the pin on that
    separation, from the real overwrite rather than a hand-built record.
    """
    cfg = config(tmp_path)
    target = tmp_path / "repos" / "alpha"
    (target / ".ai-tasks").mkdir(parents=True)
    (target / ".ai-tasks" / f"{TASK}.md").write_text(
        f"---\nid: {TASK}\nstatus: in_progress\nsession-est: 1/2\nblockers: []\n---\n\n"
        "# Census honesty\n\n## Goal\ngo\n\n## Scope\n- x\n\n## Acceptance\n- y\n\n"
        "## Session log\n\n### 2026-08-09 / sid-a / (pending → in_progress)\n"
        "- Done: work\n- Next: more\n- Open: none\n",
        encoding="utf-8",
    )
    (target / ".ai-tasks" / "index.md").write_text("# Active tasks\n", encoding="utf-8")
    target_repo = ManagedRepo(
        name="alpha", path=target, manifest=target / ".ai-deploy-manifest.json"
    )
    clock = Clock()
    backend = FlippingBackend()
    service = EvaluationService(cfg, git_runner=_git_unavailable, clock=clock)
    jobs = SemanticJobManager(
        cfg,
        evaluator=SemanticEvaluator(
            cfg, evaluation_service=service, backend=backend, clock=clock
        ),
        clock=clock,
        executor=sync_executor,
    )

    jobs.start_job(target_repo, TASK)
    completed = jobs.get_job(target_repo, TASK)
    assert completed.status == STATUS_COMPLETED
    report = (
        cfg.data_dir
        / "evaluations"
        / "alpha"
        / TASK
        / completed.evaluation_id
        / "semantic_report.json"
    )
    assert report.is_file()

    backend.fail = True
    clock.advance(3600)
    jobs.start_job(target_repo, TASK)
    retried = jobs.get_job(target_repo, TASK)

    assert retried.status == STATUS_FAILED
    # One record per task, so the completed attempt is gone from the store...
    snap = queue_snapshot(cfg, jobs.list_jobs(), now=clock.now)
    assert snap.counts[STATUS_COMPLETED] == 0
    assert snap.counts[STATUS_FAILED] == 1
    assert [e.job.status for e in snap.entries] == [STATUS_FAILED]
    # ...while the report that attempt produced is untouched. The failed retry's
    # cleanup removes only its own reserved dir, never the task's history.
    assert report.is_file()
