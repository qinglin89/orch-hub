"""Tests for the task-evaluation foundation.

Covers the four session-1 acceptance areas: active/archive target resolution,
repo/task run-aggregation scoping, disabled-mode fail-closed behavior, and safe
artifact-path layout. Fake repos, task files, and run dirs live in ``tmp_path``;
the run-record loader is injected for the scoping matrix and exercised for real
(through :func:`orch_hub.runs.load_records`) in the integration tests, so no run
manager or subprocess is ever involved.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.evaluation import (
    EVALUATIONS_SUBDIR,
    EVIDENCE_COMMIT_LIMIT,
    EVIDENCE_LOG_MAX_LINES,
    EVIDENCE_NAME,
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    EvaluationService,
    evaluation_artifact_dir,
    task_evaluations_dir,
)
from orch_hub.models import ManagedRepo, RunRecord, StoredEvaluation
from orch_hub.subprocess_utils import CommandResult

TASK = "2026-07-22-task-evaluation-foundation"

FIXED_CLOCK = dt.datetime(2026, 7, 22, 9, 0, 0, tzinfo=dt.timezone.utc)


def make_config(tmp_path: Path, *, enabled: bool = True) -> Config:
    return Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=enabled,
    )


def fake_git(*, stdout: str = "", rc: int = 128, calls: list | None = None):
    """A fake git runner: records argv and returns canned output (default fails).

    Defaults to a non-zero exit so a build/generate test that does not care about
    git gets a deterministic ``available=False`` window without a real subprocess."""

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if calls is not None:
            calls.append(argv)
        return CommandResult(tuple(argv), rc, stdout, "")

    return runner


def commit_line(subject: str, *, h: str = "a" * 40) -> str:
    """One ``%H\\x1f%h\\x1f%cI\\x1f%s`` git-log line the window query parses."""
    return "\x1f".join((h, h[:7], "2026-07-22T09:30:00+00:00", subject))


def write_run(
    config: Config,
    run_id: str,
    *,
    repo: ManagedRepo,
    task_id: str = TASK,
    started_ts: str | None = None,
    ended_ts: str | None = None,
    exit_code: int | None = None,
    log_lines: list[str] | None = None,
    spawn_lines: list[str] | None = None,
    events: list[dict] | None = None,
    events_raw: str | None = None,
    sessions: dict | None = None,
) -> Path:
    """Write a run dir with a rich ``run.json`` plus optional run artifacts.

    Creates only the artifacts a test asks for, so an omitted one exercises the
    absent/missing path. The orchestrator log is stamped from ``started_ts`` so
    :func:`orch_hub.logs.locate_log` attributes it to this run."""
    run_dir = config.data_dir / "runs" / run_id
    (run_dir / "control").mkdir(parents=True)
    log_dir = repo.path / ".cursor" / "orchestrator" / "logs"
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo_name": repo.name,
                "repo_path": str(repo.path),
                "task_id": task_id,
                "started_ts": started_ts,
                "ended_ts": ended_ts,
                "exit_code": exit_code,
                "log_dir": str(log_dir),
                "spawn_out": str(run_dir / "spawn.out"),
            }
        ),
        encoding="utf-8",
    )
    if spawn_lines is not None:
        (run_dir / "spawn.out").write_text("\n".join(spawn_lines) + "\n", encoding="utf-8")
    if events_raw is not None:
        (run_dir / "events.jsonl").write_text(events_raw, encoding="utf-8")
    elif events is not None:
        (run_dir / "events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
        )
    if sessions is not None:
        (run_dir / "sessions.json").write_text(json.dumps(sessions), encoding="utf-8")
    if log_lines is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.fromisoformat(started_ts).strftime("%Y%m%dT%H%M%SZ")
        (log_dir / f"{stamp}-{task_id}.log").write_text(
            "\n".join(log_lines) + "\n", encoding="utf-8"
        )
    return run_dir


def make_repo(tmp_path: Path, name: str = "alpha") -> ManagedRepo:
    """Fake managed repo with empty ``.ai-tasks`` and ``.ai-tasks/archive`` dirs."""
    path = tmp_path / "repos" / name
    (path / ".ai-tasks" / "archive").mkdir(parents=True)
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def write_active_task(repo: ManagedRepo, task_id: str, body: str) -> None:
    (repo.path / ".ai-tasks" / f"{task_id}.md").write_text(
        f"---\nid: {task_id}\nstatus: in_progress\n---\n\n{body}\n", encoding="utf-8"
    )


def write_archived_task(repo: ManagedRepo, task_id: str, body: str) -> None:
    (repo.path / ".ai-tasks" / "archive" / f"{task_id}.md").write_text(
        f"---\nid: {task_id}\nstatus: completed\n---\n\n{body}\n", encoding="utf-8"
    )


def write_run_dir(
    config: Config,
    run_id: str,
    *,
    repo_path: Path,
    task_id: str,
    repo_name: str = "alpha",
) -> Path:
    """Write a minimal real ``run.json`` (the fields ``_load_record`` reads)."""
    run_dir = config.data_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo_name": repo_name,
                "repo_path": str(repo_path),
                "task_id": task_id,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


# -- target resolution (active / archive) -------------------------------------


def test_resolve_target_reads_active_task_file(tmp_path):
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# Active goal")
    service = EvaluationService(make_config(tmp_path))

    target = service.resolve_target(repo, TASK)

    assert target.source == SOURCE_ACTIVE
    assert target.present is True
    assert "# Active goal" in target.content
    assert target.repo_name == "alpha"
    assert target.repo_path == repo.path
    assert target.task_id == TASK
    assert target.to_dict()["repo_path"] == str(repo.path)


def test_resolve_target_falls_back_to_archived_task_file(tmp_path):
    repo = make_repo(tmp_path)
    write_archived_task(repo, TASK, "# Archived goal")
    service = EvaluationService(make_config(tmp_path))

    target = service.resolve_target(repo, TASK)

    assert target.source == SOURCE_ARCHIVED
    assert target.present is True
    assert "# Archived goal" in target.content


def test_resolve_target_prefers_active_over_archive(tmp_path):
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# Active wins")
    write_archived_task(repo, TASK, "# Archive copy")
    service = EvaluationService(make_config(tmp_path))

    target = service.resolve_target(repo, TASK)

    assert target.source == SOURCE_ACTIVE
    assert "# Active wins" in target.content
    assert "# Archive copy" not in target.content


def test_resolve_target_absent_is_present_false(tmp_path):
    repo = make_repo(tmp_path)  # no task file written, active or archived
    service = EvaluationService(make_config(tmp_path))

    target = service.resolve_target(repo, TASK)

    assert target.present is False
    assert target.source is None
    assert target.content is None
    assert target.repo_path == repo.path  # still attributable for run aggregation


# -- related-run aggregation (repo/task scoping) ------------------------------


def test_related_runs_scopes_by_repo_and_task_with_injected_loader(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path, name="alpha")

    def loader(_cfg):
        return [
            (Path("/runs/r1"), RunRecord("r1", "alpha", repo.path, TASK)),
            (Path("/runs/r2"), RunRecord("r2", "alpha", repo.path, TASK)),
            (Path("/runs/r3"), RunRecord("r3", "alpha", repo.path, "2026-01-01-other")),
            (Path("/runs/r4"), RunRecord("r4", "beta", tmp_path / "beta", TASK)),
            (Path("/runs/r5"), RunRecord("r5", error="broken run.json")),  # no attribution
        ]

    service = EvaluationService(config, record_loader=loader)
    related = service.related_runs(repo, TASK)

    # Only this repo's runs of this exact task, in loader (oldest-first) order.
    assert [r.record.run_id for r in related] == ["r1", "r2"]
    assert [r.run_dir for r in related] == [Path("/runs/r1"), Path("/runs/r2")]
    assert related[0].to_dict() == related[0].record.to_dict()  # run_dir stays internal


def test_related_runs_empty_when_no_runs(tmp_path):
    service = EvaluationService(make_config(tmp_path), record_loader=lambda _cfg: [])
    assert service.related_runs(make_repo(tmp_path), TASK) == []


def test_related_runs_reads_real_persisted_records(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path, name="alpha")
    other = make_repo(tmp_path, name="beta")
    d1 = write_run_dir(config, "20260722T090000Z-alpha-task", repo_path=repo.path, task_id=TASK)
    d2 = write_run_dir(config, "20260722T100000Z-alpha-task", repo_path=repo.path, task_id=TASK)
    write_run_dir(config, "20260722T110000Z-alpha-other", repo_path=repo.path, task_id="2026-01-01-other")
    write_run_dir(config, "20260722T120000Z-beta-task", repo_path=other.path, task_id=TASK, repo_name="beta")

    related = EvaluationService(config).related_runs(repo, TASK)  # default real loader

    assert [r.run_dir for r in related] == [d1, d2]
    assert all(r.record.repo_path == repo.path and r.record.task_id == TASK for r in related)


def test_related_runs_excludes_unreadable_run_dir(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_run_dir(config, "20260722T090000Z-alpha-task", repo_path=repo.path, task_id=TASK)
    (config.data_dir / "runs" / "20260722T095959Z-broken").mkdir(parents=True)  # no run.json

    related = EvaluationService(config).related_runs(repo, TASK)

    assert len(related) == 1  # the error stub has no repo_path, so it cannot match
    assert related[0].record.error is None


# -- disabled mode (fail-closed backend boundary) -----------------------------


def test_service_reports_enabled_flag(tmp_path):
    assert EvaluationService(make_config(tmp_path)).enabled is True
    assert EvaluationService(make_config(tmp_path, enabled=False)).enabled is False


def test_disabled_service_refuses_every_operation(tmp_path):
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = EvaluationService(make_config(tmp_path, enabled=False))

    with pytest.raises(EvaluationDisabledError):
        service.resolve_target(repo, TASK)
    with pytest.raises(EvaluationDisabledError):
        service.related_runs(repo, TASK)
    with pytest.raises(EvaluationDisabledError):
        service.artifact_dir(repo, TASK, "eval-1")
    with pytest.raises(EvaluationDisabledError):
        service.list_evaluations(repo, TASK)
    with pytest.raises(EvaluationDisabledError):
        service.read_artifact(repo, TASK, "eval-1", EVIDENCE_NAME)


def test_disabled_service_does_not_touch_the_loader(tmp_path):
    def loader(_cfg):
        raise AssertionError("loader must not run while evaluation is disabled")

    service = EvaluationService(make_config(tmp_path, enabled=False), record_loader=loader)
    with pytest.raises(EvaluationDisabledError):
        service.related_runs(make_repo(tmp_path), TASK)


# -- artifact path layout (safe, contained) -----------------------------------


def test_artifact_dir_is_scoped_and_contained(tmp_path):
    data_dir = tmp_path / "hub-data"
    base = data_dir / EVALUATIONS_SUBDIR

    path = evaluation_artifact_dir(
        data_dir, repo_name="alpha", task_id=TASK, evaluation_id="eval-1"
    )

    assert path == base / "alpha" / TASK / "eval-1"
    assert path.resolve().is_relative_to(base.resolve())


def test_artifact_dir_slugs_repo_name(tmp_path):
    path = evaluation_artifact_dir(
        tmp_path, repo_name="weird/name space", task_id=TASK, evaluation_id="e1"
    )
    assert path.parent.parent.name == "weird-name-space"


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "a\x00b", ".", "..", ""])
def test_artifact_dir_rejects_unsafe_task_id(tmp_path, bad):
    with pytest.raises(ValueError):
        evaluation_artifact_dir(tmp_path, repo_name="alpha", task_id=bad, evaluation_id="e1")


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "a\x00b", ".", "..", ""])
def test_artifact_dir_rejects_unsafe_evaluation_id(tmp_path, bad):
    with pytest.raises(ValueError):
        evaluation_artifact_dir(tmp_path, repo_name="alpha", task_id=TASK, evaluation_id=bad)


def test_artifact_dir_rejects_dot_dot_repo_name(tmp_path):
    # A repo name that slugs to a dot reference must not escape the tree.
    with pytest.raises(ValueError):
        evaluation_artifact_dir(tmp_path, repo_name="..", task_id=TASK, evaluation_id="e1")


def test_service_artifact_dir_delegates_and_is_gated(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)

    path = EvaluationService(config).artifact_dir(repo, TASK, "eval-1")

    assert path == evaluation_artifact_dir(
        config.data_dir, repo_name=repo.name, task_id=TASK, evaluation_id="eval-1"
    )


# -- evidence pack build (aggregation, bounded, tolerant) ---------------------


def make_service(config, *, git_runner=None, clock=None, **kwargs):
    """Build a service with a fake git runner (no real subprocess) by default."""
    return EvaluationService(
        config,
        git_runner=git_runner if git_runner is not None else fake_git(),
        clock=clock if clock is not None else (lambda: FIXED_CLOCK),
        **kwargs,
    )


def test_build_evidence_aggregates_active_task_and_its_runs(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# Active goal")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        exit_code=0,
        log_lines=["[09:00:00] --- dev session start: sid=s1", "hello world"],
        events=[{"ts": "t", "kind": "run_started", "run_id": "r", "task_id": TASK}],
        sessions={
            "sessions": [{"role": "dev", "kind": "session", "turns": 2}],
            "context_budget": 200000,
        },
    )
    write_run(
        config,
        "20260722T100000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T10:00:00+00:00",  # still live (no ended_ts)
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.evaluation_id == "e1"
    assert pack.generated_ts == FIXED_CLOCK.isoformat()
    assert pack.target.source == SOURCE_ACTIVE and pack.target.present is True
    # Both runs of this task, oldest-first.
    assert [r.record.run_id for r in pack.runs] == [
        "20260722T090000Z-alpha-task",
        "20260722T100000Z-alpha-task",
    ]
    first, second = pack.runs
    assert first.ended is True and second.ended is False
    assert first.log is not None and first.log.present is True
    assert "hello world" in first.log.lines
    assert first.events and first.events[0].kind == "run_started"
    assert first.sessions is not None and first.sessions.sessions[0].role == "dev"
    # A run with no artifacts: absent, never an error.
    assert second.events == () and second.sessions is None
    assert second.log is not None and second.log.present is False
    # Serialized summary validates the pack.
    summary = pack.to_dict()["summary"]
    assert summary == {
        "run_count": 2,
        "runs_ended": 1,
        "has_task_content": True,
        "target_source": SOURCE_ACTIVE,
        "git_available": False,  # fake git defaults to a failed window
        "warning_count": 0,
    }


def test_build_evidence_archived_task(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_archived_task(repo, TASK, "# Archived goal")

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.target.source == SOURCE_ARCHIVED
    assert pack.target.present is True
    assert "# Archived goal" in pack.target.content


def test_build_evidence_absent_task_still_aggregates_runs(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)  # no task file, active or archived
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.target.present is False and pack.target.source is None
    assert pack.target.content is None
    assert [r.record.run_id for r in pack.runs] == ["20260722T090000Z-alpha-task"]


def test_build_evidence_excludes_other_repo_and_task_runs(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path, name="alpha")
    other = make_repo(tmp_path, name="beta")
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")
    write_run(config, "20260722T093000Z-alpha-other", repo=repo, task_id="other-task",
              started_ts="2026-07-22T09:30:00+00:00")
    write_run(config, "20260722T100000Z-beta-task", repo=other,
              started_ts="2026-07-22T10:00:00+00:00")

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert [r.record.run_id for r in pack.runs] == ["20260722T090000Z-alpha-task"]


def test_build_evidence_bounds_large_log(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    big = [f"line {i}" for i in range(EVIDENCE_LOG_MAX_LINES + 50)]
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00", log_lines=big)

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    log = pack.runs[0].log
    assert log is not None and log.present is True
    assert len(log.lines) == EVIDENCE_LOG_MAX_LINES  # bounded tail
    assert log.truncated is True
    assert log.lines[-1] == f"line {EVIDENCE_LOG_MAX_LINES + 49}"  # newest kept


def test_build_evidence_spawn_out_diagnostic_present(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00",
              spawn_lines=["Traceback: boom", "ModuleNotFoundError: claude"])

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    spawn = pack.runs[0].spawn_out
    assert spawn is not None and spawn.present is True
    assert "ModuleNotFoundError: claude" in spawn.lines


def test_build_evidence_corrupt_history_degrades_with_warning(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00",
              events_raw='{"ts":"t","kind":"run_started","run_id":"r"}\nnot json {\n')

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    run = pack.runs[0]
    # Corrupt line surfaces as an inline history_error event plus a run warning;
    # the pack still builds (partial evidence, not a failure).
    assert any(e.kind == "history_error" for e in run.events)
    assert any("corrupt history" in w for w in run.warnings)
    assert pack.to_dict()["summary"]["warning_count"] == 1


def test_build_evidence_isolates_unexpected_run_read_failure(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")

    def boom(_run_dir):
        raise OSError("disk gone")

    # Force an unexpected failure in a per-run read; the isolation boundary must
    # convert it to a warning and still return a pack.
    monkeypatch.setattr("orch_hub.evaluation.read_snapshot", boom)
    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.runs[0].sessions is None
    assert any("session snapshot unreadable" in w for w in pack.runs[0].warnings)


# -- git-window evidence (best-effort, bounded) -------------------------------


def test_git_window_from_run_timestamps(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00", ended_ts="2026-07-22T09:30:00+00:00")
    write_run(config, "20260722T100000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T10:00:00+00:00", ended_ts="2026-07-22T10:45:00+00:00")
    calls: list = []
    git = fake_git(rc=0, stdout=commit_line("feat: land it"), calls=calls)

    pack = make_service(config, git_runner=git).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.git.available is True
    assert pack.git.since == "2026-07-22T09:00:00+00:00"  # earliest start
    assert pack.git.until == "2026-07-22T10:45:00+00:00"  # latest end
    assert [c.subject for c in pack.git.commits] == ["feat: land it"]
    # The window bounds and the hard cap reach git.
    argv = calls[0]
    assert "--since=2026-07-22T09:00:00+00:00" in argv
    assert "--until=2026-07-22T10:45:00+00:00" in argv
    assert f"-{EVIDENCE_COMMIT_LIMIT}" in argv


def test_git_window_open_when_no_run_ended(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")  # live: no ended_ts
    calls: list = []

    pack = make_service(config, git_runner=fake_git(rc=0, calls=calls)).build_evidence(
        repo, TASK, evaluation_id="e1"
    )

    assert pack.git.since == "2026-07-22T09:00:00+00:00"
    assert pack.git.until is None
    assert not any(a.startswith("--until=") for a in calls[0])


def test_git_window_open_when_later_run_is_live(tmp_path):
    # Mixed ended+live runs: an earlier run ended, a later run is still live. The
    # live run's activity runs to now, so the window must stay open (until=None)
    # instead of closing at the earlier run's end and dropping commits made during
    # the live run.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00", ended_ts="2026-07-22T09:30:00+00:00")
    write_run(config, "20260722T100000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T10:00:00+00:00")  # later run: still live
    calls: list = []

    pack = make_service(config, git_runner=fake_git(rc=0, calls=calls)).build_evidence(
        repo, TASK, evaluation_id="e1"
    )

    assert pack.git.since == "2026-07-22T09:00:00+00:00"  # earliest start
    assert pack.git.until is None  # later run still live => window stays open
    assert not any(a.startswith("--until=") for a in calls[0])


def test_git_window_unavailable_without_run_timestamps(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo)  # no started_ts
    calls: list = []

    pack = make_service(config, git_runner=fake_git(calls=calls)).build_evidence(
        repo, TASK, evaluation_id="e1"
    )

    assert pack.git.available is False
    assert "window" in pack.git.detail
    assert calls == []  # no window => git never invoked


def test_git_window_unavailable_on_git_failure(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")

    pack = make_service(config, git_runner=fake_git(rc=128)).build_evidence(
        repo, TASK, evaluation_id="e1"
    )

    assert pack.git.available is False
    assert pack.git.detail == "git history unavailable"
    assert pack.git.commits == ()


def test_git_window_truncated_at_limit(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")
    stdout = "\n".join(
        commit_line(f"c{i}", h=f"{i:040d}") for i in range(EVIDENCE_COMMIT_LIMIT)
    )

    pack = make_service(config, git_runner=fake_git(rc=0, stdout=stdout)).build_evidence(
        repo, TASK, evaluation_id="e1"
    )

    assert len(pack.git.commits) == EVIDENCE_COMMIT_LIMIT
    assert pack.git.truncated is True


# -- evidence pack persistence (safe, atomic, read-only) ----------------------


def test_generate_evidence_persists_pack_under_contained_dir(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")

    pack = make_service(config).generate_evidence(repo, TASK)

    base = config.data_dir / EVALUATIONS_SUBDIR
    assert pack.evaluation_dir == base / "alpha" / TASK / pack.evaluation_id
    assert pack.evaluation_dir.resolve().is_relative_to(base.resolve())
    evidence_file = pack.evaluation_dir / EVIDENCE_NAME
    assert evidence_file.is_file()
    # evidence.json round-trips to the pack's serialized form.
    on_disk = json.loads(evidence_file.read_text())
    assert on_disk == pack.to_dict()
    assert on_disk["evaluation_id"] == pack.evaluation_id
    assert on_disk["summary"]["run_count"] == 1
    # No stray temp file left behind by the atomic write.
    assert list(pack.evaluation_dir.iterdir()) == [evidence_file]


def test_generate_evidence_id_is_timestamped_and_collision_safe(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)  # fixed clock => same base id both calls

    first = service.generate_evidence(repo, TASK)
    second = service.generate_evidence(repo, TASK)

    assert first.evaluation_id == "20260722T090000Z"
    assert second.evaluation_id == "20260722T090000Z-2"  # collision bumps a suffix
    assert first.evaluation_dir != second.evaluation_dir
    assert (first.evaluation_dir / EVIDENCE_NAME).is_file()
    assert (second.evaluation_dir / EVIDENCE_NAME).is_file()


def test_generate_evidence_is_read_only_over_target(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    run_dir = write_run(config, "20260722T090000Z-alpha-task", repo=repo,
                        started_ts="2026-07-22T09:00:00+00:00",
                        events=[{"ts": "t", "kind": "run_started", "run_id": "r"}])
    task_file = repo.path / ".ai-tasks" / f"{TASK}.md"
    before_task = task_file.read_bytes()
    before_run = (run_dir / "run.json").read_bytes()
    before_events = (run_dir / "events.jsonl").read_bytes()

    make_service(config).generate_evidence(repo, TASK)

    assert task_file.read_bytes() == before_task
    assert (run_dir / "run.json").read_bytes() == before_run
    assert (run_dir / "events.jsonl").read_bytes() == before_events
    # Nothing new was written inside the target repo.
    assert not (repo.path / EVALUATIONS_SUBDIR).exists()


def test_generate_evidence_rejects_unsafe_task_id(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)

    # A path-escaping task id fails closed at the artifact-path boundary and
    # creates nothing under the evaluations tree.
    with pytest.raises(ValueError):
        make_service(config).generate_evidence(repo, "../evil")
    assert not (config.data_dir / EVALUATIONS_SUBDIR).exists()


# -- disabled mode (fail-closed for build + persist) --------------------------


def test_disabled_service_refuses_build_and_generate(tmp_path):
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")

    def loader(_cfg):
        raise AssertionError("loader must not run while evaluation is disabled")

    service = EvaluationService(
        make_config(tmp_path, enabled=False),
        record_loader=loader,
        git_runner=fake_git(),
        clock=lambda: FIXED_CLOCK,
    )
    with pytest.raises(EvaluationDisabledError):
        service.build_evidence(repo, TASK, evaluation_id="e1")
    with pytest.raises(EvaluationDisabledError):
        service.generate_evidence(repo, TASK)
    # Nothing was persisted while disabled.
    assert not (make_config(tmp_path).data_dir / EVALUATIONS_SUBDIR).exists()


# -- persisted-evaluation read layer (list / read) ----------------------------
#
# The dashboard-facing reads over persisted artifacts: enumerate a task's
# evaluations newest-first with their present artifact filenames, and read one
# artifact's JSON back. Both are gated and contained like the write path.


def test_task_evaluations_dir_is_scoped_and_contained(tmp_path):
    base = tmp_path / EVALUATIONS_SUBDIR
    path = task_evaluations_dir(tmp_path, repo_name="alpha", task_id=TASK)
    assert path == base / "alpha" / TASK
    assert path.resolve().is_relative_to(base.resolve())


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "a\x00b", ".", "..", ""])
def test_task_evaluations_dir_rejects_unsafe_task_id(tmp_path, bad):
    with pytest.raises(ValueError):
        task_evaluations_dir(tmp_path, repo_name="alpha", task_id=bad)


def test_list_evaluations_empty_when_no_tree(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(make_config(tmp_path))
    # Never generated: an empty list, not an error (the tree does not exist yet).
    assert service.list_evaluations(repo, TASK) == []


def test_list_evaluations_returns_newest_first_with_artifacts(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    # Two persisted evaluation dirs (hand-built ids so the ordering is explicit),
    # plus a stray non-dir the enumerator must ignore.
    base = task_evaluations_dir(config.data_dir, repo_name=repo.name, task_id=TASK)
    (base / "20260722T090000Z").mkdir(parents=True)
    (base / "20260722T090000Z" / EVIDENCE_NAME).write_text("{}", encoding="utf-8")
    (base / "20260722T100000Z").mkdir()
    (base / "20260722T100000Z" / EVIDENCE_NAME).write_text("{}", encoding="utf-8")
    (base / "20260722T100000Z" / "static_metrics.json").write_text("{}", encoding="utf-8")
    (base / "stray-file").write_text("ignored", encoding="utf-8")

    stored = make_service(config).list_evaluations(repo, TASK)

    assert [s.evaluation_id for s in stored] == [
        "20260722T100000Z",
        "20260722T090000Z",
    ]  # newest-first
    assert stored[0].artifacts == ("evidence.json", "static_metrics.json")
    assert stored[1].artifacts == ("evidence.json",)
    assert isinstance(stored[0], StoredEvaluation)


def test_generate_then_list_and_read_round_trips(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    pack = service.generate_evidence(repo, TASK)

    stored = service.list_evaluations(repo, TASK)
    assert [s.evaluation_id for s in stored] == [pack.evaluation_id]
    assert EVIDENCE_NAME in stored[0].artifacts

    # read_artifact reads the persisted evidence.json back to the pack's own dict.
    on_disk = service.read_artifact(repo, TASK, pack.evaluation_id, EVIDENCE_NAME)
    assert on_disk == pack.to_dict()


def test_read_artifact_absent_dir_or_file_is_none(tmp_path):
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(make_config(tmp_path))

    # No evaluation generated at all: absent dir reads as None, not an error.
    assert service.read_artifact(repo, TASK, "20990101T000000Z", EVIDENCE_NAME) is None

    # A present dir missing the requested artifact also reads as None (distinct
    # from a present-but-empty file).
    pack = service.generate_evidence(repo, TASK)  # writes evidence.json only
    assert (
        service.read_artifact(repo, TASK, pack.evaluation_id, "static_metrics.json")
        is None
    )
