"""Concrete CLI-backed intake agent: a headless ``/intake-task`` driver.

This is the real :class:`~orch_hub.intake.IntakeAgent` backend the intake manager
drives once a caller (the dashboard) exists to exercise it. It mirrors the
deployed orchestrator's Claude Code session (ai-native-development
``orchestrator.py`` ``ClaudeSession``): each conversational turn is one
``claude -p --output-format stream-json`` process, the caller picks the session
id (``--session-id``) on the first turn and ``--resume``\\s it afterward, and the
JSONL stream is parsed for the assistant's final text.

``/intake-task`` gives us neither a machine-parseable result nor a read-only
mode, so this backend imposes both as NEW contracts (no in-repo precedent — see
the task's session-3-cont notes):

1. **Draft output envelope.** ``/intake-task`` is purely conversational, so the
   draft/revise prompts instruct the agent to emit the complete proposal as ONE
   JSON object between the sentinels :data:`DRAFT_START` / :data:`DRAFT_END`,
   which :func:`_parse_draft` extracts into an
   :class:`~orch_hub.models.IntakeDraft`. The confirm turn likewise reports what
   it wrote between :data:`CONFIRM_START` / :data:`CONFIRM_END`.

2. **Read-only-until-confirm gate.** The orchestrator runs fully permissive
   (``--dangerously-skip-permissions``). Intake instead runs the draft/revise
   turns under ``--permission-mode plan`` (a read-only exploration sandbox: the
   agent may read/inspect but cannot write), and only the confirm turn under
   ``--dangerously-skip-permissions`` so it can write the task file and index
   row. This is defense-in-depth: :class:`~orch_hub.intake.IntakeManager` already
   guarantees read-only *structurally* (only ``confirm_intake`` reaches the write
   path), so even a gate leak could not write before confirmation.

The subprocess itself is an injected seam (:data:`AgentRunner`), exactly like the
run manager's ``spawner``: tests drive a fake runner returning canned
``stream-json`` (and, on the write turn, writing the fixture files as real claude
would), so every mechanic — argv shapes, session threading, envelope parsing, the
read-only gate, and each failure path — is unit-tested without launching
``claude``. The thin real runner (:func:`_default_runner`) stays untested, like
runs' ``_default_spawner``.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .intake import AgentConfirmResult, AgentDraftResult
from .models import IntakeDraft, ManagedRepo
from .runs import _orchestrator_env

# Sentinels delimiting the imposed draft / confirmation JSON envelopes. Distinct
# opaque markers (not ```` ```json ````) so a task_file value that itself
# contains Markdown code fences cannot confuse extraction.
DRAFT_START = "<<<INTAKE_DRAFT_JSON>>>"
DRAFT_END = "<<<END_INTAKE_DRAFT_JSON>>>"
CONFIRM_START = "<<<INTAKE_CONFIRMED_JSON>>>"
CONFIRM_END = "<<<END_INTAKE_CONFIRMED_JSON>>>"

DEFAULT_INTAKE_MODEL = "claude-opus-4-8"
"""Claude model for intake turns; mirrors the orchestrator's cc dev model."""

DEFAULT_INTAKE_EFFORT = "high"
"""Effort for intake turns. Drafting a task spec is lighter than a full dev
session, so this defaults below the orchestrator's ``max``; injectable."""


class IntakeAgentError(Exception):
    """A concrete intake turn failed (spawn/exit/stream error or bad envelope).

    Raised into :class:`~orch_hub.intake.IntakeManager`, which catches any
    agent-turn exception and records the intake as ``failed`` with this message
    as the audited reason — never a partial or silent repo write.
    """


@dataclass(frozen=True)
class AgentRunResult:
    """One completed CLI turn: captured stdout/stderr and the exit code."""

    stdout: str
    stderr: str
    returncode: int


AgentRunner = Callable[[Sequence[str], Path], AgentRunResult]
"""Runs one agent-CLI turn to completion in ``cwd`` and returns its captured
output. Tests inject a fake; :func:`_default_runner` is the real one. Mirrors
runs' ``Spawner`` seam, but blocking and output-capturing — the intake turn's
``stream-json`` must be parsed, not detached."""


