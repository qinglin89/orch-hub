"""Orchestrator run lifecycle: launch, list, stop, and reattach.

The run manager owns the run-dir layout under ``Config.data_dir`` and the
subprocess side of the orchestrator contract
(``.cursor/orchestrator/HANDOFF-orch-hub.md``; ``README.md`` §5-§7):

.. code-block:: text

    <data_dir>/runs/<run_id>/
        run.json     hub-owned run record (atomic rewrite on updates)
        control/     the --control-dir file channel, fresh per run
        spawn.out    subprocess stdout+stderr (startup-failure diagnosis)

Run dirs live outside every target repo working tree (an in-repo dir would
dirty the tree and fail the orchestrator's post-checks), and each run gets a
fresh dir: the orchestrator never deletes ``stop.flag``, so a reused dir
would stop a new run immediately.

Each run launches the target repo's own deployed orchestrator:

.. code-block:: text

    /usr/bin/caffeinate -i <repo>/.cursor/orchestrator/.venv/bin/python \
        <repo>/.cursor/orchestrator/orchestrator.py <task-id> \
        --control-dir <run_dir>/control [extra args]

with ``cwd=<repo>`` and ``start_new_session=True``: hub restarts never kill
runs, and ``caffeinate -i`` keeps the laptop awake for the run's duration
(caffeinate forwards the orchestrator's exit code, so its pid/status track
the run). Run state is file-backed — a fresh :class:`RunManager` reattaches
by scanning run dirs and probing pid liveness; the in-memory process handles
exist only to capture exit codes of runs that end while the hub is alive.
Runs that end while the hub is down keep ``exit_code=None`` and classify
from the control-dir stop signals and task frontmatter alone.

Launches fail closed: the existing readiness checker and task-store
inventory gate every start, and one repo never has two live runs. The one
known gap: if the hub dies in the instant between spawning and writing
``run.json``, the orphan dir surfaces as an ``error`` entry but cannot be
attributed to a repo, so it does not block launches — deliberate, since the
opposite failure (a crashed launch permanently marking the repo busy) is
worse. Pid liveness is a plain ``kill(pid, 0)`` probe; a pid recycled after
reboot can masquerade as a live run until its dir is cleaned up.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Protocol

from .config import Config
from .control_dir import pending_questions, request_stop, stop_answered, stop_requested
from .history import (
    KIND_ANSWER_RECORDED,
    KIND_RUN_ENDED,
    KIND_RUN_STARTED,
    KIND_STOP_REQUESTED,
    append_event,
    read_events,
)
from .models import (
    ManagedRepo,
    Question,
    ReadinessReport,
    RepoTaskInventory,
    RunEvent,
    RunHistoryEntry,
    RunRecord,
    RunStatus,
    SessionEst,
    SessionsSnapshot,
)
from .notifications import (
    Notification,
    Notifier,
    question_notification,
    run_end_notification,
)
from .orch_config import (
    read_effective_config_log,
    read_effective_config_snapshot,
    write_effective_config_snapshot,
)
from .readiness import check_readiness
from .sessions import read_live, read_snapshot, write_snapshot
from .task_store import (
    STATUS_MISSING,
    STATUS_UNKNOWN,
    list_repo_tasks,
    read_session_est,
)

_log = logging.getLogger(__name__)

RUNS_SUBDIR = "runs"
RUN_RECORD_NAME = "run.json"
CONTROL_SUBDIR = "control"
SPAWN_OUT_NAME = "spawn.out"

CAFFEINATE = ("/usr/bin/caffeinate", "-i")
"""Sleep-inhibiting wrapper (macOS, the v1 deployment target)."""

ORCHESTRATOR_SCRIPT = Path(".cursor") / "orchestrator" / "orchestrator.py"
ORCHESTRATOR_PYTHON = Path(".cursor") / "orchestrator" / ".venv" / "bin" / "python"
ORCHESTRATOR_LOG_DIR = Path(".cursor") / "orchestrator" / "logs"
"""Deployed-orchestrator paths relative to a target repo (readiness checks
the first two; the orchestrator writes ``<log dir>/<utc-ts>-<task-id>.log``)."""

USER_CLI_PATHS = (
    Path(".local") / "bin",
    Path(".claude") / "local",
    Path(".npm-global") / "bin",
    Path(".cargo") / "bin",
)
"""Common per-user install locations for agent CLIs such as claude/codex."""

SYSTEM_PATHS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
"""Baseline macOS paths for launchd-started services with sparse env."""

BLOCKED_STATUS = "blocked"
LAUNCHABLE_STATUSES = frozenset(
    {"pending", "in_progress", "final_review", "completed"}
)
"""Task frontmatter statuses the orchestrator attaches to without caveats
(``completed`` re-runs close-out). ``blocked`` needs acknowledgement; the
task-store sentinels and unrecognized statuses are rejected outright."""

STATE_ACTIVE = "active"
STATE_WAITING = "waiting"
STATE_COMPLETED = "completed"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"
STATE_ENDED = "ended"

_RUN_ID_TS = "%Y%m%dT%H%M%SZ"
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class RunManagerError(Exception):
    """Base class for run-manager operational errors."""


class LaunchBlockedError(RunManagerError):
    """A launch was refused by the pre-start gates (fail closed)."""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


class BlockedTaskError(RunManagerError):
    """The task is ``blocked`` and the caller did not acknowledge the caveat.

    Resuming a blocked task only works when its ``claimed-by`` session id is
    resumable by the orchestrator backend; a foreign (manual-session) id makes
    the orchestrator exit with guidance instead of resuming (README §5.4).
    The hub cannot tell the two apart, so every blocked launch requires
    explicit acknowledgement.
    """


class RepoBusyError(RunManagerError):
    """The target repo already has a live run (per-repo serialization)."""

    def __init__(self, repo_name: str, run_id: str):
        self.run_id = run_id
        super().__init__(f"repo {repo_name!r} already has active run {run_id}")


class UnknownRunError(RunManagerError):
    """No run dir exists for the given run id."""


class RunEndedError(RunManagerError):
    """The run already ended, so the requested operation is meaningless.

    Notably a stop: writing ``stop.flag`` post-mortem would misclassify the
    finished run as stopped on the next scan.
    """


class ProcessHandle(Protocol):
    """The slice of ``subprocess.Popen`` the manager relies on."""

    pid: int

    def poll(self) -> int | None: ...


Spawner = Callable[..., ProcessHandle]
"""Launches the detached subprocess; tests inject fakes.

