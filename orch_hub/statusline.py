"""Claude Code statusLine writer: capture the CLI's own subscription usage.

Claude Code publishes the five-hour / seven-day subscription *figures* to exactly
one local channel: the JSON payload it pipes to a configured statusLine
command's stdin, as ``rate_limits.{five_hour,seven_day}.{used_percentage,
resets_at}`` ("only present for subscribers after first API response"). Its
``claude -p`` stream-json stdout carries no used-percentage on any verified
version — nothing at all on Claude Code 2.1.218, and on 2.1.220 a
``rate_limit_event`` naming one window and its reset instant but no figure (see
the two Claude section notes in :mod:`orch_hub.usage`). A hub-driven turn can
therefore keep a window's *boundary* current, but only this channel can say how
much of it is used.

This module is the small writer that reads that payload and persists the
rate-limit part to a hub-readable sink, applying the *Codex pattern* to Claude:
consume what the operator's own sessions already write, with no probe turn, no
private endpoint, and no UI scrape. :mod:`orch_hub.usage` reads the sink back
into the ordinary observation store, so the read path, cache, ``GET /api/usage``
payload, and Overview cards are unchanged.

Two properties make installing it safe:

- **Operator-installed, never hub-installed.** The hub does not write
  ``~/.claude/**``; the operator points their own ``statusLine.command`` at this
  script (``docs/claude-usage-statusline.md``). Uninstalled, Claude usage reads
  exactly as unavailable as it does today.
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
from dataclasses import dataclass
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
    temps make each swap independent and last-write-wins — which is correct, since
    every session reports the same account-level windows. Mirrors
    :meth:`orch_hub.usage.ClaudeUsageStore.record`.

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


# -- Deploy-time setup guidance -----------------------------------------------
#
# The writer above only matters once the operator points a Claude Code
# statusLine at it. Nothing in the hub can do that — editing ``~/.claude/**`` is
# off-limits — so ``orch-hub launchd deploy/update`` inspects the operator's
# Claude settings read-only and, when the wrapper is not in the effective
# statusLine command, prints copy-ready guidance. This is the detection half;
# the CLI prints what :func:`setup_guidance_lines` returns.

CLAUDE_SETTINGS_DIRNAME = ".claude"
"""User-level Claude Code config dir, relative to ``$HOME`` (matches the doc)."""

CLAUDE_SETTINGS_FILENAMES = ("settings.local.json", "settings.json")
"""Settings files in precedence order: ``settings.local.json`` overrides
``settings.json`` when both define ``statusLine``. A file that omits
``statusLine`` does not shadow the next one — the first file with a statusLine
command is the effective one, mirroring Claude Code's per-key merge."""

SETUP_DOC = "docs/claude-usage-statusline.md"
"""Operator guide the deploy nudge points at for the full walkthrough."""

_STATUSLINE_KEY = "statusLine"
_COMMAND_KEY = "command"


@dataclass(frozen=True)
class StatuslineSetup:
    """Read-only view of whether the operator's Claude statusLine feeds the hub.

    ``configured`` is true when the effective statusLine command already routes
    through :data:`orch-hub-statusline <CLAUDE_STATUSLINE_NAME>` (any path or bare
    name), so no guidance is due. ``existing_command`` is the operator's current
    statusLine command when one is set but does not use the wrapper — the value to
    preserve after ``--`` in the suggested command; ``None`` when nothing is set."""

    configured: bool
    existing_command: str | None = None


def default_claude_settings_dir() -> Path:
    return Path.home() / CLAUDE_SETTINGS_DIRNAME


def references_wrapper(command: str) -> bool:
    """Whether a statusLine command already routes through ``orch-hub-statusline``.

    Matches the console-script name as a substring, so an absolute venv path, a
    bare name on ``PATH``, or a wrapper from a different checkout all count as
    "already wired" — any of them feeds the same sink, so nagging would be noise.
    The name is distinctive enough that a false positive is not a real risk."""
    return "orch-hub-statusline" in command


