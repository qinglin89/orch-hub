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
  as are the unavailable ``.ai/`` diffs and the current ``.ai/`` file contents
  (leaving ``.ai/`` freshness unassessable) — so a thin input *lowers confidence*
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
  ``static_metrics.json`` + ``semantic_report.json`` + a human-readable
  ``report.md`` render), driving an **injected** backend (:class:`SemanticBackend`)
  exactly as the intake manager injects its agent. The concrete backend is
  :class:`ClaudeSemanticBackend` — one ``claude -p`` turn whose evidence isolation
  is *structural*: it runs with every built-in tool disabled (``--tools ""`` — no
  Read/Bash/Grep, so it cannot open ``.ai/``, raw logs, secrets, or any repo file),
  with project customizations disabled (``--safe-mode``), and from a fresh hub-owned
  empty cwd (never the target repo), so the model judges from the bounded prompt
  alone rather than live repository state; model from ``ORCH_HUB_EVAL_MODEL``, with
  an injected subprocess seam so every mechanic is unit-tested without launching
  ``claude``; the default :class:`_UnavailableSemanticBackend` still fails cleanly
  when no backend is wired.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Config
from .evaluation import EvaluationDisabledError, EvaluationService, EvidencePack
from .models import (
    EvaluationTarget,
    ManagedRepo,
    SemanticDoctor,
    SemanticEvaluatorInfo,
    SemanticFinding,
    SemanticReport,
    SemanticScore,
    StaticMetricsReport,
)
from .runs import _orchestrator_env
from .static_metrics import STATIC_METRICS_NAME, StaticEvaluator

SEMANTIC_REPORT_NAME = "semantic_report.json"
"""The persisted semantic report, written in the evaluation's artifact dir beside
``evidence.json`` and ``static_metrics.json`` (one dir per evaluation)."""

SEMANTIC_REPORT_MD_NAME = "report.md"
"""The persisted human-readable semantic report, rendered from the same
:class:`~orch_hub.models.SemanticReport` and written beside
``semantic_report.json`` (an export a human can read directly from the data dir,
and the download affordance the dashboard layer surfaces). Written atomically in
the same step as the JSON — a dir with the JSON has this too."""

SEMANTIC_SCHEMA_VERSION = "1"
"""Version of the semantic prompt/report contract. Stamped onto every report's
:class:`~orch_hub.models.SemanticEvaluatorInfo` so a rerun under a newer contract
is comparable rather than silently conflated with an older one."""

SEMANTIC_BACKEND_CLAUDE = "claude-cli"
"""``SemanticEvaluatorInfo.backend`` value for the concrete Claude CLI backend."""

