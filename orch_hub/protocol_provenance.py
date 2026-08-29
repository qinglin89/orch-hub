"""Per-run protocol provenance: the deployed protocol identity a run ran under.

``mandrel`` deploys the canonical protocol payload from its own repository into
each managed repo and writes two receipts at its root. ``.ai-deploy-lock.json``
names the payload: ``source_git_commit`` (the mandrel commit whose canonical
tree those bytes correspond to) and ``canonical_payload_sha256`` (a
digest over the payload's file list). Both are branch-independent — a branch name
moves, and two repos on the same branch can hold different payloads — which is
what makes the commit usable as the cohort key a downstream consumer groups
evaluation reports by. ``.ai-deploy-manifest.json`` lists what actually landed:
one record per deployed file, with the digest of the bytes written to the target.

The two lock fields travel together as one :class:`ProtocolIdentity` and are
published only together. Neither is derivable from the other — the commit names a
source tree, the digest names the file set taken from it — so publishing one
alone would let a consumer cohort by a revision whose payload the hub never
confirmed, which is the same false membership a mixed run set produces.

The lock is **live target state**, and that is the first problem this module
exists to solve: by the time a task is evaluated, the repo has usually been
redeployed, so reading the lock at evaluation time would attribute today's
identity to a run that used another one. The hub therefore stamps the lock into
the hub-owned run dir at launch, before the orchestrator is spawned, and every
downstream reader takes the identity from that durable artifact. Provenance is
never backfilled from the tree's current lock — a run whose launch capture failed
has *no* identity, not the one deployed later.

A receipt is not the tree, and that is the second problem. The lock says what a
deploy put there; nothing in it says the files are still what it put. Hand-editing
a deployed protocol file — which the protocol itself forbids, and which is
therefore exactly the case worth catching — leaves both receipts untouched, so a
pre/post lock comparison sees a deployment that never moved while the run's later
sessions read text no revision holds. An identity is only worth as much as a
comparison against the *actual* deployed files at a boundary the hub can trust.

There are two such boundaries, checked by different mechanisms:

- **Launch** — the readiness gate. ``mandrel status`` compares every deployed file
  against the manifest (and the manifest against the current canonical tree) and
  exits nonzero on any drift, so a run exists at all only because its repo's
  files matched their receipt immediately before the capture
  (:meth:`orch_hub.runs.RunManager.start_run` gates on the whole report). Nothing
  is re-validated at capture: a second, weaker copy of a check that already
  blocked the launch would add only a way for the two to disagree.
- **End** — :func:`verify_payload`, the hub's own bounded receipt-to-target
  comparison, run at the live->ended transition of a run whose end the hub
  observed itself.

Between them nothing is watched. A file edited after the launch gate and restored
before the end check passes both, and this module claims nothing about that: a
``stable`` verdict states exactly what was observed — the deployed files matched
their receipt at the two boundaries, and the receipt named one identity
throughout — which is the strongest claim two point observations support.

The end check reads the *manifest*, not the lock, because only the manifest
vouches for the deployed bytes: the lock hashes each file's canonical source, and
``mandrel`` renders a few of them into their target (this repo's ``CLAUDE.md``
among them), so a target-versus-lock comparison would report drift on every
deploy. The two receipts are joined by their shared ``source_git_commit``; a
receipt naming a different one describes a different deploy and is refused
rather than used. The lock's aggregate digest is deliberately *not* recomputed
from its own file list: that formula is ``mandrel``'s internal contract, and
re-deriving it here would make the next lock-schema change read as a damaged
deployment.

*Which* files the payload consists of is the lock's statement, though
(``deployed_files[].target_relative_path``), because the lock is where the
identity comes from; the manifest only vouches for each file's bytes. The two
file sets are therefore compared before anything is hashed. A manifest missing
one of the lock's files — or naming one target twice, which hashes that file
twice and leaves another unchecked — would otherwise let every remaining row
hash cleanly and report a match over a strict subset of the payload the identity
names, which is the same defect as a silently truncated bound: an identity backed
by less than it claims.

The artifact (``protocol.json``, beside ``sessions.json``) is deliberately not a
field of ``run.json``: the record's shape stays stable for readers that predate
this, and the end-of-run verdict below is written without rewriting the record.

Two writes, ordered by the run's own state machine:

- **Launch capture** (:func:`capture_protocol`) — validates and freezes the
  identity pair. Whatever it captured is immutable from then on.
- **End check** (:func:`settle_protocol`) — re-reads the lock at the live->ended
  transition, then compares the deployed files against the manifest, and records
  only a verdict: the deployment can be updated while a run is in flight, or its
  files edited under it, in which case the run's sessions did not all use one
  protocol payload and the run must not be published as if they had. It never
  establishes or replaces a captured identity, and it runs exactly once —
  ``checked_ts`` marks that it ran, so a later re-check cannot mistake a post-run
  redeployment for a mid-run one.

Publication is therefore narrow by construction: a run carries a publishable
identity only once the end check has run *and* found both the lock unchanged and
the payload intact (:meth:`ProtocolSnapshot.verified_identity`). A capture on its
own attests to the run's first moment, not to what its later sessions loaded, so
a run that is still live, whose receipts had vanished by its end, or whose end the
hub was not up to observe keeps its identity readable for audit and publishes
nothing.

Every one of those outcomes except the first is *terminal*. The end check runs
once and a run ends once, so a settled verdict is the last thing this module will
ever say about that run: an absent identity there is decided, not pending. In
``stability`` alone the two are indistinguishable — a live run and a run whose end
could not be compared both read ``unverified`` — which is why
:attr:`ProtocolSnapshot.coverage` exists: the four ``stability`` values with that
one ambiguity resolved by ``checked_ts``, plus the two states that precede a
stability at all (a capture that found no usable lock, and a run predating the
capture entirely). A surface answering "how many runs carry an identity, and why
do the rest not" reads that one field rather than re-deriving the rule from three,
for the same reason :meth:`ProtocolSnapshot.verified_identity` is a property here
and not a condition each consumer writes.

A *report*, though, is about a task, and a task is worked by several runs. The
same rule therefore applies once more across them
(:func:`effective_protocol`): the identity an evaluation report is published
under is the one every contributing run verified, and a set that is incomplete or
disagrees has no effective identity — only a reason. It aggregates over the whole
pair, not over its revision half: two runs can name one commit and still have
verified different payloads (a partial redeploy, a receipt rewritten between
them), and publishing the commit they share would state exactly the false cohort
membership the pair exists to prevent. Both the per-run predicate and the
cross-run aggregate live here so the rule has one owner; a consumer reads the
answer, never re-derives it from the parts.

Because the two writes never overlap, this artifact needs no cross-writer lock
(unlike :mod:`orch_hub.task_snapshots`, whose captures genuinely race); the
atomic rename is enough to keep readers from seeing a partial file.

Capture does **not** gate launch. Readiness already fails closed on deployment
integrity (``manifest_exists``, ``mandrel status``), and provenance is evidence,
like commit metadata: a missing or malformed lock makes a run publish *no*
revision, with an operator-readable reason, rather than refusing the run. Cohort
admission policy belongs to the consumer of the report feed, not to the hub.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

DEPLOY_LOCK_NAME = ".ai-deploy-lock.json"
"""The deployed protocol's lock file, at the root of a managed repo."""

