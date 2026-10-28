"""The one-line installer (`install.sh`) against a machine the test builds.

Every run gets a fake HOME, a PATH holding only the real tools the script is
allowed to use plus stubs for the rest (uname, Python, Homebrew), a synthetic
mandrel repository to clone, and a synthetic release served over file://. The
release's `.run` and the installed `orch-hub` command record how they were
invoked, so the test sees exactly what a real install would hand them, with no
network, no launchd, and no change to the real machine.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
VERSION = "0.2.0"
MANDREL_REF = "v2.4.0"
ARTIFACT = f"orch-hub-{VERSION}-darwin-arm64.run"

REAL_TOOLS = (
    "sh", "curl", "git", "mktemp", "rm", "mkdir", "mv", "cat", "sed", "grep",
    "head", "tr", "cut", "dirname", "ls", "chmod", "tail", "rmdir", "env",
    "shasum", "sha256sum", "perl", "cp",
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("curl") is None,
    reason="the installer needs real git and curl",
)


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _stub_python(path: Path, *, new_enough: bool = True) -> Path:
    """Answers the version probe; `-m venv DIR` makes DIR/bin/python."""
    probe_exit = 0 if new_enough else 1
    return _executable(
        path,
        "#!/bin/sh\n"
        f'if [ "$1" = "-I" ]; then exit {probe_exit}; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = venv ]; then\n'
        '    mkdir -p "$3/bin" && printf "#!/bin/sh\\n" > "$3/bin/python" && chmod +x "$3/bin/python"\n'
        '    printf "%s\\n" "$0" > "$3/created-by"\n'
        "    exit 0\n"
        "fi\n"
        "exit 0\n",
    )


class Machine:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        (tmp_path / "tmp").mkdir()
        self.tools = tmp_path / "tools"
        self.tools.mkdir()
        for name in REAL_TOOLS:
            found = shutil.which(name)
            if found:
                (self.tools / name).symlink_to(found)
        _executable(
            self.tools / "uname",
            '#!/bin/sh\ncase $1 in -s) echo "${FAKE_UNAME_S:-Darwin}" ;; -m) echo "${FAKE_UNAME_M:-arm64}" ;; esac\n',
        )
        _executable(self.tools / "sw_vers", "#!/bin/sh\necho 15.1\n")
        _executable(self.tools / "id", '#!/bin/sh\necho "${FAKE_UID:-501}"\n')
        _executable(self.tools / "jq", "#!/bin/sh\nexit 0\n")
        _executable(self.tools / "claude", "#!/bin/sh\nexit 0\n")
        self.pybin = tmp_path / "pybin"
        self.pybin.mkdir()
        self.brewbin = tmp_path / "brewbin"
        self.run_log = tmp_path / "run.log"
        self.open_log = tmp_path / "open.log"
        self.brew_log = tmp_path / "brew.log"
        self.mandrel_src = self._mandrel_repo()
        self.release = self._release()

    def _mandrel_repo(self) -> Path:
        src = self.tmp / "mandrel-src"
        _executable(src / "bin" / "mandrel", '#!/bin/sh\necho "[]"\n')
        (src / "canonical").mkdir()
        (src / "canonical" / "README.md").write_text("canonical\n", encoding="utf-8")
        _git(src, "init", "-q")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", "release")
        _git(src, "tag", MANDREL_REF)
        (src / "later.txt").write_text("after the tag\n", encoding="utf-8")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", "after the tag")
        return src

    def _release(self) -> Path:
        release = self.tmp / "release"
        release.mkdir()
        _executable(
            release / ARTIFACT,
            "#!/bin/sh\n"
            'for a in "$@"; do printf "%s\\n" "$a"; done >> "$FAKE_RUN_LOG"\n'
            'cmd="$HOME/.local/orch-hub/current/orch-hub"\n'
            'mkdir -p "$(dirname "$cmd")"\n'
            f'printf \'#!/bin/sh\\nprintf "%%s\\\\n" "$*" >> "{self.open_log}"\\n\' > "$cmd"\n'
            'chmod +x "$cmd"\n',
        )
        (release / "release.json").write_text('{"version": "%s"}\n' % VERSION, encoding="utf-8")
        self.write_sums()
        return release

    def write_sums(self) -> None:
        lines = []
        for name in (ARTIFACT, "release.json"):
            digest = hashlib.sha256((self.tmp / "release" / name).read_bytes()).hexdigest()
            lines.append(f"{digest}  {name}\n")
        (self.tmp / "release" / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")

    def add_python(self, name: str = "python3.13", *, new_enough: bool = True) -> Path:
        return _stub_python(self.pybin / name, new_enough=new_enough)

    def add_brew(self) -> None:
        _executable(
            self.brewbin / "brew",
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.brew_log}"\n'
            'if [ "$2" = python@3.13 ]; then\n'
            f'    cp "{self.tmp}/brew-python" "{self.brewbin}/python3.13"\n'
            "fi\n",
        )
        _stub_python(self.tmp / "brew-python")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        path_dirs = [self.tools, self.pybin]
        if self.brewbin.is_dir():
            path_dirs.append(self.brewbin)
        environment = {
            "PATH": os.pathsep.join(str(d) for d in path_dirs),
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp / "tmp"),
            "ORCH_HUB_RELEASE_URL": f"file://{self.release}",
            "ORCH_HUB_MANDREL_REPO": str(self.mandrel_src),
            "FAKE_RUN_LOG": str(self.run_log),
        }
        environment.update(env)
        return subprocess.run(
            ["sh", str(INSTALL_SH), *args],
            env=environment,
            cwd=self.tmp,
            capture_output=True,
            text=True,
            check=False,
        )

    def run_args(self) -> list[str]:
        return self.run_log.read_text().splitlines() if self.run_log.exists() else []

    @property
    def default_mandrel(self) -> Path:
        return self.home / ".local" / "share" / "mandrel"


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    return Machine(tmp_path)


def test_script_is_valid_posix_sh():
    subprocess.run(["sh", "-n", str(INSTALL_SH)], check=True)


def test_fresh_mac_gets_mandrel_a_pinned_python_and_the_release(machine: Machine):
    python = machine.add_python("python3.13")

    result = machine.run()

    assert result.returncode == 0, result.stderr
    mandrel = machine.default_mandrel
    assert (mandrel / "bin" / "mandrel").is_file()
    # Pinned to the release tag, not the branch tip the clone starts on.
    assert _git(mandrel, "rev-parse", "HEAD") == _git(machine.mandrel_src, "rev-parse", MANDREL_REF)
    assert not (mandrel / "later.txt").exists()
    assert (mandrel / ".venv" / "created-by").read_text().strip() == str(python)
    assert machine.run_args() == ["--mandrel", str(mandrel)]
    assert machine.open_log.read_text().splitlines() == ["open"]
    assert "#add-a-repo" in result.stdout
    assert not list((machine.tmp / "tmp").iterdir()), "scratch download left behind"


def test_release_installer_options_pass_through_intact(machine: Machine):
    machine.add_python()
    prefix = machine.tmp / "prefix dir"

    result = machine.run(
        "--host", "100.64.0.1", "--port=9000", "--token", "it's a token",
        "--prefix", str(prefix), "--bin-dir=/x/bin", "--no-open",
    )

    assert result.returncode == 0, result.stderr
    assert machine.run_args() == [
        "--mandrel", str(machine.default_mandrel),
        "--host=100.64.0.1", "--port=9000", "--token=it's a token",
        f"--prefix={prefix}", "--bin-dir=/x/bin",
    ]
    assert not machine.open_log.exists()
    assert "it's a token" not in result.stdout + result.stderr


def test_an_existing_checkout_is_used_exactly_as_it_is(machine: Machine):
    existing = machine.tmp / "my-mandrel"
    _git(machine.tmp, "clone", "-q", str(machine.mandrel_src), str(existing))
    head = _git(existing, "rev-parse", "HEAD")

    # No Python on PATH at all: a checkout the user brought is not ours to pin.
    result = machine.run("--mandrel", str(existing))

    assert result.returncode == 0, result.stderr
    assert _git(existing, "rev-parse", "HEAD") == head
    assert not (existing / ".venv").exists()
    assert not machine.default_mandrel.exists()
    assert machine.run_args()[:2] == ["--mandrel", str(existing)]


def test_mandrel_path_from_the_environment_is_honoured(machine: Machine):
    existing = machine.tmp / "env-mandrel"
    _git(machine.tmp, "clone", "-q", str(machine.mandrel_src), str(existing))

    result = machine.run(ORCH_HUB_MANDREL_PATH=str(existing))

    assert result.returncode == 0, result.stderr
    assert machine.run_args()[:2] == ["--mandrel", str(existing)]


@pytest.mark.skipif(not Path("/usr/libexec/PlistBuddy").exists(), reason="macOS PlistBuddy")
def test_the_installed_services_mandrel_is_reused(machine: Machine):
    existing = machine.tmp / "configured-mandrel"
    _git(machine.tmp, "clone", "-q", str(machine.mandrel_src), str(existing))
    plist = machine.home / "Library" / "LaunchAgents" / "com.qinglin89.orch-hub.plist"
    plist.parent.mkdir(parents=True)
    with plist.open("wb") as fh:
        plistlib.dump({"EnvironmentVariables": {"ORCH_HUB_MANDREL_PATH": str(existing)}}, fh)

    result = machine.run("--no-open")

    assert result.returncode == 0, result.stderr
    assert machine.run_args()[:2] == ["--mandrel", str(existing)]
    assert not machine.default_mandrel.exists()


def test_no_python_and_no_homebrew_stops_before_downloading(machine: Machine):
    machine.add_python("python3", new_enough=False)  # the one macOS ships

    result = machine.run()

    assert result.returncode == 1
    assert "Python 3.11 or newer" in result.stdout + result.stderr
    assert "brew install python@3.13" in result.stdout + result.stderr
    assert not machine.default_mandrel.exists()
    assert machine.run_args() == []


def test_homebrew_supplies_python_when_none_is_found(machine: Machine):
    machine.add_brew()

    result = machine.run("--no-open")

    assert result.returncode == 0, result.stderr
    assert machine.brew_log.read_text().splitlines() == ["install python@3.13"]
    assert "Installing Python (python@3.13) with Homebrew" in result.stdout
    created_by = (machine.default_mandrel / ".venv" / "created-by").read_text().strip()
    assert created_by == str(machine.brewbin / "python3.13")


def test_no_brew_never_runs_homebrew(machine: Machine):
    machine.add_brew()

    result = machine.run("--no-brew")

    assert result.returncode == 1
    assert not machine.brew_log.exists()
    assert machine.run_args() == []


def test_homebrew_supplies_jq_when_it_is_missing(machine: Machine):
    machine.add_python()
    machine.add_brew()
    (machine.tools / "jq").unlink()

    result = machine.run("--no-open")

    assert result.returncode == 0, result.stderr
    assert machine.brew_log.read_text().splitlines() == ["install jq"]


def test_a_tampered_download_is_never_run(machine: Machine):
    machine.add_python()
    run_file = machine.release / ARTIFACT
    run_file.write_text(run_file.read_text() + "# altered\n", encoding="utf-8")

    result = machine.run()

    assert result.returncode == 1
    assert "checksum verification failed" in result.stderr
    assert machine.run_args() == []
    assert not list((machine.tmp / "tmp").iterdir())


def test_a_missing_release_names_what_could_not_be_fetched(machine: Machine):
    machine.add_python()

    result = machine.run("--version", "9.9.9")

    assert result.returncode == 1
    assert "orch-hub-9.9.9-darwin-arm64.run" in result.stderr


@pytest.mark.parametrize(("os_name", "arch"), [("Linux", "x86_64"), ("Darwin", "x86_64")])
def test_other_machines_are_sent_to_the_source_install(machine: Machine, os_name, arch):
    machine.add_python()

    result = machine.run(FAKE_UNAME_S=os_name, FAKE_UNAME_M=arch)

    assert result.returncode == 1
    assert "Apple Silicon only" in result.stderr
    assert "#run-from-source" in result.stderr
    assert not machine.default_mandrel.exists()


def test_root_is_refused(machine: Machine):
    machine.add_python()

    result = machine.run(FAKE_UID="0")

    assert result.returncode == 1
    assert "do not run this as root" in result.stderr


def test_dry_run_changes_nothing(machine: Machine):
    machine.add_python()

    result = machine.run("--dry-run")

    assert result.returncode == 0, result.stderr
    assert f"clone {machine.mandrel_src} at {MANDREL_REF}" in result.stdout
    assert f"file://{machine.release}/{ARTIFACT}" in result.stdout
    assert not machine.default_mandrel.exists()
    assert machine.run_args() == []


def test_a_foreign_directory_is_not_mistaken_for_mandrel(machine: Machine):
    machine.add_python()
    foreign = machine.tmp / "not-mandrel"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("mine\n", encoding="utf-8")

    result = machine.run("--mandrel", str(foreign))

    assert result.returncode == 1
    assert "is not a mandrel checkout" in result.stderr
    assert (foreign / "notes.txt").read_text() == "mine\n"


def test_a_missing_mandrel_ref_leaves_no_half_clone(machine: Machine):
    machine.add_python()

    result = machine.run(ORCH_HUB_MANDREL_REF="v0.0.0-missing")

    assert result.returncode == 1
    assert "no ref v0.0.0-missing" in result.stderr
    share = machine.home / ".local" / "share"
    assert not any(share.iterdir()) if share.exists() else True


def test_unknown_options_are_refused(machine: Machine):
    result = machine.run("--frobnicate")

    assert result.returncode == 1
    assert "unknown option '--frobnicate'" in result.stderr
