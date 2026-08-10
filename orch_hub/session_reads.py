"""Bounded, read-only rollup of how a task's sessions *read* the ``.ai/`` memory.

The evidence pack already carries the **write** side of project memory
(:mod:`orch_hub.ai_snapshot`: the snapshot as it stands plus the patches the run
window produced). This module carries the **read** side, which nothing else
observes: whether each session actually loaded the lazy docs its task declared,
and — ordered — which further lazy docs it went and fetched while working.

Two distinct mechanisms, kept distinct here because they are scored differently
downstream:

- **entry prefetch completeness** — the taskfile schema's ``prefetch:`` list is
  a *pre-load at start* instruction, and the orchestrator instantiates it in the
  dev entry checklist. Loading one of three declared docs is not compliance, so
  this records the whole expected set, what was loaded, and what was missed.
- **mid-flight retrieval** — an ordered trace of the lazy ``.ai/`` docs a
  session consulted *while working*. It is deliberately **not** scored here:
  zero mid-flight reads is the correct behavior for a session whose prefetch was
  sufficient, and more reads is not better. The trace exists so a semantic judge
  can ask whether a new knowledge need arose and whether memory was consulted
  before the knowledge was re-derived from code.

The two are separated by an observable boundary rather than by declaration
membership (:data:`ENTRY_SCOPE_DIRS`): a session is assembling context until
it first *works* — reads or shells out to repo source, searches the tree, edits
a file outside its instructions, or records a decision in its own task file.
Without that partition the two mechanisms collapse into each other in both
directions: a declared doc opened only after the code was already read would
score as a completed pre-load, and a lazy doc the harness injected at entry
would read as a mid-flight retrieval that never happened. A declared doc first
delivered after the boundary is therefore recorded as a *late* recovery — not a
pre-load, and visible in the retrieval trace where it belongs.

Evidence source and boundary
----------------------------

The only observation surface is the agent CLI's own conversation transcript.
Claude Code writes one JSONL file per session at
``<claude_home>/projects/<slugged-cwd>/<session-id>.jsonl``, and the run
manager's session snapshots (:mod:`orch_hub.sessions`) already carry each
session's ``sid``, so a transcript is reachable from persisted run state with no
new orchestrator contract — the same "read what is already written" ruling that
governs the session parser.

What crosses into the pack is a **rollup**, never the transcript: paths,
booleans, counts, and an ordered event list. That is the existing
bounded-evidence rule, and the precedent is ``has_diagnostic``-style
summarization of logs the pack never ships. The transcript is also the one
place a session's *own* conversation content lives — prompts, human answers,
tool output — so shipping it would be both unbounded and a disclosure change.

Session-time truth
------------------

``prefetch:`` is mutable: the schema explicitly invites a session to backfill it
with what it actually consulted. Comparing a 2026-05 session against the list a
2026-08 session left behind would therefore invent both false misses and false
credit. The expected set is consequently taken from the *session's own* first
full read of the task file inside its transcript (the frontmatter exactly as it
stood at entry), and when that is not recoverable the session is reported
**unassessable** rather than scored against today's list.

Degradation
-----------

Every gap is a first-class recorded state, never a penalty: a session with no
transcript (a Codex-backed review session has none in this store), an
unparseable one, a backend whose events do not expose file reads, a session that
never re-read its own task file — each yields ``assessable=False`` with a
``detail``, and the downstream layers are expected to drop it from the quality
denominator and lower confidence instead.

That extends to *partial* evidence, which is the subtler case: this reader skips
a malformed or over-long row and stops at a byte budget, so a load it never saw
is indistinguishable from one that never happened. A missing doc reported
against evidence known to have holes would be a fabricated negative, so the
prefetch verdict is refused whenever the evidence it rests on was not read whole
(:func:`_entry_verdict_refusal`) — while the retrieval facts the same scan did
collect are kept, since they stand on their own. Three things can undermine that
verdict, and each is tracked positionally rather than as one blanket flag:

- a hole *inside* the entry phase, which could hide either a load or the work
  signal that would have ended the phase earlier;
- a hole anywhere after a still-unanswered entry-phase request, which could be
  the delivery that request was waiting for — phase comes from the row a read
  was *asked* for, so an entry-phase result can legitimately land late;
- a shell command this reader cannot classify, which could be the work the
  boundary should have been drawn at (:func:`_classify_command`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from .ai_snapshot import AI_DIRNAME, is_eager_doc, prefetch_doc_path
from .models import ManagedRepo
from .task_store import parse_prefetch

TRANSCRIPTS_SUBDIR = "projects"
"""Per-project transcript tree inside the Claude home (``Config.claude_home``);
:attr:`orch_hub.config.Config.claude_transcripts_dir` is the wired form."""

PREFETCH_ROLES = ("dev",)
"""Session roles the ``prefetch:`` pre-load instruction is actually delivered to.

Read off the deployed orchestrator's entry-prompt assembly, not assumed: the
prefetch checklist line is appended for ``role == "dev"`` only, while a review
session gets estimate/pending-review lines instead. A plan-gate turn composes on
top of the same dev prompt, so it carries the instruction too — hence the role,
not the kind, is the predicate.

This is what makes ``applicable`` decidable *without* a transcript: a review
session was never asked to pre-load anything, so it is not a missing measurement
that should lower confidence — it is outside the measurement.
"""

CLOSEOUT_SKILL = "ai-sync-v2"
"""The task-completion closeout skill. Reads after it are absorption (the write
path) walking ``.ai/`` to update it, not consultation, so the scan stops there —
the retired session-audit skill drew the same divider for the same reason."""

MAX_SESSIONS = 24
"""Cap on sessions rolled up per pack. A long task's runs can carry many
sessions; the rollup is per session, so the count is the unbounded dimension."""

MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
"""Byte cap on one transcript scan. Measured transcripts run ~1MB median and
~18MB at the tail, so a conforming session is read whole and a runaway one costs
a bounded read. A cut is recorded (``truncated``) and costs only the mid-flight
tail: a transcript large enough to hit this cap reached the code long before it,
so the entry-phase evidence the prefetch measurement rests on is behind the cut,
not in front of it (:func:`_entry_evidence_intact`)."""

MAX_LINE_BYTES = 1024 * 1024
"""Per-line cap inside a transcript. One JSONL row holds one message, and a
tool result can be arbitrarily large — a whole-file cap alone would still let a
single row be read into memory unbounded. An over-long row is skipped, not
parsed."""

MAX_MIDFLIGHT_EVENTS = 40
"""Cap on the ordered mid-flight trace per session. Only the *first* consult of
each path is recorded, so this bounds distinct docs, not repeat reads."""

MAX_EXPECTED_DOCS = 32
"""Cap on the declared prefetch set a session is measured against. A larger
declared set is not truncated into a coverage fraction — that would score a
session against a partial expectation — the session is reported unassessable."""

PHASE_ENTRY = "entry"
"""Delivered while the session was still assembling its starting context, i.e.
before the first observed work signal. This is the phase the ``prefetch:``
pre-load instruction is about."""

PHASE_WORK = "work"
"""Delivered after the session began working. A declared doc arriving here is a
late recovery, not a pre-load; an undeclared one is a mid-flight retrieval."""

ENTRY_SCOPE_DIRS = (AI_DIRNAME, ".ai-tasks", ".ai-protocol", ".claude")
"""Repo subtrees whose contents are the session's *instructions*, not its work.

Memory, task files, the protocol contracts, and the deployed skills are what a
session loads to know what it is doing; touching them is context assembly at any
point in the session. Everything else in the repo — source, tests, docs — is the
material the task acts on, so reaching for it is the observable start of work.

Chosen wide on purpose. The failure directions are not symmetric: a boundary
drawn too late merely credits a late pre-load, while one drawn too early invents
a missed prefetch doc for a session that read it seconds later, and the task
forbids scoring a session badly on an artifact of the measurement.
"""

ENTRY_SCOPE_NAMES = ("CLAUDE.md",)
"""Filenames that are instructions wherever they sit, matched by basename so a
subtree's own ``CLAUDE.md`` (which the harness injects when a session reads
inside that subtree) is not mistaken for work material."""

ACTOR_ROOT = "root"
"""A read performed by the session's own turn."""

ACTOR_SUBAGENT = "subagent"
"""A read performed by a delegated subagent. Consultation by delegation is still
consultation, so these count — the transcript splices them in as sidechain rows."""

METHOD_READ_TOOL = "read_tool"
"""Content reached the session through an explicit file-read tool call."""

METHOD_INJECTED = "injected"
"""Content was assembled into the context by the caller/harness, with no tool
call of the session's own. Credited identically: the operational definition of
"read" is that the content was in context, not that the session asked for it."""

SOURCE_TRANSCRIPT_TASK_READ = "transcript_task_read"
"""The expected prefetch set came from the session's own first full read of the
task file, i.e. the frontmatter exactly as it stood at that session's entry."""

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
"""Session ids reach us from persisted run state and are used as a filename, so
they are validated as a single benign segment before any path is built."""

_SLUG = re.compile(r"[^A-Za-z0-9]")
"""Claude Code's project-dir rule: every non-alphanumeric character in the
absolute cwd becomes ``-`` (``/Users/x/y.z`` -> ``-Users-x-y-z``)."""

_COMMAND_NAME = re.compile(r"<command-name>\s*/?([A-Za-z0-9._-]+)\s*</command-name>")
"""Slash-command invocations as the transcript records them; the other spelling
of a skill invocation beside a ``Skill`` tool call."""

_STATEMENTS = re.compile(r"&&|\|\||[;\n]")
"""Statement separators inside one shell command. A single ``|`` is deliberately
absent: a pipeline's tail filters the head's output rather than reaching for
anything of its own, so only the text before the first pipe names the command."""

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
"""A leading ``VAR=value`` prefix, skipped so ``PATH=... npm run build`` is read
as ``npm`` rather than as an unrecognizable head."""

