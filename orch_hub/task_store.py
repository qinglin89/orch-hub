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
from pathlib import Path, PurePosixPath

from .models import ManagedRepo, RepoTaskInventory, SessionEst, TaskFileState, TaskSummary

STATUS_MISSING = "missing"
"""Sentinel status: a task id is listed in the index but has no ``<id>.md``."""

STATUS_UNKNOWN = "unknown"
"""Sentinel status: a task file exists but carries no parseable status."""

_SEPARATOR_CELL = re.compile(r"^:?-+:?$")

_MARKDOWN_LINK = re.compile(r"^\[[^\]]*\]\((?P<target>[^)]*)\)$")
"""A full-cell Markdown link ``[text](target)`` with no nested ``]``/``)``."""

_ID_HEADERS = ("task", "id")
"""First-cell labels that mark a header row and its id column: ``Task`` (three-
and single-column) or ``id`` (hkchain five-column). Task ids are date-prefixed,
so a data row's first cell never collides with these."""

_SUMMARY_HEADERS = ("summary", "title")
"""Header labels that explicitly name the summary column and override the legacy
positional default: ``Summary`` (three-column, already at the default index 2)
or ``title`` (five-column, at index 1). A three-column header with any other
third label (e.g. quantx's ``Notes``) keeps the positional default."""

_DEFAULT_SUMMARY_COL = 2
"""Legacy positional summary column: the third cell of a three-column index
(``Summary``, quantx's ``Notes``, or any other third column). Single-column rows
have no third cell, so they carry no summary."""


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
    return _read_contained_task_file(repo.path / ".ai-tasks", task_id)


def read_archived_task_content(repo: ManagedRepo, task_id: str) -> str | None:
    """Return the raw markdown of ``.ai-tasks/archive/<task_id>.md``, or ``None``.

    Backs the run-scoped task view (``GET /api/runs/{run_id}/task``): once
    ``/ai-sync-v2`` close-out moves a completed task into ``archive/`` it leaves
    the active index, so the index-gated task routes stop resolving it — but a
    persisted run still references its id and must keep showing its file. Same
    single-segment id validation and resolved-path containment as
    :func:`read_task_content`, scoped to the ``archive`` subdir, so neither a
    crafted id nor a symlinked archive file can read outside it. There is
    deliberately no archived-task *listing*: an archived file is reachable only
    when a caller already holds its exact id (e.g. from a run record), never by
    enumerating ``archive/``.
    """
    return _read_contained_task_file(repo.path / ".ai-tasks" / "archive", task_id)


def read_task_state(repo: ManagedRepo, task_id: str) -> TaskFileState:
    """Resolve a known task active-then-archived, with status from the same file.

    This does not enumerate archives or change index-gated task admission. A
    disappearing active file can be an atomic archive move; follow through to
    the archive, while other read failures remain failures rather than absence.
    """
    for source, reader in (
        ("active", read_task_content),
        ("archived", read_archived_task_content),
    ):
        try:
            content = reader(repo, task_id)
        except FileNotFoundError:
            content = None
        if content is not None:
            status = _parse_frontmatter(content).get("status") or STATUS_UNKNOWN
            return TaskFileState(status=status, source=source, content=content)
    return TaskFileState(status=STATUS_MISSING, source=None, content=None)


def read_session_est(repo: ManagedRepo, task_id: str) -> SessionEst | None:
    """Return the task's typed ``session-est`` (current/total), or ``None``.

    Reads the frontmatter ``session-est: <current>/<total>`` from the active
    ``.ai-tasks/<task_id>.md`` first, then the archived copy — a completed task's
    run history still references its id after close-out moves the file into
    ``archive/`` (same active-then-archive fallback as the run-scoped task view).
    ``None`` means "unknown": the file is absent, has no frontmatter, omits
    ``session-est``, or its value is not two ``/``-separated non-negative
    integers. Only genuine *absence* of the active file falls through to archive;
    a present active file with a malformed est stays unknown (the active file is
    authoritative). This reads only the frontmatter est —
    :func:`list_repo_tasks` remains the source of status/summary/blockers. It
    does not swallow IO errors (a mid-read failure propagates, matching
    :func:`read_task_content`); callers that need isolation wrap it.
    """
    content = read_task_content(repo, task_id)
    if content is None:
        content = read_archived_task_content(repo, task_id)
    if content is None:
        return None
    return _parse_session_est(_parse_frontmatter(content).get("session-est"))


def parse_prefetch(content: str | None) -> tuple[str, ...]:
    """Return a task file's frontmatter ``prefetch:`` entries, verbatim.

    Takes the file *content* rather than ``(repo, task_id)`` because the caller
    that needs it — the evaluation evidence builder — already resolved the target
    through the active-then-archive fallback and holds the text. Entries are
    returned exactly as written, including malformed ones: this module owns the
    frontmatter grammar, while what counts as a usable ``.ai/`` doc path is the
    consumer's rule (:func:`orch_hub.ai_snapshot._prefetch_rel`), and folding the
    two together here would hide a malformed entry the consumer wants to warn
    about. Absent content, absent frontmatter, an absent field, and ``[]`` all
    yield ``()``.
    """
    if not content:
        return ()
    return _parse_inline_list(_parse_frontmatter(content).get("prefetch"))


