"""Tests for the bounded ``.ai/`` memory-snapshot evidence reader.

Covers the four things the reader has to get right: eager-entrypoint resolution
in both legal forms (router first, then file shape), the task's ``prefetch:``
slice, the caps, and total degradation — an absent ``.ai/``, a missing
entrypoint, an unreadable or escaping file, and a cap hit each produce a warning
plus a false/degraded flag rather than an exception.

Fake repos live in ``tmp_path`` and git is always an injected fake runner, so no
test touches a real repo, the real snapshot, or a real subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orch_hub.ai_snapshot import (
    AI_DIRNAME,
    DIFF_COMMIT_LIMIT,
    EAGER_TOPICS,
    PREFETCH_MAX_DOCS,
    ROLE_EAGER,
    ROLE_PREFETCH,
    ROOT_DOCS,
    read_ai_snapshot,
    read_router,
    resolve_entrypoint,
)
from orch_hub.models import ManagedRepo
from orch_hub.subprocess_utils import CommandNotFoundError, CommandResult

SINCE = "2026-08-09T09:00:00+00:00"
UNTIL = "2026-08-09T10:00:00+00:00"

SINGLE_FILE_BODIES = {
    "map.md": "# Map\nfeature -> module\n",
    "overview.md": "# Overview\npurpose\n",
    "architecture.md": "# Architecture\nlayers\n",
    "design.md": "# Design\nprinciples\n",
    "conventions.md": "# Conventions\nstyle\n",
}
"""The eager set minus ``index.md``, which every fixture writes as a router."""


def make_repo(tmp_path: Path, name: str = "alpha") -> ManagedRepo:
    path = tmp_path / "repos" / name
    path.mkdir(parents=True)
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def router_md(routes: dict[str, str]) -> str:
    """Render a ``.ai/index.md`` document table routing ``{label: file}``."""
    rows = "\n".join(f"| {label} | {path} | when |" for label, path in routes.items())
    return f"# AI Knowledge Router\n\n| Document | File | Use when |\n|---|---|---|\n{rows}\n"


DEFAULT_ROUTES = {
    "Overview": "overview.md",
    "Architecture": "architecture.md",
    "Design": "design.md",
    "Conventions": "conventions.md",
    "Map": "map.md",
}


def write_ai(repo: ManagedRepo, docs: dict[str, str]) -> Path:
    """Write ``{.ai/-relative path: body}`` into the repo's snapshot dir."""
    ai_dir = repo.path / AI_DIRNAME
    for rel, body in docs.items():
        target = ai_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return ai_dir


def write_single_file_snapshot(repo: ManagedRepo, **extra: str) -> Path:
    """A conforming snapshot with every eager topic in single-file form."""
    docs = {"index.md": router_md(DEFAULT_ROUTES), **SINGLE_FILE_BODIES}
    docs.update(extra)
    return write_ai(repo, docs)


def fake_git(*, stdout: str = "", rc: int = 0, calls: list | None = None):
    """A fake git runner recording argv and returning canned patch output."""

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if calls is not None:
            calls.append(argv)
        return CommandResult(tuple(argv), rc, stdout, "")

    return runner


def docs_by_path(snapshot) -> dict[str, object]:
    return {doc.path: doc for doc in snapshot.docs}


# -- eager set ----------------------------------------------------------------


