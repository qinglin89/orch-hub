"""Tests for best-effort git commit metadata and pathspec-scoped patch reads.

Injects a fake runner (never a real git subprocess) and uses ``tmp_path`` for
the repo dir so ``read_commits`` / ``latest_commit`` are exercised without
touching real git history. The core contract under test is best-effort: any git
problem degrades to an unavailable log / ``None``, never an exception.

The patch reader adds a second contract: its byte cap reaches the command
(a fake runner records the kwarg) and still binds when a runner ignores it. What
the *real* capped runner does with an oversized stream is
``tests/test_subprocess_utils.py``.
"""

from __future__ import annotations

from pathlib import Path

from orch_hub.git_meta import (
    MAX_COMMIT_LIMIT,
    CommitLog,
    latest_commit,
    read_commits,
    read_commits_between,
    read_path_diff_between,
)
from orch_hub.models import CommitInfo, ManagedRepo
from orch_hub.subprocess_utils import CommandNotFoundError, CommandResult

FIELD_SEP = "\x1f"


def make_repo(path: Path) -> ManagedRepo:
    return ManagedRepo(name="r", path=path, manifest=path / ".ai-deploy-manifest.json")


def commit_line(full: str, short: str, ts: str, subject: str) -> str:
    """One line in the ``%H\\x1f%h\\x1f%cI\\x1f%s`` format git is asked for."""
    return FIELD_SEP.join((full, short, ts, subject))


def make_runner(*, stdout: str = "", rc: int = 0, calls: list | None = None):
    """A fake runner recording each call and returning canned git output."""

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if calls is not None:
            calls.append({"argv": argv, "cwd": kwargs.get("cwd")})
        return CommandResult(tuple(argv), rc, stdout, "")

    return runner


def test_latest_commit_parses_git_log(tmp_path):
    line = commit_line(
        "a" * 40, "aaaaaaa", "2026-07-09T16:25:00+08:00", "update ai-protocal"
    )
    commit = latest_commit(make_repo(tmp_path), runner=make_runner(stdout=line))
    assert commit == CommitInfo(
        hash="a" * 40,
        short_hash="aaaaaaa",
        committed_ts="2026-07-09T16:25:00+08:00",
        subject="update ai-protocal",
    )


def test_recent_commits_parses_multiple_newest_first(tmp_path):
    stdout = "\n".join(
        [
            commit_line("h1" + "0" * 38, "h1", "2026-07-09T10:00:00+00:00", "newest"),
            commit_line("h2" + "0" * 38, "h2", "2026-07-08T10:00:00+00:00", "older"),
        ]
    )
    log = read_commits(make_repo(tmp_path), limit=5, runner=make_runner(stdout=stdout))
    assert log.available is True
    # git log is reverse-chronological, so the parsed order is newest-first.
    assert [c.subject for c in log.commits] == ["newest", "older"]
    assert log.commits[0].short_hash == "h1"


def test_read_commits_invokes_git_with_cwd_and_clamped_limit(tmp_path):
    calls: list = []
    read_commits(
        make_repo(tmp_path), limit=1000, runner=make_runner(calls=calls)
    )
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == "git" and argv[1] == "log"
    # An oversized limit is clamped to the hard cap before reaching git as `-N`.
    assert f"-{MAX_COMMIT_LIMIT}" in argv
    # Git runs in the repo work tree (matches the readiness git-clean check).
    assert str(calls[0]["cwd"]) == str(tmp_path)


def test_limit_below_one_is_clamped_up(tmp_path):
    calls: list = []
    read_commits(make_repo(tmp_path), limit=0, runner=make_runner(calls=calls))
    assert "-1" in calls[0]["argv"]


def test_read_commits_unavailable_when_git_missing(tmp_path):
    def runner(args, **kwargs):
        raise CommandNotFoundError("git")

    assert read_commits(make_repo(tmp_path), runner=runner) == CommitLog(
        available=False, commits=()
    )


def test_read_commits_unavailable_on_nonzero_exit(tmp_path):
    log = read_commits(make_repo(tmp_path), runner=make_runner(rc=128))
    assert log.available is False
    assert log.commits == ()


def test_read_commits_unavailable_when_path_missing(tmp_path):
    missing = tmp_path / "gone"
    calls: list = []
    log = read_commits(make_repo(missing), runner=make_runner(calls=calls))
    assert log.available is False
    assert calls == []  # runner never invoked when the path is not a dir


def test_latest_commit_none_when_unavailable(tmp_path):
    assert latest_commit(make_repo(tmp_path), runner=make_runner(rc=1)) is None


def test_parse_skips_malformed_lines(tmp_path):
    stdout = "\n".join(
        [
            "not-enough-fields",  # wrong field count → skipped
            commit_line("h" * 40, "h", "2026-07-09T00:00:00+00:00", "good"),
            FIELD_SEP.join(("", "x", "2026-07-09T00:00:00+00:00", "empty hash")),
        ]
    )
    log = read_commits(make_repo(tmp_path), runner=make_runner(stdout=stdout))
    # Only the well-formed, non-empty-hash line survives.
    assert [c.subject for c in log.commits] == ["good"]


def test_subject_with_colons_and_spaces_is_preserved(tmp_path):
    # The subject can contain ':' and spaces; the unit-separator split keeps it
    # intact (a naive ':'-split would corrupt a conventional-commit subject).
    subject = "feat(cli): add repo readiness command"
    line = commit_line("h" * 40, "h", "2026-07-09T00:00:00+00:00", subject)
    commit = latest_commit(make_repo(tmp_path), runner=make_runner(stdout=line))
    assert commit.subject == subject


# -- read_commits_between (windowed, best-effort) -----------------------------


