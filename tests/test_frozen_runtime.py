"""Smoke test for a built standalone runtime.

The rest of the suite exercises the package as imported source. That says
nothing about the artifact an engineer actually receives: whether the freezer
kept every module the lazy imports reach, whether the dashboard assets resolve
from the bundle instead of from a source tree, and whether any of it works
without Python on the machine. Only the compiled payload can answer that.

Building takes tens of seconds and needs a network install, so this does not
build: it runs a payload if one is present (``dist/orch-hub-darwin-arm64`` by
default, ``ORCH_HUB_RUNTIME_PAYLOAD`` to point elsewhere) and skips otherwise,
rather than passing vacuously. Build one with::

    python3 -m orch_hub.release_build

The runtime is driven with a scrubbed environment and a PATH containing no
interpreter, so "needs no Python and no checkout" is enforced by the test setup
rather than asserted in a comment. The operator's own token, data directory and
Mandrel checkout are never inherited — a smoke test must not touch the live
service's state.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from orch_hub import release_build, release_package
from orch_hub.release_audit import ForbiddenLiteral, audit_payload
from orch_hub.release_build import default_literals, is_source_revision
from orch_hub.release_package import PackageOptions, package_release
from orch_hub.release_publish import (
    PUBLICATION_NAME,
    PublishOptions,
    parse_destination,
    publish,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOAD_ENV = "ORCH_HUB_RUNTIME_PAYLOAD"
DEFAULT_PAYLOAD = REPO_ROOT / "dist" / "orch-hub-darwin-arm64"
TOKEN = "frozen-smoke-token"
STARTUP_TIMEOUT = 30.0


def payload_dir() -> Path:
    configured = os.environ.get(PAYLOAD_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_PAYLOAD


def tree_members(root: Path) -> dict[str, tuple[bool, bool]]:
    """Every path under ``root`` with the two properties a copy can lose.

    A payload is not bytes alone: a dereferenced symlink or a dropped executable
    bit produces a tree that still looks complete and no longer runs. What is
    *in* the payload depends on the pinned interpreter — the current one bundles
    a plain ``libpython`` where a framework build bundled a directory of relative
    symlinks — so this compares the two trees to each other rather than naming
    members that only one interpreter's payload has.
    """
    return {
        path.relative_to(root).as_posix(): (path.is_symlink(), os.access(path, os.X_OK))
        for path in sorted(root.rglob("*"))
    }


def executable() -> Path:
    path = payload_dir() / "orch-hub"
    if not path.is_file():
        pytest.skip(
            f"no built runtime at {payload_dir()}; "
            "build one with `python3 -m orch_hub.release_build`"
        )
    return path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def isolated_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Environment with no interpreter on PATH and no inherited hub settings."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir(exist_ok=True)
    env = {"PATH": str(empty_bin), "HOME": str(tmp_path)}
    env.update(extra)
    return env


def run_runtime(tmp_path: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(executable()), *args],
        cwd=str(tmp_path),  # never the checkout: the runtime must not need one
        env=isolated_env(tmp_path, **extra),
        capture_output=True,
        text=True,
        timeout=60,
    )


def get(url: str, token: str | None = None) -> tuple[int, bytes, str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=5
        ) as response:
            return response.status, response.read(), response.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("content-type", "")


@pytest.fixture(scope="module")
def manifest() -> dict:
    path = payload_dir() / "runtime.json"
    if not path.is_file():
        pytest.skip(f"no built runtime manifest at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def served(tmp_path_factory) -> Iterator[str]:
    """Start the built runtime's own server and yield its base URL."""
    binary = executable()
    tmp_path = tmp_path_factory.mktemp("frozen-serve")
    port = free_port()
    env = isolated_env(
        tmp_path,
        ORCH_HUB_TOKEN=TOKEN,
        # A synthetic checkout and data dir: the smoke test must not read the
        # operator's registry or write into the live service's state.
        ORCH_HUB_MANDREL_PATH=str(tmp_path / "mandrel"),
        ORCH_HUB_DATA_DIR=str(tmp_path / "data"),
        ORCH_HUB_HOST="127.0.0.1",
        ORCH_HUB_PORT=str(port),
    )
    process = subprocess.Popen(
        [str(binary), "serve"],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"runtime exited early:\n{process.communicate()[0]}")
            try:
                status, _, _ = get(f"{base_url}/api/health")
            except OSError:
                time.sleep(0.2)
                continue
            if status == 200:
                break
        else:
            pytest.fail(f"runtime did not answer /api/health within {STARTUP_TIMEOUT}s")
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stuck child
            process.kill()