Called as ``spawner(argv, cwd=Path, stdout=BinaryIO, env=dict,
start_new_session=True)``.
"""


def _default_spawner(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdout: BinaryIO,
    env: Mapping[str, str],
    start_new_session: bool,
) -> ProcessHandle:
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        start_new_session=start_new_session,
    )


def _pid_alive(pid: int) -> bool:
    """Probe pid liveness without signaling (used for reattached runs)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _orchestrator_env(
    environ: Mapping[str, str] | None = None, *, home: Path | None = None
) -> dict[str, str]:
    """Return the environment inherited by deployed orchestrator runs.

    launchd services often have a sparse PATH, while Claude/Codex CLIs are
    commonly installed under the user's home directory. The orchestrator
    launches those CLIs by name, so pass an explicit PATH that keeps the current
    environment but includes the common user and macOS locations.
    """
    env = dict(os.environ if environ is None else environ)
    resolved_home = home or Path.home()
    path_parts = [
        *(str(resolved_home / relative) for relative in USER_CLI_PATHS),
        *((env.get("PATH") or "").split(os.pathsep)),
        *SYSTEM_PATHS,
    ]
    env["PATH"] = os.pathsep.join(_dedupe_path(path_parts))
    return env


def _dedupe_path(parts: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        normalized = part.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _slug(value: str) -> str:
    """Make a repo/task name safe as a run-dir path component."""
    return _SLUG.sub("-", value).strip("-") or "x"


NotifyDispatch = Callable[[Callable[[], None]], None]
"""Runs a delivery callback. The default detaches it onto a daemon thread."""


def _thread_dispatch(deliver: Callable[[], None]) -> None:
    """Default dispatch: deliver on a short-lived daemon thread.

    Notification delivery is fired from inside the run-manager lock, but a slow
    or hung backend must never stall run operations, so delivery runs off the
    critical path. The thread is daemon so it never blocks interpreter shutdown.
    Tests inject a synchronous dispatch (``lambda deliver: deliver()``) for
    deterministic assertions.
    """
    threading.Thread(target=deliver, daemon=True, name="orch-hub-notify").start()


class RunManager:
    """Owns orchestrator subprocess lifecycle and per-repo serialization.

    One instance is meant to live inside the resident hub service. All
    collaborators are injectable for tests: ``readiness_checker`` /
    ``task_lister`` default to the real gates, ``spawner`` to a detached
    ``subprocess.Popen``, ``pid_probe`` to a ``kill(pid, 0)`` probe, and
    ``clock`` must return timezone-aware UTC datetimes.

    ``notifier`` is the optional push backend (``None`` disables notifications
    with no delivery work at all); it fires when a run reaches a terminal state
    and when a run's pending question is first observed (de-duplicated per
    ``(run_id, seq)``). ``notify_dispatch`` runs each delivery — a daemon thread
    by default so a slow backend never stalls run operations; tests inject a
    synchronous dispatch.
    """

    def __init__(
        self,
        config: Config,
        *,
        readiness_checker: Callable[[ManagedRepo, Config], ReadinessReport] = check_readiness,
        task_lister: Callable[[ManagedRepo], RepoTaskInventory] = list_repo_tasks,
        spawner: Spawner = _default_spawner,
        pid_probe: Callable[[int], bool] = _pid_alive,
        clock: Callable[[], dt.datetime] = _utc_now,
        notifier: Notifier | None = None,
        notify_dispatch: NotifyDispatch = _thread_dispatch,
    ):
        self._config = config
        self._readiness_checker = readiness_checker
        self._task_lister = task_lister
        self._spawner = spawner
        self._pid_probe = pid_probe
        self._clock = clock
        self._notifier = notifier
        self._notify_dispatch = notify_dispatch
        self._lock = threading.Lock()
        self._procs: dict[str, ProcessHandle] = {}
        # In-memory (run_id, seq) set of questions already pushed, so a pending
        # question notifies once per hub process; see _notify_new_questions.
        self._notified_questions: set[tuple[str, int]] = set()

    @property
    def runs_dir(self) -> Path:
        return self._config.data_dir / RUNS_SUBDIR

    # -- launch ---------------------------------------------------------------

    def start_run(
        self,
        repo: ManagedRepo,
        task_id: str,
        *,
        extra_args: Sequence[str] = (),
        acknowledge_blocked: bool = False,
    ) -> RunRecord:
        """Gate, spawn, and record one orchestrator run for ``repo``/``task_id``.

        Gates run cheapest-first and all fail closed: per-repo single-run,
        task inventory (frontmatter-authoritative status), then the full
        readiness checker. ``extra_args`` are appended to the orchestrator
        argv (e.g. ``--plan-gate``, ``--backend cc-codex``).
        """
        with self._lock:
            self._ensure_repo_free(repo)
            task_status = self._gate_task(repo, task_id, acknowledge_blocked)
            self._gate_readiness(repo)
            run_dir, run_id = self._create_run_dir(repo.name, task_id)
            control_dir = run_dir / CONTROL_SUBDIR
            argv = (
                *CAFFEINATE,
                str(repo.path / ORCHESTRATOR_PYTHON),
                str(repo.path / ORCHESTRATOR_SCRIPT),
                task_id,
                "--control-dir",
                str(control_dir),
                *(str(arg) for arg in extra_args),
            )
            # Stamp the start time *before* spawning. The orchestrator picks its
            # own second-precision log filename during its startup, which is
            # necessarily after this point, so floor(started_ts) <= that stamp
            # and logs.locate_log can never miss the run's own log. Stamping it
            # after the spawn let a launch that crossed a one-second boundary
            # record started_ts a second ahead of the child's stamp, hiding the
            # real log behind locate_log's floored lower bound.
            started_ts = self._clock().isoformat()
            try:
                with (run_dir / SPAWN_OUT_NAME).open("wb") as spawn_out:
                    proc = self._spawner(
                        list(argv),
                        cwd=repo.path,
                        stdout=spawn_out,
                        env=_orchestrator_env(),
                        start_new_session=True,
                    )
            except Exception:
                # The dir was created by this very call and holds nothing but
                # the empty channel; removing it keeps failed launches from
                # accumulating as broken run entries.
                shutil.rmtree(run_dir, ignore_errors=True)
                raise
            record = RunRecord(
                run_id=run_id,
                repo_name=repo.name,
                repo_path=repo.path,
                task_id=task_id,
                task_status=task_status,
                control_dir=control_dir,
                log_dir=repo.path / ORCHESTRATOR_LOG_DIR,
                spawn_out=run_dir / SPAWN_OUT_NAME,
                argv=argv,
                pid=proc.pid,
                started_ts=started_ts,
            )
            _write_record(run_dir, record)
            append_event(
                run_dir,
                _run_event(
                    record,
                    KIND_RUN_STARTED,
                    ts=record.started_ts,
                    task_status=task_status,
                ),
            )
            self._procs[run_id] = proc
            return record

    def _ensure_repo_free(self, repo: ManagedRepo) -> None:
        for run_dir in self._run_dirs():
            record = self._refresh(run_dir, _load_record(run_dir))
            if record.error or record.ended_ts is not None:
                continue
            if record.repo_path == repo.path:
                raise RepoBusyError(repo.name, record.run_id)

    def _gate_task(
        self, repo: ManagedRepo, task_id: str, acknowledge_blocked: bool
    ) -> str:
        inventory = self._task_lister(repo)
        if not inventory.index_present:
            raise LaunchBlockedError(
                [f"no .ai-tasks/index.md in {repo.path}"]
            )
        task = next(
            (t for t in inventory.tasks if t.task_id == task_id), None
        )
        if task is None:
            raise LaunchBlockedError(
                [f"task {task_id!r} is not listed in .ai-tasks/index.md"]
            )
        if task.status == STATUS_MISSING:
            raise LaunchBlockedError(
                [f"task file .ai-tasks/{task_id}.md is missing"]
            )
        if task.status == STATUS_UNKNOWN:
            raise LaunchBlockedError(
                [f"task file .ai-tasks/{task_id}.md has no parseable status"]
            )
        if task.status == BLOCKED_STATUS:
            if not acknowledge_blocked:
                raise BlockedTaskError(
                    f"task {task_id!r} is blocked"
                    f" (blockers: {', '.join(task.blockers) or 'none listed'})."
                    " If its claimed-by session id is foreign (a manual"
                    " session), the orchestrator exits with guidance instead"
                    " of resuming — unblock manually first, or acknowledge to"
                    " launch anyway (README §5.4)"
                )
        elif task.status not in LAUNCHABLE_STATUSES:
            raise LaunchBlockedError(
                [f"task {task_id!r} has unrecognized status {task.status!r}"]
            )
        return task.status

    def _gate_readiness(self, repo: ManagedRepo) -> None:
        report = self._readiness_checker(repo, self._config)
        if not report.ready:
            raise LaunchBlockedError(
                [f"{check.name}: {check.message}" for check in report.failures]
            )

    def _create_run_dir(self, repo_name: str, task_id: str) -> tuple[Path, str]:
        """Create a fresh, collision-free run dir plus its control subdir."""
        base = "-".join(
            (self._clock().strftime(_RUN_ID_TS), _slug(repo_name), _slug(task_id))
        )
        for attempt in itertools.count():
            run_id = base if attempt == 0 else f"{base}-{attempt + 1}"
            run_dir = self.runs_dir / run_id
            try:
                run_dir.mkdir(parents=True)
            except FileExistsError:
                continue
            (run_dir / CONTROL_SUBDIR).mkdir()
            return run_dir, run_id
        raise AssertionError("unreachable")

    # -- inspection -----------------------------------------------------------

    def list_runs(self) -> list[RunStatus]:
        """Return every known run, oldest first (run ids sort by start time).

        Scanning refreshes: an end observed here (via a held process handle
        or a dead pid) is persisted into the run's ``run.json``, so state
        survives hub restarts. This is also the reattach path — a fresh
        manager sees runs started by a previous hub process.
        """
        with self._lock:
            return [
                self._status(run_dir, self._refresh(run_dir, _load_record(run_dir)))
                for run_dir in self._run_dirs()
            ]

    def recent_runs(self, limit: int = 50) -> list[RunHistoryEntry]:
        """Return recent runs newest-first with audit events, session summary,
        and each run task's session estimate.

        Each entry embeds the run's persisted per-session context snapshot so the
        history view renders context strips without a per-run follow-up request,
        plus the run task's typed ``session-est`` for the est-vs-actual task
        rollup. The refresh runs first (it may persist the snapshot at a
        just-observed live->ended transition), then the snapshot is read. The est
        is read from the target repo's task file (active then archive), deduped
        per ``(repo, task)`` within the call — a task with many runs reads its
        frontmatter once — and fully isolated so a target-repo read failure
        degrades that task's est to unknown without touching history.
        """
        bounded = max(0, min(limit, 200))
        with self._lock:
            entries: list[RunHistoryEntry] = []
            est_cache: dict[tuple[str, str], SessionEst | None] = {}
            for run_dir in reversed(self._run_dirs()):
                record = self._refresh(run_dir, _load_record(run_dir))
                entries.append(
                    RunHistoryEntry(
                        status=self._status(run_dir, record),
                        events=read_events(run_dir),
                        sessions=self._history_sessions(run_dir),
                        session_est=self._history_session_est(record, est_cache),
                        effective_config=self._history_effective_config(run_dir),
                    )
                )
            return entries[:bounded]

    @staticmethod
    def _history_session_est(
        record: RunRecord, cache: dict[tuple[str, str], SessionEst | None]
    ) -> SessionEst | None:
        """The run task's typed ``session-est`` for the history rollup, cached.

        Fully isolated like the snapshot/notifier paths: a stub record with no
        repo, an empty task id, or a target-repo read failure degrades to
        ``None`` (unknown) and never raises into the history read. ``read_session_est``
        does not swallow IO errors, so the wrap here is the isolation boundary,
        mirroring :meth:`_snapshot_sessions`. Deduped per ``(repo_path, task_id)``
        so a task with many runs reads (or fails to read) its frontmatter once,
        including the failure verdict.
        """
        repo = record.managed_repo()
        if repo is None or not record.task_id:
            return None
        key = (str(repo.path), record.task_id)
        if key not in cache:
            try:
                cache[key] = read_session_est(repo, record.task_id)
            except Exception:
                # A target-repo read failure must never corrupt history; unknown
                # est is a graceful degradation. Log only the (non-secret) ids.
                _log.warning(
                    "session-est read failed for run %s task %s",
                    record.run_id,
                    record.task_id,
                )
                cache[key] = None
        return cache[key]

    def get_run(self, run_id: str) -> RunStatus:
        """Return one run's current status; unknown ids raise."""
        with self._lock:
            run_dir = self._run_dir_for(run_id)
            return self._status(run_dir, self._refresh(run_dir, _load_record(run_dir)))

    def read_sessions(self, run_id: str) -> SessionsSnapshot:
        """Return one run's per-session context summary.

        A live run is parsed from its current repo-side log; an ended run is
        served from the snapshot persisted at its end (which survives repo-side
        log cleanup), falling back to a live parse only when no snapshot exists (a
        run predating this field, or one whose end this hub never observed).
        Unknown ids raise :class:`UnknownRunError`. The resolve+refresh runs under
        the lock — brief, and the point where a just-observed end persists the
        snapshot — while the log parse / snapshot read runs outside it, like the
        logs route resolves via :meth:`get_run` then reads off the lock.
        """
        with self._lock:
            run_dir = self._run_dir_for(run_id)
            record = self._refresh(run_dir, _load_record(run_dir))
            live = record.error is None and record.ended_ts is None
        if live:
            return read_live(record)
        snapshot = read_snapshot(run_dir)
        return snapshot if snapshot is not None else read_live(record)

    def read_effective_config(self, run_id: str) -> dict | None:
        """Return one run's authoritative ``effective-config:`` launch snapshot.

        Mirrors :meth:`read_sessions`: a live run is parsed from its current
        repo-side log (the line is logged at startup, so it is present as soon as
        the log exists); an ended run is served from the ``effective_config.json``
        snapshot persisted at its end (which survives repo-side log cleanup),
        falling back to a live parse only for a pre-field run with no snapshot.
        Returns the raw JSON dict the orchestrator logged, or ``None`` when no
        snapshot is available. Unknown ids raise :class:`UnknownRunError`.
        """
        with self._lock:
            run_dir = self._run_dir_for(run_id)
            record = self._refresh(run_dir, _load_record(run_dir))
            live = record.error is None and record.ended_ts is None
        if live:
            return read_effective_config_log(record)
        snapshot = read_effective_config_snapshot(run_dir)
        return snapshot if snapshot is not None else read_effective_config_log(record)

    def stop_run(self, run_id: str) -> bool:
        """Request a graceful stop by writing the run's ``stop.flag``.

        One write covers both contract behaviors: at a session boundary the
        orchestrator stops gracefully (exit 0); while awaiting an answer it
        aborts like a ``stop`` answer (nonzero). Callers should know a stop
        during a ``convergence-budget``/``dispute-unresolved`` question
        discards the pending ruling (README §5.5). Returns True when the flag
        was newly written (False = already requested). A run that already
        ended raises :class:`RunEndedError` — flagging it post-mortem would
        rewrite history to "stopped".
        """
        with self._lock:
            run_dir = self._run_dir_for(run_id)
            record = self._refresh(run_dir, _load_record(run_dir))
            if record.error:
                raise RunEndedError(f"run {run_id} is not stoppable: {record.error}")
            if record.ended_ts is not None:
                raise RunEndedError(f"run {run_id} already ended")
            assert record.control_dir is not None
            newly_requested = request_stop(record.control_dir)
            if newly_requested:
                append_event(
                    run_dir,
                    _run_event(
                        record,
                        KIND_STOP_REQUESTED,
                        ts=self._clock().isoformat(),
                        detail="stop.flag written",
                    ),
                )
            return newly_requested

    def audit_answer(
        self,
        run_id: str,
        question: Question,
        *,
        responder: str,
        mode: str | None = None,
    ) -> None:
        """Persist answer audit metadata after a control-dir answer is written.

        The answer text is intentionally excluded: the control-dir answer file
        is the consumed contract artifact, while history keeps metadata for
        later inspection without copying potentially sensitive content. ``mode``
        is the answer's button intent (``answer``/``discuss``), recorded as
        metadata alongside the responder; it is never the answer text.
        """
        with self._lock:
            run_dir = self._run_dir_for(run_id)
            record = _load_record(run_dir)
            if record.error:
                raise UnknownRunError(f"run {run_id!r} has no readable record")
            append_event(
                run_dir,
                _run_event(
                    record,
                    KIND_ANSWER_RECORDED,
                    ts=self._clock().isoformat(),
                    seq=question.seq,
                    question_ts=question.ts,
                    question_kind=question.kind,
                    question_banner=question.banner,
                    question_message=question.message,
                    responder=responder,
                    mode=mode,
                ),
            )

    # -- internals ------------------------------------------------------------

    def _run_dirs(self) -> list[Path]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(p for p in self.runs_dir.iterdir() if p.is_dir())

    def _run_dir_for(self, run_id: str) -> Path:
        # Reject path-escaping ids up front; run ids are single dir names.
        run_dir = self.runs_dir / run_id
        if Path(run_id).name != run_id or not run_dir.is_dir():
            raise UnknownRunError(f"no run {run_id!r}")
        return run_dir

    def _refresh(self, run_dir: Path, record: RunRecord) -> RunRecord:
        """Observe process end once and persist it; no-op on settled records."""
        if record.error or record.ended_ts is not None:
            return record
        proc = self._procs.get(record.run_id)
        if proc is not None:
            exit_code = proc.poll()
            if exit_code is None:
                return record
            self._procs.pop(record.run_id)
            updated = replace(
                record, exit_code=exit_code, ended_ts=self._clock().isoformat()
            )
        elif record.pid is not None and self._pid_probe(record.pid):
            return record
        else:
            # Ended while this manager was not the parent (hub restart):
            # the exit status is unrecoverable, only the end itself is known.
            updated = replace(record, ended_ts=self._clock().isoformat())
        _write_record(run_dir, updated)
        append_event(
            run_dir,
            _run_event(
                updated,
                KIND_RUN_ENDED,
                ts=updated.ended_ts,
                exit_code=updated.exit_code,
            ),
        )
        # Snapshot the parsed per-session summary once, now that the process has
        # exited and its log is complete, so the summary survives later cleanup of
        # the repo-side log. Like the notification below, it runs a single time per
        # run (settled records return early above) and is fully isolated.
        self._snapshot_sessions(run_dir, updated)
        # Snapshot the run's authoritative effective-config line the same way, so
        # run history retains what actually launched even after the repo-side log
        # is cleaned; also isolated (a parse/IO failure never touches run state).
        self._snapshot_effective_config(run_dir, updated)
        # Fire exactly once, at the live->ended transition (this branch runs a
        # single time per run; settled records return early above). Classify
        # here so the notification carries the same completed/stopped/error/ended
        # verdict the dashboard shows.
        state, detail = self._classify_end(updated)
        self._emit(run_end_notification(updated, state, detail))
        return updated

    @staticmethod
    def _history_sessions(run_dir: Path) -> SessionsSnapshot:
        """The run's persisted per-session summary for the history view.

        History is snapshot-only and cheap: an ended run reads its small
        persisted ``sessions.json`` (written once at the live->ended transition,
        so it survives repo-side log cleanup); a live or pre-field run with no
        snapshot yet contributes an empty snapshot rather than triggering a live
        log parse under the lock. The on-demand run-detail route
        (:meth:`read_sessions`) is the path that live-parses and falls back for a
        still-running or pre-field run.
        """
        snapshot = read_snapshot(run_dir)
        return snapshot if snapshot is not None else SessionsSnapshot()

    @staticmethod
    def _history_effective_config(run_dir: Path) -> dict | None:
        """The run's persisted ``effective-config:`` snapshot for the history view.

        Snapshot-only and cheap, like :meth:`_history_sessions`: an ended run reads
        its small ``effective_config.json`` (written once at the live->ended
        transition); a live or pre-field run with no snapshot contributes ``None``
        rather than triggering a live log parse under the lock. The on-demand
        detail route (:meth:`read_effective_config`) is the path that live-parses.
        """
        return read_effective_config_snapshot(run_dir)

    def _snapshot_sessions(self, run_dir: Path, record: RunRecord) -> None:
        """Persist the run's per-session summary at the live->ended transition.

        Isolation is the invariant, mirroring :meth:`_emit`: a parse or IO failure
        here must never propagate into run state. The log is read once (a bounded
        streaming scan) and the snapshot written atomically; on any failure the run
        still ends cleanly with no snapshot, and an ended run with no snapshot
        falls back to a live parse on read (:meth:`read_sessions`).
        """
        try:
            write_snapshot(run_dir, read_live(record))
        except Exception:
            # Never let snapshotting touch run state; a missing snapshot is a
            # graceful degradation, not an error. Log only the (non-secret) run id.
            _log.warning("session snapshot failed for run %s", record.run_id)

    def _snapshot_effective_config(self, run_dir: Path, record: RunRecord) -> None:
        """Persist the run's ``effective-config:`` snapshot at the live->ended
        transition, mirroring :meth:`_snapshot_sessions`.

        Isolated the same way: parse the authoritative startup line from the log
        (falling back to ``spawn.out``) and, when present, write it beside
        ``run.json`` so history keeps what actually launched after the repo-side
        log is cleaned. A run that never logged the line (an early startup failure)
        simply gets no snapshot; any parse/IO failure degrades to no snapshot and
        never propagates into run state.
        """
        try:
            data = read_effective_config_log(record)
            if data is not None:
                write_effective_config_snapshot(run_dir, data)
        except Exception:
            _log.warning(
                "effective-config snapshot failed for run %s", record.run_id
            )

    def _emit(self, notification: Notification) -> None:
        """Deliver a notification best-effort; never raise into run state.

        Isolation is the invariant: a backend failure (network down, bad Bark
        key) must not corrupt run.json / events.jsonl / the control-dir channel.
        Delivery runs through the injected dispatch (a daemon thread in
        production) and the backend call is wrapped so any exception is logged,
        not propagated. ``None`` notifier short-circuits — no dispatch work.
        """
        notifier = self._notifier
        if notifier is None:
            return

        def deliver() -> None:
            # Isolate every backend failure: delivery is best-effort and must
            # never propagate into run state.
            try:
                notifier.notify(notification)
            except Exception as exc:
                # Log the exception *type* and the (non-secret) title only; a
                # Bark URL embeds a device key, so never log the transport
                # detail that could carry it.
                _log.warning(
                    "notification delivery failed (%s): %s",
                    type(exc).__name__,
                    notification.title,
                )

        self._notify_dispatch(deliver)

    def _notify_new_questions(
        self, record: RunRecord, pending: Sequence[Question]
    ) -> None:
        """Push a ``needs input`` notification for each newly seen question.

        Observed here because ``_status`` already scans ``pending_questions`` to
        decide active vs waiting — the notification rides that existing scan
        instead of adding a second watcher. Firing is driven by whatever runs the
        status scan: the resident background poll
        (:class:`orch_hub.monitor.RunMonitor`, added so pushes fire without
        dashboard traffic) plus every on-demand status query (``/api/runs``,
        ``/api/escalations``, ``/api/history/runs``) — exactly like the
        terminal-state push in ``_refresh``.

        De-dup is the in-memory ``(run_id, seq)`` set, mutated only here and only
        under the run-manager lock every ``_status`` caller holds, so a question
        notifies once per hub process. It is intentionally not persisted: after a
        restart a *still-open* question re-notifies once — an answered question
        is no longer pending, so it never re-fires — a useful reminder rather
        than a broken guarantee. The set only holds seqs actually observed (a
        handful per run) and is dropped on restart, so it needs no reclamation.
        A ``None`` notifier short-circuits so a disabled hub does no bookkeeping.
        """
        if self._notifier is None:
            return
        for question in pending:
            key = (record.run_id, question.seq)
            if key in self._notified_questions:
                continue
            self._notified_questions.add(key)
            self._emit(
                question_notification(record.repo_name, record.task_id, question)
            )

    def _status(self, run_dir: Path, record: RunRecord) -> RunStatus:
        if record.error:
            return RunStatus(record=record, state=STATE_ERROR, detail=record.error)
        assert record.control_dir is not None
        if record.ended_ts is None:
            pending = pending_questions(record.control_dir)
            self._notify_new_questions(record, pending)
            # Derived on every status read (not cached client-side), so a stop
            # requested earlier stays visible across dashboard reloads until the
            # run actually ends and re-classifies. Terminal runs skip this: their
            # stop, if any, is already carried by _classify_end's stopped state.
            return RunStatus(
                record=record,
                state=STATE_WAITING if pending else STATE_ACTIVE,
                pending=len(pending),
                stop_requested=stop_requested(record.control_dir),
            )
        state, detail = self._classify_end(record)
        return RunStatus(record=record, state=state, detail=detail)

    def _classify_end(self, record: RunRecord) -> tuple[str, str | None]:
        """Interpret a finished run per the contract (README §5).

        Exit 0 is ambiguous (task done vs graceful boundary stop vs a
        ``--once`` session), so classification prefers evidence: task
        frontmatter/archival first, then the hub's own stop signals, then the
        bare exit code. ``exit_code=None`` (ended while the hub was down)
        classifies from the file evidence alone.
        """
        done_detail = self._task_done_detail(record)
        if done_detail is not None:
            return STATE_COMPLETED, done_detail
        flagged = stop_requested(record.control_dir)
        if flagged or stop_answered(record.control_dir):
            if record.exit_code == 0:
                detail = "stopped gracefully at a session boundary"
            elif record.exit_code is None:
                detail = "stop requested; exit status unknown (ended while the hub was down)"
            elif flagged:
                detail = "aborted by stop request while a question was pending"
            else:
                detail = "stopped by a human 'stop' answer"
            return STATE_STOPPED, detail
        if record.exit_code == 0:
            return (
                STATE_ENDED,
                "exited 0 without completing the task (e.g. a --once session)",
            )
        if record.exit_code is None:
            return STATE_ENDED, "ended while the hub was down; exit status unknown"
        return STATE_ERROR, f"exited {record.exit_code}"

    def _task_done_detail(self, record: RunRecord) -> str | None:
        """Task-frontmatter evidence that the run finished its task.

        ``completed`` status means the final gate passed (close-out may still
        be pending); the id vanishing from a present index means close-out
        archived it. The repo is rebuilt from the record via
        :meth:`RunRecord.managed_repo`.
        """
        repo = record.managed_repo()
        if repo is None or not record.task_id:
            return None
        inventory = self._task_lister(repo)
        if not inventory.index_present:
            return None
        task = next(
            (t for t in inventory.tasks if t.task_id == record.task_id), None
        )
        if task is None:
            return "task archived by close-out"
        if task.status == "completed":
            return "task completed (close-out may still be pending)"
        return None


def _write_record(run_dir: Path, record: RunRecord) -> None:
    """Persist ``run.json`` atomically (same-dir tmp + rename)."""
    payload = {
        "run_id": record.run_id,
        "repo_name": record.repo_name,
        "repo_path": str(record.repo_path) if record.repo_path else None,
        "task_id": record.task_id,
        "task_status": record.task_status,
        "control_dir": str(record.control_dir) if record.control_dir else None,
        "log_dir": str(record.log_dir) if record.log_dir else None,
        "spawn_out": str(record.spawn_out) if record.spawn_out else None,
        "argv": list(record.argv),
        "pid": record.pid,
        "started_ts": record.started_ts,
        "ended_ts": record.ended_ts,
        "exit_code": record.exit_code,
    }
    path = run_dir / RUN_RECORD_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_records(config: Config) -> list[tuple[Path, RunRecord]]:
    """Read every persisted run record read-only, as ``(run_dir, record)`` pairs.

    Oldest-first (run ids sort by start time). Unlike :meth:`RunManager.list_runs`,
    this performs **no** refresh: it never probes pid liveness, observes a run's
    end, rewrites ``run.json``, appends events, or fires notifications. It is the
    read-only entry point for historical consumers — the task-evaluation evidence
    builder aggregates related runs by ``(repo_path, task_id)`` through it and
    must not mutate run state. An unreadable ``run.json`` still yields an
    ``error`` stub (via :func:`_load_record`), so a broken dir surfaces rather
    than silently vanishing from an evidence pack; a missing ``runs/`` dir is an
    empty list. The ``run_dir`` is returned alongside each record because
    per-run history (``events.jsonl``), the session snapshot (``sessions.json``),
    and the startup capture (``spawn.out``) are all read from it.
    """
    runs_dir = config.data_dir / RUNS_SUBDIR
    if not runs_dir.is_dir():
        return []
    return [
        (run_dir, _load_record(run_dir))
        for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir())
    ]


