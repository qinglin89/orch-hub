"""Run an isolated, deterministic orch-hub visual demo.

The real FastAPI application and SPA run over repositories rooted in a fresh
temporary directory. Every boundary that could reach operator state or an
external agent is injected with a local deterministic implementation.

Run from the repository root with::

    python -m tools.visual_demo.server --port 8877
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import os
import shutil
import signal
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import uvicorn

# ``orch_hub.api`` constructs its normal module-level app during import. Fence
# even that unused instance into a private temporary root before importing it;
# an operator's existing ORCH_HUB_* environment must never leak into this tool.
DEMO_PREFIX = "orch-hub-visual-demo-"
_BOOTSTRAP_TMP = tempfile.TemporaryDirectory(prefix=f"{DEMO_PREFIX}bootstrap-")
_BOOTSTRAP_ROOT = Path(_BOOTSTRAP_TMP.name)
_BOOTSTRAP_VALUES = {
    "ORCH_HUB_MANDREL_PATH": _BOOTSTRAP_ROOT / "mandrel",
    "ORCH_HUB_DATA_DIR": _BOOTSTRAP_ROOT / "hub-data",
    "ORCH_HUB_CODEX_HOME": _BOOTSTRAP_ROOT / "codex-home",
    "ORCH_HUB_CLAUDE_HOME": _BOOTSTRAP_ROOT / "claude-home",
}
_BOOTSTRAP_ENV_NAMES = (*_BOOTSTRAP_VALUES, "ORCH_HUB_TOKEN", "ORCH_HUB_BARK_URL")
_BOOTSTRAP_OLD_ENV = {name: os.environ.get(name) for name in _BOOTSTRAP_ENV_NAMES}
for _name, _value in _BOOTSTRAP_VALUES.items():
    Path(_value).mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_value)
os.environ["ORCH_HUB_TOKEN"] = "unused-bootstrap-token"
os.environ.pop("ORCH_HUB_BARK_URL", None)

from orch_hub import api

# The imported module-level app stays fenced into the bootstrap directory, but
# importing this maintainer tool must not leave the caller's environment changed.
for _name, _old_value in _BOOTSTRAP_OLD_ENV.items():
    if _old_value is None:
        os.environ.pop(_name, None)
    else:
        os.environ[_name] = _old_value
from orch_hub.config import Config
from orch_hub.evaluation import EvaluationService
from orch_hub.git_meta import CommitLog
from orch_hub.intake import AgentConfirmResult, AgentDraftResult, IntakeManager
from orch_hub.models import (
    CommitInfo,
    EffectiveConfig,
    IntakeDraft,
    ManagedRepo,
    ProviderUsage,
    ReadinessCheck,
    ReadinessReport,
    RoleConfig,
    SemanticDoctor,
    SemanticEvaluatorInfo,
    SubscriptionUsage,
    UsageCredits,
    UsageWindow,
)
from orch_hub.report_catalog import ReportCatalog
from orch_hub.runs import RunManager
from orch_hub.semantic import (
    SEMANTIC_END,
    SEMANTIC_SCHEMA_VERSION,
    SEMANTIC_START,
    SemanticEvaluator,
)
from orch_hub.semantic_jobs import SemanticJobManager
from orch_hub.static_metrics import StaticEvaluator
from orch_hub.subprocess_utils import CommandResult


DEMO_TOKEN = "orch-hub-demo"
DEMO_NOW = dt.datetime(2026, 8, 23, 2, 0, 0, tzinfo=dt.timezone.utc)
MAIN_REPO = "acme-web"
MAIN_TASK = "2026-08-20-audit-ready-deployments"


class DemoClock:
    """A thread-safe clock used for stable ids and readable history."""

    def __init__(self, now: dt.datetime = DEMO_NOW):
        self._now = now
        self._lock = threading.Lock()

    def __call__(self) -> dt.datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: int) -> dt.datetime:
        with self._lock:
            self._now += dt.timedelta(seconds=seconds)
            return self._now


class DemoProcess:
    """The ProcessHandle slice consumed by RunManager."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def _effective_config_payload(
    task_id: str, control_dir: Path, *, plan_gate: bool
) -> dict:
    return {
        "schema_version": 3,
        "config_revision": "demo-config-91d7e0",
        "effective_revision": "demo-effective-42b6c1",
        "profile": "default",
        "profiles": {"dev": "excellent", "review": "standard"},
        "split_profiles": True,
        "available_profiles": ["standard", "excellent"],
        "backend": "cc-codex",
        "dev": {
            "agent": "claude",
            "model": "claude-opus-5",
            "effort": "max",
        },
        "review": {"agent": "codex", "model": "gpt-5.5", "effort": "xhigh"},
        "max_sessions": 12,
        "context_budget": 200000,
        "codex_sandbox": "workspace-write",
        "task_id": task_id,
        "once": False,
        "plan_gate": plan_gate,
        "control_dir": str(control_dir),
        "sources": {
            "backend": "config",
            "dev.profile": "profile:excellent",
            "review.profile": "profile:standard",
        },
    }


