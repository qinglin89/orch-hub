"""Tests for the FastAPI service adapter.

Monkeypatches the shared application functions (exactly as the CLI tests do) so
the HTTP wiring, error responses, and JSON serialization are exercised without
touching real repos, subprocesses, or the mandrel registry.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import itertools
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orch_hub import api
from orch_hub.config import (
    DEFAULT_EVAL_EFFORT,
    DEFAULT_EVAL_MODEL,
    DEFAULT_INTAKE_EFFORT,
    DEFAULT_INTAKE_MODEL,
    HOST_ENV,
    PORT_ENV,
    Config,
)
from orch_hub.git_meta import CommitLog
from orch_hub.history import KIND_ANSWER_RECORDED, KIND_RUN_STARTED, read_events
from orch_hub.intake import (
    AgentConfirmResult,
    AgentDraftResult,
    IntakeManager,
    KIND_INTAKE_CREATED,
    KIND_INTAKE_REVISED,
    STATUS_AWAITING_REVIEW,
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_CONFIRMING,
    STATUS_DRAFTING,
    STATUS_FAILED,
    STATUS_REVISING,
)
from orch_hub.evaluation import EVIDENCE_NAME, SOURCE_ARCHIVED, EvaluationService
from orch_hub.models import (
    PROVIDER_CLAUDE,
    CommitInfo,
    IntakeDraft,
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
    RepoTaskInventory,
    RunRecord,
    TaskSummary,
)
from orch_hub.orch_config import OrchestratorConfigError, parse_effective_config
from orch_hub.registry import RegistryError
from orch_hub.protocol_provenance import LEGACY_AGGREGATE_DETAIL
from orch_hub.report_catalog import REQUIRED_ARTIFACTS, ReportCatalog
from orch_hub.runs import RunManager
from orch_hub.semantic import (
    SEMANTIC_END,
    SEMANTIC_REPORT_MD_NAME,
    SEMANTIC_REPORT_NAME,
    SEMANTIC_START,
    SemanticEvaluationError,
    SemanticEvaluator,
)
from orch_hub.semantic_jobs import SEMANTIC_JOB_NAME, SemanticJobManager
from orch_hub.static_metrics import STATIC_METRICS_NAME, StaticEvaluator
from orch_hub.subprocess_utils import CommandResult
from orch_hub.task_store import STATUS_MISSING

TOKEN = "test-token"
FAKE_CONFIG = Config(mandrel_path=Path("/fake/mandrel"), token=TOKEN)
NO_TOKEN_CONFIG = Config(mandrel_path=Path("/fake/mandrel"))


def make_repo(name: str = "r") -> ManagedRepo:
    return ManagedRepo(
        name=name,
        path=Path(f"/p/{name}"),
        manifest=Path(f"/p/{name}/.ai-deploy-manifest.json"),
    )


def build_client(config: Config | None = None, *, authed: bool = True) -> TestClient:
    """Client against a fresh app; sends the valid bearer token by default."""
    client = TestClient(api.create_app(config or FAKE_CONFIG))
    if authed:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client


def test_health_is_public_and_minimal():
    # Liveness must work with no credentials and must not leak config.
    resp = build_client(authed=False).get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_repos_ready_matches_cli_shape(monkeypatch):
    repo = make_repo()
    report = ReadinessReport((ReadinessCheck("repo_path_exists", True, "ok"),))
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "check_readiness", lambda r, config: report)
    monkeypatch.setattr(api, "latest_commit", lambda repo: None)

    resp = build_client().get("/api/repos")
    assert resp.status_code == 200
    # Byte-for-byte the same object CLI `repos --json` emits (see test_cli),
    # now including the best-effort `commit` field (null when git is unread).
    assert resp.json() == {
        "repos": [
            {
                "name": "r",
                "path": "/p/r",
                "manifest": "/p/r/.ai-deploy-manifest.json",
                "readiness": {"ready": True, "checks": []},
                "commit": None,
                "housekeeping_pending": False,
            }
        ]
    }


def test_repos_includes_latest_commit(monkeypatch):
    repo = make_repo()
    report = ReadinessReport((ReadinessCheck("repo_path_exists", True, "ok"),))
    commit = CommitInfo(
        hash="c" * 40,
        short_hash="ccccccc",
        committed_ts="2026-07-09T16:25:00+08:00",
        subject="feat: dashboard status layout",
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "check_readiness", lambda r, config: report)
    monkeypatch.setattr(api, "latest_commit", lambda repo: commit)

    data = build_client().get("/api/repos").json()
    assert data["repos"][0]["commit"] == commit.to_dict()


def test_repos_reports_housekeeping_marker_present(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    marker = repo_path / ".ai" / ".housekeeping-pending"
    marker.parent.mkdir(parents=True)
    marker.write_text("", encoding="utf-8")
    repo = ManagedRepo(
        name="r",
        path=repo_path,
        manifest=repo_path / ".ai-deploy-manifest.json",
    )
    report = ReadinessReport((ReadinessCheck("repo_path_exists", True, "ok"),))
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "check_readiness", lambda r, config: report)
    monkeypatch.setattr(api, "latest_commit", lambda repo: None)

    data = build_client().get("/api/repos").json()

    assert data["repos"][0]["housekeeping_pending"] is True


def test_repos_reports_housekeeping_marker_absent(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo = ManagedRepo(
        name="r",
        path=repo_path,
        manifest=repo_path / ".ai-deploy-manifest.json",
    )
    report = ReadinessReport((ReadinessCheck("repo_path_exists", True, "ok"),))
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "check_readiness", lambda r, config: report)
    monkeypatch.setattr(api, "latest_commit", lambda repo: None)

    data = build_client().get("/api/repos").json()

    assert data["repos"][0]["housekeeping_pending"] is False


def test_repo_commits_returns_recent_newest_first(monkeypatch):
    repo = make_repo("a")
    commits = (
        CommitInfo("h1" + "0" * 38, "h1", "2026-07-09T10:00:00+00:00", "newest"),
        CommitInfo("h2" + "0" * 38, "h2", "2026-07-08T10:00:00+00:00", "older"),
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(
        api, "read_commits", lambda r, limit: CommitLog(available=True, commits=commits)
    )

    resp = build_client().get("/api/repos/a/commits")
    assert resp.status_code == 200
    assert resp.json() == {
        "repo": "a",
        "available": True,
        "commits": [c.to_dict() for c in commits],
    }


def test_repo_commits_unknown_repo_404(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", lambda config: [])
    assert build_client().get("/api/repos/nope/commits").status_code == 404


def test_repo_commits_unavailable_is_graceful_200(monkeypatch):
    # A git failure degrades to available:false with an empty list, never a
    # 5xx — a commit-lookup problem must not hide the repo.
    repo = make_repo("a")
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(
        api, "read_commits", lambda r, limit: CommitLog(available=False, commits=())
    )
    resp = build_client().get("/api/repos/a/commits")
    assert resp.status_code == 200
    assert resp.json() == {"repo": "a", "available": False, "commits": []}


def test_repo_commits_passes_limit_through(monkeypatch):
    repo = make_repo("a")
    seen = {}

    def fake_read(r, limit):
        seen["limit"] = limit
        return CommitLog(available=True, commits=())

    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "read_commits", fake_read)
    build_client().get("/api/repos/a/commits?limit=3")
    assert seen["limit"] == 3


def test_repos_reports_only_failed_checks(monkeypatch):
    repo = make_repo()
    report = ReadinessReport(
        (
            ReadinessCheck("repo_path_exists", True, "ok"),
            ReadinessCheck("git_clean", False, "working tree has uncommitted changes"),
        )
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "check_readiness", lambda r, config: report)

    data = build_client().get("/api/repos").json()
    readiness = data["repos"][0]["readiness"]
    assert readiness["ready"] is False
    assert readiness["checks"] == [
        {"name": "git_clean", "message": "working tree has uncommitted changes"}
    ]


def test_repos_registry_error_returns_500(monkeypatch):
    def boom(config):
        raise RegistryError("invalid registry JSON from mandrel")

    monkeypatch.setattr(api, "discover_repos", boom)
    resp = build_client().get("/api/repos")
    assert resp.status_code == 500
    assert "invalid registry JSON" in resp.json()["detail"]


def test_tasks_all_repos_shape(monkeypatch):
    inv = RepoTaskInventory(
        repo=make_repo("a"),
        index_present=True,
        tasks=(
            TaskSummary(task_id="2026-07-05-a", status="pending", summary="A work"),
        ),
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [make_repo("a")])
    monkeypatch.setattr(api, "list_tasks", lambda repos: [inv])

    resp = build_client().get("/api/tasks")
    assert resp.status_code == 200
    assert resp.json() == {
        "repos": [
            {
                "name": "a",
                "path": "/p/a",
                "manifest": "/p/a/.ai-deploy-manifest.json",
                "index_present": True,
                "tasks": [
                    {
                        "task_id": "2026-07-05-a",
                        "status": "pending",
                        "summary": "A work",
                        "blockers": [],
                    }
                ],
            }
        ]
    }


def test_tasks_registry_error_returns_500(monkeypatch):
    def boom(config):
        raise RegistryError("bad registry")

    monkeypatch.setattr(api, "discover_repos", boom)
    resp = build_client().get("/api/tasks")
    assert resp.status_code == 500
    assert "bad registry" in resp.json()["detail"]


def test_repo_tasks_by_name_returns_single_repo_envelope(monkeypatch):
    repo = make_repo("alpha")
    inv = RepoTaskInventory(
        repo=repo,
        index_present=True,
        tasks=(
            TaskSummary(
                task_id="2026-07-05-x",
                status="blocked",
                summary="X",
                blockers=("external:spec",),
            ),
        ),
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "list_repo_tasks", lambda r: inv)

    resp = build_client().get("/api/repos/alpha/tasks")
    assert resp.status_code == 200
    # Same tasks_payload envelope as /api/tasks, with just this repo.
    assert resp.json() == {"repos": [inv.to_dict()]}


def test_repo_tasks_index_absent_is_preserved(monkeypatch):
    repo = make_repo("noindex")
    inv = RepoTaskInventory(repo=repo, index_present=False, tasks=())
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "list_repo_tasks", lambda r: inv)

    entry = build_client().get("/api/repos/noindex/tasks").json()["repos"][0]
    assert entry["index_present"] is False
    assert entry["tasks"] == []


def test_repo_tasks_preserves_missing_sentinel(monkeypatch):
    repo = make_repo("alpha")
    inv = RepoTaskInventory(
        repo=repo,
        index_present=True,
        tasks=(
            TaskSummary(
                task_id="2026-07-05-ghost",
                status=STATUS_MISSING,
                summary="Ghost",
            ),
        ),
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "list_repo_tasks", lambda r: inv)

    tasks = build_client().get("/api/repos/alpha/tasks").json()["repos"][0]["tasks"]
    assert tasks[0]["status"] == STATUS_MISSING


def test_repo_tasks_unknown_repo_returns_404(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", lambda config: [make_repo("alpha")])

    def fail(r):  # pragma: no cover - reached only on a routing bug
        raise AssertionError("list_repo_tasks called for an unknown repo id")

    monkeypatch.setattr(api, "list_repo_tasks", fail)

    resp = build_client().get("/api/repos/ghost/tasks")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_repo_tasks_registry_error_returns_500(monkeypatch):
    def boom(config):
        raise RegistryError("bad registry")

    monkeypatch.setattr(api, "discover_repos", boom)
    resp = build_client().get("/api/repos/alpha/tasks")
    assert resp.status_code == 500


def test_repo_task_detail_returns_summary_and_content(monkeypatch):
    repo = make_repo("alpha")
    task = TaskSummary(task_id="2026-07-05-x", status="in_progress", summary="X")
    inv = RepoTaskInventory(repo=repo, index_present=True, tasks=(task,))
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "list_repo_tasks", lambda r: inv)
    # The body is read separately from the inventory (which lacks it).
    monkeypatch.setattr(api, "read_task_content", lambda r, tid: "# body\n")

    resp = build_client().get("/api/repos/alpha/tasks/2026-07-05-x")
    assert resp.status_code == 200
    assert resp.json() == {
        "task_id": "2026-07-05-x",
        "status": "in_progress",
        "summary": "X",
        "blockers": [],
        "present": True,
        "content": "# body\n",
    }


def test_repo_task_detail_unknown_repo_is_404(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", lambda config: [make_repo("alpha")])
    resp = build_client().get("/api/repos/ghost/tasks/2026-07-05-x")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_repo_task_detail_unlisted_task_is_404(monkeypatch):
    repo = make_repo("alpha")
    inv = RepoTaskInventory(repo=repo, index_present=True, tasks=())
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "list_repo_tasks", lambda r: inv)

    def fail(r, tid):  # pragma: no cover - reached only on a gating bug
        raise AssertionError("read_task_content called for an unlisted task")

    monkeypatch.setattr(api, "read_task_content", fail)

    resp = build_client().get("/api/repos/alpha/tasks/2026-07-05-nope")
    assert resp.status_code == 404
    assert "2026-07-05-nope" in resp.json()["detail"]


def test_repo_task_detail_missing_file_reports_absent(monkeypatch):
    repo = make_repo("alpha")
    task = TaskSummary(
        task_id="2026-07-05-ghost", status=STATUS_MISSING, summary="Ghost"
    )
    inv = RepoTaskInventory(repo=repo, index_present=True, tasks=(task,))
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "list_repo_tasks", lambda r: inv)
    monkeypatch.setattr(api, "read_task_content", lambda r, tid: None)

    body = build_client().get("/api/repos/alpha/tasks/2026-07-05-ghost").json()
    # A listed-but-absent task is a 200 data problem, not an error: the detail
    # view shows the missing sentinel and an "absent" body.
    assert body["status"] == STATUS_MISSING
    assert body["present"] is False
    assert body["content"] is None


def test_repo_task_detail_reads_markdown_link_index_end_to_end(tmp_path, monkeypatch):
    # End-to-end over the real task store (no store mocking): a quantx-style
    # Markdown-link index row must be reachable by its bare id through the HTTP
    # route, gate on membership, and return the frontmatter-authoritative body.
    repo_path = tmp_path / "quantx"
    tasks_dir = repo_path / ".ai-tasks"
    tasks_dir.mkdir(parents=True)
    task_id = "2026-04-13-golden-family-a"
    (tasks_dir / "index.md").write_text(
        "# Active tasks\n\n| Task | Status | Summary |\n|---|---|---|\n"
        f"| [{task_id}]({task_id}.md) | in_progress | Golden family |\n",
        encoding="utf-8",
    )
    (tasks_dir / f"{task_id}.md").write_text(
        f"---\nid: {task_id}\nstatus: final_review\n---\n\n# {task_id}\n\nbody\n",
        encoding="utf-8",
    )
    repo = ManagedRepo(
        name="quantx",
        path=repo_path,
        manifest=repo_path / ".ai-deploy-manifest.json",
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])

    resp = build_client().get(f"/api/repos/quantx/tasks/{task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == task_id
    assert body["status"] == "final_review"  # frontmatter is authoritative
    assert body["present"] is True
    assert "status: final_review" in body["content"]


def test_module_level_app_is_fastapi():
    from fastapi import FastAPI

    assert isinstance(api.app, FastAPI)


# --- bearer-token auth -------------------------------------------------------


def routes_must_not_run(config):
    raise AssertionError("protected route ran despite failed auth")


def test_missing_token_returns_401_before_routing(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", routes_must_not_run)
    resp = build_client(authed=False).get("/api/repos")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert resp.json() == {"detail": "missing bearer token"}


def test_invalid_token_returns_401(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", routes_must_not_run)
    resp = build_client(authed=False).get(
        "/api/repos", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert resp.json() == {"detail": "invalid bearer token"}


def test_auth_errors_never_echo_the_presented_token(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", routes_must_not_run)
    resp = build_client(authed=False).get(
        "/api/repos", headers={"Authorization": "Bearer super-secret-guess"}
    )
    assert "super-secret-guess" not in resp.text


def test_header_without_bearer_scheme_returns_401(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", routes_must_not_run)
    resp = build_client(authed=False).get(
        "/api/repos", headers={"Authorization": TOKEN}
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "missing bearer token"}


def test_bearer_scheme_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", lambda config: [])
    resp = build_client(authed=False).get(
        "/api/repos", headers={"Authorization": f"bearer {TOKEN}"}
    )
    assert resp.status_code == 200


def test_valid_token_allows_all_api_routes(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", lambda config: [])
    monkeypatch.setattr(api, "list_tasks", lambda repos: [])
    client = build_client()
    assert client.get("/api/repos").status_code == 200
    assert client.get("/api/tasks").status_code == 200


def test_unconfigured_token_fails_closed_with_503(monkeypatch):
    monkeypatch.setattr(api, "discover_repos", routes_must_not_run)
    client = build_client(NO_TOKEN_CONFIG, authed=False)
    # Even presenting a token cannot help: serving is disabled, not auth.
    resp = client.get("/api/repos", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 503
    assert "ORCH_HUB_TOKEN" in resp.json()["detail"]
    # The minimal liveness probe stays reachable.
    assert client.get("/api/health").status_code == 200


def test_empty_bearer_credentials_rejected_when_unconfigured_token():
    # An unset token must never make `Authorization: Bearer ` (empty) valid.
    resp = build_client(NO_TOKEN_CONFIG, authed=False).get(
        "/api/repos", headers={"Authorization": "Bearer "}
    )
    assert resp.status_code == 503


def test_unknown_paths_are_denied_by_default(monkeypatch):
    # Default-deny covers routes that do not exist yet (future dashboard/runs):
    # the middleware answers before routing, so this is 401, not 404.
    resp = build_client(authed=False).get("/future/dashboard")
    assert resp.status_code == 401
    # With credentials the same path falls through to normal routing.
    assert build_client().get("/future/dashboard").status_code == 404


def test_openapi_docs_require_auth():
    assert build_client(authed=False).get("/openapi.json").status_code == 401
    assert build_client().get("/openapi.json").status_code == 200


def test_run_binds_env_configured_host_and_port(monkeypatch):
    # The console script calls run() with no arguments, so the launchd plist
    # must be able to steer the bind through the environment alone.
    captured = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, host, port: captured.update(host=host, port=port)
    )
    monkeypatch.setenv(HOST_ENV, "100.64.0.7")
    monkeypatch.setenv(PORT_ENV, "9000")
    api.run()
    assert captured == {"host": "100.64.0.7", "port": 9000}


def test_run_explicit_args_override_env(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, host, port: captured.update(host=host, port=port)
    )
    monkeypatch.setenv(HOST_ENV, "100.64.0.7")
    monkeypatch.setenv(PORT_ENV, "9000")
    api.run(host="127.0.0.1", port=8123)
    assert captured == {"host": "127.0.0.1", "port": 8123}


# --- run and escalation routes -----------------------------------------------
#
# These drive a *real* RunManager over fake run dirs in ``tmp_path`` with an
# injected spawner/clock/pid-probe: no real subprocess is started and no
# orchestrator code is imported. Repo resolution still monkeypatches
# ``discover_repos`` (as above); the escalation flow uses real control-dir files
# so the answer write goes end-to-end through ``orch_hub.control_dir``.

RUN_TASK_ID = "2026-07-05-feature-x"
FIXED_NOW = dt.datetime(2026, 7, 8, 3, 0, 0, tzinfo=dt.timezone.utc)


class FakeProc:
    """The ``ProcessHandle`` slice: a test ends a run by setting ``returncode``."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class FakeSpawner:
    def __init__(self):
        self.calls: list[dict] = []
        self.procs: list[FakeProc] = []
        self._pids = itertools.count(5000)

    def __call__(self, argv, *, cwd, stdout, env, start_new_session):
        proc = FakeProc(next(self._pids))
        self.calls.append({"argv": tuple(argv), "cwd": Path(cwd)})
        self.procs.append(proc)
        return proc


def passing_readiness(repo, config) -> ReadinessReport:
    return ReadinessReport((ReadinessCheck("all", True, "ok"),))


def failing_readiness(repo, config) -> ReadinessReport:
    return ReadinessReport(
        (ReadinessCheck("git_clean", False, "working tree has uncommitted changes"),)
    )


def write_task_index(repo_path: Path, task_ids: list[str]) -> None:
    rows = "\n".join(f"| {tid} | x | Summary of {tid} |" for tid in task_ids)
    (repo_path / ".ai-tasks" / "index.md").write_text(
        "# Active tasks\n\n| Task | Status | Summary |\n|---|---|---|\n" + rows + "\n",
        encoding="utf-8",
    )


def write_task_file(
    repo_path: Path, task_id: str, status: str = "in_progress", blockers: str | None = None
) -> None:
    lines = ["---", f"id: {task_id}", f"status: {status}"]
    if blockers is not None:
        lines.append(f"blockers: {blockers}")
    lines += ["---", "", "# Task", ""]
    (repo_path / ".ai-tasks" / f"{task_id}.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def make_run_repo(
    tmp_path: Path,
    name: str = "repo-a",
    *,
    task_status: str = "in_progress",
    blockers: str | None = None,
) -> ManagedRepo:
    path = tmp_path / "repos" / name
    (path / ".ai-tasks").mkdir(parents=True)
    write_task_index(path, [RUN_TASK_ID])
    write_task_file(path, RUN_TASK_ID, status=task_status, blockers=blockers)
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def run_config(tmp_path: Path) -> Config:
    return Config(
        mandrel_path=tmp_path / "mandrel",
        token=TOKEN,
        data_dir=tmp_path / "hub-data",
    )


