"""Build the compiled, standalone orch-hub runtime for a release target.

Run from a checkout::

    python3 -m orch_hub.release_build

The result is one payload directory — an executable plus the libraries,
interpreter, and resources it needs — that runs on a machine with no Python, no
virtualenv, and no orch-hub checkout. It is the unit later stages wrap: the
self-extracting installer, the versioned install layout, and the release
manifest all address this directory.

Three properties are worth more than the mechanics:

- **The build is declared, not discovered.** Dependencies come from
  ``pyproject.toml`` and are installed into an isolated build environment, so a
  release cannot silently inherit whatever the maintainer happened to have
  installed. The project itself is deliberately *not* installed there: the
  freezer reads first-party code from the checkout, so what ships is the
  working tree and never a stale copy in ``site-packages``.
- **The payload declares itself.** ``runtime.json`` records version, target,
  architecture, and the minimum macOS the produced binaries actually require —
  read back out of the Mach-O headers rather than assumed from the build host.
  Nothing in it is a path, so the manifest carries no trace of the builder.
- **The payload is audited before it is a release.** A build with findings
  fails; shipping a maintainer's absolute path or bearer token inside a binary
  is not recoverable after publication.

Cross-building is not attempted. A freezer bundles the interpreter and shared
libraries of the machine it runs on, so the host must already be the target.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# These two modules are the owners of the paths a payload must carry, so the
# build imports the real constants instead of restating them: a moved asset or
# a renamed plist then changes the bundle too, rather than silently dropping it.
# Both import chains are standard-library only, which the build driver relies on
# — it runs before the build environment with the runtime dependencies exists.
from .launchd import TEMPLATE_RELATIVE_PATH as LAUNCHD_TEMPLATE_RELATIVE_PATH
from .macho import PayloadFacts, scan_payload
from .release_audit import (
    AuditReport,
    ForbiddenLiteral,
    SECRET_MATERIAL,
    audit_payload,
    maintainer_path_literals,
)
from .resources import WEB_RELATIVE_PATH

TARGET_OS = "darwin"
TARGET_ARCH = "arm64"
TARGET = f"{TARGET_OS}-{TARGET_ARCH}"
"""The one shipped target. Named as ``<os>-<arch>`` so the release layout keys
by target from the start and a second target is an added key, not a new shape."""

EXECUTABLE_NAME = "orch-hub"
MANIFEST_NAME = "runtime.json"
MANIFEST_SCHEMA = "orch-hub-runtime/1"

BUNDLE_DIR = "_internal"
"""Subdirectory the freezer puts bundled resources and libraries in."""

RELEASE_EXTRA = "release"
"""``pyproject.toml`` optional-dependency group holding the build toolchain."""

ENTRY_RELATIVE_PATH = Path("orch_hub") / "__main__.py"
"""Program entry the freezer compiles as ``__main__`` — the same module
``python -m orch_hub`` runs, so both runtimes dispatch identically."""

BUILD_ONLY_MODULES = (
    "orch_hub.release_build",
    "orch_hub.release_audit",
    "orch_hub.macho",
)
"""First-party modules kept out of the payload.

They exist to *produce and check* a release; a release has no use for them, and
the audit's own list of forbidden material has no business shipping inside the
artifact it guards."""

EXCLUDED_MODULES = ("pytest", "tkinter")
"""Third-party/GUI machinery a served runtime never reaches.

Kept deliberately short. Trimming the standard library saves a little size and
buys a class of failure that only shows up on a path no test happened to take,
which is a bad trade for an artifact shipped to other people."""

REQUIRED_MEMBERS = (
    EXECUTABLE_NAME,
    MANIFEST_NAME,
    f"{BUNDLE_DIR}/{WEB_RELATIVE_PATH.as_posix()}/index.html",
    f"{BUNDLE_DIR}/{WEB_RELATIVE_PATH.as_posix()}/app.js",
    f"{BUNDLE_DIR}/{WEB_RELATIVE_PATH.as_posix()}/styles.css",
    f"{BUNDLE_DIR}/{LAUNCHD_TEMPLATE_RELATIVE_PATH.as_posix()}",
)
"""Members the payload must contain to be a usable runtime.

Absence of the forbidden is only half of a distributable payload; a build that
quietly dropped the dashboard assets or the plist template produces a binary
that starts and then fails at the first request."""

SECRET_ENV_LABELS = {
    "ORCH_HUB_TOKEN": "bearer token",
    "ORCH_HUB_BARK_URL": "bark endpoint",
}
"""Environment values audited for by default.

