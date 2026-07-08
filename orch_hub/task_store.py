"""Local task inventory for managed repos.

Reads each managed repo's ``.ai-tasks/index.md`` for the set of active task ids
and each ``.ai-tasks/<id>.md`` frontmatter for that task's status and blockers.

Parsing is intentionally local and minimal: it never imports protocol or
orchestrator code. The index supplies the active-id set and a human summary;
the task file's frontmatter is the source of truth for status (so an index
``Status`` column that lags the file does not mislead callers).

Task-file status (``pending``/``in_progress``/``final_review``/``blocked``/...)
stays distinct from the future run overlay (``active``/``waiting``), which the
run manager layers on separately once run data exists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from .models import ManagedRepo, RepoTaskInventory, TaskSummary

STATUS_MISSING = "missing"
"""Sentinel status: a task id is listed in the index but has no ``<id>.md``."""

STATUS_UNKNOWN = "unknown"
"""Sentinel status: a task file exists but carries no parseable status."""

_SEPARATOR_CELL = re.compile(r"^:?-+:?$")


def list_repo_tasks(repo: ManagedRepo) -> RepoTaskInventory:
    """Return the active-task inventory for one managed repo.

    A missing ``.ai-tasks/index.md`` yields ``index_present=False`` with no
    tasks, so callers can surface it distinctly from a present-but-empty index.
    """
    tasks_dir = repo.path / ".ai-tasks"
    index_path = tasks_dir / "index.md"
    if not index_path.is_file():
        return RepoTaskInventory(repo=repo, index_present=False, tasks=())
    rows = _parse_index(index_path.read_text(encoding="utf-8"))
    tasks = tuple(
        _load_task(tasks_dir, task_id, summary) for task_id, summary in rows
    )
    return RepoTaskInventory(repo=repo, index_present=True, tasks=tasks)


def list_tasks(repos: Iterable[ManagedRepo]) -> list[RepoTaskInventory]:
    """Return per-repo task inventories for every given repo (all-repos view).

    Filter the input ``repos`` (or the returned tasks by status) for narrower
    per-repo or per-state views.
    """
    return [list_repo_tasks(repo) for repo in repos]


def read_task_content(repo: ManagedRepo, task_id: str) -> str | None:
    """Return the raw markdown of ``.ai-tasks/<task_id>.md``, or ``None``.

    ``None`` means the file is absent (e.g. a ``missing`` sentinel task listed
    in the index but with no ``<id>.md``), ``task_id`` is not a bare filename,
    or the resolved file escapes the repo's ``.ai-tasks`` dir. ``task_id`` is
    validated as a single path segment — no separators, no null, not a dot ref
    — and the resolved target must stay under the resolved ``.ai-tasks`` dir, so
    neither a crafted id nor a symlinked task file can read outside it; clients
    never pass a filesystem path. This adds only the file *body*;
    :func:`list_repo_tasks` remains the source of the frontmatter-authoritative
    status/summary/blockers.
    """
    if (
        not task_id
        or "/" in task_id
        or "\\" in task_id
        or "\x00" in task_id
        or task_id in (".", "..")
    ):
        return None
    tasks_dir = repo.path / ".ai-tasks"
    task_path = tasks_dir / f"{task_id}.md"
    if not task_path.is_file():
        return None
    # is_file()/read_text() follow symlinks, so a listed task file that is a
    # symlink pointing outside .ai-tasks would otherwise escape containment.
    # Require the fully resolved target to stay under the resolved tasks dir.
    resolved = task_path.resolve()
    if not resolved.is_relative_to(tasks_dir.resolve()):
        return None
    return resolved.read_text(encoding="utf-8")


def _load_task(tasks_dir: Path, task_id: str, summary: str | None) -> TaskSummary:
    """Build a :class:`TaskSummary` from a task file, tolerating missing files."""
    task_path = tasks_dir / f"{task_id}.md"
    if not task_path.is_file():
        return TaskSummary(task_id=task_id, status=STATUS_MISSING, summary=summary)
    frontmatter = _parse_frontmatter(task_path.read_text(encoding="utf-8"))
    status = frontmatter.get("status", "").strip() or STATUS_UNKNOWN
    blockers = _parse_inline_list(frontmatter.get("blockers"))
    return TaskSummary(
        task_id=task_id, status=status, summary=summary, blockers=blockers
    )


def _parse_index(text: str) -> list[tuple[str, str | None]]:
    """Extract ``(task_id, summary)`` rows from the ``.ai-tasks/index.md`` table.

    Skips the title, the header row, and the ``|---|`` separator; tolerates rows
    that omit the summary cell. The index ``Status`` column is intentionally not
    read here — the task file's frontmatter is the source of truth for status.
    """
    rows: list[tuple[str, str | None]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        if all(_SEPARATOR_CELL.match(cell) for cell in cells):
            continue
        if cells[0].lower() == "task":
            continue
        summary = cells[2] if len(cells) > 2 and cells[2] else None
        rows.append((cells[0], summary))
    return rows


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the leading ``---`` frontmatter into raw ``key: value`` strings.

    Returns an empty mapping when the file does not open with a ``---`` fence.
    Values are taken verbatim (minus surrounding whitespace); list-valued fields
    are interpreted by :func:`_parse_inline_list`.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def _parse_inline_list(raw: str | None) -> tuple[str, ...]:
    """Parse an inline ``[a, b, c]`` frontmatter list into a tuple of strings.

    An absent field or an empty ``[]`` yields an empty tuple.
    """
    if raw is None:
        return ()
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return tuple(item.strip() for item in inner.split(",") if item.strip())
