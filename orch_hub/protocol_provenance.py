"""Per-run protocol provenance: the deployed protocol revision a run ran under.

``aii-2`` deploys the canonical protocol payload from ai-native-development into
each managed repo and writes ``<repo>/.ai-deploy-lock.json`` describing what it
put there. Two of that file's fields identify the payload exactly:
``source_git_commit`` (the ai-native-development commit it came from) and
``canonical_payload_sha256`` (the aggregate hash of the deployed bytes). Both are
branch-independent — a branch name moves, and two repos on the same branch can
hold different payloads — which is what makes the commit usable as the cohort key
a downstream consumer groups evaluation reports by.

The file is **live target state**, and that is the whole problem this module
exists to solve: by the time a task is evaluated, the repo has usually been
redeployed, so reading the lock at evaluation time would attribute today's
revision to a run that used another one. The hub therefore stamps the lock into
the hub-owned run dir at launch, before the orchestrator is spawned, and every
downstream reader takes the revision from that durable artifact. Provenance is
never backfilled from the tree's current lock — a run whose launch capture failed
has *no* revision, not the one deployed later.

The artifact (``protocol.json``, beside ``sessions.json``) is deliberately not a
field of ``run.json``: the record's shape stays stable for readers that predate
this, and the end-of-run verdict below is written without rewriting the record.

Two writes, ordered by the run's own state machine:

- **Launch capture** (:func:`capture_protocol`) — validates and freezes the two
  revision fields. Whatever it captured is immutable from then on.
- **End check** (:func:`settle_protocol`) — re-reads the lock at the live->ended
  transition and records only a *stability* verdict: the deployment can be
  updated while a run is in flight, in which case the run's sessions did not all
  use one protocol revision and the run must not be published as if they had.
  It never establishes or replaces a captured revision, and it runs exactly once
  — ``checked_ts`` marks that it ran, so a later re-check cannot mistake a
  post-run redeployment for a mid-run one.

Publication is therefore narrow by construction: a run carries a publishable
revision only once the end check has run *and* found the deployment unchanged
(:meth:`ProtocolSnapshot.verified_revision`). A capture on its own attests to the
run's first moment, not to what its later sessions loaded, so a run that is still
live, whose lock had vanished by its end, or whose end the hub was not up to
observe keeps its commit readable for audit and publishes nothing.

A *report*, though, is about a task, and a task is worked by several runs. The
same rule therefore applies once more across them
(:func:`effective_protocol`): the revision an evaluation report is published
under is the one every contributing run verified, and a set that is incomplete or
disagrees has no effective revision — only a reason. Both the per-run predicate
and the cross-run aggregate live here so the rule has one owner; a consumer reads
the answer, never re-derives it from the parts.

Because the two writes never overlap, this artifact needs no cross-writer lock
(unlike :mod:`orch_hub.task_snapshots`, whose captures genuinely race); the
atomic rename is enough to keep readers from seeing a partial file.

Capture does **not** gate launch. Readiness already fails closed on deployment
integrity (``manifest_exists``, ``aii-2 status``), and provenance is evidence,
like commit metadata: a missing or malformed lock makes a run publish *no*
revision, with an operator-readable reason, rather than refusing the run. Cohort
admission policy belongs to the consumer of the report feed, not to the hub.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

DEPLOY_LOCK_NAME = ".ai-deploy-lock.json"
"""The deployed protocol's lock file, at the root of a managed repo."""

ARTIFACT_NAME = "protocol.json"
"""The per-run capture, written beside ``run.json`` in the hub-owned run dir."""

PROTOCOL_SCHEMA_VERSION = 1
"""Shape of :meth:`ProtocolSnapshot.to_dict`. Stamped on the artifact so a later
shape change stays separable from this one, exactly like the evidence pack's."""

LOCK_MAX_BYTES = 4 * 1024 * 1024
"""Read cap for the lock file. It carries one record per deployed file (~36KB for
the ~120-file payload), so the cap leaves two orders of magnitude of headroom
while keeping a pathological file from being parsed into memory."""

STABILITY_UNVERIFIED = "unverified"
"""No usable end-of-run comparison: the run is still live, its end check has not
run, or the lock could not be re-read at its end (``detail`` says which). The
launch capture stands as audit data — this is an absence of contradiction, not a
defect — but nothing confirms it held for the whole run, so it publishes no
revision."""

STABILITY_STABLE = "stable"
"""The lock still named the captured revision when the run ended. The only state
a revision may be published from."""

