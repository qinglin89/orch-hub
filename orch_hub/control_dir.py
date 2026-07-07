"""Hub-side file channel to a running orchestrator's control dir.

Implements the orch-hub half of the ``--control-dir`` contract
(``.cursor/orchestrator/README.md`` §5): the orchestrator atomically writes
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
    if answer_path.exists() and _answer_error(answer_path) is None:
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
    answered, answer_error = _answer_state(answer_paths)
    return Question(
        seq=seq,
        path=path,
        ts=_string_field(data, "ts"),
        kind=_string_field(data, "kind"),
        banner=_string_field(data, "banner"),
        message=_string_field(data, "message"),
        error=error,
        answered=answered,
        answer_error=answer_error,
    )


def _string_field(data: dict, key: str) -> str | None:
    """Return a non-empty string payload field, or None when absent/invalid."""
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _answer_state(answer_paths: list[Path]) -> tuple[bool, str | None]:
    """Classify a question's same-seq answer files.

    Returns ``(answered, answer_error)``: answered when any file holds a valid
    answer; otherwise the first (name-ordered) file's validation error, or
    ``(False, None)`` when no answer file exists yet.
    """
    first_error: str | None = None
    for path in sorted(answer_paths):
        error = _answer_error(path)
        if error is None:
            return True, None
        if first_error is None:
            first_error = error
    return False, first_error


def _answer_error(path: Path) -> str | None:
    """Validate an answer file exactly as the orchestrator does.

    Mirrors ``_control_ask``: a JSON object with a non-empty string ``answer``
    is valid; everything else is described in the orchestrator's own error
    vocabulary.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return f"unreadable JSON ({err})"
    if not isinstance(data, dict):
        return "not a JSON object"
    if not isinstance(data.get("answer"), str) or not data["answer"].strip():
        return "missing/empty `answer` string"
    return None


def _answer_path_for(question_path: Path) -> Path:
    """Mirror the observed question filename into its answer filename."""
    name = question_path.name
    if not name.endswith(_QUESTION_SUFFIX):
        raise ValueError(f"not a question file: {question_path}")
    return question_path.with_name(name[: -len(_QUESTION_SUFFIX)] + "answer.json")
