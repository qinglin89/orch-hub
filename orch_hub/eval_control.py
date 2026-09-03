"""Hub-owned pause switch, and the drain it used to hold.

It existed for one job: stop the automatic Layer 2 dispatch sweep, live, without
the plist edit and restart ``ORCH_HUB_EVAL_AUTO`` required — and without that
switch's cost, which was that turning it off stopped completions being *accepted*
at all rather than merely holding them. A pause held only the drain, so the queue
kept its shape and resuming spent it in completion order.

There is no drain now (:mod:`orch_hub.completion_eval` says why: a semantic turn
starts only when a person asks for one, so nothing is scheduled to be held). What
is left is a persisted operator-set flag: written by the HTTP control routes, read
by the queue projection, and consulted by nothing that decides whether a turn may
run — because nothing decides that. Its retirement, along with the routes and the
dashboard controls that reach it, is the next slice of that work.

State lives in one small JSON file, ``<data_dir>/eval-control.json``, beside the
other hub-owned stores (``runs/``, ``intake/``, ``semantic-jobs/``,
``evaluations/``). Persisted rather than in-memory because the reason to pause
outlives a process — an operator pausing for an afternoon's dev work must not have
it silently undone by a launchd restart — and hub-owned rather than per-record
because it is one global fact: stamping it onto every held job would churn the
whole queue on a toggle and leave records claiming a reason that is no longer true.

It is deliberately its own store rather than a field on the job manager. The job
manager owns one record per ``(repo, task)`` and a lock serializing attempts
against each other, neither of which a hub-wide switch shares. One file, one
shape, one owner, two readers (the queue projection and the CLI) and one writer
(the HTTP control routes).

**An unreadable control reads as paused.** The fail-closed direction, from when
this held real spending: read the other way, a deliberate pause evaporated on a
corrupt byte and turns were spent in the window the operator was protecting. Read
this way the damage is visible — the reason travels to every reader as
:data:`PAUSE_UNREADABLE` rather than as somebody's choice, and ``resume``
overwrites the damaged file. A *missing* file is not this case: never having
paused is the normal state and reads as running.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

from .models import EvaluationControl

EVAL_CONTROL_NAME = "eval-control.json"
"""The persisted control record, directly under ``Config.data_dir``.

A flat file rather than a directory: there is exactly one of these per hub, it has
no per-item identity to key on, and a dir holding a single fixed name would be a
layout that implies growth it will never have."""

PAUSE_OPERATOR = "operator"
"""Paused because somebody asked (the HTTP/dashboard control)."""

PAUSE_UNREADABLE = "unreadable-control"
"""Paused because the control record could not be read, so the pause is this
store failing closed rather than an operator's choice.

A distinct token for the same reason :mod:`orch_hub.eval_dispatch` keeps
``capacity`` and ``usage-unknown`` apart: they call for opposite responses. An
operator pause ends when the operator ends it; this one is damaged state that will
not fix itself, and every reader should say so rather than reporting a decision
nobody made."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class EvaluationControlStore:
    """Reads and writes the automatic-dispatch pause switch.

    ``state()`` is the read every consumer takes (cheap: one small file, no lock
    held across IO of any consequence); ``pause()`` and ``resume()`` are the two
    writes, both idempotent — pausing an already-paused queue keeps the original
    ``paused_ts`` rather than restamping it, so "paused since" answers when the
    drain actually stopped rather than when somebody last pressed the button.

    ``clock`` must return timezone-aware UTC datetimes, injected like every other
    hub store's clock so a test can assert the stamp.
    """

    def __init__(self, data_dir: Path, *, clock=_utc_now):
        self._path = data_dir / EVAL_CONTROL_NAME
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def state(self) -> EvaluationControl:
        """The current pause state; never raises.

        Missing file → running (the normal state: nobody has ever paused). Present
        but unreadable/corrupt/wrong-shaped → paused with :data:`PAUSE_UNREADABLE`,
        the fail-closed direction argued in the module docstring. A stored
        ``paused: false`` is running regardless of what else the file holds, so a
        resumed queue is never resurrected as paused by a leftover stamp.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return EvaluationControl()
        except OSError:
            return self._unreadable()
        try:
            data = json.loads(raw)
        except ValueError:
            return self._unreadable()
        if not isinstance(data, dict) or not isinstance(data.get("paused"), bool):
            return self._unreadable()
        if not data["paused"]:
            return EvaluationControl()
        ts = data.get("paused_ts")
        reason = data.get("reason")
        return EvaluationControl(
            paused=True,
            paused_ts=ts if isinstance(ts, str) and ts else None,
            # An unrecognized stored token is normalized to the operator pause it
            # can only have come from: this vocabulary is closed and only `pause()`
            # writes it, so a foreign value is a hand-edit, not a state to invent a
            # meaning for. The pause itself still stands — the file says paused.
            reason=reason if reason in (PAUSE_OPERATOR, PAUSE_UNREADABLE) else PAUSE_OPERATOR,
        )

    def pause(self) -> EvaluationControl:
        """Hold automatic dispatch; return the resulting state.

        Idempotent on the *stamp*: an already-paused queue is returned unchanged
        rather than restamped, so repeated presses (or a second browser session)
        cannot make a queue that has been held for hours look freshly paused. A
        queue paused only because its record was unreadable is rewritten here into
        a real operator pause — the file is repaired and the reason becomes true.
        """
        with self._lock:
            current = self.state()
            if current.paused and current.reason == PAUSE_OPERATOR:
                return current
            updated = EvaluationControl(
                paused=True,
                paused_ts=self._clock().isoformat(),
                reason=PAUSE_OPERATOR,
            )
            self._write(updated)
            return updated

    def resume(self) -> EvaluationControl:
        """Release automatic dispatch; return the resulting state.

        Always writes, even when the state already reads as running: this is the
        one operation that must repair an unreadable record (which reads as paused
        precisely so that it can be seen and cleared), and a resume that skipped
        the write against a corrupt file would report success while leaving the
        queue held.
        """
        with self._lock:
            updated = EvaluationControl()
            self._write(updated)
            return updated

    def _write(self, control: EvaluationControl) -> None:
        """Persist atomically (same-dir unique temp + rename).

        Uniquely named temp for the same reason
        :meth:`orch_hub.usage.ClaudeUsageStore.record` uses one: writes can arrive
        from concurrent request threads, and a shared temp name lets one writer's
        ``os.replace`` move the file out from under another. The temp is cleaned up
        on a failed write so it cannot accumulate in a long-lived service.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(
            f"{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            tmp.write_text(
                json.dumps(control.to_dict(), indent=1), encoding="utf-8"
            )
            os.replace(tmp, self._path)
        except BaseException:  # noqa: BLE001 - clean up the unique temp, then re-raise
            tmp.unlink(missing_ok=True)
            raise

    def _unreadable(self) -> EvaluationControl:
        return EvaluationControl(paused=True, reason=PAUSE_UNREADABLE)