_KEYWORDS = frozenset(
    {"do", "done", "then", "else", "elif", "fi", "esac", "!", "{", "}"}
)
"""Shell keywords that introduce or close a statement without being a command.

Statements are split on separators, so a loop body arrives as ``do echo ...``
and ends as a bare ``done``. Reading ``do`` as the command word would make every
loop unclassifiable — and a survey of the task files written as a loop is
exactly the entry-checklist shape this reader must not lose."""

_REDIRECT = re.compile(r"(?<![-<>\w])(?:\d+|&)?>>?\s*(?:&\d+|(?P<target>[^\s;|&<>()]+))")
"""An output redirection and the file it writes, if any. Without this a shell
append is read as just another path the command *names*, which lets a
session-log write through ``printf ... >> .ai-tasks/<id>.md`` land in the entry
scope and count as context assembly.

A stream duplication (``2>&1``) matches with no target: it writes no file, and —
since the whole match is cut out of the statement — it also stops being read as
an argument of the command it trails, which would otherwise turn ``ls .ai/
2>&1`` into a listing of something called ``2>&1``.

The lookbehind keeps prose and code out of it: ``echo "(ignored -> won't dirty
tree)"`` and an ``awk`` program's ``if(b>m)`` are not redirections, and reading
them as writes of ``won't`` or ``m)`` would make an entry-checklist echo the
moment a session started working."""

_NUMERIC_FLAG = re.compile(r"^-\d+$")
"""``git log -8``: a count, spelled as a flag. Normalized to ``-n``."""

_PLACE_IN = "in"
"""An argv token that names something inside the managed repo."""

_PLACE_OUT = "out"
"""An argv token that names something outside the repo — another checkout,
``/tmp``, a home-relative path. It says nothing about this repo's code."""

_PLACE_UNKNOWN = "unknown"
"""An argv token this reader cannot place at all: a variable it would have to
run a shell to expand. Not silently dropped — for a command whose operands are
paths, an unplaceable one is why the command's verdict must be uncertain."""

_DECISION_MARKER = re.compile(
    r"^\s*(?:#{2,3}\s|- (?:Done|Next|Open|Plan-slice|Verdict|Group|Findings"
    r"|Dispute-unresolved):)",
    re.MULTILINE,
)
"""Task-file content that records a decision rather than claiming the task.

The taskfile schema's own shapes: a section or session-entry heading, or one of
the session-log fields. A session's entry checklist writes frontmatter (claim,
estimate, status) — no headings, no field lines — so this separates the
prescribed entry bookkeeping from the plan and session-log writes that are the
closest thing a transcript has to *a decision was recorded here*."""

_READ_TOOLS = ("Read",)
"""Tool names whose successful result puts a named file's content into context.
Deliberately narrow: a shell command that happens to mention a path is not
evidence that its content was read, and crediting one would turn ``ls .ai/``
into a consultation."""

_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
"""Tools that mutate a file. Mutating anything but the session's own task file
is work by definition — the claim/session-log edits the entry checklist
prescribes are the exception, and they are counted separately as protocol
bookkeeping rather than treated as the start of work."""

_SEARCH_TOOLS = ("Grep", "Glob")
"""Tools that search the tree. A repo-wide search is the canonical *rediscover
it from the code* move this measurement exists to distinguish from consulting
memory, so it ends context assembly; a search explicitly scoped to an
entry-scope path does not (the memory contract's routed reads are not a hunt)."""

_SHELL_TOOLS = ("Bash",)
"""Tools that run a shell command. Their argv is not reliably parseable, but
ignoring them is not an option either: measured sessions grep, ``sed``, and run
test suites through the shell, and doing that before opening memory is exactly
the scratch-first behavior this measurement exists to catch. So a command is
classified three ways (:func:`_classify_command`) and the one it cannot decide
is recorded as boundary uncertainty rather than guessed in either direction."""

COMMAND_NEUTRAL = "neutral"
"""A shell command *demonstrably* confined to metadata or context assembly: the
clock, a ``git`` query whose every flag is known to print commit/worktree
metadata, or an operation whose named paths all sit in the entry scope. It puts
no repo content in context, so it does not end context assembly.

The word to hold onto is demonstrable. This verdict credits every memory
delivery after it as a pre-load, so it may only be reached by a shape this
reader can show is harmless — never by "nothing said otherwise"."""

COMMAND_WORK = "work"
"""A shell command that reaches into the code: it names or writes a repo path
outside the entry scope, lists this repo's tree, carries a flag that emits file
content, or runs a content command (read, search, build, test). This is the
shell spelling of the same signal a ``Read`` or ``Grep`` carries."""

COMMAND_UNCLEAR = "unclear"
"""Neither — a shape this reader cannot place: an unrecognized command with no
repo path, a ``git`` flag it does not know, a write to the task file whose
content it cannot see, or anything relative that follows a ``cd`` out of the
repo. Treating it as neutral would credit a pre-load that may have followed real
work; treating it as work would invent a missed doc for a session that merely
ran a tool this reader has not seen. Both are claims the evidence does not
support, so it is recorded as uncertainty and refuses only the verdicts that
rest on it."""

NEUTRAL_COMMANDS = frozenset(
    {
        "cd",
        "date",
        "echo",
        "printf",
        "pwd",
        "sleep",
        "true",
        "which",
        "git branch",
        "git check-ignore",
        "git config",
        "git log",
        "git remote",
        "git rev-parse",
        "git status",
    }
)
"""Command heads whose output cannot carry repo content. The protocol's own entry
and session-end steps are here (``date -u`` for the claim timestamp, ``git
status --porcelain`` for the clean-tree check) plus ``git`` metadata: commit
subjects and hashes are what the evidence pack itself treats as display
metadata, so reading them is not reading the code.

The head alone settles only the non-``git`` ones, whose output is fixed by the
command. A ``git`` query's is not — ``git log -p`` prints patches under the same
head — so those are settled by their flags (:data:`GIT_METADATA_FLAGS`).

``ls`` is gone deliberately: it prints a listing *of this repo*, which is repo
material and not metadata (:data:`LISTING_COMMANDS`). ``cd`` stays — it prints
nothing — but only as a statement; what it does to the relative paths *around*
it is a separate question (:func:`_changes_dir`)."""

LISTING_COMMANDS = frozenset({"ls"})
"""Commands whose output is a listing of whatever tree they are pointed at.

Not metadata: a tree listing is repo material, and it is what a ``Glob`` returns
— which this reader already treats as ending context assembly. So a listing is
placed by its target rather than by its head, and its operands are read as paths
even when they carry no separator or suffix (``ls tests`` names a directory
where ``git status`` does not). Pointed at nothing it lists the session's cwd,
which is the repo root; pointed at another checkout it says nothing about this
repo at all."""

GIT_CONTENT_FLAGS = frozenset(
    {
        "-p",
        "--patch",
        "-u",
        "--unified",
        "-U",
        "--raw",
        "--stat",
        "--numstat",
        "--shortstat",
        "--dirstat",
        "--name-only",
        "--name-status",
        "--word-diff",
        "--function-context",
        "-W",
        "-S",
        "-G",
        "-L",
        "--pickaxe-regex",
    }
)
"""Flags that make an otherwise metadata-only ``git`` query emit or search file
content: patches, diffstats, changed-file lists, and the pickaxe/line-log
searches. ``git log -p -1`` is a source read wearing a metadata head, and the
head-only reading credited it as an entry-checklist step."""

GIT_METADATA_FLAGS = frozenset(
    {
        "-n",
        "-C",
        "-a",
        "-b",
        "-i",
        "-l",
        "-q",
        "-r",
        "-s",
        "-v",
        "--abbrev-commit",
        "--abbrev-ref",
        "--after",
        "--all",
        "--author",
        "--before",
        "--branch",
        "--branches",
        "--cached",
        "--color",
        "--committer",
        "--contains",
        "--date",
        "--date-order",
        "--decorate",
        "--diff-filter",
        "--first-parent",
        "--follow",
        "--format",
        "--get",
        "--git-dir",
        "--global",
        "--graph",
        "--grep",
        "--ignored",
        "--list",
        "--local",
        "--long",
        "--max-count",
        "--merged",
        "--merges",
        "--no-abbrev-commit",
        "--no-color",
        "--no-decorate",
        "--no-merged",
        "--no-merges",
        "--no-pager",
        "--oneline",
        "--porcelain",
        "--pretty",
        "--quiet",
        "--remotes",
        "--reverse",
        "--short",
        "--show-current",
        "--show-toplevel",
        "--since",
        "--skip",
        "--stdin",
        "--symbolic-full-name",
        "--tags",
        "--topo-order",
        "--until",
        "--untracked-files",
        "--verify",
    }
)
"""Flags a neutral ``git`` query may carry while staying confined to commit and
worktree metadata — every one observed on those heads across the measured
corpus, plus their obvious siblings.

An allow-list, not a deny-list, and that asymmetry is the point: a ``git`` flag
this reader has never seen may well print file content, so assuming metadata for
it would manufacture exactly the false neutral this classifier exists to avoid.
An unrecognized flag makes the command :data:`COMMAND_UNCLEAR` instead, which
costs confidence rather than inventing a verdict."""

READ_COMMANDS = frozenset(
    {
        "awk",
        "cat",
        "diff",
        "egrep",
        "fgrep",
        "find",
        "grep",
        "head",
        "jq",
        "less",
        "more",
        "rg",
        "sed",
        "sort",
        "tail",
        "uniq",
        "wc",
        "git blame",
        "git diff",
        "git grep",
        "git show",
    }
)
"""Content commands that only *read* what they are pointed at.

Separated from the ones that run or change things because only a reader can be
placed by the paths standing beside it. ``for f in .ai-tasks/a.md
.ai-tasks/b.md; do sed -n '1,12p' "$f"; done`` reads the task files the same
command names — the loop variable is unresolvable on its own, and reading the
statement alone would turn a survey of the entry scope into code work."""

CONTENT_COMMANDS = READ_COMMANDS | frozenset(
    {
        "cargo",
        "cp",
        "curl",
        "go",
        "gofmt",
        "make",
        "mv",
        "mypy",
        "node",
        "npm",
        "npx",
        "pnpm",
        "pytest",
        "python",
        "python3",
        "rm",
        "ruff",
        "tsc",
        "yarn",
        "git apply",
    }
)
"""Command heads that put repo content in context or act on it. Consulted only
when the statement names no repo path at all — ``cat .ai/index.md`` is routed
memory, not code, and the path decides that before the head is looked at.

The non-reader half is work wherever it appears: building, testing, running, or
moving files is not something the entry scope explains away."""