def make_run_manager(tmp_path: Path, *, readiness=passing_readiness) -> tuple[RunManager, FakeSpawner]:
    spawner = FakeSpawner()
    manager = RunManager(
        run_config(tmp_path),
        readiness_checker=readiness,
        spawner=spawner,
        pid_probe=lambda pid: True,
        clock=lambda: FIXED_NOW,
    )
    return manager, spawner


def make_run_client(
    tmp_path,
    monkeypatch,
    *,
    repo: ManagedRepo | None = None,
    readiness=passing_readiness,
) -> tuple[TestClient, RunManager, FakeSpawner, ManagedRepo]:
    repo = repo if repo is not None else make_run_repo(tmp_path)
    manager, spawner = make_run_manager(tmp_path, readiness=readiness)
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    client = TestClient(api.create_app(run_config(tmp_path), run_manager=manager))
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client, manager, spawner, repo


def start_a_run(client: TestClient, repo: ManagedRepo, **body) -> dict:
    resp = client.post(
        "/api/runs", json={"repo_id": repo.name, "task_id": RUN_TASK_ID, **body}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def control_dir_of(manager: RunManager, run_id: str) -> Path:
    return manager.get_run(run_id).record.control_dir


def write_question(control: Path, seq: int = 1, **fields) -> None:
    (control / f"{seq:03d}-question.json").write_text(
        json.dumps({"seq": seq, **fields}), encoding="utf-8"
    )


def write_run_log(
    manager: RunManager, run_id: str, lines: list[str], *, stamp: str = "20260708T030000Z"
) -> Path:
    """Drop an orchestrator-style log file into the run's real ``log_dir``.

    The stamp matches ``FIXED_NOW`` (the run's ``started_ts``), so the log
    locator attributes it to this run; the filename shape mirrors the deployed
    orchestrator's ``<utc-ts>-<task-id>.log``.
    """
    record = manager.get_run(run_id).record
    log_dir = record.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{stamp}-{record.task_id}.log"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def write_spawn_out(manager: RunManager, run_id: str, text: str) -> Path:
    """Overwrite the run's spawn.out with a startup-failure capture.

    ``start_run`` already created an empty spawn.out; this replaces it with the
    subprocess stdout+stderr a crashed startup would have left there.
    """
    path = manager.runs_dir / run_id / "spawn.out"
    path.write_text(text, encoding="utf-8")
    return path


# -- start run -----------------------------------------------------------------


def test_start_run_spawns_and_returns_active_status(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)

    body = start_a_run(client, repo)

    assert body["task_id"] == RUN_TASK_ID
    assert body["repo_name"] == repo.name
    assert body["state"] == "active"
    assert body["pending_questions"] == 0
    assert len(spawner.calls) == 1
    # The started run is immediately retrievable by the returned id.
    assert manager.get_run(body["run_id"]).record.task_id == RUN_TASK_ID


def test_start_run_forwards_extra_args(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)

    start_a_run(client, repo, extra_args=["--plan-gate", "--backend", "cc-codex"])

    assert spawner.calls[0]["argv"][-3:] == ("--plan-gate", "--backend", "cc-codex")


def test_start_run_unknown_repo_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)

    resp = client.post("/api/runs", json={"repo_id": "ghost", "task_id": RUN_TASK_ID})
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]
    assert spawner.calls == []


