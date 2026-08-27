---
last-updated: 2026-08-27
verified-against: c86cb40b5f7880c1876147c32c428fd8ef51d474
---

# Features

Catalog + routing only. Per-feature Modules/APIs/key-decision cross-reference
is owned by `.ai/map.md`; full per-feature behavior lives in the detail files
below.

| Feature | Status | Capability | Detail |
|---|---|---|---|
| Repo discovery and readiness | implemented | registry-driven repo list + fail-closed launch readiness + display-only commit/housekeeping metadata | core.md |
| Task inventory | implemented | read-only `.ai-tasks` index/frontmatter inventory | core.md |
| HTTP API foundation | implemented | FastAPI app: public shell/health + protected data routes | core.md |
| Auth and private access | implemented | fail-closed bearer middleware; Tailscale-only exposure | core.md |
| Task evaluation foundation | implemented | bounded read-only evidence packs for active/archived tasks | evaluation.md |
| Task static metrics | implemented | deterministic Layer 1 score report beside the evidence pack | evaluation.md |
| Task semantic evaluator | implemented | completion-triggered L1 + capacity-held async Layer 2 with recorded opt-out | evaluation-reporting.md |
| Global evaluation report feed | implemented | append-only cross-repo publication catalog + verified reads | evaluation-reporting.md |
| Run lifecycle | implemented | admission-gated orchestrator subprocess start/stop/reattach | run-control.md |
| Control-dir file channel | implemented | question/answer/stop file adapter | run-control.md |
| Human question relay | implemented | remote answering of waiting control-dir questions | run-control.md |
| Log viewing | implemented | bounded tails + SSE streaming located from run records | run-observability.md |
| Run history and audit | implemented | per-run metadata events, paged history, persisted snapshots | run-observability.md |
| Mobile dashboard | implemented | mobile-first SPA over the protected APIs | dashboard.md, dashboard-views.md |
| Agent subscription usage | implemented | Claude/Codex capacity display; automatic-L2 pacing is the sole policy consumer | usage.md |
| Notifications | implemented | best-effort Bark pushes (questions, run end, intake, semantic jobs) | ops.md |
| Launchd service | implemented | resident macOS service packaging + ops CLI | ops.md |
| Compiled release delivery | implemented | source-minimized darwin-arm64 runtime + versioned install/rollback + immutable artifact publication | ops.md |
| Remote intake-task | implemented | confirmed remote task drafting into target repos | intake.md |
| IM bot control | future | later IM surface (planned: notifications or bot adapter + api; future bot callback interface) | ops.md |

## Detail Files

| File | Contains |
|---|---|
| core.md | Repo discovery/readiness, task inventory, HTTP foundation, auth/private access |
| evaluation.md | Task evaluation foundation, task static metrics (Layer 1) |
| evaluation-reporting.md | Automatic completion evaluation, semantic evaluator (Layer 2), queue/skip visibility, global report feed |
| run-control.md | Run lifecycle, control-dir channel, question relay |
| run-observability.md | Log viewing, run history/audit |
| dashboard.md | Mobile dashboard shell, theme, responsiveness, accessibility, Overview, route infrastructure |
| dashboard-views.md | Mobile dashboard routed view catalog: repo/task/start/history/log/evaluation/report-feed/intake |
| ops.md | Notifications, launchd service, compiled release delivery, future IM bot surface |
| usage.md | Agent subscription usage display: Claude cache/collector, Codex rollouts, statusLine fallback |
| intake.md | Remote intake-task: admission, draft/revise/confirm/cancel lifecycle |
