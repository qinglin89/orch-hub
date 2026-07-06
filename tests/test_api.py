"""Tests for the FastAPI service adapter.

Monkeypatches the shared application functions (exactly as the CLI tests do) so
the HTTP wiring, error responses, and JSON serialization are exercised without
touching real repos, subprocesses, or the ai-native-development registry.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from orch_hub import api
from orch_hub.config import Config
from orch_hub.models import (
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
    RepoTaskInventory,
    TaskSummary,
)
from orch_hub.registry import RegistryError
from orch_hub.task_store import STATUS_MISSING

FAKE_CONFIG = Config(ai_native_path=Path("/fake/ai-native-development"))


def make_repo(name: str = "r") -> ManagedRepo:
    return ManagedRepo(
        name=name,
        path=Path(f"/p/{name}"),
        manifest=Path(f"/p/{name}/.ai-deploy-manifest.json"),
    )


def build_client(config: Config | None = None) -> TestClient:
    return TestClient(api.create_app(config or FAKE_CONFIG))


def test_health_reports_ok_and_config():
    resp = build_client().get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "ai_native_path": "/fake/ai-native-development",
    }


def test_repos_ready_matches_cli_shape(monkeypatch):
    repo = make_repo()
    report = ReadinessReport((ReadinessCheck("repo_path_exists", True, "ok"),))
    monkeypatch.setattr(api, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(api, "check_readiness", lambda r, config: report)

    resp = build_client().get("/api/repos")
    assert resp.status_code == 200
    # Byte-for-byte the same object CLI `repos --json` emits (see test_cli).
    assert resp.json() == {
        "repos": [
            {
                "name": "r",
                "path": "/p/r",
                "manifest": "/p/r/.ai-deploy-manifest.json",
                "readiness": {"ready": True, "checks": []},
            }
        ]
    }


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


def test_module_level_app_is_fastapi():
    from fastapi import FastAPI

    assert isinstance(api.app, FastAPI)