def test_the_audit_reads_the_code_frozen_into_the_executable():
    """The payload audit must reach the code that actually ships.

    The synthetic archives in tests/test_release_audit.py pin the parser
    against a format written by hand; this pins it against the artifact the
    freezer really produces. A first-party symbol is used as the canary because
    it exists only in compiled first-party code — so finding it proves the
    audit decompressed a frozen module, and a maintainer path or a token in one
    would be found the same way.
    """
    binary = executable()
    canary = ForbiddenLiteral(label="first-party symbol", value="runtime_root")
    # Invisible to a scan of the payload's bytes: this is the gap being closed.
    assert canary.value.encode() not in binary.read_bytes()

    report = audit_payload(payload_dir(), literals=[canary])
    assert f"{binary.name}!PYZ.pyz!orch_hub.resources" in [
        finding.path for finding in report.findings
    ]
    # The archive appended to the executable, the module archive inside it, and
    # the standard library's own zip.
    assert report.archives_scanned >= 3


def test_the_built_payload_carries_no_first_party_source_or_repository_state():
    # The build audits what it produced, but the payload on disk is what gets
    # packaged; checking it here keeps that a property of the artifact rather
    # than of the run that made it.
    executable()  # skip rather than fail when nothing has been built
    report = audit_payload(payload_dir())
    assert report.findings == ()


def test_runtime_reports_itself_as_frozen(tmp_path: Path):
    result = run_runtime(tmp_path, "version", "--json")
    assert result.returncode == 0, result.stderr
    reported = json.loads(result.stdout)
    assert reported["runtime"] == "frozen"


def test_runtime_version_matches_the_payload_manifest(tmp_path: Path, manifest: dict):
    result = run_runtime(tmp_path, "version", "--json")
    assert json.loads(result.stdout)["version"] == manifest["version"]


def test_manifest_describes_the_shipped_target(manifest: dict):
    assert manifest["target"] == "darwin-arm64"
    assert manifest["architectures"] == ["arm64"]
    assert manifest["min_macos"]


def test_manifest_records_the_source_the_payload_was_frozen_from(manifest: dict):
    """A real build's own reading of the checkout the freezer read.

    Marked modified is fine — a smoke build usually is — and so is a null, which
    is how a build from a source export says it could not name a revision. What
    is checked is that the payload answered the question at all, because that
    answer is publication's only way to bind these bytes to a tag.

    A payload from before builds recorded one is skipped rather than failed, the
    same way this module skips when there is no payload: it cannot answer, and
    the driver's own tests are what catch a build that stopped answering.
    """
    if "source_revision" not in manifest:
        pytest.skip("payload predates source-revision recording; rebuild to cover it")
    revision = manifest["source_revision"]
    assert revision is None or is_source_revision(revision)


def test_manifest_carries_no_build_machine_paths(manifest: dict):
    # The manifest is published with the artifact, so every value in it has to
    # be a property of the artifact rather than of the machine that built it.
    assert "/Users/" not in json.dumps(manifest)


def test_cli_help_works_without_python_on_path(tmp_path: Path):
    result = run_runtime(tmp_path, "--help")
    assert result.returncode == 0, result.stderr
    for command in (
        "repos", "serve", "launchd", "notify", "eval", "version",
        # The checkout-independent lifecycle: the only surface a release has.
        "install", "setup", "upgrade", "rollback", "versions", "uninstall",
    ):
        assert command in result.stdout