def _default_runner(argv: Sequence[str], cwd: Path) -> AgentRunResult:
    """Run ``claude`` to completion, capturing stdout/stderr (the real runner).

    Uses the same augmented PATH as detached orchestrator runs
    (:func:`orch_hub.runs._orchestrator_env`): the hub may run under launchd with
    a sparse PATH, while ``claude`` is commonly installed under the user's home,
    and this backend launches it by name. ``capture_output`` reads both pipes, so
    a large ``stream-json`` stdout cannot deadlock against stderr.
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
    return AgentRunResult(stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)


def _new_uuid() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class _ClaudeIntakeHandle:
    """The opaque conversational handle the manager keeps between turns.

    ``sid`` is the Claude session id (chosen on the first turn, ``--resume``\\d
    after). ``last_draft`` is the most recent proposal, so ``confirm`` can fall
    back to its id/path if the confirmation envelope is absent.
    """

    sid: str
    last_draft: IntakeDraft | None = None


class ClaudeIntakeAgent:
    """Drives ``/intake-task`` through headless ``claude -p`` turns.

    Implements the :class:`~orch_hub.intake.IntakeAgent` protocol. ``draft`` opens
    a new session and returns the parsed proposal; ``revise`` resumes it with
    feedback; ``confirm`` resumes it once more with write permission to perform
    the protocol write; ``cancel`` is a no-op (each turn is its own stateless CLI
    process — there is no live session to release). ``runner`` and
    ``new_session_id`` are injected for tests.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_INTAKE_MODEL,
        effort: str = DEFAULT_INTAKE_EFFORT,
        runner: AgentRunner = _default_runner,
        new_session_id: Callable[[], str] = _new_uuid,
        usage_observer: Callable[[str], None] | None = None,
    ):
        self.model = model
        self.effort = effort
        self._runner = runner
        self._new_session_id = new_session_id
        # Best-effort agent-subscription-usage seam (:data:`orch_hub.usage.ClaudeUsageObserver`).
        # The hub already drives these ``claude -p`` turns, so their stream-json is the
        # only place Claude's 5h/7d subscription usage can be seen without a probe turn.
        # When wired (by :func:`orch_hub.api.create_app`), each turn's stdout is fed to
        # the observer; ``None`` keeps intake fully decoupled from usage. Typed as a
        # plain callable so this module never imports usage internals.
        self._usage_observer = usage_observer

    def draft(self, request: str, repo: ManagedRepo) -> AgentDraftResult:
        sid = self._new_session_id()
        text = self._turn(
            _draft_prompt(request), sid=sid, resume=False, write=False, cwd=repo.path
        )
        draft = _parse_draft(text)
        return AgentDraftResult(
            handle=_ClaudeIntakeHandle(sid, draft), ref=sid, draft=draft
        )

    def revise(
        self, handle: object, feedback: str, repo: ManagedRepo
    ) -> AgentDraftResult:
        h = _as_handle(handle)
        text = self._turn(
            _revise_prompt(feedback), sid=h.sid, resume=True, write=False, cwd=repo.path
        )
        draft = _parse_draft(text)
        return AgentDraftResult(
            handle=_ClaudeIntakeHandle(h.sid, draft), ref=h.sid, draft=draft
        )

    def confirm(self, handle: object, repo: ManagedRepo) -> AgentConfirmResult:
        h = _as_handle(handle)
        text = self._turn(
            _confirm_prompt(), sid=h.sid, resume=True, write=True, cwd=repo.path
        )
        return _parse_confirm(text, fallback=h.last_draft, repo=repo)

    def cancel(self, handle: object) -> None:
        return None

    # -- internals ------------------------------------------------------------

    def _turn(
        self, prompt: str, *, sid: str, resume: bool, write: bool, cwd: Path
    ) -> str:
        """Run one CLI turn and return the assistant's final text.

        Raises :class:`IntakeAgentError` on a nonzero exit or a stream-level
        error, so the manager records the intake ``failed`` rather than acting on
        half-formed output.
        """
        argv = self._argv(prompt, sid=sid, resume=resume, write=write)
        result = self._runner(argv, cwd)
        self._observe_usage(result.stdout)
        text, errored = _extract_assistant_text(result.stdout)
        if result.returncode != 0 or errored:
            tail = (result.stderr or "").strip()
            suffix = f": {tail[-500:]}" if tail else ""
            raise IntakeAgentError(
                f"claude intake turn failed (exit {result.returncode},"
                f" stream_error={errored}){suffix}"
            )
        return text

    def _argv(self, prompt: str, *, sid: str, resume: bool, write: bool) -> list[str]:
        """Build the ``claude`` argv for one turn.

        Mirrors the orchestrator's ``ClaudeSession._argv`` (``claude -p
        --output-format stream-json --verbose --model … --effort …``), but swaps
        the always-permissive flag for the read-only gate: ``--permission-mode
        plan`` on draft/revise, ``--dangerously-skip-permissions`` only on the
        write turn. First turn picks the session id (``--session-id``); later
        turns ``--resume`` it.
        """
        argv = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
            "--effort",
            self.effort,
        ]
        if write:
            argv.append("--dangerously-skip-permissions")
        else:
            argv += ["--permission-mode", "plan"]
        if resume:
            argv += ["--resume", sid]
        else:
            argv += ["--session-id", sid]
        argv.append(prompt)
        return argv

    def _observe_usage(self, stdout: str) -> None:
        """Feed this turn's stdout to the injected usage observer, best-effort.

        Isolated from intake behavior: a missing observer, a parse failure, or a
        record failure can never affect the intake turn (the observer is itself
        best-effort, and this wraps the call defensively regardless of what callable
        was injected). Display-only agent-subscription-usage bookkeeping.
        """
        if self._usage_observer is None:
            return
        try:
            self._usage_observer(stdout)
        except Exception:  # noqa: BLE001 — usage bookkeeping must never break intake
            pass


