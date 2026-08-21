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
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOAD_ENV = "ORCH_HUB_RUNTIME_PAYLOAD"
DEFAULT_PAYLOAD = REPO_ROOT / "dist" / "orch-hub-darwin-arm64"
TOKEN = "frozen-smoke-token"
STARTUP_TIMEOUT = 30.0


def payload_dir() -> Path:
    configured = os.environ.get(PAYLOAD_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_PAYLOAD


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


def test_manifest_carries_no_build_machine_paths(manifest: dict):
    # The manifest is published with the artifact, so every value in it has to
    # be a property of the artifact rather than of the machine that built it.
    assert "/Users/" not in json.dumps(manifest)


def test_cli_help_works_without_python_on_path(tmp_path: Path):
    result = run_runtime(tmp_path, "--help")
    assert result.returncode == 0, result.stderr
    for command in ("repos", "serve", "launchd", "notify", "eval", "version"):
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
