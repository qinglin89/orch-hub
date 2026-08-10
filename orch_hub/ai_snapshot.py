"""Bounded, read-only view of one managed repo's ``.ai/`` memory snapshot.

The task-evaluation evidence pack (:mod:`orch_hub.evaluation`) carries what a
task's *runs* did — records, events, sessions, log tails, a commit window. This
module carries what the task's runs were supposed to *leave behind*: the
project's ``.ai/`` memory snapshot, plus the patches that changed it during the
run window. Without it two of the semantic evaluator's dimensions have no ground
truth at all — ``.ai/`` freshness/utility is unassessable, and knowledge-signal
density has to be inferred from the task file and commit subjects.

Why ``.ai/`` is admissible when code diffs are not: the evidence pack's
bounded-evidence rule bars raw diffs and transcripts because they are unbounded.
``.ai/`` is the one part of a target repo whose size is capped *by protocol* —
the memory contract holds a single doc to ~3000 tokens and the router to ~1500 —
so the eager set is bounded by construction, not by hope. The caps below are a
second, mechanical bound on top of that, for a target whose snapshot has drifted
past its own contract.

Three parts, each independently degradable (:class:`AiSnapshot`):

- **the eager set** — ``.ai/index.md``, ``.ai/map.md``, and the current
  entrypoint of each eager content topic (``overview``, ``architecture``,
  ``design``, ``conventions``). An entrypoint is resolved the way every other
  consumer resolves it (:func:`resolve_entrypoint`): ``.ai/index.md`` routing
  first, then file shape. A statically-spelled ``.ai/<topic>.md`` would silently
  read nothing after a memory-protocol §4 directory-form upgrade.
- **the prefetch slice** — the lazy docs the task's own frontmatter ``prefetch:``
  list names, i.e. the part of the snapshot the sessions declared they used.
- **the windowed diff** — ``.ai/``-scoped patches over the *same* run-lifecycle
  window the pack's commit evidence uses, byte-capped.

Everything here is read-only and total: an absent ``.ai/``, a snapshot dir that
resolves outside the managed repo, an entrypoint that routing points at but that
does not exist, an unreadable file, a malformed ``prefetch:`` entry, a git
failure, or a cap hit each produce a warning plus a false/degraded availability
flag — never an exception. Missing evidence lowers the downstream evaluator's
confidence; it does not fail the evaluation. Degradation is also *per part*: a
failed diff read keeps the docs that were collected, and vice versa.

The caps below bound the **reads**, not just what is retained afterwards. Every
doc is opened and read up to its cap (its true size comes from ``stat``), and the
patch stream's cap is pushed down into the git invocation itself
(:func:`orch_hub.git_meta.read_path_diff_between`). A snapshot that has drifted
far past its own protocol size contract — or a target repo that is not the one
its operator thinks it is — must cost bounded memory to *refuse*, not just
bounded memory to store.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from .git_meta import MAX_COMMIT_LIMIT, read_path_diff_between
from .models import ManagedRepo
from .subprocess_utils import Runner, run_command

AI_DIRNAME = ".ai"
"""The memory snapshot dir inside a managed target repo, and the git pathspec the
windowed diff is scoped to."""

ROUTER_NAME = "index.md"
"""The snapshot's routing document. Read twice: as an eager doc in its own right,
and as the authority on which entrypoint each eager content topic currently uses."""

ROOT_DOCS = ("index.md", "map.md")
"""Eager docs with fixed ``.ai/``-relative names (routing and the cross-reference
map). Unlike the content topics below these have no directory form, so they are
not routed — they are read where they are."""

EAGER_TOPICS = ("overview", "architecture", "design", "conventions")
"""The eager *content* topics, each of which may be deployed in either legal
entrypoint form (``.ai/<topic>.md`` or ``.ai/<topic>/index.md``). Mirrors the
memory-protocol loading contract's eager set, minus :data:`ROOT_DOCS`."""

ROLE_EAGER = "eager"
"""An :class:`AiDoc` that is part of the always-loaded eager set."""

