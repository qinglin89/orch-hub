"""Tests for launch-readiness checks.

Builds fake target repos in ``tmp_path`` and injects a fake runner for the git
and ``mandrel status`` checks; never touches real repos or subprocesses.
"""

from __future__ import annotations

import json
from pathlib import Path

from orch_hub.config import Config
from orch_hub.models import ManagedRepo
from orch_hub.readiness import check_readiness
from orch_hub.subprocess_utils import CommandNotFoundError, CommandResult


def build_ready_repo(tmp_path: Path) -> ManagedRepo:
    """Create a fake target repo with every deploy piece present."""
    repo = tmp_path / "repo"
    bin_dir = repo / ".mandrel" / "orchestrator" / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (repo / ".mandrel" / "orchestrator" / "orchestrator.py").write_text("# orch\n")
    (bin_dir / "python").write_text("")
    (repo / ".ai-tasks").mkdir(parents=True)
    (repo / ".ai-tasks" / "index.md").write_text("# tasks\n")
    manifest = repo / ".ai-deploy-manifest.json"
    manifest.write_text("{}\n")
    return ManagedRepo(name="repo", path=repo, manifest=manifest)


def make_runner(*, git_out="", git_rc=0, mandrel_rc=0, mandrel_out="", mandrel_err=""):
    """A fake runner that distinguishes the git and mandrel subprocess calls."""

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            return CommandResult(tuple(argv), git_rc, git_out, "")
        return CommandResult(tuple(argv), mandrel_rc, mandrel_out, mandrel_err)

    return runner


def status_json(*, placed_line="base", drifts=(), schema_version=2):
    """A ``mandrel status --json <target>`` payload: a sweep of exactly one.

    Mirrors the real envelope (mandrel ``docs/operations.md``): the source lines
    stated once above the targets, and every target stating ``placed_line`` —
    the line's name, or null for every way it stands on none.
    """
    return json.dumps(
        {
            "schema_version": schema_version,
            "source_lines": {
                "ref": "refs/heads/main",
                "base": "b" * 40,
                "candidate": None,
                "unresolved": [],
            },
            "targets": [
                {
                    "name": None,
                    "target_root": "/tmp/repo",
                    "in_sync": not drifts,
                    "total_files": 117,
                    "placed_line": placed_line,
                    "drifts": list(drifts),
                }
            ],
        }
    )


def drift(kind, *, placement=None, subject=".ai-deploy-lock.json", detail=""):
    """One finding in the payload's shape; ``placement`` only on placement kinds."""
    return {
        "kind": kind,
        "origin": "placement" if placement else "target",
        "subject": subject,
        "detail": detail,
        "placement": placement,
    }


PLACEMENT_FACTS = {
    "reason": None,
    "holds": "a" * 40,
    "base": "b" * 40,
    "candidate": None,
}


def config(tmp_path: Path) -> Config:
    return Config(mandrel_path=tmp_path)


def failure_names(report) -> list[str]:
    return [c.name for c in report.failures]


