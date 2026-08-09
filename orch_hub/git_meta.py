"""Best-effort git reads for managed repos: commit metadata and scoped patches.

Separate from :mod:`orch_hub.readiness`: readiness runs git as a launch *gate*
(is the working tree clean?), while this reads history for the dashboard's repo
cards (latest commit), the repo-detail view (recent commits), and the
task-evaluation evidence layer (a commit window, and a pathspec-scoped patch
stream for :mod:`orch_hub.ai_snapshot`).

Every read is best-effort. A missing ``git`` executable, a path that is not a
git work tree, or a failed ``git log`` degrades to "unavailable"
(:class:`CommitLog`/:class:`DiffLog` with ``available=False`` /
:func:`latest_commit` returning ``None``) rather than raising — a git problem
must never hide the repo, its readiness, or an evidence pack. Git is invoked
bare (``git``, on ``PATH``), matching the readiness git-clean check.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CommitInfo, ManagedRepo
from .subprocess_utils import CommandNotFoundError, CommandResult, Runner, run_command

DEFAULT_COMMIT_LIMIT = 10
"""Default number of recent commits for the repo-detail list."""

MAX_COMMIT_LIMIT = 50
"""Hard cap so a crafted ``limit`` cannot ask git for an unbounded history."""

# ASCII unit separator: a field delimiter that cannot appear in the single-line
# ``%s`` subject (git never emits control characters there), so each formatted
# line splits cleanly into exactly four fields.
_FIELD_SEP = "\x1f"
_COMMIT_FORMAT = _FIELD_SEP.join(("%H", "%h", "%cI", "%s"))


@dataclass(frozen=True)
class CommitLog:
    """Result of reading a repo's recent commits.

    ``available`` is False when git could not be read at all — a missing git
    executable, a path that is not a git work tree, or a non-zero ``git log`` —
    in which case ``commits`` is empty. An ``available`` log may still be empty
    in the (not expected for a managed repo) case of no matching history; the
    distinction lets the dashboard show "unavailable" rather than "no commits".
    """

    available: bool
    commits: tuple[CommitInfo, ...]


def read_commits(
    repo: ManagedRepo,
    *,
    limit: int = DEFAULT_COMMIT_LIMIT,
    runner: Runner = run_command,
) -> CommitLog:
    """Return up to ``limit`` recent commits for ``repo``, newest first.

    ``limit`` is clamped to ``1..MAX_COMMIT_LIMIT``. Returns an unavailable log
    (never raises) when the repo path is not a directory, git is missing, or the
    command fails, so callers can treat a git problem as a graceful empty state.
    """
    return _git_log(repo, count=_clamp_limit(limit), extra=(), runner=runner)


def read_commits_between(
    repo: ManagedRepo,
    *,
    since: str | None,
    until: str | None = None,
    limit: int = MAX_COMMIT_LIMIT,
    runner: Runner = run_command,
) -> CommitLog:
    """Return up to ``limit`` commits in a time window for ``repo``, newest first.

    Same best-effort format and fail-closed degradation as :func:`read_commits`,
    adding ``--since``/``--until`` bounds. The task-evaluation evidence builder
    uses it to capture the commits made during a task's run lifecycle window
    (:mod:`orch_hub.evaluation`). ``since``/``until`` are passed to git verbatim
    (git parses ISO-8601 with offset); a ``None`` bound is omitted. Windowing is
    by git's commit date over history reachable from HEAD, so the result is
    ambient repo history in that window — deliberately *not* a task-scoped filter
    (other work committed in the window is included). ``limit`` defaults to the
    hard cap and is clamped to ``1..MAX_COMMIT_LIMIT`` so a wide window cannot ask
    git for unbounded history.
    """
    extra: list[str] = []
    if since:
        extra.append(f"--since={since}")
    if until:
        extra.append(f"--until={until}")
    return _git_log(repo, count=_clamp_limit(limit), extra=tuple(extra), runner=runner)


@dataclass(frozen=True)
class DiffLog:
    """Result of reading a pathspec-scoped patch stream over a commit window.

    ``available`` is False on the same conditions as :class:`CommitLog` — a
    missing git executable, a path that is not a git work tree, or a non-zero
    ``git log`` — in which case ``text`` is empty. An ``available`` log may also
    be empty, which is a *fact* rather than a gap: nothing under the pathspec
    changed in the window. Callers must keep the two apart, since "git could not
    be read" and "the path was untouched" support opposite conclusions.

    ``text`` is bounded by the caller's ``max_bytes`` and holds the *head* of
    git's output (newest commits first). ``size`` is the full output size, so a
    consumer can report how much was cut, and ``truncated`` says the cap bound.
    The cap value is the caller's policy (:mod:`orch_hub.ai_snapshot` owns the
    ``.ai/`` budgets); enforcing it *here*, at the producer, is this module's:
    the patch stream is repo-content-sized, so a byte bound applied after an
    unbounded capture would bound only what is kept, not what is read.
    """

    available: bool
    text: str = ""
    size: int = 0
    truncated: bool = False


_DIFF_HEADER_FORMAT = "commit %h %cI %s"
"""Compact per-commit header preceding each patch: short hash, committer date,
subject. ``git log -p``'s default header block (author, email, wrapped body) is
padding in an evidence pack that already carries commit metadata separately."""


def read_path_diff_between(
    repo: ManagedRepo,
    *,
    pathspec: str,
    since: str | None,
    until: str | None = None,
    max_bytes: int,
    limit: int = MAX_COMMIT_LIMIT,
    runner: Runner = run_command,
) -> DiffLog:
    """Return the patches touching ``pathspec`` in a time window, newest first.

    The patch counterpart of :func:`read_commits_between`, sharing its exact
    windowing semantics (``--since``/``--until`` over git's commit date, history
    reachable from HEAD) so a caller can pair a commit window with the patches
    for one path inside it without a second, differently-bounded notion of "the
    window". ``limit`` caps the number of *commits* shown — after pathspec
    filtering, so it counts commits that actually touched the path — and is
    clamped to ``1..MAX_COMMIT_LIMIT``.

    ``max_bytes`` is required, not defaulted: unlike a commit list, whose size is
    the format times the commit cap, a patch stream is as large as the repo's
    content makes it, so every caller must name a bound. It is pushed down to the
    runner (:func:`orch_hub.subprocess_utils.run_command`'s capped mode), which
    retains only that many bytes and drains the rest — the read itself is bounded,
    not just the retained slice. A runner that does not measure (an injected test
    fake) is re-bounded here from its returned string, so no runner can widen the
    contract.

    Rendering is pinned so the output is a function of the repo alone: no color,
    no external diff driver, no textconv filter — a repo-local ``diff.external``
    or a ``.gitattributes`` textconv would otherwise run arbitrary configured
    commands and change what the evidence records. Same best-effort degradation
    as every other read here: an unavailable log, never an exception.
    """
    if not repo.path.is_dir():
        return DiffLog(available=False)
    cap = max(0, max_bytes)
    extra: list[str] = []
    if since:
        extra.append(f"--since={since}")
    if until:
        extra.append(f"--until={until}")
    try:
        result = runner(
            [
                "git",
                "log",
                f"-{_clamp_limit(limit)}",
                "--patch",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                f"--pretty=format:{_DIFF_HEADER_FORMAT}",
                *extra,
                "--",
                pathspec,
            ],
            cwd=repo.path,
            max_stdout_bytes=cap,
        )
    except CommandNotFoundError:
        return DiffLog(available=False)
    if not result.ok:
        return DiffLog(available=False)
    text, size, truncated = _bounded_stdout(result, cap)
    return DiffLog(available=True, text=text, size=size, truncated=truncated)


def _bounded_stdout(result: CommandResult, cap: int) -> tuple[str, int, bool]:
    """Return ``(head text, full size, truncated)`` for a capped command result.

    A runner that measured its own output (the capped mode) already returns the
    head plus the true size, and is trusted: re-slicing its decoded text could
    cut a replacement character it had to insert at the byte boundary. A runner
    that did not measure (``stdout_bytes`` None — an unbounded capture, or a test
    fake ignoring the cap) has its string bounded here instead, so the returned
    log honors the cap either way.
    """
    if result.stdout_bytes is not None:
        return result.stdout, result.stdout_bytes, result.stdout_truncated
    data = result.stdout.encode("utf-8")
    if len(data) <= cap:
        return result.stdout, len(data), False
    return data[:cap].decode("utf-8", errors="replace"), len(data), True


def _clamp_limit(limit: int) -> int:
    """Clamp a requested commit count to ``1..MAX_COMMIT_LIMIT``."""
    return max(1, min(limit, MAX_COMMIT_LIMIT))


def _git_log(
    repo: ManagedRepo,
    *,
    count: int,
    extra: tuple[str, ...],
    runner: Runner,
) -> CommitLog:
    """Run ``git log -{count} <format> <extra>`` in ``repo``, best-effort.

    Shared by :func:`read_commits` and :func:`read_commits_between`. Returns an
    unavailable log (never raises) when the repo path is not a directory, git is
    missing, or the command exits non-zero, so a git problem is a graceful empty
    state rather than a caller-visible error.
    """
    if not repo.path.is_dir():
        return CommitLog(available=False, commits=())
    try:
        result = runner(
            ["git", "log", f"-{count}", f"--pretty=format:{_COMMIT_FORMAT}", *extra],
            cwd=repo.path,
        )
    except CommandNotFoundError:
        return CommitLog(available=False, commits=())
    if not result.ok:
        return CommitLog(available=False, commits=())
    commits = tuple(
        commit
        for commit in (_parse_line(line) for line in result.stdout.splitlines())
        if commit is not None
    )
    return CommitLog(available=True, commits=commits)


def latest_commit(
    repo: ManagedRepo, *, runner: Runner = run_command
) -> CommitInfo | None:
    """Return ``repo``'s most recent commit, or ``None`` when unavailable.

    Thin wrapper over :func:`read_commits` for the repo card, which needs only
    the newest commit. ``None`` covers both "git could not be read" and the
    (not expected) empty-history case; the card simply shows no commit time.
    """
    log = read_commits(repo, limit=1, runner=runner)
    return log.commits[0] if log.commits else None


def _parse_line(line: str) -> CommitInfo | None:
    """Parse one ``%H\\x1f%h\\x1f%cI\\x1f%s`` line, or ``None`` if malformed."""
    parts = line.split(_FIELD_SEP)
    if len(parts) != 4:
        return None
    full, short, committed, subject = parts
    if not full:
        return None
    return CommitInfo(
        hash=full, short_hash=short, committed_ts=committed, subject=subject
    )