ROLE_PREFETCH = "prefetch"
"""An :class:`AiDoc` pulled in because the task's frontmatter ``prefetch:`` named
it — the lazy slice the task declared it used."""

DOC_MAX_BYTES = 24 * 1024
"""Per-doc byte cap. The memory protocol caps a single doc at ~3000 tokens (~12KB),
so this is roughly 2x headroom: a conforming snapshot is never truncated, and a
drifted one is bounded rather than refused."""

ROUTER_MAX_BYTES = DOC_MAX_BYTES
"""Byte cap on the router *parse*. :func:`read_router` reads ``.ai/index.md``
before any doc read, so it needs its own bound — the same one the doc read uses,
since it is the same file. A router cut at the cap simply routes fewer topics,
and :func:`resolve_entrypoint` falls through to file shape for the rest."""

TOTAL_MAX_BYTES = 128 * 1024
"""Cap on the summed content of every doc in one snapshot read. Measured eager
sets run ~30-52KB, so a conforming repo plus its prefetch slice fits with room to
spare. Docs are read in priority order (:data:`ROOT_DOCS`, then
:data:`EAGER_TOPICS`, then prefetch), so an exhausted budget drops the least
load-bearing docs first."""

PREFETCH_MAX_DOCS = 12
"""Cap on how many ``prefetch:`` docs one snapshot read will collect. A task file
is human-written and could name an arbitrarily long list; the eager set is the
part that must always fit."""

DIFF_MAX_BYTES = 64 * 1024
"""Byte cap on the windowed ``.ai/`` patch stream, applied after the commit-count
cap git already enforces. Held separate from :data:`TOTAL_MAX_BYTES` because the
diff and the snapshot answer different questions (what changed vs. what is there
now), so neither should be able to starve the other."""

DIFF_COMMIT_LIMIT = MAX_COMMIT_LIMIT
"""Commit cap on the windowed diff, matching the pack's commit-window cap so the
patch stream and the commit list describe the same bounded window."""


@dataclass(frozen=True)
class AiDoc:
    """One ``.ai/`` document as the evidence pack carries it.

    ``path`` is repo-relative (``.ai/...``) so a reader of the persisted pack can
    name the file without knowing where the repo lives. ``present`` follows
    :class:`orch_hub.logs.LogSnapshot`: True only when the file was found *and*
    read into ``content``, so a doc dropped for budget or one that routing points
    at but that does not exist are both absent, each with a human ``detail``.
    ``size`` is the file's true byte size even when nothing was kept, and
    ``truncated`` means the content shown is shorter than that.
    """

    path: str
    role: str
    topic: str | None = None
    present: bool = False
    content: str | None = None
    size: int = 0
    truncated: bool = False
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "role": self.role,
            "topic": self.topic,
            "present": self.present,
            "content": self.content,
            "size": self.size,
            "truncated": self.truncated,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AiDiff:
    """The ``.ai/``-scoped patch stream for a task's run-lifecycle window.

    ``available`` False means the diff could not be read — no window to query, or
    a git problem — and ``detail`` says which. ``available`` True with an empty
    ``text`` is the opposite of a gap: git answered, and ``.ai/`` was not touched
    in the window. That is a first-class evaluation signal (a task that changed
    nothing in memory), so the two must never be collapsed.

    ``since``/``until`` restate the window the patches came from, so the section
    is self-describing when read back from a persisted pack.
    """

    available: bool
    since: str | None = None
    until: str | None = None
    text: str | None = None
    size: int = 0
    truncated: bool = False
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "since": self.since,
            "until": self.until,
            "text": self.text,
            "size": self.size,
            "truncated": self.truncated,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AiSnapshot:
    """A bounded, read-only view of one repo's ``.ai/`` memory, for one task.

    ``present`` is a readable ``.ai/`` dir *belonging to this repo*: a repo
    without one, or one whose ``.ai`` resolves outside the repo (a symlinked
    snapshot dir would otherwise be evidence about a different project), yields
    an otherwise-empty snapshot with a ``detail`` and a warning rather than an
    error — a target may legitimately predate the memory protocol.

    :attr:`snippets_available` and :attr:`diffs_available` are the two facts the
    semantic layer gates its ``.ai/``-grounded judgments on. They are computed
    from what this snapshot actually carries — not from whether the read was
    *attempted* — so a snapshot that degraded to nothing reports the same as a
    repo with no ``.ai/`` at all, which is the honest answer for an evaluator
    deciding whether it may assess ``.ai/`` freshness.

    ``warnings`` are this section's evidence-degradation notes, mirroring
    :class:`orch_hub.evaluation.RunEvidence`'s per-run warnings: local to the
    section, aggregated into the pack's summary count.
    """

    present: bool
    docs: tuple[AiDoc, ...] = ()
    diff: AiDiff | None = None
    warnings: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def snippets_available(self) -> bool:
        """True when at least one doc's content was actually read."""
        return any(doc.present and doc.content for doc in self.docs)

    @property
    def diffs_available(self) -> bool:
        """True when the windowed ``.ai/`` diff was read (possibly to no changes)."""
        return self.diff is not None and self.diff.available

    def to_dict(self) -> dict:
        diff = self.diff if self.diff is not None else AiDiff(available=False)
        return {
            "present": self.present,
            "snippets_available": self.snippets_available,
            "diffs_available": self.diffs_available,
            "docs": [doc.to_dict() for doc in self.docs],
            "diff": diff.to_dict(),
            "warnings": list(self.warnings),
            "detail": self.detail,
        }


