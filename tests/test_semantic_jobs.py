"""Tests for the async semantic-evaluation job manager (:mod:`orch_hub.semantic_jobs`).

Covers the queued→running→terminal lifecycle over an injected fake evaluator:
start persists ``queued`` and settles to ``completed`` (linking the fresh
``evaluation_id`` and stamping the evaluator identity) or ``failed`` (with a
bounded diagnostic and no ``evaluation_id``); the duplicate guard reuses an
in-flight job for the same ``(repo, task)`` instead of launching a second model
turn; a terminal job is overwritten by a fresh attempt; state reloads from disk
across manager instances; orphaned transient jobs are reconciled to ``failed`` at
startup; unsafe task ids fail closed; a corrupt record surfaces as ``error``
without holding the duplicate slot; and the manager writes only under the
hub-owned ``semantic-jobs/`` subtree. One integration test drives a *real*
:class:`~orch_hub.semantic.SemanticEvaluator` (fake backend) through the manager
to confirm the completed job links a real evaluation dir and the target repo is
untouched. The protected HTTP surfaces live in ``tests/test_api.py``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.evaluation import EvaluationService
from orch_hub.models import ManagedRepo, SemanticEvaluatorInfo, SemanticReport
from orch_hub.semantic import SemanticEvaluationError, SemanticEvaluator
from orch_hub.semantic_jobs import (
    SEMANTIC_JOB_NAME,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    SemanticJobManager,
    UnknownSemanticJobError,
    _ERROR_MAX_CHARS,
)
from orch_hub.subprocess_utils import CommandResult

TASK = "2026-07-23-semantic-evaluation-async-status"
REPO_PATH = Path("/repos/alpha")
FIXED_TS = dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=dt.timezone.utc)


# -- helpers ------------------------------------------------------------------


def repo(name: str = "alpha", path: Path = REPO_PATH) -> ManagedRepo:
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def mk_info(backend: str = "fake", model: str | None = "fake-model") -> SemanticEvaluatorInfo:
    return SemanticEvaluatorInfo(backend=backend, model=model, schema_version="1")


def mk_report(evaluation_id: str = "20260723T120000Z", *, info: SemanticEvaluatorInfo | None = None) -> SemanticReport:
    return SemanticReport(
        evaluation_id=evaluation_id,
        generated_ts="2026-07-23T12:00:00+00:00",
        repo_name="alpha",
        task_id=TASK,
        target_present=True,
        target_source="active",
        evaluator=info or mk_info(),
        summary="ok",
        overall_confidence=0.7,
    )


class Clock:
    """A mutable UTC clock so a second attempt can carry a distinct stamp."""

    def __init__(self, now: dt.datetime = FIXED_TS):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)


class FakeEvaluator:
    """Stands in for :class:`SemanticEvaluator`: the (evaluator_info, evaluate) seam.

    ``on_evaluate`` is a spy hook invoked inside ``evaluate`` (off the manager
    lock) so a test can observe the on-disk ``running`` state mid-turn.
    """

    def __init__(self, *, report: SemanticReport | None = None, error: Exception | None = None,
                 on_evaluate=None, info: SemanticEvaluatorInfo | None = None):
        self._report = report if report is not None else mk_report()
        self._error = error
        self._on_evaluate = on_evaluate
        self._info = info or mk_info()
        self.calls: list[tuple[ManagedRepo, str]] = []

    @property
    def evaluator_info(self) -> SemanticEvaluatorInfo:
        return self._info

    def evaluate(self, r: ManagedRepo, task_id: str) -> SemanticReport:
        self.calls.append((r, task_id))
        if self._on_evaluate is not None:
            self._on_evaluate(r, task_id)
        if self._error is not None:
            raise self._error
        return self._report


class DeferredExecutor:
    """Captures submitted turns without running them, so a job stays ``queued``."""

    def __init__(self):
        self.pending: list = []

    def __call__(self, run) -> None:
        self.pending.append(run)

    def run_all(self) -> None:
        while self.pending:
            self.pending.pop(0)()


def sync_executor(run) -> None:
    run()


def config(tmp_path: Path, *, enabled: bool = True) -> Config:
    return Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=enabled,
    )


def manager(tmp_path: Path, *, evaluator=None, clock=None, executor=sync_executor) -> SemanticJobManager:
    return SemanticJobManager(
        config(tmp_path),
        evaluator=evaluator if evaluator is not None else FakeEvaluator(),
        clock=clock or Clock(),
        executor=executor,
    )


def read_job(cfg: Config, repo_name: str, task_id: str) -> dict:
    path = cfg.data_dir / "semantic-jobs" / repo_name / task_id / SEMANTIC_JOB_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# -- lifecycle: start / running / completed -----------------------------------


def test_start_returns_queued_and_settles_completed(tmp_path):
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-42"))
    mgr = manager(tmp_path, evaluator=ev)

    returned = mgr.start_job(repo(), TASK)

    # The immediate return is the transient queued record...
    assert returned.status == STATUS_QUEUED
    assert returned.task_id == TASK and returned.repo_name == "alpha"
    # ...and the synchronous executor has already settled it on disk to completed.
    settled = mgr.get_job(repo(), TASK)
    assert settled.status == STATUS_COMPLETED
    assert settled.evaluation_id == "EVAL-42"
    assert ev.calls == [(repo(), TASK)]  # exactly one evaluation turn


def test_queued_job_stamps_evaluator_identity_before_any_report(tmp_path):
    # The queued record names the backend/model so the UI can show what is running
    # before a report exists — stamped from evaluator_info at creation.
    ev = FakeEvaluator(info=mk_info(backend="claude-cli", model="claude-opus-4-8"))
    mgr = manager(tmp_path, evaluator=ev, executor=DeferredExecutor())

    job = mgr.start_job(repo(), TASK)

    assert job.status == STATUS_QUEUED
    assert (job.backend, job.model, job.schema_version) == ("claude-cli", "claude-opus-4-8", "1")


def test_completed_job_reaffirms_identity_from_report_and_sets_timestamps(tmp_path):
    ev = FakeEvaluator(report=mk_report(info=mk_info(backend="claude-cli", model="claude-opus-4-8")))
    clock = Clock()
    mgr = manager(tmp_path, evaluator=ev, clock=clock)

    mgr.start_job(repo(), TASK)
    done = mgr.get_job(repo(), TASK)

    assert done.status == STATUS_COMPLETED
    assert (done.backend, done.model) == ("claude-cli", "claude-opus-4-8")
    # Lifecycle timestamps are all set on a completed run (fixed clock → equal here).
    assert done.created_ts and done.started_ts and done.ended_ts and done.updated_ts


def test_running_state_is_observable_during_the_turn(tmp_path):
    observed: dict = {}
    mgr_ref: dict = {}

    def spy(r, task_id):
        observed["job"] = mgr_ref["mgr"].get_job(r, task_id)

    ev = FakeEvaluator(on_evaluate=spy)
    mgr = manager(tmp_path, evaluator=ev)
    mgr_ref["mgr"] = mgr

    mgr.start_job(repo(), TASK)

    # Mid-turn (spy runs inside evaluate, off the lock) the persisted state is running.
    assert observed["job"].status == STATUS_RUNNING
    assert observed["job"].started_ts is not None and observed["job"].ended_ts is None
    # And it settles to completed afterwards.
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED


# -- duplicate guard ----------------------------------------------------------


def test_duplicate_while_queued_reuses_job_and_runs_once(tmp_path):
    ev = FakeEvaluator()
    executor = DeferredExecutor()
    mgr = manager(tmp_path, evaluator=ev, executor=executor)

    first = mgr.start_job(repo(), TASK)
    second = mgr.start_job(repo(), TASK)  # still queued: dedup, no second submit

    assert second.job_id == first.job_id
    assert len(executor.pending) == 1  # only the first attempt was submitted
    assert ev.calls == []  # deferred: nothing ran yet

    executor.run_all()
    assert ev.calls == [(repo(), TASK)]  # exactly one model turn overall
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED


def test_duplicate_while_running_returns_same_job_no_second_turn(tmp_path):
    mgr_ref: dict = {}
    seen: dict = {}

    def spy(r, task_id):
        # Re-entrant start while this turn is running (lock is free here): dedup.
        seen["reused"] = mgr_ref["mgr"].start_job(r, task_id)

    ev = FakeEvaluator(on_evaluate=spy)
    mgr = manager(tmp_path, evaluator=ev)
    mgr_ref["mgr"] = mgr

    started = mgr.start_job(repo(), TASK)

    assert seen["reused"].job_id == started.job_id
    assert seen["reused"].status == STATUS_RUNNING
    assert len(ev.calls) == 1  # the re-entrant start launched no second turn


def test_different_tasks_get_independent_jobs(tmp_path):
    ev = FakeEvaluator()
    mgr = manager(tmp_path, evaluator=ev, executor=DeferredExecutor())
    other = "2026-07-23-other-task"

    j1 = mgr.start_job(repo(), TASK)
    j2 = mgr.start_job(repo(), other)  # different task: not deduped

    assert j1.task_id == TASK and j2.task_id == other
    assert (mgr._config.data_dir / "semantic-jobs" / "alpha" / TASK / SEMANTIC_JOB_NAME).is_file()
    assert (mgr._config.data_dir / "semantic-jobs" / "alpha" / other / SEMANTIC_JOB_NAME).is_file()


def test_new_attempt_after_terminal_overwrites_single_record(tmp_path):
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-1"))
    clock = Clock()
    mgr = manager(tmp_path, evaluator=ev, clock=clock)

    mgr.start_job(repo(), TASK)
    first = mgr.get_job(repo(), TASK)
    assert first.status == STATUS_COMPLETED and first.evaluation_id == "EVAL-1"

    # A fresh attempt is legal now the prior one is terminal; advance the clock so
    # the new attempt carries a distinct id, and hand back a new evaluation id.
    clock.advance(60)
    ev2 = FakeEvaluator(report=mk_report(evaluation_id="EVAL-2"))
    mgr2 = SemanticJobManager(mgr._config, evaluator=ev2, clock=clock, executor=sync_executor)
    mgr2.start_job(repo(), TASK)
    second = mgr2.get_job(repo(), TASK)

    assert second.status == STATUS_COMPLETED and second.evaluation_id == "EVAL-2"
    assert second.job_id != first.job_id
    # Still exactly one record dir for this (repo, task) — the attempt overwrote it.
    task_dir = mgr._config.data_dir / "semantic-jobs" / "alpha" / TASK
    assert [p.name for p in task_dir.iterdir()] == [SEMANTIC_JOB_NAME]


# -- failure paths ------------------------------------------------------------


def test_failure_records_bounded_diagnostic_and_no_evaluation_id(tmp_path):
    ev = FakeEvaluator(error=SemanticEvaluationError("model process crashed"), info=mk_info(backend="claude-cli"))
    mgr = manager(tmp_path, evaluator=ev)

    mgr.start_job(repo(), TASK)
    failed = mgr.get_job(repo(), TASK)

    assert failed.status == STATUS_FAILED
    assert failed.evaluation_id is None
    assert "model process crashed" in (failed.error or "")
    # Identity stamped at creation is preserved on failure (which model was tried).
    assert failed.backend == "claude-cli"
    assert failed.ended_ts is not None


def test_failure_diagnostic_is_length_bounded(tmp_path):
    huge = "x" * (_ERROR_MAX_CHARS * 3)
    ev = FakeEvaluator(error=RuntimeError(huge))
    mgr = manager(tmp_path, evaluator=ev)

    mgr.start_job(repo(), TASK)
    failed = mgr.get_job(repo(), TASK)

    assert failed.status == STATUS_FAILED
    assert len(failed.error) <= _ERROR_MAX_CHARS  # truncated, never unbounded output


def test_retry_after_failure_starts_fresh_attempt(tmp_path):
    clock = Clock()
    failing = FakeEvaluator(error=SemanticEvaluationError("boom"))
    mgr = SemanticJobManager(config(tmp_path), evaluator=failing, clock=clock, executor=sync_executor)
    mgr.start_job(repo(), TASK)
    assert mgr.get_job(repo(), TASK).status == STATUS_FAILED

    # A retry (the scope's retry path) is just a fresh start over the terminal job.
    clock.advance(30)
    ok = FakeEvaluator(report=mk_report(evaluation_id="EVAL-OK"))
    mgr2 = SemanticJobManager(mgr._config, evaluator=ok, clock=clock, executor=sync_executor)
    mgr2.start_job(repo(), TASK)
    assert mgr2.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert mgr2.get_job(repo(), TASK).evaluation_id == "EVAL-OK"


# -- persistence / reload -----------------------------------------------------


def test_state_reloads_from_disk_across_manager_instances(tmp_path):
    cfg = config(tmp_path)
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(report=mk_report(evaluation_id="EVAL-R")), clock=Clock(), executor=sync_executor)
    mgr.start_job(repo(), TASK)

    # A completely fresh manager over the same data dir sees the persisted job.
    fresh = SemanticJobManager(cfg, evaluator=FakeEvaluator(), clock=Clock(), executor=sync_executor)
    reloaded = fresh.get_job(repo(), TASK)
    assert reloaded.status == STATUS_COMPLETED
    assert reloaded.evaluation_id == "EVAL-R"


def test_find_job_returns_none_for_never_evaluated_task(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.find_job(repo(), "2026-07-23-nope") is None
    with pytest.raises(UnknownSemanticJobError):
        mgr.get_job(repo(), "2026-07-23-nope")


# -- reconciliation -----------------------------------------------------------


def _write_raw_job(cfg: Config, repo_name: str, task_id: str, status: str, **extra) -> Path:
    job_dir = cfg.data_dir / "semantic-jobs" / repo_name / task_id
    job_dir.mkdir(parents=True)
    payload = {"job_id": "J1", "repo_name": repo_name, "repo_path": str(REPO_PATH),
               "task_id": task_id, "status": status, **extra}
    (job_dir / SEMANTIC_JOB_NAME).write_text(json.dumps(payload), encoding="utf-8")
    return job_dir


@pytest.mark.parametrize("status", [STATUS_QUEUED, STATUS_RUNNING])
def test_reconcile_fails_transient_orphans(tmp_path, status):
    cfg = config(tmp_path)
    _write_raw_job(cfg, "alpha", TASK, status)
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(), clock=Clock(), executor=sync_executor)

    reconciled = mgr.reconcile_orphans()

    assert [j.status for j in reconciled] == [STATUS_FAILED]
    on_disk = mgr.get_job(repo(), TASK)
    assert on_disk.status == STATUS_FAILED
    assert "hub restarted" in (on_disk.error or "")
    assert on_disk.ended_ts is not None


def test_reconcile_leaves_terminal_jobs_untouched(tmp_path):
    cfg = config(tmp_path)
    _write_raw_job(cfg, "alpha", TASK, STATUS_COMPLETED, evaluation_id="EVAL-K")
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(), clock=Clock(), executor=sync_executor)

    assert mgr.reconcile_orphans() == []
    assert mgr.get_job(repo(), TASK).evaluation_id == "EVAL-K"


def test_reconciled_orphan_frees_the_slot_for_a_fresh_attempt(tmp_path):
    cfg = config(tmp_path)
    _write_raw_job(cfg, "alpha", TASK, STATUS_RUNNING)
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-NEW"))
    mgr = SemanticJobManager(cfg, evaluator=ev, clock=Clock(), executor=sync_executor)
    mgr.reconcile_orphans()

    # The failed orphan is terminal, so a new start is a fresh attempt (not dedup).
    mgr.start_job(repo(), TASK)
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert ev.calls == [(repo(), TASK)]


def test_reconcile_no_tree_is_noop(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.reconcile_orphans() == []


# -- corrupt record -----------------------------------------------------------


def test_corrupt_record_surfaces_as_error_without_holding_slot(tmp_path):
    cfg = config(tmp_path)
    job_dir = cfg.data_dir / "semantic-jobs" / "alpha" / TASK
    job_dir.mkdir(parents=True)
    (job_dir / SEMANTIC_JOB_NAME).write_text("{not json", encoding="utf-8")
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(report=mk_report(evaluation_id="EVAL-FRESH")), clock=Clock(), executor=sync_executor)

    broken = mgr.get_job(repo(), TASK)
    assert broken.status == STATUS_ERROR and broken.repo_path is None

    # error is terminal → a start overwrites it with a fresh attempt (not blocked).
    mgr.start_job(repo(), TASK)
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED
    # A corrupt orphan is skipped by reconciliation (already terminal).
    assert mgr.reconcile_orphans() == []


# -- containment / no mutation ------------------------------------------------


@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", ".", "", "x\x00y"])
def test_unsafe_task_id_is_rejected(tmp_path, bad):
    mgr = manager(tmp_path)
    with pytest.raises(ValueError):
        mgr.start_job(repo(), bad)
    with pytest.raises(ValueError):
        mgr.find_job(repo(), bad)


@pytest.mark.parametrize("bad_repo", [".", "-.-", ".."])
def test_dotlike_repo_name_is_rejected_not_silently_collapsed(tmp_path, bad_repo):
    """A repo whose slug is a bare dot reference must fail closed, not write a job
    to a collapsed/escaping path the two-level reconcile scan can never reach.

    Regression for the ``ManagedRepo(name=".")`` gap: ``_slug(".")`` (and
    ``_slug("-.-")``) is ``.``, and ``base / "." / task`` *collapses* the
    ``<repo-slug>/`` level, so the record landed at ``semantic-jobs/<task>/job.json``
    — outside the two-level walk :meth:`reconcile_orphans` scans, so a hub crash
    left that transient job stranded and its ``(repo, task)`` duplicate slot held
    forever. (``".."`` instead *escapes* the tree.) The slugged repo segment is now
    validated, so every such attempt is refused before any dir is created.
    """
    mgr = manager(tmp_path)
    dot_repo = repo(name=bad_repo)
    with pytest.raises(ValueError):
        mgr.start_job(dot_repo, TASK)
    with pytest.raises(ValueError):
        mgr.find_job(dot_repo, TASK)
    # Fail-closed before any filesystem write: no job record was persisted, so the
    # stuck-forever orphan can never exist.
    assert snapshot_tree(mgr.jobs_dir) == {}


def test_manager_only_writes_semantic_jobs_subtree(tmp_path):
    cfg = config(tmp_path)
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(), clock=Clock(), executor=sync_executor)

    mgr.start_job(repo(), TASK)

    files = list(snapshot_tree(cfg.data_dir).keys())
    assert files  # something was written
    assert all(f.startswith("semantic-jobs/") for f in files)


# -- integration: real evaluator through the manager --------------------------


VALID_ENVELOPE_PAYLOAD = {
    "summary": "clean handoff",
    "confidence": 0.8,
    "scores": [{"dimension": "handoff_completeness", "score": 0.9, "confidence": 0.8, "rationale": "log complete"}],
    "findings": [],
    "improvements": [],
}


class FakeBackend:
    """Minimal read-only semantic backend returning a canned report envelope."""

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return mk_info(backend="claude-cli", model="claude-opus-4-8")

    def evaluate(self, prompt: str, r: ManagedRepo) -> str:
        from orch_hub.semantic import SEMANTIC_END, SEMANTIC_START

        body = json.dumps(VALID_ENVELOPE_PAYLOAD)
        return f"{SEMANTIC_START}\n{body}\n{SEMANTIC_END}"

    def doctor(self):  # pragma: no cover - not exercised here
        raise NotImplementedError


def _fake_git(args, **kwargs):
    return CommandResult(tuple(str(a) for a in args), 128, "", "")  # git unavailable


def _write_repo(cfg: Config, tmp_path: Path) -> ManagedRepo:
    repo_path = tmp_path / "repos" / "alpha"
    (repo_path / ".ai-tasks").mkdir(parents=True)
    (repo_path / ".ai-tasks" / f"{TASK}.md").write_text(
        f"---\nid: {TASK}\nstatus: in_progress\nsession-est: 1/2\nblockers: []\n---\n\n"
        "# Async status\n\n## Goal\ngo\n\n## Scope\n- x\n\n## Acceptance\n- y\n\n## Session log\n\n"
        "### 2026-07-23 / sid-a / (pending → in_progress)\n- Done: work\n- Next: more\n- Open: none\n",
        encoding="utf-8",
    )
    (repo_path / ".ai-tasks" / "index.md").write_text("# Active tasks\n", encoding="utf-8")
    return ManagedRepo(name="alpha", path=repo_path, manifest=repo_path / ".ai-deploy-manifest.json")


def test_manager_drives_real_evaluator_links_evaluation_and_leaves_repo_intact(tmp_path):
    cfg = config(tmp_path)
    target_repo = _write_repo(cfg, tmp_path)
    service = EvaluationService(cfg, git_runner=_fake_git, clock=Clock())
    evaluator = SemanticEvaluator(cfg, evaluation_service=service, backend=FakeBackend(), clock=Clock())
    mgr = SemanticJobManager(cfg, evaluator=evaluator, clock=Clock(), executor=sync_executor)

    before = snapshot_tree(target_repo.path)
    mgr.start_job(target_repo, TASK)
    job = mgr.get_job(target_repo, TASK)

    assert job.status == STATUS_COMPLETED
    # The completed job links a real evaluation dir holding the persisted report.
    eval_dir = cfg.data_dir / "evaluations" / "alpha" / TASK / job.evaluation_id
    assert (eval_dir / "semantic_report.json").is_file()
    assert job.backend == "claude-cli"
    # The target repo is byte-identical: the async wrapper adds no repo mutation.
    assert snapshot_tree(target_repo.path) == before
