import fnmatch
import json
import tomllib
from importlib import resources
from pathlib import Path

import pytest
from helpers import finish_response

from ifm_advisor.errors import AdvisorRequestError
from ifm_advisor.material import TaskMaterial
from ifm_advisor.prompting import (
    PROFILE_MARKER,
    build_advisor_messages,
    load_system_prompt,
    render_public_conversation,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "ifm_advisor.prompts"


def _build(task, **overrides):
    arguments = {
        "system_prompt": load_system_prompt(),
        "task": task,
        "solver_messages": [{"role": "user", "content": "task"}],
        "draft_response": finish_response(),
        "finish_tool_name": "complete",
    }
    arguments.update(overrides)
    return build_advisor_messages(**arguments)


def test_k2_horizon_prompt_is_the_paper_prompt_byte_for_byte():
    paper = (ROOT / "tests" / "fixtures" / "pre_submit_v1.txt").read_text(encoding="utf-8")
    expected = paper.replace(
        "of this model's own reasoning blocks", "of K2-Horizon's own reasoning blocks"
    ).replace("vanilla runs of this model.", "vanilla runs of K2-Horizon.")

    assert load_system_prompt("advisor_v1", "k2_horizon") == expected


def test_generic_profile_has_no_model_statistics():
    prompt = load_system_prompt("advisor_v1", "generic")

    assert PROFILE_MARKER.strip() not in prompt
    assert "WRITE IN THE SOLVER'S REGISTER, NOT YOURS." in prompt
    assert "1.26M" not in prompt
    assert "K2-Horizon" not in prompt


@pytest.mark.parametrize("profile", ["generic", "k2_horizon"])
def test_every_profile_forbids_second_person_and_naming_the_review(profile):
    prompt = load_system_prompt("advisor_v1", profile)

    assert 'No second person, no "the user", no reference to a review' in prompt


def test_profile_can_be_read_from_a_path(tmp_path):
    custom = tmp_path / "my_solver.txt"
    custom.write_text("  WRITE IN THE SOLVER'S REGISTER, NOT YOURS.\n- CUSTOM-PROFILE-MARKER\n")

    assert "CUSTOM-PROFILE-MARKER" in load_system_prompt("advisor_v1", custom)


@pytest.mark.parametrize(
    ("version", "profile"),
    [("does_not_exist_v99", "generic"), ("advisor_v1", "no_such_profile")],
)
def test_unknown_prompt_or_profile_raises(version, profile):
    with pytest.raises(AdvisorRequestError, match="unavailable"):
        load_system_prompt(version, profile)


def test_unreadable_profile_path_raises(tmp_path):
    with pytest.raises(AdvisorRequestError, match="unreadable"):
        load_system_prompt("advisor_v1", tmp_path / "missing.txt")


def test_core_prompt_never_lets_a_finish_through_for_lack_of_a_new_argument():
    prompt = load_system_prompt()

    assert "Never trade a correct verdict for a novel one" in prompt
    assert "out of interventions" in prompt
    assert "never applies when a finish is on the table" in prompt


def test_core_prompt_forbids_the_plan_from_pre_committing_a_later_verdict():
    prompt = load_system_prompt()

    assert "records task state, never a future verdict" in prompt
    assert "no exit criterion" in prompt
    assert "ignore that sentence" in prompt
    assert "governs what you say, never whether you block" in prompt


def test_core_prompt_keeps_the_private_fields_private():
    prompt = load_system_prompt()

    assert "reaches no one but the private log" in prompt
    assert "CONTINUE is the right answer" in prompt
    assert "exactly one JSON object" in prompt


def test_every_prompt_file_is_declared_as_package_data():
    """A source checkout cannot notice a missing package-data declaration; a wheel would."""

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["tool"]["setuptools"]["package-data"][PACKAGE]
    prompts_dir = ROOT / "src" / "ifm_advisor" / "prompts"
    files = [path.relative_to(prompts_dir).as_posix() for path in prompts_dir.rglob("*.txt")]

    assert sorted(files) == [
        "advisor_v1.txt",
        "solver_profiles/generic.txt",
        "solver_profiles/k2_horizon.txt",
    ]
    for name in files:
        assert any(fnmatch.fnmatch(name, pattern) for pattern in declared), name


def test_prompts_package_is_importable_so_setuptools_finds_it():
    assert resources.files(PACKAGE).joinpath("__init__.py").is_file()


def test_request_has_every_section_and_carries_privileged_material(task):
    messages = _build(task)

    serialized = json.dumps(messages)
    assert "REFERENCE_GC_IMPLEMENTATION" in serialized
    assert "CHEAT_MARKER" not in serialized
    for section in (
        "=== REVIEW TRIGGER ===",
        "=== PUBLIC TASK ===",
        "=== PUBLIC CONVERSATION ===",
        "=== SOLVER'S LATEST TURN ===",
        "=== PRIVILEGED REFERENCE MATERIAL ===",
        "=== REQUIRED JSON RESPONSE SCHEMA ===",
    ):
        assert section in serialized
    assert messages[0] == {"role": "system", "content": load_system_prompt()}
    assert "Fix the session window processor." in messages[1]["content"]


def test_no_previous_cues_section_before_the_first_intervention(task):
    """The system prompt names the section unconditionally; the request must not."""

    assert "HINTS YOU HAVE ALREADY DELIVERED" not in _build(task, previous_cues=[])[1]["content"]


def test_previous_cues_are_shown_back_to_the_advisor(task):
    content = _build(task, previous_cues=["the fired guard is missing", "created_at is stale"])[1][
        "content"
    ]

    assert "=== HINTS YOU HAVE ALREADY DELIVERED THIS ATTEMPT ===" in content
    assert "You have intervened 2 time(s) so far." in content
    assert "--- hint 1 ---\nthe fired guard is missing" in content
    assert "--- hint 2 ---\ncreated_at is stale" in content


def test_previous_cues_are_bounded_and_report_what_was_dropped(task):
    content = _build(task, previous_cues=[f"hint number {index}" for index in range(1, 13)])[1][
        "content"
    ]

    assert "You have intervened 12 time(s) so far." in content
    assert "...[4 earlier hint(s) omitted]" in content
    assert "hint number 1\n" not in content
    assert "--- hint 5 ---\nhint number 5" in content
    assert "--- hint 12 ---\nhint number 12" in content


def test_previous_cue_limit_is_configurable(task):
    content = _build(task, previous_cues=["a", "b", "c"], max_previous_cues=1)[1]["content"]

    assert "...[2 earlier hint(s) omitted]" in content
    assert "--- hint 3 ---\nc" in content


def test_plan_and_repeat_sections_appear_only_when_given(task):
    plain = _build(task)[1]["content"]
    rich = _build(task, previous_plan="step 1 done", repeat_reason_code="OTHER", repeat_streak=2)[
        1
    ]["content"]

    assert "=== YOUR PREVIOUS PLAN ===" not in plain and "=== REPEAT SIGNAL ===" not in plain
    assert "=== YOUR PREVIOUS PLAN ===\nstep 1 done" in rich
    assert "reason_code 'OTHER' has now recurred across 2 consecutive review(s)" in rich


def test_truncated_material_is_flagged_in_the_request():
    task = TaskMaterial.from_texts("x", {"a": "A" * 10}, max_file_bytes=4, max_total_bytes=4)

    assert "privileged material was size-limited. Truncated: ['a']" in _build(task)[1]["content"]


def test_long_conversations_keep_the_opening_and_the_recent_tail():
    messages = [{"role": "user", "content": f"message {index}"} for index in range(10)]

    rendered = render_public_conversation(messages, max_messages=5)

    assert "message 0" in rendered and "message 1" in rendered
    assert "...[5 earlier messages omitted]" in rendered
    assert "message 2\n" not in rendered
    assert "message 9" in rendered


def test_each_message_is_clipped():
    rendered = render_public_conversation([{"role": "tool", "content": "x" * 50}], max_chars=10)

    assert "x" * 10 + "\n...[40 characters omitted]" in rendered
