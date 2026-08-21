#!/bin/sh
# orch-hub self-extracting installer — TEMPLATE.
#
# This file is the structure's source of truth; it is not runnable as-is.
# `python3 -m orch_hub.release_package` substitutes the placeholder assignments
# below and appends a gzipped tar of the payload directory after the final
# `exit`, producing `orch-hub-<version>-<target>.run`.
#
# The order of operations is the whole design. Everything that can say "this
# artifact does not belong on this machine" runs BEFORE a single byte is
# unpacked: the payload is a compiled binary for one os/arch/minimum macOS, so
# on a mismatched host it cannot even be executed to report the mismatch
# itself. Integrity is checked in the same breath, because a truncated download
# otherwise fails inside `tar` in a way that reads like a broken release rather
# than a broken transfer.
#
# Deliberately POSIX `sh` with no dependency beyond `uname`, `tar`, `mktemp`,
# and one of `shasum`/`sha256sum`/`openssl`: an engineer receiving this file has
# no Python, no orch-hub, and no promise of anything a login shell adds.

set -eu

VERSION='REPLACE-ORCH-HUB-VERSION'
TARGET='REPLACE-ORCH-HUB-TARGET'
TARGET_OS='REPLACE-ORCH-HUB-OS'
TARGET_ARCH='REPLACE-ORCH-HUB-ARCH'
MIN_MACOS='REPLACE-ORCH-HUB-MIN-MACOS'
PAYLOAD_DIR='REPLACE-ORCH-HUB-PAYLOAD-DIR'
COMMAND_NAME='REPLACE-ORCH-HUB-COMMAND'
ARCHIVE_SHA256='REPLACE-ORCH-HUB-ARCHIVE-SHA256'
ARCHIVE_LINE='REPLACE-ORCH-HUB-ARCHIVE-LINE'

SELF=$0

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<USAGE
orch-hub $VERSION installer ($TARGET)

Usage: $SELF [options]

