"""Tests for the global, append-only evaluation-report catalog (session-1).

Covers global identity and sequence allocation (including scoped
``evaluation_id`` collisions across repos), publication eligibility (only a
durably complete, coherent L1+L2 set for an archived task publishes — including
a report that becomes publishable only after close-out archives its task),
append durability under concurrent instances, torn-tail/corrupt recovery,
deterministic idempotent reconciliation, the entry's hashes and explicit
provenance, retention that cannot outrun publication (including a drifted
registry path and candidates the shared rule cannot judge), the live
semantic-evaluator hook, and disabled-mode fail-closed.

Session-2 adds the read side those durable guarantees exist for: cursor paging
that traverses every report exactly once under concurrent publication, the one
invalid/stale-cursor policy, the fail-closed read/export-by-global-key bundle,
and the audits that keep a hand-edited append-only file from becoming a silently
wrong answer — an ambiguous sequence/key, a stored identity that is not the one
its key derives from, and an artifact served without a usable digest. Their HTTP
surface is covered in ``tests/test_api.py``; session-3 adds the dashboard view.

The raw-artifact read is covered last: byte-exact bytes for every published
artifact (the parsed bundle cannot prove a digest, since it re-serializes), the
per-artifact availability that deliberately differs from the bundle's
all-or-nothing rule, the four no-bytes reasons an HTTP status has to stay able to
tell apart, and the containment that has to hold on the *file* and not only the
directory — the four names are fixed, but what stands under them is not.
"""

from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import signal
import threading
from pathlib import Path

import pytest

from orch_hub import report_catalog as report_catalog_module
from orch_hub.config import Config
from orch_hub.evaluation import (
    EVIDENCE_NAME,
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    EvaluationService,
)
from orch_hub.models import ManagedRepo, ReportCatalogEntry
from orch_hub.report_catalog import (
    ARTIFACT_IDENTITY,
    ARTIFACT_NOT_RETAINED,
    ARTIFACT_UNKNOWN,
    ARTIFACT_UNREADABLE,
    CATALOG_NAME,
    CATALOG_SUBDIR,
    CONFIG_UNAVAILABLE_DETAIL,
    PACK_UNREADABLE_DETAIL,
    REPORT_KEY_PREFIX,
    REQUIRED_ARTIFACTS,
    ReportCatalog,
    ReportCatalogError,
    report_key,
)
from orch_hub.semantic import SEMANTIC_REPORT_MD_NAME, SEMANTIC_REPORT_NAME
from orch_hub.static_metrics import STATIC_METRICS_NAME

TASK_A = "2026-07-30-alpha-task"
TASK_B = "2026-07-30-beta-task"
FIXED_TS = dt.datetime(2026, 7, 31, 8, 0, 0, tzinfo=dt.timezone.utc)
PROTOCOL_COMMIT = "6b62754694c289fc8822562cec22ab774e071b2c"


# -- fixtures -----------------------------------------------------------------


def cfg(tmp_path: Path, *, enabled: bool = True, retention: int = 20) -> Config:
    return Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=enabled,
        eval_retention=retention,
    )


def catalog(config: Config, *, clock=None) -> ReportCatalog:
    return ReportCatalog(config, clock=clock or (lambda: FIXED_TS))


def repo(tmp_path: Path, name: str = "alpha") -> ManagedRepo:
    path = tmp_path / "repos" / name
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def write_eval(
    config: Config,
    *,
    slug: str = "alpha",
    task_id: str = TASK_A,
    evaluation_id: str = "20260730T100000Z",
    archived: bool = True,
    artifacts: tuple[str, ...] = REQUIRED_ARTIFACTS,
    model: str = "fake-model",
    run_ids: tuple[str, ...] = ("r1",),
    git_available: bool = True,
    semantic_body: str | None = None,
    protocol: dict | None = None,
) -> Path:
    """Write one evaluation dir directly, so eligibility is exercised structurally.

    Building fixtures at the artifact level (rather than through the evaluator) is
    the point: the catalog's contract is that eligibility comes from the files on
    disk, never from job/trigger state, so the tests must be able to produce a
    half-written or hand-damaged dir the evaluator would never emit.
    """
    eval_dir = config.data_dir / "evaluations" / slug / task_id / evaluation_id
    eval_dir.mkdir(parents=True, exist_ok=True)
    evidence = {
        "evaluation_id": evaluation_id,
        "target": {
            "repo_name": slug,
            "repo_path": str(config.data_dir.parent / "repos" / slug),
            "task_id": task_id,
            "source": SOURCE_ARCHIVED if archived else SOURCE_ACTIVE,
            "present": True,
        },
        "runs": [{"run": {"run_id": rid}} for rid in run_ids],
        "git": {
            "available": git_available,
            "since": "2026-07-29T09:00:00+00:00",
            "until": "2026-07-29T10:00:00+00:00",
            "commits": [{"hash": "cafe1234"}, {"hash": "beef5678"}],
            "detail": None if git_available else "git history unavailable",
        },
    }
    # Omitted unless a case asks for it: the default fixture stays a pack from
    # before per-run protocol capture, which is the shape most published entries
    # will have been written from.
    if protocol is not None:
        evidence["protocol"] = protocol
    bodies = {
        EVIDENCE_NAME: json.dumps(evidence),
        STATIC_METRICS_NAME: json.dumps({"evaluation_id": evaluation_id, "scores": []}),
        SEMANTIC_REPORT_NAME: semantic_body
        if semantic_body is not None
        else json.dumps(
            {
                "evaluation_id": evaluation_id,
                "generated_ts": "2026-07-30T10:00:00+00:00",
                "repo_name": slug,
                "task_id": task_id,
                "target_present": True,
                "target_source": SOURCE_ARCHIVED if archived else SOURCE_ACTIVE,
                "evaluator": {
                    "backend": "fake",
                    "model": model,
                    "schema_version": "1",
                },
                "summary": "clean",
            }
        ),
        SEMANTIC_REPORT_MD_NAME: f"# Semantic Task Evaluation — {task_id}\n",
    }
    for name in artifacts:
        (eval_dir / name).write_text(bodies[name], encoding="utf-8")
    return eval_dir


def protocol_section(
    *,
    revision: object = PROTOCOL_COMMIT,
    available: bool = True,
    runs: int = 1,
    verified: int | None = None,
    detail: str | None = None,
) -> dict:
    """A pack's protocol section, in the shape `EffectiveProtocol` serializes.

    Written literally rather than through the producer for the same reason the
    artifacts are: the catalog's contract is over the bytes on disk, so the tests
    have to be able to hand it a section the evidence layer would never emit.
    """
    return {
        "available": available,
        "effective_revision": revision,
        "run_count": runs,
        "verified_count": runs if verified is None else verified,
        "detail": detail,
    }


def lines(config: Config) -> list[dict]:
    path = config.data_dir / CATALOG_SUBDIR / CATALOG_NAME
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def archive_task(tmp_path: Path, *, slug: str = "alpha", task_id: str = TASK_A) -> Path:
    """Move the target repo's task file into ``.ai-tasks/archive/`` (close-out).

    The only live state the catalog reads. The path matches the ``repo_path`` the
    fixture's evidence pack records, which is how a sweep finds it again.
    """
    archived = tmp_path / "repos" / slug / ".ai-tasks" / "archive"
    archived.mkdir(parents=True, exist_ok=True)
    path = archived / f"{task_id}.md"
    path.write_text(f"---\nid: {task_id}\nstatus: completed\n---\n", encoding="utf-8")
    return path