def test_lazily_imported_commands_survived_the_freeze(tmp_path: Path):
    # `eval doctor` reaches the semantic backend through imports that are lazy
    # in source, which is exactly what a freezer's static analysis can miss.
    # Evaluation is disabled in this environment, so a clean non-zero exit with
    # a real message is the success condition — a missing module would instead
    # crash with a ModuleNotFoundError traceback.
    result = run_runtime(tmp_path, "eval", "doctor", ORCH_HUB_MANDREL_PATH="/tmp/absent")
    assert "ModuleNotFoundError" not in (result.stderr + result.stdout)
    assert "Traceback" not in (result.stderr + result.stdout)


def test_health_is_public(served: str):
    status, body, _ = get(f"{served}/api/health")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}


def test_dashboard_shell_is_served_from_the_bundle(served: str):
    status, body, content_type = get(f"{served}/")
    assert status == 200
    assert content_type.startswith("text/html")
    assert b"orch-hub" in body
    for asset, expected in (("app.js", "javascript"), ("styles.css", "css")):
        asset_status, asset_body, asset_type = get(f"{served}/{asset}")
        assert asset_status == 200, asset
        assert expected in asset_type
        assert asset_body, asset


def test_protected_routes_still_fail_closed(served: str):
    status, _, _ = get(f"{served}/api/repos")
    assert status == 401


def test_protected_routes_answer_with_the_token(served: str):
    status, body, _ = get(f"{served}/api/repos", token=TOKEN)
    assert status == 200
    # The synthetic Mandrel checkout has no registry, which reads as no repos.
    assert json.loads(body) == {"repos": []}


# --------------------------------------------------------------------------
# installing, selecting, and removing the compiled runtime
# --------------------------------------------------------------------------


