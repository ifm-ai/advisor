"""Shared fixtures."""

from __future__ import annotations

import pytest

from ifm_advisor.config import AdvisorConfig, EndpointConfig
from ifm_advisor.material import TaskMaterial


@pytest.fixture
def task_dir(tmp_path):
    """A minimal Harbor-shaped task directory with public, privileged and excluded files."""

    root = tmp_path / "session-window-debug"
    (root / "solution" / "files").mkdir(parents=True)
    (root / "tests" / "baseline").mkdir(parents=True)
    (root / "cheat" / "files").mkdir(parents=True)
    (root / "environment").mkdir()

    (root / "instruction.md").write_text("Fix the session window processor.\n")
    (root / "task.toml").write_text('schema_version = "1.1"\n')
    (root / "README.md").write_text("## Solution explanation\nFive targeted fixes.\n")
    (root / "solution" / "solve.sh").write_text("#!/bin/bash\npatch -p1 <<'EOF'\nEOF\n")
    (root / "solution" / "files" / "gc.py").write_text("REFERENCE_GC_IMPLEMENTATION = 1\n")
    (root / "tests" / "test_outputs.py").write_text("def test_unfired_survives_gc(): ...\n")
    (root / "tests" / "baseline" / "types.py").write_text("BASELINE = 1\n")
    (root / "cheat" / "solve.sh").write_text("CHEAT_MARKER\n")
    (root / "cheat" / "files" / "merger.py").write_text("CHEAT_MARKER\n")
    (root / "environment" / "Dockerfile").write_text("FROM python:3.12\n")
    return root


@pytest.fixture
def advisor_config() -> AdvisorConfig:
    return AdvisorConfig(
        endpoint=EndpointConfig(
            model="advisor-model",
            api_base="https://advisor.example/v1",
            api_key_env="TEST_ADVISOR_API_KEY",
        ),
        max_interventions=2,
    )


@pytest.fixture(autouse=True)
def advisor_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ADVISOR_API_KEY", "test-advisor-key")


@pytest.fixture
def task(task_dir) -> TaskMaterial:
    return TaskMaterial.from_task_dir(task_dir)
