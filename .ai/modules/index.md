---
last-updated: 2026-09-03
verified-against: 4d6e1a3b7c31a073417b9b681d5a009e288b71cf
---

# Modules

Full responsibilities live in the detail files below.

| Module | Location | Description |
|---|---|---|
| CLI | `orch_hub/cli.py` | `repos` readiness output; launchd/notify/eval doctor/queue/catalog dispatch |
| Config | `orch_hub/config.py` | env defaults/overrides; token/Bark/eval/Claude/Codex settings; derived paths |
| Launchd operations | `orch_hub/launchd.py` | deploy/update/lifecycle/status; Bark URL surfacing |
| Runtime resources | `orch_hub/resources.py` | checkout/wheel/frozen resource-root resolution |
| Installed runtime | `orch_hub/install.py` | versioned placement, selection, setup, upgrade, rollback, uninstall |
| Atomic directory placement | `orch_hub/atomic_dir.py` | last-good replace primitive for payload/release/version/cache trees |
| Mach-O inspection | `orch_hub/macho.py` | target architectures + measured minimum-macOS floor |
| Release audit | `orch_hub/release_audit.py` | source/path/secret audit across files, archives, CArchive/PYZ |
| Release build | `orch_hub/release_build.py` | pinned standalone PyInstaller payload build + manifest/audit gates |
| Release interpreter | `orch_hub/release_python.py` | digest-pinned redistributable interpreter provisioning |
| Release package | `orch_hub/release_package.py` | deterministic `.run`, target manifest, inventory, checksums |
| Release publish | `orch_hub/release_publish.py` | tag/byte verification + filesystem/GitHub no-republish delivery |
| Models | `orch_hub/models.py` | shared data shapes + payload helpers, including display-only protocol placement; run-record repo reconstruction |
| Registry | `orch_hub/registry.py` | managed repo discovery from `mandrel` or registry file |
| Readiness | `orch_hub/readiness.py` | fail-closed per-repo launch checks + opaque placement relay from the same mandrel call |
| Git metadata | `orch_hub/git_meta.py` | best-effort producer-capped commit reads and caller-capped path-patch streams for display/evidence |
| Housekeeping | `orch_hub/housekeeping.py` | display-only `.ai/.housekeeping-pending` detection |
| Subprocess utilities | `orch_hub/subprocess_utils.py` | blocking wrappers; whole-call timeout, cancellation, DEVNULL stdin, optional producer-capped stdout |
| Task inventory | `orch_hub/task_store.py` | `.ai-tasks` index/frontmatter parsing; contained active/archive reads |
| Repo admission | `orch_hub/admission.py` | per-repo atomic run↔intake admission; persisted occupancy probes; structured conflicts |
| Evaluation | `orch_hub/evaluation.py` | evidence targets/packs; artifact persistence/retention/reads |
| AI snapshot evidence | `orch_hub/ai_snapshot.py` | producer-bounded routed `.ai/` docs and same-window patch evidence |
| Session read evidence | `orch_hub/session_reads.py` | bounded transcript-derived prefetch facts and ordered `.ai/` read traces |
| Static metrics | `orch_hub/static_metrics.py` | deterministic six-axis metrics over evidence packs; `static_metrics.json` |
| Semantic evaluator | `orch_hub/semantic.py` | evidence-bounded model evaluation incl. retrieval effectiveness; backend isolation; doctor |
| Completion evaluation | `orch_hub/completion_eval.py` | isolated completed-task trigger; off-lock Layer 1 only; no durable debt |
| Evaluation dispatch | `orch_hub/eval_dispatch.py` | read-only cross-repo Layer 2 attempt projection; legacy holds inert |
| Semantic jobs | `orch_hub/semantic_jobs.py` | one latest job/outcome per task; deadline reap, cancellation, manager-free reads |
| Report catalog | `orch_hub/report_catalog.py` | append-only global publication feed; reconciliation; retention guard; contained reads; distinct-task protocol-coverage census |
| Usage | `orch_hub/usage.py` | Claude cache parser/poller + fallback merging; Codex normalization; fail-soft |
| StatusLine writer | `orch_hub/statusline.py` | legacy optional statusLine fallback feeding the usage sink |
| Intake manager | `orch_hub/intake.py` | remote intake lifecycle/audit; off-lock turns; verified confirm |
| Intake agent | `orch_hub/intake_agent.py` | Claude CLI `/intake-task` backend with envelopes/resume |
| Run manager | `orch_hub/runs.py` | subprocess lifecycle/state/stop; per-repo serialization; terminal hooks; read-only protocol-coverage census |
| Orchestrator launch config | `orch_hub/orch_config.py` | `--print-config` query/parse; split/legacy argv; config snapshots |
| Run monitor | `orch_hub/monitor.py` | resident status scanner plus isolated retry sweeps |
| History | `orch_hub/history.py` | per-run JSONL event storage/readback |
| Control dir | `orch_hub/control_dir.py` | question/answer/stop file-channel handling |
| Logs | `orch_hub/logs.py` | log location, bounded tails, SSE frames |
| Sessions | `orch_hub/sessions.py` | per-session context parsing; `sessions.json` persistence |
| Session entry task snapshots | `orch_hub/task_snapshots.py` | post-claim task-file capture/miss accounting; `task_snapshots.json` persistence |
| API | `orch_hub/api.py` | FastAPI app factory; bearer middleware; route wiring; uvicorn entry |
| Notifications | `orch_hub/notifications.py` | notifier interface; Bark backend; formatters |
| Web UI | `orch_hub/web/` | mobile-first dashboard SPA assets |
| Launchd plist | `deploy/launchd/com.qinglin89.orch-hub.plist` | launchd user-agent template |
| Release installer template | `deploy/release/installer.sh` | POSIX self-extractor header and pre-unpack gates |
| Operator docs | `docs/` | launchd/release/evaluation/Claude usage operator docs |
| Tests | `tests/` | pytest coverage with fake repos and mocked subprocesses |

## Detail Files

| File | Contains |
|---|---|
| core.md | CLI, config, models, registry, readiness, git metadata, housekeeping, subprocess utilities, task inventory |
| evaluation.md | Evaluation foundation, AI snapshot/static/semantic layers, Layer 1 completion trigger, semantic jobs/attempt projection |
| report-catalog.md | Global report catalog publication |
| usage.md | Subscription usage collection, statusLine writer |
| intake.md | Intake manager, intake agent |
| runtime.md | Repo admission, run manager, orchestrator launch config, run monitor |
| runtime-io.md | History, control-dir, logs, sessions, session-entry task snapshots, notifications |
| service.md | API app, launchd operations |
| release.md | Frozen resources, install lifecycle, build/audit/package/publish modules |
| web.md | Web UI shell, theme, accessibility/route infrastructure, Overview |
| web-views.md | Web UI routed views: repo/task, run controls, run history, report feed, evaluation, intake |
| boundaries.md | Cross-module boundary rules (what each module must not do) |