A maintainer building on their own machine usually has the live service's token
and push endpoint exported. Neither can reach the payload through any code
path, which is exactly why the check is cheap and worth keeping: it costs one
byte scan and removes the only failure mode that cannot be undone."""


class BuildError(RuntimeError):
    """Raised when the runtime cannot be built or fails its audit."""


@dataclass(frozen=True)
class BuildOptions:
    """Inputs of one runtime build."""

    checkout: Path
    out_dir: Path
    work_dir: Path
    venv_dir: Path
    python: str = sys.executable
    """Interpreter the build environment is created from — and therefore the
    interpreter that gets bundled. It decides the payload's minimum macOS, so
    a release pipeline pins a redistributable one instead of taking the host's."""

    install: bool = True
    clean: bool = True
    allow_findings: bool = False
    literals: tuple[ForbiddenLiteral, ...] = ()

    @property
    def payload_dir(self) -> Path:
        return self.out_dir / f"{EXECUTABLE_NAME}-{TARGET}"


@dataclass(frozen=True)
class BuildResult:
    payload_dir: Path
    manifest: dict
    audit: AuditReport
    facts: PayloadFacts

    def to_dict(self) -> dict:
        return {
            "payload": self.payload_dir.name,
            "manifest": self.manifest,
            "audit": self.audit.to_dict(),
        }


def first_party_modules(checkout: Path) -> tuple[str, ...]:
    """Every ``orch_hub`` module the payload must carry, in import-name form.

    Read from the checkout rather than delegated to the freezer's own
    package-collection helper: that helper resolves the package by *importing*
    it in an isolated subprocess, which cannot see a checkout that was never
    installed. It answers with an empty list instead of an error, so the modules
    reached only through a lazy import would be dropped from the payload without
    a single warning — a runtime that starts fine and then fails on one command.

    Enumerating the directory keeps the rule simple and checkable: the payload
    carries the whole first-party package except the tooling that builds it.
    """
    package_dir = checkout / "orch_hub"
    if not package_dir.is_dir():
        raise BuildError(f"no orch_hub package under {checkout}")
    names = []
    for path in sorted(package_dir.glob("*.py")):
        if path.stem.startswith("__"):
            continue  # __init__ comes with the package; __main__ is the entry
        module = f"orch_hub.{path.stem}"
        if module not in BUILD_ONLY_MODULES:
            names.append(module)
    if not names:
        raise BuildError(f"no first-party modules found under {package_dir}")
    return tuple(names)