def patch_json(path: Path, **fields) -> None:
    """Rewrite top-level fields of a persisted artifact (still valid JSON)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


# -- global identity ----------------------------------------------------------


def test_report_key_is_stable_opaque_and_repo_scoped():
    key = report_key(repo_slug="alpha", task_id=TASK_A, evaluation_id="e1")

    # Stable: derived from identity, so re-deriving gives the same key. That is
    # what makes re-cataloging a no-op instead of a second global identity.
    assert key == report_key(repo_slug="alpha", task_id=TASK_A, evaluation_id="e1")
    assert key.startswith(REPORT_KEY_PREFIX)
    # Opaque: no component of the source identity leaks into the key, so a
    # consumer cannot build ordering or layout assumptions on it.
    assert "alpha" not in key and TASK_A not in key and "e1" not in key
    # Repo-scoped: the collision this whole feature exists to resolve.
    assert key != report_key(repo_slug="beta", task_id=TASK_A, evaluation_id="e1")
    assert key != report_key(repo_slug="alpha", task_id=TASK_B, evaluation_id="e1")


def test_colliding_evaluation_ids_across_repos_get_distinct_keys_and_ordered_seqs(tmp_path):
    config = cfg(tmp_path)
    # Same scoped evaluation id in two different repos — legal, because the id is
    # only unique under <repo>/<task>/.
    write_eval(config, slug="alpha", task_id=TASK_A, evaluation_id="20260730T100000Z")
    write_eval(config, slug="beta", task_id=TASK_B, evaluation_id="20260730T100000Z")

    added = catalog(config).reconcile()

    assert [entry.seq for entry in added] == [1, 2]
    assert len({entry.report_key for entry in added}) == 2
    assert {entry.repo_slug for entry in added} == {"alpha", "beta"}
    # Both carry the identical scoped id: a consumer comparing evaluation_id
    # timestamps could not order or distinguish them — seq/report_key can.
    assert {entry.evaluation_id for entry in added} == {"20260730T100000Z"}


def test_sequence_is_monotonic_across_appends_and_restarts(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, evaluation_id="20260730T100000Z")
    catalog(config).reconcile()

    write_eval(config, evaluation_id="20260730T110000Z")
    # A fresh instance = a restarted hub: the counter is re-derived from the file,
    # never carried in memory, so numbering resumes rather than restarting at 1.
    added = catalog(config).reconcile()

    assert [entry.seq for entry in added] == [2]
    assert [entry.seq for entry in catalog(config).entries()] == [1, 2]


# -- publication eligibility --------------------------------------------------


@pytest.mark.parametrize(
    "present",
    [
        (EVIDENCE_NAME,),
        (EVIDENCE_NAME, STATIC_METRICS_NAME),
        (EVIDENCE_NAME, STATIC_METRICS_NAME, SEMANTIC_REPORT_NAME),
        (EVIDENCE_NAME, SEMANTIC_REPORT_NAME, SEMANTIC_REPORT_MD_NAME),
    ],
)
def test_incomplete_artifact_sets_are_never_cataloged(tmp_path, present):
    config = cfg(tmp_path)
    write_eval(config, artifacts=present)

    assert catalog(config).reconcile() == []
    assert catalog(config).entries() == []


def test_only_an_archived_tasks_report_publishes(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, slug="alpha", task_id=TASK_A, archived=True)
    write_eval(config, slug="beta", task_id=TASK_B, archived=False)

    added = catalog(config).reconcile()

    # The feed carries finished protocol stories only, so publication IS the
    # eligibility decision and every entry is a feed item.
    assert [entry.repo_slug for entry in added] == ["alpha"]
    assert added[0].archived is True
    assert added[0].target_source == SOURCE_ARCHIVED
    # The still-active report is not lost, just not published yet: it consumed no
    # sequence, and its artifacts are untouched.
    assert [entry.repo_slug for entry in catalog(config).entries()] == ["alpha"]
    assert (config.data_dir / "evaluations" / "beta" / TASK_B).is_dir()


def test_a_report_of_a_then_active_task_publishes_once_close_out_archives_it(tmp_path):
    config = cfg(tmp_path)
    # Evaluated while the task was still in flight — the common case, since the
    # dashboard's Layer 2 button works on active tasks.
    write_eval(config, archived=False)
    assert catalog(config).reconcile() == []

    archive_task(tmp_path)
    added = catalog(config).reconcile()

    # Archive happens after the report is written and no artifact can record it,
    # so the sweep re-examines unpublished candidates and publishes this one at
    # the sequence where it became a finished story — rather than stranding it
    # ineligible forever behind an evaluation-time stamp.
    assert [entry.evaluation_id for entry in added] == ["20260730T100000Z"]
    assert added[0].seq == 1
    assert added[0].archived is True
    # The stamp stays honest about what was evaluated; ``archived`` is the
    # publication-time fact.
    assert added[0].target_source == SOURCE_ACTIVE
    # And it is published exactly once.
    assert catalog(config).reconcile() == []
    assert len(catalog(config).entries()) == 1


def test_the_live_hook_publishes_a_report_of_an_already_archived_task(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, archived=False)
    archive_task(tmp_path)

    entry = catalog(config).record_evaluation(repo(tmp_path), TASK_A, "20260730T100000Z")

    # The hook and the sweep apply one publication rule, so a task archived
    # between evaluation and hook does not need a restart to reach the feed.
    assert entry is not None and entry.archived is True
    assert catalog(config).reconcile() == []


def test_an_active_report_whose_pack_names_no_repo_stays_unpublished(tmp_path):
    config = cfg(tmp_path)
    eval_dir = write_eval(config, archived=False)
    (eval_dir / EVIDENCE_NAME).write_text("{not json", encoding="utf-8")
    archive_task(tmp_path)

    # The stamp says active and the degraded pack names no repo to probe, so the
    # catalog cannot establish that this task is archived. It declines to publish
    # rather than guessing — and the unreadable pack does not crash the sweep.
    assert catalog(config).reconcile() == []
    assert catalog(config).entries() == []


def test_live_hook_ignores_an_incomplete_evaluation(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, artifacts=(EVIDENCE_NAME, STATIC_METRICS_NAME))

    assert catalog(config).record_evaluation(repo(tmp_path), TASK_A, "20260730T100000Z") is None
    assert catalog(config).entries() == []


def test_live_hook_defers_a_report_whose_task_is_still_active(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, archived=False)

    assert catalog(config).record_evaluation(repo(tmp_path), TASK_A, "20260730T100000Z") is None
    assert catalog(config).entries() == []


def test_an_artifact_standing_as_a_link_is_not_a_publishable_report(tmp_path):
    # A link is not the file publication would hash. The entry's manifest digest
    # is the claim a consumer verifies, so recording one taken over whatever the
    # link points at would publish that claim about a file the hub never wrote.
    # The dir is therefore not a candidate at all — same predicate, same answer,
    # for the sweep, the live hook, and the retention guard.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    outside = tmp_path / "outside" / STATIC_METRICS_NAME
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(json.dumps({"scores": ["outside"]}), encoding="utf-8")
    linked = eval_dir / STATIC_METRICS_NAME
    published = linked.read_bytes()
    linked.unlink()
    linked.symlink_to(outside)
    cat = catalog(config)

    assert cat.reconcile() == []
    assert cat.record_evaluation(repo(tmp_path), TASK_A, "20260730T100000Z") is None
    assert cat.entries() == []
    # Unpublishable is not publication debt, exactly as an incomplete set is not:
    # retention stays free to prune a dir the feed will never publish.
    assert cat.unpublished_ids(repo(tmp_path), TASK_A) == set()
    # And the refusal is a fact about the dir's current state, not a mark on it:
    # restoring the file makes the same report publishable.
    linked.unlink()
    linked.write_bytes(published)
    assert [entry.evaluation_id for entry in cat.reconcile()] == ["20260730T100000Z"]


def test_publication_refuses_a_link_planted_after_the_completeness_check(
    tmp_path, monkeypatch
):
    # The static case above is only half the guarantee: completeness is checked by
    # pathname and the manifest is hashed afterwards, so a swap landing between
    # the two is the window a check-then-hash publication is exposed in. Fired
    # here from inside the check itself. The digest must come from an open that
    # refuses the link, leaving the candidate undecided — no entry, retried later
    # — rather than recording an outside file's hash as this report's.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    outside = tmp_path / "outside" / EVIDENCE_NAME
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(json.dumps({"target": {"repo_path": "/elsewhere"}}), encoding="utf-8")
    real_is_complete = report_catalog_module._is_complete
    swapped = False

    def _swap_then_check(path: Path) -> bool:
        nonlocal swapped
        verdict = real_is_complete(path)
        if verdict and not swapped:
            swapped = True
            artifact = path / EVIDENCE_NAME
            artifact.unlink()
            artifact.symlink_to(outside)
        return verdict

    monkeypatch.setattr(report_catalog_module, "_is_complete", _swap_then_check)
    added = catalog(config).reconcile()

    # The swap really landed inside the window (otherwise the assertions below
    # would be vacuous), and nothing was published from it.
    assert swapped and (eval_dir / EVIDENCE_NAME).is_symlink()
    assert added == []
    assert not (config.data_dir / CATALOG_SUBDIR / CATALOG_NAME).exists()


# -- identity coherence -------------------------------------------------------


@pytest.mark.parametrize(
    ("artifact", "fields"),
    [
        (SEMANTIC_REPORT_NAME, {"task_id": TASK_B}),
        (SEMANTIC_REPORT_NAME, {"evaluation_id": "20260730T999999Z"}),
        (SEMANTIC_REPORT_NAME, {"repo_name": "somewhere-else"}),
        (STATIC_METRICS_NAME, {"evaluation_id": "20260730T999999Z"}),
        (EVIDENCE_NAME, {"evaluation_id": "20260730T999999Z"}),
    ],
)
def test_an_artifact_contradicting_its_directory_is_never_published(
    tmp_path, artifact, fields
):
    config = cfg(tmp_path)
    eval_dir = write_eval(config, slug="alpha", evaluation_id="20260730T100000Z")
    patch_json(eval_dir / artifact, **fields)
    # A healthy sibling proves the rejection is scoped to the incoherent dir.
    write_eval(config, slug="beta", task_id=TASK_B, evaluation_id="20260730T110000Z")

    added = catalog(config).reconcile()

    # The global key comes from directory names, so publishing a bundle whose own
    # contents name a different source would put a report in the feed under a key
    # that misidentifies it — for a copied, restored, or half-renamed dir.
    assert [entry.repo_slug for entry in added] == ["beta"]
    assert [entry.seq for entry in added] == [1]


def test_the_evidence_targets_own_identity_is_checked_too(tmp_path):
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    evidence = json.loads((eval_dir / EVIDENCE_NAME).read_text(encoding="utf-8"))
    evidence["target"]["task_id"] = TASK_B
    (eval_dir / EVIDENCE_NAME).write_text(json.dumps(evidence), encoding="utf-8")

    assert catalog(config).reconcile() == []


def test_an_artifact_that_carries_no_identity_still_publishes(tmp_path):
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    # Silence is not disagreement: an older/leaner artifact must not be treated as
    # a mismatch, or the coherence check quietly becomes a second completeness rule.
    (eval_dir / STATIC_METRICS_NAME).write_text(json.dumps({"scores": []}), encoding="utf-8")

    assert [entry.seq for entry in catalog(config).reconcile()] == [1]


# -- entry content ------------------------------------------------------------


def test_entry_carries_hashes_evaluator_identity_and_explicit_provenance(tmp_path):
    config = cfg(tmp_path)
    eval_dir = write_eval(config, run_ids=("r1", "r2"))

    entry = catalog(config).reconcile()[0]

    assert entry.cataloged_ts == FIXED_TS.isoformat()
    assert entry.generated_ts == "2026-07-30T10:00:00+00:00"
    assert (entry.backend, entry.model, entry.schema_version) == ("fake", "fake-model", "1")
    assert entry.repo_path == config.data_dir.parent / "repos" / "alpha"

    # Every artifact is hashed, and the hash is the real content digest.
    assert [a.name for a in entry.artifacts] == list(REQUIRED_ARTIFACTS)
    md = eval_dir / SEMANTIC_REPORT_MD_NAME
    digest = next(a for a in entry.artifacts if a.name == SEMANTIC_REPORT_MD_NAME)
    assert digest.sha256 == hashlib.sha256(md.read_bytes()).hexdigest()
    assert digest.size == md.stat().st_size

    prov = entry.provenance
    assert prov.runs_available is True and prov.run_ids == ("r1", "r2")
    assert prov.git_available is True and prov.git_commit_count == 2
    # git log is reverse-chronological, so the window's first commit is newest.
    assert prov.git_newest_sha == "cafe1234"
    # Provenance the evidence pack cannot carry is unavailable WITH A REASON, so a
    # consumer never reads absence as "no protocol" / "default config". This pack
    # predates per-run protocol capture, which is its own named reason.
    assert prov.protocol_available is False and prov.protocol_revision is None
    assert prov.protocol_detail == (
        "evidence pack carries no protocol provenance section"
    )
    assert prov.config_available is False
    assert prov.config_detail == CONFIG_UNAVAILABLE_DETAIL


def test_entry_publishes_the_protocol_revision_its_runs_verified(tmp_path):
    config = cfg(tmp_path)
    write_eval(
        config,
        run_ids=("r1", "r2"),
        protocol=protocol_section(runs=2),
    )

    entry = catalog(config).reconcile()[0]

    # The cohort key an external consumer groups reports by: the deployed
    # ai-native-development commit both runs verifiably ran under, read from the
    # pack's durable per-run captures rather than from the repo (whose deployment
    # has usually moved on by publication time).
    assert entry.provenance.protocol_available is True
    assert entry.provenance.protocol_revision == PROTOCOL_COMMIT
    assert entry.provenance.protocol_detail is None
    assert entry.to_dict()["provenance"]["protocol"] == {
        "available": True,
        "effective_revision": PROTOCOL_COMMIT,
        "detail": None,
    }


def test_entry_carries_the_packs_reason_when_no_revision_is_effective(tmp_path):
    config = cfg(tmp_path)
    reason = "1 of 2 contributing runs carry no verified protocol revision"
    write_eval(
        config,
        run_ids=("r1", "r2"),
        protocol=protocol_section(
            available=False, revision=None, runs=2, verified=1, detail=reason
        ),
    )

    entry = catalog(config).reconcile()[0]

    # Unavailable is published WITH the pack's own reason, verbatim: the aggregate
    # already named which runs fell short, and a generic "unavailable" here would
    # lose the only part an operator can act on. The report still publishes — a
    # revision is cohorting data, not an eligibility rule.
    assert entry.provenance.protocol_available is False
    assert entry.provenance.protocol_revision is None
    assert entry.provenance.protocol_detail == reason


def test_a_protocol_section_claiming_a_revision_it_lacks_is_refused(tmp_path):
    config = cfg(tmp_path)
    # A hand-edited or truncated pack: `available` says yes, the revision is not an
    # object name. Publishing it would hand consumers a cohort key that resolves to
    # nothing, and one that silently splits a cohort it belongs to.
    write_eval(config, protocol=protocol_section(revision="HEAD"))

    entry = catalog(config).reconcile()[0]

    assert entry.provenance.protocol_available is False
    assert entry.provenance.protocol_revision is None
    assert entry.provenance.protocol_detail == (
        "evidence pack claims a protocol revision it does not carry"
    )


def test_a_malformed_protocol_section_degrades_instead_of_raising(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, protocol=protocol_section())
    eval_dir = config.data_dir / "evaluations" / "alpha" / TASK_A / "20260730T100000Z"
    evidence = json.loads((eval_dir / EVIDENCE_NAME).read_text(encoding="utf-8"))
    evidence["protocol"] = [PROTOCOL_COMMIT]
    (eval_dir / EVIDENCE_NAME).write_text(json.dumps(evidence), encoding="utf-8")

    entry = catalog(config).reconcile()[0]

    # Same rule as the nested-commit case: a structurally wrong section degrades
    # its own field rather than escaping the entry build and costing every later
    # candidate in the sweep its publication.
    assert entry.provenance.protocol_available is False
    assert entry.provenance.protocol_detail == (
        "evidence pack protocol provenance is not a JSON object"
    )


def test_entry_round_trips_through_the_persisted_line(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, run_ids=("r1",), protocol=protocol_section())
    original = catalog(config).reconcile()[0]

    # Re-read by a fresh instance: the persisted line must reconstruct the entry
    # field-for-field, since the catalog is append-only and never migrated.
    reloaded = catalog(config).entries()[0]

    assert reloaded == original
    assert reloaded.to_dict() == lines(config)[0]


def test_a_line_from_before_protocol_provenance_still_deserializes(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, protocol=protocol_section())
    published = catalog(config).reconcile()[0]

    # Rewrite the line in the pre-revision shape a running hub wrote before this
    # field existed. The catalog is append-only and never migrated, so entries
    # published then have to keep reading back — with protocol provenance simply
    # unavailable, not as a parse failure that hides the report.
    path = config.data_dir / CATALOG_SUBDIR / CATALOG_NAME
    line = published.to_dict()
    line["provenance"]["protocol"] = {"available": False, "detail": "unavailable"}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")

    entry = catalog(config).entries()[0]

    assert entry.seq == published.seq and entry.report_key == published.report_key
    assert entry.provenance.protocol_available is False
    assert entry.provenance.protocol_revision is None
    assert entry.provenance.git_available is True  # every other section intact


def test_pre_schema_version_evidence_pack_publishes_with_full_provenance(tmp_path):
    # `write_eval` builds the evidence pack in the shape that predates the pack's
    # `schema_version`/`ai` section, and that is deliberate: entries outlive the
    # schema they were written under, so an older pack must still yield complete
    # provenance rather than degrading. Asserted explicitly so a later fixture
    # refresh cannot silently retire this guarantee.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    evidence = json.loads((eval_dir / EVIDENCE_NAME).read_text(encoding="utf-8"))
    assert "schema_version" not in evidence and "ai" not in evidence

    entry = catalog(config).reconcile()[0]

    assert entry.provenance.runs_available is True and entry.provenance.run_ids == ("r1",)
    assert entry.provenance.git_available is True
    assert entry.repo_path == config.data_dir.parent / "repos" / "alpha"


def test_degraded_evidence_still_publishes_with_explicit_unavailable_provenance(tmp_path):
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    (eval_dir / EVIDENCE_NAME).write_text("{not json", encoding="utf-8")

    entry = catalog(config).reconcile()[0]

    # The report itself is intact and publishable; only its cohorting degrades.
    assert entry.repo_path is None
    assert entry.provenance.runs_available is False
    assert entry.provenance.runs_detail == PACK_UNREADABLE_DETAIL
    assert entry.provenance.git_available is False
    assert entry.provenance.protocol_available is False
    assert entry.provenance.protocol_detail == PACK_UNREADABLE_DETAIL


def test_a_malformed_nested_commit_degrades_provenance_instead_of_raising(tmp_path):
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    evidence = json.loads((eval_dir / EVIDENCE_NAME).read_text(encoding="utf-8"))
    # A structurally corrupt pack: the commit list holds scalars, not objects.
    evidence["git"]["commits"] = ["cafe1234", 7]
    (eval_dir / EVIDENCE_NAME).write_text(json.dumps(evidence), encoding="utf-8")

    entry = catalog(config).reconcile()[0]

    # Reading a nested field off a non-object would raise AttributeError, which is
    # not a per-candidate failure: it escapes the entry build and takes the rest of
    # the sweep with it. Every nested lookup is type-checked, so the field simply
    # degrades and the report still publishes.
    assert entry.provenance.git_newest_sha is None
    assert entry.provenance.git_commit_count == 2


def test_an_unexpected_error_in_one_candidate_does_not_stop_the_sweep(tmp_path, monkeypatch):
    config = cfg(tmp_path)
    write_eval(config, slug="alpha", evaluation_id="20260730T100000Z", run_ids=("boom",))
    write_eval(config, slug="beta", task_id=TASK_B, evaluation_id="20260730T110000Z")

    real = report_catalog_module._provenance

    def exploding(evidence):
        if evidence is not None and "boom" in json.dumps(evidence.get("runs", [])):
            raise RuntimeError("nested explosion")
        return real(evidence)

    monkeypatch.setattr(report_catalog_module, "_provenance", exploding)
    added = catalog(config).reconcile()

    # A startup backfill that aborted on the first structurally broken artifact
    # would strand every later report behind it — including reports in other repos.
    assert [entry.repo_slug for entry in added] == ["beta"]
    assert [entry.seq for entry in added] == [1]


def test_unreadable_semantic_report_is_skipped_without_failing_the_sweep(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, slug="alpha", evaluation_id="20260730T100000Z", semantic_body="{broken")
    write_eval(config, slug="beta", task_id=TASK_B, evaluation_id="20260730T110000Z")

    added = catalog(config).reconcile()

    # One broken dir must not cost the other repo its publication.
    assert [entry.repo_slug for entry in added] == ["beta"]
    # And the skipped one consumed no sequence number.
    assert [entry.seq for entry in added] == [1]


# -- reconciliation -----------------------------------------------------------


def test_reconcile_is_idempotent_across_repeated_sweeps(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, slug="alpha", evaluation_id="20260730T100000Z")
    write_eval(config, slug="beta", task_id=TASK_B, evaluation_id="20260730T110000Z")

    first = catalog(config).reconcile()
    assert len(first) == 2

    # Repeated sweeps (restarts) add nothing and never re-mint an identity.
    assert catalog(config).reconcile() == []
    assert catalog(config).reconcile() == []
    entries = catalog(config).entries()
    assert [entry.report_key for entry in entries] == [e.report_key for e in first]
    assert [entry.seq for entry in entries] == [1, 2]
    assert len(lines(config)) == 2


def test_reconcile_orders_backfill_by_generation_across_repos(tmp_path):
    config = cfg(tmp_path)
    # Written in an order that disagrees with generation order, and interleaved
    # across repos, so directory-walk order cannot accidentally pass this.
    write_eval(config, slug="beta", task_id=TASK_B, evaluation_id="20260730T120000Z")
    write_eval(config, slug="alpha", task_id=TASK_A, evaluation_id="20260730T100000Z")
    write_eval(config, slug="beta", task_id=TASK_B, evaluation_id="20260730T110000Z")

    added = catalog(config).reconcile()

    assert [entry.evaluation_id for entry in added] == [
        "20260730T100000Z",
        "20260730T110000Z",
        "20260730T120000Z",
    ]
    assert [entry.seq for entry in added] == [1, 2, 3]


def test_reconcile_orders_same_second_collision_suffixes_numerically(tmp_path):
    config = cfg(tmp_path)
    for suffix in ("", "-2", "-10"):
        write_eval(config, evaluation_id=f"20260730T100000Z{suffix}")

    added = catalog(config).reconcile()

    # The unpadded collision suffix must sort numerically: a lexical sort would
    # put -10 before -2 and publish the newest evaluation as if it were older.
    assert [entry.evaluation_id for entry in added] == [
        "20260730T100000Z",
        "20260730T100000Z-2",
        "20260730T100000Z-10",
    ]


def test_reconcile_backfills_reports_the_live_hook_never_saw(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, evaluation_id="20260730T100000Z")
    catalog(config).reconcile()

    # A report that landed while the hook was unwired/failing (or before the
    # feature existed) is still published by the next sweep.
    write_eval(config, evaluation_id="20260730T110000Z")
    added = catalog(config).reconcile()

    assert [entry.evaluation_id for entry in added] == ["20260730T110000Z"]
    assert len(catalog(config).entries()) == 2


def test_reconcile_ignores_stray_files_and_wrong_depth_entries(tmp_path):
    config = cfg(tmp_path)
    base = config.data_dir / "evaluations"
    base.mkdir(parents=True)
    (base / "stray.txt").write_text("x", encoding="utf-8")
    (base / "alpha").mkdir()
    (base / "alpha" / "loose.json").write_text("{}", encoding="utf-8")
    write_eval(config)

    added = catalog(config).reconcile()

    assert [entry.evaluation_id for entry in added] == ["20260730T100000Z"]


def test_reconcile_on_absent_tree_is_empty_not_an_error(tmp_path):
    assert catalog(cfg(tmp_path)).reconcile() == []


# -- append durability --------------------------------------------------------


def test_append_heals_a_torn_tail_without_corrupting_the_new_entry(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, evaluation_id="20260730T100000Z")
    catalog(config).reconcile()

    # Simulate a crash mid-append: a partial line with no trailing newline.
    path = config.data_dir / CATALOG_SUBDIR / CATALOG_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "report_key": "rpt-tor')

    write_eval(config, evaluation_id="20260730T110000Z")
    added = catalog(config).reconcile()

    # The torn bytes are fenced onto their own line, so the new entry parses.
    assert len(added) == 1
    entries = catalog(config).entries()
    assert [entry.evaluation_id for entry in entries] == [
        "20260730T100000Z",
        "20260730T110000Z",
    ]
    # The torn line never became a visible entry, so reusing its number is safe.
    assert [entry.seq for entry in entries] == [1, 2]


def test_corrupt_lines_are_skipped_without_hiding_valid_entries(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, evaluation_id="20260730T100000Z")
    catalog(config).reconcile()

    path = config.data_dir / CATALOG_SUBDIR / CATALOG_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write('{"unrelated": true}\n')  # a JSON object that is not an entry
        fh.write('{"seq": "two", "report_key": "rpt-x"}\n')  # non-integer cursor
        fh.write("\n")

    entries = catalog(config).entries()

    assert [entry.evaluation_id for entry in entries] == ["20260730T100000Z"]
    # Damage does not renumber: the next append still lands past the highest
    # sequence a consumer could already have imported.
    write_eval(config, evaluation_id="20260730T110000Z")
    assert [entry.seq for entry in catalog(config).reconcile()] == [2]


def test_appends_do_not_interleave_partial_lines(tmp_path):
    config = cfg(tmp_path)
    for index in range(6):
        write_eval(
            config,
            slug=f"repo{index}",
            task_id=f"2026-07-30-task-{index}",
            evaluation_id=f"20260730T1000{index:02d}Z",
        )

    catalog(config).reconcile()

    raw = (config.data_dir / CATALOG_SUBDIR / CATALOG_NAME).read_text(encoding="utf-8")
    # Every line is a whole record and the file ends cleanly, so a reader never
    # sees a half-written entry between two good ones.
    assert raw.endswith("\n")
    assert len(raw.splitlines()) == 6
    assert [json.loads(line)["seq"] for line in raw.splitlines()] == [1, 2, 3, 4, 5, 6]


def test_concurrent_instances_never_allocate_a_duplicate_sequence(tmp_path):
    config = cfg(tmp_path)
    for index in range(8):
        write_eval(
            config,
            slug=f"repo{index}",
            task_id=f"2026-07-30-task-{index}",
            evaluation_id=f"20260730T1000{index:02d}Z",
        )

    # Separate instances over one data dir — what a second process (a CLI sweep
    # beside the resident service, an overlapping restart) actually looks like. An
    # instance-local lock leaves each of them free to read the same durable
    # maximum and mint the same seq, which silently truncates cursor pagination:
    # a consumer that resumes after the first duplicate never sees the second.
    start = threading.Barrier(4)

    def sweep():
        instance = catalog(config)
        start.wait(5)
        instance.reconcile()

    threads = [threading.Thread(target=sweep) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    records = lines(config)
    seqs = [record["seq"] for record in records]
    keys = [record["report_key"] for record in records]
    assert sorted(seqs) == list(range(1, 9))
    assert len(set(keys)) == 8
    assert [entry.seq for entry in catalog(config).entries()] == list(range(1, 9))


def test_a_second_instance_waits_for_an_in_flight_transaction(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, slug="alpha", evaluation_id="20260730T100000Z")
    write_eval(config, slug="beta", task_id=TASK_B, evaluation_id="20260730T110000Z")
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_clock():
        entered.set()
        release.wait(5)
        return FIXED_TS

    holder = threading.Thread(target=lambda: catalog(config, clock=blocking_clock).reconcile())
    holder.start()
    assert entered.wait(5)

    def second():
        catalog(config).reconcile()
        finished.set()

    other = threading.Thread(target=second)
    other.start()
    # The transaction spans the whole sweep, so the second instance must not be
    # reading (and numbering from) a file the first is still appending to.
    assert not finished.wait(0.3)
    release.set()
    holder.join(10)
    assert finished.wait(10)
    other.join(10)

    assert [entry.seq for entry in catalog(config).entries()] == [1, 2]
    assert len({entry.report_key for entry in catalog(config).entries()}) == 2


# -- retention --------------------------------------------------------------


def service_with_guard(config: Config, cat: ReportCatalog) -> EvaluationService:
    return EvaluationService(config, retention_guard=cat.unpublished_ids)


def test_unpublished_ids_owes_only_publishable_reports(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, evaluation_id="20260730T100000Z", archived=True)
    write_eval(config, evaluation_id="20260730T110000Z", archived=False)
    cat = catalog(config)

    # Only the archived-task report is owed a publication; the active one is
    # supposed to stay unpublished, so protecting it would suspend retention for
    # exactly the common case.
    assert cat.unpublished_ids(repo(tmp_path), TASK_A) == {"20260730T100000Z"}

    cat.reconcile()

    # Once published, the artifacts are disposable: the entry outlives them.
    assert cat.unpublished_ids(repo(tmp_path), TASK_A) == set()


def test_retention_never_prunes_a_report_the_feed_has_not_published(tmp_path):
    config = cfg(tmp_path, retention=1)
    older = write_eval(config, evaluation_id="20260730T100000Z")
    write_eval(config, evaluation_id="20260730T110000Z")
    cat = catalog(config)
    service = service_with_guard(config, cat)

    # Publication of the older report failed (or never ran); retention would
    # otherwise delete it before any sweep could index it, and reconciliation
    # heals only from artifacts that still exist — so the report would be gone
    # from the feed forever.
    assert service.prune_evaluations(repo(tmp_path), TASK_A) == []
    assert older.is_dir()

    cat.reconcile()

    assert service.prune_evaluations(repo(tmp_path), TASK_A) == ["20260730T100000Z"]
    assert not older.is_dir()
    # The published entry survives its artifacts, exactly as designed.
    assert [entry.evaluation_id for entry in cat.entries()] == [
        "20260730T100000Z",
        "20260730T110000Z",
    ]


def test_retention_still_bounds_reports_that_are_not_publishable(tmp_path):
    config = cfg(tmp_path, retention=1)
    older = write_eval(config, evaluation_id="20260730T100000Z", archived=False)
    write_eval(config, evaluation_id="20260730T110000Z", archived=False)
    cat = catalog(config)

    # An active task's reports are never owed a publication, so the guard leaves
    # retention alone: the hub does not accumulate evaluations of live work.
    assert service_with_guard(config, cat).prune_evaluations(repo(tmp_path), TASK_A) == [
        "20260730T100000Z"
    ]
    assert not older.is_dir()


def test_retention_cannot_outrun_publication_when_the_repo_path_drifts(tmp_path):
    config = cfg(tmp_path, retention=1)
    older = write_eval(config, evaluation_id="20260730T100000Z", archived=False)
    write_eval(config, evaluation_id="20260730T110000Z", archived=False)
    # Close-out archived the task at the path the evidence packs record; the
    # registry has since re-registered the same repo somewhere else.
    archive_task(tmp_path)
    moved_path = tmp_path / "moved" / "alpha"
    moved = ManagedRepo(
        name="alpha", path=moved_path, manifest=moved_path / ".ai-deploy-manifest.json"
    )

    cat = catalog(config)

    # The guard must answer the archive question from each candidate's own
    # evidence pack, exactly like publication does. Probing the registry path
    # instead reports no debt for reports a sweep would publish, and retention
    # then deletes one — unrecoverably, since reconciliation heals only from
    # artifacts that still exist.
    assert service_with_guard(config, cat).prune_evaluations(moved, TASK_A) == []
    assert older.is_dir()
    assert [entry.evaluation_id for entry in cat.reconcile()] == [
        "20260730T100000Z",
        "20260730T110000Z",
    ]


def test_the_guard_protects_a_candidate_it_cannot_judge(tmp_path):
    config = cfg(tmp_path, retention=1)
    older = write_eval(
        config, evaluation_id="20260730T100000Z", semantic_body="{broken"
    )
    write_eval(config, evaluation_id="20260730T110000Z")
    cat = catalog(config)

    # An artifact the publication rule cannot read is *undecided*, not decided
    # against: a sweep skips it and retries, so pruning it on the strength of
    # "not publishable right now" is the one deletion that loses a report.
    assert cat.unpublished_ids(repo(tmp_path), TASK_A) == {
        "20260730T100000Z",
        "20260730T110000Z",
    }
    assert service_with_guard(config, cat).prune_evaluations(repo(tmp_path), TASK_A) == []
    assert older.is_dir()


def test_a_failing_archive_probe_defers_publication_and_protects_the_report(
    tmp_path, monkeypatch
):
    config = cfg(tmp_path, retention=1)
    older = write_eval(config, evaluation_id="20260730T100000Z", archived=False)
    write_eval(config, evaluation_id="20260730T110000Z", archived=False)
    archive_task(tmp_path)

    def unreadable(repo_, task_id):
        raise OSError("target repo volume is unavailable")

    monkeypatch.setattr(report_catalog_module, "read_archived_task_content", unreadable)
    cat = catalog(config)

    # Both sides of one predicate resolve "cannot tell" in their own safe
    # direction: publication declines (and retries next sweep) while retention
    # keeps the artifacts a later sweep still needs.
    assert cat.reconcile() == []
    assert service_with_guard(config, cat).prune_evaluations(repo(tmp_path), TASK_A) == []
    assert older.is_dir()

    monkeypatch.undo()

    assert [entry.evaluation_id for entry in catalog(config).reconcile()] == [
        "20260730T100000Z",
        "20260730T110000Z",
    ]


def test_retention_is_skipped_when_the_guard_cannot_answer(tmp_path):
    config = cfg(tmp_path, retention=1)
    older = write_eval(config, evaluation_id="20260730T100000Z")
    write_eval(config, evaluation_id="20260730T110000Z")

    def broken_guard(repo_, task_id):
        raise RuntimeError("catalog unreadable")

    service = EvaluationService(config, retention_guard=broken_guard)

    # Fail closed: with the feed's debt unknown, one deletion could be the one
    # that loses a report, and retention self-corrects on the next generation.
    assert service.prune_evaluations(repo(tmp_path), TASK_A) == []
    assert older.is_dir()


# -- reads --------------------------------------------------------------------


def test_entries_cursor_and_limit_traverse_each_entry_once(tmp_path):
    config = cfg(tmp_path)
    for index in range(5):
        write_eval(
            config,
            slug=f"repo{index}",
            task_id=f"2026-07-30-task-{index}",
            evaluation_id=f"20260730T1000{index:02d}Z",
        )
    cat = catalog(config)
    cat.reconcile()

    seen: list[int] = []
    cursor: int | None = None
    while True:
        page = cat.entries(after_seq=cursor, limit=2)
        if not page:
            break
        seen.extend(entry.seq for entry in page)
        cursor = page[-1].seq
        # An append concurrent with paging only ever extends the tail.
        if cursor == 2:
            write_eval(
                config,
                slug="late",
                task_id="2026-07-30-late",
                evaluation_id="20260730T190000Z",
            )
            cat.reconcile()

    assert seen == [1, 2, 3, 4, 5, 6]


def test_find_resolves_a_report_key_and_unknown_keys_are_none(tmp_path):
    config = cfg(tmp_path)
    write_eval(config)
    entry = catalog(config).reconcile()[0]

    found = catalog(config).find(entry.report_key)

    assert found is not None and found.evaluation_id == entry.evaluation_id
    assert catalog(config).find("rpt-does-not-exist") is None


def test_pruned_artifacts_leave_the_entry_auditable_and_unavailable(tmp_path):
    import shutil

    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]

    shutil.rmtree(eval_dir)

    # Retention removed the artifacts; the published entry survives with its
    # hashes intact, and the artifact dir simply no longer exists.
    still = catalog(config).find(entry.report_key)
    assert still is not None and still.artifacts == entry.artifacts
    assert not cat.artifact_dir(still).exists()
    # Re-sweeping does not drop or re-mint the entry for the vanished dir.
    assert catalog(config).reconcile() == []
    assert len(catalog(config).entries()) == 1


# -- cursor pages -------------------------------------------------------------


def publish_many(config: Config, count: int, *, start: int = 0) -> None:
    """Publish ``count`` reports, each in its own repo/task so ids never collide."""
    for index in range(start, start + count):
        write_eval(
            config,
            slug=f"repo{index}",
            task_id=f"2026-07-30-task-{index}",
            evaluation_id=f"20260730T1000{index:02d}Z",
        )
    catalog(config).reconcile()


def rewrite_lines(config: Config, rows: list[dict]) -> None:
    """Replace the catalog file with ``rows`` (for hand-built anomalies)."""
    path = config.data_dir / CATALOG_SUBDIR / CATALOG_NAME
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_paging_traverses_every_report_exactly_once_while_appends_happen(tmp_path):
    # The feed's core contract. Entries are immutable and their sequence is
    # monotonic, so a report published mid-traversal can only extend the suffix
    # the watermark names — never renumber, reorder, or land before what the
    # consumer already imported.
    config = cfg(tmp_path)
    publish_many(config, 5)
    cat = catalog(config)

    seen: list[int] = []
    cursor: int | None = None
    appended = False
    while True:
        page = cat.page(after_seq=cursor, limit=2)
        seen.extend(entry.seq for entry in page.entries)
        cursor = page.next_cursor
        if not appended:
            # Publish concurrently, from the middle of the traversal.
            publish_many(config, 2, start=90)
            appended = True
        if not page.has_more:
            break

    assert seen == [1, 2, 3, 4, 5, 6, 7]
    assert len(seen) == len(set(seen))


def test_a_full_page_reports_has_more_and_the_last_page_does_not(tmp_path):
    config = cfg(tmp_path)
    publish_many(config, 3)
    cat = catalog(config)

    first = cat.page(limit=2)
    assert [entry.seq for entry in first.entries] == [1, 2]
    assert (first.cursor, first.next_cursor, first.has_more) == (0, 2, True)

    last = cat.page(after_seq=first.next_cursor, limit=2)
    assert [entry.seq for entry in last.entries] == [3]
    assert (last.cursor, last.next_cursor, last.has_more) == (2, 3, False)


def test_a_caught_up_consumer_gets_an_empty_page_holding_its_watermark(tmp_path):
    # The steady state of an incremental poller: nothing new, and the cursor must
    # not move (or reset), so the next poll resumes from the same place.
    config = cfg(tmp_path)
    publish_many(config, 2)
    cat = catalog(config)

    idle = cat.page(after_seq=2)

    assert idle.entries == ()
    assert (idle.cursor, idle.next_cursor, idle.has_more) == (2, 2, False)

    # A cursor far past the end is the same caught-up state, not an error: a
    # consumer whose watermark outran a restored/pruned catalog keeps polling.
    stale = cat.page(after_seq=9999)
    assert (stale.entries, stale.next_cursor, stale.has_more) == ((), 9999, False)


def test_a_cursor_on_a_sequence_no_entry_has_cannot_step_over_the_next_report(tmp_path):
    # A torn append leaves a gap in the sequence. "Strictly greater" resolves it
    # without skipping: the policy never both succeeds and loses an entry.
    config = cfg(tmp_path)
    publish_many(config, 2)
    rows = lines(config)
    rows[1]["seq"] = 3
    rewrite_lines(config, rows)

    page = catalog(config).page(after_seq=2)

    assert [entry.seq for entry in page.entries] == [3]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"after_seq": -1},
        {"limit": 0},
        {"limit": -5},
        {"limit": report_catalog_module.MAX_PAGE_LIMIT + 1},
    ],
)
def test_page_rejects_an_invalid_cursor_or_limit_instead_of_guessing(tmp_path, kwargs):
    # Fail closed rather than clamp: a silently reset cursor re-imports the whole
    # feed or loses reports, and a silently clamped limit returns fewer entries
    # than the caller sized its import around.
    config = cfg(tmp_path)
    publish_many(config, 2)

    with pytest.raises(ValueError):
        catalog(config).page(**kwargs)


# -- catalog identity audits ---------------------------------------------------
#
# The write path cannot produce these states (allocation is serialized and the
# key is derived), but the file is append-only and never rewritten, so an
# operator's edit, a merge of two catalogs, or a restore over a live one can. The
# question each test asks is the same: does a broken invariant surface, or does it
# turn into a silently wrong answer?


def test_a_duplicate_sequence_fails_the_feed_instead_of_skipping_a_report(tmp_path):
    # Two entries at one seq make the cursor ambiguous: a consumer resuming from
    # that watermark never sees the second, because the cursor is strictly greater.
    # Failing loudly is the only option that does not lose a report — the catalog is
    # append-only, so renumbering the duplicate is not available either.
    config = cfg(tmp_path)
    publish_many(config, 2)
    rows = lines(config)
    rows[1]["seq"] = rows[0]["seq"]
    rewrite_lines(config, rows)

    with pytest.raises(ReportCatalogError, match="seq 1"):
        catalog(config).page()
    # Every read shares the audit, so no path serves the truncated traversal.
    with pytest.raises(ReportCatalogError):
        catalog(config).entries()
    with pytest.raises(ReportCatalogError):
        catalog(config).find(rows[0]["report_key"])


def test_a_duplicate_report_key_fails_the_same_uniqueness_audit(tmp_path):
    # The audit from the other side: one source evaluation with two global
    # identities would re-deliver a report a consumer already imported under a key
    # it was told was unique.
    config = cfg(tmp_path)
    publish_many(config, 2)
    rows = lines(config)
    rows[1]["report_key"] = rows[0]["report_key"]
    rewrite_lines(config, rows)

    with pytest.raises(ReportCatalogError, match="global identity is not unique"):
        catalog(config).page()


def test_an_ambiguous_catalog_pauses_publication_and_suspends_retention(tmp_path):
    # The reason failing closed here is safe rather than merely loud: while the
    # catalog cannot be traversed, publication stops appending onto it and the
    # retention guard cannot answer, so nothing is deleted and nothing is lost. An
    # operator who repairs the file resumes exactly where publication paused.
    config = cfg(tmp_path, retention=1)
    write_eval(config, evaluation_id="20260730T100000Z")
    write_eval(config, evaluation_id="20260730T110000Z")
    catalog(config).reconcile()
    rows = lines(config)
    rows[1]["seq"] = rows[0]["seq"]
    rewrite_lines(config, rows)

    with pytest.raises(ReportCatalogError):
        catalog(config).reconcile()

    removed = service_with_guard(config, catalog(config)).prune_evaluations(
        repo(tmp_path), TASK_A
    )

    assert removed == []
    stored = config.data_dir / "evaluations" / "alpha" / TASK_A
    assert sorted(path.name for path in stored.iterdir()) == [
        "20260730T100000Z",
        "20260730T110000Z",
    ]


# -- import bundles -----------------------------------------------------------


def test_bundle_resolves_a_global_key_to_the_entry_and_every_artifact(tmp_path):
    # The consumer-facing point of the key: fetch a whole report without knowing
    # the <repo-slug>/<task-id>/<evaluation-id>/ layout it is stored under.
    config = cfg(tmp_path)
    write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]

    bundle = cat.read_bundle(cat.find(entry.report_key))

    assert bundle.available is True and bundle.detail is None
    assert bundle.entry.report_key == entry.report_key
    assert bundle.evidence["target"]["task_id"] == TASK_A
    assert bundle.static_metrics["evaluation_id"] == entry.evaluation_id
    assert bundle.semantic_report["evaluator"]["model"] == "fake-model"
    assert bundle.report_md.startswith("# Semantic Task Evaluation")
    # Unedited artifacts still hash to what publication recorded.
    assert bundle.verified is True and bundle.mismatched == ()


def test_bundle_of_pruned_artifacts_is_explicitly_unavailable_not_missing(tmp_path):
    # The designed end state: the append-only entry outlives the artifacts it
    # indexes, so the report stays auditable while the fetch fails closed.
    import shutil

    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    shutil.rmtree(eval_dir)

    bundle = cat.read_bundle(entry)

    assert bundle.available is False
    assert "no longer retained" in bundle.detail
    assert (bundle.evidence, bundle.report_md) == (None, None)
    # Still auditable: the hashes recorded at publication survive the prune.
    assert len(bundle.entry.artifacts) == len(REQUIRED_ARTIFACTS)
    assert bundle.verified is False


def test_bundle_of_a_partially_removed_set_fails_closed(tmp_path):
    # All-or-nothing, matching what publication required: a half-populated bundle
    # would otherwise be imported as if it were the published report.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    (eval_dir / SEMANTIC_REPORT_MD_NAME).unlink()

    bundle = cat.read_bundle(entry)

    assert bundle.available is False
    # Reported as an incomplete *set* — a normal retention outcome — rather than
    # as the IO error the first absent file would otherwise raise.
    assert bundle.detail == (
        f"report artifact set is incomplete (missing {SEMANTIC_REPORT_MD_NAME})"
    )
    assert bundle.semantic_report is None


def test_bundle_of_an_unsafe_stored_identity_reads_nothing(tmp_path):
    # A hand-edited or foreign catalog line must not be able to steer a read out
    # of the evaluations tree; the containment rule rejects it before any IO.
    config = cfg(tmp_path)
    write_eval(config)
    catalog(config).reconcile()
    rows = lines(config)
    rows[0]["task_id"] = ".."
    rewrite_lines(config, rows)
    cat = catalog(config)
    entry = cat.entries()[0]

    bundle = cat.read_bundle(entry)

    assert bundle.available is False
    assert "unsafe stored report identity" in bundle.detail
    assert bundle.evidence is None


def test_bundle_of_a_slug_alias_refuses_to_serve_another_repos_report(tmp_path):
    # The slug rule is idempotent but not injective: 'alpha!!' and 'alpha' resolve
    # to the same directory. So a hand-edited slug would otherwise serve the real
    # alpha report — complete and hash-verified — under a feed identity that
    # contradicts it. Rejected before any IO, and named rather than hidden.
    config = cfg(tmp_path)
    write_eval(config, slug="alpha")
    catalog(config).reconcile()
    rows = lines(config)
    rows[0]["repo_slug"] = "alpha!!"
    rewrite_lines(config, rows)
    cat = catalog(config)
    entry = cat.entries()[0]

    bundle = cat.read_bundle(entry)

    assert bundle.available is False
    assert "incoherent stored report identity" in bundle.detail
    assert "alpha!!" in bundle.detail
    assert (bundle.evidence, bundle.report_md, bundle.verified) == (None, None, False)


def test_bundle_of_a_safe_but_wrong_source_identity_is_refused(tmp_path):
    # A retargeted line: every segment is a legal path component and the directory
    # it names holds a real report — a *different* one. Containment cannot catch
    # that; only the key can, because it is a digest of exactly the identity the
    # report was published under.
    config = cfg(tmp_path)
    write_eval(config, task_id=TASK_A, evaluation_id="20260730T100000Z")
    write_eval(config, task_id=TASK_B, evaluation_id="20260730T110000Z")
    catalog(config).reconcile()
    rows = lines(config)
    retargeted = next(row for row in rows if row["task_id"] == TASK_A)
    retargeted["task_id"] = TASK_B
    retargeted["evaluation_id"] = "20260730T110000Z"
    rewrite_lines(config, rows)
    cat = catalog(config)
    entry = next(item for item in cat.entries() if item.seq == retargeted["seq"])

    bundle = cat.read_bundle(entry)

    assert bundle.available is False
    assert "does not derive from the stored source identity" in bundle.detail
    assert bundle.semantic_report is None
    # The report it tried to point at is still served under its own key.
    other = next(item for item in cat.entries() if item.seq != retargeted["seq"])
    assert cat.read_bundle(other).available is True


@pytest.mark.parametrize(
    "name, body, parsed",
    [
        (EVIDENCE_NAME, "[]", []),
        (STATIC_METRICS_NAME, "42", 42),
    ],
)
def test_bundle_serves_a_non_object_artifact_as_the_value_it_holds(
    tmp_path, name, body, parsed
):
    # Export is the import path, so it must hand over what the bytes the manifest
    # hashed actually mean. Narrowing a non-object body to null would report "no
    # content" for an intact, hash-matching artifact — and would disagree with the
    # per-task detail route, which returns whatever the file parses to.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    (eval_dir / name).write_text(body, encoding="utf-8")
    cat = catalog(config)
    entry = cat.reconcile()[0]

    bundle = cat.read_bundle(entry)

    assert bundle.available is True and bundle.detail is None
    served = {
        EVIDENCE_NAME: bundle.evidence,
        STATIC_METRICS_NAME: bundle.static_metrics,
    }
    assert served[name] == parsed
    # Exactly the bytes publication hashed, so this is a verified import.
    assert bundle.verified is True and bundle.mismatched == ()


@pytest.mark.parametrize("damage", ["absent", "repeated", "contradictory", "malformed"])
def test_bundle_without_a_usable_digest_reports_the_artifact_unverified(
    tmp_path, damage
):
    # A digest that was never recorded, that a second manifest row repeats or
    # contradicts, or that cannot be a sha256 at all was not recorded the one way
    # publication records one. Claiming verification for it would empty the
    # content-hash contract that is the reason a consumer trusts an imported
    # report; the artifact is still served, because only the manifest is damaged.
    config = cfg(tmp_path)
    write_eval(config)
    catalog(config).reconcile()
    rows = lines(config)
    published = next(a for a in rows[0]["artifacts"] if a["name"] == EVIDENCE_NAME)
    kept = [a for a in rows[0]["artifacts"] if a["name"] != EVIDENCE_NAME]
    extra = {
        "absent": [],
        # The case a value comparison cannot catch: both rows carry the *real*
        # digest, so every check a lenient reader could make passes — and a
        # manifest publication never wrote would be reported as a fully verified
        # import.
        "repeated": [published, dict(published)],
        "contradictory": [
            {"name": EVIDENCE_NAME, "size": 1, "sha256": "a" * 64},
            {"name": EVIDENCE_NAME, "size": 1, "sha256": "b" * 64},
        ],
        "malformed": [{"name": EVIDENCE_NAME, "size": 1, "sha256": "not-a-digest"}],
    }[damage]
    rows[0]["artifacts"] = kept + extra
    rewrite_lines(config, rows)
    cat = catalog(config)
    entry = cat.entries()[0]

    bundle = cat.read_bundle(entry)

    assert bundle.available is True
    assert bundle.unverified == (EVIDENCE_NAME,)
    assert bundle.mismatched == ()
    assert bundle.verified is False
    assert bundle.evidence["target"]["task_id"] == TASK_A
    # The artifacts that do carry a usable digest are still checked normally.
    assert len(bundle.entry.artifacts) == len(kept) + len(extra)


def test_bundle_surfaces_an_artifact_edited_since_publication(tmp_path):
    # Not an availability failure: the content is still served, because only the
    # consumer can decide what an edited report means for its import. Recording
    # the digests is what makes the edit impossible to miss.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    (eval_dir / SEMANTIC_REPORT_MD_NAME).write_text("# tampered\n", encoding="utf-8")

    bundle = cat.read_bundle(entry)

    assert bundle.available is True
    assert bundle.mismatched == (SEMANTIC_REPORT_MD_NAME,)
    assert bundle.verified is False
    assert bundle.report_md == "# tampered\n"


def test_bundle_of_an_unparseable_artifact_fails_closed(tmp_path):
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    (eval_dir / STATIC_METRICS_NAME).write_text("{not json", encoding="utf-8")

    bundle = cat.read_bundle(entry)

    assert bundle.available is False
    assert STATIC_METRICS_NAME in bundle.detail
    assert bundle.static_metrics is None


@pytest.mark.parametrize("name", REQUIRED_ARTIFACTS)
def test_bundle_of_a_symlinked_artifact_serves_no_outside_bytes(tmp_path, name):
    # The directory is contained, but a path-based read follows links, so an
    # artifact replaced after publication by a link out of the tree would be
    # exported under this entry's key — as verbatim text in report_md, and as any
    # parseable outside JSON in the three parsed slots. Containment has to hold on
    # what each fixed name actually opens.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    outside = repo(tmp_path).path / "docs" / "private.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    # Readable text *and* parseable JSON, so no slot could refuse it merely for
    # having the wrong shape.
    outside.write_text('{"secret": "target repo content"}', encoding="utf-8")
    link = eval_dir / name
    link.unlink()
    link.symlink_to(outside)
    # The link is live, so the refusal below is the containment check answering
    # and not a fixture that never resolved to anything.
    assert link.read_text(encoding="utf-8") == '{"secret": "target repo content"}'

    bundle = cat.read_bundle(entry)

    assert bundle.available is False
    # All-or-nothing: no slot is populated, and nothing outside the tree reaches
    # the serialized response either.
    assert (bundle.evidence, bundle.static_metrics) == (None, None)
    assert (bundle.semantic_report, bundle.report_md) == (None, None)
    assert "target repo content" not in json.dumps(bundle.to_dict())
    # Damaged, deliberately not pruned: retention removing artifacts is this
    # feed's designed end state, so reporting a tampered tree as that end state
    # would hide it from the operator who can repair it.
    assert "damaged" in bundle.detail and "symbolic link" in bundle.detail
    assert "no longer retained" not in bundle.detail
    assert "incomplete" not in bundle.detail
    assert name in bundle.detail
    # Still auditable: the entry and its publication-time hashes are untouched.
    assert len(bundle.entry.artifacts) == len(REQUIRED_ARTIFACTS)


def test_bundle_swapped_after_the_census_still_parses_the_opened_files(
    tmp_path, monkeypatch
):
    # The static case above is only half the guarantee: naming the damaged
    # artifacts before the read is a pathname check, and the read walks each name
    # again, so a swap landing between them makes the check describe one object
    # and the export carry another. Fired here from inside the read, at the moment
    # the first artifact has been opened and is being validated — the exact window
    # a check-then-reopen bundle is exposed in.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    outside = repo(tmp_path).path / "docs" / "private.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text('{"secret": "target repo content"}', encoding="utf-8")
    artifact = eval_dir / EVIDENCE_NAME
    real_fstat = os.fstat
    swapped = False

    def _swap_then_stat(fd):
        nonlocal swapped
        if not swapped:
            swapped = True
            artifact.unlink()
            artifact.symlink_to(outside)
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", _swap_then_stat)
    bundle = cat.read_bundle(entry)

    # The swap really happened mid-read (a bundle that never validates an open
    # descriptor would leave this False and the assertions below vacuous).
    assert swapped and artifact.is_symlink()
    assert bundle.available is True
    assert bundle.evidence["target"]["task_id"] == TASK_A
    assert "target repo content" not in json.dumps(bundle.to_dict())
    # The bytes came from the object that was opened, so they still hash to what
    # publication recorded.
    assert bundle.verified is True and bundle.mismatched == ()


def test_bundle_reads_and_publishes_through_a_symlinked_data_dir(tmp_path):
    # Containment is enforced at each artifact's own hop, not by refusing every
    # link above it, so a hub whose data dir is itself reached through a symlink
    # (a relocated volume, macOS's own /var -> /private/var) still publishes and
    # exports normally. A rule that rejected any link in the path would take every
    # report on such a deployment out of the feed.
    (tmp_path / "real-data").mkdir()
    (tmp_path / "hub-data").symlink_to(tmp_path / "real-data", target_is_directory=True)
    config = cfg(tmp_path)
    write_eval(config)
    cat = catalog(config)

    entry = cat.reconcile()[0]
    bundle = cat.read_bundle(entry)

    assert bundle.available is True and bundle.detail is None
    assert bundle.report_md.startswith("# Semantic Task Evaluation")
    assert bundle.verified is True


# -- raw artifact bytes --------------------------------------------------------


def recorded(entry: ReportCatalogEntry, name: str):
    """The entry's manifest row for one artifact (what publication recorded)."""
    return next(artifact for artifact in entry.artifacts if artifact.name == name)


