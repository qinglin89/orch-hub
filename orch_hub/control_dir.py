"""Hub-side file channel to a running orchestrator's control dir.

Implements the orch-hub half of the ``--control-dir`` contract
(``.mandrel/orchestrator/README.md`` §5): the orchestrator atomically writes
``NNN-question.json`` and polls ``NNN-answer.json`` every second; a
``stop.flag`` file requests a stop. This module discovers pending questions,
writes answers, and writes the stop flag. It is file-channel only — no web,
API, or subprocess logic belongs here.

Contract rules honored on this side:

- The answer filename mirrors the observed question filename; no hub-side
  sequence counter is kept. Filename seq is authoritative over payload seq.
- Existing valid answers are never overwritten (a duplicate answer is refused);
  a malformed answer file is repaired by atomic rewrite — the orchestrator
  re-reads it each tick and never consumes a malformed file.
- Nothing here deletes control-dir files. The orchestrator never deletes
  ``stop.flag`` either, so a control dir is single-use: callers must create a
  fresh dir per run.
- Answers are written atomically (same-dir tmp + rename) so the orchestrator
  never reads a partial file; the ``.tmp`` suffix keeps the tmp file out of
  both sides' ``NNN-*.json`` scans.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from .models import Question

QUESTION_KINDS = (
    "request",
    "run-error",
    "followups-exhausted",
    "blocked",
    "convergence-budget",
    "dispute-unresolved",
    "final-review-stall",
    "closeout-incomplete",
    "plan-gate",
)
"""Documented ``kind`` values (orchestrator README §5.1-5.8).

Informational only — unknown kinds are surfaced as-is, never rejected, so a
newer orchestrator cannot break the hub.
"""

PLAN_GATE_KIND = "plan-gate"
"""The one ``kind`` whose answer parsing differs (README §5.8): its confirmation
loop has no discussion-marker channel; every non-confirm answer is feedback."""

STOP_ANSWER = "stop"
"""The answer that aborts the whole run. The orchestrator matches it (README §5,
``answer.lower() == "stop"``) before any marker/confirm parsing, so it is written
verbatim in every answer mode/kind."""

DISCUSSION_MARKER_RE = re.compile(r"^(?:[?？]|discuss\s*:)", re.IGNORECASE)
"""Mirror of the orchestrator's ``DISCUSSION_MARKER_RE`` (README §5). On a
non-``plan-gate`` escalation with a live/resumable session, an answer whose
stripped text matches this is a discussion turn — the marker is stripped and the
rest sent into that session — not a binding answer. Mirrored verbatim so the
hub's mode guards match exactly what the orchestrator will parse."""

PLAN_GATE_CONFIRM_WORDS = frozenset(
    {
        "confirm",
        "confirmed",
        "approve",
        "approved",
        "proceed",
        "yes",
        "y",
        "ok",
        "okay",
        "确认",
        "同意",
        "批准",
        "通过",
        "继续",
        "可以",
        "开始",
        "开工",
        "执行",
        "实施",
    }
)
"""Mirror of the orchestrator's plan-gate approval set (README §5.8): a standalone
word matched as ``answer.strip().lower()``; an approval word with any extra text
is feedback, not confirmation. Mirrored so a [Confirm] tap and a [Feedback] guard
agree with the parser. Mirror-drift is accepted (task notes): an upstream-added
word the hub lacks slips past the [Feedback] guard (no worse than today's typed
answer); a word the hub keeps but upstream dropped over-rejects (safe direction)."""

ANSWER_MODES = ("answer", "discuss")
"""The two explicit dashboard button intents: ``answer`` = a binding answer /
plan approval; ``discuss`` = a clarifying turn / plan feedback.
:func:`normalize_answer` requires one of these. A request that omits ``mode`` is
back-compat state owned by the API route (written verbatim, skipping
normalization), not one of these explicit intents."""

STOP_FLAG_NAME = "stop.flag"

DEFAULT_RESPONDER = "orch-hub"

_QUESTION_SUFFIX = "question.json"

_CHANNEL_FILE = re.compile(r"(\d+)-(question|answer)\.json")
"""Same filename pattern the orchestrator uses for its seq scan."""


class DuplicateAnswerError(Exception):
    """Raised when a question already has a valid answer file.

    The orchestrator may have consumed the existing answer already, so
    overwriting it would falsify the audit record without reaching anyone.
    """


class AnswerModeError(ValueError):
    """Raised when an answer's text cannot be reconciled with its declared
    ``mode`` for the question's ``kind``.

    A fail-closed guard (see :func:`normalize_answer`) so a button's intent can
    never silently land on the wrong side of the orchestrator's answer parser.
    Subclasses :class:`ValueError` so the answer route's existing 400 handling
    catches it alongside :func:`write_answer`'s empty-answer rejection.
    """


