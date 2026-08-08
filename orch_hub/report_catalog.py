"""Global, append-only catalog of completed task-evaluation reports.

The evaluation subsystem stores every report under
``<data_dir>/evaluations/<repo-slug>/<task-id>/<evaluation-id>/``. That layout is
right for the per-task dashboard — but it is the wrong shape for an external
protocol-evolution consumer, which wants to *incrementally* discover new reports
across **every** managed repo. Doing that from the tree alone means walking every
repo, every task, and every evaluation dir on each poll, and then ordering the
results by scoped ``evaluation_id`` timestamps that collide across repos and
carry no cross-repo meaning.

This module adds the missing publication surface: one durable, append-only
catalog beside the existing trees, parallel to ``runs/``, ``intake/``,
``semantic-jobs/``, and ``evaluations/``:

.. code-block:: text

    <data_dir>/report-catalog/
        catalog.jsonl    one JSON object per line, one line per cataloged report

It is strictly a **publication index**, not a second copy of the reports: an
entry names where the artifacts live and carries their content hashes plus the
provenance needed to cohort them, and the artifacts themselves stay exactly where
the evaluation subsystem put them. Nothing here mutates target repos, task files,
run records, or the evaluation artifacts it indexes.

Three properties make the feed usable for incremental import:

- **Global identity.** Each entry gets ``seq``, the catalog's monotonic append
  counter (the cursor), and ``report_key``, a stable opaque digest of the source
  ``(repo-slug, task-id, evaluation-id)`` triple (:func:`report_key`). The key is
  deliberately order-free: a consumer stores it and re-fetches by it, and never
  has to compare scoped ``evaluation_id`` stamps — which are per-task and collide
  across repos — to reason about order or identity. Because the key is derived
  from the source identity rather than allocated, re-cataloging the same
  evaluation is a no-op, which is what makes reconciliation idempotent.
- **Artifact-based candidacy.** An evaluation is a report candidate only when
  its whole L1+L2 artifact set (:data:`REQUIRED_ARTIFACTS`) is already durable
  *and* coherent — every artifact's own recorded identity must agree with the
  directory it was found under (:func:`_require_identity`). *How* it was
  evaluated is outside this module: a dir missing any of the four files is simply
  not a candidate, so a partial evidence-only generation or a failed semantic job
  can never surface as a ready report.
- **Self-healing durability.** Appends are single fsynced lines with a
  torn-tail heal (:meth:`ReportCatalog._append_line`), taken under a lock that
  spans processes (:meth:`ReportCatalog._locked`), and
  :meth:`ReportCatalog.reconcile` re-derives everything missing by scanning the
  evaluations tree. So the live hook is an *optimization for promptness*: if it
  is unwired, fails, or the hub dies mid-write, the next reconciliation catalogs
  the report anyway, and never mints a second global identity for it.

Damage the write path cannot cause, the read path still has to answer for: the
file is append-only and never rewritten, so an operator's edit, a merge of two
catalogs, or a restore over a live one is the one way its invariants break. Two
audits keep that from becoming a silent wrong answer rather than a loud one. A
repeated ``seq`` or ``report_key`` makes the cursor ambiguous, so every read fails
closed until it is repaired (:func:`_require_unique_identity`); an entry whose
stored source identity is not the one its ``report_key`` derives from cannot
address its own artifacts, so its bundle is explicitly unavailable
(:func:`_identity_fault`). Both refuse to guess, because the alternative is
handing a consumer a report it will import as authentic.

Publication is gated on the evaluated task being **archived**, because the feed
exists to hand a consumer finished protocol stories, not in-progress ones. Being
archived is an event that happens *after* the report is written, and no artifact
can record it retroactively, so the catalog resolves it in two steps: the
semantic report's own ``target_source`` stamp (an evaluation run after close-out
is publishable immediately), else a read-only probe of the target repo for
``.ai-tasks/archive/<task-id>.md`` (:meth:`ReportCatalog._task_archived`). That
probe is this module's single live read, and it is what makes a report of a
then-active task publish later, at the sequence where it *became* publishable,
instead of being stranded ineligible forever. A complete report the feed has not
published yet is therefore not disposable: retention consults
:meth:`ReportCatalog.unpublished_ids` so pruning cannot delete a report before
its artifacts have been indexed (after which the entry — hashes and all —
outlives them by design).

Publication and that retention guard are two callers of **one** rule
(:meth:`ReportCatalog._publishable`, over a :class:`_Candidate` read from the
evaluation's own artifacts), because any daylight between them is a lost report:
a guard that decides a dir is disposable while a sweep would publish it licenses
the one deletion nothing can heal. Which is also why the archive probe uses the
repo path the *evidence pack* recorded rather than the registry's current one —
those drift — and why a candidate that cannot be judged (unreadable or incoherent
artifacts, a failed probe) is undecided rather than "not publishable": each caller
then resolves the uncertainty in its own safe direction, publication by retrying
and retention by keeping.

The live hook is :class:`~orch_hub.semantic.ReportCataloger`, satisfied
structurally by :class:`ReportCatalog` and called by
:meth:`orch_hub.semantic.SemanticEvaluator.evaluate` once its artifact set is
durable. The seam keeps the dependency one-way — the evaluator never imports this
module — so this module can import the artifact-name constants from their home
modules instead of duplicating them.

Ownership boundary: orch-hub *publishes* evaluation artifacts. It does not judge
them, decide protocol evolution, or create protocol-improvement tasks; that is
the consumer's job, which is why an entry carries provenance and hashes rather
than any verdict of its own.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .evaluation import (
    EVALUATIONS_SUBDIR,
    EVIDENCE_NAME,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    evaluation_artifact_dir,
    evaluation_order_key,
    repo_slug,
    task_evaluations_dir,
)
from .models import (
    ManagedRepo,
    ReportArtifact,
    ReportBundle,
    ReportCatalogEntry,
    ReportPage,
    ReportProvenance,
)
from .semantic import SEMANTIC_REPORT_MD_NAME, SEMANTIC_REPORT_NAME
from .static_metrics import STATIC_METRICS_NAME
from .task_store import read_archived_task_content

_log = logging.getLogger(__name__)

CATALOG_SUBDIR = "report-catalog"
"""Top-level hub-data-dir subtree holding the catalog, parallel to the run
manager's ``runs/``, the intake manager's ``intake/``, the semantic-job manager's
``semantic-jobs/``, and the evaluation subsystem's ``evaluations/`` trees."""

CATALOG_NAME = "catalog.jsonl"
"""The append-only catalog file: one :class:`ReportCatalogEntry` per line."""

CATALOG_LOCK_NAME = "catalog.lock"
"""Sidecar lock file guarding the read/check/allocate/append transaction. Its
*content* is irrelevant — only the POSIX advisory lock on it matters — so it is
kept beside the catalog rather than locking the catalog file itself, whose
append-mode handle is opened and closed per write."""

REQUIRED_ARTIFACTS: tuple[str, ...] = (
    EVIDENCE_NAME,
    STATIC_METRICS_NAME,
    SEMANTIC_REPORT_NAME,
    SEMANTIC_REPORT_MD_NAME,
)
"""The complete L1+L2 artifact set an evaluation must already hold to be a report
candidate: bounded evidence, the deterministic static report, the model-judged
semantic report, and its Markdown render. Imported from the layers that own each
filename rather than re-declared, so the catalog cannot drift from what the
evaluator actually writes. All four must be present — the semantic evaluator
writes them as one all-or-nothing dir, so a dir missing any of them is either an
evidence/metrics-only generation or an interrupted attempt, and neither is a
publishable report."""

REPORT_KEY_PREFIX = "rpt-"
"""Namespace prefix on a public report key, so a key is recognizable in consumer
logs/URLs and cannot be confused with an ``evaluation_id`` or ``run_id``."""

_REPORT_KEY_HEX = 32
"""Digest hex characters kept in a report key (128 bits). Enough that collisions
are not a practical concern, short enough to stay a comfortable URL/path
segment."""

_HASH_CHUNK = 1 << 16
"""Read size for artifact hashing. Evidence packs are bounded but not tiny, and
nothing here needs the whole file in memory at once."""

