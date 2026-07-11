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
    STATUS_DRAFTING,
    STATUS_ERROR,
    STATUS_FAILED,
    UnknownIntakeError,
    read_events,
)
from orch_hub.models import IntakeDraft, ManagedRepo, ReadinessCheck, ReadinessReport

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
            ReadinessCheck("aii_status", False, "aii-2 status failed: drift"),
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


def make_manager(
    root: Path,
    *,
    agent: FakeIntakeAgent | None = None,
    readiness=passing_readiness,
    clock: FakeClock | None = None,
) -> tuple[IntakeManager, FakeIntakeAgent, FakeClock]:
    agent = agent if agent is not None else FakeIntakeAgent()
    clock = clock if clock is not None else FakeClock()
    config = Config(ai_native_path=root / "ai-native", data_dir=root / "hub-data")
    manager = IntakeManager(
        config, agent=agent, readiness_checker=readiness, clock=clock
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

    record = manager.start_intake(repo, REQUEST)

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
    revised = manager.revise_intake(record.intake_id, "Scope it to a single route")

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
    confirmed = manager.confirm_intake(record.intake_id)

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

    record = manager.start_intake(repo, REQUEST)

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

    failed = manager.revise_intake(record.intake_id, "change it")

    assert failed.status == STATUS_FAILED
    assert "revise boom" in (failed.error or "")
    assert snapshot(repo) == before


def test_confirm_failure_marks_failed_and_leaves_repo_unchanged(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    agent = FakeIntakeAgent(confirm_error=RuntimeError("write blew up"))
    manager, _, _ = make_manager(tmp_path, agent=agent)
    record = manager.start_intake(repo, REQUEST)

    failed = manager.confirm_intake(record.intake_id)

    assert failed.status == STATUS_FAILED
    assert "write blew up" in (failed.error or "")
    assert snapshot(repo) == before  # the agent failed before writing


# -- confirm postcondition: verify the task landed, roll back partial writes --


def test_confirm_fails_when_agent_reports_success_but_writes_nothing(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    manager, agent, _ = make_manager(tmp_path, agent=WriteNothingOnConfirmAgent())
    record = manager.start_intake(repo, REQUEST)

    failed = manager.confirm_intake(record.intake_id)

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

    failed = manager.confirm_intake(record.intake_id)

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

    failed = manager.confirm_intake(record.intake_id)

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

    confirmed = manager.confirm_intake(record.intake_id)

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

    failed = manager.confirm_intake(record.intake_id)

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
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data")
    # No agent injected: the default unavailable backend must fail the intake,
    # not crash the manager.
    manager = IntakeManager(
        config, readiness_checker=passing_readiness, clock=FakeClock()
    )

    record = manager.start_intake(repo, REQUEST)

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
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data")
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
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data")
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
    assert second.status == STATUS_AWAITING_REVIEW


def test_two_repos_can_intake_concurrently(tmp_path):
    repo_a = make_repo(tmp_path, "repo-a")
    repo_b = make_repo(tmp_path, "repo-b")
    manager, _, _ = make_manager(tmp_path)

    manager.start_intake(repo_a, REQUEST)
    record_b = manager.start_intake(repo_b, "b request")

    assert record_b.status == STATUS_AWAITING_REVIEW
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
    record = manager.start_intake(repo, REQUEST)

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
    assert manager.start_intake(repo, "new one").status == STATUS_AWAITING_REVIEW