@dataclass(frozen=True)
class SessionRef:
    """One session to roll up, as the evidence builder knows it.

    ``session_id`` is the backend conversation id the orchestrator logged and the
    run manager persisted; ``role``/``kind`` come from the same snapshot and
    decide applicability (:data:`PREFETCH_ROLES`) without needing a transcript.
    """

    session_id: str
    run_id: str | None = None
    role: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class Activity:
    """Content-free counts of the work a session had done at some point in it.

    The anchor the retrieval trace is read against. Row order alone cannot answer
    the timing question the judgment needs — *did this retrieval come before the
    code work it informs, or after that work had already derived the same
    knowledge?* — because order 40 means nothing without knowing what happened in
    the first 39 rows. Once the transcript is discarded these counts are the only
    surviving answer, so they travel with each read.

    Counts, never content: no paths, no commands, no text. ``task_edits`` is
    separate from ``edits`` because writing the session's own task file is
    protocol bookkeeping — the claim, a plan, the session-log entry — and the
    plan/session-log ones are the closest observable marker of *a decision was
    already recorded* that a transcript offers. ``shell`` counts only the shell
    commands that were not recognized as entry-checklist or metadata operations,
    since ``date -u`` is not work a retrieval could be late relative to — and a
    command that *writes* is counted where its write belongs (``edits``,
    ``task_edits``), so a session-log append reads the same whether it went
    through ``Edit`` or through a redirection.
    """

    code_reads: int = 0
    searches: int = 0
    edits: int = 0
    task_edits: int = 0
    shell: int = 0

    def bump(self, field_name: str) -> "Activity":
        return replace(self, **{field_name: getattr(self, field_name) + 1})

    def to_dict(self) -> dict:
        return {
            "code_reads": self.code_reads,
            "searches": self.searches,
            "edits": self.edits,
            "task_edits": self.task_edits,
            "shell": self.shell,
        }


@dataclass(frozen=True)
class PrefetchLoad:
    """One declared prefetch doc and whether the session pre-loaded it.

    ``loaded`` is specifically *delivered during entry context assembly*, which
    is what the ``prefetch:`` instruction asks for. ``phase`` distinguishes the
    two ways it can be False: :data:`PHASE_WORK` means the doc did reach the
    session, but only after it had started working (a late recovery, which the
    retrieval trace also carries), while ``None`` means it never arrived at all.
    Conflating those would hide the difference between a session that ignored a
    declared doc and one that reached for it too late.

    ``order``/``method`` describe the delivering event, so a reader can tell
    caller-injected delivery from an explicit read without a second lookup.
    """

    path: str
    loaded: bool
    method: str | None = None
    order: int | None = None
    phase: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "loaded": self.loaded,
            "method": self.method,
            "order": self.order,
            "phase": self.phase,
        }


@dataclass(frozen=True)
class MemoryRead:
    """One mid-flight consultation of a lazy ``.ai/`` doc.

    ``order`` is the event's position in the session's own transcript and
    ``before`` is the work the session had already done when it happened. The
    pair is the whole point of the trace: a doc opened before the code that would
    have re-derived it, and the same doc opened after that code was read and
    edited, are different behaviors that a bare set of paths cannot distinguish.
    Work done *after* the read is ``SessionReads.activity`` minus ``before``.
    """

    path: str
    order: int
    actor: str
    method: str
    ts: str | None = None
    before: Activity = Activity()

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "order": self.order,
            "actor": self.actor,
            "method": self.method,
            "ts": self.ts,
            "before": self.before.to_dict(),
        }


@dataclass(frozen=True)
class SessionReads:
    """One session's bounded read-behavior rollup.

    Three independent availability facts, kept separate on purpose:

    - ``available`` — a transcript was located and parsed at all.
    - ``assessable`` — *and* the session-time expected set was recovered from
      evidence with no holes in its entry phase, so prefetch completeness is a
      measurement rather than a guess.
    - ``applicable`` — the session was one the prefetch instruction was
      delivered to *and* it declared a non-empty set. ``False`` for a role that
      never receives it (nothing to comply with); ``None`` when the declared set
      could not be recovered, so applicability itself is unknown.

    The split is what keeps a missing transcript from being read as bad behavior:
    a downstream score takes ``applicable and assessable`` as its denominator and
    lets the unassessable remainder lower *confidence* instead.

    ``coverage``/``complete`` are ``None`` unless the session is assessable and
    applicable; there is no default-to-zero, because a zero is a claim.

    ``entry_boundary`` is where context assembly ended (``None`` = the session
    never reached for anything outside its instructions, so the whole scan is
    entry). ``boundary_unsure`` is the earliest row from which that partition is
    no longer certain — an unclassifiable shell command, or a lost result for a
    request that would have moved the boundary — so a reader can see that a long
    "entry" phase was inferred rather than observed. ``entry_extra`` holds the
    lazy docs loaded at entry beyond the declared set — good behavior that would
    otherwise vanish from the evidence once those reads stopped being counted as
    mid-flight retrievals.
    """

    session_id: str
    run_id: str | None = None
    role: str | None = None
    kind: str | None = None
    available: bool = False
    assessable: bool = False
    applicable: bool | None = None
    expected: tuple[str, ...] = ()
    expected_source: str | None = None
    loads: tuple[PrefetchLoad, ...] = ()
    coverage: float | None = None
    complete: bool | None = None
    entry_boundary: int | None = None
    boundary_unsure: int | None = None
    entry_extra: tuple[str, ...] = ()
    entry_extra_truncated: bool = False
    midflight: tuple[MemoryRead, ...] = ()
    midflight_truncated: bool = False
    activity: Activity = Activity()
    closeout_seen: bool = False
    events_scanned: int = 0
    skipped_rows: int = 0
    truncated: bool = False
    detail: str | None = None

    @property
    def loaded(self) -> tuple[str, ...]:
        return tuple(load.path for load in self.loads if load.loaded)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(load.path for load in self.loads if not load.loaded)

    @property
    def late(self) -> tuple[str, ...]:
        """Declared docs that arrived, but only after work had begun.

        A subset of ``missing``: missing *as a pre-load*, which is what the
        instruction asked for, while still consulted — the distinction a reader
        needs before concluding the session ignored the doc.
        """
        return tuple(load.path for load in self.loads if load.phase == PHASE_WORK)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "role": self.role,
            "kind": self.kind,
            "available": self.available,
            "assessable": self.assessable,
            "applicable": self.applicable,
            "expected": list(self.expected),
            "expected_source": self.expected_source,
            "loaded": list(self.loaded),
            "missing": list(self.missing),
            "late": list(self.late),
            "loads": [load.to_dict() for load in self.loads],
            "coverage": self.coverage,
            "complete": self.complete,
            "entry_boundary": self.entry_boundary,
            "boundary_unsure": self.boundary_unsure,
            "entry_extra": list(self.entry_extra),
            "entry_extra_truncated": self.entry_extra_truncated,
            "midflight": [read.to_dict() for read in self.midflight],
            "midflight_truncated": self.midflight_truncated,
            "activity": self.activity.to_dict(),
            "closeout_seen": self.closeout_seen,
            "events_scanned": self.events_scanned,
            "skipped_rows": self.skipped_rows,
            "truncated": self.truncated,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReadBehavior:
    """The evidence pack's read-behavior section: one rollup per known session.

    ``available`` follows :class:`orch_hub.ai_snapshot.AiSnapshot`'s rule — it
    reports what the section actually *carries*, not whether a read was
    attempted. It is True only when at least one session's transcript was
    located and parsed, so a pack built where no transcript store exists reads
    the same as one built for sessions that left none: no read evidence. An
    evaluator deciding whether it may judge read behavior needs that answer, not
    the reason.

    ``warnings`` are this section's degradation notes, aggregated into the pack's
    warning count like the per-run and ``.ai/`` ones. Only sessions that *could*
    have been measured warn — a review session with no transcript is expected,
    not degraded.
    """

    available: bool
    sessions: tuple[SessionReads, ...] = ()
    warnings: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def assessable_count(self) -> int:
        return sum(1 for session in self.sessions if session.assessable)

    @property
    def applicable_count(self) -> int:
        return sum(1 for session in self.sessions if session.applicable)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "sessions": [session.to_dict() for session in self.sessions],
            "warnings": list(self.warnings),
            "detail": self.detail,
        }


def project_slug(path: Path) -> str:
    """Claude Code's project-dir name for an absolute working directory."""
    return _SLUG.sub("-", str(path))


def locate_transcript(
    transcript_root: Path, repo: ManagedRepo, session_id: str
) -> Path | None:
    """Path of one session's transcript under ``transcript_root``, or ``None``.

    The orchestrator launches every backend session with ``cwd=<repo>``, so the
    project dir is derived from the repo path rather than searched for. A session
    stored under some other project slug — one resumed from a different cwd —
    reads as *not located* instead of being hunted across the store: widening the
    lookup would make the reader enumerate an operator's unrelated projects to
    answer a question about this repo.

    The id is validated as a benign single segment and the resolved file is
    required to stay under the resolved root, so a crafted id recorded in run
    state cannot steer the read out of the transcript store.
    """
    if not _SESSION_ID.match(session_id or ""):
        return None
    root = _resolve(transcript_root)
    if root is None:
        return None
    candidate = _resolve(
        transcript_root / project_slug(repo.path) / f"{session_id}.jsonl"
    )
    if candidate is None or not candidate.is_relative_to(root):
        return None
    return candidate if candidate.is_file() else None


