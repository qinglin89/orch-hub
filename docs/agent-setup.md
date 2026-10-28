# Set up orch-hub: instructions for a coding agent

This page is written for an AI coding agent (Claude Code, Codex, Cursor) that a
user has asked to install orch-hub, or to add a repository to it. A person can
follow it too. Work through it in order. The mechanical work is done by
commands that check their own results; your job is to run them, read what they
report, and stop to ask the user wherever this page says **Ask**.

orch-hub is a dashboard, served on the user's Mac, for running coding agents
over [Mandrel](https://github.com/qinglin89/mandrel)-managed repositories. Done
looks like this: the service is healthy, every repository the user chose shows
as ready, and the user is signed in to the dashboard in their browser.

## Ground rules

- **Never print, echo, or paste the dashboard token.** It is a credential for a
  surface that runs code on this machine. Use `orch-hub open` or
  `orch-hub token --copy`, which put it on the clipboard. Do not run a bare
  `orch-hub token` either, because that prints it.
- **Keep the service on `127.0.0.1`.** Never bind it to `0.0.0.0` or a LAN
  address, and never expose the port. For phone access, see [Hand over](#hand-over).
- **No `sudo`.** Everything installs under the user's home directory.
- **Do not bypass your own permission prompts** or switch into an
  unrestricted mode to get through this page. The installer needs network access
  to github.com, and it writes to `~/.local`, `~/Library/LaunchAgents`, and
  `~/Library/Logs`. It also starts a launchd agent. If your sandbox blocks
  that, ask the user to approve running the command outside it.
- **Do not edit files Mandrel deploys into a repository**: `CLAUDE.md`,
  `.claude/`, `.codex/`, `.cursor/`, `.ai-protocol/`, `.mandrel/`. Redeploy
  instead.
- **Commits in the user's repositories are theirs.** Ask before each one.

## Install

1. Check the machine:

   ```bash
   uname -sm
   ```

   The one-line install supports `Darwin arm64` (macOS on Apple Silicon)
   only. On anything else, stop. Tell the user, and point them to
   [running from source](https://github.com/qinglin89/orch-hub#run-from-source).

2. Fetch the installer and ask it for its plan. This step changes nothing:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/qinglin89/orch-hub/dev/install.sh -o /tmp/orch-hub-install.sh
   sh /tmp/orch-hub-install.sh --dry-run
   ```

   The plan names the Mandrel checkout it will use or clone, the Python it
   found (or that it would install Python with Homebrew), the release it will
   download, and the service address. If the plan stops on a missing Python
   and there is no Homebrew, relay its instructions to the user. Stop there.

3. **Ask** the user to approve the plan. Tell them what it installs:
   - Mandrel in `~/.local/share/mandrel` (or the checkout they already have);
   - Python and `jq` through Homebrew, if those are missing;
   - the orch-hub runtime in `~/.local/orch-hub`;
   - a launchd agent that serves the dashboard on `127.0.0.1:8765`.

   If they already have a Mandrel checkout somewhere else, add
   `--mandrel /path/to/it` to the next command.

4. Install:

   ```bash
   sh /tmp/orch-hub-install.sh --no-open
   ```

   It is safe to run again. An existing Mandrel checkout is reused unchanged,
   and an existing service keeps its token and settings. When stdout is not a
   terminal, the token is not printed. That is intended.

5. Verify:

   ```bash
   ~/.local/bin/orch-hub launchd status
   ```

   Expect `loaded: yes` and `health: ok`. If `~/.local/bin` is not on the
   user's `PATH`, keep using the full path, and tell the user about the
   `export PATH=...` line the installer printed. The installer also printed
   the Mandrel checkout it used (`mandrel:`). Below, `MANDREL` means
   `<that checkout>/bin/mandrel`; with the defaults that is
   `~/.local/share/mandrel/bin/mandrel`.

If setup fails with `mandrel cannot run under the service's PATH`, Mandrel
could not find Python 3.11+ under the background service's minimal `PATH`. Give
the checkout its own interpreter, then rerun step 4:
`python3.13 -m venv <mandrel-checkout>/.venv` (any `python3.11` or newer works).

## Add a repo

**Ask** the user which repositories orch-hub should manage, as absolute paths.
Do the following for each one; `REPO` below is that path.

1. **Check that it is ready to receive the protocol.**

   ```bash
   git -C REPO status --porcelain
   git -C REPO rev-parse --verify HEAD
   ```

   - The status output must be empty. If it is not, **ask** the user to
     commit or stash their changes. Do not do it for them without consent.
   - A repository with no commits needs one before it can be initialized.
     **Ask** before you create it.

2. **Preview the deploy.**

   ```bash
   MANDREL deploy --dry-run REPO
   ```

   Read the `update:` list. Deploy owns and overwrites `CLAUDE.md`, `.claude/`,
   `.cursor/`, `.codex/`, `.ai-protocol/`, and `.mandrel/`.
   - If any of those already hold the user's own files, show the user which
     ones and **ask** how to proceed. The usual answer is to move them aside
     first.
   - If Git already tracks one of them, follow Mandrel's
     [collision sequence](https://github.com/qinglin89/mandrel/blob/main/docs/operations.md#if-git-already-tracks-a-deploy-owned-path).

   Also check `~/.claude/skills/` for personal skills named like the deployed
   ones (`ai-init`, `intake-task`, `invoke`, `ctd-tasks`, ...). A personal copy
   shadows the deployed one, and Mandrel reports it as drift. **Ask** before
   renaming or removing anything there.

3. **Deploy, and create the scheduler's Python environment.**

   ```bash
   MANDREL deploy --bootstrap-orchestrator REPO
   ```

4. **Commit the receipts.** **Ask** first. Then:

   ```bash
   git -C REPO add .gitignore .ai-deploy-lock.json
   git -C REPO commit -m "chore: deploy mandrel protocol payload"
   ```

5. **Match the scheduler to the agents installed.** By default a run uses
   Claude Code for development and Codex for review.
   - If `codex` is not installed, tell the user to choose Claude for the review
     role when they start a run. To make that the repo's default instead, run
     `printf 'ORCH_CC_REVIEW_AGENT=claude\n' >> REPO/.mandrel/orchestrator/.env`.
     That file is local and ignored by Git.
   - If Codex is used, the user has to trust the repo's hooks once. They open
     `codex` in REPO and approve both entries under `/hooks`.

6. **Initialize project memory.** `/ai-init` builds the repository's `.ai/`
   memory. For an existing codebase it derives the memory from the code and
   asks the user to correct and approve it. For a new project it interviews
   them. It has to run in an interactive agent session opened in REPO, because
   the repo's own skills load there. Tell the user to open their agent in REPO
   and run `/ai-init`, then commit the new `.ai/` directory. Do not try to run
   it from another directory or without the user.

7. **Check readiness.**

   ```bash
   ~/.local/bin/orch-hub repos --json
   ```

   The repo should show `"ready": true`. Each failed check carries a `hint`
   naming the command or step that fixes it. Apply it, asking first where this
   page says to.

## Hand over

```bash
~/.local/bin/orch-hub open
```

This opens the dashboard and puts the token on the clipboard. It never prints
the token. Tell the user to paste it into the sign-in field. They can run
`orch-hub open` or `orch-hub token --copy` again at any time.

To use the dashboard from a phone or another computer, the user's machines
must share a private [Tailscale](https://tailscale.com) network. Keep the
loopback bind and publish the port only inside that network:

```bash
tailscale serve --bg 8765
```

The [launchd guide](launchd.md) covers this and the direct tailnet-bind
alternative. Do not set this up unless the user asks for it.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `health:` is not `ok` | Read `~/Library/Logs/orch-hub/orch-hub.err.log`, fix the cause, then rerun the installer (or `orch-hub setup`). |
| A repo is not listed | Was it deployed with the same Mandrel the service uses? `orch-hub repos --json` lists what that registry contains. |
| A repo is not ready | Follow the `hint` on each failed check in `orch-hub repos --json`. |
| Runs fail at the first review | The review agent's CLI is missing or logged out. See step 5 of [Add a repo](#add-a-repo). |
| Remove orch-hub | `orch-hub uninstall` stops the service and removes the runtime, and keeps run history. The Mandrel checkout and the repos are left alone. |
