"""Tests for local task inventory parsing.

Builds fake managed repos in ``tmp_path`` with real ``.ai-tasks`` files; never
touches the real registry, ``aii-2``, readiness checks, or orchestrator code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orch_hub.models import ManagedRepo, tasks_payload
from orch_hub.task_store import (
    STATUS_MISSING,
    STATUS_UNKNOWN,
    list_repo_tasks,
    list_tasks,
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
