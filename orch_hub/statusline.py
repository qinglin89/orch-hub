"""Claude Code statusLine writer: a transitional fallback carrier for Claude usage.

Claude Code pipes the five-hour / seven-day subscription figures to a configured
statusLine command's stdin, as ``rate_limits.{five_hour,seven_day}.{used_percentage,
resets_at}`` ("only present for subscribers after first API response"). This module
is the small writer that reads that payload and persists the rate-limit part to a
hub-readable sink; :mod:`orch_hub.usage` reads it back into the ordinary
observation store, so the read path, cache, ``GET /api/usage`` payload, and
Overview cards are unchanged.

**It is no longer required, and no longer the primary carrier.** When this was
written the statusLine payload was the only local channel carrying the figures at
all, which made Claude usage depend on somebody rendering a status line — it went
quiet on an idle Mac or a stretch of headless orchestrator runs, exactly when the
operator most wants the number. :class:`orch_hub.usage.ClaudeUsageCollector` now
reads the CLI's own authenticated utilization cache and refreshes it by running the
CLI's local ``/usage`` command, so Claude usage works with nobody watching and with
nothing installed into ``~/.claude``. This writer remains because an existing
installation should keep working and keep contributing: both carriers merge into
one store per window, so the sink is an opportunistic extra reading rather than a
competing source. New installations do not need it (``docs/claude-usage.md``).

Both runtime shapes can reach it. A source install gets the
``orch-hub-statusline`` console script from packaging metadata; a compiled
release ships one executable and no console scripts, so there it is the
``orch-hub statusline`` subcommand (:func:`run` is the shared body). The two
spellings are the same wrapper, and the sink is the only carrier that also names
the plan tier — so a compiled install is not cut off from it.

Two properties make installing it safe:

- **Operator-installed, never hub-installed.** The hub does not write
  ``~/.claude/**``; the operator points their own ``statusLine.command`` at this
  writer, and removing it again is their edit to make — nothing in the hub adds,
  edits, or nags about that setting.
- **Invisible.** Anything after ``--`` is the operator's existing statusLine
  command: it receives the same stdin bytes and its stdout/stderr/exit code are
  relayed untouched, so the rendered status line is byte-identical to before.
  With no wrapped command the script prints nothing (Claude Code then renders no
  status line).

Failure conduct mirrors the rest of the usage feature: capture is best-effort and
never blocks or alters the visible status line. A malformed payload, an
unwritable sink, or a payload with no ``rate_limits`` (a non-subscriber session,
or one before its first API response) simply records nothing, leaving the
previous observation — and therefore its honest observation time — intact.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import DATA_DIR_ENV, DEFAULT_DATA_DIR
from .usage import CLAUDE_STATUSLINE_NAME, USAGE_DIR_NAME

SINK_SCHEMA = 1
"""Sink schema version. Written for forward diagnosis only — the reader locates
the rate-limit container structurally and ignores this field."""

SINK_SOURCE = "claude-statusline"
"""Marks which carrier fed an observation, so a sink inspected by hand (or a
future second carrier) is self-describing."""

MAX_PAYLOAD_BYTES = 1024 * 1024
"""Skip *parsing* a statusLine payload larger than this — a defensive guard on a
render-path read. Real payloads are a few KB. Pass-through is unaffected: the
wrapped command still receives every byte."""

MAX_SINK_BYTES = 64 * 1024
"""Upper bound on what may be persisted. The CLI's rate-limit container is a
handful of numbers; refusing to write anything larger keeps an unexpectedly fat
future container from growing an unbounded file in the hub data dir."""

_WHITELIST_PLAN_KEY = "subscription_type"
"""The only field taken beside the rate-limit container itself.