def forbid_artifact_open(monkeypatch, *, error: OSError | None = None) -> None:
    """Fail (or raise ``error``) if any published artifact file is opened at all.

    Hooks the one gate every read of an artifact's bytes has to pass, so "decided
    before any IO" is asserted rather than assumed, and stays asserted whatever
    the read path is built out of. Unrelated opens (pytest's own, the catalog
    file) delegate untouched.
    """
    real_open = os.open

    def _guard(path, flags, *args, **kwargs):
        if os.path.basename(os.fspath(path)) in REQUIRED_ARTIFACTS:
            if error is not None:
                raise error
            pytest.fail(f"opened {path}")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _guard)


@pytest.mark.parametrize("name", REQUIRED_ARTIFACTS)
def test_raw_artifact_serves_the_bytes_the_manifest_hashed(tmp_path, name):
    # The whole point of the route: what comes back must be the persisted file,
    # not a re-serialization of it, so the entry's recorded size/sha256 are
    # verifiable by the consumer rather than only by the hub.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]

    read = cat.read_artifact_bytes(entry, name)

    assert read.data == (eval_dir / name).read_bytes()
    assert read.reason is None and read.detail is None
    manifest = recorded(entry, name)
    assert hashlib.sha256(read.data).hexdigest() == manifest.sha256
    assert len(read.data) == manifest.size
    assert (read.integrity, read.recorded_sha256) == ("verified", manifest.sha256)


