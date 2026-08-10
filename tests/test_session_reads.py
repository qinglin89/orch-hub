"""Tests for the bounded per-session ``.ai/`` read-behavior reader.

Covers what the rollup has to get right: the *session-time* expected prefetch set
(never today's task file), the ruling's success condition for a read — content
entered context, before close-out, with no earlier write to the same path — one
ordered trace over declared and undeclared reads alike carrying the timing
evidence a judge needs, applicability decided by role rather than by whether
evidence happened to survive, the caps enforced at the read rather than after it,
and total degradation: an absent, unparseable, read-less, or *holed* transcript
yields an unassessable session with a detail, never a zero.

It also guards the shape of the model itself: no phase, no entry/work boundary,
and no interpretation of a shell command survives anywhere in the rollup.

Transcripts are fixtures under ``tmp_path`` in the CLI's real on-disk shape, so
no test reads an operator's real Claude home and none of them needs a running
agent.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from orch_hub.models import ManagedRepo
from orch_hub.session_reads import (
    ACTOR_ROOT,
    ACTOR_SUBAGENT,
    CLOSEOUT_SKILL,
    MAX_EXPECTED_DOCS,
    METHOD_INJECTED,
    METHOD_READ_TOOL,
    SOURCE_TRANSCRIPT_TASK_READ,
    Activity,
    ReadBehavior,
    SessionRef,
    _iter_rows,
    _Scan,
    locate_transcript,
    project_slug,
    read_session_behavior,
)

TASK = "2026-08-10-memory-read-effectiveness"
SID = "99c59409-eb38-42e4-923a-394a049456eb"
TS = "2026-08-10T02:00:00.000Z"

FEATURES = ".ai/features/evaluation.md"
SIDECAR = ".ai/modules/sidecar.md"
RUNTIME = ".ai/modules/runtime.md"
EAGER = ".ai/architecture.md"


def make_repo(tmp_path: Path, name: str = "alpha") -> ManagedRepo:
    path = tmp_path / "repos" / name
    path.mkdir(parents=True)
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def transcript_root(tmp_path: Path) -> Path:
    return tmp_path / "claude-home" / "projects"


def task_md(prefetch: str | None, *, status: str = "in_progress") -> str:
    """A task file as a session would have read it, with the given ``prefetch:``."""
    lines = [f"id: {TASK}", f"status: {status}", "session-est: 1/2"]
    if prefetch is not None:
        lines.append(f"prefetch: {prefetch}")
    return "---\n" + "\n".join(lines) + "\n---\n\n# Goal\n\nmeasure reads\n"


def write_transcript(tmp_path: Path, repo: ManagedRepo, rows: list[dict], *, sid: str = SID) -> Path:
    """Write ``rows`` as one JSONL transcript in Claude Code's on-disk layout."""
    path = transcript_root(tmp_path) / project_slug(repo.path) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def read_rows(
    abs_path: Path | str,
    *,
    content: str | None = None,
    sidechain: bool = False,
    uid: str = "t1",
    start_line: int = 1,
    is_error: bool = False,
) -> list[dict]:
    """A ``Read`` tool call plus its result, as the transcript records the pair.

    ``content=None`` omits ``toolUseResult`` entirely, which is how a transcript
    from a CLI version that does not persist read bodies looks — the requested
    path is then the only evidence of what was delivered.
    """
    call = {
        "type": "assistant",
        "isSidechain": sidechain,
        "timestamp": TS,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": uid,
                    "name": "Read",
                    "input": {"file_path": str(abs_path)},
                }
            ],
        },
    }
    block = {"type": "tool_result", "tool_use_id": uid, "content": "ok"}
    if is_error:
        block["is_error"] = True
    result: dict = {
        "type": "user",
        "isSidechain": sidechain,
        "timestamp": TS,
        "message": {"role": "user", "content": [block]},
    }
    if content is not None:
        result["toolUseResult"] = {
            "type": "text",
            "file": {
                "filePath": str(abs_path),
                "content": content,
                "startLine": start_line,
                "numLines": content.count("\n") + 1,
                "totalLines": content.count("\n") + 1,
            },
        }
    return [call, result]


def injected_row(abs_path: Path | str, content: str) -> dict:
    """A caller-injected memory attachment: a path plus the content it carries."""
    return {
        "type": "attachment",
        "timestamp": TS,
        "attachment": {
            "type": "nested_memory",
            "path": str(abs_path),
            "content": content,
            "displayPath": str(abs_path),
        },
    }


def entry_rows(repo: ManagedRepo, prefetch: str | None) -> list[dict]:
    """The session-entry task read that carries the session-time prefetch set."""
    return read_rows(
        repo.path / ".ai-tasks" / f"{TASK}.md", content=task_md(prefetch), uid="task"
    )


def work_rows(repo: ManagedRepo, *, uid: str = "code", path: str = "orch_hub/runs.py") -> list[dict]:
    """A read of repo source: work the reader counts as an anchor for later reads."""
    return read_rows(repo.path / path, content="def start_run():\n    ...\n", uid=uid)


