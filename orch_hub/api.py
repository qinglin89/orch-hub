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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import TOKEN_ENV, Config
from .control_dir import (
    DEFAULT_RESPONDER,
    DuplicateAnswerError,
    list_questions,
    normalize_answer,
    pending_questions,
    write_answer,
)
from .evaluation import (
    EVIDENCE_NAME,
    EvaluationDisabledError,
    EvaluationService,
)
from .git_meta import DEFAULT_COMMIT_LIMIT, latest_commit, read_commits
from .housekeeping import housekeeping_pending
from .intake import (
    IntakeBusyError,
    IntakeManager,
    IntakeNotReadyError,
    IntakeStateError,
    UnknownIntakeError,
)
from .intake_agent import ClaudeIntakeAgent
from .logs import DEFAULT_TAIL_LINES, read_recent, read_spawn_out, tail_events
from .models import ManagedRepo, repos_payload, tasks_payload
from .monitor import RunMonitor
from .notifications import build_notifier
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
from .semantic import (
    SEMANTIC_REPORT_MD_NAME,
    SEMANTIC_REPORT_NAME,
    ClaudeSemanticBackend,
    SemanticEvaluationError,
    SemanticEvaluator,
)
from .static_metrics import STATIC_METRICS_NAME, StaticEvaluator
from .task_store import (
    list_repo_tasks,
    list_tasks,
    read_archived_task_content,
    read_task_content,
)

WEB_DIR = Path(__file__).resolve().parent / "web"
"""Directory holding the dashboard's static shell assets (served by SHELL_ASSETS)."""

SHELL_ASSETS = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/styles.css": ("styles.css", "text/css"),
}
"""Public dashboard shell: request path -> (filename under WEB_DIR, media type).

These are the only non-health public paths. The shell is pure client code with
no secrets — a browser cannot attach an ``Authorization`` header on plain
navigation, so the bootstrap page must load unauthenticated; the SPA then reads
the bearer token from ``localStorage`` and attaches it to every data ``fetch``.
Keeping the set explicit (not a ``StaticFiles`` mount) keeps default-deny tight:
every public path is enumerated here and pinned by the auth tests.
"""

SHELL_CACHE_CONTROL = "no-store"
"""``Cache-Control`` sent with every dashboard shell asset.

``FileResponse`` sets ``Last-Modified`` and ``ETag`` but no ``Cache-Control``,
which leaves a browser free to apply *heuristic* freshness (RFC 9111 §4.2.2):
with no explicit lifetime it may treat an asset as fresh for a fraction of the
time since it was last modified. For an ``app.js`` / ``styles.css`` last touched
hours or days ago that window is long enough that after ``orch-hub launchd
update`` ships a new build, mobile Chrome/Safari keep serving the cached (stale)
copy without revalidating — the incognito-mode / clear-site-data symptom this
policy removes. ``no-store`` forbids caching outright, so every navigation
re-fetches the current asset and a deploy is picked up on the next load. The
shell is three tiny files served over a local/Tailscale link, so not caching
them costs nothing meaningful; data routes are unaffected (this rides only on
the enumerated shell responses).
"""

PUBLIC_PATHS = frozenset({"/api/health", *SHELL_ASSETS})
"""Paths served without authentication.

The minimal liveness probe plus the dashboard shell assets (:data:`SHELL_ASSETS`).
A 401 from any other path already reveals liveness, and the shell carries no
secret material. Everything else — including docs/OpenAPI, all data routes, and
future dashboard data endpoints — requires the token.
"""