DEPLOY_MANIFEST_NAME = ".ai-deploy-manifest.json"
"""The deploy's per-file receipt, beside the lock. Its ``files`` records carry the
digest of the bytes that reached the *target*, which is what the end check
compares the tree against (the lock hashes canonical sources instead)."""

ARTIFACT_NAME = "protocol.json"
"""The per-run capture, written beside ``run.json`` in the hub-owned run dir."""

PROTOCOL_SCHEMA_VERSION = 2
"""Shape *and meaning* of :meth:`ProtocolSnapshot.to_dict`. Stamped on the
artifact so a later change stays separable, exactly like the evidence pack's.
Schema 1 wrote a ``stable`` verdict from a lock-only comparison; schema 2 requires
the deployed files to have been compared against their receipt as well, which is
why :func:`_from_dict` refuses to read an older verdict as this one.

Adding a *derived* key (``coverage``) is deliberately not a bump. Nothing
persisted changed meaning, the reader re-derives it, and a bump would demote every
existing ``stable`` verdict through the same older-schema clause — costing real
identities to record an addition that took none away."""

LOCK_MAX_BYTES = 4 * 1024 * 1024
"""Read cap for the lock file. It carries one record per deployed file (~36KB for
the ~120-file payload), so the cap leaves two orders of magnitude of headroom
while keeping a pathological file from being parsed into memory."""

MANIFEST_MAX_BYTES = 4 * 1024 * 1024
"""Read cap for the manifest, for the same reason and with the same headroom: one
record per deployed file, ~43KB for the ~120-file payload."""

PAYLOAD_MAX_FILES = 512
"""How many deployed files the end check will compare (~120 today). *Either*
receipt listing more is refused rather than sampled: a partial comparison is not
a verification, and a bound that quietly truncated would publish an identity
backed by less than it claims. Raising it also widens what :func:`_from_dict`
accepts as a persisted count, which is the only place the two meet."""

PAYLOAD_MAX_FILE_BYTES = 4 * 1024 * 1024
"""Per-file read cap (largest deployed file today ~145KB). A record declaring more
is refused for the same reason."""

PAYLOAD_MAX_TOTAL_BYTES = 32 * 1024 * 1024
"""Aggregate read cap across the payload (~490KB today). Keeps the end check —
which runs inline on the run manager's live->ended transition — bounded no matter
what a receipt declares."""

MAX_LISTED_PATHS = 4
"""How many deployed files a drift ``detail`` names before summarizing. The whole
payload can differ at once (a re-render, a restored backup), and the reason has to
stay a sentence: it is persisted in the run artifact and carried into evidence."""

STABILITY_UNVERIFIED = "unverified"
"""No usable end-of-run comparison: the run is still live, its end check has not
run, or a receipt could not be read or trusted at its end (``detail`` says which).
The launch capture stands as audit data — this is an absence of contradiction,
not a defect — but nothing confirms it held for the whole run, so it publishes no
identity."""

STABILITY_STABLE = "stable"
"""The lock still named the captured identity when the run ended, and every
deployed file still matched its manifest record. The only state an identity may
be published from."""

STABILITY_CHANGED = "changed"
"""The deployment was updated while the run was in flight, so the run's sessions
did not all use the captured identity. Conservatively unusable for
publication."""

STABILITY_DRIFTED = "drifted"
"""The receipts still named the captured identity at the run's end, but the
deployed files no longer matched them: the payload was edited in place, so the
identity names bytes the run's later sessions did not read. Kept distinct from
:data:`STABILITY_CHANGED` because the two are different operator facts — a
redeployment is ordinary, an edited deployed copy is damage the protocol forbids
— and because only this one says the receipts are lying."""

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
"""A git commit id, SHA-1 today and SHA-256 for a repo that has migrated."""

_SHA256 = re.compile(r"[0-9a-f]{64}")

_STABILITIES = (
    STABILITY_UNVERIFIED,
    STABILITY_STABLE,
    STABILITY_CHANGED,
    STABILITY_DRIFTED,
)

COVERAGE_VERIFIED = "verified"
"""This run carries a publishable identity (``stability`` :data:`STABILITY_STABLE`
with both halves validated). The one value that answers "yes" to the coverage
question; every other one is a reason for "no"."""

COVERAGE_CHANGED = "changed"
"""The deployment was replaced mid-run (:data:`STABILITY_CHANGED`). An operator's
own action, and ordinary — but it costs this run, and therefore its task, an
identity."""

COVERAGE_DRIFTED = "drifted"
"""The deployed files were edited under the run (:data:`STABILITY_DRIFTED`).
Damage the protocol forbids and the receipts do not record; the only value here
that names a defect to go and fix."""

COVERAGE_UNCOMPARED = "uncompared"
"""The end check ran and could conclude nothing: the hub was not up to observe the
run's end, or a receipt was unreadable or untrustworthy at it. ``detail`` says
which. Kept apart from :data:`COVERAGE_PENDING` because it is *terminal* — one run
gets one look (:func:`settle_protocol`) — and a permanent absence shown as an
outstanding one invites an operator to wait for an answer that is not coming."""

COVERAGE_PENDING = "pending"
"""No end check is recorded yet. For a live run this is the ordinary in-flight
state and the only non-terminal value here. For a run that has already ended it
means the check did not run and will not — its transition has passed — so a
reader that knows the run's liveness (run history, the run record) reports it as
decided; this module holds the artifact only and cannot tell the two apart."""

COVERAGE_UNCAPTURED = "uncaptured"
"""The launch capture found no usable identity to freeze — a missing, oversize,
malformed, or damaged deploy lock — so there was never anything for an end check
to confirm. ``detail`` names the file and its defect. Distinct from
:data:`COVERAGE_ABSENT`: the hub looked."""

COVERAGE_ABSENT = "absent"
"""The run wrote no ``protocol.json`` at all: it was launched before this capture
existed. The one value :meth:`ProtocolSnapshot.coverage` cannot return, since
there is no snapshot to ask — :func:`coverage_of` supplies it. Never repairable:
provenance is not backfilled from a later deployment."""

COVERAGES = (
    COVERAGE_VERIFIED,
    COVERAGE_PENDING,
    COVERAGE_CHANGED,
    COVERAGE_DRIFTED,
    COVERAGE_UNCOMPARED,
    COVERAGE_UNCAPTURED,
    COVERAGE_ABSENT,
)
"""Every coverage value, in the order a reader should present them: the one that
publishes, the one still resolving, then the reasons it did not — operator action,
damage, and the three flavours of no evidence. Exported so a census and a UI
enumerate the same buckets in the same order instead of each hardcoding a list
that a new value would silently fall out of."""

_HASH_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ProtocolIdentity:
    """One deployed protocol, named atomically: its source revision *and* payload.

    The unit of publication. A consumer cohorting reports asks two questions of
    the same deployment — which canonical revision produced it, and which bytes
    that revision produced — and the answers are only jointly meaningful: the
    commit alone does not pin the payload (``mandrel`` refuses to state a commit for
    a payload that does not match that tree, but the hub reads a receipt, not the
    tree), and the digest alone names bytes with no place in any history. Carrying
    them as one value is what keeps a caller from publishing half a fact.

    Frozen and compared by value, so "the runs agree" is ``==`` over the pair
    rather than a field-by-field check each consumer writes for itself.
    """

    source_git_commit: str
    canonical_payload_sha256: str


