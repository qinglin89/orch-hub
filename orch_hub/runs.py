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
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Protocol

from .config import Config
from .control_dir import pending_questions, request_stop, stop_answered, stop_requested
from .models import (
    ManagedRepo,
    ReadinessReport,
    RepoTaskInventory,
    RunRecord,
    RunStatus,
)
from .readiness import check_readiness
from .task_store import STATUS_MISSING, STATUS_UNKNOWN, list_repo_tasks

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

Called as ``spawner(argv, cwd=Path, stdout=BinaryIO, start_new_session=True)``.
"""


def _default_spawner(
    argv: Sequence[str], *, cwd: Path, stdout: BinaryIO, start_new_session: bool
) -> ProcessHandle:
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd),
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


def _slug(value: str) -> str:
    """Make a repo/task name safe as a run-dir path component."""
    return _SLUG.sub("-", value).strip("-") or "x"


class RunManager:
    """Owns orchestrator subprocess lifecycle and per-repo serialization.

    One instance is meant to live inside the resident hub service. All
    collaborators are injectable for tests: ``readiness_checker`` /
    ``task_lister`` default to the real gates, ``spawner`` to a detached
    ``subprocess.Popen``, ``pid_probe`` to a ``kill(pid, 0)`` probe, and
    ``clock`` must return timezone-aware UTC datetimes.
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
    ):
        self._config = config
        self._readiness_checker = readiness_checker
        self._task_lister = task_lister
        self._spawner = spawner
        self._pid_probe = pid_probe
        self._clock = clock
        self._lock = threading.Lock()
        self._procs: dict[str, ProcessHandle] = {}

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
            self._gate_task(repo, task_id, acknowledge_blocked)
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
            try:
                with (run_dir / SPAWN_OUT_NAME).open("wb") as spawn_out:
                    proc = self._spawner(
                        list(argv),
                        cwd=repo.path,
                        stdout=spawn_out,
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
                control_dir=control_dir,
                log_dir=repo.path / ORCHESTRATOR_LOG_DIR,
                argv=argv,
                pid=proc.pid,
                started_ts=self._clock().isoformat(),
            )
            _write_record(run_dir, record)
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
    ) -> None:
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

    def get_run(self, run_id: str) -> RunStatus:
        """Return one run's current status; unknown ids raise."""
        with self._lock:
            run_dir = self._run_dir_for(run_id)
            return self._status(run_dir, self._refresh(run_dir, _load_record(run_dir)))

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
            return request_stop(record.control_dir)

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
        return updated

    def _status(self, run_dir: Path, record: RunRecord) -> RunStatus:
        if record.error:
            return RunStatus(record=record, state=STATE_ERROR, detail=record.error)
        assert record.control_dir is not None
        if record.ended_ts is None:
            pending = pending_questions(record.control_dir)
            return RunStatus(
                record=record,
                state=STATE_WAITING if pending else STATE_ACTIVE,
                pending=len(pending),
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
        archived it. The repo is rebuilt from the record — ``manifest`` is the
        deployment-standard path and is not read by the task store.
        """
        if record.repo_path is None or not record.task_id:
            return None
        repo = ManagedRepo(
            name=record.repo_name or record.repo_path.name,
            path=record.repo_path,
            manifest=record.repo_path / ".ai-deploy-manifest.json",
        )
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
        "control_dir": str(record.control_dir) if record.control_dir else None,
        "log_dir": str(record.log_dir) if record.log_dir else None,
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
        control_dir=_path_field(data, "control_dir") or run_dir / CONTROL_SUBDIR,
        log_dir=_path_field(data, "log_dir"),
        argv=tuple(str(a) for a in argv) if isinstance(argv, list) else (),
        pid=_int_field(data, "pid"),
        started_ts=_str_field(data, "started_ts"),
        ended_ts=_str_field(data, "ended_ts"),
        exit_code=_int_field(data, "exit_code"),
    )


def _stub(run_dir: Path, error: str) -> RunRecord:
    return RunRecord(
        run_id=run_dir.name,
        control_dir=run_dir / CONTROL_SUBDIR,
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
