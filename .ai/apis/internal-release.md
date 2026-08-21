---
last-updated: 2026-08-22
verified-against: 908707b16015269eefab9696bba52fa91feb9ea9
---

# APIs: Internal — Release Delivery

## Runtime And Installation

`runtime_root()` / `bundled_resource(path)` resolve source or frozen resources.
`runtime_info()` reports version/frozen/Python identity.

`read_payload_manifest` validates schema `orch-hub-runtime/1`, fixed product and
executable identity, safe version component, and executable presence.
`check_target` compares os/arch and readable minimum macOS. `install_version`,
`select_version`, `read_installation`, `setup_service`, `upgrade`, `rollback`,
and `uninstall` own the installed-runtime lifecycle; callers receive structured
result dataclasses and operational errors, never partial success hidden as a
healthy install.

Installed layout:

```text
<prefix>/versions/<version>/
<prefix>/current -> versions/<version>
<prefix>/previous  # absent until selection first moves
<bin-dir>/orch-hub -> <prefix>/current/orch-hub
```

## Build Manifest And Audit

`release_build.build_runtime(BuildOptions)` produces one staged/audited payload
and then publishes it. `runtime.json` carries schema/name/version/target/os/
arch/executable, measured architectures and `min_macos`, Python/freezer
identity, and binary count. The default interpreter is `PINNED[target]`,
provisioned by URL+SHA-256; explicit `--python` is an identity-enforced override.

`release_audit.audit_payload` returns labeled findings without echoing forbidden
secret values. Required members and default machine/secret literals come from
the build contract. Its archive reader understands ZIP, PyInstaller CArchive,
and nested PYZ member bytes.

## Release Artifact Schema

`release_package.package_release(PackageOptions)` emits schema
`orch-hub-release/1`:

- top-level name/version/checksum filename and target-keyed map;
- per-target normalized os/arch/minimum macOS, verbatim runtime manifest, and
  exactly one v1 installer artifact with name/kind/size/SHA-256;
- `SHA256SUMS` covers every other packaged file and cannot cover itself.

The `.run` shell header embeds version/target/os/arch/minimum floor, payload
root/executable, appended-archive line, and digest. Host and digest gates run
before extraction; `--extract` skips only host compatibility.

## Publication

`verify_release(directory)` recomputes exact file set, sizes/digests, checksum
agreement, v1 artifact kind/cardinality, and installer header identity.
`resolve_tag` requires an existing `v<version>` local tag at clean HEAD;
checkout-declared and release versions must agree.

`publish(PublishOptions)` creates a per-invocation checked stage and emits
schema `orch-hub-publication/1` with tag, commit, and every file including
`SHA256SUMS`'s digest. Filesystem destinations stage alongside then claim the
tag using no-clobber `mkdir`; a tag may be briefly visible while same-filesystem
renames fill it, and failed placement removes the partial claim. GitHub uses
`gh release create --verify-tag`. Neither path replaces an existing tag.

Publication does not prove which source revision the frozen payload contains;
the compiled manifest currently has no such field.
