import os
import py_compile
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ifm_advisor import TaskMaterial

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
TOY = EXAMPLES / "tasks" / "toy-task"


def test_offline_quickstart_runs_end_to_end():
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "01_offline_quickstart.py")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("committed the revision") == 2
    assert "kept the draft" in result.stdout
    assert "solver submitted." in result.stdout
    assert '"interventions": 2' in result.stdout


@pytest.mark.parametrize(
    "name", ["02_custom_agent_loop.py", "03_harbor_mini_swe_agent.py", "check_reasoning_prefill.py"]
)
def test_other_examples_compile(name, tmp_path):
    py_compile.compile(str(EXAMPLES / name), cfile=str(tmp_path / "compiled.pyc"), doraise=True)


def test_toy_task_loads_with_the_default_layout():
    material = TaskMaterial.from_task_dir(TOY)

    assert material.name == "toy-task"
    assert "median" in material.instruction
    assert [entry.relative_path for entry in material.files] == [
        "README.md",
        "solution/solve.sh",
        "tests/test.sh",
        "tests/test_outputs.py",
    ]


def _reference_stats() -> str:
    solve = (TOY / "solution" / "solve.sh").read_text()
    return re.search(r"<<'PY'\n(.*?)\nPY\n", solve, re.S).group(1) + "\n"


@pytest.mark.parametrize(("source", "should_pass"), [("reference", True), ("starting code", False)])
def test_hidden_tests_separate_the_reference_from_the_starting_code(tmp_path, source, should_pass):
    code = (
        _reference_stats()
        if source == "reference"
        else (TOY / "environment" / "workspace" / "stats.py").read_text()
    )
    (tmp_path / "stats.py").write_text(code)
    shutil.copy(TOY / "tests" / "test_outputs.py", tmp_path / "test_outputs.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "pytest.ini"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert (result.returncode == 0) is should_pass, result.stdout
