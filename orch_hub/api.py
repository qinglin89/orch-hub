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

Every route except the minimal ``GET /api/health`` liveness probe requires a
bearer token (``Config.token``, from ``ORCH_HUB_TOKEN``). Enforcement is a
default-deny middleware rather than per-route dependencies so future surfaces
(runs, logs, dashboard) are protected the moment they are mounted, not when
someone remembers to annotate them. With no token configured, protected routes
fail closed with 503. :func:`run` still binds to localhost by default; the
private-network deployment assumptions are documented in the README.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import TOKEN_ENV, Config
from .control_dir import (
    DEFAULT_RESPONDER,
    DuplicateAnswerError,
    list_questions,
    pending_questions,
    write_answer,
)
from .models import repos_payload, tasks_payload
from .readiness import check_readiness
from .registry import RegistryError, discover_repos
from .runs import (
    BlockedTaskError,
    LaunchBlockedError,
    RepoBusyError,
    RunEndedError,
    RunManager,
    UnknownRunError,
)
from .task_store import list_repo_tasks, list_tasks

PUBLIC_PATHS = frozenset({"/api/health"})
"""Paths served without authentication.

Only the minimal liveness probe: monitors and launchd health checks must not
need secret material, and a 401 from any other path already reveals liveness
anyway. Everything else — including docs/OpenAPI and future dashboard routes —
requires the token.
"""


class StartRunRequest(BaseModel):
    """Body for ``POST /api/runs``.

    ``repo_id`` is the managed-repo name (resolved through registry discovery,
    like ``GET /api/repos/{repo_id}/tasks``). ``extra_args`` are appended to the
    orchestrator argv; ``acknowledge_blocked`` opts into launching a ``blocked``
    task despite the §5.4 resume caveat.
    """

    repo_id: str
    task_id: str
    extra_args: list[str] = Field(default_factory=list)
    acknowledge_blocked: bool = False


class AnswerRequest(BaseModel):
    """Body for answering an escalation (a pending orchestrator question).

    ``responder`` is recorded in the answer file's audit fields; it defaults to
    the control-dir adapter's ``orch-hub`` when the client sends nothing.
    """

    answer: str
    responder: str | None = None


