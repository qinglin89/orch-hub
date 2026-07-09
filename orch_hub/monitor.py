"""Resident background monitor that drives the run-manager status scan.

Why this exists: run-lifecycle and pending-question push notifications
(:mod:`orch_hub.notifications`, fired by :class:`orch_hub.runs.RunManager`) only
happen when something calls the manager's status path — ``_refresh`` observes a
live->ended transition, ``_status`` observes a newly pending question. Until
this module those calls came *only* from HTTP reads (``/api/runs``,
``/api/escalations``, ``/api/history/runs``), so a run that ended or asked a
question while no dashboard tab or client was polling produced no push: the
operator had to keep the dashboard open for a push to be timely, which defeats
the point of push.

:class:`RunMonitor` closes that gap with the smallest possible mechanism: one
daemon thread that calls ``RunManager.list_runs()`` on a bounded, low-frequency
interval. ``list_runs()`` is exactly the scan the dashboard already triggers, so
the monitor reuses the manager's existing trigger policy and its per-
``(run_id, seq)`` de-dup wholesale — it changes *when* the scan runs, never
*what* it decides. It is deliberately not an event bus and owns no run state:
``run.json`` / ``events.jsonl`` persistence stays entirely in the run manager.

The monitor is started and stopped by the FastAPI application lifecycle
(:mod:`orch_hub.api`), so it lives exactly as long as the resident service and
never runs during ordinary unit tests (which build the app without triggering
the lifespan).
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

_log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 30.0
"""Seconds between background status scans.

Bounded and low-frequency on purpose: the only thing waiting on a scan is a push
(a run ending, a question appearing), and neither is sub-second urgent, so a
~30s worst-case latency is a fine trade for negligible idle load. A scan is the
same filesystem walk ``/api/runs`` already performs, so the monitor is no
heavier than a single dashboard client polling at this cadence.
"""

JOIN_TIMEOUT = 5.0
"""Seconds :meth:`RunMonitor.stop` waits for the scan thread before giving up.

A scan is bounded filesystem work (notification *delivery* is dispatched off the
scan thread by the run manager), so a thread parked in the interval wait or
mid-scan joins well within this. The thread is a daemon, so even a missed join
never blocks interpreter shutdown.
"""


class RunScanner(Protocol):
    """The one slice of :class:`orch_hub.runs.RunManager` the monitor drives.

    ``list_runs`` refreshes every run's live/end state and, as a side effect the
    run manager owns, fires the terminal-state and pending-question
    notifications. The monitor only needs to call it; tests inject a fake.
    """

    def list_runs(self) -> object: ...


class RunMonitor:
    """Periodically drive ``scanner.list_runs()`` on a background daemon thread.

    :meth:`start` / :meth:`stop` are both idempotent, so wiring the monitor to
    the app lifespan cannot spawn duplicate workers even if startup fires more
    than once, and ``stop`` without a prior ``start`` is a safe no-op. Each scan
    is best-effort: any exception it raises is logged and swallowed so one bad
    scan never kills the thread or the service (notification-delivery failures
    are already isolated inside the run manager, so they never reach here).
    """

    def __init__(self, scanner: RunScanner, *, interval: float = DEFAULT_INTERVAL):
        self._scanner = scanner
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the scan thread; a no-op when one is already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # Clear a stop signal left by a previous stop() so a restarted
            # monitor's first wait() is a real interval, not an instant exit.
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="orch-hub-monitor", daemon=True
            )
            self._thread.start()
            _log.info("run monitor started (interval=%.0fs)", self._interval)

    def stop(self, *, timeout: float = JOIN_TIMEOUT) -> None:
        """Signal the scan thread to exit and wait (bounded) for it to finish."""
        with self._lock:
            thread = self._thread
            self._thread = None
            if thread is None:
                return
            self._stop.set()
        # Join outside the lock: the thread may be mid-scan and a scan does not
        # need the monitor lock, so holding it here would only serialize need-
        # lessly against a concurrent start().
        thread.join(timeout)
        _log.info("run monitor stopped")

    def _run(self) -> None:
        # Event.wait(interval) is the interruptible sleep: it returns True the
        # instant stop() sets the event (so shutdown is prompt) and False on
        # timeout (time for the next scan). Waiting *before* the first scan means
        # construction/startup does no filesystem work; the first push can lag
        # one interval, which is within the latency this deliberately trades for.
        while not self._stop.wait(self._interval):
            try:
                self._scanner.list_runs()
            except Exception:
                # Best-effort: a scan failure (an unexpected OS/filesystem error;
                # Bark failures are already swallowed inside the run manager) must
                # not kill the loop or the service. The traceback is safe to log —
                # nothing on this path carries the Bark device-key secret.
                _log.exception("run monitor scan failed; continuing")