_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")
"""What a recorded artifact digest must look like to be verifiable at all. A
manifest value that is not one cannot be any content's sha256, so the read path
treats it as *no* digest (unverified) rather than as a mismatch — the difference
between "this report was edited" and "this entry never recorded a checkable
hash"."""

DEFAULT_PAGE_LIMIT = 50
"""Entries per feed page when a consumer states no preference. Sized for a poll
that usually returns nothing new; a backfill raises it explicitly."""

MAX_PAGE_LIMIT = 500
"""Ceiling on a caller-chosen page size. An entry carries provenance plus four
artifact digests, so this bounds one response at a few hundred KB. Unlike the run
history pager — whose page size is a traversal invariant, deliberately not a
query parameter — the feed lets a consumer choose, because ``has_more`` makes a
short page unambiguous and a bulk importer and an idle poller genuinely want
different sizes. Exceeding it is a fail-closed error rather than a silent clamp
(config contract: malformed explicit values fail loudly)."""

PROTOCOL_UNAVAILABLE_DETAIL = (
    "evidence packs carry no .ai-protocol version for the evaluated task"
)
"""Why protocol provenance is explicitly unavailable. Stated as a reason rather
than left empty so a consumer cannot read "no protocol recorded" as "the default
protocol": the evidence layer simply does not capture it today."""

CONFIG_UNAVAILABLE_DETAIL = (
    "evidence packs carry no orchestrator effective-config snapshot"
)
"""Why orchestrator-config provenance is explicitly unavailable. The snapshot
exists per run (``effective_config.json`` in the run dir) but is not part of the
bounded evidence pack, and the catalog reads only durable evaluation artifacts —
never live run state — so an entry stays reproducible from what it names."""


class ReportCatalogError(Exception):
    """A report could not be cataloged (unreadable/malformed source artifacts).

    Raised by the per-evaluation build so one broken evaluation dir is isolated:
    :meth:`ReportCatalog.reconcile` logs and skips it (the sweep continues), and
    the live hook path lets it reach the evaluator's best-effort wrapper. It never
    leaves a partial line in the catalog, because the entry is fully built before
    anything is appended.
    """


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def report_key(*, repo_slug: str, task_id: str, evaluation_id: str) -> str:
    """Return the stable public report key for one source evaluation.

    A namespaced digest of the ``(repo-slug, task-id, evaluation-id)`` triple that
    *locates the artifacts* — the hub-owned storage identity, which is what makes
    the key stable across restarts, reconciliations, and repo renames that do not
    move the evaluations tree. Two different repos whose evaluations collide on
    ``evaluation_id`` still get different keys because the repo slug is part of
    the digest.

    Derived, not allocated: re-deriving it for an already-published evaluation
    yields the same key, which is what makes :meth:`ReportCatalog.reconcile` and
    the live hook converge on one identity per source evaluation instead of
    racing to mint two. (Allocation is still serialized — ``seq`` has no such
    property — but a *lost* lock could at worst duplicate a number here, never an
    identity, and a second sweep still adds nothing.) It carries no
    ordering information by construction (a digest of identity, not of time), so a
    consumer cannot accidentally build ordering assumptions on it — ``seq`` is the
    only order in the feed. Components are joined with a null byte, which cannot
    appear in any of them (all three are validated single path segments), so no
    two distinct triples can hash the same joined string.
    """
    raw = "\x00".join((repo_slug, task_id, evaluation_id)).encode("utf-8")
    return REPORT_KEY_PREFIX + hashlib.sha256(raw).hexdigest()[:_REPORT_KEY_HEX]


