import pytest

from ifm_advisor.config import ReasoningPrefillCue, SelfCheckCue
from ifm_advisor.cues import (
    SAFE_INSTRUCTIONS,
    compile_cue,
    compile_self_check_instruction,
)
from ifm_advisor.decision import AdvisorAction, AdvisorDecision, AdvisorDecisionError

HINT = "Hmm. The odd-length case passes, but nothing shows what an even-length list returns."


def _decision(action: str = "RUN_BROADER_VALIDATION", hint: str = HINT) -> AdvisorDecision:
    return AdvisorDecision(
        intervene=True,
        confidence=0.9,
        reason_code="OTHER",
        action=action,
        plan="",
        hint=hint,
        private_diagnosis="",
    )


@pytest.mark.parametrize("action", sorted(SAFE_INSTRUCTIONS))
def test_every_actionable_action_compiles_to_fixed_text(action):
    instruction = compile_self_check_instruction(action)

    assert SAFE_INSTRUCTIONS[action] in instruction
    assert compile_self_check_instruction(action) == instruction


def test_every_non_continue_action_has_a_compiled_instruction():
    assert set(AdvisorAction.__args__) - {"CONTINUE"} == set(SAFE_INSTRUCTIONS)


@pytest.mark.parametrize("action", ["CONTINUE", "", "UNKNOWN_ACTION"])
def test_unmapped_actions_never_produce_solver_text(action):
    with pytest.raises(AdvisorDecisionError):
        compile_self_check_instruction(action)


def test_compiled_instructions_leak_nothing_task_specific():
    forbidden = (
        "test_",
        "assert",
        "gc.py",
        "merger.py",
        "events.py",
        "watermark",
        "retraction",
        "reference",
        "solution",
        "hidden",
        "advis",
    )
    for action, text in SAFE_INSTRUCTIONS.items():
        for marker in forbidden:
            assert marker not in text.lower(), (action, marker)


def test_compiled_instruction_tells_the_solver_not_to_disclose():
    lowered = compile_self_check_instruction("REVIEW_ALL_REQUIREMENTS").lower()

    assert "do not restate or refer to this message" in lowered
    assert "anywhere other than your own investigation" in lowered
    assert "another tool call" in lowered
    assert "independently conclude" in lowered


@pytest.mark.parametrize("action", sorted(SAFE_INSTRUCTIONS))
def test_compiled_instruction_never_names_the_review_mechanism(action):
    lowered = compile_self_check_instruction(action).lower()

    for word in ("advisor", "reviewer", "review", "hint", "self-check", "guidance"):
        assert word not in lowered, (action, word)


def test_self_check_cue_is_one_user_message_and_ignores_the_hint():
    cue = compile_cue(SelfCheckCue(), _decision("TRACE_STATE_TRANSITIONS"), intervention_index=0)

    expected = compile_self_check_instruction("TRACE_STATE_TRANSITIONS")
    assert cue.message == {"role": "user", "content": expected}
    assert cue.extra_body == {}
    assert cue.delivered_text == expected
    assert cue.prefill == ""
    assert HINT not in cue.message["content"]


def test_self_check_cue_rejects_continue():
    with pytest.raises(AdvisorDecisionError):
        compile_cue(SelfCheckCue(), _decision("CONTINUE"), intervention_index=0)


def test_prefill_cue_opens_an_assistant_turn_with_the_hint():
    cue = compile_cue(ReasoningPrefillCue.k2_horizon_sglang(), _decision(), intervention_index=0)

    assert cue.message == {"role": "assistant", "content": HINT}
    assert cue.extra_body == {"continue_final_message": True}
    assert cue.delivered_text == HINT
    assert cue.prefill == HINT
    assert cue.continuation_suffix == ""


def test_prefix_goes_in_the_message_but_not_in_the_spliced_reasoning():
    cue = compile_cue(ReasoningPrefillCue(prefix="<think>\n"), _decision(), intervention_index=0)

    assert cue.message["content"] == "<think>\n" + HINT
    assert cue.prefill == HINT


def test_continuation_suffixes_rotate_by_intervention_index():
    config = ReasoningPrefillCue(continuation_suffixes=("Let me think.", "Hmm."))
    hint = "The retention check still reclaims a session that never fired.  "

    contents = [
        compile_cue(config, _decision(hint=hint), intervention_index=index).message["content"]
        for index in range(3)
    ]

    assert contents == [
        hint.rstrip() + " Let me think.",
        hint.rstrip() + " Hmm.",
        hint.rstrip() + " Let me think.",
    ]
