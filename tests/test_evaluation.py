"""Tests for the task-evaluation foundation.

Covers the four session-1 acceptance areas: active/archive target resolution,
repo/task run-aggregation scoping, disabled-mode fail-closed behavior, and safe
artifact-path layout. Fake repos, task files, and run dirs live in ``tmp_path``;
the run-record loader is injected for the scoping matrix and exercised for real
(through :func:`orch_hub.runs.load_records`) in the integration tests, so no run
manager or subprocess is ever involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.evaluation import (
    EVALUATIONS_SUBDIR,
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    EvaluationService,
    evaluation_artifact_dir,
)
from orch_hub.models import ManagedRepo, RunRecord

TASK = "2026-07-22-task-evaluation-foundation"


def make_config(tmp_path: Path, *, enabled: bool = True) -> Config:
    return Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=enabled,
    )


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
