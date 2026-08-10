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
    EVIDENCE_SCHEMA_VERSION,
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    EvaluationService,
    evaluation_artifact_dir,
    task_evaluations_dir,
)
from orch_hub.models import ManagedRepo, RunRecord, StoredEvaluation
from orch_hub.session_reads import project_slug
from orch_hub.subprocess_utils import CommandResult

TASK = "2026-07-22-task-evaluation-foundation"

FIXED_CLOCK = dt.datetime(2026, 7, 22, 9, 0, 0, tzinfo=dt.timezone.utc)


def make_config(
    tmp_path: Path, *, enabled: bool = True, retention: int | None = None
) -> Config:
    kwargs = {}
    if retention is not None:
        kwargs["eval_retention"] = retention
    return Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        # A fixture Claude home, so the read-behavior section never reaches an
        # operator's real transcript store.
        claude_home=tmp_path / "claude-home",
        evaluation_enabled=enabled,
        **kwargs,
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


def write_active_task(
    repo: ManagedRepo, task_id: str, body: str, *, prefetch: str | None = None
) -> None:
    front = f"id: {task_id}\nstatus: in_progress\n"
    if prefetch is not None:
        front += f"prefetch: {prefetch}\n"
    (repo.path / ".ai-tasks" / f"{task_id}.md").write_text(
        f"---\n{front}---\n\n{body}\n", encoding="utf-8"
    )


def write_ai_snapshot(repo: ManagedRepo, **extra: str) -> Path:
    """Write a conforming eager `.ai/` set (plus any extra docs) into ``repo``.

    Most fixtures here deliberately have no `.ai/` at all — the pre-existing
    shape, which exercises the absent-snapshot degradation — so a test that wants
    the grounded path asks for it explicitly.
    """
    docs = {
        "index.md": (
            "# AI Knowledge Router\n\n| Document | File | Use when |\n|---|---|---|\n"
            "| Overview | overview.md | scope |\n"
            "| Architecture | architecture.md | shape |\n"
            "| Design | design.md | rationale |\n"
            "| Conventions | conventions.md | style |\n"
            "| Map | map.md | cross-ref |\n"
        ),
        "map.md": "# Map\nfeature -> module\n",
        "overview.md": "# Overview\npurpose\n",
        "architecture.md": "# Architecture\nlayers\n",
        "design.md": "# Design\nprinciples\n",
        "conventions.md": "# Conventions\nstyle\n",
    }
    docs.update(extra)
    ai_dir = repo.path / ".ai"
    for rel, text in docs.items():
        target = ai_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return ai_dir


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
        # This fixture repo has no `.ai/` at all: both `.ai/` flags are false and
        # the absent snapshot is the pack's one degradation warning.
        "ai_snapshot_present": False,
        "ai_doc_count": 0,
        "ai_snippets_available": False,
        "ai_diffs_available": False,
        # The session snapshot here carries no reconciled `sid`, so there is no
        # session to attribute read behavior to and nothing is read.
        "reads_available": False,
        "reads_session_count": 0,
        "reads_applicable_count": 0,
        "reads_assessable_count": 0,
        "warning_count": 1,
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
    # Summary counts every section's warnings: this run's, plus the absent `.ai/`
    # snapshot in this fixture repo.
    assert pack.to_dict()["summary"]["warning_count"] == 2


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


def test_git_window_truncated_by_the_readers_byte_cap(tmp_path):
    # A byte-cut list is *short* of the count cap, so testing the count alone
    # would report a partial window as a whole one.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")
    stdout = "\n".join(
        commit_line("s" * 4000, h=f"{i:040d}") for i in range(EVIDENCE_COMMIT_LIMIT // 2)
    )

    pack = make_service(config, git_runner=fake_git(rc=0, stdout=stdout)).build_evidence(
        repo, TASK, evaluation_id="e1"
    )

    assert len(pack.git.commits) < EVIDENCE_COMMIT_LIMIT // 2
    assert pack.git.truncated is True


# -- `.ai/` snapshot evidence -------------------------------------------------


def test_build_evidence_carries_the_ai_snapshot_and_the_prefetch_slice(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo, **{"features/evaluation.md": "# Evaluation\nfeature doc\n"})
    write_active_task(repo, TASK, "# body", prefetch="[.ai/features/evaluation.md]")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00",
              ended_ts="2026-07-22T09:30:00+00:00")

    pack = make_service(
        config, git_runner=fake_git(rc=0, stdout="commit abc .ai patch\n")
    ).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.ai is not None and pack.ai.present is True
    paths = [doc.path for doc in pack.ai.docs]
    # Full eager set, then the doc the task's own frontmatter declared it used.
    assert paths == [
        ".ai/index.md",
        ".ai/map.md",
        ".ai/overview.md",
        ".ai/architecture.md",
        ".ai/design.md",
        ".ai/conventions.md",
        ".ai/features/evaluation.md",
    ]
    assert pack.ai.snippets_available is True and pack.ai.diffs_available is True
    summary = pack.to_dict()["summary"]
    assert summary["ai_snapshot_present"] is True
    assert summary["ai_doc_count"] == 7
    assert summary["ai_snippets_available"] is True
    assert summary["ai_diffs_available"] is True
    assert summary["warning_count"] == 0