PAYLOAD_MATCHED = "matched"
"""Every deployed file the receipt lists was present and byte-identical to it."""

PAYLOAD_MISMATCHED = "mismatched"
"""At least one deployed file is missing, resized, replaced, or no longer a
regular file. A positive finding: the tree is not what the receipt says."""

PAYLOAD_UNVERIFIABLE = "unverifiable"
"""The comparison could not be made — no manifest, one that describes another
deploy, a damaged record, a receipt past a bound, or a file that could not be
read. Deliberately distinct from :data:`PAYLOAD_MISMATCHED`: not knowing is not
the same as knowing the payload is wrong, and only one of them is a defect an
operator should go looking for."""


@dataclass(frozen=True)
class PayloadCheck:
    """One point-in-time comparison of a repo's deployed files against its receipt.

    Kept in its own vocabulary rather than reusing the ``stability`` values,
    because the two answer different questions: this is a *point* observation
    ("does the tree match its receipt now"), while ``stability`` is a *span*
    verdict about the whole run. Conflating them is precisely the error this
    module was extended to fix — two matching lock reads are not a stable payload.

    ``files`` is how many records were actually compared, and ``None`` when the
    check never reached the per-file loop. It makes the bound observable: a caller
    can tell a verdict backed by the whole payload from one backed by nothing.
    """

    state: str
    files: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ProtocolSnapshot:
    """One run's protocol provenance, as persisted in its ``protocol.json``.

    ``available`` is the single load-bearing flag: it is true only when both
    halves of the :class:`ProtocolIdentity` were read from a well-formed lock and
    validated, so a reader never has to re-decide whether a value is trustworthy.
    When it is false, ``detail`` says why in operator-readable terms and both
    fields are ``None`` — the unavailability is explicit, never an empty string a
    consumer could read as "no protocol" or "default". They are never carried one
    without the other: half an identity is not a weaker fact, it is a different
    one.

    ``source_repo`` and ``lock_schema_version`` are the lock's own bookkeeping,
    carried for audit but deliberately *not* part of the identity: the same commit
    and payload hash mean the same protocol text whatever the lock calls its
    source.

    ``captured_ts`` is when the launch capture ran, ``checked_ts`` when the end
    comparison ran (``None`` while it has not), and ``payload_files`` how many
    deployed files that comparison actually checked against the manifest (``None``
    when it never got that far). Everything above them is frozen at capture; the
    end check writes only ``stability``/``checked_ts``/``payload_files``/``detail``.
    """

    available: bool
    source_git_commit: str | None = None
    canonical_payload_sha256: str | None = None
    source_repo: str | None = None
    lock_schema_version: int | None = None
    captured_ts: str | None = None
    stability: str = STABILITY_UNVERIFIED
    checked_ts: str | None = None
    payload_files: int | None = None
    detail: str | None = None

    @property
    def verified_identity(self) -> ProtocolIdentity | None:
        """The identity this run may be published under, or ``None``.

        The one predicate downstream aggregation should use, so "which runs count
        as carrying an identity" is decided here rather than re-derived at each
        consumer. It needs a completed end check that found the deployment
        unchanged *and* its files intact: a launch capture attests only to the
        run's first moment, and a report publishes what the run ran under end to
        end. So a contradicted capture (:data:`STABILITY_CHANGED`), an edited
        payload (:data:`STABILITY_DRIFTED`), and an uncompared end
        (:data:`STABILITY_UNVERIFIED` — still live, a receipt unreadable at the
        end, or an end the hub was not up to observe) are alike here: each keeps
        its recorded identity readable for audit and publishes nothing. Absent
        evidence of one payload is not evidence of one payload.

        The pair is returned whole because it is published whole; a consumer that
        wants only the revision takes it from the pair rather than from a snapshot
        field that might not have been verified together with the digest.
        """
        if not self.available or self.stability != STABILITY_STABLE:
            return None
        if self.source_git_commit is None or self.canonical_payload_sha256 is None:
            # Unreachable through the constructors here (``available`` is only set
            # with both fields validated), and still checked: this is the property
            # that grants publication, and it must not depend on a caller having
            # built the record correctly.
            return None
        return ProtocolIdentity(
            source_git_commit=self.source_git_commit,
            canonical_payload_sha256=self.canonical_payload_sha256,
        )

    @property
    def coverage(self) -> str:
        """This run's identity coverage as a reader sees it: one of :data:`COVERAGES`.

        The reader-facing counterpart of :attr:`verified_identity`, which answers
        only *whether* a run publishes. A surface — a run row, a census — has to
        say why not, and the three fields that decide it (``available``,
        ``stability``, ``checked_ts``) do not read as one answer: ``unverified``
        covers both a run still in flight and one whose end check concluded
        nothing, and neither of the two states that precede a stability at all is
        expressible in ``stability``. Deriving that here keeps the mapping in the
        same place as the publication predicate, so a new value cannot mean one
        thing to the dashboard and another to a tally.

        Ordered most-decided first: an unusable capture is answered before the
        end check that never happened over it, and publication is asked of
        :attr:`verified_identity` rather than of ``stability`` directly, so no
        reader can be told "verified" about an identity that property would
        refuse.
        """
        if not self.available:
            return COVERAGE_UNCAPTURED
        if self.checked_ts is None:
            return COVERAGE_PENDING
        if self.verified_identity is not None:
            return COVERAGE_VERIFIED
        if self.stability == STABILITY_CHANGED:
            return COVERAGE_CHANGED
        if self.stability == STABILITY_DRIFTED:
            return COVERAGE_DRIFTED
        return COVERAGE_UNCOMPARED

    @property
    def verified_revision(self) -> str | None:
        """The revision half of :attr:`verified_identity`, or ``None``.

        A convenience for the revision-keyed aggregate, never an independent
        predicate: it is derived from the pair so that no path can publish a
        commit the hub did not verify together with its payload digest.
        """
        identity = self.verified_identity
        return identity.source_git_commit if identity is not None else None

    def to_dict(self) -> dict:
        return {
            "schema_version": PROTOCOL_SCHEMA_VERSION,
            "available": self.available,
            "source_git_commit": self.source_git_commit,
            "canonical_payload_sha256": self.canonical_payload_sha256,
            "source_repo": self.source_repo,
            "lock_schema_version": self.lock_schema_version,
            "captured_ts": self.captured_ts,
            "stability": self.stability,
            "checked_ts": self.checked_ts,
            "payload_files": self.payload_files,
            "coverage": self.coverage,
            "detail": self.detail,
        }


def coverage_of(snapshot: ProtocolSnapshot | None) -> str:
    """One run's coverage value, including for a run that captured nothing.

    The census entry point. :attr:`ProtocolSnapshot.coverage` cannot express
    :data:`COVERAGE_ABSENT` — there is no snapshot to ask — and ``None`` is
    exactly what :func:`read_protocol` returns for a run predating the capture, so
    every caller that tallies over run dirs would otherwise write the same
    ``None`` branch and be free to call it something else. It is deliberately not
    folded into ``read_protocol`` as a synthetic unavailable snapshot: absent and
    uncaptured are different operator facts (:data:`COVERAGE_ABSENT`), and that
    distinction is the reason the reader returns ``None`` in the first place.
    """
    return COVERAGE_ABSENT if snapshot is None else snapshot.coverage


