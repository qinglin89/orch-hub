"""Tests for the bounded per-session ``.ai/`` read-behavior reader.

Covers what the rollup has to get right: the *session-time* expected prefetch set
(never today's task file), an operational definition of "read" that credits
caller-injected delivery and refuses a failed tool call, the observable boundary
that keeps entry pre-loading and mid-flight retrieval from collapsing into each
other, an ordered trace that excludes the eager set and the close-out write path,
applicability that is decided by role rather than by whether evidence happened to
survive, the caps — enforced at the read, not after it — and total degradation:
an absent, unparseable, read-less, or *holed* transcript yields an unassessable
session with a detail, never a zero.

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
    PHASE_ENTRY,
    PHASE_WORK,
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
    """A read of repo source: the observable moment context assembly ends."""
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


def edit_row(
    abs_path: Path | str,
    *,
    uid: str = "e",
    name: str = "Edit",
    new_string: str | None = None,
) -> dict:
    args: dict = {"file_path": str(abs_path)}
    if new_string is not None:
        args["new_string"] = new_string
    return tool_row(name, args, uid=uid)


def bash_row(command: str, *, uid: str = "sh") -> dict:
    return tool_row("Bash", {"command": command}, uid=uid)


def search_row(path: Path | str | None = None, *, uid: str = "g") -> dict:
    args: dict = {"pattern": "RunManager"}
    if path is not None:
        args["path"] = str(path)
    return tool_row("Grep", args, uid=uid)


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


# -- entry prefetch completeness ----------------------------------------------


def test_complete_prefetch_load_is_complete_with_full_coverage(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}, {SIDECAR}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="b"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.available is True and session.assessable is True
    assert session.applicable is True
    assert session.expected == (FEATURES, SIDECAR)
    assert session.expected_source == SOURCE_TRANSCRIPT_TASK_READ
    assert session.loaded == (FEATURES, SIDECAR)
    assert session.missing == ()
    assert session.coverage == 1.0 and session.complete is True


def test_partial_prefetch_load_is_not_complete_and_names_the_missing_doc(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}, {SIDECAR}, {RUNTIME}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Any-intersection is not compliance: one of three loaded is incomplete.
    assert session.complete is False
    assert session.coverage == 1 / 3
    assert session.loaded == (FEATURES,)
    assert session.missing == (SIDECAR, RUNTIME)


def test_caller_injected_prefetch_counts_without_a_read_tool_call(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]") + [injected_row(repo.path / FEATURES, "# eval")],
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.complete is True
    assert session.loads[0].method == METHOD_INJECTED


def test_read_tool_delivery_is_credited_and_labelled(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]") + read_rows(repo.path / FEATURES, content="# eval"),
    )

    load = only(roll(tmp_path, repo, [dev()])).loads[0]

    assert (load.loaded, load.method) == (True, METHOD_READ_TOOL)
    assert load.order is not None


def test_a_read_result_without_a_body_still_credits_the_requested_path(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content=None),  # no toolUseResult body
    )

    assert only(roll(tmp_path, repo, [dev()])).complete is True


def test_a_failed_read_is_not_credited_as_loaded(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content=None, is_error=True),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The call was made but nothing reached the context, so nothing was read.
    assert session.complete is False and session.missing == (FEATURES,)


def test_an_unanswered_read_call_is_not_credited(tmp_path):
    repo = make_repo(tmp_path)
    call, _result = read_rows(repo.path / FEATURES, content=None)
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{FEATURES}]") + [call])

    assert only(roll(tmp_path, repo, [dev()])).complete is False


def test_reads_outside_the_repo_are_ignored(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(tmp_path / "elsewhere" / ".ai" / "features" / "evaluation.md",
                    content="# other project"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.complete is False
    assert session.midflight == ()


def test_malformed_prefetch_entries_are_excluded_and_explained(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}, docs/not-memory.md]")
        + read_rows(repo.path / FEATURES, content="# eval"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.expected == (FEATURES,)
    assert session.complete is True
    assert "malformed" in (session.detail or "")


# -- session-time truth -------------------------------------------------------


def test_expected_set_is_the_session_time_one_not_the_current_task_file(tmp_path):
    repo = make_repo(tmp_path)
    # The task file on disk now names three docs (a later session backfilled it).
    (repo.path / ".ai-tasks").mkdir(parents=True)
    (repo.path / ".ai-tasks" / f"{TASK}.md").write_text(
        task_md(f"[{FEATURES}, {SIDECAR}, {RUNTIME}]"), encoding="utf-8"
    )
    # This session read a task file that declared only one.
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]") + read_rows(repo.path / FEATURES, content="# eval"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.expected == (FEATURES,)
    assert session.complete is True  # not judged against docs it never declared


def test_a_session_that_never_read_its_task_file_is_unassessable(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(tmp_path, repo, read_rows(repo.path / FEATURES, content="# eval"))

    behavior = roll(tmp_path, repo, [dev()])
    session = only(behavior)

    assert session.available is True  # the transcript parsed fine
    assert session.assessable is False and session.applicable is None
    assert session.coverage is None and session.complete is None
    assert "expected set unrecoverable" in session.detail
    assert behavior.warnings  # a dev session we could not measure is a real gap


def test_an_offset_task_read_does_not_supply_the_expected_set(tmp_path):
    repo = make_repo(tmp_path)
    body = task_md(f"[{FEATURES}]")
    write_transcript(
        tmp_path,
        repo,
        read_rows(
            repo.path / ".ai-tasks" / f"{TASK}.md",
            content=body.split("\n", 4)[-1],  # a slice with no frontmatter
            start_line=40,
        ),
    )

    assert only(roll(tmp_path, repo, [dev()])).assessable is False


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
        + read_rows(repo.path / FEATURES, content="# eval"),
    )

    assert only(roll(tmp_path, repo, [dev()])).complete is True


def test_an_empty_declared_set_is_recovered_but_not_applicable(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(tmp_path, repo, entry_rows(repo, None))

    session = only(roll(tmp_path, repo, [dev()]))

    # Recovered-and-empty is a measurement (nothing to comply with), not a gap.
    assert session.assessable is True and session.applicable is False
    assert session.expected == () and session.coverage is None


def test_an_oversize_declared_set_is_refused_rather_than_partially_measured(tmp_path):
    repo = make_repo(tmp_path)
    docs = ", ".join(f".ai/features/f{n}.md" for n in range(MAX_EXPECTED_DOCS + 1))
    write_transcript(tmp_path, repo, entry_rows(repo, f"[{docs}]"))

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.assessable is False and session.coverage is None
    assert "cap" in session.detail


# -- applicability ------------------------------------------------------------


def test_a_review_session_is_not_applicable_and_does_not_warn(tmp_path):
    repo = make_repo(tmp_path)  # no transcript at all: Codex leaves none here

    behavior = roll(
        tmp_path,
        repo,
        [SessionRef(session_id="019fe91c-878f-7db1-bd8a-f17eadab8249",
                    run_id="run-1", role="review", kind="session")],
    )
    session = only(behavior)

    assert session.applicable is False and session.assessable is False
    assert behavior.warnings == ()  # never asked to pre-load; not a missing measurement


def test_a_plan_gate_session_shares_the_dev_prefetch_expectation(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path, repo, entry_rows(repo, f"[{FEATURES}]"), sid="plan-sid"
    )

    session = only(
        roll(
            tmp_path,
            repo,
            [SessionRef(session_id="plan-sid", role="dev", kind="plan-gate")],
        )
    )

    assert session.applicable is True and session.complete is False


# -- mid-flight retrieval trace -----------------------------------------------


def test_midflight_trace_is_ordered_and_excludes_prefetch_and_eager_docs(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="a")
        + work_rows(repo)  # work begins: everything after it is retrieval
        + read_rows(repo.path / ".ai/index.md", content="# router", uid="b")
        + read_rows(repo.path / ".ai/design/index.md", content="# design", uid="c")
        + read_rows(repo.path / ".ai/apis/index.md", content="# apis", uid="d")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="e")
        + read_rows(repo.path / SIDECAR, content="# sidecar again", uid="f"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Eager docs (router, design entrypoint) and the declared prefetch doc are out;
    # the rest appear once each, in the order they were consulted.
    assert [read.path for read in session.midflight] == [".ai/apis/index.md", SIDECAR]
    assert [read.order for read in session.midflight] == sorted(
        read.order for read in session.midflight
    )
    assert {read.actor for read in session.midflight} == {ACTOR_ROOT}
    assert session.complete is True  # the declared doc was still a pre-load


def test_subagent_reads_count_as_consultation_and_are_attributed(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + work_rows(repo)
        + read_rows(repo.path / SIDECAR, content="# sidecar", sidechain=True, uid="s"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert [(r.path, r.actor) for r in session.midflight] == [(SIDECAR, ACTOR_SUBAGENT)]


def test_a_subagent_read_can_satisfy_a_prefetch_doc(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content="# eval", sidechain=True, uid="s"),
    )

    assert only(roll(tmp_path, repo, [dev()])).complete is True


def test_no_midflight_reads_is_an_empty_trace_not_a_defect(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]") + read_rows(repo.path / FEATURES, content="# eval"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.midflight == () and session.complete is True


def test_midflight_trace_is_capped_and_says_so(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}]") + work_rows(repo)
    for n in range(5):
        rows += read_rows(repo.path / f".ai/features/f{n}.md", content="x", uid=f"u{n}")
    write_transcript(tmp_path, repo, rows)

    session = only(roll(tmp_path, repo, [dev()], max_midflight=2))

    assert len(session.midflight) == 2 and session.midflight_truncated is True


def test_midflight_trace_survives_an_unrecoverable_expected_set(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        read_rows(repo.path / RUNTIME, content="# runtime", uid="pre")
        + work_rows(repo)
        + read_rows(repo.path / SIDECAR, content="# sidecar"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Prefetch is unassessable, but the observed loading and retrieval still are:
    # both traces come off the phase boundary, not off a set we never recovered.
    assert session.assessable is False
    assert session.entry_extra == (RUNTIME,)
    assert [read.path for read in session.midflight] == [SIDECAR]


# -- the entry / work partition -----------------------------------------------


def test_a_declared_doc_read_only_after_work_began_is_late_not_a_preload(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + work_rows(repo)  # went to the code first
        + read_rows(repo.path / FEATURES, content="# eval", uid="late"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # Reaching for a declared doc after reading the code is a recovery, not the
    # pre-load the instruction asked for — but it is not "never consulted" either.
    assert session.complete is False and session.coverage == 0.0
    assert session.missing == (FEATURES,) and session.late == (FEATURES,)
    assert session.loads[0].phase == PHASE_WORK
    assert [read.path for read in session.midflight] == [FEATURES]


def test_an_undeclared_doc_delivered_at_entry_is_not_a_midflight_retrieval(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [injected_row(repo.path / FEATURES, "# eval")]
        + [injected_row(repo.path / SIDECAR, "# sidecar")]  # undeclared, at entry
        + work_rows(repo)
        + read_rows(repo.path / SIDECAR, content="# sidecar again", uid="re"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # It was already in context when work started, so nothing was retrieved
    # mid-flight — but the extra pre-load is real behavior and is kept.
    assert session.midflight == ()
    assert session.entry_extra == (SIDECAR,)
    assert session.complete is True


def test_the_claim_edit_does_not_end_context_assembly(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [edit_row(repo.path / ".ai-tasks" / f"{TASK}.md", uid="claim")]
        + read_rows(repo.path / FEATURES, content="# eval"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The entry checklist puts the claim edit before the pre-load; treating it as
    # work would fail every compliant session.
    assert session.entry_boundary is None
    assert session.complete is True
    assert session.activity == Activity(task_edits=1)


def test_editing_anything_else_ends_context_assembly(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [edit_row(repo.path / "orch_hub" / "runs.py", uid="w", name="Write")]
        + read_rows(repo.path / FEATURES, content="# eval"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.complete is False and session.late == (FEATURES,)
    assert session.activity == Activity(edits=1)


def test_a_repo_search_ends_context_assembly_but_a_scoped_one_does_not(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}]")
    cases = {
        "scoped-sid": search_row(repo.path / ".ai", uid="scoped"),  # routed lookup
        "elsewhere-sid": search_row(tmp_path / "other", uid="away"),  # another tree
        "wide-sid": search_row(uid="wide"),  # unscoped: a search of the code
    }
    for sid, row in cases.items():
        write_transcript(
            tmp_path,
            repo,
            rows + [row] + read_rows(repo.path / FEATURES, content="# eval", uid="ok"),
            sid=sid,
        )

    rolled = {
        sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in cases
    }

    assert rolled["scoped-sid"].complete is True
    assert rolled["elsewhere-sid"].complete is True  # not this repo's code
    assert rolled["wide-sid"].complete is False
    assert rolled["wide-sid"].late == (FEATURES,)


def test_midflight_reads_carry_the_work_done_before_them(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + read_rows(repo.path / FEATURES, content="# eval", uid="pre")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="early")  # still entry
        + work_rows(repo)
        + [edit_row(repo.path / "orch_hub" / "runs.py", uid="edit")]
        + read_rows(repo.path / RUNTIME, content="# runtime", uid="late"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The anchor is what survives the transcript: this doc was opened only after
    # the code had been read and changed, which a bare row order cannot say.
    assert [read.path for read in session.midflight] == [RUNTIME]
    assert session.midflight[0].before == Activity(code_reads=1, edits=1)
    assert session.activity == Activity(code_reads=1, edits=1)
    assert session.entry_extra == (SIDECAR,)  # pre-loaded before any of it


def test_a_parallel_entry_batch_is_not_split_by_its_own_first_code_read(tmp_path):
    repo = make_repo(tmp_path)
    doc_call, doc_result = read_rows(repo.path / FEATURES, content="# eval", uid="doc")
    code_call, code_result = work_rows(repo)
    write_transcript(
        tmp_path,
        repo,
        # Both requested in the entry batch; the code result happens to land first.
        entry_rows(repo, f"[{FEATURES}]") + [doc_call, code_call, code_result, doc_result],
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The doc was asked for while still assembling context, so a sibling call's
    # result landing first does not turn it into a late recovery.
    assert session.complete is True
    assert session.loads[0].phase == PHASE_ENTRY


def test_entry_checklist_commands_do_not_end_context_assembly(tmp_path):
    repo = make_repo(tmp_path)
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [bash_row("date -u +%Y-%m-%dT%H:%M:%SZ", uid="ts")]
        + [bash_row("git status --porcelain && git log --oneline -8", uid="git")]
        + [bash_row(f"ls {repo.path}/.ai/features/", uid="route")]
        + read_rows(repo.path / FEATURES, content="# eval"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The claim timestamp, the clean-tree check, and a routed listing are the
    # protocol's own entry steps: they put no repo content in context, so a
    # pre-load after them is still a pre-load.
    assert session.entry_boundary is None and session.boundary_unsure is None
    assert session.complete is True
    assert session.activity == Activity()


def test_shell_mediated_code_work_ends_context_assembly(tmp_path):
    repo = make_repo(tmp_path)
    cases = {
        "grep-sid": 'grep -rn "RunManager" orch_hub/',
        "read-sid": "sed -n '1,40p' orch_hub/runs.py",
        "test-sid": "go test ./...",
    }
    for sid, command in cases.items():
        write_transcript(
            tmp_path,
            repo,
            entry_rows(repo, f"[{FEATURES}]")
            + [bash_row(command, uid="w")]
            + read_rows(repo.path / FEATURES, content="# eval", uid="late")
            + read_rows(repo.path / SIDECAR, content="# sidecar", uid="extra"),
            sid=sid,
        )

    rolled = {sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in cases}

    # Rediscovering the code through the shell is the same behavior as reading or
    # grepping it through a tool: the declared doc opened afterwards is a late
    # recovery, and the undeclared one is a mid-flight retrieval rather than an
    # entry pre-load that never happened.
    for sid, session in rolled.items():
        assert session.assessable is True, sid
        assert (session.complete, session.coverage) == (False, 0.0), sid
        assert session.late == (FEATURES,), sid
        assert session.activity == Activity(shell=1), sid
        assert [read.path for read in session.midflight] == [FEATURES, SIDECAR], sid
        assert session.entry_extra == (), sid


def test_an_unclassifiable_command_refuses_only_what_follows_it(tmp_path):
    repo = make_repo(tmp_path)
    probe = bash_row("for f in a b c; do echo $f; done", uid="probe")
    doc = read_rows(repo.path / FEATURES, content="# eval", uid="a")
    entry = entry_rows(repo, f"[{FEATURES}]")
    write_transcript(tmp_path, repo, entry + [probe] + doc, sid="after-sid")
    write_transcript(tmp_path, repo, entry + doc + [probe], sid="before-sid")

    after = only(roll(tmp_path, repo, [dev(sid="after-sid")]))
    before = only(roll(tmp_path, repo, [dev(sid="before-sid")]))

    # A command this reader cannot place may have been work; crediting the doc
    # that followed it would be a guess, so the verdict is refused rather than
    # decided in either direction.
    assert after.boundary_unsure == 2
    assert after.assessable is False
    assert after.coverage is None and after.complete is None
    assert after.loaded == (FEATURES,)  # the observation still stands
    assert "boundary not determinable" in after.detail
    # A doc already in context before it is a pre-load whatever it turns out to
    # have been: the doubt does not spread backwards.
    assert before.boundary_unsure == 4
    assert before.assessable is True and before.complete is True


def test_listing_this_repo_ends_context_assembly_but_a_routed_listing_does_not(tmp_path):
    repo = make_repo(tmp_path)
    work = {
        "bare-sid": "ls",
        "flags-sid": "ls -la",
        "tree-sid": "ls tests",
    }
    assembly = {
        "routed-sid": "ls .ai/features/",
        "abs-sid": f"ls -la {repo.path}/.ai-tasks/ 2>/dev/null | head -50",
        "elsewhere-sid": "ls -la ~/.claude/projects/",
    }
    for sid, command in {**work, **assembly}.items():
        write_transcript(
            tmp_path,
            repo,
            entry_rows(repo, f"[{FEATURES}]")
            + [bash_row(command, uid="w")]
            + read_rows(repo.path / FEATURES, content="# eval", uid="doc"),
            sid=sid,
        )

    rolled = {sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in {**work, **assembly}}

    # A listing is a tree, which is what a search returns: pointed at this repo's
    # source it is the same work signal, so a declared doc opened afterwards is a
    # late recovery rather than a pre-load.
    for sid in work:
        assert (rolled[sid].complete, rolled[sid].late) == (False, (FEATURES,)), sid
        assert rolled[sid].activity == Activity(shell=1), sid
    # Pointed at the session's own instructions — or at another tree entirely —
    # it says nothing about this repo's code.
    for sid in assembly:
        assert rolled[sid].complete is True, sid
        assert rolled[sid].entry_boundary is None, sid
        assert rolled[sid].boundary_unsure is None, sid


def test_a_git_query_is_placed_by_its_flags_not_by_its_head(tmp_path):
    repo = make_repo(tmp_path)
    cases = {
        "patch-sid": "git log -p -1",
        "pickaxe-sid": 'git log --oneline -S "RunManager"',
        "meta-sid": "git log --oneline -8 --format='%h %s' && git status --porcelain",
        "unknown-sid": "git log --some-flag-this-reader-has-never-seen -1",
    }
    for sid, command in cases.items():
        write_transcript(
            tmp_path,
            repo,
            entry_rows(repo, f"[{FEATURES}]")
            + [bash_row(command, uid="q")]
            + read_rows(repo.path / FEATURES, content="# eval", uid="doc"),
            sid=sid,
        )

    rolled = {sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in cases}

    # ``git log`` prints commit subjects or the whole diff of every commit
    # depending on one flag; the head cannot tell them apart.
    for sid in ("patch-sid", "pickaxe-sid"):
        assert (rolled[sid].complete, rolled[sid].late) == (False, (FEATURES,)), sid
    assert rolled["meta-sid"].complete is True
    assert rolled["meta-sid"].entry_boundary is None
    # An unrecognized flag may print anything, so it buys uncertainty rather than
    # the benefit of the doubt.
    assert rolled["unknown-sid"].assessable is False
    assert rolled["unknown-sid"].boundary_unsure == 2
    assert "boundary not determinable" in rolled["unknown-sid"].detail


def test_a_git_flag_cluster_is_read_out_past_its_first_letter(tmp_path):
    repo = make_repo(tmp_path)
    content = {
        "patch-sid": "git log -sp -1",
        "pickaxe-sid": "git log -sSsession -1",
    }
    metadata = {
        "pair-sid": "git status -sb",
        "count-sid": "git log -n5 --oneline",
    }
    for sid, command in {**content, **metadata}.items():
        write_transcript(
            tmp_path,
            repo,
            entry_rows(repo, f"[{FEATURES}]")
            + [bash_row(command, uid="q")]
            + read_rows(repo.path / FEATURES, content="# eval", uid="doc"),
            sid=sid,
        )

    rolled = {sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in {**content, **metadata}}

    # Short options cluster, so one token can spell more than one flag: ``-sp``
    # prints the patch and ``-sSsession`` searches file diffs, both while wearing
    # the metadata-only ``-s`` that reading the leading pair alone would see.
    for sid in content:
        assert (rolled[sid].complete, rolled[sid].late) == (False, (FEATURES,)), sid
        assert rolled[sid].activity == Activity(shell=1), sid
    # Reading the cluster out is not a licence to invent flags: every letter of
    # ``-sb`` is metadata, and what follows ``-n`` is its count, not more flags.
    for sid in metadata:
        assert rolled[sid].complete is True, sid
        assert rolled[sid].entry_boundary is None, sid
        assert rolled[sid].boundary_unsure is None, sid


def test_a_redirect_target_is_a_write_whatever_it_is_named(tmp_path):
    repo = make_repo(tmp_path)
    writes = {
        "make-sid": "echo generated > Makefile",
        "version-sid": "printf '1.4.0' > VERSION",
    }
    # A ``>`` inside a string is the text the command was handed, not a
    # redirection — including when the string spans a statement separator, which
    # is where a per-statement reading of the quoting loses it.
    quoted = {
        "prose-sid": 'echo "coverage rose > baseline"',
        "pattern-sid": 'grep -n "^\\.list\\|> \\.list" orch_hub/web/styles.css',
        "arrow-sid": (
            'node -e "\nfetch(url).then(r=>r.json()).then(j=>{\n'
            '  console.log(j.total);\n});"'
        ),
    }
    for sid, command in {**writes, **quoted}.items():
        write_transcript(
            tmp_path,
            repo,
            entry_rows(repo, f"[{FEATURES}]")
            + [bash_row(command, uid="w")]
            + read_rows(repo.path / FEATURES, content="# eval", uid="doc"),
            sid=sid,
        )

    rolled = {sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in {**writes, **quoted}}

    # ``Makefile`` and ``VERSION`` are this repo's files however plain their
    # names are; asking whether the target *looked* like a filename left the
    # write wearing the neutral verdict of the ``echo`` in front of it.
    for sid in writes:
        assert (rolled[sid].complete, rolled[sid].late) == (False, (FEATURES,)), sid
        assert rolled[sid].activity == Activity(edits=1), sid
    assert rolled["prose-sid"].complete is True
    assert rolled["prose-sid"].entry_boundary is None
    assert rolled["prose-sid"].activity == Activity()
    # These two do reach the code — by reading and by running it. That is what
    # ends context assembly; neither of them wrote ``.list`` or ``r.json``.
    for sid in ("pattern-sid", "arrow-sid"):
        assert (rolled[sid].complete, rolled[sid].late) == (False, (FEATURES,)), sid
        assert rolled[sid].activity == Activity(shell=1), sid


def test_a_shell_write_is_weighed_like_the_tool_edit_it_stands_in_for(tmp_path):
    repo = make_repo(tmp_path)
    task_file = f"{repo.path}/.ai-tasks/{TASK}.md"
    cases = {
        "log-sid": (
            f"cat >> {task_file} <<'EOF'\n"
            f"### 2026-08-10 / {SID} / (in_progress → in_progress)\n"
            "- Done: remediated the boundary\nEOF"
        ),
        "claim-sid": f"printf 'claimed-by: x@2026-08-10T00:00:00Z\\n' >> {task_file}",
        "source-sid": "echo 'generated' > orch_hub/generated.py",
    }
    for sid, command in cases.items():
        write_transcript(
            tmp_path,
            repo,
            entry_rows(repo, f"[{FEATURES}]")
            + [bash_row(command, uid="w")]
            + read_rows(repo.path / FEATURES, content="# eval", uid="doc"),
            sid=sid,
        )

    rolled = {sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in cases}

    # A session-log append is the recorded decision whether it is written through
    # ``Edit`` or through the shell, and the entry scope of its path does not
    # make it context assembly.
    assert (rolled["log-sid"].complete, rolled["log-sid"].late) == (False, (FEATURES,))
    assert rolled["log-sid"].activity == Activity(task_edits=1)
    # A shell write whose content this reader cannot classify is not shown to be
    # the claim, so it does not get the claim's exemption either.
    assert rolled["claim-sid"].assessable is False
    assert rolled["claim-sid"].boundary_unsure == 2
    assert rolled["claim-sid"].activity == Activity(task_edits=1)
    # Writing source is work by the same rule the ``Edit`` path applies.
    assert (rolled["source-sid"].complete, rolled["source-sid"].late) == (
        False,
        (FEATURES,),
    )
    assert rolled["source-sid"].activity == Activity(edits=1)


def test_shell_work_on_another_tree_is_not_this_repo_s_work(tmp_path):
    repo = make_repo(tmp_path)
    cases = {
        "scratch-sid": "rm -rf /tmp/orchhub-probe && cat ~/.claude/settings.json",
        "sibling-sid": f"sed -n '1,40p' {tmp_path}/repos/beta/src/main.py",
    }
    for sid, command in cases.items():
        write_transcript(
            tmp_path,
            repo,
            entry_rows(repo, f"[{FEATURES}]")
            + [bash_row(command, uid="w")]
            + read_rows(repo.path / FEATURES, content="# eval", uid="doc"),
            sid=sid,
        )

    rolled = {sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in cases}

    # The tool side simply drops a read that resolves outside the repo; the shell
    # spelling of it cannot mean more. Otherwise a scratch file in ``/tmp`` would
    # invent a missed prefetch doc for a session that never touched the code.
    for sid, session in rolled.items():
        assert session.complete is True, sid
        assert session.entry_boundary is None, sid


def test_a_cd_keeps_shell_work_visible_inside_the_repo_and_uncertain_outside(tmp_path):
    repo = make_repo(tmp_path)
    cases = {
        "inside-sid": f"cd {repo.path}/web && cat package.json",
        "outside-sid": "cd ~/.claude/projects && ls -la *.jsonl",
    }
    for sid, command in cases.items():
        write_transcript(
            tmp_path,
            repo,
            entry_rows(repo, f"[{FEATURES}]")
            + [bash_row(command, uid="w")]
            + read_rows(repo.path / FEATURES, content="# eval", uid="doc"),
            sid=sid,
        )

    rolled = {sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in cases}

    # Moved inside the repo, the relative path still names this repo's source.
    assert (rolled["inside-sid"].complete, rolled["inside-sid"].late) == (
        False,
        (FEATURES,),
    )
    # Moved out of it, ``*.jsonl`` names nothing here — reading it as this repo's
    # tree would invent work, so the partition is uncertain instead.
    assert rolled["outside-sid"].assessable is False
    assert rolled["outside-sid"].boundary_unsure == 2


def test_a_reader_over_the_entry_scope_is_still_context_assembly(tmp_path):
    repo = make_repo(tmp_path)
    survey = (
        "for f in .ai-tasks/2026-07-05-im-bot-adapter.md .ai-tasks/index.md; do\n"
        'echo "===== $f ====="; sed -n \'1,9p\' "$f"; done'
    )
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [bash_row(survey, uid="survey")]
        + read_rows(repo.path / FEATURES, content="# eval", uid="doc"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    # The loop variable is unresolvable on its own, but every path this command
    # names is the session's own instructions, so that is what its reader read.
    assert session.complete is True
    assert session.entry_boundary is None and session.boundary_unsure is None


def test_a_session_log_write_ends_context_assembly_but_a_claim_does_not(tmp_path):
    repo = make_repo(tmp_path)
    task_file = repo.path / ".ai-tasks" / f"{TASK}.md"
    cases = {
        "claim-sid": "claimed-by: 93abe2b6@2026-08-10T02:58:15Z\nsession-est: 3/3",
        "log-sid": f"### 2026-08-10 / {SID} / (in_progress → in_progress)\n- Done: ...",
    }
    for sid, new_string in cases.items():
        write_transcript(
            tmp_path,
            repo,
            entry_rows(repo, f"[{FEATURES}]")
            + [edit_row(task_file, uid="t", new_string=new_string)]
            + read_rows(repo.path / FEATURES, content="# eval"),
            sid=sid,
        )

    rolled = {sid: only(roll(tmp_path, repo, [dev(sid=sid)])) for sid in cases}

    # Both are task-file writes and both are counted as such, but only one of
    # them records a decision a later read could no longer have informed.
    assert rolled["claim-sid"].complete is True
    assert rolled["claim-sid"].entry_boundary is None
    assert rolled["log-sid"].complete is False
    assert rolled["log-sid"].late == (FEATURES,)
    assert rolled["log-sid"].activity == Activity(task_edits=1)


# -- close-out exclusion ------------------------------------------------------


def test_reads_after_the_closeout_skill_do_not_count_as_consultation(tmp_path):
    repo = make_repo(tmp_path)
    closeout = {
        "type": "assistant",
        "timestamp": TS,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "sk",
                    "name": "Skill",
                    "input": {"skill": CLOSEOUT_SKILL},
                }
            ],
        },
    }
    write_transcript(
        tmp_path,
        repo,
        entry_rows(repo, f"[{FEATURES}]")
        + [closeout]
        + read_rows(repo.path / FEATURES, content="# eval", uid="late")
        + read_rows(repo.path / SIDECAR, content="# sidecar", uid="later"),
    )

    session = only(roll(tmp_path, repo, [dev()]))

    assert session.closeout_seen is True
    assert session.complete is False  # absorption is the write path, not a pre-load
    assert session.midflight == ()


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

    assert session.closeout_seen is True and session.midflight == ()


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


def test_a_hole_in_the_entry_evidence_refuses_the_completeness_verdict(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}, {SIDECAR}]")
    rows += read_rows(repo.path / FEATURES, content="# eval", uid="a")
    path = write_transcript(tmp_path, repo, rows)
    # A row the reader cannot use, before any work began: the missing doc may be
    # exactly what it held.
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
    assert session.loaded == (FEATURES,) and session.missing == (SIDECAR,)
    assert "entry-phase evidence incomplete" in session.detail
    assert behavior.warnings


def test_holes_after_the_boundary_leave_the_entry_measurement_standing(tmp_path):
    repo = make_repo(tmp_path)
    rows = entry_rows(repo, f"[{FEATURES}]")
    rows += read_rows(repo.path / FEATURES, content="# eval", uid="a")
    rows += work_rows(repo)
    path = write_transcript(tmp_path, repo, rows)
    text = path.read_text(encoding="utf-8")
    # Mixed damage, all of it in the work phase: a malformed row, a row past the
    # per-row cap, and a byte budget that cuts the last row in half.
    damaged = (
        "{oops\n" + json.dumps({"type": "user", "pad": "x" * 9000}) + "\n" + "{tail\n"
    )
    path.write_text(text + damaged, encoding="utf-8")

    session = only(
        roll(
            tmp_path,
            repo,
            [dev()],
            max_bytes=len(text) + len(damaged) - 3,
            max_line_bytes=4000,
        )
    )

    assert (session.skipped_rows, session.truncated) == (3, True)
    assert session.entry_boundary is not None
    # Nothing after the session reached the code can change what it had loaded
    # before that, so the pre-load verdict is still owed.
    assert session.assessable is True and session.complete is True


def delayed_doc_rows(repo: ManagedRepo, *, content: str = "# eval"):
    """An entry batch whose memory result lands after a sibling code read's.

    The shape the phase partition exists for — and the shape that makes lost
    evidence dangerous, because the surviving rows say the boundary is closed
    while the answer to a pre-boundary request is still outstanding.
    """
    doc_call, doc_result = read_rows(repo.path / FEATURES, content=content, uid="a")
    code_call, code_result = work_rows(repo)
    return (
        entry_rows(repo, f"[{FEATURES}, {SIDECAR}]") + [doc_call, code_call, code_result],
        doc_result,
    )


def assert_delayed_result_refused(session):
    """A lost delayed result must cost the verdict, not the doc it was carrying."""
    assert session.entry_boundary is not None  # damage is all past the boundary
    assert session.assessable is False
    assert session.coverage is None and session.complete is None
    assert session.applicable is True
    assert session.expected == (FEATURES, SIDECAR)
    assert session.missing == (FEATURES, SIDECAR)
    assert "did not survive the scan" in session.detail


def test_a_delayed_entry_result_lost_to_a_malformed_row_refuses_the_verdict(tmp_path):
    repo = make_repo(tmp_path)
    rows, _ = delayed_doc_rows(repo)
    path = write_transcript(tmp_path, repo, rows)
    # The row that would have carried the doc is unreadable.
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")

    behavior = roll(tmp_path, repo, [dev()])

    # The hole sits after the boundary, so the entry phase itself is intact — but
    # the request it may have answered was made before it, and reporting that doc
    # as never loaded would be a negative this evidence cannot support.
    assert_delayed_result_refused(only(behavior))
    assert behavior.warnings


def test_a_delayed_entry_result_past_the_row_cap_refuses_the_verdict(tmp_path):
    repo = make_repo(tmp_path)
    rows, doc_result = delayed_doc_rows(repo, content="x" * 9000)
    write_transcript(tmp_path, repo, rows + [doc_result])

    session = only(roll(tmp_path, repo, [dev()], max_line_bytes=4000))

    assert session.skipped_rows == 1
    assert_delayed_result_refused(session)


def test_a_delayed_entry_result_in_the_truncated_tail_refuses_the_verdict(tmp_path):
    repo = make_repo(tmp_path)
    rows, doc_result = delayed_doc_rows(repo)
    budget = len("".join(json.dumps(row) + "\n" for row in rows))
    write_transcript(tmp_path, repo, rows + [doc_result])

    session = only(roll(tmp_path, repo, [dev()], max_bytes=budget))

    # A clean cut, no unreadable row at all: the tail alone is the lost evidence.
    assert (session.truncated, session.skipped_rows) == (True, 0)
    assert_delayed_result_refused(session)


def test_a_lost_code_read_result_makes_the_boundary_unsure_not_the_load(tmp_path):
    repo = make_repo(tmp_path)
    code_call, _ = work_rows(repo)
    rows = (
        entry_rows(repo, f"[{FEATURES}]")
        + [code_call]  # its result never arrives
        + read_rows(repo.path / FEATURES, content="# eval", uid="a")
        + [edit_row(repo.path / "orch_hub" / "runs.py", uid="w", name="Write")]
    )
    path = write_transcript(tmp_path, repo, rows)
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")

    session = only(roll(tmp_path, repo, [dev()]))

    # The lost row may hold the code delivery that would have closed context
    # assembly *before* the doc, so the doc's entry credit is not owed either —
    # the same doubt as an unclassifiable command, reached from the other side.
    assert session.entry_boundary == 5 and session.boundary_unsure == 2
    assert session.assessable is False and session.complete is None
    assert session.loaded == (FEATURES,)
    assert "boundary not determinable" in session.detail


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

    # A body cut inside the frontmatter would parse as "declared nothing" and
    # quietly drop the session out of the measurement instead of reporting a gap.
    assert session.assessable is False and session.expected == ()
    assert "expected set unrecoverable" in session.detail


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
    assert session.loads[0].method == METHOD_INJECTED


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
    assert session.loaded == (FEATURES,)
    # But a hole in the entry evidence is not a measurement: the load is reported
    # as an observation while the score stays unclaimed.
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
    # The session never reached the code, so the cut tail is inside the entry
    # phase and could hold the missing load: no completeness verdict is owed.
    assert session.entry_boundary is None
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
