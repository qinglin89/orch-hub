"""Launch-readiness checks for a managed repo.

Readiness fails closed: any missing deploy piece, a dirty working tree, a
failing ``mandrel status``, or a missing tool blocks launch. The report always
carries the same seven checks in a stable order so callers (CLI today, API and
dashboard later) can render a consistent shape.

``mandrel status`` is asked for its machine payload (``--json``) so one call
answers two questions: the gate (its exit code, which ``--json`` does not
change) and the display-only protocol placement it reports alongside
(:class:`~orch_hub.models.ProtocolPlacement`). The second is a passenger on the
first — the placement is read per request from the payload of the call the gate
already makes, never from a second subprocess and never from a run's
launch-frozen capture, which would show the label of the last run rather than
what the repo holds now.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .models import ManagedRepo, ProtocolPlacement, ReadinessCheck, ReadinessReport
from .subprocess_utils import CommandNotFoundError, Runner, run_command

STATUS_PLACED_LINE_SCHEMA = 2
"""First ``mandrel status --json`` schema version carrying ``placed_line``.

``mandrel`` versions that payload by its key set — every key added, removed, or
repurposed moves the number — and states that a consumer should read the version
rather than probe for keys. So the placement is taken only from a payload whose
version names the field; an older mandrel reports a version this hub then
declines to read a line out of, rather than one it guesses at.
"""


def check_readiness(
    repo: ManagedRepo,
    config: Config,
    *,
    runner: Runner = run_command,
) -> ReadinessReport:
    """Run all readiness checks for ``repo`` and aggregate a report.

    Filesystem checks always run. The git-clean and ``mandrel status`` checks run
    only when the repo path is a directory; otherwise they are recorded as
    failed (skipped) so the report keeps its seven-check shape and fails closed.

    A skipped ``mandrel status`` also leaves the protocol placement unread: no
    payload was produced, so there is no line to relay and none is invented.
    """
    path = repo.path
    checks: list[ReadinessCheck] = []
    placement = ProtocolPlacement()

    path_exists = path.is_dir()
    checks.append(
        ReadinessCheck(
            "repo_path_exists",
            path_exists,
            f"repo path present: {path}"
            if path_exists
            else f"repo path missing: {path}",
        )
    )
    checks.append(_file_check("manifest_exists", repo.manifest))
    checks.append(
        _file_check(
            "orchestrator_exists",
            path / ".mandrel" / "orchestrator" / "orchestrator.py",
        )
    )
    checks.append(
        _file_check(
            "venv_python_exists",
            path / ".mandrel" / "orchestrator" / ".venv" / "bin" / "python",
        )
    )
    checks.append(_file_check("task_index_exists", path / ".ai-tasks" / "index.md"))

    if path_exists:
        checks.append(_git_clean_check(path, runner))
        status_check, placement = _mandrel_status_check(repo, config, runner)
        checks.append(status_check)
    else:
        reason = "skipped: repo path missing"
        checks.append(ReadinessCheck("git_clean", False, reason))
        checks.append(ReadinessCheck("mandrel_status", False, reason))

    return ReadinessReport(tuple(checks), placement)


def _file_check(name: str, target: Path) -> ReadinessCheck:
    exists = target.exists()
    return ReadinessCheck(
        name,
        exists,
        f"found: {target}" if exists else f"missing: {target}",
    )


def _git_clean_check(repo_path: Path, runner: Runner) -> ReadinessCheck:
    try:
        result = runner(["git", "status", "--porcelain"], cwd=repo_path)
    except CommandNotFoundError:
        return ReadinessCheck("git_clean", False, "git executable not found")
    if not result.ok:
        detail = (
            _first_line(result.stderr)
            or _first_line(result.stdout)
            or f"exit {result.returncode}"
        )
        return ReadinessCheck("git_clean", False, f"git status failed: {detail}")
    clean = result.stdout.strip() == ""
    return ReadinessCheck(
        "git_clean",
        clean,
        "working tree clean" if clean else "working tree has uncommitted changes",
    )


def _mandrel_status_check(
    repo: ManagedRepo, config: Config, runner: Runner
) -> tuple[ReadinessCheck, ProtocolPlacement]:
    """Run ``mandrel status --json <repo>`` once; read the gate and the label.

    The gate is unchanged and stays on the exit code, which ``--json`` does not
    touch (0 in sync, 1 drift, 2 error). Only the failure *reason* moves: the
    prose the old invocation put on stdout is now the payload, so the reason is
    rebuilt from it, falling back to whatever text the command did emit when the
    payload cannot be parsed — an unrecognized flag on a mandrel older than the
    JSON surface prints usage on stderr, which is the visible degradation.

    The placement is read from the same payload and is independent of the
    verdict: a repo can fail on a dirty tree while standing squarely on a line,
    and a repo can pass while the payload says nothing this hub can read.
    """
    try:
        result = runner([config.mandrel_bin, "status", "--json", repo.path])
    except CommandNotFoundError:
        return (
            ReadinessCheck(
                "mandrel_status", False, f"mandrel not found: {config.mandrel_bin}"
            ),
            ProtocolPlacement(),
        )
    payload = _status_payload(result.stdout)
    placement = _read_placement(payload)
    if result.ok:
        return ReadinessCheck("mandrel_status", True, "mandrel status ok"), placement
    detail = (
        _drift_summary(payload)
        or _first_line(result.stdout)
        or _first_line(result.stderr)
        or f"exit {result.returncode}"
    )
    return (
        ReadinessCheck("mandrel_status", False, f"mandrel status failed: {detail}"),
        placement,
    )


def _status_payload(stdout: str) -> dict | None:
    """The parsed ``status --json`` envelope, or ``None`` if it is not one."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _status_target(payload: dict | None) -> dict | None:
    """The single target of a one-repo reading.

    ``status <path>`` is a sweep of one, so anything but exactly one target is a
    payload this hub did not ask for and does not read.
    """
    if payload is None:
        return None
    targets = payload.get("targets")
    if not isinstance(targets, list) or len(targets) != 1:
        return None
    target = targets[0]
    return target if isinstance(target, dict) else None


