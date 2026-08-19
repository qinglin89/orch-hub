"""Tests for the remote intake manager (draft / revise / confirm / cancel).

Everything runs against fake repos in ``tmp_path`` with an injected
conversational agent, readiness checker, and clock — no real agent is ever
launched and no ``/intake-task`` code is imported. The pinned invariant: the
target repo's ``.ai-tasks/`` files are untouched until an explicit confirm, and
on cancellation, agent failure, or a hub restart before confirmation. The fake
agent writes to the repo ONLY inside ``confirm``, so any stray write would be a
manager bug the snapshot assertions catch.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.intake import (
    AgentConfirmResult,
    AgentDraftResult,
    IntakeBusyError,
    IntakeManager,
    IntakeNotReadyError,
    IntakeStateError,
    KIND_INTAKE_CANCELLED,
    KIND_INTAKE_CONFIRMED,
    KIND_INTAKE_CREATED,
    KIND_INTAKE_FAILED,
    KIND_INTAKE_REVISED,
    STATUS_AWAITING_REVIEW,
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_CONFIRMING,
    STATUS_DRAFTING,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_REVISING,
    UnknownIntakeError,
    read_events,
)
from orch_hub.models import (
    IntakeDraft,
    IntakeRecord,
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
)

FIXED_NOW = dt.datetime(2026, 7, 11, 4, 0, 0, tzinfo=dt.timezone.utc)
DRAFT_TASK_ID = "2026-07-11-add-widget"
REQUEST = "Add a widget endpoint to the dashboard"


class FakeClock:
    def __init__(self, now: dt.datetime = FIXED_NOW):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def tick(self, seconds: int = 60) -> None:
        self.now += dt.timedelta(seconds=seconds)


class FakeSession:
    """The opaque conversational handle the manager keeps between turns."""

    def __init__(self, request: str):
        self.request = request
        self.feedback: list[str] = []
        self.cancelled = False


def _draft_for(iteration: int) -> IntakeDraft:
    return IntakeDraft(
        task_id=DRAFT_TASK_ID,
        summary="Add a widget",
        task_file=(
            f"---\nid: {DRAFT_TASK_ID}\nstatus: pending\nsession-est: 0/2\n"
            f"---\n\n# Add a widget\n\nDraft iteration {iteration}.\n"
        ),
        index_entry=f"| {DRAFT_TASK_ID} | Add a widget | pending | 0/2 | [] |",
        notes=None,
    )


class FakeIntakeAgent:
    """A scripted conversational agent.

    ``draft``/``revise`` are strictly read-only (they only return proposals);
    ``confirm`` performs the protocol-defined write (task file + index row).
    Failures are injected per-turn to exercise the manager's fail-closed paths.
    """

    def __init__(self, *, draft_error=None, revise_error=None, confirm_error=None):
        self.draft_error = draft_error
        self.revise_error = revise_error
        self.confirm_error = confirm_error
        self.draft_calls = 0
        self.revise_calls = 0
        self.confirm_calls = 0
        self.cancel_calls = 0

    def draft(self, request, repo) -> AgentDraftResult:
        self.draft_calls += 1
        if self.draft_error is not None:
            raise self.draft_error
        return AgentDraftResult(
            handle=FakeSession(request), ref="sess-1", draft=_draft_for(0)
        )

    def revise(self, handle, feedback, repo) -> AgentDraftResult:
        self.revise_calls += 1
        if self.revise_error is not None:
            raise self.revise_error
        handle.feedback.append(feedback)
        return AgentDraftResult(
            handle=handle, ref="sess-1", draft=_draft_for(len(handle.feedback))
        )

    def confirm(self, handle, repo) -> AgentConfirmResult:
        self.confirm_calls += 1
        if self.confirm_error is not None:
            raise self.confirm_error
        draft = _draft_for(len(handle.feedback))
        tasks = repo.path / ".ai-tasks"
        task_path = tasks / f"{draft.task_id}.md"
        task_path.write_text(draft.task_file, encoding="utf-8")
        index = tasks / "index.md"
        index.write_text(
            index.read_text(encoding="utf-8") + draft.index_entry + "\n",
            encoding="utf-8",
        )
        return AgentConfirmResult(task_id=draft.task_id, task_path=task_path)

    def cancel(self, handle) -> None:
        self.cancel_calls += 1
        handle.cancelled = True


class WriteNothingOnConfirmAgent(FakeIntakeAgent):
    """``confirm`` reports success (returns a result) but writes NO files — a
    real agent answering 'done' with exit 0 having written nothing."""

    def confirm(self, handle, repo) -> AgentConfirmResult:
        self.confirm_calls += 1
        return AgentConfirmResult(task_id=DRAFT_TASK_ID)


class PartialWriteOnConfirmAgent(FakeIntakeAgent):
    """``confirm`` writes only the task file and never the index row, so the
    task never becomes inventory-visible."""

    def confirm(self, handle, repo) -> AgentConfirmResult:
        self.confirm_calls += 1
        draft = _draft_for(len(handle.feedback))
        (repo.path / ".ai-tasks" / f"{draft.task_id}.md").write_text(
            draft.task_file, encoding="utf-8"
        )
        return AgentConfirmResult(task_id=draft.task_id)


class WriteThenFailOnConfirmAgent(FakeIntakeAgent):
    """``confirm`` writes the full task file + index row, then the turn fails."""

    def confirm(self, handle, repo) -> AgentConfirmResult:
        self.confirm_calls += 1
        draft = _draft_for(len(handle.feedback))
        tasks = repo.path / ".ai-tasks"
        (tasks / f"{draft.task_id}.md").write_text(draft.task_file, encoding="utf-8")
        index = tasks / "index.md"
        index.write_text(
            index.read_text(encoding="utf-8") + draft.index_entry + "\n",
            encoding="utf-8",
        )
        raise RuntimeError("write turn exited nonzero after writing")


class ConfirmWritesTaskWithStatusAgent(FakeIntakeAgent):
    """``confirm`` writes a complete task file + index row whose frontmatter
    carries a caller-chosen status. Used to prove the confirm postcondition
    accepts only ``pending``/``blocked`` and rejects every other landed status.
    An empty ``status`` writes no ``status:`` line, so the task-store reports the
    ``unknown`` sentinel; ``blocked`` also writes a ``blockers`` line so the
    file is a well-formed blocked task.
    """

    def __init__(self, status: str, **kw):
        super().__init__(**kw)
        self.status = status

    def confirm(self, handle, repo) -> AgentConfirmResult:
        self.confirm_calls += 1
        tasks = repo.path / ".ai-tasks"
        status_line = f"status: {self.status}\n" if self.status else ""
        blockers_line = (
            "blockers: [external:awaiting API spec]\n"
            if self.status == "blocked"
            else ""
        )
        task_file = (
            f"---\nid: {DRAFT_TASK_ID}\n{status_line}{blockers_line}"
            f"session-est: 0/2\n---\n\n# Add a widget\n\nLanded task.\n"
        )
        (tasks / f"{DRAFT_TASK_ID}.md").write_text(task_file, encoding="utf-8")
        index = tasks / "index.md"
        index.write_text(
            index.read_text(encoding="utf-8")
            + f"| {DRAFT_TASK_ID} | Add a widget | {self.status or 'unknown'} | 0/2 | [] |\n",
            encoding="utf-8",
        )
        return AgentConfirmResult(task_id=DRAFT_TASK_ID)


def passing_readiness(repo, config) -> ReadinessReport:
    return ReadinessReport((ReadinessCheck("all", True, "ok"),))


def failing_readiness(repo, config) -> ReadinessReport:
    return ReadinessReport(
        (
            ReadinessCheck("git_clean", False, "working tree has uncommitted changes"),
            ReadinessCheck("mandrel_status", False, "mandrel status failed: drift"),
        )
    )


def make_repo(root: Path, name: str = "repo-a") -> ManagedRepo:
    path = root / "repos" / name
    (path / ".ai-tasks").mkdir(parents=True)
    (path / ".ai-tasks" / "index.md").write_text(
        "# Active tasks\n\n| id | title | status | session-est | blockers |\n"
        "|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def sync_executor(run) -> None:
    """Run an intake turn inline so a mutating call settles before it returns.

    The manager runs each agent turn off the lock through an injected executor;
    the production default is a daemon thread. Tests inject this synchronous one
    so a turn finishes (and the record settles) within the mutating call — the
    return value stays the transient record, but a subsequent ``get_intake``
    deterministically observes the settled outcome with no thread joins.
    """
    run()


def settle(manager: IntakeManager, transient: IntakeRecord) -> IntakeRecord:
    """The persisted record after a (synchronously executed) turn finished.

    A mutating call returns the transient (drafting/revising/confirming) record;
    with :func:`sync_executor` the off-lock turn already ran, so re-reading shows
    the settled awaiting_review/confirmed/failed state.
    """
    return manager.get_intake(transient.intake_id)


def make_manager(
    root: Path,
    *,
    agent: FakeIntakeAgent | None = None,
    readiness=passing_readiness,
    clock: FakeClock | None = None,
    executor=sync_executor,
    notifier=None,
    notify_dispatch=None,
) -> tuple[IntakeManager, FakeIntakeAgent, FakeClock]:
    agent = agent if agent is not None else FakeIntakeAgent()
    clock = clock if clock is not None else FakeClock()
    config = Config(mandrel_path=root / "mandrel", data_dir=root / "hub-data")
    manager = IntakeManager(
        config,
        agent=agent,
        readiness_checker=readiness,
        clock=clock,
        executor=executor,
        notifier=notifier,
        # Deliver inline (not on the production daemon thread) so notification
        # assertions are deterministic; it is only invoked when a notifier is set.
        notify_dispatch=notify_dispatch
        if notify_dispatch is not None
        else (lambda deliver: deliver()),
    )
    return manager, agent, clock


def snapshot(repo: ManagedRepo) -> dict[str, bytes]:
    """Byte-exact contents of the repo's ``.ai-tasks/`` files."""
    tasks = repo.path / ".ai-tasks"
    return {p.name: p.read_bytes() for p in sorted(tasks.iterdir()) if p.is_file()}