def read_session_behavior(
    repo: ManagedRepo,
    *,
    task_id: str,
    sessions: Sequence[SessionRef],
    transcript_root: Path,
    max_sessions: int = MAX_SESSIONS,
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
    max_line_bytes: int = MAX_LINE_BYTES,
    max_midflight: int = MAX_MIDFLIGHT_EVENTS,
) -> ReadBehavior:
    """Roll up read behavior for ``sessions``, bounded and total.

    Never raises for transcript-store state: a missing store, a missing or
    unparseable transcript, an unreadable file, or a session that never re-read
    its own task file each degrade to a recorded state with a ``detail``.

    With no session ids at all — the pack's ordinary shape for a live run, whose
    session snapshot is written only at the live-to-ended transition — nothing is
    read and the section reports why. That early exit is also what keeps a pack
    built for a task with no sessions from touching the transcript store at all.
    """
    if not sessions:
        return ReadBehavior(
            available=False,
            detail="no persisted session ids to attribute read behavior to",
        )
    repo_root = _resolve(repo.path)
    if repo_root is None:
        return ReadBehavior(
            available=False,
            detail="managed repo path does not resolve",
            warnings=("read behavior: managed repo path does not resolve",),
        )
    task_paths = _task_paths(task_id)

    unique = _unique_sessions(sessions)
    warnings: list[str] = []
    rollups: list[SessionReads] = []
    for ref in unique[:max_sessions]:
        rollup = _session_rollup(
            ref,
            repo=repo,
            repo_root=repo_root,
            task_paths=task_paths,
            transcript_root=transcript_root,
            max_bytes=max_bytes,
            max_line_bytes=max_line_bytes,
            max_midflight=max_midflight,
            warnings=warnings,
        )
        rollups.append(rollup)
    dropped = max(0, len(unique) - max_sessions)
    if dropped:
        warnings.append(
            f"read behavior: {dropped} session(s) dropped past the"
            f" {max_sessions}-session cap"
        )
    available = any(rollup.available for rollup in rollups)
    return ReadBehavior(
        available=available,
        sessions=tuple(rollups),
        warnings=tuple(warnings),
        detail=None if available else "no session transcript could be read",
    )


def _unique_sessions(sessions: Sequence[SessionRef]) -> list[SessionRef]:
    """Sessions in given order, first mention of each id winning.

    A resumed or reattached run can carry the same conversation id in two run
    snapshots; the rollup is per *session*, so the second mention is the same
    subject, not a second measurement.
    """
    seen: set[str] = set()
    unique: list[SessionRef] = []
    for ref in sessions:
        if not ref.session_id or ref.session_id in seen:
            continue
        seen.add(ref.session_id)
        unique.append(ref)
    return unique


def _task_paths(task_id: str) -> tuple[str, ...]:
    """Repo-relative spellings of the target task file, active and archived.

    Both are accepted because a session that ran before close-out read the active
    path while a later reader of the same task may see the archived one; either
    read carries the same frontmatter.
    """
    return (
        f".ai-tasks/{task_id}.md",
        f".ai-tasks/archive/{task_id}.md",
    )


def _session_rollup(
    ref: SessionRef,
    *,
    repo: ManagedRepo,
    repo_root: Path,
    task_paths: tuple[str, ...],
    transcript_root: Path,
    max_bytes: int,
    max_line_bytes: int,
    max_midflight: int,
    warnings: list[str],
) -> SessionReads:
    """Locate, scan, and summarize one session's transcript."""
    base = SessionReads(
        session_id=ref.session_id,
        run_id=ref.run_id,
        role=ref.role,
        kind=ref.kind,
        applicable=None if _prefetch_role(ref.role) else False,
    )
    path = locate_transcript(transcript_root, repo, ref.session_id)
    if path is None:
        return _degraded(base, "no session transcript found", warnings)

    scan = _scan_transcript(
        path,
        repo_root=repo_root,
        task_paths=task_paths,
        max_bytes=max_bytes,
        max_line_bytes=max_line_bytes,
    )
    if scan.error is not None:
        return _degraded(base, scan.error, warnings)

    base = _replace(
        base,
        available=True,
        events_scanned=scan.rows,
        skipped_rows=scan.skipped,
        truncated=scan.truncated,
        closeout_seen=scan.closeout_seen,
        entry_boundary=scan.boundary,
        boundary_unsure=scan.boundary_unsure,
        activity=scan.activity,
    )
    if scan.rows == 0:
        return _degraded(base, "session transcript exposed no readable events", warnings)

    expected, expected_detail = _expected_set(scan.task_content)
    # The two mechanisms degrade independently, like the ``.ai/`` section's docs
    # and patch stream: an unrecoverable expected set makes prefetch completeness
    # unassessable, but what the session was observed to load and consult is still
    # real and is not thrown away with it. Both traces are read off the phase
    # boundary alone; the declared set only decides which entry loads are already
    # reported as ``loads`` and so need not repeat as extras.
    midflight, midflight_truncated = _midflight_trace(scan.reads, limit=max_midflight)
    entry_extra, entry_extra_truncated = _entry_extra(
        scan.reads, expected=expected or (), limit=max_midflight
    )
    base = _replace(
        base,
        midflight=midflight,
        midflight_truncated=midflight_truncated,
        entry_extra=entry_extra,
        entry_extra_truncated=entry_extra_truncated,
    )
    if expected is None:
        return _degraded(
            base,
            expected_detail or "session-time prefetch set not recoverable",
            warnings,
        )

    loads = _prefetch_loads(expected, scan.reads)
    applicable = bool(expected) and _prefetch_role(ref.role)
    base = _replace(
        base,
        applicable=applicable,
        expected=expected,
        expected_source=SOURCE_TRANSCRIPT_TASK_READ,
        loads=loads,
        detail=expected_detail,
    )
    # Facts stay, the score does not: lost evidence could hold the very load that
    # would flip this verdict — or the work signal that would move one of the
    # loads we did see out of the entry phase — so the observed loads are
    # reported as diagnosis while coverage stays unclaimed.
    refusal = _entry_verdict_refusal(scan, loads) if expected else None
    if refusal is not None:
        return _degraded(base, refusal, warnings)

    loaded = sum(1 for load in loads if load.loaded)
    return _replace(
        base,
        assessable=True,
        coverage=(loaded / len(expected)) if applicable else None,
        complete=(loaded == len(expected)) if applicable else None,
    )


def _prefetch_role(role: str | None) -> bool:
    """Whether a session in ``role`` is delivered the prefetch pre-load instruction."""
    return role in PREFETCH_ROLES


def _entry_verdict_refusal(
    scan: "_Scan", loads: Sequence[PrefetchLoad]
) -> str | None:
    """Why this session's prefetch verdict cannot be claimed, or ``None``.

    Three questions, each about a different way the evidence can fail to support
    the verdict, and each asked only of the part of the evidence that verdict
    actually rests on. A blanket "any damage anywhere makes the session
    unassessable" would be simpler and much more expensive: transcripts big
    enough to hit the byte cap reached the code long before it, so the cut is
    always in the work phase, and the pre-load evidence in front of it is intact.
    """
    if not _entry_evidence_intact(scan):
        return _gap_detail(scan)
    lost = {load.path for load in loads if not load.loaded} & scan.lost_entry
    if lost:
        return (
            f"entry-phase evidence incomplete ({len(lost)} declared doc(s) were"
            " requested before the boundary and their results did not survive the"
            " scan); prefetch loading not measured"
        )
    unsure = {load.path for load in loads if load.loaded} & _unsure_entry_paths(scan)
    if unsure:
        return (
            f"entry boundary not determinable ({len(unsure)} declared doc(s) were"
            " delivered after activity this reader could not classify); prefetch"
            " loading not measured"
        )
    return None


def _entry_evidence_intact(scan: "_Scan") -> bool:
    """Whether the scanned prefix that decides entry prefetch was read whole.

    Only the entry phase matters, so this is positional rather than a blanket
    "any hole anywhere": a row skipped or a tail cut *after* the session began
    working can hide a late recovery or a mid-flight read (both already reported
    as their own bounded, flagged facts) but cannot change what was in context
    before the boundary — with the one exception the caller checks next, a read
    asked for before the boundary whose result landed in the damage.

    With no boundary observed, the entry phase is the whole scan and any hole is
    inside it — including a truncated tail, which is why a cut with no boundary
    is not measurable while a cut after one is.
    """
    if scan.first_gap is not None and (
        scan.boundary is None or scan.first_gap <= scan.boundary
    ):
        return False
    return not (scan.truncated and scan.boundary is None)


def _unsure_entry_paths(scan: "_Scan") -> set[str]:
    """Paths whose entry-phase credit rests on an undetermined boundary.

    A doc delivered *before* the first unclassifiable activity is a pre-load
    whatever that activity turns out to have been; only what came after it is in
    question, so the refusal stays as narrow as the doubt.
    """
    if scan.boundary_unsure is None:
        return set()
    return {
        read.path
        for read in scan.reads
        if read.phase == PHASE_ENTRY and read.requested_at >= scan.boundary_unsure
    }


def _gap_detail(scan: "_Scan") -> str:
    """Why the entry-phase evidence is incomplete, in the reader's own terms."""
    parts = []
    if scan.skipped:
        parts.append(f"{scan.skipped} unreadable row(s)")
    if scan.truncated:
        parts.append("a truncated tail")
    holes = " and ".join(parts) or "incomplete evidence"
    return f"entry-phase evidence incomplete ({holes}); prefetch loading not measured"


def _degraded(base: SessionReads, detail: str, warnings: list[str]) -> SessionReads:
    """Record an unassessable session, warning only when it was measurable.

    A role that never receives the prefetch instruction (a review session, whose
    Codex-backed conversation leaves nothing in this store) is *expected* to have
    no read evidence, so it is not degradation. A dev session with no usable
    transcript is a real gap in the measurement and says so.
    """
    if _prefetch_role(base.role):
        warnings.append(f"read behavior: session {base.session_id}: {detail}")
    return _replace(base, assessable=False, detail=detail)


def _replace(base: SessionReads, **changes: object) -> SessionReads:
    """``dataclasses.replace`` for the frozen rollup, typed loosely for brevity."""
    return replace(base, **changes)  # type: ignore[arg-type]


