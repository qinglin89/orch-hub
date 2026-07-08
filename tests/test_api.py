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

from fastapi.testclient import TestClient

from orch_hub import api
from orch_hub.config import HOST_ENV, PORT_ENV, Config
from orch_hub.history import KIND_ANSWER_RECORDED, KIND_RUN_STARTED, read_events
from orch_hub.models import (
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
    RepoTaskInventory,
    TaskSummary,
)
from orch_hub.registry import RegistryError
from orch_hub.runs import RunManager
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

    def __call__(self, argv, *, cwd, stdout, start_new_session):
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
    assert unauthed.post("/api/runs/anything/stop").status_code == 401
    assert unauthed.get("/api/runs/anything/logs").status_code == 401
    assert unauthed.get("/api/runs/anything/logs/stream").status_code == 401
    assert unauthed.get("/api/escalations").status_code == 401
    assert unauthed.post(
        "/api/runs/anything/escalations/1/answer", json={"answer": "go"}
    ).status_code == 401