STABILITY_CHANGED = "changed"
"""The deployment was updated while the run was in flight, so the run's sessions
did not all use the captured revision. Conservatively unusable for publication."""

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
"""A git commit id, SHA-1 today and SHA-256 for a repo that has migrated."""

_SHA256 = re.compile(r"[0-9a-f]{64}")

_STABILITIES = (STABILITY_UNVERIFIED, STABILITY_STABLE, STABILITY_CHANGED)


@dataclass(frozen=True)
class ProtocolSnapshot:
    """One run's protocol provenance, as persisted in its ``protocol.json``.

    ``available`` is the single load-bearing flag: it is true only when both
    revision fields were read from a well-formed lock and validated, so a reader
    never has to re-decide whether a value is trustworthy. When it is false,
    ``detail`` says why in operator-readable terms and the revision fields are
    ``None`` — the unavailability is explicit, never an empty string a consumer
    could read as "no protocol" or "default".

    ``source_repo`` and ``lock_schema_version`` are the lock's own identity
    fields, carried for cohorting but deliberately *not* part of the revision:
    the same commit and payload hash mean the same protocol text whatever the
    lock calls its source.

    ``captured_ts`` is when the launch capture ran, ``checked_ts`` when the end
    comparison ran (``None`` while it has not). Everything above them is frozen
    at capture; the end check writes only ``stability``/``checked_ts``/``detail``.
    """

    available: bool
    source_git_commit: str | None = None
    canonical_payload_sha256: str | None = None
    source_repo: str | None = None
    lock_schema_version: int | None = None
    captured_ts: str | None = None
    stability: str = STABILITY_UNVERIFIED
    checked_ts: str | None = None
    detail: str | None = None

    @property
    def verified_revision(self) -> str | None:
        """The commit this run may be published under, or ``None``.

        The one predicate downstream aggregation should use, so "which runs count
        as carrying a revision" is decided here rather than re-derived at each
        consumer. It needs a completed end check that found the deployment
        unchanged: a launch capture attests only to the run's first moment, and a
        report publishes what the run ran under end to end. So a contradicted
        capture (:data:`STABILITY_CHANGED`) and an uncompared one
        (:data:`STABILITY_UNVERIFIED` — still live, lock unreadable at the end, or
        an end the hub was not up to observe) are alike here: both keep their
        recorded commit readable for audit and publish nothing. Absent evidence
        of one revision is not evidence of one revision.
        """
        if not self.available or self.stability != STABILITY_STABLE:
            return None
        return self.source_git_commit

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
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EffectiveProtocol:
    """The protocol revision a set of runs may be published under, or why none is.

    The cross-run counterpart of :meth:`ProtocolSnapshot.verified_revision`,
    carried by an evidence pack and copied onto the catalog entry a consumer
    cohorts by. ``available`` is again the single load-bearing flag: when it is
    false ``revision`` is ``None`` and ``detail`` says why in operator-readable
    terms, so "the runs disagreed" and "the hub never verified them" are never
    served as an absent field a consumer could read as "no protocol".

    ``run_count`` and ``verified_count`` are kept beside the verdict so the shape
    of the failure is machine-readable without parsing ``detail``: ``0/0`` is a
    task with no runs, ``1/3`` an incomplete set, and ``3/3`` with no revision the
    disagreement case.

    The field is ``revision`` but serializes as ``effective_revision``: within
    this record the qualifier is redundant, while a consumer reading
    ``provenance.protocol`` needs it to know the value is the *set's* revision and
    not one run's captured one.
    """

    available: bool
    revision: str | None = None
    run_count: int = 0
    verified_count: int = 0
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "effective_revision": self.revision,
            "run_count": self.run_count,
            "verified_count": self.verified_count,
            "detail": self.detail,
        }


MAX_LISTED_REVISIONS = 4
"""How many distinct revisions a disagreement ``detail`` names before summarizing.
Bounds the string a catalog line carries: revisions are full object names and a
task's run count has no ceiling, so the reason stays a sentence rather than
growing with the task."""