def _expected_set(content: str | None) -> tuple[tuple[str, ...] | None, str | None]:
    """The session-time expected prefetch set, or ``None`` when unrecoverable.

    ``content`` is the task file exactly as the session read it at entry. Entries
    are normalized through the one prefetch-path rule
    (:func:`orch_hub.ai_snapshot.prefetch_doc_path`), so a malformed entry cannot
    be counted as a doc that could have been loaded; it is noted in the detail
    instead, because a reader comparing ``expected`` against the raw frontmatter
    would otherwise see an unexplained gap.

    A recovered-but-empty set is ``()``, not ``None``: a task that declared no
    prefetch has nothing to comply with, which is a measurement, whereas an
    unrecovered set is the absence of one.
    """
    if content is None:
        return None, "session did not read its task file; expected set unrecoverable"
    entries = parse_prefetch(content)
    expected: list[str] = []
    malformed = 0
    for entry in entries:
        rel = prefetch_doc_path(entry)
        if rel is None:
            malformed += 1
            continue
        if rel not in expected:
            expected.append(rel)
    if len(expected) > MAX_EXPECTED_DOCS:
        return None, (
            f"declared prefetch set of {len(expected)} docs exceeds the"
            f" {MAX_EXPECTED_DOCS}-doc cap; not measured"
        )
    detail = (
        f"{malformed} malformed prefetch entr{'y' if malformed == 1 else 'ies'} ignored"
        if malformed
        else None
    )
    return tuple(expected), detail


def _prefetch_loads(
    expected: Sequence[str], reads: Sequence["_Read"]
) -> tuple[PrefetchLoad, ...]:
    """Pair each expected doc with the event that delivered it, and in which phase.

    Order *within* the entry phase is not required: the checklist's pre-load step
    competes with the claim edit and other entry bookkeeping, so a prefetch doc
    read a few events in is still a pre-load. Crossing the boundary is what
    counts — a doc first delivered once the session was already reading code is a
    late recovery, not compliance with an instruction to pre-load — and an entry
    delivery always wins over a later one for the same path.
    """
    entry = _first_by_path(reads, PHASE_ENTRY)
    work = _first_by_path(reads, PHASE_WORK)
    loads = []
    for path in expected:
        read = entry.get(path) or work.get(path)
        loads.append(
            PrefetchLoad(
                path=path,
                loaded=path in entry,
                method=read.method if read else None,
                order=read.order if read else None,
                phase=read.phase if read else None,
            )
        )
    return tuple(loads)


def _entry_extra(
    reads: Sequence["_Read"], *, expected: Sequence[str], limit: int
) -> tuple[tuple[str, ...], bool]:
    """Lazy ``.ai/`` docs pre-loaded at entry beyond the declared set, in order.

    Kept because the phase partition would otherwise make them disappear: they
    are not prefetch compliance (undeclared) and not mid-flight retrieval (no
    work had begun), yet loading memory the task never declared is exactly the
    behavior the read side exists to see. Paths only — the timing question these
    would answer is trivially "before any work".
    """
    declared = set(expected)
    paths: list[str] = []
    truncated = False
    for read in reads:
        if read.phase != PHASE_ENTRY or not _lazy_memory_doc(read.path):
            continue
        if read.path in declared or read.path in paths:
            continue
        if len(paths) >= limit:
            truncated = True
            continue
        paths.append(read.path)
    return tuple(paths), truncated


def _midflight_trace(
    reads: Sequence["_Read"], *, limit: int
) -> tuple[tuple[MemoryRead, ...], bool]:
    """The ordered first-consult trace of lazy ``.ai/`` docs read while working.

    Membership is decided by the observable phase boundary, not by the declared
    set: a doc already in context when work began is not being retrieved (that
    covers both the declared pre-loads and anything the harness injected at
    entry), while a *declared* doc first opened after the boundary is a genuine
    retrieval — a late one — and belongs in the trace it was recovered in.

    The eager set is excluded throughout: it is in every session's context by the
    loading contract, so reading it is not retrieval and skipping it is not a
    missed consultation. Needing no expected set, the trace survives a session
    whose declared set could not be recovered at all.
    """
    in_context = {read.path for read in reads if read.phase == PHASE_ENTRY}
    seen: set[str] = set()
    trace: list[MemoryRead] = []
    truncated = False
    for read in reads:
        if read.phase != PHASE_WORK or not _lazy_memory_doc(read.path):
            continue
        if read.path in in_context or read.path in seen:
            continue
        seen.add(read.path)
        if len(trace) >= limit:
            truncated = True
            continue
        trace.append(
            MemoryRead(
                path=read.path,
                order=read.order,
                actor=read.actor,
                method=read.method,
                ts=read.ts,
                before=read.before,
            )
        )
    return tuple(trace), truncated


def _lazy_memory_doc(rel: str) -> bool:
    """Whether a repo-relative path is a ``.ai/`` doc that has to be fetched."""
    return rel.startswith(f"{AI_DIRNAME}/") and not is_eager_doc(rel)


def _first_by_path(reads: Sequence["_Read"], phase: str) -> dict[str, "_Read"]:
    """First delivery of each path within one phase."""
    first: dict[str, _Read] = {}
    for read in reads:
        if read.phase == phase:
            first.setdefault(read.path, read)
    return first


@dataclass(frozen=True)
class _Read:
    """One observed delivery of a repo file's content into a session's context.

    ``order`` is the row the content landed in; ``phase`` is decided from
    ``requested_at``, the row the session *asked* for it, so a parallel batch
    issued during context assembly stays entry-phase even when its results
    trickle in after the batch's own code read already closed the boundary. Both
    rows are kept because that split is exactly what makes a late-landing result
    vulnerable to a hole the entry phase itself never had.
    """

    path: str
    order: int
    requested_at: int
    actor: str
    method: str
    phase: str
    ts: str | None = None
    before: Activity = Activity()


@dataclass
class _Scan:
    """Raw facts extracted from one transcript, before any judgment.

    ``pending`` is scanning scaffolding, not an extracted fact: it holds each
    read tool call's repo-relative path and row only until its result arrives, so
    a call that never returned is dropped rather than credited. What it leaves
    behind at the end of the scan is evidence in its own right — see
    :meth:`close_pending`.

    ``first_gap``/``last_gap`` are the row positions of the earliest and latest
    hole — a malformed row, a row past the per-row cap. Positions rather than a
    flag because that is what decides which verdicts a hole can undermine
    (:func:`_entry_verdict_refusal`): the earliest says whether entry-phase
    evidence was lost, the latest whether a still-unanswered request could have
    been answered inside one. A truncated tail is recorded separately since it is
    always after everything.
    """

    reads: list[_Read] = field(default_factory=list)
    task_content: str | None = None
    closeout_seen: bool = False
    rows: int = 0
    truncated: bool = False
    skipped: int = 0
    first_gap: int | None = None
    last_gap: int | None = None
    boundary: int | None = None
    boundary_unsure: int | None = None
    lost_entry: set[str] = field(default_factory=set)
    activity: Activity = Activity()
    error: str | None = None
    pending: dict[str, tuple[str | None, int]] = field(default_factory=dict)
    seen: set[tuple[str, str]] = field(default_factory=set)

    def note_gap(self) -> None:
        """Record a row this reader could not use, at the position it would hold."""
        self.skipped += 1
        if self.first_gap is None:
            self.first_gap = self.rows
        self.last_gap = self.rows

    def note_activity(self, kind: str) -> None:
        """Count one observed activity without ending context assembly.

        The claim edit the entry checklist prescribes happens *during* assembly,
        so treating it as work would end the entry phase before the pre-load it
        is there to measure — but it is still activity the retrieval trace is
        read against.
        """
        self.activity = self.activity.bump(kind)

    def note_work(self, order: int, kind: str) -> None:
        """Count one observed activity and close context assembly."""
        self.note_activity(kind)
        if self.boundary is None:
            self.boundary = order

    def note_unsure(self, order: int) -> None:
        """Record that from ``order`` on, the entry/work partition is a guess.

        Only meaningful while context assembly is still open: once work has been
        observed the boundary is already placed, and nothing later can move it
        earlier.
        """
        if self.boundary is not None and self.boundary <= order:
            return
        if self.boundary_unsure is None or order < self.boundary_unsure:
            self.boundary_unsure = order

    def phase_at(self, order: int) -> str:
        """Which phase a delivery requested at ``order`` belongs to."""
        return (
            PHASE_ENTRY
            if self.boundary is None or order < self.boundary
            else PHASE_WORK
        )

    def add_read(
        self,
        *,
        path: str,
        order: int,
        requested_at: int,
        actor: str,
        method: str,
        ts: str | None,
    ) -> None:
        """Keep the first delivery of each ``(path, phase)``; drop repeats.

        Only ``.ai/`` docs are retained: a read is evidence only insofar as it is
        memory the session loaded, and every other read has already been folded
        into the boundary and the activity counters by the time it gets here.
        Two entries per path at most, which is what bounds this list — the first
        pre-load and the first later consult are the whole story; the tenth
        reread of the same doc adds nothing and would grow with the session.
        """
        if not path.startswith(f"{AI_DIRNAME}/"):
            return
        phase = self.phase_at(requested_at)
        if (path, phase) in self.seen:
            return
        self.seen.add((path, phase))
        self.reads.append(
            _Read(
                path=path,
                order=order,
                requested_at=requested_at,
                actor=actor,
                method=method,
                phase=phase,
                ts=ts,
                before=self.activity,
            )
        )

    def close_pending(self) -> None:
        """Turn the requests that never got a result into evidence about the gaps.

        A read whose result never arrived normally means nothing entered the
        context — a turn cut off mid-flight is not a load. But if this scan lost
        rows *after* that request, the result may be exactly what it lost, and
        then two different conclusions become unsafe:

        - an unanswered request for a memory doc, issued while assembling
          context, could be a declared doc this rollup would otherwise report as
          never loaded — a fabricated negative;
        - an unanswered request for repo source could be the delivery that would
          have ended context assembly, leaving every later "pre-load" credited
          against a boundary that should have closed earlier.

        Both are recorded and left for :func:`_entry_verdict_refusal` to weigh
        against what the session actually declared.
        """
        for rel, requested_at in self.pending.values():
            if rel is None or self.phase_at(requested_at) != PHASE_ENTRY:
                continue
            if not (
                self.truncated
                or (self.last_gap is not None and self.last_gap > requested_at)
            ):
                continue
            if rel.startswith(f"{AI_DIRNAME}/"):
                self.lost_entry.add(rel)
            elif not _entry_scope(rel):
                self.note_unsure(requested_at)
        self.pending.clear()


