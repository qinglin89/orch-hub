#!/bin/sh
# orch-hub one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/qinglin89/orch-hub/dev/install.sh | sh
#
# Takes an Apple-Silicon Mac from nothing to a running orch-hub dashboard:
#
#   1. checks the machine: macOS on Apple Silicon, git, curl, a sha256 tool;
#   2. finds mandrel (the protocol orch-hub drives) or clones it at a pinned
#      release into ~/.local/share/mandrel, with a Python 3.11+ to run it;
#   3. downloads the pinned orch-hub release, verifies its checksums, and runs
#      its installer: user-local, no sudo, a launchd service on 127.0.0.1;
#   4. opens the dashboard with the bearer token on the clipboard.
#
# Re-running it is safe. An existing mandrel checkout is reused exactly as it
# is and never moved to another version (that would put every repo it manages
# out of sync), and the release installer keeps the installed token, bind
# address, data directory, and push endpoint.
#
# Nothing here needs root, and nothing is installed system-wide. The only thing
# it may install outside its own directories is Python and jq through an
# existing Homebrew, announced first and skippable with --no-brew.
#
# The file is function definitions down to `main "$@"` on its last line, so a
# download cut short anywhere runs nothing at all.

set -eu

ORCH_HUB_REPO='qinglin89/orch-hub'
DEFAULT_VERSION='0.2.0'
TARGET='darwin-arm64'
MANDREL_DEFAULT_REPO='https://github.com/qinglin89/mandrel.git'
MANDREL_DEFAULT_REF='v2.4.0'
AGENT_GUIDE='https://github.com/qinglin89/orch-hub/blob/dev/docs/agent-setup.md'
SOURCE_INSTALL='https://github.com/qinglin89/orch-hub#run-from-source'
PLIST_NAME='com.qinglin89.orch-hub.plist'
BREW_PYTHON='python@3.13'

say() {
    printf '%s\n' "$*"
}

step() {
    printf '==> %s\n' "$*"
}

warn() {
    printf 'warning: %s\n' "$*" >&2
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<USAGE
orch-hub installer

Usage: install.sh [options]
       curl -fsSL https://raw.githubusercontent.com/$ORCH_HUB_REPO/dev/install.sh | sh -s -- [options]

Installs mandrel (unless you already have it) and the orch-hub release, starts
the dashboard service on this Mac, and opens it.

Options:
  --mandrel DIR      use the mandrel checkout at DIR, or clone mandrel into DIR
                     when it does not exist yet (default: the checkout orch-hub
                     is already configured with, else ~/.local/share/mandrel)
  --version VERSION  orch-hub release to install (default: $DEFAULT_VERSION;
                     "latest" asks GitHub for the newest release)
  --no-brew          never install Python or jq with Homebrew; stop and explain
  --no-open          do not open the dashboard when done
  --dry-run          check this machine and print the plan; change nothing
  -h, --help         print this help

Passed through to the release installer:
  --host ADDR  --port N  --token VALUE  --prefix DIR  --bin-dir DIR  --no-service

Environment (mirrors and testing): ORCH_HUB_VERSION, ORCH_HUB_RELEASE_URL
(directory URL holding the release files), ORCH_HUB_MANDREL_PATH,
ORCH_HUB_MANDREL_REPO, ORCH_HUB_MANDREL_REF.
USAGE
}

nonblank() {
    [ -n "$2" ] || die "$1 needs a value"
}

# --- machine checks -----------------------------------------------------------

check_host() {
    _os=$(uname -s 2>/dev/null || printf unknown)
    _arch=$(uname -m 2>/dev/null || printf unknown)
    if [ "$_os" != Darwin ] || [ "$_arch" != arm64 ]; then
        die "the one-line install supports macOS on Apple Silicon only (this is $_os $_arch); run orch-hub from source instead: $SOURCE_INSTALL"
    fi
}

check_not_root() {
    if [ "$(id -u)" -eq 0 ] && [ -z "${ORCH_HUB_ALLOW_ROOT:-}" ]; then
        die "do not run this as root or with sudo: orch-hub installs for your user only"
    fi
}

check_tools() {
    command -v curl >/dev/null 2>&1 || die "curl is required"
    if ! command -v git >/dev/null 2>&1 || ! git --version >/dev/null 2>&1; then
        die "git is required: install the Command Line Tools with 'xcode-select --install', then re-run"
    fi
    if command -v shasum >/dev/null 2>&1; then
        SHA_CHECK='shasum -a 256 -c'
    elif command -v sha256sum >/dev/null 2>&1; then
        SHA_CHECK='sha256sum -c'
    else
        die "shasum or sha256sum is required to verify the download"
    fi
}