def _as_handle(handle: object) -> _ClaudeIntakeHandle:
    if not isinstance(handle, _ClaudeIntakeHandle):
        raise IntakeAgentError(
            f"unexpected intake handle: {type(handle).__name__}"
        )
    return handle


# -- prompts (the imposed draft-output contract) ------------------------------


def _draft_prompt(request: str) -> str:
    return f"""You are running the /intake-task intake flow for THIS repository, \
on behalf of a remote operator who cannot see this session. Follow the project's \
intake-task skill and ai-coding-tasks-v2.md (frontmatter, task body, tasks index) \
to design exactly ONE new pending task from the request below.

REQUEST:
{request}

This is a READ-ONLY DRAFTING turn. Do NOT create, edit, or delete any file — do \
NOT write .ai-tasks/<id>.md and do NOT touch .ai-tasks/index.md. You are only \
proposing; a human reviews and explicitly confirms before anything is written. \
You may read and inspect the repo freely to design a good task.

Apply the full intake-task procedure while drafting: scan .ai-tasks/index.md for \
duplicates/overlap (note it rather than refusing), estimate session-est, suggest \
prefetch docs, and if the scope is too large, decompose it into a Session plan \
and say so in notes.

When the proposal is complete, output it as EXACTLY ONE JSON object between the \
two markers below — nothing but the object between them, each marker alone on its \
own line:

{DRAFT_START}
{{
  "task_id": "<YYYY-MM-DD>-<kebab-slug>",
  "summary": "<one line for the tasks index / dashboard>",
  "task_file": "<the COMPLETE .ai-tasks/<task_id>.md content: frontmatter + body>",
  "index_entry": "<the single row to append to .ai-tasks/index.md>",
  "notes": "<duplicate/overlap, oversized-scope, or clarification notes, or null>"
}}
{DRAFT_END}

The JSON must be valid: encode newlines inside task_file/index_entry as \\n. \
task_file must be the whole file a human would review, not a summary."""


def _revise_prompt(feedback: str) -> str:
    return f"""The operator reviewed your proposed task and REQUESTED CHANGES:

{feedback}

Revise the COMPLETE proposed task accordingly. This is still a READ-ONLY turn — \
do NOT write or modify any file. Re-output the full proposal as ONE JSON object \
between the SAME markers, in the SAME shape (task_id, summary, task_file, \
index_entry, notes):

{DRAFT_START}
{{ ... updated proposal ... }}
{DRAFT_END}"""


def _confirm_prompt() -> str:
    return f"""The operator CONFIRMED the task you last proposed. Now perform the \
intake-task write step, writing EXACTLY the task you last proposed (do not \
redesign it):

1. Create .ai-tasks/<task_id>.md with the full task_file content you proposed.
2. Append your index_entry row to .ai-tasks/index.md (if the table is still the \
`(none)` placeholder, replace it with the header plus your row, per the skill).

Change nothing else. After the files are written, output ONE JSON object between \
the markers below confirming what you wrote:

{CONFIRM_START}
{{ "task_id": "<the id actually written>", "task_path": "<absolute path to the task file>" }}
{CONFIRM_END}"""