def read_router(
    ai_dir: Path, *, root: Path | None = None, max_bytes: int = ROUTER_MAX_BYTES
) -> dict[str, str]:
    """Parse ``.ai/index.md``'s document table into ``{lowercased label: path}``.

    Mirrors how the session-start loaders read the router: any table row's second
    cell is the label and its third is the routed file, first row per label wins.
    Header and separator rows are harmless — their labels (``document``, ``---``)
    collide with no topic. An absent, escaping, or unreadable router is an empty
    mapping, so :func:`resolve_entrypoint` falls through to file shape.

    Only the first ``max_bytes`` are read, and only when the router resolves
    inside ``root`` (the resolved snapshot dir; ``ai_dir`` itself by default) —
    this read happens before any doc read, so it cannot be the one that escapes
    the snapshot or loads an unbounded file.
    """
    contain_root = root if root is not None else _resolve(ai_dir)
    if contain_root is None:
        return {}
    target = _contained_path(ai_dir / ROUTER_NAME, contain_root)
    if target is None:
        return {}
    text = _read_head(target, max_bytes)
    if text is None:
        return {}
    entries: dict[str, str] = {}
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 3:
            continue
        label = cells[1].strip().lower()
        routed = cells[2].strip().replace("`", "")
        if label and routed and label not in entries:
            entries[label] = routed
    return entries


def resolve_entrypoint(ai_dir: Path, topic: str, router: Mapping[str, str]) -> str:
    """Return the ``.ai/``-relative entrypoint for one eager content topic.

    The resolution *order* is the contract, and it is shared with every other
    consumer of the snapshot (the deployment tool's
    ``resolve_memory_entrypoint``, the session-start hooks): the router first,
    then file shape, then the single-file default. Reimplemented here rather than
    imported because orch-hub reads target repos through file contracts only and
    never imports protocol tooling — the same reason task-index parsing is local.

    A routed value is honored only when it names one of the topic's two legal
    forms *and* that file exists, so a router left pointing at a pre-upgrade path
    does not win over the file that is really there. With no usable route, a
    directory form that exists alone wins; otherwise the single-file default is
    returned even when it does not exist, so the caller records a concrete
    missing path rather than a vague "unresolved topic".
    """
    flat = f"{topic}.md"
    dir_index = f"{topic}/index.md"
    routed = _routed_rel(router, topic)
    if routed in (flat, dir_index) and (ai_dir / routed).is_file():
        return routed
    if (ai_dir / dir_index).is_file() and not (ai_dir / flat).is_file():
        return dir_index
    return flat