def read_record_json(manager: IntakeManager, intake_id: str) -> dict:
    return json.loads(
        (manager.intake_dir / intake_id / "intake.json").read_text(encoding="utf-8")
    )


# -- drafting is read-only ----------------------------------------------------


def test_start_intake_drafts_without_touching_the_repo(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager, agent, _ = make_manager(tmp_path)

    transient = manager.start_intake(repo, REQUEST)
    assert transient.status == STATUS_DRAFTING  # returned before the off-lock turn

    record = settle(manager, transient)
    assert record.status == STATUS_AWAITING_REVIEW
    assert record.draft is not None
    assert record.draft.task_id == DRAFT_TASK_ID
    assert record.iteration == 0
    assert record.request == REQUEST
    assert agent.draft_calls == 1
    assert agent.confirm_calls == 0
    assert snapshot(repo) == before  # read-only: no task written


def test_start_intake_persists_record_and_creation_event(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)

    record = manager.start_intake(repo, REQUEST)

    persisted = read_record_json(manager, record.intake_id)
    assert persisted["status"] == STATUS_AWAITING_REVIEW
    assert persisted["draft"]["task_id"] == DRAFT_TASK_ID
    assert persisted["agent_ref"] == "sess-1"

    events = read_events(manager.intake_dir / record.intake_id)
    assert [e.kind for e in events] == [KIND_INTAKE_CREATED]


def test_request_is_stripped_and_empty_is_rejected(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)

    record = manager.start_intake(repo, f"  {REQUEST}  ")
    assert record.request == REQUEST

    with pytest.raises(ValueError):
        manager.start_intake(repo, "   ")


# -- revision is read-only ----------------------------------------------------


def test_revise_updates_draft_and_bumps_iteration_without_writing(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager, agent, clock = make_manager(tmp_path)
    record = manager.start_intake(repo, REQUEST)

    clock.tick()
    transient = manager.revise_intake(record.intake_id, "Scope it to a single route")
    assert transient.status == STATUS_REVISING  # returned before the off-lock turn

    revised = settle(manager, transient)
    assert revised.status == STATUS_AWAITING_REVIEW
    assert revised.iteration == 1
    assert "iteration 1" in revised.draft.task_file
    assert agent.revise_calls == 1
    assert agent.confirm_calls == 0
    assert snapshot(repo) == before  # revision is still read-only

    events = read_events(manager.intake_dir / record.intake_id)
    assert [e.kind for e in events] == [KIND_INTAKE_CREATED, KIND_INTAKE_REVISED]
    revised_event = events[-1]
    assert revised_event.iteration == 1
    assert "Scope it to a single route" in (revised_event.detail or "")


def test_revise_requires_a_non_empty_feedback(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_intake(repo, REQUEST)

    with pytest.raises(ValueError):
        manager.revise_intake(record.intake_id, "  ")


# -- confirmation is the only write -------------------------------------------


def test_confirm_is_the_only_write_and_records_created_task(tmp_path):
    repo = make_repo(tmp_path)
    manager, agent, clock = make_manager(tmp_path)
    record = manager.start_intake(repo, REQUEST)
    clock.tick()
    manager.revise_intake(record.intake_id, "Tighten the acceptance")

    clock.tick()
    transient = manager.confirm_intake(record.intake_id)
    assert transient.status == STATUS_CONFIRMING  # returned before the write turn

    confirmed = settle(manager, transient)
    assert confirmed.status == STATUS_CONFIRMED
    assert confirmed.created_task_id == DRAFT_TASK_ID
    assert agent.confirm_calls == 1

    task_file = repo.path / ".ai-tasks" / f"{DRAFT_TASK_ID}.md"
    assert task_file.is_file()
    index = (repo.path / ".ai-tasks" / "index.md").read_text(encoding="utf-8")
    assert DRAFT_TASK_ID in index

    events = read_events(manager.intake_dir / record.intake_id)
    assert [e.kind for e in events] == [
        KIND_INTAKE_CREATED,
        KIND_INTAKE_REVISED,
        KIND_INTAKE_CONFIRMED,
    ]
    assert events[-1].task_id == DRAFT_TASK_ID


def test_confirm_requires_awaiting_review(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_intake(repo, REQUEST)
    manager.confirm_intake(record.intake_id)

    with pytest.raises(IntakeStateError):
        manager.confirm_intake(record.intake_id)  # already confirmed


# -- cancellation leaves the repo unchanged -----------------------------------


def test_cancel_leaves_repo_unchanged_and_audits(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager, agent, _ = make_manager(tmp_path)
    record = manager.start_intake(repo, REQUEST)

    cancelled = manager.cancel_intake(record.intake_id)

    assert cancelled.status == STATUS_CANCELLED
    assert agent.cancel_calls == 1
    assert agent.confirm_calls == 0
    assert snapshot(repo) == before

    events = read_events(manager.intake_dir / record.intake_id)
    assert events[-1].kind == KIND_INTAKE_CANCELLED


def test_cancel_rejected_from_terminal_status(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_intake(repo, REQUEST)
    manager.confirm_intake(record.intake_id)

    with pytest.raises(IntakeStateError):
        manager.cancel_intake(record.intake_id)


# -- failure paths leave the repo unchanged -----------------------------------


def test_draft_failure_marks_failed_and_leaves_repo_unchanged(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    agent = FakeIntakeAgent(draft_error=RuntimeError("agent crashed"))
    manager, _, _ = make_manager(tmp_path, agent=agent)

    record = settle(manager, manager.start_intake(repo, REQUEST))

    assert record.status == STATUS_FAILED
    assert "agent crashed" in (record.error or "")
    assert snapshot(repo) == before
    events = read_events(manager.intake_dir / record.intake_id)
    assert [e.kind for e in events] == [KIND_INTAKE_CREATED, KIND_INTAKE_FAILED]


def test_revise_failure_marks_failed(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    agent = FakeIntakeAgent(revise_error=RuntimeError("revise boom"))
    manager, _, _ = make_manager(tmp_path, agent=agent)
    record = manager.start_intake(repo, REQUEST)

    failed = settle(manager, manager.revise_intake(record.intake_id, "change it"))

    assert failed.status == STATUS_FAILED
    assert "revise boom" in (failed.error or "")
    assert snapshot(repo) == before


def test_confirm_failure_marks_failed_and_leaves_repo_unchanged(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    agent = FakeIntakeAgent(confirm_error=RuntimeError("write blew up"))
    manager, _, _ = make_manager(tmp_path, agent=agent)
    record = manager.start_intake(repo, REQUEST)

    failed = settle(manager, manager.confirm_intake(record.intake_id))

    assert failed.status == STATUS_FAILED
    assert "write blew up" in (failed.error or "")
    assert snapshot(repo) == before  # the agent failed before writing


# -- confirm postcondition: verify the task landed, roll back partial writes --


def test_confirm_fails_when_agent_reports_success_but_writes_nothing(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager, agent, _ = make_manager(tmp_path, agent=WriteNothingOnConfirmAgent())
    record = manager.start_intake(repo, REQUEST)

    failed = settle(manager, manager.confirm_intake(record.intake_id))

    # Exit-0 with no write is NOT a confirmation: the manager verifies the task
    # actually appears in the repo inventory before recording ``confirmed``, so
    # the dashboard never routes to a task that does not exist.
    assert failed.status == STATUS_FAILED
    assert failed.created_task_id is None
    assert "inventory" in (failed.error or "")
    assert agent.confirm_calls == 1
    assert snapshot(repo) == before  # no phantom task, repo untouched
    events = read_events(manager.intake_dir / record.intake_id)
    assert events[-1].kind == KIND_INTAKE_FAILED


def test_confirm_rolls_back_partial_task_file_when_index_row_missing(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager, _, _ = make_manager(tmp_path, agent=PartialWriteOnConfirmAgent())
    record = manager.start_intake(repo, REQUEST)

    failed = settle(manager, manager.confirm_intake(record.intake_id))

    # A task file with no index row is not inventory-visible → unverified → the
    # orphan file is rolled back; a non-confirmed intake leaves no artifact.
    assert failed.status == STATUS_FAILED
    assert not (repo.path / ".ai-tasks" / f"{DRAFT_TASK_ID}.md").exists()
    assert snapshot(repo) == before


def test_confirm_rolls_back_when_write_turn_fails_after_writing(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager, _, _ = make_manager(tmp_path, agent=WriteThenFailOnConfirmAgent())
    record = manager.start_intake(repo, REQUEST)

    failed = settle(manager, manager.confirm_intake(record.intake_id))

    # A write-permitted confirm turn that mutates .ai-tasks/ and then fails must
    # not leave the created task behind (acceptance: a failed intake does not
    # modify the target repo): the partial write is rolled back.
    assert failed.status == STATUS_FAILED
    assert "write turn exited nonzero" in (failed.error or "")
    assert not (repo.path / ".ai-tasks" / f"{DRAFT_TASK_ID}.md").exists()
    assert snapshot(repo) == before


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_confirm_accepts_valid_post_intake_status(tmp_path, status):
    repo = make_repo(tmp_path)
    manager, agent, _ = make_manager(
        tmp_path, agent=ConfirmWritesTaskWithStatusAgent(status)
    )
    record = manager.start_intake(repo, REQUEST)

    confirmed = settle(manager, manager.confirm_intake(record.intake_id))

    # ``pending`` (fresh intake) and ``blocked`` (intake recorded blockers) are
    # the two legal post-intake states, so both satisfy the confirm
    # postcondition and record ``confirmed`` with the created task id.
    assert confirmed.status == STATUS_CONFIRMED
    assert confirmed.created_task_id == DRAFT_TASK_ID
    assert (repo.path / ".ai-tasks" / f"{DRAFT_TASK_ID}.md").is_file()
    assert agent.confirm_calls == 1


@pytest.mark.parametrize(
    "status",
    ["in_progress", "final_review", "completed", ""],
    ids=["in_progress", "final_review", "completed", "unknown-no-status"],
)
def test_confirm_rejects_and_rolls_back_non_intake_status(tmp_path, status):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager, agent, _ = make_manager(
        tmp_path, agent=ConfirmWritesTaskWithStatusAgent(status)
    )
    record = manager.start_intake(repo, REQUEST)

    failed = settle(manager, manager.confirm_intake(record.intake_id))

    # Only ``pending``/``blocked`` are legal post-intake states. A confirm turn
    # that lands a task in any other frontmatter status — an in-flight/finished
    # ``in_progress``/``final_review``/``completed``, or an empty-status
    # ``unknown`` sentinel — is not a valid intake: the postcondition fails
    # closed and the unverified write is rolled back, so the repo is unchanged.
    assert failed.status == STATUS_FAILED
    assert failed.created_task_id is None
    assert not (repo.path / ".ai-tasks" / f"{DRAFT_TASK_ID}.md").exists()
    assert snapshot(repo) == before
    assert agent.confirm_calls == 1


def test_default_agent_makes_intake_fail_cleanly(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    config = Config(mandrel_path=tmp_path / "mandrel", data_dir=tmp_path / "hub-data")
    # No agent injected: the default unavailable backend must fail the intake,
    # not crash the manager.
    manager = IntakeManager(
        config,
        readiness_checker=passing_readiness,
        clock=FakeClock(),
        executor=sync_executor,
    )

    record = settle(manager, manager.start_intake(repo, REQUEST))

    assert record.status == STATUS_FAILED
    assert "no intake agent configured" in (record.error or "")
    assert snapshot(repo) == before


# -- interruption: a hub restart before confirmation --------------------------


def test_lost_session_fails_confirm_without_writing(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager1, _, clock = make_manager(tmp_path)
    record = manager1.start_intake(repo, REQUEST)

    # A fresh manager over the same data dir (a hub restart): the in-memory
    # conversational handle is gone, so confirmation must fail closed.
    fresh_agent = FakeIntakeAgent()
    config = Config(mandrel_path=tmp_path / "mandrel", data_dir=tmp_path / "hub-data")
    manager2 = IntakeManager(
        config, agent=fresh_agent, readiness_checker=passing_readiness, clock=clock
    )

    failed = manager2.confirm_intake(record.intake_id)

    assert failed.status == STATUS_FAILED
    assert "session lost" in (failed.error or "")
    assert fresh_agent.confirm_calls == 0
    assert snapshot(repo) == before


def test_lost_session_fails_revise_without_writing(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager1, _, clock = make_manager(tmp_path)
    record = manager1.start_intake(repo, REQUEST)

    fresh_agent = FakeIntakeAgent()
    config = Config(mandrel_path=tmp_path / "mandrel", data_dir=tmp_path / "hub-data")
    manager2 = IntakeManager(
        config, agent=fresh_agent, readiness_checker=passing_readiness, clock=clock
    )

    failed = manager2.revise_intake(record.intake_id, "please change")

    assert failed.status == STATUS_FAILED
    assert fresh_agent.revise_calls == 0
    assert snapshot(repo) == before


def test_interrupted_drafting_intake_is_cancellable_not_confirmable(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_intake(repo, REQUEST)

    # Simulate a crash mid-draft: the persisted transient ``drafting`` state.
    path = manager.intake_dir / record.intake_id / "intake.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = STATUS_DRAFTING
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(IntakeStateError):
        manager.confirm_intake(record.intake_id)

    cancelled = manager.cancel_intake(record.intake_id)
    assert cancelled.status == STATUS_CANCELLED
    assert snapshot(repo) == before


# -- gating -------------------------------------------------------------------


def test_readiness_failure_blocks_intake(tmp_path):
    repo = make_repo(tmp_path)
    manager, agent, _ = make_manager(tmp_path, readiness=failing_readiness)

    with pytest.raises(IntakeNotReadyError) as excinfo:
        manager.start_intake(repo, REQUEST)

    assert "git_clean" in str(excinfo.value)
    assert agent.draft_calls == 0
    assert manager.list_intakes() == []  # no dir created for a gated start


def test_repo_busy_blocks_a_second_intake(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    manager.start_intake(repo, REQUEST)

    with pytest.raises(IntakeBusyError):
        manager.start_intake(repo, "another request")


def test_terminal_intake_frees_the_repo_for_a_new_one(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, clock = make_manager(tmp_path)
    first = manager.start_intake(repo, REQUEST)
    manager.cancel_intake(first.intake_id)

    clock.tick()
    second = manager.start_intake(repo, "a fresh request")

    assert second.intake_id != first.intake_id
    assert settle(manager, second).status == STATUS_AWAITING_REVIEW


def test_two_repos_can_intake_concurrently(tmp_path):
    repo_a = make_repo(tmp_path, "repo-a")
    repo_b = make_repo(tmp_path, "repo-b")
    manager, _, _ = make_manager(tmp_path)

    manager.start_intake(repo_a, REQUEST)
    record_b = manager.start_intake(repo_b, "b request")

    assert settle(manager, record_b).status == STATUS_AWAITING_REVIEW
    assert len(manager.list_intakes()) == 2


# -- inspection & serialization ------------------------------------------------


def test_unknown_intake_id_raises(tmp_path):
    manager, _, _ = make_manager(tmp_path)

    with pytest.raises(UnknownIntakeError):
        manager.get_intake("nope")
    with pytest.raises(UnknownIntakeError):
        manager.get_intake("../escape")


def test_get_and_list_and_history(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, clock = make_manager(tmp_path)
    record = manager.start_intake(repo, REQUEST)
    clock.tick()
    manager.revise_intake(record.intake_id, "adjust")

    fetched = manager.get_intake(record.intake_id)
    assert fetched.iteration == 1

    listed = manager.list_intakes()
    assert [r.intake_id for r in listed] == [record.intake_id]

    history = manager.intake_history(record.intake_id)
    assert history.record.intake_id == record.intake_id
    assert [e.kind for e in history.events] == [
        KIND_INTAKE_CREATED,
        KIND_INTAKE_REVISED,
    ]


def test_record_to_dict_hides_agent_ref_but_shows_draft(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = settle(manager, manager.start_intake(repo, REQUEST))

    payload = record.to_dict()
    assert "agent_ref" not in payload
    assert payload["draft"]["task_id"] == DRAFT_TASK_ID
    assert payload["status"] == STATUS_AWAITING_REVIEW


def test_unreadable_record_surfaces_as_error(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_intake(repo, REQUEST)
    (manager.intake_dir / record.intake_id / "intake.json").write_text(
        "{not json", encoding="utf-8"
    )

    surfaced = manager.get_intake(record.intake_id)
    assert surfaced.status == STATUS_ERROR
    assert "unreadable" in (surfaced.error or "")
    # An error record carries no repo attribution, so it never holds the slot.
    assert settle(manager, manager.start_intake(repo, "new one")).status == STATUS_AWAITING_REVIEW


# -- off-lock turns: concurrency, cancel-race, startup reconciliation ----------
#
# These exercise the async model directly with a real background thread: a turn
# that blocks (BlockingIntakeAgent) so a read or cancel can land while it is in
# flight, run through a joinable executor (ThreadExecutor) so finalize completes
# deterministically. The sync-executor tests above already cover the happy/fail
# state machine; here the point is that the turn runs OFF the manager lock.


class BlockingIntakeAgent(FakeIntakeAgent):
    """A ``FakeIntakeAgent`` whose selected turn blocks until released.

    Lets a test observe a turn in flight off the manager lock: the turn signals
    ``started`` and waits on ``release`` before producing its result. Only the
    turn named by ``block`` (``"draft"`` or ``"revise"``) blocks.
    """

    def __init__(self, *, block: str = "draft", **kw):
        super().__init__(**kw)
        self._block = block
        self.started = threading.Event()
        self.release = threading.Event()

    def draft(self, request, repo) -> AgentDraftResult:
        if self._block == "draft":
            self.started.set()
            assert self.release.wait(timeout=5), "draft turn was never released"
        return super().draft(request, repo)

    def revise(self, handle, feedback, repo) -> AgentDraftResult:
        if self._block == "revise":
            self.started.set()
            assert self.release.wait(timeout=5), "revise turn was never released"
        return super().revise(handle, feedback, repo)


class ThreadExecutor:
    """Runs each intake turn on its own daemon thread the test can join.

    The production executor is a fire-and-forget daemon thread; this one keeps
    the threads so a test can wait for finalize to complete before asserting.
    """

    def __init__(self):
        self._threads: list[threading.Thread] = []

    def __call__(self, run) -> None:
        thread = threading.Thread(target=run, daemon=True)
        self._threads.append(thread)
        thread.start()

    def join(self, timeout: float = 5.0) -> None:
        for thread in self._threads:
            thread.join(timeout)
            assert not thread.is_alive(), "intake turn thread did not finish"


def call_without_blocking(fn, timeout: float = 5.0):
    """Run ``fn`` on a thread; fail if it does not return within ``timeout``.

    A read that blocks means an in-flight turn is (wrongly) holding the manager
    lock; this turns that deadlock into a clean assertion instead of a hung test.
    """
    box: dict = {}
    thread = threading.Thread(
        target=lambda: box.__setitem__("value", fn()), daemon=True
    )
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "call blocked — a turn is holding the manager lock"
    return box["value"]


def force_status(manager: IntakeManager, intake_id: str, status: str) -> None:
    """Rewrite a persisted intake's status (simulating a hub crash mid-turn)."""
    path = manager.intake_dir / intake_id / "intake.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = status
    path.write_text(json.dumps(data), encoding="utf-8")


def test_reads_do_not_block_during_in_flight_turn(tmp_path):
    repo = make_repo(tmp_path)
    agent = BlockingIntakeAgent()
    executor = ThreadExecutor()
    manager, _, _ = make_manager(tmp_path, agent=agent, executor=executor)

    transient = manager.start_intake(repo, REQUEST)
    assert transient.status == STATUS_DRAFTING
    assert agent.started.wait(timeout=5)  # the draft turn is running off the lock

    # The in-flight turn holds no lock, so reads return at once with the
    # transient state. If the turn held the manager lock these would deadlock;
    # ``call_without_blocking`` fails cleanly in that case instead of hanging.
    listed = call_without_blocking(manager.list_intakes)
    assert [r.status for r in listed] == [STATUS_DRAFTING]
    fetched = call_without_blocking(lambda: manager.get_intake(transient.intake_id))
    assert fetched.status == STATUS_DRAFTING

    agent.release.set()
    executor.join()
    assert manager.get_intake(transient.intake_id).status == STATUS_AWAITING_REVIEW


def test_cancel_during_drafting_turn_ends_cancelled_and_discards_result(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    agent = BlockingIntakeAgent()
    executor = ThreadExecutor()
    manager, _, _ = make_manager(tmp_path, agent=agent, executor=executor)

    transient = manager.start_intake(repo, REQUEST)
    assert agent.started.wait(timeout=5)  # draft turn in flight, blocked

    # Cancel lands mid-turn: drafting is cancellable, so the record goes terminal
    # now while the turn is still blocked — a concurrent cancel does not wait for
    # the turn.
    cancelled = manager.cancel_intake(transient.intake_id)
    assert cancelled.status == STATUS_CANCELLED

    # Release the turn; finalize must DISCARD its result and never resurrect the
    # cancelled intake back to awaiting_review.
    agent.release.set()
    executor.join()

    final = manager.get_intake(transient.intake_id)
    assert final.status == STATUS_CANCELLED
    assert final.draft is None  # the finished draft was thrown away
    assert snapshot(repo) == before  # drafting is read-only regardless
    # The orphaned session is released best-effort exactly once, by finalize
    # (the drafting turn had not yet stored a handle for cancel to release).
    assert agent.cancel_calls == 1
    events = read_events(manager.intake_dir / transient.intake_id)
    assert [e.kind for e in events] == [KIND_INTAKE_CREATED, KIND_INTAKE_CANCELLED]


def test_cancel_during_revising_turn_ends_cancelled_and_discards_result(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    agent = BlockingIntakeAgent(block="revise")
    executor = ThreadExecutor()
    manager, _, _ = make_manager(tmp_path, agent=agent, executor=executor)

    started = manager.start_intake(repo, REQUEST)  # draft runs (not blocked)
    executor.join()  # let the draft settle to awaiting_review
    assert manager.get_intake(started.intake_id).status == STATUS_AWAITING_REVIEW

    transient = manager.revise_intake(started.intake_id, "make it smaller")
    assert transient.status == STATUS_REVISING
    assert agent.started.wait(timeout=5)  # revise turn in flight, blocked

    cancelled = manager.cancel_intake(started.intake_id)
    assert cancelled.status == STATUS_CANCELLED
    # The revision's draft is still iteration 0 (the revise result is discarded).
    assert cancelled.iteration == 0

    agent.release.set()
    executor.join()

    final = manager.get_intake(started.intake_id)
    assert final.status == STATUS_CANCELLED
    assert final.iteration == 0  # the discarded revise never bumped iteration
    assert snapshot(repo) == before
    events = read_events(manager.intake_dir / started.intake_id)
    # No revised event: the cancelled turn's result is discarded, not audited.
    assert [e.kind for e in events] == [KIND_INTAKE_CREATED, KIND_INTAKE_CANCELLED]


@pytest.mark.parametrize(
    "transient_status", [STATUS_DRAFTING, STATUS_REVISING, STATUS_CONFIRMING]
)
def test_reconcile_orphans_fails_orphaned_transient_records(tmp_path, transient_status):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager1, _, clock = make_manager(tmp_path)
    started = manager1.start_intake(repo, REQUEST)  # settles awaiting_review (sync)

    # Simulate a hub crash mid-turn: force a persisted transient status. A fresh
    # manager over the same data dir has no in-memory handle, so this is an
    # orphan no thread will finalize.
    force_status(manager1, started.intake_id, transient_status)
    manager2, _, _ = make_manager(tmp_path, clock=clock)

    # The orphan holds the repo's intake slot until reconciled.
    with pytest.raises(IntakeBusyError):
        manager2.start_intake(repo, "blocked while the orphan holds the slot")

    reconciled = manager2.reconcile_orphans()

    assert [r.intake_id for r in reconciled] == [started.intake_id]
    assert reconciled[0].status == STATUS_FAILED
    assert "session lost" in (reconciled[0].error or "")
    assert manager2.get_intake(started.intake_id).status == STATUS_FAILED
    assert snapshot(repo) == before  # reconciliation never mutates the target repo
    events = read_events(manager2.intake_dir / started.intake_id)
    assert events[-1].kind == KIND_INTAKE_FAILED
    # The freed slot lets a new intake start.
    fresh = manager2.start_intake(repo, "now unblocked")
    assert settle(manager2, fresh).status == STATUS_AWAITING_REVIEW


def test_reconcile_orphans_preserves_resting_states_and_returns_nothing(tmp_path):
    repo = make_repo(tmp_path)
    manager1, _, clock = make_manager(tmp_path)
    started = settle(manager1, manager1.start_intake(repo, REQUEST))
    assert started.status == STATUS_AWAITING_REVIEW

    # A restart: fresh manager over the same data dir, no in-memory handles. An
    # awaiting_review draft is a resting (not in-flight transient) state, so
    # reconciliation leaves it untouched — the human can still review it after a
    # restart. Reconciliation only fails the in-flight transient trio.
    manager2, _, _ = make_manager(tmp_path, clock=clock)
    assert manager2.reconcile_orphans() == []
    assert manager2.get_intake(started.intake_id).status == STATUS_AWAITING_REVIEW


# -- intake-ready notifications -----------------------------------------------
#
# A draft or revision that settles to awaiting_review fires exactly one
# metadata-only "intake ready" push through the injected notifier; every other
# outcome (cancel-race, turn failure, confirm) fires nothing. Delivery is
# best-effort and isolated: a failing backend never changes the settled record.
# Dispatch is synchronous in these tests (see make_manager), so a push is
# observable immediately after the turn settles.


class RecordingNotifier:
    """Captures every delivered notification (synchronous dispatch in tests)."""

    def __init__(self):
        self.notifications: list = []

    def notify(self, notification) -> None:
        self.notifications.append(notification)


class RaisingNotifier:
    """A backend that always fails, to prove delivery failures are isolated."""

    def __init__(self):
        self.calls = 0

    def notify(self, notification) -> None:
        self.calls += 1
        raise OSError("bark unreachable")


def test_successful_draft_emits_one_intake_ready_notification(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)

    record = settle(manager, manager.start_intake(repo, REQUEST))
    assert record.status == STATUS_AWAITING_REVIEW

    assert len(notifier.notifications) == 1
    note = notifier.notifications[0]
    assert note.title == "repo-a: intake ready"
    assert note.group == "repo-a"
    assert note.body == f"{record.intake_id}\ndraft ready for review"
    # Metadata only: the operator's request never rides the push.
    assert REQUEST not in note.body


def test_each_revision_emits_its_own_intake_ready_notification(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)

    record = settle(manager, manager.start_intake(repo, REQUEST))  # push 1: draft
    settle(manager, manager.revise_intake(record.intake_id, "make it smaller"))

    assert len(notifier.notifications) == 2
    assert notifier.notifications[0].body.endswith("draft ready for review")
    assert notifier.notifications[1].body.endswith("revision 1 ready for review")


def test_confirm_emits_no_intake_ready_notification(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)

    record = settle(manager, manager.start_intake(repo, REQUEST))  # push 1: draft
    confirmed = settle(manager, manager.confirm_intake(record.intake_id))
    assert confirmed.status == STATUS_CONFIRMED

    # A confirm settling to confirmed is not an awaiting_review event: no push.
    assert len(notifier.notifications) == 1
    assert notifier.notifications[0].body.endswith("draft ready for review")


def test_failed_draft_turn_emits_no_notification(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    agent = FakeIntakeAgent(draft_error=RuntimeError("backend down"))
    manager, _, _ = make_manager(tmp_path, agent=agent, notifier=notifier)

    failed = settle(manager, manager.start_intake(repo, REQUEST))
    assert failed.status == STATUS_FAILED
    assert notifier.notifications == []


def test_failed_revision_turn_emits_no_notification(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    agent = FakeIntakeAgent(revise_error=RuntimeError("backend down"))
    manager, _, _ = make_manager(tmp_path, agent=agent, notifier=notifier)

    record = settle(manager, manager.start_intake(repo, REQUEST))  # push 1: draft
    failed = settle(manager, manager.revise_intake(record.intake_id, "smaller"))
    assert failed.status == STATUS_FAILED
    # Only the initial draft notified; the failed revision fired nothing more.
    assert len(notifier.notifications) == 1


def test_cancel_during_drafting_turn_emits_no_notification(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    agent = BlockingIntakeAgent()
    executor = ThreadExecutor()
    manager, _, _ = make_manager(
        tmp_path, agent=agent, executor=executor, notifier=notifier
    )

    transient = manager.start_intake(repo, REQUEST)
    assert agent.started.wait(timeout=5)  # draft turn in flight, blocked off-lock

    # Cancel lands mid-turn; releasing the blocked draft, finalize sees a
    # cancelled (non-drafting) record and DISCARDS the result before the emit, so
    # a discarded turn fires nothing.
    manager.cancel_intake(transient.intake_id)
    agent.release.set()
    executor.join()

    assert manager.get_intake(transient.intake_id).status == STATUS_CANCELLED
    assert notifier.notifications == []


def test_notifier_failure_does_not_change_the_settled_intake(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RaisingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)

    record = settle(manager, manager.start_intake(repo, REQUEST))

    assert notifier.calls == 1  # delivery was attempted...
    # ...and the failure is swallowed: the draft still settled to awaiting_review
    # with its draft intact, and the persisted record is unchanged by the drop.
    assert record.status == STATUS_AWAITING_REVIEW
    assert record.draft is not None
    assert read_record_json(manager, record.intake_id)["status"] == STATUS_AWAITING_REVIEW


def test_no_notifier_configured_does_no_dispatch_work(tmp_path):
    repo = make_repo(tmp_path)
    dispatched: list = []
    manager, _, _ = make_manager(
        tmp_path,
        notifier=None,
        notify_dispatch=lambda deliver: dispatched.append(deliver),
    )

    record = settle(manager, manager.start_intake(repo, REQUEST))

    assert record.status == STATUS_AWAITING_REVIEW  # settles normally
    assert dispatched == []  # a None notifier short-circuits before any dispatch
