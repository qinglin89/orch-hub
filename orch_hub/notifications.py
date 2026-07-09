"""User-facing push notifications for orch-hub lifecycle events.

The hub notifies the operator's mobile device when a run reaches a terminal
state (and, in a later slice, when a run needs human input) so the operator does
not have to watch the dashboard. Bark (https://github.com/Finb/Bark) is the v1
backend; the :class:`Notifier` protocol keeps room for later ones (an IM bot).

Two invariants shape this module:

- It **formats and delivers** notifications; it does not decide *when* one fires.
  The run manager owns firing (mirroring how it owns when history events are
  written), so trigger policy stays in one place.
- Delivery is **best-effort and isolating**. A backend may raise (network down,
  bad Bark key); the run manager delivers through an isolating dispatch that
  swallows the failure, so a dropped notification never corrupts run.json,
  events.jsonl, or the control-dir channel.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .models import Question, RunRecord

if TYPE_CHECKING:
    from .config import Config

DEFAULT_TIMEOUT = 5.0
"""Seconds a Bark POST may block before it is abandoned.

Bounded so a hung or unreachable Bark server cannot stall the delivering thread
indefinitely. Delivery is best-effort, so a timeout is just a dropped
notification, never a run-state failure.
"""


@dataclass(frozen=True)
class Notification:
    """One backend-agnostic push notification.

    ``group`` lets a backend cluster related notifications — Bark threads them by
    this string — so the hub sets it to the repo name and a device shows one
    thread per repo.
    """

    title: str
    body: str
    group: str | None = None

    def to_dict(self) -> dict:
        payload = {"title": self.title, "body": self.body}
        if self.group:
            payload["group"] = self.group
        return payload


class Notifier(Protocol):
    """A push backend.

    ``notify`` may raise (delivery is inherently unreliable); the run manager
    isolates the call so a failure never propagates into run state.
    """

    def notify(self, notification: Notification) -> None: ...


Transport = Callable[[str, bytes, float], None]
"""Performs the actual POST ``(url, json_payload, timeout)``.

Injectable so tests assert on the request without touching the network."""


def _urllib_post(url: str, payload: bytes, timeout: float) -> None:
    """Default transport: POST ``payload`` as JSON, draining the response.

    Raises ``urllib.error.URLError`` / ``HTTPError`` / ``socket.timeout`` on
    failure; the caller (the run manager's isolating dispatch) swallows it.
    """
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


class BarkNotifier:
    """Bark push backend.

    Bark accepts ``POST <server>/<device-key>`` with a JSON body
    (``{"title", "body", "group"}``); ``base_url`` is that full endpoint (server
    plus device key). The device key is a secret, so the URL is never logged.
    The POST is synchronous — the run manager delivers it off its critical path —
    and ``transport`` is injectable so tests assert on the request payload
    without a real network call.
    """

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport = _urllib_post,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        url = base_url.strip()
        if not url:
            raise ValueError("BarkNotifier requires a non-empty base_url")
        self._url = url
        self._transport = transport
        self._timeout = timeout

    def notify(self, notification: Notification) -> None:
        payload = json.dumps(notification.to_dict()).encode("utf-8")
        self._transport(self._url, payload, self._timeout)


def build_notifier(config: "Config") -> Notifier | None:
    """Build the configured notifier, or ``None`` when notifications are off.

    ``None`` (not a no-op object) is the single "disabled" signal the run manager
    checks, so an unconfigured hub spawns no delivery work at all — one mechanism
    for off, not a null-object plus an on/off flag.
    """
    if config.bark_url:
        return BarkNotifier(config.bark_url)
    return None


# Terminal run states, mirrored from ``orch_hub.runs.STATE_*``. They are not
# imported (runs imports this module — importing back would cycle); this map is
# the presentation layer over those classification strings.
_END_LABELS = {
    "completed": "run completed",
    "stopped": "run stopped",
    "error": "run failed",
    "ended": "run ended",
}


def run_end_notification(
    record: RunRecord, state: str, detail: str | None
) -> Notification:
    """Format the notification for a run that reached a terminal ``state``.

    Covers the run-ended, run-errored, and close-out-done triggers in one shape:
    ``state`` is the run manager's end classification — ``completed`` = the
    task's final gate passed or close-out archived it, ``error`` = a nonzero exit
    with no stop evidence, ``stopped`` / ``ended`` = the remaining terminal
    cases. ``detail`` is the classification's human explanation.
    """
    label = _END_LABELS.get(state, "run ended")
    repo = _repo_label(record)
    body = record.task_id or "(unknown task)"
    if detail:
        body = f"{body}\n{detail}"
    return Notification(title=f"{repo}: {label}", body=body, group=repo)


def question_notification(
    repo_name: str, task_id: str, question: Question
) -> Notification:
    """Format the notification for a newly observed pending question.

    The run-manager wiring that fires this (per-question, de-duplicated so a
    single question notifies once) is a later slice; the formatter lives here now
    so both the terminal-state and question surfaces share one notification
    shape.
    """
    repo = repo_name or "?"
    prompt = question.message or question.banner or f"question #{question.seq}"
    body = f"{task_id or '(unknown task)'}\n{prompt}"
    return Notification(title=f"{repo}: needs input", body=body, group=repo)


def _repo_label(record: RunRecord) -> str:
    if record.repo_name:
        return record.repo_name
    if record.repo_path is not None:
        return record.repo_path.name
    return "?"
