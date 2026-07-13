"""Tests for the notification backends and event formatting.

The Bark backend is exercised with an injected transport that records the
request, so no test ever touches the network. Formatting is tested as pure
functions over the domain models.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.models import Question, RunRecord
from orch_hub.notifications import (
    QUESTION_PROMPT_MAX_CHARS,
    BarkNotifier,
    Notification,
    build_notifier,
    probe_notification,
    question_notification,
    run_end_notification,
)

BARK_URL = "https://bark.example/DEVICEKEY"


class RecordingTransport:
    """Captures ``(url, payload, timeout)`` and can be told to fail."""

    def __init__(self, *, fail: Exception | None = None):
        self.calls: list[tuple[str, bytes, float]] = []
        self._fail = fail

    def __call__(self, url: str, payload: bytes, timeout: float) -> None:
        self.calls.append((url, payload, timeout))
        if self._fail is not None:
            raise self._fail


# -- Notification shape --------------------------------------------------------


def test_notification_to_dict_omits_absent_group():
    assert Notification(title="t", body="b").to_dict() == {"title": "t", "body": "b"}


def test_notification_to_dict_includes_group():
    assert Notification(title="t", body="b", group="repo-a").to_dict() == {
        "title": "t",
        "body": "b",
        "group": "repo-a",
    }


# -- BarkNotifier --------------------------------------------------------------


def test_bark_notifier_posts_json_payload_to_the_configured_url():
    transport = RecordingTransport()
    notifier = BarkNotifier(BARK_URL, transport=transport, timeout=2.5)

    notifier.notify(Notification(title="repo-a: run completed", body="task-x", group="repo-a"))

    assert len(transport.calls) == 1
    url, payload, timeout = transport.calls[0]
    assert url == BARK_URL
    assert timeout == 2.5
    assert json.loads(payload.decode("utf-8")) == {
        "title": "repo-a: run completed",
        "body": "task-x",
        "group": "repo-a",
    }


def test_bark_notifier_strips_surrounding_whitespace_from_url():
    transport = RecordingTransport()
    BarkNotifier(f"  {BARK_URL}\n", transport=transport).notify(
        Notification(title="t", body="b")
    )
    assert transport.calls[0][0] == BARK_URL


def test_bark_notifier_rejects_blank_url():
    with pytest.raises(ValueError, match="base_url"):
        BarkNotifier("   ")


def test_bark_notifier_propagates_transport_failure():
    # The backend itself surfaces failures; the run manager is what isolates
    # them (covered in test_runs). A direct caller sees the error.
    transport = RecordingTransport(fail=OSError("connection refused"))
    with pytest.raises(OSError, match="connection refused"):
        BarkNotifier(BARK_URL, transport=transport).notify(
            Notification(title="t", body="b")
        )


# -- build_notifier ------------------------------------------------------------


def test_build_notifier_returns_bark_backend_when_configured():
    cfg = Config(ai_native_path=Path("/x"), bark_url=BARK_URL)
    notifier = build_notifier(cfg)
    assert isinstance(notifier, BarkNotifier)


def test_build_notifier_returns_none_when_unconfigured():
    assert build_notifier(Config(ai_native_path=Path("/x"))) is None


# -- run_end_notification formatting -------------------------------------------


def _record(**overrides) -> RunRecord:
    fields = {"run_id": "run-1", "repo_name": "repo-a", "task_id": "task-x"}
    fields.update(overrides)
    return RunRecord(**fields)


@pytest.mark.parametrize(
    ("state", "label"),
    [
        ("completed", "run completed"),
        ("stopped", "run stopped"),
        ("error", "run failed"),
        ("ended", "run ended"),
    ],
)
def test_run_end_notification_titles_each_terminal_state(state, label):
    note = run_end_notification(_record(), state, "some detail")
    assert note.title == f"repo-a: {label}"
    assert note.group == "repo-a"
    assert note.body == "task-x\nsome detail"


def test_run_end_notification_unknown_state_falls_back():
    note = run_end_notification(_record(), "weird", None)
    assert note.title == "repo-a: run ended"
    assert note.body == "task-x"  # no detail appended


def test_run_end_notification_falls_back_to_repo_path_name():
    note = run_end_notification(
        _record(repo_name="", repo_path=Path("/repos/repo-b")), "completed", None
    )
    assert note.title == "repo-b: run completed"


# -- question_notification formatting ------------------------------------------


def test_question_notification_uses_message_then_banner():
    q = Question(seq=3, path=Path("/x/003-question.json"), message="Pick A or B?")
    note = question_notification("repo-a", "task-x", q)
    assert note.title == "repo-a: needs input"
    assert note.group == "repo-a"
    assert note.body == "task-x\nPick A or B?"


def test_question_notification_falls_back_to_seq_when_textless():
    q = Question(seq=7, path=Path("/x/007-question.json"))
    note = question_notification("repo-a", "task-x", q)
    assert note.body == "task-x\nquestion #7"


def test_question_notification_plan_gate_is_compact_and_omits_the_plan():
    # The plan-gate banner carries the full ~12 KB plan report; the push must
    # not include it or it silently fails to deliver.
    plan = "## Goal\n" + ("do the thing " * 2000)  # >> the payload limit
    q = Question(
        seq=2,
        path=Path("/x/002-question.json"),
        kind="plan-gate",
        banner=plan,
        message=plan,
    )
    note = question_notification("repo-a", "task-x", q)

    assert note.title == "repo-a: needs input"
    assert note.group == "repo-a"
    assert note.body == "task-x\nplan ready for review"
    assert "do the thing" not in note.body  # no plan content leaked


def test_question_notification_bounds_a_large_non_plan_gate_prompt():
    # A non-plan-gate banner (e.g. a run-error traceback) is still bounded so an
    # oversized prompt cannot break delivery — its leading content is kept.
    prompt = "Traceback: " + ("x" * 5000)
    q = Question(seq=4, path=Path("/x/004-question.json"), message=prompt)
    note = question_notification("repo-a", "task-x", q)

    body = note.body
    assert body.startswith("task-x\nTraceback: ")
    assert body.endswith("…")
    # prompt portion (after "task-x\n") is bounded; ellipsis adds one char.
    prompt_out = body.split("\n", 1)[1]
    assert len(prompt_out) <= QUESTION_PROMPT_MAX_CHARS + 1


def test_question_notification_keeps_a_prompt_at_the_bound_verbatim():
    prompt = "y" * QUESTION_PROMPT_MAX_CHARS
    q = Question(seq=5, path=Path("/x/005-question.json"), message=prompt)
    note = question_notification("repo-a", "task-x", q)
    assert note.body == f"task-x\n{prompt}"  # exactly at the bound: no ellipsis


# -- probe_notification formatting ---------------------------------------------


def test_probe_notification_is_backend_agnostic_probe():
    note = probe_notification()
    assert note.title == "orch-hub: test notification"
    assert "working" in note.body.lower()
    # No repo context, so no grouping — it is not a run/question event.
    assert note.group is None