def _load_record(run_dir: Path) -> RunRecord:
    """Load ``run.json``; an unreadable one yields an ``error`` stub.

    Stubs surface broken dirs instead of hiding them (a hub crash between
    spawn and record write leaves exactly this), but carry no repo
    attribution, so they never block launches.
    """
    path = run_dir / RUN_RECORD_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _stub(run_dir, "missing run.json (crashed launch or foreign dir)")
    except (OSError, ValueError) as err:
        return _stub(run_dir, f"unreadable run.json ({err})")
    if not isinstance(data, dict):
        return _stub(run_dir, "run.json is not a JSON object")
    argv = data.get("argv")
    return RunRecord(
        run_id=_str_field(data, "run_id") or run_dir.name,
        repo_name=_str_field(data, "repo_name") or "",
        repo_path=_path_field(data, "repo_path"),
        task_id=_str_field(data, "task_id") or "",
        task_status=_str_field(data, "task_status"),
        control_dir=_path_field(data, "control_dir") or run_dir / CONTROL_SUBDIR,
        log_dir=_path_field(data, "log_dir"),
        # Fall back to the fixed dir layout so records predating the field (and
        # reattached runs) still resolve their startup-diagnostic capture.
        spawn_out=_path_field(data, "spawn_out") or run_dir / SPAWN_OUT_NAME,
        argv=tuple(str(a) for a in argv) if isinstance(argv, list) else (),
        pid=_int_field(data, "pid"),
        started_ts=_str_field(data, "started_ts"),
        ended_ts=_str_field(data, "ended_ts"),
        exit_code=_int_field(data, "exit_code"),
    )