def test_raw_artifact_round_trips_unicode_and_exact_trailing_bytes(tmp_path):
    # Byte-for-byte means the parts a parse-and-re-serialize path silently
    # rewrites: non-ASCII escaped or not, CRLF, trailing spaces, and the presence
    # or absence of a final newline. Each of those changes the digest, so a
    # consumer that re-hashed a normalized body would see a false mismatch.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    bodies = {
        # Identity fields stay coherent (that is publication's rule, not this
        # route's); the exotic bytes ride in the fields around them.
        EVIDENCE_NAME: '{"target": {"source": "archived", "note": "é任务"}, "runs": []}',
        STATIC_METRICS_NAME: '{"note": "tab\\tand — dash"}   ',
        SEMANTIC_REPORT_NAME: '{"target_source": "archived", "summary": "café"}',
        SEMANTIC_REPORT_MD_NAME: "# report\r\n\r\n- café   \n\n",
    }
    for name, body in bodies.items():
        (eval_dir / name).write_text(body, encoding="utf-8")
    cat = catalog(config)
    entry = cat.reconcile()[0]

    for name, body in bodies.items():
        read = cat.read_artifact_bytes(entry, name)
        assert read.data == body.encode("utf-8")
        assert read.integrity == "verified"
        assert len(read.data) == recorded(entry, name).size