def _scan_transcript(
    path: Path,
    *,
    repo_root: Path,
    task_paths: tuple[str, ...],
    max_bytes: int,
    max_line_bytes: int,
) -> _Scan:
    """Stream one transcript into a :class:`_Scan`, bounded at the read itself.

    Bounded at the producer, like every other evidence reader here: the file cap
    and the per-row cap are applied while reading, not to a buffer already in
    memory, and only the small extracted facts are retained — never a row, never
    the file.

    The scan stops at the close-out marker rather than filtering afterwards, so
    a long absorption phase costs nothing and no post-close-out read can leak
    into either measurement.

    Rows this reader cannot use — malformed, non-object, or past the per-row cap
    — are counted and positioned rather than silently dropped: what is missing
    from the evidence decides what may be concluded from it. A blank line is not
    such a row; it carries nothing either way.
    """
    scan = _Scan()
    try:
        with path.open("rb") as handle:
            for raw, complete in _iter_rows(
                handle, scan, max_bytes=max_bytes, max_line_bytes=max_line_bytes
            ):
                if not raw.strip():
                    continue
                if not complete:
                    scan.note_gap()
                    continue
                row = _parse_row(raw)
                if row is None:
                    scan.note_gap()
                    continue
                order = scan.rows
                scan.rows += 1
                closeout = _absorb_row(
                    row,
                    scan,
                    order=order,
                    repo_root=repo_root,
                    task_paths=task_paths,
                )
                if closeout:
                    scan.closeout_seen = True
                    break
    except OSError as err:
        return _Scan(error=f"session transcript unreadable ({err})")
    scan.close_pending()
    scan.seen.clear()
    return scan


def _iter_rows(
    handle,
    scan: _Scan,
    *,
    max_bytes: int,
    max_line_bytes: int,
) -> Iterator[tuple[bytes, bool]]:
    """Yield ``(row bytes, complete)`` under both caps, recording a budget cut.

    ``readline`` is given an explicit size so a single pathological row cannot be
    pulled into memory whole; an over-long row's tail is then drained (still
    bounded) so the next yield resumes on a real row boundary rather than on the
    middle of one, which would otherwise turn one huge row into a stream of
    unparseable fragments.

    ``truncated`` is set only when content is actually left behind — a file that
    ends exactly on the budget was read whole, and reporting it as cut would put
    a false gap in the evidence.
    """
    remaining = max_bytes
    while remaining > 0:
        chunk = handle.readline(min(max_line_bytes, remaining) + 1)
        if not chunk:
            return
        remaining -= len(chunk)
        if chunk.endswith(b"\n"):
            yield chunk, True
            continue
        # No terminator: either the file's last row, the per-row cap, or the byte
        # budget. Drain to the next boundary to tell them apart.
        drained = 0
        while remaining > 0:
            more = handle.readline(min(max_line_bytes, remaining) + 1)
            if not more:
                break
            remaining -= len(more)
            drained += len(more)
            if more.endswith(b"\n"):
                break
        yield chunk, drained == 0
    scan.truncated = bool(handle.read(1))


def _parse_row(raw: bytes) -> dict | None:
    """One JSONL row as a dict, or ``None`` for anything else.

    A mid-write tail line, a non-object row, or a row this reader's version of
    the format does not understand is skipped rather than failing the scan — the
    same tolerance the orchestrator-log parser applies for the same reason.
    """
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _absorb_row(
    row: dict,
    scan: _Scan,
    *,
    order: int,
    repo_root: Path,
    task_paths: tuple[str, ...],
) -> bool:
    """Fold one row into ``scan``; return True when it is the close-out marker.

    Reads are credited from the **result** side, never the request: a ``Read``
    call that failed put nothing into context, and crediting the call would let a
    typo'd path count as a loaded prefetch doc. The pending map holds the
    requested path only until its result arrives, and an unanswered request (a
    session cut off mid-turn) is simply never credited.

    Mutations and searches are read from the **call** side instead, because that
    is where the session's intent to work is expressed and their results carry
    nothing this reader needs. Either way the row that first shows work closes
    context assembly.
    """
    kind = row.get("type")
    actor = ACTOR_SUBAGENT if row.get("isSidechain") else ACTOR_ROOT
    ts = row.get("timestamp") if isinstance(row.get("timestamp"), str) else None

    if kind == "assistant":
        for block in _content_blocks(row):
            if block.get("type") != "tool_use":
                continue
            name, block_id = block.get("name"), block.get("id")
            args = block.get("input")
            args = args if isinstance(args, dict) else {}
            if name == "Skill" and args.get("skill") == CLOSEOUT_SKILL:
                return True
            if name in _READ_TOOLS and isinstance(block_id, str):
                scan.pending[block_id] = (
                    _repo_rel(args.get("file_path"), repo_root),
                    order,
                )
            elif name in _EDIT_TOOLS:
                _absorb_edit(args, scan, order=order, repo_root=repo_root,
                             task_paths=task_paths)
            elif name in _SEARCH_TOOLS:
                _absorb_search(args, scan, order=order, repo_root=repo_root)
            elif name in _SHELL_TOOLS:
                _absorb_command(
                    args,
                    scan,
                    order=order,
                    repo_root=repo_root,
                    task_paths=task_paths,
                )
        return False

    if kind == "user":
        if _closeout_command(row):
            return True
        _absorb_tool_results(
            row,
            scan,
            order=order,
            actor=actor,
            ts=ts,
            repo_root=repo_root,
            task_paths=task_paths,
        )
        return False

    if kind == "attachment":
        _absorb_attachment(
            row,
            scan,
            order=order,
            actor=actor,
            ts=ts,
            repo_root=repo_root,
            task_paths=task_paths,
        )
    return False


def _absorb_edit(
    args: dict,
    scan: _Scan,
    *,
    order: int,
    repo_root: Path,
    task_paths: tuple[str, ...],
) -> None:
    """Count one file mutation, and close context assembly unless it is bookkeeping.

    Writing the session's own task file splits in two. The claim — status,
    estimate, ``claimed-by`` — is the entry checklist's own first step and must
    not end the phase it precedes. A plan or session-log write is the opposite:
    it is the transcript's clearest evidence that a decision has been made, so a
    doc opened after it is not informing that decision. Both are counted as
    ``task_edits``; only the second one closes context assembly.

    Writing anything else inside the repo is work; writing outside it (a scratch
    file in ``/tmp``) is not this repo's work at all.
    """
    rel = _repo_rel(args.get("file_path") or args.get("notebook_path"), repo_root)
    if rel is None:
        return
    if rel in task_paths:
        if _records_decision(args):
            scan.note_work(order, "task_edits")
        else:
            scan.note_activity("task_edits")
    elif not _entry_scope(rel):
        scan.note_work(order, "edits")


def _records_decision(args: dict) -> bool:
    """Whether a task-file edit writes plan/session-log content rather than a claim.

    Reads the edit's replacement text only to classify its *shape*
    (:data:`_DECISION_MARKER`); nothing about it is retained. ``MultiEdit``
    carries its replacements one level down, and a whole-file ``Write`` of a task
    file necessarily contains the session log, so both are covered.
    """
    values = [args.get("new_string"), args.get("content")]
    edits = args.get("edits")
    if isinstance(edits, list):
        values += [edit.get("new_string") for edit in edits if isinstance(edit, dict)]
    return any(
        isinstance(value, str) and _DECISION_MARKER.search(value) for value in values
    )


def _absorb_command(
    args: dict,
    scan: _Scan,
    *,
    order: int,
    repo_root: Path,
    task_paths: tuple[str, ...],
) -> None:
    """Fold one shell command into the boundary, or into its uncertainty."""
    command = args.get("command")
    verdict, kind = _classify_command(
        command if isinstance(command, str) else "",
        repo_root,
        task_paths=task_paths,
    )
    if verdict == COMMAND_WORK:
        scan.note_work(order, kind)
    elif verdict == COMMAND_UNCLEAR:
        scan.note_activity(kind)
        scan.note_unsure(order)


def _classify_command(
    command: str, repo_root: Path, *, task_paths: tuple[str, ...]
) -> tuple[str, str]:
    """Place one shell command, as ``(verdict, activity kind)``.

    Statement by statement, because a command is a sequence of them and the
    strongest signal in it is the command's: ``date -u && sed -n 1,40p
    src/api.ts`` is a code read that happens to start with the clock. Work wins
    over uncertainty, and uncertainty over neutrality — the ordering that never
    lets an unplaceable statement be read as harmless.

    The ``activity kind`` is which :class:`Activity` counter the command feeds,
    so a session-log write through the shell lands where the same write through
    ``Edit`` would.
    """
    records_decision = bool(_DECISION_MARKER.search(command))
    root = repo_root.as_posix()
    # Every path the command names anywhere, as the context a reader statement
    # that names none of its own is placed against (:data:`READ_COMMANDS`).
    context = tuple(_command_paths(command, repo_root))
    verdicts = []
    cwd: str | None = ""
    for statement in _STATEMENTS.split(command):
        # A ``cd`` reaches forward, not back: the statements after it are read
        # against the directory it moved to, while the ones before it stay
        # exactly as observed.
        cwd = _next_cwd(cwd, statement, root)
        verdicts.append(
            _classify_statement(
                statement,
                repo_root,
                task_paths=task_paths,
                records_decision=records_decision,
                cwd=cwd,
                context=context,
            )
        )
    for wanted in (COMMAND_WORK, COMMAND_UNCLEAR):
        for verdict, kind in verdicts:
            if verdict == wanted:
                return verdict, kind
    return COMMAND_NEUTRAL, "shell"