def test_ready_repo_all_pass(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert report.ready
    assert report.failures == ()
    assert report.to_dict() == {"ready": True, "checks": []}
    # Stable seven-check shape.
    assert len(report.checks) == 7


def test_check_names_are_the_stable_user_visible_labels(tmp_path):
    """Check names are product language, not internal identifiers.

    The CLI, repo cards, repo detail, the intake refusal block, and the
    Overview attention rows all print ``name`` verbatim, so both the spelling
    and the order are a user-visible contract. Pinned here so a rename cannot
    reach the dashboard unnoticed.
    """
    repo = build_ready_repo(tmp_path)
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        calls.append(argv)
        return CommandResult(tuple(argv), 0, "", "")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert [c.name for c in report.checks] == [
        "repo_path_exists",
        "manifest_exists",
        "orchestrator_exists",
        "venv_python_exists",
        "task_index_exists",
        "git_clean",
        "mandrel_status",
    ]
    # The check is named after the command it runs: `mandrel status <repo>`,
    # invoked by absolute path inside the mandrel checkout, and asked for the
    # machine payload so the same call answers the gate and the protocol label.
    assert calls[-1] == [
        str(config(tmp_path).mandrel_bin),
        "status",
        "--json",
        str(repo.path),
    ]
    # Exactly one mandrel call: the label rides the gate's subprocess.
    assert [c for c in calls if c[0] != "git"] == [calls[-1]]


def test_missing_manifest(tmp_path):
    repo = build_ready_repo(tmp_path)
    repo.manifest.unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert not report.ready
    assert "manifest_exists" in failure_names(report)


def test_missing_orchestrator(tmp_path):
    repo = build_ready_repo(tmp_path)
    (repo.path / ".mandrel" / "orchestrator" / "orchestrator.py").unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert not report.ready
    assert "orchestrator_exists" in failure_names(report)


def test_missing_venv_python(tmp_path):
    repo = build_ready_repo(tmp_path)
    (repo.path / ".mandrel" / "orchestrator" / ".venv" / "bin" / "python").unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert not report.ready
    assert "venv_python_exists" in failure_names(report)


def test_missing_task_index(tmp_path):
    repo = build_ready_repo(tmp_path)
    (repo.path / ".ai-tasks" / "index.md").unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert not report.ready
    assert "task_index_exists" in failure_names(report)


def test_dirty_git_tree(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(git_out=" M file.py\n")
    )
    assert not report.ready
    fail = {c.name: c for c in report.failures}
    assert "git_clean" in fail
    assert "uncommitted" in fail["git_clean"].message


def test_git_status_command_failure(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(git_rc=128)
    )
    assert "git_clean" in failure_names(report)


def test_git_executable_missing(tmp_path):
    repo = build_ready_repo(tmp_path)

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            raise CommandNotFoundError("git")
        return CommandResult(tuple(argv), 0, "", "")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert "git_clean" in failure_names(report)


def test_mandrel_status_failure(tmp_path):
    repo = build_ready_repo(tmp_path)
    payload = status_json(
        placed_line=None,
        drifts=[drift("target modified", subject=f"a{n}.py") for n in range(4)],
    )
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(mandrel_rc=1, mandrel_out=payload)
    )
    assert not report.ready
    fail = {c.name: c for c in report.failures}
    assert "mandrel_status" in fail
    # Concise: the finding count, not the per-file detail under it.
    assert "drift detected (4 issues)" in fail["mandrel_status"].message
    assert "target modified" not in fail["mandrel_status"].message
    assert "a0.py" not in fail["mandrel_status"].message
    # The public payload the CLI and dashboard render carries the same key.
    assert [c["name"] for c in report.to_dict()["checks"]] == ["mandrel_status"]


def test_mandrel_status_failure_counts_a_single_finding(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(
            mandrel_rc=1,
            mandrel_out=status_json(placed_line=None, drifts=[drift("missing target file")]),
        ),
    )
    fail = {c.name: c for c in report.failures}
    assert "drift detected (1 issue)" in fail["mandrel_status"].message


def test_unparseable_status_output_falls_back_to_the_text_it_printed(tmp_path):
    """A mandrel too old for `--json` errors on the flag and prints usage.

    Fail closed on the exit code as always, and report what the command
    actually said — a visible degradation, not a silent one. No label is
    guessed from a payload that never arrived.
    """
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(
            mandrel_rc=2,
            mandrel_err="usage: mandrel status\nmandrel status: error: unrecognized arguments: --json\n",
        ),
    )
    assert not report.ready
    fail = {c.name: c for c in report.failures}
    assert "usage: mandrel status" in fail["mandrel_status"].message
    assert report.placement.line is None
    assert report.placement.finding is None


def test_mandrel_executable_missing(tmp_path):
    repo = build_ready_repo(tmp_path)

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            return CommandResult(tuple(argv), 0, "", "")
        raise CommandNotFoundError("mandrel")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert "mandrel_status" in failure_names(report)