# -- stream / envelope parsing ------------------------------------------------


def _extract_assistant_text(stdout: str) -> tuple[str, bool]:
    """Concatenate assistant text from a claude ``stream-json`` stdout.

    Returns ``(text, errored)`` where ``text`` is every assistant text block
    joined (the ``result`` event's ``result`` string is a fallback when no
    assistant text streamed), and ``errored`` is set by a ``result`` event with
    ``is_error`` — the same success test the orchestrator's ``CliSession`` uses.
    Non-JSON lines are ignored, mirroring the orchestrator's tolerant reader.
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


def _extract_envelope(text: str, marker: str) -> dict:
    """Extract the one JSON object after the last ``marker`` in ``text``.

    ``rfind`` picks the last marker so an agent that mentions the sentinel in
    prose before emitting the real block still parses. ``raw_decode`` reads
    exactly one object, tolerating the closing marker and any trailing prose.
    """
    idx = text.rfind(marker)
    if idx == -1:
        raise IntakeAgentError(f"agent output has no {marker} marker")
    region = text[idx + len(marker):]
    brace = region.find("{")
    if brace == -1:
        raise IntakeAgentError(f"no JSON object after {marker}")
    try:
        obj, _end = json.JSONDecoder().raw_decode(region[brace:])
    except ValueError as err:
        raise IntakeAgentError(f"invalid JSON after {marker}: {err}") from None
    if not isinstance(obj, dict):
        raise IntakeAgentError(f"{marker} did not wrap a JSON object")
    return obj


def _require_str(obj: dict, key: str, marker: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IntakeAgentError(f"{marker} envelope is missing a non-empty {key!r}")
    return value


def _parse_draft(text: str) -> IntakeDraft:
    """Parse a draft/revise turn's envelope into an :class:`IntakeDraft`.

    ``task_id``/``task_file``/``index_entry`` are required (a proposal is
    meaningless without them); ``summary`` and ``notes`` are best-effort.
    """
    obj = _extract_envelope(text, DRAFT_START)
    task_id = _require_str(obj, "task_id", DRAFT_START).strip()
    task_file = _require_str(obj, "task_file", DRAFT_START)
    index_entry = _require_str(obj, "index_entry", DRAFT_START).strip()
    summary_raw = obj.get("summary")
    summary = summary_raw.strip() if isinstance(summary_raw, str) else ""
    notes_raw = obj.get("notes")
    notes = notes_raw.strip() if isinstance(notes_raw, str) and notes_raw.strip() else None
    return IntakeDraft(
        task_id=task_id,
        summary=summary,
        task_file=task_file,
        index_entry=index_entry,
        notes=notes,
    )


def _parse_confirm(
    text: str, *, fallback: IntakeDraft | None, repo: ManagedRepo
) -> AgentConfirmResult:
    """Parse a confirm turn's envelope; fall back to the confirmed draft.

    An absent or garbled confirmation envelope is not fatal here: the reported
    id/path fall back to the last draft's id and the conventional ``.ai-tasks/``
    path. This is only the agent's best-effort report of what it wrote — an
    exit-0 turn is not proof it actually wrote the task. The
    :class:`~orch_hub.intake.IntakeManager` independently verifies the task
    landed in the repo inventory (and rolls back a partial write) before
    recording the intake ``confirmed``, so an optimistic or wrong id is caught
    there rather than trusted.
    """
    task_id: str | None = None
    task_path: Path | None = None
    try:
        obj = _extract_envelope(text, CONFIRM_START)
    except IntakeAgentError:
        obj = {}
    reported_id = obj.get("task_id")
    if isinstance(reported_id, str) and reported_id.strip():
        task_id = reported_id.strip()
    reported_path = obj.get("task_path")
    if isinstance(reported_path, str) and reported_path.strip():
        task_path = Path(reported_path.strip())
    if task_id is None:
        if fallback is None:
            raise IntakeAgentError("confirm turn reported no task id and no draft to fall back on")
        task_id = fallback.task_id
    if task_path is None:
        task_path = repo.path / ".ai-tasks" / f"{task_id}.md"
    return AgentConfirmResult(task_id=task_id, task_path=task_path)