The statusLine payload also carries session id, transcript path, cwd, model, and
cost fields. None of them are subscription capacity, so none are persisted — the
sink holds the rate-limit container plus the plan name and nothing else."""

_RATE_LIMITS_KEY = "rate_limits"


def resolve_sink(*, sink: str | None = None, data_dir: str | None = None, env: dict[str, str] | None = None) -> Path:
    """Resolve the sink path from explicit flags, then ``ORCH_HUB_DATA_DIR``.

    Deliberately *not* via :meth:`Config.from_env`: that validates the whole
    service configuration and raises on, say, a malformed ``ORCH_HUB_PORT`` —
    which must never take down a status line. Only the data-dir constants are
    shared, so the sink location cannot drift from the reader's."""
    if sink:
        return Path(sink).expanduser()
    environ = os.environ if env is None else env
    raw = (data_dir or environ.get(DATA_DIR_ENV) or "").strip()
    root = Path(raw).expanduser() if raw else DEFAULT_DATA_DIR
    return root / USAGE_DIR_NAME / CLAUDE_STATUSLINE_NAME


def build_record(payload: object, *, observed_ts: str) -> dict | None:
    """The sink record for one statusLine payload, or ``None`` when there is
    nothing to record.

    Carries the CLI's ``rate_limits`` container **verbatim** plus the plan name,
    stamped with the observation time. The container is passed through rather
    than field-filtered on purpose: :mod:`orch_hub.usage` already whitelists the
    fields it reads (and is the verified, tested owner of that mapping), so
    re-deciding it here would duplicate the very knowledge the reader owns.

    ``None`` when the payload is not an object or carries no ``rate_limits``
    object — a non-subscriber session, or one before its first API response.
    Recording nothing is the right degrade: it preserves the previous
    observation and its honest observation time instead of overwriting real
    figures with an empty snapshot."""
    if not isinstance(payload, dict):
        return None
    rate_limits = payload.get(_RATE_LIMITS_KEY)
    if not isinstance(rate_limits, dict) or not rate_limits:
        return None
    record: dict = {
        "schema": SINK_SCHEMA,
        "source": SINK_SOURCE,
        "observed_ts": observed_ts,
        _RATE_LIMITS_KEY: rate_limits,
    }
    plan = payload.get(_WHITELIST_PLAN_KEY)
    if isinstance(plan, str) and plan.strip():
        record[_WHITELIST_PLAN_KEY] = plan.strip()
    return record


def write_sink(path: Path, record: dict) -> bool:
    """Persist ``record`` atomically (same-dir unique temp + ``os.replace``).

    The temp name carries the pid because several Claude Code sessions render
    status lines concurrently, all writing this one sink; a shared temp name lets
    one writer's ``os.replace`` move the file out from under another. Per-writer
    temps make each swap independent and last-write-wins. Every session reports the
    same account-level *windows*, but not the same *figures* at a given instant —
    an idle session publishes an older reading — so the writer does not attempt to
    dedup: :func:`orch_hub.usage.merge_observations` merges the sink into the store
    per window by each figure's own window instant, so a stale writer's newer write
    cannot lower a current figure. Correctness lives on the read side; this writer
    just persists atomically. Mirrors :meth:`orch_hub.usage.ClaudeUsageStore.record`.

    Returns whether the record was written: a record over :data:`MAX_SINK_BYTES`
    is refused, and the refusal is reported rather than silently reading as a
    successful capture."""
    text = json.dumps(record, indent=1)
    if len(text.encode("utf-8")) > MAX_SINK_BYTES:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:  # noqa: BLE001 — clean up the unique temp, then re-raise
        tmp.unlink(missing_ok=True)
        raise
    return True


def capture(raw: bytes, sink: Path, *, now: dt.datetime | None = None) -> bool:
    """Best-effort: persist the usage part of one statusLine payload to ``sink``.

    Returns whether a record was written. Never raises — a status line must
    render whatever the hub's bookkeeping does, so an oversized/malformed payload
    or an unwritable sink is simply "nothing recorded"."""
    try:
        if len(raw) > MAX_PAYLOAD_BYTES:
            return False
        observed = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
        record = build_record(json.loads(raw.decode("utf-8", errors="replace")), observed_ts=observed)
        if record is None:
            return False
        return write_sink(sink, record)
    except Exception:  # noqa: BLE001 — display-only bookkeeping; never break the status line
        return False