class ReportCatalog:
    """Durable append-only catalog of published reports, gated like evaluation.

    One instance lives in the resident hub service. It shares the evaluation
    subsystem's ``Config.evaluation_enabled`` gate, so a deployment that disables
    evaluation gets the same fail-closed boundary here as everywhere else in the
    subsystem (the API maps it to the uniform 404) — a disabled hub publishes
    nothing and reconciles nothing.

    Every entry is a feed item: publication is the eligibility decision (a
    complete, coherent report whose task is archived), so the read paths need no
    second filter and ``seq`` is exactly the order in which reports *became*
    publishable. A report of a task that was still active is not dropped — it is
    simply not published yet, and the next :meth:`reconcile` picks it up once its
    task file has moved into ``.ai-tasks/archive/``.

    ``clock`` must return timezone-aware UTC datetimes (it stamps
    ``cataloged_ts``). Sequence allocation is derived from the *file* on every
    cycle, never carried in memory across cycles, so a restart (or a crash
    mid-reconcile) resumes numbering correctly from durable state; the whole
    read-check-allocate-append cycle runs under :meth:`_locked`, which excludes
    both concurrent threads (parallel semantic jobs finish on independent daemon
    threads) *and* a second process sharing the data dir — a CLI sweep beside the
    resident service, or an overlapping restart — either of which would otherwise
    read the same durable maximum and mint the same ``seq`` twice.

    Every write is an append of one complete line: entries are immutable, and the
    file is never rewritten. That is what lets a consumer treat ``seq`` as a
    stable cursor while new reports keep arriving.
    """

    def __init__(
        self,
        config: Config,
        *,
        clock: Callable[[], dt.datetime] = _utc_now,
    ):
        self._config = config
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._config.evaluation_enabled

    def _require_enabled(self) -> None:
        if not self._config.evaluation_enabled:
            raise EvaluationDisabledError(
                "task evaluation is disabled (ORCH_HUB_EVAL_ENABLED)"
            )

    @property
    def catalog_dir(self) -> Path:
        return self._config.data_dir / CATALOG_SUBDIR

    @property
    def catalog_path(self) -> Path:
        return self.catalog_dir / CATALOG_NAME

    @property
    def lock_path(self) -> Path:
        return self.catalog_dir / CATALOG_LOCK_NAME

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the catalog's write transaction against threads *and* processes.

        Two nested locks, because ``seq`` allocation is a read-check-allocate-
        append cycle and any interleaving of it duplicates a sequence number (and
        with it a ``report_key``), which silently breaks cursor pagination — a
        consumer that resumes after the first of the two duplicates never sees the
        second. The instance lock is the cheap intra-process case; the POSIX
        advisory lock on :attr:`lock_path` covers what the instance lock provably
        cannot — a second ``ReportCatalog`` over the same data dir, in this process
        or another one (a CLI sweep, an overlapping restart). ``flock`` is
        associated with the open file description, so a fresh handle per entry
        makes it exclusive between threads as well.

        Held for the whole of a :meth:`reconcile` sweep rather than per append:
        the sweep re-derives its next sequence from the file once, so releasing
        between appends would reintroduce exactly the interleaving this prevents.
        The lock is released by the OS if the holder dies, so a crashed sweep
        cannot wedge the feed.
        """
        with self._lock:
            self.catalog_dir.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- reads ----------------------------------------------------------------

    def entries(
        self, *, after_seq: int | None = None, limit: int | None = None
    ) -> list[ReportCatalogEntry]:
        """Return cataloged entries in global order, optionally after a cursor.

        The durable read primitive :meth:`page` (and through it the feed route) is
        a thin adapter over: ``after_seq`` is the cursor — strictly greater, so a
        consumer that stores the last ``seq`` it imported resumes without
        re-reading or skipping an entry — and ``limit`` caps one page. Entries are
        returned in ascending
        ``seq``, which is append order, so concurrent appends only ever add to the
        *end* of a page boundary and never renumber what a consumer already saw.
        Every entry is a feed item (publication *is* the eligibility decision), so
        there is no second filter to apply here.

        Deliberately outside :meth:`_locked`: a reader must never wait on a long
        reconciliation sweep, and it does not need to — an append adds a whole
        line, so the worst a concurrent write can do is expose a half-written tail
        that this read skips and the next poll returns (the cursor has not moved
        past it). Corrupt lines are skipped for the same reason: a torn tail from
        a crashed append, or hand-editing, must not make the whole catalog
        unreadable. Gated and read-only.
        """
        self._require_enabled()
        found = self._read_entries()
        if after_seq is not None:
            found = [entry for entry in found if entry.seq > after_seq]
        if limit is not None and limit >= 0:
            found = found[:limit]
        return found

    def find(self, key: str) -> ReportCatalogEntry | None:
        """Return the entry for one public report key, or ``None`` if uncataloged.

        The by-key read behind the export route (:meth:`read_bundle`): a consumer
        that stored a ``report_key`` resolves it here without knowing the
        repo/task/evaluation directory layout. ``None`` means the key names no
        published report — distinct from a published report whose artifacts a
        retention prune has since removed, which stays an entry and is reported as
        explicitly unavailable by the read path. Lock-free like :meth:`entries`.
        Gated and read-only.
        """
        self._require_enabled()
        for entry in self._read_entries():
            if entry.report_key == key:
                return entry
        return None

    def artifact_dir(self, entry: ReportCatalogEntry) -> Path:
        """Contained artifact dir for a cataloged entry (pure path math).

        Resolves through the evaluation subsystem's own contained-path helper, so
        the same unsafe-segment rejection guards a catalog-driven read that guards
        every other artifact read. Passing the stored slug back through the slug
        rule is harmless for an entry this module wrote (the rule is idempotent —
        :func:`orch_hub.evaluation.repo_slug`) but it is *not* injective, so this is
        path math only: whether the stored identity is the one the artifacts were
        published under is a separate question, answered by :func:`_identity_fault`
        before :meth:`read_bundle` touches the filesystem. The dir may not exist —
        retention prunes artifacts while entries persist — so callers must treat
        absence as "no longer retained", not as an error.
        """
        return evaluation_artifact_dir(
            self._config.data_dir,
            repo_name=entry.repo_slug,
            task_id=entry.task_id,
            evaluation_id=entry.evaluation_id,
        )

    def page(
        self, *, after_seq: int | None = None, limit: int = DEFAULT_PAGE_LIMIT
    ) -> ReportPage:
        """Return one cursor-anchored page of the feed, with its next cursor.

        The incremental-poll primitive the feed route serializes. ``after_seq`` is
        the consumer's watermark — the highest ``seq`` it has already imported —
        and the page is the entries strictly after it, in ascending ``seq``. The
        cursor contract is what makes "exactly once, in global order" hold while
        the catalog is being appended to: entries are immutable and their sequence
        is monotonic, so an append can only ever extend the suffix a watermark
        names. It can never renumber, reorder, or insert *before* what a consumer
        already read, which is exactly the failure a timestamp-ordered feed over
        scoped ``evaluation_id`` stamps would have.

        **One cursor policy, and it never silently skips:** a cursor that is not a
        non-negative integer is rejected (``ValueError`` — a malformed watermark
        must not be quietly read as "start from the beginning", which would
        re-import everything, nor as "start from the end", which would lose
        reports). Any other cursor *resolves*, including a stale one: a watermark
        past the highest ``seq`` is simply a caught-up consumer and yields an empty
        page, and one naming a sequence no entry has (a number skipped by a torn
        append) still resolves as "strictly greater", so nothing between it and the
        next real entry can be stepped over. There is no cursor that both succeeds
        and loses an entry.

        The other half of "never silently skips" is not about the cursor a consumer
        sends but about the file it lands in: this can only promise strictly-greater
        traversal while sequences are unique, so a catalog holding a repeat raises
        :class:`ReportCatalogError` from the read underneath rather than serving a
        page that quietly omits the duplicate's twin
        (:func:`_require_unique_identity`).

        ``limit`` is validated against :data:`MAX_PAGE_LIMIT` rather than clamped,
        so a caller learns its request was out of range instead of silently
        receiving fewer entries than it sized its import around. One extra entry is
        read to answer ``has_more`` without a second pass; it is not returned.
        Gated and read-only, and lock-free like :meth:`entries` — a poller must
        never block behind a reconciliation sweep.
        """
        self._require_enabled()
        if after_seq is not None and after_seq < 0:
            raise ValueError(f"cursor must not be negative: {after_seq}")
        if limit < 1 or limit > MAX_PAGE_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}: {limit}")
        window = self.entries(after_seq=after_seq, limit=limit + 1)
        entries = tuple(window[:limit])
        cursor = after_seq if after_seq is not None else 0
        return ReportPage(
            entries=entries,
            cursor=cursor,
            # An empty page holds the watermark where it was, so a caught-up
            # consumer can store ``next_cursor`` unconditionally and keep polling
            # from the same place instead of restarting the feed.
            next_cursor=entries[-1].seq if entries else cursor,
            has_more=len(window) > limit,
        )

    def read_bundle(self, entry: ReportCatalogEntry) -> ReportBundle:
        """Read one published report's artifacts as a contained import bundle.

        The by-key fetch behind the export route, so a consumer works from the
        global key alone and never constructs a repo/task/evaluation path. Reads
        are contained by :meth:`artifact_dir` (the same unsafe-segment rejection as
        every other artifact read) and confined to the evaluation's own four files;
        nothing about the target repo is touched.

        Availability is **all-or-nothing**, matching what publication required: a
        bundle is served only when the complete L1+L2 set is still present and
        readable. Every other case is ``available: false`` with a ``detail`` — a
        pruned artifact dir (the designed end state of an entry outliving its
        artifacts), a partially removed one, an unreadable or unparseable file, a
        stored identity that cannot address a contained path at all, or one that
        can but is not provably the identity the report was published under
        (:func:`_identity_fault`). Failing closed matters more here than degrading
        gracefully: a half-populated or misattributed bundle would be imported as
        if it were the published report.

        Integrity is reported, not enforced: served artifacts whose bytes no longer
        match the digests recorded at publication are named in ``mismatched``, and
        ones the entry holds no usable digest for in ``unverified``. Neither
        suppresses content, because the recorded digests exist precisely so a
        consumer can tell an edited or uncheckable report from an intact one and
        decide for itself — hiding the bytes would hide the evidence. Gated and
        read-only.
        """
        self._require_enabled()
        try:
            eval_dir = self.artifact_dir(entry)
        except ValueError as err:
            # A hand-edited or foreign catalog line: nothing is read at all.
            return ReportBundle(
                entry=entry,
                available=False,
                detail=f"unsafe stored report identity ({err})",
            )
        # Also before any IO: a line that *can* name a contained dir but is not the
        # identity that dir was published under would otherwise serve another
        # report's artifacts, hash-verified, under this entry's key.
        fault = _identity_fault(entry)
        if fault is not None:
            return ReportBundle(
                entry=entry,
                available=False,
                detail=f"incoherent stored report identity ({fault})",
            )
        if not eval_dir.is_dir():
            return ReportBundle(
                entry=entry,
                available=False,
                detail="report artifacts are no longer retained",
            )
        # Same completeness rule publication used, asked for its detail rather
        # than its verdict: a partially pruned set must read as an incomplete set,
        # not as the IO error its first absent file would otherwise produce below.
        missing = _missing_artifacts(eval_dir)
        if missing:
            return ReportBundle(
                entry=entry,
                available=False,
                detail=(
                    "report artifact set is incomplete"
                    f" (missing {', '.join(missing)})"
                ),
            )
        cataloged = _cataloged_digests(entry)
        raw: dict[str, bytes] = {}
        mismatched: list[str] = []
        unverified: list[str] = []
        for name in REQUIRED_ARTIFACTS:
            try:
                data = (eval_dir / name).read_bytes()
            except OSError as err:
                return ReportBundle(
                    entry=entry,
                    available=False,
                    detail=f"unreadable report artifact {name} ({err})",
                )
            raw[name] = data
            expected = cataloged.get(name)
            if expected is None:
                # An entry from an older/leaner writer, or one a hand-edit left
                # without a usable digest for this artifact: nothing was compared,
                # which is a weaker claim than "matched" and is reported as such
                # rather than folded into the verified case.
                unverified.append(name)
            elif hashlib.sha256(data).hexdigest() != expected:
                mismatched.append(name)
        try:
            evidence = _bundle_json(EVIDENCE_NAME, raw[EVIDENCE_NAME])
            static = _bundle_json(STATIC_METRICS_NAME, raw[STATIC_METRICS_NAME])
            report = _bundle_json(SEMANTIC_REPORT_NAME, raw[SEMANTIC_REPORT_NAME])
            report_md = _bundle_text(
                SEMANTIC_REPORT_MD_NAME, raw[SEMANTIC_REPORT_MD_NAME]
            )
        except ReportCatalogError as err:
            return ReportBundle(entry=entry, available=False, detail=str(err))
        return ReportBundle(
            entry=entry,
            available=True,
            evidence=evidence,
            static_metrics=static,
            semantic_report=report,
            report_md=report_md,
            mismatched=tuple(mismatched),
            unverified=tuple(unverified),
        )

    # -- writes ---------------------------------------------------------------

    def record_evaluation(
        self, repo: ManagedRepo, task_id: str, evaluation_id: str
    ) -> ReportCatalogEntry | None:
        """Publish one evaluation if it is a complete, archived, unpublished report.

        The live hook (:class:`orch_hub.semantic.ReportCataloger`), called by the
        semantic evaluator once its whole artifact set is durable. Returns the new
        entry, or ``None`` for the three ordinary non-publication cases: the
        artifact set is incomplete, the evaluated task is not archived yet (an
        evaluation of live work — it publishes from a later sweep, once close-out
        archives the task), or it is already published. None of the three is an
        error; the last is what makes a re-run hook, a racing reconciliation, and
        a restart converge on exactly one global identity per source evaluation.

        Raises :class:`ReportCatalogError` when the report cannot be *judged* — the
        artifacts exist but cannot be read/parsed, contradict the identity of the
        directory holding them, or the archive probe failed. The evaluator isolates
        that so a catalog problem never fails an otherwise-successful evaluation,
        and the next reconciliation retries; retention, meanwhile, keeps the
        artifacts that retry needs (:meth:`unpublished_ids`).
        """
        self._require_enabled()
        eval_dir = evaluation_artifact_dir(
            self._config.data_dir,
            repo_name=repo.name,
            task_id=task_id,
            evaluation_id=evaluation_id,
        )
        # Take the slug from the resolved path rather than re-deriving it: the
        # ``<repo-slug>/`` level of the artifact dir IS the storage identity, so
        # a key built from it provably matches the one a reconciliation walk
        # (which only ever sees directory names) derives for the same evaluation.
        slug = eval_dir.parent.parent.name
        if not _is_complete(eval_dir):
            return None
        key = report_key(
            repo_slug=slug, task_id=task_id, evaluation_id=evaluation_id
        )
        with self._locked():
            existing = self._read_entries()
            if any(entry.report_key == key for entry in existing):
                return None
            return self._publish(
                seq=_next_seq(existing),
                slug=slug,
                task_id=task_id,
                evaluation_id=evaluation_id,
                eval_dir=eval_dir,
            )

    def unpublished_ids(self, repo: ManagedRepo, task_id: str) -> set[str]:
        """Evaluation ids of ``(repo, task_id)`` that retention must not delete.

        The retention guard (wired into
        :class:`~orch_hub.evaluation.EvaluationService`): these are complete
        reports the feed still owes an entry, and deleting one is *unrecoverable*
        — reconciliation heals only from artifacts that still exist, so a report
        whose live append failed and whose dir was then pruned by a later
        generation would vanish from the feed permanently. Retention keeps them
        until a sweep publishes them, after which the entry (hashes and all)
        outlives the artifacts by design and pruning is free again.

        ``repo`` only *locates* the artifacts — the same ``repo.name`` derivation
        :meth:`~orch_hub.evaluation.EvaluationService.list_evaluations` uses to
        pick the dirs this prune is about to delete, so the guard is guaranteed to
        be talking about them. Whether a dir is publishable is answered per
        candidate by the shared rule (:meth:`_publishable`) from that candidate's
        own artifacts, never from ``repo.path``: a repo re-registered at a new path
        still has evaluations whose evidence packs name the old one, and a guard
        probing the registry path would report no debt for reports a sweep then
        publishes at the recorded path — with retention free, in between, to delete
        one.

        Deliberately mirrors publication rather than protecting every uncataloged
        report: an evaluation of a still-active task is *supposed* to stay
        unpublished, and protecting those would suspend retention for exactly the
        common case. A candidate that cannot be judged is owed, because *undecided*
        is not "not publishable" — the same reason a sweep retries it. Reading each
        unpublished candidate's artifacts costs a JSON parse per dir (bounded by
        the retention cap, on a path that already follows a model call), which is
        the price of the two sides never disagreeing. Read-only; gated.
        """
        self._require_enabled()
        parent = task_evaluations_dir(
            self._config.data_dir, repo_name=repo.name, task_id=task_id
        )
        if not parent.is_dir():
            return set()
        slug = parent.parent.name
        known = {entry.report_key for entry in self.entries()}
        archived: dict[tuple[Path, str], bool] = {}
        owed: set[str] = set()
        for eval_dir in sorted(parent.iterdir()):
            if not eval_dir.is_dir() or not _is_complete(eval_dir):
                continue
            key = report_key(
                repo_slug=slug, task_id=task_id, evaluation_id=eval_dir.name
            )
            if key in known:
                continue
            try:
                publishable = self._publishable(
                    _read_candidate(
                        eval_dir,
                        slug=slug,
                        task_id=task_id,
                        evaluation_id=eval_dir.name,
                    ),
                    task_id,
                    archived,
                )
            except ReportCatalogError as err:
                _log.warning(
                    "retention keeps unjudged report %s/%s/%s: %s",
                    slug,
                    task_id,
                    eval_dir.name,
                    err,
                )
                publishable = True
            if publishable:
                owed.add(eval_dir.name)
        return owed

    def reconcile(self) -> list[ReportCatalogEntry]:
        """Publish every complete, archived, unpublished report; return what was added.

        The completeness half of the design, run at startup and available as a
        manual repair. It walks the evaluations tree, keeps the dirs holding a
        full L1+L2 artifact set, and appends the ones no entry covers yet — which
        picks up reports written before this feature existed, reports whose live
        hook was unwired or failed, and reports for tasks archived long ago.

        It is also how a report *becomes* publishable: a candidate whose task was
        still active when it was evaluated is skipped (no sequence consumed) and
        re-examined on the next sweep, so the report enters the feed at the
        sequence where close-out made it a finished protocol story rather than
        never at all. That re-examination is the reason candidates are re-scanned
        every sweep instead of once.

        **Deterministic order:** candidates are appended sorted by
        ``(evaluation order, repo slug, task id)``, so a backfilled global
        sequence follows generation order across repos rather than
        directory-listing order, and two hubs backfilling the same tree produce
        the same sequence. The evaluation-order rule is imported from the
        evaluation module, so ordering cannot drift from how the per-task listing
        sorts.

        **Idempotent:** membership is tested by :func:`report_key`, which is
        derived from the source identity, so a second sweep (or a restart
        mid-sweep) adds nothing and never mints a second identity for a report.
        One broken evaluation is logged and skipped rather than failing the sweep
        — *any* exception it raises, not just the anticipated ones, because a
        startup backfill that aborts on the first structurally corrupt artifact
        would strand every later report behind it. The remaining reports still
        publish, and the broken one is retried next time. Gated.
        """
        self._require_enabled()
        with self._locked():
            existing = self._read_entries()
            known = {entry.report_key for entry in existing}
            seq = _next_seq(existing)
            added: list[ReportCatalogEntry] = []
            archived: dict[tuple[Path, str], bool] = {}
            for slug, task_id, evaluation_id, eval_dir in self._scan_candidates():
                key = report_key(
                    repo_slug=slug, task_id=task_id, evaluation_id=evaluation_id
                )
                if key in known:
                    continue
                try:
                    entry = self._publish(
                        seq=seq,
                        slug=slug,
                        task_id=task_id,
                        evaluation_id=evaluation_id,
                        eval_dir=eval_dir,
                        archived_cache=archived,
                    )
                except Exception as err:  # noqa: BLE001 - one dir cannot stop the sweep
                    # No line was written, so ``seq`` is not consumed; the next
                    # reconciliation retries once the artifact is readable.
                    _log.warning(
                        "report catalog skipped %s/%s/%s: %s",
                        slug,
                        task_id,
                        evaluation_id,
                        err,
                    )
                    continue
                if entry is None:
                    continue  # not archived yet: a later sweep publishes it
                known.add(key)
                added.append(entry)
                seq += 1
            return added

    # -- internals ------------------------------------------------------------

    def _publish(
        self,
        *,
        seq: int,
        slug: str,
        task_id: str,
        evaluation_id: str,
        eval_dir: Path,
        archived_cache: dict[tuple[Path, str], bool] | None = None,
    ) -> ReportCatalogEntry | None:
        """Publish one candidate if the publication rule admits it. Lock held.

        Returns ``None`` without writing anything when the candidate is decidedly
        not publishable yet (its task is not archived). A candidate that cannot be
        judged at all — unreadable artifacts, an identity contradiction, a failed
        archive probe — raises :class:`ReportCatalogError` instead, so the caller
        retries it rather than recording a decision it did not reach. The entry is
        otherwise built *completely first* and only then appended, so nothing
        touches the catalog until it is certain to be a whole record; there is no
        partial-entry state to clean up.
        """
        candidate = _read_candidate(
            eval_dir, slug=slug, task_id=task_id, evaluation_id=evaluation_id
        )
        if not self._publishable(candidate, task_id, archived_cache):
            return None
        entry = self._build_entry(
            candidate,
            seq=seq,
            slug=slug,
            task_id=task_id,
            evaluation_id=evaluation_id,
        )
        self._append_line(entry)
        return entry

    def _publishable(
        self,
        candidate: _Candidate,
        task_id: str,
        cache: dict[tuple[Path, str], bool] | None = None,
    ) -> bool:
        """Whether the feed should publish this candidate now — the publication rule.

        The single predicate behind both a sweep and the retention guard
        (:meth:`unpublished_ids`), so the two can never disagree about the same
        directory — a disagreement in that direction deletes a report the other
        side would have published.

        Both of its inputs come from the candidate's own artifacts: the semantic
        report's evaluation-time ``target_source`` stamp (a report written after
        close-out is publishable with no live read at all), else a probe of the
        repo path the *evidence pack* recorded. Deliberately not the registry's
        current ``ManagedRepo.path``, which is a different fact that drifts from
        the recorded one whenever a repo is re-registered elsewhere.

        ``False`` is always a decision ("not archived yet"); an undecidable
        candidate raises out of here or out of :func:`_read_candidate`.
        """
        if candidate.target_source == SOURCE_ARCHIVED:
            return True
        return self._task_archived(candidate.repo_path, task_id, cache)

    def _build_entry(
        self,
        candidate: _Candidate,
        *,
        seq: int,
        slug: str,
        task_id: str,
        evaluation_id: str,
    ) -> ReportCatalogEntry:
        """Assemble one catalog entry from an already-judged candidate.

        Pure assembly: identity coherence is settled by :func:`_read_candidate`
        and publishability by :meth:`_publishable`, so everything here is
        transcription. Content comes only from the evaluation's own durable files
        — the semantic report for identity/evaluator/target facts, the evidence
        pack for provenance, and all four artifacts for size/hash — never the
        registry, run records, or live task content, so an entry stays
        reproducible from the artifacts it names.
        """
        report = candidate.report
        evaluator = _mapping(report.get("evaluator")) or {}
        return ReportCatalogEntry(
            seq=seq,
            report_key=report_key(
                repo_slug=slug, task_id=task_id, evaluation_id=evaluation_id
            ),
            cataloged_ts=self._clock().isoformat(),
            # Display name from the report (what the evaluation recorded), slug
            # from the tree (what locates the artifacts). They differ whenever a
            # repo name needed slugging, and both matter: the consumer shows one
            # and re-resolves by the other.
            repo_name=_str_field(report, "repo_name") or slug,
            repo_slug=slug,
            repo_path=candidate.repo_path,
            task_id=task_id,
            evaluation_id=evaluation_id,
            generated_ts=_str_field(report, "generated_ts"),
            # The evaluation-time stamp, kept beside the publication-time fact
            # rather than collapsed into it: the two differ exactly for a report
            # of then-live work that a later sweep published after close-out.
            target_source=candidate.target_source,
            # True by construction — publication *is* the eligibility decision, so
            # an entry exists only for an archived task. It stays an explicit
            # field because it is what a consumer reads off the feed item.
            archived=True,
            backend=_str_field(evaluator, "backend"),
            model=_str_field(evaluator, "model"),
            schema_version=_str_field(evaluator, "schema_version"),
            artifacts=tuple(
                _artifact_digest(candidate.eval_dir / name)
                for name in REQUIRED_ARTIFACTS
            ),
            provenance=_provenance(candidate.evidence),
        )

    def _task_archived(
        self,
        repo_path: Path | None,
        task_id: str,
        cache: dict[tuple[Path, str], bool] | None = None,
    ) -> bool:
        """Whether the evaluated task file now lives in ``.ai-tasks/archive/``.

        The module's one live read, and the mechanism that lets a report of a
        then-active task publish after close-out archives it. Routed through the
        contained :func:`orch_hub.task_store.read_archived_task_content` reader, so
        the same single-segment id validation and resolved-path containment that
        guard every other task-file read apply here — a crafted task id or a
        symlinked archive file cannot read outside the repo's archive dir. Nothing
        is written, and nothing about the target repo is inferred beyond this one
        fact.

        A candidate with no recorded repo path is a decided ``False``: there is
        nothing to probe and no later sweep will find one either (the pack is
        immutable), so such a report can only ever publish on its own archived
        stamp. A probe that *fails* — an unreadable file, an unmounted volume — is
        not that: it raises, leaving the candidate undecided, because reading one
        broken read as "not archived" would let the retention guard treat a report
        as disposable that the next sweep would have published. ``cache`` memoizes
        the probe across the candidates that ask the same question — a task's
        evaluations up to the retention cap, in a sweep or in one retention pass.
        """
        if repo_path is None:
            return False
        key = (repo_path, task_id)
        if cache is not None and key in cache:
            return cache[key]
        try:
            found = read_archived_task_content(_repo_at(repo_path), task_id) is not None
        except (OSError, ValueError) as err:
            raise ReportCatalogError(
                f"archive probe failed for {repo_path} / {task_id} ({err})"
            ) from err
        if cache is not None:
            cache[key] = found
        return found

    def _append_line(self, entry: ReportCatalogEntry) -> None:
        """Append one entry as a single durable JSONL line. Lock held.

        Three deliberate choices make an append survive a crash:

        - **One line, one write, then fsync.** The entry is serialized whole
          before the file is opened, so the append is a single write of a
          complete record and a reader never sees a syntactically valid line with
          half an entry in it.
        - **Torn-tail heal.** If the file does not already end with a newline, a
          previous append was interrupted mid-line. Writing a leading newline
          isolates those bytes as their own (unparseable, therefore skipped) line
          instead of splicing them onto the front of this entry, which would
          corrupt an otherwise good record.
        - **Sequence from disk.** ``seq`` was derived from the file this cycle,
          so an interrupted append simply leaves its number free for the next
          one; no counter state is held across cycles to drift.
        """
        line = json.dumps(entry.to_dict(), sort_keys=True)
        path = self.catalog_path
        path.parent.mkdir(parents=True, exist_ok=True)
        heal = _has_torn_tail(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if heal else "") + line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _read_entries(self) -> list[ReportCatalogEntry]:
        """Load every valid catalog line, ascending by ``seq``.

        Called both inside a write transaction (to allocate the next sequence) and
        by the lock-free readers, which is why it must tolerate a torn tail rather
        than assume a quiescent file.

        A missing catalog is an empty list (nothing published yet), not an error.
        Lines that are blank, unparseable, not objects, or missing the identity
        fields an entry must have (``seq``/``report_key``) are skipped: a torn or
        hand-edited line must not make the rest of the catalog unreadable. What
        cannot be skipped is a *readable* line that breaks the identity the cursor
        is built on — see :func:`_require_unique_identity`, which is why this
        returns entries or raises, never a quietly narrowed set.

        Sorting by ``seq`` rather than trusting file order costs nothing at this
        size and makes the cursor contract hold even if a line were ever written
        out of order.
        """
        try:
            text = self.catalog_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as err:
            raise ReportCatalogError(f"unreadable {CATALOG_NAME} ({err})") from err
        found: list[ReportCatalogEntry] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except ValueError:
                _log.warning("report catalog line %d is not valid JSON", lineno)
                continue
            entry = _entry_from_dict(data)
            if entry is None:
                _log.warning("report catalog line %d is not a catalog entry", lineno)
                continue
            found.append(entry)
        found.sort(key=lambda entry: entry.seq)
        _require_unique_identity(found)
        return found

    def _scan_candidates(self) -> list[tuple[str, str, str, Path]]:
        """Every complete evaluation in the tree, in deterministic global order.

        Walks the fixed ``<repo-slug>/<task-id>/<evaluation-id>/`` layout (a
        stray file or extra nesting level is simply not a candidate) and keeps
        only dirs holding the full artifact set. The sort key is
        ``(evaluation order, repo slug, task id)``: evaluation order first so a
        backfill numbers reports in generation order across repos, then slug and
        task id to break the ties that scoped evaluation ids produce when two
        repos evaluate in the same second — which is precisely the collision the
        global sequence exists to resolve.
        """
        base = self._config.data_dir / EVALUATIONS_SUBDIR
        candidates = [
            (slug, task_id, evaluation_id, eval_dir)
            for slug, task_id, evaluation_id, eval_dir in _walk_evaluations(base)
            if _is_complete(eval_dir)
        ]
        candidates.sort(
            key=lambda item: (evaluation_order_key(item[2]), item[0], item[1])
        )
        return candidates


# -- module helpers -----------------------------------------------------------


def _walk_evaluations(base: Path) -> Iterator[tuple[str, str, str, Path]]:
    """Yield ``(repo_slug, task_id, evaluation_id, dir)`` for the evaluations tree.

    A plain three-level walk, sorted at each level so the raw yield order is
    itself stable (the caller re-sorts into global order). An absent tree yields
    nothing — a hub that has never evaluated anything is not an error state.
    """
    if not base.is_dir():
        return
    for repo_dir in sorted(base.iterdir()):
        if not repo_dir.is_dir():
            continue
        for task_dir in sorted(repo_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            for eval_dir in sorted(task_dir.iterdir()):
                if eval_dir.is_dir():
                    yield repo_dir.name, task_dir.name, eval_dir.name, eval_dir


def _missing_artifacts(eval_dir: Path) -> list[str]:
    """Which of the required L1+L2 artifacts an evaluation dir does not hold."""
    return [name for name in REQUIRED_ARTIFACTS if not (eval_dir / name).is_file()]


def _is_complete(eval_dir: Path) -> bool:
    """Whether an evaluation dir holds the full L1+L2 report artifact set.

    The candidate predicate, and the reason a failed semantic job or a
    ``/metrics``-only generation never appears as a ready report: the semantic
    evaluator writes all four files as one all-or-nothing dir, so their joint
    presence is exactly the "durably complete" condition, checked on the
    filesystem rather than inferred from job state. Completeness alone does not
    publish — the candidate must also be coherent and archived.

    Phrased over :func:`_missing_artifacts` so the read side can report *which*
    artifacts are gone without restating the rule; publication and the bundle
    export must never be able to disagree about what a complete report is.
    """
    return not _missing_artifacts(eval_dir)


def _require_unique_identity(entries: list[ReportCatalogEntry]) -> None:
    """Raise unless every entry holds its own ``seq`` and its own ``report_key``.

    Both uniqueness rules are what exactly-once traversal is *made of*, and the
    write path is built to guarantee them: :meth:`ReportCatalog._locked` serializes
    allocation across threads and processes, and :func:`report_key` derives
    identity from the source so re-publishing an evaluation is a no-op rather than
    a second entry. A file that nevertheless holds a repeat did not get there
    through that path — a hand-edit, two catalogs merged, a backup restored over a
    live one, or an advisory lock silently ignored by the filesystem underneath the
    data dir.

    Whatever the cause, the cursor is then ambiguous: with two entries at ``seq``
    N, a consumer that resumes from watermark N never sees the second one, because
    the cursor is *strictly* greater — the feed would drop a report while reporting
    a clean traversal. So this fails explicitly instead of picking a winner.
    Skipping the repeat is the one outcome the cursor policy promises can never
    happen, and renumbering is not available either: the catalog is append-only
    precisely so a published ``seq`` never moves under a consumer that already
    stored it. A repeated ``report_key`` is the same audit from the other side —
    one source evaluation with two global identities, which would re-deliver a
    report a consumer already imported under a key it thought was unique.

    Failing here fails *every* path through
    :meth:`ReportCatalog._read_entries`, which is the intended blast radius: the
    feed refuses to serve a traversal it cannot guarantee, publication pauses
    rather than appending onto an ambiguous file, and the retention guard's
    fail-closed wiring (:meth:`ReportCatalog.unpublished_ids`, consulted by
    :meth:`orch_hub.evaluation.EvaluationService.prune_evaluations`) keeps every
    artifact meanwhile. Nothing is deleted or lost while the catalog is broken, and
    an operator who repairs the file resumes exactly where publication paused.
    """
    seen_seq: set[int] = set()
    seen_key: set[str] = set()
    for entry in entries:
        if entry.seq in seen_seq:
            raise ReportCatalogError(
                f"{CATALOG_NAME} holds more than one entry at seq {entry.seq}:"
                " the feed cursor is ambiguous"
            )
        if entry.report_key in seen_key:
            raise ReportCatalogError(
                f"{CATALOG_NAME} holds more than one entry for report"
                f" {entry.report_key}: global identity is not unique"
            )
        seen_seq.add(entry.seq)
        seen_key.add(entry.report_key)


def _identity_fault(entry: ReportCatalogEntry) -> str | None:
    """Why a stored entry cannot be trusted to address its artifacts, else ``None``.

    A catalog line is data, not a promise: the file is append-only and never
    rewritten, so a hand-edit or a merge can leave a line whose *stored* identity
    is not the one that produced its ``report_key``. That matters because
    :meth:`ReportCatalog.artifact_dir` resolves the artifacts from the stored
    slug/task/evaluation triple through :func:`orch_hub.evaluation.repo_slug`,
    which is idempotent but also *lossy*: ``alpha!!`` and ``alpha`` slug to the
    same directory. A line reading ``alpha!!`` would therefore serve the real
    ``alpha`` repo's report — present, complete, hash-verified — under a feed
    identity that contradicts it.

    Two checks, one rule (*the stored identity is the published identity*):

    - the slug must already be canonical, so it names the directory it was
      published from rather than an alias that re-slugs onto another one;
    - the ``report_key`` must re-derive from the stored triple, which catches every
      other edit of the source identity, since the key is a digest of exactly that
      triple (:func:`report_key`).

    Reported as unavailability rather than raised, and checked before any IO: like
    a pruned bundle this is a data state of an immutable file that no retry heals,
    so naming the reason serves an operator better than a permanently failing
    endpoint. The entry still lists in the feed — dropping it from the traversal
    would be the silent skip the cursor policy forbids.
    """
    if repo_slug(entry.repo_slug) != entry.repo_slug:
        return f"repo slug {entry.repo_slug!r} is not a canonical stored slug"
    expected = report_key(
        repo_slug=entry.repo_slug,
        task_id=entry.task_id,
        evaluation_id=entry.evaluation_id,
    )
    if expected != entry.report_key:
        return (
            "report key does not derive from the stored source identity"
            f" ({entry.repo_slug}/{entry.task_id}/{entry.evaluation_id})"
        )
    return None


def _cataloged_digests(entry: ReportCatalogEntry) -> dict[str, str]:
    """The one usable recorded digest per artifact name in an entry's manifest.

    Verification only means something against a digest recorded the one way
    publication records one: exactly one row per artifact name (``_build_entry``
    writes a single digest per :data:`REQUIRED_ARTIFACTS` name), holding something
    that could actually be a sha256. A name failing either test has no usable
    digest and is left out, so the read path reports that artifact as *unverified*
    rather than comparing against a value it picked out of a damaged manifest.

    Repetition disqualifies a name whether or not the repeated rows agree. Agreeing
    copies are not the safer case they look like: the entry is still a line the
    writer could not have produced, so nothing about its manifest is evidence of
    anything — and a reader that accepts them is really checking "no row
    contradicts", which is a strictly weaker fact than the one ``verified``
    reports. Counting also states the rule once, instead of spending a
    same-name value comparison to reach the same verdict for disagreeing rows.
    """
    counts = Counter(artifact.name for artifact in entry.artifacts)
    return {
        artifact.name: artifact.sha256
        for artifact in entry.artifacts
        if counts[artifact.name] == 1 and _SHA256_HEX.match(artifact.sha256)
    }


def _next_seq(existing: list[ReportCatalogEntry]) -> int:
    """The next global sequence: one past the highest cataloged. Starts at 1.

    Derived from durable state on every append cycle rather than kept in memory,
    so a restart resumes correctly and no in-process counter can drift from the
    file. ``max`` rather than ``len`` because skipped (torn/corrupt) lines must
    not pull the next number back onto one a consumer could already have seen.
    """
    return max((entry.seq for entry in existing), default=0) + 1


def _has_torn_tail(path: Path) -> bool:
    """Whether the catalog's last byte is a non-newline (an interrupted append).

    Checked before appending so the heal in :meth:`ReportCatalog._append_line`
    can fence off the partial bytes. A missing or empty file is not torn.
    """
    try:
        with path.open("rb") as fh:
            if fh.seek(0, os.SEEK_END) == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def _artifact_digest(path: Path) -> ReportArtifact:
    """Hash and size one artifact file for the entry's manifest.

    Read in chunks: an evidence pack is bounded but not small, and nothing here
    needs it in memory. The caller has already established the file exists
    (:func:`_is_complete`), so a read failure here is a genuine IO problem and is
    raised as :class:`ReportCatalogError` — publishing an entry with an
    unverifiable hash would defeat the point of recording one.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(_HASH_CHUNK):
                digest.update(chunk)
                size += len(chunk)
    except OSError as err:
        raise ReportCatalogError(f"unreadable artifact {path.name} ({err})") from err
    return ReportArtifact(name=path.name, size=size, sha256=digest.hexdigest())


