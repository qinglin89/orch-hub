"""Best-effort git commit metadata for managed repos (display-only).

Separate from :mod:`orch_hub.readiness`: readiness runs git as a launch *gate*
(is the working tree clean?), while this reads commit history for the
dashboard's repo cards (latest commit) and repo-detail view (recent commits).

Every read is best-effort. A missing ``git`` executable, a path that is not a
git work tree, or a failed ``git log`` degrades to "unavailable"
(:class:`CommitLog` with ``available=False`` / :func:`latest_commit` returning
``None``) rather than raising — a commit-lookup problem must never hide the
repo or its readiness. Git is invoked bare (``git``, on ``PATH``), matching the
readiness git-clean check.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CommitInfo, ManagedRepo
from .subprocess_utils import CommandNotFoundError, Runner, run_command

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
