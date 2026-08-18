"""Tests for local task inventory parsing.

Builds fake managed repos in ``tmp_path`` with real ``.ai-tasks`` files; never
touches the real registry, ``mandrel``, readiness checks, or orchestrator code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orch_hub.models import ManagedRepo, SessionEst, tasks_payload
from orch_hub.task_store import (
    STATUS_MISSING,
    STATUS_UNKNOWN,
    list_repo_tasks,
    list_tasks,
    read_archived_task_content,
    read_session_est,
    read_task_content,
)


def make_repo(root: Path, name: str = "repo") -> ManagedRepo:
    path = root / name
    return ManagedRepo(
        name=name, path=path, manifest=path / ".ai-deploy-manifest.json"
    )


def write_index(tasks_dir: Path, rows: list[tuple[str, str, str]]) -> None:
    """Write a standard three-column ``.ai-tasks/index.md`` table."""
    lines = ["# Active tasks", "", "| Task | Status | Summary |", "|---|---|---|"]
    for task_id, status, summary in rows:
        lines.append(f"| {task_id} | {status} | {summary} |")
    (tasks_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_task(
    tasks_dir: Path, task_id: str, status: str, *, blockers: str | None = None
) -> None:
    lines = ["---", f"id: {task_id}", f"status: {status}", "session-est: 0/2"]
    if blockers is not None:
        lines.append(f"blockers: {blockers}")
    lines += ["prefetch: [.ai/modules.md]", "---", "", f"# {task_id}", ""]
    (tasks_dir / f"{task_id}.md").write_text("\n".join(lines), encoding="utf-8")


def make_repo_with_index(
    root: Path, rows: list[tuple[str, str, str]], name: str = "repo"
) -> tuple[ManagedRepo, Path]:
    repo = make_repo(root, name)
    tasks_dir = repo.path / ".ai-tasks"
    tasks_dir.mkdir(parents=True)
    write_index(tasks_dir, rows)
    return repo, tasks_dir


def test_reads_ids_from_index_and_status_from_frontmatter(tmp_path):
    repo, tasks_dir = make_repo_with_index(
        tmp_path,
        [
            ("2026-07-05-alpha", "pending", "Alpha work"),
            ("2026-07-05-beta", "blocked", "Beta work"),
        ],
    )
    write_task(tasks_dir, "2026-07-05-alpha", "pending")
    # Frontmatter is the source of truth: beta's file says in_progress even
    # though the index Status column still reads "blocked".
    write_task(tasks_dir, "2026-07-05-beta", "in_progress")

    inv = list_repo_tasks(repo)

    assert inv.index_present is True
    assert [t.task_id for t in inv.tasks] == ["2026-07-05-alpha", "2026-07-05-beta"]
    by_id = {t.task_id: t for t in inv.tasks}
    assert by_id["2026-07-05-alpha"].status == "pending"
    assert by_id["2026-07-05-beta"].status == "in_progress"
    assert by_id["2026-07-05-alpha"].summary == "Alpha work"


@pytest.mark.parametrize(
    "status", ["pending", "in_progress", "final_review", "completed", "blocked"]
)
def test_frontmatter_status_values_parse(tmp_path, status):
    repo, tasks_dir = make_repo_with_index(tmp_path, [("2026-07-05-x", status, "X")])
    write_task(tasks_dir, "2026-07-05-x", status)
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].status == status


def test_blocked_task_blockers_parsed(tmp_path):
    repo, tasks_dir = make_repo_with_index(
        tmp_path, [("2026-07-05-b", "blocked", "Blocked one")]
    )
    write_task(
        tasks_dir,
        "2026-07-05-b",
        "blocked",
        blockers="[external:awaiting spec, 2026-07-05-other]",
    )
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].blockers == ("external:awaiting spec", "2026-07-05-other")


def test_task_without_blockers_has_empty_tuple(tmp_path):
    repo, tasks_dir = make_repo_with_index(tmp_path, [("2026-07-05-a", "pending", "A")])
    write_task(tasks_dir, "2026-07-05-a", "pending")
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].blockers == ()


def test_empty_blockers_list_parses_empty(tmp_path):
    repo, tasks_dir = make_repo_with_index(tmp_path, [("2026-07-05-a", "pending", "A")])
    write_task(tasks_dir, "2026-07-05-a", "pending", blockers="[]")
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].blockers == ()


def test_missing_index_is_surfaced(tmp_path):
    repo = make_repo(tmp_path)
    (repo.path / ".ai-tasks").mkdir(parents=True)  # dir exists, no index.md
    inv = list_repo_tasks(repo)
    assert inv.index_present is False
    assert inv.tasks == ()


def test_missing_tasks_dir_is_surfaced(tmp_path):
    repo = make_repo(tmp_path)  # nothing created for the repo at all
    inv = list_repo_tasks(repo)
    assert inv.index_present is False
    assert inv.tasks == ()


def test_empty_index_has_no_tasks(tmp_path):
    repo, _ = make_repo_with_index(tmp_path, [])
    inv = list_repo_tasks(repo)
    assert inv.index_present is True
    assert inv.tasks == ()


def test_task_id_without_file_is_missing(tmp_path):
    repo, _ = make_repo_with_index(
        tmp_path, [("2026-07-05-ghost", "pending", "Ghost")]
    )  # index lists it, but no <id>.md is written
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].status == STATUS_MISSING
    assert inv.tasks[0].summary == "Ghost"


def test_task_file_without_status_is_unknown(tmp_path):
    repo, tasks_dir = make_repo_with_index(tmp_path, [("2026-07-05-x", "pending", "X")])
    (tasks_dir / "2026-07-05-x.md").write_text(
        "# X\n\nno frontmatter here\n", encoding="utf-8"
    )
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].status == STATUS_UNKNOWN


def test_index_row_without_summary_cell(tmp_path):
    repo = make_repo(tmp_path)
    tasks_dir = repo.path / ".ai-tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "index.md").write_text(
        "| Task |\n|---|\n| 2026-07-05-x |\n", encoding="utf-8"
    )
    write_task(tasks_dir, "2026-07-05-x", "pending")
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].task_id == "2026-07-05-x"
    assert inv.tasks[0].summary is None
    assert inv.tasks[0].status == "pending"


def write_five_column_index(
    tasks_dir: Path, rows: list[tuple[str, str, str, str, str]]
) -> None:
    """Write a hkchain-style five-column ``.ai-tasks/index.md``.

    Columns: ``id | title | status | session-est | blockers``. The index has YAML
    frontmatter and prose above the table (like the real hkchain file), and the
    status/blockers columns are deliberately allowed to disagree with task
    frontmatter so tests can prove the parser ignores them.
    """
    lines = [
        "---",
        "last-updated: 2026-06-13",
        "---",
        "",
        "# Active tasks",
        "",
        "Prose preamble with a `2026-06-11-x` code ref the parser must ignore.",
        "",
        "| id | title | status | session-est | blockers |",
        "|---|---|---|---|---|",
    ]
    for task_id, title, status, est, blockers in rows:
        lines.append(f"| {task_id} | {title} | {status} | {est} | {blockers} |")
    (tasks_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_repo_with_five_column_index(
    root: Path, rows: list[tuple[str, str, str, str, str]], name: str = "repo"
) -> tuple[ManagedRepo, Path]:
    repo = make_repo(root, name)
    tasks_dir = repo.path / ".ai-tasks"
    tasks_dir.mkdir(parents=True)
    write_five_column_index(tasks_dir, rows)
    return repo, tasks_dir


def test_five_column_index_produces_its_rows(tmp_path):
    # hkchain renders `| id | title | status | session-est | blockers |`. All
    # five rows must parse, the `id` header must not become a task, and each
    # summary must come from the `title` column (not `status`).
    hk_rows = [
        ("2026-06-11-queryreport-in-app-render", "In-app QueryReport render", "pending", "0/1", "[]"),
        ("2026-06-11-partner-live-scenario", "GoGreen partner live scenario", "pending", "0/2", "[]"),
        ("2026-06-11-cli-coin-timestamp-fix", "Hand-rolled cobra cmds fix", "pending", "0/1", "[]"),
        ("2026-06-11-kyc-distribution-drilldown", "KYC distribution drill-down", "pending", "0/1", "[]"),
        ("2026-06-11-agentic-tx-annotation", "Annotate agent txs on dashboard", "pending", "0/1", "[]"),
    ]
    repo, tasks_dir = make_repo_with_five_column_index(tmp_path, hk_rows)
    for task_id, *_ in hk_rows:
        write_task(tasks_dir, task_id, "pending")

    inv = list_repo_tasks(repo)

    assert [t.task_id for t in inv.tasks] == [row[0] for row in hk_rows]
    # The `id` header row was not mistaken for a task.
    assert all(t.task_id not in ("id", "title") for t in inv.tasks)
    by_id = {t.task_id: t for t in inv.tasks}
    assert by_id["2026-06-11-queryreport-in-app-render"].summary == "In-app QueryReport render"
    assert by_id["2026-06-11-agentic-tx-annotation"].summary == "Annotate agent txs on dashboard"


def test_five_column_index_status_and_blockers_from_frontmatter(tmp_path):
    # The index columns disagree with the task files; frontmatter must win.
    repo, tasks_dir = make_repo_with_five_column_index(
        tmp_path,
        [("2026-06-11-x", "Title X", "pending", "0/3", "[]")],
    )
    write_task(
        tasks_dir, "2026-06-11-x", "blocked", blockers="[external:awaiting spec]"
    )
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].task_id == "2026-06-11-x"
    assert inv.tasks[0].summary == "Title X"
    # Not the index's "pending"/"[]"; the frontmatter's blocked/[external:...].
    assert inv.tasks[0].status == "blocked"
    assert inv.tasks[0].blockers == ("external:awaiting spec",)


def test_three_column_notes_header_still_supplies_summary(tmp_path):
    # quantx renders `| Task | Status | Notes |` — the third column is the
    # summary regardless of its label, via the legacy positional default.
    repo = make_repo(tmp_path)
    tasks_dir = repo.path / ".ai-tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "index.md").write_text(
        "# Active tasks\n\n"
        "| Task | Status | Notes |\n"
        "|---|---|---|\n"
        "| [2026-04-13-golden-a](2026-04-13-golden-a.md) | pending (0/3) | Backtest fixtures. |\n",
        encoding="utf-8",
    )
    write_task(tasks_dir, "2026-04-13-golden-a", "in_progress")
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].task_id == "2026-04-13-golden-a"
    assert inv.tasks[0].summary == "Backtest fixtures."
    assert inv.tasks[0].status == "in_progress"  # frontmatter, not the index column


def test_markdown_link_index_row_resolves_task_file(tmp_path):
    # quantx-style index: the Task cell is a Markdown link, not a bare id. It
    # must resolve to the real <id>.md and report the frontmatter status.
    task_id = "2026-04-13-golden-family-a"
    repo, tasks_dir = make_repo_with_index(
        tmp_path,
        [(f"[{task_id}]({task_id}.md)", "pending", "Golden family")],
    )
    write_task(tasks_dir, task_id, "in_progress")
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].task_id == task_id
    assert inv.tasks[0].status == "in_progress"
    assert inv.tasks[0].summary == "Golden family"


def test_markdown_link_index_row_missing_file_is_missing(tmp_path):
    # A link row whose resolved <id>.md is genuinely absent still reports the
    # missing sentinel, exactly as a bare-id row would.
    task_id = "2026-04-13-golden-family-a"
    repo, _ = make_repo_with_index(
        tmp_path, [(f"[{task_id}]({task_id}.md)", "pending", "Golden")]
    )
    inv = list_repo_tasks(repo)
    assert inv.tasks[0].task_id == task_id
    assert inv.tasks[0].status == STATUS_MISSING
    assert inv.tasks[0].summary == "Golden"


def test_index_mixes_bare_and_markdown_link_rows(tmp_path):
    # Bare-id rows keep behaving exactly as today alongside link rows.
    repo, tasks_dir = make_repo_with_index(
        tmp_path,
        [
            ("2026-07-05-bare", "pending", "Bare"),
            ("[2026-04-13-linked](2026-04-13-linked.md)", "pending", "Linked"),
        ],
    )
    write_task(tasks_dir, "2026-07-05-bare", "pending")
    write_task(tasks_dir, "2026-04-13-linked", "final_review")
    inv = list_repo_tasks(repo)
    assert [t.task_id for t in inv.tasks] == ["2026-07-05-bare", "2026-04-13-linked"]
    by_id = {t.task_id: t for t in inv.tasks}
    assert by_id["2026-07-05-bare"].status == "pending"
    assert by_id["2026-04-13-linked"].status == "final_review"


def test_markdown_link_target_basename_prevents_traversal(tmp_path):
    # A link target with a traversal prefix must not read a file outside
    # .ai-tasks. Only the target's basename is used, so [x](../secret.md)
    # resolves to .ai-tasks/secret.md (absent here), never the repo-root file.
    repo, tasks_dir = make_repo_with_index(
        tmp_path, [("[golden](../secret.md)", "pending", "Golden")]
    )
    (repo.path / "secret.md").write_text(
        "---\nid: secret\nstatus: leaked\n---\n", encoding="utf-8"
    )
    inv = list_repo_tasks(repo)
    # Normalized to the bare basename, and the outside file is never read.
    assert inv.tasks[0].task_id == "secret"
    assert inv.tasks[0].status == STATUS_MISSING


def test_list_tasks_covers_all_repos(tmp_path):
    repo_a, dir_a = make_repo_with_index(
        tmp_path, [("2026-07-05-a", "pending", "A")], name="a"
    )
    write_task(dir_a, "2026-07-05-a", "pending")
    repo_b = make_repo(tmp_path, "b")  # discovered repo with no .ai-tasks dir

    inventories = list_tasks([repo_a, repo_b])

    assert [inv.index_present for inv in inventories] == [True, False]
    assert inventories[0].tasks[0].task_id == "2026-07-05-a"
    assert inventories[1].tasks == ()


def test_read_task_content_returns_raw_body(tmp_path):
    repo, tasks_dir = make_repo_with_index(
        tmp_path, [("2026-07-05-a", "pending", "A")]
    )
    write_task(tasks_dir, "2026-07-05-a", "pending")
    content = read_task_content(repo, "2026-07-05-a")
    # The whole file, frontmatter included — the detail view renders it verbatim.
    assert content is not None
    assert "status: pending" in content
    assert "# 2026-07-05-a" in content


def test_read_task_content_absent_file_is_none(tmp_path):
    # Listed in the index but no <id>.md on disk (the STATUS_MISSING case).
    repo, _ = make_repo_with_index(tmp_path, [("2026-07-05-ghost", "pending", "G")])
    assert read_task_content(repo, "2026-07-05-ghost") is None


def test_read_task_content_no_tasks_dir_is_none(tmp_path):
    repo = make_repo(tmp_path)  # no .ai-tasks dir at all
    assert read_task_content(repo, "2026-07-05-a") is None


@pytest.mark.parametrize(
    "bad_id",
    ["../secret", "a/b", "..", ".", "", "nested\\win", "null\x00byte"],
)
def test_read_task_content_rejects_unsafe_ids(tmp_path, bad_id):
    repo, tasks_dir = make_repo_with_index(tmp_path, [("2026-07-05-a", "pending", "A")])
    write_task(tasks_dir, "2026-07-05-a", "pending")
    # A file outside .ai-tasks must never be reachable via a crafted task id.
    (repo.path / "secret.md").write_text("top secret", encoding="utf-8")
    assert read_task_content(repo, bad_id) is None


def test_read_task_content_rejects_symlink_escaping_tasks_dir(tmp_path):
    # A listed task file may itself be a symlink; is_file()/read_text() follow
    # symlinks, so a bare-filename id alone does not guarantee containment. A
    # symlink whose target resolves outside .ai-tasks must not leak that file.
    repo, tasks_dir = make_repo_with_index(
        tmp_path, [("2026-07-05-linked", "pending", "Linked")]
    )
    secret = repo.path / "secret.md"
    secret.write_text("top secret", encoding="utf-8")
    (tasks_dir / "2026-07-05-linked.md").symlink_to(secret)
    assert read_task_content(repo, "2026-07-05-linked") is None


def write_archived_task(
    tasks_dir: Path, task_id: str, *, body: str = "archived detail"
) -> Path:
    """Write a completed task into ``.ai-tasks/archive/<id>.md`` (close-out shape)."""
    archive_dir = tasks_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{task_id}.md"
    path.write_text(
        f"---\nid: {task_id}\nstatus: completed\n---\n\n# {task_id}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_read_archived_task_content_returns_raw_body(tmp_path):
    # The run-scoped task view reads a completed task straight from archive/,
    # even though it is no longer listed in the active index.
    repo, tasks_dir = make_repo_with_index(tmp_path, [])
    write_archived_task(tasks_dir, "2026-07-05-done", body="archived detail")
    content = read_archived_task_content(repo, "2026-07-05-done")
    assert content is not None
    assert "status: completed" in content
    assert "archived detail" in content


def test_read_archived_task_content_absent_file_is_none(tmp_path):
    # Archive dir exists but this id was never archived.
    repo, tasks_dir = make_repo_with_index(tmp_path, [])
    (tasks_dir / "archive").mkdir()
    assert read_archived_task_content(repo, "2026-07-05-ghost") is None


def test_read_archived_task_content_no_archive_dir_is_none(tmp_path):
    # A repo whose close-out never ran has no archive/ dir at all.
    repo, _ = make_repo_with_index(tmp_path, [])
    assert read_archived_task_content(repo, "2026-07-05-a") is None


@pytest.mark.parametrize(
    "bad_id",
    ["../secret", "a/b", "..", ".", "", "nested\\win", "null\x00byte"],
)
def test_read_archived_task_content_rejects_unsafe_ids(tmp_path, bad_id):
    repo, tasks_dir = make_repo_with_index(tmp_path, [])
    write_archived_task(tasks_dir, "2026-07-05-done")
    # Files outside the archive dir must never be reachable via a crafted id
    # (the same containment guarantee as the active reader).
    (repo.path / "secret.md").write_text("top secret", encoding="utf-8")
    (tasks_dir / "secret.md").write_text("active secret", encoding="utf-8")
    assert read_archived_task_content(repo, bad_id) is None


def test_read_archived_task_content_rejects_symlink_escaping_archive_dir(tmp_path):
    # An archived task file that is itself a symlink out of archive/ must not
    # leak its target — is_file()/read_text() follow symlinks, so containment is
    # enforced on the fully resolved path, exactly as the active reader does.
    repo, tasks_dir = make_repo_with_index(tmp_path, [])
    archive_dir = tasks_dir / "archive"
    archive_dir.mkdir()
    secret = repo.path / "secret.md"
    secret.write_text("top secret", encoding="utf-8")
    (archive_dir / "2026-07-05-linked.md").symlink_to(secret)
    assert read_archived_task_content(repo, "2026-07-05-linked") is None


# -- session-est parse (est-vs-actual rollup source) ---------------------------


def write_task_est(tasks_dir: Path, task_id: str, est_line: str | None) -> None:
    """Write a minimal task file whose ``session-est`` line is ``est_line``.

    ``est_line`` is the whole raw frontmatter line (e.g. ``"session-est: 2/3"``)
    so tests can exercise malformed shapes verbatim, or ``None`` to omit the
    field entirely.
    """
    lines = ["---", f"id: {task_id}", "status: in_progress"]
    if est_line is not None:
        lines.append(est_line)
    lines += ["---", "", f"# {task_id}", ""]
    (tasks_dir / f"{task_id}.md").write_text("\n".join(lines), encoding="utf-8")


def test_read_session_est_parses_active_frontmatter(tmp_path):
    repo, tasks_dir = make_repo_with_index(tmp_path, [])
    write_task_est(tasks_dir, "2026-07-05-a", "session-est: 2/3")
    assert read_session_est(repo, "2026-07-05-a") == SessionEst(current=2, total=3)


def test_read_session_est_falls_back_to_archive(tmp_path):
    # A completed task lives only in archive/ after close-out; the rollup still
    # reads its est through the run record's id (active-then-archive fallback).
    repo, tasks_dir = make_repo_with_index(tmp_path, [])
    archive = tasks_dir / "archive"
    archive.mkdir()
    write_task_est(archive, "2026-07-05-done", "session-est: 3/3")
    assert read_session_est(repo, "2026-07-05-done") == SessionEst(3, 3)


def test_read_session_est_active_is_authoritative_over_archive(tmp_path):
    # Only genuine absence of the active file falls through to archive; a present
    # active file with a malformed est stays unknown rather than reading a stale
    # archived copy.
    repo, tasks_dir = make_repo_with_index(tmp_path, [])
    write_task_est(tasks_dir, "2026-07-05-x", "session-est: oops")  # malformed active
    archive = tasks_dir / "archive"
    archive.mkdir()
    write_task_est(archive, "2026-07-05-x", "session-est: 1/2")  # valid archive
    assert read_session_est(repo, "2026-07-05-x") is None


@pytest.mark.parametrize(
    "est_line",
    [
        "session-est: 2",  # no total
        "session-est: /3",  # no current
        "session-est: abc/3",  # non-numeric current
        "session-est: 2/x",  # non-numeric total
        "session-est: 2/3/4",  # too many parts
        "session-est: ²/3",  # superscript "²": isdigit() true but not int-able
        "session-est:",  # empty value
        None,  # field omitted entirely
    ],
)
def test_read_session_est_malformed_or_missing_is_unknown(tmp_path, est_line):
    repo, tasks_dir = make_repo_with_index(tmp_path, [])
    write_task_est(tasks_dir, "2026-07-05-m", est_line)
    assert read_session_est(repo, "2026-07-05-m") is None


def test_read_session_est_absent_task_file_is_unknown(tmp_path):
    # No active file and no archive/ copy: unknown, never an error.
    repo, tasks_dir = make_repo_with_index(tmp_path, [])
    assert read_session_est(repo, "2026-07-05-ghost") is None


def test_session_est_to_dict_round_trip():
    assert SessionEst(current=1, total=4).to_dict() == {"current": 1, "total": 4}


def test_to_dict_and_payload_shape(tmp_path):
    repo, tasks_dir = make_repo_with_index(
        tmp_path, [("2026-07-05-a", "blocked", "A work")]
    )
    write_task(tasks_dir, "2026-07-05-a", "blocked", blockers="[external:spec]")
    inv = list_repo_tasks(repo)

    as_dict = inv.to_dict()
    assert as_dict["name"] == "repo"
    assert as_dict["path"] == str(repo.path)
    assert as_dict["index_present"] is True
    assert as_dict["tasks"] == [
        {
            "task_id": "2026-07-05-a",
            "status": "blocked",
            "summary": "A work",
            "blockers": ["external:spec"],
        }
    ]
    assert tasks_payload([inv]) == {"repos": [as_dict]}