def _bundle_json(name: str, data: bytes) -> object:
    """Parse one already-read bundle artifact, naming it if it will not parse.

    Separate from :func:`_read_json` because a bundle read has the bytes in hand
    already (they were hashed against the entry's digest), and re-reading the file
    would risk hashing one version and serving another.

    The parsed value is returned in whatever JSON shape the file holds — object,
    array, or scalar. Narrowing a non-object body to ``None`` would report "no
    content" for bytes the entry's own manifest hash says are exactly the published
    ones, and would disagree with the per-task detail route, which hands back
    whatever the artifact parses to. (A pack that is not an object is publishable:
    the entry build degrades its provenance rather than rejecting it.) So inside an
    available bundle ``null`` means the content *is* JSON ``null``, never that the
    artifact is absent — availability is all-or-nothing, so all four files are
    present. Unparseable content still raises: that is a bundle the export must
    fail closed on.
    """
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise ReportCatalogError(f"unparseable report artifact {name} ({err})") from err


def _bundle_text(name: str, data: bytes) -> str:
    """Decode one already-read text bundle artifact (the Markdown render)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ReportCatalogError(f"undecodable report artifact {name} ({err})") from err


def _read_json(path: Path) -> object:
    """Parse one artifact JSON, raising :class:`ReportCatalogError` on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ReportCatalogError(f"unreadable {path.name} ({err})") from err