def _stub(run_dir: Path, error: str) -> RunRecord:
    # A crashed launch (missing/unreadable run.json) may still have written a
    # spawn.out; keep the fixed path so the logs route can surface it.
    return RunRecord(
        run_id=run_dir.name,
        control_dir=run_dir / CONTROL_SUBDIR,
        spawn_out=run_dir / SPAWN_OUT_NAME,
        error=error,
    )


def _str_field(data: dict, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _path_field(data: dict, key: str) -> Path | None:
    value = _str_field(data, key)
    return Path(value) if value else None


def _int_field(data: dict, key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _run_event(
    record: RunRecord,
    kind: str,
    *,
    ts: str | None,
    task_status: str | None = None,
    state: str | None = None,
    detail: str | None = None,
    exit_code: int | None = None,
    seq: int | None = None,
    question_ts: str | None = None,
    question_kind: str | None = None,
    question_banner: str | None = None,
    question_message: str | None = None,
    responder: str | None = None,
    mode: str | None = None,
) -> RunEvent:
    """Build an audit event from persisted run identity."""
    return RunEvent(
        ts=ts or "",
        kind=kind,
        run_id=record.run_id,
        repo_name=record.repo_name,
        repo_path=record.repo_path,
        task_id=record.task_id,
        task_status=task_status if task_status is not None else record.task_status,
        state=state,
        detail=detail,
        exit_code=exit_code,
        seq=seq,
        question_ts=question_ts,
        question_kind=question_kind,
        question_banner=question_banner,
        question_message=question_message,
        responder=responder,
        mode=mode,
    )
