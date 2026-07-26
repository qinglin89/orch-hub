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
from orch_hub.models import IntakeDraft, IntakeRecord, Question, RunRecord, SemanticJob
from orch_hub.notifications import (
    QUESTION_PROMPT_MAX_CHARS,
    BarkNotifier,
    Notification,
    build_notifier,
    intake_ready_notification,
    probe_notification,
    question_notification,
    run_end_notification,
    semantic_job_notification,
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


# -- intake_ready_notification formatting --------------------------------------


def _intake_record(**overrides) -> IntakeRecord:
    """An awaiting-review intake record carrying a full request + draft body.

    The request and drafted task body are populated with sentinel strings so the
    formatting tests can prove the push carries neither — the notification is
    metadata-only by construction.
    """
    draft = IntakeDraft(
        task_id="2026-07-19-add-widget",
        summary="Add a widget",
        task_file="---\nid: 2026-07-19-add-widget\n---\n\nSECRET-TASK-BODY\n",
        index_entry="| 2026-07-19-add-widget | Add a widget | pending | 0/2 | [] |",
    )
    fields = {
        "intake_id": "20260719T040000Z-repo-a-intake",
        "repo_name": "repo-a",
        "repo_path": Path("/repos/repo-a"),
        "request": "SECRET-REQUEST-TEXT",
        "status": "awaiting_review",
        "draft": draft,
        "iteration": 0,
        "agent_ref": "SECRET-SESSION-ID",
    }
    fields.update(overrides)
    return IntakeRecord(**fields)


def test_intake_ready_notification_initial_draft():
    note = intake_ready_notification(_intake_record(iteration=0))
    assert note.title == "repo-a: intake ready"
    assert note.group == "repo-a"  # clusters into the repo's device thread
    assert note.body == "20260719T040000Z-repo-a-intake\ndraft ready for review"


def test_intake_ready_notification_revision_names_the_iteration():
    note = intake_ready_notification(_intake_record(iteration=2))
    assert note.title == "repo-a: intake ready"
    assert note.body == "20260719T040000Z-repo-a-intake\nrevision 2 ready for review"


def test_intake_ready_notification_excludes_request_body_and_handle():
    # Metadata only: neither the operator's request, the drafted task body, nor
    # the internal agent handle may reach the push payload.
    note = intake_ready_notification(_intake_record())
    blob = f"{note.title}\n{note.body}\n{note.group}"
    assert "SECRET-REQUEST-TEXT" not in blob
    assert "SECRET-TASK-BODY" not in blob
    assert "SECRET-SESSION-ID" not in blob
    assert "/repos/repo-a" not in blob  # nor the repo filesystem path


def test_intake_ready_notification_falls_back_when_repo_name_is_empty():
    note = intake_ready_notification(_intake_record(repo_name=""))
    assert note.title == "?: intake ready"
    assert note.group == "?"


# -- semantic_job_notification formatting --------------------------------------


def _semantic_job(**overrides) -> SemanticJob:
    """A settled semantic job; ``**overrides`` set the fields a test varies.

    The failure tests pass a sentinel ``error`` so they can prove the push
    carries no diagnostic — that field may quote a backend stderr tail (model
    output), and the payload is metadata-only by construction.
    """
    fields = {
        "job_id": "20260723T120000Z",
        "repo_name": "repo-a",
        "repo_path": Path("/repos/repo-a"),
        "task_id": "2026-07-19-add-widget",
        "status": "completed",
        "backend": "claude-cli",
        "model": "claude-opus-5",
        "schema_version": "1",
        "evaluation_id": "20260723T115900Z",
        "error": None,
    }
    fields.update(overrides)
    return SemanticJob(**fields)


def test_semantic_job_notification_completed_links_the_evaluation():
    note = semantic_job_notification(_semantic_job())
    assert note.title == "repo-a: evaluation completed"
    assert note.group == "repo-a"  # clusters into the repo's device thread
    assert note.body == "2026-07-19-add-widget\nreport 20260723T115900Z"


def test_semantic_job_notification_failed_names_the_task_only():
    # A failed job has no evaluation to link, so the body is task identity alone.
    note = semantic_job_notification(
        _semantic_job(status="failed", evaluation_id=None, error="boom")
    )
    assert note.title == "repo-a: evaluation failed"
    assert note.body == "2026-07-19-add-widget"


def test_semantic_job_notification_excludes_the_failure_diagnostic():
    # Metadata only: the bounded diagnostic may quote backend stderr / model
    # output, so it must never reach the push payload.
    note = semantic_job_notification(
        _semantic_job(
            status="failed",
            evaluation_id=None,
            error="semantic evaluation failed: SECRET-MODEL-STDERR",
        )
    )
    blob = f"{note.title}\n{note.body}\n{note.group}"
    assert "SECRET-MODEL-STDERR" not in blob
    assert "/repos/repo-a" not in blob  # nor the repo filesystem path


def test_semantic_job_notification_falls_back_to_repo_path_then_placeholder():
    named = semantic_job_notification(_semantic_job(repo_name=""))
    assert named.title == "repo-a: evaluation completed"  # from repo_path.name
    anonymous = semantic_job_notification(_semantic_job(repo_name="", repo_path=None))
    assert anonymous.title == "?: evaluation completed"
    assert anonymous.group == "?"


def test_semantic_job_notification_unknown_status_uses_neutral_label():
    # Defensive: the manager only fires on terminal statuses, so an unmapped one
    # still formats a sane push rather than leaking a raw status string.
    note = semantic_job_notification(_semantic_job(status="error", evaluation_id=None))
    assert note.title == "repo-a: evaluation ended"


# -- probe_notification formatting ---------------------------------------------


def test_probe_notification_is_backend_agnostic_probe():
    note = probe_notification()
    assert note.title == "orch-hub: test notification"
    assert "working" in note.body.lower()
    # No repo context, so no grouping — it is not a run/question event.
    assert note.group is None