DEFAULT_SEMANTIC_EFFORT = "high"
"""Reasoning effort for the semantic evaluation turn. Judging one task's process
quality is a bounded analysis, so this defaults below the orchestrator's ``max``;
injectable, and the model itself comes from ``ORCH_HUB_EVAL_MODEL``."""

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
_PENALTY_NO_AI_SNIPPETS = 0.10
"""Current ``.ai/`` file contents (snippets) unavailable as input — the
``ai_freshness_utility`` dimension cannot be grounded from the evidence, so it is
marked unassessable in the prompt and confidence is reduced. A distinct signal from
the diffs above (current state vs. what changed); like it, the common case today and
kept modest, flipping off once a future pack carries ``.ai/`` snippets and
:attr:`SemanticEvidence.ai_snippets_available` is True."""
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

    Three things are deliberately **absent** and recorded in ``omissions`` (which is
    what makes bounded construction auditable rather than silent): the pack's
    bounded orchestrator-log / ``spawn.out`` tails are **excluded** from the model
    input (the scope bars raw logs; their signal survives as the static
    ``has_diagnostic`` rollup), ``.ai/`` changed-file diffs are unavailable
    (``ai_diffs_available`` False — the pack carries commit subjects only), and the
    current ``.ai/`` file contents are unavailable too (``ai_snippets_available``
    False — the pack carries no ``.ai/`` snippets). The last means ``.ai/``
    freshness/utility cannot be grounded from the evidence, so the prompt marks the
    ``ai_freshness_utility`` dimension unassessable rather than inviting the backend
    to read repo files outside this bounded input. All three lower
    :meth:`confidence_penalty` rather than failing the evaluation.
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
    ai_snippets_available: bool
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
        if not self.ai_snippets_available:
            penalty += _PENALTY_NO_AI_SNIPPETS
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
            "ai_snippets_available": self.ai_snippets_available,
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
    # Current ``.ai/`` file contents are likewise not carried by the pack, so the
    # evaluator has no grounded view of ``.ai/`` to judge freshness/utility. Rather
    # than leave that ungrounded or invite a live repo read outside this bounded
    # input, record it, degrade confidence, and mark the dimension unassessable in
    # the prompt (:func:`build_prompt`). Flips True when a future pack carries snippets.
    ai_snippets_available = False
    omissions.append(
        ".ai/ file contents unavailable to the evaluator (evidence pack carries no"
        " .ai/ snippets); the ai_freshness_utility dimension is unassessable from this"
        " evidence and is marked so in the prompt"
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
        ai_snippets_available=ai_snippets_available,
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
changes state, or start any process — you are only diagnosing. Base your judgment \
ONLY on the bounded evidence below: do not read or rely on repository state that is \
not shown here, and produce only the JSON report described at the end.

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
evidence; do not invent findings you cannot support from the evidence. In \
particular, the current `.ai/` file contents are NOT included here, so `.ai/` \
freshness/utility is unassessable from this evidence: score the \
`ai_freshness_utility` dimension null (or low confidence) rather than guessing, and \
treat any `.ai/`-freshness aspect of `knowledge_signal_density` as only weakly \
grounded.

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

    Contract: ``evaluate`` MUST be read-only AND evidence-bounded — it bases its
    judgment ONLY on the ``prompt``'s bounded evidence, never on live repository
    reads outside that persisted boundary (so the report is reproducible from the
    persisted artifacts, and the prompt's "unassessable" markers for absent inputs
    like ``.ai/`` contents actually hold). Evidence-boundedness must be enforced,
    not merely requested of the model: a prompt instruction alone does not stop a
    tool-enabled agent from opening ``.ai/``, raw logs, or secrets. It must also not
    write target repos, ``.ai/``, ``.ai-tasks/``, run/intake state, or task
    lifecycle. A backend that cannot run raises :class:`SemanticEvaluationError`,
    which the service turns into a clean failed evaluation (no partial artifacts, no
    target mutation).
    """

    @property
    def info(self) -> SemanticEvaluatorInfo: ...

    def evaluate(self, prompt: str, repo: ManagedRepo) -> str: ...

    def doctor(self) -> SemanticDoctor:
        """Report whether this backend can actually run, without a model call.

        A cheap, side-effect-free configuration/availability probe (no ``evaluate``
        turn, no cost) used to gate the dashboard's Layer 2 control and answer
        ``orch-hub eval doctor``. It reports the backend identity plus ``available``
        — e.g. the concrete Claude backend checks that ``claude`` resolves on PATH.
        It MUST NOT launch an evaluation or mutate any state.
        """
        ...


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

    def doctor(self) -> SemanticDoctor:
        """Never available — no concrete evaluator model is wired."""
        return SemanticDoctor(
            backend="unavailable",
            model=None,
            schema_version=SEMANTIC_SCHEMA_VERSION,
            available=False,
            detail="no semantic evaluator backend configured",
        )


# -- concrete Claude CLI backend ----------------------------------------------
# The real evaluator: one headless ``claude -p`` turn. It mirrors the intake
# agent's CLI mechanics (:mod:`orch_hub.intake_agent`) but is a *single* stateless
# turn (no session threading — the whole bounded evidence is in the one prompt), so
# it stays a local, self-contained backend rather than importing intake internals
# (the same independence stance the envelope extractor takes).
#
# Evidence isolation is STRUCTURAL, not prompt-deep. Intake runs its draft turn
# under ``--permission-mode plan`` — but that is a read-only *exploration* sandbox
# where the agent still reads/inspects repo files freely (intake WANTS that: it
# designs a task by reading the repo). A semantic evaluation is the opposite: its
# report must be bounded to the persisted :class:`SemanticEvidence` and reproducible
# from it, so the turn must not read live repo state at all. Plan mode only blocks
# writes, so this backend adds the read gate the evaluator needs: ``--tools ""``
# disables every built-in tool (no Read/Bash/Grep — it cannot open ``.ai/``, raw
# logs, secrets, or any file), ``--safe-mode`` disables project customizations (no
# target-repo ``CLAUDE.md``/skills/hooks/MCP injected into context), and the turn
# runs from a fresh hub-owned empty cwd (never the target repo — see
# :meth:`ClaudeSemanticBackend.evaluate`) so even passive cwd/git-status context
# reaches no repo state. ``--permission-mode plan`` is kept as a write-side
# belt-and-suspenders and ``--dangerously-skip-permissions`` is never added. The
# subprocess is an injected seam (:data:`SemanticRunner`) exactly like the run
# manager's ``spawner`` and the intake agent's ``runner``, so every mechanic is
# unit-tested with a fake and the thin real runner (:func:`_default_runner`) stays
# untested.


@dataclass(frozen=True)
class SemanticRunResult:
    """One completed CLI turn: captured stdout/stderr and the exit code."""

    stdout: str
    stderr: str
    returncode: int


SemanticRunner = Callable[[Sequence[str], Path], SemanticRunResult]
"""Runs one evaluator-CLI turn to completion in ``cwd`` and returns its captured
output (blocking and output-capturing — the ``stream-json`` stdout must be parsed,
not detached). Tests inject a fake; :func:`_default_runner` is the real one."""


def _default_runner(argv: Sequence[str], cwd: Path) -> SemanticRunResult:
    """Run ``claude`` to completion, capturing stdout/stderr (the real runner).

    Uses the augmented PATH shared with detached orchestrator and intake runs
    (:func:`orch_hub.runs._orchestrator_env`): the hub may run under launchd with
    a sparse PATH while ``claude`` is installed under the user's home, and this
    backend launches it by name. ``capture_output`` drains both pipes so a large
    ``stream-json`` stdout cannot deadlock against stderr. Untested, like runs'
    ``_default_spawner`` and the intake agent's ``_default_runner``.
    """
    proc = subprocess.run(
        list(argv),
        cwd=str(cwd),
        env=_orchestrator_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return SemanticRunResult(
        stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode
    )


def _default_which() -> str | None:
    """Resolve the ``claude`` executable on the augmented PATH, or ``None``.

    Uses the same augmented PATH the real evaluator/orchestrator/intake runs use
    (:func:`orch_hub.runs._orchestrator_env`) so a doctor probe agrees with what a
    real ``evaluate`` turn would find — the hub may run under launchd with a sparse
    PATH while ``claude`` is installed under the user's home. Pure filesystem
    resolution (``shutil.which``): no subprocess, no model call, safe to call on a
    dashboard view load. Looked up as a module global inside
    :meth:`ClaudeSemanticBackend.doctor` so a test can monkeypatch it."""
    return shutil.which("claude", path=_orchestrator_env().get("PATH"))


class ClaudeSemanticBackend:
    """Drives one read-only, evidence-bounded evaluation turn through ``claude -p``.

    Implements the :class:`SemanticBackend` protocol. :meth:`evaluate` runs a single
    ``claude -p --output-format stream-json … --tools "" --safe-mode
    --no-session-persistence --permission-mode plan`` process from a fresh hub-owned
    empty cwd (NOT the target repo), sending the self-contained evaluator prompt
    (:func:`build_prompt`, a plain prompt — not a slash command) and returning the
    assistant's final text (the report envelope) for the service to parse. With all
    tools disabled and an isolated cwd, the model judges from the bounded prompt
    alone and cannot reach target-repo files — so the report stays reproducible from
    the persisted evidence. ``model`` comes from ``ORCH_HUB_EVAL_MODEL``
    (via :attr:`~orch_hub.config.Config.eval_model`); ``runner`` is injected for
    tests. Constructing it launches nothing — a subprocess only starts when a
    semantic evaluation is actually requested — so the default app wiring can build
    it unconditionally, exactly like the intake agent.
    """

    def __init__(
        self,
        *,
        model: str,
        effort: str = DEFAULT_SEMANTIC_EFFORT,
        runner: SemanticRunner = _default_runner,
        which: Callable[[], str | None] | None = None,
    ):
        self.model = model
        self.effort = effort
        self._runner = runner
        # Injectable ``claude`` resolver for :meth:`doctor`; ``None`` means resolve
        # via the module-global :func:`_default_which` at call time (so a test can
        # monkeypatch that global) rather than binding it at construction.
        self._which = which

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return SemanticEvaluatorInfo(
            backend=SEMANTIC_BACKEND_CLAUDE,
            model=self.model,
            schema_version=SEMANTIC_SCHEMA_VERSION,
        )

    def doctor(self) -> SemanticDoctor:
        """Report whether ``claude`` is runnable, without launching an evaluation.

        A cheap availability probe: it resolves the ``claude`` executable on the
        augmented PATH (:func:`_default_which`, or the injected resolver) — the
        exact failure the session-1 UI could only surface as a 502 on click. It
        runs no ``claude`` process and makes no model call, so it is safe to call
        on a dashboard view load to gate the Layer 2 control. ``available`` is
        whether the CLI resolved; ``detail`` carries the resolved path or the
        not-found reason."""
        resolver = self._which if self._which is not None else _default_which
        path = resolver()
        if path:
            detail = f"claude resolved at {path}"
        else:
            detail = "claude executable not found on PATH"
        return SemanticDoctor(
            backend=SEMANTIC_BACKEND_CLAUDE,
            model=self.model,
            schema_version=SEMANTIC_SCHEMA_VERSION,
            available=bool(path),
            detail=detail,
        )

    def evaluate(self, prompt: str, repo: ManagedRepo) -> str:
        """Run the one read-only, evidence-bounded turn; return the assistant's text.

        Evidence isolation is structural (see :meth:`_argv` for the CLI gates). The
        turn additionally runs from a fresh hub-owned empty directory rather than
        ``repo.path``, so that even passive context (the cwd path, a git-status
        snapshot of the cwd) reflects no target-repo state and the model has nothing
        but the bounded prompt to judge from. ``repo`` is accepted for protocol
        conformance but deliberately NOT used as the working directory — reaching
        into the repo is exactly what the bounded evaluator must not do. The scratch
        dir is removed after the turn (it only exists so ``claude`` has a benign,
        empty place to run).

        Raises :class:`SemanticEvaluationError` on a nonzero exit or a stream-level
        error (the same success test the orchestrator/intake readers use), so the
        service turns a backend/model failure into a clean failed evaluation (dir
        removed, no partial artifacts, target state untouched).
        """
        argv = self._argv(prompt)
        sandbox = Path(tempfile.mkdtemp(prefix="orch-hub-semantic-"))
        try:
            result = self._runner(argv, sandbox)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
        text, errored = _extract_assistant_text(result.stdout)
        if result.returncode != 0 or errored:
            tail = (result.stderr or "").strip()
            suffix = f": {tail[-500:]}" if tail else ""
            raise SemanticEvaluationError(
                f"claude semantic evaluation turn failed (exit {result.returncode},"
                f" stream_error={errored}){suffix}"
            )
        return text

    def _argv(self, prompt: str) -> list[str]:
        """Build the ``claude`` argv for the read-only, evidence-bounded turn.

        The isolation gates are load-bearing — a bounded evaluator must judge from
        the prompt alone, never live repo state:

        - ``--tools ""`` disables every built-in tool, so the turn has no Read/Bash/
          Grep/etc. and literally cannot open ``.ai/``, raw logs, secrets, or any
          target file (the read gate; ``--permission-mode plan`` only blocks
          *writes*, still allowing reads — see the module comment above).
        - ``--safe-mode`` disables project customizations (target-repo ``CLAUDE.md``,
          skills, plugins, hooks, MCP servers, custom agents), so none of that repo
          state is injected into the model context. Auth/model/permissions still work.
        - ``--no-session-persistence`` — one stateless turn (no ``--resume``), so it
          leaves no session transcript on disk.
        - ``--permission-mode plan`` is kept as a write-side belt-and-suspenders and
          ``--dangerously-skip-permissions`` is never added, so the evaluator cannot
          write the target repo, ``.ai/``, or ``.ai-tasks/`` either.

        ``--tools ""`` is placed before another flag (never immediately before the
        positional prompt) so the variadic option cannot swallow the prompt as a
        tool name. Combined with the isolated empty cwd in :meth:`evaluate`, the
        model sees only the bounded evidence prompt.
        """
        return [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--tools",
            "",
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode",
            "plan",
            prompt,
        ]


def _extract_assistant_text(stdout: str) -> tuple[str, bool]:
    """Concatenate assistant text from a claude ``stream-json`` stdout.

    Returns ``(text, errored)`` where ``text`` is every assistant text block
    joined (the ``result`` event's ``result`` string is a fallback when no
    assistant text streamed) and ``errored`` is set by a ``result`` event with
    ``is_error``. A local copy of the intake agent's reader (kept here so the
    semantic module stays independent of intake internals), tolerant of non-JSON
    lines like the orchestrator's own reader.
    """
    chunks: list[str] = []
    result_text = ""
    errored = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "assistant":
            msg = ev.get("message")
            content = (msg or {}).get("content") if isinstance(msg, dict) else None
            for block in content or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        elif etype == "result":
            if ev.get("is_error"):
                errored = True
            res = ev.get("result")
            if isinstance(res, str):
                result_text = res
    return ("".join(chunks) or result_text), errored


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
    without a rationale, a finding with no usable ``evidence_refs``, a non-numeric
    where a number is required, a missing top-level ``confidence``) raises
    :class:`SemanticEvaluationError` — schema validation is a hard gate, kept
    distinct from thin-but-valid evidence. Numeric ranges are clamped to ``[0, 1]``
    (lenient on ranges, strict on types/required fields).

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
    is a clamped number; ``evidence_refs`` a *required* non-empty list of non-empty
    strings (a finding with no usable reference is unsupported — the prompt contract
    requires every finding to cite evidence, and the refs are what keep it auditable
    rather than an unfalsifiable assertion); ``detail``/``suggestion`` optional strings."""
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
        evidence_refs=_require_str_list(entry, "evidence_refs", "finding"),
        detail=_optional_str(entry.get("detail")),
        suggestion=_optional_str(entry.get("suggestion")),
    )