def _read_json_object(path: Path) -> dict | None:
    """Parse one artifact JSON as an object, or ``None`` when it is neither.

    The tolerant counterpart of :func:`_read_json`, for the static report — an
    artifact a report can publish *without*, so a missing or corrupt one degrades
    the entry's identity checks rather than suppressing an otherwise-intact
    report. Absence is degradation; a *readable* artifact that contradicts the
    directory is a different matter (:func:`_require_identity`). The evidence pack
    has its own reader (:func:`_read_evidence`), which separates a file that will
    not heal from one that merely cannot be read right now.
    """
    try:
        data = _read_json(path)
    except ReportCatalogError:
        return None
    return data if isinstance(data, dict) else None


def _read_evidence(eval_dir: Path) -> dict | None:
    """The evaluation's parsed evidence pack, or ``None`` when it is unusable.

    Read once per candidate and shared by every consumer (identity, repo path,
    provenance): a pack carries bounded log excerpts for every related run, so
    parsing it more than once per candidate is real work on a startup sweep.

    ``None`` means *degrade*: a pack that is corrupt, or gone since the
    completeness check, costs the entry its provenance and its archive probe but
    not its publication — those are settled facts about a file that will not heal.
    An IO failure is a different thing entirely: the pack may be intact and merely
    unreadable right now, so it raises and leaves the candidate undecided rather
    than quietly becoming "names no repo path, therefore not archived" — which the
    retention guard would read as permission to delete it.
    """
    path = eval_dir / EVIDENCE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    except OSError as err:
        raise ReportCatalogError(f"unreadable {path.name} ({err})") from err
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class _Candidate:
    """One evaluation dir's publication facts, read once from its own artifacts.

    The unit both callers of the publication rule work from — the entry build and
    the retention guard — so "would the feed publish this dir?" has one answer no
    matter who asks, derived from one set of reads. Carrying the parsed artifacts
    also keeps an entry build to a single read per file.
    """

    eval_dir: Path
    report: dict
    evidence: dict | None
    target_source: str | None
    repo_path: Path | None