find_brew() {
    BREW=$(command -v brew 2>/dev/null || true)
}

python_is_new_enough() {
    "$1" -I -S -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1
}

# Same preference order as mandrel's own bin/mandrel: versioned names promise
# their version; an unversioned python3 counts only when it is new enough (the
# one macOS ships is not).
find_python() {
    PYTHON=''
    for _candidate in python3.16 python3.15 python3.14 python3.13 python3.12 python3.11; do
        if command -v "$_candidate" >/dev/null 2>&1; then
            PYTHON=$(command -v "$_candidate")
            return 0
        fi
    done
    for _candidate in python3 python; do
        if command -v "$_candidate" >/dev/null 2>&1 && python_is_new_enough "$_candidate"; then
            PYTHON=$(command -v "$_candidate")
            return 0
        fi
    done
    return 1
}

python_instructions() {
    say "mandrel needs Python 3.11 or newer, and none was found on PATH. Install one, then re-run:"
    say "  brew install $BREW_PYTHON        (Homebrew: https://brew.sh)"
    say "  or the macOS installer from https://www.python.org/downloads/macos/"
}

ensure_python() {
    if find_python; then
        return 0
    fi
    if [ -n "$BREW" ] && [ "$use_brew" = yes ]; then
        if [ "$dry_run" = yes ]; then
            PYTHON="(would install $BREW_PYTHON with Homebrew)"
            return 0
        fi
        step "Installing Python ($BREW_PYTHON) with Homebrew; mandrel needs Python 3.11+"
        "$BREW" install "$BREW_PYTHON" </dev/null || die "brew install $BREW_PYTHON failed"
        find_python && return 0
        die "$BREW_PYTHON was installed but no python3.1x is on PATH; open a new terminal and re-run"
    fi
    python_instructions >&2
    exit 1
}

ensure_jq() {
    command -v jq >/dev/null 2>&1 && return 0
    if [ -n "$BREW" ] && [ "$use_brew" = yes ]; then
        if [ "$dry_run" = yes ]; then
            say "jq:       would install with Homebrew (mandrel's agent hooks need it)"
            return 0
        fi
        step "Installing jq with Homebrew; mandrel's agent hooks need it"
        "$BREW" install jq </dev/null || warn "brew install jq failed; install jq before running tasks"
        return 0
    fi
    warn "jq is not installed; mandrel's agent hooks need it (brew install jq)"
}

check_agents() {
    for _agent in claude codex cursor-agent; do
        command -v "$_agent" >/dev/null 2>&1 && return 0
    done
    warn "no coding-agent CLI found (claude, codex, or cursor-agent); orch-hub runs need at least one"
}

# --- mandrel ------------------------------------------------------------------

installed_plist_mandrel() {
    _plist="$HOME/Library/LaunchAgents/$PLIST_NAME"
    [ -f "$_plist" ] || return 0
    [ -x /usr/libexec/PlistBuddy ] || return 0
    for _key in ORCH_HUB_MANDREL_PATH ORCH_HUB_AI_NATIVE_DEPLOYMENT; do
        _value=$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:$_key" "$_plist" 2>/dev/null || true)
        if [ -n "$_value" ]; then
            printf '%s\n' "$_value"
            return 0
        fi
    done
}

is_mandrel_checkout() {
    [ -f "$1/bin/mandrel" ] && [ -d "$1/canonical" ]
}