@pytest.mark.parametrize(
    "name", ["notes.txt", "../evidence.json", "evidence.json.bak", "", "."]
)
def test_raw_artifact_of_a_name_outside_the_published_set_reads_nothing(
    tmp_path, monkeypatch, name
):
    # ``name`` selects from a closed set; it is never a path fragment. A file that
    # genuinely sits in the evaluation dir under another name is still not a
    # published artifact, which is the same rule that makes traversal inert. The
    # decision is taken before any IO, which is asserted rather than assumed.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    (eval_dir / "notes.txt").write_text("operator scratch\n", encoding="utf-8")
    cat = catalog(config)
    entry = cat.reconcile()[0]
    forbid_artifact_open(monkeypatch)

    read = cat.read_artifact_bytes(entry, name)

    assert read.data is None
    assert read.reason == ARTIFACT_UNKNOWN
    assert "not a published report artifact" in read.detail


def test_raw_artifact_of_a_pruned_report_is_explicitly_not_retained(tmp_path):
    # The designed end state: the entry outlives its artifacts, so this is a
    # published report whose bytes are gone — distinct from an unknown key.
    import shutil

    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    shutil.rmtree(eval_dir)

    read = cat.read_artifact_bytes(entry, EVIDENCE_NAME)

    assert read.data is None
    assert read.reason == ARTIFACT_NOT_RETAINED
    assert "no longer retained" in read.detail
    # Still auditable: the digest recorded at publication survives the prune.
    assert recorded(entry, EVIDENCE_NAME).sha256


