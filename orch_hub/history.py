"""File-backed run history and audit events.

History extends the existing per-run directory layout with append-only JSONL:

```text
<data_dir>/runs/<run_id>/
  run.json
  events.jsonl
```

The run manager owns when events are written. This module only serializes and
loads event records; it never parses task files or control-dir answers, and it
never stores human answer text.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import RunEvent

RUN_EVENTS_NAME = "events.jsonl"

KIND_RUN_STARTED = "run_started"
KIND_RUN_ENDED = "run_ended"
KIND_STOP_REQUESTED = "stop_requested"
KIND_ANSWER_RECORDED = "answer_recorded"
KIND_HISTORY_ERROR = "history_error"


def append_event(run_dir: Path, event: RunEvent) -> None:
    """Append one event to a run's JSONL history file."""
    path = run_dir / RUN_EVENTS_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")


def read_events(run_dir: Path) -> tuple[RunEvent, ...]:
    """Read a run's events, returning an error event for corrupt history lines."""
    path = run_dir / RUN_EVENTS_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    except OSError as err:
        return (_history_error(run_dir, f"unreadable {RUN_EVENTS_NAME} ({err})"),)

    events: list[RunEvent] = []
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError as err:
            events.append(_history_error(run_dir, f"line {lineno}: invalid JSON ({err})"))
            continue
        if not isinstance(data, dict):
            events.append(_history_error(run_dir, f"line {lineno}: not a JSON object"))
            continue
        events.append(_event_from_dict(data, default_run_id=run_dir.name))
    return tuple(events)


def _event_from_dict(data: dict, *, default_run_id: str) -> RunEvent:
    return RunEvent(
        ts=_str_field(data, "ts") or "",
        kind=_str_field(data, "kind") or KIND_HISTORY_ERROR,
        run_id=_str_field(data, "run_id") or default_run_id,
        repo_name=_str_field(data, "repo_name") or "",
        repo_path=_path_field(data, "repo_path"),
        task_id=_str_field(data, "task_id") or "",
        task_status=_str_field(data, "task_status"),
        state=_str_field(data, "state"),
        detail=_str_field(data, "detail"),
        exit_code=_int_field(data, "exit_code"),
        seq=_int_field(data, "seq"),
        question_ts=_str_field(data, "question_ts"),
        question_kind=_str_field(data, "question_kind"),
        question_banner=_str_field(data, "question_banner"),
        question_message=_str_field(data, "question_message"),
        responder=_str_field(data, "responder"),
        mode=_str_field(data, "mode"),
    )


def _history_error(run_dir: Path, detail: str) -> RunEvent:
    return RunEvent(
        ts="",
        kind=KIND_HISTORY_ERROR,
        run_id=run_dir.name,
        detail=detail,
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
