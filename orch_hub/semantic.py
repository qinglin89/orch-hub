"""Optional read-only semantic task evaluator (Layer 2 of the evaluation subsystem).

Where :mod:`orch_hub.static_metrics` computes *deterministic* task-quality metrics
from an evidence pack, this module asks a **model** to judge the qualities a static
check cannot: whether the ``.ai/`` knowledge signal is dense or thin, whether the
session-log handoff and decision trace are complete, whether acceptance was really
covered, whether the scope/slice fit, and which protocol soft-failures a human
would flag. It is part of the same optional subsystem, shares the same config gate
(``Config.evaluation_enabled``) and :class:`~orch_hub.evaluation.EvaluationDisabledError`
fail-closed boundary, and — like the rest of the subsystem — is **strictly
read-only over target repo/run state**: the only writes are its own hub-owned
artifacts under ``<data_dir>/evaluations/<repo>/<task>/<evaluation-id>/``.

The evaluation is built from four composable, independently-tested pieces:

- **bounded evidence build** (:func:`build_semantic_evidence`) — distil the
  deterministic :class:`~orch_hub.evaluation.EvidencePack` + the
  :class:`~orch_hub.models.StaticMetricsReport` into a model-ready
  :class:`SemanticEvidence`: the full (small) task file, the static-metrics JSON,
  git commit *subjects* (never diffs), and an explicit ``omissions`` list. Raw
  orchestrator logs / ``spawn.out`` tails are deliberately **excluded** (the scope
  bars unbounded raw logs from the evaluator input) and recorded as an omission,
  as are the unavailable ``.ai/`` diffs — so a thin input *lowers confidence*
  rather than failing the evaluation.
- **prompt contract** (:func:`build_prompt`) — render the evidence into the
  evaluator prompt, instructing the model to emit exactly one JSON report between
  the :data:`SEMANTIC_START` / :data:`SEMANTIC_END` sentinels (``/intake-task``'s
  imposed-envelope trick — a conversational surface with no native structured
  result).
- **report parse/validate** (:func:`parse_report`) — extract that envelope and
  validate it into a :class:`~orch_hub.models.SemanticReport`. The model supplies
  only the *judgment* (scores/findings/summary/improvements); task identity and
  provenance are stamped by the harness, so a model cannot spoof what it judged. A
  malformed envelope is a hard :class:`SemanticEvaluationError` (schema validation),
  kept distinct from thin-but-valid evidence (degraded confidence).
- **read-only service** (:class:`SemanticEvaluator`) — orchestrate the above over a
  freshly generated, self-contained evaluation dir (``evidence.json`` +
  ``static_metrics.json`` + ``semantic_report.json``), driving an **injected**
  backend (:class:`SemanticBackend`) exactly as the intake manager injects its
  agent. The concrete configured/CLI backend lands in session-2; session-1 proves
  the whole boundary with a fake backend and the default
  :class:`_UnavailableSemanticBackend` (so the service is always constructible and
  an unconfigured backend fails cleanly, never crashes).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .evaluation import EvaluationDisabledError, EvaluationService, EvidencePack
from .models import (
    EvaluationTarget,
    ManagedRepo,
    SemanticEvaluatorInfo,
    SemanticFinding,
    SemanticReport,
    SemanticScore,
    StaticMetricsReport,
)
from .static_metrics import STATIC_METRICS_NAME, StaticEvaluator

SEMANTIC_REPORT_NAME = "semantic_report.json"
"""The persisted semantic report, written in the evaluation's artifact dir beside
``evidence.json`` and ``static_metrics.json`` (one dir per evaluation)."""

SEMANTIC_SCHEMA_VERSION = "1"
"""Version of the semantic prompt/report contract. Stamped onto every report's
:class:`~orch_hub.models.SemanticEvaluatorInfo` so a rerun under a newer contract
is comparable rather than silently conflated with an older one."""

# Sentinels delimiting the imposed report JSON envelope. Distinct opaque markers
# (not ```` ```json ````) so evidence text that itself contains Markdown fences
# cannot confuse extraction — the same reasoning as the intake agent's envelope.
SEMANTIC_START = "<<<SEMANTIC_REPORT_JSON>>>"
SEMANTIC_END = "<<<END_SEMANTIC_REPORT_JSON>>>"

SEMANTIC_SEVERITIES = ("info", "low", "medium", "high")
"""Allowed :class:`~orch_hub.models.SemanticFinding` severities, low→high. An
envelope finding with any other severity is a schema violation (rejected), so the
severity axis stays a small, stable, sortable vocabulary."""

SEMANTIC_DIMENSIONS = (
    "knowledge_signal_density",
    "ai_freshness_utility",
    "handoff_completeness",
    "decision_trace",
    "acceptance_coverage",
    "scope_slice_fit",
    "rediscovery_risk",
    "protocol_soft_failures",
)
"""The semantic dimensions the scope names, offered to the model as the
recommended ``dimension`` vocabulary for scores/findings. Not enforced on model
output (a relevant extra dimension is allowed) — just the shared axis language."""

# -- confidence degradation for thin evidence ---------------------------------
# The scope's robustness rule: missing/partial evidence must *lower confidence*,
# not fail the evaluation. The harness multiplies the model's headline confidence
# by (1 - penalty) where penalty sums these documented per-omission costs (capped),
# so a report over thin evidence is honestly less certain. This is a harness
# guarantee, independent of whatever confidence the model self-reports.
_PENALTY_NO_STATIC = 0.30
"""Static metrics unavailable as input — a major deterministic signal is missing."""
_PENALTY_NO_AI_DIFFS = 0.10
"""``.ai/`` changed-file diffs unavailable (the evidence pack carries commit
subjects only). The common case today, so kept modest; it flips off once a future
pack carries ``.ai/`` diffs and :attr:`SemanticEvidence.ai_diffs_available` is True."""
_PENALTY_PER_WARNING = 0.05
"""Per evidence-degradation warning (missing/corrupt run artifact)."""
_PENALTY_WARNING_CAP = 0.30
"""Cap on the summed per-warning penalty, so a flood of run warnings alone cannot
zero confidence — the model still saw the task file."""
_PENALTY_CAP = 0.60
"""Overall cap on the degradation penalty: even the thinnest evidence retains some
confidence, because the primary subject (the task file) is (almost) always present."""

# -- bounded prompt sizing ----------------------------------------------------
_MAX_GIT_SUBJECTS = 50
"""Cap on git commit *subjects* fed to the model (the pack already caps commits at
:data:`orch_hub.evaluation.EVIDENCE_COMMIT_LIMIT`; this is a defensive second cap)."""


class SemanticEvaluationError(Exception):
    """A semantic evaluation failed: a backend/process/model error, or an output
    envelope that does not validate against the report schema.

    Raised by the backend (a spawn/exit/stream error, session-2) and by
    :func:`parse_report` (a missing envelope or a schema violation). The service
    lets it propagate after removing the reserved evaluation dir, so a failed
    evaluation leaves no half-written artifacts and never mutates target state.
    Kept distinct from :class:`~orch_hub.evaluation.EvaluationDisabledError` (the
    gate) — a disabled subsystem is not a failure, it is off.
    """


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass(frozen=True)
class SemanticEvidence:
    """The bounded, read-only input one semantic evaluation sees for a task.

    Distilled from the deterministic :class:`~orch_hub.evaluation.EvidencePack`
    plus the :class:`~orch_hub.models.StaticMetricsReport` into a model-ready,
    size-bounded selection — the counterpart of the evidence pack for Layer 2.
    ``task_file`` is the full ``.ai-tasks/<id>.md`` markdown (small, and the
    primary subject, so unlike run logs it is not truncated); ``static_metrics``
    is the static report's dict (scores, protocol facts, per-run rollups) or
    ``None`` when unavailable; ``git_subjects`` are commit *subjects* only (never
    diffs); ``evidence_warnings`` are the pack's evidence-degradation notes.

    Two things are deliberately **absent** and recorded in ``omissions`` (which is
    what makes bounded construction auditable rather than silent): the pack's
    bounded orchestrator-log / ``spawn.out`` tails are **excluded** from the model
    input (the scope bars raw logs; their signal survives as the static
    ``has_diagnostic`` rollup), and ``.ai/`` changed-file diffs are unavailable
    (``ai_diffs_available`` False — the pack carries commit subjects only). Both
    lower :meth:`confidence_penalty` rather than failing the evaluation.
    """

    evaluation_id: str
    repo_name: str
    task_id: str
    target_present: bool
    target_source: str | None
    task_file: str | None
    static_metrics: dict | None
    git_subjects: tuple[str, ...]
    evidence_warnings: tuple[str, ...]
    ai_diffs_available: bool
    omissions: tuple[str, ...]

    @property
    def static_metrics_available(self) -> bool:
        return self.static_metrics is not None

    def confidence_penalty(self) -> float:
        """Degradation penalty in ``[0, _PENALTY_CAP]`` from missing/partial input.

        Sums the documented per-omission costs — static metrics absent, ``.ai/``
        diffs unavailable, and per evidence-degradation warning (capped) — so a
        thin evidence input honestly lowers the report's confidence. Capped so the
        thinnest evidence still retains some confidence (the task file is the
        primary subject and is almost always present)."""
        penalty = 0.0
        if not self.static_metrics_available:
            penalty += _PENALTY_NO_STATIC
        if not self.ai_diffs_available:
            penalty += _PENALTY_NO_AI_DIFFS
        penalty += min(_PENALTY_WARNING_CAP, len(self.evidence_warnings) * _PENALTY_PER_WARNING)
        return min(_PENALTY_CAP, penalty)

    def to_dict(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "repo_name": self.repo_name,
            "task_id": self.task_id,
            "target_present": self.target_present,
            "target_source": self.target_source,
            "task_file": self.task_file,
            "static_metrics": self.static_metrics,
            "git_subjects": list(self.git_subjects),
            "evidence_warnings": list(self.evidence_warnings),
            "ai_diffs_available": self.ai_diffs_available,
            "omissions": list(self.omissions),
        }


def build_semantic_evidence(
    pack: EvidencePack, static_report: StaticMetricsReport | None
) -> SemanticEvidence:
    """Distil a bounded, read-only :class:`SemanticEvidence` from the inputs (pure).

    Selects only what a semantic judge needs and can be trusted with: the full
    task file, the static-metrics dict (when computed), git commit subjects, and
    the pack's evidence warnings. Raw logs are excluded by construction, and every
    exclusion is appended to ``omissions`` so the omission is a fact in the record,
    not a silent gap. Total over any pack: an absent task file, an absent static
    report, and a git-less pack all degrade to their empty defaults and lower the
    confidence penalty rather than raising.
    """
    git = pack.git
    git_subjects = ()
    if git is not None and git.available:
        git_subjects = tuple(c.subject for c in git.commits[:_MAX_GIT_SUBJECTS])

    evidence_warnings = tuple(pack.warnings) + tuple(
        w for run in pack.runs for w in run.warnings
    )

    omissions: list[str] = []
    if pack.runs:
        # The pack carries bounded log/spawn.out tails, but they are intentionally
        # not forwarded to the model (raw logs are barred, and the deterministic
        # ``has_diagnostic`` rollup already captures whether a diagnostic existed).
        omissions.append(
            "raw orchestrator logs and spawn.out excluded from the evaluator input"
            " (bounded tails live in evidence.json; the static has_diagnostic"
            " rollup summarizes them)"
        )
    # ``.ai/`` diffs are not derivable from the evidence pack yet (it carries commit
    # subjects only), so the evaluator never sees them; record it as a degradation.
    ai_diffs_available = False
    omissions.append(
        ".ai/ changed-file diffs unavailable (evidence pack carries commit subjects"
        " only); knowledge-signal judgment rests on the task file and commit subjects"
    )
    if static_report is None:
        omissions.append("static metrics unavailable as input")
    if not pack.target.present:
        omissions.append("target task file absent (evaluable for run history only)")

    return SemanticEvidence(
        evaluation_id=pack.evaluation_id,
        repo_name=pack.target.repo_name,
        task_id=pack.target.task_id,
        target_present=pack.target.present,
        target_source=pack.target.source,
        task_file=pack.target.content,
        static_metrics=static_report.to_dict() if static_report is not None else None,
        git_subjects=git_subjects,
        evidence_warnings=evidence_warnings,
        ai_diffs_available=ai_diffs_available,
        omissions=tuple(omissions),
    )


# -- prompt contract ----------------------------------------------------------


def build_prompt(evidence: SemanticEvidence) -> str:
    """Render the evaluator prompt for ``evidence`` (pure, deterministic).

    Frames the model as a read-only task-quality evaluator, hands it the bounded
    evidence (task file, static-metrics JSON, git subjects, and the explicit
    omissions), names the semantic dimensions to assess, and requires the single
    JSON report envelope between :data:`SEMANTIC_START` / :data:`SEMANTIC_END`.
    The imposed-envelope contract mirrors the intake agent's: a conversational
    model surface has no native structured result, so the prompt makes the schema
    explicit and the parser enforces it.
    """
    static_json = (
        json.dumps(evidence.static_metrics, indent=1, sort_keys=True)
        if evidence.static_metrics is not None
        else "(static metrics unavailable)"
    )
    git_block = (
        "\n".join(f"- {subject}" for subject in evidence.git_subjects)
        if evidence.git_subjects
        else "(no git commit window available)"
    )
    omissions_block = (
        "\n".join(f"- {note}" for note in evidence.omissions)
        if evidence.omissions
        else "- (none)"
    )
    dimensions_block = "\n".join(f"- {dim}" for dim in SEMANTIC_DIMENSIONS)
    task_block = evidence.task_file or "(no task file content — evaluate run history only)"

    return f"""You are a READ-ONLY task-quality evaluator for an AI-coding project. \
