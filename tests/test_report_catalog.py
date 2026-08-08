"""Tests for the global, append-only evaluation-report catalog (session-1).

Covers global identity and sequence allocation (including scoped
``evaluation_id`` collisions across repos), artifact-based eligibility (only a
durably complete L1+L2 set publishes), append durability and torn-tail/corrupt
recovery, deterministic idempotent reconciliation, the entry's hashes and
explicit provenance, retention visibility, the live semantic-evaluator hook, and
disabled-mode fail-closed. Session-2 adds the cursor-paginated feed and
read/export-by-key HTTP routes; session-3 the dashboard view.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.evaluation import (
    EVIDENCE_NAME,
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
)
from orch_hub.models import ManagedRepo
from orch_hub.report_catalog import (
    CATALOG_NAME,
    CATALOG_SUBDIR,
    CONFIG_UNAVAILABLE_DETAIL,
    PROTOCOL_UNAVAILABLE_DETAIL,
    REPORT_KEY_PREFIX,
    REQUIRED_ARTIFACTS,
    ReportCatalog,
    is_eligible,
    report_key,
)
from orch_hub.semantic import SEMANTIC_REPORT_MD_NAME, SEMANTIC_REPORT_NAME
from orch_hub.static_metrics import STATIC_METRICS_NAME

TASK_A = "2026-07-30-alpha-task"
TASK_B = "2026-07-30-beta-task"
FIXED_TS = dt.datetime(2026, 7, 31, 8, 0, 0, tzinfo=dt.timezone.utc)


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
) -> Path:
    """Write one evaluation dir directly, so eligibility is exercised structurally.

    Building fixtures at the artifact level (rather than through the evaluator) is
    the point: the catalog's contract is that eligibility comes from the files on
    disk, never from job/trigger state, so the tests must be able to produce a
    half-written or hand-damaged dir the evaluator would never emit.
    """
    eval_dir = config.data_dir / "evaluations" / slug / task_id / evaluation_id
    eval_dir.mkdir(parents=True, exist_ok=True)
    bodies = {
        EVIDENCE_NAME: json.dumps(
            {
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
        ),
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


def lines(config: Config) -> list[dict]:
    path = config.data_dir / CATALOG_SUBDIR / CATALOG_NAME
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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


# -- artifact-based eligibility ----------------------------------------------


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


def test_complete_set_publishes_and_records_archived_eligibility(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, slug="alpha", task_id=TASK_A, archived=True)
    write_eval(config, slug="beta", task_id=TASK_B, archived=False)

    added = catalog(config).reconcile()

    by_slug = {entry.repo_slug: entry for entry in added}
    assert by_slug["alpha"].archived is True
    assert by_slug["alpha"].target_source == SOURCE_ARCHIVED
    assert is_eligible(by_slug["alpha"]) is True
    # An active-task report is cataloged for audit but is not a feed item: the
    # eligibility fact is stamped from the artifact, not re-read from the repo.
    assert by_slug["beta"].archived is False
    assert is_eligible(by_slug["beta"]) is False


def test_live_hook_ignores_an_incomplete_evaluation(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, artifacts=(EVIDENCE_NAME, STATIC_METRICS_NAME))

    assert catalog(config).record_evaluation(repo(tmp_path), TASK_A, "20260730T100000Z") is None
    assert catalog(config).entries() == []


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
    # consumer never reads absence as "no protocol" / "default config".
    assert prov.protocol_available is False
    assert prov.protocol_detail == PROTOCOL_UNAVAILABLE_DETAIL
    assert prov.config_available is False
    assert prov.config_detail == CONFIG_UNAVAILABLE_DETAIL


def test_entry_round_trips_through_the_persisted_line(tmp_path):
    config = cfg(tmp_path)
    write_eval(config, run_ids=("r1",))
    original = catalog(config).reconcile()[0]

    # Re-read by a fresh instance: the persisted line must reconstruct the entry
    # field-for-field, since the catalog is append-only and never migrated.
    reloaded = catalog(config).entries()[0]

    assert reloaded == original
    assert reloaded.to_dict() == lines(config)[0]


def test_degraded_evidence_still_publishes_with_explicit_unavailable_provenance(tmp_path):
    config = cfg(tmp_path)
    eval_dir = write_eval(config)
    (eval_dir / EVIDENCE_NAME).write_text("{not json", encoding="utf-8")

    entry = catalog(config).reconcile()[0]

    # The report itself is intact and publishable; only its cohorting degrades.
    assert entry.repo_path is None
    assert entry.provenance.runs_available is False
    assert entry.provenance.runs_detail == "evidence pack unreadable"
    assert entry.provenance.git_available is False


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


# -- disabled mode ------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda cat, rp: cat.entries(),
        lambda cat, rp: cat.find("rpt-x"),
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