def _classify_statement(
    statement: str,
    repo_root: Path,
    *,
    task_paths: tuple[str, ...],
    records_decision: bool,
    cwd: str | None = "",
    context: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Place one shell statement as :data:`COMMAND_NEUTRAL`/``WORK``/``UNCLEAR``.

    A best-effort reading of argv, asked in the order that keeps the common
    shapes right:

    1. what it *writes* — a write to the task file is the shell spelling of the
       session-log edit that marks a recorded decision, and reading it as just
       another entry-scope path would let it pass as context assembly;
    2. what it *names* — ``cat .ai/index.md`` is routed memory whatever ``cat``
       usually means, and ``sed -n 1,40p src/api.ts`` is the code whatever
       ``sed`` usually means, with a listing's operands read as the paths they
       are (:func:`_listing_targets`);
    3. whether everything it touched is somewhere else on the machine, which is
       nothing this repo's measurement can conclude anything from;
    4. its head word, and — where the head alone does not fix the output — its
       flags (:func:`_git_flag_verdict`) or the paths standing beside it
       (:data:`READ_COMMANDS`).

    This is not a shell parser and does not pretend to be one: quoting, command
    substitution, and functions are not interpreted. It is safe because every
    misreading has somewhere to go — :data:`COMMAND_UNCLEAR` — instead of having
    to become one of the two verdicts that carry a claim.
    """
    if not statement.strip():
        return COMMAND_NEUTRAL, "shell"
    root = repo_root.as_posix()
    head = _statement_head(statement)
    if head == "cd":
        # The move is the caller's business (:func:`_next_cwd`); the statement
        # itself puts nothing in context.
        return COMMAND_NEUTRAL, "shell"
    writes, body = _split_redirects(statement, root, cwd=cwd)
    if cwd is None:
        # A ``cd`` left the repo, so every relative token is measured against a
        # directory this reader cannot name; only what was named absolutely
        # still means what it says.
        named = _command_paths(body, repo_root, cwd=cwd)
        if any(not _entry_scope(rel) for rel in named):
            return COMMAND_WORK, "shell"
        return COMMAND_UNCLEAR, "shell"
    for rel in writes:
        if rel in task_paths:
            # The shell's answer to ``_records_decision``. A write whose content
            # this reader cannot classify is not shown to be bookkeeping, so it
            # does not get the benefit of the doubt the claim edit gets.
            if records_decision:
                return COMMAND_WORK, "task_edits"
            return COMMAND_UNCLEAR, "task_edits"
        if not _entry_scope(rel):
            return COMMAND_WORK, "edits"
    listed, unplaced = _listing_targets(body, root, cwd=cwd)
    named = _command_paths(body, repo_root, cwd=cwd) + listed
    if any(not _entry_scope(rel) for rel in named):
        return COMMAND_WORK, "shell"
    if unplaced:
        return COMMAND_UNCLEAR, "shell"
    if named or writes:
        return COMMAND_NEUTRAL, "shell"
    if _names_outside(body, root, cwd=cwd):
        # Everything it touched is somewhere else on the machine — another
        # checkout, ``/tmp``. The tool side already ignores such a read (it never
        # resolves to a repo path), and the shell spelling of it is no different.
        return COMMAND_NEUTRAL, "shell"
    if head in CONTENT_COMMANDS:
        if head in READ_COMMANDS and context and all(map(_entry_scope, context)):
            # It named nothing itself, but everything this command names is the
            # session's instructions, so that is what it read.
            return COMMAND_NEUTRAL, "shell"
        return COMMAND_WORK, "shell"
    if head in LISTING_COMMANDS:
        # Everything it was pointed at is outside the repo (``ls ~``, ``ls ..``);
        # an in-repo target, the cwd included, was decided above.
        return COMMAND_NEUTRAL, "shell"
    if head in NEUTRAL_COMMANDS:
        verdict = _git_flag_verdict(body) if head.startswith("git ") else COMMAND_NEUTRAL
        return verdict, "shell"
    if head is None:
        # A statement with no command word at all — a loop's ``done``, a stray
        # brace. It ran nothing, so there is nothing to be uncertain about.
        return COMMAND_NEUTRAL, "shell"
    return COMMAND_UNCLEAR, "shell"


def _split_redirects(
    statement: str, root: str, *, cwd: str | None = ""
) -> tuple[list[str], str]:
    """``(in-repo write targets, statement without its redirections)``.

    The targets are removed from the text the rest of the classification reads,
    so a write is weighed as a write and not counted a second time as a path the
    command merely names. Only a target that looks like a file is kept: this is
    a heuristic over unparsed argv, and a bare word picked out of prose is far
    more likely to be prose than a write.
    """
    writes: list[str] = []

    def swallow(match: re.Match) -> str:
        target = match.group("target")
        if target is None:
            return " "
        place, rel, absolute = _place_token(target, root)
        placeable = absolute or cwd is not None
        if place == _PLACE_IN and rel and _pathlike(rel) and placeable:
            writes.append(_under(rel, absolute, cwd))
        return " "

    return writes, _REDIRECT.sub(swallow, statement)


def _next_cwd(cwd: str | None, statement: str, root: str) -> str | None:
    """Where a statement's ``cd`` leaves the shell, as a repo-relative directory.

    Every relative token in a command is read against the session's cwd, because
    the orchestrator launches it in the repo root. A ``cd`` moves that ground,
    and the two directions it can move are not the same fact: into ``web/`` the
    tokens are still this repo's (``cat package.json`` is source, read under the
    new prefix), while out to ``~/.claude/projects`` they name nothing here at
    all — ``ls 9073*.jsonl`` there is not this repo's tree, and reading it as one
    would invent work and, with it, a missed prefetch doc.

    ``None`` is that second case: the cwd left the repo, or landed somewhere this
    reader cannot name (``cd -``, ``cd`` with no argument at all).
    """
    tokens = statement.split()
    for index, token in enumerate(tokens):
        if token != "cd" or cwd is None:
            continue
        if index + 1 >= len(tokens):
            return None
        place, rel, absolute = _place_token(tokens[index + 1], root)
        if place != _PLACE_IN or rel == "-":
            return None
        cwd = _under(rel, absolute, cwd)
    return cwd


def _under(rel: str, absolute: bool, cwd: str | None) -> str:
    """A token's repo-relative path once the shell's cwd is accounted for."""
    if absolute or not cwd:
        return rel
    return f"{cwd}/{rel}" if rel else cwd


def _listing_targets(
    statement: str, root: str, *, cwd: str | None = ""
) -> tuple[list[str], bool]:
    """``(in-repo trees a listing prints, whether an operand was unplaceable)``.

    Empty for anything but a listing head. A listing's operands are paths by
    definition, so they are read as such without the shape test a general argv
    token has to pass — that test exists to keep ``git status``'s subcommand from
    being read as a file, and it is what let ``ls tests`` pass as metadata. With
    no operand at all the listing prints the session's cwd, which is the repo
    root: the widest tree listing there is, and the shape the head-only reading
    called neutral.
    """
    if _statement_head(statement) not in LISTING_COMMANDS:
        return [], False
    operands = _head_operands(statement)
    if not operands:
        return [_under("", False, cwd)], False
    targets: list[str] = []
    unplaced = False
    for token in operands:
        place, rel, absolute = _place_token(token, root)
        if place == _PLACE_IN:
            targets.append(_under(rel, absolute, cwd))
        elif place == _PLACE_UNKNOWN:
            unplaced = True
    return targets, unplaced


def _names_outside(statement: str, root: str, *, cwd: str | None = "") -> bool:
    """Whether the statement names a path, and every one it names is outside.

    ``ls`` handles its own operands (:func:`_listing_targets`); this is the same
    question for every other command, and it is what keeps ``rm -rf /tmp/probe``
    or a read of a *different* repo from being read as work in this one.
    """
    outside = False
    for token in statement.replace("\n", " ").split():
        if token.startswith("-") or _ASSIGNMENT.match(token):
            continue
        place, rel, absolute = _place_token(token, root)
        if place == _PLACE_OUT and _pathlike(token.strip("\"'`();{}")):
            outside = True
        elif place == _PLACE_IN and rel and _pathlike(rel):
            return False
    return outside


def _head_operands(statement: str) -> list[str]:
    """The non-flag arguments of a statement's own command, before any pipe."""
    tokens = statement.split("|", 1)[0].split()
    while tokens and _ASSIGNMENT.match(tokens[0]):
        tokens = tokens[1:]
    return [token for token in tokens[1:] if not token.startswith("-")]


def _git_flag_verdict(statement: str) -> str:
    """Place a metadata ``git`` query by the flags it actually carries.

    The head says ``git log``; whether that prints commit subjects or the diff
    of every one of them is decided entirely by the flags. Only the statement's
    own flags are read — a pipe's tail carries its own (``| head -40``), and
    attributing those to the query would place it on the strength of another
    command's argv.
    """
    for token in statement.split("|", 1)[0].split():
        name = _flag_name(token)
        if name is None:
            continue
        if name in GIT_CONTENT_FLAGS:
            return COMMAND_WORK
        if name not in GIT_METADATA_FLAGS:
            return COMMAND_UNCLEAR
    return COMMAND_NEUTRAL


def _flag_name(token: str) -> str | None:
    """An argv token's flag name, or ``None`` when it is not a flag.

    Normalized so one spelling stands for one flag: an attached value is dropped
    (``--format='%h'``, ``-S"symbol"``), and a bare count is the count flag
    (``git log -8`` is ``-n``). Surrounding punctuation is stripped because
    commands reach this reader as they were typed, quotes and trailing commas
    included.
    """
    token = token.strip("\"'`,;")
    if not token.startswith("-") or token in ("-", "--"):
        return None
    name = token.split("=", 1)[0]
    if _NUMERIC_FLAG.match(name):
        return "-n"
    if not name.startswith("--") and len(name) > 2:
        return name[:2]
    return name


