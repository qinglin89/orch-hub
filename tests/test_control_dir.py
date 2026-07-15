"""Tests for the control-dir file channel.

Exercises the hub side of the orchestrator ``--control-dir`` contract
(``.cursor/orchestrator/README.md`` §5) against fake control dirs in
``tmp_path``; never imports or runs orchestrator code. Answer files written
here are asserted against the orchestrator's documented validity rule (a JSON
object with a non-empty string ``answer``) so both sides stay in lockstep.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from orch_hub.control_dir import (
    DEFAULT_RESPONDER,
    PLAN_GATE_CONFIRM_WORDS,
    QUESTION_KINDS,
    STOP_FLAG_NAME,
    AnswerModeError,
    DuplicateAnswerError,
    list_questions,
    normalize_answer,
    pending_questions,
    request_stop,
    stop_answered,
    stop_requested,
    write_answer,
)
from orch_hub.models import Question


def write_question(
    ctl: Path, seq: int, *, kind: str = "blocked", banner: str = "Need a ruling"
) -> Path:
    """Drop a well-formed question file the way the orchestrator writes it."""
    path = ctl / f"{seq:03d}-question.json"
    payload = {
        "seq": seq,
        "ts": "2026-07-07T00:00:00+00:00",
        "kind": kind,
        "banner": banner,
        "message": banner,
    }
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def write_answer_file(ctl: Path, seq: int, content: str) -> Path:
    path = ctl / f"{seq:03d}-answer.json"
    path.write_text(content, encoding="utf-8")
    return path


def only_question(ctl: Path) -> Question:
    questions = list_questions(ctl)
    assert len(questions) == 1
    return questions[0]


# -- question discovery -------------------------------------------------------


def test_missing_control_dir_yields_no_questions(tmp_path):
    assert list_questions(tmp_path / "never-created") == ()
    assert pending_questions(tmp_path / "never-created") == ()


def test_empty_control_dir_yields_no_questions(tmp_path):
    assert list_questions(tmp_path) == ()


def test_ignores_files_outside_the_channel_pattern(tmp_path):
    (tmp_path / STOP_FLAG_NAME).write_text("")
    (tmp_path / "001-question.json.tmp").write_text("{}")
    (tmp_path / "abc-question.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("scratch")
    assert list_questions(tmp_path) == ()


def test_parses_question_fields(tmp_path):
    write_question(tmp_path, 1, kind="blocked", banner="Need a ruling on X")

    question = only_question(tmp_path)

    assert question.seq == 1
    assert question.kind == "blocked"
    assert question.banner == "Need a ruling on X"
    assert question.message == question.banner  # message aliases banner in v1
    assert question.ts == "2026-07-07T00:00:00+00:00"
    assert question.error is None
    assert question.answered is False
    assert question.pending is True
    assert question.answer_error is None
    assert question.kind in QUESTION_KINDS


def test_filename_seq_is_authoritative_over_payload_seq(tmp_path):
    (tmp_path / "003-question.json").write_text(
        json.dumps({"seq": 7, "banner": "mismatched"}), encoding="utf-8"
    )
    assert only_question(tmp_path).seq == 3


def test_questions_are_ordered_by_seq(tmp_path):
    for seq in (10, 2, 1):
        write_question(tmp_path, seq)
    assert [q.seq for q in list_questions(tmp_path)] == [1, 2, 10]


def test_minimal_stale_question_yields_none_fields(tmp_path):
    # A reused dir may hold minimal stale files (e.g. just {"seq": 1}); they
    # parse as JSON objects, so error stays None but every field is absent.
    (tmp_path / "001-question.json").write_text('{"seq": 1}', encoding="utf-8")

    question = only_question(tmp_path)

    assert question.error is None
    assert question.kind is None
    assert question.banner is None
    assert question.message is None
    assert question.ts is None


def test_unreadable_question_is_listed_with_error(tmp_path):
    (tmp_path / "001-question.json").write_text("{ not json", encoding="utf-8")

    question = only_question(tmp_path)

    assert question.error is not None and "unreadable JSON" in question.error
    assert question.banner is None
    assert question.pending is True


def test_non_object_question_is_listed_with_error(tmp_path):
    (tmp_path / "002-question.json").write_text("[1, 2]", encoding="utf-8")
    assert only_question(tmp_path).error == "not a JSON object"


def test_unknown_kind_is_surfaced_not_rejected(tmp_path):
    write_question(tmp_path, 1, kind="some-future-kind")
    assert only_question(tmp_path).kind == "some-future-kind"


# -- answered / pending state -------------------------------------------------


def test_valid_answer_marks_question_answered(tmp_path):
    write_question(tmp_path, 1)
    write_answer_file(tmp_path, 1, json.dumps({"answer": "proceed"}))

    question = only_question(tmp_path)

    assert question.answered is True
    assert question.pending is False
    assert question.answer_error is None
    assert question.answer == "proceed"


def test_answer_text_is_stripped_like_the_orchestrator_reads_it(tmp_path):
    write_question(tmp_path, 1)
    write_answer_file(tmp_path, 1, json.dumps({"answer": "  merge it \n"}))
    assert only_question(tmp_path).answer == "merge it"


def test_unanswered_and_malformed_answers_carry_no_answer_text(tmp_path):
    write_question(tmp_path, 1)
    write_question(tmp_path, 2)
    write_answer_file(tmp_path, 2, "{ not json")

    first, second = list_questions(tmp_path)

    assert first.answer is None
    assert second.answer is None


@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        ("{ not json", "unreadable JSON"),
        ("[]", "not a JSON object"),
        ('{"answer": "   "}', "missing/empty `answer` string"),
        ('{"answer": 42}', "missing/empty `answer` string"),
        ('{"responder": "hub"}', "missing/empty `answer` string"),
    ],
)
def test_malformed_answer_keeps_question_pending(tmp_path, content, expected_error):
    # The orchestrator re-reads a malformed answer each tick without consuming
    # it, so the question must stay visible as pending for repair.
    write_question(tmp_path, 1)
    write_answer_file(tmp_path, 1, content)

    question = only_question(tmp_path)

    assert question.answered is False
    assert question.pending is True
    assert question.answer_error is not None
    assert expected_error in question.answer_error


def test_pending_questions_filters_answered(tmp_path):
    write_question(tmp_path, 1)
    write_question(tmp_path, 2)
    write_answer_file(tmp_path, 1, json.dumps({"answer": "done"}))

    assert [q.seq for q in pending_questions(tmp_path)] == [2]
    assert [q.seq for q in list_questions(tmp_path)] == [1, 2]


# -- answer write -------------------------------------------------------------


def test_write_answer_mirrors_filename_with_full_fields(tmp_path):
    write_question(tmp_path, 1)

    path = write_answer(only_question(tmp_path), "  proceed  ")

    assert path == tmp_path / "001-answer.json"
    assert not list(tmp_path.glob("*.tmp")), "tmp file must be renamed away"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["seq"] == 1
    assert data["answer"] == "proceed"  # stripped, like the orchestrator reads
    assert data["responder"] == DEFAULT_RESPONDER
    dt.datetime.fromisoformat(data["ts"])  # ts must be ISO-8601
    # Exactly what the orchestrator requires of a valid answer file:
    assert isinstance(data, dict)
    assert isinstance(data["answer"], str) and data["answer"].strip()


def test_write_answer_records_custom_responder(tmp_path):
    write_question(tmp_path, 1)
    path = write_answer(only_question(tmp_path), "go", responder="web:qinglin")
    assert json.loads(path.read_text(encoding="utf-8"))["responder"] == "web:qinglin"


def test_write_answer_rejects_empty_answer(tmp_path):
    write_question(tmp_path, 1)
    with pytest.raises(ValueError):
        write_answer(only_question(tmp_path), "   ")
    assert not (tmp_path / "001-answer.json").exists()


def test_write_answer_refuses_to_overwrite_valid_answer(tmp_path):
    write_question(tmp_path, 1)
    original = json.dumps({"answer": "first ruling"})
    write_answer_file(tmp_path, 1, original)

    with pytest.raises(DuplicateAnswerError):
        write_answer(only_question(tmp_path), "second ruling")
    assert (tmp_path / "001-answer.json").read_text(encoding="utf-8") == original


def test_write_answer_repairs_malformed_answer(tmp_path):
    write_question(tmp_path, 1)
    write_answer_file(tmp_path, 1, "{ half-written garbage")

    path = write_answer(only_question(tmp_path), "fixed", responder="hub")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["answer"] == "fixed"
    assert only_question(tmp_path).answered is True


def test_write_answer_mirrors_unpadded_question_filename(tmp_path):
    # Hand-made stale file with nonstandard padding: mirror the observed name,
    # never reconstruct one from a hub-side counter.
    (tmp_path / "01-question.json").write_text(
        json.dumps({"banner": "odd"}), encoding="utf-8"
    )
    path = write_answer(only_question(tmp_path), "ok")
    assert path.name == "01-answer.json"
    assert json.loads(path.read_text(encoding="utf-8"))["seq"] == 1


def test_write_answer_rejects_non_question_path(tmp_path):
    bogus = Question(seq=1, path=tmp_path / "001-answer.json")
    with pytest.raises(ValueError):
        write_answer(bogus, "ok")


def test_written_answer_round_trips_through_rescan(tmp_path):
    write_question(tmp_path, 1, kind="run-error", banner="retry?")
    write_answer(only_question(tmp_path), "retry with flag")

    question = only_question(tmp_path)

    assert question.answered is True
    assert question.answer_error is None
    assert pending_questions(tmp_path) == ()


def test_stale_files_are_never_touched_by_discovery(tmp_path):
    # Reused-dir scenario: stale question+answer pairs and an unanswered stale
    # question must be readable without any file being modified.
    write_question(tmp_path, 1)
    write_answer_file(tmp_path, 1, json.dumps({"answer": "stale"}))
    write_question(tmp_path, 2)
    before = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir()}

    list_questions(tmp_path)
    pending_questions(tmp_path)

    after = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.iterdir()}
    assert after == before


# -- stop flag ----------------------------------------------------------------


def test_request_stop_creates_empty_flag_once(tmp_path):
    assert request_stop(tmp_path) is True
    flag = tmp_path / STOP_FLAG_NAME
    assert flag.is_file()
    assert flag.read_text(encoding="utf-8") == ""

    assert request_stop(tmp_path) is False  # idempotent: flag never deleted
    assert flag.read_text(encoding="utf-8") == ""


def test_request_stop_requires_existing_control_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        request_stop(tmp_path / "no-such-run")


def test_stop_requested_reflects_flag_presence(tmp_path):
    assert stop_requested(tmp_path) is False
    assert stop_requested(tmp_path / "never-created") is False

    request_stop(tmp_path)

    assert stop_requested(tmp_path) is True


@pytest.mark.parametrize(
    ("answer_text", "expected"),
    [
        ("stop", True),
        ("  Stop \n", True),  # orchestrator compares answer.lower() == "stop"
        ("STOP", True),
        ("stop the presses", False),
        ("proceed", False),
    ],
)
def test_stop_answered_mirrors_orchestrator_comparison(
    tmp_path, answer_text, expected
):
    write_question(tmp_path, 1)
    write_answer_file(tmp_path, 1, json.dumps({"answer": answer_text}))
    assert stop_answered(tmp_path) is expected


def test_stop_answered_ignores_malformed_and_absent_answers(tmp_path):
    assert stop_answered(tmp_path / "never-created") is False
    write_question(tmp_path, 1)
    assert stop_answered(tmp_path) is False
    write_answer_file(tmp_path, 1, '{"answer": ""}')  # invalid, never consumed
    assert stop_answered(tmp_path) is False


# -- Question model -----------------------------------------------------------


def test_question_to_dict_is_stable_and_excludes_path(tmp_path):
    write_question(tmp_path, 1, kind="plan-gate", banner="Plan OK?")
    payload = only_question(tmp_path).to_dict()

    assert payload == {
        "seq": 1,
        "ts": "2026-07-07T00:00:00+00:00",
        "kind": "plan-gate",
        "banner": "Plan OK?",
        "message": "Plan OK?",
        "error": None,
        "answered": False,
        "answer": None,
        "answer_error": None,
    }


# -- answer-mode normalization (matrix mode x kind) ---------------------------
#
# ``normalize_answer`` mirrors the orchestrator's own answer parsing (README
# §5/§5.8) so a dashboard button's intent can never silently mismatch the
# parse. Every matrix cell below is also exercised end-to-end through the HTTP
# answer route in ``tests/test_api.py``; these unit cases pin the pure helper.

PLAN_GATE = "plan-gate"


@pytest.mark.parametrize(
    "text, mode, kind, expected",
    [
        # non-plan-gate / answer: verbatim (marker-free binding answers).
        ("go", "answer", None, "go"),
        ("  keep padding  ", "answer", None, "  keep padding  "),
        ("go", "answer", "blocked", "go"),  # any non-plan-gate kind is uniform
        # non-plan-gate / discuss: prepend unless already marker-prefixed.
        ("what about X?", "discuss", None, "discuss: what about X?"),
        ("  trim then prepend  ", "discuss", None, "discuss: trim then prepend"),
        ("? already marked", "discuss", None, "? already marked"),
        ("？全角 marked", "discuss", None, "？全角 marked"),
        ("discuss: already", "discuss", None, "discuss: already"),
        ("DISCUSS: upper", "discuss", None, "DISCUSS: upper"),  # marker is ci
        ("discuss : spaced", "discuss", "run-error", "discuss : spaced"),
        # plan-gate / answer: empty -> confirm; bare confirm word -> verbatim.
        ("", "answer", PLAN_GATE, "confirm"),
        ("   ", "answer", PLAN_GATE, "confirm"),
        ("confirm", "answer", PLAN_GATE, "confirm"),
        ("  Approve  ", "answer", PLAN_GATE, "  Approve  "),
        ("确认", "answer", PLAN_GATE, "确认"),
        # plan-gate / discuss: feedback verbatim (non-confirm text).
        ("please tweak step 2", "discuss", PLAN_GATE, "please tweak step 2"),
        ("confirm the wording please", "discuss", PLAN_GATE, "confirm the wording please"),
    ],
)
def test_normalize_answer_matrix_pass(text, mode, kind, expected):
    assert normalize_answer(text, mode=mode, kind=kind) == expected


@pytest.mark.parametrize(
    "text, mode, kind",
    [
        # stop aborts the run in every mode/kind, before marker/confirm parsing,
        # so it is written verbatim (never discuss-prefixed, never refused).
        ("stop", "answer", None),
        ("STOP", "discuss", None),
        ("  Stop  ", "answer", PLAN_GATE),
        ("stop", "discuss", PLAN_GATE),
    ],
)
def test_normalize_answer_stop_passthrough(text, mode, kind):
    assert normalize_answer(text, mode=mode, kind=kind) == text


@pytest.mark.parametrize(
    "text, mode, kind, detail_substr",
    [
        # non-plan-gate / answer: a marker would be reinterpreted as discussion.
        ("? sneaky", "answer", None, "discussion marker"),
        ("discuss: sneaky", "answer", None, "discussion marker"),
        ("？全角 sneaky", "answer", "blocked", "discussion marker"),
        # plan-gate / answer: only a bare confirm word (or empty) approves.
        ("revise the plan", "answer", PLAN_GATE, "plan approval"),
        ("confirm please", "answer", PLAN_GATE, "plan approval"),
        # plan-gate / discuss: a bare confirm word would approve, not discuss.
        ("confirm", "discuss", PLAN_GATE, "plan-approval word"),
        ("  YES  ", "discuss", PLAN_GATE, "plan-approval word"),
        ("确认", "discuss", PLAN_GATE, "plan-approval word"),
    ],
)
def test_normalize_answer_matrix_guard_rejects(text, mode, kind, detail_substr):
    with pytest.raises(AnswerModeError) as excinfo:
        normalize_answer(text, mode=mode, kind=kind)
    assert detail_substr in str(excinfo.value)


@pytest.mark.parametrize(
    "text, mode, kind",
    [
        ("", "answer", None),  # non-plan-gate answer, empty -> existing 400
        ("   ", "answer", "blocked"),
        ("", "discuss", None),  # discussion needs text
        ("   ", "discuss", None),
        ("", "discuss", PLAN_GATE),  # empty is legal only as plan-gate answer
        ("   ", "discuss", PLAN_GATE),
    ],
)
def test_normalize_answer_empty_keeps_existing_non_empty_error(text, mode, kind):
    with pytest.raises(ValueError) as excinfo:
        normalize_answer(text, mode=mode, kind=kind)
    assert "non-empty" in str(excinfo.value)


def test_normalize_answer_empty_confirm_fill_is_a_recognized_word():
    # The plan-gate empty-answer fill must be a word the orchestrator's parser
    # actually accepts as approval, or a one-tap Confirm would silently fail.
    filled = normalize_answer("", mode="answer", kind=PLAN_GATE)
    assert filled.strip().lower() in PLAN_GATE_CONFIRM_WORDS


def test_normalize_answer_rejects_unknown_mode():
    with pytest.raises(AnswerModeError):
        normalize_answer("go", mode="bogus", kind=None)
