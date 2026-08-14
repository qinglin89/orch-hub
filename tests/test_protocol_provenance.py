"""Tests for per-run protocol provenance capture and its end-of-run check.

Everything runs against fake repos and run dirs in ``tmp_path``: the module reads
two receipts plus the files they describe from a repo and writes one artifact into
a run dir, so no registry, no subprocess, and no orchestrator is involved.

The deployed payload is written for real — files on disk, hashed into the manifest
that vouches for them — so a test drifts a deployment the way an operator does, by
editing a file, and never by editing a digest into disagreement.

The last two sections cover the cross-run aggregate a *report* is published
under — unanimity over the per-run predicate, both of its failure modes, and the
fail-closed read of a persisted aggregate the report catalog holds as JSON.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from orch_hub.protocol_provenance import (
    ARTIFACT_NAME,
    DEPLOY_LOCK_NAME,
    DEPLOY_MANIFEST_NAME,
    LEGACY_VERDICT_DETAIL,
    LOCK_MAX_BYTES,
    MAX_LISTED_REVISIONS,
    PAYLOAD_MATCHED,
    PAYLOAD_MAX_FILE_BYTES,
    PAYLOAD_MAX_FILES,
    PAYLOAD_MISMATCHED,
    PAYLOAD_UNVERIFIABLE,
    PROTOCOL_SCHEMA_VERSION,
    STABILITY_CHANGED,
    STABILITY_DRIFTED,
    STABILITY_STABLE,
    STABILITY_UNVERIFIED,
    ProtocolIdentity,
    ProtocolSnapshot,
    artifact_path,
    capture_protocol,
    effective_protocol,
    effective_protocol_from_dict,
    read_deploy_lock,
    read_protocol,
    settle_protocol,
    verify_payload,
    write_protocol,
)

CAPTURED_AT = dt.datetime(2026, 8, 13, 9, 0, 0, tzinfo=dt.timezone.utc)
ENDED_AT = dt.datetime(2026, 8, 13, 11, 30, 0, tzinfo=dt.timezone.utc)

COMMIT = "6b62754694c289fc8822562cec22ab774e071b2c"
OTHER_COMMIT = "aa11bb22cc33dd44ee55ff6677889900aabbccdd"
PAYLOAD = "533a87d311bd99d9ae9ba25f1385a3c22396b4e704a8b75a2ad9b9b36614f450"
OTHER_PAYLOAD = "1" * 64

CONDUCT = ".ai-protocol/protocols/conduct.md"
RUNBOOK = ".ai-protocol/workflow/runbook.md"
DEPLOYED = {CONDUCT: "# Conduct\n\nReason from actual code.\n", RUNBOOK: "# Runbook\n"}


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
                "target_relative_path": CONDUCT,
            }
        ],
        "schema_version": schema_version,
        "source_git_commit": commit,
        "source_repo": source_repo,
    }
    (repo_path / DEPLOY_LOCK_NAME).write_text(
        json.dumps(body, indent=1), encoding="utf-8"
    )


def deploy_payload(
    repo_path: Path,
    *,
    files: dict[str, str] | None = None,
    commit: object = COMMIT,
    manifest_files: object | None = None,
) -> None:
    """Write the deployed files plus the manifest that vouches for their bytes.

    The other half of a real deploy: the lock names the payload, the manifest
    records what landed. Digests are taken from the bytes actually written, so a
    test that wants drift edits a file afterwards.
    """
    records = {}
    for relative, text in (DEPLOYED if files is None else files).items():
        target = repo_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        target.write_bytes(data)
        records[relative] = {
            "canonical_relative_path": f"canonical/{relative}",
            "mode": 420,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "target_relative_path": relative,
        }
    body = {
        "deployed_at": "2026-08-13T08:00:00+00:00",
        "files": records if manifest_files is None else manifest_files,
        "schema_version": 1,
        "source_git_commit": commit,
        "source_repo_path": "/src/ai-native-development",
        "target_repo_path": str(repo_path),
    }
    (repo_path / DEPLOY_MANIFEST_NAME).write_text(
        json.dumps(body, indent=1), encoding="utf-8"
    )


def deploy(repo_path: Path, *, commit: str = COMMIT, payload: str = PAYLOAD) -> None:
    """One coherent deployment: lock, manifest, and the files both describe."""
    write_lock(repo_path, commit=commit, payload=payload)
    deploy_payload(repo_path, commit=commit)


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


# -- comparing the deployed files against their receipt ------------------------
#
# The check that makes an identity worth publishing: a lock names a payload, and
# only this says the tree still holds it.


def test_verify_payload_matches_a_clean_deployment(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    deploy_payload(repo_path)

    check = verify_payload(repo_path, COMMIT)

    assert check.state == PAYLOAD_MATCHED
    assert check.files == len(DEPLOYED)  # the whole payload, not a sample
    assert check.detail is None


def test_verify_payload_catches_an_edited_deployed_file(tmp_path: Path) -> None:
    # The case a lock comparison cannot see: both receipts are untouched, and the
    # protocol text the sessions read is not the one the identity names.
    repo_path = make_repo(tmp_path)
    deploy_payload(repo_path)
    (repo_path / CONDUCT).write_text("# Conduct\n\nDo whatever.\n", encoding="utf-8")

    check = verify_payload(repo_path, COMMIT)

    assert check.state == PAYLOAD_MISMATCHED
    assert check.files == len(DEPLOYED)
    assert CONDUCT in (check.detail or "")


def test_verify_payload_catches_a_same_size_edit(tmp_path: Path) -> None:
    # Size is only the cheap half of the comparison; equal length must still be
    # hashed, or an in-place character swap would pass.
    repo_path = make_repo(tmp_path)
    deploy_payload(repo_path)
    original = (repo_path / CONDUCT).read_text(encoding="utf-8")
    (repo_path / CONDUCT).write_text(original.replace("actual", "ACTUAL"), "utf-8")

    check = verify_payload(repo_path, COMMIT)

    assert check.state == PAYLOAD_MISMATCHED
    assert "content differs" in (check.detail or "")


def test_verify_payload_catches_a_missing_deployed_file(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    deploy_payload(repo_path)
    (repo_path / RUNBOOK).unlink()

    check = verify_payload(repo_path, COMMIT)

    assert check.state == PAYLOAD_MISMATCHED
    assert RUNBOOK in (check.detail or "")


def test_verify_payload_refuses_to_follow_a_linked_deployed_file(
    tmp_path: Path,
) -> None:
    # A deploy writes regular files. A link where one belongs is drift in its own
    # right, and following it would hash bytes from outside the payload.
    repo_path = make_repo(tmp_path)
    deploy_payload(repo_path)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text(DEPLOYED[CONDUCT], encoding="utf-8")
    (repo_path / CONDUCT).unlink()
    (repo_path / CONDUCT).symlink_to(elsewhere)

    check = verify_payload(repo_path, COMMIT)

    assert check.state == PAYLOAD_MISMATCHED
    assert "not a regular file" in (check.detail or "")


def test_verify_payload_refuses_a_manifest_from_another_deploy(
    tmp_path: Path,
) -> None:
    # Without the shared commit the two receipts describe different deployments,
    # and hashing against the wrong one would answer a question nobody asked.
    repo_path = make_repo(tmp_path)
    deploy_payload(repo_path, commit=OTHER_COMMIT)

    check = verify_payload(repo_path, COMMIT)

    assert check.state == PAYLOAD_UNVERIFIABLE
    assert check.files is None
    assert DEPLOY_MANIFEST_NAME in (check.detail or "")


def test_verify_payload_without_a_manifest_is_unverifiable(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    deploy_payload(repo_path)
    (repo_path / DEPLOY_MANIFEST_NAME).unlink()

    check = verify_payload(repo_path, COMMIT)

    assert check.state == PAYLOAD_UNVERIFIABLE
    assert DEPLOY_MANIFEST_NAME in (check.detail or "")


def test_verify_payload_refuses_a_damaged_receipt(tmp_path: Path) -> None:
    # Not knowing is not the same as knowing the payload is wrong: a receipt this
    # check cannot read makes the whole comparison unverifiable rather than
    # partial, since the records it *could* read say nothing about the rest.
    good = {
        "canonical_relative_path": f"canonical/{CONDUCT}",
        "sha256": hashlib.sha256(DEPLOYED[CONDUCT].encode()).hexdigest(),
        "size_bytes": len(DEPLOYED[CONDUCT].encode()),
        "target_relative_path": CONDUCT,
    }
    cases = {
        "not_an_object": {CONDUCT: "just a hash"},
        "escaping_path": {CONDUCT: {**good, "target_relative_path": "../outside.md"}},
        "absolute_path": {CONDUCT: {**good, "target_relative_path": "/etc/passwd"}},
        "no_digest": {CONDUCT: {**good, "sha256": "nope"}},
        "no_size": {CONDUCT: {k: v for k, v in good.items() if k != "size_bytes"}},
        "oversize_record": {
            CONDUCT: {**good, "size_bytes": PAYLOAD_MAX_FILE_BYTES + 1}
        },
        "empty": {},
        "not_a_mapping": [good],
    }
    for name, files in cases.items():
        repo_path = make_repo(tmp_path, name)
        deploy_payload(repo_path, manifest_files=files)

        check = verify_payload(repo_path, COMMIT)

        assert check.state == PAYLOAD_UNVERIFIABLE, name
        assert check.files is None, name
        assert check.detail, name


def test_verify_payload_refuses_a_receipt_past_the_file_cap(tmp_path: Path) -> None:
    # Refused, never sampled: a bound that quietly truncated would publish an
    # identity backed by less than it claims.
    repo_path = make_repo(tmp_path)
    oversized = {
        f"file-{index}.md": {
            "sha256": "0" * 64,
            "size_bytes": 1,
            "target_relative_path": f"file-{index}.md",
        }
        for index in range(PAYLOAD_MAX_FILES + 1)
    }
    deploy_payload(repo_path, manifest_files=oversized)

    check = verify_payload(repo_path, COMMIT)

    assert check.state == PAYLOAD_UNVERIFIABLE
    assert str(PAYLOAD_MAX_FILES) in (check.detail or "")


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
    # confirms the deployment held, the run has an identity to audit and none to
    # publish: the deployment could still be updated under its later sessions,
    # and a report must not claim one payload for all of them.
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    deploy(repo_path)

    captured = capture_protocol(run_dir, repo_path, now=CAPTURED_AT)

    assert captured.source_git_commit == COMMIT
    assert captured.verified_identity is None

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.verified_identity == ProtocolIdentity(COMMIT, PAYLOAD)


# -- end-of-run stability check ----------------------------------------------


def test_settle_marks_an_intact_deployment_stable(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    deploy(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.stability == STABILITY_STABLE
    assert settled.checked_ts == ENDED_AT.isoformat()
    assert settled.detail is None
    # What the verdict rests on is recorded with it: the whole payload, compared.
    assert settled.payload_files == len(DEPLOYED)
    assert settled.verified_identity == ProtocolIdentity(COMMIT, PAYLOAD)
    assert read_protocol(run_dir) == settled


def test_settle_marks_an_edited_payload_drifted(tmp_path: Path) -> None:
    # The whole point of the check: the lock reads identically at both ends, and
    # the run's later sessions still read protocol text no revision holds. An
    # unchanged receipt is not a stable payload.
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    deploy(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    (repo_path / CONDUCT).write_text("# Conduct\n\nSkip the tests.\n", encoding="utf-8")

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.stability == STABILITY_DRIFTED
    assert CONDUCT in (settled.detail or "")
    # Auditable, and unpublishable — the same split every unconfirmed end gets.
    assert settled.source_git_commit == COMMIT
    assert settled.canonical_payload_sha256 == PAYLOAD
    assert settled.verified_identity is None
    assert read_protocol(run_dir) == settled


def test_settle_cannot_verify_a_payload_it_cannot_read(tmp_path: Path) -> None:
    # Distinct from drift, and distinct in the artifact: the hub could not look,
    # which is an absence of evidence rather than evidence of an edited payload.
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    deploy(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    (repo_path / DEPLOY_MANIFEST_NAME).unlink()

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.stability == STABILITY_UNVERIFIED
    assert "could not be verified" in (settled.detail or "")
    assert settled.source_git_commit == COMMIT
    assert settled.verified_identity is None


def test_settle_does_not_check_the_payload_of_a_replaced_deployment(
    tmp_path: Path,
) -> None:
    # A redeploy answers the question already: whatever the tree holds now belongs
    # to another identity, so hashing it against this run's receipt would only
    # restate the change under a name ('drifted') that means something else.
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    deploy(repo_path)
    capture_protocol(run_dir, repo_path, now=CAPTURED_AT)
    deploy(repo_path, commit=OTHER_COMMIT, payload=OTHER_PAYLOAD)

    settled = settle_protocol(run_dir, repo_path, now=ENDED_AT)

    assert settled is not None
    assert settled.stability == STABILITY_CHANGED
    assert settled.payload_files is None
    assert settled.verified_identity is None


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


def test_settle_ignores_non_identity_lock_fields(tmp_path: Path) -> None:
    repo_path = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    deploy(repo_path)
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
    stable = {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "available": True,
        "source_git_commit": COMMIT,
        "canonical_payload_sha256": PAYLOAD,
        "stability": STABILITY_STABLE,
        "checked_ts": ENDED_AT.isoformat(),
        "payload_files": 2,
    }
    cases = {
        "not_object": json.dumps([1, 2, 3]),
        "no_schema_version": json.dumps(
            {k: v for k, v in stable.items() if k != "schema_version"}
        ),
        # A field this version reads may not mean here what it meant there.
        "newer_schema": json.dumps(
            {**stable, "schema_version": PROTOCOL_SCHEMA_VERSION + 1}
        ),
        "unknown_stability": json.dumps({**stable, "stability": "probably-fine"}),
        "available_without_identity": json.dumps(
            {k: v for k, v in stable.items() if k != "source_git_commit"}
        ),
        "available_with_malformed_identity": json.dumps(
            {**stable, "source_git_commit": "nope"}
        ),
        # Half an identity is not one: the pair is verified and published whole.
        "available_without_payload_hash": json.dumps(
            {k: v for k, v in stable.items() if k != "canonical_payload_sha256"}
        ),
        # 'stable' is what grants publication, so a verdict recording no check
        # that could have reached it is refused rather than trusted.
        "verdict_without_a_check": json.dumps(
            {k: v for k, v in stable.items() if k != "checked_ts"}
        ),
        # ... and one recording no payload comparison is the same defect: under
        # this schema a stable verdict always names how many files it compared.
        "verdict_without_a_payload_count": json.dumps(
            {k: v for k, v in stable.items() if k != "payload_files"}
        ),
    }
    for name, body in cases.items():
        run_dir = make_run_dir(tmp_path, name)
        artifact_path(run_dir).write_text(body, encoding="utf-8")

        snapshot = read_protocol(run_dir)

        assert snapshot is not None, name
        assert snapshot.available is False, name
        assert snapshot.verified_identity is None, name
        assert snapshot.detail, name


def test_a_pre_payload_check_verdict_reads_back_unverified(tmp_path: Path) -> None:
    # Schema 1 reached 'stable' by comparing two lock reads, which this version
    # does not accept as a verified payload. The artifact is never repaired or
    # rewritten: it stays readable, and it publishes nothing.
    run_dir = make_run_dir(tmp_path)
    artifact_path(run_dir).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "available": True,
                "source_git_commit": COMMIT,
                "canonical_payload_sha256": PAYLOAD,
                "captured_ts": CAPTURED_AT.isoformat(),
                "stability": STABILITY_STABLE,
                "checked_ts": ENDED_AT.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    snapshot = read_protocol(run_dir)

    assert snapshot is not None
    assert snapshot.available is True  # the capture itself is still good evidence
    assert snapshot.source_git_commit == COMMIT
    assert snapshot.stability == STABILITY_UNVERIFIED
    assert snapshot.detail == LEGACY_VERDICT_DETAIL
    assert snapshot.verified_identity is None
    assert json.loads(artifact_path(run_dir).read_text())["stability"] == (
        STABILITY_STABLE
    )


def test_a_legacy_contradicted_verdict_needs_no_demotion(tmp_path: Path) -> None:
    # 'changed' published nothing under either schema, and saying it was never
    # payload-checked would replace a fact with a caveat.
    run_dir = make_run_dir(tmp_path)
    artifact_path(run_dir).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "available": True,
                "source_git_commit": COMMIT,
                "canonical_payload_sha256": PAYLOAD,
                "stability": STABILITY_CHANGED,
                "checked_ts": ENDED_AT.isoformat(),
                "detail": "deployed protocol changed during the run: ...",
            }
        ),
        encoding="utf-8",
    )

    snapshot = read_protocol(run_dir)

    assert snapshot is not None
    assert snapshot.stability == STABILITY_CHANGED
    assert snapshot.detail == "deployed protocol changed during the run: ..."
    assert snapshot.verified_identity is None


# -- what a verified run publishes --------------------------------------------


def test_verified_identity_is_the_whole_pair_or_nothing(tmp_path: Path) -> None:
    verified = snap()

    assert verified.verified_identity == ProtocolIdentity(COMMIT, PAYLOAD)
    # The revision is derived from the pair, never verified on its own.
    assert verified.verified_revision == COMMIT
    for stability in (STABILITY_CHANGED, STABILITY_DRIFTED, STABILITY_UNVERIFIED):
        withheld = snap(stability=stability)
        assert withheld.verified_identity is None, stability
        assert withheld.verified_revision is None, stability
        # ... while both halves stay readable for audit.
        assert withheld.source_git_commit == COMMIT, stability
        assert withheld.canonical_payload_sha256 == PAYLOAD, stability


def test_persisted_snapshots_round_trip(tmp_path: Path) -> None:
    run_dir = make_run_dir(tmp_path)
    snapshot = ProtocolSnapshot(
        available=True,
        source_git_commit=COMMIT,
        canonical_payload_sha256=PAYLOAD,
        source_repo="ai-native-deployment",
        lock_schema_version=1,
        captured_ts=CAPTURED_AT.isoformat(),
        stability=STABILITY_DRIFTED,
        checked_ts=ENDED_AT.isoformat(),
        payload_files=117,
        detail="deployed files no longer match ...",
    )

    write_protocol(run_dir, snapshot)

    assert read_protocol(run_dir) == snapshot


# -- aggregating a task's runs into one effective revision --------------------
#
# A run's capture answers for one run; a report answers for a task, which several
# runs worked. The same fail-closed rule applies once more across them: the
# revision published is the one every contributing run verified.


def snap(
    commit: str = COMMIT,
    *,
    stability: str = STABILITY_STABLE,
    available: bool = True,
) -> ProtocolSnapshot:
    return ProtocolSnapshot(
        available=available,
        source_git_commit=commit if available else None,
        canonical_payload_sha256=PAYLOAD if available else None,
        captured_ts=CAPTURED_AT.isoformat(),
        stability=stability,
        checked_ts=(
            None if stability == STABILITY_UNVERIFIED else ENDED_AT.isoformat()
        ),
    )


def test_unanimous_verified_runs_have_an_effective_revision() -> None:
    result = effective_protocol([snap(), snap(), snap()])

    assert result.available is True
    assert result.revision == COMMIT
    assert (result.run_count, result.verified_count) == (3, 3)
    assert result.detail is None
    assert result.to_dict()["effective_revision"] == COMMIT


def test_one_unverifiable_run_costs_the_whole_task_its_revision() -> None:
    # Every non-`stable` state and a run with no artifact at all are alike here:
    # a report published under a revision one of its runs cannot be shown to have
    # used states a cohort membership that is false for that run.
    cases = {
        "no artifact": None,
        "no end check": snap(stability=STABILITY_UNVERIFIED),
        "redeployed mid-run": snap(stability=STABILITY_CHANGED),
        "no usable lock": snap(available=False),
    }
    for label, missing in cases.items():
        result = effective_protocol([snap(), missing])

        assert result.available is False, label
        assert result.revision is None, label
        assert (result.run_count, result.verified_count) == (2, 1), label
        assert result.detail == (
            "1 of 2 contributing runs carry no verified protocol revision"
        ), label


def test_runs_spanning_a_redeployment_have_no_effective_revision() -> None:
    result = effective_protocol([snap(COMMIT), snap(OTHER_COMMIT)])

    assert result.available is False
    assert result.verified_count == 2
    assert result.detail == (
        "contributing runs verified different protocol revisions: "
        + ", ".join(sorted((COMMIT, OTHER_COMMIT)))
    )


def test_both_defects_are_reported_together() -> None:
    # Naming only the incompleteness would suggest the missing evidence is all
    # that stands between this task and a revision. It is not: the runs that did
    # verify one disagree.
    result = effective_protocol([snap(COMMIT), snap(OTHER_COMMIT), None])

    assert result.available is False
    assert result.detail is not None
    assert "1 of 3 contributing runs carry no verified" in result.detail
    assert "verified different protocol revisions" in result.detail


def test_a_disagreement_detail_stays_bounded() -> None:
    commits = [f"{index:040x}" for index in range(1, 8)]

    result = effective_protocol([snap(commit) for commit in commits])

    # A task's run count has no ceiling, so the reason summarizes rather than
    # growing with it. The listed ones are the first in the same sorted order the
    # rule compares by, so the message is deterministic.
    assert result.detail is not None
    assert result.detail.endswith("(+3 more)")
    assert all(commit in result.detail for commit in commits[:MAX_LISTED_REVISIONS])
    assert commits[MAX_LISTED_REVISIONS] not in result.detail


def test_a_task_with_no_runs_has_no_effective_revision() -> None:
    result = effective_protocol([])

    assert result.available is False
    assert (result.run_count, result.verified_count) == (0, 0)
    assert result.detail == "no related runs to take a protocol revision from"


# -- reading a persisted aggregate (the catalog's view) -----------------------


def test_persisted_aggregates_round_trip() -> None:
    original = effective_protocol([snap(), snap()])

    assert effective_protocol_from_dict(original.to_dict()) == original


def test_an_absent_section_is_distinguished_from_a_damaged_one() -> None:
    # A pack written before packs carried this section, versus one something
    # rewrote. Both publish nothing; only the reason differs, and only the second
    # is a defect worth an operator's attention.
    absent = effective_protocol_from_dict(None)
    damaged = effective_protocol_from_dict([COMMIT])

    assert absent.available is False
    assert absent.detail == "evidence pack carries no protocol provenance section"
    assert damaged.available is False
    assert damaged.detail == "evidence pack protocol provenance is not a JSON object"


def test_an_unavailable_section_carries_its_own_reason_through() -> None:
    reason = "1 of 2 contributing runs carry no verified protocol revision"

    result = effective_protocol_from_dict(
        {"available": False, "run_count": 2, "verified_count": 1, "detail": reason}
    )

    # The aggregate already named which runs fell short; restating it generically
    # would drop the only part a consumer can act on.
    assert result.available is False
    assert result.detail == reason
    assert (result.run_count, result.verified_count) == (2, 1)


def test_a_section_claiming_a_revision_it_does_not_carry_is_refused() -> None:
    for revision in (None, "", "HEAD", "main", COMMIT[:8], 7):
        result = effective_protocol_from_dict(
            {"available": True, "effective_revision": revision, "run_count": 1}
        )

        assert result.available is False, revision
        assert result.revision is None, revision
        assert result.detail == (
            "evidence pack claims a protocol revision it does not carry"
        )


def test_a_persisted_revision_is_normalized_like_a_captured_one() -> None:
    result = effective_protocol_from_dict(
        {"available": True, "effective_revision": f"  {COMMIT.upper()} "}
    )

    # Same normalization the capture applies, so a hand-edited pack cannot split a
    # cohort by case or stray whitespace.
    assert result.available is True and result.revision == COMMIT