def effective_protocol(
    snapshots: Sequence[ProtocolSnapshot | None],
) -> EffectiveProtocol:
    """The one revision every contributing run verified, or an explained absence.

    ``snapshots`` is one entry per contributing run, in run order, each the run's
    persisted capture or ``None`` for a run that wrote none (one predating the
    capture). The rule is unanimity over
    :meth:`ProtocolSnapshot.verified_revision`, and it is deliberately the strict
    reading of "the protocol this task ran under": a report published under a
    revision two of its three runs used would state a cohort membership that is
    false for the third, and a consumer grouping by it cannot tell that from a
    true one. An incomplete or disagreeing set is worth less than an unavailable
    one only if the consumer is guessing — which the ``detail`` exists to stop.

    Both failure modes are reported together when both hold: a set can be short of
    verified runs *and* have the verified ones disagree, and naming only the first
    would suggest the missing evidence is all that stands between the task and a
    revision.
    """
    total = len(snapshots)
    if not total:
        return EffectiveProtocol(
            available=False,
            detail="no related runs to take a protocol revision from",
        )
    revisions = [
        snapshot.verified_revision
        for snapshot in snapshots
        if snapshot is not None and snapshot.verified_revision is not None
    ]
    distinct = sorted(set(revisions))
    reasons = []
    if len(revisions) < total:
        reasons.append(
            f"{total - len(revisions)} of {total} contributing runs carry no"
            " verified protocol revision"
        )
    if len(distinct) > 1:
        listed = ", ".join(distinct[:MAX_LISTED_REVISIONS])
        extra = len(distinct) - MAX_LISTED_REVISIONS
        reasons.append(
            "contributing runs verified different protocol revisions: "
            + listed
            + (f" (+{extra} more)" if extra > 0 else "")
        )
    if reasons:
        return EffectiveProtocol(
            available=False,
            run_count=total,
            verified_count=len(revisions),
            detail="; ".join(reasons),
        )
    return EffectiveProtocol(
        available=True,
        revision=distinct[0],
        run_count=total,
        verified_count=len(revisions),
    )


def effective_protocol_from_dict(data: object) -> EffectiveProtocol:
    """Read a persisted aggregate section (an evidence pack's) fail-closed.

    The reader for a consumer that holds the serialized pack rather than the
    :class:`EffectiveProtocol` — today the report catalog, which reads
    ``evidence.json`` as JSON. It lives here so the availability rule keeps one
    owner: a second implementation at the catalog would be free to drift into
    publishing a revision this module would not.

    Absence is distinguished from damage, because they mean different things to an
    operator: ``None`` is a pack written before packs carried this section (the
    common case for a while), while a present-but-wrong section is a pack
    something rewrote. An unavailable section's own reason is carried through
    verbatim — it already names which runs fell short — and a section claiming
    availability without a well-formed object name is refused rather than
    published as a cohort key no consumer can resolve.
    """
    if data is None:
        return EffectiveProtocol(
            available=False,
            detail="evidence pack carries no protocol provenance section",
        )
    if not isinstance(data, dict):
        return EffectiveProtocol(
            available=False,
            detail="evidence pack protocol provenance is not a JSON object",
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
            detail="evidence pack claims a protocol revision it does not carry",
        )
    return EffectiveProtocol(
        available=True,
        revision=revision,
        run_count=runs,
        verified_count=verified,
        detail=_str_field(data, "detail"),
    )


def artifact_path(run_dir: Path) -> Path:
    return run_dir / ARTIFACT_NAME


def deploy_lock_path(repo_path: Path) -> Path:
    return repo_path / DEPLOY_LOCK_NAME


