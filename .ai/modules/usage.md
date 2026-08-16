---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Modules: Subscription Usage Display

Display-only Claude/Codex capacity collection. Semantic jobs: `evaluation.md`.
Foundation modules: `core.md`.

## Usage

Location: `orch_hub/usage.py`

Responsibility: produce provider-normalized, read-only Claude/Codex
subscription usage for display. Codex usage is read from the newest local
`rate_limits` snapshots in `<codex_home>/sessions/**/rollout-*.jsonl`, bounded
by file count and size, with optional primary/secondary windows and aggregate
credits sanitized by whitelist. Claude's primary carrier is the CLI's
`~/.claude.json.cachedUsageUtilization`: `parse_claude_usage_cache` retains only
`fetchedAtMs`, the two window reset instants, and `limits[].percent` figures
paired by kind and equal normalized reset; ambiguous window-map
`utilization`, account ids, credentials, projects, and machine fields are
ignored. `ClaudeUsageCollector` reads that cache and refreshes it through
`claude -p "/usage"` from an empty cwd. Its non-blocking lock, five-minute
freshness/attempt floors, 60s timeout, and mandatory zero-turn/zero-cost JSON
envelope make refresh bounded, single-flight, and model-free; failures return
the old observation with its original timestamp.

`UsageService` ingests this cache first and the optional
`<data_dir>/usage/claude-statusline.json` fallback second, merging both into
`<data_dir>/usage/claude.json` per window. `merge_observations` keys each figure
by store-internal `UsageWindow.figure_instant`; known reset instants within five
minutes are one jitter-tolerant identity whose stored instant converges forward,
while farther-apart instants retain genuine-rollover ordering. Same-window
figures take `max(used_percent)`, legacy stores self-heal, and partial merges
floor provider freshness to the older source. Stale/empty reads dispatch a
background collector refresh for the next read; the app also gives `RunManager`
this request callable for each observed terminal transition. No verified
stream-json version carries a percentage;
2.1.220's percentage-less event may advance only a stored reset boundary.
`ClaudeUsageStore.record()` writes via per-writer temp files plus `os.replace`
so concurrent observations cannot collide; `UsageService` serializes Claude
store read-modify-write paths. It aggregates Claude then Codex behind a 30s
cache and returns fail-soft
`ProviderUsage.unavailable(...)` snapshots on missing/failed sources. It never
starts a model turn, directly calls provider endpoints, opens credential files,
spends Codex reset credits, or participates in operational gates.

## StatusLine Writer

Location: `orch_hub/statusline.py`

Responsibility: retain legacy console script `orch-hub-statusline` for an
operator-owned Claude Code `statusLine.command` installation. It reads stdin,
persists only the `rate_limits` container plus `subscription_type` to
`<data_dir>/usage/claude-statusline.json`, then runs everything after `--` as
the operator's wrapped statusline argv with the same stdin and inherited
stdout/stderr/exit code. It deliberately avoids `Config.from_env()` validation,
never edits `~/.claude/**`, never starts Claude or calls provider endpoints, and
drops session id, transcript path, cwd, model, workspace, and cost fields before
writing the sink. It owns no setup detection or guidance; deploy/update do not
inspect Claude settings, and the wrapper is optional because the cache carrier
is primary.
