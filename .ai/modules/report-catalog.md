---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Modules: Global Report Catalog

Cross-repo publication of complete archived-task evaluations. Artifact
producer and retention-guard consumer: `evaluation.md`.

## Global Report Catalog

Location: `orch_hub/report_catalog.py`

Responsibility: publish archived tasks' coherent, complete L1+L2 artifact sets
through one hub-owned append-only catalog at
`<data_dir>/report-catalog/catalog.jsonl`. `ReportCatalog` assigns an immutable
monotonic `seq` (global order/cursor) and a stable opaque `report_key` derived
from the source evaluation identity. Its semantic-finalization hook is
best-effort promptness; the deterministic startup `reconcile()` sweep is the
correctness path and makes backfill/restart idempotent. Reports evaluated while
active remain candidates and publish after a contained archive probe succeeds.

The read/check/allocate/append transaction uses a process-wide `flock`; appends
are fsynced single lines with torn-tail fencing. `page()` provides forward-only
strictly-greater integer-watermark traversal; `read_bundle()` resolves a global
key to the complete contained artifact set and reports digest verification. The
catalog re-opens every scanned candidate and every bundle/raw read through the
evaluation module's descriptor walk: the repo/task/evaluation hops and each
fixed artifact are no-follow, and artifacts are validated and consumed through
the opened descriptors. A linked interior hop is damaged (distinct from
retention-pruned/incomplete), is never published or hashed through, and is not
publication debt; non-canonical repo directory names are not swept.
Catalog/source identity ambiguity fails closed. Published entries outlive
retention-pruned artifacts, which remain auditable but bundle-unavailable.

`audit()` is the read-only operator diagnosis: it shares catalog scanning with
feed reads, locates every parse/duplicate/identity defect, and classifies each
distinct source/key identity through the bundle path's contained directory
census and artifact opens (`retained`, absent-dir `pruned`, `incomplete`,
`damaged`, `unreadable`, `unaddressable`). `repair()` is the only physical
rewrite of the append-only file: dry-run unless explicitly confirmed, limited to
byte-identical repeated lines and an unparseable torn tail, and refusing distinct
identity conflicts. It holds the append lock through planning and replacement,
writes a durable collision-safe non-overwriting backup first, and preserves all
published `seq`/`report_key` identities.

Protocol provenance is copied only from the persisted evidence pack's
fail-closed aggregate into the atomic `provenance.protocol.effective_revision`
plus `deploy_lock_hash` pair. The catalog never reads current target state or
invents/completes an identity for legacy revision-only, unverified, or mixed
runs; append-only legacy lines demote at readback.

`unpublished_ids()` is injected as `EvaluationService.retention_guard` so a
prune cannot destroy recoverable publication debt. The guard and publisher read
the same artifact-derived candidate and publishability predicate; uncertainty
defers publication but protects retention. The catalog owns publication and
provenance only—it never evaluates, ranks, decides protocol evolution, or
creates protocol-improvement tasks.