def read_deploy_lock(repo_path: Path) -> ProtocolSnapshot:
    """Read and validate a repo's deployment lock into an unstamped snapshot.

    Never raises: every failure mode — absent file, unreadable path, oversize
    blob, non-JSON, non-object, missing or malformed revision fields — becomes an
    unavailable snapshot naming the reason. A launch must not fail because a
    display/evidence artifact could not be built, and the end check must be able
    to report "could not look" as distinct from "changed".

    Validation is what makes ``available`` mean something: a commit id that is not
    a hex object name, or a payload hash that is not SHA-256 shaped, is refused
    rather than published as a cohort key that no consumer can resolve.
    """
    path = deploy_lock_path(repo_path)
    try:
        with path.open("rb") as handle:
            raw = handle.read(LOCK_MAX_BYTES + 1)
    except FileNotFoundError:
        return _unavailable(f"no {DEPLOY_LOCK_NAME} in {repo_path}")
    except OSError as err:
        return _unavailable(f"unreadable {DEPLOY_LOCK_NAME} ({err})")
    if len(raw) > LOCK_MAX_BYTES:
        return _unavailable(
            f"{DEPLOY_LOCK_NAME} exceeds the {LOCK_MAX_BYTES}-byte read cap"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        return _unavailable(f"unparseable {DEPLOY_LOCK_NAME} ({err})")
    if not isinstance(data, dict):
        return _unavailable(f"{DEPLOY_LOCK_NAME} is not a JSON object")
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


def capture_protocol(
    run_dir: Path, repo_path: Path, *, now: dt.datetime
) -> ProtocolSnapshot:
    """Freeze ``repo_path``'s protocol revision into ``run_dir`` at launch.

    Called after the readiness gates and before the orchestrator is spawned, so
    the captured revision is the one the run's first session actually loads. The
    read cannot fail the launch (see :func:`read_deploy_lock`); the *write* can,
    and is meant to — a just-created run dir that cannot hold a few hundred bytes
    cannot hold ``run.json`` either, and the caller's existing cleanup removes the
    dir and refuses the launch, which is the same handling a failed spawn gets.
    """
    snapshot = replace(read_deploy_lock(repo_path), captured_ts=now.isoformat())
    write_protocol(run_dir, snapshot)
    return snapshot


def settle_protocol(
    run_dir: Path,
    repo_path: Path,
    *,
    now: dt.datetime,
    end_observed: bool = True,
) -> ProtocolSnapshot | None:
    """Compare the lock against the run's capture at its live->ended transition.

    Returns the settled snapshot, or ``None`` when there is nothing to settle: a
    run with no artifact (one predating this capture), a capture that carries no
    revision to contradict, or an end check that already ran.

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

    Only the two revision fields are compared. A lock that renamed its source or
    bumped its own schema while naming the same commit and payload hash deployed
    the same protocol text, and reporting that as a change would cost a
    publishable report for no gain in truth.
    """
    snapshot = read_protocol(run_dir)
    if snapshot is None or not snapshot.available or snapshot.checked_ts is not None:
        return None
    if not end_observed:
        stability, detail = (
            STABILITY_UNVERIFIED,
            "run ended while the hub was down; the deployment could not be"
            " compared at its end",
        )
    else:
        current = read_deploy_lock(repo_path)
        stability, detail = _verdict(snapshot, current)
    settled = replace(
        snapshot, stability=stability, checked_ts=now.isoformat(), detail=detail
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


def _from_dict(data: object) -> ProtocolSnapshot:
    """Rebuild a snapshot from persisted JSON, failing closed on any defect.

    Every path that cannot produce a *validated* revision yields an unavailable
    snapshot, so ``available`` keeps its meaning for every reader: an artifact
    claiming availability without a well-formed commit and payload hash, or
    carrying a stability value this version does not know (a hand-edited file, or
    one written by a newer hub), is unusable rather than partially trusted.

    ``stability`` is validated the same way and for the same reason: it is the
    field that grants publication (:meth:`ProtocolSnapshot.verified_revision`), so
    a verdict with no ``checked_ts`` — a conclusion recording no check that could
    have reached it, which :func:`settle_protocol` never writes — is refused
    rather than read as a confirmed revision.
    """
    if not isinstance(data, dict):
        return _unavailable(f"{ARTIFACT_NAME} is not a JSON object")
    stability = data.get("stability")
    if stability not in _STABILITIES:
        return _unavailable(f"{ARTIFACT_NAME} has an unrecognized stability")
    commit = _hex_field(data, "source_git_commit", _SHA)
    payload = _hex_field(data, "canonical_payload_sha256", _SHA256)
    available = data.get("available") is True
    checked_ts = _str_field(data, "checked_ts")
    if available and (commit is None or payload is None):
        return _unavailable(f"{ARTIFACT_NAME} claims a revision it does not carry")
    if available and stability != STABILITY_UNVERIFIED and checked_ts is None:
        return _unavailable(f"{ARTIFACT_NAME} carries a verdict it never checked")
    return ProtocolSnapshot(
        available=available,
        source_git_commit=commit if available else None,
        canonical_payload_sha256=payload if available else None,
        source_repo=_str_field(data, "source_repo"),
        lock_schema_version=_int_field(data, "lock_schema_version"),
        captured_ts=_str_field(data, "captured_ts"),
        stability=stability,
        checked_ts=checked_ts,
        detail=_str_field(data, "detail"),
    )


def _verdict(
    captured: ProtocolSnapshot, current: ProtocolSnapshot
) -> tuple[str, str | None]:
    """The stability verdict and its reason for one end-of-run comparison."""
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
