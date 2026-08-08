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
- **Artifact-based eligibility.** An evaluation is a report candidate only when
  its whole L1+L2 artifact set (:data:`REQUIRED_ARTIFACTS`) is already durable.
  *How* it was evaluated is outside this module: a dir missing any of the four
  files is simply not a candidate, so a partial evidence-only generation or a
  failed semantic job can never surface as a ready report. ``archived`` is
  stamped from the evidence pack's own target source, so eligibility rests on the
  artifact, not on a live re-read of the repo.
- **Self-healing durability.** Appends are single fsynced lines with a
  torn-tail heal (:meth:`ReportCatalog._append_line`), and
  :meth:`ReportCatalog.reconcile` re-derives everything missing by scanning the
  evaluations tree. So the live hook is an *optimization for promptness*: if it
  is unwired, fails, or the hub dies mid-write, the next reconciliation catalogs
  the report anyway, and never mints a second global identity for it.

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
import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from .config import Config
from .evaluation import (
    EVALUATIONS_SUBDIR,
    EVIDENCE_NAME,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    evaluation_artifact_dir,
    evaluation_order_key,
)
from .models import ManagedRepo, ReportArtifact, ReportCatalogEntry, ReportProvenance
from .semantic import SEMANTIC_REPORT_MD_NAME, SEMANTIC_REPORT_NAME
from .static_metrics import STATIC_METRICS_NAME

_log = logging.getLogger(__name__)

CATALOG_SUBDIR = "report-catalog"
"""Top-level hub-data-dir subtree holding the catalog, parallel to the run
manager's ``runs/``, the intake manager's ``intake/``, the semantic-job manager's
``semantic-jobs/``, and the evaluation subsystem's ``evaluations/`` trees."""