def _orchestrator_log_path(
    repo_path: Path, stamp: dt.datetime, task_id: str
) -> Path:
    log_dir = repo_path / ".mandrel" / "orchestrator" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{task_id}.log"


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _append_log(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))


def _replace_frontmatter_value(content: str, key: str, value: str) -> str:
    prefix = f"{key}:"
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{key}: {value}"
            return "\n".join(lines) + "\n"
    return content


def _complete_and_archive(repo_path: Path, task_id: str) -> None:
    """Close one synthetic task inside its synthetic repository."""

    tasks = repo_path / ".ai-tasks"
    active = tasks / f"{task_id}.md"
    if not active.is_file():
        return
    content = active.read_text(encoding="utf-8")
    content = _replace_frontmatter_value(content, "status", "completed")
    content = _replace_frontmatter_value(content, "session-est", "2/2")
    content = content.replace("- [ ]", "- [x]")
    content += (
        "\n### 2026-08-23 / demo-plan-71c9 / (pending → in_progress)\n"
        "- Done: approved the metadata-only implementation plan.\n"
        "- Next: implement and verify the audit trail.\n"
        "- Open: none\n\n"
        "### 2026-08-23 / demo-dev-a32f / (in_progress → final_review)\n"
        "- Done: implemented append-only records, API projection, and tests.\n"
        "- Next: independent review.\n"
        "- Open: none\n\n"
        "### 2026-08-23 / demo-review-b821 / review of demo-dev-a32f / "
        "(final_review → completed)\n"
        "- Verdict: pass\n"
        "- Group: demo-dev-a32f\n"
        "- Findings: none\n"
        "- Open: none\n"
    )
    active.write_text(content, encoding="utf-8")
    index = tasks / "index.md"
    kept = [
        line
        for line in index.read_text(encoding="utf-8").splitlines()
        if f"| {task_id} |" not in line
    ]
    index.write_text("\n".join(kept) + "\n", encoding="utf-8")
    archive = tasks / "archive"
    archive.mkdir(exist_ok=True)
    active.replace(archive / active.name)


