"""Shared hermetic process configuration for the test suite."""

from __future__ import annotations

import os


# ``orch_hub.api`` constructs its default ASGI app at import time. Production
# now requires an explicit Mandrel checkout, so collection supplies a harmless
# synthetic path before test modules import the app. Individual config tests
# still pass explicit mappings and therefore exercise missing-path failures.
os.environ.setdefault("ORCH_HUB_MANDREL_PATH", "/tmp/orch-hub-test-mandrel")