def test_reads_the_full_eager_set_in_single_file_form(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    assert snapshot.present is True
    assert snapshot.snippets_available is True
    assert [doc.path for doc in snapshot.docs] == [
        f"{AI_DIRNAME}/index.md",
        f"{AI_DIRNAME}/map.md",
        f"{AI_DIRNAME}/overview.md",
        f"{AI_DIRNAME}/architecture.md",
        f"{AI_DIRNAME}/design.md",
        f"{AI_DIRNAME}/conventions.md",
    ]
    assert all(doc.role == ROLE_EAGER for doc in snapshot.docs)
    assert all(doc.present and not doc.truncated for doc in snapshot.docs)
    assert "purpose" in docs_by_path(snapshot)[f"{AI_DIRNAME}/overview.md"].content
    assert snapshot.warnings == ()


def test_root_docs_and_topics_carry_their_names(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    topics = [doc.topic for doc in snapshot.docs]
    assert topics == [Path(name).stem for name in ROOT_DOCS] + list(EAGER_TOPICS)


def test_directory_form_entrypoint_resolves_by_file_shape(tmp_path):
    # `design` upgraded to directory form (memory protocol §4) and the router was
    # not re-pointed: file shape alone must still find it.
    repo = make_repo(tmp_path)
    docs = dict(SINGLE_FILE_BODIES)
    del docs["design.md"]
    write_ai(repo, {"index.md": router_md(DEFAULT_ROUTES), **docs})
    write_ai(repo, {"design/index.md": "# Design\nsub-index\n"})

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    design = docs_by_path(snapshot)[f"{AI_DIRNAME}/design/index.md"]
    assert design.present is True and design.topic == "design"
    assert "sub-index" in design.content
    assert f"{AI_DIRNAME}/design.md" not in docs_by_path(snapshot)


def test_router_picks_the_entrypoint_when_both_forms_exist(tmp_path):
    # A broken upgrade left both forms on disk. Routing is the authority, so the
    # doc the loader would actually read is the one the pack carries.
    repo = make_repo(tmp_path)
    routes = {**DEFAULT_ROUTES, "Design": "design/index.md"}
    write_ai(
        repo,
        {
            "index.md": router_md(routes),
            **SINGLE_FILE_BODIES,
            "design/index.md": "# Design\nrouted form\n",
        },
    )

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    assert "routed form" in docs_by_path(snapshot)[f"{AI_DIRNAME}/design/index.md"].content


def test_router_route_to_a_missing_file_loses_to_the_form_that_exists(tmp_path):
    # A loader left pointing at the pre-upgrade path must not make the pack read
    # nothing: the route is honored only when the file it names is really there.
    repo = make_repo(tmp_path)
    docs = dict(SINGLE_FILE_BODIES)
    del docs["design.md"]
    write_ai(
        repo,
        {
            "index.md": router_md(DEFAULT_ROUTES),  # routes design -> design.md
            **docs,
            "design/index.md": "# Design\nupgraded\n",
        },
    )

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    assert "upgraded" in docs_by_path(snapshot)[f"{AI_DIRNAME}/design/index.md"].content


@pytest.mark.parametrize(
    "route", ["../../etc/passwd", "/etc/passwd", "../outside.md", "notes/other.md"]
)
def test_router_route_outside_the_topic_forms_is_ignored(tmp_path, route):
    repo = make_repo(tmp_path)
    write_ai(
        repo,
        {
            "index.md": router_md({**DEFAULT_ROUTES, "Design": route}),
            **SINGLE_FILE_BODIES,
        },
    )

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    # Falls back to file shape: the single-file form that actually exists.
    assert "principles" in docs_by_path(snapshot)[f"{AI_DIRNAME}/design.md"].content


def test_router_accepts_an_ai_prefixed_route(tmp_path):
    repo = make_repo(tmp_path)
    write_ai(
        repo,
        {
            "index.md": router_md({**DEFAULT_ROUTES, "Design": ".ai/design/index.md"}),
            **SINGLE_FILE_BODIES,
            "design/index.md": "# Design\nprefixed route\n",
        },
    )

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    assert "prefixed route" in docs_by_path(snapshot)[f"{AI_DIRNAME}/design/index.md"].content


def test_read_router_tolerates_absent_and_unparseable_index(tmp_path):
    repo = make_repo(tmp_path)
    write_ai(repo, {"map.md": "# Map\n"})
    ai_dir = repo.path / AI_DIRNAME

    assert read_router(ai_dir) == {}

    (ai_dir / "index.md").write_text("no table here\n", encoding="utf-8")
    assert read_router(ai_dir) == {}


def test_resolve_entrypoint_defaults_to_single_file_when_neither_exists(tmp_path):
    repo = make_repo(tmp_path)
    write_ai(repo, {"map.md": "# Map\n"})

    assert resolve_entrypoint(repo.path / AI_DIRNAME, "design", {}) == "design.md"


def test_missing_eager_entrypoint_is_absent_with_a_warning(tmp_path):
    repo = make_repo(tmp_path)
    docs = dict(SINGLE_FILE_BODIES)
    del docs["architecture.md"]
    write_ai(repo, {"index.md": router_md(DEFAULT_ROUTES), **docs})

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    missing = docs_by_path(snapshot)[f"{AI_DIRNAME}/architecture.md"]
    assert missing.present is False and missing.detail == "not present"
    assert any("architecture.md: not present" in w for w in snapshot.warnings)
    # One missing doc does not take the rest of the snapshot with it.
    assert snapshot.snippets_available is True


# -- absent snapshot ----------------------------------------------------------


def test_absent_ai_dir_is_unavailable_with_a_warning(tmp_path):
    repo = make_repo(tmp_path)
    calls: list = []

    snapshot = read_ai_snapshot(repo, since=SINCE, runner=fake_git(calls=calls))

    assert snapshot.present is False
    assert snapshot.docs == ()
    assert snapshot.snippets_available is False
    assert snapshot.diffs_available is False
    assert "absent" in snapshot.detail
    assert len(snapshot.warnings) == 1
    assert calls == []  # nothing to diff against => git is never invoked


# -- prefetch slice -----------------------------------------------------------


def test_prefetch_docs_are_collected_after_the_eager_set(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(
        repo,
        **{"features/evaluation.md": "# Evaluation\nfeature\n", "glossary.md": "# Glossary\n"},
    )

    snapshot = read_ai_snapshot(
        repo,
        prefetch=[".ai/features/evaluation.md", ".ai/glossary.md"],
        runner=fake_git(),
    )

    tail = snapshot.docs[-2:]
    assert [doc.path for doc in tail] == [
        f"{AI_DIRNAME}/features/evaluation.md",
        f"{AI_DIRNAME}/glossary.md",
    ]
    assert all(doc.role == ROLE_PREFETCH and doc.topic is None for doc in tail)
    assert "feature" in tail[0].content


def test_prefetch_entry_naming_an_eager_doc_is_dropped_silently(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    snapshot = read_ai_snapshot(repo, prefetch=[".ai/overview.md"], runner=fake_git())

    assert [doc.path for doc in snapshot.docs].count(f"{AI_DIRNAME}/overview.md") == 1
    assert not any(doc.role == ROLE_PREFETCH for doc in snapshot.docs)
    assert snapshot.warnings == ()  # redundant, not wrong


def test_prefetch_entry_naming_a_missing_doc_is_absent_with_a_warning(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    snapshot = read_ai_snapshot(repo, prefetch=[".ai/features/gone.md"], runner=fake_git())

    missing = docs_by_path(snapshot)[f"{AI_DIRNAME}/features/gone.md"]
    assert missing.present is False and missing.role == ROLE_PREFETCH
    assert any("features/gone.md: not present" in w for w in snapshot.warnings)


@pytest.mark.parametrize(
    "entry",
    ["overview.md", "docs/readme.md", ".ai/overview", "/etc/passwd", ".ai/../secret.md", ""],
)
def test_malformed_prefetch_entry_warns_and_is_skipped(tmp_path, entry):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    snapshot = read_ai_snapshot(repo, prefetch=[entry], runner=fake_git())

    assert not any(doc.role == ROLE_PREFETCH for doc in snapshot.docs)
    assert any("is not a .ai/*.md path" in w for w in snapshot.warnings)


def test_prefetch_cap_drops_the_tail_with_a_warning(tmp_path):
    repo = make_repo(tmp_path)
    extra = {f"lazy/doc{i}.md": f"# Doc {i}\n" for i in range(PREFETCH_MAX_DOCS + 3)}
    write_single_file_snapshot(repo, **extra)

    snapshot = read_ai_snapshot(
        repo, prefetch=[f".ai/{rel}" for rel in extra], runner=fake_git()
    )

    assert sum(1 for doc in snapshot.docs if doc.role == ROLE_PREFETCH) == PREFETCH_MAX_DOCS
    assert any(f"3 prefetch doc(s) dropped past the {PREFETCH_MAX_DOCS}" in w for w in snapshot.warnings)


# -- caps ---------------------------------------------------------------------


def test_oversize_doc_is_truncated_to_the_cap_with_a_flag(tmp_path):
    repo = make_repo(tmp_path)
    body = "x" * 5000
    write_single_file_snapshot(repo, **{"overview.md": body})

    snapshot = read_ai_snapshot(repo, doc_max_bytes=1000, runner=fake_git())

    overview = docs_by_path(snapshot)[f"{AI_DIRNAME}/overview.md"]
    assert overview.present is True
    assert overview.truncated is True
    assert overview.size == 5000  # true file size, not the kept slice
    assert len(overview.content) == 1000
    assert any("overview.md: truncated to 1000 of 5000 bytes" in w for w in snapshot.warnings)


def test_total_budget_exhaustion_omits_later_docs_with_a_warning(tmp_path):
    # Budget fits the router and map only; the eager content topics fall off the
    # end, which is why docs are read in priority order.
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)
    ai_dir = repo.path / AI_DIRNAME
    budget = (ai_dir / "index.md").stat().st_size + (ai_dir / "map.md").stat().st_size

    snapshot = read_ai_snapshot(repo, total_max_bytes=budget, runner=fake_git())

    by_path = docs_by_path(snapshot)
    assert by_path[f"{AI_DIRNAME}/index.md"].present is True
    assert by_path[f"{AI_DIRNAME}/map.md"].present is True
    dropped = by_path[f"{AI_DIRNAME}/overview.md"]
    assert dropped.present is False
    assert dropped.truncated is True
    assert dropped.size > 0  # the file is there; only its content was skipped
    assert dropped.detail == "content budget exhausted"
    assert any("overview.md: omitted" in w for w in snapshot.warnings)
    # Some content survived, so the snapshot is degraded, not unavailable.
    assert snapshot.snippets_available is True


def test_a_doc_read_never_escapes_the_snapshot_dir(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)
    secret = repo.path / "secret.md"
    secret.write_text("# Not part of .ai/\n", encoding="utf-8")
    escaping = repo.path / AI_DIRNAME / "overview.md"
    escaping.unlink()
    escaping.symlink_to(secret)

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    overview = docs_by_path(snapshot)[f"{AI_DIRNAME}/overview.md"]
    assert overview.present is False
    assert overview.content is None
    assert "resolves outside" in overview.detail
    assert any("resolves outside" in w for w in snapshot.warnings)


def test_unreadable_doc_degrades_to_a_warning(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)
    real_read_bytes = Path.read_bytes

    def failing(self):
        if self.name == "overview.md":
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", failing)

    snapshot = read_ai_snapshot(repo, runner=fake_git())

    overview = docs_by_path(snapshot)[f"{AI_DIRNAME}/overview.md"]
    assert overview.present is False and "unreadable" in overview.detail
    assert any("overview.md: unreadable" in w for w in snapshot.warnings)
    assert snapshot.present is True  # one bad doc is not a bad snapshot


# -- windowed diff ------------------------------------------------------------


def test_diff_is_path_scoped_window_bounded_and_commit_capped(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)
    calls: list = []

    snapshot = read_ai_snapshot(
        repo,
        since=SINCE,
        until=UNTIL,
        runner=fake_git(stdout="commit abc123 2026-08-09 absorb\n+ a line\n", calls=calls),
    )

    assert snapshot.diffs_available is True
    assert snapshot.diff.since == SINCE and snapshot.diff.until == UNTIL
    assert "+ a line" in snapshot.diff.text
    assert snapshot.diff.truncated is False
    argv = calls[0]
    assert argv[:2] == ["git", "log"]
    assert f"-{DIFF_COMMIT_LIMIT}" in argv
    assert "--patch" in argv
    assert f"--since={SINCE}" in argv and f"--until={UNTIL}" in argv
    assert argv[-2:] == ["--", AI_DIRNAME]  # scoped to .ai/, never the whole tree


def test_diff_available_but_empty_is_a_fact_not_a_gap(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    snapshot = read_ai_snapshot(repo, since=SINCE, runner=fake_git(stdout=""))

    # git answered and `.ai/` was untouched in the window: available, empty, and
    # no warning — the evaluator may conclude "no absorption", not "unknown".
    assert snapshot.diffs_available is True
    assert snapshot.diff.text == ""
    assert snapshot.warnings == ()


def test_diff_is_byte_capped_with_a_truncation_flag(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    snapshot = read_ai_snapshot(
        repo, since=SINCE, diff_max_bytes=100, runner=fake_git(stdout="d" * 4000)
    )

    assert snapshot.diff.truncated is True
    assert snapshot.diff.size == 4000
    assert len(snapshot.diff.text) == 100
    assert any("windowed diff truncated to 100 of 4000 bytes" in w for w in snapshot.warnings)


def test_diff_unavailable_on_git_failure_warns(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    snapshot = read_ai_snapshot(repo, since=SINCE, runner=fake_git(rc=128))

    assert snapshot.diffs_available is False
    assert snapshot.diff.detail == "git history unavailable"
    assert any("windowed diff unavailable" in w for w in snapshot.warnings)
    # The doc snippets are independent of the diff.
    assert snapshot.snippets_available is True


def test_diff_unavailable_when_git_is_missing(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    def missing(args, **kwargs):
        raise CommandNotFoundError("git")

    snapshot = read_ai_snapshot(repo, since=SINCE, runner=missing)

    assert snapshot.diffs_available is False


def test_no_window_skips_git_and_reuses_the_caller_reason(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)
    calls: list = []

    snapshot = read_ai_snapshot(
        repo,
        since=None,
        window_detail="no run start timestamps to form a git window",
        runner=fake_git(calls=calls),
    )

    assert calls == []
    assert snapshot.diffs_available is False
    assert snapshot.diff.detail == "no run start timestamps to form a git window"
    # An absent window is not a `.ai/` read failure, so it is not a warning here.
    assert snapshot.warnings == ()


# -- serialization ------------------------------------------------------------


def test_to_dict_is_self_describing(tmp_path):
    repo = make_repo(tmp_path)
    write_single_file_snapshot(repo)

    data = read_ai_snapshot(
        repo, since=SINCE, until=UNTIL, runner=fake_git(stdout="patch\n")
    ).to_dict()

    assert data["present"] is True
    assert data["snippets_available"] is True
    assert data["diffs_available"] is True
    assert len(data["docs"]) == len(ROOT_DOCS) + len(EAGER_TOPICS)
    assert data["docs"][0]["path"] == f"{AI_DIRNAME}/index.md"
    assert data["diff"]["since"] == SINCE and data["diff"]["until"] == UNTIL
    assert data["warnings"] == []