@dataclass(frozen=True)
class EffectiveProtocol:
    """The protocol identity a set of runs may be published under, or why none is.

    The cross-run counterpart of :meth:`ProtocolSnapshot.verified_identity`,
    carried by an evidence pack and copied onto the catalog entry a consumer
    cohorts by. ``available`` is again the single load-bearing flag: when it is
    false both halves are ``None`` and ``detail`` says why in operator-readable
    terms, so "the runs disagreed" and "the hub never verified them" are never
    served as an absent field a consumer could read as "no protocol".

    The two halves are the :class:`ProtocolIdentity` the whole set verified, held
    as flat fields because this record is a *serialized* section rather than a
    comparison key — and set together or not at all, for the reason the identity
    is atomic in the first place.

    ``run_count`` and ``verified_count`` are kept beside the verdict so the shape
    of the failure is machine-readable without parsing ``detail``: ``0/0`` is a
    task with no runs, ``1/3`` an incomplete set, and ``3/3`` with no identity the
    disagreement case.

    Both fields are renamed by :meth:`to_dict`, because a consumer reading
    ``provenance.protocol`` needs what is redundant inside this record: that the
    revision is the *set's* and not one run's captured one, and that the digest is
    the one the deploy lock names for the payload (not a hash of the report, the
    pack, or the tree the hub walked).
    """

    available: bool
    revision: str | None = None
    payload_sha256: str | None = None
    run_count: int = 0
    verified_count: int = 0
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "effective_revision": self.revision,
            "deploy_lock_hash": self.payload_sha256,
            "run_count": self.run_count,
            "verified_count": self.verified_count,
            "detail": self.detail,
        }


MAX_LISTED_REVISIONS = 4
"""How many distinct revisions — or payload digests — a disagreement ``detail``
names before summarizing. Bounds the string a catalog line carries: both are full
object names and a task's run count has no ceiling, so the reason stays a
sentence rather than growing with the task."""


def effective_protocol(
    snapshots: Sequence[ProtocolSnapshot | None],
) -> EffectiveProtocol:
    """The one identity every contributing run verified, or an explained absence.

    ``snapshots`` is one entry per contributing run, in run order, each the run's
    persisted capture or ``None`` for a run that wrote none (one predating the
    capture). The rule is unanimity over
    :meth:`ProtocolSnapshot.verified_identity`, and it is deliberately the strict
    reading of "the protocol this task ran under": a report published under an
    identity two of its three runs used would state a cohort membership that is
    false for the third, and a consumer grouping by it cannot tell that from a
    true one. An incomplete or disagreeing set is worth less than an unavailable
    one only if the consumer is guessing — which the ``detail`` exists to stop.

    Unanimity is over the whole pair. A set whose runs verified one revision with
    two different payload digests has no effective identity either, and says so in
    the payload's own terms: the runs did not all read the same protocol text, and
    publishing the revision they happen to share would hand a consumer a cohort
    key for a payload half of them never ran under.

    Every failure mode that holds is reported: a set can be short of verified runs
    *and* have the verified ones disagree — about the revision, about the payload,
    or about both — and naming only the first would suggest the missing evidence
    is all that stands between the task and an identity.
    """
    total = len(snapshots)
    if not total:
        return EffectiveProtocol(
            available=False,
            detail="no related runs to take a protocol identity from",
        )
    identities = [
        snapshot.verified_identity
        for snapshot in snapshots
        if snapshot is not None and snapshot.verified_identity is not None
    ]
    distinct = sorted(
        set(identities),
        key=lambda identity: (
            identity.source_git_commit,
            identity.canonical_payload_sha256,
        ),
    )
    reasons = []
    if len(identities) < total:
        reasons.append(
            f"{total - len(identities)} of {total} contributing runs carry no"
            " verified protocol identity"
        )
    if len(distinct) > 1:
        # Two identities differ in at least one half, so at least one of these
        # holds. Which one is the operator's whole question: different revisions
        # are a redeployment mid-task, different payloads under one revision are a
        # tree that stopped matching what the revision deployed.
        revisions = sorted({item.source_git_commit for item in distinct})
        payloads = sorted({item.canonical_payload_sha256 for item in distinct})
        if len(revisions) > 1:
            reasons.append(
                "contributing runs verified different protocol revisions: "
                + _listed(revisions, MAX_LISTED_REVISIONS)
            )
        if len(payloads) > 1:
            reasons.append(
                "contributing runs verified different protocol payloads: "
                + _listed(payloads, MAX_LISTED_REVISIONS)
            )
    if reasons:
        return EffectiveProtocol(
            available=False,
            run_count=total,
            verified_count=len(identities),
            detail="; ".join(reasons),
        )
    return EffectiveProtocol(
        available=True,
        revision=distinct[0].source_git_commit,
        payload_sha256=distinct[0].canonical_payload_sha256,
        run_count=total,
        verified_count=len(identities),
    )


LEGACY_AGGREGATE_DETAIL = (
    "recorded by an earlier hub that published a protocol revision on its own:"
    " the payload digest the revision must be published with is not in this"
    " record"
)
"""Why a pre-pair aggregate does not publish. The counterpart of
:data:`LEGACY_VERDICT_DETAIL` one level up: evidence packs before schema 18 and
the catalog lines built from them carry ``effective_revision`` alone, and half an
identity is not a weaker cohort key — it is one whose payload nothing here
verified. Named because every report published before this is read through it."""


def effective_protocol_from_dict(data: object) -> EffectiveProtocol:
    """Read a persisted aggregate section fail-closed.

    The reader for a consumer that holds the serialized section rather than the
    :class:`EffectiveProtocol`: the report catalog, which builds an entry from
    ``evidence.json`` as JSON, and its own round-trip of the catalog line that
    entry was written to. It lives here so the availability rule keeps one owner —
    a second implementation at the catalog would be free to drift into publishing
    an identity this module would not, and the demotions below are exactly where
    that would happen quietly.

    Absence is distinguished from damage, because they mean different things to an
    operator: a missing section is a record written before records carried one,
    while a present-but-wrong section is one something rewrote. An unavailable
    section's own reason is carried through verbatim — it already names which runs
    fell short — and a section claiming availability without a well-formed object
    name is refused rather than published as a cohort key no consumer can resolve.

    A section carrying a revision but no payload digest gets the same treatment
    for the same reason, and is the common case rather than a defect: everything
    written before the identity became a pair looks like that
    (:data:`LEGACY_AGGREGATE_DETAIL`). Its revision is *not* completed from
    anything — not the runs, not the repo's current lock — because the digest
    those would supply is not one this record's runs were shown to have used.
    """
    if data is None:
        return EffectiveProtocol(
            available=False,
            detail="no protocol provenance section was persisted",
        )
    if not isinstance(data, dict):
        return EffectiveProtocol(
            available=False,
            detail="persisted protocol provenance is not a JSON object",
        )
    runs = _int_field(data, "run_count") or 0
    verified = _int_field(data, "verified_count") or 0
    if data.get("available") is not True:
        return EffectiveProtocol(
            available=False,
            run_count=runs,
            verified_count=verified,
            detail=_str_field(data, "detail") or "protocol provenance unavailable",
        )
    revision = _hex_field(data, "effective_revision", _SHA)
    if revision is None:
        return EffectiveProtocol(
            available=False,
            run_count=runs,
            verified_count=verified,
            detail="persisted protocol provenance claims a revision it does not carry",
        )
    payload = _hex_field(data, "deploy_lock_hash", _SHA256)
    if payload is None:
        return EffectiveProtocol(
            available=False,
            run_count=runs,
            verified_count=verified,
            detail=(
                LEGACY_AGGREGATE_DETAIL
                if "deploy_lock_hash" not in data
                else "persisted protocol provenance claims a payload digest it"
                " does not carry"
            ),
        )
    return EffectiveProtocol(
        available=True,
        revision=revision,
        payload_sha256=payload,
        run_count=runs,
        verified_count=verified,
        detail=_str_field(data, "detail"),
    )