def tool_row(name: str, args: dict, *, uid: str = "t") -> dict:
    """One assistant tool call, as the transcript records it."""
    return {
        "type": "assistant",
        "timestamp": TS,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": uid, "name": name, "input": args}],
        },
    }


def edit_rows(
    abs_path: Path | str,
    *,
    uid: str = "e",
    name: str = "Edit",
    new_string: str | None = None,
    is_error: bool = False,
    answered: bool = True,
) -> list[dict]:
    """An edit call plus the result that says whether it wrote, as recorded.

    ``is_error=True`` is a refused edit (``File has not been read yet``, a stale
    read, a missed match) and ``answered=False`` a call whose result never landed;
    both are calls that changed no file. An edit's result carries its ``filePath``
    at the top level rather than under ``file``, unlike a read's.
    """
    args: dict = {"file_path": str(abs_path)}
    if new_string is not None:
        args["new_string"] = new_string
    call = tool_row(name, args, uid=uid)
    if not answered:
        return [call]
    block = {"type": "tool_result", "tool_use_id": uid, "content": "ok"}
    if is_error:
        block["is_error"] = True
    return [
        call,
        {
            "type": "user",
            "timestamp": TS,
            "message": {"role": "user", "content": [block]},
            "toolUseResult": {"filePath": str(abs_path), "structuredPatch": []},
        },
    ]


def bash_row(command: str, *, uid: str = "sh") -> dict:
    return tool_row("Bash", {"command": command}, uid=uid)


def search_row(path: Path | str | None = None, *, uid: str = "g") -> dict:
    args: dict = {"pattern": "RunManager"}
    if path is not None:
        args["path"] = str(path)
    return tool_row("Grep", args, uid=uid)


def closeout_skill_row() -> dict:
    return tool_row("Skill", {"skill": CLOSEOUT_SKILL}, uid="sk")


def dev(run_id: str = "run-1", sid: str = SID) -> SessionRef:
    return SessionRef(session_id=sid, run_id=run_id, role="dev", kind="session")


def roll(tmp_path: Path, repo: ManagedRepo, refs, **kwargs) -> ReadBehavior:
    return read_session_behavior(
        repo,
        task_id=TASK,
        sessions=refs,
        transcript_root=transcript_root(tmp_path),
        **kwargs,
    )


def only(behavior: ReadBehavior):
    assert len(behavior.sessions) == 1
    return behavior.sessions[0]


# -- prefetch set read completeness -------------------------------------------


def test_a_fully_read_prefetch_set_is_complete_with_full_coverage(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}, {SIDECAR}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="b"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.expected == (FEATURES, SIDECAR)
    assert session.expected_source == SOURCE_TRANSCRIPT_TASK_READ
    assert session.read == (FEATURES, SIDECAR) and session.missing == ()
    assert (session.coverage, session.complete) == (1.0, True)
    assert session.available and session.assessable and session.applicable


def test_a_partly_read_prefetch_set_names_the_missing_doc(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}, {SIDECAR}, {RUNTIME}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # One of three is not compliance: the whole declared set is the expectation.
    assert session.read == (FEATURES,)
    assert session.missing == (SIDECAR, RUNTIME)
    assert (round(session.coverage, 4), session.complete) == (0.3333, False)


