# Claude subscription usage on the Overview (statusLine feed)

The Overview's **Subscription usage** section shows how much of each agent CLI's
rate-limit budget is spent. Codex works out of the box: orch-hub reads the
`rate_limits` snapshots Codex already writes into its own session rollouts.

Claude needs one small opt-in step, and this guide is it. Install a statusLine
wrapper into your own Claude Code config and the Claude card starts showing real
five-hour / seven-day figures. Skip this guide and the Claude card reads
**unavailable** — which is exactly what it does today. Nothing else changes
either way.

> `orch-hub launchd deploy` (and `update`) point you here automatically: when no
> Claude Code statusLine routes through the wrapper, they print a copy-ready
> `statusLine.command` line built from your deployed wrapper path — with your
> current statusLine command preserved after `--`, and a `--data-dir` flag when
> the hub uses a non-default data dir. The check is read-only and advisory:
> orch-hub never edits `~/.claude`, and an already-configured wrapper prints no
> nag. Missing or malformed Claude settings never fail the deploy.

## Why a statusLine hook

Claude Code publishes the subscription windows to exactly one local channel: the
JSON payload it pipes to a configured statusLine command's stdin, as

```json
"rate_limits": {
  "five_hour": {"used_percentage": 41.2, "resets_at": 1785290400},
  "seven_day": {"used_percentage":  9.5, "resets_at": 1785790400}
}
```

("only present for subscribers after first API response"). Its
`claude -p --output-format stream-json` stdout — the output of the turns orch-hub
itself drives for intake and semantic evaluation — carries no used-percentage on
any version checked: nothing at all on Claude Code 2.1.218, and on 2.1.220 a
`rate_limit_event` that names one window and when it resets, but no figure. The hub
does read that event, so its own turns keep a window's reset time current — but how
much you have used can only come from this hook, no matter how many turns the hub
runs.

The alternatives were rejected deliberately:

- **The CLI's `/usage` command** fetches from a private endpoint and spends a
  `quota_check` API call. orch-hub does not call private endpoints and does not
  spend your quota to measure your quota.
- **A probe turn** — running Claude just to read the headers — is barred for the
  same reason: usage display must never cost tokens.
- **Scraping the interactive UI** is not a supported interface.

What is left is the Codex pattern: read what your own sessions already wrote.
The statusLine hook is how a Claude session writes it down.

## What gets installed, and by whom

**orch-hub never touches `~/.claude/`.** There is no install command, and the
service does not edit your Claude Code config. You add one line to your own
settings; you can remove it the same way.

What you point that line at is `orch-hub-statusline`, a small wrapper shipped
with the hub. On each status-line render it:

1. reads the payload Claude Code pipes to it,
2. writes the rate-limit part to `<ORCH_HUB_DATA_DIR>/usage/claude-statusline.json`
   (default `~/.orch-hub/usage/claude-statusline.json`),
3. runs **your** statusLine command with the same input and relays its output,
   errors, and exit code untouched.

Step 3 is the point: your status line renders exactly as before. If you have no
statusLine command, the wrapper prints nothing and Claude Code renders no status
line — also exactly as before.

## Install

The wrapper is installed with the package, so its path is the hub venv's:

```bash
# The absolute path to use below:
ls "$(pwd)/.venv/bin/orch-hub-statusline"
```

Then edit `~/.claude/settings.json` (or `~/.claude/settings.local.json`).

**If you already have a status line**, wrap it. Everything after `--` is your
existing command, split into arguments:

```jsonc
{
  "statusLine": {
    "type": "command",
    // was: "sh /Users/you/.claude/statusline-command.sh"
    "command": "/Users/you/workplace/orch-hub/.venv/bin/orch-hub-statusline -- sh /Users/you/.claude/statusline-command.sh"
  }
}
```

**If you have no status line yet**, install the wrapper alone:

```jsonc
{
  "statusLine": {
    "type": "command",
    "command": "/Users/you/workplace/orch-hub/.venv/bin/orch-hub-statusline"
  }
}
```

Notes on the wrapped command:

- It is run as **argv, not through a shell**, so nothing in the payload can be
  interpreted as shell syntax. If your status line genuinely needs shell
  features (pipes, `&&`, variable expansion), ask for a shell explicitly:
  `-- /bin/sh -c 'foo | bar'`.
- A wrapped command that cannot be executed makes the wrapper print nothing and
  exit `127`, rather than render half a status line.

If your hub runs with a non-default `ORCH_HUB_DATA_DIR` (see
[docs/launchd.md](launchd.md)), tell the wrapper too — your interactive shell
does not inherit the launchd service's environment:

```
... /orch-hub-statusline --data-dir /path/to/data -- sh ~/.claude/statusline-command.sh
```

## Verify

```bash
# 1. Where the wrapper will write:
.venv/bin/orch-hub-statusline --print-sink

# 2. Open any Claude Code session (a real one — this is the whole point: no
#    turn is started just to measure usage), then:
cat ~/.orch-hub/usage/claude-statusline.json

# 3. What the hub serves:
curl -sH "Authorization: Bearer $ORCH_HUB_TOKEN" localhost:8765/api/usage | python3 -m json.tool
```

The Claude card on the Overview picks it up within its 30-second refresh.

If the sink file never appears: the session is not a subscriber session, or it
has not made its first API response yet — the CLI omits `rate_limits` in both
cases, and the wrapper deliberately records nothing rather than overwriting good
figures with an empty snapshot.

## What is read, and what is never read

The statusLine payload carries a lot of context — session id, transcript path,
cwd, model, cost. **Only two things are persisted**: the `rate_limits` container
and the `subscription_type` plan name (`pro` / `max` / ...). Everything else is
dropped before the write, so no session id, no repo path, and no transcript
location ever lands in the hub data dir.

Never read at all, by construction: your account, any credential or OAuth token,
`~/.claude.json`, conversation content, or anything over the network.

The sink is treated as untrusted input on the read side too — bounded read,
tolerant parse. A corrupt, hand-edited, oversized, or unrecognized file degrades
the Claude card to *unavailable*; it cannot produce wrong figures and cannot
fail the `/api/usage` route.

## Staleness is normal

The sink is written when a Claude Code session renders a status line. An idle Mac
writes nothing, so the figures go stale between your sessions — that is the
resting state, not a fault. Past an hour the card marks the numbers as last
observed, keeping the (absolute, still meaningful) reset times. **Stale never
means zero**: unavailable and unused are distinct states everywhere in this
feature, precisely so an unfed source cannot read as free capacity.

## Uninstall

Restore your `statusLine.command` to its original value (or delete the
`statusLine` block) and, if you like, delete the sink:

```bash
rm ~/.orch-hub/usage/claude-statusline.json
```

The Claude card returns to *unavailable* at the next refresh. Nothing else is
affected.

## It gates nothing

Subscription usage is display-only, everywhere:

- it never contributes to repo **readiness** or to Needs-attention rows;
- it never blocks or delays **Start**, **Stop**, run state, or log streaming;
- it never blocks **intake** drafting, revision, or confirmation;
- a failing or absent source degrades that one card and nothing else.

A quota you are about to exhaust is information for you, not a gate for the hub.