def artifact_path(run_dir: Path) -> Path:
    return run_dir / ARTIFACT_NAME


def deploy_lock_path(repo_path: Path) -> Path:
    return repo_path / DEPLOY_LOCK_NAME


def manifest_path(repo_path: Path) -> Path:
    return repo_path / DEPLOY_MANIFEST_NAME


def _read_receipt(path: Path, cap: int) -> tuple[dict | None, str | None]:
    """Bounded read of one deploy receipt: ``(object, None)`` or ``(None, reason)``.

    Shared by the lock and the manifest so both fail the same way and neither can
    quietly grow a second policy for an oversize or unparseable receipt. Never
    raises — every reason is a sentence naming the file, because both callers turn
    it into operator-readable unavailability rather than an error.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(cap + 1)
    except FileNotFoundError:
        return None, f"no {path.name} in {path.parent}"
    except OSError as err:
        return None, f"unreadable {path.name} ({err})"
    if len(raw) > cap:
        return None, f"{path.name} exceeds the {cap}-byte read cap"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        return None, f"unparseable {path.name} ({err})"
    if not isinstance(data, dict):
        return None, f"{path.name} is not a JSON object"
    return data, None


def read_deploy_lock(repo_path: Path) -> ProtocolSnapshot:
    """Read and validate a repo's deployment lock into an unstamped snapshot.

    Never raises: every failure mode — absent file, unreadable path, oversize
    blob, non-JSON, non-object, missing or malformed identity fields — becomes an
    unavailable snapshot naming the reason. A launch must not fail because a
    display/evidence artifact could not be built, and the end check must be able
    to report "could not look" as distinct from "changed".

    Validation is what makes ``available`` mean something: a commit id that is not
    a hex object name, or a payload hash that is not SHA-256 shaped, is refused
    rather than published as a cohort key that no consumer can resolve. Both
    fields are required together — an ``available`` snapshot always carries a
    whole :class:`ProtocolIdentity`, never half of one.
    """
    data, error = _read_receipt(deploy_lock_path(repo_path), LOCK_MAX_BYTES)
    if error is not None or data is None:
        return _unavailable(error or f"unreadable {DEPLOY_LOCK_NAME}")
    commit = _hex_field(data, "source_git_commit", _SHA)
    if commit is None:
        return _unavailable(
            f"{DEPLOY_LOCK_NAME} has no valid source_git_commit"
        )
    payload = _hex_field(data, "canonical_payload_sha256", _SHA256)
    if payload is None:
        return _unavailable(
            f"{DEPLOY_LOCK_NAME} has no valid canonical_payload_sha256"
        )
    return ProtocolSnapshot(
        available=True,
        source_git_commit=commit,
        canonical_payload_sha256=payload,
        source_repo=_str_field(data, "source_repo"),
        lock_schema_version=_int_field(data, "schema_version"),
    )


def verify_payload(repo_path: Path, expected_commit: str) -> PayloadCheck:
    """Compare the repo's deployed files against the receipts for ``expected_commit``.

    The receipt-to-target half of the identity: the lock says *which* payload was
    deployed, and this says whether the tree still holds it. Never raises — every
    failure is a state with a reason, because the caller is settling a run that has
    already ended and must record what it could see, not fail.

    ``expected_commit`` is the captured run's commit, and *both* receipts must name
    it. That join is what makes the comparison say something about the *captured*
    identity: a receipt from a different deploy would have the hub hashing files
    against a list the run never ran under, and answering "matched" to a question
    nobody asked. The lock is re-read here rather than taken from the caller, so
    the join is established from the receipts themselves and this predicate carries
    no precondition a caller could forget.

    The payload's file set comes from the lock and the manifest must match it
    exactly (see the module header): a comparison over the manifest's rows alone
    verifies whatever subset the manifest happens to list. Paths are compared as
    the receipts spell them — ``mandrel`` writes the same string in both — so a
    receipt that respelled one fails closed rather than being normalized into an
    agreement it does not state.

    Bounded at every producer (:data:`PAYLOAD_MAX_FILES`,
    :data:`PAYLOAD_MAX_FILE_BYTES`, :data:`PAYLOAD_MAX_TOTAL_BYTES`,
    :data:`MANIFEST_MAX_BYTES`) and refused — not truncated — past any of them:
    this runs inline on the run manager's live->ended transition, and a partial
    comparison reported as a match would publish an identity backed by less than
    it claims.

    Files are opened ``O_NOFOLLOW`` and required to be regular: a deploy writes
    regular files, so a link where one belongs is a mismatch, not a path to
    follow. Interior directory hops are *not* walked no-follow the way the
    evaluation tree's are — nothing here leaves the process (a digest is compared,
    never served), so containment buys no more than the leaf check already gives,
    and 120 files' worth of descriptor walking would buy it at real cost. Modes
    are deliberately not compared either: this answers what the run's sessions
    *read*, and an executable bit is not protocol text.
    """
    lock, error = _read_receipt(deploy_lock_path(repo_path), LOCK_MAX_BYTES)
    if error is not None or lock is None:
        return PayloadCheck(
            PAYLOAD_UNVERIFIABLE, detail=error or f"unreadable {DEPLOY_LOCK_NAME}"
        )
    if _hex_field(lock, "source_git_commit", _SHA) != expected_commit:
        return PayloadCheck(
            PAYLOAD_UNVERIFIABLE,
            detail=f"{DEPLOY_LOCK_NAME} no longer names the deploy this run captured",
        )
    expected, error = _deployed_targets(lock)
    if expected is None or error is not None:
        return PayloadCheck(
            PAYLOAD_UNVERIFIABLE, detail=error or f"unusable {DEPLOY_LOCK_NAME}"
        )
    data, error = _read_receipt(manifest_path(repo_path), MANIFEST_MAX_BYTES)
    if error is not None or data is None:
        return PayloadCheck(
            PAYLOAD_UNVERIFIABLE, detail=error or f"unreadable {DEPLOY_MANIFEST_NAME}"
        )
    commit = _hex_field(data, "source_git_commit", _SHA)
    if commit != expected_commit:
        return PayloadCheck(
            PAYLOAD_UNVERIFIABLE,
            detail=(
                f"{DEPLOY_MANIFEST_NAME} describes a different deploy than"
                f" {DEPLOY_LOCK_NAME} (manifest {commit or 'has no valid commit'})"
            ),
        )
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        return PayloadCheck(
            PAYLOAD_UNVERIFIABLE,
            detail=f"{DEPLOY_MANIFEST_NAME} lists no deployed files",
        )
    if len(files) > PAYLOAD_MAX_FILES:
        return PayloadCheck(
            PAYLOAD_UNVERIFIABLE,
            detail=(
                f"{DEPLOY_MANIFEST_NAME} lists {len(files)} files, past the"
                f" {PAYLOAD_MAX_FILES}-file check cap"
            ),
        )
    records = []
    listed: set[str] = set()
    total = 0
    for key, record in sorted(files.items(), key=lambda item: str(item[0])):
        parsed = _payload_record(key, record)
        if parsed is None:
            return PayloadCheck(
                PAYLOAD_UNVERIFIABLE,
                detail=(
                    f"{DEPLOY_MANIFEST_NAME} has an unusable record for"
                    f" {str(key)!r}"
                ),
            )
        if parsed[0] in listed:
            # Two rows pointing at one target hash that file twice and leave the
            # other unchecked, which is drift-hiding rather than a duplicate fact.
            return PayloadCheck(
                PAYLOAD_UNVERIFIABLE,
                detail=(
                    f"{DEPLOY_MANIFEST_NAME} records {parsed[0]!r} more than once"
                ),
            )
        listed.add(parsed[0])
        total += parsed[2]
        if total > PAYLOAD_MAX_TOTAL_BYTES:
            return PayloadCheck(
                PAYLOAD_UNVERIFIABLE,
                detail=(
                    f"{DEPLOY_MANIFEST_NAME} declares more than the"
                    f" {PAYLOAD_MAX_TOTAL_BYTES}-byte check cap"
                ),
            )
        records.append(parsed)
    if listed != expected:
        parts = []
        absent = sorted(expected - listed)
        added = sorted(listed - expected)
        if absent:
            parts.append(f"{DEPLOY_MANIFEST_NAME} does not vouch for {_listed(absent)}")
        if added:
            parts.append(f"{DEPLOY_MANIFEST_NAME} adds {_listed(added)}")
        return PayloadCheck(
            PAYLOAD_UNVERIFIABLE,
            detail=(
                "the deploy receipts disagree about which files the payload is: "
                + "; ".join(parts)
            ),
        )
    mismatched: list[str] = []
    unreadable: list[str] = []
    for relative, digest, size in records:
        state, reason = _compare_deployed_file(repo_path, relative, digest, size)
        if state == PAYLOAD_MISMATCHED:
            mismatched.append(reason)
        elif state == PAYLOAD_UNVERIFIABLE:
            unreadable.append(reason)
    if mismatched:
        # A definite finding outranks an incomplete one: files that could not be
        # read are still named, but the tree is already known not to match.
        return PayloadCheck(
            PAYLOAD_MISMATCHED,
            files=len(records),
            detail=(
                f"deployed files no longer match {DEPLOY_MANIFEST_NAME}: "
                + _listed(mismatched)
                + (f"; unreadable: {_listed(unreadable)}" if unreadable else "")
            ),
        )
    if unreadable:
        return PayloadCheck(
            PAYLOAD_UNVERIFIABLE,
            files=len(records),
            detail=f"deployed files could not be read: {_listed(unreadable)}",
        )
    return PayloadCheck(PAYLOAD_MATCHED, files=len(records))


def _deployed_targets(lock: dict) -> tuple[frozenset[str] | None, str | None]:
    """The complete set of target paths the lock says its payload consists of.

    ``(None, reason)`` whenever the lock cannot establish that set, which makes the
    whole comparison unverifiable: without it the hub knows only what the manifest
    chose to list, and "every row matched" would be a claim about the manifest
    rather than about the payload. A lock naming one target twice is refused for
    the same reason the manifest is — a set the receipt states ambiguously is not
    one the hub can hold the tree to.

    Only ``target_relative_path`` is read. The canonical digests beside it hash the
    payload's *sources*, which ``mandrel`` renders per target for a few files, so
    they say nothing about the deployed bytes (that is the manifest's job).
    """
    deployed = lock.get("deployed_files")
    if not isinstance(deployed, list) or not deployed:
        return None, f"{DEPLOY_LOCK_NAME} lists no deployed files"
    if len(deployed) > PAYLOAD_MAX_FILES:
        return None, (
            f"{DEPLOY_LOCK_NAME} lists {len(deployed)} files, past the"
            f" {PAYLOAD_MAX_FILES}-file check cap"
        )
    targets: set[str] = set()
    for record in deployed:
        relative = (
            _str_field(record, "target_relative_path")
            if isinstance(record, dict)
            else None
        )
        if relative is None or not _is_contained_relative(relative):
            return None, f"{DEPLOY_LOCK_NAME} has an unusable deployed-file record"
        if relative in targets:
            return None, f"{DEPLOY_LOCK_NAME} names {relative!r} more than once"
        targets.add(relative)
    return frozenset(targets), None


def _payload_record(key: object, record: object) -> tuple[str, str, int] | None:
    """One manifest record as ``(relative path, sha256, size)``, or ``None``.

    ``None`` is a record this check cannot use, which makes the whole comparison
    unverifiable rather than partial. The path must be repo-relative and free of
    ``..``: a receipt naming anything else is damaged, and the honest answer is
    that the payload could not be verified — not a hash of a file outside the tree.

    ``target_relative_path`` wins over the dict key when both are present, matching
    what ``mandrel``'s own status check does with the same records.
    """
    if not isinstance(record, dict):
        return None
    relative = _str_field(record, "target_relative_path") or (
        key if isinstance(key, str) and key else None
    )
    if relative is None or not _is_contained_relative(relative):
        return None
    digest = _hex_field(record, "sha256", _SHA256)
    if digest is None:
        return None
    size = _int_field(record, "size_bytes")
    if size is None or size < 0 or size > PAYLOAD_MAX_FILE_BYTES:
        return None
    return relative, digest, size


def _is_contained_relative(relative: str) -> bool:
    path = Path(relative)
    if path.is_absolute() or path.drive or path.anchor:
        return False
    return all(part not in ("", os.curdir, os.pardir) for part in path.parts)


def _compare_deployed_file(
    repo_path: Path, relative: str, digest: str, size: int
) -> tuple[str | None, str]:
    """``(state, reason)`` for one deployed file; ``state`` is ``None`` when it matches.

    Size is checked from the open descriptor before any bytes are hashed: it is the
    same fact as the digest for a file whose recorded length differs, and skipping
    the read keeps a receipt that under-declares a huge file from costing more than
    a stat.
    """
    try:
        handle = os.open(repo_path / relative, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return PAYLOAD_MISMATCHED, f"{relative} (missing)"
    except OSError as err:
        if err.errno in (errno.ELOOP, errno.EMLINK):
            return PAYLOAD_MISMATCHED, f"{relative} (not a regular file)"
        return PAYLOAD_UNVERIFIABLE, f"{relative} ({err.strerror or err})"
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            return PAYLOAD_MISMATCHED, f"{relative} (not a regular file)"
        if info.st_size != size:
            return (
                PAYLOAD_MISMATCHED,
                f"{relative} ({info.st_size} bytes, receipt says {size})",
            )
        actual = _digest_fd(handle, size)
    except OSError as err:
        return PAYLOAD_UNVERIFIABLE, f"{relative} ({err.strerror or err})"
    finally:
        os.close(handle)
    if actual is None:
        return PAYLOAD_MISMATCHED, f"{relative} (shorter than the receipt says)"
    if actual != digest:
        return PAYLOAD_MISMATCHED, f"{relative} (content differs)"
    return None, relative


def _digest_fd(handle: int, size: int) -> str | None:
    """SHA-256 of exactly ``size`` bytes from ``handle``, or ``None`` if it ends early.

    Reads to the stat'ed length rather than to EOF, so a file being appended to
    while it is hashed still costs a bounded read.
    """
    digest = hashlib.sha256()
    remaining = size
    while remaining > 0:
        chunk = os.read(handle, min(_HASH_CHUNK, remaining))
        if not chunk:
            return None
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _listed(values: Sequence[str], limit: int = MAX_LISTED_PATHS) -> str:
    """Bounded join: the detail lands in an artifact and a catalog line.

    One helper for both bounds — deployed paths and, at the aggregate,
    revisions/payload digests (:data:`MAX_LISTED_REVISIONS`) — so a caller cannot
    add a list that grows with the task by forgetting to cap it.
    """
    shown = ", ".join(values[:limit])
    extra = len(values) - limit
    return shown + (f" (+{extra} more)" if extra > 0 else "")


def capture_protocol(
    run_dir: Path, repo_path: Path, *, now: dt.datetime
) -> ProtocolSnapshot:
    """Freeze ``repo_path``'s protocol identity into ``run_dir`` at launch.

    Called after the readiness gates and before the orchestrator is spawned, so
    the captured identity is the one the run's first session actually loads —
    and, because those gates include ``mandrel status``, one whose deployed files
    were compared against their receipt moments earlier. That gate is the launch
    boundary; nothing is re-hashed here.

    The read cannot fail the launch (see :func:`read_deploy_lock`); the *write*
    can, and is meant to — a just-created run dir that cannot hold a few hundred
    bytes cannot hold ``run.json`` either, and the caller's existing cleanup
    removes the dir and refuses the launch, which is the same handling a failed
    spawn gets.
    """
    snapshot = replace(read_deploy_lock(repo_path), captured_ts=now.isoformat())
    write_protocol(run_dir, snapshot)
    return snapshot


UNOBSERVED_END_DETAIL = (
    "run ended while the hub was down, so the deployment could not be compared at"
    " its end; one run gets one such comparison, so this run can never publish a"
    " protocol identity"
)
"""Why a run the hub did not see end publishes nothing, *and* that the answer is
final. The permanence is stated rather than implied because this is the reason an
operator meets most often — every hub restart across a live run produces one — and
a bare "could not be compared" reads as a check still owed. The rationale for the
one-look rule, and what evidence a second look would have needed, is in
:func:`settle_protocol`."""


def settle_protocol(
    run_dir: Path,
    repo_path: Path,
    *,
    now: dt.datetime,
    end_observed: bool = True,
) -> ProtocolSnapshot | None:
    """Check the deployment against the run's capture at its live->ended transition.

    Two comparisons, in order. The lock is re-read first: if it now names another
    identity, the deployment was replaced mid-run and there is nothing left to
    verify the payload *of*. Only an unchanged lock reaches
    :func:`verify_payload`, which is where an identity earns publication — a
    matching receipt at both ends says only that nobody redeployed, and the
    deployed files can have been edited under the run without either lock read
    noticing.

    Returns the settled snapshot, or ``None`` when there is nothing to settle: a
    run with no artifact (one predating this capture), a capture that carries no
    identity to contradict, or an end check that already ran.

    "Already ran" is keyed on ``checked_ts``, not on the verdict, and that is
    deliberate. An end check that could not read the lock leaves the stability
    ``unverified``; keying on the verdict would let a later pass re-check against
    a lock that has since been redeployed — turning an ordinary post-run
    deployment into a false mid-run ``changed``. One run, one look.

    ``end_observed`` is that same falsifiability rule at the other edge: this
    compares *now* against a capture from launch, which only says something about
    the run when ``now`` is close to the real end. It is for a run whose exit the
    hub saw itself (within a scan interval); a run that ended while the hub was
    down is observed at an unbounded lag, where "the deployment differs" no longer
    distinguishes a mid-run redeployment from any of the ones since. Such a run
    records that its end could not be compared rather than a verdict it cannot
    support. That buys no publication — an uncompared end publishes nothing
    either way — but it keeps the recorded verdict truthful: a false ``changed``
    tells an operator, in ``detail``, that the deployment moved under their run
    when nothing observed says it did.

    An unobserved end is therefore **permanent**, and that is a decision, not an
    omission: this hub will not look again, and the record says so
    (:data:`UNOBSERVED_END_DETAIL`) instead of leaving an absence that reads as
    outstanding. A second look would be admissible only against evidence that the
    deployment did not move between the run's real end and the late look, and that
    evidence has exactly one shape — a record of the repo's deployed identity at
    some moment inside that interval, written by something that watches the
    *deployment* rather than the run. Nothing writes one: this module reads a
    target's lock at a run's own two boundaries and nowhere else, and ``mandrel``
    leaves no deploy journal the hub reads. So the alternative is not "retry the
    check" but "keep a deployment history", a separate mechanism with its own
    truthfulness to establish — and one that would still say nothing about any run
    already recorded, since its evidence would start after them. Until it exists,
    an end nobody observed is uncompared for good, which is what
    :data:`COVERAGE_UNCOMPARED` reports and what a surface should show.

    Only the two identity fields are compared between the locks. A lock that
    renamed its source or bumped its own schema while naming the same commit and
    payload hash deployed the same protocol text, and reporting that as a change
    would cost a publishable report for no gain in truth.
    """
    snapshot = read_protocol(run_dir)
    if snapshot is None or not snapshot.available or snapshot.checked_ts is not None:
        return None
    payload_files: int | None = None
    if not end_observed:
        stability, detail = STABILITY_UNVERIFIED, UNOBSERVED_END_DETAIL
    else:
        current = read_deploy_lock(repo_path)
        stability, detail = _verdict(snapshot, current)
        if stability == STABILITY_STABLE:
            check = verify_payload(repo_path, snapshot.source_git_commit or "")
            payload_files = check.files
            stability, detail = _payload_verdict(check)
    settled = replace(
        snapshot,
        stability=stability,
        checked_ts=now.isoformat(),
        payload_files=payload_files,
        detail=detail,
    )
    write_protocol(run_dir, settled)
    return settled


def write_protocol(run_dir: Path, snapshot: ProtocolSnapshot) -> None:
    """Persist ``protocol.json`` atomically (per-writer tmp + rename).

    No cross-writer lock: the capture and the end check are ordered by the run's
    own lifecycle and never overlap. The tmp name still carries pid and thread id
    so a caller writing from outside that lifecycle (tests, a future tool) cannot
    collide on the tmp path.
    """
    path = artifact_path(run_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(snapshot.to_dict(), indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_protocol(run_dir: Path) -> ProtocolSnapshot | None:
    """Load a run's persisted capture, or ``None`` when it never wrote one.

    ``None`` is the legacy/absent answer — a run launched before this capture
    existed — and is deliberately distinct from an unavailable snapshot, which
    records that the hub looked and found no usable revision. A corrupt or
    internally inconsistent artifact resolves to the latter, not the former: the
    hub did capture something for this run, and claiming otherwise would invite a
    consumer to treat it as backfillable.
    """
    try:
        data = json.loads(artifact_path(run_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        return _unavailable(f"unreadable {ARTIFACT_NAME} ({err})")
    return _from_dict(data)


LEGACY_VERDICT_DETAIL = (
    "recorded by an earlier hub that compared only the deployment lock: the"
    " deployed files were never checked against their receipt"
)
"""Why a pre-payload-check ``stable`` verdict does not publish. Named because it is
the reason an operator will meet most often for a while — every run that ended
before this check existed carries it."""


def _from_dict(data: object) -> ProtocolSnapshot:
    """Rebuild a snapshot from persisted JSON, failing closed on any defect.

    Every path that cannot produce a *validated* identity yields an unavailable
    snapshot, so ``available`` keeps its meaning for every reader: an artifact
    claiming availability without a well-formed commit and payload hash, or
    carrying a stability value this version does not know (a hand-edited file, or
    one written by a newer hub), is unusable rather than partially trusted.

    ``stability`` is validated the same way and for the same reason: it is the
    field that grants publication (:meth:`ProtocolSnapshot.verified_identity`), so
    a verdict with no ``checked_ts`` — a conclusion recording no check that could
    have reached it, which :func:`settle_protocol` never writes — is refused
    rather than read as a confirmed identity. Under this schema a ``stable``
    verdict also names how many files it compared, and the count has to be one
    :func:`verify_payload` could have produced — it refuses an empty receipt and
    caps it at :data:`PAYLOAD_MAX_FILES`, so ``0``, a negative, or a larger count
    is a rewritten artifact, not a smaller verification. Both are the same class
    of defect as a verdict with no ``checked_ts``: evidence that contradicts the
    only process that writes it.

    The persisted ``coverage`` is not among the fields read back. It is derived
    (:attr:`ProtocolSnapshot.coverage`) and re-derived here from the three fields
    that decide it, so a hand-edited or stale one is ignored rather than trusted —
    and so a demotion below cannot leave a record whose verdict and whose reader
    label disagree.

    The schema stamp is read as *meaning*, not just shape, which is what keeps the
    older artifacts honest without touching them. A schema-1 ``stable`` was reached
    by comparing two lock reads, which this version does not accept as a verified
    payload, so it reads back ``unverified`` with :data:`LEGACY_VERDICT_DETAIL` —
    the identity stays readable for audit and publishes nothing. Its ``changed``
    and ``unverified`` verdicts need no such demotion: they published nothing
    already. A *newer* schema is refused outright, since a field this version
    reads may not mean here what it meant there.
    """
    if not isinstance(data, dict):
        return _unavailable(f"{ARTIFACT_NAME} is not a JSON object")
    version = _int_field(data, "schema_version")
    if version is None or version < 1:
        return _unavailable(f"{ARTIFACT_NAME} carries no usable schema version")
    if version > PROTOCOL_SCHEMA_VERSION:
        return _unavailable(
            f"{ARTIFACT_NAME} was written by a newer hub (schema {version})"
        )
    stability = data.get("stability")
    if stability not in _STABILITIES:
        return _unavailable(f"{ARTIFACT_NAME} has an unrecognized stability")
    commit = _hex_field(data, "source_git_commit", _SHA)
    payload = _hex_field(data, "canonical_payload_sha256", _SHA256)
    available = data.get("available") is True
    checked_ts = _str_field(data, "checked_ts")
    payload_files = _int_field(data, "payload_files")
    detail = _str_field(data, "detail")
    if available and (commit is None or payload is None):
        return _unavailable(f"{ARTIFACT_NAME} claims an identity it does not carry")
    if available and stability != STABILITY_UNVERIFIED and checked_ts is None:
        return _unavailable(f"{ARTIFACT_NAME} carries a verdict it never checked")
    if available and stability == STABILITY_STABLE:
        if version < PROTOCOL_SCHEMA_VERSION:
            stability, detail = STABILITY_UNVERIFIED, LEGACY_VERDICT_DETAIL
        elif payload_files is None:
            return _unavailable(
                f"{ARTIFACT_NAME} claims a verified payload it never counted"
            )
        elif not 1 <= payload_files <= PAYLOAD_MAX_FILES:
            return _unavailable(
                f"{ARTIFACT_NAME} claims a verified payload of {payload_files}"
                " files, which no check could have produced"
            )
    return ProtocolSnapshot(
        available=available,
        source_git_commit=commit if available else None,
        canonical_payload_sha256=payload if available else None,
        source_repo=_str_field(data, "source_repo"),
        lock_schema_version=_int_field(data, "lock_schema_version"),
        captured_ts=_str_field(data, "captured_ts"),
        stability=stability,
        checked_ts=checked_ts,
        payload_files=payload_files,
        detail=detail,
    )


def _payload_verdict(check: PayloadCheck) -> tuple[str, str | None]:
    """One :class:`PayloadCheck` as an end-of-run verdict and its reason.

    The point observation becomes a span verdict only here, and only because the
    caller has already established that the lock named one identity across the
    run: "matched, and nothing was redeployed" is what ``stable`` means.
    """
    if check.state == PAYLOAD_MATCHED:
        return STABILITY_STABLE, None
    if check.state == PAYLOAD_MISMATCHED:
        return STABILITY_DRIFTED, check.detail
    return (
        STABILITY_UNVERIFIED,
        f"deployed payload could not be verified at run end: {check.detail}",
    )


def _verdict(
    captured: ProtocolSnapshot, current: ProtocolSnapshot
) -> tuple[str, str | None]:
    """The lock-comparison verdict and its reason for one end-of-run check."""
    if not current.available:
        return (
            STABILITY_UNVERIFIED,
            f"deployment lock unreadable at run end: {current.detail}",
        )
    changes = [
        f"{field} {before} -> {after}"
        for field, before, after in (
            (
                "source_git_commit",
                captured.source_git_commit,
                current.source_git_commit,
            ),
            (
                "canonical_payload_sha256",
                captured.canonical_payload_sha256,
                current.canonical_payload_sha256,
            ),
        )
        if before != after
    ]
    if not changes:
        return STABILITY_STABLE, None
    return (
        STABILITY_CHANGED,
        "deployed protocol changed during the run: " + "; ".join(changes),
    )


def _unavailable(detail: str) -> ProtocolSnapshot:
    return ProtocolSnapshot(available=False, detail=detail)


def _hex_field(data: dict, key: str, pattern: re.Pattern[str]) -> str | None:
    """A lowercase-normalized hex digest field, or ``None`` when malformed."""
    value = data.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if pattern.fullmatch(normalized) else None


def _str_field(data: dict, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _int_field(data: dict, key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None
