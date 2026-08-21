---
last-updated: 2026-08-22
verified-against: 908707b16015269eefab9696bba52fa91feb9ea9
---

# Modules: Release Delivery

## Runtime Resources

`resources.py` maps checkout/wheel resource roots to the PyInstaller
`sys._MEIPASS` root when frozen. Dashboard assets and the launchd template use
this boundary; product modules do not reconstruct bundle paths.

## Installed Runtime

`install.py` validates `runtime.json`, target compatibility, executable/name,
and safe version components before writes. It owns versioned placement,
`current`/`previous` selection, command linking, checkout-independent setup,
upgrade, rollback, version inventory, and uninstall. Version selection is an
atomic symlink swap; uninstall positively proves prefix ownership from valid
payload content bound to its directory and aborts all deletion on a real
launchd stop error. Hub data is independent and survives unless purge is
explicitly confirmed.

`atomic_dir.py` is a replace-with-last-good primitive: stage alongside the
destination filesystem, swap, restore the prior tree on failure. Build payload,
package output, installed version, and interpreter cache need replacement;
release publication deliberately does not use it because published tags are
no-clobber.

## Build And Audit

`release_python.py` provisions the target's digest-pinned redistributable
interpreter under build scratch. Cache identity includes archive digest; failed
download/verification/extraction leaves no reusable partial state.

`release_build.py` freezes the checkout with pinned PyInstaller onedir, without
installing first-party code into the build venv. It enumerates every package
module as a hidden import, writes `runtime.json`, measures target/minimum macOS
with `macho.py`, audits the staged payload, and publishes only after all gates
pass. Build-only modules are excluded from the runtime.

`release_audit.py` scans filesystem/archive members plus the executable's
PyInstaller CArchive and nested PYZ decompressed bytes. It rejects first-party
source/bytecode files, tests/repo/protocol/agent state, maintainer paths, and
configured secrets without echoing matched secret values.

## Package And Publish

`release_package.py` revalidates/audits payloads and emits deterministic
installers, target-keyed `release.json`, and `SHA256SUMS`. Payload, scratch, and
release directories may not contain one another in either direction. Manifest
validation must be at least as strict as `install.py`; minimum macOS is ASCII
dotted numeric because downstream readers fail open on unreadable text.

`deploy/release/installer.sh` is the POSIX self-extractor template. Packaging
substitutes placeholders in one regex pass, then appends a normalized tar.gz.
The script checks host and payload digest before unpacking and delegates every
installed path decision to the extracted runtime.

`release_publish.py` re-derives inventory/checksums/header identity from bytes,
binds `v<version>` to clean HEAD, stages per invocation, and rechecks the stage
before transfer. Filesystem publication stages on the destination filesystem
and atomically claims `<destination>/<tag>` with `mkdir`; GitHub publication
uses absolute assets and `gh release create --verify-tag`. A published tag is
never replaced. `publication.json` records tag/commit and closes the artifact
inventory at `SHA256SUMS` by recording that file's own digest.

## Boundaries

- Build/package/publish modules never ship in the frozen payload.
- Frozen product entry is `orch_hub.__main__`; `serve` replaces the absent
  `orch-hub-api` console script. The legacy statusLine console script is not
  reachable in the compiled release.
- `tests/test_frozen_runtime.py` consumes an existing payload and skips when
  absent; it does not build. Rebuild before treating it as evidence after a
  frozen-behavior change.
- Publication proves checkout/tag identity and artifact consistency, not the
  source revision embedded in the payload; that provenance is separate work.
