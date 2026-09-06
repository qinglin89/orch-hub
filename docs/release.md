# Installing from a release, and building one

orch-hub ships as a compiled, self-contained macOS runtime. A machine that
receives it needs no Python, no virtualenv, and no orch-hub checkout: the
release carries its own interpreter and libraries, and one file installs it.

This guide has two halves. [Part 1](#part-1--operating-from-a-release) is for
whoever installs and runs it. [Part 2](#part-2--producing-a-release) is the
maintainer's build → package → publish path.

Running orch-hub from a source checkout is unchanged and documented in
[launchd deployment](launchd.md); the two shapes differ only in how the program
gets onto the machine and which verb configures the service.

> [!IMPORTANT]
> Installing orch-hub installs a remote code execution control surface. The
> security model in [launchd deployment](launchd.md#security-model-read-first)
> applies identically to a release install: loopback or tailnet only, bearer
> token always, never the public internet.

## What a release is

Packaging produces exactly three files:

| File | What it is |
|---|---|
| `orch-hub-<version>-<target>.run` | POSIX-shell self-extractor with the compiled payload appended. One file, one command, no Python on the far side. |
| `release.json` | Target-keyed metadata (`orch-hub-release/1`): version, and per target the os/arch, the minimum macOS, the runtime manifest it was built from, and every artifact with its size and SHA-256. |
| `SHA256SUMS` | The same digests in `shasum -a 256 -c` form, covering every other file in the release, so verification needs no JSON tooling. |

A published release carries a fourth file, `publication.json`, written by the
publish step rather than by packaging: it binds the artifact set to a version
tag and the commit that tag names — which publication has proven is also the
commit every payload was frozen from — and records the digest of `SHA256SUMS`
itself, the one link a checksum file cannot close for itself.

The shipped and supported target is `darwin-arm64` (Apple Silicon). The schema
is target-keyed so a second target is a build concern rather than a format
change, but no other target is produced today.

---

# Part 1 — operating from a release

## Requirements

- An Apple Silicon Mac running at least the `min_macos` the release declares in
  `release.json`. The installer checks that floor with `--check`; its
  `--version` output identifies only the version and target. The current
  release declares macOS 11.0.
- A local [Mandrel](https://github.com/qinglin89/mandrel) checkout with an
  executable `bin/mandrel`, and at least one repo registered with it. orch-hub
  needs no Python; **Mandrel is a separate program with its own requirements**,
  and the hub reports an empty portfolio without it.
- Optional: Tailscale for remote access, Bark for push notifications, and an
  installed/authenticated Claude CLI for remote intake and Layer 2 evaluation.

## Install

Fetch the artifacts, check them, and run the installer by path:

```bash
# 1. Download the release files into one directory.
curl -fLO <url>/orch-hub-0.1.0-darwin-arm64.run
curl -fLO <url>/release.json
curl -fLO <url>/SHA256SUMS

# 2. Verify what you received before running it.
shasum -a 256 -c SHA256SUMS

# 3. Install the runtime and start the resident service.
sh orch-hub-0.1.0-darwin-arm64.run --mandrel /absolute/path/to/mandrel
```

`sh <file>.run` is the intended invocation: a downloaded file is not executable
and does not need to be. If no token was supplied or already installed, the run
generates one and prints it — the only time it is echoed. It is also written
into the installed plist, which is mode `600` because it embeds that secret, so
a token you did not copy can be read back from there.

Installer options:

| Option | Effect |
|---|---|
| `--mandrel DIR` | Mandrel checkout. Required the first time unless `ORCH_HUB_MANDREL_PATH` is exported or a previous install already named one. |
| `--token VALUE` | Bearer token. Default: keep the installed one, otherwise generate and print it once. |
| `--host ADDR` | Bind address. Needed to reach the hub from another device; see the two supported network shapes in [launchd deployment](launchd.md#security-model-read-first). |
| `--port N` | Bind port (default `8765`). |
| `--prefix DIR` | Install prefix (default `~/.local/orch-hub`). |
| `--bin-dir DIR` | Directory the `orch-hub` command is linked into (default `~/.local/bin`). |
| `--no-service` | Install the runtime only; configure the service later with `orch-hub setup`. |
| `--extract DIR` | Unpack the payload into `DIR` and exit. Installs nothing. |
| `--check` | Verify host compatibility and payload integrity, then exit. |
| `--version` | Print the version and target, then exit. |

Both `--opt value` and `--opt=value` are accepted.

If the first install uses a non-default `--prefix`, repeat it when running a
later `.run`: the self-extractor runs from a temporary payload and cannot infer
which custom prefix an earlier installer used. An installed `orch-hub` command
does infer its own prefix. Repeat a custom `--bin-dir` on later installer runs
and on `orch-hub uninstall` so those operations update or remove that link
instead of the default `~/.local/bin/orch-hub`.

## What the installer refuses, and when

Everything that can say "this artifact does not belong on this machine" runs
**before a single byte is unpacked**. The payload is a compiled binary for one
os/arch/minimum-macOS, so on a mismatched host it cannot even be executed to
report the mismatch itself.

| Refusal | Condition |
|---|---|
| Wrong machine | `uname -s`/`uname -m` do not match the target the installer carries. |
| Too old a macOS | `sw_vers -productVersion` is below the declared `min_macos`. An unreadable or non-numeric version on either side is not treated as a mismatch. |
| Corrupt or truncated download | The appended archive's SHA-256 does not match the digest baked into the shell header. |
| No way to check | None of `shasum`, `sha256sum`, or `openssl` is available — the check is refused rather than skipped. |
| Not a file | The installer was piped rather than saved; it has to read its own appended archive, so save it and run it by path. |

`--extract` is integrity-checked but deliberately **not** host-checked: it
installs nothing and runs nothing, so it stays usable for inspecting an
artifact on the machine that just reported it does not fit.

After those gates pass, the installer unpacks to a temporary directory and runs
the *extracted* binary's own `install` and `setup` verbs. The default prefix,
the version directory, and the program the launchd plist names are the
runtime's own answers, never paths the shell script re-derives.

## What gets installed where

```text
~/.local/orch-hub/versions/<version>/    one payload per version, never mutated;
                                         bounded by retention (see below)
~/.local/orch-hub/current -> versions/X  which one is live; swapped atomically
~/.local/orch-hub/previous               which one it was; written the first
                                         time the selection moves
~/.local/orch-hub/.lock                  held while one step changes any of
                                         the above
~/.local/bin/orch-hub -> ~/.local/orch-hub/current/orch-hub
```

Placing a version, selecting one, pruning, and removing an installation each
hold `.lock` exclusively for the length of that step, so those steps queue
rather than interleave against one prefix. **The lock does not span a whole
command**: `upgrade` releases it before it restarts the service, and `rollback`
before it waits for health, so two commands running at once against one prefix
can still finish in an order neither of them reports. Run them one at a time.

A version number may not end in `.incoming`, `.previous`, `.removing`, or
`.spare`, in any capitalisation: those name the installer's own scratch paths
beside a version, and the filesystem here does not tell the spellings apart.

Nothing is installed system-wide and nothing needs `sudo`. The launchd plist
names `<prefix>/current/orch-hub`, so **selecting a version is a symlink swap
plus a service restart** — the installed service configuration, its token, and
the hub's own data under `ORCH_HUB_DATA_DIR` are never rewritten to change
which code runs. That is what makes an upgrade cheap and a rollback safe.

If `~/.local/bin` is not on your `PATH`, the install output says so and prints
the `export` line to add.

## Configure or reconfigure the service

`orch-hub setup` is the checkout-independent counterpart of `launchd deploy`:
there is no package to install and no virtualenv to create, so it resolves
configuration, writes the plist, bootstraps launchd, and waits for the service
to answer `GET /api/health`. Health is verified rather than assumed, and a
service that does not come up exits non-zero.

```bash
# First configuration (what the installer runs for you).
orch-hub setup --mandrel /absolute/path/to/mandrel

# Rebind to the Mac's Tailscale IP; everything else is preserved.
orch-hub setup --host "$(tailscale ip -4)"

# Rotate the bearer token.
orch-hub setup --token "$(openssl rand -hex 32)"
```

Resolution order, per setting:

| Setting | Resolution |
|---|---|
| Mandrel checkout | `--mandrel`, then `ORCH_HUB_MANDREL_PATH`, then the legacy `ORCH_HUB_AI_NATIVE_DEPLOYMENT`, then the installed plist. There is no portable default; the first setup fails with guidance if none exists. The value is verified: it must be a directory holding an **executable** `bin/mandrel`, because the registry runs that file directly. |
| Bearer token | `--token`, then `ORCH_HUB_TOKEN`, then the installed plist, then a generated token printed once. |
| Host / port / data dir | corresponding flag, then environment, then the installed plist, then default (`127.0.0.1`, `8765`, `~/.orch-hub`). |
| Plist / log paths | `--plist-path` / `--log-dir`, then the `~/Library/...` defaults. |
| Bark push URL | `ORCH_HUB_BARK_URL`, then the value already in the installed plist, else disabled. No flag — the URL embeds a device-key secret, so it is supplied through the environment and never printed. |

The installed plist is written mode `600` because it embeds the bearer token.
Empty or whitespace-only environment values count as unset; a malformed
explicit value such as a non-numeric `ORCH_HUB_PORT` fails loudly instead of
silently selecting a default. The full variable reference is in
[launchd deployment](launchd.md#environment-variables).

## Everyday commands

An installed runtime is one executable with the same command surface a source
checkout has:

```bash
orch-hub version              # which runtime is running (frozen vs source)
orch-hub versions             # what is installed, and which is current/previous
orch-hub repos                # managed repos and launch readiness
orch-hub serve                # foreground HTTP service, for a quick check

orch-hub launchd status       # label, plist, load state, dashboard URL, health
orch-hub launchd start        # bootstrap the installed service
orch-hub launchd stop         # unload it (does not stop orchestrator runs)
orch-hub launchd restart      # stop then start

orch-hub notify test          # prove push delivery end to end
orch-hub eval doctor          # Layer 2 semantic evaluator readiness
orch-hub eval queue           # Layer 2 attempts: census plus the rows worth reading
orch-hub eval catalog verify  # global report catalog integrity

orch-hub statusline           # optional Claude statusLine wrapper (see below)
```

Commands with a stable machine-readable form take `--json`, including `repos`,
`version`, `versions`, `install`, `eval queue`, and both `eval catalog` verbs.

`serve` exists because a compiled release ships **one** executable: the
`orch-hub-api` console script a source install gets from packaging metadata
does not exist here, so both runtimes agree on one spelling of "serve the API".
`statusline` is the same story for the optional Claude statusLine wrapper —
`orch-hub-statusline` on a checkout, `orch-hub statusline` here, one wrapper
either way, with everything after `--` passed to your own status line command
untouched. It is not something a release needs you to install: Claude
subscription usage is read from the CLI's own local cache and requires no
statusLine setup ([Claude usage](claude-usage.md)).

`launchd deploy` and `update` are **not** for an installed runtime: they install
a checkout's package into `<checkout>/.venv` and point launchd at that. Use
`setup` and `upgrade` instead.

## Upgrade

Run the newer installer. That is the whole procedure:

```bash
sh orch-hub-0.2.0-darwin-arm64.run
```

Nothing about the running configuration is passed on the command line, because
nothing needs to be: the plist rewrite re-resolves every setting from the
environment and the installed plist, so the **token, Mandrel checkout, bind
address, data directory, and push endpoint all survive**. Hub-owned data — run
records, history, intake records, evaluation artifacts, the report catalog — is
never touched by an upgrade.

If you already have an extracted payload directory, the same operation is
available directly:

```bash
orch-hub upgrade --from /path/to/orch-hub-darwin-arm64
```

The version being replaced stays installed under its own name. If the new
version installs but the service does not come back, the error says so and
names the verb that returns to the old one:

```text
error: installed 0.2.0 but could not start it: <reason>; the previous version
is still installed — run `orch-hub rollback --to 0.1.0`
```

Restarting the hub service does not stop orchestrator runs. They are detached
process sessions and keep going; see
[stop semantics](launchd.md#5-stop-semantics).

## Rollback

```bash
orch-hub rollback              # back to the previously selected version
orch-hub rollback --to 0.1.0   # or name one explicitly
orch-hub versions              # what is available to roll back to
```

The plist is not rewritten — it names `<prefix>/current/orch-hub` — so a
rollback changes the program and nothing else: service configuration and hub
data stay exactly as they were, and no network or installer is involved.

It refuses a version that is not installed under the prefix, a version that is
already selected, and a bare `rollback` when no previous version was recorded
(the error then lists what is installed).
The health probe target is read *before* the selection moves, so an unreadable
service configuration fails while the running version is still the selected one.

## Retention

Every version is a full ~30MB payload, so a prefix that never removed one grew
by that much on every upgrade. **Install and upgrade keep the newest three
versions and remove the rest.** Three things are never removed, however old
they are:

- the selected `current` version — the program launchd runs;
- the version `previous` names — where a bare `rollback` goes;
- the payload the running command is executing from.

So the bound is "three, plus whatever is in use": if you are sitting on an old
version after a rollback, it is kept and the set is four. Space is never
reclaimed by taking away something that is in use.

What you can rely on for rollback: **everything `orch-hub versions` lists is a
valid `rollback --to` target, and that listing is the whole set.** It ends with
a line stating the rule, so the retained set is legible before and after an
upgrade:

```text
prefix: /Users/you/.local/orch-hub
0.3.0
0.4.0 (previous)
0.5.0 (current)
retention: the newest 3, plus current and previous; installing prunes the rest
```

An install or upgrade reports what it removed and what it kept:

```text
upgraded orch-hub 0.4.0 -> 0.5.0
removed: 0.2.0
kept: 0.3.0, 0.4.0, 0.5.0
```

A pruned version is not lost: reinstall it from its release artifact with
`sh orch-hub-<version>-darwin-arm64.run` or `orch-hub install --from <payload>`.
Nothing goes with it — service configuration and hub data live outside the
prefix and are untouched by any of this.

Pruning never fails an install or an upgrade; the version you asked for is
installed and running either way. **Removal is all or nothing.** Before a doomed
version's files are deleted it is renamed out of `versions/` and copied to a
`.spare` name beside it — a copy that shares the payload's files rather than
duplicating them, so it costs directory entries and not another 30MB. Those
spares are dropped only after every doomed version has been deleted, which is
what lets a deletion that fails halfway be undone: it removed one of two names
for each file, and the version goes back exactly as it was. Deleting a directory
tree is not an operation the filesystem can promise to finish, and no check made
beforehand changes that — so retention keeps the means to undo it instead.

So **a prune that reports a problem has removed nothing**, and everything
`orch-hub versions` listed before it is still installed and still a valid
`rollback --to` target. What a failure can cost is disk, never a version:

| What happened | What you see | What is true |
|---|---|---|
| A version could not be moved aside or copied | `note: kept every version: cannot remove <version>: <reason>` | Nothing was removed; every version is still selectable |
| The prefix was busy or unwritable | `note: kept every version: cannot lock <prefix>: <reason>` | Nothing was removed; retention runs again on the next install |
| A deletion failed, having already removed part of a payload | `note: kept every version: cannot remove <version>: <reason>; left scratch at <path>` | Every version was put back whole and is selectable. What is left at that path shares its files with the version that came back, so it costs no disk while that version is installed — remove it by hand; no later prune will touch a path it did not create |
| A version could not be put back | `note: cannot remove <version>: <reason>; <version> is whole at <path> but could not be put back` | Nothing of that version is lost; it is not listed until you rename that path to `versions/<version>` |
| Every version was removed, but a spare would not go | `note: removed every version, but their disk is not all reclaimed: <path> could not be removed; remove by hand` | The versions are gone as intended; that path holds one payload's bytes until you remove it |

A version that refuses to delete therefore stops retention for the whole prefix
until you deal with it, and says so on every install. Working around it would
mean removing other versions while that one cannot be put back, which is the
trade this refuses to make.

## Uninstall

```bash
orch-hub uninstall                      # keep run history and evaluations
orch-hub uninstall --purge-data --yes   # also delete the hub data dir
```

Uninstall stops the service, removes the installed plist, removes the
`orch-hub` command link when it points into this prefix, and removes the
prefix. The output reports each of those individually.

Two things are established before anything is deleted. The prefix must be an
installation this installer made — `--prefix` takes any path, and a recursive
removal is the wrong place to discover a typo. And the service must actually be
stopped: if it cannot be unloaded, **nothing is removed**, rather than leaving a
running process with no configuration.

The hub data directory is left alone unless `--purge-data --yes` is given
together: run history, audit events, and evaluation artifacts are not
re-creatable from a release, so removing a program must not remove them by
default. `--purge-data` without `--yes` is refused.

Uninstall does not stop orchestrator runs. Detached runs keep their own process
sessions; stop them from the dashboard or `POST /api/runs/{run_id}/stop` before
removing the hub if you want them ended.

## Release install vs. checkout install

| | Source checkout | Release |
|---|---|---|
| Prerequisite | Python 3.10+, a venv, the repository | none beyond macOS and Mandrel |
| Install | `pip install .` into `<checkout>/.venv` | `sh <artifact>.run` |
| Configure the service | `orch-hub launchd deploy \| update` | `orch-hub setup` |
| Program launchd runs | `<checkout>/.venv/bin/orch-hub-api` | `<prefix>/current/orch-hub serve` |
| Foreground service | `orch-hub-api` | `orch-hub serve` |
| statusLine wrapper | `orch-hub-statusline` (optional, legacy) | `orch-hub statusline` (same, optional) |
| Update | `git pull` + `launchd update` | run the newer `.run`, or `orch-hub upgrade --from` |
| Go back a version | `git checkout` + `launchd update` | `orch-hub rollback` |
| Remove | delete the checkout, `launchd stop`, remove the plist | `orch-hub uninstall` |

Everything above the program boundary — routes, dashboard, run control,
evaluation, notifications, configuration variables — is identical. `orch-hub
version` reports which runtime you are on (`frozen` or `source`).

## Unsigned artifacts and macOS

Releases are **unsigned**: no Developer ID signature and no notarization.
Signing is an optional future wrapping/publishing stage, not a runtime
dependency, so nothing in the installed runtime depends on it.

In practice this does not require a workaround for the documented path. A `.run`
downloaded through a browser is marked `com.apple.quarantine`, but it is a shell
script run by path from a terminal, and the files it unpacks do not inherit the
flag — so the compiled binaries are not Gatekeeper-gated. Fetching with `curl`
and running with `sh` avoids the question entirely. If macOS does refuse a file
you downloaded, clear the flag explicitly rather than disabling Gatekeeper:

```bash
xattr -d com.apple.quarantine orch-hub-0.1.0-darwin-arm64.run
```

Because the artifacts are unsigned, `shasum -a 256 -c SHA256SUMS` against the
digests published with them is the integrity check that matters. The installer
independently verifies its own appended payload before unpacking, but that
proves the file is intact — not that it is the file that was published.

---

# Part 2 — producing a release

Three commands, run from a checkout, in this order:

```bash
python3 -m orch_hub.release_build     # -> dist/orch-hub-darwin-arm64/
python3 -m orch_hub.release_package   # -> dist/release/{*.run,release.json,SHA256SUMS}
python3 -m orch_hub.release_publish --tag v0.1.0 --to ~/Releases/orch-hub
```

Each takes `--json` for a machine-readable result. `dist/` and `build/` are
gitignored; the build's scratch state lives in `build/release/`.

The host must already be the target — a freezer bundles the interpreter and
shared libraries of the machine it runs on, so **cross-building is not
attempted**. What the tag drives is publication, not production: the artifacts
are made on a machine, and moving production onto a CI runner later changes who
fills `dist/release` and nothing about what is checked before it leaves.

## Build

```bash
python3 -m orch_hub.release_build
```

Produces one payload directory — the executable plus the libraries,
interpreter, and resources it needs — carrying a `runtime.json` that records
version, target, architecture, the freezer, the interpreter version, the source
revision it was frozen from, and the minimum macOS the produced binaries
actually require, read back out of the Mach-O load commands rather than assumed
from the build host. Nothing in that manifest is a path, so it carries no trace
of the builder.

`source_revision` is what lets publication prove the bytes came from the source
being tagged, so it says exactly one of three things:

| Recorded | Means |
|---|---|
| `<commit>` | The work tree was that commit, and the freezer read the commit rather than the directory (below). This is the only form publication accepts. |
| `<commit>-dirty` | That commit with uncommitted changes on top — tracked edits or untracked files, because the freezer reads the working tree and an untracked `orch_hub/*.py` ships as readily as a committed one. |
| `null` | No revision could be read: no git on the machine, a source export with no history, a repository with no commit yet. |

The last two are recorded rather than refused. A build has to work from a source
export, and a local smoke build from a modified tree is the ordinary case; a
payload that honestly declares it cannot name its source is one nobody can
publish, not one nobody can build. The build prints what it recorded, saying so
when the payload is unpublishable. Git being absent never fails a build.

**A build off a clean checkout freezes the commit, not the directory.** The
revision is read first, and then `git archive` writes that commit's tree out of
the object database into `build/release/source/`; the freezer reads that copy.
A build takes minutes, and reading the working tree throughout them would let a
maintainer who edits and commits mid-build leave a checkout that is clean at a
*new* commit — a payload of mixed old and new source, recorded under a commit
publication would accept. Probing the checkout again at the end does not fix
that; it only compares two observations of something that was free to move
between them. A tree with nothing to export — modified, or with no history —
is frozen where it lies, and records `-dirty` or `null` accordingly.

Three things make the payload a property of the source rather than of the
machine:

- **The build is declared, not discovered.** The freezer is pinned exactly in
  `pyproject.toml`'s `release` extra, and the interpreter is pinned in
  `orch_hub/release_python.py` — addressed by URL, proven by digest, unpacked
  into the build directory, installed nowhere and needing no privileges. The
  interpreter is pinned for the same reason the freezer is: it decides what the
  artifact *is*, down to which macOS versions it runs on.
- **The project is deliberately not installed into the build environment.** The
  freezer reads first-party code from the settled source tree, so a build can
  never ship a stale copy from `site-packages`.
- **The source is settled before anything slow starts.** The revision, the
  version, and the dependencies the build environment is made of are all read
  from the exported tree, so they describe one moment rather than three.

| The build refuses | Because |
|---|---|
| Audit findings in the staged payload | Shipping a maintainer's absolute path or bearer token inside a binary is silent and unrecoverable after publication. `--allow-findings` reports without failing. |
| A payload requiring a newer macOS than the release supports | Above the declared floor the artifact is only wrong on somebody else's Mac. `--allow-unsupported-macos` overrides — never for a published artifact. |
| A commit git named but could not export | The only fallback would be to freeze the working tree while recording that commit as its source — the one thing the export exists to prevent. Failing to *read* a revision still never fails a build. |

Both run on the *staged* build, so a rejected candidate is never published and
the last payload that passed stays where the installer and the smoke test look
for it.

The audit reaches the code that actually runs, which is not in any file: the
freezer's archives are opened, including the one appended to the executable and
the module archive nested inside it. It looks for first-party `.py`/`.pyc`,
bundled `tests`, repository/task/protocol state (`.git`, `.ai`, `.ai-tasks`,
`.ai-protocol`, `.mandrel`, agent config directories), and machine-specific
literals — home/checkout paths, the bearer token, the Bark endpoint. Findings
name the file and the label; **the matched value itself is never quoted** into
the report, the build log, or any JSON a pipeline keeps.

Useful flags: `--out`, `--work`, `--venv`, `--python` (name another interpreter
instead of the pin — it becomes the one bundled, and therefore the one that
sets the minimum macOS), `--skip-install`, `--no-clean`.

Once a payload exists, `tests/test_frozen_runtime.py` drives it: it runs the
compiled binary under a scrubbed environment and a `PATH` with no interpreter
on it, so "needs no Python and no checkout" is enforced by the harness rather
than asserted. It reads `dist/orch-hub-darwin-arm64` by default
(`ORCH_HUB_RUNTIME_PAYLOAD` points it elsewhere) and **skips** when no payload
is present, rather than passing vacuously — so run a build before trusting a
green suite as evidence about the artifact.

## Package

```bash
python3 -m orch_hub.release_package
```

Turns the payload directory into the three files a release ships. Nobody can be
handed a directory.

- **The artifact set is a pure function of the payload.** The archive is built
  with fixed member order, timestamps, ownership, and normalised modes, and
  nothing anywhere records a build time. Packaging the same payload twice
  produces byte-identical files, so "immutable release" is checkable rather
  than promised.
- **Packaging re-checks the payload.** The audit and the macOS floor check run
  again here. They are the same checks at two different moments, and only the
  second one covers a payload that was built once — or built with the build's
  own escape hatch — and packaged later.
- **The installer's gates are baked in.** Target, minimum macOS, and the
  archive's SHA-256 are substituted into the shell header from
  `deploy/release/installer.sh`, which is the structure's source of truth and
  is not runnable as-is.

`--payload` may be repeated for a multi-target release; `release.json` is keyed
by target for exactly that. `--out`, `--work`, `--template`, `--allow-findings`,
and `--allow-unsupported-macos` are also available.

## Publish

```bash
# Private storage: hands consumers files, with no repository access.
python3 -m orch_hub.release_publish --tag v0.1.0 --to ~/Releases/orch-hub

# Public (or a separate public releases repo): one flag, not one rewrite.
python3 -m orch_hub.release_publish --tag v0.1.0 --to github:qinglin89/orch-hub
```

A filesystem destination places the artifact set in `<path>/<tag>/`. This is
the channel a private engineering release needs: a consumer of a *private*
repository's GitHub release still needs read access to the repository itself,
which is the thing this release path exists to avoid.

The `github:` destination shells out to `gh release create` and always passes
`--verify-tag`, so the tag must already exist on the remote. Without it `gh`
would create a missing tag from the default branch's head, publishing artifacts
under a tag pointing at a commit they were never verified against — the exact
binding this step exists to make.

| Publication refuses | Because |
|---|---|
| A tag that does not name this release's version | `--tag v0.2.0` over a 0.1.0 artifact set files bytes under a name that is not theirs. |
| A checkout that declares a different version | `dist/` survives every build, so what is sitting there is whatever was built last — which is the *previous* version's artifact set exactly when the release being tagged is a version bump. |
| A tag that does not exist, or a checkout not at it | "The artifacts for v0.1.0" otherwise means "whatever was in `dist/` when somebody typed a tag name". |
| A dirty working tree | A tagged commit with edits on top is a checkout whose state has no name. |
| A payload whose `source_revision` is not the tag's commit | Every other check holds for a `dist/` filled by a build from last week at the same version. The payload names the commit it was frozen from, and it has to be this one — every target, not the first. |
| A payload built from a modified tree, or recording no revision | A `<commit>-dirty` payload's source has no name to publish under; a `null` one was built where its source had no history. Neither is fixed by re-tagging. |
| Digests, sizes, names, or `SHA256SUMS` that disagree with the bytes | Every entry in `release.json` is recomputed from the files on disk; `SHA256SUMS` must agree with it and cover exactly the same set; the `.run` header must name the version and target it is filed under. |
| A destination that already holds this tag | The one guarantee a consumer cannot verify locally is that the bytes behind a version did not change after they were handed out. |
| A release directory, scratch directory, or destination nested inside another | The scratch tree is created and cleared by the run; a release or destination under it would be destroyed by the run meant to read or fill it, and a scratch tree under either puts working files inside the set being verified or beside the tags being handed out. Refused in both directions. |

Publication assembles and re-checks an invocation-owned staging directory first.
For a filesystem destination, it brings those files onto the destination's
filesystem before claiming the tag with a single `mkdir`, then fills the claimed
directory with same-filesystem renames. Two concurrent publications of one tag
therefore produce one winner and one clear refusal, never a success report
describing bytes that another publisher placed. The claimed directory can be
briefly visible while it is being filled; a placement failure removes it, so a
failed first publication leaves no canonical partial tree behind.

Alongside the artifacts it writes `publication.json`: schema, name, version,
tag, commit, and the file inventory **including the digest of `SHA256SUMS`
itself**. There is no timestamp in it, deliberately — a publication that is a
pure function of its tag and its bytes can be produced twice and compared.

**What the three bindings cover.** The tag proves the *checkout* is the tag; the
inventory re-derivation proves the artifact set agrees with its own bytes; and
`source_revision` proves the *payload* was frozen from the commit being tagged.
They bound different failures and none replaces another — the first two both
hold perfectly for a `dist/` that was filled before the source changed, which is
exactly what the third refuses.

## Verifying a release you received

```bash
# The digests the release publishes for itself.
shasum -a 256 -c SHA256SUMS

# Host compatibility and payload integrity, without installing anything.
sh orch-hub-0.1.0-darwin-arm64.run --check

# Unpack the payload instead of installing it. Extraction is integrity-checked
# but not host-checked, so it works even on a machine the runtime does not fit;
# the extracted binary itself still only runs on the target.
sh orch-hub-0.1.0-darwin-arm64.run --extract /tmp/inspect
/tmp/inspect/orch-hub-darwin-arm64/orch-hub version
```

For a published set, `publication.json` additionally names the tag, the commit,
and the digest of `SHA256SUMS` — which is what lets you check the checksum file
itself rather than trusting it. That commit is the revision the payloads were
frozen from: publication refuses a release whose `release.json` records any
other one under `targets.<target>.runtime.source_revision`, so the two documents
can be compared and must agree.
