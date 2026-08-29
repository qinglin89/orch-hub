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

Refusing to guess leaves someone else holding the question, though, and until now
that someone read a ``500`` and edited the file blind. :meth:`ReportCatalog.audit`
is the diagnosis those refusals imply — every defect at once, each pinned to a line
number and to the identity it collides with, plus the artifact census that keeps
*pruned* (an entry outliving what it indexes, by design) apart from *damaged* (a
link the hub never wrote, which retention can no longer reclaim and only an
operator can clear). :meth:`ReportCatalog.repair` is the narrow companion: it drops
byte-identical repeats and an interrupted append's trailing bytes — the two edits
that provably leave every published ``seq``/``report_key`` where it was — and
refuses outright, writing nothing, when two *distinct* entries collide. It is
operator-invoked and never runs at startup: a broken catalog is already inert and
safe, so self-repairing it at boot would trade a visible halt for an invisible
rewrite of durable publication state.

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
import stat
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .evaluation import (
    EVIDENCE_NAME,
    SOURCE_ARCHIVED,
    EvaluationDamagedTreeError,
    EvaluationDisabledError,
    contained_dir_names,
    evaluation_artifact_dir,
    evaluation_order_key,
    opened_artifact,
    opened_evaluations_dir,
    repo_slug,
    tree_segments,
)
from .models import (
    CatalogAudit,
    CatalogDefect,
    CatalogRepair,
    CatalogRepairAction,
    ManagedRepo,
    ReportArtifact,
    ReportArtifactRead,
    ReportBundle,
    ReportCatalogEntry,
    ReportPage,
    ReportProtocolCoverage,
    ReportProtocolCoverageEntry,
    ReportProvenance,
)
from .protocol_provenance import effective_protocol_from_dict
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

COVERAGE_ENTRY_LIMIT = 50
"""How many identity-less reports the coverage census lists before truncating.
The counts beside them stay complete, so this bounds the response without ever
understating the corpus; a reader that wants every row walks the feed itself,
which is what the feed's cursors are for."""

CATALOG_UNREADABLE_DETAIL = (
    "the report catalog could not be read; run `orch-hub eval catalog verify`"
)
"""Why a coverage census reports no reports at all. The feed fails closed on a
catalog whose identities are ambiguous rather than serving a page that quietly
omits one, and a census must inherit that refusal instead of publishing a total
built from whatever happened to parse. It names the repair path, because the
alternative reading — "this hub has published nothing" — is the one wrong answer
that looks like a valid one."""

EVALUATION_DISABLED_DETAIL = (
    "task evaluation is disabled on this hub, so no reports are published"
)
"""Why the report half of the census is unavailable on a hub with evaluation off.
Distinct from an empty catalog on purpose: nothing was published *because nothing
publishes here*, which is a configuration fact rather than a coverage one."""

PACK_UNREADABLE_DETAIL = "evidence pack unreadable"
"""Why every derived provenance section is unavailable when the pack itself could
not be read. One reason for all of them: the sections differ in what they *would*
have said, but not in why nothing can be said."""

CONFIG_UNAVAILABLE_DETAIL = (
    "evidence packs carry no orchestrator effective-config snapshot"
)
"""Why orchestrator-config provenance is explicitly unavailable. The snapshot
exists per run (``effective_config.json`` in the run dir) but is not part of the
bounded evidence pack, and the catalog reads only durable evaluation artifacts —
never live run state — so an entry stays reproducible from what it names."""

ARTIFACT_UNKNOWN = "unknown_artifact"
ARTIFACT_IDENTITY = "identity_fault"
ARTIFACT_NOT_RETAINED = "not_retained"
ARTIFACT_UNREADABLE = "unreadable"
"""Why :meth:`ReportCatalog.read_artifact_bytes` served no bytes.

Stable machine codes rather than prose, because a raw-byte response has no
envelope to carry ``available: false`` — the reason has to become a status code
at the HTTP boundary, and a caller that switches on a sentence is one reworded
message away from mapping the wrong one. The four are genuinely different
answers: a name outside the published set (never a file), a stored identity that
cannot be trusted to address the artifacts, a report whose artifact retention
removed (the designed end state of an entry outliving what it indexes), and a
file that is there but does not read — either because IO failed or because a
symbolic link stands where the published artifact or one of its parent
directories should, which is refused rather than followed. Both are hub-side
defects, deliberately not folded into ``not_retained``: reporting a damaged
artifact tree as the designed end state of retention would hide it."""

INTEGRITY_VERIFIED = "verified"
INTEGRITY_MISMATCHED = "mismatched"
INTEGRITY_UNVERIFIED = "unverified"
"""Per-artifact digest states, the same vocabulary :class:`ReportBundle` reports
over a whole set. ``unverified`` is deliberately not folded into ``verified``:
an artifact the entry holds no usable digest for was never compared to anything,
which is a weaker claim than a match."""

DEFECT_UNREADABLE_CATALOG = "unreadable_catalog"
DEFECT_UNPARSEABLE_LINE = "unparseable_line"
DEFECT_NOT_AN_ENTRY = "not_an_entry"
DEFECT_DUPLICATE_SEQ = "duplicate_seq"
DEFECT_DUPLICATE_KEY = "duplicate_report_key"
DEFECT_UNADDRESSABLE = "unaddressable_identity"
DEFECT_INCOMPLETE_ARTIFACTS = "incomplete_artifacts"
DEFECT_DAMAGED_ARTIFACTS = "damaged_artifacts"
DEFECT_UNREADABLE_ARTIFACTS = "unreadable_artifacts"
"""What :meth:`ReportCatalog.audit` can find wrong, as stable machine codes.

Two of them stop the feed and the rest do not, which is the distinction the verify
verb's exit code is built on: a repeated ``seq``/``report_key`` (or a catalog that
will not read at all) makes every read path fail closed
(:func:`_require_unique_identity`), while a skipped line, a partially pruned or
damaged artifact set, and a stored identity that cannot address its own artifacts
each answer for themselves at fetch time and leave the traversal whole.

``DEFECT_DAMAGED_ARTIFACTS`` is deliberately not folded into the *pruned* census
that has no defect at all. Retention removing artifacts is this feed's designed
end state; a link standing at an artifact or at a tree hop is damage the hub never
writes, cannot reclaim — a prune walks the tree's own hops no-follow, so it refuses
the linked hop and reports the id as retained — and only an operator can clear.
Reporting it as retention doing its job would hide the one state that accumulates
until someone acts on it."""

ARTIFACTS_RETAINED = "retained"
ARTIFACTS_PRUNED = "pruned"
ARTIFACTS_INCOMPLETE = "incomplete"
ARTIFACTS_DAMAGED = "damaged"
ARTIFACTS_UNREADABLE = "unreadable"
ARTIFACTS_UNADDRESSABLE = "unaddressable"
"""The per-entry artifact states :meth:`ReportCatalog.audit` censuses.

The same partition the bundle read reports one entry at a time
(:meth:`ReportCatalog.read_bundle`), counted across the whole catalog — derived
from the same two sources, so the census and a fetch can never disagree about one
entry: the per-name census :func:`_artifact_faults` for the artifacts themselves,
and :class:`~orch_hub.evaluation.EvaluationDamagedTreeError` out of the contained
walk that produced the descriptor it reads from."""

REPAIR_DROP_DUPLICATE_LINE = "drop_duplicate_line"
REPAIR_DROP_TORN_TAIL = "drop_torn_tail"
"""The only two things :meth:`ReportCatalog.repair` will do to the file.

Both are provably identity-preserving, which is the whole admission rule: a
byte-identical repeat leaves an identical surviving line carrying the same
published ``seq``/``report_key``, and a trailing partial line is bytes no reader
has ever parsed into an entry. Everything else — renumbering, dropping a distinct
entry, rewriting a field — moves a published identity a consumer may already have
stored, and is refused rather than automated."""

CATALOG_BACKUP_SUFFIX = ".bak"
"""Extension of the timestamped pre-repair copy written beside the catalog. The
repair is the one path that rewrites an append-only file, so the bytes it replaces
are kept: an operator who disagrees with the result has the original, and the
backup is inert to every read path (only :data:`CATALOG_NAME` is ever read)."""

