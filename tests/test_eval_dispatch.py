"""Tests for the semantic-job queue projection (:mod:`orch_hub.eval_dispatch`).

This module used to own the automatic Layer 2 dispatch policy — a capacity gate,
spacing between turns, a sweep that promoted held records — and this file used to
test it. All of that is gone (:mod:`orch_hub.completion_eval` says why), and what
is left is the read-only fold ``queue_snapshot`` takes over the persisted job
store: a complete census the row cap never shrinks, the two-group order, legacy
records reported as they rest, a read that moves nothing — and what the census is
*not*, pinned from a real completed-then-failed retry.

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
from orch_hub.models import (
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


def test_legacy_skipped_records_are_counted_but_never_listed(tmp_path):
    """A resting outcome nothing can produce any more: a figure, never a row.

    ``skipped`` was written when a completion's run had declined its Layer 2 at
    launch. Both the opt-out and the automatic path are gone, so every such record
    is a leftover — still worth counting, since a reader comparing this census
    against a repo's task list should see where the store's attempts went, but
    nothing to act on. One row per legacy skip would crowd out the jobs that
    actually need reading, and a hub that ran the automatic path holds one per task
    it accepted.
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


def test_the_retired_per_repo_skip_tally_is_gone(tmp_path):
    """It measured a thinning corpus under an opt-out that no longer exists.

    The tally answered "which repo is losing the most reports to the launch-time
    decline?" Nothing declines anything at launch now, so the figure could only
    ever describe history — and a standing per-repo breakdown of a frozen legacy
    set reads as a live pattern. The flat ``skipped`` count keeps the records
    visible without implying one.
    """
    snap = queue_snapshot(
        config(tmp_path),
        [job("a", STATUS_SKIPPED, repo_name="alpha"), job("b", STATUS_SKIPPED, repo_name="")],
        now=NOW,
    )

    assert snap.counts[STATUS_SKIPPED] == 2
    assert not hasattr(snap, "skipped_by_repo")
    assert "skipped_by_repo" not in snap.to_dict()


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

    The waiting group leads with the longest-open record, so the top row is the
    one that has been unanswered longest; the settled group leads with the newest
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
    """How long this attempt has been open, not how long its turn has run."""
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


def test_a_long_standing_record_is_reported_without_being_called_late(tmp_path):
    """No staleness flag, and no bound to measure one against.

    While a dispatcher drained the queue, a held record past
    ``ORCH_HUB_EVAL_QUEUE_MAX_AGE`` meant the drain had stopped. Nothing drains
    anything now, so a month-old legacy record is not evidence of a malfunction —
    it is a task nobody asked about. A warning that fires on every such record is
    a warning that means nothing, so the age is reported and left to the reader.
    """
    old = NOW - dt.timedelta(days=30)
    snap = queue_snapshot(
        config(tmp_path),
        [job("d", STATUS_DEFERRED, created=old), job("q", STATUS_QUEUED, created=old)],
        now=NOW,
    )

    assert [e.age_seconds for e in snap.entries] == [2592000, 2592000]
    assert not hasattr(snap, "overdue")
    assert not hasattr(snap.entries[0], "overdue")
    assert "overdue" not in snap.to_dict()
    assert "overdue" not in snap.to_dict()["entries"][0]
    assert "queue_max_age_seconds" not in snap.to_dict()


def test_the_projection_reports_no_switch_state_at_all(tmp_path):
    """Nothing holds these records, so there is no hold to explain.

    The shape carried the completion trigger's gate and the operator pause because
    a queue that moved by itself could be still for reasons no record explained. A
    turn starts only when somebody asks now, so every record is explained by the
    record — and ``ORCH_HUB_EVAL_AUTO`` switches automatic *Layer 1*, which has
    nothing to do with this store. Echoing it here would send a reader to the wrong
    switch.
    """
    snap = queue_snapshot(config(tmp_path, eval_auto=False), [job("a", STATUS_DEFERRED)], now=NOW)

    assert set(snap.to_dict()) == {"counts", "entries", "truncated"}
    assert not hasattr(snap, "auto_enabled")
    assert not hasattr(snap, "control")
    # The record is still reported: hiding it would answer "why is this sitting
    # here?" with the same silence that made the question necessary.
    assert [e.job.task_id for e in snap.entries] == ["a"]


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
    assert snap.counts[STATUS_DEFERRED] == 5  # the census sees all five
    assert [e.job.task_id for e in snap.entries] == ["t04", "t03"]  # oldest first


def test_an_uncapped_page_is_not_marked_truncated(tmp_path):
    snap = queue_snapshot(config(tmp_path), [job("a", STATUS_DEFERRED)], now=NOW)

    assert snap.truncated is False
    assert len(snap.entries) == 1


def test_the_fold_shows_the_same_records_whatever_the_gates_say(tmp_path):
    """A reader asking about a record must never be answered with an omission.

    The evaluation switches say nothing about this store — one gates automatic
    Layer 1, the other the whole subsystem's routes — so neither may filter what
    the projection returns. The route above it still refuses while the subsystem is
    disabled; that is the caller's gate, not the fold's.
    """
    records = [job("a", STATUS_DEFERRED)]
    for cfg in (
        config(tmp_path),
        config(tmp_path, eval_auto=False),
        config(tmp_path, evaluation_enabled=False),
    ):
        snap = queue_snapshot(cfg, records, now=NOW)
        assert [e.job.task_id for e in snap.entries] == ["a"]


def test_an_empty_store_is_a_calm_snapshot_not_an_error(tmp_path):
    snap = queue_snapshot(config(tmp_path), [], now=NOW)

    assert snap.entries == ()
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
