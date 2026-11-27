"""Repository hygiene: CI workflows and dependency declarations."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$", re.M)


def test_workflow_actions_are_pinned_to_commits():
    assert WORKFLOWS
    for workflow in WORKFLOWS:
        for action, comment in USES.findall(workflow.read_text()):
            where = f"{workflow.name}: {action}"
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), where
            assert re.search(r"# v\d+\.\d+\.\d+", comment), f"{where} has no version"


def test_workflows_default_to_read_only_permissions():
    for workflow in WORKFLOWS:
        assert re.search(r"^permissions:\n  contents: read\n(?!  )", workflow.read_text(), re.M), (
            workflow.name
        )


def test_ci_cancels_superseded_runs_and_checks_types_coverage_and_diagrams():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "cancel-in-progress: true" in ci
    assert "run: mypy" in ci
    assert "--cov-fail-under=90" in ci
    assert "scripts/check_mermaid.py" in ci


JOB = re.compile(r"^  ([\w-]+):\n((?:(?:    .*)?\n)*)", re.M)
JOB_TIMEOUTS = {
    "ci.yml": {"test": 30, "diagrams": 15},
    "docs.yml": {"build": 15, "deploy": 10},
}


def _jobs(workflow: Path) -> dict[str, str]:
    text = workflow.read_text()
    return dict(JOB.findall(text[text.index("\njobs:\n") :]))


def test_every_job_has_a_timeout():
    assert sorted(path.name for path in WORKFLOWS) == sorted(JOB_TIMEOUTS)
    for workflow in WORKFLOWS:
        jobs = _jobs(workflow)
        assert sorted(jobs) == sorted(JOB_TIMEOUTS[workflow.name]), workflow.name
        for name, minutes in JOB_TIMEOUTS[workflow.name].items():
            assert re.search(rf"^    timeout-minutes: {minutes}$", jobs[name], re.M), (
                f"{workflow.name}: {name}"
            )


def test_checkouts_do_not_persist_credentials():
    checkout = re.compile(
        r"^(\s*)- uses: actions/checkout@\S+.*\n\1  with:\n\1    persist-credentials: false$", re.M
    )
    for workflow in WORKFLOWS:
        text = workflow.read_text()
        assert len(checkout.findall(text)) == text.count("uses: actions/checkout@"), workflow.name


def test_dependabot_watches_actions_and_pip():
    config = (ROOT / ".github" / "dependabot.yml").read_text()

    assert "package-ecosystem: github-actions" in config and "package-ecosystem: pip" in config


def test_runtime_dependencies_have_upper_bounds():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert sorted(project["dependencies"]) == ["litellm>=1.95,<2", "pydantic>=2.11,<3"]


def test_there_is_a_security_policy():
    policy = (ROOT / "SECURITY.md").read_text()

    assert "Report a vulnerability" in policy and "private_trace.jsonl" in policy


def test_the_documentation_says_how_to_cite_the_project():
    # CITATION.cff and the README's citation section were removed; the docs home page still has one
    text = (ROOT / "docs" / "index.md").read_text()

    assert "## Citation" in text
    assert "author       = {Pimpalkhute, Varad and Sun, Yuekai and Yurochkin, Mikhail}" in text
    assert "title        = {Advisor: A Privileged Advisor for Agentic Coding Rollouts}" in text