def split_argv(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split our own flags from the wrapped statusLine command after ``--``.

    Done before any parser sees the argv, by both entry points (the legacy
    console script and ``orch-hub statusline``), because the wrapped command's
    own flags are not ours to interpret: an operator status line invoked as
    ``-- /usr/local/bin/mystatus --color`` must keep its ``--color``. Splitting
    on the *first* ``--`` leaves any later one inside the wrapped argv, where a
    nested command (``-- /bin/sh -c '...'``) may need it."""
    args = list(argv)
    if "--" in args:
        index = args.index("--")
        return args[:index], args[index + 1 :]
    return args, []


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orch-hub-statusline",
        description=(
            "Capture Claude Code's statusLine subscription usage for the orch-hub "
            "Overview, then run the wrapped statusLine command unchanged."
        ),
        epilog="Everything after `--` is your own statusLine command (argv, no shell).",
    )
    parser.add_argument("--sink", help="explicit sink path (default: <data-dir>/usage/claude-statusline.json)")
    parser.add_argument("--data-dir", help=f"hub data dir (default: ${DATA_DIR_ENV} or {DEFAULT_DATA_DIR})")
    parser.add_argument(
        "--print-sink",
        action="store_true",
        help="print the resolved sink path and exit (troubleshooting; reads no stdin)",
    )
    return parser


def run(
    *,
    sink: str | None = None,
    data_dir: str | None = None,
    print_sink: bool = False,
    wrapped: Sequence[str] = (),
) -> int:
    """Capture usage from stdin, then pass through to the wrapped command.

    The whole behavior of the writer, shared by its two spellings — the legacy
    ``orch-hub-statusline`` console script and the ``orch-hub statusline``
    subcommand a compiled release reaches (:mod:`orch_hub.cli`) — so an operator
    pointing ``statusLine.command`` at either gets the identical wrapper rather
    than two drifting copies of it.

    Exit code is the wrapped command's, or ``0`` when none is configured — the
    capture result never changes it, because a status line's success has nothing
    to do with the hub's display bookkeeping."""
    path = resolve_sink(sink=sink, data_dir=data_dir)
    if print_sink:
        print(path)
        return 0

    # Read stdin whole (the CLI is the producer and closes the pipe) so the wrapped
    # command receives byte-identical input; the size guard bounds parsing only.
    try:
        raw = sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001 — no stdin is not a reason to skip pass-through
        raw = b""
    capture(raw, path)
    return passthrough(wrapped, raw)


def main(argv: Sequence[str] | None = None) -> int:
    """Legacy ``orch-hub-statusline`` console-script entry point."""
    own, wrapped = split_argv(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(own)
    return run(
        sink=args.sink,
        data_dir=args.data_dir,
        print_sink=args.print_sink,
        wrapped=wrapped,
    )


def passthrough(command: Sequence[str], stdin_bytes: bytes) -> int:
    """Run the operator's own statusLine command with the same stdin, relaying its
    output untouched.

    ``stdout``/``stderr`` are inherited rather than captured, so the rendered
    status line — colours, control sequences, trailing bytes — is exactly what it
    was before this wrapper was installed. No shell is used: the argv after ``--``
    is executed directly, so nothing in the payload or environment can be
    interpreted as shell syntax (a shell pipeline can still be requested
    explicitly with ``-- /bin/sh -c '...'``). An empty command prints nothing and
    succeeds; a command that cannot be executed reports on stderr and returns
    ``127`` without printing a partial status line."""
    if not command:
        return 0
    try:
        return subprocess.run(list(command), input=stdin_bytes, check=False).returncode
    except OSError as exc:
        print(f"orch-hub-statusline: cannot run {command[0]!r}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
