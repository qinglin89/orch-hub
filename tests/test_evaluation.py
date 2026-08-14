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
import hashlib
import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.ai_snapshot import (
    PREFETCH_SOURCE_SESSION_ENTRIES,
    PREFETCH_SOURCE_TASK_FILE,
)
from orch_hub.evaluation import (
    EVALUATIONS_SUBDIR,
    EVIDENCE_COMMIT_LIMIT,
    EVIDENCE_LOG_MAX_LINES,
    EVIDENCE_NAME,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_TASK_ENTRIES_MAX_BYTES,
    EVIDENCE_TASK_FILE_MAX_BYTES,
    FIDELITY_EXACT,
    FIDELITY_PARTIAL,
    FIDELITY_RECONSTRUCTED,
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    EvaluationDamagedTreeError,
    EvaluationDisabledError,
    EvaluationService,
    evaluation_artifact_dir,
    task_evaluations_dir,
)
from orch_hub.models import ManagedRepo, RunRecord, StoredEvaluation
from orch_hub.protocol_provenance import ARTIFACT_NAME as PROTOCOL_ARTIFACT
from orch_hub.protocol_provenance import (
    DEPLOY_LOCK_NAME,
    DEPLOY_MANIFEST_NAME,
    PROTOCOL_SCHEMA_VERSION,
    STABILITY_DRIFTED,
    STABILITY_STABLE,
    STABILITY_UNVERIFIED,
    capture_protocol,
    settle_protocol,
)
from orch_hub.session_reads import SOURCE_SESSION_ENTRY_SNAPSHOT, project_slug
from orch_hub.subprocess_utils import CommandResult
from orch_hub.task_snapshots import ARTIFACT_NAME as TASK_SNAPSHOTS_ARTIFACT

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
    task_snapshots: dict | str | None = None,
    protocol: dict | str | None = None,
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
    if task_snapshots is not None:
        # A raw string writes the artifact verbatim, so a corrupt one can be posed.
        (run_dir / TASK_SNAPSHOTS_ARTIFACT).write_text(
            task_snapshots
            if isinstance(task_snapshots, str)
            else json.dumps(task_snapshots),
            encoding="utf-8",
        )
    if protocol is not None:
        (run_dir / PROTOCOL_ARTIFACT).write_text(
            protocol if isinstance(protocol, str) else json.dumps(protocol),
            encoding="utf-8",
        )
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
        # Neither run captured a deployed protocol revision, so the task has no
        # effective one — the runs' own sections say why.
        "protocol_available": False,
        "protocol_verified_runs": 0,
        "has_task_content": True,
        "target_source": SOURCE_ACTIVE,
        # One parsed session bracket, no run wrote an entry capture for it.
        "task_entry_fidelity": FIDELITY_RECONSTRUCTED,
        "task_entry_sessions": 1,
        "task_entries_exact": 0,
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


def test_pack_persisted_before_the_session_entry_section_still_reads_back(tmp_path):
    # The bump that added session-entry task files is additive like the ones
    # before it: a v15 run entry has no `task_snapshots` key and no fidelity in
    # its summary, and must read back verbatim rather than through a migration.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    evaluation_id = "20260722T090000Z"
    eval_dir = config.data_dir / EVALUATIONS_SUBDIR / "alpha" / TASK / evaluation_id
    eval_dir.mkdir(parents=True)
    previous = {
        "schema_version": "15",
        "evaluation_id": evaluation_id,
        "generated_ts": "2026-07-22T09:00:00+00:00",
        "target": {"repo_name": "alpha", "repo_path": str(repo.path),
                   "task_id": TASK, "source": SOURCE_ARCHIVED, "present": True,
                   "content": "# body"},
        "runs": [{"run": {"run_id": "r1", "task_id": TASK}, "ended": True,
                  "events": [], "sessions": None, "log": None, "spawn_out": None,
                  "warnings": []}],
        "git": {"available": False, "commits": []},
        "ai": {"present": False, "docs": [], "warnings": []},
        "reads": {"available": False, "sessions": [], "warnings": []},
        "warnings": [],
        "summary": {"run_count": 1, "warning_count": 0},
    }
    (eval_dir / EVIDENCE_NAME).write_text(json.dumps(previous), encoding="utf-8")

    read_back = make_service(config).read_artifact(
        repo, TASK, evaluation_id, EVIDENCE_NAME
    )

    assert read_back == previous
    assert "task_snapshots" not in read_back["runs"][0]
    assert "task_entry_fidelity" not in read_back["summary"]


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
        # Then it goes to the code, and only afterwards fetches a further doc: one
        # ordered trace carries both, with the work between them as the anchor.
        + read_pair(repo.path / "orch_hub" / "evaluation.py", "def build():\n", "code")
        + read_pair(repo.path / ".ai" / "modules" / "sidecar.md", "# Sidecar\n", "mid"),
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.reads is not None and pack.reads.available is True
    session = pack.reads.sessions[0]
    assert session.session_id == "sid-1"
    assert session.run_id == "20260722T090000Z-alpha-task"
    assert session.complete is True and session.coverage == 1.0
    assert [(read.path, read.declared_prefetch) for read in session.trace] == [
        (".ai/features/evaluation.md", True),
        (".ai/modules/sidecar.md", False),
    ]
    assert session.trace[1].before.code_reads == 1
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


# -- session-entry task files (the exact state each session started from) -----


def entry_task(prefetch: str, *, est: str = "1/2", body: str = "# Goal\n\nship it") -> str:
    """A task file as one session entered it — the fields the archive loses."""
    return (
        f"---\nid: {TASK}\nstatus: in_progress\nsession-est: {est}\n"
        f"prefetch: {prefetch}\n---\n\n{body}\n"
    )


