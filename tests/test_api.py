"""Tests for the FastAPI service adapter.

Monkeypatches the shared application functions (exactly as the CLI tests do) so
the HTTP wiring, error responses, and JSON serialization are exercised without
touching real repos, subprocesses, or the ai-native-development registry.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from orch_hub import api
from orch_hub.config import HOST_ENV, PORT_ENV, Config
from orch_hub.models import (
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
    RepoTaskInventory,
    TaskSummary,
)
from orch_hub.registry import RegistryError
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
