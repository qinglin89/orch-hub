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
    count = max(1, min(limit, MAX_COMMIT_LIMIT))
    if not repo.path.is_dir():
        return CommitLog(available=False, commits=())
    try:
        result = runner(
            ["git", "log", f"-{count}", f"--pretty=format:{_COMMIT_FORMAT}"],
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