def read_project(checkout: Path) -> dict:
    """Parse ``pyproject.toml`` — the single declaration of what a build needs."""
    pyproject = checkout / "pyproject.toml"
    try:
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BuildError(f"cannot read {pyproject}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BuildError(f"malformed {pyproject}: {exc}") from exc


def read_version(checkout: Path) -> str:
    """Read ``orch_hub.__version__`` without importing the package.

    The build driver runs on a bare interpreter that has none of the runtime
    dependencies, so importing ``orch_hub`` to ask for its version would fail
    before the build environment exists.
    """
    source = (checkout / "orch_hub" / "__init__.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if line.startswith("__version__"):
            _, _, value = line.partition("=")
            return value.strip().strip("\"'")
    raise BuildError("orch_hub/__init__.py declares no __version__")


def build_requirements(project: dict) -> tuple[str, ...]:
    """Runtime dependencies plus the pinned build toolchain, in install order."""
    table = project.get("project", {})
    runtime = tuple(table.get("dependencies", ()))
    extras = table.get("optional-dependencies", {})
    release = tuple(extras.get(RELEASE_EXTRA, ()))
    if not release:
        raise BuildError(
            f"pyproject.toml declares no '{RELEASE_EXTRA}' optional dependencies; "
            "the build toolchain must be pinned there"
        )
    return runtime + release


def check_host(*, system: str | None = None, machine: str | None = None) -> None:
    """Refuse to build for a target this machine cannot produce.

    The freezer copies the running interpreter and its shared libraries, so a
    build is always for the host. Saying so up front beats emitting a payload
    labelled ``darwin-arm64`` that holds another platform's binaries.
    """
    resolved_system = (system or platform.system()).lower()
    resolved_machine = (machine or platform.machine()).lower()
    if resolved_system != TARGET_OS or resolved_machine != TARGET_ARCH:
        raise BuildError(
            f"cannot build the {TARGET} runtime on {resolved_system}-{resolved_machine}: "
            "the build host must be the target (no cross-building)"
        )


def default_literals(
    checkout: Path, environ: dict[str, str], *, home: Path | None = None
) -> tuple[ForbiddenLiteral, ...]:
    """Literals a payload built on this machine must not contain."""
    resolved_home = home if home is not None else Path.home()
    literals = list(maintainer_path_literals([checkout, resolved_home]))
    for name, label in SECRET_ENV_LABELS.items():
        value = (environ.get(name) or "").strip()
        if value:
            literals.append(
                ForbiddenLiteral(label=label, value=value, kind=SECRET_MATERIAL)
            )
    return tuple(literals)


def build_runtime(options: BuildOptions) -> BuildResult:
    """Freeze, arrange, describe, and audit one runtime payload."""
    check_host()
    version = read_version(options.checkout)
    project = read_project(options.checkout)
    venv_python = ensure_build_venv(options, project)
    frozen_dir = _freeze(options, venv_python)
    payload_dir = _place_payload(frozen_dir, options.payload_dir)
    facts = scan_payload(payload_dir)
    manifest = _write_manifest(
        payload_dir,
        version=version,
        facts=facts,
        python_version=_interpreter_version(venv_python),
        builder=_builder_identity(venv_python),
    )
    report = audit_payload(
        payload_dir, required=REQUIRED_MEMBERS, literals=options.literals
    )
    if report.findings and not options.allow_findings:
        raise BuildError(_findings_message(report))
    return BuildResult(
        payload_dir=payload_dir, manifest=manifest, audit=report, facts=facts
    )


def ensure_build_venv(options: BuildOptions, project: dict) -> Path:
    """Create (or reuse) the isolated build environment and return its python."""
    venv_python = options.venv_dir / "bin" / "python"
    if not options.install:
        if not venv_python.is_file():
            raise BuildError(
                f"build environment {options.venv_dir} has no interpreter; "
                "run without --skip-install to create it"
            )
        return venv_python
    if not venv_python.is_file():
        _run([options.python, "-m", "venv", str(options.venv_dir)], "create build venv")
    _run(
        [str(venv_python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        "upgrade build pip",
    )
    _run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--quiet",
            *build_requirements(project),
        ],
        "install build requirements",
    )
    return venv_python


def _freeze(options: BuildOptions, venv_python: Path) -> Path:
    """Run the freezer and return the directory it produced."""
    dist_dir = options.work_dir / "dist"
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    argv = [
        str(venv_python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--console",
        "--noupx",
        "--name",
        EXECUTABLE_NAME,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(options.work_dir / "pyinstaller"),
        "--specpath",
        str(options.work_dir),
        # First-party code is read from the checkout, never from an install, so
        # a build always reflects the working tree.
        "--paths",
        str(options.checkout),
    ]
    # Name every first-party module explicitly rather than letting the freezer's
    # reachability analysis decide: which imports are static today is an
    # implementation detail, and a module reached only lazily would otherwise be
    # missing from the payload with no build-time signal.
    for module in first_party_modules(options.checkout):
        argv += ["--hidden-import", module]
    if options.clean:
        argv.append("--clean")
    for module in (*BUILD_ONLY_MODULES, *EXCLUDED_MODULES):
        argv += ["--exclude-module", module]
    for source, destination in (
        (WEB_RELATIVE_PATH, WEB_RELATIVE_PATH),
        (LAUNCHD_TEMPLATE_RELATIVE_PATH, LAUNCHD_TEMPLATE_RELATIVE_PATH.parent),
    ):
        argv += [
            "--add-data",
            f"{options.checkout / source}:{destination.as_posix()}",
        ]
    argv.append(str(options.checkout / ENTRY_RELATIVE_PATH))
    _run(argv, "freeze runtime", cwd=options.work_dir)
    produced = dist_dir / EXECUTABLE_NAME
    if not (produced / EXECUTABLE_NAME).is_file():
        raise BuildError(f"freezer produced no executable at {produced}")
    return produced


def _place_payload(frozen_dir: Path, payload_dir: Path) -> Path:
    """Move a finished build into its target-keyed name.

    The previous payload is replaced only once the new one exists, so a failed
    build leaves the last good payload in place.
    """
    payload_dir.parent.mkdir(parents=True, exist_ok=True)
    if payload_dir.exists():
        shutil.rmtree(payload_dir)
    shutil.move(str(frozen_dir), str(payload_dir))
    return payload_dir


def _write_manifest(
    payload_dir: Path,
    *,
    version: str,
    facts: PayloadFacts,
    python_version: str,
    builder: str,
) -> dict:
    """Write the payload's self-description.

    Deliberately path-free: every value is a property of the artifact, not of
    the machine that produced it, so the manifest is safe to publish and stable
    across builders.
    """
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "name": EXECUTABLE_NAME,
        "version": version,
        "target": TARGET,
        "os": TARGET_OS,
        "arch": TARGET_ARCH,
        "executable": EXECUTABLE_NAME,
        "python_version": python_version,
        "builder": builder,
        **facts.to_dict(),
    }
    path = payload_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _interpreter_version(venv_python: Path) -> str:
    result = _run(
        [
            str(venv_python),
            "-c",
            "import sys; print('%d.%d.%d' % sys.version_info[:3])",
        ],
        "read build interpreter version",
        capture=True,
    )
    return (result or "").strip()


def _builder_identity(venv_python: Path) -> str:
    result = _run(
        [str(venv_python), "-c", "import PyInstaller; print(PyInstaller.__version__)"],
        "read freezer version",
        capture=True,
    )
    return f"pyinstaller {(result or '').strip()}"


def _findings_message(report: AuditReport) -> str:
    lines = [f"payload audit found {len(report.findings)} problem(s):"]
    lines += [
        f"  {finding.kind}: {finding.path} ({finding.detail})"
        for finding in report.findings
    ]
    return "\n".join(lines)


def _run(
    argv: Sequence[str], what: str, *, cwd: Path | None = None, capture: bool = False
) -> str | None:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=None if cwd is None else str(cwd),
            check=False,
            capture_output=capture,
            text=True,
        )
    except OSError as exc:
        raise BuildError(f"failed to {what}: {exc}") from exc
    if completed.returncode != 0:
        detail = ""
        if capture:
            detail = f": {(completed.stderr or completed.stdout or '').strip()}"
        raise BuildError(f"failed to {what} (exit {completed.returncode}){detail}")
    return completed.stdout if capture else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m orch_hub.release_build",
        description=f"Build the standalone {TARGET} orch-hub runtime from this checkout.",
    )
    checkout_default = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--checkout",
        type=Path,
        default=checkout_default,
        help="source checkout to build from (default: the one holding this module)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="directory receiving the payload (default: <checkout>/dist)",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="scratch directory for freezer state (default: <checkout>/build/release)",
    )
    parser.add_argument(
        "--venv",
        type=Path,
        default=None,
        help="build environment (default: <work>/venv)",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help=(
            "interpreter the build environment is created from; it is the one "
            "bundled, so it sets the payload's minimum macOS"
        ),
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="reuse the existing build environment without installing anything",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="keep the freezer cache between builds (faster, less reproducible)",
    )
    parser.add_argument(
        "--allow-findings",
        action="store_true",
        help="report audit findings without failing the build",
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    args = parser.parse_args(argv)

    checkout = args.checkout.expanduser().resolve()
    work_dir = (args.work or checkout / "build" / "release").expanduser().resolve()
    options = BuildOptions(
        checkout=checkout,
        out_dir=(args.out or checkout / "dist").expanduser().resolve(),
        work_dir=work_dir,
        venv_dir=(args.venv or work_dir / "venv").expanduser().resolve(),
        python=args.python,
        install=not args.skip_install,
        clean=not args.no_clean,
        allow_findings=args.allow_findings,
        literals=default_literals(checkout, dict(os.environ)),
    )
    options.work_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = build_runtime(options)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0


def _print_human(result: BuildResult) -> None:
    manifest = result.manifest
    print(f"payload:    {result.payload_dir}")
    print(f"version:    {manifest['version']} ({manifest['target']})")
    print(f"builder:    {manifest['builder']}, python {manifest['python_version']}")
    print(
        "binaries:   "
        f"{manifest['binaries']} ({', '.join(manifest['architectures']) or 'none'})"
    )
    print(f"min macOS:  {manifest['min_macos'] or 'undeclared'}")
    scanned = f"{result.audit.files_scanned} files, {result.audit.archives_scanned} archives"
    if result.audit.clean:
        print(f"audit:      clean ({scanned})")
        return
    print(f"audit:      {len(result.audit.findings)} finding(s) ({scanned})")
    for finding in result.audit.findings:
        print(f"  {finding.kind}: {finding.path} ({finding.detail})")


if __name__ == "__main__":
    sys.exit(main())
