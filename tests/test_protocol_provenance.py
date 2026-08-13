"""Tests for per-run protocol provenance capture and its end-of-run check.

Everything runs against fake repos and run dirs in ``tmp_path``: the module reads
one file from a repo and writes one artifact into a run dir, so no registry, no
subprocess, and no orchestrator is involved.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from orch_hub.protocol_provenance import (
    ARTIFACT_NAME,
    DEPLOY_LOCK_NAME,
    LOCK_MAX_BYTES,
    PROTOCOL_SCHEMA_VERSION,
    STABILITY_CHANGED,
    STABILITY_STABLE,
    STABILITY_UNVERIFIED,
    ProtocolSnapshot,
    artifact_path,
    capture_protocol,
    read_deploy_lock,
    read_protocol,
    settle_protocol,
    write_protocol,
)

CAPTURED_AT = dt.datetime(2026, 8, 13, 9, 0, 0, tzinfo=dt.timezone.utc)
ENDED_AT = dt.datetime(2026, 8, 13, 11, 30, 0, tzinfo=dt.timezone.utc)

COMMIT = "6b62754694c289fc8822562cec22ab774e071b2c"
OTHER_COMMIT = "aa11bb22cc33dd44ee55ff6677889900aabbccdd"
PAYLOAD = "533a87d311bd99d9ae9ba25f1385a3c22396b4e704a8b75a2ad9b9b36614f450"
OTHER_PAYLOAD = "1" * 64


def write_lock(
    repo_path: Path,
    *,
    commit: object = COMMIT,
    payload: object = PAYLOAD,
    source_repo: object = "ai-native-deployment",
    schema_version: object = 1,
) -> None:
    """Write a deployment lock shaped like the real ``aii-2`` output."""
    body = {
        "canonical_payload_sha256": payload,
        "deployed_files": [
            {
                "canonical_relative_path": "canonical/protocols/conduct.md",
                "canonical_sha256": "0" * 64,
                "canonical_size_bytes": 2176,
                "target_relative_path": ".ai-protocol/protocols/conduct.md",
            }
        ],
        "schema_version": schema_version,
        "source_git_commit": commit,
        "source_repo": source_repo,
    }
    (repo_path / DEPLOY_LOCK_NAME).write_text(
        json.dumps(body, indent=1), encoding="utf-8"
    )


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo_path = tmp_path / name
    repo_path.mkdir(parents=True)
    return repo_path


def make_run_dir(tmp_path: Path, name: str = "run") -> Path:
    run_dir = tmp_path / "runs" / name
    run_dir.mkdir(parents=True)
    return run_dir


# -- reading the deployment lock ---------------------------------------------


def test_read_deploy_lock_validates_and_normalizes(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    write_lock(repo_path, commit=COMMIT.upper())

    snapshot = read_deploy_lock(repo_path)

    assert snapshot.available is True
    # A hex object name is case-insensitive; the cohort key is not, so it is
    # normalized once here rather than at every consumer.
    assert snapshot.source_git_commit == COMMIT
    assert snapshot.canonical_payload_sha256 == PAYLOAD
    assert snapshot.source_repo == "ai-native-deployment"
    assert snapshot.lock_schema_version == 1
    assert snapshot.detail is None
    assert snapshot.stability == STABILITY_UNVERIFIED


def test_read_deploy_lock_missing_file_is_unavailable_with_reason(
    tmp_path: Path,
) -> None:
    snapshot = read_deploy_lock(make_repo(tmp_path))

    assert snapshot.available is False
    assert snapshot.source_git_commit is None
    assert snapshot.canonical_payload_sha256 is None
    assert DEPLOY_LOCK_NAME in (snapshot.detail or "")


def test_read_deploy_lock_malformed_shapes_are_unavailable(tmp_path: Path) -> None:
    cases = {
        "unparseable": "{not json",
        "not_object": "[1, 2, 3]",
        "missing_commit": json.dumps({"canonical_payload_sha256": PAYLOAD}),
        "short_commit": json.dumps(
            {"source_git_commit": COMMIT[:12], "canonical_payload_sha256": PAYLOAD}
        ),
        "non_hex_commit": json.dumps(
            {"source_git_commit": "z" * 40, "canonical_payload_sha256": PAYLOAD}
        ),
        "commit_not_a_string": json.dumps(
            {"source_git_commit": 12345, "canonical_payload_sha256": PAYLOAD}
        ),
        "missing_payload": json.dumps({"source_git_commit": COMMIT}),
        "short_payload": json.dumps(
            {"source_git_commit": COMMIT, "canonical_payload_sha256": PAYLOAD[:40]}
        ),
    }
    for name, body in cases.items():
        repo_path = make_repo(tmp_path, name)
        (repo_path / DEPLOY_LOCK_NAME).write_text(body, encoding="utf-8")

        snapshot = read_deploy_lock(repo_path)

        assert snapshot.available is False, name
        assert snapshot.detail, name
        assert snapshot.verified_revision is None, name


def test_read_deploy_lock_accepts_sha256_object_names(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    write_lock(repo_path, commit="c" * 64)

    snapshot = read_deploy_lock(repo_path)

    assert snapshot.available is True
    assert snapshot.source_git_commit == "c" * 64


def test_read_deploy_lock_refuses_an_oversize_lock(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    (repo_path / DEPLOY_LOCK_NAME).write_bytes(b"x" * (LOCK_MAX_BYTES + 1))

    snapshot = read_deploy_lock(repo_path)

    assert snapshot.available is False
    assert "cap" in (snapshot.detail or "")


def test_read_deploy_lock_survives_an_unreadable_path(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    # A directory where the lock file belongs: the read fails with an OSError
    # that is not FileNotFoundError, which must still degrade rather than raise.
    (repo_path / DEPLOY_LOCK_NAME).mkdir()

    snapshot = read_deploy_lock(repo_path)

    assert snapshot.available is False
    assert snapshot.detail


# -- launch capture -----------------------------------------------------------


def test_capture_writes_a_validated_stamped_artifact(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)

    snapshot = capture_protocol(run_dir, repo_path, now=CAPTURED_AT)

    assert snapshot.available is True
    assert snapshot.captured_ts == CAPTURED_AT.isoformat()
    assert snapshot.stability == STABILITY_UNVERIFIED
    assert snapshot.checked_ts is None
    assert snapshot.source_git_commit == COMMIT
    persisted = json.loads((run_dir / ARTIFACT_NAME).read_text(encoding="utf-8"))
    assert persisted["schema_version"] == PROTOCOL_SCHEMA_VERSION
    assert persisted["source_git_commit"] == COMMIT
    assert persisted["canonical_payload_sha256"] == PAYLOAD
    assert read_protocol(run_dir) == snapshot


def test_capture_records_an_absent_lock_instead_of_raising(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)

    snapshot = capture_protocol(run_dir, repo_path, now=CAPTURED_AT)

    assert snapshot.available is False
    assert snapshot.captured_ts == CAPTURED_AT.isoformat()
    assert snapshot.verified_revision is None
    # The unavailability is durable: a later reader learns the hub looked and
    # found nothing, which is what keeps it from backfilling a revision.
    reloaded = read_protocol(run_dir)
    assert reloaded is not None
    assert reloaded.available is False
    assert reloaded.detail


def test_a_live_capture_is_not_publishable_until_its_end_check(tmp_path: Path) -> None:
    # The capture attests to the run's first moment only. Until the end check
    # confirms the deployment held, the run has a commit to audit and no commit
    # to publish: the deployment could still be updated under its later sessions,
    # and a report must not claim one revision for all of them.
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)

    captured = capture_protocol(run_dir, repo_path, now=CAPTURED_AT)

    assert captured.source_git_commit == COMMIT
    assert captured.verified_revision is None

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.verified_revision == COMMIT


# -- end-of-run stability check ----------------------------------------------


def test_settle_marks_an_unchanged_deployment_stable(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.stability == STABILITY_STABLE
    assert settled.checked_ts == ENDED_AT.isoformat()
    assert settled.detail is None
    assert settled.verified_revision == COMMIT
    assert read_protocol(run_dir) == settled


def test_settle_marks_a_mid_run_redeploy_changed(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    write_lock(repo_path, commit=OTHER_COMMIT, payload=OTHER_PAYLOAD)

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.stability == STABILITY_CHANGED
    # The captured revision stays readable for audit, but is no longer what the
    # run ran under end to end, so it is not publishable.
    assert settled.source_git_commit == COMMIT
    assert settled.verified_revision is None
    assert "source_git_commit" in (settled.detail or "")
    assert "canonical_payload_sha256" in (settled.detail or "")


def test_settle_treats_a_payload_only_change_as_changed(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    # Same source commit, different deployed bytes (a hand-edited or repaired
    # deployment): the sessions after it did not read the captured payload.
    write_lock(repo_path, payload=OTHER_PAYLOAD)

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.stability == STABILITY_CHANGED
    assert "canonical_payload_sha256" in (settled.detail or "")
    assert "source_git_commit" not in (settled.detail or "")


def test_settle_ignores_non_revision_lock_fields(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    write_lock(repo_path, source_repo="renamed-source", schema_version=2)

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.stability == STABILITY_STABLE
    # The capture's own identity fields are frozen; the end check writes only the
    # verdict.
    assert settled.source_repo == "ai-native-deployment"
    assert settled.lock_schema_version == 1


def test_settle_keeps_the_capture_when_the_lock_cannot_be_reread(
    tmp_path: Path,
) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    (repo_path / DEPLOY_LOCK_NAME).unlink()

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.stability == STABILITY_UNVERIFIED
    assert settled.checked_ts == ENDED_AT.isoformat()
    assert "unreadable at run end" in (settled.detail or "")
    # Not being able to look is not a contradiction, so the launch capture stands
    # for audit; it is not a confirmation either, and only a confirmed revision
    # may be published.
    assert settled.source_git_commit == COMMIT
    assert settled.verified_revision is None


def test_settle_runs_once_even_when_it_could_not_verify(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    (repo_path / DEPLOY_LOCK_NAME).unlink()
    settle_protocol(run_dir, repo_path, now=ENDED_AT)
    # A redeploy *after* the run ended must not be read as a mid-run change by a
    # second pass; the recorded check is what makes the window one-shot.
    write_lock(repo_path, commit=OTHER_COMMIT)

    again = settle_protocol(
        run_dir, repo_path, now=ENDED_AT + dt.timedelta(hours=1)
    )

    assert again is None
    reloaded = read_protocol(run_dir)
    assert reloaded is not None
    assert reloaded.stability == STABILITY_UNVERIFIED
    assert reloaded.checked_ts == ENDED_AT.isoformat()


def test_settle_declines_to_compare_an_unobserved_end(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    # Redeployed at some point between the real end and the scan that noticed it:
    # the lag is unbounded, so this difference cannot be attributed to the run.
    write_lock(repo_path, commit=OTHER_COMMIT)

    settled = settle_protocol(
        run_dir, repo_path, now=ENDED_AT, end_observed=False
    )

    assert settled is not None
    assert settled.stability == STABILITY_UNVERIFIED
    assert settled.checked_ts == ENDED_AT.isoformat()
    assert "hub was down" in (settled.detail or "")
    # Declining to compare protects the verdict's honesty, not the publication:
    # an end nobody observed closely enough confirms nothing either.
    assert settled.source_git_commit == COMMIT
    assert settled.verified_revision is None


def test_settle_never_backfills_an_unavailable_capture(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    # The lock appears only after the run: adopting it would attribute a revision
    # the run's sessions never loaded.
    write_lock(repo_path)

    assert settle_protocol(run_dir, repo_path, now=ENDED_AT) is None
    reloaded = read_protocol(run_dir)
    assert reloaded is not None
    assert reloaded.available is False
    assert reloaded.source_git_commit is None


def test_settle_is_a_no_op_for_a_run_with_no_capture(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_lock(repo_path)

    assert settle_protocol(run_dir, repo_path, now=ENDED_AT) is None
    assert not artifact_path(run_dir).exists()


# -- reading a persisted capture ---------------------------------------------


def test_read_protocol_distinguishes_absent_from_unusable(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path)

    # A run that predates the capture wrote no artifact at all.
    assert read_protocol(run_dir) is None

    artifact_path(run_dir).write_text("{not json", encoding="utf-8")
    corrupt = read_protocol(run_dir)
    assert corrupt is not None
    assert corrupt.available is False
    assert corrupt.detail


def test_read_protocol_fails_closed_on_inconsistent_artifacts(tmp_path: Path) -> None:
    cases = {
        "not_object": json.dumps([1, 2, 3]),
        "unknown_stability": json.dumps(
            {
                "available": True,
                "source_git_commit": COMMIT,
                "canonical_payload_sha256": PAYLOAD,
                "stability": "probably-fine",
            }
        ),
        "available_without_revision": json.dumps(
            {"available": True, "stability": STABILITY_STABLE}
        ),
        "available_with_malformed_revision": json.dumps(
            {
                "available": True,
                "source_git_commit": "nope",
                "canonical_payload_sha256": PAYLOAD,
                "stability": STABILITY_STABLE,
            }
        ),
        # 'stable' is what grants publication, so a verdict recording no check
        # that could have reached it is refused rather than trusted.
        "verdict_without_a_check": json.dumps(
            {
                "available": True,
                "source_git_commit": COMMIT,
                "canonical_payload_sha256": PAYLOAD,
                "stability": STABILITY_STABLE,
                "captured_ts": CAPTURED_AT.isoformat(),
            }
        ),
    }
    for name, body in cases.items():
        run_dir = make_run_dir(tmp_path, name)
        artifact_path(run_dir).write_text(body, encoding="utf-8")

        snapshot = read_protocol(run_dir)

        assert snapshot is not None, name
        assert snapshot.available is False, name
        assert snapshot.verified_revision is None, name
        assert snapshot.detail, name


def test_persisted_snapshots_round_trip(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path)
    snapshot = ProtocolSnapshot(
        available=True,
        source_git_commit=COMMIT,
        canonical_payload_sha256=PAYLOAD,
        source_repo="ai-native-deployment",
        lock_schema_version=1,
        captured_ts=CAPTURED_AT.isoformat(),
        stability=STABILITY_CHANGED,
        checked_ts=ENDED_AT.isoformat(),
        detail="deployed protocol changed during the run: ...",
    )

    write_protocol(run_dir, snapshot)

    assert read_protocol(run_dir) == snapshot