Installs the compiled orch-hub runtime under a user-local prefix and starts the
resident launchd service. Re-running a newer installer upgrades in place: the
installed token, mandrel checkout, bind address, data directory, and push
endpoint are preserved, and the version being replaced stays installed so
\`$COMMAND_NAME rollback\` can go back to it.

Options:
  --prefix DIR    install prefix (default: the runtime's own ~/.local/orch-hub)
  --bin-dir DIR   directory the $COMMAND_NAME command is linked into
  --mandrel DIR   mandrel checkout; required the first time unless the
                  environment or an installed service already names one
  --token VALUE   bearer token (default: keep the installed one, else generate)
  --host ADDR     bind address; needed to reach the hub from another device
  --port N        bind port
  --no-service    install the runtime only; do not configure or start the service
  --extract DIR   unpack the payload into DIR and exit; installs nothing
  --check         verify host compatibility and payload integrity, then exit
  --version       print the version and target, then exit
  -h, --help      print this help
USAGE
}

nonblank() {
    [ -n "$2" ] || die "$1 needs a value"
}

mode=install
prefix=''
bin_dir=''
mandrel=''
token=''
host=''
port=''
extract_dir=''
want_service=yes

# Both `--opt value` and `--opt=value` are accepted: an installer is typed by
# hand under time pressure, and refusing one spelling is a support question.
# `nonblank` runs before the `shift 2`, so a trailing option with no value is
# reported by name instead of by the shell's own "can't shift that many".
while [ $# -gt 0 ]; do
    case $1 in
        --prefix) prefix=${2:-}; nonblank "$1" "$prefix"; shift 2 ;;
        --prefix=*) prefix=${1#--prefix=}; nonblank --prefix "$prefix"; shift ;;
        --bin-dir) bin_dir=${2:-}; nonblank "$1" "$bin_dir"; shift 2 ;;
        --bin-dir=*) bin_dir=${1#--bin-dir=}; nonblank --bin-dir "$bin_dir"; shift ;;
        --mandrel) mandrel=${2:-}; nonblank "$1" "$mandrel"; shift 2 ;;
        --mandrel=*) mandrel=${1#--mandrel=}; nonblank --mandrel "$mandrel"; shift ;;
        --token) token=${2:-}; nonblank "$1" "$token"; shift 2 ;;
        --token=*) token=${1#--token=}; nonblank --token "$token"; shift ;;
        --host) host=${2:-}; nonblank "$1" "$host"; shift 2 ;;
        --host=*) host=${1#--host=}; nonblank --host "$host"; shift ;;
        --port) port=${2:-}; nonblank "$1" "$port"; shift 2 ;;
        --port=*) port=${1#--port=}; nonblank --port "$port"; shift ;;
        --extract) extract_dir=${2:-}; nonblank "$1" "$extract_dir"; mode=extract; shift 2 ;;
        --extract=*) extract_dir=${1#--extract=}; nonblank --extract "$extract_dir"; mode=extract; shift ;;
        --no-service) want_service=no; shift ;;
        --check) mode=check; shift ;;
        --version) mode=version; shift ;;
        -h|--help) mode=help; shift ;;
        *) die "unknown option '$1'; run with --help" ;;
    esac
done

if [ "$mode" = help ]; then
    usage
    exit 0
fi
if [ "$mode" = version ]; then
    # Deliberately before every other check, including reading this file back:
    # identifying a download must work on the machine that turns out not to be
    # able to run it, and when it arrived through a pipe rather than a file.
    printf '%s %s (%s)\n' "$COMMAND_NAME" "$VERSION" "$TARGET"
    exit 0
fi

# Everything past here reads the archive appended to this file, so this file has
# to be a file. An installer is run by path (`./x.run` or `sh x.run`), so $0 is
# that path; anything else is reported rather than guessed at.
[ -f "$SELF" ] && [ -r "$SELF" ] ||
    die "cannot read this installer at '$SELF'; save it to a file and run it by path"

# Succeeds when dotted numeric version $1 is at least $2. Missing components
# count as zero, so `26` and `26.0.0` compare equal.
version_at_least() {
    _left=$1
    _right=$2
    while [ -n "$_left" ] || [ -n "$_right" ]; do
        _l=${_left%%.*}
        _r=${_right%%.*}
        [ -n "$_l" ] || _l=0
        [ -n "$_r" ] || _r=0
        if [ "$_l" -gt "$_r" ]; then
            return 0
        fi
        if [ "$_l" -lt "$_r" ]; then
            return 1
        fi
        case $_left in *.*) _left=${_left#*.} ;; *) _left='' ;; esac
        case $_right in *.*) _right=${_right#*.} ;; *) _right='' ;; esac
    done
    return 0
}

is_numeric_version() {
    case $1 in
        '' | *[!0-9.]* | .* | *. | *..*) return 1 ;;
    esac
    return 0
}

check_host() {
    _os=$(uname -s 2>/dev/null | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz')
    _arch=$(uname -m 2>/dev/null || printf 'unknown')
    if [ "$_os" != "$TARGET_OS" ] || [ "$_arch" != "$TARGET_ARCH" ]; then
        die "this installer carries the $TARGET runtime, but this machine is ${_os:-unknown}-${_arch:-unknown}"
    fi
    if [ "$TARGET_OS" != darwin ] || [ -z "$MIN_MACOS" ]; then
        return 0
    fi
    _macos=$(sw_vers -productVersion 2>/dev/null || printf '')
    # An unreadable or non-numeric version on either side is not evidence of a
    # mismatch; the runtime then fails or works on its own terms rather than
    # being refused over a string this check could not read. Same rule as the
    # runtime's own target check, so the two cannot disagree.
    if is_numeric_version "$_macos" && is_numeric_version "$MIN_MACOS"; then
        if ! version_at_least "$_macos" "$MIN_MACOS"; then
            die "this runtime requires macOS $MIN_MACOS or newer, but this machine runs $_macos"
        fi
    fi
}

archive_sha256() {
    if command -v shasum >/dev/null 2>&1; then
        tail -n +"$ARCHIVE_LINE" "$SELF" | shasum -a 256 | cut -d' ' -f1
    elif command -v sha256sum >/dev/null 2>&1; then
        tail -n +"$ARCHIVE_LINE" "$SELF" | sha256sum | cut -d' ' -f1
    elif command -v openssl >/dev/null 2>&1; then
        tail -n +"$ARCHIVE_LINE" "$SELF" | openssl dgst -sha256 | sed 's/^.*= *//'
    else
        return 1
    fi
}

check_payload() {
    _actual=$(archive_sha256) ||
        die "no sha256 tool found (shasum, sha256sum, or openssl); cannot verify this installer"
    [ "$_actual" = "$ARCHIVE_SHA256" ] ||
        die "payload checksum mismatch (expected $ARCHIVE_SHA256, got $_actual); this installer is truncated or corrupt"
}

extract_into() {
    tail -n +"$ARCHIVE_LINE" "$SELF" | (cd "$1" && tar -xzf -) ||
        die "cannot unpack the payload into $1"
    [ -x "$1/$PAYLOAD_DIR/$COMMAND_NAME" ] ||
        die "unpacked payload has no executable at $PAYLOAD_DIR/$COMMAND_NAME"
}

# `--extract` is checked for integrity but not for host match: it installs
# nothing and runs nothing, so refusing it would only stop someone from
# inspecting the artifact on the machine that just told them it does not fit.
if [ "$mode" != extract ]; then
    check_host
fi
check_payload

if [ "$mode" = check ]; then
    printf '%s %s (%s) is installable on this machine\n' "$COMMAND_NAME" "$VERSION" "$TARGET"
    exit 0
fi

if [ "$mode" = extract ]; then
    mkdir -p "$extract_dir" || die "cannot create $extract_dir"
    extract_into "$extract_dir"
    printf 'extracted %s\n' "$extract_dir/$PAYLOAD_DIR"
    exit 0
fi

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/orch-hub-install.XXXXXX") ||
    die 'cannot create a temporary directory'
trap '_status=$?; rm -rf "$work_dir"; exit "$_status"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

extract_into "$work_dir"
payload=$work_dir/$PAYLOAD_DIR

# Both verbs run from the *extracted* binary, never from a path this script
# derives. The default prefix, the version directory, and the program the plist
# names are the runtime's own answers; re-deriving them here would be a second
# source of truth that drifts the first time one of them moves. The installed
# copy is still proven to run: `setup` waits for launchd's own service — which
# is `<prefix>/current/orch-hub` — to answer its health endpoint.
set -- install --from "$payload"
if [ -n "$prefix" ]; then set -- "$@" --prefix "$prefix"; fi
if [ -n "$bin_dir" ]; then set -- "$@" --bin-dir "$bin_dir"; fi
"$payload/$COMMAND_NAME" "$@"

if [ "$want_service" = no ]; then
    printf '\nservice not configured (--no-service). Configure it later with:\n'
    printf '  %s setup --mandrel /absolute/path/to/mandrel\n' "$COMMAND_NAME"
    exit 0
fi

# `setup` with none of these flags is exactly what `upgrade` runs after placing
# a version: it re-resolves the token, mandrel checkout, bind address, data
# directory, and push endpoint from the environment and the installed plist, so
# a second run of this installer preserves the running configuration. Each flag
# given here overrides one of those, which is how a first install reaches a
# tailnet address instead of the loopback default.
set -- setup
if [ -n "$prefix" ]; then set -- "$@" --prefix "$prefix"; fi
if [ -n "$mandrel" ]; then set -- "$@" --mandrel "$mandrel"; fi
if [ -n "$token" ]; then set -- "$@" --token "$token"; fi
if [ -n "$host" ]; then set -- "$@" --host "$host"; fi
if [ -n "$port" ]; then set -- "$@" --port "$port"; fi
if ! "$payload/$COMMAND_NAME" "$@"; then
    printf '\nthe runtime is installed but the service did not start.\n' >&2
    printf 'inspect it with:  %s launchd status\n' "$COMMAND_NAME" >&2
    printf 'go back with:     %s rollback\n' "$COMMAND_NAME" >&2
    exit 1
fi

exit 0