def test_start_run_repo_busy_is_409(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    first = start_a_run(client, repo)

    resp = client.post("/api/runs", json={"repo_id": repo.name, "task_id": RUN_TASK_ID})
    assert resp.status_code == 409
    assert resp.json()["run_id"] == first["run_id"]
    assert len(spawner.calls) == 1  # the second launch never spawned


def test_start_run_launch_blocked_is_409_with_reasons(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(
        tmp_path, monkeypatch, readiness=failing_readiness
    )

    resp = client.post("/api/runs", json={"repo_id": repo.name, "task_id": RUN_TASK_ID})
    assert resp.status_code == 409
    assert resp.json()["reasons"] == [
        "git_clean: working tree has uncommitted changes"
    ]
    assert spawner.calls == []


def test_start_run_blocked_task_requires_acknowledgement(tmp_path, monkeypatch):
    repo = make_run_repo(tmp_path, task_status="blocked", blockers="[external:need ruling]")
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch, repo=repo)

    refused = client.post(
        "/api/runs", json={"repo_id": repo.name, "task_id": RUN_TASK_ID}
    )
    assert refused.status_code == 409
    assert refused.json()["acknowledge_required"] is True
    assert "external:need ruling" in refused.json()["detail"]
    assert spawner.calls == []

    acked = start_a_run(client, repo, acknowledge_blocked=True)
    assert acked["state"] == "active"
    assert len(spawner.calls) == 1


def test_start_run_missing_body_fields_is_422(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    # Pydantic validation (task_id absent) → FastAPI 422, before any launch.
    resp = client.post("/api/runs", json={"repo_id": repo.name})
    assert resp.status_code == 422
    assert spawner.calls == []


# -- start run: structured launch modes ---------------------------------------


def launch_args_of(spawner, call: int = 0) -> tuple:
    """The launch flags a run received: everything after ``--control-dir <dir>``."""
    argv = spawner.calls[call]["argv"]
    idx = argv.index("--control-dir")
    return argv[idx + 2 :]


def start_with_launch(client, repo, launch: dict, **body) -> "object":
    return client.post(
        "/api/runs",
        json={"repo_id": repo.name, "task_id": RUN_TASK_ID, "launch": launch, **body},
    )


def test_launch_default_inherited_omits_backend(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(client, repo, {"mode": "default"})
    assert resp.status_code == 201, resp.text
    assert launch_args_of(spawner) == ()  # nothing appended: full inheritance


def test_launch_default_explicit_backend(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    start_with_launch(client, repo, {"mode": "default", "backend": "cc-codex"})
    assert launch_args_of(spawner) == ("--backend", "cc-codex")


def test_launch_standard_passes_profile_only(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    start_with_launch(client, repo, {"mode": "standard", "backend": "cursor"})
    assert launch_args_of(spawner) == ("--backend", "cursor", "--profile", "standard")


def test_launch_excellent_passes_profile_only(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    start_with_launch(client, repo, {"mode": "excellent", "backend": "cc-codex"})
    assert launch_args_of(spawner) == ("--backend", "cc-codex", "--profile", "excellent")


def test_launch_custom_cc_codex_full_flags(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    start_with_launch(
        client,
        repo,
        {
            "mode": "custom",
            "backend": "cc-codex",
            "dev_agent": "claude",
            "dev_model": "claude-opus-4-8",
            "dev_effort": "max",
            "review_model": "gpt-5.5",
            "review_effort": "xhigh",
        },
    )
    assert launch_args_of(spawner) == (
        "--backend", "cc-codex",
        "--dev-agent", "claude",
        "--dev-model", "claude-opus-4-8",
        "--dev-effort", "max",
        "--review-model", "gpt-5.5",
        "--review-effort", "xhigh",
    )


def test_launch_custom_cursor_rejects_dev_agent(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client,
        repo,
        {
            "mode": "custom",
            "backend": "cursor",
            "dev_agent": "claude",
            "dev_model": "m",
            "dev_effort": "e",
            "review_model": "rm",
            "review_effort": "re",
        },
    )
    assert resp.status_code == 400
    assert "dev-agent" in resp.json()["detail"]
    assert spawner.calls == []  # never launched


def test_launch_custom_missing_model_is_400(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client,
        repo,
        {
            "mode": "custom",
            "backend": "cc-codex",
            "dev_effort": "max",
            "review_model": "gpt-5.5",
            "review_effort": "xhigh",
        },
    )
    assert resp.status_code == 400
    assert spawner.calls == []


def test_launch_plan_gate_orthogonal_to_mode(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    start_with_launch(client, repo, {"mode": "default", "plan_gate": True})
    assert launch_args_of(spawner) == ("--plan-gate",)


def test_launch_bad_mode_is_422(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    # Enum-invalid mode is caught by Pydantic before the builder.
    resp = start_with_launch(client, repo, {"mode": "turbo"})
    assert resp.status_code == 422
    assert spawner.calls == []


def test_launch_and_extra_args_conflict_is_400(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client, repo, {"mode": "default"}, extra_args=["--plan-gate"]
    )
    assert resp.status_code == 400
    assert spawner.calls == []


def test_launch_absent_keeps_extra_args_compatible(tmp_path, monkeypatch):
    # The legacy extra_args path is unchanged when no structured launch is sent.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    start_a_run(client, repo, extra_args=["--backend", "cc-codex", "--plan-gate"])
    assert launch_args_of(spawner) == ("--backend", "cc-codex", "--plan-gate")


# -- start run: independent dev/review role selection --------------------------


def test_launch_split_roles_default_inherits_everything(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client, repo, {"dev_mode": "default", "review_mode": "default"}
    )
    assert resp.status_code == 201, resp.text
    assert launch_args_of(spawner) == ()


def test_launch_split_roles_mixed_named_profiles(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client,
        repo,
        {"dev_mode": "standard", "review_mode": "excellent", "backend": "cc-codex"},
    )
    assert resp.status_code == 201, resp.text
    assert launch_args_of(spawner) == (
        "--backend", "cc-codex",
        "--dev-profile", "standard",
        "--review-profile", "excellent",
    )


def test_launch_split_custom_dev_with_named_review(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client,
        repo,
        {
            "dev_mode": "custom",
            "review_mode": "excellent",
            "backend": "cc-codex",
            "dev_agent": "claude",
            "dev_model": "claude-opus-4-8",
            "dev_effort": "max",
            "plan_gate": True,
        },
    )
    assert resp.status_code == 201, resp.text
    assert launch_args_of(spawner) == (
        "--backend", "cc-codex",
        "--dev-agent", "claude",
        "--dev-model", "claude-opus-4-8",
        "--dev-effort", "max",
        "--review-profile", "excellent",
        "--plan-gate",
    )


def test_launch_split_custom_dev_without_agent_is_400(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client,
        repo,
        {
            "dev_mode": "custom",
            "review_mode": "default",
            "backend": "cc-codex",
            "dev_model": "claude-opus-4-8",
            "dev_effort": "max",
        },
    )
    assert resp.status_code == 400
    assert spawner.calls == []  # never launched


def test_launch_split_custom_review_sends_its_own_agent(tmp_path, monkeypatch):
    """REVIEW names its own CLI agent, symmetric with DEV: the whole tuple reaches
    the orchestrator, so the agent that runs is the one the operator previewed."""
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client,
        repo,
        {
            "dev_mode": "standard",
            "review_mode": "custom",
            "backend": "cc-codex",
            "review_agent": "claude",
            "review_model": "claude-opus-5",
            "review_effort": "xhigh",
        },
    )
    assert resp.status_code == 201, resp.text
    assert launch_args_of(spawner) == (
        "--backend", "cc-codex",
        "--dev-profile", "standard",
        "--review-agent", "claude",
        "--review-model", "claude-opus-5",
        "--review-effort", "xhigh",
    )


def test_launch_split_custom_review_without_agent_is_400(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client,
        repo,
        {
            "dev_mode": "default",
            "review_mode": "custom",
            "backend": "cc-codex",
            "review_model": "gpt-5.5",
            "review_effort": "xhigh",
        },
    )
    assert resp.status_code == 400
    assert spawner.calls == []  # never launched


def test_launch_bad_review_agent_is_422(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client,
        repo,
        {
            "dev_mode": "default",
            "review_mode": "custom",
            "backend": "cc-codex",
            "review_agent": "gemini",
            "review_model": "m",
            "review_effort": "e",
        },
    )
    assert resp.status_code == 422
    assert spawner.calls == []


def test_launch_mixing_mode_and_role_modes_is_400(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(
        client,
        repo,
        {"mode": "standard", "dev_mode": "excellent", "backend": "cc-codex"},
    )
    assert resp.status_code == 400
    assert "not both" in resp.json()["detail"]
    assert spawner.calls == []


def test_launch_empty_selection_is_400(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(client, repo, {"backend": "cc-codex"})
    assert resp.status_code == 400
    assert spawner.calls == []


def test_launch_bad_role_mode_is_422(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = start_with_launch(client, repo, {"dev_mode": "turbo"})
    assert resp.status_code == 422
    assert spawner.calls == []


# -- orchestrator-config query route ------------------------------------------


SAMPLE_CONFIG = {
    "schema_version": 3,
    "config_revision": "cfg-sha",
    "effective_revision": "eff-sha",
    "profile": "standard",
    "profiles": {"dev": "standard", "review": "excellent"},
    "available_profiles": ["excellent", "standard"],
    "backend": "cc-codex",
    "dev": {"agent": "claude", "model": "claude-opus-4-8", "effort": "max"},
    "review": {"agent": "codex", "model": "gpt-5.5", "effort": "xhigh"},
    "max_sessions": 40,
    "context_budget": 200000,
    "codex_sandbox": "danger-full-access",
    "task_id": None,
    "once": False,
    "plan_gate": False,
    "control_dir": None,
    "sources": {"backend": "config"},
    # The orchestrator's selection-independent catalog of what a launch MAY
    # select, keyed by backend/role/agent (trimmed to one model per agent).
    "options": {
        "backends": ["cc-codex", "cursor"],
        "efforts": {"effort": ["low", "max"], "reasoning": ["none", "xhigh"]},
        "agents": {
            "cc-codex": {
                role: {
                    "claude": {
                        "effort_axis": "effort",
                        "efforts": ["low", "max"],
                        "models": [
                            {
                                "id": "claude-opus-4-8",
                                "effort_axis": "effort",
                                "efforts": ["low", "max"],
                            }
                        ],
                    },
                    "codex": {
                        "effort_axis": "reasoning",
                        "efforts": ["none", "xhigh"],
                        "models": [
                            {
                                "id": "gpt-5.5",
                                "effort_axis": "reasoning",
                                "efforts": ["none", "xhigh"],
                            }
                        ],
                    },
                }
                for role in ("dev", "review")
            }
        },
    },
}

# What the route serves: the catalog projected to the part a caller renders.
SAMPLE_CONFIG_OPTIONS = {"agents": SAMPLE_CONFIG["options"]["agents"]}


def capture_config_query(monkeypatch, config: dict = SAMPLE_CONFIG) -> dict:
    """Replace the orchestrator query with a recorder; returns the captured kwargs."""
    captured: dict = {"calls": 0}

    def fake_query(target, *, backend=None, profile=None, dev_profile=None, review_profile=None):
        captured["calls"] += 1
        captured["repo"] = target.name
        captured["backend"] = backend
        captured["profile"] = profile
        captured["dev_profile"] = dev_profile
        captured["review_profile"] = review_profile
        return parse_effective_config(config)

    monkeypatch.setattr(api, "query_effective_config", fake_query)
    return captured


def test_orchestrator_config_returns_effective_config(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    captured = capture_config_query(monkeypatch)

    resp = client.get(
        f"/api/repos/{repo.name}/orchestrator-config?backend=cc-codex&profile=standard"
    )
    assert resp.status_code == 200
    # The hub-owned capability marker rides along with the orchestrator's fields,
    # and the option catalog reaches the caller keyed by backend/role/agent.
    assert resp.json() == {
        **SAMPLE_CONFIG,
        "split_profiles": True,
        "options": SAMPLE_CONFIG_OPTIONS,
    }
    assert resp.json()["options"]["agents"]["cc-codex"]["review"]["codex"]["efforts"] == [
        "none",
        "xhigh",
    ]
    assert captured["repo"] == repo.name
    assert captured["backend"] == "cc-codex"
    assert captured["profile"] == "standard"
    assert captured["dev_profile"] is None and captured["review_profile"] is None


def test_orchestrator_config_serves_no_catalog_as_null(tmp_path, monkeypatch):
    """A deployment that publishes no catalog must reach the dialog as an explicit
    absence. Anything else — an empty object, a hub-filled list — would let the
    browser render choices the orchestrator never published."""
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    payload = {k: v for k, v in SAMPLE_CONFIG.items() if k != "options"}
    capture_config_query(monkeypatch, payload)

    resp = client.get(f"/api/repos/{repo.name}/orchestrator-config")
    assert resp.status_code == 200
    assert resp.json()["options"] is None
    # The rest of the launch config still resolves: the catalog is advisory, so
    # its absence must not disable launching.
    assert resp.json()["dev"]["model"] == "claude-opus-4-8"


def test_orchestrator_config_serves_a_catalog_that_omits_an_agent(tmp_path, monkeypatch):
    """A catalog listing one agent says nothing about the other — the CLI still
    launches it. The route passes exactly that through, so the dialog can offer
    the agent while degrading its model/effort to free text."""
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    agents = SAMPLE_CONFIG["options"]["agents"]["cc-codex"]
    partial = {
        **SAMPLE_CONFIG,
        "options": {
            "agents": {
                "cc-codex": {role: {"claude": agents[role]["claude"]} for role in agents}
            }
        },
    }
    capture_config_query(monkeypatch, partial)

    resp = client.get(f"/api/repos/{repo.name}/orchestrator-config")
    assert resp.status_code == 200
    served = resp.json()["options"]["agents"]["cc-codex"]
    assert sorted(served["review"]) == ["claude"]
    assert served["review"]["claude"]["models"][0]["id"] == "claude-opus-4-8"


def test_orchestrator_config_forwards_both_role_profiles_in_one_query(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    captured = capture_config_query(monkeypatch)

    resp = client.get(
        f"/api/repos/{repo.name}/orchestrator-config"
        "?backend=cc-codex&dev_profile=standard&review_profile=excellent"
    )
    assert resp.status_code == 200
    assert captured["calls"] == 1  # one coherent resolution, not one per role
    assert captured["backend"] == "cc-codex"
    assert captured["dev_profile"] == "standard"
    assert captured["review_profile"] == "excellent"
    assert captured["profile"] is None  # the split path never sends the run-wide one
    assert resp.json()["profiles"] == {"dev": "standard", "review": "excellent"}


def test_orchestrator_config_reports_legacy_deployment_without_split_support(
    tmp_path, monkeypatch
):
    """An older deployment answers the flag-free probe with no `profiles`; the
    response stays readable but tells the dialog it cannot offer split selection."""
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    legacy = {k: v for k, v in SAMPLE_CONFIG.items() if k != "profiles"}
    capture_config_query(monkeypatch, legacy)

    body = client.get(f"/api/repos/{repo.name}/orchestrator-config").json()

    assert body["split_profiles"] is False
    assert body["profiles"] == {"dev": "standard", "review": "standard"}
    assert body["profile"] == "standard"


def test_orchestrator_config_unknown_repo_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = client.get("/api/repos/ghost/orchestrator-config")
    assert resp.status_code == 404


def test_orchestrator_config_bad_backend_is_400(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = client.get(f"/api/repos/{repo.name}/orchestrator-config?backend=banana")
    assert resp.status_code == 400


def test_orchestrator_config_query_failure_is_502(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)

    def failing_query(target, *, backend=None, profile=None, dev_profile=None, review_profile=None):
        raise OrchestratorConfigError(
            "orchestrator --print-config failed: unrecognized arguments"
        )

    monkeypatch.setattr(api, "query_effective_config", failing_query)
    resp = client.get(f"/api/repos/{repo.name}/orchestrator-config")
    assert resp.status_code == 502
    assert "unrecognized arguments" in resp.json()["detail"]


def test_orchestrator_config_requires_auth(tmp_path, monkeypatch):
    repo = make_run_repo(tmp_path)
    manager, spawner = make_run_manager(tmp_path)
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    unauthed = TestClient(api.create_app(run_config(tmp_path), run_manager=manager))
    resp = unauthed.get(f"/api/repos/{repo.name}/orchestrator-config")
    assert resp.status_code == 401


# -- run effective-config snapshot route --------------------------------------


def effective_config_line(config: dict = SAMPLE_CONFIG) -> str:
    return "[03:00:00] effective-config: " + json.dumps(
        config, sort_keys=True, separators=(",", ":")
    )


def test_run_effective_config_live_parses_log(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_run_log(manager, started["run_id"], [effective_config_line(), "[03:00:01] orchestrator start"])

    resp = client.get(f"/api/runs/{started['run_id']}/effective-config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == started["run_id"]
    assert body["effective_config"] == SAMPLE_CONFIG


def test_run_effective_config_null_when_absent(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)  # no log with the line
    resp = client.get(f"/api/runs/{started['run_id']}/effective-config")
    assert resp.status_code == 200
    assert resp.json()["effective_config"] is None


def test_run_effective_config_unknown_run_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = client.get("/api/runs/20990101T000000Z-nope-x/effective-config")
    assert resp.status_code == 404


def test_history_runs_embeds_effective_config(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_run_log(manager, started["run_id"], [effective_config_line()])
    spawner.procs[0].returncode = 0
    client.get("/api/runs")  # observe end -> snapshot persisted

    resp = client.get("/api/history/runs?limit=1")
    assert resp.status_code == 200
    assert resp.json()["runs"][0]["effective_config"] == SAMPLE_CONFIG


# -- list / get ----------------------------------------------------------------


def test_list_runs_returns_statuses(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)

    resp = client.get("/api/runs")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == started["run_id"]
    assert runs[0]["state"] == "active"


def test_history_runs_returns_recent_runs_with_events(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    first = start_a_run(client, repo)
    spawner.procs[0].returncode = 0
    client.get("/api/runs")  # observe first end and free the repo
    second = start_a_run(client, repo)

    resp = client.get("/api/history/runs?limit=1")

    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == second["run_id"]
    assert runs[0]["task_status"] == "in_progress"
    assert [event["kind"] for event in runs[0]["events"]] == [KIND_RUN_STARTED]
    assert first["run_id"] not in {run["run_id"] for run in runs}


# -- run-history paging --------------------------------------------------------
# (2026-07-30-run-history-pagination-collapse)
#
# The route serves two contracts over the same data: fixed-size pages walked by
# cursor (the traversal), and a bounded summary (`?limit=N`, the Overview's
# strip). The checks below are that each stays itself — a page's size cannot be
# chosen by the caller, and a summary cannot be walked — and that a page carries
# the metadata a pager needs instead of leaving it to infer boundaries.

PAGING_KEYS = {
    "page",
    "page_size",
    "total_runs",
    "total_pages",
    "has_prev",
    "has_next",
    "prev_cursor",
    "next_cursor",
}


def run_history_arc(client, spawner, repo, count: int) -> list[str]:
    """Start and end ``count`` runs of the repo's task; return ids oldest-first.

    Each run has to end before the next may start (per-repo serialization), and
    ``GET /api/runs`` is what observes the end.
    """
    ids = []
    for i in range(count):
        ids.append(start_a_run(client, repo)["run_id"])
        spawner.procs[i].returncode = 0
        client.get("/api/runs")  # observe the end, freeing the repo
    return ids


def test_history_runs_walks_persisted_runs_by_cursor(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    newest_first = list(reversed(run_history_arc(client, spawner, repo, 3)))

    first = client.get("/api/history/runs").json()
    rest = client.get("/api/history/runs?cursor=" + newest_first[1]).json()
    back = client.get("/api/history/runs?cursor=" + rest["prev_cursor"]).json()

    # No cursor starts at the newest run; a cursor anchors the window at the run
    # it names, so a step is a position in the ordering rather than an offset
    # into a list re-read per request.
    assert [run["run_id"] for run in first["runs"]] == newest_first
    assert [run["run_id"] for run in rest["runs"]] == newest_first[1:]
    # Both windows carry the whole-history totals, so a pager needs no second
    # request to know where it is.
    assert (first["total_runs"], rest["total_runs"]) == (3, 3)
    assert (first["prev_cursor"], first["next_cursor"]) == (None, None)
    assert (first["has_prev"], first["has_next"]) == (False, False)
    # Off the newest window the response carries a Previous cursor, which the
    # client echoes back: it resolves to the runs strictly newer than this window,
    # so a round trip never re-serves a run the pager was already showing.
    assert rest["has_prev"] is True
    assert [run["run_id"] for run in back["runs"]] == newest_first[:1]
    assert (back["page"], back["has_prev"]) == (1, False)
    # Both windows of that walk report a position inside their own page count,
    # even though neither sits on the snapshot's runs-over-page-size grid: the
    # count is of the traversal's windows, so a client can render "Page N of M"
    # verbatim instead of guarding against N > M.
    assert (rest["page"], rest["total_pages"]) == (2, 2)
    assert (back["page"], back["total_pages"]) == (1, 2)
    # And its own Next closes the round trip on the window it came from.
    forward = client.get("/api/history/runs?cursor=" + back["next_cursor"]).json()
    assert [run["run_id"] for run in forward["runs"]] == newest_first[1:]


def test_history_runs_page_size_is_fixed_and_a_summary_is_not_a_page(
    tmp_path, monkeypatch
):
    # The window size is the traversal's invariant, not a caller preference: no
    # query can change it. `limit` selects the *other* contract, and what comes
    # back carries no pagination metadata at all, so a short bounded read can
    # never be mistaken for — or walked as — a page.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    run_history_arc(client, spawner, repo, 3)

    paged = client.get("/api/history/runs?page_size=2&page=2").json()
    summary = client.get("/api/history/runs?limit=2").json()

    assert paged["page_size"] == 50
    assert (paged["page"], paged["total_pages"]) == (1, 1)
    assert len(paged["runs"]) == 3
    assert len(summary["runs"]) == 2
    assert not PAGING_KEYS & set(summary)


def test_history_runs_rejects_a_summary_asked_to_page(tmp_path, monkeypatch):
    # The two contracts answer different questions and only the caller knows
    # which it wanted, so mixing them is a 400 rather than a silent precedence.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    run_history_arc(client, spawner, repo, 2)

    resp = client.get("/api/history/runs?limit=1&cursor=anything")

    assert resp.status_code == 400
    assert "cursor" in resp.json()["detail"]


def test_history_runs_empty_history_reports_no_pages(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)

    payload = client.get("/api/history/runs").json()

    assert payload["runs"] == []
    assert payload["page"] == 1
    assert payload["page_size"] == 50
    assert payload["total_runs"] == 0
    assert payload["total_pages"] == 0
    assert (payload["has_prev"], payload["has_next"]) == (False, False)


def test_history_runs_cursor_past_the_end_is_empty_and_navigable(
    tmp_path, monkeypatch
):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    newest_first = list(reversed(run_history_arc(client, spawner, repo, 3)))

    beyond = client.get("/api/history/runs?cursor=0")  # below every run id

    # A window whose runs are gone (pruned between two requests, or a hand-typed
    # cursor) is a 200 with an empty window and honest totals — not an error and
    # not a silent wrap — and its Previous carries a cursor back to the last runs
    # that do exist, so the pager is never a dead end.
    assert beyond.status_code == 200
    payload = beyond.json()
    assert payload["runs"] == []
    assert (payload["page"], payload["total_pages"]) == (2, 1)
    assert (payload["has_prev"], payload["has_next"]) == (True, False)
    back = client.get("/api/history/runs?cursor=" + payload["prev_cursor"]).json()
    assert [run["run_id"] for run in back["runs"]] == newest_first


def test_history_runs_batches_evaluation_state_for_the_returned_window_only(
    tmp_path, monkeypatch
):
    # Batched evaluation state covers the task groups a client can actually see
    # in this window — it must not fan out over the whole history.
    repo_a = make_run_repo(tmp_path, "repo-a")
    repo_b = make_run_repo(tmp_path, "repo-b")
    manager, spawner = make_run_manager(tmp_path)
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo_a, repo_b])
    client = TestClient(api.create_app(run_config(tmp_path), run_manager=manager))
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    start_a_run(client, repo_a)
    older = client.get("/api/history/runs").json()["runs"][-1]["run_id"]
    start_a_run(client, repo_b)  # a different repo may run concurrently

    whole = client.get("/api/history/runs").json()
    tail = client.get("/api/history/runs?cursor=" + older).json()

    assert [state["repo_name"] for state in whole["evaluation_states"]] == [
        "repo-b",
        "repo-a",
    ]
    assert [run["run_id"] for run in tail["runs"]] == [older]
    assert [state["repo_name"] for state in tail["evaluation_states"]] == ["repo-a"]


def test_history_runs_batches_three_level_evaluation_state(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    first = start_a_run(client, repo)
    spawner.procs[0].returncode = 0
    client.get("/api/runs")  # observe the end and allow a second run of the task
    start_a_run(client, repo)

    calls = []
    original = EvaluationService.list_evaluations

    def counted_list(service, target_repo, task_id):
        calls.append((target_repo.path, task_id))
        return original(service, target_repo, task_id)

    monkeypatch.setattr(EvaluationService, "list_evaluations", counted_list)

    # Evidence alone is not an evaluation report, so it stays at the lowest
    # level. The one task has two runs but is structurally listed only once.
    evaluation = EvaluationService(run_config(tmp_path))
    evaluation_id = "20260730T010000Z"
    artifact_dir = evaluation.artifact_dir(repo, RUN_TASK_ID, evaluation_id)
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "evidence.json").write_text("{}", encoding="utf-8")

    payload = client.get("/api/history/runs").json()
    assert payload["evaluation_enabled"] is True
    assert payload["evaluation_states"] == [
        {
            "repo_name": repo.name,
            "repo_path": str(repo.path),
            "task_id": RUN_TASK_ID,
            "latest_evaluation_id": evaluation_id,
            "level": "none",
        }
    ]
    assert calls == [(repo.path, RUN_TASK_ID)]

    # The same batched entry advances through L1 and L1+L2 from the newest
    # evaluation's artifact set.
    (artifact_dir / "static_metrics.json").write_text("{}", encoding="utf-8")
    assert client.get("/api/history/runs").json()["evaluation_states"][0]["level"] == "l1"
    (artifact_dir / "semantic_report.json").write_text("{}", encoding="utf-8")
    assert (
        client.get("/api/history/runs").json()["evaluation_states"][0]["level"]
        == "l1_l2"
    )


def test_history_runs_evaluation_state_degrades_without_hiding_runs(
    tmp_path, monkeypatch
):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)

    def unreadable(_service, _repo, _task_id):
        raise OSError("evaluation tree unavailable")

    monkeypatch.setattr(EvaluationService, "list_evaluations", unreadable)
    payload = client.get("/api/history/runs").json()

    assert [run["run_id"] for run in payload["runs"]] == [started["run_id"]]
    assert payload["evaluation_enabled"] is True
    assert payload["evaluation_states"] == []


def test_history_runs_disabled_evaluation_skips_state_reads(tmp_path, monkeypatch):
    repo = make_run_repo(tmp_path)
    manager, spawner = make_run_manager(tmp_path)
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])

    def should_not_read(_service, _repo, _task_id):
        raise AssertionError("disabled evaluation state was read")

    monkeypatch.setattr(EvaluationService, "list_evaluations", should_not_read)
    disabled = replace(run_config(tmp_path), evaluation_enabled=False)
    client = TestClient(api.create_app(disabled, run_manager=manager))
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    start_a_run(client, repo)

    payload = client.get("/api/history/runs").json()
    assert payload["evaluation_enabled"] is False
    assert payload["evaluation_states"] == []


def test_get_run_returns_status_and_unknown_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)

    ok = client.get(f"/api/runs/{started['run_id']}")
    assert ok.status_code == 200
    assert ok.json()["run_id"] == started["run_id"]

    missing = client.get("/api/runs/20990101T000000Z-nope-x")
    assert missing.status_code == 404


# -- run-scoped task view (active or archived) --------------------------------
#
# GET /api/runs/{run_id}/task resolves the task from the persisted run record,
# not the active index, so a run's task file stays reachable after close-out
# archives it. The inventory routes stay active-only and index-gated; there is
# deliberately no general archived-task listing.


def archive_run_task(repo: ManagedRepo, task_id: str, body: str) -> Path:
    """Simulate close-out: drop the task from the active index/file into archive."""
    tasks_dir = repo.path / ".ai-tasks"
    (tasks_dir / f"{task_id}.md").unlink()  # remove the active task file
    write_task_index(repo.path, [])  # ...and its active-index row
    archive_dir = tasks_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{task_id}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_run_task_returns_active_task(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)

    body = client.get(f"/api/runs/{started['run_id']}/task").json()
    assert body["task_id"] == RUN_TASK_ID
    assert body["source"] == "active"
    assert body["present"] is True
    assert f"id: {RUN_TASK_ID}" in body["content"]


def test_run_task_falls_back_to_archived_task(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    archived = f"---\nid: {RUN_TASK_ID}\nstatus: completed\n---\n\n# archived body\n"
    archive_run_task(repo, RUN_TASK_ID, archived)

    body = client.get(f"/api/runs/{started['run_id']}/task").json()
    assert body["source"] == "archived"
    assert body["present"] is True
    assert body["content"] == archived
    # The active inventory routes stay active-only: the archived (now unlisted)
    # task no longer resolves there — no general archived-task listing.
    assert client.get(f"/api/repos/{repo.name}/tasks/{RUN_TASK_ID}").status_code == 404
    listing = client.get(f"/api/repos/{repo.name}/tasks").json()
    assert listing["repos"][0]["tasks"] == []


def test_run_task_missing_task_is_absent(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    # Neither active nor archived: the active file is gone and close-out never
    # archived it.
    (repo.path / ".ai-tasks" / f"{RUN_TASK_ID}.md").unlink()

    body = client.get(f"/api/runs/{started['run_id']}/task").json()
    assert body["source"] is None
    assert body["present"] is False
    assert body["content"] is None


def test_run_task_unknown_run_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = client.get("/api/runs/20990101T000000Z-nope-x/task")
    assert resp.status_code == 404


# -- stop ----------------------------------------------------------------------


def test_stop_run_writes_flag_and_is_idempotent(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)

    first = client.post(f"/api/runs/{started['run_id']}/stop")
    assert first.status_code == 200
    assert first.json()["newly_requested"] is True
    assert (control_dir_of(manager, started["run_id"]) / "stop.flag").is_file()

    again = client.post(f"/api/runs/{started['run_id']}/stop")
    assert again.json()["newly_requested"] is False
    # A stop is a request, not a kill: the run is still alive.
    assert client.get(f"/api/runs/{started['run_id']}").json()["state"] == "active"


def test_stop_run_unknown_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = client.post("/api/runs/20990101T000000Z-nope-x/stop")
    assert resp.status_code == 404


def test_stop_run_ended_is_409(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    spawner.procs[0].returncode = 3  # the run ends

    resp = client.post(f"/api/runs/{started['run_id']}/stop")
    assert resp.status_code == 409


def test_running_run_reports_stop_requested_across_reads(tmp_path, monkeypatch):
    # The stop is durable server state, so every read after POST /stop — a page
    # reload, a second device — sees stop_requested=True while the run is live,
    # instead of the client re-enabling Stop because its transient flag was lost.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    run_id = started["run_id"]

    assert started["stop_requested"] is False
    assert client.get(f"/api/runs/{run_id}").json()["stop_requested"] is False
    assert client.get("/api/runs").json()["runs"][0]["stop_requested"] is False

    client.post(f"/api/runs/{run_id}/stop")

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["state"] == "active"  # still live; a stop is a request
    assert detail["stop_requested"] is True
    assert client.get("/api/runs").json()["runs"][0]["stop_requested"] is True


# -- escalations: list ---------------------------------------------------------


def test_escalations_list_pending_questions_with_context(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_question(
        control_dir_of(manager, started["run_id"]),
        kind="blocked",
        banner="need a ruling",
    )

    resp = client.get("/api/escalations")
    assert resp.status_code == 200
    escalations = resp.json()["escalations"]
    assert len(escalations) == 1
    entry = escalations[0]
    assert entry["run_id"] == started["run_id"]
    assert entry["repo_name"] == repo.name
    assert entry["task_id"] == RUN_TASK_ID
    assert entry["question"]["seq"] == 1
    assert entry["question"]["banner"] == "need a ruling"
    assert entry["question"]["answered"] is False


def test_escalations_empty_without_pending_questions(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    start_a_run(client, repo)
    assert client.get("/api/escalations").json() == {"escalations": []}


def test_escalations_skip_ended_runs(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_question(control_dir_of(manager, started["run_id"]), banner="?")
    spawner.procs[0].returncode = 0  # run ends with the question still pending

    # A question on a finished run cannot unblock anything, so it is excluded.
    assert client.get("/api/escalations").json() == {"escalations": []}


# -- escalations: answer -------------------------------------------------------


def test_answer_escalation_writes_answer_file(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, banner="?")

    resp = client.post(
        f"/api/runs/{started['run_id']}/escalations/1/answer",
        json={"answer": "  proceed  ", "responder": "iphone"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "run_id": started["run_id"],
        "seq": 1,
        "answered": True,
        "responder": "iphone",
        "answer": "proceed",
    }
    # The write went through control_dir: a valid, audited answer file exists.
    written = json.loads((control / "001-answer.json").read_text(encoding="utf-8"))
    assert written["answer"] == "proceed"
    assert written["responder"] == "iphone"
    assert written["seq"] == 1
    events = read_events(manager.runs_dir / started["run_id"])
    assert [event.kind for event in events] == [KIND_RUN_STARTED, KIND_ANSWER_RECORDED]
    audit = events[-1].to_dict()
    assert audit["seq"] == 1
    assert audit["question_banner"] == "?"
    assert audit["responder"] == "iphone"
    assert "answer" not in audit
    assert "proceed" not in json.dumps(audit)
    # The question is no longer an escalation.
    assert client.get("/api/escalations").json()["escalations"] == []


def test_answer_escalation_defaults_responder_to_orch_hub(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_question(control_dir_of(manager, started["run_id"]), banner="?")

    resp = client.post(
        f"/api/runs/{started['run_id']}/escalations/1/answer", json={"answer": "go"}
    )
    assert resp.json()["responder"] == "orch-hub"


def test_answer_escalation_unknown_run_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/runs/20990101T000000Z-nope-x/escalations/1/answer", json={"answer": "go"}
    )
    assert resp.status_code == 404


def test_answer_escalation_unknown_seq_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)  # no question files written

    resp = client.post(
        f"/api/runs/{started['run_id']}/escalations/9/answer", json={"answer": "go"}
    )
    assert resp.status_code == 404
    assert "seq 9" in resp.json()["detail"]


def test_answer_escalation_duplicate_is_409(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_question(control_dir_of(manager, started["run_id"]), banner="?")
    url = f"/api/runs/{started['run_id']}/escalations/1/answer"

    assert client.post(url, json={"answer": "first"}).status_code == 200
    dup = client.post(url, json={"answer": "second"})
    assert dup.status_code == 409  # existing valid answer is never overwritten


def test_answer_escalation_empty_answer_is_400(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_question(control_dir_of(manager, started["run_id"]), banner="?")

    resp = client.post(
        f"/api/runs/{started['run_id']}/escalations/1/answer", json={"answer": "   "}
    )
    assert resp.status_code == 400


# -- escalations: answer mode / kind normalization -----------------------------
#
# The route reconciles the request ``mode`` with the question ``kind`` through
# ``normalize_answer`` before writing (control_dir mirrors the orchestrator's
# own parse; the pure-helper matrix is unit-tested in test_control_dir.py). Here
# the same cells go end-to-end: the written answer file, the fail-closed 400s,
# the ``mode`` audit metadata, and back-compat with an omitted field.


def answer(client, run_id, seq=1, **body):
    return client.post(f"/api/runs/{run_id}/escalations/{seq}/answer", json=body)


def answer_file_text(control: Path, seq: int = 1) -> str:
    return json.loads((control / f"{seq:03d}-answer.json").read_text())["answer"]


def audit_of(manager, run_id):
    events = read_events(manager.runs_dir / run_id)
    assert [e.kind for e in events] == [KIND_RUN_STARTED, KIND_ANSWER_RECORDED]
    return events[-1].to_dict()


def test_answer_no_mode_is_back_compat(tmp_path, monkeypatch):
    # An omitted ``mode`` keeps a plain binding answer's behavior: verbatim
    # write, and no button intent is recorded in the audit (mode is None).
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, banner="Need a ruling")  # no kind -> non-plan-gate

    resp = answer(client, started["run_id"], answer="  proceed with plan B  ")
    assert resp.status_code == 200
    assert resp.json()["answer"] == "proceed with plan B"
    assert answer_file_text(control) == "proceed with plan B"
    assert audit_of(manager, started["run_id"])["mode"] is None


def test_answer_no_mode_marker_prefixed_is_back_compat(tmp_path, monkeypatch):
    # A no-mode request with a discussion-marker answer must NOT hit the
    # answer-mode guard: with no declared button intent it is written verbatim,
    # exactly as before the ``mode`` field existed, for the orchestrator to parse
    # as a discussion turn.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, banner="Need a ruling")  # no kind -> non-plan-gate

    resp = answer(client, started["run_id"], answer="? clarify the constraint")
    assert resp.status_code == 200
    assert answer_file_text(control) == "? clarify the constraint"
    assert audit_of(manager, started["run_id"])["mode"] is None


def test_answer_no_mode_plan_gate_feedback_is_back_compat(tmp_path, monkeypatch):
    # A no-mode plan-gate answer that is not a confirm word must NOT hit the
    # plan-gate answer guard: with no declared button intent it is written
    # verbatim for the orchestrator to parse as plan feedback (today's behavior).
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, kind="plan-gate", banner="Plan OK?")

    resp = answer(client, started["run_id"], answer="revise step 2")
    assert resp.status_code == 200
    assert answer_file_text(control) == "revise step 2"
    assert audit_of(manager, started["run_id"])["mode"] is None


def test_answer_discuss_mode_prepends_marker_and_audits_mode(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, banner="Need a ruling")

    resp = answer(client, started["run_id"], answer="what about plan C?", mode="discuss")
    assert resp.status_code == 200
    # The orchestrator will strip the marker and route the rest into the session.
    assert answer_file_text(control) == "discuss: what about plan C?"
    audit = audit_of(manager, started["run_id"])
    assert audit["mode"] == "discuss"
    assert "plan C" not in json.dumps(audit)  # intent metadata only, no answer text


def test_answer_discuss_mode_keeps_existing_marker(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, banner="Need a ruling")

    resp = answer(client, started["run_id"], answer="? already asking", mode="discuss")
    assert resp.status_code == 200
    assert answer_file_text(control) == "? already asking"


def test_answer_mode_refuses_marker_prefixed_text_fail_closed(tmp_path, monkeypatch):
    # A marker in answer mode would be silently reinterpreted as a discussion
    # turn: refuse it, and write nothing.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, banner="Need a ruling")

    resp = answer(client, started["run_id"], answer="? sneaky", mode="answer")
    assert resp.status_code == 400
    assert "discussion marker" in resp.json()["detail"]
    assert not (control / "001-answer.json").exists()  # nothing written
    # No answer_recorded event either — the guard fired before the write.
    assert [e.kind for e in read_events(manager.runs_dir / started["run_id"])] == [
        KIND_RUN_STARTED
    ]


def test_answer_plan_gate_empty_confirms_in_one_tap(tmp_path, monkeypatch):
    # A plan-gate approval is one tap on empty input: the hub fills ``confirm``.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, kind="plan-gate", banner="Plan OK?")

    resp = answer(client, started["run_id"], answer="", mode="answer")
    assert resp.status_code == 200
    assert answer_file_text(control) == "confirm"
    assert audit_of(manager, started["run_id"])["mode"] == "answer"


def test_answer_plan_gate_confirm_word_is_verbatim(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, kind="plan-gate", banner="Plan OK?")

    resp = answer(client, started["run_id"], answer="  Approve  ", mode="answer")
    assert resp.status_code == 200
    assert answer_file_text(control) == "Approve"  # write_answer strips


def test_answer_plan_gate_answer_mode_refuses_feedback(tmp_path, monkeypatch):
    # Free text on the [Confirm] button would be parsed as feedback, not
    # approval — a button/parse mismatch — so it fails closed.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, kind="plan-gate", banner="Plan OK?")

    resp = answer(client, started["run_id"], answer="revise the wording", mode="answer")
    assert resp.status_code == 400
    assert "plan approval" in resp.json()["detail"]
    assert not (control / "001-answer.json").exists()


def test_answer_plan_gate_discuss_sends_feedback(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, kind="plan-gate", banner="Plan OK?")

    resp = answer(client, started["run_id"], answer="tighten the scope", mode="discuss")
    assert resp.status_code == 200
    assert answer_file_text(control) == "tighten the scope"
    assert audit_of(manager, started["run_id"])["mode"] == "discuss"


def test_answer_plan_gate_discuss_refuses_bare_confirm_word(tmp_path, monkeypatch):
    # A bare confirm word in discuss mode would approve instead of discuss.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, kind="plan-gate", banner="Plan OK?")

    resp = answer(client, started["run_id"], answer="confirm", mode="discuss")
    assert resp.status_code == 400
    assert "plan-approval word" in resp.json()["detail"]
    assert not (control / "001-answer.json").exists()


@pytest.mark.parametrize("kind", [None, "plan-gate"])
@pytest.mark.parametrize("mode", ["answer", "discuss"])
def test_answer_stop_passes_through_every_mode_and_kind(tmp_path, monkeypatch, kind, mode):
    # ``stop`` aborts the run before any marker/confirm parsing, so it is
    # written verbatim regardless of mode/kind (never discuss-prefixed).
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    fields = {"banner": "?"} if kind is None else {"kind": kind, "banner": "Plan OK?"}
    write_question(control, **fields)

    resp = answer(client, started["run_id"], answer="stop", mode=mode)
    assert resp.status_code == 200
    assert answer_file_text(control) == "stop"


def test_answer_invalid_mode_is_422(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    control = control_dir_of(manager, started["run_id"])
    write_question(control, banner="Need a ruling")

    resp = answer(client, started["run_id"], answer="go", mode="bogus")
    assert resp.status_code == 422  # rejected by request-model validation
    assert not (control / "001-answer.json").exists()


# -- run sessions (per-session context) ----------------------------------------


ORCH_SESSION_LOG = [
    "[03:00:10] --- dev plan-gate start: sid=pg backend=claude:x@max status_before=in_progress",
    "[03:05:00] run finished: status=finished context≈100000 tokens",
    "[03:05:00] --- dev plan-gate end: sid=pg",
    "[03:05:01] --- dev session start: sid=dev1 backend=claude:x@max status_before=in_progress",
    "[03:20:00] run finished: status=finished context≈150000 tokens",
    "[03:20:00] --- dev session end: sid=dev1 status=final_review",
]


def test_get_run_sessions_live_returns_parsed_summary(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_run_log(manager, started["run_id"], ORCH_SESSION_LOG)

    resp = client.get(f"/api/runs/{started['run_id']}/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == started["run_id"]
    assert body["context_budget"] == 200_000
    assert [(s["kind"], s["sid"], s["turns"], s["context_tokens"]) for s in body["sessions"]] == [
        ("plan-gate", "pg", 1, 100000),
        ("session", "dev1", 1, 150000),
    ]
    assert body["sessions"][0]["role"] == "dev"
    assert body["sessions"][1]["end_status"] == "final_review"


def test_get_run_sessions_no_log_is_empty_and_200(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)  # no orchestrator log yet

    resp = client.get(f"/api/runs/{started['run_id']}/sessions")

    assert resp.status_code == 200  # missing data is graceful, never a 5xx
    body = resp.json()
    assert body["sessions"] == []
    assert body["context_budget"] == 200_000


def test_get_run_sessions_ended_served_from_snapshot_after_log_deleted(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    log_path = write_run_log(manager, started["run_id"], ORCH_SESSION_LOG)
    spawner.procs[0].returncode = 0
    client.get("/api/runs")  # observe the end -> snapshot persisted
    log_path.unlink()  # repo-side log cleaned up afterward

    body = client.get(f"/api/runs/{started['run_id']}/sessions").json()

    assert [s["sid"] for s in body["sessions"]] == ["pg", "dev1"]  # survived deletion


def test_get_run_sessions_unknown_run_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = client.get("/api/runs/20990101T000000Z-nope-x/sessions")
    assert resp.status_code == 404


def test_history_runs_embeds_per_run_sessions_snapshot(tmp_path, monkeypatch):
    # Each history entry embeds the run's persisted per-session snapshot (shape: a
    # `sessions` list + the soft `context_budget`), so the history view renders
    # context strips without an extra per-run request. An ended run with a log
    # carries its parsed sessions.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_run_log(manager, started["run_id"], ORCH_SESSION_LOG)
    spawner.procs[0].returncode = 0
    client.get("/api/runs")  # observe the end -> snapshot persisted

    run = client.get("/api/history/runs").json()["runs"][0]
    assert run["context_budget"] == 200_000
    assert [(s["kind"], s["sid"]) for s in run["sessions"]] == [
        ("plan-gate", "pg"),
        ("session", "dev1"),
    ]


def test_history_runs_sessions_shape_present_even_when_empty(tmp_path, monkeypatch):
    # Every history entry carries the sessions shape even with no parsed log: an
    # empty `sessions` list plus the soft budget, so the client reads it uniformly.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    start_a_run(client, repo)  # no orchestrator log written

    run = client.get("/api/history/runs").json()["runs"][0]
    assert run["sessions"] == []
    assert run["context_budget"] == 200_000


def write_run_task_est(repo: ManagedRepo, *, current: int, total: int) -> None:
    """Give the run task a ``session-est: current/total`` for the rollup source."""
    (repo.path / ".ai-tasks" / f"{RUN_TASK_ID}.md").write_text(
        f"---\nid: {RUN_TASK_ID}\nstatus: in_progress\n"
        f"session-est: {current}/{total}\n---\n\n# Task\n",
        encoding="utf-8",
    )


def test_history_runs_embeds_session_est(tmp_path, monkeypatch):
    # Each history entry embeds the run task's typed session-est (current/total),
    # so the est-vs-actual task rollup needs no per-task follow-up request.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    write_run_task_est(repo, current=1, total=3)
    start_a_run(client, repo)

    run = client.get("/api/history/runs").json()["runs"][0]
    assert run["session_est"] == {"current": 1, "total": 3}


def test_history_runs_session_est_null_when_unknown(tmp_path, monkeypatch):
    # A task with no session-est frontmatter carries the key as null (unknown),
    # so the client reads the shape uniformly rather than probing for absence.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    start_a_run(client, repo)  # default run task has no session-est

    run = client.get("/api/history/runs").json()["runs"][0]
    assert run["session_est"] is None


# -- run logs ------------------------------------------------------------------


def test_get_run_logs_returns_recent_tail(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_run_log(manager, started["run_id"], ["run started", "dev turn", "done"])

    resp = client.get(f"/api/runs/{started['run_id']}/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == started["run_id"]
    assert body["present"] is True
    assert body["lines"] == ["run started", "dev turn", "done"]
    assert body["truncated"] is False
    assert body["size"] > 0
    # Filesystem paths are internal; the route must not leak the log path.
    assert "path" not in body


def test_get_run_logs_missing_is_graceful(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)  # no log file written yet

    resp = client.get(f"/api/runs/{started['run_id']}/logs")
    assert resp.status_code == 200  # a missing log is normal, not an error
    body = resp.json()
    assert body["present"] is False
    assert body["lines"] == []
    assert body["detail"]


def test_get_run_logs_tail_param_limits_lines(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_run_log(manager, started["run_id"], [f"line-{i}" for i in range(10)])

    body = client.get(f"/api/runs/{started['run_id']}/logs?tail=3").json()
    assert body["lines"] == ["line-7", "line-8", "line-9"]
    assert body["truncated"] is True


def test_get_run_logs_unknown_run_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = client.get("/api/runs/20990101T000000Z-nope-x/logs")
    assert resp.status_code == 404


def test_get_run_logs_falls_back_to_spawn_out_diagnostic(tmp_path, monkeypatch):
    # A run that fails during startup never opens an orchestrator log; its error
    # lands in spawn.out. The route surfaces that capture as `diagnostic` so the
    # log view is a real error tail rather than empty.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_spawn_out(
        manager,
        started["run_id"],
        "Traceback (most recent call last):\n  ...\nRuntimeError: missing credential\n",
    )
    spawner.procs[0].returncode = 1  # exits during startup, no orchestrator log

    body = client.get(f"/api/runs/{started['run_id']}/logs").json()
    assert body["present"] is False  # no orchestrator log located
    assert body["lines"] == []
    diag = body["diagnostic"]
    assert diag is not None
    assert diag["source"] == "spawn.out"
    assert diag["present"] is True
    assert diag["lines"][-1] == "RuntimeError: missing credential"


def test_get_run_logs_no_diagnostic_when_orchestrator_log_present(tmp_path, monkeypatch):
    # A real orchestrator log wins; the spawn.out fallback is not attached even
    # when a capture exists.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_run_log(manager, started["run_id"], ["run started", "done"])
    write_spawn_out(manager, started["run_id"], "some startup chatter\n")

    body = client.get(f"/api/runs/{started['run_id']}/logs").json()
    assert body["present"] is True
    assert body["diagnostic"] is None


def test_get_run_logs_empty_spawn_out_leaves_diagnostic_null(tmp_path, monkeypatch):
    # spawn.out is created empty by start_run; with no orchestrator log and an
    # empty capture there is nothing to show, so diagnostic stays null and the
    # plain "no log" detail rides along.
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    spawner.procs[0].returncode = 0

    body = client.get(f"/api/runs/{started['run_id']}/logs").json()
    assert body["present"] is False
    assert body["diagnostic"] is None
    assert body["detail"]


# -- run logs: SSE stream ------------------------------------------------------
#
# The stream is driven against ended runs so the tail generator terminates and
# ``client.get`` returns the full body (a live run would tail forever). The
# generator hits no real ``asyncio.sleep`` on the ended path: it drains the
# located log, emits ``event: end``, and stops.


def test_stream_run_logs_emits_events_for_ended_run(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_run_log(manager, started["run_id"], ["run started", "dev turn", "done"])
    spawner.procs[0].returncode = 0  # the run ends, so the stream terminates

    resp = client.get(f"/api/runs/{started['run_id']}/logs/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "data: run started\n\n" in body
    assert "data: dev turn\n\n" in body
    assert "data: done\n\n" in body
    assert body.endswith("event: end\ndata: run ended\n\n")


def test_stream_run_logs_missing_log_ends_cleanly(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    spawner.procs[0].returncode = 0  # ended, and no log was ever written

    resp = client.get(f"/api/runs/{started['run_id']}/logs/stream")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.text == "event: end\ndata: no log file was written for this run\n\n"


def test_stream_run_logs_tail_param_bounds_context(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    started = start_a_run(client, repo)
    write_run_log(manager, started["run_id"], [f"line-{i}" for i in range(5)])
    spawner.procs[0].returncode = 0

    body = client.get(f"/api/runs/{started['run_id']}/logs/stream?tail=2").text
    assert "data: line-3\n\n" in body
    assert "data: line-4\n\n" in body
    assert "data: line-0\n\n" not in body


def test_stream_run_logs_unknown_run_is_404(tmp_path, monkeypatch):
    client, manager, spawner, repo = make_run_client(tmp_path, monkeypatch)
    resp = client.get("/api/runs/20990101T000000Z-nope-x/logs/stream")
    assert resp.status_code == 404


# -- auth ----------------------------------------------------------------------


def test_run_and_escalation_routes_require_auth(tmp_path, monkeypatch):
    # The default-deny middleware covers the new routes with no per-route
    # exception: an unauthenticated caller is 401 on every one of them.
    manager, _ = make_run_manager(tmp_path)
    unauthed = TestClient(api.create_app(run_config(tmp_path), run_manager=manager))
    assert unauthed.post(
        "/api/runs", json={"repo_id": "r", "task_id": RUN_TASK_ID}
    ).status_code == 401
    assert unauthed.get("/api/runs").status_code == 401
    assert unauthed.get("/api/history/runs").status_code == 401
    assert unauthed.get("/api/runs/anything").status_code == 401
    assert unauthed.get("/api/runs/anything/task").status_code == 401
    assert unauthed.post("/api/runs/anything/stop").status_code == 401
    assert unauthed.get("/api/runs/anything/logs").status_code == 401
    assert unauthed.get("/api/runs/anything/logs/stream").status_code == 401
    assert unauthed.get("/api/runs/anything/sessions").status_code == 401
    assert unauthed.get("/api/escalations").status_code == 401
    assert unauthed.post(
        "/api/runs/anything/escalations/1/answer", json={"answer": "go"}
    ).status_code == 401


# -- background monitor lifecycle ----------------------------------------------


def test_app_lifespan_starts_and_stops_the_run_monitor(tmp_path, monkeypatch):
    # The resident monitor is owned by the app lifecycle: not running on mere
    # construction (so the 300+ plain-TestClient tests spawn no thread), running
    # while the lifespan is active, and stopped again on shutdown.
    repo = make_run_repo(tmp_path)
    manager, _ = make_run_manager(tmp_path)
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    app = api.create_app(run_config(tmp_path), run_manager=manager)
    monitor = app.state.run_monitor

    assert not monitor.is_running  # construction alone starts no worker
    with TestClient(app):  # __enter__ runs startup, __exit__ runs shutdown
        assert monitor.is_running
    assert not monitor.is_running


# --- intake routes -----------------------------------------------------------
#
# These drive a *real* IntakeManager over a fake repo in ``tmp_path`` with an
# injected conversational agent/readiness/clock: no real /intake-task session is
# launched and no orchestrator code is imported. The fake agent writes to the
# repo ONLY inside ``confirm``, so the read-only-until-confirm invariant is
# observable through the HTTP surface too. Repo resolution monkeypatches
# ``discover_repos`` (as elsewhere); confirmed inventory refresh goes end-to-end
# through the real task store reading the files the fake agent wrote.

INTAKE_REQUEST = "Add a widget endpoint to the dashboard"
INTAKE_TASK_ID = "2026-07-11-add-widget"


def _intake_draft(iteration: int) -> IntakeDraft:
    return IntakeDraft(
        task_id=INTAKE_TASK_ID,
        summary="Add a widget",
        task_file=(
            f"---\nid: {INTAKE_TASK_ID}\nstatus: pending\nsession-est: 0/2\n"
            f"---\n\n# Add a widget\n\nDraft iteration {iteration}.\n"
        ),
        index_entry=f"| {INTAKE_TASK_ID} | Add a widget | pending | 0/2 | [] |",
    )


class FakeIntakeSession:
    """The opaque conversational handle the manager keeps between turns."""

    def __init__(self, request: str):
        self.request = request
        self.feedback: list[str] = []


class FakeIntakeAgent:
    """A scripted intake agent: draft/revise are read-only, confirm writes."""

    def __init__(self, *, draft_error=None, confirm_error=None):
        self.draft_error = draft_error
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
            handle=FakeIntakeSession(request), ref="sess-1", draft=_intake_draft(0)
        )

    def revise(self, handle, feedback, repo) -> AgentDraftResult:
        self.revise_calls += 1
        handle.feedback.append(feedback)
        return AgentDraftResult(
            handle=handle, ref="sess-1", draft=_intake_draft(len(handle.feedback))
        )

    def confirm(self, handle, repo) -> AgentConfirmResult:
        self.confirm_calls += 1
        if self.confirm_error is not None:
            raise self.confirm_error
        draft = _intake_draft(len(handle.feedback))
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


def make_intake_repo(tmp_path: Path, name: str = "repo-a") -> ManagedRepo:
    path = tmp_path / "repos" / name
    (path / ".ai-tasks").mkdir(parents=True)
    (path / ".ai-tasks" / "index.md").write_text(
        "# Active tasks\n\n| id | title | status | session-est | blockers |\n"
        "|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def intake_sync_executor(run) -> None:
    """Run an intake turn inline so a mutating route settles before responding.

    The manager runs each agent turn off its lock through an injected executor
    (a daemon thread in production). Injecting this synchronous one keeps the API
    tests deterministic: a mutating route still returns the transient record, but
    a follow-up ``GET /api/intakes/{id}`` observes the settled outcome with no
    thread joins.
    """
    run()


def make_intake_manager(
    tmp_path: Path,
    *,
    agent: FakeIntakeAgent | None = None,
    readiness=passing_readiness,
) -> tuple[IntakeManager, FakeIntakeAgent]:
    agent = agent if agent is not None else FakeIntakeAgent()
    manager = IntakeManager(
        run_config(tmp_path),
        agent=agent,
        readiness_checker=readiness,
        clock=lambda: FIXED_NOW,
        executor=intake_sync_executor,
    )
    return manager, agent


def make_intake_client(
    tmp_path,
    monkeypatch,
    *,
    repo: ManagedRepo | None = None,
    agent: FakeIntakeAgent | None = None,
    readiness=passing_readiness,
) -> tuple[TestClient, IntakeManager, FakeIntakeAgent, ManagedRepo]:
    repo = repo if repo is not None else make_intake_repo(tmp_path)
    manager, agent = make_intake_manager(tmp_path, agent=agent, readiness=readiness)
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    client = TestClient(api.create_app(run_config(tmp_path), intake_manager=manager))
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client, manager, agent, repo


def intake_task_files(repo: ManagedRepo) -> dict[str, bytes]:
    """Byte-exact ``.ai-tasks/`` file contents — a stray-write detector."""
    tasks = repo.path / ".ai-tasks"
    return {p.name: p.read_bytes() for p in sorted(tasks.iterdir()) if p.is_file()}


def start_an_intake(
    client: TestClient, repo: ManagedRepo, request: str = INTAKE_REQUEST
) -> dict:
    resp = client.post("/api/intakes", json={"repo_id": repo.name, "request": request})
    assert resp.status_code == 201, resp.text
    return resp.json()


# -- start intake --------------------------------------------------------------


def test_start_intake_returns_transient_drafting_then_settles(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    before = intake_task_files(repo)

    # POST returns the transient ``drafting`` record immediately (the turn runs
    # off the manager lock); there is no draft body yet and agent_ref stays
    # internal.
    body = start_an_intake(client, repo)
    assert body["status"] == STATUS_DRAFTING
    assert body["repo_name"] == repo.name
    assert body["request"] == INTAKE_REQUEST
    assert body["draft"] is None
    assert "agent_ref" not in body

    # A follow-up GET observes the settled draft — the injected sync executor ran
    # the turn within the POST, so it is already awaiting_review here (production
    # clients poll until this flip).
    settled = client.get(f"/api/intakes/{body['intake_id']}").json()
    assert settled["status"] == STATUS_AWAITING_REVIEW
    assert settled["iteration"] == 0
    assert settled["draft"]["task_id"] == INTAKE_TASK_ID
    assert settled["draft"]["task_file"].startswith("---")
    assert "agent_ref" not in settled
    assert agent.draft_calls == 1
    assert agent.confirm_calls == 0
    assert intake_task_files(repo) == before  # drafting is read-only


def test_start_intake_unknown_repo_is_404(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    resp = client.post(
        "/api/intakes", json={"repo_id": "ghost", "request": INTAKE_REQUEST}
    )
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]
    assert agent.draft_calls == 0


def test_start_intake_repo_busy_is_409(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    first = start_an_intake(client, repo)

    resp = client.post(
        "/api/intakes", json={"repo_id": repo.name, "request": "another request"}
    )
    assert resp.status_code == 409
    assert resp.json()["intake_id"] == first["intake_id"]


def test_start_intake_not_ready_is_409_with_reasons(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(
        tmp_path, monkeypatch, readiness=failing_readiness
    )
    resp = client.post(
        "/api/intakes", json={"repo_id": repo.name, "request": INTAKE_REQUEST}
    )
    assert resp.status_code == 409
    assert resp.json()["reasons"] == [
        "git_clean: working tree has uncommitted changes"
    ]
    assert agent.draft_calls == 0
    assert manager.list_intakes() == []  # a gated start creates no intake dir


def test_start_intake_empty_request_is_400(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    resp = client.post("/api/intakes", json={"repo_id": repo.name, "request": "   "})
    assert resp.status_code == 400
    assert agent.draft_calls == 0


def test_start_intake_missing_body_field_is_422(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    resp = client.post("/api/intakes", json={"repo_id": repo.name})  # no request
    assert resp.status_code == 422


def test_start_intake_agent_failure_is_201_then_failed_record(tmp_path, monkeypatch):
    # A drafting-turn failure is an auditable outcome, not a 5xx: the intake
    # resource is created (201, transient drafting) and settles to ``failed`` with
    # the repo untouched.
    client, manager, agent, repo = make_intake_client(
        tmp_path, monkeypatch, agent=FakeIntakeAgent(draft_error=RuntimeError("agent crashed"))
    )
    before = intake_task_files(repo)

    resp = client.post(
        "/api/intakes", json={"repo_id": repo.name, "request": INTAKE_REQUEST}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == STATUS_DRAFTING  # transient; the turn fails off-lock

    # The failed drafting turn settles to a ``failed`` record with its reason.
    settled = client.get(f"/api/intakes/{body['intake_id']}").json()
    assert settled["status"] == STATUS_FAILED
    assert "agent crashed" in settled["error"]
    assert intake_task_files(repo) == before


def test_start_intake_registry_error_returns_500(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)

    def boom(config):
        raise RegistryError("bad registry")

    monkeypatch.setattr(api, "discover_repos", boom)
    resp = client.post(
        "/api/intakes", json={"repo_id": repo.name, "request": INTAKE_REQUEST}
    )
    assert resp.status_code == 500


# -- get / list / history ------------------------------------------------------


def test_get_intake_returns_record_and_unknown_is_404(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    started = start_an_intake(client, repo)

    ok = client.get(f"/api/intakes/{started['intake_id']}")
    assert ok.status_code == 200
    assert ok.json()["intake_id"] == started["intake_id"]
    assert ok.json()["draft"]["task_id"] == INTAKE_TASK_ID

    assert client.get("/api/intakes/nope").status_code == 404


def test_list_intakes_returns_started_intake(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    started = start_an_intake(client, repo)

    resp = client.get("/api/intakes")
    assert resp.status_code == 200
    intakes = resp.json()["intakes"]
    assert [r["intake_id"] for r in intakes] == [started["intake_id"]]
    assert "agent_ref" not in intakes[0]


def test_intake_history_returns_record_and_events(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    started = start_an_intake(client, repo)
    client.post(
        f"/api/intakes/{started['intake_id']}/revise",
        json={"feedback": "tighten scope"},
    )

    resp = client.get(f"/api/intakes/{started['intake_id']}/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["intake_id"] == started["intake_id"]
    assert [e["kind"] for e in body["events"]] == [
        KIND_INTAKE_CREATED,
        KIND_INTAKE_REVISED,
    ]
    # Audit events never carry the unconfirmed draft body.
    assert all("task_file" not in e for e in body["events"])


def test_intake_history_unknown_is_404(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    assert client.get("/api/intakes/nope/history").status_code == 404


# -- revise --------------------------------------------------------------------


def test_revise_intake_updates_draft_without_writing(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    before = intake_task_files(repo)
    started = start_an_intake(client, repo)

    resp = client.post(
        f"/api/intakes/{started['intake_id']}/revise",
        json={"feedback": "Scope it to a single route"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == STATUS_REVISING  # transient; turn runs off-lock

    settled = client.get(f"/api/intakes/{started['intake_id']}").json()
    assert settled["status"] == STATUS_AWAITING_REVIEW
    assert settled["iteration"] == 1
    assert "iteration 1" in settled["draft"]["task_file"]
    assert agent.revise_calls == 1
    assert agent.confirm_calls == 0
    assert intake_task_files(repo) == before  # revision stays read-only


def test_revise_intake_empty_feedback_is_400(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    started = start_an_intake(client, repo)
    resp = client.post(
        f"/api/intakes/{started['intake_id']}/revise", json={"feedback": "  "}
    )
    assert resp.status_code == 400


def test_revise_intake_wrong_state_is_409(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    started = start_an_intake(client, repo)
    client.post(f"/api/intakes/{started['intake_id']}/confirm")  # → confirmed

    resp = client.post(
        f"/api/intakes/{started['intake_id']}/revise", json={"feedback": "again"}
    )
    assert resp.status_code == 409


def test_revise_intake_unknown_is_404(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    resp = client.post("/api/intakes/nope/revise", json={"feedback": "x"})
    assert resp.status_code == 404


# -- confirm -------------------------------------------------------------------


def test_confirm_intake_writes_task_and_refreshes_inventory(tmp_path, monkeypatch):
    # Confirmation is the only write. After it, the new pending task must be
    # visible through the *existing* task-inventory route (list_repo_tasks),
    # frontmatter-authoritative — intake does not duplicate task listing.
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    started = start_an_intake(client, repo)

    resp = client.post(f"/api/intakes/{started['intake_id']}/confirm")
    assert resp.status_code == 200
    assert resp.json()["status"] == STATUS_CONFIRMING  # transient; write runs off-lock

    settled = client.get(f"/api/intakes/{started['intake_id']}").json()
    assert settled["status"] == STATUS_CONFIRMED
    assert settled["created_task_id"] == INTAKE_TASK_ID
    assert agent.confirm_calls == 1

    # The confirmed write landed a real task file + index row; the existing
    # inventory route now surfaces it with its frontmatter status.
    listing = client.get(f"/api/repos/{repo.name}/tasks").json()
    tasks = listing["repos"][0]["tasks"]
    assert [t["task_id"] for t in tasks] == [INTAKE_TASK_ID]
    assert tasks[0]["status"] == "pending"

    detail = client.get(f"/api/repos/{repo.name}/tasks/{INTAKE_TASK_ID}").json()
    assert detail["present"] is True
    assert f"id: {INTAKE_TASK_ID}" in detail["content"]


def test_confirm_intake_already_confirmed_is_409(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    started = start_an_intake(client, repo)
    assert client.post(f"/api/intakes/{started['intake_id']}/confirm").status_code == 200

    again = client.post(f"/api/intakes/{started['intake_id']}/confirm")
    assert again.status_code == 409  # already confirmed


def test_confirm_intake_unknown_is_404(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    assert client.post("/api/intakes/nope/confirm").status_code == 404


def test_confirm_lost_session_is_failed_not_5xx(tmp_path, monkeypatch):
    # A hub restart drops the in-memory conversational handle. Confirming through
    # a fresh app over the same data dir must fail closed (a ``failed`` record,
    # 200) without writing — never a silent repo write and never a 5xx.
    client1, manager1, agent1, repo = make_intake_client(tmp_path, monkeypatch)
    before = intake_task_files(repo)
    started = start_an_intake(client1, repo)

    fresh_agent = FakeIntakeAgent()
    manager2, _ = make_intake_manager(tmp_path, agent=fresh_agent)
    client2 = TestClient(api.create_app(run_config(tmp_path), intake_manager=manager2))
    client2.headers["Authorization"] = f"Bearer {TOKEN}"

    resp = client2.post(f"/api/intakes/{started['intake_id']}/confirm")
    assert resp.status_code == 200
    assert resp.json()["status"] == STATUS_FAILED
    assert fresh_agent.confirm_calls == 0
    assert intake_task_files(repo) == before


def test_app_lifespan_reconciles_orphaned_intakes(tmp_path, monkeypatch):
    # A hub restart with an orphaned transient intake (a crash mid-turn) must free
    # the repo's intake slot on startup: the app lifespan calls reconcile_orphans.
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    started = start_an_intake(client, repo)  # settles awaiting_review

    # Force a persisted transient status: an orphan no thread will finalize.
    path = manager.intake_dir / started["intake_id"] / "intake.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = STATUS_DRAFTING
    path.write_text(json.dumps(data), encoding="utf-8")

    # Entering the app lifespan (startup) runs reconciliation over that manager.
    app = api.create_app(run_config(tmp_path), intake_manager=manager)
    with TestClient(app):
        pass
    assert manager.get_intake(started["intake_id"]).status == STATUS_FAILED


# -- cancel --------------------------------------------------------------------


def test_cancel_intake_leaves_repo_unchanged(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    before = intake_task_files(repo)
    started = start_an_intake(client, repo)

    resp = client.post(f"/api/intakes/{started['intake_id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == STATUS_CANCELLED
    assert agent.cancel_calls == 1
    assert agent.confirm_calls == 0
    assert intake_task_files(repo) == before


def test_cancel_intake_terminal_is_409(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    started = start_an_intake(client, repo)
    client.post(f"/api/intakes/{started['intake_id']}/confirm")  # terminal

    resp = client.post(f"/api/intakes/{started['intake_id']}/cancel")
    assert resp.status_code == 409


def test_cancel_intake_unknown_is_404(tmp_path, monkeypatch):
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    assert client.post("/api/intakes/nope/cancel").status_code == 404


def test_terminal_intake_frees_repo_for_a_new_one(tmp_path, monkeypatch):
    # A terminal intake releases the per-repo slot: a second start succeeds.
    client, manager, agent, repo = make_intake_client(tmp_path, monkeypatch)
    first = start_an_intake(client, repo)
    client.post(f"/api/intakes/{first['intake_id']}/cancel")

    second = client.post(
        "/api/intakes", json={"repo_id": repo.name, "request": "a fresh request"}
    )
    assert second.status_code == 201
    assert second.json()["intake_id"] != first["intake_id"]


# -- auth ----------------------------------------------------------------------


def test_intake_routes_require_auth(tmp_path, monkeypatch):
    # The default-deny middleware covers the intake routes with no per-route
    # exception: an unauthenticated caller is 401 on every one of them.
    manager, _ = make_intake_manager(tmp_path)
    unauthed = TestClient(api.create_app(run_config(tmp_path), intake_manager=manager))
    assert unauthed.post(
        "/api/intakes", json={"repo_id": "r", "request": "x"}
    ).status_code == 401
    assert unauthed.get("/api/intakes").status_code == 401
    assert unauthed.get("/api/intakes/anything").status_code == 401
    assert unauthed.get("/api/intakes/anything/history").status_code == 401
    assert unauthed.post(
        "/api/intakes/anything/revise", json={"feedback": "x"}
    ).status_code == 401
    assert unauthed.post("/api/intakes/anything/confirm").status_code == 401
    assert unauthed.post("/api/intakes/anything/cancel").status_code == 401


# --- repo admission: a live run and a remote intake never share a repo --------
#
# Both managers are wired to ONE RepoAdmission (as the app wires them) over one
# repo, so these drive the real cross-subsystem gate through HTTP. The manager
# and concurrency coverage lives in tests/test_admission.py; what is pinned here
# is the wire contract: 409 with the blocking id, and no side effect before it.


def make_admission_client(
    tmp_path, monkeypatch, *, repo: ManagedRepo | None = None
) -> tuple[TestClient, RunManager, IntakeManager, FakeSpawner, FakeIntakeAgent, ManagedRepo]:
    """One client whose run and intake managers share an admission coordinator.

    The repo carries ``RUN_TASK_ID`` so it is launchable, and an intake can still
    draft a new task into it. The app adopts the injected run manager's
    coordinator and hands it to the intake manager built alongside it — the same
    single-coordinator wiring the real service uses.
    """
    repo = repo if repo is not None else make_run_repo(tmp_path)
    run_manager, spawner = make_run_manager(tmp_path)
    agent = FakeIntakeAgent()
    intake_manager = IntakeManager(
        run_config(tmp_path),
        agent=agent,
        readiness_checker=passing_readiness,
        clock=lambda: FIXED_NOW,
        executor=intake_sync_executor,
        admission=run_manager.admission,
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    client = TestClient(
        api.create_app(
            run_config(tmp_path),
            run_manager=run_manager,
            intake_manager=intake_manager,
        )
    )
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client, run_manager, intake_manager, spawner, agent, repo


def test_start_intake_during_a_live_run_is_409_with_the_run_id(tmp_path, monkeypatch):
    client, _, intake_manager, _, agent, repo = make_admission_client(
        tmp_path, monkeypatch
    )
    run = start_a_run(client, repo)

    resp = client.post(
        "/api/intakes", json={"repo_id": repo.name, "request": INTAKE_REQUEST}
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["conflict"] == "run_live"
    assert body["run_id"] == run["run_id"]
    # An admission conflict is not a readiness failure and carries no reasons.
    assert "reasons" not in body
    # Refused before any intake record was created or agent turn invoked.
    assert client.get("/api/intakes").json() == {"intakes": []}
    assert agent.draft_calls == 0


def test_start_run_during_an_in_flight_intake_is_409_with_the_intake_id(
    tmp_path, monkeypatch
):
    client, _, _, spawner, _, repo = make_admission_client(tmp_path, monkeypatch)
    intake = start_an_intake(client, repo)

    resp = client.post(
        "/api/runs", json={"repo_id": repo.name, "task_id": RUN_TASK_ID}
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["conflict"] == "intake_in_flight"
    assert body["intake_id"] == intake["intake_id"]
    assert "reasons" not in body
    # Refused before any run record was created or process spawned.
    assert client.get("/api/runs").json() == {"runs": []}
    assert spawner.calls == []


def test_admission_conflicts_do_not_alter_the_readiness_payload(tmp_path, monkeypatch):
    """Admission and readiness stay separate concepts.

    A repo claimed by the other subsystem is not an unready repo: the readiness
    payload the dashboard badges off must read exactly as it did before the
    conflict, so the refusal cannot leak into READY/NOT READY.
    """
    client, _, _, _, _, repo = make_admission_client(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "check_readiness", passing_readiness)
    before = client.get("/api/repos").json()

    start_a_run(client, repo)
    conflict = client.post(
        "/api/intakes", json={"repo_id": repo.name, "request": INTAKE_REQUEST}
    )

    assert conflict.status_code == 409
    after = client.get("/api/repos").json()
    assert after == before
    assert after["repos"][0]["readiness"]["ready"] is True


def test_intake_is_admitted_once_the_run_ends(tmp_path, monkeypatch):
    client, _, _, spawner, _, repo = make_admission_client(tmp_path, monkeypatch)
    start_a_run(client, repo)
    spawner.procs[0].returncode = 0  # the run exits
    client.get("/api/runs")  # the scan persists its end

    resp = client.post(
        "/api/intakes", json={"repo_id": repo.name, "request": INTAKE_REQUEST}
    )

    assert resp.status_code == 201
    settled = client.get(f"/api/intakes/{resp.json()['intake_id']}").json()
    assert settled["status"] == STATUS_AWAITING_REVIEW


def test_run_is_admitted_once_the_intake_is_cancelled(tmp_path, monkeypatch):
    client, _, _, _, _, repo = make_admission_client(tmp_path, monkeypatch)
    intake = start_an_intake(client, repo)
    assert client.post(
        f"/api/intakes/{intake['intake_id']}/cancel"
    ).status_code == 200

    resp = client.post(
        "/api/runs", json={"repo_id": repo.name, "task_id": RUN_TASK_ID}
    )

    assert resp.status_code == 201
    assert resp.json()["state"] == "active"


def make_independent_manager_client(tmp_path, monkeypatch):
    """A client whose two managers were built *independently*.

    Neither was handed the other's coordinator, so each arrives carrying its own
    — the wiring ``create_app`` has to reconcile, and the shape any caller
    constructing both managers separately produces.
    """
    repo = make_run_repo(tmp_path)
    run_manager, spawner = make_run_manager(tmp_path)
    agent = FakeIntakeAgent()
    intake_manager = IntakeManager(
        run_config(tmp_path),
        agent=agent,
        readiness_checker=passing_readiness,
        clock=lambda: FIXED_NOW,
        executor=intake_sync_executor,
    )
    assert intake_manager.admission is not run_manager.admission
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    app = api.create_app(
        run_config(tmp_path), run_manager=run_manager, intake_manager=intake_manager
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client, app, run_manager, intake_manager, spawner, agent, repo


@pytest.mark.parametrize("admitted", ["run", "intake"])
def test_independently_injected_managers_share_one_coordinator(
    tmp_path, monkeypatch, admitted
):
    """The app must place BOTH managers on the coordinator it adopts.

    Adopting one manager's coordinator and leaving the other on its own gives
    each an unbound peer probe: every cross-kind check answers "nothing there"
    and the app admits a live run and a remote intake for the same repo, in
    either order — the exact overlap this feature exists to prevent.
    """
    client, app, run_manager, intake_manager, spawner, agent, repo = (
        make_independent_manager_client(tmp_path, monkeypatch)
    )

    assert run_manager.admission is app.state.repo_admission
    assert intake_manager.admission is app.state.repo_admission

    if admitted == "run":
        run = start_a_run(client, repo)
        resp = client.post(
            "/api/intakes", json={"repo_id": repo.name, "request": INTAKE_REQUEST}
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["conflict"] == "run_live"
        assert resp.json()["run_id"] == run["run_id"]
        assert client.get("/api/intakes").json() == {"intakes": []}
        assert agent.draft_calls == 0
    else:
        intake = start_an_intake(client, repo)
        resp = client.post(
            "/api/runs", json={"repo_id": repo.name, "task_id": RUN_TASK_ID}
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["conflict"] == "intake_in_flight"
        assert resp.json()["intake_id"] == intake["intake_id"]
        assert client.get("/api/runs").json() == {"runs": []}
        assert spawner.calls == []


def recovered_overlap(tmp_path, monkeypatch):
    """A repo carrying both an ``awaiting_review`` intake and a live run.

    Admission stops this pair from forming but cannot undo one that already
    exists — a legacy record, or a restart that reattaches a run while a
    persisted ``awaiting_review`` intake survives. Built here by launching
    through a run manager that is not admission-coupled, which is the state a
    pre-feature hub leaves on disk.
    """
    client, _, intake_manager, spawner, agent, repo = make_admission_client(
        tmp_path, monkeypatch
    )
    intake = start_an_intake(client, repo)
    assert client.get(f"/api/intakes/{intake['intake_id']}").json()["status"] == (
        STATUS_AWAITING_REVIEW
    )
    uncoupled = RunManager(
        run_config(tmp_path),
        readiness_checker=passing_readiness,
        spawner=spawner,
        pid_probe=lambda pid: True,
        clock=lambda: FIXED_NOW,
    )
    run = uncoupled.start_run(repo, RUN_TASK_ID)
    return client, intake["intake_id"], run.run_id, repo, agent


def test_recovered_overlap_refuses_revise_and_confirm_but_allows_cancel(
    tmp_path, monkeypatch
):
    client, intake_id, run_id, repo, agent = recovered_overlap(tmp_path, monkeypatch)
    before = intake_task_files(repo)

    revise = client.post(
        f"/api/intakes/{intake_id}/revise", json={"feedback": "tighten the scope"}
    )
    confirm = client.post(f"/api/intakes/{intake_id}/confirm")

    for resp in (revise, confirm):
        assert resp.status_code == 409, resp.text
        assert resp.json()["conflict"] == "run_live"
        assert resp.json()["run_id"] == run_id
    assert agent.revise_calls == 0
    assert agent.confirm_calls == 0
    assert intake_task_files(repo) == before  # no write raced the live run
    # Refusing is a guard, not a state change, and Cancel stays available so the
    # overlap can be cleared.
    assert client.get(f"/api/intakes/{intake_id}").json()["status"] == (
        STATUS_AWAITING_REVIEW
    )
    cancelled = client.post(f"/api/intakes/{intake_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == STATUS_CANCELLED
    assert intake_task_files(repo) == before


# --- task evaluation routes ---------------------------------------------------
#
# These drive the app's real EvaluationService + StaticEvaluator over a fake repo
# in ``tmp_path``, with an injected record loader, a fake git runner, and a fixed
# clock, so no real run history, subprocess, or git is touched. Generation writes
# only hub-owned artifacts under ``<data_dir>/evaluations``; the tests pin that
# the target repo/task is never mutated and that disabled mode fails closed.

EVAL_CLOCK = dt.datetime(2026, 7, 22, 9, 0, 0, tzinfo=dt.timezone.utc)


def eval_config(tmp_path: Path, *, enabled: bool = True) -> Config:
    return Config(
        mandrel_path=tmp_path / "mandrel",
        token=TOKEN,
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=enabled,
    )


def fake_eval_git(args, **kwargs) -> CommandResult:
    """A git runner that always fails, so the evidence git window is unavailable
    without a real subprocess (mirrors the evaluation-module tests)."""
    return CommandResult(tuple(str(a) for a in args), 128, "", "")


SEMANTIC_PAYLOAD = {
    "summary": "The task landed with a complete handoff.",
    "confidence": 0.8,
    "scores": [
        {"dimension": "handoff_completeness", "score": 0.9, "confidence": 0.8,
         "rationale": "session log complete"},
    ],
    "findings": [
        {"dimension": "rediscovery_risk", "severity": "low", "summary": "thin note",
         "confidence": 0.6, "evidence_refs": ["session-log entry sid-a"],
         "suggestion": "record more in .ai/"},
    ],
    "improvements": ["Add a decision-trace reminder to the dev contract."],
}


def semantic_envelope(payload: dict | None = None) -> str:
    body = json.dumps(SEMANTIC_PAYLOAD if payload is None else payload)
    return f"here is my judgment\n{SEMANTIC_START}\n{body}\n{SEMANTIC_END}\nend"


class FakeSemanticBackend:
    """A semantic backend returning canned envelope text (no ``claude`` launched)."""

    def __init__(self, text: str | None = None, *, available: bool = True):
        self._text = text if text is not None else semantic_envelope()
        self._available = available
        self.prompts: list[str] = []

    @property
    def info(self):
        from orch_hub.models import SemanticEvaluatorInfo

        return SemanticEvaluatorInfo(backend="fake", model="fake-model", schema_version="1")

    def evaluate(self, prompt: str, repo: ManagedRepo) -> str:
        self.prompts.append(prompt)
        return self._text

    def doctor(self):
        from orch_hub.models import SemanticDoctor

        return SemanticDoctor(
            backend="fake",
            model="fake-model",
            schema_version="1",
            available=self._available,
            detail="claude resolved at /fake/claude" if self._available else "not found",
        )


class RaisingSemanticBackend:
    """A semantic backend whose model turn fails (a process/model error)."""

    @property
    def info(self):
        from orch_hub.models import SemanticEvaluatorInfo

        return SemanticEvaluatorInfo(backend="fake", model="fake-model", schema_version="1")

    def evaluate(self, prompt: str, repo: ManagedRepo) -> str:
        raise SemanticEvaluationError("claude semantic evaluation turn failed (exit 1)")

    def doctor(self):
        from orch_hub.models import SemanticDoctor

        return SemanticDoctor(
            backend="fake", model="fake-model", schema_version="1", available=True
        )


def make_eval_client(
    tmp_path,
    monkeypatch,
    *,
    repo: ManagedRepo | None = None,
    enabled: bool = True,
    records=None,
    semantic_backend=None,
    semantic_job_executor=None,
) -> tuple[TestClient, EvaluationService, StaticEvaluator, SemanticEvaluator, ManagedRepo]:
    repo = repo if repo is not None else make_run_repo(tmp_path)
    config = eval_config(tmp_path, enabled=enabled)
    loader = lambda cfg: list(records or [])
    service = EvaluationService(
        config, record_loader=loader, git_runner=fake_eval_git, clock=lambda: EVAL_CLOCK
    )
    static = StaticEvaluator(
        config, evaluation_service=service, clock=lambda: EVAL_CLOCK
    )
    # Inject a fake semantic backend so the ``/semantic`` route never launches
    # ``claude``; it shares the same service + static evaluator as the other routes.
    semantic = SemanticEvaluator(
        config,
        evaluation_service=service,
        static_evaluator=static,
        backend=semantic_backend if semantic_backend is not None else FakeSemanticBackend(),
        clock=lambda: EVAL_CLOCK,
    )
    # The async semantic-job manager drives that same evaluator. Its executor
    # defaults to SYNCHRONOUS so a ``POST .../semantic/job`` settles the job before
    # the request returns — deterministic HTTP tests without a background thread; a
    # test that needs an in-flight (queued) job passes a deferred executor.
    sem_jobs = SemanticJobManager(
        config,
        evaluator=semantic,
        clock=lambda: EVAL_CLOCK,
        executor=semantic_job_executor if semantic_job_executor is not None else (lambda run: run()),
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    client = TestClient(
        api.create_app(
            config,
            evaluation_service=service,
            static_evaluator=static,
            semantic_evaluator=semantic,
            semantic_job_manager=sem_jobs,
        )
    )
    client.headers["Authorization"] = f"Bearer {TOKEN}"
    return client, service, static, semantic, repo


# -- availability --------------------------------------------------------------


def test_evaluation_availability_reports_enabled(tmp_path, monkeypatch):
    client, *_ = make_eval_client(tmp_path, monkeypatch)
    resp = client.get("/api/evaluation")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}


def test_evaluation_availability_reports_disabled(tmp_path, monkeypatch):
    # Availability is the one evaluation route that answers while disabled, so the
    # dashboard can hide controls rather than probe a 404.
    client, *_ = make_eval_client(tmp_path, monkeypatch, enabled=False)
    resp = client.get("/api/evaluation")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


# -- default backend wiring ----------------------------------------------------


def test_default_semantic_backend_gets_configured_model_and_effort(tmp_path, monkeypatch):
    # Every other evaluation test injects a fake evaluator, so this is the one place
    # the app's OWN Layer 2 backend is built: the configured tier
    # (ORCH_HUB_EVAL_MODEL / ORCH_HUB_EVAL_EFFORT, already resolved into Config) has
    # to reach it, or an operator's setting silently applies to nothing. Building the
    # app still launches no ``claude`` — only a semantic job would.
    seen: list[dict] = []

    class RecordingBackend(FakeSemanticBackend):
        def __init__(self, **kwargs):
            super().__init__()
            seen.append(kwargs)

    monkeypatch.setattr(api, "ClaudeSemanticBackend", RecordingBackend)

    api.create_app(eval_config(tmp_path))
    assert (seen[-1]["model"], seen[-1]["effort"]) == (DEFAULT_EVAL_MODEL, DEFAULT_EVAL_EFFORT)

    api.create_app(replace(eval_config(tmp_path), eval_model="claude-x", eval_effort="low"))
    assert (seen[-1]["model"], seen[-1]["effort"]) == ("claude-x", "low")


def test_default_intake_agent_gets_configured_model_and_effort(tmp_path, monkeypatch):
    # Same argument one layer over: every other intake test injects a fake agent, so
    # this is the one place the app's OWN ``/intake-task`` backend is built and the
    # configured tier (ORCH_HUB_INTAKE_MODEL / ORCH_HUB_INTAKE_EFFORT, resolved into
    # Config) has to reach it. ``eval_config`` is reused only for its token and
    # isolated data dir — intake is not behind the evaluation gate. Building the app
    # still launches no ``claude``; only an intake turn would.
    seen: list[dict] = []

    class RecordingAgent(FakeIntakeAgent):
        def __init__(self, **kwargs):
            super().__init__()
            seen.append(kwargs)

    monkeypatch.setattr(api, "ClaudeIntakeAgent", RecordingAgent)

    api.create_app(eval_config(tmp_path))
    assert (seen[-1]["model"], seen[-1]["effort"]) == (
        DEFAULT_INTAKE_MODEL,
        DEFAULT_INTAKE_EFFORT,
    )

    api.create_app(
        replace(eval_config(tmp_path), intake_model="claude-x", intake_effort="low")
    )
    assert (seen[-1]["model"], seen[-1]["effort"]) == ("claude-x", "low")


# -- doctor (Layer 2 readiness probe) ------------------------------------------


def test_evaluation_doctor_reports_available_backend(tmp_path, monkeypatch):
    # The doctor is a config/availability probe: enabled + a resolvable backend →
    # 200 with the semantic readiness, no model call, no run-control impact.
    client, *_ = make_eval_client(
        tmp_path, monkeypatch, semantic_backend=FakeSemanticBackend(available=True)
    )
    resp = client.get("/api/evaluation/doctor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["semantic"]["available"] is True
    assert body["semantic"]["backend"] == "fake"
    assert body["semantic"]["model"] == "fake-model"
    assert body["semantic"]["schema_version"] == "1"


def test_evaluation_doctor_reports_unconfigured_backend(tmp_path, monkeypatch):
    # An enabled-but-unconfigured backend is a 200 with available:false (NOT a
    # failure) so the dashboard can disable the Layer 2 control while keeping
    # Layer 1 usable, rather than the session-1 502-on-click.
    client, *_ = make_eval_client(
        tmp_path, monkeypatch, semantic_backend=FakeSemanticBackend(available=False)
    )
    resp = client.get("/api/evaluation/doctor")
    assert resp.status_code == 200
    assert resp.json()["semantic"]["available"] is False


def test_evaluation_doctor_disabled_is_404(tmp_path, monkeypatch):
    # Enabled-gated like every action route: a disabled hub fails closed at 404.
    client, *_ = make_eval_client(tmp_path, monkeypatch, enabled=False)
    resp = client.get("/api/evaluation/doctor")
    assert resp.status_code == 404


def test_evaluation_doctor_requires_auth(tmp_path, monkeypatch):
    client, *_ = make_eval_client(tmp_path, monkeypatch)
    del client.headers["Authorization"]
    resp = client.get("/api/evaluation/doctor")
    assert resp.status_code == 401


# -- generate / list / fetch ---------------------------------------------------


def test_list_evaluations_empty_for_never_evaluated_active_task(tmp_path, monkeypatch):
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    resp = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["evaluations"] == []
    assert body["latest_evaluation_id"] is None
    # The target resolves to the active task file even with no evaluations yet.
    assert body["target"] == {"present": True, "source": "active"}
    assert body["enabled"] is True


def test_generate_metrics_then_list_and_fetch(tmp_path, monkeypatch):
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)

    gen = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/metrics")
    assert gen.status_code == 201
    eval_id = gen.json()["evaluation_id"]
    report = gen.json()["static_metrics"]
    assert report["evaluation_id"] == eval_id
    assert report["task_id"] == RUN_TASK_ID
    assert report["target_source"] == "active"
    # The deterministic score axes are present in the report.
    assert {s["name"] for s in report["scores"]} == {
        "protocol", "reliability", "handoff", "estimate", "knowledge",
        "prefetch_set_read_completeness",
    }

    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    assert listing["latest_evaluation_id"] == eval_id
    assert listing["evaluations"] == [
        {
            "evaluation_id": eval_id,
            "has_evidence": True,
            "has_static_metrics": True,
            "has_semantic_report": False,
            # No semantic report → no evaluator identity to compare in the picker.
            "evaluator": None,
        }
    ]

    detail = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/{eval_id}"
    ).json()
    assert detail["evaluation_id"] == eval_id
    assert detail["static_metrics"] == report
    # Evidence is generated alongside metrics and readable for local inspection.
    assert detail["evidence"]["evaluation_id"] == eval_id
    assert detail["evidence"]["summary"]["run_count"] == 0
    # A deterministic metrics run has no semantic layer.
    assert detail["semantic_report"] is None and detail["report_md"] is None


def test_generate_evidence_only_has_no_static_metrics(tmp_path, monkeypatch):
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)

    gen = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/evidence")
    assert gen.status_code == 201
    eval_id = gen.json()["evaluation_id"]
    assert gen.json()["evidence"]["evaluation_id"] == eval_id

    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    assert listing["evaluations"] == [
        {
            "evaluation_id": eval_id,
            "has_evidence": True,
            "has_static_metrics": False,
            "has_semantic_report": False,
            "evaluator": None,
        }
    ]

    detail = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/{eval_id}"
    ).json()
    assert detail["evidence"]["evaluation_id"] == eval_id
    assert detail["static_metrics"] is None  # evidence-only: no report artifact
    assert detail["semantic_report"] is None and detail["report_md"] is None


@pytest.mark.parametrize("artifact", ["evidence.json", "report.md"])
def test_detail_route_serves_nothing_through_a_linked_artifact(
    tmp_path, monkeypatch, artifact
):
    # The dashboard sidecar is the second way a stored artifact becomes bytes a
    # client sees, and it reads both a JSON slot and the Markdown one. A link
    # planted where an artifact was published must not be followed — the target
    # repo's own files sit one link away — and must not read as the ``null`` that
    # means "this layer was never generated" either.
    client, service, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    eval_id = client.post(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/evidence"
    ).json()["evaluation_id"]
    eval_dir = service.artifact_dir(repo, RUN_TASK_ID, eval_id)
    outside = tmp_path / "outside.txt"
    outside.write_text("target repo content", encoding="utf-8")
    (eval_dir / artifact).unlink(missing_ok=True)
    (eval_dir / artifact).symlink_to(outside)

    resp = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/{eval_id}")

    assert resp.status_code == 500
    assert "damaged" in resp.json()["detail"]
    assert "target repo content" not in resp.text


def test_detail_route_refuses_a_linked_evaluation_directory(tmp_path, monkeypatch):
    # The hop the artifact rule cannot see: a linked evaluation *dir* holds regular
    # files, so a no-follow open of the artifact alone would hand back another
    # tree's report under this task's id.
    client, service, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    eval_id = client.post(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/evidence"
    ).json()["evaluation_id"]
    eval_dir = service.artifact_dir(repo, RUN_TASK_ID, eval_id)
    outside = tmp_path / "outside-eval"
    outside.mkdir()
    (outside / "evidence.json").write_text('{"outside": true}', encoding="utf-8")
    shutil.rmtree(eval_dir)
    eval_dir.symlink_to(outside, target_is_directory=True)

    detail = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/{eval_id}"
    )
    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}")

    # Not an evaluation at all as far as the listing is concerned, and the detail
    # route 404s on that rather than serving the outside dir's pack.
    assert listing.status_code == 200 and listing.json()["evaluations"] == []
    assert detail.status_code == 404
    assert "outside" not in detail.text


def test_generate_metrics_refresh_creates_second_evaluation(tmp_path, monkeypatch):
    # Re-running is a refresh: a same-second collision bumps a ``-N`` suffix, and
    # the list is newest-first without ever mutating task/run state.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    first = client.post(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/metrics"
    ).json()["evaluation_id"]
    second = client.post(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/metrics"
    ).json()["evaluation_id"]

    assert first != second
    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    ids = [e["evaluation_id"] for e in listing["evaluations"]]
    assert ids == sorted(ids, reverse=True)  # newest-first
    assert set(ids) == {first, second}
    assert listing["latest_evaluation_id"] == ids[0]


def test_evaluation_of_archived_task(tmp_path, monkeypatch):
    # A completed task dropped from the active index at close-out is still
    # evaluable through the active-then-archive target resolver.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    archived = f"---\nid: {RUN_TASK_ID}\nstatus: completed\n---\n\n# archived body\n"
    archive_run_task(repo, RUN_TASK_ID, archived)

    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    assert listing["target"] == {"present": True, "source": "archived"}

    gen = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/metrics")
    assert gen.status_code == 201
    assert gen.json()["static_metrics"]["target_source"] == "archived"


def test_unknown_task_evaluates_as_absent_target(tmp_path, monkeypatch):
    # A task id with no active or archived file: not an error — it is evaluable
    # for its run history alone, with an absent target.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    ghost = "2026-01-01-no-such-task"

    listing = client.get(f"/api/repos/{repo.name}/evaluations/{ghost}").json()
    assert listing["target"] == {"present": False, "source": None}
    assert listing["evaluations"] == []

    gen = client.post(f"/api/repos/{repo.name}/evaluations/{ghost}/metrics")
    assert gen.status_code == 201
    assert gen.json()["static_metrics"]["target_present"] is False


# -- semantic (Layer 2) --------------------------------------------------------


def test_completed_semantic_job_lists_and_reads_back_the_full_chain(tmp_path, monkeypatch):
    # The list + detail routes read a persisted semantic evaluation back the same
    # way regardless of how it was minted. The synchronous /semantic route is
    # retired, so the evaluation is generated through the async job (the default
    # test executor settles it before the POST returns); this pins the sidecar
    # readback of the full evidence→static→semantic chain that route used to cover.
    # Backend-failure, schema-violation, 404, and read-only coverage now live in the
    # async-job tests below.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)

    client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job")
    job = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job"
    ).json()["job"]
    assert job["status"] == "completed"
    eval_id = job["evaluation_id"]

    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    assert listing["latest_evaluation_id"] == eval_id
    assert listing["evaluations"] == [
        {
            "evaluation_id": eval_id,
            "has_evidence": True,
            "has_static_metrics": True,
            "has_semantic_report": True,
            # The evaluator identity is read back best-effort from the persisted
            # semantic report so the history picker can compare runs by model.
            "evaluator": {
                "backend": "fake",
                "model": "fake-model",
                "schema_version": "1",
            },
        }
    ]

    detail = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/{eval_id}"
    ).json()
    report = detail["semantic_report"]
    assert report["evaluation_id"] == eval_id
    assert report["task_id"] == RUN_TASK_ID
    assert report["evaluator"]["backend"] == "fake"
    assert report["scores"] and report["findings"]
    # The chain is evidence→static→semantic: static metrics were fed as input.
    assert report["input_static_metrics"] is True
    # All three JSON layers plus the rendered Markdown export are readable.
    assert detail["evidence"]["evaluation_id"] == eval_id
    assert detail["static_metrics"]["evaluation_id"] == eval_id
    assert detail["report_md"].startswith("# Semantic Task Evaluation")
    assert "handoff_completeness" in detail["report_md"]


def test_list_surfaces_semantic_evaluator_for_picker_comparison(tmp_path, monkeypatch):
    # The list route reads the persisted semantic report's evaluator best-effort so
    # the history picker can compare runs by model/version without opening each one.
    client, service, _static, _sem, repo = make_eval_client(
        tmp_path, monkeypatch, semantic_backend=FakeSemanticBackend(available=True)
    )
    # Generate via the async job (settled synchronously by the default test
    # executor); the completed job carries the fresh evaluation_id.
    client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job")
    eval_id = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job"
    ).json()["job"]["evaluation_id"]

    entry = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}"
    ).json()["evaluations"][0]
    assert entry["evaluation_id"] == eval_id
    assert entry["has_semantic_report"] is True
    assert entry["evaluator"] == {
        "backend": "fake",
        "model": "fake-model",
        "schema_version": "1",
    }


def test_list_evaluator_degrades_to_none_when_semantic_report_unreadable(
    tmp_path, monkeypatch
):
    # The evaluator read is best-effort presentation enrichment, so a corrupt or
    # unreadable semantic report must never 500 the newest-first list (its primary
    # payload) — it degrades to a null evaluator while the artifact flag still
    # reports the report is present. (The detail route is where corruption surfaces.)
    client, service, _static, _sem, repo = make_eval_client(
        tmp_path, monkeypatch, semantic_backend=FakeSemanticBackend(available=True)
    )
    client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job")
    eval_id = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job"
    ).json()["job"]["evaluation_id"]
    report_path = (
        service.artifact_dir(repo, RUN_TASK_ID, eval_id) / SEMANTIC_REPORT_NAME
    )
    report_path.write_text("{ not valid json", encoding="utf-8")

    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}")
    assert listing.status_code == 200  # the corrupt report did not 500 the list
    entry = listing.json()["evaluations"][0]
    assert entry["has_semantic_report"] is True  # the file is still present
    assert entry["evaluator"] is None  # but its identity degraded cleanly


# -- async semantic-evaluation jobs --------------------------------------------


class DeferredJobExecutor:
    """Captures submitted job turns without running them (job stays ``queued``)."""

    def __init__(self):
        self.pending: list = []

    def __call__(self, run) -> None:
        self.pending.append(run)

    def run_all(self) -> None:
        while self.pending:
            self.pending.pop(0)()


def test_start_semantic_job_runs_to_completion(tmp_path, monkeypatch):
    # POST starts an async job; the synchronous test executor settles it before the
    # response, so the returned record is the transient queued state and a GET reads
    # the completed job linking the freshly written evaluation.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)

    started = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job")
    assert started.status_code == 201
    body = started.json()["job"]  # same {"job": ...} envelope as the poll route
    assert body["status"] == "queued"
    assert body["task_id"] == RUN_TASK_ID
    # The evaluator identity is stamped so the UI can name the model while running.
    assert body["backend"] == "fake" and body["model"] == "fake-model"

    got = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job")
    assert got.status_code == 200
    job = got.json()["job"]
    assert job["status"] == "completed"
    assert job["evaluation_id"]
    # The linked evaluation is a real one, listed with a semantic report.
    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    assert listing["latest_evaluation_id"] == job["evaluation_id"]
    assert listing["evaluations"][0]["has_semantic_report"] is True


def test_get_semantic_job_is_null_for_never_evaluated_task(tmp_path, monkeypatch):
    # A task with no async job yet is the normal {"job": null} case, not a 404.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    resp = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job")
    assert resp.status_code == 200
    assert resp.json() == {"job": None}


def test_semantic_job_failure_is_persisted_failed_not_502(tmp_path, monkeypatch):
    # Unlike the synchronous /semantic route (502 on backend failure), the async
    # job absorbs the failure into a persisted, reload-survivable failed record with
    # a bounded diagnostic — the start POST still succeeds (the job exists).
    client, *_ , repo = make_eval_client(
        tmp_path, monkeypatch, semantic_backend=RaisingSemanticBackend()
    )

    started = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job")
    assert started.status_code == 201

    job = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job"
    ).json()["job"]
    assert job["status"] == "failed"
    assert job["evaluation_id"] is None
    assert "failed" in (job["error"] or "")
    # Atomic cleanup: the failed turn left no half-written evaluation behind.
    assert client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()["evaluations"] == []
    # The retry path is just another start over the terminal job (still 201).
    assert client.post(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job"
    ).status_code == 201


def test_semantic_job_duplicate_in_flight_returns_same_job(tmp_path, monkeypatch):
    # With a deferred executor the first job stays queued, so a second POST dedups
    # to the same job and launches no second evaluation turn.
    executor = DeferredJobExecutor()
    client, *_ , repo = make_eval_client(
        tmp_path, monkeypatch, semantic_job_executor=executor
    )

    first = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job").json()["job"]
    second = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job").json()["job"]
    assert first["job_id"] == second["job_id"]
    assert first["status"] == second["status"] == "queued"
    assert len(executor.pending) == 1  # only one turn was ever submitted

    executor.run_all()
    job = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job"
    ).json()["job"]
    assert job["status"] == "completed"
    # Exactly one evaluation was generated for the two POSTs (no duplicate model call).
    assert len(client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()["evaluations"]) == 1


def test_start_semantic_job_does_not_mutate_target_repo(tmp_path, monkeypatch):
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    task_file = repo.path / ".ai-tasks" / f"{RUN_TASK_ID}.md"
    before = task_file.read_text(encoding="utf-8")
    before_listing = sorted(p.name for p in (repo.path / ".ai-tasks").iterdir())

    client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job")

    assert task_file.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in (repo.path / ".ai-tasks").iterdir()) == before_listing


def test_start_semantic_job_unknown_repo_is_404(tmp_path, monkeypatch):
    client, *_ = make_eval_client(tmp_path, monkeypatch)
    resp = client.post(f"/api/repos/ghost/evaluations/{RUN_TASK_ID}/semantic/job")
    assert resp.status_code == 404


def test_get_semantic_job_unknown_repo_is_404(tmp_path, monkeypatch):
    client, *_ = make_eval_client(tmp_path, monkeypatch)
    resp = client.get(f"/api/repos/ghost/evaluations/{RUN_TASK_ID}/semantic/job")
    assert resp.status_code == 404


def test_semantic_job_routes_disabled_are_404(tmp_path, monkeypatch):
    # Enabled-gated like every action route: a disabled hub fails both closed.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch, enabled=False)
    assert client.post(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job"
    ).status_code == 404
    assert client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job"
    ).status_code == 404


def test_app_lifespan_reconciles_orphaned_semantic_jobs(tmp_path, monkeypatch):
    # A hub restart with an orphaned transient semantic job (a crash mid-turn) must
    # free the (repo, task) slot on startup: the app lifespan calls reconcile_orphans
    # over the semantic-job manager, exactly as it does for intakes.
    config = eval_config(tmp_path)
    repo = make_run_repo(tmp_path)
    mgr = SemanticJobManager(
        config,
        evaluator=SemanticEvaluator(config, backend=FakeSemanticBackend()),
        clock=lambda: EVAL_CLOCK,
    )
    job_dir = config.data_dir / "semantic-jobs" / repo.name / RUN_TASK_ID
    job_dir.mkdir(parents=True)
    (job_dir / SEMANTIC_JOB_NAME).write_text(
        json.dumps({
            "job_id": "J", "repo_name": repo.name, "repo_path": str(repo.path),
            "task_id": RUN_TASK_ID, "status": "running",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])

    with TestClient(api.create_app(config, semantic_job_manager=mgr)):  # runs startup
        pass

    assert mgr.get_job(repo, RUN_TASK_ID).status == "failed"


def write_complete_evaluation(
    config: Config, repo: ManagedRepo, evaluation_id: str, *, protocol: dict | None = None
) -> Path:
    """Write one complete L1+L2 evaluation dir straight to the data dir."""
    eval_dir = (
        config.data_dir / "evaluations" / repo.name / RUN_TASK_ID / evaluation_id
    )
    eval_dir.mkdir(parents=True)
    evidence = {
        "target": {"repo_path": str(repo.path), "source": SOURCE_ARCHIVED},
        "runs": [],
        "git": {"available": False, "commits": []},
    }
    if protocol is not None:
        evidence["protocol"] = protocol
    (eval_dir / EVIDENCE_NAME).write_text(json.dumps(evidence), encoding="utf-8")
    (eval_dir / STATIC_METRICS_NAME).write_text("{}", encoding="utf-8")
    (eval_dir / SEMANTIC_REPORT_NAME).write_text(
        json.dumps({
            "evaluation_id": evaluation_id, "repo_name": repo.name,
            "task_id": RUN_TASK_ID, "target_source": SOURCE_ARCHIVED,
            "generated_ts": "2026-07-22T09:00:00+00:00",
            "evaluator": {"backend": "fake", "model": "m", "schema_version": "1"},
        }),
        encoding="utf-8",
    )
    (eval_dir / SEMANTIC_REPORT_MD_NAME).write_text("# report\n", encoding="utf-8")
    return eval_dir


def test_app_lifespan_backfills_the_global_report_catalog(tmp_path, monkeypatch):
    # A complete report that predates the catalog (or whose live publication hook
    # failed) must reach the cross-repo feed at startup, so a consumer never has to
    # re-trigger an evaluation to get an existing report published.
    config = eval_config(tmp_path)
    repo = make_run_repo(tmp_path)
    write_complete_evaluation(config, repo, "20260722T090000Z")
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])

    with TestClient(api.create_app(config)):  # runs startup
        pass

    entries = ReportCatalog(config).entries()
    assert [(e.seq, e.evaluation_id) for e in entries] == [(1, "20260722T090000Z")]
    assert entries[0].archived is True

    # A second startup is idempotent: no duplicate identity, no renumbering.
    with TestClient(api.create_app(config)):
        pass
    assert [e.seq for e in ReportCatalog(config).entries()] == [1]


def test_app_lifespan_starts_with_evaluation_disabled_and_publishes_nothing(
    tmp_path, monkeypatch
):
    # The catalog shares the evaluation gate, and startup backfill is isolated:
    # a disabled hub must still serve run control, not fail to boot.
    config = eval_config(tmp_path, enabled=False)
    repo = make_run_repo(tmp_path)
    write_complete_evaluation(config, repo, "20260722T090000Z")
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])

    with TestClient(api.create_app(config)) as client:
        assert client.get("/api/health").status_code == 200

    assert not (config.data_dir / "report-catalog").exists()


# -- global report feed --------------------------------------------------------


def make_feed_client(
    tmp_path,
    monkeypatch,
    *,
    enabled: bool = True,
    evaluation_ids=(),
    publish=True,
    protocol: dict | None = None,
):
    """An eval client whose catalog already holds ``evaluation_ids`` as reports.

    Publication is driven explicitly rather than through the lifespan, so a test
    can also set up a complete-but-unpublished report and prove a *read* route
    never publishes it.
    """
    config = eval_config(tmp_path, enabled=enabled)
    repo = make_run_repo(tmp_path)
    for evaluation_id in evaluation_ids:
        write_complete_evaluation(config, repo, evaluation_id, protocol=protocol)
    if publish and enabled:
        ReportCatalog(config).reconcile()
    client, *_ = make_eval_client(tmp_path, monkeypatch, repo=repo, enabled=enabled)
    return client, config, repo


def test_report_feed_serializes_global_identity_hashes_and_provenance(
    tmp_path, monkeypatch
):
    # One feed item must carry everything a consumer needs to cohort and verify a
    # report without opening the per-task routes or the artifact tree.
    client, _config, repo = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=("20260722T090000Z",)
    )

    resp = client.get("/api/evaluation/reports")

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert (body["cursor"], body["next_cursor"], body["has_more"]) == (0, 1, False)
    item = body["reports"][0]
    assert item["seq"] == 1
    assert item["report_key"].startswith("rpt-")
    # Source identity stays exactly the per-task surface's, so the two agree.
    assert (item["repo_slug"], item["task_id"], item["evaluation_id"]) == (
        repo.name,
        RUN_TASK_ID,
        "20260722T090000Z",
    )
    assert item["archived"] is True and item["target_source"] == SOURCE_ARCHIVED
    assert item["evaluator"] == {
        "backend": "fake", "model": "m", "schema_version": "1"
    }
    assert {a["name"] for a in item["artifacts"]} == set(REQUIRED_ARTIFACTS)
    assert all(len(a["sha256"]) == 64 for a in item["artifacts"])
    # Provenance the evidence pack cannot carry is explicitly unavailable *with a
    # reason*, never an empty value a consumer could read as "no protocol". This
    # pack predates per-run protocol capture, so it carries no revision at all.
    assert item["provenance"]["protocol"]["available"] is False
    assert item["provenance"]["protocol"]["effective_revision"] is None
    assert item["provenance"]["protocol"]["deploy_lock_hash"] is None
    assert item["provenance"]["protocol"]["detail"]
    assert item["provenance"]["orchestrator_config"]["detail"]


def test_report_feed_serializes_the_effective_protocol_identity(tmp_path, monkeypatch):
    # The cohort key the feed exists to hand an external consumer: the deployed
    # mandrel commit this report's runs verifiably ran under, with
    # the deploy lock's digest of the payload that commit deployed, taken from the
    # durable evidence pack and never from the repo's current deployment.
    revision = "6b62754694c289fc8822562cec22ab774e071b2c"
    payload = "5" * 64
    client, _config, _repo = make_feed_client(
        tmp_path,
        monkeypatch,
        evaluation_ids=("20260722T090000Z",),
        protocol={
            "available": True,
            "effective_revision": revision,
            "deploy_lock_hash": payload,
            "run_count": 2,
            "verified_count": 2,
            "detail": None,
        },
    )

    item = client.get("/api/evaluation/reports").json()["reports"][0]

    assert item["provenance"]["protocol"] == {
        "available": True,
        "effective_revision": revision,
        "deploy_lock_hash": payload,
        "detail": None,
    }

    # The by-key bundle resolves to the same entry, so a consumer that stored the
    # key re-reads the same cohort membership it imported.
    bundle = client.get(f"/api/evaluation/reports/{item['report_key']}").json()
    protocol = bundle["entry"]["provenance"]["protocol"]
    assert (protocol["effective_revision"], protocol["deploy_lock_hash"]) == (
        revision,
        payload,
    )


def test_report_feed_serves_no_half_identity_for_a_pre_pair_pack(
    tmp_path, monkeypatch
):
    # Every report published before the identity became a pair: a revision whose
    # payload nothing verified. The feed says unavailable with the reason rather
    # than serving the revision alone, which a consumer would cohort by exactly as
    # if the hub had confirmed the bytes behind it.
    client, _config, _repo = make_feed_client(
        tmp_path,
        monkeypatch,
        evaluation_ids=("20260722T090000Z",),
        protocol={
            "available": True,
            "effective_revision": "6b62754694c289fc8822562cec22ab774e071b2c",
            "run_count": 2,
            "verified_count": 2,
            "detail": None,
        },
    )

    item = client.get("/api/evaluation/reports").json()["reports"][0]

    assert item["provenance"]["protocol"]["available"] is False
    assert item["provenance"]["protocol"]["effective_revision"] is None
    assert item["provenance"]["protocol"]["deploy_lock_hash"] is None
    assert item["provenance"]["protocol"]["detail"] == LEGACY_AGGREGATE_DETAIL


def test_report_feed_paging_reaches_every_report_exactly_once(tmp_path, monkeypatch):
    client, *_ = make_feed_client(
        tmp_path,
        monkeypatch,
        evaluation_ids=("20260722T090000Z", "20260722T100000Z", "20260722T110000Z"),
    )

    seen: list[int] = []
    params = {"limit": 2}
    while True:
        body = client.get("/api/evaluation/reports", params=params).json()
        seen.extend(item["seq"] for item in body["reports"])
        if not body["has_more"]:
            break
        params = {"limit": 2, "after": body["next_cursor"]}

    assert seen == [1, 2, 3]


def test_report_feed_stale_cursor_is_an_empty_page_not_an_error(tmp_path, monkeypatch):
    # A caught-up (or over-run) consumer keeps polling from the same watermark
    # rather than being handed an error or silently restarted at the beginning.
    client, *_ = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=("20260722T090000Z",)
    )

    body = client.get("/api/evaluation/reports", params={"after": 9999}).json()

    assert body["reports"] == []
    assert (body["next_cursor"], body["has_more"]) == (9999, False)


@pytest.mark.parametrize(
    "params",
    [
        {"after": "not-a-number"},
        {"after": "1.5"},
        {"after": -1},
        {"limit": 0},
        {"limit": 100000},
    ],
)
def test_report_feed_rejects_an_invalid_cursor_or_limit(tmp_path, monkeypatch, params):
    # One documented policy, one status code: a malformed watermark fails rather
    # than silently resetting the feed in either direction.
    client, *_ = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=("20260722T090000Z",)
    )

    resp = client.get("/api/evaluation/reports", params=params)

    assert resp.status_code == 400


def test_report_feed_reads_never_publish_or_evaluate(tmp_path, monkeypatch):
    # Listing is strictly a read: a complete but unpublished report stays
    # unpublished, no catalog is created, and nothing triggers an evaluation.
    client, config, repo = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=("20260722T090000Z",), publish=False
    )

    body = client.get("/api/evaluation/reports").json()

    assert body["reports"] == []
    assert not (config.data_dir / "report-catalog").exists()
    # And the per-task surface still shows the evaluation that was already there,
    # with no extra evaluation dir minted by the feed read.
    listed = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    assert [e["evaluation_id"] for e in listed["evaluations"]] == ["20260722T090000Z"]


def test_report_bundle_fetches_a_whole_report_by_global_key(tmp_path, monkeypatch):
    # The point of the key: a consumer that stored it fetches the artifacts
    # without knowing the repo/task/evaluation directory layout.
    client, *_ = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=("20260722T090000Z",)
    )
    key = client.get("/api/evaluation/reports").json()["reports"][0]["report_key"]

    resp = client.get(f"/api/evaluation/reports/{key}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["report_key"] == key
    assert body["available"] is True and body["detail"] is None
    assert body["entry"]["seq"] == 1
    artifacts = body["artifacts"]
    assert artifacts["semantic_report"]["evaluator"]["model"] == "m"
    assert artifacts["static_metrics"] == {}
    assert artifacts["evidence"]["target"]["source"] == SOURCE_ARCHIVED
    assert artifacts["report_md"] == "# report\n"
    # Verified means every served artifact was compared against a recorded digest
    # and matched — not merely that nothing contradicted one.
    assert body["integrity"] == {
        "verified": True,
        "mismatched": [],
        "unverified": [],
    }


def test_report_bundle_unknown_key_is_404(tmp_path, monkeypatch):
    client, *_ = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=("20260722T090000Z",)
    )

    resp = client.get("/api/evaluation/reports/rpt-0123456789abcdef0123456789abcdef")

    assert resp.status_code == 404
    assert "rpt-0123456789abcdef" in resp.json()["detail"]


def test_report_bundle_of_pruned_artifacts_is_unavailable_not_404(
    tmp_path, monkeypatch
):
    # The entry outlives its artifacts by design, so this is a published report
    # whose bundle cannot be served — deliberately distinct from "no such report".
    import shutil

    client, config, repo = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=("20260722T090000Z",)
    )
    key = client.get("/api/evaluation/reports").json()["reports"][0]["report_key"]
    shutil.rmtree(config.data_dir / "evaluations" / repo.name / RUN_TASK_ID)

    resp = client.get(f"/api/evaluation/reports/{key}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "no longer retained" in body["detail"]
    assert body["artifacts"]["report_md"] is None
    # Still auditable: the entry and its publication-time hashes are intact.
    assert len(body["entry"]["artifacts"]) == len(REQUIRED_ARTIFACTS)
    assert client.get("/api/evaluation/reports").json()["reports"][0]["seq"] == 1
    # Export is a pure read: a missing bundle is never healed by re-evaluating
    # the task, so no artifact dir reappears and no model call can be provoked.
    assert not (config.data_dir / "evaluations" / repo.name / RUN_TASK_ID).exists()


def test_report_bundle_of_a_symlinked_artifact_serves_no_outside_bytes(
    tmp_path, monkeypatch
):
    # The route's containment promise is about the bytes it hands back, not only
    # the path it builds: the four names are fixed, but the files standing under
    # them are not, so an artifact replaced after publication by a link out of the
    # evaluations tree must reach no consumer — neither as text in report_md nor
    # as parseable outside JSON in the three parsed slots.
    client, config, repo = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=("20260722T090000Z",)
    )
    item = client.get("/api/evaluation/reports").json()["reports"][0]
    eval_dir = (
        config.data_dir / "evaluations" / repo.name / RUN_TASK_ID / "20260722T090000Z"
    )
    outside = config.data_dir.parent / "outside-the-tree.json"
    outside.write_text('{"secret": "private content"}', encoding="utf-8")
    link = eval_dir / EVIDENCE_NAME
    link.unlink()
    link.symlink_to(outside)

    resp = client.get(f"/api/evaluation/reports/{item['report_key']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert b"private content" not in resp.content
    assert body["artifacts"] == {
        "evidence": None,
        "static_metrics": None,
        "semantic_report": None,
        "report_md": None,
    }
    # A damaged tree, deliberately distinguishable from the pruned and partially
    # pruned sets the same envelope reports: retention is this feed's designed end
    # state, and dressing tampering up as it would hide the defect.
    assert "damaged" in body["detail"] and "symbolic link" in body["detail"]
    assert "no longer retained" not in body["detail"]
    # Still auditable, and the per-artifact route keeps answering per artifact.
    assert len(body["entry"]["artifacts"]) == len(REQUIRED_ARTIFACTS)
    assert (
        client.get(artifact_url(item["report_key"], SEMANTIC_REPORT_MD_NAME)).status_code
        == 200
    )


def test_report_feed_refuses_to_serve_an_ambiguous_catalog(tmp_path, monkeypatch):
    # Only a hand-edit, a merge, or a restore can put two entries at one sequence,
    # and a cursor over that file cannot promise exactly-once traversal: a consumer
    # resuming from the duplicate's watermark would never see its twin. Both routes
    # therefore name the defect instead of serving a page that quietly omits it.
    client, config, _repo = make_feed_client(
        tmp_path,
        monkeypatch,
        evaluation_ids=("20260722T090000Z", "20260722T100000Z"),
    )
    path = config.data_dir / "report-catalog" / "catalog.jsonl"
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    key = rows[0]["report_key"]
    rows[1]["seq"] = rows[0]["seq"]
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    listed = client.get("/api/evaluation/reports")
    fetched = client.get(f"/api/evaluation/reports/{key}")

    assert listed.status_code == 500
    assert "ambiguous" in listed.json()["detail"]
    assert fetched.status_code == 500


def test_report_feed_routes_are_404_when_evaluation_is_disabled(tmp_path, monkeypatch):
    client, *_ = make_feed_client(tmp_path, monkeypatch, enabled=False)

    for resp in (
        client.get("/api/evaluation/reports"),
        client.get("/api/evaluation/reports/rpt-x"),
        client.get(f"/api/evaluation/reports/rpt-x/artifacts/{EVIDENCE_NAME}"),
    ):
        assert resp.status_code == 404
        assert "disabled" in resp.json()["detail"]


def test_report_feed_routes_require_auth(tmp_path):
    unauthed = TestClient(api.create_app(eval_config(tmp_path)))
    assert unauthed.get("/api/evaluation/reports").status_code == 401
    assert unauthed.get("/api/evaluation/reports/rpt-x").status_code == 401
    assert (
        unauthed.get(
            f"/api/evaluation/reports/rpt-x/artifacts/{EVIDENCE_NAME}"
        ).status_code
        == 401
    )


# -- raw report artifacts ------------------------------------------------------


def artifact_url(key: str, name: str) -> str:
    return f"/api/evaluation/reports/{key}/artifacts/{name}"


def published_report(tmp_path, monkeypatch, evaluation_id="20260722T090000Z"):
    """A client plus the feed item and artifact dir of one published report."""
    client, config, repo = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=(evaluation_id,)
    )
    item = client.get("/api/evaluation/reports").json()["reports"][0]
    eval_dir = config.data_dir / "evaluations" / repo.name / RUN_TASK_ID / evaluation_id
    return client, config, item, eval_dir


@pytest.mark.parametrize("name", REQUIRED_ARTIFACTS)
def test_raw_artifact_response_is_byte_exact_and_matches_the_feed_manifest(
    tmp_path, monkeypatch, name
):
    # The reason this route exists: the bundle route parses each artifact and the
    # JSON response re-serializes it, so its bytes are this service's formatting
    # and hashing them proves nothing. Here the body is the persisted file, so the
    # size/sha256 the feed published are verifiable by the consumer.
    client, _config, item, eval_dir = published_report(tmp_path, monkeypatch)
    manifest = next(a for a in item["artifacts"] if a["name"] == name)

    resp = client.get(artifact_url(item["report_key"], name))

    assert resp.status_code == 200
    assert resp.content == (eval_dir / name).read_bytes()
    assert hashlib.sha256(resp.content).hexdigest() == manifest["sha256"]
    assert len(resp.content) == manifest["size"]
    assert resp.headers["x-report-artifact-integrity"] == "verified"
    assert resp.headers["x-report-artifact-sha256"] == manifest["sha256"]
    assert name in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_raw_artifact_content_types_label_json_and_markdown(tmp_path, monkeypatch):
    client, _config, item, _dir = published_report(tmp_path, monkeypatch)

    json_type = client.get(artifact_url(item["report_key"], EVIDENCE_NAME))
    md_type = client.get(artifact_url(item["report_key"], SEMANTIC_REPORT_MD_NAME))

    assert json_type.headers["content-type"] == "application/json"
    assert md_type.headers["content-type"].startswith("text/markdown")


def test_raw_artifact_preserves_unicode_and_exact_trailing_bytes(tmp_path, monkeypatch):
    # Whitespace, a CRLF, and a missing final newline all change the digest, so a
    # response that normalized any of them would read as a tampered report.
    body = "# report\r\n\r\n- café   \n\n没有换行"
    client, _config, item, eval_dir = published_report(tmp_path, monkeypatch)
    (eval_dir / SEMANTIC_REPORT_MD_NAME).write_text(body, encoding="utf-8")

    resp = client.get(artifact_url(item["report_key"], SEMANTIC_REPORT_MD_NAME))

    assert resp.content == body.encode("utf-8")
    assert resp.headers["content-length"] == str(len(body.encode("utf-8")))
    # Edited after publication, so the recorded digest no longer matches — the
    # bytes are still served, and the header is where the discrepancy is stated.
    assert resp.headers["x-report-artifact-integrity"] == "mismatched"


def test_raw_artifact_unknown_key_is_404(tmp_path, monkeypatch):
    client, *_ = published_report(tmp_path, monkeypatch)

    resp = client.get(
        artifact_url("rpt-0123456789abcdef0123456789abcdef", EVIDENCE_NAME)
    )

    assert resp.status_code == 404
    assert "unknown report key" in resp.json()["detail"]


@pytest.mark.parametrize("name", ["notes.txt", "..%2Fevidence.json", "evidence.json~"])
def test_raw_artifact_unknown_name_is_404_and_reads_no_file(
    tmp_path, monkeypatch, name
):
    # ``name`` selects from the four published artifacts; it is never a path. A
    # real file next to them under another name is still not a published artifact,
    # which is the same rule that makes a traversal attempt inert — and an escaped
    # separator does not even reach the handler, since a decoded slash matches no
    # single path segment.
    client, _config, item, eval_dir = published_report(tmp_path, monkeypatch)
    (eval_dir / "notes.txt").write_text("operator scratch\n", encoding="utf-8")

    resp = client.get(artifact_url(item["report_key"], name))

    assert resp.status_code == 404
    assert b"operator scratch" not in resp.content


def test_raw_artifact_of_a_pruned_report_is_410_not_404(tmp_path, monkeypatch):
    # The entry outlives its artifacts by design, so "published, and gone" must be
    # distinguishable from "wrong key" without parsing the message.
    import shutil

    client, config, item, _dir = published_report(tmp_path, monkeypatch)
    shutil.rmtree(config.data_dir / "evaluations")

    resp = client.get(artifact_url(item["report_key"], EVIDENCE_NAME))

    assert resp.status_code == 410
    assert "no longer retained" in resp.json()["detail"]
    # A read is a read: nothing is re-evaluated to heal the missing artifact.
    assert not (config.data_dir / "evaluations").exists()


def test_raw_artifact_serves_a_survivor_of_a_partially_pruned_set(
    tmp_path, monkeypatch
):
    # Availability is per artifact here, unlike the all-or-nothing bundle: the
    # bundle protects an import from being taken for a whole report, while a
    # byte check of one artifact needs nothing from the other three.
    client, _config, item, eval_dir = published_report(tmp_path, monkeypatch)
    (eval_dir / SEMANTIC_REPORT_MD_NAME).unlink()

    survivor = client.get(artifact_url(item["report_key"], EVIDENCE_NAME))
    removed = client.get(artifact_url(item["report_key"], SEMANTIC_REPORT_MD_NAME))
    bundle = client.get(f"/api/evaluation/reports/{item['report_key']}")

    assert survivor.status_code == 200
    assert survivor.content == (eval_dir / EVIDENCE_NAME).read_bytes()
    assert removed.status_code == 410
    assert bundle.json()["available"] is False


def test_raw_artifact_of_an_incoherent_catalog_identity_is_409(tmp_path, monkeypatch):
    # The slug rule is idempotent but lossy, so a hand-edited alias would serve the
    # real repo's artifact under a contradicting feed identity. Neither missing nor
    # a server fault: the stored line is the defect, and no retry heals it.
    client, config, item, _dir = published_report(tmp_path, monkeypatch)
    path = config.data_dir / "report-catalog" / "catalog.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["repo_slug"] = rows[0]["repo_slug"] + "!!"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    resp = client.get(artifact_url(item["report_key"], EVIDENCE_NAME))

    assert resp.status_code == 409
    assert "incoherent stored report identity" in resp.json()["detail"]


def test_raw_artifact_refuses_to_serve_from_an_ambiguous_catalog(tmp_path, monkeypatch):
    # Same fail-closed audit as the other two feed routes: a cursor over a file
    # with a repeated sequence cannot promise exactly-once traversal, so no read
    # path serves from it until it is repaired.
    client, config, _repo = make_feed_client(
        tmp_path,
        monkeypatch,
        evaluation_ids=("20260722T090000Z", "20260722T100000Z"),
    )
    path = config.data_dir / "report-catalog" / "catalog.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    key = rows[0]["report_key"]
    rows[1]["seq"] = rows[0]["seq"]
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    resp = client.get(artifact_url(key, EVIDENCE_NAME))

    assert resp.status_code == 500
    assert "ambiguous" in resp.json()["detail"]


def test_raw_artifact_that_will_not_read_is_500_not_a_missing_report(
    tmp_path, monkeypatch
):
    # A present-but-unreadable file is a hub-side fault. Answering 410 would tell a
    # consumer the report was pruned — a permanent answer to a transient problem.
    client, _config, item, _dir = published_report(tmp_path, monkeypatch)
    real_open = os.open

    def _boom(path, flags, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == EVIDENCE_NAME:
            raise OSError(errno.EIO, "input/output error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _boom)
    resp = client.get(artifact_url(item["report_key"], EVIDENCE_NAME))

    assert resp.status_code == 500
    assert EVIDENCE_NAME in resp.json()["detail"]


def test_raw_artifact_symlinked_out_of_the_tree_serves_no_outside_bytes(
    tmp_path, monkeypatch
):
    # The route's containment promise is about the bytes it returns, not only the
    # path it builds: the artifact name is fixed, but the file standing under it is
    # not, so an artifact replaced after publication by a link to anything readable
    # outside the evaluations tree must not reach a consumer through the key.
    client, config, item, eval_dir = published_report(tmp_path, monkeypatch)
    outside = config.data_dir.parent / "outside-the-tree.md"
    outside.write_text("# private content\n", encoding="utf-8")
    link = eval_dir / SEMANTIC_REPORT_MD_NAME
    link.unlink()
    link.symlink_to(outside)

    resp = client.get(artifact_url(item["report_key"], SEMANTIC_REPORT_MD_NAME))

    assert resp.status_code == 500
    assert b"private content" not in resp.content
    assert SEMANTIC_REPORT_MD_NAME in resp.json()["detail"]
    # Refused per artifact: the report's untouched artifacts still serve, so a
    # tampered file cannot take the whole published report offline.
    intact = client.get(artifact_url(item["report_key"], EVIDENCE_NAME))
    assert intact.status_code == 200


def test_raw_artifact_symlink_loop_answers_500_instead_of_crashing_the_route(
    tmp_path, monkeypatch
):
    # A damaged tree must reach the client as the route's documented hub-side
    # fault, not as an unclassified crash: resolving a loop by pathname *raises*
    # (`RuntimeError` on Python <= 3.12, which this project supports), so the read
    # has to answer with a reason rather than let that escape its result.
    client, _config, item, eval_dir = published_report(tmp_path, monkeypatch)
    artifact = eval_dir / SEMANTIC_REPORT_MD_NAME
    artifact.unlink()
    artifact.symlink_to(artifact)

    resp = client.get(artifact_url(item["report_key"], SEMANTIC_REPORT_MD_NAME))

    assert resp.status_code == 500
    assert SEMANTIC_REPORT_MD_NAME in resp.json()["detail"]
    intact = client.get(artifact_url(item["report_key"], EVIDENCE_NAME))
    assert intact.status_code == 200


def test_raw_artifact_reads_change_nothing_on_disk(tmp_path, monkeypatch):
    # Read-only in the strong sense: no publication, no evaluation, no retention
    # side effect — the hub's data dir is byte-identical after a full sweep of the
    # route across every artifact of a published report.
    client, config, item, _dir = published_report(tmp_path, monkeypatch)

    def snapshot() -> dict:
        return {
            path.relative_to(config.data_dir): path.read_bytes()
            for path in sorted(config.data_dir.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    for name in REQUIRED_ARTIFACTS:
        assert client.get(artifact_url(item["report_key"], name)).status_code == 200

    assert snapshot() == before


def test_raw_artifact_and_bundle_agree_on_the_same_published_content(
    tmp_path, monkeypatch
):
    # The two routes are different views of one artifact, not two sources: the raw
    # bytes must parse to exactly what the bundle (and the per-task detail route)
    # hands back.
    client, _config, item, _dir = published_report(tmp_path, monkeypatch)
    key = item["report_key"]

    bundle = client.get(f"/api/evaluation/reports/{key}").json()
    raw_report = client.get(artifact_url(key, SEMANTIC_REPORT_NAME))
    raw_md = client.get(artifact_url(key, SEMANTIC_REPORT_MD_NAME))

    assert json.loads(raw_report.content) == bundle["artifacts"]["semantic_report"]
    assert raw_md.content.decode("utf-8") == bundle["artifacts"]["report_md"]
    # And the feed page it was discovered through is unchanged by either read.
    assert client.get("/api/evaluation/reports").json()["reports"] == [item]


def test_task_level_evaluation_routes_stay_compatible_with_the_feed(
    tmp_path, monkeypatch
):
    # The feed is additive: the per-task list/detail routes the dashboard uses
    # keep their shapes, and both surfaces name the same source evaluation.
    client, _config, repo = make_feed_client(
        tmp_path, monkeypatch, evaluation_ids=("20260722T090000Z",)
    )
    item = client.get("/api/evaluation/reports").json()["reports"][0]

    listed = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    assert listed["latest_evaluation_id"] == item["evaluation_id"]
    assert listed["evaluations"][0]["has_semantic_report"] is True

    detail = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/{item['evaluation_id']}"
    ).json()
    bundle = client.get(f"/api/evaluation/reports/{item['report_key']}").json()
    # Same artifacts, reached two ways: by (repo, task, evaluation) and by key.
    assert detail["semantic_report"] == bundle["artifacts"]["semantic_report"]
    assert detail["report_md"] == bundle["artifacts"]["report_md"]


# -- error shapes --------------------------------------------------------------


def test_evaluation_list_unknown_repo_is_404(tmp_path, monkeypatch):
    client, *_ = make_eval_client(tmp_path, monkeypatch)
    resp = client.get(f"/api/repos/ghost/evaluations/{RUN_TASK_ID}")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]


def test_evaluation_generate_unknown_repo_is_404(tmp_path, monkeypatch):
    client, *_ = make_eval_client(tmp_path, monkeypatch)
    resp = client.post(f"/api/repos/ghost/evaluations/{RUN_TASK_ID}/metrics")
    assert resp.status_code == 404


def test_fetch_unknown_evaluation_id_is_404(tmp_path, monkeypatch):
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    resp = client.get(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/20990101T000000Z"
    )
    assert resp.status_code == 404


# -- partial evidence ----------------------------------------------------------


def test_partial_evidence_surfaces_failed_run_without_diagnostic(tmp_path, monkeypatch):
    # A related run that ended non-zero with no log or spawn.out is partial
    # evidence: the pack still classifies it and the report flags the missing
    # diagnostic, all end-to-end through the HTTP surface.
    repo = make_run_repo(tmp_path)
    run_dir = tmp_path / "hub-data" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    record = RunRecord(
        "r1",
        repo.name,
        repo.path,
        RUN_TASK_ID,
        log_dir=run_dir,  # a real but empty dir → no log located, degrades cleanly
        started_ts="2026-07-22T09:00:00Z",
        ended_ts="2026-07-22T09:10:00Z",
        exit_code=1,
    )
    client, *_ = make_eval_client(
        tmp_path, monkeypatch, repo=repo, records=[(run_dir, record)]
    )

    body = client.post(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/metrics"
    ).json()
    runs = body["static_metrics"]["runs"]
    assert runs["run_count"] == 1
    assert runs["failed_count"] == 1
    assert runs["failed_without_diagnostic_count"] == 1
    reliability = next(
        s for s in body["static_metrics"]["scores"] if s["name"] == "reliability"
    )
    assert reliability["score"] is not None and reliability["score"] < 1.0


# -- disabled mode -------------------------------------------------------------


def test_disabled_mode_action_routes_are_404(tmp_path, monkeypatch):
    # Every evaluation route except availability is removed (404) with the
    # fail-closed reason when ``ORCH_HUB_EVAL_ENABLED`` is off.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch, enabled=False)

    for resp in (
        client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}"),
        client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/anything"),
        client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/evidence"),
        client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/metrics"),
        client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic/job"),
    ):
        assert resp.status_code == 404
        assert "disabled" in resp.json()["detail"]


def test_disabled_mode_writes_no_artifacts(tmp_path, monkeypatch):
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch, enabled=False)
    client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/metrics")
    # The fail-closed gate short-circuits before any generation: no evaluations
    # tree is created under the hub data dir.
    assert not (tmp_path / "hub-data" / "evaluations").exists()


# -- module boundary: disabling evaluation must not break core run control ------


def test_disabling_evaluation_leaves_core_run_control_intact(tmp_path, monkeypatch):
    # The evaluation subsystem is an optional sidecar. A customer deployment can
    # disable it (ORCH_HUB_EVAL_ENABLED falsy), and every core flow — repo/task
    # inventory, run start/list/history/logs/sessions, control-dir human input, and
    # graceful stop — must keep working while only the evaluation routes fail
    # closed. This pins the module boundary the docs/evaluation.md guarantee.
    repo = make_run_repo(tmp_path)
    manager, _spawner = make_run_manager(tmp_path)
    config = Config(
        mandrel_path=tmp_path / "mandrel",
        token=TOKEN,
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=False,
    )
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    client = TestClient(api.create_app(config, run_manager=manager))
    client.headers["Authorization"] = f"Bearer {TOKEN}"

    # Evaluation is off: availability says so, and the doctor + task routes 404.
    assert client.get("/api/evaluation").json() == {"enabled": False}
    assert client.get("/api/evaluation/doctor").status_code == 404
    assert (
        client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").status_code
        == 404
    )

    # Repo/task inventory is unaffected.
    assert client.get("/api/repos").status_code == 200
    assert client.get("/api/tasks").status_code == 200
    assert client.get(f"/api/repos/{repo.name}/tasks").status_code == 200

    # Start/list a run.
    run_id = start_a_run(client, repo)["run_id"]
    assert client.get("/api/runs").status_code == 200
    assert client.get(f"/api/runs/{run_id}").status_code == 200

    # Run history, logs, and per-session context all serve.
    assert client.get("/api/history/runs").status_code == 200
    assert client.get(f"/api/runs/{run_id}/logs").status_code == 200
    assert client.get(f"/api/runs/{run_id}/sessions").status_code == 200

    # Control-dir human input round-trips: a pending question can be answered.
    write_question(control_dir_of(manager, run_id), banner="?")
    assert client.get("/api/escalations").status_code == 200
    answered = client.post(
        f"/api/runs/{run_id}/escalations/1/answer", json={"answer": "go"}
    )
    assert answered.status_code == 200

    # Graceful stop still works.
    assert client.post(f"/api/runs/{run_id}/stop").status_code == 200


# -- read-only over the target -------------------------------------------------


def test_generate_is_read_only_over_the_target_repo(tmp_path, monkeypatch):
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    task_file = repo.path / ".ai-tasks" / f"{RUN_TASK_ID}.md"
    before = task_file.read_text(encoding="utf-8")
    before_listing = sorted(p.name for p in (repo.path / ".ai-tasks").iterdir())

    client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/metrics")

    assert task_file.read_text(encoding="utf-8") == before  # task file untouched
    assert sorted(p.name for p in (repo.path / ".ai-tasks").iterdir()) == before_listing
    # Artifacts landed only in the hub-owned evaluations tree, outside the repo.
    assert (tmp_path / "hub-data" / "evaluations" / repo.name / RUN_TASK_ID).is_dir()


# -- auth ----------------------------------------------------------------------


def test_evaluation_routes_require_auth(tmp_path, monkeypatch):
    # The default-deny middleware covers every evaluation route, availability
    # included: an unauthenticated caller is 401 on all of them.
    config = eval_config(tmp_path)
    unauthed = TestClient(api.create_app(config))
    assert unauthed.get("/api/evaluation").status_code == 401
    assert unauthed.get(f"/api/repos/r/evaluations/{RUN_TASK_ID}").status_code == 401
    assert unauthed.get(
        f"/api/repos/r/evaluations/{RUN_TASK_ID}/e1"
    ).status_code == 401
    assert unauthed.post(
        f"/api/repos/r/evaluations/{RUN_TASK_ID}/evidence"
    ).status_code == 401
    assert unauthed.post(
        f"/api/repos/r/evaluations/{RUN_TASK_ID}/metrics"
    ).status_code == 401
    assert unauthed.post(
        f"/api/repos/r/evaluations/{RUN_TASK_ID}/semantic/job"
    ).status_code == 401
    assert unauthed.get(
        f"/api/repos/r/evaluations/{RUN_TASK_ID}/semantic/job"
    ).status_code == 401


# -- completion-triggered evaluation wiring ------------------------------------


def test_the_app_wires_completion_triggered_evaluation(tmp_path):
    # The trigger is reachable only through a run ending, so assert the wiring
    # structurally: the app builds one and both gates resolve on. What it then
    # does with a completed run is covered end-to-end in tests/test_completion_eval.py.
    app = api.create_app(eval_config(tmp_path))

    trigger = app.state.completion_trigger
    assert trigger.enabled is True


def test_the_app_wires_the_dispatch_policy_onto_the_resident_tick(tmp_path):
    """A held Layer 2 job is picked back up by nothing else.

    Structural, like the trigger above: the dispatcher exists, it gates on the
    Claude subscription (the provider the shipped Layer 2 backend spends
    against), and the resident monitor drives its sweep — which is also what
    resumes the queue after a restart, so no startup step owns that.
    """
    app = api.create_app(eval_config(tmp_path))

    dispatcher = app.state.eval_dispatcher
    assert dispatcher is not None
    assert dispatcher._gate.provider == PROVIDER_CLAUDE
    assert dispatcher.sweep in app.state.run_monitor._sweeps


def test_the_dispatch_gate_reads_usage_without_ever_gating_run_control(tmp_path):
    """Usage is display-only, so a broken feed must hold evaluation and nothing else."""
    class BrokenUsage:
        def provider_usage(self, provider):
            raise OSError("usage store unreadable")

        def get_usage(self):
            raise OSError("usage store unreadable")

        def request_claude_refresh(self):
            return None

        def claude_observer(self):
            return lambda stdout: None

    app = api.create_app(eval_config(tmp_path), usage_service=BrokenUsage())

    # The gate degrades to deferral rather than raising into the caller...
    assert app.state.eval_dispatcher._gate.check() == "usage-unknown"
    # ...and one sweep over an empty queue is still a clean no-op.
    assert app.state.eval_dispatcher.sweep() is None


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"evaluation_enabled": False}, False),  # subsystem off: nothing to trigger
        ({"eval_auto": False}, False),  # trigger off: manual-only operation
    ],
)
def test_the_apps_trigger_honors_both_gates(tmp_path, overrides, expected):
    config = Config(
        mandrel_path=tmp_path / "mandrel",
        token=TOKEN,
        data_dir=tmp_path / "hub-data",
        **overrides,
    )
    app = api.create_app(config)

    assert app.state.completion_trigger.enabled is expected
    # Both halves of the automatic path, because the queue is durable: the sweep
    # is wired onto the resident tick unconditionally, so a switch honored only at
    # acceptance would still promote whatever an earlier enabled process accepted.
    # What a closed gate then does to a pre-existing queue: tests/test_eval_dispatch.py.
    assert app.state.eval_dispatcher.enabled is expected


def test_an_injected_run_manager_is_never_handed_the_apps_evaluator(
    tmp_path, monkeypatch
):
    """A hermetic run test must not acquire a live evaluator by being wired here.

    The hook is a constructor argument (the usage-refresh convention), so an
    injected manager keeps whatever hooks it was built with — here, none. A run
    that ends completed under it therefore writes no evaluation artifacts, while
    the app still exposes a trigger for its own manager.
    """
    repo = make_run_repo(tmp_path)
    manager, spawner = make_run_manager(tmp_path)
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    app = api.create_app(eval_config(tmp_path), run_manager=manager)
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {TOKEN}"

    run_id = start_a_run(client, repo)["run_id"]
    write_run_log(
        manager,
        run_id,
        [
            "[03:00:10] --- dev session start: sid=dev1 backend=claude:x@max status_before=in_progress",
            "[03:15:00] --- dev session end: sid=dev1 status=completed+archived",
        ],
    )
    spawner.procs[0].returncode = 0

    assert client.get(f"/api/runs/{run_id}").json()["state"] == "completed"
    assert app.state.completion_trigger is not None
    assert not (tmp_path / "hub-data" / "evaluations").exists()
    assert not (tmp_path / "hub-data" / "semantic-jobs").exists()