def prefetch_doc_path(entry: str) -> str | None:
    """Repo-relative ``.ai/...`` path for one frontmatter ``prefetch:`` entry.

    The public spelling of this module's prefetch-entry grammar
    (:func:`_prefetch_rel`), for the read-behavior reader
    (:mod:`orch_hub.session_reads`), which must decide whether an observed file
    read *is* one of the session's declared prefetch docs. It compares against
    read paths, which are repo-relative, so it needs the ``.ai/``-prefixed form
    rather than the snapshot-relative one the doc reader uses. Both spellings
    come from the one rule here, so a malformed entry cannot be admitted by one
    consumer and refused by the other.
    """
    rel = _prefetch_rel(entry)
    return None if rel is None else f"{AI_DIRNAME}/{rel}"


def is_eager_doc(repo_rel: str) -> bool:
    """True when a repo-relative ``.ai/`` path belongs to the always-loaded eager set.

    Membership is by *topic*, so both legal entrypoint forms of an eager content
    topic answer True without consulting the repo — which is what a consumer
    judging a historical session needs, since the form in effect back then may
    not be the form on disk now.

    The read-behavior reader uses this to exclude the eager set from its
    mid-flight retrieval trace: the eager set is in every session's context
    already (memory-protocol loading contract), so *not* reading it is not a
    missed consultation and reading it is not a retrieval.
    """
    prefix = f"{AI_DIRNAME}/"
    if not repo_rel.startswith(prefix):
        return False
    rel = repo_rel[len(prefix) :]
    if rel in ROOT_DOCS:
        return True
    return any(rel in (f"{topic}.md", f"{topic}/index.md") for topic in EAGER_TOPICS)


def read_ai_snapshot(
    repo: ManagedRepo,
    *,
    prefetch: Sequence[str] = (),
    since: str | None = None,
    until: str | None = None,
    window_detail: str | None = None,
    doc_max_bytes: int = DOC_MAX_BYTES,
    total_max_bytes: int = TOTAL_MAX_BYTES,
    diff_max_bytes: int = DIFF_MAX_BYTES,
    runner: Runner = run_command,
) -> AiSnapshot:
    """Read ``repo``'s ``.ai/`` snapshot and its windowed diff, bounded and total.

    ``prefetch`` is the target task's frontmatter ``prefetch:`` list verbatim
    (:func:`orch_hub.task_store.parse_prefetch`); malformed entries are warned
    about and skipped rather than rejecting the list. ``since``/``until`` are the
    pack's run-lifecycle window, passed through unchanged so the diff describes
    exactly the window the commit evidence does; ``window_detail`` is the
    caller's reason when there is no window, reused verbatim so both sections
    explain an absent window the same way.

    Two containment rules bracket the reads: the snapshot dir must resolve inside
    the managed repo (else this is another project's memory, and nothing is read),
    and every file read must resolve inside that snapshot dir. Both are decided on
    resolved paths, so a symlink cannot widen either boundary.

    Never raises for target-repo state: every failure mode degrades to a warning
    plus a false availability flag. The only writes are none — this is a pure
    read, and the ``git log`` it runs is read-only.
    """
    ai_dir = repo.path / AI_DIRNAME
    if not ai_dir.is_dir():
        return _unreadable_snapshot(
            f"{AI_DIRNAME}/ snapshot absent in target repo", since, until
        )
    root = _snapshot_root(repo, ai_dir)
    if root is None:
        # ``is_dir()`` follows symlinks, so a ``.ai`` link pointing at a sibling
        # checkout would otherwise hand another repo's memory back as this repo's
        # evidence — with every child check passing, since they are relative to
        # the resolved (outside) dir. Repo containment is therefore decided on the
        # root, before the router or any doc is read.
        return _unreadable_snapshot(
            f"{AI_DIRNAME}/ resolves outside the managed repo", since, until
        )

    warnings: list[str] = []
    wanted = _wanted_docs(ai_dir, root, prefetch, warnings)

    docs: list[AiDoc] = []
    budget = max(0, total_max_bytes)
    for rel, role, topic in wanted:
        doc, warning, spent = _read_doc(
            ai_dir,
            rel,
            root=root,
            role=role,
            topic=topic,
            max_bytes=doc_max_bytes,
            budget=budget,
        )
        docs.append(doc)
        if warning is not None:
            warnings.append(warning)
        budget -= spent

    diff = _read_diff(
        repo,
        since=since,
        until=until,
        window_detail=window_detail,
        max_bytes=diff_max_bytes,
        runner=runner,
        warnings=warnings,
    )
    return AiSnapshot(
        present=True, docs=tuple(docs), diff=diff, warnings=tuple(warnings)
    )