def test_raw_artifact_serves_a_survivor_of_a_partially_pruned_set(tmp_path):
    # Deliberately unlike the bundle's all-or-nothing rule. That rule protects an
    # *import* from being taken for a whole report; verifying one artifact's bytes
    # against one recorded digest needs nothing from the other three.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    (eval_dir / SEMANTIC_REPORT_MD_NAME).unlink()

    survivor = cat.read_artifact_bytes(entry, EVIDENCE_NAME)
    removed = cat.read_artifact_bytes(entry, SEMANTIC_REPORT_MD_NAME)

    assert survivor.data == (eval_dir / EVIDENCE_NAME).read_bytes()
    assert survivor.integrity == "verified"
    assert (removed.data, removed.reason) == (None, ARTIFACT_NOT_RETAINED)
    # The bundle still fails closed on the same directory: the two rules coexist
    # because they answer different questions.
    assert cat.read_bundle(entry).available is False


def test_raw_artifact_serves_bytes_edited_since_publication_as_mismatched(tmp_path):
    # Withholding an edited artifact would withhold the evidence of the edit from
    # the one caller equipped to prove it. The header-level state says so instead.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    (eval_dir / SEMANTIC_REPORT_MD_NAME).write_text("# tampered\n", encoding="utf-8")

    read = cat.read_artifact_bytes(entry, SEMANTIC_REPORT_MD_NAME)

    assert read.data == b"# tampered\n"
    assert read.integrity == "mismatched"
    # The recorded digest is still handed back, so the consumer can name the
    # discrepancy without re-reading the feed page it came from.
    assert read.recorded_sha256 == recorded(entry, SEMANTIC_REPORT_MD_NAME).sha256