_BACKUP_NAME_ATTEMPTS = 100
"""How many free backup names one repair looks for before refusing to write.

The stamp in a backup's name is second-resolution, so repeated repairs inside one
second are the ordinary collision (:func:`_write_new_file` suffixes past them). A
bound is here because the loop's other exit is a directory something else is
filling as fast as this walks it, and a repair that cannot preserve the bytes it
is about to replace must stop rather than spin — or, worse, proceed unbacked."""


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

    def _opened_dir(self, entry: ReportCatalogEntry):
        """The contained descriptor both read paths work from (a context manager).

        The read counterpart of :meth:`artifact_dir`, and the reason that method is
        path math only: a directory *name* is re-walked by every operation that
        uses it, so a link swapped in above the artifacts would redirect a read
        that a pathname check had just approved. This walks the tree's hops
        no-follow once and hands back the descriptor everything else — the
        completeness census, each artifact open, each hash — is taken relative to
        (:func:`~orch_hub.evaluation.opened_evaluations_dir`, which also states why
        links *above* the tree stay followable).
        """
        return opened_evaluations_dir(
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

    def protocol_coverage(self) -> ReportProtocolCoverage:
        """How much published work carries a protocol identity, and why the rest does not.

        The published half of the coverage census. ``provenance.protocol`` is the
        block an external importer cohorts by, so this is where "how much of my
        corpus can be cohorted at all" is answered; per-run coverage
        (:func:`orch_hub.runs.protocol_coverage`) explains the number but cannot
        stand in for it — a task publishes an identity only when *every*
        contributing run verified the same one.

        Counted over **tasks** first, and over report versions beside them. A
        catalog entry is one *publication* of a task's evaluation, and reruns mint
        fresh evaluation ids by design (history is preserved by minting, not by
        overwriting), so entries are the wrong denominator for "how much work is
        covered": one much-rerun task would weigh several times, and a task whose
        older report carried an identity its newest does not would be counted on
        both sides at once. So each ``(repo_slug, task_id)`` is answered by the
        one report that speaks for it — its newest, by
        :func:`~orch_hub.evaluation.evaluation_order_key` (the rule the per-task
        evaluation view already lists by) with publication ``seq`` as the
        tie-break, since a backfilled reconciliation can publish an older
        evaluation after a newer one. Both figures are returned because the two
        surfaces must reconcile: the feed pages entries, and a reader comparing
        them should not have to guess which population a single ``total`` meant
        (:class:`~orch_hub.models.ReportProtocolCoverage`).

        Read straight off the durable catalog lines, never from live run or repo
        state: an entry records what was true when it was published, and a report
        whose repo has since been redeployed still carries the identity its runs
        actually used. The reasons are the hub's own persisted sentences, listed
        per task rather than bucketed, because the catalog publishes availability
        plus prose and inventing classes by parsing that prose back would be a
        second, unowned vocabulary.

        Degrades explicitly instead of raising, in both directions this read can
        fail: a disabled subsystem and a catalog failing closed on ambiguous
        identities each return ``available=False`` with the reason. It is a census
        *over* the feed, not a feed read — a caller asking how much is covered gets
        an honest "cannot say" rather than a 4xx that also takes the run half of
        the page down with it. Lock-free and read-only like :meth:`entries`.
        """
        if not self.enabled:
            return ReportProtocolCoverage(
                available=False, detail=EVALUATION_DISABLED_DETAIL
            )
        try:
            found = self.entries()
        except ReportCatalogError as err:
            _log.warning("protocol coverage census: catalog unreadable (%s)", err)
            return ReportProtocolCoverage(
                available=False, detail=CATALOG_UNREADABLE_DETAIL
            )
        deciding: dict[tuple[str, str], ReportCatalogEntry] = {}
        for entry in found:
            key = (entry.repo_slug, entry.task_id)
            standing = deciding.get(key)
            if standing is None or _report_version_key(entry) > _report_version_key(
                standing
            ):
                deciding[key] = entry
        missing = [
            ReportProtocolCoverageEntry(
                seq=entry.seq,
                report_key=entry.report_key,
                repo_name=entry.repo_name,
                task_id=entry.task_id,
                evaluation_id=entry.evaluation_id,
                detail=entry.provenance.protocol_detail,
            )
            # By publication order, not by the grouping's: the rows are capped, and
            # which ones survive the cap has to be a stated rule rather than the
            # order tasks happened to first appear in the catalog.
            for entry in sorted(deciding.values(), key=lambda e: e.seq)
            if not entry.provenance.protocol_available
        ]
        return ReportProtocolCoverage(
            available=True,
            tasks=len(deciding),
            tasks_with_identity=len(deciding) - len(missing),
            reports=len(found),
            reports_with_identity=sum(
                1 for entry in found if entry.provenance.protocol_available
            ),
            # Newest first: the reasons worth acting on are the ones still being
            # produced, and a corpus's oldest reports are the ones whose runs
            # predate the capture entirely. The feed itself stays ascending —
            # that ordering is a cursor contract, and this is not a cursor.
            entries=tuple(reversed(missing[-COVERAGE_ENTRY_LIMIT:])),
            truncated=len(missing) > COVERAGE_ENTRY_LIMIT,
        )

    def read_bundle(self, entry: ReportCatalogEntry) -> ReportBundle:
        """Read one published report's artifacts as a contained import bundle.

        The by-key fetch behind the export route, so a consumer works from the
        global key alone and never constructs a repo/task/evaluation path. Reads
        are contained by :meth:`artifact_dir` (the same unsafe-segment rejection as
        every other artifact read) and confined to the evaluation's own four files;
        nothing about the target repo is touched. Containment of each *file* is
        the raw read's, for the same reason and by the same helper: a fixed name
        does not fix the object standing under it, so every artifact is opened
        no-follow and parsed from that descriptor (:func:`_read_artifact_file`).
        An artifact replaced after publication by a link out of the tree therefore
        reaches no consumer — not as text in ``report_md``, not as outside JSON in
        the three parsed slots — whether the link was planted before this call or
        lands inside it.

        Availability is **all-or-nothing**, matching what publication required: a
        bundle is served only when the complete L1+L2 set is still present and
        readable. Every other case is ``available: false`` with a ``detail`` — a
        pruned artifact dir (the designed end state of an entry outliving its
        artifacts), a partially removed one, an artifact that is *damaged* rather
        than gone (a link standing where the published file was), an unreadable or
        unparseable file, a stored identity that cannot address a contained path at
        all, or one that can but is not provably the identity the report was
        published under (:func:`_identity_fault`). Failing closed matters more here
        than degrading gracefully: a half-populated or misattributed bundle would
        be imported as if it were the published report. The damaged case is kept
        distinct from the pruned ones on purpose: retention removing artifacts is
        this feed's designed end state, and reporting a tampered tree as that end
        state would hide it from the operator who can repair it.

        Integrity is reported, not enforced: served artifacts whose bytes no longer
        match the digests recorded at publication are named in ``mismatched``, and
        ones the entry holds no usable digest for in ``unverified``. Neither
        suppresses content, because the recorded digests exist precisely so a
        consumer can tell an edited or uncheckable report from an intact one and
        decide for itself — hiding the bytes would hide the evidence. Gated and
        read-only.
        """
        self._require_enabled()
        # Both audits decide before any IO. A hand-edited or foreign catalog line
        # that cannot address the tree is read no further at all; a line that *can*
        # name a contained dir but is not the identity that dir was published under
        # would otherwise serve another report's artifacts, hash-verified, under
        # this entry's key.
        unsafe = _unaddressable(entry)
        if unsafe is not None:
            return ReportBundle(
                entry=entry,
                available=False,
                detail=f"unsafe stored report identity ({unsafe})",
            )
        fault = _identity_fault(entry)
        if fault is not None:
            return ReportBundle(
                entry=entry,
                available=False,
                detail=f"incoherent stored report identity ({fault})",
            )
        gone = ReportBundle(
            entry=entry,
            available=False,
            detail="report artifacts are no longer retained",
        )
        try:
            with self._opened_dir(entry) as dir_fd:
                if dir_fd is None:
                    return gone
                return self._read_opened_bundle(entry, dir_fd, gone)
        except EvaluationDamagedTreeError as err:
            # A link (or a loop, which is never followed far enough to become one)
            # standing at a directory hop of the tree: damage, not the pruning that
            # an entry outliving its artifacts is made of.
            return ReportBundle(
                entry=entry, available=False, detail=_damaged_hop_detail(err)
            )
        except OSError as err:
            return ReportBundle(
                entry=entry,
                available=False,
                detail=f"unreadable report artifacts ({err})",
            )

    def _read_opened_bundle(
        self, entry: ReportCatalogEntry, dir_fd: int, gone: ReportBundle
    ) -> ReportBundle:
        """The artifact half of :meth:`read_bundle`, over an opened evaluation dir.

        Split out only so the containment walk's failure modes are classified once,
        around one ``with``; every rule this applies is documented on the caller.
        """
        # Same completeness rule publication used, asked for its detail rather
        # than its verdict: a partially pruned set must read as an incomplete set,
        # not as the IO error its first absent file would otherwise produce below,
        # and a damaged one must not read as either. Advisory only — it names what
        # is wrong across the whole set before anything is read, while the read
        # below is what containment actually rests on.
        missing, uncontained = _artifact_faults(dir_fd)
        if uncontained:
            return ReportBundle(
                entry=entry, available=False, detail=_damaged_detail(uncontained)
            )
        if missing:
            return ReportBundle(
                entry=entry, available=False, detail=_incomplete_detail(missing)
            )
        cataloged = _cataloged_digests(entry)
        raw: dict[str, bytes] = {}
        mismatched: list[str] = []
        unverified: list[str] = []
        for name in REQUIRED_ARTIFACTS:
            # The check above described the names; this opens the objects. Nothing
            # a pathname check proves survives the second walk a path-based read
            # takes, so the open itself refuses a link at the artifact's own hop
            # and the bytes come from that descriptor — a swap landing inside this
            # loop changes what the name means, never what the bundle carries.
            try:
                data = _read_artifact_file(name, dir_fd=dir_fd)
            except EvaluationDamagedTreeError:
                return ReportBundle(
                    entry=entry, available=False, detail=_damaged_detail((name,))
                )
            except FileNotFoundError:
                # Pruned between the check and the open: still absence, and the
                # answer absence gets — not the IO fault it is not.
                return gone
            except OSError as err:
                return ReportBundle(
                    entry=entry,
                    available=False,
                    detail=_unreadable_artifact_detail(name, err),
                )
            if data is None:
                # Not a regular file — a directory or FIFO standing where the
                # artifact was: not the published artifact, so not retained.
                return gone
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

    def read_artifact_bytes(
        self, entry: ReportCatalogEntry, name: str
    ) -> ReportArtifactRead:
        """Read one published artifact's persisted bytes, exactly as stored.

        The verification path beside :meth:`read_bundle`, which structurally cannot
        be one: a bundle *parses* each artifact, so what reaches a consumer through
        it is a re-serialization whose bytes are this process's formatting choices —
        key order, separators, escaping, the trailing newline — not the bytes the
        entry's ``sha256`` was taken over. Recomputing a digest from that is
        checking the response encoder, not the report. Nothing here parses,
        decodes, or normalizes: the file's bytes *are* the return value, so a
        consumer can prove for itself that what the hub stored is what the hub
        published, which is the whole reason publication records a digest.

        Addressed **per artifact**, deliberately unlike the bundle's all-or-nothing
        availability. That rule exists because a half-populated *import* would be
        imported as if it were the whole report; a single file has no such failure
        mode, so a surviving artifact beside a partially pruned set still reads and
        the missing sibling is that sibling's own answer.

        Reads are confined three times. Before any IO: ``name`` must be one of the
        four published artifact names (:data:`REQUIRED_ARTIFACTS`) — the parameter
        is a selector over a closed set, never a path fragment, so nothing a caller
        writes can name a file the set does not contain — and the directory comes
        from the same contained resolution and stored-identity audit
        (:func:`_identity_fault`) that guard :meth:`read_bundle`. A traversal
        attempt or a hand-edited line therefore opens no file at all. Those two
        constrain only what the *caller* and the *catalog* can address, though; the
        third constrains the filesystem itself, and constrains it at the open
        rather than at a pathname (:func:`_read_artifact_file`): the artifact's own
        hop follows no link, and the bytes come from that descriptor. A link
        planted where the artifact was — before, during, or after any check — can
        therefore never make this path read outside the evaluations tree, least of
        all the target repo, which it never touches.

        Bytes that no longer match the recorded digest are still returned, marked
        ``mismatched``: the same rule the bundle applies, and more load-bearing
        here, because withholding an edited artifact would withhold the evidence of
        the edit from the one caller equipped to prove it. Gated and read-only —
        nothing is published, evaluated, or pruned on this path.
        """
        self._require_enabled()
        if name not in REQUIRED_ARTIFACTS:
            return ReportArtifactRead(
                entry=entry,
                name=name,
                reason=ARTIFACT_UNKNOWN,
                detail=(
                    f"{name!r} is not a published report artifact (expected one of"
                    f" {', '.join(REQUIRED_ARTIFACTS)})"
                ),
            )
        unsafe = _unaddressable(entry)
        if unsafe is not None:
            return ReportArtifactRead(
                entry=entry,
                name=name,
                reason=ARTIFACT_IDENTITY,
                detail=f"unsafe stored report identity ({unsafe})",
            )
        fault = _identity_fault(entry)
        if fault is not None:
            return ReportArtifactRead(
                entry=entry,
                name=name,
                reason=ARTIFACT_IDENTITY,
                detail=f"incoherent stored report identity ({fault})",
            )
        gone = ReportArtifactRead(
            entry=entry,
            name=name,
            reason=ARTIFACT_NOT_RETAINED,
            detail=f"report artifact {name} is no longer retained",
        )
        # A contained *directory* says nothing about what the fixed name inside it
        # opens, and a pathname settles neither: whatever a check proved about the
        # name, the read walks it a second time and may get a different object —
        # an artifact, or a directory above it, swapped for a link to a
        # target-repo file in between would be served as this entry's published
        # artifact. So the walk and the open are the containment check
        # (:meth:`_opened_dir`, :func:`_read_artifact_file`), and every step after
        # them works on descriptors.
        try:
            with self._opened_dir(entry) as dir_fd:
                if dir_fd is None:
                    # A pruned dir, or something that is not a directory standing
                    # where it was: as absent as a pruned artifact.
                    return gone
                data = _read_artifact_file(name, dir_fd=dir_fd)
        except EvaluationDamagedTreeError as err:
            # A link refused at the artifact's own hop or at a directory above it;
            # a loop is the same answer, never followed far enough to become one.
            return ReportArtifactRead(
                entry=entry,
                name=name,
                reason=ARTIFACT_UNREADABLE,
                detail=f"report artifact {name} is damaged ({err})",
            )
        except FileNotFoundError:
            # Lost to a prune before or during the open: still absence, not an IO
            # fault, so it must not be reported as one.
            return gone
        except OSError as err:
            return ReportArtifactRead(
                entry=entry,
                name=name,
                reason=ARTIFACT_UNREADABLE,
                detail=f"unreadable report artifact {name} ({err})",
            )
        if data is None:
            # A pruned dir, a partially pruned set, or something that is not a
            # regular file standing where the artifact was: all the same answer to
            # the only question asked here — these bytes are not retained.
            return gone
        expected = _cataloged_digests(entry).get(name)
        if expected is None:
            integrity = INTEGRITY_UNVERIFIED
        elif hashlib.sha256(data).hexdigest() == expected:
            integrity = INTEGRITY_VERIFIED
        else:
            integrity = INTEGRITY_MISMATCHED
        return ReportArtifactRead(
            entry=entry,
            name=name,
            data=data,
            integrity=integrity,
            recorded_sha256=expected,
        )

    def audit(self) -> CatalogAudit:
        """Diagnose the catalog: what it holds, what is wrong, and where.

        The operator surface behind ``orch-hub eval catalog verify``, and the
        answer to a question the feed routes deliberately cannot give. Their
        fail-closed audit (:func:`_require_unique_identity`) raises on the *first*
        ambiguity it meets, because a read that cannot promise exactly-once
        traversal must serve nothing; an operator repairing the file needs the
        opposite — every defect at once, each located by line number and by the
        ``seq``/``report_key`` it collides with. So this re-scans the file through
        the same parse (:func:`_scan_catalog`, shared with
        :meth:`_read_entries` so a clean verdict here and a working feed cannot
        drift apart) and enumerates what that function would have raised on.

        ``serves_feed`` is the only thing severity means: it is false exactly when
        a read path fails closed — a repeated sequence or key, or a catalog that
        will not read at all. The other findings each answer for themselves at
        fetch time and leave the traversal whole, so they are reported without
        claiming the feed is down.

        The artifact census walks each entry's dir through the same contained
        descriptor a bundle read uses, and keeps *damaged* apart from *pruned* for
        the reason the whole verb exists: pruned artifacts are this feed's designed
        end state, while a link at an artifact or at a tree hop is damage the hub
        never wrote and retention can no longer reclaim, so it accumulates silently
        until an operator clears it. Both damage shapes are kept —
        :func:`_artifact_faults` for the artifacts themselves and
        :class:`~orch_hub.evaluation.EvaluationDamagedTreeError` for the directory
        hops — rather than restated here.

        The census walks each *identity* once, not each ``report_key`` once
        (:func:`_census_identity`). The difference is exactly the damaged file this
        verb is for: two lines can share a key and still be different reports, and
        skipping the second on its key alone would drop its findings from a report
        that promises to name every defect — while it is the duplicate-key finding
        above that tells the operator the two collide.

        Strictly read-only and lock-free, like every other read: it publishes
        nothing, evaluates nothing, calls no model, and writes no byte — a verify
        run must be safe to make a habit of, including while a reconciliation
        sweep is appending. It is therefore a diagnosis *of a moment*: a concurrent
        append adds a line this scan did not see, which is the same freshness a
        feed poll has.
        """
        self._require_enabled()
        path = self.catalog_path
        try:
            present = path.is_file()
        except OSError:  # pragma: no cover - an unstattable data dir
            present = False
        try:
            raw = self._catalog_bytes()
        except ReportCatalogError as err:
            # The one finding that is about the file rather than about anything in
            # it, and it is feed-blocking for the same reason the read path raises:
            # an unreadable catalog must never be mistaken for an empty one.
            return CatalogAudit(
                catalog_path=path,
                present=present,
                serves_feed=False,
                defects=(
                    CatalogDefect(
                        kind=DEFECT_UNREADABLE_CATALOG,
                        detail=str(err),
                        blocks_feed=True,
                    ),
                ),
            )
        scanned = _scan_catalog(raw)
        defects = [
            CatalogDefect(
                kind=line.fault,
                line=line.lineno,
                detail=(
                    "line does not parse as JSON and is skipped by every read"
                    if line.fault == DEFECT_UNPARSEABLE_LINE
                    else "line is JSON but carries no seq/report_key, so it is"
                    " skipped by every read"
                ),
            )
            for line in scanned
            if line.entry is None
        ]
        defects.extend(_duplicate_defects(scanned))
        census: Counter[str] = Counter()
        seen: set[tuple[str, str, str, str]] = set()
        for line in scanned:
            entry = line.entry
            if entry is None:
                continue
            identity = _census_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            state, detail, where = self._entry_artifacts(entry)
            census[state] += 1
            if detail is not None:
                defects.append(
                    CatalogDefect(
                        kind=_ARTIFACT_DEFECTS[state],
                        line=line.lineno,
                        seq=entry.seq,
                        report_key=entry.report_key,
                        path=where,
                        detail=detail,
                    )
                )
        # File order, so the list reads as a walk down the file an operator is
        # about to edit. Findings with no line of their own sort first: they are
        # about the catalog rather than about anything in it.
        defects.sort(key=lambda defect: (defect.line or 0, defect.kind))
        return CatalogAudit(
            catalog_path=path,
            present=present,
            lines=len(scanned),
            entries=sum(1 for line in scanned if line.entry is not None),
            serves_feed=not any(defect.blocks_feed for defect in defects),
            retained=census[ARTIFACTS_RETAINED],
            pruned=census[ARTIFACTS_PRUNED],
            incomplete=census[ARTIFACTS_INCOMPLETE],
            damaged=census[ARTIFACTS_DAMAGED],
            unreadable=census[ARTIFACTS_UNREADABLE],
            unaddressable=census[ARTIFACTS_UNADDRESSABLE],
            defects=tuple(defects),
        )

    def _entry_artifacts(
        self, entry: ReportCatalogEntry
    ) -> tuple[str, str | None, Path | None]:
        """One entry's artifact state, why it is a defect, and where to go fix it.

        Exactly the classification :meth:`read_bundle` performs for a single fetch,
        stopping at the last step it takes — the same stored-identity audits, the
        same contained walk, the same per-name :func:`_artifact_faults`, the same
        per-artifact open, and the same order, since a link is reported ahead of an
        absence and an absence ahead of a fault. Sharing those rules is the whole
        promise of the census: a state it counts is the answer a fetch of that
        entry gives, so *retained* is a bundle a consumer can actually take and
        *pruned* is the set of entries a fetch answers "no longer retained" for.
        Only an absent evaluation dir is pruned — retention removes the directory,
        so a dir that is still there with nothing in it is a set that lost its
        files some other way, which a fetch reports as incomplete.

        ``None`` as the detail means the state is not a defect: a complete set, or
        the designed end state of an entry outliving artifacts retention removed.
        The path accompanies the states an operator clears in the tree rather than
        in the catalog file; an unaddressable identity gets none, because whatever
        directory its triple resolves to is not evidence of anything and the repair
        is on the line, which the defect already names.
        """
        unsafe = _unaddressable(entry)
        if unsafe is not None:
            return (
                ARTIFACTS_UNADDRESSABLE,
                f"unsafe stored report identity ({unsafe})",
                None,
            )
        fault = _identity_fault(entry)
        if fault is not None:
            return (
                ARTIFACTS_UNADDRESSABLE,
                f"incoherent stored report identity ({fault})",
                None,
            )
        # Pure path math, and used only to *name* the dir in a finding — reaching
        # it is the contained walk's job, below. The identity audits above are what
        # make this safe to compute at all.
        where = self.artifact_dir(entry)
        try:
            with self._opened_dir(entry) as dir_fd:
                if dir_fd is None:
                    return ARTIFACTS_PRUNED, None, None
                state, detail = _fetchable_state(dir_fd)
        except EvaluationDamagedTreeError as err:
            return ARTIFACTS_DAMAGED, _damaged_hop_detail(err), where
        except OSError as err:
            return (
                ARTIFACTS_UNREADABLE,
                f"unreadable report artifacts ({err})",
                where,
            )
        # A directory only where there is something to go clear: the two states
        # that are not defects name none, since there is nothing there to act on.
        return state, detail, (None if detail is None else where)

    # -- writes ---------------------------------------------------------------

    def record_evaluation(
        self, repo: ManagedRepo, task_id: str, evaluation_id: str
    ) -> ReportCatalogEntry | None:
        """Publish one evaluation if it is a complete, archived, unpublished report.

        The live hook (:class:`orch_hub.semantic.ReportCataloger`), called by the
        semantic evaluator once its whole artifact set is durable. Returns the new
        entry, or ``None`` for the three ordinary non-publication cases: the
        artifact set is not one the feed can publish (incomplete, or damaged by a
        link at one of its own hops), the evaluated task is not archived yet (an
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
        # The slug is the ``<repo-slug>/`` level of the artifact dir — the storage
        # identity — so a key built from it provably matches the one a
        # reconciliation walk (which only ever sees directory names) derives for
        # the same evaluation.
        slug = repo_slug(repo.name)
        key = report_key(
            repo_slug=slug, task_id=task_id, evaluation_id=evaluation_id
        )
        # One contained descriptor for the whole judgement: completeness, the
        # identity reads, and the manifest hashes all describe the directory this
        # open accepted, not a name re-walked once per artifact.
        try:
            with opened_evaluations_dir(
                self._config.data_dir,
                repo_name=repo.name,
                task_id=task_id,
                evaluation_id=evaluation_id,
            ) as dir_fd:
                if dir_fd is None or not _is_complete(dir_fd):
                    return None
                with self._locked():
                    existing = self._read_entries()
                    if any(entry.report_key == key for entry in existing):
                        return None
                    return self._publish(
                        seq=_next_seq(existing),
                        slug=slug,
                        task_id=task_id,
                        evaluation_id=evaluation_id,
                        dir_fd=dir_fd,
                    )
        except EvaluationDamagedTreeError:
            # A link at one of the tree's own hops, refused rather than followed:
            # the same non-candidate answer :func:`_is_complete` gives a linked
            # artifact, so the hook, the sweep, and the retention guard agree that
            # a damaged dir is not a report and is not publication debt. Nothing
            # inside the lock can raise this — every artifact read below converts
            # its own failures — so this classifies the walk alone.
            return None

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
        slug = repo_slug(repo.name)
        known = {entry.report_key for entry in self.entries()}
        archived: dict[tuple[Path, str], bool] = {}
        owed: set[str] = set()
        for evaluation_id in _level_names(
            self._config.data_dir, repo_name=repo.name, task_id=task_id
        ):
            key = report_key(
                repo_slug=slug, task_id=task_id, evaluation_id=evaluation_id
            )
            if key in known:
                continue
            try:
                with opened_evaluations_dir(
                    self._config.data_dir,
                    repo_name=repo.name,
                    task_id=task_id,
                    evaluation_id=evaluation_id,
                ) as dir_fd:
                    if dir_fd is None or not _is_complete(dir_fd):
                        continue
                    try:
                        publishable = self._publishable(
                            _read_candidate(
                                dir_fd,
                                slug=slug,
                                task_id=task_id,
                                evaluation_id=evaluation_id,
                            ),
                            task_id,
                            archived,
                        )
                    except ReportCatalogError as err:
                        _log.warning(
                            "retention keeps unjudged report %s/%s/%s: %s",
                            slug,
                            task_id,
                            evaluation_id,
                            err,
                        )
                        publishable = True
            except EvaluationDamagedTreeError:
                # Damage is not debt: a link at one of the tree's hops is the same
                # non-candidate answer publication gives it, so retention stays free
                # to prune a dir the feed will never publish.
                continue
            if publishable:
                owed.add(evaluation_id)
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
            for slug, task_id, evaluation_id in self._scan_candidates():
                key = report_key(
                    repo_slug=slug, task_id=task_id, evaluation_id=evaluation_id
                )
                if key in known:
                    continue
                try:
                    entry = self._publish_id(
                        seq=seq,
                        slug=slug,
                        task_id=task_id,
                        evaluation_id=evaluation_id,
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

    def repair(self, *, apply: bool = False) -> CatalogRepair:
        """Drop the catalog's provably redundant lines, or refuse to touch it.

        The other half of the operator surface (``orch-hub eval catalog repair``),
        and the only path in the subsystem that rewrites an append-only file. What
        it may do is therefore fixed by one rule rather than by judgement: **every
        already-published entry keeps its ``seq`` and its ``report_key``, or nothing
        happens at all.** Two drops satisfy it, and they are the two an operator
        cannot make safely by hand at scale:

        - a line byte-identical to an earlier one, which a merged catalog or a
          backup restored over a live one produces wholesale. The surviving copy is
          the same bytes, so the entry set is unchanged and only the ambiguity goes
          away;
        - trailing bytes from an interrupted append that parse as nothing. No read
          has ever seen an entry there, and the next append would only fence them
          off as their own permanently unparseable line.

        Everything else is refused, explicitly, with the conflict named: two
        *distinct* entries sharing a ``seq`` or a ``report_key`` can only be
        separated by renumbering or dropping one, and both move an identity a
        consumer may already have imported — a decision that belongs to whoever
        knows what has been consumed, not to a verb. A refusal writes **nothing**,
        not even the safe drops: half-repairing a file an operator is about to edit
        by hand would invalidate the line numbers ``verify`` just gave them.

        Interior lines that merely fail to parse are also left alone. They are
        inert — every read skips them — and they are the only surviving evidence of
        whatever damaged the file, which a repair verb has no business destroying.

        Deliberately **not** wired into startup. A damaged catalog is already
        fail-closed and safe: publication pauses, retention stops pruning, and
        nothing is lost. Repairing it silently at boot would trade a visible halt
        for an invisible rewrite of durable publication state. So the plan is
        computed and returned; ``apply=True`` — the confirmed operator decision —
        is what writes, after copying the original beside the catalog
        (:data:`CATALOG_BACKUP_SUFFIX`).

        The whole plan-and-write cycle runs under :meth:`_locked`, the same
        transaction publication allocates sequences in, so a concurrent hook or
        sweep cannot append between the scan and the replacement and have its line
        dropped. Gated.
        """
        self._require_enabled()
        with self._locked():
            raw = self._catalog_bytes()
            scanned = _scan_catalog(raw)
            # ``serves_feed`` describes the file as it stands, so it is measured by
            # the audit that decides it (:func:`_duplicate_defects`) rather than
            # inferred from the plan: a refusal and an unconfirmed plan both leave
            # a duplicate-bearing catalog on disk, and reporting either as working
            # would tell a health check the opposite of what the feed answers.
            conflict = _identity_conflict(scanned)
            if conflict is not None:
                return CatalogRepair(
                    catalog_path=self.catalog_path,
                    refused=conflict,
                    serves_feed=not _duplicate_defects(scanned),
                )
            actions = tuple(_repair_plan(raw, scanned))
            if not actions or not apply:
                return CatalogRepair(
                    catalog_path=self.catalog_path,
                    actions=actions,
                    serves_feed=not _duplicate_defects(scanned),
                )
            repaired = _repaired_bytes(raw, actions)
            backup = self._backup_catalog(raw)
            _write_catalog(self.catalog_path, repaired)
            return CatalogRepair(
                catalog_path=self.catalog_path,
                actions=actions,
                applied=True,
                backup_path=backup,
                # Re-measured over the bytes actually written, so the claim is
                # about the repaired file rather than about the plan's intent.
                serves_feed=not _duplicate_defects(_scan_catalog(repaired)),
            )

    def _backup_catalog(self, raw: bytes) -> Path:
        """Copy the pre-repair bytes beside the catalog and return the copy's path.

        The evidence a confirmed repair is allowed to write over the catalog *for*,
        so it is the one file here that must never be replaced: a backup a later
        repair overwrote is worse than no backup, because it still looks like the
        original. The name carries the repair's own clock, but the clock is a
        label and not the guarantee — a second-resolution stamp collides between
        two repairs in one second, and the operator running them is exactly the
        operator who has just lost the state they were preserving. So the write
        *takes* a free name (:func:`_write_new_file`) rather than trusting the
        stamp to be one, and a repair that cannot get one writes nothing at all.

        The suffix keeps the copy inert: every read path opens :data:`CATALOG_NAME`
        by name and the candidate sweep never looks in this directory at all, so it
        is a file for an operator, not a second catalog the hub might find.
        """
        stamp = self._clock().strftime("%Y%m%dT%H%M%SZ")
        return _write_new_file(
            self.catalog_dir / f"{CATALOG_NAME}.{stamp}{CATALOG_BACKUP_SUFFIX}",
            raw,
        )

    # -- internals ------------------------------------------------------------

    def _publish_id(
        self,
        *,
        seq: int,
        slug: str,
        task_id: str,
        evaluation_id: str,
        archived_cache: dict[tuple[Path, str], bool] | None = None,
    ) -> ReportCatalogEntry | None:
        """Open one swept identity's artifact dir and publish it. Lock held.

        The sweep's bridge from a directory *name* to the descriptor everything
        else works from, and the reason the scan carries identities rather than
        paths: the dir is re-opened here, under the containment rule, so nothing is
        published on the strength of what an earlier walk of the same names saw. A
        dir pruned since, replaced by a file, left incomplete, or reached through a
        link is not a candidate any more — ``None``, no sequence consumed, retried
        next sweep if it comes back.
        """
        try:
            with opened_evaluations_dir(
                self._config.data_dir,
                repo_name=slug,
                task_id=task_id,
                evaluation_id=evaluation_id,
            ) as dir_fd:
                if dir_fd is None or not _is_complete(dir_fd):
                    return None
                return self._publish(
                    seq=seq,
                    slug=slug,
                    task_id=task_id,
                    evaluation_id=evaluation_id,
                    dir_fd=dir_fd,
                    archived_cache=archived_cache,
                )
        except EvaluationDamagedTreeError:
            # Not a candidate, exactly as for the live hook: a link at a hop is the
            # same answer a linked artifact gets, so no sequence is consumed and no
            # entry is written.
            return None

    def _publish(
        self,
        *,
        seq: int,
        slug: str,
        task_id: str,
        evaluation_id: str,
        dir_fd: int,
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
            dir_fd, slug=slug, task_id=task_id, evaluation_id=evaluation_id
        )
        if not self._publishable(candidate, task_id, archived_cache):
            return None
        entry = self._build_entry(
            candidate,
            seq=seq,
            slug=slug,
            task_id=task_id,
            evaluation_id=evaluation_id,
            dir_fd=dir_fd,
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
        dir_fd: int,
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
                _artifact_digest(name, dir_fd=dir_fd) for name in REQUIRED_ARTIFACTS
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
        found: list[ReportCatalogEntry] = []
        for line in _scan_catalog(self._catalog_bytes()):
            if line.entry is None:
                _log.warning(
                    "report catalog line %d skipped (%s)", line.lineno, line.fault
                )
                continue
            found.append(line.entry)
        found.sort(key=lambda entry: entry.seq)
        _require_unique_identity(found)
        return found

    def _catalog_bytes(self) -> bytes:
        """The catalog file's raw bytes; empty when nothing has been published.

        Bytes rather than text, and the reason is durability rather than taste: a
        crashed append can cut a line mid UTF-8 sequence, and a whole-file
        ``read_text`` would then refuse to decode *the entire catalog* over those
        few trailing bytes — one interrupted write taking the feed down is exactly
        the failure the torn-tail heal exists to prevent. Decoding is per line
        instead (:func:`_scan_catalog`), so undecodable bytes cost their own line
        and nothing else.

        A missing file is an empty catalog, not an error (nothing published yet).
        An IO failure is neither: it raises, so every read fails closed rather than
        reporting a readable-but-unreachable catalog as empty — which publication
        would then number from 1 again.
        """
        try:
            return self.catalog_path.read_bytes()
        except FileNotFoundError:
            return b""
        except OSError as err:
            raise ReportCatalogError(f"unreadable {CATALOG_NAME} ({err})") from err

    def _scan_candidates(self) -> list[tuple[str, str, str]]:
        """Every evaluation identity in the tree, in deterministic global order.

        Walks the fixed ``<repo-slug>/<task-id>/<evaluation-id>/`` layout (a
        stray file, a linked hop, or an extra nesting level is simply not a
        candidate). The sort key is ``(evaluation order, repo slug, task id)``:
        evaluation order first so a backfill numbers reports in generation order
        across repos, then slug and task id to break the ties that scoped
        evaluation ids produce when two repos evaluate in the same second — which
        is precisely the collision the global sequence exists to resolve.

        Completeness is deliberately *not* filtered here: it is answered inside the
        publication step from the descriptor it will hash, so the scan cannot
        admit a dir on the strength of a check taken against an earlier walk of
        the same names.
        """
        candidates = _walk_evaluations(self._config.data_dir)
        candidates.sort(
            key=lambda item: (evaluation_order_key(item[2]), item[0], item[1])
        )
        return candidates


# -- module helpers -----------------------------------------------------------


def _walk_evaluations(data_dir: Path) -> list[tuple[str, str, str]]:
    """Every ``(repo_slug, task_id, evaluation_id)`` the evaluations tree holds.

    A plain three-level walk, sorted at each level so the raw order is itself
    stable (the caller re-sorts into global order). An absent tree yields nothing
    — a hub that has never evaluated anything is not an error state.

    Identity triples rather than directories, because a path handed forward is a
    name the publication step would have to walk again: every step that reads a
    candidate re-opens it through :func:`~orch_hub.evaluation.opened_evaluations_dir`
    from these names, so the sweep is bound by the same containment rule as a feed
    read rather than by whatever the tree looked like when it was listed
    (:func:`_level_names`).

    A repo directory whose name is not already a canonical slug is skipped: the
    hub writes only slugged names, the slug rule is not injective, and re-opening
    ``alpha!!`` by that name would open ``alpha`` — publishing one repo's report
    under another's storage identity. It is the rule
    :func:`_identity_fault` applies to a stored entry, applied here to the
    directory that would produce one.
    """
    found: list[tuple[str, str, str]] = []
    for slug in _level_names(data_dir):
        if repo_slug(slug) != slug:
            continue
        for task_id in _level_names(data_dir, repo_name=slug):
            found.extend(
                (slug, task_id, evaluation_id)
                for evaluation_id in _level_names(
                    data_dir, repo_name=slug, task_id=task_id
                )
            )
    return found


def _level_names(data_dir: Path, **level: str) -> list[str]:
    """Subdirectory names at one level of the evaluations tree, or none at all.

    The sweep's census step. Anything that stops the level from being read as
    hub-owned tree — absent, replaced by a file, reached through a link, or an
    unsafe stored name — is simply not a level with candidates in it, which is the
    answer a sweep needs: it must not abort a startup backfill over one damaged
    subtree, and a damaged dir is not publishable anyway, so skipping it and
    publishing everything else is the same verdict the read paths give.
    """
    try:
        with opened_evaluations_dir(data_dir, **level) as dir_fd:
            return [] if dir_fd is None else contained_dir_names(dir_fd)
    except (OSError, ValueError, EvaluationDamagedTreeError):
        return []


def _artifact_faults(dir_fd: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(missing, uncontained)`` names among an evaluation dir's four artifacts.

    Two different defects, deliberately never merged into one list. *Missing* is
    the ordinary state — retention pruned the file, or the dir only ever held an
    evidence-only generation — and it is what the feed's "no longer retained"
    story is made of. *Uncontained* is a link standing where a published artifact
    should be, which the hub never writes: the artifact's bytes would be some
    other file's, so the report is damaged rather than gone, and a caller that
    could not tell the two apart would report tampering as retention's designed
    end state.

    ``lstat`` rather than ``is_file()`` is the whole difference: the latter
    follows links, so a symlinked artifact counted as *present* is what let
    publication hash an outside file and the bundle serve it. Anything that is
    not a regular file at that hop — a directory, a FIFO, an absent name, a
    parent that will not stat — is counted missing, matching what ``is_file()``
    answered for those.

    This is a census, not a containment check: it says what is wrong across the
    whole set before anything is opened (a bundle names every absent artifact at
    once rather than failing at the first). Containment itself is at the open, in
    :func:`_read_artifact_file` for a read and :func:`_artifact_digest` for a
    publication, because a name checked here can mean something else by the time
    it is opened. Taking ``dir_fd`` rather than a path is what keeps the census
    describing the same directory those opens will use, whatever the names above
    it come to mean in between.
    """
    missing: list[str] = []
    uncontained: list[str] = []
    for name in REQUIRED_ARTIFACTS:
        try:
            mode = os.lstat(name, dir_fd=dir_fd).st_mode
        except OSError:
            missing.append(name)
            continue
        if stat.S_ISLNK(mode):
            uncontained.append(name)
        elif not stat.S_ISREG(mode):
            missing.append(name)
    return tuple(missing), tuple(uncontained)


def _damaged_detail(names: tuple[str, ...]) -> str:
    """Why a bundle is unavailable because artifacts are linked, not present.

    One phrasing for both the pre-read census and a link the read itself refuses,
    so the two cannot drift into describing the same tree two ways. Kept
    recognizably apart from the "no longer retained" and "incomplete" details,
    which a consumer reads as retention doing its job.
    """
    return (
        "report artifact set is damaged"
        f" ({', '.join(names)} reached through a symbolic link"
        " and refused rather than followed)"
    )


def _incomplete_detail(names: tuple[str, ...]) -> str:
    """Why a set is neither whole nor gone: these published artifacts are absent.

    Shared by the bundle read and the audit census for the same reason
    :func:`_damaged_detail` is: an operator comparing a verify run with a failed
    fetch must not have to decide whether two wordings mean one state.
    """
    return f"report artifact set is incomplete (missing {', '.join(names)})"


def _unreadable_artifact_detail(name: str, err: OSError) -> str:
    """Why one artifact that is *there* still cannot be read.

    Distinct from absence and from damage on purpose: the file the hub published
    is still standing at its own hop, so this is neither retention's end state nor
    a redirected read — it is an IO fault (a mode, a failing device) that stays
    until something outside this subsystem changes.
    """
    return f"unreadable report artifact {name} ({err})"


def _damaged_hop_detail(err: EvaluationDamagedTreeError) -> str:
    """The same verdict for a link at a *directory* hop rather than at a file.

    Shares the "damaged" wording with :func:`_damaged_detail` on purpose: a
    consumer separates damage from retention's designed end state, and which hop
    of the tree carries the link is repair detail, not a second category. Before
    the tree hops were walked no-follow, a linked evaluation dir published and
    served an outside artifact set outright, and a *looped* one read as pruned —
    the two failures this phrasing now names.
    """
    return f"report artifact set is damaged ({err})"


def _fetchable_state(dir_fd: int) -> tuple[str, str | None]:
    """What a bundle fetch of this opened evaluation dir would answer, and why.

    The census half of :meth:`ReportCatalog._read_opened_bundle`, in its order and
    with its wordings, so :meth:`ReportCatalog.audit` reports the state a fetch
    reaches rather than a second opinion about the same directory. The census is
    the only thing a verify run says about an entry's artifacts, so anything it
    cannot see it would report as a healthy report a consumer can take.

    Two steps, because the names and the objects are different questions.
    :func:`_artifact_faults` describes the names — a link where a file should be,
    an absent or non-regular one — and the open then proves the object is there to
    read: a published artifact left unreadable (a mode, a failing device) passes
    every ``lstat`` and still fails every fetch. Absence found at the open is the
    ordinary race, and gets absence's answer.

    Opened, not read, and that is the one deliberate gap: an audit walks every
    published entry, so reading the bytes would make a verify run cost the whole
    catalog's artifact volume — for the faults that appear only mid-file, which
    the fetch itself still reports. What is bought is the class of fault that is
    *permanent* until an operator acts, which is what the verb is for.

    Yields the same ``(state, detail)`` vocabulary the census counts, with
    ``None`` for the two states that are not defects.
    """
    missing, uncontained = _artifact_faults(dir_fd)
    if uncontained:
        return ARTIFACTS_DAMAGED, _damaged_detail(uncontained)
    if missing:
        return ARTIFACTS_INCOMPLETE, _incomplete_detail(missing)
    for name in REQUIRED_ARTIFACTS:
        try:
            with opened_artifact(name, dir_fd=dir_fd) as handle:
                published = handle is not None
        except EvaluationDamagedTreeError:
            return ARTIFACTS_DAMAGED, _damaged_detail((name,))
        except FileNotFoundError:
            return ARTIFACTS_PRUNED, None
        except OSError as err:
            return ARTIFACTS_UNREADABLE, _unreadable_artifact_detail(name, err)
        if not published:
            # Swapped for a directory or a FIFO between the census and the open:
            # not the published artifact, so not retained — a fetch says the same.
            return ARTIFACTS_PRUNED, None
    return ARTIFACTS_RETAINED, None


def _is_complete(dir_fd: int) -> bool:
    """Whether an evaluation dir holds the full L1+L2 report artifact set.

    The candidate predicate, and the reason a failed semantic job or a
    ``/metrics``-only generation never appears as a ready report: the semantic
    evaluator writes all four files as one all-or-nothing dir, so their joint
    presence is exactly the "durably complete" condition, checked on the
    filesystem rather than inferred from job state. Completeness alone does not
    publish — the candidate must also be coherent and archived.

    A *linked* artifact is not a present one here either, which is a publication
    rule and not only a read rule: an entry's manifest digest is the promise a
    consumer verifies, and taking it over whatever a link points at would publish
    that promise about a file the hub never wrote. So a damaged dir is not a
    candidate — for a sweep, for the live hook, and for the retention guard that
    shares this predicate (an unpublishable dir is not publication debt, exactly
    as an incomplete one is not).

    Phrased over :func:`_artifact_faults` so the read side can report *which*
    artifacts are gone or damaged without restating the rule; publication and the
    bundle export must never be able to disagree about what a complete report is.
    """
    missing, uncontained = _artifact_faults(dir_fd)
    return not missing and not uncontained


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


@dataclass(frozen=True)
class _ScannedLine:
    """One non-blank catalog line, as both readers and the audit see it.

    ``entry`` and ``fault`` are exclusive: a line either round-trips into an entry
    or carries the machine code saying why it did not. ``text`` is the decoded line
    (``None`` when the bytes are not UTF-8 at all), kept so the repair can compare
    lines for byte-identity without decoding twice.
    """

    lineno: int
    text: str | None
    entry: ReportCatalogEntry | None
    fault: str | None


def _scan_catalog(raw: bytes) -> list[_ScannedLine]:
    """Parse the catalog's bytes into located lines, keeping what failed and why.

    The one parse both the feed's readers (:meth:`ReportCatalog._read_entries`) and
    the operator audit (:meth:`ReportCatalog.audit`) go through, so "verify says
    clean" and "the feed reads" cannot become different questions. The readers drop
    the line numbers and the faults; the audit is the caller that needs them.

    Split on ``\\n`` alone and decoded per line. Both matter, and neither is
    cosmetic: the writer emits exactly one ``\\n`` per record, so splitting on the
    wider set of Unicode line breaks ``str.splitlines`` recognizes could cut a line
    *inside* a JSON string, and decoding per line keeps a torn tail that cut a
    multi-byte character from making the whole catalog undecodable. Line numbers
    are 1-based over that split, so they are the ones an operator's editor shows.

    Blank lines are skipped rather than reported: the torn-tail heal writes one
    deliberately (:meth:`ReportCatalog._append_line`), so they are the write path
    working, not damage.
    """
    scanned: list[_ScannedLine] = []
    for lineno, chunk in enumerate(raw.split(b"\n"), start=1):
        if not chunk.strip():
            continue
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            scanned.append(
                _ScannedLine(lineno, None, None, DEFECT_UNPARSEABLE_LINE)
            )
            continue
        try:
            data = json.loads(text)
        except ValueError:
            scanned.append(
                _ScannedLine(lineno, text, None, DEFECT_UNPARSEABLE_LINE)
            )
            continue
        entry = _entry_from_dict(data)
        if entry is None:
            scanned.append(_ScannedLine(lineno, text, None, DEFECT_NOT_AN_ENTRY))
            continue
        scanned.append(_ScannedLine(lineno, text, entry, None))
    return scanned


_ARTIFACT_DEFECTS = {
    ARTIFACTS_INCOMPLETE: DEFECT_INCOMPLETE_ARTIFACTS,
    ARTIFACTS_DAMAGED: DEFECT_DAMAGED_ARTIFACTS,
    ARTIFACTS_UNREADABLE: DEFECT_UNREADABLE_ARTIFACTS,
    ARTIFACTS_UNADDRESSABLE: DEFECT_UNADDRESSABLE,
}
"""Which artifact states are defects, and under which code they are reported.
``retained`` and ``pruned`` are absent on purpose: a complete set is nothing to
report, and an entry outliving pruned artifacts is the feed working as designed."""


def _report_version_key(entry: ReportCatalogEntry) -> tuple[tuple[str, int], int]:
    """Which of one task's published reports is the newer, as the coverage census ranks them.

    Generation order first (:func:`~orch_hub.evaluation.evaluation_order_key`, the
    same rule the per-task evaluation view lists by, so the census and the view a
    row links to cannot disagree about which report is current), publication
    ``seq`` second. The tie-break is not decoration: reconciliation backfills a
    report evaluated before its task was archived, so a *later* seq can carry an
    *older* evaluation, and ranking by seq alone would let that backfill overrule
    the newer report of the same task. Comparing scoped evaluation ids is sound
    only within one ``(repo_slug, task_id)`` group, which is the only place this
    is used.
    """
    return (evaluation_order_key(entry.evaluation_id), entry.seq)


def _census_identity(entry: ReportCatalogEntry) -> tuple[str, str, str, str]:
    """Everything an entry's artifact verdict is a function of.

    The audit's dedup key, and it is the *whole* identity rather than the
    ``report_key`` alone for a reason the verb exists to serve: a damaged catalog
    is precisely where two lines carry one key and different sources, and a census
    keyed on the key would then examine the first line and silently answer for the
    second — hiding, say, an unaddressable identity or a damaged artifact set
    behind a duplicate-key finding that says nothing about either. Every field
    :meth:`ReportCatalog._entry_artifacts` reads is here, so two lines collapse
    only when they provably get the same answer, which is what keeps a
    byte-identical repeat from being counted twice.
    """
    return (entry.repo_slug, entry.task_id, entry.evaluation_id, entry.report_key)


def _duplicate_defects(scanned: list[_ScannedLine]) -> list[CatalogDefect]:
    """Every repeated ``seq``/``report_key``, located at the line that repeats it.

    The enumerating counterpart of :func:`_require_unique_identity`, which raises at
    the first repeat because a read that cannot promise exactly-once traversal must
    serve nothing. An operator needs the whole list, and needs each item pinned to
    the line that *introduced* the ambiguity plus the earlier line it collides with
    — that pair is the edit to make.

    Both audits are applied to every line rather than short-circuiting on the
    first, so a line repeating a sequence *and* a key reports both: they are
    different breakages (an ambiguous cursor, versus one source evaluation with two
    global identities) and an operator fixing one should not discover the other on
    the next run.
    """
    first_seq: dict[int, int] = {}
    first_key: dict[str, int] = {}
    found: list[CatalogDefect] = []
    for line in scanned:
        entry = line.entry
        if entry is None:
            continue
        prior = first_seq.setdefault(entry.seq, line.lineno)
        if prior != line.lineno:
            found.append(
                CatalogDefect(
                    kind=DEFECT_DUPLICATE_SEQ,
                    line=line.lineno,
                    seq=entry.seq,
                    report_key=entry.report_key,
                    detail=(
                        f"seq {entry.seq} is already cataloged at line {prior}:"
                        " the feed cursor is ambiguous"
                    ),
                    blocks_feed=True,
                )
            )
        prior = first_key.setdefault(entry.report_key, line.lineno)
        if prior != line.lineno:
            found.append(
                CatalogDefect(
                    kind=DEFECT_DUPLICATE_KEY,
                    line=line.lineno,
                    seq=entry.seq,
                    report_key=entry.report_key,
                    detail=(
                        f"report {entry.report_key} is already cataloged at line"
                        f" {prior}: global identity is not unique"
                    ),
                    blocks_feed=True,
                )
            )
    return found


def _identity_conflict(scanned: list[_ScannedLine]) -> str | None:
    """Why the catalog cannot be repaired safely, or ``None`` if it can.

    Asks the only question the repair's admission rule turns on: *once the
    byte-identical repeats are collapsed, does a ``seq`` or a ``report_key`` still
    appear twice?* If it does, the two lines are genuinely different entries, and
    separating them means renumbering or dropping one — either of which moves an
    identity a consumer may already have stored under the promise that a published
    ``seq``/``report_key`` never changes. There is no safe automatic answer, so the
    repair reports the conflicting pair and writes nothing.

    Deliberately evaluated *before* the plan rather than on its result: a refusal
    must leave the file exactly as ``verify`` described it, line numbers included.
    """
    seen_text: set[str] = set()
    by_seq: dict[int, int] = {}
    by_key: dict[str, int] = {}
    for line in scanned:
        entry = line.entry
        if entry is None:
            continue
        text = (line.text or "").strip()
        if text in seen_text:
            continue  # a byte-identical repeat: the plan removes it losslessly
        seen_text.add(text)
        prior = by_seq.setdefault(entry.seq, line.lineno)
        if prior != line.lineno:
            return (
                f"{CATALOG_NAME} holds distinct entries at seq {entry.seq}"
                f" (lines {prior} and {line.lineno}); dropping or renumbering"
                " either would change an already-published identity, so it must be"
                " resolved by hand"
            )
        prior = by_key.setdefault(entry.report_key, line.lineno)
        if prior != line.lineno:
            return (
                f"{CATALOG_NAME} holds distinct entries for report"
                f" {entry.report_key} (lines {prior} and {line.lineno}); dropping"
                " either would change an already-published identity, so it must be"
                " resolved by hand"
            )
    return None


def _repair_plan(
    raw: bytes, scanned: list[_ScannedLine]
) -> list[CatalogRepairAction]:
    """The lines a repair may drop, in file order. Every one is identity-preserving.

    Two admissions, and the docstring on :meth:`ReportCatalog.repair` says why they
    are the only two. A byte-identical repeat leaves the same bytes behind, so no
    entry's ``seq``, ``report_key``, or content changes — only the ambiguity does.
    Trailing bytes of an interrupted append parse as no entry at all, so nothing
    published is in them.

    Interior unparseable lines are deliberately absent: they are already skipped by
    every read, so removing them buys nothing, and they are the only surviving
    evidence of whatever damaged the file.
    """
    actions: list[CatalogRepairAction] = []
    first_text: dict[str, int] = {}
    for line in scanned:
        if line.entry is None:
            continue
        prior = first_text.setdefault((line.text or "").strip(), line.lineno)
        if prior != line.lineno:
            actions.append(
                CatalogRepairAction(
                    kind=REPAIR_DROP_DUPLICATE_LINE,
                    line=line.lineno,
                    detail=f"byte-identical repeat of line {prior}",
                )
            )
    torn = _torn_lineno(raw)
    if torn is not None and any(
        line.lineno == torn and line.fault == DEFECT_UNPARSEABLE_LINE
        for line in scanned
    ):
        actions.append(
            CatalogRepairAction(
                kind=REPAIR_DROP_TORN_TAIL,
                line=torn,
                detail=(
                    "trailing bytes of an interrupted append; no read has ever"
                    " parsed an entry here"
                ),
            )
        )
    actions.sort(key=lambda action: action.line)
    return actions


def _torn_lineno(raw: bytes) -> int | None:
    """The line number of an unterminated final line, or ``None`` if there is none.

    The same condition :func:`_has_torn_tail` checks before an append fences it off,
    asked for *which* line rather than whether. A final line that parses as an
    entry is not reported: it is a complete record that merely lacks its newline,
    and the next append's heal keeps it readable.
    """
    if not raw or raw.endswith(b"\n"):
        return None
    chunks = raw.split(b"\n")
    return len(chunks) if chunks[-1].strip() else None


def _repaired_bytes(raw: bytes, actions: tuple[CatalogRepairAction, ...]) -> bytes:
    """The catalog's bytes with the planned lines removed, and nothing else touched.

    Every surviving line is copied verbatim, including blank ones and any interior
    line that would not parse: the repair is a deletion of named lines, never a
    re-serialization of the entries. That is what makes "every published ``seq`` and
    ``report_key`` is preserved" a property of the bytes rather than of a round-trip
    through this hub's current record shape — a line written by an older or newer
    writer survives a repair unchanged.

    The result ends with a newline unless it is empty, so the file the repair leaves
    is never itself torn.
    """
    drop = {action.line for action in actions}
    kept = [
        chunk
        for lineno, chunk in enumerate(raw.split(b"\n"), start=1)
        if lineno not in drop
    ]
    repaired = b"\n".join(kept)
    if repaired and not repaired.endswith(b"\n"):
        repaired += b"\n"
    return repaired


def _write_catalog(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` durably: same-dir temp, fsync, rename, fsync dir.

    The repair's write, and the reason it is not a plain ``write_bytes``: it
    replaces the durable record of every global publication identity, so a crash
    part-way through must leave either the original or the repaired file and never
    a truncated one. The rename is the commit point; the directory fsync is what
    makes the rename itself survive, since an entry that is fsynced but not linked
    durably is still a file that can disappear.
    """
    tmp = _staged_bytes(path, data)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _write_new_file(path: Path, data: bytes) -> Path:
    """Write ``data`` to ``path``, or to the next free name beside it. Never over one.

    The write for a file whose whole value is that it is *not* the current one: a
    repair's pre-repair backup. :func:`_write_catalog` commits with a rename, which
    replaces whatever stood there — correct for the catalog, and destruction here,
    since two repairs within one clock second would otherwise name the same backup
    and the second would erase the first's evidence.

    So the name is taken rather than assumed free: the bytes are staged in the same
    directory and committed with ``os.link``, which is the primitive that *cannot*
    replace an existing name — it fails with ``EEXIST`` instead, and this then tries
    the next suffixed candidate. Committing by link rather than by an exclusive
    create also keeps the file all-or-nothing: the name appears already holding
    every byte, so a crash mid-write leaves no truncated file wearing the backup's
    name. Returns the name that was actually taken; the caller reports it rather
    than assuming the one it asked for.
    """
    tmp = _staged_bytes(path, data)
    try:
        for index in range(_BACKUP_NAME_ATTEMPTS):
            candidate = (
                path
                if index == 0
                else path.with_name(f"{path.stem}-{index}{path.suffix}")
            )
            try:
                os.link(tmp, candidate)
            except FileExistsError:
                continue
            _fsync_dir(path.parent)
            return candidate
    finally:
        tmp.unlink(missing_ok=True)
    raise ReportCatalogError(
        f"no free name for {path.name} beside {CATALOG_NAME} after"
        f" {_BACKUP_NAME_ATTEMPTS} attempts"
    )


def _staged_bytes(path: Path, data: bytes) -> Path:
    """Write ``data`` durably to a temp file beside ``path`` and return it.

    Same directory, so the commit that follows — a rename or a link — is a
    directory operation rather than a copy across filesystems, and the ``fsync``
    is what makes the commit publish bytes that are actually on the device.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return tmp


def _fsync_dir(path: Path) -> None:
    """Make a rename or a link into ``path`` itself durable.

    A file whose bytes are fsynced but whose directory entry is not can still
    disappear, which for a catalog replacement or its backup means losing the
    thing the write existed to preserve.
    """
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _unaddressable(entry: ReportCatalogEntry) -> str | None:
    """Why a stored identity cannot name a path in the tree at all, else ``None``.

    The first of the two pre-IO audits both read paths run, and the narrower one:
    it asks only whether the stored triple is a legal address
    (:func:`~orch_hub.evaluation.tree_segments` — the same segment rule the walk
    and the write paths use), so a hand-edited line carrying ``..`` or a separator
    is refused as *unsafe* before the coherence question is even meaningful.
    Reported rather than raised, like :func:`_identity_fault`: an immutable line no
    retry heals is better named than served by a permanently failing endpoint.
    """
    try:
        tree_segments(
            repo_name=entry.repo_slug,
            task_id=entry.task_id,
            evaluation_id=entry.evaluation_id,
        )
    except ValueError as err:
        return str(err)
    return None


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


def _read_artifact_file(name: str, *, dir_fd: int) -> bytes | None:
    """The whole contained artifact ``name``, or ``None`` if it is not one.

    The read-side wrapper of :func:`~orch_hub.evaluation.opened_artifact`: both
    feed read paths hand back or parse *these* bytes, so what a consumer receives
    came from the object the open accepted rather than from whatever the name
    resolves to now — and, because the open is relative to a descriptor the
    contained walk produced, from inside the evaluations tree rather than from
    wherever a directory above it was made to point.
    """
    with opened_artifact(name, dir_fd=dir_fd) as handle:
        return handle.read() if handle is not None else None


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


def _artifact_digest(name: str, *, dir_fd: int) -> ReportArtifact:
    """Hash and size one artifact file for the entry's manifest.

    Read in chunks: an evidence pack is bounded but not small, and nothing here
    needs it in memory. The caller has already established the file exists
    (:func:`_is_complete`), so a read failure here is a genuine IO problem and is
    raised as :class:`ReportCatalogError` — publishing an entry with an
    unverifiable hash would defeat the point of recording one.

    Through the same no-follow open the read paths use, because the recorded
    digest is the strongest claim an entry makes: a link planted between the
    completeness check and this hash would otherwise put an outside file's digest
    in the manifest, and every later verification — the consumer's included —
    would then be checking that file. A refused link (or anything else that is not
    a regular file) is the undecided answer rather than a decision: no entry is
    written, a sweep logs and retries it, and the shared predicate keeps the dir
    out of publication meanwhile.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with opened_artifact(name, dir_fd=dir_fd) as handle:
            if handle is None:
                raise ReportCatalogError(
                    f"artifact {name} is not a contained regular file"
                )
            while chunk := handle.read(_HASH_CHUNK):
                digest.update(chunk)
                size += len(chunk)
    except (OSError, EvaluationDamagedTreeError) as err:
        raise ReportCatalogError(f"unreadable artifact {name} ({err})") from err
    return ReportArtifact(name=name, size=size, sha256=digest.hexdigest())


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


def _read_json(name: str, *, dir_fd: int) -> object:
    """Parse one artifact JSON, raising :class:`ReportCatalogError` on failure.

    Through the same contained open the feed's reads and the manifest digest use
    (:func:`_read_artifact_file`), so publication judges the identity of exactly
    the bytes it is about to hash: a name a check found intact but that opens as
    something else by now is refused here too, rather than deciding the report on
    one file and publishing another. A refused link leaves the candidate undecided
    (the sweep retries it, retention keeps it) rather than deciding it.
    """
    try:
        data = _read_artifact_file(name, dir_fd=dir_fd)
    except (OSError, EvaluationDamagedTreeError) as err:
        raise ReportCatalogError(f"unreadable {name} ({err})") from err
    if data is None:
        raise ReportCatalogError(f"unreadable {name} (not a contained regular file)")
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError as err:
        raise ReportCatalogError(f"unreadable {name} ({err})") from err


def _read_json_object(name: str, *, dir_fd: int) -> dict | None:
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
        data = _read_json(name, dir_fd=dir_fd)
    except ReportCatalogError:
        return None
    return data if isinstance(data, dict) else None


def _read_evidence(dir_fd: int) -> dict | None:
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
    try:
        raw = _read_artifact_file(EVIDENCE_NAME, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except (OSError, EvaluationDamagedTreeError) as err:
        raise ReportCatalogError(f"unreadable {EVIDENCE_NAME} ({err})") from err
    if raw is None:
        # Not a regular file: as settled a fact about this pack as a corrupt one,
        # and no read will heal it either.
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class _Candidate:
    """One evaluation dir's publication facts, read once from its own artifacts.

    The unit both callers of the publication rule work from — the entry build and
    the retention guard — so "would the feed publish this dir?" has one answer no
    matter who asks, derived from one set of reads. Carrying the parsed artifacts
    also keeps an entry build to a single read per file.
    """

    report: dict
    evidence: dict | None
    target_source: str | None
    repo_path: Path | None


def _read_candidate(
    dir_fd: int, *, slug: str, task_id: str, evaluation_id: str
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
    report = _read_json(SEMANTIC_REPORT_NAME, dir_fd=dir_fd)
    if not isinstance(report, dict):
        raise ReportCatalogError(
            f"{SEMANTIC_REPORT_NAME} is not a JSON object in"
            f" {slug}/{task_id}/{evaluation_id}"
        )
    identity = dict(slug=slug, task_id=task_id, evaluation_id=evaluation_id)
    _require_identity(report, name=SEMANTIC_REPORT_NAME, **identity)
    _require_identity(
        _read_json_object(STATIC_METRICS_NAME, dir_fd=dir_fd),
        name=STATIC_METRICS_NAME,
        **identity,
    )
    evidence = _read_evidence(dir_fd)
    _require_identity(evidence, name=EVIDENCE_NAME, **identity)
    _require_identity(
        _mapping(evidence.get("target")) if evidence is not None else None,
        name=f"{EVIDENCE_NAME} target",
        **identity,
    )
    return _Candidate(
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
    explicitly unavailable *with a reason* — ``orchestrator_config`` always,
    ``protocol`` for a pack predating the per-run capture, one predating the
    payload half of the identity, or one whose runs did not verifiably share one
    identity, and ``runs``/``git`` when the pack is missing or degraded — so a
    consumer never mistakes an absent field for a known-empty one. An unreadable
    pack is not fatal: the report itself is still publishable, just less
    cohortable.

    The protocol section is read, not re-derived: the pack already aggregated its
    runs' captures under the rule that owns publication
    (:func:`orch_hub.protocol_provenance.effective_protocol_from_dict`), and a
    second rule here could publish an identity that one would not. Reading it from
    the pack is also what keeps the entry reproducible — the repo's deployment
    lock has almost certainly moved on by publication time.

    Every nested lookup is type-checked before use rather than trusted to match
    the schema. The pack is a file on disk that anything could have truncated or
    rewritten, and a raised ``AttributeError`` here is not a local failure: it
    would propagate out of the entry build and, in a sweep, cost every *later*
    candidate its publication too.
    """
    unavailable = ReportProvenance(
        runs_detail=PACK_UNREADABLE_DETAIL,
        git_detail=PACK_UNREADABLE_DETAIL,
        protocol_detail=PACK_UNREADABLE_DETAIL,
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
    protocol = effective_protocol_from_dict(evidence.get("protocol"))
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
        protocol_available=protocol.available,
        protocol_revision=protocol.revision,
        protocol_payload_sha256=protocol.payload_sha256,
        protocol_detail=protocol.detail,
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
    """Round-trip the nested provenance sections; anything absent stays unavailable.

    The protocol section is read through the rule that owns publication rather
    than field by field
    (:func:`orch_hub.protocol_provenance.effective_protocol_from_dict`), which is
    what keeps a line written by an older hub honest without migrating the
    append-only catalog: an entry published under a revision alone, before the
    identity became a pair, reads back unavailable with that reason instead of
    serving half an identity to a consumer cohorting by it.
    """
    if not isinstance(data, dict):
        return ReportProvenance()
    runs = data.get("runs")
    runs = runs if isinstance(runs, dict) else {}
    git = data.get("git")
    git = git if isinstance(git, dict) else {}
    protocol = effective_protocol_from_dict(data.get("protocol"))
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
        protocol_available=protocol.available,
        protocol_revision=protocol.revision,
        protocol_payload_sha256=protocol.payload_sha256,
        protocol_detail=protocol.detail,
        config_available=bool(config.get("available")),
        config_detail=_str_field(config, "detail"),
    )


def _str_field(data: dict, key: str) -> str | None:
    """A non-empty string field, else ``None`` (matches the other record loaders)."""
    value = data.get(key)
    return value if isinstance(value, str) and value else None
