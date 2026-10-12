import tomllib
from pathlib import Path

import ifm_advisor

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_0_1_0():
    assert ifm_advisor.__version__ == "0.1.0"


def test_runtime_dependencies_are_only_pydantic_and_litellm():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    names = sorted(requirement.split(">=")[0] for requirement in project["dependencies"])

    assert names == ["litellm", "pydantic"]


def test_public_api():
    expected = {
        "Advisor",
        "AdvisorConfig",
        "AdvisorDecision",
        "AdvisorDecisionError",
        "AdvisorRequestError",
        "EndpointConfig",
        "ReasoningPrefillCue",
        "Regenerate",
        "Revision",
        "SelfCheckCue",
        "TaskLayout",
        "TaskMaterial",
        "litellm_hook",
    }

    assert set(ifm_advisor.__all__) == expected
    for name in expected:
        assert hasattr(ifm_advisor, name), name


def test_the_distribution_and_command_are_named_ifm_advisor():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "ifm-advisor"
    assert project["scripts"] == {"ifm-advisor": "ifm_advisor.cli:main"}


def test_the_package_ships_its_typing_marker():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert (ROOT / "src" / "ifm_advisor" / "py.typed").is_file()
    assert "py.typed" in package_data["ifm_advisor"]
    assert "Typing :: Typed" in pyproject["project"]["classifiers"]


def test_the_sdist_leaves_out_the_tests():
    """The tests need docs/, README.md and fixtures from a checkout, so they run from one."""

    assert "prune tests" in (ROOT / "MANIFEST.in").read_text().splitlines()