def _statusline_command_in(path: Path) -> str | None:
    """The ``statusLine.command`` string in one settings file, or ``None``.

    Fail-soft on untrusted external input: a missing/unreadable file, non-JSON,
    a non-object root, a missing/blank ``statusLine.command``, or an unexpected
    shape all yield ``None`` rather than raising."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    status_line = data.get(_STATUSLINE_KEY)
    if not isinstance(status_line, dict):
        return None
    command = status_line.get(_COMMAND_KEY)
    if isinstance(command, str) and command.strip():
        return command.strip()
    return None


def _effective_statusline_command(settings_dir: Path) -> str | None:
    for name in CLAUDE_SETTINGS_FILENAMES:
        command = _statusline_command_in(settings_dir / name)
        if command is not None:
            return command
    return None


def detect_statusline_setup(*, settings_dir: Path | None = None) -> StatuslineSetup:
    """Inspect the operator's Claude settings read-only for the wrapper.

    Never writes and never raises. ``settings_dir`` defaults to
    ``~/.claude``; it is injectable so tests need not touch a real home."""
    directory = settings_dir if settings_dir is not None else default_claude_settings_dir()
    command = _effective_statusline_command(directory)
    if command is None:
        return StatuslineSetup(configured=False)
    if references_wrapper(command):
        return StatuslineSetup(configured=True)
    return StatuslineSetup(configured=False, existing_command=command)


def _shell_single_quote(value: str) -> str:
    """POSIX single-quote ``value`` so a shell reproduces it byte-for-byte."""
    return "'" + value.replace("'", "'\\''") + "'"


def _is_non_default_data_dir(data_dir: Path) -> bool:
    """Whether the hub's data dir differs from the wrapper's built-in default.

    An interactive shell does not inherit the launchd service's environment, so
    a non-default ``ORCH_HUB_DATA_DIR`` must be passed to the wrapper explicitly
    or it writes to the wrong sink; only then is the ``--data-dir`` flag added."""
    try:
        return data_dir.expanduser().resolve() != DEFAULT_DATA_DIR.expanduser().resolve()
    except OSError:  # resolve() should not raise for missing paths, but stay total
        return str(data_dir) != str(DEFAULT_DATA_DIR)


def suggested_statusline_command(
    setup: StatuslineSetup, *, wrapper_path: Path, data_dir: Path
) -> str:
    """The copy-ready ``statusLine.command`` value tailored to this deployment.

    Uses the deployed wrapper's absolute path, adds ``--data-dir`` only for a
    non-default hub data dir, and preserves any existing operator command after
    ``--`` by running it through ``/bin/sh -c`` — which keeps shell features and
    ``~`` expansion that a bare argv append would silently drop."""
    parts: list[str] = [str(wrapper_path)]
    if _is_non_default_data_dir(data_dir):
        parts += ["--data-dir", str(data_dir)]
    if setup.existing_command:
        parts += ["--", "/bin/sh", "-c", _shell_single_quote(setup.existing_command)]
    return " ".join(parts)


def setup_guidance_lines(
    setup: StatuslineSetup, *, wrapper_path: Path, data_dir: Path
) -> list[str]:
    """Advisory lines nudging Claude-usage setup, or ``[]`` when already wired.

    Read-only guidance: it never edits ``~/.claude`` — the operator sets
    ``statusLine`` themselves. The lines are display-only and carry no hub secret
    (no token, no Bark URL); only paths and the operator's own command appear."""
    if setup.configured:
        return []
    command = suggested_statusline_command(
        setup, wrapper_path=wrapper_path, data_dir=data_dir
    )
    lines = [
        "claude usage: no orch-hub statusLine wrapper detected in ~/.claude",
        '  the Overview Claude subscription card stays "unavailable" until a Claude',
        "  Code session routes its statusLine through the wrapper. orch-hub never",
        "  edits ~/.claude; set statusLine.command in ~/.claude/settings.json to:",
        f"    {command}",
    ]
    if setup.existing_command:
        lines.append("  (your existing statusLine command is preserved after `--`)")
    lines.append(f"  setup guide: {SETUP_DOC}")
    return lines


def _split_argv(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split our own flags from the wrapped statusLine command after ``--``."""
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


def main(argv: Sequence[str] | None = None) -> int:
    """Capture usage from stdin, then pass through to the wrapped command.

    Exit code is the wrapped command's, or ``0`` when none is configured — the
    capture result never changes it, because a status line's success has nothing
    to do with the hub's display bookkeeping."""
    own, wrapped = _split_argv(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(own)
    sink = resolve_sink(sink=args.sink, data_dir=args.data_dir)
    if args.print_sink:
        print(sink)
        return 0

    # Read stdin whole (the CLI is the producer and closes the pipe) so the wrapped
    # command receives byte-identical input; the size guard bounds parsing only.
    try:
        raw = sys.stdin.buffer.read()
    except Exception:  # noqa: BLE001 — no stdin is not a reason to skip pass-through
        raw = b""
    capture(raw, sink)
    return passthrough(wrapped, raw)


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