def _unreadable_snapshot(detail: str, since: str | None, until: str | None) -> AiSnapshot:
    """A snapshot that could not be read at all: no docs, no diff, one warning.

    Shared by the two whole-snapshot refusals — no ``.ai/`` dir, and a ``.ai/``
    that resolves outside the managed repo. Both skip git as well as the docs:
    with no readable snapshot there is nothing for a patch stream to be evidence
    *about*, and the caller's window reason is not the relevant fact here.
    """
    return AiSnapshot(
        present=False,
        diff=AiDiff(available=False, since=since, until=until, detail=detail),
        warnings=(f"{AI_DIRNAME}/: {detail}",),
        detail=detail,
    )


def _snapshot_root(repo: ManagedRepo, ai_dir: Path) -> Path | None:
    """The resolved ``.ai/`` dir, or ``None`` when it escapes the managed repo.

    Both sides are resolved before comparison so a repo reached through a
    symlinked parent (``/var`` -> ``/private/var`` on macOS) is not mistaken for
    an escape. A ``.ai`` symlink pointing *within* the repo is legitimate and
    resolves inside; the returned path is what every child containment check is
    then measured against, so the snapshot's own boundary stays where the repo
    put it.
    """
    resolved = _resolve(ai_dir)
    repo_root = _resolve(repo.path)
    if resolved is None or repo_root is None:
        return None
    return resolved if resolved.is_relative_to(repo_root) else None


def _wanted_docs(
    ai_dir: Path, root: Path, prefetch: Sequence[str], warnings: list[str]
) -> list[tuple[str, str, str | None]]:
    """Build the ordered ``(rel, role, topic)`` read list for one snapshot.

    Order is priority order, because the total byte budget is spent down it: the
    router and map, then each eager content topic at its resolved entrypoint,
    then the task's prefetch slice. A prefetch entry that names a doc already in
    the eager set is dropped silently — it is legal, just redundant — while a
    malformed one and an over-cap tail are warned about, since those are things a
    reader of the pack would otherwise mistake for "the task named nothing else".
    """
    wanted: list[tuple[str, str, str | None]] = [
        (name, ROLE_EAGER, PurePosixPath(name).stem) for name in ROOT_DOCS
    ]
    router = read_router(ai_dir, root=root)
    wanted += [
        (resolve_entrypoint(ai_dir, topic, router), ROLE_EAGER, topic)
        for topic in EAGER_TOPICS
    ]

    seen = {rel for rel, _, _ in wanted}
    kept = 0
    dropped = 0
    for entry in prefetch:
        rel = _prefetch_rel(entry)
        if rel is None:
            warnings.append(
                f"{AI_DIRNAME}/: prefetch entry is not a {AI_DIRNAME}/*.md path"
                f" ({entry!r})"
            )
            continue
        if rel in seen:
            continue
        if kept >= PREFETCH_MAX_DOCS:
            dropped += 1
            continue
        seen.add(rel)
        kept += 1
        wanted.append((rel, ROLE_PREFETCH, None))
    if dropped:
        warnings.append(
            f"{AI_DIRNAME}/: {dropped} prefetch doc(s) dropped past the"
            f" {PREFETCH_MAX_DOCS}-doc cap"
        )
    return wanted


