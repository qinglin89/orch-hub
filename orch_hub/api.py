"""FastAPI service foundation for orch-hub.

A thin HTTP adapter over the same application functions the CLI already uses, so
discovery, readiness, and task-inventory logic is never duplicated:

- discovery/readiness: :func:`orch_hub.registry.discover_repos`,
  :func:`orch_hub.readiness.check_readiness`, :func:`orch_hub.models.repos_payload`.
- task inventory: :func:`orch_hub.task_store.list_tasks`,
  :func:`orch_hub.task_store.list_repo_tasks`,
  :func:`orch_hub.models.tasks_payload`.

Route handlers stay thin: they resolve repos, delegate to the shared functions,
and return the existing stable payload shapes. ``GET /api/repos`` returns the
same object as ``python -m orch_hub.cli repos --json``; the task endpoints
return the ``tasks_payload`` envelope, preserving ``index_present`` and the
task-store sentinel statuses.

Auth and non-localhost binding are intentionally out of scope here; they belong
to the bearer-auth task. :func:`run` binds to localhost by default.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Config
from .models import repos_payload, tasks_payload
from .readiness import check_readiness
from .registry import RegistryError, discover_repos
from .task_store import list_repo_tasks, list_tasks


def create_app(config: Config | None = None) -> FastAPI:
    """Build the FastAPI app, resolving config from the environment by default.

    ``config`` is captured once per app instance; tests pass an explicit
    :class:`~orch_hub.config.Config` to stay hermetic. The core functions are
    referenced as module globals so tests can monkeypatch them (mirroring the
    CLI tests) without threading fakes through the routes.
    """
    resolved = config if config is not None else Config.from_env()
    app = FastAPI(
        title="orch-hub",
        description="Local control plane for AI coding orchestrators.",
        version="0.1.0",
    )

    @app.exception_handler(RegistryError)
    async def _registry_error(request: Request, exc: RegistryError) -> JSONResponse:
        # Malformed registry data is an upstream/operational failure, not a
        # client error; surface it as 500 with the concise reason.
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.get("/api/health")
    def health() -> dict:
        """Liveness plus configuration sanity (which deployment the hub targets)."""
        return {"status": "ok", "ai_native_path": str(resolved.ai_native_path)}

    @app.get("/api/repos")
    def get_repos() -> dict:
        """List managed repos with readiness, identical to CLI ``repos --json``."""
        repos = discover_repos(resolved)
        pairs = [(repo, check_readiness(repo, resolved)) for repo in repos]
        return repos_payload(pairs)

    @app.get("/api/tasks")
    def get_tasks() -> dict:
        """List tasks across all discovered repos in the ``tasks_payload`` shape."""
        repos = discover_repos(resolved)
        return tasks_payload(list_tasks(repos))

    @app.get("/api/repos/{repo_id}/tasks")
    def get_repo_tasks(repo_id: str) -> dict:
        """List tasks for one repo (``repo_id`` is the managed-repo name).

        Returns the same ``tasks_payload`` envelope as ``/api/tasks`` with a
        single repo entry, so ``index_present`` and the sentinel statuses are
        preserved. An unknown repo id is a 404.
        """
        repos = discover_repos(resolved)
        for repo in repos:
            if repo.name == repo_id:
                return tasks_payload([list_repo_tasks(repo)])
        raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")

    return app


app = create_app()
"""Module-level app for ``uvicorn orch_hub.api:app`` and default serving."""


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the API with uvicorn, bound to localhost by default.

    The localhost default is deliberate: private-network binding and bearer
    auth are the bearer-auth task's concern, so this foundation slice does not
    expose the hub beyond the loopback interface.
    """
    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
