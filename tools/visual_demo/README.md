# Isolated visual demo

This maintainer tool runs the real orch-hub v0.1.0 FastAPI app and SPA against a
fresh synthetic portfolio. It exists to capture public screenshots and video
without exposing or mutating an operator environment.

```bash
python -m tools.visual_demo.server --port 8877
```

Open `http://127.0.0.1:8877/` and sign in with `orch-hub-demo`.

## Safety boundary

Every invocation creates an `orch-hub-visual-demo-*` directory under the system
temporary directory. The process binds to loopback and removes that directory on
exit. The synthetic repo names are `acme-web`, `payments-api`, and
`infra-config`.

The demo injects deterministic replacements for every boundary that could touch
operator state or incur external effects:

- repository discovery and readiness;
- orchestrator process spawning and the control-dir conversation;
- remote task intake;
- semantic model evaluation;
- Claude and Codex usage collection;
- notifications and CLI-home reads;
- Git history and diff reads.

The application routes, task parser, run manager, control-dir answer path,
history snapshots, evaluation artifact store, report catalog, and unmodified SPA
remain real. The `ISOLATED DEMO` badge is injected into a temporary copy of the
shell, never into the product assets.

The server refuses non-loopback hosts. Cleanup also refuses any path outside the
system temporary directory or without the visual-demo prefix.

## Suggested capture story

1. Operations overview: portfolio readiness, recent outcomes, and subscription
   capacity in one place.
2. Open `acme-web` and the `2026-08-20-audit-ready-deployments` task.
3. Open Start run, choose Excellent for DEV, Standard for REVIEW, and enable the
   plan gate.
4. Launch, wait for the plan question, then confirm it.
5. Open Run history to inspect configuration, sessions, context, and audit events.
6. Open the task's Evaluation, generate deterministic metrics, then run the
   deterministic demo semantic evaluator.
7. Open All evaluation reports to show the cross-repository audit feed.

The semantic backend is intentionally labeled `demo-fixture` /
`deterministic-demo-evaluator`; screenshots prove the product flow and artifact
model, not the quality or cost of a real model call.

## Render a privacy-safe walkthrough

For native high-resolution sources, start a disposable headless Chrome instance
with a loopback-only debugging port and a new temporary profile, then run:

```bash
node tools/visual_demo/capture_hires.mjs \
  --base-url http://127.0.0.1:8877/ \
  --cdp-url http://127.0.0.1:9333/ \
  --output dist/visual-demo-hires/screenshots
```

The capture script requires the `playwright` Node package. It fixes the CSS
viewport at 1440x900 with a 2x device scale, producing direct 2880x1800 PNGs.
It also rejects non-loopback base/CDP URLs and aborts every browser request that
does not target loopback (or an inline `data:` resource).

After capturing the six app-only PNGs above as `01-overview.png` through
`06-report-feed.png`, render a 1920x1080 H.264 walkthrough locally:

```bash
swift tools/visual_demo/render_video.swift \
  --screenshots dist/visual-demo-hires/screenshots \
  --output dist/visual-demo-hires/orch-hub-demo-1080p.mp4
```

The renderer downsamples the sources without cropping, zooming, or panning. It
uses high-quality interpolation, 16 Mbps H.264, crossfades, and vector-rendered
captions. It never records the desktop, browser chrome, notifications,
microphone, or other tabs. The output is a captioned visual walkthrough, not a
claim of a continuous live screen recording.