def test_ai_diff_uses_the_same_window_as_the_commit_evidence(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00",
              ended_ts="2026-07-22T09:30:00+00:00")
    calls: list = []

    pack = make_service(
        config, git_runner=fake_git(rc=0, stdout=commit_line("feat: x"), calls=calls)
    ).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.ai.diff.since == pack.git.since == "2026-07-22T09:00:00+00:00"
    assert pack.ai.diff.until == pack.git.until == "2026-07-22T09:30:00+00:00"
    # Two git reads: the commit window, then the `.ai/`-scoped patch stream over
    # exactly the same bounds.
    window_argv, diff_argv = calls
    assert "--patch" not in window_argv
    assert diff_argv[-2:] == ["--", ".ai"]
    for argv in (window_argv, diff_argv):
        assert "--since=2026-07-22T09:00:00+00:00" in argv
        assert "--until=2026-07-22T09:30:00+00:00" in argv


def test_no_run_window_leaves_both_git_sections_unavailable_without_calling_git(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo)  # no started_ts
    calls: list = []

    pack = make_service(config, git_runner=fake_git(calls=calls)).build_evidence(
        repo, TASK, evaluation_id="e1"
    )

    assert calls == []
    assert pack.git.available is False
    assert pack.ai.diffs_available is False
    # Both sections explain the absent window the same way.
    assert pack.ai.diff.detail == pack.git.detail
    # Snippets do not depend on git, so they are still grounded.
    assert pack.ai.snippets_available is True


def test_a_failed_ai_diff_read_keeps_the_docs_it_already_collected(tmp_path):
    # A git failure distinct from executable absence, in the `.ai/` patch read
    # only: the two `.ai/` parts are independently degradable, so the snippets
    # must not be discarded with the diff that failed.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00",
              ended_ts="2026-07-22T09:30:00+00:00")

    def git(args, **kwargs):
        argv = [str(a) for a in args]
        if "--patch" in argv:  # the `.ai/` patch stream, not the commit window
            raise PermissionError(13, "Permission denied")
        return CommandResult(tuple(argv), 0, commit_line("feat: x"), "")

    pack = make_service(config, git_runner=git).build_evidence(
        repo, TASK, evaluation_id="e1"
    )

    assert pack.ai.present is True
    assert pack.ai.snippets_available is True  # docs survived
    assert pack.ai.diffs_available is False
    assert "PermissionError" in pack.ai.diff.detail
    assert pack.git.available is True  # the commit window is unaffected


def test_absent_ai_snapshot_degrades_the_pack_instead_of_failing_it(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)  # no `.ai/`
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.target.present is True  # the pack itself is intact
    assert pack.ai.present is False
    assert pack.ai.snippets_available is False and pack.ai.diffs_available is False
    assert len(pack.ai.warnings) == 1
    assert pack.to_dict()["ai"]["docs"] == []


