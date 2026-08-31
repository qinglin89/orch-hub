"""Tests for the automatic-dispatch pause switch (:mod:`orch_hub.eval_control`).

The store is small; what it has to get right is not. Pausing must survive the
restart that ``ORCH_HUB_EVAL_AUTO`` demands, must answer *when* the drain stopped
rather than when the button was last pressed, and must fail toward *paused* when
its own record is unreadable — the recoverable direction, since the alternative
silently spends the subscription window an operator was protecting. These pin all
three, plus the resume that repairs a damaged record rather than reporting success
over it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from orch_hub.eval_control import (
    EVAL_CONTROL_NAME,
    PAUSE_OPERATOR,
    PAUSE_UNREADABLE,
    EvaluationControlStore,
)

NOW = dt.datetime(2026, 8, 27, 9, 14, 0, tzinfo=dt.timezone.utc)


class Clock:
    def __init__(self, now: dt.datetime = NOW):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)


def store(tmp_path: Path, clock: Clock | None = None) -> EvaluationControlStore:
    return EvaluationControlStore(tmp_path / "hub-data", clock=clock or Clock())


def test_a_hub_that_was_never_paused_is_running(tmp_path):
    """No file is the normal state, not an unknown one."""
    state = store(tmp_path).state()

    assert state.paused is False
    assert state.reason is None
    assert state.paused_ts is None


def test_pausing_persists_and_survives_a_new_store(tmp_path):
    """The reason to pause outlives the process.

    A pause an operator set for an afternoon's dev work must not be silently
    undone by a launchd restart — which is exactly what an in-memory switch would
    do, and the failure mode that rules the environment variable out.
    """
    control = store(tmp_path)
    paused = control.pause()

    assert paused.paused is True
    assert paused.reason == PAUSE_OPERATOR
    assert paused.paused_ts == NOW.isoformat()
    # A brand-new store over the same data dir — the shape a restart produces.
    assert store(tmp_path).state() == paused


def test_pausing_twice_keeps_the_instant_the_drain_actually_stopped(tmp_path):
    """"Held since" must answer when the queue stopped, not when a button was last
    pressed. Repeated presses (or a second browser session) otherwise make a queue
    held for hours read as freshly paused."""
    clock = Clock()
    control = store(tmp_path, clock)
    first = control.pause()
    clock.advance(3600)

    again = control.pause()

    assert again.paused_ts == first.paused_ts == NOW.isoformat()


def test_resuming_releases_the_queue(tmp_path):
    control = store(tmp_path)
    control.pause()

    resumed = control.resume()

    assert resumed.paused is False
    assert resumed.reason is None
    assert store(tmp_path).state().paused is False


def test_an_unreadable_control_holds_the_queue_and_names_itself_as_damage(tmp_path):
    """Fail toward deferral, the same direction the dispatch policy takes for an
    unreadable usage feed.

    Read the other way a deliberate pause evaporates on one corrupt byte and turns
    are spent in the window the operator was protecting. Read this way the queue
    holds and the reason travels as damage rather than as somebody's decision — so
    an operator who never paused is not sent looking for a choice they never made.
    """
    control = store(tmp_path)
    control.pause()
    (tmp_path / "hub-data" / EVAL_CONTROL_NAME).write_text("{ not json", encoding="utf-8")

    state = control.state()

    assert state.paused is True
    assert state.reason == PAUSE_UNREADABLE
    assert state.paused_ts is None


def test_a_wrong_shaped_record_is_damage_rather_than_a_running_queue(tmp_path):
    """JSON that parses but says nothing about `paused` is not evidence of a
    running queue; it is the same unreadable record."""
    control = store(tmp_path)
    control.pause()
    (tmp_path / "hub-data" / EVAL_CONTROL_NAME).write_text('["paused"]', encoding="utf-8")

    assert control.state().reason == PAUSE_UNREADABLE


def test_resume_repairs_an_unreadable_control(tmp_path):
    """The one operation that must write unconditionally.

    An unreadable record reads as paused precisely so the damage is visible; a
    resume that skipped the write against it would report success while leaving
    the queue held forever.
    """
    control = store(tmp_path)
    control.pause()
    (tmp_path / "hub-data" / EVAL_CONTROL_NAME).write_text("garbage", encoding="utf-8")

    resumed = control.resume()

    assert resumed.paused is False
    assert store(tmp_path).state().paused is False


def test_pausing_over_a_damaged_record_makes_the_reason_true(tmp_path):
    """A queue held only because its record was unreadable becomes a real operator
    pause when somebody presses Pause — the file is repaired and the reason stops
    lying about who decided."""
    clock = Clock()
    control = store(tmp_path, clock)
    control.pause()
    (tmp_path / "hub-data" / EVAL_CONTROL_NAME).write_text("garbage", encoding="utf-8")
    clock.advance(60)

    repaired = control.pause()

    assert repaired.reason == PAUSE_OPERATOR
    assert repaired.paused_ts == clock.now.isoformat()
    assert store(tmp_path).state() == repaired


def test_a_foreign_reason_token_still_reads_as_a_pause(tmp_path):
    """The vocabulary is closed and only `pause()` writes it, so an unrecognized
    token is a hand-edit. The file says paused, so the queue stays held; the
    reason is normalized rather than invented."""
    control = store(tmp_path)
    control.pause()
    (tmp_path / "hub-data" / EVAL_CONTROL_NAME).write_text(
        '{"paused": true, "paused_ts": null, "reason": "because-i-said-so"}',
        encoding="utf-8",
    )

    state = control.state()

    assert state.paused is True
    assert state.reason == PAUSE_OPERATOR


def test_the_record_lands_beside_the_other_hub_owned_stores(tmp_path):
    """Under the hub data dir, never inside a managed repo — the same containment
    every other hub-owned store keeps."""
    control = store(tmp_path)
    control.pause()

    assert control.path == tmp_path / "hub-data" / EVAL_CONTROL_NAME
    assert control.path.is_file()