def test_raw_artifact_without_a_usable_digest_is_served_unverified(tmp_path):
    # Nothing was compared, which is a weaker claim than a match — and the bytes
    # are still the ones on disk, so they are still served.
    config = cfg(tmp_path)
    write_eval(config)
    catalog(config).reconcile()
    rows = lines(config)
    rows[0]["artifacts"] = [
        artifact
        for artifact in rows[0]["artifacts"]
        if artifact["name"] != EVIDENCE_NAME
    ]
    rewrite_lines(config, rows)
    cat = catalog(config)
    entry = cat.entries()[0]

    read = cat.read_artifact_bytes(entry, EVIDENCE_NAME)

    assert read.data is not None and read.reason is None
    assert (read.integrity, read.recorded_sha256) == ("unverified", None)
    # An artifact whose manifest row survived is still checked normally.
    assert cat.read_artifact_bytes(entry, STATIC_METRICS_NAME).integrity == "verified"


@pytest.mark.parametrize(
    "field, value, fragment",
    [
        ("task_id", "..", "unsafe stored report identity"),
        ("repo_slug", "alpha!!", "incoherent stored report identity"),
    ],
)
def test_raw_artifact_of_a_damaged_identity_line_reads_no_file(
    tmp_path, monkeypatch, field, value, fragment
):
    # Same two audits the bundle applies, and for the same reason: a hand-edited
    # line must not steer a read out of the tree, and a slug alias must not serve
    # the real repo's artifact under a contradicting identity. Both decide before
    # any IO, which is asserted rather than assumed.
    config = cfg(tmp_path)
    write_eval(config, slug="alpha")
    catalog(config).reconcile()
    rows = lines(config)
    rows[0][field] = value
    rewrite_lines(config, rows)
    cat = catalog(config)
    entry = cat.entries()[0]
    forbid_artifact_open(monkeypatch)

    read = cat.read_artifact_bytes(entry, EVIDENCE_NAME)

    assert read.data is None
    assert read.reason == ARTIFACT_IDENTITY
    assert fragment in read.detail


