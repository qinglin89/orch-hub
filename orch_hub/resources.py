"""Runtime resource resolution for source checkouts and frozen bundles.

orch-hub runs in two shapes. In a **source runtime** the package is imported
from a checkout (or a site-packages install) and every non-code resource sits
next to the package or in the checkout. In a **frozen runtime** the same code
is compiled into a standalone binary whose first-party modules have no real
files on disk; bundled resources are unpacked into one directory the bootloader
points at.

Everything that reads a shipped file goes through this module so the two shapes
differ in exactly one place. The rule is deliberately narrow:

- Resources that ship *with the package* (the dashboard shell) resolve under
  :func:`runtime_root`, which is the bundle directory when frozen and the
  package's parent directory otherwise. Both give ``<root>/orch_hub/web``, so
  one formula covers editable checkouts, wheel installs, and bundles.
- Resources that ship *with the checkout* (the launchd plist template) have no
  package-relative home, so their owning module keeps the checkout-relative
  path and only asks this module whether a bundled copy should win.

Nothing here reads configuration or the environment for *policy*; it answers
"where does this file live for the runtime I am", and nothing else.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import __version__

PACKAGE_DIR = Path(__file__).resolve().parent
"""Directory of the installed/imported ``orch_hub`` package (source runtime)."""

WEB_RELATIVE_PATH = Path("orch_hub") / "web"
"""Dashboard shell assets, addressed the same way in both runtime shapes."""

BOOTLOADER_ENV_PREFIX = "_PYI"
"""Prefix of the private variables the frozen bootloader exports to itself.

The bootloader uses these to recognise its own re-execution (archive location,
parent-process nesting level). They are inherited by every child process the
hub spawns, where they are at best noise and at worst misleading: another
frozen program launched by the hub would read them as *its* bootloader state.
They are stripped from spawned environments by :func:`strip_bootloader_env`.
"""


@dataclass(frozen=True)
class RuntimeInfo:
    """Identity of the running orch-hub runtime.

    Reported by the ``version`` command and recorded in a built runtime's
    manifest, so an operator can tell a compiled release apart from a checkout
    without inspecting paths.
    """

    version: str
    """orch-hub version (``orch_hub.__version__``)."""

    frozen: bool
    """True when running from a compiled standalone bundle."""

    python_version: str
    """``major.minor.micro`` of the interpreter executing the runtime."""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "runtime": "frozen" if self.frozen else "source",
            "python_version": self.python_version,
        }


def is_frozen() -> bool:
    """Whether this process runs from a compiled standalone bundle.

    ``sys.frozen`` is the freezer-set marker; it is absent in every source
    runtime, so the default answers for checkouts and wheel installs alike.
    """
    return bool(getattr(sys, "frozen", False))


def runtime_root() -> Path:
    """Directory that package-shipped resources are addressed from.

    Frozen: the bundle directory the bootloader unpacked resources into
    (``sys._MEIPASS``). Source: the package's parent — the checkout root for an
    editable install, ``site-packages`` for a wheel install. In both shapes
    ``runtime_root() / "orch_hub" / <name>`` names the same resource, which is
    why bundles mirror the package layout instead of flattening it.

    Falls back to the package's parent if a freezer marks ``sys.frozen`` without
    publishing a bundle directory: a wrong-but-existing root produces a clear
    missing-file error at the point of use, whereas raising here would break
    imports of every module that resolves a resource at import time.
    """
    if is_frozen():
        bundle_dir = getattr(sys, "_MEIPASS", None)
        if bundle_dir:
            return Path(bundle_dir)
    return PACKAGE_DIR.parent


def web_dir() -> Path:
    """Directory holding the dashboard shell assets for this runtime."""
    return runtime_root() / WEB_RELATIVE_PATH


def bundled_resource(relative: Path | str) -> Path | None:
    """Return a checkout-relative resource's bundled copy, or ``None``.

    For resources that live in the checkout rather than in the package (the
    launchd plist template): when frozen, the build copies them into the bundle
    under their checkout-relative path and this returns that copy; in a source
    runtime it returns ``None`` so the caller keeps using its checkout path.

    ``None`` also comes back when a frozen runtime simply does not carry the
    resource, which keeps the caller's own "not found" reporting authoritative
    instead of inventing a path that cannot exist.
    """
    if not is_frozen():
        return None
    candidate = runtime_root() / relative
    return candidate if candidate.is_file() else None


def strip_bootloader_env(env: Mapping[str, str]) -> dict[str, str]:
    """Copy ``env`` without the frozen bootloader's private variables.

    Applied to every environment the hub hands to a child process. In a source
    runtime this is a plain copy; in a frozen runtime it keeps the bootloader's
    self-addressed state (see :data:`BOOTLOADER_ENV_PREFIX`) from being
    inherited by the orchestrator and the agent CLIs it launches.
    """
    return {
        key: value
        for key, value in env.items()
        if not key.startswith(BOOTLOADER_ENV_PREFIX)
    }


def runtime_info() -> RuntimeInfo:
    """Describe the running runtime for ``version`` output and manifests."""
    return RuntimeInfo(
        version=__version__,
        frozen=is_frozen(),
        python_version="%d.%d.%d" % sys.version_info[:3],
    )