def test_missing_repo_path_skips_subprocess_checks(tmp_path):
    missing = tmp_path / "gone"
    repo = ManagedRepo(
        name="gone",
        path=missing,
        manifest=missing / ".ai-deploy-manifest.json",
    )

    calls = []

    def runner(args, **kwargs):
        calls.append([str(a) for a in args])
        return CommandResult(("x",), 0, "", "")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert not report.ready
    names = failure_names(report)
    assert "repo_path_exists" in names
    # Subprocess checks are skipped but still recorded as failed (fail closed).
    assert "git_clean" in names
    assert "mandrel_status" in names
    assert calls == []  # runner never invoked when the repo path is missing


# --------------------------------------------------------------------------
# Protocol placement: which line `mandrel status` placed the repo on.
#
# Relayed, never derived. These tests pin that the hub reads the payload and
# stops: no second subprocess, no revision comparison, no invented vocabulary,
# and no effect on the readiness verdict in either direction.
# --------------------------------------------------------------------------


def test_placement_relays_the_line_the_payload_names(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(mandrel_out=status_json())
    )
    assert report.ready
    assert report.placement.line == "base"
    assert report.placement.finding is None


def test_placement_distinguishes_a_sealed_candidate_from_base(tmp_path):
    """The distinction the whole label exists for.

    Both repos pass — standing on a sealed candidate is exactly as correct as
    standing on base — so nothing in the readiness verdict tells them apart.
    """
    repo = build_ready_repo(tmp_path)
    on_base = check_readiness(
        repo, config(tmp_path), runner=make_runner(mandrel_out=status_json())
    )
    on_candidate = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(mandrel_out=status_json(placed_line="candidate")),
    )
    assert on_base.ready and on_candidate.ready
    assert on_base.placement.line == "base"
    assert on_candidate.placement.line == "candidate"


def test_a_line_the_hub_has_never_seen_travels_verbatim(tmp_path):
    """No dictionary of line names, so no bucket to be wrongly sorted into.

    mandrel owns this vocabulary and may extend it; a value this hub does not
    recognize must arrive at the card as itself rather than be folded into one
    it does.
    """
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(mandrel_out=status_json(placed_line="successor")),
    )
    assert report.placement.line == "successor"
    assert report.placement.finding is None


def test_a_repo_on_neither_line_relays_mandrels_own_finding(tmp_path):
    """The third distinguishable state, in mandrel's words rather than the hub's."""
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(
            mandrel_rc=1,
            mandrel_out=status_json(
                placed_line=None,
                drifts=[drift("off the source line", placement=PLACEMENT_FACTS)],
            ),
        ),
    )
    assert report.placement.line is None
    assert report.placement.finding == "off the source line"


def test_an_unplaceable_receipt_relays_its_finding(tmp_path):
    repo = build_ready_repo(tmp_path)
    facts = {**PLACEMENT_FACTS, "reason": "no deploy lock", "holds": None}
    report = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(
            mandrel_rc=1,
            mandrel_out=status_json(
                placed_line=None,
                drifts=[drift("unplaceable deployment", placement=facts)],
            ),
        ),
    )
    assert report.placement.finding == "unplaceable deployment"


def test_a_non_placement_finding_is_not_a_placement_answer(tmp_path):
    """`target modified` says a file drifted, not where the repo stands.

    Selected structurally — mandrel carries a `placement` object on exactly its
    placement findings — so the hub needs no list of which kinds those are.
    """
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(
            mandrel_rc=1,
            mandrel_out=status_json(placed_line=None, drifts=[drift("target modified")]),
        ),
    )
    assert report.placement.line is None
    assert report.placement.finding is None


def test_an_unresolved_source_line_is_not_relayed_as_the_repos_line(tmp_path):
    """A line *this checkout* could not name is mandrel's state, not the repo's.

    It carries no placement object for exactly that reason, so relaying it would
    blame a target for a reading nobody could make.
    """
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(
            mandrel_rc=1,
            mandrel_out=status_json(
                placed_line=None,
                drifts=[drift("unresolved source line", subject="source line")],
            ),
        ),
    )
    assert report.placement.line is None
    assert report.placement.finding is None


