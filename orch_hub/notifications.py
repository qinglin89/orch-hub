"""User-facing push notifications for orch-hub lifecycle events.

The hub notifies the operator's mobile device when a run reaches a terminal
state (and, in a later slice, when a run needs human input) so the operator does
not have to watch the dashboard. Bark (https://github.com/Finb/Bark) is the v1
backend; the :class:`Notifier` protocol keeps room for later ones (an IM bot).

Two invariants shape this module:

- It **formats and delivers** notifications; it does not decide *when* one fires.
  Each owning manager fires (mirroring how the run manager owns when history
  events are written), so trigger policy stays with the state it observes.
- Delivery is **best-effort and isolating**. A backend may raise (network down,
  bad Bark key); each manager delivers through an isolating dispatch that
  swallows the failure, so a dropped notification never corrupts run.json,
  events.jsonl, job.json, or the control-dir channel.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .models import IntakeRecord, Question, RunRecord, SemanticJob

if TYPE_CHECKING:
    from .config import Config

DEFAULT_TIMEOUT = 5.0
"""Seconds a Bark POST may block before it is abandoned.

Bounded so a hung or unreachable Bark server cannot stall the delivering thread
indefinitely. Delivery is best-effort, so a timeout is just a dropped
notification, never a run-state failure.
"""

PLAN_GATE_KIND = "plan-gate"
"""Control-dir ``kind`` for a ``--plan-gate`` confirmation question.

Mirrors ``control_dir.QUESTION_KINDS`` as a local literal (this presentation
module deliberately does not import the adapter, the same way ``_END_LABELS``
mirrors ``runs.STATE_*`` rather than importing it). ``kind`` is the stable
contract for spotting a plan-gate question; the banner *text* is not, so
formatting keys off this, never off substring-sniffing the plan report.
"""

QUESTION_PROMPT_MAX_CHARS = 500
"""Max characters of question prompt carried in a push body.

Push delivery (Bark → APNs) silently drops a payload past a few KB, and one
plan-gate banner alone can reach ~12 KB (``PLAN_GATE_BANNER_CHARS`` in the
orchestrator), so an unbounded prompt loses the whole notification. A plan-gate
prompt is replaced with a fixed compact summary and never reaches this bound;
every other prompt is truncated to it. 500 chars stays far under the payload
ceiling even in multi-byte UTF-8, still fits a lock-screen preview, and the
full text remains in the dashboard, the control-dir question, and the log.
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

    The run manager fires this from its status scan when it first sees a pending
    control-dir question, de-duplicated per ``(run_id, seq)`` so a single
    question notifies once. It shares the terminal-state shape: ``title`` names
    the repo, ``body`` is the task id plus the question prompt, and ``group``
    clusters a repo's notifications into one device thread.

    The prompt is size-guarded so the push always delivers: a ``plan-gate``
    banner (the full multi-KB plan report) is replaced with a fixed compact
    summary, and every other prompt is bounded to
    ``QUESTION_PROMPT_MAX_CHARS``. The complete plan/prompt stays in the
    dashboard, the control-dir question file, and the orchestrator log.
    """
    repo = repo_name or "?"
    body = f"{task_id or '(unknown task)'}\n{_question_prompt(question)}"
    return Notification(title=f"{repo}: needs input", body=body, group=repo)


def _question_prompt(question: Question) -> str:
    """Return the size-bounded prompt text for a question push.

    A ``plan-gate`` question's banner is the full plan report — too large to
    deliver — so it collapses to a fixed summary that only announces a plan is
    waiting. Any other prompt keeps its useful leading content, truncated to
    ``QUESTION_PROMPT_MAX_CHARS`` (with an ellipsis) so an oversized banner (a
    traceback, a long ruling) still delivers. The result never exceeds that
    bound.
    """
    if question.kind == PLAN_GATE_KIND:
        return "plan ready for review"
    prompt = question.message or question.banner or f"question #{question.seq}"
    if len(prompt) <= QUESTION_PROMPT_MAX_CHARS:
        return prompt
    return prompt[:QUESTION_PROMPT_MAX_CHARS].rstrip() + "…"


def intake_ready_notification(record: IntakeRecord) -> Notification:
    """Format the notification for an intake draft/revision ready for review.

    The intake manager fires this from a finalize step when a draft or revision
    settles to ``awaiting_review``, so the operator learns the intake agent has
    produced a proposal without watching the dashboard. It shares the shape of
    the other formatters: ``title`` names the repo, ``group`` clusters a repo's
    notifications into one device thread, and ``body`` carries only intake
    identity and status metadata — the intake id and whether this is the initial
    draft or a numbered revision (``record.iteration``: 0 = initial draft).

    Deliberately metadata-only: the operator's request, the proposed task body,
    and any internal agent handle stay out of the push by construction — the
    formatter never reads those fields. The complete proposal stays in the
    dashboard intake-review view and the hub-owned intake record.
    """
    repo = record.repo_name or "?"
    if record.iteration > 0:
        descriptor = f"revision {record.iteration} ready for review"
    else:
        descriptor = "draft ready for review"
    body = f"{record.intake_id}\n{descriptor}"
    return Notification(title=f"{repo}: intake ready", body=body, group=repo)


# Terminal semantic-job statuses, mirrored from ``orch_hub.semantic_jobs.STATUS_*``
# for the same reason as ``_END_LABELS``: that module imports this one, so the
# labels are a local presentation map, not an import. ``error`` (an unreadable
# record) and the transient statuses are deliberately absent — the job manager
# never fires for them.
_SEMANTIC_JOB_LABELS = {
    "completed": "evaluation completed",
    "failed": "evaluation failed",
}


def semantic_job_notification(job: SemanticJob) -> Notification:
    """Format the notification for a semantic-evaluation job that settled.

    The semantic-job manager fires this once from the finalize step that writes
    a terminal ``completed``/``failed`` record, so an operator who started a
    Layer 2 evaluation and navigated away learns the outcome without keeping the
    dashboard open. Same shape as the other formatters: ``title`` names the repo
    and the outcome, ``group`` clusters a repo's notifications into one device
    thread, and ``body`` carries the task id plus — on ``completed`` — the linked
    ``evaluation_id`` the dashboard selects.

    Deliberately metadata-only: the prompt, the evidence, the report, and the
    failure diagnostic (``job.error``, which may quote a backend stderr tail)
    stay out of the push by construction — the formatter never reads those
    fields. A failed job's diagnostic stays in the dashboard and the hub-owned
    job record.
    """
    label = _SEMANTIC_JOB_LABELS.get(job.status, "evaluation ended")
    repo = job.repo_name or (job.repo_path.name if job.repo_path else "?")
    body = job.task_id or "(unknown task)"
    if job.evaluation_id:
        body = f"{body}\nreport {job.evaluation_id}"
    return Notification(title=f"{repo}: {label}", body=body, group=repo)


def probe_notification() -> Notification:
    """Format the harmless probe sent by ``orch-hub notify test``.

    A backend-agnostic delivery check: it distinguishes "push is configured"
    from "push delivery actually works". Like the other formatters this only
    builds the payload — the CLI delivers it directly (not through the run
    manager's trigger path), so it touches no run/history/control-dir state.
    """
    return Notification(
        title="orch-hub: test notification",
        body="If you can see this, push delivery is working.",
    )


def _repo_label(record: RunRecord) -> str:
    if record.repo_name:
        return record.repo_name
    if record.repo_path is not None:
        return record.repo_path.name
    return "?"