def list_questions(control_dir: Path) -> tuple[Question, ...]:
    """Return every question in ``control_dir``, ordered by seq.

    A missing (or not-yet-created) control dir yields no questions. Question
    files that cannot be parsed are still listed — with ``error`` set and
    payload fields ``None`` — never silently skipped, so a broken file cannot
    hide a run that is blocked waiting on a human.
    """
    if not control_dir.is_dir():
        return ()
    questions: list[tuple[int, str, Path]] = []
    answers: dict[int, list[Path]] = {}
    for path in control_dir.iterdir():
        match = _CHANNEL_FILE.fullmatch(path.name)
        if not match:
            continue
        seq = int(match.group(1))
        if match.group(2) == "question":
            questions.append((seq, path.name, path))
        else:
            answers.setdefault(seq, []).append(path)
    return tuple(
        _load_question(seq, path, answers.get(seq, []))
        for seq, _, path in sorted(questions)
    )


def pending_questions(control_dir: Path) -> tuple[Question, ...]:
    """Return the questions still awaiting a valid answer, ordered by seq."""
    return tuple(q for q in list_questions(control_dir) if q.pending)


def write_answer(
    question: Question, answer: str, *, responder: str = DEFAULT_RESPONDER
) -> Path:
    """Atomically write the answer file for ``question``; return its path.

    The payload carries the full documented fields (``seq``, ``ts``,
    ``responder``, ``answer``) even though the orchestrator only requires a
    non-empty ``answer``; ``ts``/``responder`` make the file the audit record
    of who answered and when. The answer text is stripped (the orchestrator
    strips on read) and must be non-empty; ``"stop"`` is a legitimate answer
    that aborts the whole run.

    An existing *valid* answer raises :class:`DuplicateAnswerError`; an
    existing malformed one is repaired by atomic rewrite per the contract.
    """
    text = answer.strip()
    if not text:
        raise ValueError("answer must be a non-empty string")
    answer_path = _answer_path_for(question.path)
    if answer_path.exists() and _read_answer(answer_path)[1] is None:
        raise DuplicateAnswerError(
            f"{answer_path.name} already holds a valid answer"
        )
    payload = {
        "seq": question.seq,
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "responder": responder,
        "answer": text,
    }
    tmp = answer_path.with_name(answer_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, answer_path)
    return answer_path


def normalize_answer(text: str, *, mode: str, kind: str | None) -> str:
    """Reconcile a dashboard button ``mode`` with a question's ``kind``.

    Returns the exact answer text to hand :func:`write_answer`, or raises
    :class:`AnswerModeError` (→ HTTP 400) when the two cannot be reconciled
    fail-closed. The returned string is what the orchestrator's own parser (see
    :data:`DISCUSSION_MARKER_RE`, :data:`PLAN_GATE_CONFIRM_WORDS`) then
    interprets, so this *is* the guard that keeps button intent and parse
    outcome aligned — nothing is silently reinterpreted.

    ``mode`` is :data:`ANSWER_MODES`; only ``kind == "plan-gate"`` parses
    differently (README §5.8), every other/unknown/absent ``kind`` is an
    ordinary escalation. Rules (task normalization matrix):

    - ``stop`` (strip+casefold) → verbatim in every mode/kind: the orchestrator
      aborts on it before any marker/confirm parsing.
    - Empty text is legal only as plan-gate + ``answer`` (the hub fills the
      literal ``confirm`` so a one-tap approval needs no typed text); every
      other empty answer keeps the existing non-empty 400.
    - non-plan-gate / ``discuss`` → prepend ``discuss: `` unless already
      marker-prefixed; non-plan-gate / ``answer`` → verbatim, but a
      marker-prefixed text is refused so it cannot become a discussion turn.
    - plan-gate / ``discuss`` → verbatim feedback, but a bare confirm word is
      refused (the parser would read it as approval); plan-gate / ``answer`` →
      a bare confirm word is verbatim, any other non-empty text is refused (use
      discuss for plan feedback).
    """
    if mode not in ANSWER_MODES:
        raise AnswerModeError(f"mode must be one of {', '.join(ANSWER_MODES)}")
    stripped = text.strip()
    is_plan_gate = kind == PLAN_GATE_KIND

    # `stop` aborts the run and is matched before any marker/confirm parsing, so
    # it bypasses every mode/kind guard in every mode.
    if stripped.casefold() == STOP_ANSWER:
        return text

    # Empty is meaningful only as a plan-gate approval, where the hub fills the
    # canonical confirm word so a bare [Confirm] tap needs no typed text.
    if not stripped:
        if is_plan_gate and mode == "answer":
            return "confirm"  # canonical approval word (in PLAN_GATE_CONFIRM_WORDS)
        raise ValueError("answer must be a non-empty string")

    if is_plan_gate:
        return _normalize_plan_gate_answer(text, stripped, mode)
    return _normalize_escalation_answer(text, stripped, mode)


def _normalize_escalation_answer(text: str, stripped: str, mode: str) -> str:
    """Normalize a non-``plan-gate`` escalation answer (``stripped`` non-empty)."""
    marked = DISCUSSION_MARKER_RE.match(stripped) is not None
    if mode == "discuss":
        # Already a marker → send as-is; else make the discussion intent explicit
        # so the orchestrator routes it into the escalation's live session.
        return text if marked else f"discuss: {stripped}"
    # mode == "answer": a marker-prefixed answer would be silently reinterpreted
    # as a discussion turn, so refuse it fail-closed.
    if marked:
        raise AnswerModeError(
            "answer starts with a discussion marker (?, ？, or 'discuss:') and "
            "would be read as a discussion turn — use Discuss, or remove the "
            "marker to send a binding answer"
        )
    return text