def stub_launchctl(tmp_path: Path) -> Path:
    """A launchctl that reports an empty domain and does nothing else.

    The service label is a fixed singleton, so a test that ran the real
    launchctl would boot out the operator's own resident hub. This stands in
    for it: `print`/`bootout` answer "not loaded" in launchd's own words (the
    "is it there" check reads the message, not just the exit code), and
    `bootstrap` succeeds without starting anything.
    """
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "launchctl"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  print|bootout) echo "Could not find service" >&2; exit 113 ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def install_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Isolated environment that can still reach launchctl (stub) and plutil."""
    env = isolated_env(tmp_path, **extra)
    env["PATH"] = os.pathsep.join([str(stub_launchctl(tmp_path)), "/usr/bin", "/bin"])
    return env


def run_installed(binary: Path, tmp_path: Path, *args: str, **extra: str):
    return subprocess.run(
        [str(binary), *args],
        cwd=str(tmp_path),
        env=install_env(tmp_path, **extra),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def installed(tmp_path: Path) -> tuple[Path, Path]:
    """Install the built payload into a scratch prefix; yield (prefix, command)."""
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    result = run_installed(
        executable(),
        tmp_path,
        "install",
        "--from",
        str(payload_dir()),
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(bin_dir),
    )
    assert result.returncode == 0, result.stderr
    return prefix, bin_dir / "orch-hub"


def test_install_places_a_runnable_versioned_copy(installed, tmp_path: Path, manifest):
    """The copied payload has to still be a working runtime, symlinks included."""
    prefix, command = installed
    version_dir = prefix / "versions" / manifest["version"]

    assert (prefix / "current").is_symlink()
    assert os.readlink(prefix / "current") == f"versions/{manifest['version']}"
    # Copied, not rebuilt: same members, same link-or-file, same executability.
    assert tree_members(version_dir) == tree_members(payload_dir())
    assert command.is_symlink()

    # Run it through both indirections: the command link and the current link.
    for entry in (command, prefix / "current" / "orch-hub"):
        result = run_installed(entry, tmp_path, "version", "--json")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["version"] == manifest["version"]


def test_installed_runtime_finds_its_own_prefix(installed, tmp_path: Path, manifest):
    """`versions` with no --prefix must answer about the install it runs from."""
    _, command = installed

    result = run_installed(command, tmp_path, "versions")

    assert result.returncode == 0, result.stderr
    assert f"{manifest['version']} (current)" in result.stdout


def test_setup_renders_the_bundled_plist_template(
    installed, tmp_path: Path, served: str
):
    """Setup must work with no checkout: the template comes from the bundle.

    launchd is stubbed, so nothing is actually started — the health check is
    pointed at the runtime the `served` fixture already runs, which is what
    makes this exercise the verification step instead of waiting out its
    timeout.
    """
    prefix, command = installed
    plist_path = tmp_path / "agent.plist"
    mandrel = tmp_path / "mandrel"
    (mandrel / "bin").mkdir(parents=True)
    entry = mandrel / "bin" / "mandrel"
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    entry.chmod(0o755)  # the registry runs it; setup verifies that it can
    host, port = served.rsplit("/", 1)[-1].split(":")

    result = run_installed(
        command,
        tmp_path,
        "setup",
        "--mandrel", str(mandrel),
        "--plist-path", str(plist_path),
        "--log-dir", str(tmp_path / "logs"),
        "--data-dir", str(tmp_path / "hub-data"),
        "--host", host,
        "--port", port,
        ORCH_HUB_TOKEN="frozen-setup-token",
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "health: ok" in result.stdout
    assert plist_path.stat().st_mode & 0o777 == 0o600
    with plist_path.open("rb") as fh:
        plist = plistlib.load(fh)
    assert plist["ProgramArguments"] == [str(prefix / "current" / "orch-hub"), "serve"]
    assert plist["EnvironmentVariables"]["ORCH_HUB_TOKEN"] == "frozen-setup-token"
    assert "REPLACE-" not in plist_path.read_text(encoding="utf-8")


def test_setup_refuses_a_path_that_is_not_a_mandrel_checkout(installed, tmp_path: Path):
    _, command = installed
    (tmp_path / "not-mandrel").mkdir()

    result = run_installed(
        command,
        tmp_path,
        "setup",
        "--mandrel", str(tmp_path / "not-mandrel"),
        "--plist-path", str(tmp_path / "agent.plist"),
    )

    assert result.returncode == 1
    assert "does not look like a mandrel checkout" in result.stderr
    assert not (tmp_path / "agent.plist").exists()


def test_uninstall_removes_the_tree_it_is_running_from(installed, tmp_path: Path):
    """The one operation that deletes the running executable's own files.

    A frozen runtime loads shared libraries out of its payload directory, so an
    uninstall that removed them mid-flight would fail somewhere unrelated. The
    hub's data directory is deliberately left behind.
    """
    prefix, command = installed
    data_dir = tmp_path / "hub-data"
    (data_dir / "runs").mkdir(parents=True)

    result = run_installed(
        command,
        tmp_path,
        "uninstall",
        "--prefix", str(prefix),
        "--bin-dir", str(command.parent),
        "--plist-path", str(tmp_path / "agent.plist"),
        ORCH_HUB_DATA_DIR=str(data_dir),
    )

    assert result.returncode == 0, result.stderr
    assert "orch-hub uninstalled" in result.stdout
    assert not prefix.exists()
    assert not command.is_symlink()
    assert (data_dir / "runs").is_dir()


def test_uninstall_refuses_a_prefix_the_installer_does_not_own(tmp_path: Path):
    """The shipped binary's most destructive verb, aimed at unrelated files.

    ``--prefix`` takes any path and uninstall removes it recursively, so this is
    the one command where a typo is unrecoverable. Proven on the artifact an
    engineer actually receives, not only on the source it was frozen from.
    """
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "notes.txt").write_text("keep me", encoding="utf-8")

    result = run_installed(
        executable(),
        tmp_path,
        "uninstall",
        "--prefix", str(home),
        "--bin-dir", str(tmp_path / "bin"),
        "--plist-path", str(tmp_path / "agent.plist"),
    )

    assert result.returncode == 1
    assert "not an orch-hub install prefix" in result.stderr
    assert (home / "Documents" / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_purge_refuses_a_service_configuration_it_cannot_read(
    installed, tmp_path: Path
):
    """The purge target comes from the installed plist, so it must be readable.

    A plist that parses but carries an unreadable environment used to resolve
    the *default* data dir — on a real machine, the operator's own
    ``~/.orch-hub`` — and ``--purge-data --yes`` would then delete run history
    the service never wrote there. Nothing may be removed instead.
    """
    prefix, command = installed
    plist_path = tmp_path / "agent.plist"
    with plist_path.open("wb") as fh:
        plistlib.dump(
            {
                "Label": "com.qinglin89.orch-hub",
                "ProgramArguments": [str(prefix / "current" / "orch-hub"), "serve"],
                "EnvironmentVariables": ["not", "a", "dict"],
            },
            fh,
        )
    # HOME is tmp_path here, so this is the default the reader used to fall back
    # to; the real one is never in reach of this test.
    default_data = tmp_path / ".orch-hub"
    (default_data / "runs").mkdir(parents=True)

    result = run_installed(
        command,
        tmp_path,
        "uninstall",
        "--purge-data",
        "--yes",
        "--prefix", str(prefix),
        "--bin-dir", str(command.parent),
        "--plist-path", str(plist_path),
    )

    assert result.returncode == 1
    assert "EnvironmentVariables" in result.stderr
    assert (default_data / "runs").is_dir()
    assert (prefix / "current" / "orch-hub").is_file()
    assert plist_path.is_file()
    assert command.is_symlink()


def test_uninstall_refuses_a_prefix_that_only_looks_installed(tmp_path: Path):
    """An install-shaped foreign tree: `versions/<name>/runtime.json` and data.

    Ownership has to be proven from the manifest's content, not from a filename
    any runtime may use. Checked on the shipped binary because the source-level
    version of this proof is one the release cannot be assumed to carry.
    """
    prefix = tmp_path / "prefix"
    project = prefix / "versions" / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.js").write_text("console.log(1)\n", encoding="utf-8")
    (project / "runtime.json").write_text(
        json.dumps({"runtime": "node", "version": "22.3.0"}), encoding="utf-8"
    )

    result = run_installed(
        executable(),
        tmp_path,
        "uninstall",
        "--prefix", str(prefix),
        "--bin-dir", str(tmp_path / "bin"),
        "--plist-path", str(tmp_path / "agent.plist"),
    )

    assert result.returncode == 1
    assert "not an installed orch-hub version" in result.stderr
    kept = (project / "src" / "main.js").read_text(encoding="utf-8")
    assert kept == "console.log(1)\n"


def test_install_refuses_a_payload_for_another_machine(tmp_path: Path):
    """Architecture mismatches fail before anything is written."""
    foreign = tmp_path / "foreign-payload"
    shutil.copytree(payload_dir(), foreign, symlinks=True)
    manifest_path = foreign / "runtime.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"arch": "x86_64", "target": "darwin-x86_64"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    prefix = tmp_path / "prefix"

    result = run_installed(
        executable(), tmp_path, "install", "--from", str(foreign), "--prefix", str(prefix)
    )

    assert result.returncode == 1
    assert "darwin-x86_64" in result.stderr
    assert not prefix.exists()


# --------------------------------------------------------------------------
# the release artifact set, built from the payload that actually ships
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def release(tmp_path_factory) -> Path:
    """Package the built payload once and yield the release directory.

    tests/test_release_package.py pins the packager's decisions against a
    synthetic payload it can shape freely. This ties the same code to the
    artifact the freezer really emits: 30 MB, a bundled interpreter library, and
    a Mach-O executable — the things a synthetic fixture cannot honestly stand
    in for.
    """
    payload = payload_dir()
    executable()  # skip here, not mid-test, when nothing is built
    out = tmp_path_factory.mktemp("release")
    result = package_release(
        PackageOptions(
            payload_dirs=(payload,),
            out_dir=out / "artifacts",
            work_dir=out / "work",
            template_path=REPO_ROOT / release_package.TEMPLATE_RELATIVE_PATH,
            literals=default_literals(REPO_ROOT, dict(os.environ)),
        )
    )
    return result.out_dir


def run_installer(installer: Path, tmp_path: Path, *args: str, **extra: str):
    """Run a released ``.run`` the way an engineer would: `sh ./installer.run`."""
    return subprocess.run(
        ["sh", str(installer), *args],
        cwd=str(tmp_path),
        env=install_env(tmp_path, **extra),
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_the_real_payload_packages_into_a_verifiable_artifact_set(
    release: Path, manifest: dict
):
    installer = release / f"orch-hub-{manifest['version']}-{manifest['target']}.run"
    assert installer.is_file() and os.access(installer, os.X_OK)
    metadata = json.loads((release / "release.json").read_text(encoding="utf-8"))
    entry = metadata["targets"][manifest["target"]]
    # The release describes the payload it was made from, not a restatement of
    # it: a mismatch here means the two drifted.
    assert entry["runtime"] == manifest
    assert entry["min_macos"] == manifest["min_macos"]
    assert entry["artifacts"][0]["size"] == installer.stat().st_size
    verified = subprocess.run(
        ["shasum", "-a", "256", "-c", "SHA256SUMS"],
        cwd=str(release),
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_the_real_installer_verifies_itself_and_the_host(
    release: Path, tmp_path: Path, manifest: dict
):
    installer = next(release.glob("*.run"))
    checked = run_installer(installer, tmp_path, "--check")
    assert checked.returncode == 0, checked.stderr
    assert "is installable on this machine" in checked.stdout

    corrupt = tmp_path / "corrupt.run"
    data = bytearray(installer.read_bytes())
    data[-64] ^= 0xFF
    corrupt.write_bytes(bytes(data))
    damaged = run_installer(corrupt, tmp_path, "--check")
    assert damaged.returncode == 1
    assert "truncated or corrupt" in damaged.stderr


def test_the_real_installer_unpacks_the_payload_it_was_built_from(
    release: Path, tmp_path: Path, manifest: dict
):
    """Extraction has to reproduce the payload member for member."""
    installer = next(release.glob("*.run"))
    result = run_installer(installer, tmp_path, "--extract", str(tmp_path / "unpacked"))
    assert result.returncode == 0, result.stderr
    unpacked = tmp_path / "unpacked" / f"orch-hub-{manifest['target']}"
    original = payload_dir()

    assert tree_members(unpacked) == tree_members(original)
    assert (unpacked / "orch-hub").read_bytes() == (original / "orch-hub").read_bytes()


def test_the_real_installer_installs_and_starts_the_service(
    release: Path, tmp_path: Path, served: str, manifest: dict
):
    """The whole delivery path, from one file to a service reporting health.

    launchd is stubbed and the health probe is pointed at the runtime the
    `served` fixture already runs, so this exercises the installer's own steps —
    gate, unpack, install, configure, verify — without booting out the
    operator's resident service or waiting out a timeout.
    """
    installer = next(release.glob("*.run"))
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    mandrel = tmp_path / "mandrel"
    (mandrel / "bin").mkdir(parents=True)
    entry = mandrel / "bin" / "mandrel"
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    entry.chmod(0o755)
    host, port = served.rsplit("/", 1)[-1].split(":")

    result = run_installer(
        installer,
        tmp_path,
        "--prefix", str(prefix),
        "--bin-dir", str(bin_dir),
        "--mandrel", str(mandrel),
        "--host", host,
        "--port", port,
        "--token", "frozen-installer-token",
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "health: ok" in result.stdout
    installed_binary = prefix / "versions" / manifest["version"] / "orch-hub"
    assert installed_binary.is_file()
    assert (prefix / "current").resolve() == installed_binary.parent
    assert (bin_dir / "orch-hub").is_symlink()
    # The plist names the selection link, never the scratch directory the
    # installer unpacked into and then deleted.
    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.qinglin89.orch-hub.plist"
    with plist_path.open("rb") as fh:
        plist = plistlib.load(fh)
    assert plist["ProgramArguments"] == [str(prefix / "current" / "orch-hub"), "serve"]
    assert plist["EnvironmentVariables"]["ORCH_HUB_TOKEN"] == "frozen-installer-token"
    version = run_installed(bin_dir / "orch-hub", tmp_path, "version")
    assert version.returncode == 0, version.stderr
    assert manifest["version"] in version.stdout


def test_the_real_installer_leaves_no_scratch_directory_behind(
    release: Path, tmp_path: Path
):
    installer = next(release.glob("*.run"))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = run_installer(
        installer,
        tmp_path,
        "--prefix", str(tmp_path / "prefix"),
        "--no-service",
        TMPDIR=str(scratch),
    )
    assert result.returncode == 0, result.stderr
    assert list(scratch.iterdir()) == []


def tagged_checkout(root: Path, tag: str) -> Path:
    """A real repository sitting on ``tag`` with a clean tree.

    Publication resolves the tag with git rather than trusting the caller, so a
    smoke test of the real artifact set needs a real tag to resolve.
    """
    checkout = root / "checkout"
    (checkout / "orch_hub").mkdir(parents=True)
    # Publication holds the artifact set to the version the source declares.
    (checkout / "orch_hub" / "__init__.py").write_text(
        f'__version__ = "{tag.lstrip("v")}"\n', encoding="utf-8"
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.com",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.com",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=checkout, env=env, check=True, capture_output=True
        )

    git("init", "--quiet")
    git("add", "orch_hub/__init__.py")
    git("commit", "--quiet", "-m", tag)
    git("tag", "-a", tag, "-m", tag)
    return checkout


@pytest.fixture(scope="module")
def publishable(tmp_path_factory) -> tuple[Path, Path, str]:
    """A real artifact set whose payload records the commit its tag names.

    Publication binds the bytes to their source, so the accepting path is out of
    reach of any smoke test taken literally: the tag it resolves is one it just
    created, and the payload on disk was frozen from a working checkout that is
    not sitting on a release tag. The recorded revision is therefore the one
    value stood in for — restamped on a *copy* of the payload, never on the
    built one — and everything downstream of it stays real: the freezer's
    payload, the packager, the 17 MB self-extractor, and the placement.
    """
    payload = payload_dir()
    executable()  # skip here, not mid-test, when nothing is built
    root = tmp_path_factory.mktemp("publishable")
    version = json.loads(
        (payload / release_build.MANIFEST_NAME).read_text(encoding="utf-8")
    )["version"]
    tag = f"v{version}"
    checkout = tagged_checkout(root, tag)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(checkout),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    copy = root / payload.name
    shutil.copytree(payload, copy, symlinks=True)
    manifest_path = copy / release_build.MANIFEST_NAME
    stamped = json.loads(manifest_path.read_text(encoding="utf-8"))
    stamped["source_revision"] = commit
    manifest_path.write_text(
        json.dumps(stamped, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    result = package_release(
        PackageOptions(
            payload_dirs=(copy,),
            out_dir=root / "artifacts",
            work_dir=root / "work",
            template_path=REPO_ROOT / release_package.TEMPLATE_RELATIVE_PATH,
            literals=default_literals(REPO_ROOT, dict(os.environ)),
        )
    )
    return result.out_dir, checkout, tag


def test_the_real_release_publishes_under_its_tag(publishable, tmp_path: Path):
    """Publish the artifact set an engineer would actually be handed.

    tests/test_release_publish.py pins every refusal against synthetic releases.
    This runs the accepting path over the real one: a 17 MB self-extractor whose
    header has to be found and whose digest has to be recomputed from the same
    bytes the packager wrote.
    """
    release, checkout, tag = publishable
    destination = tmp_path / "storage"
    result = publish(
        PublishOptions(
            checkout=checkout,
            release_dir=release,
            work_dir=tmp_path / "work",
            tag=tag,
            destination=parse_destination(str(destination)),
        )
    )

    published = Path(result.published)
    assert published == (destination / tag).resolve()
    for path in release.iterdir():
        assert (published / path.name).read_bytes() == path.read_bytes()
    # The published copy answers the same check a consumer runs on arrival.
    verified = subprocess.run(
        ["shasum", "-a", "256", "-c", "SHA256SUMS"],
        cwd=str(published),
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    record = json.loads((published / PUBLICATION_NAME).read_text(encoding="utf-8"))
    assert record["tag"] == tag
    assert record["version"] == tag.lstrip("v")
    # The commit it records is the one every published payload declares, because
    # publication refused every other possibility before it wrote this.
    document = json.loads((published / "release.json").read_text(encoding="utf-8"))
    for entry in document["targets"].values():
        assert entry["runtime"]["source_revision"] == record["commit"]
    # The one digest the release cannot carry for itself.
    assert "SHA256SUMS" in {item["name"] for item in record["files"]}