def test_a_declared_doc_read_late_is_still_read_and_says_when(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + work_rows(repo)
        + edit_rows(repo.path / "orch_hub" / "runs.py", name="Write", uid="w")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The ruling asks whether a qualifying read exists anywhere in the session,
    # never where it sits, so this is complete...
    assert session.complete is True and session.coverage == 1.0
    # ...and the evidence that it came after the code work is in the trace, for
    # the semantic layer to call late if it judges it so.
    read = session.trace[0]
    assert read.declared_prefetch is True
    assert read.before == Activity(code_reads=1, edits=1)
    assert session.activity == Activity(code_reads=1, edits=1)


def test_an_empty_declared_set_is_recovered_but_not_applicable(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(tmp_path, repo, entry_rows(repo, "[]"))

    session = only(roll(tmp_path, repo, [dev()]))

    # Nothing to comply with is a measurement, not a missing one.
    assert session.expected == () and session.assessable is True
    assert session.applicable is False
    assert session.coverage is None and session.complete is None


def test_a_review_session_owes_no_prefetch_but_its_missing_trace_is_recorded(tmp_path):
    repo = make_repo(tmp_path)  # a Codex-backed review session leaves no transcript

    behavior = roll(
        tmp_path, repo, [SessionRef(session_id=SID, run_id="r", role="review")]
    )
    session = only(behavior)

    # It was never asked to pre-load anything, so no prefetch obligation is
    # invented for it...
    assert session.applicable is False and session.assessable is False
    # ...but its `.ai/` reads are trace evidence all the same, and nothing was
    # read from it, so the loss is on the record rather than silently absent.
    assert behavior.unobserved_sessions == 1
    assert any("left no readable transcript" in w for w in behavior.warnings)


def test_a_parsed_transcript_that_yields_no_verdict_warns_whatever_the_role(tmp_path):
    repo = make_repo(tmp_path)
    # A review session that did leave a transcript, and never read its task file:
    # nothing was expected of it, but its reads *are* trace evidence, and the
    # layer that judges them cannot tell a short trace from a whole one unless the
    # section says the session could not be read out.
    write_transcript(tmp_path, repo, read_rows(repo.path / SIDECAR, content="# s"))

    behavior = roll(
        tmp_path, repo, [SessionRef(session_id=SID, run_id="r", role="review")]
    )
    session = only(behavior)

    assert session.available is True and session.assessable is False
    assert [r.path for r in session.trace] == [SIDECAR]
    assert any(SID in w for w in behavior.warnings)


def test_a_plan_gate_session_shares_the_dev_prefetch_expectation(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{FEATURES}]"))

    session = only(
        roll(
            tmp_path,
            repo,
            [SessionRef(session_id=SID, run_id="r", role="dev", kind="plan-gate")],
        )
    )

    # A plan-gate turn composes on top of the dev prompt, so it inherits the
    # instruction: role decides, not kind.
    assert session.applicable is True and session.complete is False


# -- what counts as a read ----------------------------------------------------


def test_caller_injected_content_counts_without_a_read_tool_call(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]") + [injected_row(repo.path / FEATURES, "# eval")],
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The definition is "content was in context", not "the session asked for it".
    assert session.complete is True
    assert session.docs[0].method == METHOD_INJECTED


def test_a_read_tool_delivery_is_credited_and_labelled(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.docs[0].method == METHOD_READ_TOOL
    assert session.docs[0].order == 3


def test_a_read_result_without_a_body_still_credits_the_requested_path(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content=None, uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # A backend that does not persist read bodies still shows what was asked for
    # and answered, which is the whole of the claim.
    assert session.complete is True


def test_a_failed_read_is_not_credited(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content=None, uid="a", is_error=True),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.missing == (FEATURES,) and session.complete is False
    assert session.trace == ()


def test_an_unanswered_read_call_is_not_credited(tmp_path):
    repo = make_repo(tmp_path)
    call, _ = read_rows(repo.path / FEATURES, content="# eval", uid="a")
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{FEATURES}]") + [call])

    session = only(roll(tmp_path, repo, [dev()]))

    # A turn cut off before its result put nothing into the context.
    assert session.missing == (FEATURES,) and session.complete is False


def test_reads_outside_the_repo_are_ignored(tmp_path):
    repo = make_repo(tmp_path)
    other = tmp_path / "elsewhere" / "notes.md"
    other.parent.mkdir(parents=True)
    other.write_text("x", encoding="utf-8")
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]") + read_rows(other, content="x", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.missing == (FEATURES,)
    assert session.trace == () and session.activity == Activity()


def test_a_doc_this_session_wrote_first_is_a_read_back_not_a_consultation(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + edit_rows(repo.path / FEATURES, name="Write", uid="w")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Reading back text this session just wrote is not use of existing project
    # memory — but the read-back itself is recorded rather than silently dropped.
    assert session.missing == (FEATURES,) and session.complete is False
    assert session.trace == ()
    assert session.writeback_excluded == (FEATURES,)


def test_read_then_write_counts_and_carries_the_later_write(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a")
        + edit_rows(repo.path / FEATURES, uid="w")
        + read_rows(repo.path / FEATURES, content="# eval", uid="b"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Existing knowledge used, then the memory updated: the read counts, and the
    # update is evidence of read-then-update rather than a reason to drop it.
    # The re-read afterwards changes nothing — the qualifying read already stands,
    # so calling it a read-back would contradict the trace it sits in.
    assert session.complete is True and session.writeback_excluded == ()
    assert len(session.trace) == 1
    read = session.trace[0]
    # The write is anchored where its result confirmed it, one row past the call.
    assert read.order == 3 and read.first_write == 5


def test_a_refused_edit_of_a_doc_does_not_make_a_later_read_a_read_back(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + edit_rows(repo.path / FEATURES, name="Write", uid="w", is_error=True)
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # An edit the tool refused wrote nothing, so nothing preceded this read: it is
    # an ordinary consultation of memory the session did not author.
    assert session.read == (FEATURES,) and session.missing == ()
    assert (session.coverage, session.complete) == (1.0, True)
    assert session.assessable and session.writeback_excluded == ()
    assert [read.path for read in session.trace] == [FEATURES]
    assert session.trace[0].first_write is None
    # The attempt is still an anchor: what the session was doing does not depend
    # on whether the tool let it.
    assert session.activity.edits == 1


def test_an_unanswered_edit_of_a_doc_withholds_the_verdict_but_keeps_the_read(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + edit_rows(repo.path / FEATURES, name="Write", uid="w", answered=False)
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Whether that edit wrote is simply unknown, so the read cannot be told from a
    # read-back. Unknown is not a miss: the read stands as evidence and only the
    # verdict is withheld.
    assert session.available and not session.assessable
    assert (session.coverage, session.complete) == (None, None)
    assert session.writeback_excluded == ()
    assert [read.path for read in session.trace] == [FEATURES]
    assert "in flight" in (session.detail or "")
    # The same doubt reaches both consumers: the declared-set verdict here, and
    # the read the semantic layer will weigh.
    assert session.trace[0].write_uncertain is True


def test_an_edit_result_landing_after_the_read_it_raced_withholds_the_verdict(tmp_path):
    repo = make_repo(tmp_path)
    call, result = edit_rows(repo.path / FEATURES, name="Write", uid="w")
    read_call, read_result = read_rows(repo.path / FEATURES, content="# eval", uid="a")
    write_transcript(
        tmp_path,
        repo,
        # Both issued in one batch: the read's result lands first, so whether the
        # file had already changed under it is not settled by their order.
        entry_rows(repo, f"[{FEATURES}]") + [call, read_call, read_result, result],
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.available and not session.assessable
    assert (session.coverage, session.complete) == (None, None)
    assert [read.path for read in session.trace] == [FEATURES]


def test_an_unsettled_write_marks_its_read_even_when_no_verdict_speaks_for_it(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        # The declared set is read cleanly; the unsettled edit is of a doc the
        # task never declared, so the prefetch verdict never looks at it.
        entry_rows(repo, f"[{SIDECAR}]")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="s")
        + edit_rows(repo.path / FEATURES, name="Write", uid="w", answered=False)
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Compliance with the declared set is measured and unharmed: an unsettled
    # write elsewhere is not evidence about the docs the task asked for.
    assert (session.coverage, session.complete) == (1.0, True)
    assert session.assessable is True
    # But the trace is what the semantic layer judges, declared or not, so the
    # undeclared read must not pass for settled — its ordering against that edit
    # is exactly as unresolved as a declared doc's would have been.
    declared, undeclared = session.trace
    assert (declared.path, declared.write_uncertain) == (SIDECAR, False)
    assert (undeclared.path, undeclared.write_uncertain) == (FEATURES, True)


def test_a_racing_edit_marks_the_read_it_raced_and_still_shows_where_it_landed(tmp_path):
    repo = make_repo(tmp_path)
    call, result = edit_rows(repo.path / FEATURES, name="Write", uid="w")
    read_call, read_result = read_rows(repo.path / FEATURES, content="# eval", uid="a")
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{SIDECAR}]")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="s")
        + [call, read_call, read_result, result],
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The write did land, after the read's result — so both facts travel: where it
    # was confirmed, and that their order does not settle which came first.
    read = session.trace[1]
    assert read.path == FEATURES and read.write_uncertain is True
    assert read.first_write is not None and read.first_write > read.order
    assert (session.coverage, session.complete) == (1.0, True)


def test_a_refused_edit_leaves_its_reads_ordering_settled(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{SIDECAR}]")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="s")
        + edit_rows(repo.path / FEATURES, name="Write", uid="w", is_error=True)
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # An edit that came back refused wrote nothing, so there is no doubt to carry:
    # False here is a settled fact, not an unrecorded one.
    assert [r.write_uncertain for r in session.trace] == [False, False]


def test_an_edit_of_another_doc_leaves_this_read_alone(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + edit_rows(repo.path / SIDECAR, name="Write", uid="w", answered=False)
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The clause is per path: an unsettled write to a different doc says nothing
    # about this one.
    assert (session.coverage, session.complete) == (1.0, True)
    assert session.assessable


def test_reads_after_the_closeout_skill_do_not_count_as_consultation(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [closeout_skill_row()]
        + read_rows(repo.path / FEATURES, content="# eval", uid="late")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="later"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Absorption walks .ai/ to update it; that is the write path, not knowledge
    # used while working.
    assert session.closeout_seen is True
    assert session.complete is False and session.trace == ()


def test_a_closeout_slash_command_ends_the_scan_too(tmp_path):
    repo = make_repo(tmp_path)
    command = {
        "type": "user",
        "timestamp": TS,
        "message": {
            "role": "user",
            "content": f"<command-name>/{CLOSEOUT_SKILL}</command-name>",
        },
    }
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [command]
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="late"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.closeout_seen is True and session.trace == ()


def test_subagent_reads_count_as_consultation_and_are_attributed(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="b", sidechain=True),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Consultation by delegation is still consultation.
    assert [(r.path, r.actor) for r in session.trace] == [
        (FEATURES, ACTOR_ROOT),
        (SIDECAR, ACTOR_SUBAGENT),
    ]


def test_a_subagent_read_can_satisfy_a_declared_doc(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a", sidechain=True),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.complete is True


# -- the session-time expected set --------------------------------------------


def test_the_expected_set_is_the_session_time_one_not_the_current_task_file(tmp_path):
    repo = make_repo(tmp_path)
    task = repo.path / ".ai-tasks" / f"{TASK}.md"
    task.parent.mkdir(parents=True)
    # Today's file declares three docs; this session only ever saw one.
    task.write_text(task_md(f"[{FEATURES}, {SIDECAR}, {RUNTIME}]"), encoding="utf-8")
    write_transcript(
        tmp_path,
        repo,
        read_rows(task, content=task_md(f"[{FEATURES}]"), uid="task")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Scoring an old session against a later, grown prefetch list would invent
    # both false misses and false credit.
    assert session.expected == (FEATURES,) and session.complete is True


def test_a_session_that_never_read_its_task_file_is_unassessable(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path, repo, read_rows(repo.path / FEATURES, content="# eval", uid="a")
    )

    behavior = roll(tmp_path, repo, [dev()])
    session = only(behavior)

    assert session.available is True and session.assessable is False
    assert session.applicable is None  # applicability itself is unknown
    assert session.coverage is None and session.complete is None
    assert "expected set unrecoverable" in session.detail
    assert behavior.warnings


def test_an_offset_task_read_does_not_supply_the_expected_set(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        read_rows(
            repo.path / ".ai-tasks" / f"{TASK}.md",
            content="## Session log\n",
            uid="task",
            start_line=200,
        ),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # A slice from the middle carries no frontmatter and would read as "declared
    # nothing", which is a false measurement rather than a missing one.
    assert session.assessable is False and session.expected == ()


def test_a_task_read_cut_mid_frontmatter_does_not_supply_the_expected_set(tmp_path):
    repo = make_repo(tmp_path)
    body = task_md(f"[{FEATURES}, {SIDECAR}]")
    write_transcript(
        tmp_path,
        repo,
        read_rows(
            repo.path / ".ai-tasks" / f"{TASK}.md",
            content=body.split("prefetch:")[0],  # fence never closes
            uid="task",
        )
        + read_rows(repo.path / FEATURES, content="# eval"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.assessable is False and session.expected == ()
    assert "expected set unrecoverable" in session.detail


def test_an_archived_task_read_also_supplies_the_expected_set(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        read_rows(
            repo.path / ".ai-tasks" / "archive" / f"{TASK}.md",
            content=task_md(f"[{FEATURES}]"),
            uid="task",
        )
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.expected == (FEATURES,) and session.complete is True


def test_an_injected_task_file_supplies_the_expected_set(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        [injected_row(repo.path / ".ai-tasks" / f"{TASK}.md", task_md(f"[{FEATURES}]"))]
        + [injected_row(repo.path / FEATURES, "# eval")],
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.expected == (FEATURES,) and session.complete is True
    assert session.docs[0].method == METHOD_INJECTED


def test_malformed_prefetch_entries_are_excluded_and_explained(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}, ../escape.md, ]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # A malformed entry is not a doc that could have been read, so it must not
    # sit in the denominator — but the gap against the raw frontmatter is named.
    assert session.expected == (FEATURES,) and session.complete is True
    assert "malformed prefetch entry ignored" in session.detail


def test_a_declared_eager_doc_is_not_something_a_session_can_be_missing(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}, {EAGER}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # prefetch: is lazy-only by the schema, but tasks do declare eager docs — and
    # the eager set reaches a session through the system prompt, which the
    # transcript never records. Scoring it missing would penalize a session for
    # a doc the loading contract had already put in its context.
    assert session.expected == (FEATURES,)
    assert session.missing == () and session.complete is True
    assert "eager doc(s) declared in prefetch ignored" in session.detail


def test_a_wholly_eager_declared_set_leaves_nothing_to_comply_with(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{EAGER}]"))

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.expected == () and session.applicable is False
    assert session.assessable is True and session.complete is None


def test_an_oversize_declared_set_is_refused_rather_than_partially_measured(tmp_path):
    repo = make_repo(tmp_path)
    docs = [f".ai/modules/m{n}.md" for n in range(MAX_EXPECTED_DOCS + 1)]
    write_transcript(tmp_path, repo, entry_rows(repo, "[" + ", ".join(docs) + "]"))

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.assessable is False and session.coverage is None
    assert "exceeds the" in session.detail


# -- the ordered trace --------------------------------------------------------


def test_the_trace_is_one_ordered_sequence_over_declared_and_undeclared_reads(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a")
        + read_rows(repo.path / EAGER, content="# arch", uid="b")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="c")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="d"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # One trace, attribution as an attribute: the declared doc and the one the
    # session went and found are the same kind of event.
    assert [(r.path, r.declared_prefetch) for r in session.trace] == [
        (FEATURES, True),
        (SIDECAR, False),
    ]
    assert session.undeclared_reads == (SIDECAR,)
    # The eager set is in every session's context by the loading contract, so
    # reading it is not retrieval; a re-read of the same doc adds nothing.
    assert EAGER not in [r.path for r in session.trace]


def test_no_undeclared_reads_is_not_a_defect(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # A sufficient prefetch set with no new need arising is correct behavior.
    assert session.undeclared_reads == ()
    assert session.complete is True and session.assessable is True


def test_the_trace_survives_an_unrecoverable_expected_set(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        read_rows(repo.path / SIDECAR, content="# sidecar", uid="a")
        + read_rows(repo.path / RUNTIME, content="# runtime", uid="b"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Which docs were read and in what order stands on its own evidence; only
    # membership in the declared set becomes unknown.
    assert [r.path for r in session.trace] == [SIDECAR, RUNTIME]
    assert [r.declared_prefetch for r in session.trace] == [None, None]
    assert session.undeclared_reads == ()  # unknown is not "undeclared"
    assert session.assessable is False


def test_the_trace_is_capped_and_says_so(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}]")
    for n in range(6):
        rows += read_rows(repo.path / f".ai/modules/m{n}.md", content="x", uid=f"u{n}")
    write_transcript(tmp_path, repo, rows)

    behavior = roll(tmp_path, repo, [dev()], max_reads=3)
    session = only(behavior)

    assert len(session.trace) == 3 and session.trace_truncated is True
    # The session was still measured — the cap cuts the trace, not the verdict —
    # but the list a judge reads stops early, and nothing downstream can tell a
    # short trace from a whole one unless the section says so.
    assert session.assessable is True and session.complete is not None
    assert any("trace cut at the event cap" in w for w in behavior.warnings)


# -- timing evidence, not a timing verdict ------------------------------------


def test_each_read_carries_the_work_done_before_it(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, "[]")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="early")
        + work_rows(repo)
        + [search_row(uid="g"), bash_row("pytest -q", uid="sh")]
        + edit_rows(repo.path / "orch_hub" / "runs.py", uid="w")
        + edit_rows(repo.path / ".ai-tasks" / f"{TASK}.md", uid="tw")
        + read_rows(repo.path / RUNTIME, content="# runtime", uid="late"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    early, late = session.trace
    # Read-then-do and do-then-read are different behaviors that a bare set of
    # paths cannot distinguish; the counts are what survives the transcript.
    assert early.before == Activity()
    assert late.before == Activity(code_reads=1, searches=1, edits=1, task_edits=1, shell=1)
    assert session.activity == late.before  # nothing happened after the late read


def test_a_shell_command_is_counted_never_interpreted(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [bash_row("date -u", uid="s1"), bash_row("sed -n 1,40p orch_hub/api.py", uid="s2")]
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # A clock query and a source read are one fact here — a command ran. Deciding
    # which of them was work is the shell approximation the ruling deleted, and
    # nothing about the verdict turns on it.
    assert session.activity == Activity(shell=2)
    assert session.complete is True
    assert session.trace[0].before == Activity(shell=2)


def test_a_search_scoped_to_the_instructions_is_not_counted_as_work(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, "[]")
        + [search_row(repo.path / ".ai-tasks", uid="g1")]
        + [search_row(tmp_path / "elsewhere", uid="g2")]
        + [search_row(uid="g3")]
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Routed task/memory lookup and another tree's search are not this repo's
    # work; an unscoped search runs from the repo and is.
    assert session.activity == Activity(searches=1)
    assert session.trace[0].before == Activity(searches=1)


def test_the_rollup_exposes_no_phase_boundary_or_command_classification(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [bash_row("git log -p -1 && ls tests", uid="s1")]
        + read_rows(repo.path / FEATURES, content="# eval", uid="a")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="b"),
    )

    behavior = roll(tmp_path, repo, [dev()])
    payload = json.dumps(behavior.to_dict())

    for gone in (
        "phase",
        "entry",
        "boundary",
        "midflight",
        "late",
        "unclear",
        "neutral",
    ):
        assert gone not in payload
    # The command decides nothing: the doc read after it is read, full stop.
    assert only(behavior).complete is True


# -- degradation --------------------------------------------------------------


def test_an_absent_transcript_is_unassessable_never_incomplete(tmp_path):
    repo = make_repo(tmp_path)  # nothing written under the transcript root

    behavior = roll(tmp_path, repo, [dev()])
    session = only(behavior)

    assert behavior.available is False
    assert session.available is False and session.assessable is False
    assert session.complete is None and session.coverage is None
    assert session.detail == "no session transcript found"
    assert behavior.warnings and "no session transcript found" in behavior.warnings[0]


def test_an_unparseable_transcript_yields_no_facts(tmp_path):
    repo = make_repo(tmp_path)
    path = transcript_root(tmp_path) / project_slug(repo.path) / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not json\n{also not\n", encoding="utf-8")

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.assessable is False and session.events_scanned == 0
    assert "no readable events" in session.detail


def test_a_backend_that_exposes_no_reads_is_unassessable_not_scratch_first(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        [
            {"type": "user", "timestamp": TS, "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "timestamp": TS,
             "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
        ],
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.available is True and session.assessable is False
    assert session.complete is None


def test_lost_evidence_refuses_a_missing_verdict_wherever_the_hole_sits(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}, {SIDECAR}]")
    rows += read_rows(repo.path / FEATURES, content="# eval", uid="a")
    path = write_transcript(tmp_path, repo, rows)
    # Damage at the very end, long after both reads: a read is positive evidence,
    # so what was lost can only ever hide the read that would have found SIDECAR.
    path.write_text(
        path.read_text(encoding="utf-8") + "{not json\n" + '"a string row"\n',
        encoding="utf-8",
    )

    behavior = roll(tmp_path, repo, [dev()])
    session = only(behavior)

    assert session.skipped_rows == 2
    assert session.assessable is False
    assert session.coverage is None and session.complete is None
    assert session.applicable is True  # it was still a session we should measure
    assert session.read == (FEATURES,) and session.missing == (SIDECAR,)
    assert "could have been read in what was lost" in session.detail
    assert behavior.warnings


def test_lost_evidence_before_a_read_refuses_that_docs_verdict(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}]")
    path = write_transcript(tmp_path, repo, rows)
    later = "".join(
        json.dumps(row) + "\n"
        for row in read_rows(repo.path / FEATURES, content="# eval", uid="a")
    )
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n" + later, "utf-8")

    session = only(roll(tmp_path, repo, [dev()]))

    # Nothing is missing, but the lost row sits before the read that satisfied
    # the set: it could have carried an earlier write to that doc, or the
    # close-out marker that would have ended the scan before it.
    assert session.read == (FEATURES,) and session.missing == ()
    assert session.assessable is False and session.complete is None
    assert "before a declared doc's read" in session.detail


def test_lost_evidence_after_every_read_leaves_a_complete_verdict_standing(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}]")
    rows += read_rows(repo.path / FEATURES, content="# eval", uid="a")
    rows += work_rows(repo)
    path = write_transcript(tmp_path, repo, rows)
    text = path.read_text(encoding="utf-8")
    # Mixed damage, all of it after the declared doc was read: a malformed row, a
    # row past the per-row cap, and a byte budget that cuts the last row in half.
    damaged = (
        "{oops\n" + json.dumps({"type": "user", "pad": "x" * 9000}) + "\n" + "{tail\n"
    )
    path.write_text(text + damaged, encoding="utf-8")

    behavior = roll(
        tmp_path,
        repo,
        [dev()],
        max_bytes=len(text) + len(damaged) - 3,
        max_line_bytes=4000,
    )
    session = only(behavior)

    assert (session.skipped_rows, session.truncated) == (3, True)
    # A doc that was read stays read: no later damage can un-deliver it, and
    # nothing was reported missing that the damage could have been hiding.
    assert session.assessable is True and session.complete is True
    # The verdict standing is not the same as the evidence being whole: what was
    # lost could have held a read this trace never lists, so the loss is recorded
    # for the layer that judges the trace rather than the verdict.
    assert any(
        "measured over incomplete evidence" in w and "unreadable row(s)" in w
        for w in behavior.warnings
    )


def test_blank_rows_are_not_evidence_holes(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}]") + read_rows(repo.path / FEATURES, content="# eval")
    path = write_transcript(tmp_path, repo, rows)
    path.write_text(path.read_text(encoding="utf-8") + "\n   \n", encoding="utf-8")

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.skipped_rows == 0
    assert session.assessable is True and session.complete is True


def test_no_sessions_reads_nothing_and_says_why(tmp_path):
    repo = make_repo(tmp_path)

    behavior = roll(tmp_path, repo, [])

    assert behavior.available is False and behavior.sessions == ()
    assert "no persisted session ids" in behavior.detail
    assert behavior.warnings == ()


def test_section_availability_follows_what_was_actually_read(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{FEATURES}]"))

    behavior = roll(
        tmp_path, repo, [dev(), SessionRef(session_id="missing-sid", role="dev")]
    )

    assert behavior.available is True  # one of the two was located
    assert [s.available for s in behavior.sessions] == [True, False]
    assert behavior.assessable_count == 1 and behavior.applicable_count == 1


def test_an_observed_dev_session_beside_a_missing_review_one_is_partial_evidence(
    tmp_path,
):
    repo = make_repo(tmp_path)
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{FEATURES}]"))

    behavior = roll(
        tmp_path,
        repo,
        [dev(), SessionRef(session_id="review-sid", run_id="run-1", role="review")],
    )
    measured, unread = behavior.sessions

    # The task's ordinary shape under a two-backend orchestrator, and the section
    # reads as available on the strength of the one transcript that parsed. What
    # the other session read is simply not here, and only this note says so —
    # without it a half-observed task prices identically to a whole one.
    assert behavior.available is True
    assert measured.assessable is True and unread.available is False
    assert behavior.unobserved_sessions == 1
    assert [w for w in behavior.warnings if "left no readable transcript" in w] == [
        "read behavior: 1 session(s) left no readable transcript, so their `.ai/`"
        " reads are absent from this evidence (no session transcript found)"
    ]


def test_unobserved_sessions_are_one_note_however_many_there_are(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{FEATURES}]"))
    reviews = [
        SessionRef(session_id=f"review-{n}", run_id="run-1", role="review")
        for n in range(4)
    ]

    behavior = roll(tmp_path, repo, [dev(), *reviews])

    # One structural fact — this backend leaves no transcript — priced once. A
    # warning each would spend the semantic layer's whole warning budget on it and
    # leave the degradations that differ between tasks unable to move confidence.
    assert behavior.unobserved_sessions == 4
    assert len([w for w in behavior.warnings if "left no readable" in w]) == 1
    assert "4 session(s)" in behavior.warnings[0]


def test_repeated_session_ids_are_rolled_up_once(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{FEATURES}]"))

    behavior = roll(tmp_path, repo, [dev(run_id="run-1"), dev(run_id="run-2")])

    assert [s.run_id for s in behavior.sessions] == ["run-1"]


def test_sessions_past_the_cap_are_dropped_with_a_warning(tmp_path):
    repo = make_repo(tmp_path)
    refs = [dev(sid=f"sid-{n}") for n in range(4)]
    for ref in refs:
        write_transcript(tmp_path, repo, entry_rows(repo, f"[{FEATURES}]"), sid=ref.session_id)

    behavior = roll(tmp_path, repo, refs, max_sessions=2)

    assert len(behavior.sessions) == 2
    assert any("dropped past" in warning for warning in behavior.warnings)
    # The cap bounds the rollups, not the truth: a dropped session still happened,
    # and its role already settles whether it owed a pre-load — so a downstream
    # denominator can keep counting it instead of narrowing to what fit.
    assert behavior.dropped_sessions == 2 and behavior.dropped_candidates == 2
    assert any("a role the prefetch instruction reaches" in w for w in behavior.warnings)


def test_a_dropped_session_with_nothing_to_comply_with_is_not_a_candidate(tmp_path):
    repo = make_repo(tmp_path)
    kept = dev(sid="sid-0")
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{FEATURES}]"), sid=kept.session_id)
    refs = [
        kept,
        SessionRef(session_id="sid-r", run_id="run-1", role="review"),
        dev(sid="sid-2"),
    ]

    behavior = roll(tmp_path, repo, refs, max_sessions=1)

    # Two dropped, but the review session was never asked to pre-load anything:
    # counting it as an unmeasured candidate would invent an obligation the cap
    # merely happened to hide.
    assert behavior.dropped_sessions == 2 and behavior.dropped_candidates == 1


# -- containment and caps -----------------------------------------------------


def test_a_crafted_session_id_cannot_escape_the_transcript_store(tmp_path):
    repo = make_repo(tmp_path)
    root = transcript_root(tmp_path)
    root.mkdir(parents=True)
    (tmp_path / "secret.jsonl").write_text("{}\n", encoding="utf-8")

    assert locate_transcript(root, repo, "../../secret") is None
    assert locate_transcript(root, repo, "") is None
    assert locate_transcript(root, repo, "a/b") is None


def test_locate_transcript_resolves_the_cwd_slug(tmp_path):
    repo = make_repo(tmp_path)
    path = write_transcript(tmp_path, repo, [])

    assert locate_transcript(transcript_root(tmp_path), repo, SID) == path.resolve()
    assert locate_transcript(transcript_root(tmp_path), repo, "other") is None


def test_project_slug_matches_the_cli_layout():
    assert project_slug(Path("/Users/x/workplace/github.com/y/orch-hub")) == (
        "-Users-x-workplace-github-com-y-orch-hub"
    )


def test_an_oversize_row_is_skipped_rather_than_parsed(tmp_path):
    repo = make_repo(tmp_path)
    huge = {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": "x" * 50_000}]}}
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [huge]
        + read_rows(repo.path / FEATURES, content="# eval"),
    )

    session = only(roll(tmp_path, repo, [dev()], max_line_bytes=4000))

    # The giant row is dropped whole; the rows on either side are still read, so
    # the scan resumed on a real row boundary rather than on fragments.
    assert session.events_scanned == 4 and session.skipped_rows == 1
    assert session.read == (FEATURES,)
    # But the hole precedes that read, so the observation is reported while the
    # verdict stays unclaimed.
    assert session.assessable is False and session.complete is None


def test_the_byte_budget_stops_the_scan_and_is_recorded(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}]")
    for n in range(50):
        rows += read_rows(repo.path / f".ai/features/f{n}.md", content="x" * 200, uid=f"u{n}")
    write_transcript(tmp_path, repo, rows)

    session = only(roll(tmp_path, repo, [dev()], max_bytes=2000))

    assert session.truncated is True
    assert session.events_scanned < len(rows)
    # The cut tail could hold the read that would have found the declared doc.
    assert session.assessable is False and session.complete is None
    assert session.missing == (FEATURES,)  # the observation survives the refusal


def test_iter_rows_never_reads_past_the_byte_budget(tmp_path):
    """The bound is on the read itself, not on a buffer already in memory."""
    payload = ("{}\n" * 5000).encode()
    stream = io.BytesIO(payload)
    scan = _Scan()

    consumed = sum(len(raw) for raw, _ in _iter_rows(stream, scan, max_bytes=300,
                                                     max_line_bytes=64))

    assert consumed <= 300
    assert stream.tell() <= 301  # the one-byte truncation probe
    assert scan.truncated is True


# -- what the rollup must not carry -------------------------------------------


def test_the_rollup_carries_no_transcript_text(tmp_path):
    repo = make_repo(tmp_path)
    secret = "SENSITIVE-ANSWER-TEXT"
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [{"type": "user", "timestamp": TS, "message": {"role": "user", "content": secret}}]
        + read_rows(repo.path / FEATURES, content=f"# eval {secret}"),
    )

    serialized = json.dumps(roll(tmp_path, repo, [dev()]).to_dict())

    assert secret not in serialized
    assert FEATURES in serialized  # paths and counts are the whole payload