def create_app(
    config: Config | None = None, *, run_manager: RunManager | None = None
) -> FastAPI:
    """Build the FastAPI app, resolving config from the environment by default.

    ``config`` is captured once per app instance; tests pass an explicit
    :class:`~orch_hub.config.Config` to stay hermetic. The core functions are
    referenced as module globals so tests can monkeypatch them (mirroring the
    CLI tests) without threading fakes through the routes.

    ``run_manager`` is the single :class:`~orch_hub.runs.RunManager` the run and
    escalation routes drive; it is built from ``resolved`` by default, and tests
    inject one wired with fake spawner/clock over a temp data dir so no real
    subprocess is ever started.
    """
    resolved = config if config is not None else Config.from_env()
    manager = run_manager if run_manager is not None else RunManager(resolved)
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

    # Run-manager errors map to their HTTP shapes here (not in each handler) so
    # every run/escalation route reuses one mapping. 404 = no such run; 409 =
    # a state conflict the caller can act on (busy repo, blocked task needing
    # acknowledgement, refused launch gate, ended run, duplicate answer).
    @app.exception_handler(UnknownRunError)
    async def _unknown_run(request: Request, exc: UnknownRunError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(RepoBusyError)
    async def _repo_busy(request: Request, exc: RepoBusyError) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "run_id": exc.run_id}
        )

    @app.exception_handler(RunEndedError)
    async def _run_ended(request: Request, exc: RunEndedError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(BlockedTaskError)
    async def _blocked_task(request: Request, exc: BlockedTaskError) -> JSONResponse:
        # The client can retry with acknowledge_blocked=true; the §5.4 caveat
        # is spelled out in the message.
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "acknowledge_required": True},
        )

    @app.exception_handler(LaunchBlockedError)
    async def _launch_blocked(
        request: Request, exc: LaunchBlockedError
    ) -> JSONResponse:
        # The failed fail-closed gates (readiness reasons, bad task status, ...)
        # ride along as a list for the UI to render.
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "reasons": list(exc.reasons)},
        )

    @app.exception_handler(DuplicateAnswerError)
    async def _duplicate_answer(
        request: Request, exc: DuplicateAnswerError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.middleware("http")
    async def require_bearer_token(request: Request, call_next):
        """Default-deny bearer auth for every path outside ``PUBLIC_PATHS``.

        Runs before routing, so unknown paths get 401 (not 404) when
        unauthenticated. Error bodies are static strings: the presented token
        is never echoed, logged, or compared non-constant-time.
        """
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        if resolved.token is None:
            # Fail closed: an unconfigured token disables serving, it never
            # disables auth.
            return JSONResponse(
                status_code=503,
                content={"detail": f"bearer token not configured; set {TOKEN_ENV}"},
            )
        scheme, _, credentials = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not credentials.strip():
            return JSONResponse(
                status_code=401,
                content={"detail": "missing bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not secrets.compare_digest(
            credentials.strip().encode(), resolved.token.encode()
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict:
        """Minimal public liveness probe.

        Deliberately free of configuration detail (it used to report
        ``ai_native_path``): this is the one unauthenticated route, so it
        must not disclose anything about the deployment.
        """
        return {"status": "ok"}

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

    @app.post("/api/runs", status_code=201)
    def start_run(req: StartRunRequest) -> dict:
        """Start an orchestrator run, resolving ``repo_id`` by managed-repo name.

        The run manager applies the fail-closed launch gates (per-repo single
        run, task inventory/status, readiness) and spawns the deployed
        orchestrator subprocess; gate refusals surface as the mapped 4xx
        responses above. An unknown ``repo_id`` is a 404. The response is the
        new run's :class:`RunStatus` (``active`` with no pending questions),
        the same shape as ``GET /api/runs/{run_id}``.
        """
        repos = discover_repos(resolved)
        repo = next((r for r in repos if r.name == req.repo_id), None)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {req.repo_id}")
        record = manager.start_run(
            repo,
            req.task_id,
            extra_args=req.extra_args,
            acknowledge_blocked=req.acknowledge_blocked,
        )
        return manager.get_run(record.run_id).to_dict()

    @app.get("/api/runs")
    def list_runs() -> dict:
        """List every known run (oldest first) with its computed live/end state."""
        return {"runs": [status.to_dict() for status in manager.list_runs()]}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        """Return one run's current :class:`RunStatus`; unknown ids are 404."""
        return manager.get_run(run_id).to_dict()

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str) -> dict:
        """Request a graceful stop by writing the run's ``stop.flag``.

        The stop lands at the orchestrator's next session boundary; if a
        question is pending it aborts the wait and discards any pending
        convergence-budget/dispute-unresolved ruling (README §5.5).
        ``newly_requested`` is False when a stop was already requested (the
        write is idempotent). Unknown ids are 404; an already-ended run is 409.
        """
        newly = manager.stop_run(run_id)
        return {
            "run_id": run_id,
            "newly_requested": newly,
            "detail": (
                "stop.flag written; the orchestrator stops at its next session"
                " boundary. If a question is pending, the stop aborts the wait"
                " and discards any pending convergence-budget/dispute-unresolved"
                " ruling (README §5.5)."
            ),
        }

    @app.get("/api/escalations")
    def list_escalations() -> dict:
        """List pending human questions across live runs, with run/repo context.

        Only live runs are scanned — a question on a finished run cannot unblock
        anything. Each entry carries the ``run_id`` plus the ``question`` (whose
        ``seq`` addresses the answer route
        ``POST /api/runs/{run_id}/escalations/{seq}/answer``).
        """
        escalations = []
        for status in manager.list_runs():
            record = status.record
            if (
                record.ended_ts is not None
                or record.error is not None
                or record.control_dir is None
            ):
                continue
            for question in pending_questions(record.control_dir):
                escalations.append(
                    {
                        "run_id": record.run_id,
                        "repo_name": record.repo_name,
                        "task_id": record.task_id,
                        "question": question.to_dict(),
                    }
                )
        return {"escalations": escalations}

    @app.post("/api/runs/{run_id}/escalations/{seq}/answer")
    def answer_escalation(run_id: str, seq: int, req: AnswerRequest) -> dict:
        """Write the answer file for one pending question, auditing the responder.

        The run is located by ``run_id`` (unknown → 404) and the question by
        ``seq`` within its control dir (unknown → 404). The write goes through
        :func:`orch_hub.control_dir.write_answer`, so a pre-existing valid
        answer is refused (409) and an empty/whitespace answer is rejected
        (400); ``"stop"`` is a legitimate answer that aborts the run.
        """
        status = manager.get_run(run_id)
        control_dir = status.record.control_dir
        if control_dir is None:
            raise HTTPException(
                status_code=404, detail=f"run {run_id} has no control dir"
            )
        question = next(
            (q for q in list_questions(control_dir) if q.seq == seq), None
        )
        if question is None:
            raise HTTPException(
                status_code=404,
                detail=f"run {run_id!r} has no question with seq {seq}",
            )
        responder = req.responder or DEFAULT_RESPONDER
        try:
            write_answer(question, req.answer, responder=responder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "run_id": run_id,
            "seq": seq,
            "answered": True,
            "responder": responder,
            "answer": req.answer.strip(),
        }

    return app


app = create_app()
"""Module-level app for ``uvicorn orch_hub.api:app`` and default serving."""


def run(host: str | None = None, port: int | None = None) -> None:
    """Serve the API with uvicorn, bound to localhost by default.

    ``host``/``port`` fall back to the environment configuration
    (``ORCH_HUB_HOST`` / ``ORCH_HUB_PORT``, default ``127.0.0.1:8765``), so
    service deployments — the launchd agent in particular — configure the bind
    through environment variables alone; the console script passes no
    arguments. Binding beyond loopback is only meant for private,
    Tailscale-reachable interfaces, with ``ORCH_HUB_TOKEN`` exported first —
    without it every protected route answers 503. Never expose the hub
    publicly.
    """
    import uvicorn

    config = Config.from_env()
    uvicorn.run(
        app,
        host=config.host if host is None else host,
        port=config.port if port is None else port,
    )


if __name__ == "__main__":
    run()