You are judging ONE completed task's process and knowledge quality from the bounded \
evidence below. Do NOT create, edit, or delete any file, run any command that \
changes state, or start any process — you are only diagnosing. You may read the \
repository to inspect context, but you produce only the JSON report described at \
the end.

Assess these semantic dimensions (use them as the `dimension` vocabulary for your \
scores and findings; you may add another dimension if genuinely relevant):
{dimensions_block}

Consider, per dimension: knowledge signal density and whether `.ai/` looks \
fresh/useful, session-log handoff completeness, the decision trace (were choices \
and rejected alternatives recorded), acceptance coverage, scope/slice fit versus \
the estimate, rediscovery risk (what a future session would have to re-derive), \
and protocol soft-failures (things technically legal but weak).

EVIDENCE BOUNDS: raw logs are excluded and some inputs are unavailable — treat the \
omissions as reduced certainty, NOT as defects. Lower your confidence for thin \
evidence; do not invent findings you cannot support from the evidence.

== TASK FILE ({evidence.task_id}, source={evidence.target_source}) ==
{task_block}

== DETERMINISTIC STATIC METRICS (already computed; do not recompute) ==
{static_json}

== GIT COMMIT SUBJECTS (window over the task's runs; subjects only, no diffs) ==
{git_block}

== EVIDENCE OMISSIONS (what you are NOT seeing, and why) ==
{omissions_block}

When your assessment is complete, output EXACTLY ONE JSON object between the two \
markers below — nothing but the object between them, each marker alone on its own \
line. Every score MUST carry a rationale (no naked scores); every finding MUST \
carry evidence_refs pointing into the evidence above.

{SEMANTIC_START}
{{
  "summary": "<2-4 sentence overall assessment>",
  "confidence": <overall 0.0-1.0 certainty given the evidence bounds>,
  "scores": [
    {{"dimension": "<one of the dimensions>", "score": <0.0-1.0 or null>, "confidence": <0.0-1.0>, "rationale": "<why this score>"}}
  ],
  "findings": [
    {{"dimension": "<dimension>", "severity": "<info|low|medium|high>", "summary": "<one line>", "detail": "<optional explanation or null>", "confidence": <0.0-1.0>, "evidence_refs": ["<pointer into the evidence>"], "suggestion": "<concrete improvement, often a system/protocol change, or null>"}}
  ],
  "improvements": ["<suggested system or protocol improvement>"]
}}
{SEMANTIC_END}

The JSON must be valid: encode newlines inside strings as \\n. Output the markers \
and the object even if the evidence is thin (use low confidence and few findings)."""


# -- backend ------------------------------------------------------------------


class SemanticBackend(Protocol):
    """The model backend the semantic evaluator drives (injected like the intake
    agent). ``evaluate`` runs one read-only evaluation turn for ``prompt`` in the
    context of ``repo`` and returns the model's raw text (the report envelope); the
    service parses/validates it. ``info`` is the backend's rerun identity
    (backend/model/schema-version), stamped onto the report.

    Contract: ``evaluate`` MUST be read-only — it may read the repo to inspect
    context (e.g. ``.ai/``) but must not write target repos, ``.ai/``,
    ``.ai-tasks/``, run/intake state, or task lifecycle. A backend that cannot run
    raises :class:`SemanticEvaluationError`, which the service turns into a clean
    failed evaluation (no partial artifacts, no target mutation).
    """

    @property
    def info(self) -> SemanticEvaluatorInfo: ...

    def evaluate(self, prompt: str, repo: ManagedRepo) -> str: ...


class _UnavailableSemanticBackend:
    """Default backend: no concrete evaluator model is wired yet.

    The service, its schema, evidence build, prompt contract, persistence, and
    read-only boundary are fully exercised with an injected backend (a fake in
    tests; the real configured/CLI backend lands in session-2). Until then this
    default makes every evaluation fail cleanly — a :class:`SemanticEvaluationError`
    the service surfaces after removing the reserved dir — rather than crashing or
    fabricating a report.
    """

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return SemanticEvaluatorInfo(
            backend="unavailable", model=None, schema_version=SEMANTIC_SCHEMA_VERSION
        )

    def evaluate(self, prompt: str, repo: ManagedRepo) -> str:
        raise SemanticEvaluationError("no semantic evaluator backend configured")


# -- report parse / validate --------------------------------------------------


def parse_report(
    text: str,
    *,
    evaluation_id: str,
    target: EvaluationTarget,
    evaluator: SemanticEvaluatorInfo,
    generated_ts: str | None,
    evidence: SemanticEvidence,
) -> SemanticReport:
    """Parse and validate the model's envelope into a :class:`SemanticReport`.

    The model supplies only the judgment fields; identity and provenance
    (``evaluation_id``, repo/task, ``evaluator``, ``generated_ts``, input links)
    are stamped from context so the model cannot spoof what it evaluated. A missing
    envelope, a non-object envelope, or a schema violation (bad severity, a score
    without a rationale, a non-numeric where a number is required, a missing
    top-level ``confidence``) raises :class:`SemanticEvaluationError` — schema
    validation is a hard gate, kept distinct from thin-but-valid evidence. Numeric
    ranges are clamped to ``[0, 1]`` (lenient on ranges, strict on types/required
    fields).

    ``overall_confidence`` is the model's reported confidence *degraded* by
    :meth:`SemanticEvidence.confidence_penalty`, and the evidence omissions are
    folded into the report ``warnings`` — so thin evidence yields a lower-confidence
    report with the reasons attached, never a failure.
    """
    obj = _extract_json_envelope(text, SEMANTIC_START)
    scores = tuple(_parse_score(entry) for entry in _require_list(obj, "scores"))
    findings = tuple(_parse_finding(entry) for entry in _require_list(obj, "findings"))
    improvements = _parse_str_list(obj.get("improvements"))
    summary = _optional_str(obj.get("summary"))
    model_confidence = _require_confidence(obj, "confidence")

    overall_confidence = _clamp01(model_confidence * (1.0 - evidence.confidence_penalty()))
    warnings = _degradation_warnings(evidence)

    return SemanticReport(
        evaluation_id=evaluation_id,
        generated_ts=generated_ts,
        repo_name=target.repo_name,
        task_id=target.task_id,
        target_present=target.present,
        target_source=target.source,
        evaluator=evaluator,
        summary=summary,
        overall_confidence=overall_confidence,
        scores=scores,
        findings=findings,
        improvements=improvements,
        input_evidence_id=evaluation_id,
        input_static_metrics=evidence.static_metrics_available,
        warnings=warnings,
    )


def _degradation_warnings(evidence: SemanticEvidence) -> tuple[str, ...]:
    """The report's evidence-degradation warnings: the evidence omissions, plus a
    headline note when a penalty was applied — so a reader sees why confidence
    dropped without re-deriving it."""
    warnings = list(evidence.omissions)
    if evidence.confidence_penalty() > 0:
        warnings.append(
            "overall confidence degraded for missing/partial evidence"
            " (see omissions above)"
        )
    return tuple(warnings)


def _extract_json_envelope(text: str, marker: str) -> dict:
    """Extract the one JSON object after the last ``marker`` in ``text``.

    Mirrors the intake agent's envelope extraction (a local copy, so the semantic
    module stays independent of intake internals): ``rfind`` picks the last marker
    so a prose mention of the sentinel before the real block still parses, and
    ``raw_decode`` reads exactly one object, tolerating the closing marker and any
    trailing prose. A missing marker or a non-object is a schema violation.
    """
    idx = text.rfind(marker)
    if idx == -1:
        raise SemanticEvaluationError(f"evaluator output has no {marker} marker")
    region = text[idx + len(marker):]
    brace = region.find("{")
    if brace == -1:
        raise SemanticEvaluationError(f"no JSON object after {marker}")
    try:
        obj, _end = json.JSONDecoder().raw_decode(region[brace:])
    except ValueError as err:
        raise SemanticEvaluationError(f"invalid JSON after {marker}: {err}") from None
    if not isinstance(obj, dict):
        raise SemanticEvaluationError(f"{marker} did not wrap a JSON object")
    return obj


def _require_list(obj: dict, key: str) -> list:
    """Return ``obj[key]`` as a list; absent is an empty list, a non-list is a
    schema violation (a scalar where the schema requires an array)."""
    value = obj.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise SemanticEvaluationError(f"{key!r} must be a JSON array")
    return value


def _parse_score(entry: object) -> SemanticScore:
    """Validate one score entry. ``dimension`` and ``rationale`` are required
    non-empty strings (no naked score — a score must be explained); ``score`` is a
    clamped number or ``null``; ``confidence`` a clamped number (default 0.0)."""
    if not isinstance(entry, dict):
        raise SemanticEvaluationError("each score must be a JSON object")
    return SemanticScore(
        dimension=_require_nonempty_str(entry, "dimension", "score"),
        score=_optional_clamped_number(entry.get("score"), "score.score"),
        confidence=_clamp01(_number(entry.get("confidence"), "score.confidence", default=0.0)),
        rationale=_require_nonempty_str(entry, "rationale", "score"),
    )


def _parse_finding(entry: object) -> SemanticFinding:
    """Validate one finding entry. ``dimension``/``summary`` are required non-empty
    strings; ``severity`` must be one of :data:`SEMANTIC_SEVERITIES`; ``confidence``
    is a clamped number; ``evidence_refs`` a list of strings; ``detail``/``suggestion``
    optional strings."""
    if not isinstance(entry, dict):
        raise SemanticEvaluationError("each finding must be a JSON object")
    severity = entry.get("severity")
    if severity not in SEMANTIC_SEVERITIES:
        raise SemanticEvaluationError(
            f"finding severity {severity!r} is not one of {SEMANTIC_SEVERITIES}"
        )
    return SemanticFinding(
        dimension=_require_nonempty_str(entry, "dimension", "finding"),
        severity=severity,
        summary=_require_nonempty_str(entry, "summary", "finding"),
        confidence=_clamp01(_number(entry.get("confidence"), "finding.confidence", default=0.0)),
        evidence_refs=_parse_str_list(entry.get("evidence_refs")),
        detail=_optional_str(entry.get("detail")),
        suggestion=_optional_str(entry.get("suggestion")),
    )


def _parse_str_list(value: object) -> tuple[str, ...]:
    """Coerce a JSON value into a tuple of non-empty strings (absent → empty).

    Lenient — a non-list, or non-string / blank members, are dropped rather than
    rejected: these are best-effort auxiliary fields (improvements, evidence refs),
    not load-bearing schema like severity or a score's rationale."""
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _require_confidence(obj: dict, key: str) -> float:
    """Return the required top-level headline confidence as a clamped number; its
    absence or a non-number is a schema violation (it is the report's headline)."""
    value = obj.get(key)
    if not _is_number(value):
        raise SemanticEvaluationError(f"report {key!r} must be a number in 0.0-1.0")
    return _clamp01(float(value))