CATALOG_NAME = "catalog.jsonl"
"""The append-only catalog file: one :class:`ReportCatalogEntry` per line."""

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

    Derived, not allocated: re-deriving it for an already-cataloged evaluation
    yields the same key, which is exactly how :meth:`ReportCatalog.reconcile` and
    the live hook stay idempotent without a lock spanning processes. It carries no
    ordering information by construction (a digest of identity, not of time), so a
    consumer cannot accidentally build ordering assumptions on it — ``seq`` is the
    only order in the feed. Components are joined with a null byte, which cannot
    appear in any of them (all three are validated single path segments), so no
    two distinct triples can hash the same joined string.
    """
    raw = "\x00".join((repo_slug, task_id, evaluation_id)).encode("utf-8")
    return REPORT_KEY_PREFIX + hashlib.sha256(raw).hexdigest()[:_REPORT_KEY_HEX]


def is_eligible(entry: ReportCatalogEntry) -> bool:
    """Whether an entry is a publishable feed item (an archived-task report).

    The feed's eligibility policy in one place: the catalog records every durably
    complete report for audit, while list/read paths surface only reports whose
    evaluated task was already archived — i.e. a completed task whose protocol
    story is finished and worth importing. Reading the stamped ``archived`` fact
    (from the evidence pack's target source at evaluation time) rather than
    re-resolving the task file keeps eligibility artifact-based and stable: an
    entry's eligibility never changes after it is written.
    """
    return entry.archived


class ReportCatalog:
    """Durable append-only catalog of completed reports, gated like evaluation.

    One instance lives in the resident hub service. It shares the evaluation
    subsystem's ``Config.evaluation_enabled`` gate, so a deployment that disables
    evaluation gets the same fail-closed boundary here as everywhere else in the
    subsystem (the API maps it to the uniform 404) — a disabled hub publishes
    nothing and reconciles nothing.

    ``clock`` must return timezone-aware UTC datetimes (it stamps
    ``cataloged_ts``). The instance lock serializes read-modify-append cycles so
    concurrent semantic jobs — which finish on independent daemon threads — cannot
    interleave and allocate the same ``seq``. Sequence allocation is derived from
    the *file* on every cycle, never carried in memory across cycles, so a restart
    (or a crash mid-reconcile) resumes numbering correctly from durable state.

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

    # -- reads ----------------------------------------------------------------

    def entries(
        self, *, after_seq: int | None = None, limit: int | None = None
    ) -> list[ReportCatalogEntry]:
        """Return cataloged entries in global order, optionally after a cursor.

        The durable read primitive the (session-2) feed route is a thin adapter
        over: ``after_seq`` is the cursor — strictly greater, so a consumer that
        stores the last ``seq`` it imported resumes without re-reading or skipping
        an entry — and ``limit`` caps one page. Entries are returned in ascending
        ``seq``, which is append order, so concurrent appends only ever add to the
        *end* of a page boundary and never renumber what a consumer already saw.

        Corrupt lines are skipped rather than fatal: a torn tail from a crashed
        append (or hand-editing) must not make the whole catalog unreadable, and
        such a line was never a visible entry to any consumer, so skipping it
        cannot hide a report a consumer already imported. Gated and read-only.
        """
        self._require_enabled()
        with self._lock:
            found = self._read_entries()
        if after_seq is not None:
            found = [entry for entry in found if entry.seq > after_seq]
        if limit is not None and limit >= 0:
            found = found[:limit]
        return found

    def find(self, key: str) -> ReportCatalogEntry | None:
        """Return the entry for one public report key, or ``None`` if uncataloged.

        The by-key read behind the (session-2) export route: a consumer that
        stored a ``report_key`` resolves it here without knowing the
        repo/task/evaluation directory layout. ``None`` means the key names no
        published report — distinct from a published report whose artifacts a
        retention prune has since removed, which stays an entry and is reported as
        explicitly unavailable by the read path. Gated and read-only.
        """
        self._require_enabled()
        with self._lock:
            for entry in self._read_entries():
                if entry.report_key == key:
                    return entry
        return None

    def artifact_dir(self, entry: ReportCatalogEntry) -> Path:
        """Contained artifact dir for a cataloged entry (pure path math).

        Resolves through the evaluation subsystem's own contained-path helper, so
        the same unsafe-segment rejection guards a catalog-driven read that guards
        every other artifact read. Re-slugging the stored slug is safe because the
        slug rule is idempotent (:func:`orch_hub.evaluation.repo_slug`). The dir
        may not exist — retention prunes artifacts while entries persist — so
        callers must treat absence as "no longer retained", not as an error.
        """
        return evaluation_artifact_dir(
            self._config.data_dir,
            repo_name=entry.repo_slug,
            task_id=entry.task_id,
            evaluation_id=entry.evaluation_id,
        )

    # -- writes ---------------------------------------------------------------

    def record_evaluation(
        self, repo: ManagedRepo, task_id: str, evaluation_id: str
    ) -> ReportCatalogEntry | None:
        """Catalog one evaluation if it is a complete, not-yet-cataloged report.

        The live hook (:class:`orch_hub.semantic.ReportCataloger`), called by the
        semantic evaluator once its whole artifact set is durable. Returns the new
        entry, or ``None`` when the evaluation is not a candidate — its artifact
        set is incomplete — or is already cataloged. Both no-op cases are normal,
        not errors: the second is what makes a re-run hook, a racing
        reconciliation, and a restart converge on exactly one global identity per
        source evaluation.

        Raises :class:`ReportCatalogError` when the artifacts exist but cannot be
        read/parsed; the evaluator isolates that so a catalog problem never fails
        an otherwise-successful evaluation, and the next reconciliation retries.
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
        with self._lock:
            existing = self._read_entries()
            if any(entry.report_key == key for entry in existing):
                return None
            return self._append(
                seq=_next_seq(existing),
                slug=slug,
                task_id=task_id,
                evaluation_id=evaluation_id,
                eval_dir=eval_dir,
            )

    def reconcile(self) -> list[ReportCatalogEntry]:
        """Backfill every complete-but-uncataloged report; return what was added.

        The completeness half of the design, run at startup and available as a
        manual repair. It walks the evaluations tree, keeps the dirs holding a
        full L1+L2 artifact set, and appends the ones no entry covers yet — which
        picks up reports written before this feature existed, reports whose live
        hook was unwired or failed, and reports for tasks archived long ago.

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
        One unreadable evaluation is logged and skipped rather than failing the
        sweep — the remaining reports still publish, and the broken one is retried
        next time. Gated.
        """
        self._require_enabled()
        with self._lock:
            existing = self._read_entries()
            known = {entry.report_key for entry in existing}
            seq = _next_seq(existing)
            added: list[ReportCatalogEntry] = []
            for slug, task_id, evaluation_id, eval_dir in self._scan_candidates():
                key = report_key(
                    repo_slug=slug, task_id=task_id, evaluation_id=evaluation_id
                )
                if key in known:
                    continue
                try:
                    entry = self._append(
                        seq=seq,
                        slug=slug,
                        task_id=task_id,
                        evaluation_id=evaluation_id,
                        eval_dir=eval_dir,
                    )
                except (ReportCatalogError, OSError) as err:
                    # One broken evaluation must not abort the sweep: skip it (no
                    # line was written, so ``seq`` is not consumed) and let the
                    # next reconciliation retry it once the artifact is readable.
                    _log.warning(
                        "report catalog skipped %s/%s/%s: %s",
                        slug,
                        task_id,
                        evaluation_id,
                        err,
                    )
                    continue
                known.add(key)
                added.append(entry)
                seq += 1
            return added

    # -- internals ------------------------------------------------------------

    def _append(
        self,
        *,
        seq: int,
        slug: str,
        task_id: str,
        evaluation_id: str,
        eval_dir: Path,
    ) -> ReportCatalogEntry:
        """Build one entry from its durable artifacts and append it. Lock held.

        The entry is built *completely first* and only then appended, so a source
        artifact that fails to read/parse raises before anything touches the
        catalog — there is no partial-entry state to clean up.
        """
        entry = self._build_entry(
            seq=seq,
            slug=slug,
            task_id=task_id,
            evaluation_id=evaluation_id,
            eval_dir=eval_dir,
        )
        self._append_line(entry)
        return entry

    def _build_entry(
        self,
        *,
        seq: int,
        slug: str,
        task_id: str,
        evaluation_id: str,
        eval_dir: Path,
    ) -> ReportCatalogEntry:
        """Assemble one catalog entry from an evaluation's persisted artifacts.

        Reads only the evaluation's own durable files: the semantic report for
        identity/evaluator/target facts, the evidence pack for provenance, and all
        four artifacts for size/hash. It deliberately does *not* consult the
        registry, the target repo, or run records — an entry must stay
        reproducible from the artifacts it names, and the catalog must never take
        a live read that could disagree with what was actually evaluated.
        """
        report = _read_json(eval_dir / SEMANTIC_REPORT_NAME)
        if not isinstance(report, dict):
            raise ReportCatalogError(
                f"{SEMANTIC_REPORT_NAME} is not a JSON object in {eval_dir}"
            )
        evaluator = report.get("evaluator")
        evaluator = evaluator if isinstance(evaluator, dict) else {}
        target_source = _str_field(report, "target_source")
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
            repo_path=_evidence_repo_path(eval_dir),
            task_id=task_id,
            evaluation_id=evaluation_id,
            generated_ts=_str_field(report, "generated_ts"),
            target_source=target_source,
            archived=target_source == SOURCE_ARCHIVED,
            backend=_str_field(evaluator, "backend"),
            model=_str_field(evaluator, "model"),
            schema_version=_str_field(evaluator, "schema_version"),
            artifacts=tuple(
                _artifact_digest(eval_dir / name) for name in REQUIRED_ARTIFACTS
            ),
            provenance=_provenance(eval_dir),
        )

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
        """Load every valid catalog line, ascending by ``seq``. Lock held.

        A missing catalog is an empty list (nothing published yet), not an error.
        Lines that are blank, unparseable, not objects, or missing the identity
        fields an entry must have (``seq``/``report_key``) are skipped: a torn or
        hand-edited line must not make the rest of the catalog unreadable.

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


def _is_complete(eval_dir: Path) -> bool:
    """Whether an evaluation dir holds the full L1+L2 report artifact set.

    The eligibility predicate, and the reason a failed semantic job or a
    ``/metrics``-only generation never appears as a ready report: the semantic
    evaluator writes all four files as one all-or-nothing dir, so their joint
    presence is exactly the "durably complete" condition, checked on the
    filesystem rather than inferred from job state.
    """
    return all((eval_dir / name).is_file() for name in REQUIRED_ARTIFACTS)


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


def _read_json(path: Path) -> object:
    """Parse one artifact JSON, raising :class:`ReportCatalogError` on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ReportCatalogError(f"unreadable {path.name} ({err})") from err


def _evidence_repo_path(eval_dir: Path) -> Path | None:
    """The evaluated repo's absolute path, read from the evidence pack's target.

    Best-effort and deliberately non-fatal: the repo path is consumer-facing
    context for cohorting, not part of the entry's identity (which is the
    slug/task/evaluation triple), so a degraded evidence pack yields ``None``
    rather than blocking publication of a perfectly good report.
    """
    try:
        evidence = _read_json(eval_dir / EVIDENCE_NAME)
    except ReportCatalogError:
        return None
    if not isinstance(evidence, dict):
        return None
    target = evidence.get("target")
    if not isinstance(target, dict):
        return None
    raw = _str_field(target, "repo_path")
    return Path(raw) if raw else None


def _provenance(eval_dir: Path) -> ReportProvenance:
    """Derive cohorting provenance from the evaluation's own evidence pack.

    Only durable artifact content is consulted, so the record is reproducible
    from what the entry names. What the pack cannot answer is stated as
    explicitly unavailable *with a reason* — ``protocol`` and
    ``orchestrator_config`` always, and ``runs``/``git`` when the pack is missing
    or degraded — so a consumer never mistakes an absent field for a known-empty
    one. An unreadable pack is not fatal: the report itself is still publishable,
    just less cohortable.
    """
    unavailable = ReportProvenance(
        runs_detail="evidence pack unreadable",
        git_detail="evidence pack unreadable",
        protocol_detail=PROTOCOL_UNAVAILABLE_DETAIL,
        config_detail=CONFIG_UNAVAILABLE_DETAIL,
    )
    try:
        evidence = _read_json(eval_dir / EVIDENCE_NAME)
    except ReportCatalogError:
        return unavailable
    if not isinstance(evidence, dict):
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
    git = evidence.get("git")
    git = git if isinstance(git, dict) else {}
    commits = git.get("commits")
    commits = commits if isinstance(commits, list) else []
    return ReportProvenance(
        runs_available=runs_available,
        run_ids=run_ids,
        runs_detail=None if runs_available else "evidence pack carries no run list",
        git_available=bool(git.get("available")),
        git_since=_str_field(git, "since"),
        git_until=_str_field(git, "until"),
        git_commit_count=len(commits),
        # ``git log`` is reverse-chronological, so the window's first commit is
        # its newest — roughly the repo state the task's runs ended at.
        git_newest_sha=_str_field(commits[0], "hash") if commits else None,
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