def _normalize_plan_gate_answer(text: str, stripped: str, mode: str) -> str:
    """Normalize a ``plan-gate`` answer (``stripped`` non-empty)."""
    is_confirm_word = stripped.lower() in PLAN_GATE_CONFIRM_WORDS
    if mode == "discuss":
        # Plan-gate has no marker channel: every non-confirm answer is feedback
        # to the planning session. Only a bare confirm word is dangerous (it
        # would approve instead of discuss), so refuse exactly that.
        if is_confirm_word:
            raise AnswerModeError(
                "this text is a plan-approval word and would confirm the plan — "
                "use Confirm to approve, or reword the feedback"
            )
        return text
    # mode == "answer": approving the current plan. A bare confirm word is the
    # approval; any other non-empty text on the Confirm button is ambiguous and
    # would be parsed as feedback, not approval, so refuse it fail-closed.
    if is_confirm_word:
        return text
    raise AnswerModeError(
        "plan approval takes no free text — leave it empty (or send a single "
        "confirm word) to confirm, or use Feedback to send plan changes"
    )


def request_stop(control_dir: Path) -> bool:
    """Write ``stop.flag`` (empty file) into ``control_dir``.

    Existence is the whole signal: at a session boundary the run stops
    gracefully; while a question is pending it aborts like a ``stop`` answer.
    Returns True when the flag was newly created, False when it already
    existed (idempotent — the orchestrator never deletes the flag). A missing
    control dir raises :class:`FileNotFoundError`: the flag targets a real
    run's dir, and silently creating one would hide a caller bug.
    """
    flag = control_dir / STOP_FLAG_NAME
    if flag.exists():
        return False
    flag.touch()
    return True


def stop_requested(control_dir: Path) -> bool:
    """Return whether ``control_dir`` carries a ``stop.flag``.

    A missing control dir means no stop was requested (mirroring
    :func:`list_questions`' tolerance, since callers probe dirs that may not
    exist anymore).
    """
    return (control_dir / STOP_FLAG_NAME).is_file()


def stop_answered(control_dir: Path) -> bool:
    """Return whether any question was answered ``stop``.

    Mirrors the orchestrator's own comparison (``answer.lower() == "stop"``
    after stripping): such an answer aborts the whole run with a nonzero
    exit, so callers classifying a finished run can tell a human-requested
    stop from an error.
    """
    return any(
        q.answer is not None and q.answer.lower() == "stop"
        for q in list_questions(control_dir)
    )


def _load_question(seq: int, path: Path, answer_paths: list[Path]) -> Question:
    """Build a :class:`Question` from one question file plus its answer state."""
    error: str | None = None
    data: dict = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        error = f"unreadable JSON ({err})"
    else:
        if isinstance(raw, dict):
            data = raw
        else:
            error = "not a JSON object"
    answered, answer, answer_error = _answer_state(answer_paths)
    return Question(
        seq=seq,
        path=path,
        ts=_string_field(data, "ts"),
        kind=_string_field(data, "kind"),
        banner=_string_field(data, "banner"),
        message=_string_field(data, "message"),
        error=error,
        answered=answered,
        answer=answer,
        answer_error=answer_error,
    )


def _string_field(data: dict, key: str) -> str | None:
    """Return a non-empty string payload field, or None when absent/invalid."""
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _answer_state(
    answer_paths: list[Path],
) -> tuple[bool, str | None, str | None]:
    """Classify a question's same-seq answer files.

    Returns ``(answered, answer, answer_error)``: the first (name-ordered)
    valid file's stripped answer text when one exists; otherwise the first
    file's validation error, or ``(False, None, None)`` when no answer file
    exists yet.
    """
    first_error: str | None = None
    for path in sorted(answer_paths):
        answer, error = _read_answer(path)
        if error is None:
            return True, answer, None
        if first_error is None:
            first_error = error
    return False, None, first_error


def _read_answer(path: Path) -> tuple[str | None, str | None]:
    """Read an answer file, validating exactly as the orchestrator does.

    Mirrors ``_control_ask``: a JSON object with a non-empty string ``answer``
    is valid; everything else is described in the orchestrator's own error
    vocabulary. Returns ``(stripped answer, None)`` or ``(None, error)``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return None, f"unreadable JSON ({err})"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    if not isinstance(data.get("answer"), str) or not data["answer"].strip():
        return None, "missing/empty `answer` string"
    return data["answer"].strip(), None


def _answer_path_for(question_path: Path) -> Path:
    """Mirror the observed question filename into its answer filename."""
    name = question_path.name
    if not name.endswith(_QUESTION_SUFFIX):
        raise ValueError(f"not a question file: {question_path}")
    return question_path.with_name(name[: -len(_QUESTION_SUFFIX)] + "answer.json")