def _command_paths(
    command: str, repo_root: Path, *, cwd: str | None = ""
) -> list[str]:
    """Repo-relative paths a command names, as repo-relative POSIX strings.

    Textual, unlike :func:`_repo_rel`: a command token is a hint about intent,
    not a file this reader opens, and resolving one would put filesystem state
    from *now* into a judgment about *then*. Relative tokens are read against the
    repo because that is the cwd every orchestrator session is launched with,
    or against wherever a ``cd`` has since left it (``cwd``; ``None`` = outside
    the repo, where only absolute tokens still say anything).

    Two arguments are deliberately not read targets: ``-C``'s and ``cd``'s, which
    name a working directory. Counting ``git -C <repo> status`` as touching the
    repo root would make the single most common entry-checklist command in the
    measured corpus read as work.
    """
    root = repo_root.as_posix()
    paths: list[str] = []
    skip = False
    for token in command.replace("\n", " ").split():
        if skip:
            skip = False
            continue
        if token in ("-C", "cd"):
            skip = True
            continue
        if token.startswith("-") or _ASSIGNMENT.match(token):
            continue
        rel = _command_rel(token, root, cwd=cwd)
        if rel is not None:
            paths.append(rel)
    return paths


def _command_rel(token: str, root: str, *, cwd: str | None = "") -> str | None:
    """One argv token as a repo-relative path, or ``None`` when it is not one.

    Anything outside the repo drops out first — another checkout, ``/tmp``, a
    ``~``-relative path — because it says nothing about this repo's code. So does
    the repo root itself: naming it is not naming a file in it, and the one shape
    where it matters (a listing pointed at the whole tree) is read separately.
    """
    place, rel, absolute = _place_token(token, root)
    if place != _PLACE_IN or (cwd is None and not absolute):
        return None
    if not rel or not _pathlike(rel):
        return None
    return _under(rel, absolute, cwd)


def _place_token(token: str, root: str) -> tuple[str, str, bool]:
    """``(place, repo-relative path, named absolutely)`` for one argv token.

    The single place argv tokens are located, so every caller agrees on what
    counts as "in this repo". ``~`` and ``..`` are outside it: they resolve
    against the home directory and the repo's parent, neither of which is this
    repo's code, and reading them as repo-relative would invent work.
    """
    token = token.strip("\"'`();{}")
    if not token or "$" in token:
        return _PLACE_UNKNOWN, "", False
    if token.startswith("~"):
        return _PLACE_OUT, "", True
    if token in (root, f"{root}/"):
        return _PLACE_IN, "", True
    if token.startswith(f"{root}/"):
        return _PLACE_IN, token[len(root) + 1 :].rstrip("/"), True
    if token.startswith("/"):
        return _PLACE_OUT, "", True
    if token.startswith("./"):
        token = token[2:]
    if token in (".", "./"):
        return _PLACE_IN, "", False
    if token == ".." or token.startswith("../"):
        return _PLACE_OUT, "", False
    return _PLACE_IN, token.rstrip("/"), False


def _pathlike(token: str) -> bool:
    """Whether an argv token names a file or subtree at all.

    A token counts when it has a separator, names an entry-scope root outright,
    or carries a suffix. A bare glob (``find -name '*.md'``) is a pattern rather
    than a path, and a token holding a variable cannot be resolved to one
    without running the shell — neither may decide a verdict.
    """
    if not token or token in (".", "..") or "$" in token:
        return False
    if "*" in token or "?" in token:
        return "/" in token
    if "/" in token:
        return True
    if token in ENTRY_SCOPE_DIRS or token in ENTRY_SCOPE_NAMES:
        return True
    return "." in token.lstrip(".")


def _statement_head(statement: str) -> str | None:
    """One statement's command word, or ``None`` when it has none."""
    tokens = statement.split("|", 1)[0].split()
    while tokens and (_ASSIGNMENT.match(tokens[0]) or tokens[0] in _KEYWORDS):
        tokens = tokens[1:]
    if not tokens:
        return None
    head = tokens[0].strip("\"'`(){}").rsplit("/", 1)[-1]
    if head != "git":
        return head
    rest = iter(tokens[1:])
    for token in rest:
        if token == "-C":
            next(rest, None)
            continue
        if token.startswith("-"):
            continue
        return f"git {token}"
    return head


def _absorb_search(args: dict, scan: _Scan, *, order: int, repo_root: Path) -> None:
    """Count one tree search, unless its scope says it is not searching this code.

    An unscoped search runs from the session's cwd — the repo — so it is a search
    of the code and ends context assembly. A search scoped to the entry paths is
    routed memory or task lookup, which assembly legitimately does, and one
    scoped somewhere else on the machine is not this repo's work at all; neither
    is a signal here.
    """
    path = args.get("path")
    if isinstance(path, str) and path:
        rel = _repo_rel(path, repo_root)
        if rel is None or _entry_scope(rel):
            return
    scan.note_work(order, "searches")


def _absorb_tool_results(
    row: dict,
    scan: _Scan,
    *,
    order: int,
    actor: str,
    ts: str | None,
    repo_root: Path,
    task_paths: tuple[str, ...],
) -> None:
    """Credit the reads whose results landed in this row.

    The path comes from the call that was answered, not from the row-level result
    body: a row carries at most one result in practice, but attributing a body's
    ``filePath`` to a second block would credit a file that block never read.
    The body is only consulted when the request itself is unavailable (a
    transcript whose head was cut) and, for the task file, only when it agrees
    about which file it holds.

    A delivery of repo content from outside the entry scope is the session
    reaching into the code, which both counts as work and — as the moment the
    content actually entered the context — closes context assembly.
    """
    pending = scan.pending
    result = row.get("toolUseResult")
    result_file = result.get("file") if isinstance(result, dict) else None
    result_file = result_file if isinstance(result_file, dict) else {}
    result_rel = _repo_rel(result_file.get("filePath"), repo_root)
    for block in _content_blocks(row):
        if block.get("type") != "tool_result":
            continue
        requested, requested_at = pending.pop(block.get("tool_use_id"), (None, order))
        if block.get("is_error"):
            continue
        rel = requested or result_rel
        if rel is None:
            continue
        if not _entry_scope(rel):
            scan.note_work(order, "code_reads")
        scan.add_read(
            path=rel,
            order=order,
            requested_at=requested_at,
            actor=actor,
            method=METHOD_READ_TOOL,
            ts=ts,
        )
        if rel in task_paths and rel == result_rel and scan.task_content is None:
            scan.task_content = _whole_file_content(result_file)


def _absorb_attachment(
    row: dict,
    scan: _Scan,
    *,
    order: int,
    actor: str,
    ts: str | None,
    repo_root: Path,
    task_paths: tuple[str, ...],
) -> None:
    """Credit caller-injected content that names the file it carries.

    Matched by *shape* — a path plus content — rather than by attachment type, so
    a harness that starts pre-loading a session's prefetch set is credited without
    this reader needing to learn a new name first. That is the operational
    definition the measurement rests on: content in context counts, whether or not
    the session asked for it.
    """
    attachment = row.get("attachment")
    if not isinstance(attachment, dict):
        return
    content = attachment.get("content")
    if not isinstance(content, str) or not content:
        return
    rel = _repo_rel(attachment.get("path"), repo_root)
    if rel is None:
        return
    if not _entry_scope(rel):
        scan.note_work(order, "code_reads")
    scan.add_read(
        path=rel,
        order=order,
        requested_at=order,
        actor=actor,
        method=METHOD_INJECTED,
        ts=ts,
    )
    if rel in task_paths and scan.task_content is None and _complete_frontmatter(content):
        scan.task_content = content


def _closeout_command(row: dict) -> bool:
    """Whether a user row invokes the close-out skill as a slash command."""
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    text = content if isinstance(content, str) else None
    if text is None:
        text = "".join(
            block.get("text", "")
            for block in _content_blocks(row)
            if block.get("type") == "text"
        )
    return any(name == CLOSEOUT_SKILL for name in _COMMAND_NAME.findall(text))


def _content_blocks(row: dict) -> Iterable[dict]:
    """The row's message content blocks, or nothing for a non-block message."""
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ()
    return [block for block in content if isinstance(block, dict)]


def _whole_file_content(result_file: dict) -> str | None:
    """The file body from a read result, only when it carries whole frontmatter.

    An offset/limited read can return a slice with no frontmatter at all, and a
    line-capped one can return a frontmatter block cut open in the middle — both
    would parse as "declared fewer docs than it did", a false measurement rather
    than a missing one, and the false direction here invents misses. Only a body
    that starts at line 1 *and* closes its frontmatter fence is accepted; a later
    full read in the same session then supplies the content instead.
    """
    start = result_file.get("startLine")
    if start is not None and start != 1:
        return None
    content = result_file.get("content")
    if not isinstance(content, str) or not _complete_frontmatter(content):
        return None
    return content


def _complete_frontmatter(content: str) -> bool:
    """Whether ``content`` opens a ``---`` frontmatter fence and closes it.

    The frontmatter parser tolerates a missing closing fence (it simply consumes
    what is there), which is right for reading a file but wrong for deciding
    whether a *body* is complete: a task file cut mid-frontmatter would silently
    yield a shorter ``prefetch:`` set, or none.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    return any(line.strip() == "---" for line in lines[1:])


def _entry_scope(rel: str) -> bool:
    """Whether a repo-relative path is instructions rather than work material.

    Matches the directories themselves as well as what is inside them, since a
    search is scoped by naming the directory.
    """
    if rel.rsplit("/", 1)[-1] in ENTRY_SCOPE_NAMES:
        return True
    return any(rel == d or rel.startswith(f"{d}/") for d in ENTRY_SCOPE_DIRS)


def _repo_rel(value: object, repo_root: Path) -> str | None:
    """A read path as a repo-relative POSIX path, or ``None`` when outside the repo.

    Paths are resolved before comparison so a repo reached through a symlinked
    parent is not mistaken for an outside path; a file elsewhere on the machine
    is simply not this repo's memory and is dropped.
    """
    if not isinstance(value, str) or not value:
        return None
    resolved = _resolve(Path(value))
    if resolved is None:
        return None
    try:
        return resolved.relative_to(repo_root).as_posix()
    except ValueError:
        return None


def _resolve(path: Path) -> Path | None:
    """Fully resolve ``path``, or ``None`` when the filesystem refuses."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None
