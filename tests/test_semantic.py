"""Tests for the optional read-only semantic task evaluator (Layer 2).

Covers the session-1 acceptance subset: the report schema, bounded evidence
construction (excludes raw logs, records omissions), the prompt contract, envelope
parse/schema validation, confidence degradation for thin evidence, report
persistence beside evidence/static artifacts, the read-only/no-mutation service
boundary, disabled-mode fail-closed, and failure paths — all with an injected fake
backend. The concrete configured/CLI backend, rerun/version history, Markdown
rendering, and API surfaces are session-2.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.evaluation import (
    EVIDENCE_NAME,
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    EvaluationService,
    EvidencePack,
    RunEvidence,
)
from orch_hub.logs import LogSnapshot
from orch_hub.models import (
    EvaluationTarget,
    ManagedRepo,
    RunRecord,
    SemanticEvaluatorInfo,
    SemanticFinding,
    SemanticReport,
    SemanticScore,
)
from orch_hub.semantic import (
    SEMANTIC_END,
    SEMANTIC_REPORT_NAME,
    SEMANTIC_SCHEMA_VERSION,
    SEMANTIC_SEVERITIES,
    SEMANTIC_START,
    SemanticEvaluationError,
    SemanticEvaluator,
    SemanticEvidence,
    _UnavailableSemanticBackend,
    build_prompt,
    build_semantic_evidence,
    parse_report,
)
from orch_hub.static_metrics import STATIC_METRICS_NAME, StaticEvaluator
from orch_hub.subprocess_utils import CommandResult

TASK = "2026-07-22-semantic-task-evaluator"
REPO_PATH = Path("/repos/alpha")
START = "2026-07-22T09:00:00+00:00"
END = "2026-07-22T09:30:00+00:00"
FIXED_TS = dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
FIXED_ID = FIXED_TS.strftime("%Y%m%dT%H%M%SZ")


# -- builders (pure, no filesystem) -------------------------------------------


def target(*, present: bool = True, source: str | None = SOURCE_ACTIVE) -> EvaluationTarget:
    return EvaluationTarget(
        repo_name="alpha",
        repo_path=REPO_PATH,
        task_id=TASK,
        source=source if present else None,
        present=present,
        content="# body\n## Goal\ndo it" if present else None,
    )


def run_ev(run_id: str = "r1", *, log: LogSnapshot | None = None, warnings=()) -> RunEvidence:
    record = RunRecord(
        run_id=run_id,
        repo_name="alpha",
        repo_path=REPO_PATH,
        task_id=TASK,
        started_ts=START,
        ended_ts=END,
        exit_code=0,
    )
    return RunEvidence(record=record, log=log, warnings=tuple(warnings))


def pack(*runs: RunEvidence, tgt=None, warnings=(), git=None) -> EvidencePack:
    return EvidencePack(
        evaluation_id="e1",
        generated_ts=START,
        target=tgt if tgt is not None else target(),
        runs=tuple(runs),
        git=git,
        warnings=tuple(warnings),
    )


def logsnap(*lines: str) -> LogSnapshot:
    return LogSnapshot(present=True, lines=tuple(lines))


def evaluator_info(backend: str = "fake", model: str | None = "fake-model") -> SemanticEvaluatorInfo:
    return SemanticEvaluatorInfo(backend=backend, model=model, schema_version=SEMANTIC_SCHEMA_VERSION)


VALID_PAYLOAD = {
    "summary": "The task was handled cleanly with a complete handoff.",
    "confidence": 0.8,
    "scores": [
        {"dimension": "handoff_completeness", "score": 0.9, "confidence": 0.8, "rationale": "session log complete"},
        {"dimension": "decision_trace", "score": None, "confidence": 0.4, "rationale": "few decisions recorded"},
    ],
    "findings": [
        {
            "dimension": "rediscovery_risk",
            "severity": "low",
            "summary": "minor rediscovery risk",
            "detail": "the .ai note is thin",
            "confidence": 0.6,
            "evidence_refs": ["session-log entry sid-b", "score:handoff"],
            "suggestion": "record more context in .ai/",
        }
    ],
    "improvements": ["Add a decision-trace reminder to the dev contract."],
}


def envelope(payload: dict | None = None, *, before: str = "prose before\n", after: str = "\ntrailing prose") -> str:
    body = json.dumps(VALID_PAYLOAD if payload is None else payload)
    return f"{before}{SEMANTIC_START}\n{body}\n{SEMANTIC_END}{after}"


class FakeBackend:
    """Records the prompt it received and returns canned envelope text."""

    def __init__(self, text: str, *, info: SemanticEvaluatorInfo | None = None):
        self._text = text
        self._info = info or evaluator_info()
        self.prompts: list[str] = []
        self.repos: list[ManagedRepo] = []

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return self._info

    def evaluate(self, prompt: str, repo: ManagedRepo) -> str:
        self.prompts.append(prompt)
        self.repos.append(repo)
        return self._text


class RaisingBackend:
    """A backend whose model turn fails (a process/model error)."""

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return evaluator_info()

    def evaluate(self, prompt: str, repo: ManagedRepo) -> str:
        raise SemanticEvaluationError("model process crashed")


def _fake_git(args, **kwargs):
    return CommandResult(tuple(str(a) for a in args), 128, "", "")  # git unavailable


def semantic_evaluator(
    *, enabled: bool = True, service=None, static=None, backend=None, clock=None,
    config: Config | None = None,
) -> SemanticEvaluator:
    cfg = config or Config(
        ai_native_path=Path("/ai-native"),
        data_dir=Path("/hub-data"),
        evaluation_enabled=enabled,
    )
    return SemanticEvaluator(
        cfg,
        evaluation_service=service,
        static_evaluator=static,
        backend=backend,
        clock=clock or (lambda: FIXED_TS),
    )


def mk_evidence(*, static: dict | None = None, warnings=(), ai_diffs: bool = False,
                ai_snippets: bool = False, present: bool = True) -> SemanticEvidence:
    return SemanticEvidence(
        evaluation_id="e1",
        repo_name="alpha",
        task_id=TASK,
        target_present=present,
        target_source=SOURCE_ACTIVE if present else None,
        task_file="# body" if present else None,
        static_metrics=static,
        git_subjects=(),
        evidence_warnings=tuple(warnings),
        ai_diffs_available=ai_diffs,
        ai_snippets_available=ai_snippets,
        omissions=(),
    )


def parse(text: str, *, evidence: SemanticEvidence | None = None,
          tgt: EvaluationTarget | None = None) -> SemanticReport:
    return parse_report(
        text,
        evaluation_id=FIXED_ID,
        target=tgt or target(),
        evaluator=evaluator_info(),
        generated_ts=FIXED_TS.isoformat(),
        evidence=evidence or mk_evidence(static={"scores": []}),
    )


# -- schema shapes ------------------------------------------------------------


def test_report_to_dict_is_json_serializable_and_stable():
    report = SemanticReport(
        evaluation_id=FIXED_ID,
        generated_ts=FIXED_TS.isoformat(),
        repo_name="alpha",
        task_id=TASK,
        target_present=True,
        target_source=SOURCE_ACTIVE,
        evaluator=evaluator_info(),
        summary="ok",
        overall_confidence=0.7,
        scores=(SemanticScore("handoff_completeness", 0.9, 0.8, "complete"),),
        findings=(SemanticFinding("decision_trace", "low", "thin trace", 0.5,
                                  evidence_refs=("sid-b",), suggestion="record more"),),
        improvements=("tighten the contract",),
        input_evidence_id=FIXED_ID,
        input_static_metrics=True,
        warnings=("note",),
    )

    data = report.to_dict()
    json.dumps(data)  # must not raise
    assert data["evaluation_id"] == FIXED_ID
    assert data["evaluator"] == {"backend": "fake", "model": "fake-model", "schema_version": "1"}
    assert data["scores"][0]["rationale"] == "complete"  # never a naked score
    assert data["findings"][0]["severity"] == "low"
    assert data["findings"][0]["evidence_refs"] == ["sid-b"]
    assert data["input_static_metrics"] is True


# -- bounded evidence construction --------------------------------------------


def test_evidence_excludes_raw_logs_and_records_omission():
    p = pack(run_ev(log=logsnap("SECRET-LOG-LINE about a crash")))

    evidence = build_semantic_evidence(p, static_report=None)

    # The raw log line never enters the evidence dict...
    blob = json.dumps(evidence.to_dict())
    assert "SECRET-LOG-LINE" not in blob
    # ...and the exclusion is recorded as an omission (auditable, not silent).
    assert any("raw orchestrator logs" in o for o in evidence.omissions)


def test_evidence_records_ai_diffs_omission_and_carries_git_subjects_only():
    from orch_hub.evaluation import EvidenceGit
    from orch_hub.models import CommitInfo

    commit = CommitInfo(hash="a" * 40, short_hash="aaaaaaa", committed_ts=START, subject="feat: do it")
    p = pack(run_ev(), git=EvidenceGit(available=True, commits=(commit,)))

    evidence = build_semantic_evidence(p, static_report=None)

    assert evidence.ai_diffs_available is False
    assert any(".ai/ changed-file diffs unavailable" in o for o in evidence.omissions)
    assert evidence.git_subjects == ("feat: do it",)  # subject only, no hash/diff


def test_evidence_prompt_excludes_logs_and_includes_static_and_markers():
    p = pack(run_ev(log=logsnap("SECRET-LOG-LINE")))
    evidence = build_semantic_evidence(p, static_report=None)

    prompt = build_prompt(evidence)

    assert "SECRET-LOG-LINE" not in prompt
    assert SEMANTIC_START in prompt and SEMANTIC_END in prompt
    assert "handoff_completeness" in prompt  # the dimension vocabulary is offered
    assert "static metrics unavailable" in prompt.lower()


def test_evidence_records_ai_snippets_omission_and_marks_unavailable():
    # The pack carries no current .ai/ contents, so the evaluator cannot ground a
    # .ai/ freshness judgment: the omission is recorded and the flag stays False.
    evidence = build_semantic_evidence(pack(run_ev()), static_report=None)

    assert evidence.ai_snippets_available is False
    assert any(".ai/ file contents unavailable" in o for o in evidence.omissions)
    assert evidence.to_dict()["ai_snippets_available"] is False


def test_prompt_is_evidence_bounded_and_marks_ai_freshness_unassessable():
    prompt = build_prompt(build_semantic_evidence(pack(run_ev()), static_report=None))

    # No invitation to read the repository outside the persisted evidence boundary...
    assert "may read the repository" not in prompt.lower()
    # ...and the .ai/ freshness dimension is explicitly flagged unassessable.
    assert "unassessable" in prompt.lower()
    assert "ai_freshness_utility" in prompt


def test_confidence_penalty_grows_as_evidence_thins():
    full = mk_evidence(static={"scores": []}, ai_diffs=True, ai_snippets=True)
    no_static = mk_evidence(static=None, ai_diffs=True, ai_snippets=True)
    warned = mk_evidence(static={"scores": []}, ai_diffs=True, ai_snippets=True,
                         warnings=("w1", "w2", "w3"))

    assert full.confidence_penalty() == 0.0
    assert no_static.confidence_penalty() == pytest.approx(0.30)
    assert warned.confidence_penalty() == pytest.approx(0.15)
    # A missing .ai/ snippet set is its own degradation, parallel to missing diffs.
    no_snippets = mk_evidence(static={"scores": []}, ai_diffs=True, ai_snippets=False)
    assert no_snippets.confidence_penalty() == pytest.approx(0.10)
    # Even the thinnest evidence keeps some confidence (task file is the subject).
    thin = mk_evidence(static=None, ai_diffs=False, ai_snippets=False,
                       warnings=tuple(f"w{i}" for i in range(20)))
    assert thin.confidence_penalty() <= 0.60


# -- envelope parse / schema validation ---------------------------------------


def test_parse_valid_envelope_builds_report():
    report = parse(envelope())

    assert report.summary.startswith("The task was handled")
    assert len(report.scores) == 2 and report.scores[1].score is None  # null = not assessable
    assert report.findings[0].severity == "low"
    assert report.findings[0].evidence_refs == ("session-log entry sid-b", "score:handoff")
    assert report.improvements == ("Add a decision-trace reminder to the dev contract.",)


def test_parse_stamps_identity_from_context_not_from_model():
    # A model that tries to claim a different identity is ignored: identity and
    # provenance are stamped by the harness so the model cannot spoof what it judged.
    spoof = {**VALID_PAYLOAD, "task_id": "other-task", "evaluation_id": "evil",
             "repo_name": "victim", "evaluator": {"backend": "spoof"}}
    report = parse(envelope(spoof))

    assert report.task_id == TASK and report.repo_name == "alpha"
    assert report.evaluation_id == FIXED_ID and report.input_evidence_id == FIXED_ID
    assert report.evaluator.backend == "fake"  # from the injected evaluator info


def test_parse_missing_envelope_raises():
    with pytest.raises(SemanticEvaluationError, match="marker"):
        parse("no markers here, just prose")


def test_parse_non_object_envelope_raises():
    with pytest.raises(SemanticEvaluationError):
        parse(f"{SEMANTIC_START}\n[1, 2, 3]\n{SEMANTIC_END}")


def test_parse_missing_confidence_raises():
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "confidence"}
    with pytest.raises(SemanticEvaluationError, match="confidence"):
        parse(envelope(payload))


def test_parse_bad_severity_raises():
    payload = {**VALID_PAYLOAD, "findings": [{**VALID_PAYLOAD["findings"][0], "severity": "catastrophic"}]}
    with pytest.raises(SemanticEvaluationError, match="severity"):
        parse(envelope(payload))


def _finding_with_refs(refs: object, *, present: bool = True) -> dict:
    base = dict(VALID_PAYLOAD["findings"][0])
    if present:
        base["evidence_refs"] = refs
    else:
        base.pop("evidence_refs", None)
    return {**VALID_PAYLOAD, "findings": [base]}


def test_parse_finding_missing_evidence_refs_raises():
    # A persisted finding must cite evidence; an absent refs field is rejected.
    with pytest.raises(SemanticEvaluationError, match="evidence_refs"):
        parse(envelope(_finding_with_refs(None, present=False)))


def test_parse_finding_empty_evidence_refs_raises():
    with pytest.raises(SemanticEvaluationError, match="evidence_refs"):
        parse(envelope(_finding_with_refs([])))


def test_parse_finding_non_array_evidence_refs_raises():
    with pytest.raises(SemanticEvaluationError, match="evidence_refs"):
        parse(envelope(_finding_with_refs("sid-b")))


def test_parse_finding_blank_only_evidence_refs_raises():
    with pytest.raises(SemanticEvaluationError, match="evidence_refs"):
        parse(envelope(_finding_with_refs(["", "  ", 3])))


def test_parse_finding_drops_blank_refs_but_keeps_the_real_ones():
    # Lenient on members, strict on the whole: blanks/non-strings drop, but at least
    # one real reference must survive (here it does).
    report = parse(envelope(_finding_with_refs(["good ref", "", "  ", 7])))
    assert report.findings[0].evidence_refs == ("good ref",)


def test_parse_score_without_rationale_raises():
    payload = {**VALID_PAYLOAD, "scores": [{"dimension": "handoff_completeness", "score": 0.5, "confidence": 0.5}]}
    with pytest.raises(SemanticEvaluationError, match="rationale"):
        parse(envelope(payload))


def test_parse_clamps_out_of_range_numbers():
    payload = {
        **VALID_PAYLOAD,
        "confidence": 5.0,
        "scores": [{"dimension": "d", "score": -2.0, "confidence": 9.0, "rationale": "why"}],
    }
    report = parse(envelope(payload), evidence=mk_evidence(static={"scores": []}, ai_diffs=True))

    assert report.scores[0].score == 0.0 and report.scores[0].confidence == 1.0
    # headline confidence 5.0 clamps to 1.0, then degrades only for the (absent) full evidence
    assert 0.0 <= report.overall_confidence <= 1.0


def test_parse_non_numeric_confidence_raises():
    payload = {**VALID_PAYLOAD, "confidence": "high"}
    with pytest.raises(SemanticEvaluationError, match="number"):
        parse(envelope(payload))


def test_parse_tolerates_missing_optional_fields():
    payload = {"confidence": 0.5, "scores": [], "findings": []}  # no summary/improvements
    report = parse(envelope(payload))

    assert report.summary is None
    assert report.scores == () and report.findings == () and report.improvements == ()


def test_parse_degrades_confidence_and_records_warnings_for_thin_evidence():
    with_static = parse(envelope(), evidence=mk_evidence(static={"scores": []}, ai_diffs=True))
    without_static = parse(envelope(), evidence=mk_evidence(static=None, ai_diffs=True))

    assert without_static.overall_confidence < with_static.overall_confidence
    assert without_static.input_static_metrics is False
    assert any("degraded" in w for w in without_static.warnings)


def test_all_severities_are_accepted():
    for sev in SEMANTIC_SEVERITIES:
        payload = {**VALID_PAYLOAD, "findings": [{**VALID_PAYLOAD["findings"][0], "severity": sev}]}
        report = parse(envelope(payload))
        assert report.findings[0].severity == sev


# -- disabled-mode gate -------------------------------------------------------


def test_disabled_evaluator_refuses_build_evidence():
    with pytest.raises(EvaluationDisabledError):
        semantic_evaluator(enabled=False).build_evidence(pack(run_ev()))


def test_disabled_evaluator_refuses_evaluate(tmp_path):
    config = Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=False,
    )
    repo = ManagedRepo(name="alpha", path=tmp_path / "repo", manifest=tmp_path / "repo" / "m.json")
    with pytest.raises(EvaluationDisabledError):
        semantic_evaluator(config=config, backend=FakeBackend(envelope())).evaluate(repo, TASK)


def test_enabled_flag_reports_config():
    assert semantic_evaluator(enabled=True).enabled is True
    assert semantic_evaluator(enabled=False).enabled is False


# -- integration: persistence, no mutation, failure paths ---------------------


def _write_repo_and_run(config: Config, tmp_path: Path, *, archived: bool = False) -> ManagedRepo:
    """A tmp repo with a task file (active or archived), a `.ai/` file, and one run."""
    repo_path = tmp_path / "repos" / "alpha"
    task_body = (
        f"---\nid: {TASK}\nstatus: {'completed' if archived else 'in_progress'}\n"
        f"session-est: 1/2\nblockers: []\n---\n\n# Semantic evaluator\n\n## Goal\ngo\n"
        "\n## Scope\n- x\n\n## Acceptance\n- y\n\n## Session log\n\n"
        "### 2026-07-22 / sid-a / (pending → in_progress)\n- Done: work\n- Next: more\n- Open: none\n"
    )
    if archived:
        (repo_path / ".ai-tasks" / "archive").mkdir(parents=True)
        (repo_path / ".ai-tasks" / "archive" / f"{TASK}.md").write_text(task_body, encoding="utf-8")
    else:
        (repo_path / ".ai-tasks").mkdir(parents=True)
        (repo_path / ".ai-tasks" / f"{TASK}.md").write_text(task_body, encoding="utf-8")
    (repo_path / ".ai-tasks" / "index.md").write_text("# Active tasks\n", encoding="utf-8")
    (repo_path / ".ai").mkdir(parents=True)
    (repo_path / ".ai" / "overview.md").write_text("# overview\nsignal\n", encoding="utf-8")

    run_id = "20260722T090000Z-alpha-task"
    run_dir = config.data_dir / "runs" / run_id
    (run_dir / "control").mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({
            "run_id": run_id, "repo_name": "alpha", "repo_path": str(repo_path),
            "task_id": TASK, "started_ts": START, "ended_ts": END, "exit_code": 0,
        }),
        encoding="utf-8",
    )
    return ManagedRepo(name="alpha", path=repo_path, manifest=repo_path / ".ai-deploy-manifest.json")


def _service(config: Config) -> EvaluationService:
    return EvaluationService(config, git_runner=_fake_git, clock=lambda: FIXED_TS)


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_evaluate_persists_semantic_report_beside_evidence_and_static(tmp_path):
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data",
                    evaluation_enabled=True)
    repo = _write_repo_and_run(config, tmp_path, archived=True)
    backend = FakeBackend(envelope())
    ev = semantic_evaluator(config=config, service=_service(config), backend=backend)

    report = ev.evaluate(repo, TASK)

    eval_dir = config.data_dir / "evaluations" / "alpha" / TASK / report.evaluation_id
    assert (eval_dir / EVIDENCE_NAME).is_file()
    assert (eval_dir / STATIC_METRICS_NAME).is_file()
    assert (eval_dir / SEMANTIC_REPORT_NAME).is_file()

    persisted = json.loads((eval_dir / SEMANTIC_REPORT_NAME).read_text(encoding="utf-8"))
    assert persisted["evaluation_id"] == report.evaluation_id == FIXED_ID
    assert persisted["task_id"] == TASK and persisted["target_source"] == SOURCE_ARCHIVED
    assert persisted["evaluator"]["backend"] == "fake"
    assert persisted["input_evidence_id"] == report.evaluation_id
    assert persisted["input_static_metrics"] is True
    assert persisted["scores"] and persisted["findings"]
    # The backend saw a prompt carrying the task's static metrics, not raw logs.
    assert backend.prompts and "handoff_completeness" in backend.prompts[0]


def test_evaluate_does_not_mutate_target_repo_or_run_state(tmp_path):
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data",
                    evaluation_enabled=True)
    repo = _write_repo_and_run(config, tmp_path, archived=False)
    runs_dir = config.data_dir / "runs"
    before_repo = _snapshot_tree(repo.path)
    before_runs = _snapshot_tree(runs_dir)

    ev = semantic_evaluator(config=config, service=_service(config), backend=FakeBackend(envelope()))
    ev.evaluate(repo, TASK)

    # The target repo (.ai-tasks/ + .ai/) and every run record are byte-identical:
    # the service only ever writes hub-owned evaluation artifacts.
    assert _snapshot_tree(repo.path) == before_repo
    assert _snapshot_tree(runs_dir) == before_runs
    # Only the evaluations subtree gained files.
    assert (config.data_dir / "evaluations" / "alpha" / TASK / FIXED_ID / SEMANTIC_REPORT_NAME).is_file()


def test_evaluate_cleans_up_dir_on_backend_failure(tmp_path):
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data",
                    evaluation_enabled=True)
    repo = _write_repo_and_run(config, tmp_path, archived=True)
    ev = semantic_evaluator(config=config, service=_service(config), backend=RaisingBackend())

    with pytest.raises(SemanticEvaluationError, match="crashed"):
        ev.evaluate(repo, TASK)

    # No half-written evaluation is left behind (evidence.json is removed too).
    eval_dir = config.data_dir / "evaluations" / "alpha" / TASK / FIXED_ID
    assert not eval_dir.exists()
    assert _snapshot_tree(repo.path)  # repo still intact, unchanged by the failure


def test_evaluate_cleans_up_dir_on_schema_violation(tmp_path):
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data",
                    evaluation_enabled=True)
    repo = _write_repo_and_run(config, tmp_path, archived=True)
    bad = envelope({"scores": [], "findings": []})  # missing required top-level confidence
    ev = semantic_evaluator(config=config, service=_service(config), backend=FakeBackend(bad))

    with pytest.raises(SemanticEvaluationError, match="confidence"):
        ev.evaluate(repo, TASK)

    assert not (config.data_dir / "evaluations" / "alpha" / TASK / FIXED_ID).exists()


def test_default_backend_is_unavailable_and_fails_cleanly(tmp_path):
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data",
                    evaluation_enabled=True)
    repo = _write_repo_and_run(config, tmp_path, archived=True)
    # No backend injected: the default _UnavailableSemanticBackend fails the turn.
    ev = SemanticEvaluator(config, evaluation_service=_service(config), clock=lambda: FIXED_TS)

    with pytest.raises(SemanticEvaluationError, match="no semantic evaluator backend"):
        ev.evaluate(repo, TASK)

    assert not (config.data_dir / "evaluations" / "alpha" / TASK / FIXED_ID).exists()


def test_unavailable_backend_info_has_no_model():
    info = _UnavailableSemanticBackend().info
    assert info.backend == "unavailable" and info.model is None
    assert info.schema_version == SEMANTIC_SCHEMA_VERSION


def test_evaluate_over_absent_task_still_persists_low_confidence_report(tmp_path):
    # A task id with no file (evaluable for run history only) must not crash: it
    # degrades confidence and still produces a persisted report.
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data",
                    evaluation_enabled=True)
    repo = _write_repo_and_run(config, tmp_path, archived=True)
    other = "2026-07-22-nonexistent-task"
    ev = semantic_evaluator(config=config, service=_service(config), backend=FakeBackend(envelope()))

    report = ev.evaluate(repo, other)

    assert report.target_present is False and report.target_source is None
    eval_dir = config.data_dir / "evaluations" / "alpha" / other / FIXED_ID
    assert (eval_dir / SEMANTIC_REPORT_NAME).is_file()