def _read_doc(
    ai_dir: Path,
    rel: str,
    *,
    root: Path,
    role: str,
    topic: str | None,
    max_bytes: int,
    budget: int,
) -> tuple[AiDoc, str | None, int]:
    """Read one ``.ai/``-relative doc; return it, an optional warning, and bytes spent.

    Absence is the expected case for a topic a repo simply does not have, so it
    is recorded rather than raised. Containment is checked on the *resolved* path
    against ``root`` (the resolved snapshot dir): ``is_file``/``open`` follow
    symlinks, so a link inside ``.ai/`` pointing outside it would otherwise pull
    arbitrary file content into a persisted evidence artifact.

    Size comes from ``stat`` and the read stops at the cap, so an oversize doc
    costs the cap in memory rather than its own size — the truncation flag is the
    *result* of a bounded read, not a slice taken after an unbounded one. A doc
    whose retained bytes would not fit the remaining total budget is skipped
    without opening it at all.
    """
    repo_rel = f"{AI_DIRNAME}/{rel}"
    absent = AiDoc(path=repo_rel, role=role, topic=topic)
    path = ai_dir / rel
    try:
        if not path.is_file():
            return (
                replace(absent, detail="not present"),
                f"{repo_rel}: not present",
                0,
            )
        resolved = _contained_path(path, root)
        if resolved is None:
            return (
                replace(absent, detail=f"resolves outside {AI_DIRNAME}/"),
                f"{repo_rel}: resolves outside {AI_DIRNAME}/ and was not read",
                0,
            )
        size = resolved.stat().st_size
        keep = min(size, max(0, max_bytes))
        if keep > budget:
            return (
                replace(
                    absent, size=size, truncated=True, detail="content budget exhausted"
                ),
                f"{repo_rel}: omitted, {size} bytes exceeds the remaining content budget",
                0,
            )
        with resolved.open("rb") as handle:
            data = handle.read(keep)
    except OSError as err:
        return (
            replace(absent, detail=f"unreadable ({err})"),
            f"{repo_rel}: unreadable ({err})",
            0,
        )

    # Keeps the *head*: a doc leads with its frontmatter and routing. Decoding is
    # lossy-tolerant (matching the log tail reader), which also absorbs the
    # partial multi-byte character a cut at an arbitrary byte offset can leave.
    text = data.decode("utf-8", errors="replace")
    truncated = len(data) < size
    warning = (
        f"{repo_rel}: truncated to {len(data)} of {size} bytes" if truncated else None
    )
    return (
        AiDoc(
            path=repo_rel,
            role=role,
            topic=topic,
            present=True,
            content=text,
            size=size,
            truncated=truncated,
        ),
        warning,
        len(data),
    )