def test_a_payload_older_than_the_field_yields_no_label(tmp_path):
    """Read the version, do not probe for keys.

    A schema-1 mandrel states no `placed_line`; passing there means "somewhere
    on a line", not "on base", and inventing base would be exactly the second
    placement this hub must not spell.
    """
    repo = build_ready_repo(tmp_path)
    payload = json.loads(status_json())
    payload["schema_version"] = 1
    del payload["targets"][0]["placed_line"]
    report = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(mandrel_out=json.dumps(payload)),
    )
    assert report.ready
    assert report.placement.line is None
    assert report.placement.finding is None


def test_a_multi_target_payload_is_not_read(tmp_path):
    """`status <path>` is a sweep of one; anything else was not what we asked."""
    repo = build_ready_repo(tmp_path)
    payload = json.loads(status_json())
    payload["targets"].append(dict(payload["targets"][0], placed_line="candidate"))
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(mandrel_out=json.dumps(payload))
    )
    assert report.placement.line is None


def test_a_missing_mandrel_yields_no_label(tmp_path):
    repo = build_ready_repo(tmp_path)

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            return CommandResult(tuple(argv), 0, "", "")
        raise CommandNotFoundError("mandrel")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert report.placement.line is None
    assert report.placement.finding is None


def test_a_skipped_status_check_yields_no_label(tmp_path):
    missing = tmp_path / "gone"
    repo = ManagedRepo(
        name="gone", path=missing, manifest=missing / ".ai-deploy-manifest.json"
    )
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert report.placement.line is None
    assert report.placement.finding is None


def test_placement_never_reaches_the_readiness_verdict(tmp_path):
    """Display only, in both directions.

    A repo on a candidate stays ready; a repo failing on a dirty tree still
    reports where it stands, because the two answers are independent.
    """
    repo = build_ready_repo(tmp_path)
    ready = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(mandrel_out=status_json(placed_line="candidate")),
    )
    assert ready.ready
    assert ready.to_dict() == {"ready": True, "checks": []}

    dirty = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(git_out=" M f.py\n", mandrel_out=status_json()),
    )
    assert not dirty.ready
    assert failure_names(dirty) == ["git_clean"]
    assert dirty.placement.line == "base"
    # The label is not a readiness reason and never becomes one.
    assert "base" not in json.dumps(dirty.to_dict())


def test_the_label_is_read_per_call_not_remembered(tmp_path):
    """A redeploy moves the label on the next request, with no run involved.

    The placement comes back on the readiness call every request makes, so a
    redeploy that changes the repo's canonical identity shows up immediately,
    and one that changes no canonical byte leaves the answer alone. A run's
    launch-frozen capture would instead show the last run's identity.
    """
    repo = build_ready_repo(tmp_path)
    payloads = iter(
        [status_json(), status_json(placed_line="candidate"), status_json(placed_line="candidate")]
    )

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            return CommandResult(tuple(argv), 0, "", "")
        return CommandResult(tuple(argv), 0, next(payloads), "")

    cfg = config(tmp_path)
    assert check_readiness(repo, cfg, runner=runner).placement.line == "base"
    assert check_readiness(repo, cfg, runner=runner).placement.line == "candidate"
    assert check_readiness(repo, cfg, runner=runner).placement.line == "candidate"


def test_the_label_never_reads_the_repos_own_receipt(tmp_path):
    """Relayed, never derived: the hub reads mandrel's answer and nothing else.

    The fixture repo has no ``.ai-deploy-lock.json`` at all, and its manifest is
    an empty object, so a label can only have come from the payload. Nothing on
    this path opens a receipt, runs git, or compares two revisions.
    """
    repo = build_ready_repo(tmp_path)
    assert not (repo.path / ".ai-deploy-lock.json").exists()

    report = check_readiness(
        repo,
        config(tmp_path),
        runner=make_runner(mandrel_out=status_json(placed_line="candidate")),
    )
    assert report.placement.line == "candidate"
    # Still no receipt: reading one would be a second placement derivation.
    assert not (repo.path / ".ai-deploy-lock.json").exists()