def test_unexpected_ai_read_failure_is_isolated_from_the_pack(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo)
    write_active_task(repo, TASK, "# body")
    write_run(config, "20260722T090000Z-alpha-task", repo=repo,
              started_ts="2026-07-22T09:00:00+00:00")
    monkeypatch.setattr(
        "orch_hub.evaluation.read_ai_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    # `.ai/` is secondary evidence: it degrades, the pack survives.
    assert pack.ai.present is False
    assert "RuntimeError" in pack.ai.detail
    assert any("boom" in w for w in pack.ai.warnings)
    assert pack.runs and pack.target.present is True


def test_malformed_prefetch_does_not_stop_the_eager_set(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo)
    write_active_task(repo, TASK, "# body", prefetch="[docs/not-ai.md]")

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.ai.snippets_available is True
    assert any("is not a .ai/*.md path" in w for w in pack.ai.warnings)
    assert pack.to_dict()["summary"]["warning_count"] == 1


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


def test_persisted_pack_stamps_the_schema_version_and_ai_section(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo)
    write_active_task(repo, TASK, "# body")

    pack = make_service(config).generate_evidence(repo, TASK)

    on_disk = json.loads((pack.evaluation_dir / EVIDENCE_NAME).read_text())
    # The version is what lets a consumer split reports into coherent cohorts
    # across a change that moved what the evaluator can see.
    assert on_disk["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert on_disk["ai"]["present"] is True
    assert on_disk["ai"]["snippets_available"] is True
    assert on_disk["ai"]["docs"][0]["path"] == ".ai/index.md"
    assert "# Overview" in on_disk["ai"]["docs"][2]["content"]


def test_pack_persisted_before_the_ai_section_still_reads_back(tmp_path):
    # A pack written by the previous schema: no `schema_version`, no `ai` key. The
    # shape only ever grows by addition, so an older artifact stays readable
    # rather than needing a migration or being silently dropped.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    evaluation_id = "20260722T090000Z"
    eval_dir = config.data_dir / EVALUATIONS_SUBDIR / "alpha" / TASK / evaluation_id
    eval_dir.mkdir(parents=True)
    legacy = {
        "evaluation_id": evaluation_id,
        "generated_ts": "2026-07-22T09:00:00+00:00",
        "target": {
            "repo_name": "alpha",
            "repo_path": str(repo.path),
            "task_id": TASK,
            "source": SOURCE_ARCHIVED,
            "present": True,
            "content": "# body",
        },
        "runs": [],
        "git": {"available": False, "commits": [], "detail": "git history unavailable"},
        "warnings": [],
        "summary": {"run_count": 0, "warning_count": 0},
    }
    (eval_dir / EVIDENCE_NAME).write_text(json.dumps(legacy), encoding="utf-8")

    service = make_service(config)

    assert service.read_artifact(repo, TASK, evaluation_id, EVIDENCE_NAME) == legacy
    stored = service.list_evaluations(repo, TASK)
    assert [e.evaluation_id for e in stored] == [evaluation_id]
    assert stored[0].artifacts == (EVIDENCE_NAME,)


# -- read-behavior section (per-session prefetch + mid-flight retrieval) ------


def write_transcript(config: Config, repo: ManagedRepo, sid: str, rows: list[dict]) -> Path:
    """Write one session transcript into the fixture Claude home."""
    path = (
        config.claude_transcripts_dir / project_slug(repo.path) / f"{sid}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def read_pair(abs_path: Path, content: str, uid: str) -> list[dict]:
    """A ``Read`` call and its result, as a transcript records the pair."""
    return [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": uid, "name": "Read",
                     "input": {"file_path": str(abs_path)}}
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": uid, "content": "ok"}],
            },
            "toolUseResult": {
                "type": "text",
                "file": {"filePath": str(abs_path), "content": content, "startLine": 1},
            },
        },
    ]


