"""Keyboard smoke test for the dashboard SPA, driven under JavaScriptCore.

The rest of the dashboard suite (tests/test_web.py) pins the SPA at the
served-asset boundary — it can assert that a string appears in `app.js`, which
is enough for wiring but says nothing about *behaviour*. Keyboard operability
is behaviour: whether a route change actually moves focus, whether Escape closes
the launch dialog, whether focus comes back to the control that opened it.

There is no browser in this environment, so `tests/keyboard_harness.js` stubs
one (HTML parse, selector matching, bubbling events, focus/tab order, fetch)
and drives the real `orch_hub/web/app.js` against the real
`orch_hub/web/index.html`. This module is the thin pytest wrapper: it runs the
harness and fails on any `not ok` line. Where no JS engine is available the test
skips rather than passing vacuously.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "tests" / "keyboard_harness.js"

# macOS ships JavaScriptCore's shell at a fixed path inside the framework; it is
# not on PATH, hence the explicit fallback.
JSC_FRAMEWORK_PATH = Path(
    "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
)


def find_jsc() -> str | None:
    found = shutil.which("jsc")
    if found:
        return found
    return str(JSC_FRAMEWORK_PATH) if JSC_FRAMEWORK_PATH.exists() else None


def run_harness() -> str:
    jsc = find_jsc()
    if jsc is None:
        pytest.skip("no JavaScriptCore shell (jsc) available to run the SPA harness")
    proc = subprocess.run(
        [jsc, str(HARNESS)],
        cwd=REPO_ROOT,  # the harness reads the shipped assets by repo-relative path
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"jsc failed: {proc.stderr or proc.stdout}"
    return proc.stdout


@pytest.fixture(scope="module")
def harness_output() -> str:
    return run_harness()


def test_keyboard_smoke_has_no_failures(harness_output: str):
    failures = [ln for ln in harness_output.splitlines() if ln.startswith("not ok")]
    assert not failures, "keyboard smoke failures:\n" + "\n".join(failures)


def test_keyboard_smoke_actually_ran(harness_output: str):
    # Guard against a silently empty run (a harness crash before the first check
    # would otherwise leave zero `not ok` lines and pass the test above).
    summary = [ln for ln in harness_output.splitlines() if ln.startswith("SUMMARY ")]
    assert len(summary) == 1, f"expected one SUMMARY line, got {summary!r}"
    passed, total = summary[0].removeprefix("SUMMARY ").split("/")
    assert int(total) >= 50, f"harness ran only {total} checks"
    assert passed == total


@pytest.mark.parametrize(
    "scenario",
    [
        "login:",
        "nav:",
        "theme:",
        "palette:",
        "task:",
        "dialog:",
        "catalog:",
        "intake:",
        "eval:",
        "outline (",
        "log:",
        "layout:",
        "stale route:",
        "run history:",
        "run nav:",
        "readiness:",
        "admission:",
        "usage:",
        "first run:",
        "hint:",
        "paste:",
    ],
)
def test_keyboard_smoke_covers_each_acceptance_surface(harness_output: str, scenario: str):
    # The task's acceptance names the surfaces the keyboard smoke test must
    # cover, plus `stale route:` for the predictable-focus regression a review
    # caught; pin that each one is still exercised so a future edit can't quietly
    # drop a scenario and keep a green suite.
    assert any(
        ln.startswith("ok - " + scenario) for ln in harness_output.splitlines()
    ), f"no checks ran for {scenario!r}"