def _parse_str_list(value: object) -> tuple[str, ...]:
    """Coerce a JSON value into a tuple of non-empty strings (absent → empty).

    Lenient — a non-list, or non-string / blank members, are dropped rather than
    rejected: this is for best-effort auxiliary fields (``improvements``), not
    load-bearing schema like severity or a score's rationale. A finding's
    ``evidence_refs`` is *not* auxiliary (the prompt requires every finding to cite
    evidence), so it uses the strict :func:`_require_str_list` gate instead."""
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _require_str_list(entry: dict, key: str, what: str) -> tuple[str, ...]:
    """Return ``entry[key]`` as a non-empty tuple of non-empty strings, else raise.

    The strict counterpart of :func:`_parse_str_list` for a load-bearing list field.
    A missing/absent value or a non-array is a schema violation, and so is an array
    with no usable member (empty, or only blank/non-string entries) — the value must
    yield at least one non-empty string. Blank/non-string members are still dropped
    (lenient on the members) so long as one real reference survives. Used for a
    finding's ``evidence_refs``: a persisted finding must cite evidence, so an
    unsupported one is rejected here rather than stored as a valid diagnosis."""
    value = entry.get(key)
    if not isinstance(value, list):
        raise SemanticEvaluationError(f"{what} {key!r} must be a non-empty JSON array")
    refs = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if not refs:
        raise SemanticEvaluationError(
            f"{what} {key!r} must contain at least one non-empty string"
        )
    return refs


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


