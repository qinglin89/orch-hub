"""Tests for the FastAPI service adapter.

Monkeypatches the shared application functions (exactly as the CLI tests do) so
the HTTP wiring, error responses, and JSON serialization are exercised without
touching real repos, subprocesses, or the ai-native-development registry.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orch_hub import api
from orch_hub.config import HOST_ENV, PORT_ENV, Config
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
from orch_hub.evaluation import EvaluationService
from orch_hub.models import (
    CommitInfo,
    IntakeDraft,
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
    RepoTaskInventory,
    RunRecord,
    TaskSummary,
)
from orch_hub.registry import RegistryError
from orch_hub.runs import RunManager
from orch_hub.semantic import (
    SEMANTIC_END,
    SEMANTIC_REPORT_NAME,
    SEMANTIC_START,
    SemanticEvaluationError,
    SemanticEvaluator,
)
from orch_hub.static_metrics import StaticEvaluator
from orch_hub.subprocess_utils import CommandResult
from orch_hub.task_store import STATUS_MISSING

TOKEN = "test-token"
FAKE_CONFIG = Config(ai_native_path=Path("/fake/ai-native-development"), token=TOKEN)
NO_TOKEN_CONFIG = Config(ai_native_path=Path("/fake/ai-native-development"))


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
        raise RegistryError("invalid registry JSON from aii-2")

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
        ai_native_path=tmp_path / "ai-native",
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
        ai_native_path=tmp_path / "ai-native",
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
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    client = TestClient(
        api.create_app(
            config,
            evaluation_service=service,
            static_evaluator=static,
            semantic_evaluator=semantic,
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
    # The five deterministic score axes are present in the report.
    assert {s["name"] for s in report["scores"]} == {
        "protocol", "reliability", "handoff", "estimate", "knowledge"
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


def test_generate_semantic_then_list_and_fetch(tmp_path, monkeypatch):
    # A semantic evaluation over a fake backend persists the full chain and is
    # listed and read back through the same sidecar routes as Layer 1.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)

    gen = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic")
    assert gen.status_code == 201
    eval_id = gen.json()["evaluation_id"]
    report = gen.json()["semantic_report"]
    assert report["evaluation_id"] == eval_id
    assert report["task_id"] == RUN_TASK_ID
    assert report["evaluator"]["backend"] == "fake"
    assert report["scores"] and report["findings"]
    # Static metrics were fed as input (the chain is evidence→static→semantic).
    assert report["input_static_metrics"] is True

    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
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
    assert detail["semantic_report"] == report
    # All three JSON layers plus the rendered Markdown export are readable.
    assert detail["evidence"]["evaluation_id"] == eval_id
    assert detail["static_metrics"]["evaluation_id"] == eval_id
    assert detail["report_md"].startswith("# Semantic Task Evaluation")
    assert "handoff_completeness" in detail["report_md"]


def test_generate_semantic_backend_failure_is_502(tmp_path, monkeypatch):
    # A backend/model failure is a 502 (upstream evaluator failed) and, because
    # cleanup is atomic, leaves no persisted evaluation behind.
    client, *_ , repo = make_eval_client(
        tmp_path, monkeypatch, semantic_backend=RaisingSemanticBackend()
    )

    resp = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic")
    assert resp.status_code == 502
    assert "failed" in resp.json()["detail"]

    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    assert listing["evaluations"] == []  # nothing half-written


def test_generate_semantic_schema_violation_is_502(tmp_path, monkeypatch):
    # A non-conforming model response (missing the required confidence) is also a
    # 502 from the semantic layer's schema gate, with no artifact persisted.
    bad = semantic_envelope({"scores": [], "findings": []})
    client, *_ , repo = make_eval_client(
        tmp_path, monkeypatch, semantic_backend=FakeSemanticBackend(bad)
    )

    resp = client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic")
    assert resp.status_code == 502
    # Atomic cleanup removed the reserved evaluation dir: nothing is listed.
    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}").json()
    assert listing["evaluations"] == []


def test_generate_semantic_unknown_repo_is_404(tmp_path, monkeypatch):
    client, *_ = make_eval_client(tmp_path, monkeypatch)
    resp = client.post(f"/api/repos/ghost/evaluations/{RUN_TASK_ID}/semantic")
    assert resp.status_code == 404


def test_generate_semantic_is_read_only_over_the_target_repo(tmp_path, monkeypatch):
    # The service boundary is read-only: a semantic run never touches the target
    # task file or its .ai-tasks/ dir, only the hub-owned evaluations tree.
    client, *_ , repo = make_eval_client(tmp_path, monkeypatch)
    task_file = repo.path / ".ai-tasks" / f"{RUN_TASK_ID}.md"
    before = task_file.read_text(encoding="utf-8")
    before_listing = sorted(p.name for p in (repo.path / ".ai-tasks").iterdir())

    client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic")

    assert task_file.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in (repo.path / ".ai-tasks").iterdir()) == before_listing
    assert (tmp_path / "hub-data" / "evaluations" / repo.name / RUN_TASK_ID).is_dir()


def test_list_surfaces_semantic_evaluator_for_picker_comparison(tmp_path, monkeypatch):
    # The list route reads the persisted semantic report's evaluator best-effort so
    # the history picker can compare runs by model/version without opening each one.
    client, service, _static, _sem, repo = make_eval_client(
        tmp_path, monkeypatch, semantic_backend=FakeSemanticBackend(available=True)
    )
    eval_id = client.post(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic"
    ).json()["evaluation_id"]

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
    eval_id = client.post(
        f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic"
    ).json()["evaluation_id"]
    report_path = (
        service.artifact_dir(repo, RUN_TASK_ID, eval_id) / SEMANTIC_REPORT_NAME
    )
    report_path.write_text("{ not valid json", encoding="utf-8")

    listing = client.get(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}")
    assert listing.status_code == 200  # the corrupt report did not 500 the list
    entry = listing.json()["evaluations"][0]
    assert entry["has_semantic_report"] is True  # the file is still present
    assert entry["evaluator"] is None  # but its identity degraded cleanly


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
        client.post(f"/api/repos/{repo.name}/evaluations/{RUN_TASK_ID}/semantic"),
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
        ai_native_path=tmp_path / "ai-native",
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
        f"/api/repos/r/evaluations/{RUN_TASK_ID}/semantic"
    ).status_code == 401