def multibyte_entry_task(prefetch: str) -> str:
    """A task file the per-entry cap cuts *inside* a three-byte character.

    The boundary case the cap has to survive: a byte-offset slice leaves a partial
    character, and replacing it on decode makes the kept text serialize back over
    the cap. The assert is the fixture's own check — the padding is arithmetic
    over :func:`entry_task`'s frontmatter, and a silently-aligned cut would leave
    this test passing while proving nothing."""
    lead = len(entry_task(prefetch, body="").encode()) - 1  # trailing body newline
    pad = (EVIDENCE_TASK_FILE_MAX_BYTES - lead - 2) % 3
    content = entry_task(
        prefetch, body="a" * pad + "€" * EVIDENCE_TASK_FILE_MAX_BYTES
    )
    assert content.encode()[EVIDENCE_TASK_FILE_MAX_BYTES] & 0xC0 == 0x80
    return content


def entry(index: int, sid: str | None, content: str | None, **extra) -> dict:
    """One persisted ``task_snapshots.json`` entry, present iff it carries bytes."""
    return {
        "index": index,
        "sid": sid,
        "role": "dev",
        "kind": "session",
        "status_before": "pending",
        "session_start_hms": "09:00:00",
        "captured_at": "2026-07-22T09:00:25+00:00",
        "capture_point": "post_claim",
        "entry_lag_seconds": 25,
        "present": content is not None,
        "content": content,
        "size": len(content.encode()) if content is not None else 0,
        "truncated": False,
        **extra,
    }