def _read_placement(payload: dict | None) -> ProtocolPlacement:
    """Relay what ``mandrel`` placed, or nothing.

    Values travel opaquely: the hub keeps no dictionary of line names, so a line
    it has never seen is returned as itself rather than bucketed into one it
    recognizes. Nothing here compares revisions or orders one line against
    another — it reads two strings out of a document.
    """
    target = _status_target(payload)
    if payload is None or target is None:
        return ProtocolPlacement()
    version = payload.get("schema_version")
    if not isinstance(version, int) or version < STATUS_PLACED_LINE_SCHEMA:
        return ProtocolPlacement()
    line = target.get("placed_line")
    if isinstance(line, str) and line.strip():
        return ProtocolPlacement(line=line.strip())
    return ProtocolPlacement(finding=_placement_finding(target))


def _placement_finding(target: dict) -> str | None:
    """``mandrel``'s own name for a reading that placed the repo on no line.

    Chosen structurally rather than from a list of kinds: ``mandrel`` carries a
    ``placement`` object on exactly the findings its placement comparison
    produced and ``null`` on every other kind, so the hub needs no opinion about
    which kinds are placement kinds. That structure also draws the right line
    for free — a receipt that never got as far as a placement, and a source line
    *this checkout* could not resolve, carry no placement object, and neither is
    a fact about where the target stands.
    """
    drifts = target.get("drifts")
    if not isinstance(drifts, list):
        return None
    for drift in drifts:
        if not isinstance(drift, dict) or drift.get("placement") is None:
            continue
        kind = drift.get("kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip()
    return None


def _drift_summary(payload: dict | None) -> str:
    """The concise failure reason, counted from the payload's own findings.

    Deliberately just the count, matching the first line of the prose form this
    replaces: the per-file detail below it is what the repo card must not grow.
    """
    target = _status_target(payload)
    if target is None:
        return ""
    drifts = target.get("drifts")
    if not isinstance(drifts, list) or not drifts:
        return ""
    count = len(drifts)
    return f"drift detected ({count} issue{'' if count == 1 else 's'})"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