def _read_diff(
    repo: ManagedRepo,
    *,
    since: str | None,
    until: str | None,
    window_detail: str | None,
    max_bytes: int,
    runner: Runner,
    warnings: list[str],
) -> AiDiff:
    """Read the ``.ai/``-scoped patch stream for the window, bounded and best-effort.

    No window means no query at all — the caller already knows why (its
    ``window_detail``), and asking git for an unbounded history would be exactly
    the widening the bounded-evidence rule forbids. The byte cap is this module's
    policy but is enforced by the producer: it goes down into the git read
    (:func:`orch_hub.git_meta.read_path_diff_between`), which retains only that
    much and drains the rest.

    The diff degrades *alone*. Its expected failures (missing git, non-zero
    ``git log``) already come back as an unavailable log, and an unexpected one —
    a runner that raises for any other reason — is caught here rather than at the
    snapshot boundary, where it would discard perfectly good docs along with the
    patch stream that failed. The two parts answer different questions and fail
    independently.
    """
    if not since:
        return AiDiff(
            available=False,
            since=since,
            until=until,
            detail=window_detail or "no run window to scope the diff to",
        )
    cap = max(0, max_bytes)
    try:
        log = read_path_diff_between(
            repo,
            pathspec=AI_DIRNAME,
            since=since,
            until=until,
            max_bytes=cap,
            limit=DIFF_COMMIT_LIMIT,
            runner=runner,
        )
    except Exception as err:  # noqa: BLE001 - a failed diff must not drop the docs
        detail = f"git diff read failed ({type(err).__name__}: {err})"
        warnings.append(f"{AI_DIRNAME}/: windowed diff unavailable ({detail})")
        return AiDiff(available=False, since=since, until=until, detail=detail)
    if not log.available:
        warnings.append(f"{AI_DIRNAME}/: windowed diff unavailable (git history unreadable)")
        return AiDiff(
            available=False,
            since=since,
            until=until,
            detail="git history unavailable",
        )
    if log.truncated:
        warnings.append(
            f"{AI_DIRNAME}/: windowed diff truncated to {cap} of {log.size} bytes"
        )
    return AiDiff(
        available=True,
        since=since,
        until=until,
        text=log.text,
        size=log.size,
        truncated=log.truncated,
    )


def _read_head(path: Path, limit: int) -> str | None:
    """Decode at most ``limit`` bytes from the start of ``path``, or ``None``.

    Bounds bytes, not characters, because the caps are byte budgets, and decodes
    lossy-tolerantly so a cut mid-character yields a replacement rather than an
    error. ``None`` is any OS failure — the callers here all have a defined
    degraded answer for "could not read this file".
    """
    try:
        with path.open("rb") as handle:
            return handle.read(max(0, limit)).decode("utf-8", errors="replace")
    except OSError:
        return None


def _resolve(path: Path) -> Path | None:
    """Fully resolve ``path``, or ``None`` when the filesystem refuses.

    A symlink loop or an unreadable parent is not an error to propagate: every
    caller treats an unresolvable path as "not contained", which is the safe
    answer for a containment check.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _contained_path(path: Path, root: Path) -> Path | None:
    """``path`` resolved, when it lies inside the already-resolved ``root``.

    Returning the resolved path rather than a boolean is the point: every read
    here opens what was checked, instead of re-walking the original path and
    giving a symlink a second chance to point somewhere else.
    """
    resolved = _resolve(path)
    if resolved is None or not resolved.is_relative_to(root):
        return None
    return resolved


def _routed_rel(router: Mapping[str, str], topic: str) -> str | None:
    """The ``.ai/``-relative path the router assigns to ``topic``, or ``None``.

    Router cells are written by hand and may or may not carry the ``.ai/``
    prefix, so both spellings normalize to the same relative path. An absolute
    path or one containing ``..`` is discarded rather than normalized: the router
    routes within the snapshot, and anything else is not a route this reader will
    follow.
    """
    routed = router.get(topic, "").strip()
    if not routed:
        return None
    if routed.startswith("./"):
        routed = routed[2:]
    if routed.startswith(f"{AI_DIRNAME}/"):
        routed = routed[len(AI_DIRNAME) + 1 :]
    if not routed or routed.startswith("/") or ".." in PurePosixPath(routed).parts:
        return None
    return routed


def _prefetch_rel(entry: str) -> str | None:
    """The ``.ai/``-relative path for one frontmatter ``prefetch:`` entry, or ``None``.

    The taskfile schema spells these as ``.ai/*.md`` paths, and that is enforced
    rather than guessed at: an entry without the prefix or the extension is
    malformed (the same rule the static-metrics protocol check applies), and one
    that escapes the snapshot is refused outright.
    """
    value = entry.strip().strip("`").strip()
    if not value.endswith(".md"):
        return None
    if value.startswith("./"):
        value = value[2:]
    if not value.startswith(f"{AI_DIRNAME}/"):
        return None
    rel = value[len(AI_DIRNAME) + 1 :]
    if not rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts:
        return None
    return rel