def _write_text(path: Path, text: str) -> None:
    """Persist a text artifact atomically (same-dir tmp + rename), the ``report.md``
    counterpart of :func:`_write_json` for non-JSON siblings."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# -- Markdown rendering -------------------------------------------------------


def render_report_md(report: SemanticReport) -> str:
    """Render a :class:`~orch_hub.models.SemanticReport` as human-readable Markdown.

    Pure and deterministic (a fixed report renders byte-identically) — the
    ``report.md`` sibling of ``semantic_report.json``. Sections mirror the report
    shape: a provenance header, the narrative summary (the explanation that
    replaces a naked overall score), a scores table (``n/a`` for an axis the model
    could not assess), findings ordered most-severe first, suggested improvements,
    and the evidence-degradation notes — so a human reading the file alone sees the
    whole diagnosis with its confidence and evidence caveats.
    """
    ev = report.evaluator
    target = report.target_source or ("present" if report.target_present else "absent")
    lines: list[str] = [
        f"# Semantic Task Evaluation — {report.task_id}",
        "",
        f"- **Repository:** {report.repo_name}",
        f"- **Evaluation ID:** {report.evaluation_id}",
        f"- **Generated:** {report.generated_ts or 'n/a'}",
        f"- **Evaluator:** {ev.backend} / {ev.model or 'n/a'} (schema v{ev.schema_version})",
        f"- **Target:** {target}",
        f"- **Overall confidence:** {_fmt_num(report.overall_confidence)}",
        f"- **Static metrics input:** {'yes' if report.input_static_metrics else 'no'}",
        "",
        "## Summary",
        "",
        report.summary or "_(no summary provided)_",
        "",
        "## Scores",
        "",
    ]
    if report.scores:
        lines += ["| Dimension | Score | Confidence | Rationale |", "|---|---|---|---|"]
        lines += [
            f"| {s.dimension} | {_fmt_score(s.score)} | {_fmt_num(s.confidence)}"
            f" | {_md_cell(s.rationale)} |"
            for s in report.scores
        ]
    else:
        lines.append("_(no scores)_")

    lines += ["", "## Findings", ""]
    if report.findings:
        for f in _findings_by_severity(report.findings):
            lines += [f"### [{f.severity.upper()}] {f.dimension} — {f.summary}", ""]
            if f.detail:
                lines += [f.detail, ""]
            lines.append(f"- Confidence: {_fmt_num(f.confidence)}")
            if f.evidence_refs:
                lines.append(f"- Evidence: {'; '.join(f.evidence_refs)}")
            if f.suggestion:
                lines.append(f"- Suggestion: {f.suggestion}")
            lines.append("")
    else:
        lines += ["_(no findings)_", ""]

    lines += ["## Suggested Improvements", ""]
    lines += [f"- {imp}" for imp in report.improvements] if report.improvements else ["_(none)_"]

    if report.warnings:
        lines += ["", "## Evidence Notes", ""]
        lines += [f"- {w}" for w in report.warnings]

    return "\n".join(lines) + "\n"


def _findings_by_severity(
    findings: Sequence[SemanticFinding],
) -> list[SemanticFinding]:
    """Findings most-severe first (``high`` → ``info``), stable within a severity
    (Python's ``reverse=True`` preserves input order among equal keys)."""
    rank = {sev: i for i, sev in enumerate(SEMANTIC_SEVERITIES)}
    return sorted(findings, key=lambda f: rank.get(f.severity, -1), reverse=True)


def _fmt_num(value: float) -> str:
    """Two-decimal fixed format for a 0.0–1.0 confidence/score, stable in text."""
    return f"{value:.2f}"


def _fmt_score(score: float | None) -> str:
    """A score cell: ``n/a`` when the model marked the axis unassessable, else the
    two-decimal number."""
    return "n/a" if score is None else _fmt_num(score)


def _md_cell(text: str) -> str:
    """Make free text safe inside a one-line Markdown table cell: newlines become
    spaces and a literal ``|`` is escaped so a rationale cannot break the row."""
    return text.replace("\n", " ").replace("|", "\\|")


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

    @property
    def evaluator_info(self) -> SemanticEvaluatorInfo:
        """The backend's rerun identity (backend/model/schema-version), no probe.

        A pure read of the injected backend's :attr:`SemanticBackend.info` — no
        model call, no ``claude`` resolution (unlike :meth:`doctor`), no gate. The
        semantic-job manager (:mod:`orch_hub.semantic_jobs`) stamps this onto a job
        at creation so the queued/running UI shows which backend/model is running
        before the report exists; it matches the ``evaluator`` the completed report
        carries, since both come from the same backend.
        """
        return self._backend.info

    def _require_enabled(self) -> None:
        if not self._config.evaluation_enabled:
            raise EvaluationDisabledError(
                "task evaluation is disabled (ORCH_HUB_EVAL_ENABLED)"
            )

    def doctor(self) -> SemanticDoctor:
        """Report Layer 2 readiness (backend availability + identity), gated.

        Fails closed with :class:`~orch_hub.evaluation.EvaluationDisabledError`
        when the subsystem is disabled (like every other operation), so the API
        maps a disabled hub to the uniform 404. Otherwise it delegates to the
        injected backend's :meth:`SemanticBackend.doctor` — a cheap
        configuration/availability check with no model call — so the dashboard can
        disable the Layer 2 control (keeping Layer 1 usable) and the CLI can smoke
        check the evaluator without ever gating run Start/Stop/log flows."""
        self._require_enabled()
        return self._backend.doctor()

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
        injected backend read-only, validates the returned report, and writes both
        ``semantic_report.json`` and its human-readable ``report.md`` render into
        the same dir — so one dir holds the whole evidence→static→semantic chain,
        tied by ``evaluation_id``. A rerun (a different model, or just a
        re-request) mints its own dir, preserving prior reports for comparison.

        Atomic and fail-closed: :meth:`EvaluationService.generate_evidence` mints
        and reserves the dir (removing it on its own failure); every step after is
        wrapped so a static-write, backend/model, parse/schema, or report-write
        failure removes the whole reserved dir and re-raises — a failed evaluation
        leaves no half-written artifacts and, being confined to the hub data dir,
        never touches target repo/run/task state. The all-or-nothing cleanup is
        deliberate: it keeps the invariant that every dir this method mints is a
        *complete* semantic evaluation, so an evidence+static-only dir unambiguously
        means a deterministic ``/metrics`` run, not a semantic attempt that failed
        at the model step. (Rejected keeping the evidence+static dir on a semantic
        failure: each evaluation is self-contained and a retry mints a fresh dir, so
        a leftover would not speed a retry — it would only blur that distinction and
        litter history on a flaky backend.)

        Retention is deferred to after the whole evidence→static→semantic set is
        durable: this generates with ``prune=False`` and calls
        :meth:`EvaluationService.auto_prune` only once every artifact is written, so
        a backend/model/parse/write failure — whose cleanup removes only this new
        dir — can never first delete the task's older retained evaluations.
        """
        self._require_enabled()
        pack = self._service.generate_evidence(repo, task_id, prune=False)
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
            _write_text(eval_dir / SEMANTIC_REPORT_MD_NAME, render_report_md(report))
        except Exception:
            # generate_evidence created this dir; remove the whole reserved
            # evaluation (evidence + static + any partial semantic write) so a
            # failed evaluation leaves nothing behind — the same no-half-pair
            # cleanup the static/evidence generators use.
            shutil.rmtree(eval_dir, ignore_errors=True)
            raise
        # Retention runs only here — after the complete evaluation is durable — so a
        # failure above never deletes older retained evaluations (see auto_prune).
        self._service.auto_prune(repo, task_id, protect=pack.evaluation_id)
        return report