def _read_candidate(
    eval_dir: Path, *, slug: str, task_id: str, evaluation_id: str
) -> _Candidate:
    """Read and identity-check one complete evaluation dir.

    Directory names alone decide the global ``report_key``, so a dir whose
    artifacts claim a different repo/task/evaluation — a copied or hand-moved
    bundle, a stale file left by an interrupted write — would otherwise be
    published under a key that misidentifies its own contents. Every JSON artifact
    is therefore checked against the directory it was found under
    (:func:`_require_identity`).

    Raises :class:`ReportCatalogError` for anything that leaves the candidate
    *undecided*: an unreadable semantic report or evidence pack, or an identity
    contradiction. Callers must not read that as "not publishable" — a sweep
    retries it, and retention keeps it — because the two are only the same answer
    when nothing is at stake, and here one of them is unrecoverable.
    """
    report = _read_json(eval_dir / SEMANTIC_REPORT_NAME)
    if not isinstance(report, dict):
        raise ReportCatalogError(
            f"{SEMANTIC_REPORT_NAME} is not a JSON object in {eval_dir}"
        )
    identity = dict(slug=slug, task_id=task_id, evaluation_id=evaluation_id)
    _require_identity(report, name=SEMANTIC_REPORT_NAME, **identity)
    _require_identity(
        _read_json_object(eval_dir / STATIC_METRICS_NAME),
        name=STATIC_METRICS_NAME,
        **identity,
    )
    evidence = _read_evidence(eval_dir)
    _require_identity(evidence, name=EVIDENCE_NAME, **identity)
    _require_identity(
        _mapping(evidence.get("target")) if evidence is not None else None,
        name=f"{EVIDENCE_NAME} target",
        **identity,
    )
    return _Candidate(
        eval_dir=eval_dir,
        report=report,
        evidence=evidence,
        target_source=_str_field(report, "target_source"),
        repo_path=_evidence_repo_path(evidence),
    )