class BearerAuthMiddleware:
    """Default-deny bearer auth for every path outside :data:`PUBLIC_PATHS`.

    Pure ASGI, deliberately not ``BaseHTTPMiddleware`` (the ``@app.middleware(
    "http")`` form): ``BaseHTTPMiddleware`` wraps the response in an anyio
    stream that can buffer a streaming body and delay client-disconnect
    propagation, which breaks a long-lived SSE log stream
    (``GET /api/runs/{run_id}/logs/stream``). Running before routing means an
    unknown path is 401 (not 404) when unauthenticated. Error bodies are static
    — the presented token is never echoed — and the compare is constant-time.
    Behavior is otherwise identical to the previous ``@app.middleware("http")``
    implementation; the auth tests pin every case.
    """

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Non-HTTP scopes (lifespan; websockets, of which there are none) and the
        # public liveness probe pass straight through.
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        denial = self._denial(scope)
        if denial is not None:
            await denial(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _denial(self, scope: Scope) -> JSONResponse | None:
        """Return the response to send if unauthorized, else ``None`` to allow."""
        if self.token is None:
            # Fail closed: an unconfigured token disables serving, never auth.
            return JSONResponse(
                status_code=503,
                content={"detail": f"bearer token not configured; set {TOKEN_ENV}"},
            )
        authorization = Headers(scope=scope).get("Authorization", "")
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credentials.strip():
            return JSONResponse(
                status_code=401,
                content={"detail": "missing bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not secrets.compare_digest(
            credentials.strip().encode(), self.token.encode()
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None


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

    ``mode`` is the dashboard button's explicit intent — ``answer`` (a binding
    answer / plan approval) or ``discuss`` (a clarifying turn / plan feedback).
    An explicit mode is reconciled with the question's ``kind`` through
    :func:`orch_hub.control_dir.normalize_answer` before writing, failing closed
    (400) rather than letting button intent silently mismatch the orchestrator's
    parse. An unrecognized value is rejected by validation (422).

    ``mode`` is optional and defaults to ``None`` = no declared button intent: a
    request that omits it (a legacy/pre-``mode`` client, including the current
    single-input dashboard) is written verbatim exactly as before this field
    existed, skipping normalization. The fail-closed guards apply only to the
    explicit modes the button pair sends, so an omitted ``mode`` behaves exactly
    as today (back-compat).
    """

    answer: str
    responder: str | None = None
    mode: Literal["answer", "discuss"] | None = None


class StartIntakeRequest(BaseModel):
    """Body for ``POST /api/intakes``.

    ``repo_id`` is the managed-repo name (resolved through registry discovery,
    like the run routes). ``request`` is the natural-language task request the
    intake agent drafts from; the manager rejects an empty/whitespace request.
    """

    repo_id: str
    request: str


class ReviseIntakeRequest(BaseModel):
    """Body for ``POST /api/intakes/{intake_id}/revise``.

    ``feedback`` is the reviewer's change request, sent back to the same intake
    session for a fresh draft; the manager rejects empty/whitespace feedback.
    """

    feedback: str


def _evaluation_summary(stored, evaluator: dict | None = None) -> dict:
    """Map a :class:`~orch_hub.models.StoredEvaluation` to its API summary.

    The evaluation-history picker needs the id and which artifacts exist, not the
    concrete filenames: this translates the stored ``artifacts`` set into the
    stable ``has_evidence``/``has_static_metrics``/``has_semantic_report``
    booleans, keeping ``evidence.json`` / ``static_metrics.json`` /
    ``semantic_report.json`` an internal detail of the evaluation subsystem rather
    than part of the HTTP contract. ``report.md`` is not surfaced separately — it is
    written atomically with ``semantic_report.json``, so its presence tracks that
    flag and the detail route serves the rendered text under ``report_md``.

    ``evaluator`` is the optional ``{backend, model, schema_version}`` identity the
    list route reads best-effort from a present ``semantic_report.json`` so the
    history picker can compare runs by model/version without opening each one; it
    is ``None`` for a metrics/evidence-only evaluation or when the read degraded.
    """
    return {
        "evaluation_id": stored.evaluation_id,
        "has_evidence": EVIDENCE_NAME in stored.artifacts,
        "has_static_metrics": STATIC_METRICS_NAME in stored.artifacts,
        "has_semantic_report": SEMANTIC_REPORT_NAME in stored.artifacts,
        "evaluator": evaluator,
    }


def create_app(
    config: Config | None = None,
    *,
    run_manager: RunManager | None = None,
    intake_manager: IntakeManager | None = None,
    evaluation_service: EvaluationService | None = None,
    static_evaluator: StaticEvaluator | None = None,
    semantic_evaluator: SemanticEvaluator | None = None,
) -> FastAPI:
    """Build the FastAPI app, resolving config from the environment by default.

    ``config`` is captured once per app instance; tests pass an explicit
    :class:`~orch_hub.config.Config` to stay hermetic. The core functions are
    referenced as module globals so tests can monkeypatch them (mirroring the
    CLI tests) without threading fakes through the routes.

    ``run_manager`` is the single :class:`~orch_hub.runs.RunManager` the run and
    escalation routes drive; it is built from ``resolved`` by default (with the
    push notifier from :func:`~orch_hub.notifications.build_notifier`, i.e. Bark
    when ``ORCH_HUB_BARK_URL`` is set, else disabled), and tests inject one wired
    with fake spawner/clock over a temp data dir so no real subprocess is ever
    started.

    ``intake_manager`` is the single :class:`~orch_hub.intake.IntakeManager` the
    intake routes drive; it is built from ``resolved`` by default with the
    concrete :class:`~orch_hub.intake_agent.ClaudeIntakeAgent` backend (a headless
    ``/intake-task`` driver over ``claude -p``). If ``claude`` is unavailable the
    drafting turn fails cleanly and the intake is recorded as ``failed`` — the
    same graceful, auditable outcome the earlier unavailable-agent default gave,
    now actually functional wherever the CLI is installed. It shares the run
    manager's push notifier (:func:`~orch_hub.notifications.build_notifier`), so
    a draft or revision reaching ``awaiting_review`` fires one metadata-only
    ``intake ready`` Bark push when ``ORCH_HUB_BARK_URL`` is set. Tests inject
    one wired with a fake agent so the read-only-until-confirm invariant is
    exercised through the HTTP surface without launching a real intake session.

    A :class:`~orch_hub.monitor.RunMonitor` over that manager is started on
    lifespan startup and stopped on shutdown, so run-ended / pending-question
    pushes fire without any dashboard or client polling. It is created here but
    only *started* by the lifespan, so merely building the app — the common test
    path, and the module-level ``app`` below — spawns no background thread; only
    running the lifespan (uvicorn, or ``with TestClient(app)``) does. The monitor
    is exposed on ``app.state.run_monitor`` for lifecycle introspection in tests.
    """
    resolved = config if config is not None else Config.from_env()
    manager = (
        run_manager
        if run_manager is not None
        else RunManager(resolved, notifier=build_notifier(resolved))
    )
    intake = (
        intake_manager
        if intake_manager is not None
        else IntakeManager(
            resolved,
            agent=ClaudeIntakeAgent(),
            notifier=build_notifier(resolved),
        )
    )
    # Optional task-evaluation subsystem (gated by ``Config.evaluation_enabled``).
    # The static evaluator wraps the same evaluation service the evidence/read
    # routes use, so injecting a fake ``evaluation_service`` alone (a hermetic
    # test with a fake git runner / record loader / clock) also drives the metrics
    # route through those fakes; a test wanting a deterministic report clock
    # injects ``static_evaluator`` too.
    evaluation = (
        evaluation_service
        if evaluation_service is not None
        else EvaluationService(resolved)
    )
    static_metrics = (
        static_evaluator
        if static_evaluator is not None
        else StaticEvaluator(resolved, evaluation_service=evaluation)
    )
    # The semantic (Layer 2) evaluator shares the same evaluation service + static
    # evaluator, so an injected fake service flows to all three layers. Its default
    # backend is the concrete Claude CLI driver (:class:`ClaudeSemanticBackend`,
    # read-only under ``--permission-mode plan``, model from ``ORCH_HUB_EVAL_MODEL``);
    # constructing it launches nothing, so building the app spawns no subprocess —
    # a semantic evaluation only reaches ``claude`` when the ``/semantic`` route is
    # hit. If ``claude`` is unavailable the turn fails cleanly as a 502. Tests inject
    # one wired with a fake backend so the read-only boundary is exercised over HTTP.
    semantic = (
        semantic_evaluator
        if semantic_evaluator is not None
        else SemanticEvaluator(
            resolved,
            evaluation_service=evaluation,
            static_evaluator=static_metrics,
            backend=ClaudeSemanticBackend(model=resolved.eval_model),
        )
    )
    monitor = RunMonitor(manager)

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        # Recover intake state before serving: an orphaned transient intake
        # record (a hub that died mid-turn) would hold its repo's intake slot
        # forever, so fail it once at startup while no turn is in flight. Then own
        # the monitor's lifetime with the service: start the background poll on
        # startup, stop it (joining the worker) on shutdown so the service leaves
        # no thread behind. The auth middleware passes the lifespan scope straight
        # through, so this runs regardless of the token.
        intake.reconcile_orphans()
        monitor.start()
        try:
            yield
        finally:
            monitor.stop()

    app = FastAPI(
        title="orch-hub",
        description="Local control plane for AI coding orchestrators.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.run_monitor = monitor

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

    # Intake-manager errors map to their HTTP shapes here, mirroring the run
    # mapping: 404 = no such intake; 409 = a state conflict the caller can act
    # on (an in-flight intake already holds the repo, a failed readiness gate, or
    # an operation illegal for the intake's current status). An agent-turn
    # failure and a lost session are deliberately NOT errors here — the manager
    # records them as a ``failed`` intake that the routes return with a success
    # code — so only the fail-closed gates reach these handlers.
    @app.exception_handler(UnknownIntakeError)
    async def _unknown_intake(
        request: Request, exc: UnknownIntakeError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(IntakeBusyError)
    async def _intake_busy(request: Request, exc: IntakeBusyError) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "intake_id": exc.intake_id}
        )

    @app.exception_handler(IntakeNotReadyError)
    async def _intake_not_ready(
        request: Request, exc: IntakeNotReadyError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "reasons": list(exc.reasons)}
        )

    @app.exception_handler(IntakeStateError)
    async def _intake_state(request: Request, exc: IntakeStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # The optional evaluation subsystem is off (``ORCH_HUB_EVAL_ENABLED`` falsy):
    # every evaluation action/read route is removed → 404 with the fail-closed
    # reason, matching the config contract. ``GET /api/evaluation`` never raises
    # this (it answers 200 ``{"enabled": false}``) so the dashboard can hide the
    # evaluation controls with a concise explanation instead of probing a 404.
    @app.exception_handler(EvaluationDisabledError)
    async def _evaluation_disabled(
        request: Request, exc: EvaluationDisabledError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # Default-deny bearer auth as pure ASGI middleware (see BearerAuthMiddleware
    # for why not BaseHTTPMiddleware): it wraps the whole app, so it protects
    # every current and future path — including the SSE log stream — the moment
    # it is mounted, and lets the streaming response pass through unbuffered.
    app.add_middleware(BearerAuthMiddleware, token=resolved.token)

    @app.get("/api/health")
    def health() -> dict:
        """Minimal public liveness probe.

        Deliberately free of configuration detail (it used to report
        ``ai_native_path``): this is the one unauthenticated route, so it
        must not disclose anything about the deployment.
        """
        return {"status": "ok"}

    # Public dashboard shell (see SHELL_ASSETS): the three static assets the
    # browser needs before it can attach the bearer token. A factory binds each
    # filename/media type so the loop variable is not captured late. They are
    # kept out of the OpenAPI schema (which is itself protected) and the auth
    # middleware allows exactly these paths unauthenticated. Every response
    # carries SHELL_CACHE_CONTROL so a new build ships to mobile browsers on the
    # next load instead of being masked by heuristic caching (see the constant).
    def _make_shell_route(filename: str, media_type: str):
        def serve_shell_asset() -> FileResponse:
            return FileResponse(
                WEB_DIR / filename,
                media_type=media_type,
                headers={"Cache-Control": SHELL_CACHE_CONTROL},
            )

        return serve_shell_asset

    for shell_path, (shell_file, shell_type) in SHELL_ASSETS.items():
        app.add_api_route(
            shell_path,
            _make_shell_route(shell_file, shell_type),
            methods=["GET"],
            include_in_schema=False,
        )

    @app.get("/api/repos")
    def get_repos() -> dict:
        """List managed repos with readiness, latest commit, and housekeeping.

        Identical to CLI ``repos --json`` (both go through ``repos_payload``):
        each repo carries readiness, its most recent ``commit`` (or ``null``
        when git could not be read), and ``housekeeping_pending`` from the repo
        marker file. Commit and housekeeping metadata are display-only and
        never change launch readiness.
        """
        repos = discover_repos(resolved)
        entries = [
            (
                repo,
                check_readiness(repo, resolved),
                latest_commit(repo),
                housekeeping_pending(repo),
            )
            for repo in repos
        ]
        return repos_payload(entries)

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

    @app.get("/api/repos/{repo_id}/tasks/{task_id}")
    def get_repo_task(repo_id: str, task_id: str) -> dict:
        """Return one task's inventory fields plus its raw markdown body.

        Backs the dashboard task-detail view, which needs the task file body the
        list endpoints deliberately omit. ``repo_id`` is the managed-repo name
        and ``task_id`` must be one listed in that repo's index (either unknown
        → 404). Status/summary/blockers come from the same
        frontmatter-authoritative inventory as ``/api/repos/{repo_id}/tasks``;
        ``content`` is the read-only ``.ai-tasks/<task_id>.md`` body, scoped
        inside the repo (clients never pass a path). ``present``/``content`` are
        ``false``/``null`` for a ``missing`` sentinel task whose file is absent.
        """
        repos = discover_repos(resolved)
        repo = next((r for r in repos if r.name == repo_id), None)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
        inventory = list_repo_tasks(repo)
        task = next((t for t in inventory.tasks if t.task_id == task_id), None)
        if task is None:
            raise HTTPException(
                status_code=404,
                detail=f"repo {repo_id!r} has no task {task_id!r}",
            )
        content = read_task_content(repo, task_id)
        return {**task.to_dict(), "present": content is not None, "content": content}

    @app.get("/api/repos/{repo_id}/commits")
    def get_repo_commits(repo_id: str, limit: int = DEFAULT_COMMIT_LIMIT) -> dict:
        """Return one repo's recent commits (newest first) for the detail view.

        ``repo_id`` is the managed-repo name (unknown → 404). ``limit`` is
        clamped by the git-meta reader to a sane range. ``available`` is False
        when git could not be read at all (missing git, not a work tree, or a
        failed ``git log``); the read is best-effort and never raises, so a git
        problem degrades to an empty ``available: false`` list rather than
        hiding the repo. The dashboard renders that as a graceful "commits
        unavailable" state.
        """
        repos = discover_repos(resolved)
        repo = next((r for r in repos if r.name == repo_id), None)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
        log = read_commits(repo, limit=limit)
        return {
            "repo": repo_id,
            "available": log.available,
            "commits": [commit.to_dict() for commit in log.commits],
        }

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

    @app.get("/api/history/runs")
    def list_run_history(limit: int = 50) -> dict:
        """Return recent runs newest-first with persisted audit events."""
        return {"runs": [entry.to_dict() for entry in manager.recent_runs(limit=limit)]}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        """Return one run's current :class:`RunStatus`; unknown ids are 404."""
        return manager.get_run(run_id).to_dict()

    @app.get("/api/runs/{run_id}/task")
    def get_run_task(run_id: str) -> dict:
        """Return the task file associated with a run, active or archived.

        Backs the run/audit UI's task link. Unlike the index-gated inventory
        routes (``/api/tasks``, ``/api/repos/{repo_id}/tasks/{task_id}``, which
        stay active-only), this resolves the task purely from the persisted run
        record (``repo_path`` + ``task_id``), so it keeps working after
        ``/ai-sync-v2`` close-out archives the task and drops it from the active
        index. It reads the active ``.ai-tasks/<task_id>.md`` first, then falls
        back to ``.ai-tasks/archive/<task_id>.md``; both reads keep the
        ``.ai-tasks`` containment protections and reject unsafe task ids, and
        there is deliberately no general archived-task *listing* — an archived
        task is reachable only through a run that references it.

        ``source`` is ``"active"``/``"archived"``/``null`` (null when neither is
        present); ``present``/``content`` mirror the other task routes. Unknown
        run ids are 404 (via the run manager); a broken run record with no
        repo/task resolves to ``present: false`` rather than an error.
        """
        record = manager.get_run(run_id).record  # UnknownRunError → 404
        repo = record.managed_repo()
        source = None
        content = None
        if repo is not None and record.task_id:
            content = read_task_content(repo, record.task_id)
            if content is not None:
                source = "active"
            else:
                content = read_archived_task_content(repo, record.task_id)
                if content is not None:
                    source = "archived"
        return {
            "run_id": run_id,
            "task_id": record.task_id,
            "source": source,
            "present": content is not None,
            "content": content,
        }

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

    @app.get("/api/runs/{run_id}/sessions")
    def get_run_sessions(run_id: str) -> dict:
        """Return one run's per-session context summary (live-parsed or persisted).

        Resolves the run by ``run_id`` (unknown → 404), then returns its ordered
        per-session summaries plus the context-budget reference. A live run is
        parsed from its current orchestrator log on demand (located from the run
        record, never a client path); an ended run is served from the snapshot
        persisted at its end, so the summary survives later cleanup of the
        repo-side log. A run with no log yet, or one whose log was already cleaned,
        is a 200 with an empty ``sessions`` list — missing data is graceful, never
        a 5xx.
        """
        snapshot = manager.read_sessions(run_id)  # UnknownRunError → 404
        return {"run_id": run_id, **snapshot.to_dict()}

    @app.get("/api/runs/{run_id}/logs")
    def get_run_logs(run_id: str, tail: int = DEFAULT_TAIL_LINES) -> dict:
        """Return a bounded recent tail of one run's orchestrator log.

        The run is resolved by ``run_id`` (unknown → 404); its log file is then
        located from the run record alone (``log_dir``/``task_id``/
        ``started_ts``), never a client path. ``tail`` caps the returned lines
        and is clamped to a sane range. A run with no log yet, or one whose file
        rotated away, is a 200 with ``present: false`` and a ``detail`` — a
        missing log is normal, not an error.

        When no orchestrator log is located, ``diagnostic`` carries a bounded
        tail of the run's ``spawn.out`` startup capture (its ``source``) so a
        run that failed *before* opening its log — a credential/PATH/missing-CLI
        error — is a useful error tail rather than an empty view. It is ``null``
        when an orchestrator log is present or the capture is absent/empty.
        """
        status = manager.get_run(run_id)
        snapshot = read_recent(status.record, max_lines=tail)
        diagnostic = None
        if not snapshot.present:
            diag = read_spawn_out(status.record, max_lines=tail)
            if diag.present:
                diagnostic = {**diag.to_dict(), "source": "spawn.out"}
        return {"run_id": run_id, **snapshot.to_dict(), "diagnostic": diagnostic}

    @app.get("/api/runs/{run_id}/logs/stream")
    def stream_run_logs(run_id: str, tail: int = DEFAULT_TAIL_LINES) -> StreamingResponse:
        """Live-tail one run's orchestrator log as Server-Sent Events.

        Resolves the run by ``run_id`` (unknown → 404), then follows its located
        log file: a bounded initial tail of ``tail`` lines (same clamp as the
        recent-log route), then one ``data:`` event per newly appended line, then
        a terminal ``event: end`` when the run finishes or the log rotates away.
        Liveness is re-checked from the run manager each poll, so the stream
        terminates on its own once the run ends — an already-ended run streams
        its final log and closes.

        Auth is the standard bearer header (the default-deny middleware covers
        this path too), so a browser client tails it with ``fetch`` plus a
        stream reader, not ``EventSource`` (which cannot set ``Authorization``);
        the token is never taken from the URL. ``Cache-Control``/
        ``X-Accel-Buffering`` ask any intermediary not to buffer the stream.
        """
        record = manager.get_run(run_id).record  # UnknownRunError → 404

        def is_live() -> bool:
            try:
                current = manager.get_run(run_id).record
            except UnknownRunError:
                return False
            return current.ended_ts is None and current.error is None

        return StreamingResponse(
            tail_events(record, is_live=is_live, initial_lines=tail),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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
        ``seq`` within its control dir (unknown → 404). An explicit ``req.mode``
        is reconciled with the question's ``kind`` by
        :func:`orch_hub.control_dir.normalize_answer` — a fail-closed guard that
        either yields the exact text to write or rejects the request (400) so a
        button's intent can never silently land on the wrong side of the
        orchestrator parser. An omitted ``mode`` (``None``) declares no button
        intent and is written verbatim, preserving the pre-``mode`` behavior for
        legacy clients (back-compat). Either way the text goes through
        :func:`orch_hub.control_dir.write_answer`, so an empty/whitespace answer
        is rejected (400), a pre-existing valid answer is refused (409); ``"stop"``
        is a legitimate answer that aborts the run.
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
            if req.mode is None:
                # No declared button intent: preserve the pre-``mode`` path and
                # write the answer verbatim (back-compat). write_answer still
                # rejects an empty/whitespace answer.
                answer = req.answer
            else:
                answer = normalize_answer(
                    req.answer, mode=req.mode, kind=question.kind
                )
            write_answer(question, answer, responder=responder)
        except ValueError as exc:
            # AnswerModeError (guard) and write_answer's empty-answer ValueError
            # both surface as a 400 with an actionable detail.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        manager.audit_answer(run_id, question, responder=responder, mode=req.mode)
        return {
            "run_id": run_id,
            "seq": seq,
            "answered": True,
            "responder": responder,
            "answer": answer.strip(),
        }

    # -- remote intake-task ----------------------------------------------------
    #
    # Thin adapters over the app's single IntakeManager (like the run routes over
    # RunManager). The load-bearing contract is unchanged across the HTTP surface:
    # only ``confirm`` reaches the agent's write path, so a drafted, revised,
    # cancelled, or failed intake never modifies the target repo. Agent turns run
    # off the manager lock, so a mutating route returns the transient record
    # (drafting/revising/confirming) at once and the client polls
    # ``GET /api/intakes/{id}`` for the settled outcome; reads never block behind
    # an in-flight turn. Gate refusals (repo busy, readiness, illegal state,
    # unknown id) surface as the mapped 4xx above; a lost session is a ``failed``
    # record returned with a success code, not a 5xx.

    @app.post("/api/intakes", status_code=201)
    def start_intake(req: StartIntakeRequest) -> dict:
        """Start a remote intake for ``repo_id``, returning the transient record.

        Resolves ``repo_id`` by managed-repo name (unknown → 404) and delegates
        to :meth:`IntakeManager.start_intake`, which fail-closed gates on a
        non-empty request (empty → 400), one in-flight intake per repo (busy →
        409), and readiness (not ready → 409) before drafting. The drafting turn
        runs off the manager lock, so the response is the transient
        :class:`~orch_hub.models.IntakeRecord` (``status: drafting``, no draft
        yet) returned immediately; the client then polls
        ``GET /api/intakes/{id}`` to observe the settled ``awaiting_review``
        draft, or a ``failed`` record when the drafting turn (including the
        default unconfigured backend) failed — a failed draft is an auditable
        outcome, not an error. The intake resource is created either way, hence
        201; the ``status`` field carries the current lifecycle state.
        """
        repos = discover_repos(resolved)
        repo = next((r for r in repos if r.name == req.repo_id), None)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {req.repo_id}")
        try:
            record = intake.start_intake(repo, req.request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return record.to_dict()

    @app.get("/api/intakes")
    def list_intakes() -> dict:
        """List every known intake (oldest first); ``agent_ref`` stays internal."""
        return {"intakes": [record.to_dict() for record in intake.list_intakes()]}

    @app.get("/api/intakes/{intake_id}")
    def get_intake(intake_id: str) -> dict:
        """Return one intake's current record (draft + status); unknown → 404."""
        return intake.get_intake(intake_id).to_dict()

    @app.get("/api/intakes/{intake_id}/history")
    def get_intake_history(intake_id: str) -> dict:
        """Return one intake's record plus its metadata-only audit events.

        Backs the review UI's audit trail. Events never carry an unconfirmed
        draft's body, so history stays reconstructable without treating a draft
        as a task. Unknown ids are 404.
        """
        return intake.intake_history(intake_id).to_dict()

    @app.post("/api/intakes/{intake_id}/revise")
    def revise_intake(intake_id: str, req: ReviseIntakeRequest) -> dict:
        """Send review feedback back to the intake session for a fresh draft.

        Legal only from ``awaiting_review`` (any other status → 409); empty
        feedback → 400. Still read-only: the revise turn runs off the lock and
        the revised draft replaces the current one and bumps ``iteration``
        without touching the target repo. The response is the transient
        ``revising`` record (200) returned immediately; poll ``GET
        /api/intakes/{id}`` for the settled ``awaiting_review`` draft. A lost
        session (fail-closed before any turn) resolves to a ``failed`` record
        (200), not a 5xx. Unknown ids are 404.
        """
        try:
            record = intake.revise_intake(intake_id, req.feedback)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return record.to_dict()

    @app.post("/api/intakes/{intake_id}/confirm")
    def confirm_intake(intake_id: str) -> dict:
        """Confirm the current draft, letting the session perform the write.

        The sole route that reaches the agent's write path, so it is the only way
        an intake creates the ``.ai-tasks/`` task file and index row. Legal only
        from ``awaiting_review`` (else 409); unknown ids are 404. The write turn
        runs off the lock, so the response is the transient ``confirming`` record
        (200) returned immediately; poll ``GET /api/intakes/{id}`` for the settled
        outcome. On success that record carries ``created_task_id`` and the new
        ``pending`` task is then visible through the existing task-inventory
        routes (``GET /api/repos/{repo_id}/tasks``) — intake does not duplicate
        task listing. A lost session (fail-closed before any turn) or a write
        failure resolves to a ``failed`` record with no task left behind by the
        manager.
        """
        return intake.confirm_intake(intake_id).to_dict()

    @app.post("/api/intakes/{intake_id}/cancel")
    def cancel_intake(intake_id: str) -> dict:
        """Cancel a non-confirmed intake, leaving the target repo unchanged.

        Legal from ``drafting``/``awaiting_review``/``revising`` (a terminal or
        mid-write ``confirming`` intake → 409); unknown ids are 404. Cancellation
        never writes to the repo and never needs a live session, so it always
        succeeds for a cancellable intake.
        """
        return intake.cancel_intake(intake_id).to_dict()

    # -- task evaluation -------------------------------------------------------
    #
    # Read-only sidecar over the optional evaluation subsystem (gated by
    # ``Config.evaluation_enabled``). "Read-only" is w.r.t. the managed repos:
    # generation writes only hub-owned artifacts under ``<data_dir>/evaluations``,
    # never the target task file, run records, ``.ai/``, or ``.ai-tasks/`` — so
    # these routes are a sidecar view that can never block normal run control.
    # When the subsystem is disabled every route below except availability 404s
    # via the EvaluationDisabledError handler; ``require_evaluation_enabled`` runs
    # that gate before repo resolution so the disabled signal is uniform. The
    # ``.enabled`` source of truth is the (possibly injected) evaluation service's
    # config, so availability, the gate, and the service agree.
    #
    # ``task_id`` is deliberately NOT index-gated (unlike the ``/tasks/`` routes):
    # an evaluation targets an exact active *or archived* task id — the archived
    # case is how a completed task, dropped from the active index at close-out, is
    # still evaluable — so resolution goes through the service's active-then-archive
    # resolver, kept in a distinct ``/evaluations/`` subtree to make that explicit.

    def require_evaluation_enabled() -> None:
        if not evaluation.enabled:
            raise EvaluationDisabledError(
                "task evaluation is disabled (ORCH_HUB_EVAL_ENABLED)"
            )

    def resolve_repo(repo_id: str) -> ManagedRepo:
        repo = next((r for r in discover_repos(resolved) if r.name == repo_id), None)
        if repo is None:
            raise HTTPException(status_code=404, detail=f"unknown repo: {repo_id}")
        return repo

    @app.get("/api/evaluation")
    def evaluation_availability() -> dict:
        """Report whether the optional task-evaluation subsystem is enabled.

        The one evaluation route that answers even when disabled (200,
        ``{"enabled": false}``), so the dashboard hides/disables the evaluation
        controls with a concise explanation rather than probing an action route
        and handling its 404. Protected like every non-health route.
        """
        return {"enabled": evaluation.enabled}

    @app.get("/api/evaluation/doctor")
    def evaluation_doctor() -> dict:
        """Report Layer 2 semantic-evaluator readiness (a config/availability probe).

        Delegates to :meth:`SemanticEvaluator.doctor`: a cheap check (no model
        call, no cost) of whether the semantic backend can actually run — e.g. the
        Claude CLI resolves on PATH — plus the evaluator identity. The dashboard
        calls it to disable the Layer 2 control with an explanation when the backend
        is unconfigured while keeping Layer 1 usable, and ``orch-hub eval doctor``
        smoke checks the same thing. Enabled-gated like every action route
        (disabled → 404 via the ``EvaluationDisabledError`` handler); it deliberately
        does NOT gate any run Start/Stop/log flow. Shape:
        ``{"enabled": true, "semantic": SemanticDoctor}``.
        """
        require_evaluation_enabled()
        return {"enabled": True, "semantic": semantic.doctor().to_dict()}

    @app.get("/api/repos/{repo_id}/evaluations/{task_id}")
    def list_task_evaluations(repo_id: str, task_id: str) -> dict:
        """List a task's persisted evaluations plus its resolved target state.

        ``repo_id`` is the managed-repo name (unknown → 404); ``task_id`` resolves
        active-then-archive, not through the active index. ``target`` carries only
        ``present``/``source`` (``active``/``archived``/``null`` — the task
        status/archive-state signal; the full body stays on the existing task
        routes), degrading to ``null``/``null`` if the task file is genuinely
        unreadable so a target IO error never blanks the list. ``evaluations`` are
        newest-first ``{evaluation_id, has_evidence, has_static_metrics,
        has_semantic_report, evaluator}`` summaries (``evaluator`` is the
        best-effort ``{backend, model, schema_version}`` read from a present
        semantic report so the picker can compare runs by model/version, else
        ``null``) and ``latest_evaluation_id`` names the newest — ``null`` with an
        empty list when none exist yet, a normal 200 (a never-evaluated task is
        not an error). An unsafe ``task_id`` path segment → 400.
        """
        require_evaluation_enabled()
        repo = resolve_repo(repo_id)
        try:
            stored = evaluation.list_evaluations(repo, task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            target = evaluation.resolve_target(repo, task_id)
            target_info = {"present": target.present, "source": target.source}
        except OSError:
            # A genuine target-file IO error (not mere absence) must not blank the
            # evaluation list, which is this route's primary payload.
            target_info = {"present": None, "source": None}

        def _evaluator_of(entry) -> dict | None:
            # Best-effort model/version identity for the history picker's
            # comparison, read at the presentation layer (list_evaluations stays a
            # pure structural manifest). Only touched when a semantic report is
            # present; any read/parse failure degrades to None so a corrupt or
            # unreadable report never 500s the newest-first list (its primary
            # payload) — the detail route is where a corrupt artifact surfaces.
            if SEMANTIC_REPORT_NAME not in entry.artifacts:
                return None
            try:
                report = evaluation.read_artifact(
                    repo, task_id, entry.evaluation_id, SEMANTIC_REPORT_NAME
                )
            except Exception:
                return None
            if not isinstance(report, dict):
                return None
            ev = report.get("evaluator")
            if not isinstance(ev, dict):
                return None
            return {
                "backend": ev.get("backend"),
                "model": ev.get("model"),
                "schema_version": ev.get("schema_version"),
            }

        return {
            "repo": repo_id,
            "task_id": task_id,
            "enabled": True,
            "target": target_info,
            "latest_evaluation_id": stored[0].evaluation_id if stored else None,
            "evaluations": [_evaluation_summary(s, _evaluator_of(s)) for s in stored],
        }

    @app.get("/api/repos/{repo_id}/evaluations/{task_id}/{evaluation_id}")
    def get_task_evaluation(
        repo_id: str, task_id: str, evaluation_id: str
    ) -> dict:
        """Return one persisted evaluation's full artifacts for ``(repo, task)``.

        ``evidence`` is the bounded evidence pack, ``static_metrics`` the Layer 1
        report, ``semantic_report`` the Layer 2 model judgment, and ``report_md``
        its human-readable render; each is ``null`` when that artifact was not
        generated (an evidence-only evaluation has evidence but no reports). The set
        backs both the metrics/semantic views and the local-inspection download
        affordances. ``repo_id`` unknown → 404; an ``evaluation_id`` with no
        persisted dir → 404 (kept distinct from a present dir missing one artifact,
        which is a ``null`` field). An unsafe ``task_id`` → 400; a corrupt persisted
        artifact surfaces as a 500 rather than being masked as "not generated".
        """
        require_evaluation_enabled()
        repo = resolve_repo(repo_id)
        try:
            stored = next(
                (
                    s
                    for s in evaluation.list_evaluations(repo, task_id)
                    if s.evaluation_id == evaluation_id
                ),
                None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if stored is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"repo {repo_id!r} task {task_id!r} has no evaluation"
                    f" {evaluation_id!r}"
                ),
            )
        # Read only the artifacts the dir actually holds (task_id already validated
        # by list_evaluations above); a corrupt file's decode error rides out to a
        # 500 here, deliberately not caught as the 400 that path-safety uses.
        evidence = (
            evaluation.read_artifact(repo, task_id, evaluation_id, EVIDENCE_NAME)
            if EVIDENCE_NAME in stored.artifacts
            else None
        )
        static = (
            evaluation.read_artifact(
                repo, task_id, evaluation_id, STATIC_METRICS_NAME
            )
            if STATIC_METRICS_NAME in stored.artifacts
            else None
        )
        semantic = (
            evaluation.read_artifact(
                repo, task_id, evaluation_id, SEMANTIC_REPORT_NAME
            )
            if SEMANTIC_REPORT_NAME in stored.artifacts
            else None
        )
        report_md = (
            evaluation.read_text_artifact(
                repo, task_id, evaluation_id, SEMANTIC_REPORT_MD_NAME
            )
            if SEMANTIC_REPORT_MD_NAME in stored.artifacts
            else None
        )
        return {
            "evaluation_id": evaluation_id,
            "evidence": evidence,
            "static_metrics": static,
            "semantic_report": semantic,
            "report_md": report_md,
        }

    @app.post(
        "/api/repos/{repo_id}/evaluations/{task_id}/evidence", status_code=201
    )
    def generate_task_evidence(repo_id: str, task_id: str) -> dict:
        """Generate a fresh evidence pack for ``(repo, task)`` and return it.

        Delegates to :meth:`EvaluationService.generate_evidence`: mints an
        evaluation id, assembles the bounded read-only pack from the task file and
        its related run records, and persists ``evidence.json`` under
        ``<data_dir>/evaluations`` — writing only hub-owned artifacts, never the
        target task or run state. ``repo_id`` unknown → 404; an unsafe id → 400;
        disabled → 404. Returns the new ``evaluation_id`` and the pack (201).
        """
        require_evaluation_enabled()
        repo = resolve_repo(repo_id)
        try:
            pack = evaluation.generate_evidence(repo, task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"evaluation_id": pack.evaluation_id, "evidence": pack.to_dict()}

    @app.post(
        "/api/repos/{repo_id}/evaluations/{task_id}/metrics", status_code=201
    )
    def generate_task_metrics(repo_id: str, task_id: str) -> dict:
        """Generate a fresh Layer 1 static-metrics report for ``(repo, task)``.

        Delegates to :meth:`StaticEvaluator.generate_metrics`, which builds a fresh
        evidence pack and persists both ``evidence.json`` and
        ``static_metrics.json`` in one evaluation dir — the deterministic refresh
        path, hub-owned artifacts only, target task/run state untouched, no
        semantic model call. ``repo_id`` unknown → 404; an unsafe id → 400;
        disabled → 404. Returns the new ``evaluation_id`` and the report (201); its
        paired evidence is then readable through the detail route.
        """
        require_evaluation_enabled()
        repo = resolve_repo(repo_id)
        try:
            report = static_metrics.generate_metrics(repo, task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "evaluation_id": report.evaluation_id,
            "static_metrics": report.to_dict(),
        }

    @app.post(
        "/api/repos/{repo_id}/evaluations/{task_id}/semantic", status_code=201
    )
    def generate_task_semantic(repo_id: str, task_id: str) -> dict:
        """Generate a fresh Layer 2 semantic evaluation for ``(repo, task)``.

        Delegates to :meth:`SemanticEvaluator.evaluate`, which mints one
        self-contained evaluation dir holding ``evidence.json`` +
        ``static_metrics.json`` + ``semantic_report.json`` + ``report.md``, driving
        the configured read-only Claude CLI backend. Hub-owned artifacts only; the
        target task/run state and ``.ai/``/``.ai-tasks/`` are never touched, and the
        cleanup is atomic so a failed turn leaves nothing behind. ``repo_id`` unknown
        → 404; an unsafe id → 400; disabled → 404; a backend/model failure or a
        non-conforming model response → 502 (the upstream evaluator failed), with no
        persisted artifact. Returns the new ``evaluation_id`` and the report (201);
        its paired evidence/static/report.md are then readable through the detail
        route.
        """
        require_evaluation_enabled()
        repo = resolve_repo(repo_id)
        try:
            report = semantic.evaluate(repo, task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SemanticEvaluationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "evaluation_id": report.evaluation_id,
            "semantic_report": report.to_dict(),
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
