"""Tests for the concrete CLI-backed intake agent (``ClaudeIntakeAgent``).

Everything runs against an injected fake runner — ``claude`` is never launched
and no ``/intake-task`` code is imported. The fake returns canned ``stream-json``
and, on the write turn, writes the fixture files exactly as real claude would, so
every mechanic is exercised without a real agent: argv shapes, the
read-only-until-confirm gate, session threading, envelope parsing, the confirm
fallback, and each failure path. The thin real runner (``_default_runner``) stays
untested, like runs' ``_default_spawner``.

The final test drives the real :class:`IntakeManager` with the agent over a fake
runner end-to-end and pins the task invariant: ``.ai-tasks/`` is byte-identical
through drafting and revision and changes only on an explicit confirm.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from orch_hub.intake import (
    IntakeManager,
    STATUS_AWAITING_REVIEW,
    STATUS_CONFIRMED,
    STATUS_CONFIRMING,
    STATUS_DRAFTING,
    STATUS_FAILED,
)
from orch_hub.intake_agent import (
    CONFIRM_END,
    CONFIRM_START,
    DRAFT_END,
    DRAFT_START,
    AgentRunResult,
    ClaudeIntakeAgent,
    IntakeAgentError,
    _ClaudeIntakeHandle,
)
from orch_hub.config import DEFAULT_INTAKE_EFFORT, DEFAULT_INTAKE_MODEL, Config
from orch_hub.models import (
    IntakeDraft,
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
)

FIXED_NOW = dt.datetime(2026, 7, 11, 4, 0, 0, tzinfo=dt.timezone.utc)
REQUEST = "Add a widget endpoint to the dashboard"
TASK_ID = "2026-07-11-add-widget"


# -- fakes / helpers ----------------------------------------------------------


class ScriptRunner:
    """Returns a scripted ``AgentRunResult`` per call, recording argv + cwd."""

    def __init__(self, *results: AgentRunResult):
        self._results = list(results)
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv, cwd) -> AgentRunResult:
        self.calls.append((list(argv), cwd))
        if not self._results:
            raise AssertionError("runner called more times than scripted")
        return self._results.pop(0)

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _cwd in self.calls]


def _task_file(task_id: str = TASK_ID, tag: str = "") -> str:
    return (
        f"---\nid: {task_id}\nstatus: pending\nsession-est: 0/2\nblockers: []\n"
        f"---\n\n# Add a widget\n\nBody.{tag}\n"
    )


def draft_envelope(
    *,
    task_id: str = TASK_ID,
    summary: str = "Add a widget",
    task_file: str | None = None,
    index_entry: str | None = None,
    notes=None,
    prose: bool = True,
) -> str:
    obj = {
        "task_id": task_id,
        "summary": summary,
        "task_file": task_file if task_file is not None else _task_file(task_id),
        "index_entry": index_entry
        if index_entry is not None
        else f"| {task_id} | {summary} | pending | 0/2 | [] |",
        "notes": notes,
    }
    block = f"{DRAFT_START}\n{json.dumps(obj)}\n{DRAFT_END}"
    if prose:
        return f"Here is the proposal.\n\n{block}\n\nTell me what to change."
    return block


def confirm_envelope(*, task_id: str = TASK_ID, task_path: str | None = None) -> str:
    obj: dict = {"task_id": task_id}
    if task_path is not None:
        obj["task_path"] = task_path
    return f"Wrote the task.\n{CONFIRM_START}\n{json.dumps(obj)}\n{CONFIRM_END}"


def stream(text: str, *, is_error: bool = False, returncode: int = 0) -> AgentRunResult:
    """A claude ``stream-json`` stdout carrying ``text`` as the assistant reply."""
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        ),
        json.dumps(
            {"type": "result", "subtype": "success", "is_error": is_error, "result": text}
        ),
    ]
    return AgentRunResult(stdout="\n".join(lines) + "\n", stderr="", returncode=returncode)


def stream_result_only(text: str) -> AgentRunResult:
    """A stream whose only text carrier is the ``result`` event (no assistant)."""
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text}),
    ]
    return AgentRunResult(stdout="\n".join(lines) + "\n", stderr="", returncode=0)


def make_repo(tmp_path: Path, name: str = "repo-a") -> ManagedRepo:
    path = tmp_path / "repos" / name
    (path / ".ai-tasks").mkdir(parents=True)
    (path / ".ai-tasks" / "index.md").write_text(
        "# Active tasks\n\n| id | title | status | session-est | blockers |\n"
        "|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def agent_with(*results: AgentRunResult, sid: str = "sid-1") -> tuple[ClaudeIntakeAgent, ScriptRunner]:
    runner = ScriptRunner(*results)
    return ClaudeIntakeAgent(runner=runner, new_session_id=lambda: sid), runner


# -- argv shapes + the read-only-until-confirm gate ---------------------------


def test_draft_argv_first_turn_picks_session_and_is_read_only(tmp_path):
    repo = make_repo(tmp_path)
    agent, runner = agent_with(stream(draft_envelope()), sid="sid-abc")

    result = agent.draft(REQUEST, repo)

    argv, cwd = runner.calls[0]
    assert argv[:5] == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--model") + 1] == DEFAULT_INTAKE_MODEL
    assert argv[argv.index("--effort") + 1] == DEFAULT_INTAKE_EFFORT
    # read-only gate: plan mode, session-id (not resume), never write-permitted.
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--session-id") + 1] == "sid-abc"
    assert "--resume" not in argv
    assert argv[-1].endswith(REQUEST) or REQUEST in argv[-1]  # prompt is last
    assert DRAFT_START in argv[-1]
    assert cwd == repo.path
    assert result.ref == "sid-abc"
    assert isinstance(result.handle, _ClaudeIntakeHandle)
    assert result.handle.sid == "sid-abc"
    assert result.handle.last_draft == result.draft


def test_revise_resumes_the_same_session_read_only(tmp_path):
    repo = make_repo(tmp_path)
    agent, runner = agent_with(
        stream(draft_envelope()),
        stream(draft_envelope(task_file=_task_file(tag=" revised"))),
        sid="sid-xyz",
    )

    drafted = agent.draft(REQUEST, repo)
    revised = agent.revise(drafted.handle, "Scope it to one route", repo)

    argv = runner.argvs[1]
    assert argv[argv.index("--resume") + 1] == "sid-xyz"
    assert "--session-id" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in argv
    assert "Scope it to one route" in argv[-1]
    assert "revised" in revised.draft.task_file
    assert revised.ref == "sid-xyz"
    assert revised.handle.last_draft == revised.draft


def test_confirm_resumes_with_write_permission(tmp_path):
    repo = make_repo(tmp_path)
    agent, runner = agent_with(
        stream(draft_envelope()),
        stream(confirm_envelope(task_path="/repos/repo-a/.ai-tasks/x.md")),
        sid="sid-w",
    )

    drafted = agent.draft(REQUEST, repo)
    confirmed = agent.confirm(drafted.handle, repo)

    argv = runner.argvs[1]
    assert argv[argv.index("--resume") + 1] == "sid-w"
    assert "--dangerously-skip-permissions" in argv
    assert "--permission-mode" not in argv
    assert confirmed.task_id == TASK_ID
    assert confirmed.task_path == Path("/repos/repo-a/.ai-tasks/x.md")


def test_default_tier_reaches_argv(tmp_path):
    # End of the config→argv chain: an unconfigured hub drafts tasks at the same
    # tier its dev sessions run at, and nothing between Config and the CLI call
    # quietly substitutes a cheaper one.
    repo = make_repo(tmp_path)
    config = Config.from_env({})
    runner = ScriptRunner(stream(draft_envelope()))
    agent = ClaudeIntakeAgent(
        model=config.intake_model,
        effort=config.intake_effort,
        runner=runner,
        new_session_id=lambda: "sid-1",
    )

    agent.draft(REQUEST, repo)

    argv = runner.argvs[0]
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert argv[argv.index("--effort") + 1] == "max"


def test_every_turn_kind_carries_the_configured_tier(tmp_path):
    # One tier for the whole conversation: the confirm turn takes the other argv
    # branch (write-permitted, not plan), so a downgrade hiding on just one turn
    # kind — the revise loop or the write turn — would not show up above.
    repo = make_repo(tmp_path)
    runner = ScriptRunner(
        stream(draft_envelope()),
        stream(draft_envelope(task_file=_task_file(tag=" revised"))),
        stream(confirm_envelope()),
    )
    agent = ClaudeIntakeAgent(
        model="claude-x", effort="low", runner=runner, new_session_id=lambda: "sid-t"
    )

    drafted = agent.draft(REQUEST, repo)
    revised = agent.revise(drafted.handle, "Scope it down", repo)
    agent.confirm(revised.handle, repo)

    assert len(runner.argvs) == 3
    for argv in runner.argvs:
        assert argv[argv.index("--model") + 1] == "claude-x"
        assert argv[argv.index("--effort") + 1] == "low"


# -- draft envelope parsing ---------------------------------------------------


def test_draft_parses_all_fields(tmp_path):
    repo = make_repo(tmp_path)
    agent, _ = agent_with(
        stream(draft_envelope(summary="Widget route", notes="overlaps task Y"))
    )

    draft = agent.draft(REQUEST, repo).draft

    assert draft == IntakeDraft(
        task_id=TASK_ID,
        summary="Widget route",
        task_file=_task_file(),
        index_entry=f"| {TASK_ID} | Widget route | pending | 0/2 | [] |",
        notes="overlaps task Y",
    )


def test_draft_notes_null_and_missing_summary_are_tolerated(tmp_path):
    repo = make_repo(tmp_path)
    body = draft_envelope(notes=None, summary="")
    agent, _ = agent_with(stream(body))

    draft = agent.draft(REQUEST, repo).draft

    assert draft.notes is None
    assert draft.summary == ""
    assert draft.task_id == TASK_ID  # required fields still parsed


def test_draft_reads_result_event_when_no_assistant_text(tmp_path):
    repo = make_repo(tmp_path)
    agent, _ = agent_with(stream_result_only(draft_envelope(prose=False)))

    draft = agent.draft(REQUEST, repo).draft

    assert draft.task_id == TASK_ID


def test_draft_ignores_non_json_lines_and_prose_mention(tmp_path):
    repo = make_repo(tmp_path)
    # A non-JSON preamble line, plus the marker named in prose before the real
    # block: rfind must select the real (last) envelope, not the prose mention.
    text = (
        f"I will emit the {DRAFT_START} block once ready.\n\n"
        f"{draft_envelope(prose=False)}"
    )
    result = AgentRunResult(
        stdout="not json at all\n"
        + json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
        )
        + "\n"
        + json.dumps({"type": "result", "is_error": False, "result": text})
        + "\n",
        stderr="",
        returncode=0,
    )
    agent, _ = agent_with(result)

    assert agent.draft(REQUEST, repo).draft.task_id == TASK_ID


# -- failure paths ------------------------------------------------------------


def test_nonzero_exit_raises_with_stderr_tail(tmp_path):
    repo = make_repo(tmp_path)
    runner = ScriptRunner(
        AgentRunResult(stdout="", stderr="boom: claude crashed", returncode=2)
    )
    agent = ClaudeIntakeAgent(runner=runner)

    with pytest.raises(IntakeAgentError) as exc:
        agent.draft(REQUEST, repo)
    assert "exit 2" in str(exc.value)
    assert "boom: claude crashed" in str(exc.value)


def test_stream_result_error_raises(tmp_path):
    repo = make_repo(tmp_path)
    agent, _ = agent_with(stream(draft_envelope(), is_error=True))

    with pytest.raises(IntakeAgentError) as exc:
        agent.draft(REQUEST, repo)
    assert "stream_error=True" in str(exc.value)


def test_missing_marker_raises(tmp_path):
    repo = make_repo(tmp_path)
    agent, _ = agent_with(stream("I could not produce a task, sorry."))

    with pytest.raises(IntakeAgentError, match=DRAFT_START):
        agent.draft(REQUEST, repo)


def test_invalid_json_envelope_raises(tmp_path):
    repo = make_repo(tmp_path)
    agent, _ = agent_with(stream(f"{DRAFT_START}\n{{not valid json,}}\n{DRAFT_END}"))

    with pytest.raises(IntakeAgentError, match="invalid JSON"):
        agent.draft(REQUEST, repo)


def test_missing_required_key_raises(tmp_path):
    repo = make_repo(tmp_path)
    obj = {"task_id": TASK_ID, "summary": "s", "index_entry": "| ... |"}  # no task_file
    agent, _ = agent_with(stream(f"{DRAFT_START}\n{json.dumps(obj)}\n{DRAFT_END}"))

    with pytest.raises(IntakeAgentError, match="task_file"):
        agent.draft(REQUEST, repo)


# -- best-effort agent-subscription-usage observer feed -----------------------
# The hub drives these claude -p turns, so their stream-json is where Claude's 5h/7d
# usage is seen without a probe turn. The agent forwards each turn's raw stdout to an
# injected observer, best-effort: it feeds every turn (even one that then errors), and
# a missing/raising observer never affects intake. The stream-json -> observation
# parsing itself lives in orch_hub.usage and is tested in test_usage.py.


def test_draft_turn_feeds_usage_observer(tmp_path):
    repo = make_repo(tmp_path)
    seen: list[str] = []
    turn = stream(draft_envelope())
    agent = ClaudeIntakeAgent(
        runner=ScriptRunner(turn), new_session_id=lambda: "sid-u", usage_observer=seen.append
    )

    result = agent.draft(REQUEST, repo)

    assert seen == [turn.stdout]  # the turn's raw stdout reached the observer
    assert result.draft.task_id == TASK_ID  # drafting is unaffected by the feed


def test_usage_observer_failure_does_not_break_intake(tmp_path):
    repo = make_repo(tmp_path)

    def boom(_stdout: str) -> None:
        raise RuntimeError("usage bookkeeping boom")

    agent = ClaudeIntakeAgent(
        runner=ScriptRunner(stream(draft_envelope())), new_session_id=lambda: "s", usage_observer=boom
    )

    result = agent.draft(REQUEST, repo)  # must not raise
    assert result.draft.task_id == TASK_ID


def test_usage_observer_fires_even_when_turn_errors(tmp_path):
    # The feed runs before the turn's success check, so a turn that consumed usage but
    # then fails still records it. A raising turn still raises IntakeAgentError.
    repo = make_repo(tmp_path)
    seen: list[str] = []
    turn = stream("boom", is_error=True, returncode=1)
    agent = ClaudeIntakeAgent(
        runner=ScriptRunner(turn), new_session_id=lambda: "s", usage_observer=seen.append
    )

    with pytest.raises(IntakeAgentError):
        agent.draft(REQUEST, repo)
    assert seen == [turn.stdout]


# -- confirm fallback + failure -----------------------------------------------


def test_confirm_without_envelope_falls_back_to_draft_id_and_path(tmp_path):
    repo = make_repo(tmp_path)
    agent, _ = agent_with(
        stream(draft_envelope()),
        stream("All done, the files are written."),  # no confirm envelope
        sid="s",
    )

    drafted = agent.draft(REQUEST, repo)
    confirmed = agent.confirm(drafted.handle, repo)

    assert confirmed.task_id == TASK_ID
    assert confirmed.task_path == repo.path / ".ai-tasks" / f"{TASK_ID}.md"


def test_confirm_partial_envelope_derives_missing_path(tmp_path):
    repo = make_repo(tmp_path)
    agent, _ = agent_with(
        stream(draft_envelope()),
        stream(confirm_envelope(task_id="2026-07-11-add-widget-2", task_path=None)),
        sid="s",
    )

    drafted = agent.draft(REQUEST, repo)
    confirmed = agent.confirm(drafted.handle, repo)

    assert confirmed.task_id == "2026-07-11-add-widget-2"
    assert confirmed.task_path == repo.path / ".ai-tasks" / "2026-07-11-add-widget-2.md"


def test_confirm_turn_failure_raises(tmp_path):
    repo = make_repo(tmp_path)
    agent, _ = agent_with(
        stream(draft_envelope()),
        AgentRunResult(stdout="", stderr="write denied", returncode=1),
        sid="s",
    )

    drafted = agent.draft(REQUEST, repo)
    with pytest.raises(IntakeAgentError):
        agent.confirm(drafted.handle, repo)


def test_cancel_is_a_noop_and_never_runs(tmp_path):
    runner = ScriptRunner()  # no scripted results: any call would raise
    agent = ClaudeIntakeAgent(runner=runner)

    assert agent.cancel(_ClaudeIntakeHandle("s")) is None
    assert runner.calls == []


def test_unexpected_handle_type_raises(tmp_path):
    repo = make_repo(tmp_path)
    agent = ClaudeIntakeAgent(runner=ScriptRunner())

    with pytest.raises(IntakeAgentError, match="unexpected intake handle"):
        agent.confirm(object(), repo)


# -- end-to-end through the real IntakeManager --------------------------------


def sync_executor(run) -> None:
    """Run each intake turn inline so a mutating call settles before it returns.

    The manager runs agent turns off its lock through an injected executor (a
    daemon thread in production); this synchronous one keeps the end-to-end
    assertions deterministic — the mutating call returns the transient record and
    a follow-up ``get_intake`` observes the settled outcome.
    """
    run()


def passing_readiness(repo, config) -> ReadinessReport:
    return ReadinessReport((ReadinessCheck("all", True, "ok"),))


def snapshot(repo: ManagedRepo) -> dict[str, bytes]:
    tasks = repo.path / ".ai-tasks"
    return {p.name: p.read_bytes() for p in sorted(tasks.iterdir()) if p.is_file()}


class ClaudeLikeRunner:
    """A faithful fake: read-only turns return a draft envelope and touch no
    file; the write turn writes the task file + index row (as real claude would
    with ``--dangerously-skip-permissions``) and returns a confirm envelope."""

    def __init__(self, task_id: str = TASK_ID):
        self.task_id = task_id
        self.reads = 0
        self.argvs: list[list[str]] = []

    def __call__(self, argv, cwd) -> AgentRunResult:
        self.argvs.append(list(argv))
        if "--dangerously-skip-permissions" in argv:
            tasks = Path(cwd) / ".ai-tasks"
            task_path = tasks / f"{self.task_id}.md"
            task_path.write_text(_task_file(self.task_id), encoding="utf-8")
            index = tasks / "index.md"
            index.write_text(
                index.read_text(encoding="utf-8")
                + f"| {self.task_id} | Add a widget | pending | 0/2 | [] |\n",
                encoding="utf-8",
            )
            return stream(confirm_envelope(task_id=self.task_id, task_path=str(task_path)))
        self.reads += 1
        return stream(draft_envelope(task_file=_task_file(tag=f" read#{self.reads}")))


def test_manager_end_to_end_is_read_only_until_confirm(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    runner = ClaudeLikeRunner()
    agent = ClaudeIntakeAgent(runner=runner, new_session_id=lambda: "sid-e2e")
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data")
    manager = IntakeManager(
        config,
        agent=agent,
        readiness_checker=passing_readiness,
        clock=lambda: FIXED_NOW,
        executor=sync_executor,
    )

    transient = manager.start_intake(repo, REQUEST)
    assert transient.status == STATUS_DRAFTING  # returned before the off-lock turn
    record = manager.get_intake(transient.intake_id)
    assert record.status == STATUS_AWAITING_REVIEW
    assert record.draft.task_id == TASK_ID
    assert snapshot(repo) == before  # drafting wrote nothing to the repo

    manager.revise_intake(record.intake_id, "Scope to one route")
    revised = manager.get_intake(record.intake_id)
    assert revised.iteration == 1
    assert "read#2" in revised.draft.task_file
    assert snapshot(repo) == before  # revision wrote nothing to the repo

    manager.confirm_intake(record.intake_id)
    confirmed = manager.get_intake(record.intake_id)
    assert confirmed.status == STATUS_CONFIRMED
    assert confirmed.created_task_id == TASK_ID
    assert (repo.path / ".ai-tasks" / f"{TASK_ID}.md").exists()
    assert snapshot(repo) != before  # only confirm wrote the task

    # The read-only gate was honored per turn: draft + revise under plan mode,
    # confirm the sole write-permitted turn.
    assert "--permission-mode" in runner.argvs[0] and "plan" in runner.argvs[0]
    assert "--dangerously-skip-permissions" not in runner.argvs[0]
    assert "--dangerously-skip-permissions" not in runner.argvs[1]
    assert "--dangerously-skip-permissions" in runner.argvs[2]
    # Session threading: one chosen id, resumed on every later turn.
    assert runner.argvs[0][runner.argvs[0].index("--session-id") + 1] == "sid-e2e"
    assert runner.argvs[1][runner.argvs[1].index("--resume") + 1] == "sid-e2e"
    assert runner.argvs[2][runner.argvs[2].index("--resume") + 1] == "sid-e2e"


class WriteNothingRunner:
    """Read turns return a draft; the write turn returns a confirm envelope but
    writes NO files — a real agent claiming success (exit 0) having written
    nothing. Proves the manager verifies the postcondition, not the exit code."""

    def __call__(self, argv, cwd) -> AgentRunResult:
        if "--dangerously-skip-permissions" in argv:
            return stream(confirm_envelope())  # claims done; touches no file
        return stream(draft_envelope())


def test_manager_confirm_fails_when_backend_writes_nothing(tmp_path):
    repo = make_repo(tmp_path)
    before = snapshot(repo)
    agent = ClaudeIntakeAgent(runner=WriteNothingRunner(), new_session_id=lambda: "sid")
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data")
    manager = IntakeManager(
        config,
        agent=agent,
        readiness_checker=passing_readiness,
        clock=lambda: FIXED_NOW,
        executor=sync_executor,
    )

    transient = manager.start_intake(repo, REQUEST)
    assert transient.status == STATUS_DRAFTING
    assert manager.get_intake(transient.intake_id).status == STATUS_AWAITING_REVIEW

    confirming = manager.confirm_intake(transient.intake_id)
    assert confirming.status == STATUS_CONFIRMING  # transient; write runs off-lock
    confirmed = manager.get_intake(transient.intake_id)

    # The backend turn exited 0 with a confirm envelope but wrote nothing; the
    # manager's inventory postcondition catches it and records ``failed``, not a
    # phantom ``confirmed``. The target repo is untouched.
    assert confirmed.status == STATUS_FAILED
    assert confirmed.created_task_id is None
    assert snapshot(repo) == before
