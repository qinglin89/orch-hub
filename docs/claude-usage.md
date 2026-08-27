# Claude subscription usage on the Overview

The Overview's **Subscription usage** section shows how much of each agent CLI's
rate-limit budget is spent. Both providers work out of the box, and neither needs
anything installed into your CLI config:

- **Codex** — orch-hub reads the `rate_limits` snapshots Codex already writes into
  its own session rollouts.
- **Claude** — orch-hub reads the utilization Claude Code itself caches in
  `~/.claude.json`, and asks the CLI to refresh that cache when it has gone stale.

**There is nothing to set up.** If you followed an earlier version of this guide
and installed the `orch-hub-statusline` wrapper, it still works and still
contributes figures — see [Legacy: the statusLine wrapper](#legacy-the-statusline-wrapper)
for why it is no longer needed and how to remove it if you want to.

## How the Claude figures are obtained

Claude Code performs an authenticated utilization fetch on its own schedule and
keeps the result in its config file:

```jsonc
"cachedUsageUtilization": {
  "fetchedAtMs": 1785470888223,
  "utilization": {
    "five_hour": {"resets_at": "2026-07-31T07:20:00.202915+00:00", ...},
    "seven_day": {"resets_at": "2026-07-31T22:00:00.202935+00:00", ...},
    "limits": [{"kind": "session",    "percent":  9, "resets_at": "...07:20:00..."},
               {"kind": "weekly_all", "percent": 43, "resets_at": "...22:00:00..."}]
  }
}
```

orch-hub reads that file (read-only, whitelisted fields only) on every usage read.
When the cached fetch has gone stale, it runs

```
claude -p "/usage" --output-format json --tools "" --safe-mode \
       --no-session-persistence --permission-mode plan
```

from an empty scratch directory, waits for the CLI to refresh its own cache, and
reads the file again.

**`/usage` is handled inside the CLI and never reaches the model.** The turn
reports `num_turns: 0` and `total_cost_usd: 0`, and orch-hub *checks that on every
run*: a turn that reports any model-call accounting — or a CLI version whose
envelope cannot report it — is treated as a failed refresh, and the previous
figures are kept. Nothing about your subscription is spent to display your
subscription.

The refresh is also:

- **asynchronous** — an Overview read never waits for it; the read returns what is
  on disk and the refresh lands for the next read;
- **single-flight** — concurrent requests cost one subprocess, not one each;
- **bounded** — a 60s hard timeout per turn, at most one attempt per 5 minutes,
  and no attempt at all while the CLI's own cached fetch is under 5 minutes old
  (the CLI serves its cache within its own interval, so polling harder cannot
  produce fresher data);
- **non-destructive** — a timeout, an authentication failure, a missing CLI, or a
  malformed cache leaves the last real figures in place *with their own
  timestamp*. A failed refresh can never be presented as a fresh reading.

## When a run ends

Besides stale Overview reads, orch-hub asks for a refresh once when an
orchestrator run reaches a terminal state — the moment that run has finished
spending quota. So the figures are current when you next look, whether or not a
dashboard was open while the run was going.

## What is read, and what is never read

`~/.claude.json` is Claude Code's whole config file, so the read is whitelisted to
the rate-limit fields: the two windows' percentage and reset instant, plus the
cache's `fetchedAtMs`. The `accountUuid` inside that very container — and the
`oauthAccount`, `userID`, `machineID`, and `projects` (every path you have opened)
siblings — are not read, not persisted, and not logged. Failure messages never
echo CLI output, because this file's diagnostics can carry account context.

Never touched at all: your Keychain, any credential or OAuth token, conversation
content, provider endpoints, and the interactive UI. orch-hub does not write
`~/.claude.json` or anything under `~/.claude/` — only the CLI itself does, when
it refreshes its own cache.

The file is treated as untrusted input: bounded read, tolerant parse. A corrupt,
hand-edited, oversized, or unrecognized payload degrades the Claude card to
*unavailable*; it cannot produce wrong figures and cannot fail the `/api/usage`
route.

## Verify

```bash
# 1. What the CLI has cached, and when it fetched it:
python3 - <<'EOF'
import json, pathlib
cache = json.loads(pathlib.Path.home().joinpath(".claude.json").read_text())["cachedUsageUtilization"]
print(cache["fetchedAtMs"], [(l["kind"], l["percent"]) for l in cache["utilization"]["limits"]])
EOF

# 2. What the hub serves:
curl -sH "Authorization: Bearer $ORCH_HUB_TOKEN" localhost:8765/api/usage | python3 -m json.tool
```

The Claude card on the Overview picks up a refresh within its 30-second cache.

If the card stays *unavailable*: the CLI is not installed on the service's `PATH`,
has never been authenticated, or is not a subscriber account — the cache is absent
in all three cases, and orch-hub records nothing rather than inventing figures.

## Staleness is normal

Figures are dated by the CLI's own `fetchedAtMs`, never by when orch-hub read
them. Past an hour the card marks the numbers as last observed, keeping the
(absolute, still meaningful) reset times. A window whose reset has passed reads as
*no active window* rather than showing a retained figure as current. **Stale never
means zero**: unavailable and unused are distinct states everywhere in this
feature, precisely so an unfed source cannot read as free capacity.

## Legacy: the statusLine wrapper

Before the hub could read the CLI's utilization cache, the only local channel
carrying the five-hour / seven-day figures was the JSON payload Claude Code pipes
to a configured `statusLine` command. `orch-hub-statusline` is the small wrapper
that captured it into `<ORCH_HUB_DATA_DIR>/usage/claude-statusline.json` while
relaying your own status line untouched.

That dependency is gone: the figures no longer require an interactive session to
render a status line, so `deploy` and `update` no longer ask you to install
anything.

If you already have it installed, **leaving it or removing it are both fine**:

- **Leaving it** costs nothing and still helps. Both carriers merge into one store
  per window (the higher figure wins within a window, a genuine rollover
  replaces), so the sink is an extra reading rather than a competing source — and
  it is the only carrier that also reports your plan name.
- **Removing it** is your edit to make, in your own settings — orch-hub never
  touches `~/.claude/`:

  ```bash
  # Restore statusLine.command in ~/.claude/settings.json (or settings.local.json)
  # to its original value, or delete the statusLine block, then:
  rm ~/.orch-hub/usage/claude-statusline.json
  ```

  The Claude card keeps working from the utilization cache.

The wrapper itself is unchanged: it is operator-installed (orch-hub never edits
`~/.claude/**`), it persists only the `rate_limits` container plus the
`subscription_type` plan name, and it relays your wrapped statusLine command's
stdin, stdout, stderr, and exit code untouched.

`orch-hub-statusline` is a source-install console script. A compiled release
ships one executable and does not carry it — which costs nothing, because the
Claude card is fed by the utilization cache either way. See
[installing from a release](release.md).

## It gates nothing you do

Subscription usage never stands between you and your work:

- it never contributes to repo **readiness** or to Needs-attention rows;
- it never blocks or delays **Start**, **Stop**, run state, or log streaming;
- it never blocks **intake** drafting, revision, or confirmation;
- a failing or absent source degrades that one card and nothing else.

A quota you are about to exhaust is information for you, not a gate for the hub.

### The one thing that reads it as a decision

Automatic Layer 2 task evaluation is the exception, and it exists to get *out* of
your way. When a task completes, the hub schedules a semantic evaluation for it —
one `claude -p` turn, drawing on the same Claude window you are working in. So
the dispatcher consults these figures before spending it: a turn waits while the
most exhausted measured window is at or above `ORCH_HUB_EVAL_USAGE_CEILING`
(default 80%), and successive turns are spaced by
`ORCH_HUB_EVAL_DISPATCH_SPACING` (default 900s). See
[When an automatic Layer 2 turn actually runs](evaluation.md#when-an-automatic-layer-2-turn-actually-runs).

The four guarantees above still hold, because this is not a gate on anything you
asked for:

- it delays only work the **hub scheduled for itself**. The Layer 2 button, and
  every other manual evaluation path, ignores capacity entirely — that is also
  the escape hatch if a broken usage feed ever holds the queue.
- unknown capacity **defers**, never dispatches. A missing, stale, or unreadable
  figure stops the hub's own spending, not yours.
- a deferred turn is never dropped: it waits, and the queue drains oldest-first
  when capacity returns.
- `ORCH_HUB_EVAL_AUTO=false` removes this reader altogether, and a queue already
  accepted stays put until you turn it back on.