def test_read_commits_between_passes_window_and_clamps_limit(tmp_path):
    calls: list = []
    read_commits_between(
        make_repo(tmp_path),
        since="2026-07-22T09:00:00+00:00",
        until="2026-07-22T10:00:00+00:00",
        limit=1000,
        runner=make_runner(calls=calls),
    )
    argv = calls[0]["argv"]
    assert argv[0] == "git" and argv[1] == "log"
    assert "--since=2026-07-22T09:00:00+00:00" in argv
    assert "--until=2026-07-22T10:00:00+00:00" in argv
    # An oversized limit is clamped to the hard cap, like read_commits.
    assert f"-{MAX_COMMIT_LIMIT}" in argv
    assert str(calls[0]["cwd"]) == str(tmp_path)


def test_read_commits_between_omits_absent_bounds(tmp_path):
    calls: list = []
    read_commits_between(
        make_repo(tmp_path), since=None, until=None, runner=make_runner(calls=calls)
    )
    argv = calls[0]["argv"]
    assert not any(a.startswith("--since=") for a in argv)
    assert not any(a.startswith("--until=") for a in argv)


def test_read_commits_between_parses_commits(tmp_path):
    line = commit_line("h" * 40, "hhhhhhh", "2026-07-22T09:30:00+00:00", "windowed")
    log = read_commits_between(
        make_repo(tmp_path),
        since="2026-07-22T09:00:00+00:00",
        runner=make_runner(stdout=line),
    )
    assert log.available is True
    assert [c.subject for c in log.commits] == ["windowed"]


def test_read_commits_between_unavailable_on_failure(tmp_path):
    log = read_commits_between(
        make_repo(tmp_path), since="2026-07-22T09:00:00+00:00", runner=make_runner(rc=128)
    )
    assert log == CommitLog(available=False, commits=())


def test_read_commits_between_unavailable_when_path_missing(tmp_path):
    calls: list = []
    log = read_commits_between(
        make_repo(tmp_path / "gone"),
        since="2026-07-22T09:00:00+00:00",
        runner=make_runner(calls=calls),
    )
    assert log.available is False
    assert calls == []  # runner never invoked when the path is not a dir


# -- pathspec-scoped patch stream (byte-bounded at the producer) ---------------

SINCE = "2026-08-09T09:00:00+00:00"


def test_path_diff_pushes_the_byte_cap_into_the_command(tmp_path):
    # The cap has to reach the runner: a patch stream is repo-content-sized, so
    # bounding it only after capture would bound what is kept, not what is read.
    calls: list = []

    def runner(args, **kwargs):
        calls.append(kwargs)
        return CommandResult(tuple(str(a) for a in args), 0, "", "")

    read_path_diff_between(
        make_repo(tmp_path), pathspec=".ai", since=SINCE, max_bytes=4096, runner=runner
    )

    assert calls[0]["max_stdout_bytes"] == 4096


def test_path_diff_trusts_a_runner_that_measured_its_own_output(tmp_path):
    # The capped runner returns the head plus the true size; re-slicing its text
    # could cut the replacement character it had to insert at the byte boundary.
    def runner(args, **kwargs):
        return CommandResult(
            tuple(str(a) for a in args), 0, "head", "", stdout_bytes=9000,
            stdout_truncated=True,
        )

    log = read_path_diff_between(
        make_repo(tmp_path), pathspec=".ai", since=SINCE, max_bytes=4, runner=runner
    )

    assert log.available is True
    assert log.text == "head" and log.size == 9000 and log.truncated is True


def test_path_diff_bounds_a_runner_that_ignored_the_cap(tmp_path):
    # An unmeasured capture (an injected fake, or an unbounded runner) must not
    # be able to widen the contract the caller asked for.
    log = read_path_diff_between(
        make_repo(tmp_path),
        pathspec=".ai",
        since=SINCE,
        max_bytes=100,
        runner=make_runner(stdout="d" * 4000),
    )

    assert len(log.text) == 100 and log.size == 4000 and log.truncated is True


def test_path_diff_below_the_cap_is_whole_and_untruncated(tmp_path):
    log = read_path_diff_between(
        make_repo(tmp_path),
        pathspec=".ai",
        since=SINCE,
        max_bytes=100,
        runner=make_runner(stdout="commit abc\n+ line\n"),
    )

    assert log.text == "commit abc\n+ line\n"
    assert log.size == len("commit abc\n+ line\n") and log.truncated is False


def test_path_diff_unavailable_on_failure_and_missing_git(tmp_path):
    repo = make_repo(tmp_path)

    def missing(args, **kwargs):
        raise CommandNotFoundError("git")

    failed = read_path_diff_between(
        repo, pathspec=".ai", since=SINCE, max_bytes=100, runner=make_runner(rc=128)
    )
    absent = read_path_diff_between(
        repo, pathspec=".ai", since=SINCE, max_bytes=100, runner=missing
    )

    assert failed.available is False and failed.text == ""
    assert absent.available is False


def test_path_diff_scopes_the_pathspec_and_pins_rendering(tmp_path):
    calls: list = []
    read_path_diff_between(
        make_repo(tmp_path),
        pathspec=".ai",
        since=SINCE,
        until="2026-08-09T10:00:00+00:00",
        max_bytes=100,
        runner=make_runner(calls=calls),
    )

    argv = calls[0]["argv"]
    assert argv[:2] == ["git", "log"]
    assert argv[-2:] == ["--", ".ai"]
    for pinned in ("--patch", "--no-color", "--no-ext-diff", "--no-textconv"):
        assert pinned in argv
    assert f"-{MAX_COMMIT_LIMIT}" in argv