class DemoSpawner:
    """Script a control-dir run without starting an agent or subprocess."""

    def __init__(self, clock: DemoClock):
        self.clock = clock
        self.interactive = False
        self.procs: list[DemoProcess] = []
        self._pids = itertools.count(8100)
        self._threads: list[threading.Thread] = []
        self._shutdown = threading.Event()

    def __call__(self, argv, *, cwd, stdout, env, start_new_session):
        del stdout, env, start_new_session
        proc = DemoProcess(next(self._pids))
        self.procs.append(proc)
        if self.interactive:
            thread = threading.Thread(
                target=self._run_interactive,
                args=(proc, tuple(str(arg) for arg in argv), Path(cwd)),
                name=f"visual-demo-run-{proc.pid}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        return proc

    def _pause(self, seconds: float) -> bool:
        return self._shutdown.wait(seconds)

    def _run_interactive(
        self, proc: DemoProcess, argv: tuple[str, ...], repo_path: Path
    ) -> None:
        control_index = argv.index("--control-dir")
        task_id = argv[control_index - 1]
        control_dir = Path(argv[control_index + 1])
        stamp = self.clock()
        log_path = _orchestrator_log_path(repo_path, stamp, task_id)
        plan_gate = "--plan-gate" in argv
        config = _effective_config_payload(task_id, control_dir, plan_gate=plan_gate)

        if self._pause(0.45):
            return
        _write_log(
            log_path,
            [
                "[02:18:00] effective-config: "
                + json.dumps(config, sort_keys=True, separators=(",", ":")),
                "[02:18:02] --- dev plan-gate start: sid=demo-plan-71c9 "
                "backend=claude:claude-opus-5@max status_before=pending",
                "[02:18:04] run finished: status=finished context≈42000 tokens",
            ],
        )
        banner = (
            "Goal: make every deployment approval reconstructable from run history.\n\n"
            "Plan\n"
            "1. Add an append-only approval record with actor, revision, and timestamp.\n"
            "2. Expose the record through the deployment audit API.\n"
            "3. Cover redaction, retry, and ordering behavior with deterministic tests.\n"
            "4. Update operator guidance, then run review and closeout.\n\n"
            "No production credentials or customer payloads enter the audit record."
        )
        question = {
            "seq": 1,
            "ts": self.clock().isoformat(),
            "kind": "plan-gate",
            "banner": banner,
            "message": banner,
        }
        (control_dir / "001-question.json").write_text(
            json.dumps(question, indent=1), encoding="utf-8"
        )

        answer_path = control_dir / "001-answer.json"
        while not answer_path.is_file():
            if self._pause(0.10):
                return

        self.clock.advance(90)
        _append_log(
            log_path,
            [
                "[02:19:30] --- dev plan-gate end: sid=demo-plan-71c9",
                "[02:19:32] --- dev session start: sid=demo-dev-a32f "
                "backend=claude:claude-opus-5@max status_before=in_progress",
                "[02:20:10] Implemented append-only approval records and API projection.",
                "[02:20:48] run finished: status=finished context≈126000 tokens",
                "[02:20:50] --- dev session end: sid=demo-dev-a32f "
                "status=final_review",
            ],
        )
        if self._pause(1.15):
            return
        self.clock.advance(80)
        _append_log(
            log_path,
            [
                "[02:20:52] --- review session start: sid=(pending) "
                "backend=codex:gpt-5.5@xhigh status_before=final_review",
                "[02:21:34] Acceptance checks passed; audit payload contains metadata only.",
                "[02:21:40] run finished: tool=codex exit=0 status=finished "
                "context≈88000 tokens",
                "[02:21:42] --- review session end: sid=demo-review-b821 "
                "status=completed+archived",
            ],
        )
        if self._pause(1.15):
            return
        _complete_and_archive(repo_path, task_id)
        self.clock.advance(12)
        proc.returncode = 0

    def shutdown(self) -> None:
        self._shutdown.set()
        for thread in self._threads:
            thread.join(timeout=2)


class DemoUsageService:
    """Static display data; never reads a CLI home or launches a probe."""

    def get_usage(self) -> SubscriptionUsage:
        return SubscriptionUsage(
            providers=(
                ProviderUsage(
                    provider="claude",
                    available=True,
                    plan="Max",
                    windows=(
                        UsageWindow(
                            key="five_hour",
                            label="5H",
                            window_minutes=300,
                            used_percent=28,
                            remaining_percent=72,
                            resets_at="2026-08-23T18:00:00+00:00",
                        ),
                        UsageWindow(
                            key="seven_day",
                            label="7D",
                            window_minutes=10080,
                            used_percent=41,
                            remaining_percent=59,
                            resets_at="2026-08-29T00:00:00+00:00",
                        ),
                    ),
                    observed_ts="2026-08-23T13:45:00+00:00",
                ),
                ProviderUsage(
                    provider="codex",
                    available=True,
                    plan="Pro",
                    windows=(
                        UsageWindow(
                            key="primary",
                            label="5H",
                            window_minutes=300,
                            used_percent=17,
                            remaining_percent=83,
                            resets_at="2026-08-23T18:30:00+00:00",
                        ),
                        UsageWindow(
                            key="secondary",
                            label="7D",
                            window_minutes=10080,
                            used_percent=36,
                            remaining_percent=64,
                            resets_at="2026-08-28T00:00:00+00:00",
                        ),
                    ),
                    credits=UsageCredits(
                        has_credits=True, unlimited=False, balance="4"
                    ),
                    observed_ts="2026-08-23T13:45:00+00:00",
                ),
            )
        )

    def claude_observer(self) -> Callable[[str], None]:
        return lambda _text: None

    def request_claude_refresh(self) -> None:
        return None


class DemoIntakeAgent:
    """A deterministic intake agent that writes only on confirmation."""

    @staticmethod
    def _draft(iteration: int) -> IntakeDraft:
        task_id = "2026-08-23-add-deployment-audit-summary"
        content = _task_markdown(
            task_id,
            "Add a deployment audit summary",
            "pending",
            "Give operators a one-screen summary of approval actors, revisions, "
            "and outcomes.",
        )
        if iteration:
            content += (
                "\nRevision note: limit the first slice to metadata-only summaries.\n"
            )
        return IntakeDraft(
            task_id=task_id,
            summary="Add a metadata-only deployment audit summary.",
            task_file=content,
            index_entry=(
                f"| {task_id} | pending | "
                "Add a metadata-only deployment audit summary. |"
            ),
            notes="The draft is synthetic and remains read-only until confirmation.",
        )

    def draft(self, request, repo) -> AgentDraftResult:
        del repo
        return AgentDraftResult(
            handle={"request": request, "iteration": 0},
            ref="demo-intake",
            draft=self._draft(0),
        )

    def revise(self, handle, feedback, repo) -> AgentDraftResult:
        del feedback, repo
        handle["iteration"] += 1
        return AgentDraftResult(
            handle=handle,
            ref="demo-intake",
            draft=self._draft(handle["iteration"]),
        )

    def confirm(self, handle, repo) -> AgentConfirmResult:
        draft = self._draft(handle["iteration"])
        path = repo.path / ".ai-tasks" / f"{draft.task_id}.md"
        path.write_text(draft.task_file, encoding="utf-8")
        index = repo.path / ".ai-tasks" / "index.md"
        with index.open("a", encoding="utf-8") as output:
            output.write(draft.index_entry + "\n")
        return AgentConfirmResult(task_id=draft.task_id, task_path=path)

    def cancel(self, handle) -> None:
        del handle


class DemoSemanticBackend:
    """Return a rich schema-valid report without calling a model."""

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return SemanticEvaluatorInfo(
            backend="demo-fixture",
            model="deterministic-demo-evaluator",
            schema_version=SEMANTIC_SCHEMA_VERSION,
        )

    def doctor(self) -> SemanticDoctor:
        return SemanticDoctor(
            backend=self.info.backend,
            model=self.info.model,
            schema_version=self.info.schema_version,
            available=True,
            detail="isolated deterministic demo backend",
        )

    def evaluate(self, prompt: str, repo: ManagedRepo) -> str:
        del prompt, repo
        payload = {
            "summary": (
                "The task produced a reviewable implementation trail with explicit "
                "acceptance evidence and a complete handoff."
            ),
            "confidence": 0.94,
            "scores": [
                {
                    "dimension": "handoff_completeness",
                    "score": 0.95,
                    "confidence": 0.93,
                    "rationale": (
                        "The task log records implementation, verification, review, "
                        "and closeout outcomes."
                    ),
                },
                {
                    "dimension": "acceptance_coverage",
                    "score": 0.92,
                    "confidence": 0.91,
                    "rationale": (
                        "Each acceptance item has a corresponding test or review "
                        "statement in the run evidence."
                    ),
                },
                {
                    "dimension": "decision_trace",
                    "score": 0.88,
                    "confidence": 0.89,
                    "rationale": (
                        "The plan gate captures intended scope before implementation "
                        "and the reviewer records the final disposition."
                    ),
                },
                {
                    "dimension": "rediscovery_risk",
                    "score": 0.86,
                    "confidence": 0.86,
                    "rationale": (
                        "Run configuration, session context, and task closeout remain "
                        "available as durable evidence."
                    ),
                },
            ],
            "findings": [
                {
                    "dimension": "knowledge_signal_density",
                    "severity": "low",
                    "summary": "The implementation trace is concise and evidence-linked.",
                    "detail": (
                        "The audit trail names decision points without copying "
                        "customer content."
                    ),
                    "confidence": 0.9,
                    "evidence_refs": [
                        "run history",
                        "task session log",
                        "plan-gate answer audit",
                    ],
                    "suggestion": (
                        "Keep future audit additions metadata-only and attach "
                        "explicit acceptance references."
                    ),
                }
            ],
            "improvements": [
                "Add a stable policy revision field when deployment policies "
                "become independently versioned."
            ],
        }
        return f"{SEMANTIC_START}\n{json.dumps(payload)}\n{SEMANTIC_END}"


def _task_markdown(task_id: str, title: str, status: str, goal: str) -> str:
    return f"""---
id: {task_id}
status: {status}
session-est: 0/2
blockers: []
prefetch: [.ai/architecture/audit.md, .ai/modules/run-control.md]
---

# {title}

## Goal

{goal}

## Scope

- Persist an append-only, metadata-only approval trail.
- Expose it through the existing authenticated audit surface.
- Preserve ordering, redaction, and retry invariants.

## Acceptance

- [ ] Operators can reconstruct actor, revision, decision, and timestamp.
- [ ] No credential, prompt, or customer payload is copied into the trail.
- [ ] Deterministic tests cover ordering, retry, and redaction behavior.
- [ ] Review and closeout leave a durable session log.

## Session plan

### session-1

Scope:
- Implement and verify the bounded change, then request independent review.

## Session log

"""


def _write_demo_deploy_receipts(repo: ManagedRepo) -> None:
    """Create one internally coherent synthetic protocol deployment receipt."""

    relative = ".ai-protocol/protocols/conduct.md"
    content = b"# Conduct\n\nDemo protocol payload.\n"
    target = repo.path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    commit = "6b62754694c289fc8822562cec22ab774e071b2c"
    payload = "533a87d311bd99d9ae9ba25f1385a3c22396b4e704a8b75a2ad9b9b36614f450"
    lock = {
        "canonical_payload_sha256": payload,
        "deployed_files": [
            {
                "canonical_relative_path": "canonical/protocols/conduct.md",
                "canonical_sha256": digest,
                "canonical_size_bytes": len(content),
                "target_relative_path": relative,
            }
        ],
        "schema_version": 1,
        "source_git_commit": commit,
        "source_repo": "mandrel-demo",
    }
    (repo.path / ".ai-deploy-lock.json").write_text(
        json.dumps(lock, indent=1), encoding="utf-8"
    )
    manifest = {
        "deployed_at": "2026-08-23T01:30:00+00:00",
        "files": {
            relative: {
                "canonical_relative_path": "canonical/protocols/conduct.md",
                "mode": 420,
                "sha256": digest,
                "size_bytes": len(content),
                "target_relative_path": relative,
            }
        },
        "schema_version": 1,
        "source_git_commit": commit,
        "source_repo_path": "/synthetic/mandrel",
        "target_repo_path": str(repo.path),
    }
    repo.manifest.write_text(json.dumps(manifest, indent=1), encoding="utf-8")


ACTIVE_TASKS = {
    "acme-web": (
        (
            MAIN_TASK,
            "Make deployment approvals auditable end to end.",
            "pending",
            "Make every deployment approval reconstructable from run history "
            "without retaining customer payloads.",
        ),
        (
            "2026-08-21-stream-run-events",
            "Stream run-state changes to internal operator consoles.",
            "in_progress",
            "Expose a resumable stream of run lifecycle events to internal consoles.",
        ),
        (
            "2026-08-22-release-health-summary",
            "Add a compact release health summary.",
            "final_review",
            "Summarize release checks and reviewer disposition in one view.",
        ),
    ),
    "payments-api": (
        (
            "2026-08-19-idempotent-retries",
            "Harden idempotent retry handling for settlement jobs.",
            "pending",
            "Make settlement retries safe across worker restarts.",
        ),
        (
            "2026-08-20-redact-audit-payloads",
            "Redact sensitive fields from audit payloads.",
            "blocked",
            "Apply the approved data classification policy to audit events.",
        ),
    ),
    "infra-config": (
        (
            "2026-08-18-detect-policy-drift",
            "Detect deployment policy drift before rollout.",
            "pending",
            "Refuse a rollout when its policy snapshot no longer matches approval.",
        ),
        (
            "2026-08-22-canary-promotion-gate",
            "Add a canary promotion gate.",
            "pending",
            "Require explicit evidence before promoting a canary globally.",
        ),
    ),
}


ARCHIVED_DEMOS = (
    (
        "infra-config",
        "2026-08-15-deployment-policy-drift",
        "Record deployment policy drift decisions.",
        112000,
        71000,
    ),
    (
        "payments-api",
        "2026-08-16-redact-provider-secrets",
        "Redact provider secrets from operator-visible diagnostics.",
        148000,
        93000,
    ),
    (
        "acme-web",
        "2026-08-17-export-run-audit",
        "Export a bounded run audit bundle for compliance review.",
        176000,
        104000,
    ),
)


def _write_active_inventory(repo: ManagedRepo) -> None:
    tasks = repo.path / ".ai-tasks"
    (tasks / "archive").mkdir(parents=True)
    rows = []
    for task_id, summary, status, goal in ACTIVE_TASKS[repo.name]:
        rows.append(f"| {task_id} | {status} | {summary} |")
        content = _task_markdown(task_id, summary.rstrip("."), status, goal)
        if status == "blocked":
            content = content.replace(
                "blockers: []", "blockers: [external:security-policy-approval]"
            )
        (tasks / f"{task_id}.md").write_text(content, encoding="utf-8")
    (tasks / "index.md").write_text(
        "# Active tasks\n\n| Task | Status | Summary |\n|---|---|---|\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )


def _append_active_task(repo: ManagedRepo, task_id: str, summary: str) -> None:
    path = repo.path / ".ai-tasks" / f"{task_id}.md"
    path.write_text(
        _task_markdown(task_id, summary.rstrip("."), "pending", summary),
        encoding="utf-8",
    )
    with (repo.path / ".ai-tasks" / "index.md").open(
        "a", encoding="utf-8"
    ) as output:
        output.write(f"| {task_id} | pending | {summary} |\n")


def _ready(_repo: ManagedRepo, _config: Config) -> ReadinessReport:
    return ReadinessReport(
        (
            ReadinessCheck("git_clean", True, "synthetic working tree is clean"),
            ReadinessCheck("protocol", True, "demo protocol payload is current"),
            ReadinessCheck(
                "orchestrator", True, "deterministic demo runner is available"
            ),
        )
    )


def _commit_series(repo_name: str) -> tuple[CommitInfo, ...]:
    subjects = {
        "acme-web": (
            "Add approval audit projection",
            "Cover metadata redaction boundaries",
            "Document release review evidence",
        ),
        "payments-api": (
            "Harden idempotent settlement retries",
            "Redact provider diagnostics",
            "Add worker restart regression cases",
        ),
        "infra-config": (
            "Detect policy revision drift",
            "Record canary promotion evidence",
            "Document rollback decision path",
        ),
    }[repo_name]
    return tuple(
        CommitInfo(
            hash=f"{index + 1:040x}",
            short_hash=f"{index + 1:07x}",
            committed_ts=f"2026-08-{22 - index:02d}T10:2{index}:00+08:00",
            subject=subject,
        )
        for index, subject in enumerate(subjects)
    )


def _preview_config(
    repo: ManagedRepo,
    *,
    backend: str | None = None,
    profile: str | None = None,
    dev_profile: str | None = None,
    review_profile: str | None = None,
) -> EffectiveConfig:
    del repo
    selected_backend = backend or "cc-codex"
    dev_selected = dev_profile or profile or "standard"
    review_selected = review_profile or profile or "standard"
    dev = (
        RoleConfig(agent="claude", model="claude-opus-5", effort="max")
        if dev_selected == "excellent"
        else RoleConfig(agent="claude", model="claude-sonnet-4", effort="high")
    )
    review = (
        RoleConfig(agent="codex", model="gpt-5.5", effort="xhigh")
        if review_selected == "excellent"
        else RoleConfig(agent="codex", model="gpt-5.4", effort="high")
    )
    if selected_backend == "cursor":
        dev = RoleConfig(agent=None, model="cursor-auto", effort="high")
        review = RoleConfig(agent=None, model="cursor-auto", effort="high")
    return EffectiveConfig(
        schema_version=3,
        config_revision="demo-config-91d7e0",
        effective_revision=(
            f"demo-{selected_backend}-{dev_selected}-{review_selected}"
        ),
        profile=profile or "default",
        available_profiles=("standard", "excellent"),
        dev_profile=dev_selected,
        review_profile=review_selected,
        split_profiles=True,
        backend=selected_backend,
        dev=dev,
        review=review,
        max_sessions=12,
        context_budget=200000,
        codex_sandbox=(
            "workspace-write" if selected_backend == "cc-codex" else None
        ),
        sources={
            "backend": "config",
            "dev.profile": f"profile:{dev_selected}",
            "review.profile": f"profile:{review_selected}",
        },
    )


def _fake_git(args, **kwargs) -> CommandResult:
    del kwargs
    argv = tuple(str(arg) for arg in args)
    if "--patch" in argv:
        patch = (
            "commit 5ac91d2 2026-08-22T10:20:00+08:00 Record audit decision\n"
            "diff --git a/.ai/architecture/audit.md "
            "b/.ai/architecture/audit.md\n"
            "+Approval records are append-only and metadata-only.\n"
        )
        return CommandResult(argv, 0, patch, "")
    sep = "\x1f"
    rows = [
        sep.join(
            (
                "5ac91d25ac91d25ac91d25ac91d25ac91d25ac9",
                "5ac91d2",
                "2026-08-22T10:20:00+08:00",
                "Record audit decision",
            )
        ),
        sep.join(
            (
                "3be62f13be62f13be62f13be62f13be62f13be6",
                "3be62f1",
                "2026-08-22T09:40:00+08:00",
                "Cover redaction boundary",
            )
        ),
    ]
    return CommandResult(argv, 0, "\n".join(rows), "")


def _copy_demo_shell(root: Path) -> Path:
    source = Path(api.WEB_DIR)
    target = root / "web"
    shutil.copytree(source, target)
    index = target / "index.html"
    html = index.read_text(encoding="utf-8")
    html = html.replace(
        '<span class="app-title">orch-hub</span>',
        '<span class="app-title">orch-hub</span>'
        '<span class="demo-badge">isolated demo</span>',
    )
    index.write_text(html, encoding="utf-8")
    with (target / "styles.css").open("a", encoding="utf-8") as styles:
        styles.write(
            "\n/* Injected only by tools/visual_demo; never shipped in the app. */\n"
            ".demo-badge { margin-left: 10px; padding: 3px 7px; "
            "border: 1px solid var(--accent); border-radius: 999px; "
            "color: var(--accent); font: 650 9px/1 var(--mono); "
            "letter-spacing: .12em; text-transform: uppercase; "
            "white-space: nowrap; }\n"
            "@media (max-width: 380px) { .demo-badge { display: none; } }\n"
        )
    app_js = target / "app.js"
    js = app_js.read_text(encoding="utf-8").replace(
        "const OVERVIEW_REFRESH_MS = 15000;",
        "const OVERVIEW_REFRESH_MS = 1200;",
    )
    app_js.write_text(js, encoding="utf-8")
    return target


@dataclass
class DemoRuntime:
    app: object
    root: Path
    config: Config
    repos: tuple[ManagedRepo, ...]
    manager: RunManager
    spawner: DemoSpawner
    _tmp: tempfile.TemporaryDirectory[str]
    _originals: dict[str, object]

    def close(self) -> None:
        self.spawner.shutdown()
        for name, value in self._originals.items():
            setattr(api, name, value)
        resolved = self.root.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if not resolved.is_relative_to(temp_root) or not resolved.name.startswith(
            DEMO_PREFIX
        ):
            raise RuntimeError(f"refusing to clean non-demo path: {resolved}")
        self._tmp.cleanup()


def build_demo_runtime() -> DemoRuntime:
    """Build one throwaway app instance and seed its synthetic audit history."""

    tmp = tempfile.TemporaryDirectory(prefix=DEMO_PREFIX)
    root = Path(tmp.name)
    temp_root = Path(tempfile.gettempdir()).resolve()
    if not root.resolve().is_relative_to(temp_root) or not root.name.startswith(
        DEMO_PREFIX
    ):
        tmp.cleanup()
        raise RuntimeError(f"temporary runtime escaped the demo boundary: {root}")

    workspace = root / "workspace"
    repos = tuple(
        ManagedRepo(
            name=name,
            path=workspace / name,
            manifest=workspace / name / ".ai-deploy-manifest.json",
        )
        for name in ACTIVE_TASKS
    )
    for repo in repos:
        repo.path.mkdir(parents=True)
        (repo.path / ".ai" / "architecture").mkdir(parents=True)
        (repo.path / ".ai" / "modules").mkdir()
        (repo.path / ".ai" / "architecture" / "audit.md").write_text(
            "# Audit architecture\n\n"
            "Approval records are append-only and metadata-only.\n",
            encoding="utf-8",
        )
        (repo.path / ".ai" / "modules" / "run-control.md").write_text(
            "# Run control\n\n"
            "Human gates are represented by the control-dir protocol.\n",
            encoding="utf-8",
        )
        _write_demo_deploy_receipts(repo)
        _write_active_inventory(repo)

    (root / "mandrel").mkdir()
    (root / "codex-home").mkdir()
    (root / "claude-home").mkdir()
    config = Config(
        mandrel_path=root / "mandrel",
        token=DEMO_TOKEN,
        host="127.0.0.1",
        data_dir=root / "hub-data",
        codex_home=root / "codex-home",
        claude_home=root / "claude-home",
        bark_url=None,
        evaluation_enabled=True,
        eval_model="deterministic-demo-evaluator",
        eval_effort="none",
    )
    clock = DemoClock()
    spawner = DemoSpawner(clock)
    manager = RunManager(
        config,
        readiness_checker=_ready,
        spawner=spawner,
        pid_probe=lambda _pid: False,
        clock=clock,
        notifier=None,
        usage_refresh=lambda: None,
    )
    intake = IntakeManager(
        config,
        agent=DemoIntakeAgent(),
        readiness_checker=_ready,
        clock=clock,
        executor=lambda run: run(),
        notifier=None,
    )
    catalog = ReportCatalog(config, clock=clock)
    evaluation = EvaluationService(
        config,
        git_runner=_fake_git,
        clock=clock,
        retention_guard=catalog.unpublished_ids,
    )
    static = StaticEvaluator(config, evaluation_service=evaluation, clock=clock)
    semantic = SemanticEvaluator(
        config,
        evaluation_service=evaluation,
        static_evaluator=static,
        backend=DemoSemanticBackend(),
        catalog=catalog,
        clock=clock,
    )
    semantic_jobs = SemanticJobManager(
        config,
        evaluator=semantic,
        clock=clock,
        executor=lambda run: run(),
        notifier=None,
    )

    repo_by_name = {repo.name: repo for repo in repos}
    for (
        repo_name,
        task_id,
        summary,
        dev_context,
        review_context,
    ) in ARCHIVED_DEMOS:
        repo = repo_by_name[repo_name]
        _append_active_task(repo, task_id, summary)
        record = manager.start_run(repo, task_id, extra_args=("--plan-gate",))
        log = _orchestrator_log_path(repo.path, clock(), task_id)
        config_line = _effective_config_payload(
            task_id, record.control_dir, plan_gate=True
        )
        _write_log(
            log,
            [
                "[02:00:00] effective-config: "
                + json.dumps(config_line, sort_keys=True, separators=(",", ":")),
                f"[02:00:03] --- dev plan-gate start: sid=seed-plan-{repo_name} "
                "backend=claude:claude-opus-5@max status_before=pending",
                "[02:01:00] run finished: status=finished context≈38000 tokens",
                f"[02:01:02] --- dev plan-gate end: sid=seed-plan-{repo_name}",
                f"[02:01:04] --- dev session start: sid=seed-dev-{repo_name} "
                "backend=claude:claude-opus-5@max status_before=in_progress",
                f"[02:08:00] run finished: status=finished "
                f"context≈{dev_context} tokens",
                f"[02:08:02] --- dev session end: sid=seed-dev-{repo_name} "
                "status=final_review",
                "[02:08:05] --- review session start: sid=(pending) "
                "backend=codex:gpt-5.5@xhigh status_before=final_review",
                f"[02:11:00] run finished: tool=codex exit=0 status=finished "
                f"context≈{review_context} tokens",
                f"[02:11:02] --- review session end: sid=seed-review-{repo_name} "
                "status=completed+archived",
            ],
        )
        _complete_and_archive(repo.path, task_id)
        spawner.procs[-1].returncode = 0
        clock.advance(662)
        manager.list_runs()
        clock.advance(20)
        semantic.evaluate(repo, task_id)
        clock.advance(300)

    commits = {repo.name: _commit_series(repo.name) for repo in repos}
    demo_shell = _copy_demo_shell(root)
    originals = {
        "WEB_DIR": api.WEB_DIR,
        "discover_repos": api.discover_repos,
        "check_readiness": api.check_readiness,
        "latest_commit": api.latest_commit,
        "housekeeping_pending": api.housekeeping_pending,
        "read_commits": api.read_commits,
        "query_effective_config": api.query_effective_config,
    }
    api.WEB_DIR = demo_shell
    api.discover_repos = lambda _config: list(repos)
    api.check_readiness = _ready
    api.latest_commit = lambda repo: commits[repo.name][0]
    api.housekeeping_pending = lambda _repo: False
    api.read_commits = lambda repo, limit=10: CommitLog(
        True, commits[repo.name][:limit]
    )
    api.query_effective_config = _preview_config

    app = api.create_app(
        config,
        run_manager=manager,
        intake_manager=intake,
        evaluation_service=evaluation,
        static_evaluator=static,
        semantic_evaluator=semantic,
        semantic_job_manager=semantic_jobs,
        report_catalog=catalog,
        usage_service=DemoUsageService(),
    )
    app.state.visual_demo = {
        "isolated": True,
        "runtime_root": str(root),
        "main_repo": MAIN_REPO,
        "main_task": MAIN_TASK,
    }
    spawner.interactive = True
    return DemoRuntime(app, root, config, repos, manager, spawner, tmp, originals)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("visual demo refuses non-loopback hosts")
    runtime = build_demo_runtime()
    print(f"isolated demo root: {runtime.root}", flush=True)
    print(f"open: http://127.0.0.1:{args.port}/", flush=True)
    print(f"token: {DEMO_TOKEN}", flush=True)

    def _stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    try:
        uvicorn.run(runtime.app, host=args.host, port=args.port, log_level="warning")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