def test_build_evidence_carries_each_sessions_entry_task_file(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")  # the end state, already in the pack
    first, second = entry_task("[.ai/map.md]"), entry_task("[.ai/map.md]", est="2/3")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={
            "sessions": [
                {"role": "dev", "kind": "session", "sid": "sid-1"},
                {"role": "dev", "kind": "session", "sid": "sid-2"},
            ]
        },
        task_snapshots={
            "snapshots": [entry(0, "sid-1", first), entry(1, "sid-2", second)]
        },
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    run = pack.runs[0]
    assert run.task_snapshots is not None
    # Joined to the session summaries by the sid the run manager reconciled.
    assert run.task_snapshots.for_sid("sid-1").content == first
    assert run.task_snapshots.for_sid("sid-2").content == second
    # The estimate total the archived file would have flattened to one value.
    assert "session-est: 1/2" in run.task_snapshots.for_sid("sid-1").content
    assert "session-est: 2/3" in run.task_snapshots.for_sid("sid-2").content
    assert pack.entry_fidelity == FIDELITY_EXACT
    summary = pack.to_dict()["summary"]
    assert summary["task_entry_fidelity"] == FIDELITY_EXACT
    assert (summary["task_entry_sessions"], summary["task_entries_exact"]) == (2, 2)
    # Serialized whole, so a reader of the persisted pack sees the same bytes.
    persisted = pack.to_dict()["runs"][0]["task_snapshots"]["snapshots"]
    assert [e["sid"] for e in persisted] == ["sid-1", "sid-2"]
    assert persisted[0]["capture_point"] == "post_claim"


def test_entry_fidelity_is_partial_when_only_some_sessions_were_captured(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        sessions={
            "sessions": [
                {"role": "dev", "kind": "session", "sid": "sid-1"},
                {"role": "review", "kind": "session", "sid": "sid-2"},
            ]
        },
        # The second session began and ended between two scans: accounted for,
        # never captured.
        task_snapshots={
            "snapshots": [
                entry(0, "sid-1", entry_task("[.ai/map.md]")),
                entry(1, "sid-2", None, detail="session had already ended when first observed"),
            ]
        },
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.entry_counts == (2, 1)
    assert pack.entry_fidelity == FIDELITY_PARTIAL
    # A recorded miss stays in the pack: "no exact file" and "no such session"
    # must not read the same downstream.
    missed = pack.runs[0].task_snapshots.for_sid("sid-2")
    assert missed is not None and missed.present is False
    assert "already ended" in missed.detail


def test_a_run_that_wrote_no_entry_accounting_is_reconstructed(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        sessions={"sessions": [{"role": "dev", "kind": "session", "sid": "sid-1"}]},
        task_snapshots="{not json",  # a corrupt artifact reads as absent, not fatal
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.runs[0].task_snapshots is None
    assert pack.entry_counts == (1, 0)
    assert pack.entry_fidelity == FIDELITY_RECONSTRUCTED


def test_a_live_runs_entries_count_before_its_session_snapshot_exists(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    # sessions.json is written at the live->ended transition; entries are written
    # during the run, so mid-run the entries are the only account of its sessions.
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        task_snapshots={"snapshots": [entry(0, "sid-1", entry_task("[.ai/map.md]"))]},
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.runs[0].sessions is None
    assert pack.entry_counts == (1, 1)
    assert pack.entry_fidelity == FIDELITY_EXACT


def test_plan_gate_brackets_are_outside_the_entry_accounting(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={
            "sessions": [
                {"role": "dev", "kind": "plan-gate", "sid": "sid-0"},
                {"role": "dev", "kind": "session", "sid": "sid-1"},
            ]
        },
        task_snapshots={"snapshots": [entry(1, "sid-1", entry_task("[.ai/map.md]"))]},
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    # A plan-gate never claims the task, so counting it would report a permanent
    # 1-of-2 for a run that captured everything there was to capture.
    assert pack.entry_counts == (1, 1)
    assert pack.entry_fidelity == FIDELITY_EXACT


def test_entries_past_the_pack_budget_are_carried_without_content(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    # Whole files, each comfortably under the per-entry cap, so what this measures
    # is the pack-wide budget alone.
    big = entry_task("[.ai/map.md]", body="x" * (EVIDENCE_TASK_FILE_MAX_BYTES - 1024))
    fits = EVIDENCE_TASK_ENTRIES_MAX_BYTES // len(big.encode())
    count = fits + 2
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={
            "sessions": [
                {"role": "dev", "kind": "session", "sid": f"sid-{i}"}
                for i in range(count)
            ]
        },
        task_snapshots={
            "snapshots": [entry(i, f"sid-{i}", big) for i in range(count)]
        },
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    entries = pack.runs[0].task_snapshots.snapshots
    carried = [e for e in entries if e.present]
    dropped = [e for e in entries if not e.present]
    # Oldest-first: the newest entries are the ones closest to the task file the
    # pack already carries whole, so they are the cheapest to drop.
    assert [e.sid for e in carried] == [f"sid-{i}" for i in range(fits)]
    assert dropped and all("budget was exhausted" in e.detail for e in dropped)
    assert sum(len(e.content.encode()) for e in carried) <= EVIDENCE_TASK_ENTRIES_MAX_BYTES
    # The limit is the pack's own and is stated once, not once per dropped entry.
    assert len(pack.warnings) == 1 and "budget" in pack.warnings[0]
    # What the budget dropped is not exact evidence; what it kept is whole.
    assert all(e.complete for e in carried)
    assert pack.entry_fidelity == FIDELITY_PARTIAL


def test_a_head_cut_in_the_body_is_kept_but_is_not_exact_evidence(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo, **{"modules/runtime.md": "# Runtime\n"})
    write_active_task(repo, TASK, "# body", prefetch="[.ai/modules/runtime.md]")
    huge = entry_task(
        "[.ai/modules/runtime.md]", body="y" * (EVIDENCE_TASK_FILE_MAX_BYTES * 2)
    )
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={"sessions": [{"role": "dev", "kind": "session", "sid": "sid-1"}]},
        task_snapshots={"snapshots": [entry(0, "sid-1", huge)]},
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    kept = pack.runs[0].task_snapshots.for_sid("sid-1")
    assert kept.present is True and kept.truncated is True
    assert len(kept.content.encode()) <= EVIDENCE_TASK_FILE_MAX_BYTES
    assert kept.size == len(huge.encode())  # the true size, not what was kept
    # The head is what matters: the frontmatter the archive cannot give back.
    assert kept.content.startswith(f"---\nid: {TASK}\n")
    # But a head is not the file the session entered on: an unknown amount of
    # Goal/Scope/Acceptance is missing, so it cannot be replayed exactly.
    assert kept.complete is False
    assert pack.entry_fidelity == FIDELITY_RECONSTRUCTED
    # The declared fields all sit above the cut, so they still answer exactly.
    assert kept.frontmatter_complete is True
    assert pack.ai.prefetch_source == PREFETCH_SOURCE_SESSION_ENTRIES
    assert pack.reads.sessions[0].expected == (".ai/modules/runtime.md",)
    assert pack.reads.sessions[0].expected_source == SOURCE_SESSION_ENTRY_SNAPSHOT


def test_a_head_cut_inside_the_frontmatter_declares_nothing(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo, **{"modules/runtime.md": "# Runtime\n"})
    write_active_task(repo, TASK, "# body", prefetch="[.ai/modules/runtime.md]")
    # A frontmatter longer than the per-entry cap: the head stops before the
    # `prefetch:` line, which parses as a session that declared nothing.
    padded = (
        f"---\nid: {TASK}\nstatus: in_progress\nsession-est: 1/2\n"
        f"notes: {'n' * EVIDENCE_TASK_FILE_MAX_BYTES}\n"
        "prefetch: [.ai/modules/runtime.md]\n---\n\n# Goal\n"
    )
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={"sessions": [{"role": "dev", "kind": "session", "sid": "sid-1"}]},
        task_snapshots={"snapshots": [entry(0, "sid-1", padded)]},
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    kept = pack.runs[0].task_snapshots.for_sid("sid-1")
    assert kept.present is True and kept.frontmatter_complete is False
    assert "frontmatter" in kept.detail
    assert any("frontmatter" in w for w in pack.warnings)  # never a silent loss
    assert pack.entry_fidelity == FIDELITY_RECONSTRUCTED
    # "Not known" is not "declared nothing": the accumulated value is the only
    # surviving source, and `prefetch_source` says the slice came from it.
    assert pack.ai.prefetch_source == PREFETCH_SOURCE_TASK_FILE
    assert [d.path for d in pack.ai.docs if d.role == "prefetch"] == [
        ".ai/modules/runtime.md"
    ]
    # And the read rollup claims no session-time set rather than an empty one.
    assert pack.reads.sessions[0].expected == ()
    assert pack.reads.sessions[0].expected_source is None


def test_the_per_entry_cap_cuts_on_a_character_boundary(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    huge = multibyte_entry_task("[.ai/map.md]")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={"sessions": [{"role": "dev", "kind": "session", "sid": "sid-1"}]},
        task_snapshots={"snapshots": [entry(0, "sid-1", huge)]},
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    kept = pack.runs[0].task_snapshots.for_sid("sid-1")
    assert len(kept.content.encode()) <= EVIDENCE_TASK_FILE_MAX_BYTES
    assert "�" not in kept.content
    assert huge.startswith(kept.content)  # a real prefix, not a repaired one


def test_the_pack_budget_holds_across_multibyte_entries(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    # Enough cut entries to fill the pack budget to the byte, so a per-entry
    # overshoot of even one byte puts the carried set over it.
    huge = multibyte_entry_task("[.ai/map.md]")
    count = EVIDENCE_TASK_ENTRIES_MAX_BYTES // EVIDENCE_TASK_FILE_MAX_BYTES
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={
            "sessions": [
                {"role": "dev", "kind": "session", "sid": f"sid-{i}"}
                for i in range(count)
            ]
        },
        task_snapshots={"snapshots": [entry(i, f"sid-{i}", huge) for i in range(count)]},
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    carried = [e for e in pack.runs[0].task_snapshots.snapshots if e.present]
    # Every entry is debited at what it actually serializes to, so a budget filled
    # to the byte by cut entries still holds once the pack is written out.
    assert len(carried) == count
    assert (
        sum(len(e.content.encode()) for e in carried) <= EVIDENCE_TASK_ENTRIES_MAX_BYTES
    )


def test_the_ai_prefetch_slice_comes_from_the_session_entry_declarations(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(
        repo,
        **{"modules/runtime.md": "# Runtime\n", "features/evaluation.md": "# Eval\n"},
    )
    # The archived value is the accumulated one — it has picked up a doc the
    # sessions only discovered they needed at close-out.
    write_active_task(
        repo, TASK, "# body",
        prefetch="[.ai/modules/runtime.md, .ai/features/evaluation.md]",
    )
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={"sessions": [{"role": "dev", "kind": "session", "sid": "sid-1"}]},
        task_snapshots={
            "snapshots": [entry(0, "sid-1", entry_task("[.ai/modules/runtime.md]"))]
        },
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.ai.prefetch_source == PREFETCH_SOURCE_SESSION_ENTRIES
    prefetch_docs = [d.path for d in pack.ai.docs if d.role == "prefetch"]
    assert prefetch_docs == [".ai/modules/runtime.md"]
    # And the read section measures the session against the same declaration.
    assert pack.reads.sessions[0].expected == (".ai/modules/runtime.md",)
    assert pack.reads.sessions[0].expected_source == SOURCE_SESSION_ENTRY_SNAPSHOT


def test_the_ai_prefetch_slice_falls_back_to_the_task_file_without_entries(tmp_path):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo, **{"modules/runtime.md": "# Runtime\n"})
    write_active_task(repo, TASK, "# body", prefetch="[.ai/modules/runtime.md]")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        sessions={"sessions": [{"role": "dev", "kind": "session", "sid": "sid-1"}]},
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.ai.prefetch_source == PREFETCH_SOURCE_TASK_FILE
    assert [d.path for d in pack.ai.docs if d.role == "prefetch"] == [
        ".ai/modules/runtime.md"
    ]


def test_sessions_that_declared_nothing_give_an_empty_slice_not_the_accumulated_one(
    tmp_path,
):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_ai_snapshot(repo, **{"modules/runtime.md": "# Runtime\n"})
    # Backfilled at close-out with what was consulted; no session ever declared it.
    write_active_task(repo, TASK, "# body", prefetch="[.ai/modules/runtime.md]")
    write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        sessions={"sessions": [{"role": "dev", "kind": "session", "sid": "sid-1"}]},
        task_snapshots={"snapshots": [entry(0, "sid-1", entry_task("[]"))]},
    )

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.ai.prefetch_source == PREFETCH_SOURCE_SESSION_ENTRIES
    assert [d.path for d in pack.ai.docs if d.role == "prefetch"] == []


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


# -- protocol provenance (which deployed protocol the task's runs ran under) --
#
# The pack carries each run's persisted capture verbatim and aggregates them into
# the one revision the task may be published under. Everything comes from the run
# dirs: the repo's own deployment lock is never read here, because by evaluation
# time it usually names a later deployment than any of these runs used.

COMMIT_A = "6b62754694c289fc8822562cec22ab774e071b2c"
COMMIT_B = "aa11bb22cc33dd44ee55ff6677889900aabbccdd"
PAYLOAD = "5" * 64
PAYLOAD_B = "6" * 64


def protocol_artifact(
    *,
    commit: str = COMMIT_A,
    payload: str = PAYLOAD,
    stability: str = STABILITY_STABLE,
    available: bool = True,
    detail: str | None = None,
) -> dict:
    """One run's persisted capture, in the shape ``protocol.json`` holds."""
    return {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "available": available,
        "source_git_commit": commit if available else None,
        "canonical_payload_sha256": payload if available else None,
        "source_repo": "ai-native-deployment",
        "lock_schema_version": 1,
        "captured_ts": "2026-07-22T09:00:00+00:00",
        "stability": stability,
        "checked_ts": (
            None if stability == STABILITY_UNVERIFIED else "2026-07-22T09:30:00+00:00"
        ),
        # A stable verdict names the payload comparison behind it; a run that
        # published nothing has none to name.
        "payload_files": 117 if stability == STABILITY_STABLE else None,
        "detail": detail,
    }


def build_with_protocols(tmp_path, *protocols: dict | str | None):
    """Build a pack over one run per given capture (``None`` = a run with none)."""
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# Active goal")
    for index, protocol in enumerate(protocols):
        write_run(
            config,
            f"20260722T0{index}0000Z-alpha-task",
            repo=repo,
            started_ts=f"2026-07-22T0{index}:00:00+00:00",
            ended_ts=f"2026-07-22T0{index}:30:00+00:00",
            exit_code=0,
            protocol=protocol,
        )
    return make_service(config).build_evidence(repo, TASK, evaluation_id="e1")


def test_pack_publishes_the_identity_every_run_verified(tmp_path):
    pack = build_with_protocols(
        tmp_path, protocol_artifact(), protocol_artifact()
    )

    assert pack.protocol.available is True
    assert (pack.protocol.revision, pack.protocol.payload_sha256) == (
        COMMIT_A,
        PAYLOAD,
    )
    assert (pack.protocol.run_count, pack.protocol.verified_count) == (2, 2)
    assert pack.protocol.detail is None

    data = pack.to_dict()
    # The aggregate is a top-level section beside the runs it summarizes, and each
    # run's own capture is carried whole — a consumer that disagrees with the
    # aggregation rule can still see what every run recorded.
    assert data["protocol"] == {
        "available": True,
        "effective_revision": COMMIT_A,
        "deploy_lock_hash": PAYLOAD,
        "run_count": 2,
        "verified_count": 2,
        "detail": None,
    }
    assert data["runs"][0]["protocol"]["source_git_commit"] == COMMIT_A
    assert data["runs"][0]["protocol"]["stability"] == STABILITY_STABLE
    assert data["summary"]["protocol_available"] is True
    assert data["summary"]["protocol_verified_runs"] == 2


def test_pack_reports_a_run_without_a_capture_as_missing_not_absent(tmp_path):
    # A run launched before the capture existed. Legacy packs stay readable and
    # publishable; what they cannot do is claim a revision for a run nothing
    # observed — the tree's current lock would be a later deployment.
    pack = build_with_protocols(tmp_path, protocol_artifact(), None)

    assert pack.runs[1].protocol is None
    assert pack.to_dict()["runs"][1]["protocol"] is None
    assert pack.protocol.available is False
    assert pack.protocol.revision is None
    assert pack.protocol.detail == (
        "1 of 2 contributing runs carry no verified protocol identity"
    )


def test_pack_refuses_an_identity_when_runs_span_a_redeployment(tmp_path):
    pack = build_with_protocols(
        tmp_path,
        protocol_artifact(commit=COMMIT_A),
        protocol_artifact(commit=COMMIT_B),
    )

    # Both runs verified a revision; they are not the same one, so the task ran
    # under two protocols and belongs to neither cohort.
    assert (pack.protocol.run_count, pack.protocol.verified_count) == (2, 2)
    assert pack.protocol.available is False
    assert COMMIT_A in pack.protocol.detail and COMMIT_B in pack.protocol.detail


def test_pack_refuses_an_identity_when_runs_verified_two_payloads(tmp_path):
    pack = build_with_protocols(
        tmp_path,
        protocol_artifact(payload=PAYLOAD),
        protocol_artifact(payload=PAYLOAD_B),
    )

    # One revision, two payloads: the runs read different protocol text, so the
    # commit they share is not a cohort membership the pack may publish.
    assert (pack.protocol.run_count, pack.protocol.verified_count) == (2, 2)
    assert pack.protocol.available is False
    data = pack.to_dict()["protocol"]
    assert (data["effective_revision"], data["deploy_lock_hash"]) == (None, None)
    assert "different protocol payloads" in data["detail"]


def test_pack_publishes_nothing_for_a_run_whose_deployed_files_drifted(tmp_path):
    # The case the payload half exists for: the lock never moved, so every
    # lock-only comparison would have called this run stable, while its deployed
    # protocol files stopped matching their receipt under it. Neither half of the
    # identity reaches the pack.
    pack = build_with_protocols(
        tmp_path,
        protocol_artifact(
            stability=STABILITY_DRIFTED, detail="deployed files no longer match ..."
        ),
    )

    assert pack.runs[0].protocol.source_git_commit == COMMIT_A  # readable for audit
    assert pack.protocol.available is False
    assert (pack.protocol.revision, pack.protocol.payload_sha256) == (None, None)
    assert pack.protocol.verified_count == 0


def test_pack_does_not_publish_an_unverified_capture(tmp_path):
    # A run whose end check never completed: the commit stays readable for audit,
    # but nothing confirmed the deployment held for the whole run.
    pack = build_with_protocols(
        tmp_path, protocol_artifact(stability=STABILITY_UNVERIFIED)
    )

    assert pack.runs[0].protocol.source_git_commit == COMMIT_A
    assert pack.protocol.available is False
    assert pack.protocol.verified_count == 0


def test_a_corrupt_capture_degrades_the_pack_instead_of_failing_it(tmp_path):
    pack = build_with_protocols(tmp_path, "{not json", protocol_artifact())

    # Unreadable is *unavailable*, not absent: the hub did capture something for
    # this run, so a consumer must not treat it as backfillable.
    assert pack.runs[0].protocol is not None
    assert pack.runs[0].protocol.available is False
    assert "unreadable" in pack.runs[0].protocol.detail
    assert pack.protocol.available is False


def test_a_pack_with_no_runs_has_no_effective_identity(tmp_path):
    pack = build_with_protocols(tmp_path)

    assert pack.protocol.available is False
    assert pack.protocol.run_count == 0
    assert pack.protocol.detail == "no related runs to take a protocol identity from"


def test_the_pack_reads_what_a_real_capture_wrote(tmp_path):
    # The other cases hand-write the artifact, which cannot catch the two modules
    # disagreeing about its shape. This one drives the real producer — a deployed
    # payload with both its receipts, a launch capture, an end check that hashes
    # the files — and reads the result back through the pack, so the run-dir
    # contract is asserted end to end.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# Active goal")
    (repo.path / DEPLOY_LOCK_NAME).write_text(
        json.dumps(
            {
                "canonical_payload_sha256": PAYLOAD,
                "deployed_files": [
                    {
                        "canonical_sha256": "0" * 64,
                        "target_relative_path": ".ai-protocol/protocols/conduct.md",
                    }
                ],
                "schema_version": 1,
                "source_git_commit": COMMIT_A,
                "source_repo": "ai-native-deployment",
            }
        ),
        encoding="utf-8",
    )
    deployed = repo.path / ".ai-protocol" / "protocols" / "conduct.md"
    deployed.parent.mkdir(parents=True, exist_ok=True)
    deployed.write_bytes(b"# Conduct\n")
    (repo.path / DEPLOY_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "files": {
                    ".ai-protocol/protocols/conduct.md": {
                        "sha256": hashlib.sha256(b"# Conduct\n").hexdigest(),
                        "size_bytes": len(b"# Conduct\n"),
                        "target_relative_path": ".ai-protocol/protocols/conduct.md",
                    }
                },
                "schema_version": 1,
                "source_git_commit": COMMIT_A,
            }
        ),
        encoding="utf-8",
    )
    run_dir = write_run(
        config,
        "20260722T090000Z-alpha-task",
        repo=repo,
        started_ts="2026-07-22T09:00:00+00:00",
        ended_ts="2026-07-22T09:30:00+00:00",
        exit_code=0,
    )
    capture_protocol(run_dir, repo.path, now=FIXED_CLOCK)
    settle_protocol(run_dir, repo.path, now=FIXED_CLOCK)

    pack = make_service(config).build_evidence(repo, TASK, evaluation_id="e1")

    assert pack.runs[0].protocol.canonical_payload_sha256 == PAYLOAD
    assert pack.protocol.available is True
    # The published identity is the lock's own pair, carried whole from the run's
    # capture through the aggregate to the section a consumer cohorts by.
    assert (pack.protocol.revision, pack.protocol.payload_sha256) == (
        COMMIT_A,
        PAYLOAD,
    )


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


# -- artifact-tree containment ------------------------------------------------
#
# The hub writes only real directories and regular files under
# <data_dir>/evaluations. A link standing at any hop inside that tree is something
# the subsystem did not put there, and following one would serve a target-repo (or
# any other outside) file to the dashboard as this evaluation's artifact. The rule
# is per hop rather than a blanket refusal of links, because the tree itself may be
# *reached* through one — see the relocated-data-dir case below.


@pytest.mark.parametrize("reader", ["read_artifact", "read_text_artifact"])
def test_reads_refuse_an_artifact_standing_as_a_link(tmp_path, reader):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    pack = service.generate_evidence(repo, TASK)
    outside = repo.path / "docs" / "private.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("target repo content", encoding="utf-8")
    linked = pack.evaluation_dir / "report.md"
    linked.symlink_to(outside)

    # Refused, and *named* as damage: read as absence it would be indistinguishable
    # from an evidence-only evaluation, and the operator who can repair the tree
    # would never hear about it.
    with pytest.raises(EvaluationDamagedTreeError):
        getattr(service, reader)(repo, TASK, pack.evaluation_id, "report.md")
    # And the listing does not advertise bytes the read refuses to serve.
    stored = service.list_evaluations(repo, TASK)
    assert stored[0].artifacts == (EVIDENCE_NAME,)


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_reads_refuse_a_linked_directory_hop(tmp_path, depth):
    # The artifact-hop rule cannot see this one: a linked *directory* holds
    # perfectly regular files, so a no-follow open of the artifact alone would
    # succeed and hand back an outside evaluation's bytes under this task's id.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    outside = tmp_path / "outside" / "evaluations" / "alpha" / TASK / "20990101T000000Z"
    outside.mkdir(parents=True)
    (outside / EVIDENCE_NAME).write_text('{"outside": true}', encoding="utf-8")
    parts = ("alpha", TASK, "20990101T000000Z")[:depth]
    link = config.data_dir / "evaluations" / Path(*parts)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(
        tmp_path / "outside" / "evaluations" / Path(*parts), target_is_directory=True
    )

    with pytest.raises(EvaluationDamagedTreeError):
        service.read_artifact(repo, TASK, "20990101T000000Z", EVIDENCE_NAME)
    if depth == 3:
        # A linked evaluation dir is not an evaluation: the listing that gates the
        # detail route does not offer it either.
        assert service.list_evaluations(repo, TASK) == []
    else:
        # A linked repo/task hop cannot be enumerated at all — refused, not read as
        # "this task has no evaluations".
        with pytest.raises(EvaluationDamagedTreeError):
            service.list_evaluations(repo, TASK)


def test_reads_and_listing_work_through_a_relocated_data_dir(tmp_path):
    # The compatibility half of the rule, and why it is stated per hop: a hub whose
    # data dir is reached through a link — a relocated volume, or macOS's own
    # /var -> /private/var — must read exactly as one on a plain path does.
    real = tmp_path / "volume" / "hub-data"
    real.mkdir(parents=True)
    link = tmp_path / "hub-data"
    link.symlink_to(real, target_is_directory=True)
    config = make_config(tmp_path)
    assert config.data_dir == link
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    pack = service.generate_evidence(repo, TASK)
    (pack.evaluation_dir / "report.md").write_text("# a report\n", encoding="utf-8")

    stored = service.list_evaluations(repo, TASK)
    assert [s.evaluation_id for s in stored] == [pack.evaluation_id]
    assert service.read_artifact(repo, TASK, pack.evaluation_id, EVIDENCE_NAME) == (
        pack.to_dict()
    )
    assert (
        service.read_text_artifact(repo, TASK, pack.evaluation_id, "report.md")
        == "# a report\n"
    )


# -- artifact-tree containment: the write half --------------------------------
#
# Same rule, at the only places that can put something *in* the tree. The path
# helpers' resolve() assertion already refuses a hop link pointing outside the
# tree, but it proves that about names at check time and the kernel resolves them
# again at mkdir/open time — so the write paths walk the tree hop by hop like the
# reads do. The point is less the escape (planting the link needs write access to
# the hub's own data dir) than coherence: the hub must not be the thing that
# populates a tree its own reads then refuse as damaged.


@pytest.mark.parametrize("depth", [1, 2])
def test_generation_refuses_a_linked_directory_hop(tmp_path, depth):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    outside = tmp_path / "outside"
    (outside / TASK).mkdir(parents=True)
    parts = ("alpha", TASK)[:depth]
    link = config.data_dir / EVALUATIONS_SUBDIR / Path(*parts)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(
        outside if depth == 1 else outside / TASK, target_is_directory=True
    )

    with pytest.raises(EvaluationDamagedTreeError):
        service.generate_evidence(repo, TASK)
    # Refused, not followed: the generation created nothing on the other side.
    assert list((outside / TASK).iterdir()) == []


def test_generation_refuses_a_hop_linked_to_another_dir_inside_the_tree(tmp_path):
    # The case the resolve() assertion cannot see at all: this link stays *under*
    # the evaluations base, so the pathname check passes and the mkdir would file
    # one task's evaluations under another task's id.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    other = config.data_dir / EVALUATIONS_SUBDIR / "alpha" / "other-task"
    other.mkdir(parents=True)
    (config.data_dir / EVALUATIONS_SUBDIR / "alpha" / TASK).symlink_to(
        other, target_is_directory=True
    )

    with pytest.raises(EvaluationDamagedTreeError):
        service.generate_evidence(repo, TASK)
    assert list(other.iterdir()) == []


def test_artifact_write_refuses_a_linked_temp_file(tmp_path):
    # Where the write side was weakest: the temp file's open is O_CREAT|O_TRUNC,
    # which through a link truncates and overwrites whatever it names.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    pack = service.generate_evidence(repo, TASK)
    outside = repo.path / "docs" / "private.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("target repo content", encoding="utf-8")
    (pack.evaluation_dir / "report.md.tmp").symlink_to(outside)

    with pytest.raises(EvaluationDamagedTreeError):
        service.write_text_artifact(repo, TASK, pack.evaluation_id, "report.md", "x")
    assert outside.read_text(encoding="utf-8") == "target repo content"


def test_artifact_write_replaces_a_linked_artifact_without_following_it(tmp_path):
    # The final hop needs no separate check: renameat replaces a link standing at
    # the destination rather than writing through it. Pinned because the whole
    # atomic-write shape rests on it.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    pack = service.generate_evidence(repo, TASK)
    outside = repo.path / "docs" / "private.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("target repo content", encoding="utf-8")
    linked = pack.evaluation_dir / "report.md"
    linked.symlink_to(outside)

    service.write_text_artifact(repo, TASK, pack.evaluation_id, "report.md", "# fresh\n")

    assert not linked.is_symlink()
    assert outside.read_text(encoding="utf-8") == "target repo content"
    assert (
        service.read_text_artifact(repo, TASK, pack.evaluation_id, "report.md")
        == "# fresh\n"
    )


def test_artifact_write_on_an_absent_evaluation_dir_fails_loudly(tmp_path):
    # A generation reserves its dir first, so absence here means it was removed
    # mid-generation: a failed write to report, not a silent no-op.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    with pytest.raises(FileNotFoundError):
        service.write_artifact(repo, TASK, "20990101T000000Z", EVIDENCE_NAME, {"a": 1})
    with pytest.raises(ValueError):
        service.write_artifact(repo, TASK, "20990101T000000Z", "../x.json", {"a": 1})


def test_generation_works_through_a_relocated_data_dir(tmp_path):
    # The compatibility half, for the write paths: creating the tree hop by hop
    # must not stop a hub whose data dir is reached through a link from writing.
    real = tmp_path / "volume" / "hub-data"
    real.mkdir(parents=True)
    (tmp_path / "hub-data").symlink_to(real, target_is_directory=True)
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    pack = service.generate_evidence(repo, TASK)
    service.write_text_artifact(repo, TASK, pack.evaluation_id, "report.md", "# r\n")

    landed = real / EVALUATIONS_SUBDIR / "alpha" / TASK / pack.evaluation_id
    assert (landed / EVIDENCE_NAME).is_file()
    assert (landed / "report.md").read_text(encoding="utf-8") == "# r\n"


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


def test_prune_never_deletes_through_a_linked_evaluation_dir(tmp_path):
    # Deletion holds the same no-follow rule the reads and writes do, at the
    # evaluation's own hop.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    outside = tmp_path / "outside" / "20990101T000000Z"
    outside.mkdir(parents=True)
    (outside / EVIDENCE_NAME).write_text('{"outside": true}', encoding="utf-8")
    keeper = service.generate_evidence(repo, TASK)
    linked_id = "20990101T000000Z"
    task_dir = config.data_dir / EVALUATIONS_SUBDIR / "alpha" / TASK
    (task_dir / linked_id).symlink_to(outside, target_is_directory=True)

    # 1. The listing the prune deletes from is the contained walk, so a linked dir
    #    is not a candidate at all — nothing to delete, nothing to follow.
    assert [s.evaluation_id for s in service.list_evaluations(repo, TASK)] == [
        keeper.evaluation_id
    ]
    assert service.prune_evaluations(repo, TASK, keep=1) == []

    # 2. And if one reaches the deletion list anyway (a dir swapped for a link
    #    after the listing), the delete opens that hop O_NOFOLLOW and leaves it
    #    alone. Reported as retained, not as removed: a caller told the id was
    #    deleted would reason about history the tree still holds.
    def forced(*_a, **_k):
        return [
            StoredEvaluation(evaluation_id=keeper.evaluation_id, artifacts=()),
            StoredEvaluation(evaluation_id=linked_id, artifacts=()),
        ]

    service.list_evaluations = forced  # type: ignore[method-assign]
    assert service.prune_evaluations(repo, TASK, keep=1) == []
    assert (outside / EVIDENCE_NAME).is_file()
    assert (task_dir / linked_id).is_symlink()

    # A link pointing *inside* the tree is refused by exactly the same hop check.
    # It is the case the resolve() assertion on a pathname cannot see at all —
    # the target stays under the base, so only the no-follow open catches it.
    sibling = task_dir / "20990101T000001Z"
    sibling.mkdir()
    (sibling / EVIDENCE_NAME).write_text('{"sibling": true}', encoding="utf-8")
    (task_dir / linked_id).unlink()
    (task_dir / linked_id).symlink_to(sibling, target_is_directory=True)

    assert service.prune_evaluations(repo, TASK, keep=1) == []
    assert (sibling / EVIDENCE_NAME).is_file()
    assert (task_dir / linked_id).is_symlink()


def test_prune_never_deletes_through_a_swapped_intermediate_hop(tmp_path):
    # The hop *above* the evaluation is the half a final-component check cannot
    # cover: shutil.rmtree lstats its own argument, but the kernel has already
    # resolved <repo-slug>/<task-id> by then. The listing proves those hops were
    # real when it ran; the delete that follows re-walks them itself.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    doomed = service.generate_evidence(repo, TASK)
    keeper = service.generate_evidence(repo, TASK)
    base = config.data_dir / EVALUATIONS_SUBDIR

    # An outside tree shaped exactly like the one being pruned, holding the id the
    # prune is about to delete.
    outside = tmp_path / "outside"
    victim = outside / TASK / doomed.evaluation_id
    victim.mkdir(parents=True)
    (victim / EVIDENCE_NAME).write_text('{"outside": true}', encoding="utf-8")
    (outside / "sentinel").write_text("sentinel", encoding="utf-8")

    # The swap: after the reservation/listing proved it real, the <repo-slug> hop
    # becomes a link to that outside tree.
    real_repo_dir = tmp_path / "stashed-repo-dir"
    (base / "alpha").rename(real_repo_dir)
    (base / "alpha").symlink_to(outside, target_is_directory=True)

    def forced(*_a, **_k):
        return [
            StoredEvaluation(evaluation_id=keeper.evaluation_id, artifacts=()),
            StoredEvaluation(evaluation_id=doomed.evaluation_id, artifacts=()),
        ]

    service.list_evaluations = forced  # type: ignore[method-assign]
    assert service.prune_evaluations(repo, TASK, keep=1) == []

    # Nothing outside the tree was touched, and the link itself was not consumed.
    assert (victim / EVIDENCE_NAME).is_file()
    assert (outside / "sentinel").is_file()
    assert (base / "alpha").is_symlink()
    # The evaluation the prune actually meant is still there, unpruned — a delete
    # it could not perform safely is a retained evaluation, never a wrong one.
    assert (real_repo_dir / TASK / doomed.evaluation_id / EVIDENCE_NAME).is_file()


def test_prune_never_deletes_through_an_in_tree_intermediate_hop(tmp_path):
    # The variant no pathname check can see: the swapped <repo-slug> hop points at
    # another directory *inside* the tree, so resolve() finds the target still under
    # the base and approves it. Only opening the hop no-follow catches this one —
    # and what it protects is another repo's evaluation history, not an outside file.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    doomed = service.generate_evidence(repo, TASK)
    keeper = service.generate_evidence(repo, TASK)
    base = config.data_dir / EVALUATIONS_SUBDIR

    # A second repo's subtree, inside the tree, holding the same ids.
    decoy = base / "decoy"
    for eid in (doomed.evaluation_id, keeper.evaluation_id):
        (decoy / TASK / eid).mkdir(parents=True)
        (decoy / TASK / eid / EVIDENCE_NAME).write_text(
            '{"decoy": 1}', encoding="utf-8"
        )

    real_repo_dir = tmp_path / "stashed-repo-dir"
    (base / "alpha").rename(real_repo_dir)
    (base / "alpha").symlink_to(decoy, target_is_directory=True)

    # The pathname the old delete would have used passes the escape assertion,
    # because the link's target really is inside the tree.
    approved = evaluation_artifact_dir(
        config.data_dir,
        repo_name="alpha",
        task_id=TASK,
        evaluation_id=doomed.evaluation_id,
    )
    assert approved.resolve() == (decoy / TASK / doomed.evaluation_id).resolve()

    def forced(*_a, **_k):
        return [
            StoredEvaluation(evaluation_id=keeper.evaluation_id, artifacts=()),
            StoredEvaluation(evaluation_id=doomed.evaluation_id, artifacts=()),
        ]

    service.list_evaluations = forced  # type: ignore[method-assign]
    assert service.prune_evaluations(repo, TASK, keep=1) == []

    # The decoy repo's evaluations are intact — retention for one repo never
    # reaches another's history.
    assert (decoy / TASK / doomed.evaluation_id / EVIDENCE_NAME).is_file()
    assert (decoy / TASK / keeper.evaluation_id / EVIDENCE_NAME).is_file()
    assert (real_repo_dir / TASK / doomed.evaluation_id / EVIDENCE_NAME).is_file()


def test_generate_evidence_cleanup_never_deletes_through_a_swapped_hop(tmp_path):
    # Same window on the failure-cleanup path: the dir is reserved, the build then
    # fails, and the cleanup runs against hops that are no longer what the
    # reservation walked.
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)
    base = config.data_dir / EVALUATIONS_SUBDIR
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("sentinel", encoding="utf-8")

    swapped: dict[str, str] = {}
    real_build = service.build_evidence

    def build_then_swap(repo_arg, task_arg, *, evaluation_id):
        # Between the reservation and the cleanup, exactly where the race lives.
        victim = outside / TASK / evaluation_id
        victim.mkdir(parents=True)
        (victim / EVIDENCE_NAME).write_text('{"outside": true}', encoding="utf-8")
        (base / "alpha").rename(tmp_path / "stashed-repo-dir")
        (base / "alpha").symlink_to(outside, target_is_directory=True)
        swapped["id"] = evaluation_id
        raise RuntimeError("build failed")

    service.build_evidence = build_then_swap  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        service.generate_evidence(repo, TASK)
    service.build_evidence = real_build  # type: ignore[method-assign]

    # The cleanup refused the swapped hop rather than deleting the matching dir it
    # now pointed at; the leaked reserved dir is the intended cost.
    assert (outside / TASK / swapped["id"] / EVIDENCE_NAME).is_file()
    assert (outside / "sentinel").is_file()
    assert (tmp_path / "stashed-repo-dir" / TASK / swapped["id"]).is_dir()


def test_remove_evaluation_deletes_a_populated_dir_through_a_relocated_data_dir(
    tmp_path,
):
    # The ordinary path still works, and still works when the evaluations base is
    # itself reached through a link (a relocated data dir) — links above the tree
    # stay followable; only hops inside it are refused.
    real = tmp_path / "volume" / "hub-data"
    real.mkdir(parents=True)
    (tmp_path / "hub-data").symlink_to(real, target_is_directory=True)
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    write_active_task(repo, TASK, "# body")
    service = make_service(config)

    pack = service.generate_evidence(repo, TASK)
    eval_dir = real / EVALUATIONS_SUBDIR / "alpha" / TASK / pack.evaluation_id
    # A nested dir the hub never writes: a flat unlink would leave it, and its
    # parent, behind forever.
    (eval_dir / "nested").mkdir()
    (eval_dir / "nested" / "leftover.json").write_text("{}", encoding="utf-8")
    assert (eval_dir / EVIDENCE_NAME).is_file()

    assert service.remove_evaluation(repo, TASK, pack.evaluation_id) is True
    assert not eval_dir.exists()
    assert service.list_evaluations(repo, TASK) == []
    # Idempotent: already gone is the caller's goal, not a failure.
    assert service.remove_evaluation(repo, TASK, pack.evaluation_id) is True


def test_remove_evaluation_reports_absent_levels_and_fails_closed_when_disabled(
    tmp_path,
):
    config = make_config(tmp_path)
    repo = make_repo(tmp_path)
    service = make_service(config)
    # Nothing generated yet: no parent level to remove from.
    assert service.remove_evaluation(repo, TASK, "20990101T000000Z") is False

    disabled = EvaluationService(make_config(tmp_path, enabled=False))
    with pytest.raises(EvaluationDisabledError):
        disabled.remove_evaluation(repo, TASK, "20990101T000000Z")


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