def _require_nonempty_str(entry: dict, key: str, what: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SemanticEvaluationError(f"{what} is missing a non-empty {key!r}")
    return value.strip()


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(value: object, what: str, *, default: float) -> float:
    """A number, or ``default`` when absent (``None``); a non-numeric non-None is a
    schema violation."""
    if value is None:
        return default
    if not _is_number(value):
        raise SemanticEvaluationError(f"{what} must be a number")
    return float(value)


def _optional_clamped_number(value: object, what: str) -> float | None:
    """A clamped number, or ``None`` when the model marked the axis unassessable; a
    non-numeric non-None is a schema violation."""
    if value is None:
        return None
    if not _is_number(value):
        raise SemanticEvaluationError(f"{what} must be a number or null")
    return _clamp01(float(value))


def _clamp01(value: float) -> float:
    """Clamp to ``[0, 1]`` and round to 3 places for stable JSON (matches the
    static evaluator's ``_clamp01``)."""
    return round(max(0.0, min(1.0, value)), 3)


def _write_json(path: Path, data: dict) -> None:
    """Persist a report artifact atomically (same-dir tmp + rename), matching
    ``evaluation._write_evidence`` / ``static_metrics._write_json``."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class SemanticEvaluator:
    """Read-only semantic task evaluator, gated by ``evaluation_enabled``.

    Layer 2 of the optional evaluation subsystem: it shares the gate (every
    operation fails closed with
    :class:`~orch_hub.evaluation.EvaluationDisabledError` when disabled) and the
    read-only invariant (the only writes are hub-owned artifacts under
    ``<data_dir>/evaluations``). Collaborators are injectable exactly like the
    static evaluator and intake manager: ``evaluation_service`` generates the
    evidence pack, ``static_evaluator`` computes the deterministic report,
    ``backend`` is the model driver (a fake in tests; the default
    :class:`_UnavailableSemanticBackend` fails cleanly until session-2 wires a real
    one), and ``clock`` stamps the report's ``generated_ts``.

    :meth:`evaluate` is the whole flow: generate a fresh, self-contained evaluation
    dir (``evidence.json`` + ``static_metrics.json``), build the bounded semantic
    evidence and prompt, drive the backend, validate the report, and persist
    ``semantic_report.json`` beside the other two. A rerun (a different model, or
    just a re-request) mints its own dir, so previous reports are preserved and
    comparable — the scope's rerun/version history falls out of the foundation's
    per-evaluation dir layout rather than needing a bespoke store.
    """

    def __init__(
        self,
        config: Config,
        *,
        evaluation_service: EvaluationService | None = None,
        static_evaluator: StaticEvaluator | None = None,
        backend: SemanticBackend | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ):
        self._config = config
        self._service = (
            evaluation_service
            if evaluation_service is not None
            else EvaluationService(config)
        )
        # Share the one evaluation service with the static evaluator so an injected
        # fake service (record loader / git runner) flows to both layers.
        self._static = (
            static_evaluator
            if static_evaluator is not None
            else StaticEvaluator(config, evaluation_service=self._service)
        )
        self._backend: SemanticBackend = (
            backend if backend is not None else _UnavailableSemanticBackend()
        )
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._config.evaluation_enabled

    def _require_enabled(self) -> None:
        if not self._config.evaluation_enabled:
            raise EvaluationDisabledError(
                "task evaluation is disabled (ORCH_HUB_EVAL_ENABLED)"
            )

    def build_evidence(self, pack: EvidencePack) -> SemanticEvidence:
        """Build the bounded semantic evidence for an already-assembled pack.

        Computes the deterministic static report for the pack (pure), then distils
        both into :class:`SemanticEvidence`. Exposed so a caller (or a test) can
        inspect exactly what the model will see without invoking a backend or
        persisting anything. Gated; read-only.
        """
        self._require_enabled()
        static_report = self._static.build_report(pack)
        return build_semantic_evidence(pack, static_report)

    def evaluate(self, repo: ManagedRepo, task_id: str) -> SemanticReport:
        """Run one semantic evaluation for ``(repo, task_id)`` and persist it.

        Generates a fresh, self-contained evaluation (``evidence.json`` +
        ``static_metrics.json``), builds the bounded evidence and prompt, drives the
        injected backend read-only, validates the returned report, and writes
        ``semantic_report.json`` into the same dir — so one dir holds the whole
        evidence→static→semantic chain, tied by ``evaluation_id``.

        Atomic and fail-closed: :meth:`EvaluationService.generate_evidence` mints
        and reserves the dir (removing it on its own failure); every step after is
        wrapped so a static-write, backend/model, parse/schema, or report-write
        failure removes the whole reserved dir and re-raises — a failed evaluation
        leaves no half-written artifacts and, being confined to the hub data dir,
        never touches target repo/run/task state.
        """
        self._require_enabled()
        pack = self._service.generate_evidence(repo, task_id)
        assert pack.evaluation_dir is not None  # generate_evidence always sets it
        eval_dir = pack.evaluation_dir
        try:
            static_report = self._static.build_report(pack)
            _write_json(eval_dir / STATIC_METRICS_NAME, static_report.to_dict())
            evidence = build_semantic_evidence(pack, static_report)
            prompt = build_prompt(evidence)
            text = self._backend.evaluate(prompt, repo)
            report = parse_report(
                text,
                evaluation_id=pack.evaluation_id,
                target=pack.target,
                evaluator=self._backend.info,
                generated_ts=self._clock().isoformat(),
                evidence=evidence,
            )
            _write_json(eval_dir / SEMANTIC_REPORT_NAME, report.to_dict())
        except Exception:
            # generate_evidence created this dir; remove the whole reserved
            # evaluation (evidence + static + any partial semantic write) so a
            # failed evaluation leaves nothing behind — the same no-half-pair
            # cleanup the static/evidence generators use.
            shutil.rmtree(eval_dir, ignore_errors=True)
            raise
        return report
