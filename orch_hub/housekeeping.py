"""AI memory housekeeping marker helpers for managed repos."""

from __future__ import annotations

from pathlib import Path

from .models import ManagedRepo

HOUSEKEEPING_MARKER = Path(".ai") / ".housekeeping-pending"
"""Repo-relative marker file written by the AI memory workflow."""


def housekeeping_pending(repo: ManagedRepo) -> bool:
    """Return whether the repo has pending AI memory housekeeping."""
    return (repo.path / HOUSEKEEPING_MARKER).is_file()