# Decides MANDREL_DIR and whether it has to be cloned. An explicitly named or
# already-configured checkout is used as-is; only the default location is ever
# created by this script.
resolve_mandrel() {
    MANDREL_CLONE=no
    if [ -n "$mandrel_arg" ]; then
        MANDREL_DIR=$mandrel_arg
        MANDREL_ORIGIN='--mandrel'
    elif [ -n "${ORCH_HUB_MANDREL_PATH:-}" ]; then
        MANDREL_DIR=$ORCH_HUB_MANDREL_PATH
        MANDREL_ORIGIN='ORCH_HUB_MANDREL_PATH'
    else
        MANDREL_DIR=$(installed_plist_mandrel)
        MANDREL_ORIGIN='the installed orch-hub service'
        if [ -z "$MANDREL_DIR" ]; then
            MANDREL_DIR="$HOME/.local/share/mandrel"
            MANDREL_ORIGIN='default location'
        fi
    fi
    case $MANDREL_DIR in
        /*) ;;
        *) MANDREL_DIR="$(pwd)/$MANDREL_DIR" ;;
    esac
    if is_mandrel_checkout "$MANDREL_DIR"; then
        return 0
    fi
    if [ -e "$MANDREL_DIR" ] && [ -n "$(ls -A "$MANDREL_DIR" 2>/dev/null)" ]; then
        die "$MANDREL_DIR ($MANDREL_ORIGIN) exists but is not a mandrel checkout; pass --mandrel with the right path"
    fi
    if [ "$MANDREL_ORIGIN" = 'the installed orch-hub service' ]; then
        die "the installed service names mandrel at $MANDREL_DIR, which is missing; pass --mandrel DIR"
    fi
    MANDREL_CLONE=yes
}

clone_mandrel() {
    _partial="$MANDREL_DIR.partial.$$"
    rm -rf "$_partial"
    mkdir -p "$(dirname "$MANDREL_DIR")"
    step "Cloning mandrel $MANDREL_REF into $MANDREL_DIR"
    git clone --quiet "$MANDREL_REPO" "$_partial" </dev/null ||
        { rm -rf "$_partial"; die "cannot clone $MANDREL_REPO"; }
    git -C "$_partial" -c advice.detachedHead=false checkout --quiet "$MANDREL_REF" </dev/null ||
        { rm -rf "$_partial"; die "mandrel has no ref $MANDREL_REF"; }
    is_mandrel_checkout "$_partial" ||
        { rm -rf "$_partial"; die "$MANDREL_REPO at $MANDREL_REF is not a mandrel checkout"; }
    rmdir "$MANDREL_DIR" 2>/dev/null || true
    mv "$_partial" "$MANDREL_DIR"
}

# A checkout this script created gets its own .venv: bin/mandrel prefers it,
# which pins mandrel's interpreter independently of any PATH (the launchd
# service's PATH is minimal). A checkout the user brought is left untouched.
pin_mandrel_python() {
    [ -x "$MANDREL_DIR/.venv/bin/python" ] && return 0
    step "Pinning mandrel's Python ($PYTHON)"
    "$PYTHON" -m venv "$MANDREL_DIR/.venv" </dev/null ||
        die "cannot create $MANDREL_DIR/.venv with $PYTHON"
}

check_mandrel_runs() {
    if ! _out=$("$MANDREL_DIR/bin/mandrel" registry list --json 2>&1 </dev/null); then
        printf '%s\n' "$_out" >&2
        die "mandrel at $MANDREL_DIR does not run; fix the error above (an older checkout may need 'git -C $MANDREL_DIR pull')"
    fi
}

# --- orch-hub release ---------------------------------------------------------

resolve_version() {
    if [ "$VERSION" = latest ]; then
        VERSION=$(curl -fsSL "https://api.github.com/repos/$ORCH_HUB_REPO/releases/latest" </dev/null |
            sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' | head -n 1) || true
        [ -n "$VERSION" ] || die "cannot resolve the latest orch-hub release; pass --version"
    fi
    VERSION=${VERSION#v}
    ARTIFACT="orch-hub-$VERSION-$TARGET.run"
    RELEASE_URL=${ORCH_HUB_RELEASE_URL:-"https://github.com/$ORCH_HUB_REPO/releases/download/v$VERSION"}
}

download_release() {
    step "Downloading orch-hub $VERSION ($TARGET)"
    for _file in "$ARTIFACT" release.json SHA256SUMS; do
        curl -fsSL --retry 3 -o "$WORK_DIR/$_file" "$RELEASE_URL/$_file" </dev/null ||
            die "cannot download $RELEASE_URL/$_file"
    done
    step "Verifying checksums"
    (cd "$WORK_DIR" && $SHA_CHECK SHA256SUMS >/dev/null) ||
        die "checksum verification failed; the download is incomplete or altered"
}

cleanup() {
    _status=$?
    rm -rf "$WORK_DIR"
    exit "$_status"
}

run_release_installer() {
    step "Installing orch-hub $VERSION"
    sh "$WORK_DIR/$ARTIFACT" --mandrel "$MANDREL_DIR" "$@" </dev/null
}

open_dashboard() {
    _command="${prefix:-$HOME/.local/orch-hub}/current/orch-hub"
    if [ -x "$_command" ] && "$_command" open </dev/null; then
        return 0
    fi
    say "Open the dashboard at http://127.0.0.1:${port:-8765}/ and sign in with the token ('orch-hub token --copy' copies it)."
}

print_plan() {
    say "machine:  macOS $(sw_vers -productVersion 2>/dev/null || printf '?') on Apple Silicon"
    if [ "$MANDREL_CLONE" = yes ]; then
        say "mandrel:  clone $MANDREL_REPO at $MANDREL_REF into $MANDREL_DIR"
    else
        say "mandrel:  use $MANDREL_DIR ($MANDREL_ORIGIN), unchanged"
    fi
    say "python:   ${PYTHON:-not needed}"
    say "orch-hub: $RELEASE_URL/$ARTIFACT"
    if [ "$want_service" = yes ]; then
        say "service:  launchd agent on http://${host:-127.0.0.1}:${port:-8765}/"
    else
        say "service:  not configured (--no-service)"
    fi
}

print_next_steps() {
    say ''
    say 'orch-hub is installed.'
    say "  mandrel: $MANDREL_DIR"
    if [ "$want_service" = no ]; then
        say "  service: not configured; start it with: orch-hub setup --mandrel $MANDREL_DIR"
        return 0
    fi
    say '  token:   orch-hub token --copy   (copies the dashboard sign-in token)'
    say ''
    say 'Next, add a repo. Tell your coding agent:'
    say "  Add my repo at /path/to/your-repo to orch-hub by following $AGENT_GUIDE#add-a-repo"
}

# --- main ---------------------------------------------------------------------

# Single-quotes a value for the `eval` that rebuilds the passthrough arguments,
# so a token or path with spaces or quotes survives intact.
quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}


main() {
    mandrel_arg=''
    VERSION=${ORCH_HUB_VERSION:-$DEFAULT_VERSION}
    MANDREL_REPO=${ORCH_HUB_MANDREL_REPO:-$MANDREL_DEFAULT_REPO}
    MANDREL_REF=${ORCH_HUB_MANDREL_REF:-$MANDREL_DEFAULT_REF}
    use_brew=yes
    want_open=yes
    want_service=yes
    dry_run=no
    host=''
    port=''
    prefix=''
    passthrough=''

    while [ $# -gt 0 ]; do
        case $1 in
            --mandrel) mandrel_arg=${2:-}; nonblank "$1" "$mandrel_arg"; shift 2 ;;
            --mandrel=*) mandrel_arg=${1#--mandrel=}; nonblank --mandrel "$mandrel_arg"; shift ;;
            --version) VERSION=${2:-}; nonblank "$1" "$VERSION"; shift 2 ;;
            --version=*) VERSION=${1#--version=}; nonblank --version "$VERSION"; shift ;;
            --no-brew) use_brew=no; shift ;;
            --no-open) want_open=no; shift ;;
            --dry-run) dry_run=yes; shift ;;
            --no-service) want_service=no; want_open=no; passthrough="$passthrough --no-service"; shift ;;
            --host|--port|--token|--prefix|--bin-dir)
                nonblank "$1" "${2:-}"
                case $1 in
                    --host) host=$2 ;;
                    --port) port=$2 ;;
                    --prefix) prefix=$2 ;;
                esac
                passthrough="$passthrough $1=$(quote "$2")"
                shift 2 ;;
            --host=*|--port=*|--token=*|--prefix=*|--bin-dir=*)
                case $1 in
                    --host=*) host=${1#--host=} ;;
                    --port=*) port=${1#--port=} ;;
                    --prefix=*) prefix=${1#--prefix=} ;;
                esac
                passthrough="$passthrough ${1%%=*}=$(quote "${1#*=}")"
                shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option '$1'; run with --help" ;;
        esac
    done

    check_not_root
    check_host
    check_tools
    find_brew
    resolve_mandrel
    # Only a checkout this script creates needs an interpreter found for it; a
    # checkout the user brought already knows how to start, and if it cannot,
    # its own error says why.
    PYTHON=''
    if [ "$MANDREL_CLONE" = yes ]; then
        ensure_python
    fi
    resolve_version

    if [ "$dry_run" = yes ]; then
        ensure_jq
        print_plan
        exit 0
    fi

    ensure_jq
    check_agents
    if [ "$MANDREL_CLONE" = yes ]; then
        clone_mandrel
        pin_mandrel_python
    fi
    check_mandrel_runs

    WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/orch-hub-install.XXXXXX") ||
        die 'cannot create a temporary directory'
    trap cleanup EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    download_release
    eval "set -- $passthrough"
    run_release_installer "$@"

    if [ "$want_open" = yes ]; then
        open_dashboard
    fi
    print_next_steps
}

main "$@"
