from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

_DEMO_ENV_NAMES = (
    "ORCH_HUB_MANDREL_PATH",
    "ORCH_HUB_DATA_DIR",
    "ORCH_HUB_CODEX_HOME",
    "ORCH_HUB_CLAUDE_HOME",
    "ORCH_HUB_TOKEN",
    "ORCH_HUB_BARK_URL",
)
_ENV_BEFORE_DEMO_IMPORT = {name: os.environ.get(name) for name in _DEMO_ENV_NAMES}

from tools.visual_demo.server import (
    DEMO_PREFIX,
    DEMO_TOKEN,
    MAIN_REPO,
    MAIN_TASK,
    build_demo_runtime,
)

_ENV_AFTER_DEMO_IMPORT = {name: os.environ.get(name) for name in _DEMO_ENV_NAMES}


def test_visual_demo_import_restores_operator_environment():
    assert _ENV_AFTER_DEMO_IMPORT == _ENV_BEFORE_DEMO_IMPORT


def test_visual_demo_is_temp_only_and_serves_synthetic_portfolio():
    runtime = build_demo_runtime()
    root = runtime.root
    try:
        assert root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        assert root.name.startswith(DEMO_PREFIX)
        assert runtime.config.data_dir.is_relative_to(root)
        assert runtime.config.codex_home.is_relative_to(root)
        assert runtime.config.claude_home.is_relative_to(root)
        assert all(repo.path.is_relative_to(root) for repo in runtime.repos)

        with TestClient(runtime.app) as client:
            headers = {"Authorization": f"Bearer {DEMO_TOKEN}"}
            repos = client.get("/api/repos", headers=headers)
            assert repos.status_code == 200
            assert {entry["name"] for entry in repos.json()["repos"]} == {
                "acme-web",
                "payments-api",
                "infra-config",
            }
            assert all(
                entry["readiness"]["ready"] for entry in repos.json()["repos"]
            )

            tasks = client.get("/api/tasks", headers=headers).json()
            inventory = next(
                entry
                for entry in tasks["repos"]
                if entry["name"] == MAIN_REPO
            )
            assert any(
                task["task_id"] == MAIN_TASK for task in inventory["tasks"]
            )

            usage = client.get("/api/usage", headers=headers).json()
            assert [provider["provider"] for provider in usage["providers"]] == [
                "claude",
                "codex",
            ]

            history = client.get("/api/history/runs", headers=headers).json()
            assert history["total_runs"] == 3
            assert all(str(root) in run["repo_path"] for run in history["runs"])

            reports = client.get(
                "/api/evaluation/reports", headers=headers
            ).json()
            assert len(reports["reports"]) == 3
            assert all(
                report["evaluator"]["backend"] == "demo-fixture"
                for report in reports["reports"]
            )

            seeded_task = "2026-08-15-deployment-policy-drift"
            evaluations = client.get(
                f"/api/repos/infra-config/evaluations/{seeded_task}",
                headers=headers,
            ).json()
            evaluation_id = evaluations["latest_evaluation_id"]
            detail = client.get(
                f"/api/repos/infra-config/evaluations/{seeded_task}/"
                f"{evaluation_id}",
                headers=headers,
            ).json()
            protocol_score = next(
                score
                for score in detail["static_metrics"]["scores"]
                if score["name"] == "protocol"
            )
            assert protocol_score["score"] == 1
    finally:
        runtime.close()
    assert not root.exists()


def test_visual_demo_shell_is_marked_but_product_shell_is_not_modified():
    runtime = build_demo_runtime()
    try:
        with TestClient(runtime.app) as client:
            assert "isolated demo" in client.get("/").text.lower()
        product_shell = (
            Path(__file__).parents[1] / "orch_hub" / "web" / "index.html"
        )
        assert "isolated demo" not in product_shell.read_text(
            encoding="utf-8"
        ).lower()
    finally:
        runtime.close()