def test_raw_artifact_that_will_not_read_is_a_fault_not_an_absence(
    tmp_path, monkeypatch
):
    # A present-but-unreadable file is a hub-side fault. Reporting it as "no longer
    # retained" would tell a consumer the report was pruned, which is a permanent
    # answer to a transient problem.
    config = cfg(tmp_path)
    write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    forbid_artifact_open(monkeypatch, error=OSError(errno.EIO, "input/output error"))

    read = cat.read_artifact_bytes(entry, EVIDENCE_NAME)

    assert read.data is None
    assert read.reason == ARTIFACT_UNREADABLE
    assert EVIDENCE_NAME in read.detail


def test_raw_artifact_symlinked_out_of_its_dir_is_refused_not_followed(tmp_path):
    # The *directory* is contained, but a path-based read follows symlinks, so
    # containment has to hold on what the fixed name actually opens. Otherwise an
    # artifact swapped after publication for a link out of the tree serves those
    # outside bytes — a target-repo file included — under this entry's published
    # key, which is exactly what this read path promises it cannot do.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    outside = repo(tmp_path).path / "docs" / "private.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("# target repo content\n", encoding="utf-8")
    link = eval_dir / SEMANTIC_REPORT_MD_NAME
    link.unlink()
    link.symlink_to(outside)
    # The link is live, so the refusal below is the containment check answering and
    # not a fixture that never resolved to anything in the first place.
    assert link.read_bytes() == b"# target repo content\n"

    read = cat.read_artifact_bytes(entry, SEMANTIC_REPORT_MD_NAME)

    assert read.data is None
    # A damaged artifact tree, deliberately not `not_retained`: answering "pruned"
    # would report tampering as retention's designed end state and hide it.
    assert read.reason == ARTIFACT_UNREADABLE
    assert "symbolic link" in read.detail
    # Per artifact, like every other outcome here — the untouched siblings verify.
    assert cat.read_artifact_bytes(entry, EVIDENCE_NAME).integrity == "verified"


def test_raw_artifact_swapped_after_validation_still_serves_the_opened_file(
    tmp_path, monkeypatch
):
    # The static case above is only half the guarantee: a read that validates a
    # *pathname* and then opens that pathname walks the tree twice, so a swap
    # landing between the two makes the check describe one file and the response
    # carry another. Here the swap is fired from inside the read itself, at the
    # moment the artifact has been opened and is being validated — the exact window
    # a check-then-reopen implementation is exposed in. The bytes must come from
    # the object that was opened, so the swap changes nothing.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    outside = repo(tmp_path).path / "docs" / "private.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("# target repo content\n", encoding="utf-8")
    artifact = eval_dir / SEMANTIC_REPORT_MD_NAME
    published = artifact.read_bytes()
    real_fstat = os.fstat
    swapped = False

    def _swap_then_stat(fd):
        nonlocal swapped
        if not swapped:
            swapped = True
            artifact.unlink()
            artifact.symlink_to(outside)
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", _swap_then_stat)
    read = cat.read_artifact_bytes(entry, SEMANTIC_REPORT_MD_NAME)

    # The swap really happened mid-read (a read that never validates an open
    # descriptor would leave this False and the assertions below vacuous).
    assert swapped and artifact.is_symlink()
    assert read.data == published
    assert b"target repo content" not in read.data
    assert read.integrity == "verified"


def test_raw_artifact_symlink_loop_is_a_reported_fault_not_a_raised_one(tmp_path):
    # A damaged artifact tree must stay inside the structured result. Resolving a
    # loop by pathname raises instead of answering — `RuntimeError` on Python
    # <= 3.12, which this project still supports — so a read built on `resolve()`
    # escaped its own contract and surfaced as an unclassified crash. Opening
    # answers with ELOOP on every supported version, which is a reason, not an
    # exception.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    artifact = eval_dir / SEMANTIC_REPORT_MD_NAME
    artifact.unlink()
    artifact.symlink_to(artifact)

    read = cat.read_artifact_bytes(entry, SEMANTIC_REPORT_MD_NAME)

    assert read.data is None
    assert read.reason == ARTIFACT_UNREADABLE
    assert SEMANTIC_REPORT_MD_NAME in read.detail
    assert cat.read_artifact_bytes(entry, EVIDENCE_NAME).integrity == "verified"


def test_raw_artifact_replaced_by_a_pipe_answers_instead_of_blocking(tmp_path):
    # Opening a FIFO for reading waits for a writer, forever if none comes. That
    # would hang a request thread of the resident service on a tampered tree, so
    # the open must not be a blocking one; the alarm turns a regression into a
    # failure instead of a suite that never finishes.
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]
    artifact = eval_dir / SEMANTIC_REPORT_MD_NAME
    artifact.unlink()
    os.mkfifo(artifact)

    def _timeout(signum, frame):
        raise AssertionError("the artifact read blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    try:
        read = cat.read_artifact_bytes(entry, SEMANTIC_REPORT_MD_NAME)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    # Not a regular file, so not the published artifact: the same answer absence
    # gets, rather than an IO fault it is not.
    assert read.data is None
    assert read.reason == ARTIFACT_NOT_RETAINED


def test_raw_artifact_reads_through_a_symlinked_data_dir(tmp_path):
    # Containment is enforced at the artifact's own hop, not by refusing every link
    # above it, so a hub whose data dir is itself reached through a symlink (a
    # relocated volume, or macOS's own /var -> /private/var) still reads normally.
    # A rule that rejected any symlink in the path would refuse every artifact on
    # such a deployment.
    (tmp_path / "real-data").mkdir()
    (tmp_path / "hub-data").symlink_to(tmp_path / "real-data", target_is_directory=True)
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    cat = catalog(config)
    entry = cat.reconcile()[0]

    read = cat.read_artifact_bytes(entry, EVIDENCE_NAME)

    assert read.data == (eval_dir / EVIDENCE_NAME).read_bytes()
    assert read.integrity == "verified"


# -- disabled mode ------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda cat, rp: cat.entries(),
        lambda cat, rp: cat.find("rpt-x"),
        lambda cat, rp: cat.page(),
        lambda cat, rp: cat.read_bundle(
            ReportCatalogEntry(
                seq=1,
                report_key="rpt-x",
                cataloged_ts=None,
                repo_name="alpha",
                repo_slug="alpha",
                repo_path=None,
                task_id=TASK_A,
                evaluation_id="20260730T100000Z",
                generated_ts=None,
                target_source=SOURCE_ARCHIVED,
                archived=True,
            )
        ),
        lambda cat, rp: cat.read_artifact_bytes(
            ReportCatalogEntry(
                seq=1,
                report_key="rpt-x",
                cataloged_ts=None,
                repo_name="alpha",
                repo_slug="alpha",
                repo_path=None,
                task_id=TASK_A,
                evaluation_id="20260730T100000Z",
                generated_ts=None,
                target_source=SOURCE_ARCHIVED,
                archived=True,
            ),
            EVIDENCE_NAME,
        ),
        lambda cat, rp: cat.reconcile(),
        lambda cat, rp: cat.record_evaluation(rp, TASK_A, "20260730T100000Z"),
    ],
)
def test_disabled_catalog_fails_closed(tmp_path, call):
    config = cfg(tmp_path, enabled=False)
    write_eval(config)

    with pytest.raises(EvaluationDisabledError):
        call(catalog(config), repo(tmp_path))


def test_disabled_catalog_writes_nothing(tmp_path):
    config = cfg(tmp_path, enabled=False)
    write_eval(config)

    with pytest.raises(EvaluationDisabledError):
        catalog(config).reconcile()

    assert not (config.data_dir / CATALOG_SUBDIR).exists()