def _mapping(value: object) -> dict | None:
    """``value`` when it is a JSON object, else ``None``."""
    return value if isinstance(value, dict) else None


def _require_identity(
    data: dict | None,
    *,
    name: str,
    slug: str,
    task_id: str,
    evaluation_id: str,
) -> None:
    """Reject an artifact whose own identity contradicts its directory.

    The global ``report_key`` is derived from directory names, which are the
    storage identity — but the *contents* are what a consumer imports, so the two
    must agree or the feed publishes a report under a key that misidentifies it.
    A bundle copied between task dirs, restored from a backup into the wrong
    place, or left behind by an interrupted rename parses perfectly and would
    otherwise pass unnoticed.

    Only contradictions are fatal. A field an artifact does not carry (an older
    schema, or a degraded pack that did not parse at all — ``data`` is ``None``)
    is silence, not disagreement, and leaves publication alone; that keeps this
    check from becoming a second, stricter completeness rule. ``repo_name`` is
    compared through :func:`orch_hub.evaluation.repo_slug`, the same rule that
    produced the directory, so a repo whose name needed slugging still matches.
    """
    if data is None:
        return
    claimed_task = _str_field(data, "task_id")
    claimed_eval = _str_field(data, "evaluation_id")
    claimed_repo = _str_field(data, "repo_name")
    mismatch: str | None = None
    if claimed_eval is not None and claimed_eval != evaluation_id:
        mismatch = f"evaluation_id {claimed_eval!r} != {evaluation_id!r}"
    elif claimed_task is not None and claimed_task != task_id:
        mismatch = f"task_id {claimed_task!r} != {task_id!r}"
    elif claimed_repo is not None and repo_slug(claimed_repo) != slug:
        mismatch = f"repo {claimed_repo!r} != {slug!r}"
    if mismatch is not None:
        raise ReportCatalogError(f"{name} identity mismatch: {mismatch}")