def _parse_session_est(raw: str | None) -> SessionEst | None:
    """Parse a ``session-est`` frontmatter value ``<current>/<total>``.

    ``None`` for an absent value or any shape that is not exactly two
    slash-separated non-negative integers — ``2`` (no total), ``/3`` (no
    current), ``abc/3``, ``2/3/4``, or an empty string all degrade to unknown
    rather than raising.
    """
    if raw is None:
        return None
    parts = raw.split("/")
    if len(parts) != 2:
        return None
    current, total = parts[0].strip(), parts[1].strip()
    # ASCII-only: ``str.isdigit()`` is also true for chars like superscript "²"
    # that ``int()`` rejects, so guard with ``isascii()`` to keep the "never
    # raises" contract (every accepted char is then an int-able 0-9).
    if not _ascii_digits(current) or not _ascii_digits(total):
        return None
    return SessionEst(current=int(current), total=int(total))


def _ascii_digits(text: str) -> bool:
    """True iff ``text`` is a non-empty run of ASCII decimal digits (0-9)."""
    return bool(text) and text.isascii() and text.isdigit()


def _read_contained_task_file(base_dir: Path, task_id: str) -> str | None:
    """Read ``<base_dir>/<task_id>.md`` iff it is safe and stays contained.

    Shared by the active (:func:`read_task_content`) and archived
    (:func:`read_archived_task_content`) readers so both enforce identical
    id/containment discipline; only ``base_dir`` differs. ``task_id`` must be a
    single path segment (no separators, no null byte, not a dot ref), the file
    must exist, and — because ``is_file()``/``read_text()`` follow symlinks — its
    fully resolved target must stay under the resolved ``base_dir``, so neither a
    crafted id nor a symlinked task file can read outside the intended directory.
    """
    if (
        not task_id
        or "/" in task_id
        or "\\" in task_id
        or "\x00" in task_id
        or task_id in (".", "..")
    ):
        return None
    task_path = base_dir / f"{task_id}.md"
    if not task_path.is_file():
        return None
    resolved = task_path.resolve()
    if not resolved.is_relative_to(base_dir.resolve()):
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

    The id is always the first cell. The summary is positional by default — the
    third cell — which covers the three-column ``| Task | Status | Summary |``
    layout and any other third column (e.g. quantx's ``Notes``). A header that
    names a ``title`` column repositions the summary, so the five-column
    ``| id | title | status | session-est | blockers |`` layout (hkchain) reads
    its summary from ``title`` (index 1) instead. Single-column ``| Task |`` rows
    have no third cell and so carry no summary. The title, header, and ``|---|``
    separator rows are skipped, and rows may omit the summary cell. The index
    status/blockers columns are intentionally not read — task-file frontmatter is
    the source of truth for those.
    """
    summary_col = _DEFAULT_SUMMARY_COL
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
        if cells[0].lower() in _ID_HEADERS:
            named = _named_summary_column(cells)
            if named is not None:
                summary_col = named
            continue
        summary = None
        if len(cells) > summary_col and cells[summary_col]:
            summary = cells[summary_col]
        rows.append((_normalize_task_id(cells[0]), summary))
    return rows


def _named_summary_column(header_cells: list[str]) -> int | None:
    """Return the index of an explicitly-named summary column, or ``None``.

    Only a ``Summary`` or ``title`` header (case-insensitive) counts; any other
    header — including quantx's ``Notes`` third column — returns ``None`` so the
    caller keeps the legacy positional default (:data:`_DEFAULT_SUMMARY_COL`).
    """
    for index, cell in enumerate(header_cells):
        if cell.lower() in _SUMMARY_HEADERS:
            return index
    return None


def _normalize_task_id(cell: str) -> str:
    """Reduce an index ``Task`` cell to the bare task id used for file lookup.

    Managed repos differ in how they render the ``Task`` column: some list a
    bare id (``2026-07-05-alpha``), others a Markdown link (quantx uses
    ``[2026-04-13-golden-family-a](2026-04-13-golden-family-a.md)``). A bare cell
    is returned unchanged; a Markdown-link cell resolves to its link target's
    filename stem so the id maps to the real ``.ai-tasks/<id>.md`` file.

    Only the target's basename is used, so directory components (``../``,
    ``dir/``) are discarded and a link can never steer the lookup outside
    ``.ai-tasks``. A cell that is not a well-formed link, or whose target yields
    no stem, is returned verbatim, so its behavior is identical to a bare id and
    a genuinely absent file still reports ``missing``.
    """
    match = _MARKDOWN_LINK.match(cell)
    if match is None:
        return cell
    stem = PurePosixPath(match.group("target").strip()).name
    if stem.endswith(".md"):
        stem = stem[: -len(".md")]
    return stem or cell


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