def test_build_evidence_carries_the_per_session_read_rollup(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo, **{"features/evaluation.md": "# Evaluation\n"})
    write_active_task(repo, TASK, "# body", prefetch="[.ai/features/evaluation.md]")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={"sessions": [{"role": "dev", "kind": "session", "sid": "sid-1"}]},
    )
    write_transcript(
        config,
        repo,
        "sid-1",
        read_pair(
            repo.path / ".ai-tasks" / f"{TASK}.md",
            "---\nid: x\nprefetch: [.ai/features/evaluation.md]\n---\n",
            "task",
        )
        + read_pair(repo.path / ".ai" / "features" / "evaluation.md", "# Evaluation\n", "doc")
        # Then it goes to the code, and only afterwards fetches a further doc:
        # the pre-load and the mid-flight retrieval are on opposite sides of that.
        + read_pair(repo.path / "orch_hub" / "evaluation.py", "def build():\n", "code")
        + read_pair(repo.path / ".ai" / "modules" / "sidecar.md", "# Sidecar\n", "mid"),
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.reads is not None and pack.reads.available is True
    session = pack.reads.sessions[0]
    assert session.session_id == "sid-1"
    assert session.run_id == "20260722T090000Z-alpha-task"
    assert session.complete is True and session.coverage == 1.0
    assert [read.path for read in session.midflight] == [".ai/modules/sidecar.md"]
    summary = pack.to_dict()["summary"]
    assert summary["reads_available"] is True
    assert summary["reads_session_count"] == 1
    assert summary["reads_applicable_count"] == 1
    assert summary["reads_assessable_count"] == 1


def test_read_rollup_is_absent_without_session_ids(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body", prefetch="[.ai/features/evaluation.md]")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        sessions={"sessions": [{"role": "dev", "kind": "session"}]},  # sid never revealed
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.reads.available is False and pack.reads.sessions == ()
    assert pack.reads.warnings == ()  # nothing to attribute is not degradation


def test_read_behavior_failure_degrades_alone(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo)
    write_active_task(repo, TASK, "# body")

    def boom(*_args, **_kwargs):
        raise RuntimeError("transcript store exploded")

    monkeypatch.setattr("orch_hub.evaluation.read_session_behavior", boom)

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.reads.available is False
    assert "transcript store exploded" in pack.reads.detail
    assert any("read behavior" in w for w in pack.reads.warnings)
    # The rest of the pack is untouched by the failure.
    assert pack.target.present is True and pack.ai.present is True


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


def test_list_evaluations_orders_double_digit_suffix_numerically(tmp_path):
    # Regression: same-second collisions past -9 must still list newest-first.
    # The collision suffix is unpadded (base, -2 .. -11), so a plain reverse
    # *lexical* sort ranks "-9" above "-11"/"-10" and would report a stale
    # latest evaluation; the numeric ``(stamp, suffix)`` key keeps -11 newest.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)  # fixed clock => one base stamp, bumping suffix

    ids = [service.generate_evidence(repo, TASK).evaluation_id for _ in range(11)]
    assert ids[0] == "20260722T090000Z"
    assert ids[-2:] == ["20260722T090000Z-10", "20260722T090000Z-11"]

    stored = service.list_evaluations(repo, TASK)
    # Newest-first is the exact reverse of the mint (chronological) order: the
    # last-minted -11 leads and the suffix-free base id trails.
    assert [s.evaluation_id for s in stored] == list(reversed(ids))
    assert stored[0].evaluation_id == "20260722T090000Z-11"  # true latest
    assert stored[-1].evaluation_id == "20260722T090000Z"


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


def test_read_text_artifact_reads_verbatim_and_absent_is_none(tmp_path):
    # The text sibling of read_artifact (for the semantic layer's report.md):
    # returns raw text for a present file, None for an absent dir/file, and stays
    # contained (an unsafe artifact name is rejected).
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    pack = service.generate_evidence(repo, TASK)  # mints the dir with evidence.json
    md_path = pack.evaluation_dir / "report.md"
    md_path.write_text("# a report\nline\n", encoding="utf-8")

    assert service.read_text_artifact(repo, TASK, pack.evaluation_id, "report.md") == "# a report\nline\n"
    # Absent artifact and absent dir both read as None, not an error.
    assert service.read_text_artifact(repo, TASK, pack.evaluation_id, "missing.md") is None
    assert service.read_text_artifact(repo, TASK, "20990101T000000Z", "report.md") is None
    # Containment: a path-traversal artifact name is rejected before any read.
    with pytest.raises(ValueError):
        service.read_text_artifact(repo, TASK, pack.evaluation_id, "../evidence.json")


# -- retention / cleanup ------------------------------------------------------
#
# Evaluation artifacts accumulate one dir per generation; retention keeps only the
# newest N per task so the sidecar tree stays bounded. The fixed clock mints one
# base stamp with bumping suffixes, so a plain count check is deterministic.


def test_prune_evaluations_keeps_newest_and_removes_older(tmp_path):
    # Isolate the explicit prune primitive: generate with retention disabled (0) so
    # auto-prune does not fire, giving six distinct evaluations, then prune to 3.
    config = make_config(tmp_path, retention=0)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    ids = [service.generate_evidence(repo, TASK).evaluation_id for _ in range(6)]
    assert len(set(ids)) == 6  # no reuse when nothing is pruned mid-run

    removed = service.prune_evaluations(repo, TASK, keep=3)

    remaining = [s.evaluation_id for s in service.list_evaluations(repo, TASK)]
    assert len(remaining) == 3
    # The three newest (last-minted) survive; the three oldest are removed.
    assert remaining == list(reversed(ids))[:3]
    assert set(removed) == set(ids[:3])
    assert set(remaining).isdisjoint(set(removed))
    # Every removed dir is actually gone from disk.
    for rid in removed:
        assert not service.artifact_dir(repo, TASK, rid).exists()


def test_prune_evaluations_keep_zero_disables(tmp_path):
    config = make_config(tmp_path, retention=0)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    for _ in range(4):
        service.generate_evidence(repo, TASK)
    # keep=0 is an explicit no-op: retention disabled, keep everything.
    assert service.prune_evaluations(repo, TASK, keep=0) == []
    assert len(service.list_evaluations(repo, TASK)) == 4


def test_prune_evaluations_is_scoped_to_one_task(tmp_path):
    config = make_config(tmp_path, retention=1)
    repo = make_repo(tmp_path)
    other = "2026-07-22-some-other-task"
    write_active_task(repo, TASK, "# body")
    write_active_task(repo, other, "# body")
    service = make_service(config)

    service.generate_evidence(repo, TASK)
    service.generate_evidence(repo, TASK)
    service.generate_evidence(repo, other)
    service.prune_evaluations(repo, TASK, keep=1)

    # Pruning TASK never touches the other task's evaluations.
    assert len(service.list_evaluations(repo, TASK)) == 1
    assert len(service.list_evaluations(repo, other)) == 1


def test_generate_evidence_auto_prunes_to_config_cap(tmp_path):
    # Retention is enforced automatically on generation, so growth stays bounded
    # without a manual prune. The just-generated dir always survives (a metrics or
    # semantic flow writes into it after generate_evidence returns).
    config = make_config(tmp_path, retention=2)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    for _ in range(5):
        pack = service.generate_evidence(repo, TASK)
        # The returned dir must exist for the subsequent static/semantic writes.
        assert pack.evaluation_dir.is_dir()
        # Bounded to the cap (transiently cap+1 when the fresh id sorts oldest).
        assert len(service.list_evaluations(repo, TASK)) <= config.eval_retention + 1

    assert len(service.list_evaluations(repo, TASK)) <= config.eval_retention + 1


def test_generate_evidence_default_retention_keeps_modest_history(tmp_path):
    # The default cap is generous, so a handful of evaluations are all retained.
    config = make_config(tmp_path)  # default retention
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    for _ in range(5):
        service.generate_evidence(repo, TASK)
    assert len(service.list_evaluations(repo, TASK)) == 5


def test_generate_evidence_prune_false_defers_to_explicit_auto_prune(tmp_path):
    # The metrics/semantic seam: generate_evidence(prune=False) mints and persists
    # the dir but does NOT prune, so a higher-layer write can still fail and roll
    # the dir back without retention having already deleted older evaluations. The
    # caller runs retention itself via auto_prune once its artifacts are durable.
    config = make_config(tmp_path, retention=1)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    existing = service.generate_evidence(repo, TASK)  # default prune=True path
    assert len(service.list_evaluations(repo, TASK)) == 1

    fresh = service.generate_evidence(repo, TASK, prune=False)
    # Deferred: both dirs are live even though retention=1 (no prune happened yet).
    ids = {s.evaluation_id for s in service.list_evaluations(repo, TASK)}
    assert ids == {existing.evaluation_id, fresh.evaluation_id}

    # auto_prune enforces the cap, protecting the just-minted id, and never raises.
    service.auto_prune(repo, TASK, protect=fresh.evaluation_id)
    remaining = [s.evaluation_id for s in service.list_evaluations(repo, TASK)]
    assert remaining == [fresh.evaluation_id]


def test_prune_evaluations_disabled_service_fails_closed(tmp_path):
    repo = make_repo(tmp_path)
    service = EvaluationService(make_config(tmp_path, enabled=False))
    with pytest.raises(EvaluationDisabledError):
        service.prune_evaluations(repo, TASK)