def _repo_at(repo_path: Path) -> ManagedRepo:
    """A minimal :class:`ManagedRepo` for a path, for contained task-file reads.

    Mirrors :meth:`orch_hub.models.RunRecord.managed_repo`: the archive probe needs
    only a repo root, and reconstructing one locally keeps the catalog off the
    registry (which could disagree with, or no longer list, the repo an old report
    came from).
    """
    return ManagedRepo(
        name=repo_path.name,
        path=repo_path,
        manifest=repo_path / ".ai-deploy-manifest.json",
    )


def _evidence_repo_path(evidence: dict | None) -> Path | None:
    """The evaluated repo's absolute path, read from the evidence pack's target.

    Best-effort and deliberately non-fatal: the repo path is consumer-facing
    context for cohorting, not part of the entry's identity (which is the
    slug/task/evaluation triple), so a degraded evidence pack yields ``None``
    rather than blocking publication of a perfectly good report. It is also the
    only source for the archive probe's repo root, which is why a pack without one
    leaves a then-active report unpublishable.
    """
    target = _mapping(evidence.get("target")) if evidence is not None else None
    if target is None:
        return None
    raw = _str_field(target, "repo_path")
    return Path(raw) if raw else None


def _provenance(evidence: dict | None) -> ReportProvenance:
    """Derive cohorting provenance from the evaluation's own evidence pack.

    Only durable artifact content is consulted, so the record is reproducible
    from what the entry names. What the pack cannot answer is stated as
    explicitly unavailable *with a reason* — ``protocol`` and
    ``orchestrator_config`` always, and ``runs``/``git`` when the pack is missing
    or degraded — so a consumer never mistakes an absent field for a known-empty
    one. An unreadable pack is not fatal: the report itself is still publishable,
    just less cohortable.

    Every nested lookup is type-checked before use rather than trusted to match
    the schema. The pack is a file on disk that anything could have truncated or
    rewritten, and a raised ``AttributeError`` here is not a local failure: it
    would propagate out of the entry build and, in a sweep, cost every *later*
    candidate its publication too.
    """
    unavailable = ReportProvenance(
        runs_detail="evidence pack unreadable",
        git_detail="evidence pack unreadable",
        protocol_detail=PROTOCOL_UNAVAILABLE_DETAIL,
        config_detail=CONFIG_UNAVAILABLE_DETAIL,
    )
    if evidence is None:
        return unavailable
    runs = evidence.get("runs")
    runs_available = isinstance(runs, list)
    run_ids: tuple[str, ...] = ()
    if runs_available:
        run_ids = tuple(
            run_id
            for run_id in (_run_id_of(item) for item in runs)
            if run_id is not None
        )
    git = _mapping(evidence.get("git")) or {}
    commits = git.get("commits")
    commits = commits if isinstance(commits, list) else []
    # ``git log`` is reverse-chronological, so the window's first commit is its
    # newest — roughly the repo state the task's runs ended at. A malformed item
    # there degrades the field; it does not fail the report.
    newest = _mapping(commits[0]) if commits else None
    return ReportProvenance(
        runs_available=runs_available,
        run_ids=run_ids,
        runs_detail=None if runs_available else "evidence pack carries no run list",
        git_available=bool(git.get("available")),
        git_since=_str_field(git, "since"),
        git_until=_str_field(git, "until"),
        git_commit_count=len(commits),
        git_newest_sha=_str_field(newest, "hash") if newest is not None else None,
        git_detail=_str_field(git, "detail"),
        protocol_available=False,
        protocol_detail=PROTOCOL_UNAVAILABLE_DETAIL,
        config_available=False,
        config_detail=CONFIG_UNAVAILABLE_DETAIL,
    )


def _run_id_of(item: object) -> str | None:
    """The ``run_id`` of one serialized ``RunEvidence`` entry, or ``None``."""
    if not isinstance(item, dict):
        return None
    run = item.get("run")
    return _str_field(run, "run_id") if isinstance(run, dict) else None


def _entry_from_dict(data: object) -> ReportCatalogEntry | None:
    """Round-trip one catalog line back into an entry, or ``None`` if it is not one.

    The exact inverse of :meth:`ReportCatalogEntry.to_dict`. An entry is only
    recognized when it carries the two identity fields that make it addressable —
    an integer ``seq`` (the cursor) and a ``report_key`` — so a stray JSON object
    in the file is skipped rather than surfacing as a phantom report. Everything
    else degrades to a null/empty default: a line written by an older or newer
    hub version stays readable, which matters because the catalog is append-only
    and never migrated.
    """
    if not isinstance(data, dict):
        return None
    seq = data.get("seq")
    key = _str_field(data, "report_key")
    if not isinstance(seq, int) or isinstance(seq, bool) or key is None:
        return None
    evaluator = data.get("evaluator")
    evaluator = evaluator if isinstance(evaluator, dict) else {}
    repo_path = _str_field(data, "repo_path")
    artifacts = data.get("artifacts")
    return ReportCatalogEntry(
        seq=seq,
        report_key=key,
        cataloged_ts=_str_field(data, "cataloged_ts"),
        repo_name=_str_field(data, "repo_name") or "",
        repo_slug=_str_field(data, "repo_slug") or "",
        repo_path=Path(repo_path) if repo_path else None,
        task_id=_str_field(data, "task_id") or "",
        evaluation_id=_str_field(data, "evaluation_id") or "",
        generated_ts=_str_field(data, "generated_ts"),
        target_source=_str_field(data, "target_source"),
        archived=bool(data.get("archived")),
        backend=_str_field(evaluator, "backend"),
        model=_str_field(evaluator, "model"),
        schema_version=_str_field(evaluator, "schema_version"),
        artifacts=tuple(
            artifact
            for artifact in (
                _artifact_from_dict(item)
                for item in (artifacts if isinstance(artifacts, list) else [])
            )
            if artifact is not None
        ),
        provenance=_provenance_from_dict(data.get("provenance")),
    )


def _artifact_from_dict(data: object) -> ReportArtifact | None:
    if not isinstance(data, dict):
        return None
    name = _str_field(data, "name")
    sha256 = _str_field(data, "sha256")
    if name is None or sha256 is None:
        return None
    size = data.get("size")
    return ReportArtifact(
        name=name,
        size=size if isinstance(size, int) and not isinstance(size, bool) else 0,
        sha256=sha256,
    )


def _provenance_from_dict(data: object) -> ReportProvenance:
    """Round-trip the nested provenance sections; anything absent stays unavailable."""
    if not isinstance(data, dict):
        return ReportProvenance()
    runs = data.get("runs")
    runs = runs if isinstance(runs, dict) else {}
    git = data.get("git")
    git = git if isinstance(git, dict) else {}
    protocol = data.get("protocol")
    protocol = protocol if isinstance(protocol, dict) else {}
    config = data.get("orchestrator_config")
    config = config if isinstance(config, dict) else {}
    raw_ids = runs.get("run_ids")
    commit_count = git.get("commit_count")
    return ReportProvenance(
        runs_available=bool(runs.get("available")),
        run_ids=tuple(
            value
            for value in (raw_ids if isinstance(raw_ids, list) else [])
            if isinstance(value, str) and value
        ),
        runs_detail=_str_field(runs, "detail"),
        git_available=bool(git.get("available")),
        git_since=_str_field(git, "since"),
        git_until=_str_field(git, "until"),
        git_commit_count=(
            commit_count
            if isinstance(commit_count, int) and not isinstance(commit_count, bool)
            else 0
        ),
        git_newest_sha=_str_field(git, "newest_sha"),
        git_detail=_str_field(git, "detail"),
        protocol_available=bool(protocol.get("available")),
        protocol_detail=_str_field(protocol, "detail"),
        config_available=bool(config.get("available")),
        config_detail=_str_field(config, "detail"),
    )


def _str_field(data: dict, key: str) -> str | None:
    """A non-empty string field, else ``None`` (matches the other record loaders)."""
    value = data.get(key)
    return value if isinstance(value, str) and value else None
